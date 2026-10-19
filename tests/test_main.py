"""Arranque de la app (main.py): montaje, registro, recuperación y errores no capturados.

Sin red y sin audio real: salida `FakeOutput`, motores falsos, sin descargador y una
bandeja falsa. Todas las esperas tienen tope.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from logging.handlers import RotatingFileHandler

import pytest
from PySide6.QtCore import QTimer

import main
from lector.errors import AudioOutputError
from lector.models import ChapterRef, Document, Position, SourceKind
from lector.persistence.database import open_database
from lector.persistence.repositories import DocumentRepo, ProgressRecord, ProgressRepo
from lector.services.reading_controller import SessionState
from lector.ui.tray import TrayController
from tests.fakes import FakeEdge, FakeOutput, FakePiper
from tests.test_tray import FakeTray

WAIT_MS = 3000


class StubEdge(FakeEdge):
    def __init__(self):
        super().__init__()
        self.known: list[dict] | None = None

    def set_known_voices(self, voices):
        self.known = voices


class Built:
    """Lo que una prueba necesita además del contexto: motores y salidas creadas."""

    def __init__(self):
        self.edge = StubEdge()
        self.piper = FakePiper()
        self.outputs: list[FakeOutput] = []
        self.quit_calls = 0


@pytest.fixture
def make_app(qapp, tmp_path):
    contexts = []

    def build(*, tray: bool = True, output_factory=None, **kwargs):
        built = Built()

        def outputs():
            out = FakeOutput()
            built.outputs.append(out)
            return out

        def tray_factory(window, bridge, worker, controller, icon):
            if not tray:
                return None

            def quit_app():
                built.quit_calls += 1

            return TrayController(window, bridge, worker, controller, icon,
                                  tray=FakeTray(), quit_app=quit_app)

        ctx = main.build_app(
            [],
            data_dir=tmp_path,
            output_factory=output_factory or outputs,
            fetcher_factory=lambda settings: None,
            engine_factory=lambda settings, models: (built.edge, built.piper),
            tray_factory=tray_factory,
            **kwargs,
        )
        contexts.append(ctx)
        return ctx, built

    yield build
    for ctx in contexts:
        ctx.quit()
        ctx.close()


def seed_progress(tmp_path) -> None:
    """Una lectura a medias guardada en la base de datos de la app."""
    conn = open_database(tmp_path / "lector.db")
    chapters = [ChapterRef(0, "Capítulo 1", "spine:c1.xhtml"),
                ChapterRef(1, "Capítulo 2", "spine:c2.xhtml")]
    DocumentRepo(conn).upsert(
        Document("doc1", SourceKind.EPUB, "/x/novela.epub", "Mi novela", None, None, "h",
                 chapters),
        None, 1, 1.0,
    )
    ProgressRepo(conn).save(ProgressRecord(
        "doc1", SourceKind.EPUB, "/x/novela.epub", "Mi novela", 100.0,
        Position(1, "spine:c2.xhtml", 3, 7, 0, None), None, None,
        "es-MX-DaliaNeural", "es_ES-sharvard-medium#F", 1.0, "reading",
    ))
    conn.close()


def spy_shutdown(ctx) -> list:
    calls = []

    async def shutdown(save_only=False):
        calls.append(save_only)

    ctx.controller.shutdown = shutdown
    return calls


def worker_alive(ctx) -> bool:
    future = ctx.worker.submit(asyncio.sleep(0, result="ok"))
    return future.result(timeout=2) == "ok"


# --- arranque y salida --------------------------------------------------------------------


def test_build_app_starts_and_quits_cleanly(make_app, qtbot):
    before = set(threading.enumerate())
    ctx, built = make_app()

    assert ctx.window.isVisible()
    assert ctx.tray is not None
    assert ctx.app.quitOnLastWindowClosed() is False
    assert ctx.controller.state is SessionState.IDLE
    assert any(t.name == "lector-worker" for t in threading.enumerate())
    assert worker_alive(ctx)

    ctx.quit()
    ctx.close()

    assert built.quit_calls == 1
    with pytest.raises(RuntimeError):
        ctx.worker.submit(asyncio.sleep(0))
    qtbot.waitUntil(
        lambda: not [t for t in threading.enumerate() if t not in before and t.is_alive()],
        timeout=WAIT_MS,
    )


def test_log_file_is_created_in_data_dir(make_app, tmp_path):
    ctx, _ = make_app()
    logging.getLogger("lector.prueba").info("línea de prueba con eñe")
    handler = ctx.log_handler
    handler.flush()

    assert ctx.log_path == tmp_path / "lector.log"
    assert isinstance(handler, RotatingFileHandler)
    assert handler.maxBytes == 2_000_000
    assert handler.backupCount == 3
    assert handler.encoding.lower().replace("-", "") == "utf8"
    text = ctx.log_path.read_text(encoding="utf-8")
    assert "línea de prueba con eñe" in text


def test_last_session_is_offered_but_never_played(make_app, qtbot, tmp_path):
    seed_progress(tmp_path)
    ctx, built = make_app()
    played = []

    async def play(doc_id, start=None):
        played.append(doc_id)

    ctx.controller.play = play  # antes de que la interfaz procese eventos

    qtbot.waitUntil(lambda: "Continuar" in ctx.window.notice_label.text(), timeout=WAIT_MS)
    assert ctx.window.notice_label.text() == "Continuar Mi novela — Capítulo 2"
    qtbot.wait(300)  # por si algo intentara leer solo
    assert played == []
    assert built.edge.calls == [] and built.piper.calls == []
    assert all(out.starts == 0 for out in built.outputs)
    assert ctx.controller.state is SessionState.IDLE
    # La oferta está a la vista (diálogo) o pendiente hasta que el usuario mire la ventana.
    dialog = ctx.window.resume_dialog
    assert (dialog is not None and dialog.isVisible()) or ctx.window._resume.pending


def test_first_run_without_progress_offers_nothing(make_app, qtbot):
    ctx, _ = make_app()
    qtbot.wait(300)
    assert "Continuar" not in ctx.window.notice_label.text()
    assert ctx.window.resume_dialog is None


# --- fallos al construir ------------------------------------------------------------------


def test_audio_device_failure_still_opens_window_with_notice(make_app, qtbot):
    def broken_output():
        raise OSError("PortAudio library not found")

    ctx, _ = make_app(output_factory=broken_output)

    assert ctx.window.isVisible()
    assert AudioOutputError.user_message in ctx.window.notice_label.text()
    # La salida se vuelve a intentar al leer; si sigue sin haber, es un AudioOutputError
    # que el controlador ya sabe tratar (un reintento y luego pausa con aviso).
    with pytest.raises(AudioOutputError):
        ctx.player._output.start(lambda block: None)


def test_without_piper_model_the_app_opens_with_the_banner(make_app, qtbot):
    ctx, _ = make_app()
    qtbot.waitUntil(lambda: ctx.window.first_run_banner.isVisible(), timeout=WAIT_MS)
    assert ctx.window.isVisible()


def test_corrupt_database_is_replaced_with_a_notice(make_app, qtbot, tmp_path):
    (tmp_path / "lector.db").write_bytes(b"esto no es una base de datos" * 200)
    ctx, _ = make_app()

    assert main.DB_RECOVERED in ctx.window.notice_label.text()
    assert list(tmp_path.glob("lector.db.corrupt-*"))
    assert worker_alive(ctx)


# --- errores no capturados ----------------------------------------------------------------


@pytest.mark.qt_no_exception_capture
def test_uncaught_ui_exception_is_logged_saved_and_notified(make_app, qtbot, caplog):
    ctx, _ = make_app()
    saves = spy_shutdown(ctx)

    def boom():
        raise ValueError("fallo en la interfaz")

    with caplog.at_level(logging.ERROR):
        QTimer.singleShot(0, boom)
        qtbot.waitUntil(lambda: saves == [True], timeout=WAIT_MS)
        qtbot.waitUntil(lambda: ctx.window.notice_label.text() == main.UNEXPECTED_ERROR,
                        timeout=WAIT_MS)

    record = next(r for r in caplog.records if r.exc_info and r.exc_info[0] is ValueError)
    assert "fallo en la interfaz" in str(record.exc_info[1])
    ctx.log_handler.flush()
    text = ctx.log_path.read_text(encoding="utf-8")
    assert "Traceback" in text and "fallo en la interfaz" in text
    # La app sigue viva: ventana abierta, trabajador en marcha y eventos Qt atendidos.
    assert ctx.window.isVisible()
    assert worker_alive(ctx)
    ticks = []
    QTimer.singleShot(0, lambda: ticks.append(1))
    qtbot.waitUntil(lambda: ticks == [1], timeout=WAIT_MS)


def test_exception_in_worker_task_is_logged_saved_and_notified(make_app, qtbot, caplog):
    ctx, _ = make_app()
    saves = spy_shutdown(ctx)

    async def fails():
        raise RuntimeError("fallo en una tarea del trabajador")

    async def fire_and_forget():
        asyncio.get_running_loop().create_task(fails())  # nadie recoge su error

    with caplog.at_level(logging.ERROR):
        ctx.worker.submit(fire_and_forget()).result(timeout=2)
        qtbot.waitUntil(lambda: saves == [True], timeout=WAIT_MS)
        qtbot.waitUntil(lambda: ctx.window.notice_label.text() == main.UNEXPECTED_ERROR,
                        timeout=WAIT_MS)

    assert any(r.exc_info and "fallo en una tarea del trabajador" in str(r.exc_info[1])
               for r in caplog.records)
    assert ctx.window.isVisible()
    assert worker_alive(ctx)


def test_exception_in_worker_callback_is_handled(make_app, qtbot, caplog):
    ctx, _ = make_app()
    saves = spy_shutdown(ctx)

    def boom():
        raise KeyError("fallo en un callback")

    with caplog.at_level(logging.ERROR):
        ctx.worker.loop.call_soon_threadsafe(boom)
        qtbot.waitUntil(lambda: saves == [True], timeout=WAIT_MS)
    assert any(r.exc_info and r.exc_info[0] is KeyError for r in caplog.records)
    assert worker_alive(ctx)


@pytest.mark.qt_no_exception_capture
def test_failing_handler_does_not_recurse(make_app, qtbot, caplog):
    ctx, _ = make_app()
    notices = []

    def broken_notice(message, level="info"):
        notices.append(message)
        raise RuntimeError("el aviso también falla")

    def broken_shutdown(save_only=False):
        raise TypeError("el guardado también falla")

    ctx.window.show_notice = broken_notice
    ctx.controller.shutdown = broken_shutdown

    def boom():
        raise ValueError("fallo original")

    with caplog.at_level(logging.ERROR):
        QTimer.singleShot(0, boom)
        qtbot.waitUntil(lambda: bool(notices), timeout=WAIT_MS)
        qtbot.wait(200)

    del ctx.window.show_notice, ctx.controller.shutdown  # vuelven los reales para salir
    assert notices == [main.UNEXPECTED_ERROR]  # una sola vez: no hay recursión
    assert any(r.exc_info and r.exc_info[0] is ValueError for r in caplog.records)
    assert ctx.window.isVisible()
    assert worker_alive(ctx)


def test_exceptions_in_other_threads_are_logged(make_app, caplog):
    ctx, _ = make_app()

    def boom():
        raise ValueError("fallo en otro hilo")

    with caplog.at_level(logging.ERROR):
        thread = threading.Thread(target=boom, name="hilo-de-prueba")
        thread.start()
        thread.join(timeout=2)

    assert any(r.exc_info and "fallo en otro hilo" in str(r.exc_info[1]) for r in caplog.records)
    assert worker_alive(ctx)


def test_close_restores_previous_hooks(make_app):
    import sys

    before = (sys.excepthook, threading.excepthook)
    ctx, _ = make_app()
    assert sys.excepthook is not before[0]
    ctx.quit()
    ctx.close()
    assert (sys.excepthook, threading.excepthook) == before


# --- salida sin bandeja -------------------------------------------------------------------


def test_without_tray_closing_the_window_quits_cleanly(make_app, qtbot):
    ctx, built = make_app(tray=False)
    saves = spy_shutdown(ctx)
    assert ctx.tray is None

    ctx.window.close()

    qtbot.waitUntil(lambda: not ctx.window.isVisible(), timeout=WAIT_MS)
    assert saves == [False]  # cierre completo del controlador, no solo guardar
    with pytest.raises(RuntimeError):
        ctx.worker.submit(asyncio.sleep(0))


# --- Windows -------------------------------------------------------------------------------


class FakeShell32:
    def __init__(self):
        self.ids = []

    def SetCurrentProcessExplicitAppUserModelID(self, app_id):  # noqa: N802 (API de Windows)
        self.ids.append(app_id)
        return 0


def test_app_user_model_id_only_on_windows():
    shell = FakeShell32()
    assert main.set_windows_app_id(platform="darwin", shell32=shell) is False
    assert shell.ids == []
    assert main.set_windows_app_id(platform="win32", shell32=shell) is True
    assert shell.ids == [main.APP_USER_MODEL_ID]


# --- main() ---------------------------------------------------------------------------------


def test_main_runs_the_event_loop_until_quit(make_app, monkeypatch):
    ctx, _ = make_app(tray=False)  # sin bandeja, salir llama de verdad a QApplication.quit
    monkeypatch.setattr(main, "build_app", lambda argv: ctx)
    monkeypatch.setattr(main, "_show_startup_failure", lambda: pytest.fail("no arrancó"))
    QTimer.singleShot(100, ctx.quit)

    assert main.main() == 0

    with pytest.raises(RuntimeError):
        ctx.worker.submit(asyncio.sleep(0))
