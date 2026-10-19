"""Arranque de la app (main.py): montaje, registro, recuperación y errores no capturados.

Sin red y sin audio real: salida `FakeOutput`, motores falsos, sin descargador y una
bandeja falsa. Todas las esperas tienen tope.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from logging.handlers import RotatingFileHandler

import pytest
from PySide6.QtCore import QTimer

import main
from lector.errors import AudioOutputError
from lector.services import audio_output
from lector.services.app_builder import DB_RECOVERED, DB_UNAVAILABLE
from lector.services.app_errors import UNEXPECTED_ERROR
from lector.ui import app as app_module
from lector.ui.app import APP_USER_MODEL_ID, AlreadyRunning, StartupError, set_windows_app_id
from lector.ui.single_instance import SingleInstance
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

        ctx = app_module.build_app(
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
    # La salida se vuelve a intentar al leer (en un hilo, sin bloquear el trabajador); si
    # sigue sin haber, avisa con un AudioOutputError que el controlador ya sabe tratar
    # (reintentos con espera creciente y luego pausa con aviso).
    errors: list[Exception] = []
    ctx.player._output.start(lambda block: None, on_error=errors.append)
    qtbot.waitUntil(lambda: bool(errors), timeout=WAIT_MS)
    assert isinstance(errors[0], AudioOutputError)


def test_without_piper_model_the_app_opens_with_the_banner(make_app, qtbot):
    ctx, _ = make_app()
    qtbot.waitUntil(lambda: ctx.window.first_run_banner.isVisible(), timeout=WAIT_MS)
    assert ctx.window.isVisible()


def test_corrupt_database_is_replaced_with_a_notice(make_app, qtbot, tmp_path):
    (tmp_path / "lector.db").write_bytes(b"esto no es una base de datos" * 200)
    ctx, _ = make_app()

    assert DB_RECOVERED in ctx.window.notice_label.text()
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
        qtbot.waitUntil(lambda: ctx.window.notice_label.text() == UNEXPECTED_ERROR,
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
        qtbot.waitUntil(lambda: ctx.window.notice_label.text() == UNEXPECTED_ERROR,
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
    assert notices == [UNEXPECTED_ERROR]  # una sola vez: no hay recursión
    assert any(r.exc_info and r.exc_info[0] is ValueError for r in caplog.records)
    assert ctx.window.isVisible()
    assert worker_alive(ctx)


def test_exceptions_in_other_threads_are_logged_saved_and_notified(make_app, qtbot, caplog):
    ctx, _ = make_app()
    saves = spy_shutdown(ctx)

    def boom():
        raise ValueError("fallo en otro hilo")

    with caplog.at_level(logging.ERROR):
        thread = threading.Thread(target=boom, name="hilo-de-prueba")
        thread.start()
        thread.join(timeout=2)
        qtbot.waitUntil(lambda: saves == [True], timeout=WAIT_MS)
        qtbot.waitUntil(lambda: ctx.window.notice_label.text() == UNEXPECTED_ERROR,
                        timeout=WAIT_MS)

    record = next(r for r in caplog.records if r.exc_info and r.exc_info[0] is ValueError)
    assert "fallo en otro hilo" in str(record.exc_info[1])
    assert "hilo-de-prueba" in record.getMessage()
    assert worker_alive(ctx)


def test_system_exit_in_a_thread_is_ignored(make_app, qtbot):
    ctx, _ = make_app()
    saves = spy_shutdown(ctx)
    thread = threading.Thread(target=lambda: (_ for _ in ()).throw(SystemExit(0)))
    thread.start()
    thread.join(timeout=2)
    qtbot.wait(200)
    assert saves == []


def test_unexpected_error_text_does_not_claim_the_save_already_finished():
    assert "Se guardó" not in UNEXPECTED_ERROR
    assert "guardando" in UNEXPECTED_ERROR


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
    assert set_windows_app_id(platform="darwin", shell32=shell) is False
    assert shell.ids == []
    assert set_windows_app_id(platform="win32", shell32=shell) is True
    assert shell.ids == [APP_USER_MODEL_ID]


# --- main() ---------------------------------------------------------------------------------


def test_main_runs_the_event_loop_until_quit(make_app, monkeypatch):
    ctx, _ = make_app(tray=False)  # sin bandeja, salir llama de verdad a QApplication.quit
    monkeypatch.setattr(main, "build_app", lambda argv: ctx)
    monkeypatch.setattr(main, "_show_startup_failure", lambda: pytest.fail("no arrancó"))
    QTimer.singleShot(100, ctx.quit)

    assert main.main() == 0

    with pytest.raises(RuntimeError):
        ctx.worker.submit(asyncio.sleep(0))


# --- una sola instancia ---------------------------------------------------------------------


def test_second_launch_shows_the_first_window_and_opens_nothing(make_app, qtbot, tmp_path,
                                                                monkeypatch):
    ctx, _ = make_app()
    ctx.window.hide()
    opened = []
    monkeypatch.setattr(app_module, "open_app_database",
                        lambda *a, **k: opened.append(a) or pytest.fail("abrió la base"))
    with pytest.raises(AlreadyRunning):
        app_module.build_app([], data_dir=tmp_path)

    qtbot.waitUntil(lambda: ctx.window.isVisible(), timeout=WAIT_MS)
    assert opened == []
    workers = [t for t in threading.enumerate() if t.name == "lector-worker" and t.is_alive()]
    assert len(workers) == 1  # la segunda no arrancó trabajador
    assert len([w for w in ctx.app.topLevelWidgets()
                if type(w).__name__ == "MainWindow" and w.isVisible()]) == 1


def test_lock_is_released_on_close(make_app, tmp_path):
    ctx, _ = make_app()
    probe = SingleInstance(tmp_path / "lector.lock", "sin-uso")
    assert not probe.acquire()
    ctx.quit()
    ctx.close()
    assert probe.acquire()
    probe.release()


def test_main_returns_zero_when_another_instance_runs(monkeypatch):
    def already(argv):
        raise AlreadyRunning()

    monkeypatch.setattr(main, "build_app", already)
    monkeypatch.setattr(main, "_show_startup_failure", lambda: pytest.fail("no es un fallo"))
    assert main.main() == 0


# --- fallos al arrancar -----------------------------------------------------------------------


def _no_worker_left() -> bool:
    return not any(t.name == "lector-worker" and t.is_alive() for t in threading.enumerate())


def test_failed_build_cleans_up_and_raises_startup_error(qapp, qtbot, tmp_path):
    import sys

    hooks = (sys.excepthook, threading.excepthook)
    handlers = list(logging.getLogger().handlers)

    def broken_engines(settings, models):
        raise RuntimeError("motores rotos")

    with pytest.raises(StartupError):
        app_module.build_app([], data_dir=tmp_path, output_factory=FakeOutput,
                             fetcher_factory=lambda s: None, engine_factory=broken_engines,
                             tray_factory=lambda *a: None)

    qtbot.waitUntil(_no_worker_left, timeout=WAIT_MS)
    assert (sys.excepthook, threading.excepthook) == hooks
    assert logging.getLogger().handlers == handlers
    probe = SingleInstance(tmp_path / "lector.lock", "sin-uso")
    assert probe.acquire()  # el cerrojo se soltó
    probe.release()
    assert "motores rotos" in (tmp_path / "lector.log").read_text(encoding="utf-8")


def test_build_timeout_cleans_up_and_raises_startup_error(qapp, qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "BUILD_TIMEOUT_S", 0.2)

    def slow_engines(settings, models):
        time.sleep(0.6)  # bloquea el bucle del trabajador más que el tope
        return StubEdge(), FakePiper()

    started = time.monotonic()
    with pytest.raises(StartupError):
        app_module.build_app([], data_dir=tmp_path, output_factory=FakeOutput,
                             fetcher_factory=lambda s: None, engine_factory=slow_engines,
                             tray_factory=lambda *a: None)
    assert time.monotonic() - started < 5
    qtbot.waitUntil(_no_worker_left, timeout=WAIT_MS)


def test_main_returns_one_and_warns_when_startup_fails(monkeypatch):
    shown = []

    def broken(argv):
        raise StartupError("no se pudo")

    monkeypatch.setattr(main, "build_app", broken)
    monkeypatch.setattr(main, "_show_startup_failure", lambda: shown.append(True))
    assert main.main() == 1
    assert shown == [True]


def test_unopenable_database_still_opens_the_app(make_app, tmp_path):
    (tmp_path / "lector.db").mkdir()
    ctx, _ = make_app()
    assert ctx.window.isVisible()
    assert DB_UNAVAILABLE in ctx.window.notice_label.text()
    assert worker_alive(ctx)


def test_hanging_audio_probe_does_not_block_startup(make_app, monkeypatch):
    monkeypatch.setattr(audio_output, "AUDIO_PROBE_TIMEOUT_S", 0.2)
    release = threading.Event()

    def hangs():
        release.wait(3)
        return FakeOutput()

    started = time.monotonic()
    try:
        ctx, _ = make_app(output_factory=hangs)
        assert time.monotonic() - started < 5
        assert ctx.window.isVisible()
        assert AudioOutputError.user_message in ctx.window.notice_label.text()
    finally:
        release.set()


# --- montaje ------------------------------------------------------------------------------------


def test_old_chunk_log_rows_are_pruned_at_startup(make_app, tmp_path):
    conn = open_database(tmp_path / "lector.db")
    now = time.time()
    conn.executemany("INSERT INTO chunk_log(chunk_id, engine, outcome, at) VALUES(?,?,?,?)",
                     [("viejo", "edge", "ok", now - 40 * 86400), ("nuevo", "edge", "ok", now)])
    conn.commit()
    conn.close()

    make_app()

    check = sqlite3.connect(tmp_path / "lector.db")
    try:
        rows = [r[0] for r in check.execute("SELECT chunk_id FROM chunk_log")]
    finally:
        check.close()
    assert rows == ["nuevo"]


def test_cached_voices_reach_the_edge_engine(make_app, tmp_path):
    from lector.persistence.repositories import VoicesCacheRepo

    conn = open_database(tmp_path / "lector.db")
    voices = [{"ShortName": "es-MX-DaliaNeural", "Locale": "es-MX", "Gender": "Female"}]
    VoicesCacheRepo(conn).save(voices)
    conn.close()

    _, built = make_app()
    assert built.edge.known == voices


def test_same_engines_go_to_manager_settings_and_sample(make_app):
    ctx, built = make_app()
    manager = ctx.services.engine_manager
    assert manager._edge is built.edge and manager._piper is built.piper
    assert ctx.services.edge is built.edge and ctx.services.piper is built.piper
    assert ctx.window._edge is built.edge  # «Actualizar lista» de la configuración
    assert ctx.window._sample._engine is built.piper  # «Escuchar muestra»


# --- un sondeo de audio colgado no retiene el proceso ------------------------------------------


def test_hung_audio_probe_leaves_no_blocking_thread_after_quit(make_app, monkeypatch, qtbot):
    monkeypatch.setattr(audio_output, "AUDIO_PROBE_TIMEOUT_S", 0.2)
    before = set(threading.enumerate())
    never = threading.Event()

    def never_returns():
        never.wait(30)  # como un PortAudio colgado
        return FakeOutput()

    try:
        ctx, _ = make_app(output_factory=never_returns)
        assert AudioOutputError.user_message in ctx.window.notice_label.text()
        ctx.quit()
        ctx.close()
        # Python espera al salir a los hilos de los executors (aunque sean daemon); solo
        # puede quedar el hilo propio del sondeo, que es daemon y no se espera.
        def left():
            return [t for t in threading.enumerate() if t not in before and t.is_alive()]

        qtbot.waitUntil(lambda: all(t.name == audio_output.PROBE_THREAD_NAME for t in left()),
                        timeout=WAIT_MS)
        assert all(t.daemon for t in left())
    finally:
        never.set()


_EXIT_SCRIPT = """
import sys, threading
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from lector.services import audio_output
from lector.ui import app as app_module
from tests.fakes import FakeEdge, FakePiper

class Edge(FakeEdge):
    def set_known_voices(self, voices):
        pass

audio_output.AUDIO_PROBE_TIMEOUT_S = 0.2

def never_returns():
    threading.Event().wait()  # nunca vuelve

ctx = app_module.build_app([], data_dir=sys.argv[1], output_factory=never_returns,
                           fetcher_factory=lambda s: None,
                           engine_factory=lambda s, m: (Edge(), FakePiper()),
                           tray_factory=lambda *a: None)
QTimer.singleShot(200, ctx.quit)
code = ctx.app.exec()
ctx.close()
print("salida", code, flush=True)
"""


def test_process_exits_after_quit_even_with_a_hung_audio_probe(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONPATH=str(repo))
    started = time.monotonic()
    result = subprocess.run([sys.executable, "-c", _EXIT_SCRIPT, str(tmp_path)], cwd=repo,
                            env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert "salida 0" in result.stdout
    assert time.monotonic() - started < 30


def test_second_launch_does_not_touch_the_first_instance_log(make_app, tmp_path):
    ctx, _ = make_app()
    ctx.log_handler.flush()
    before = ctx.log_path.read_bytes()

    with pytest.raises(AlreadyRunning):
        app_module.build_app([], data_dir=tmp_path)

    ctx.log_handler.flush()
    assert ctx.log_path.read_bytes() == before
    assert not (tmp_path / "lector.log.1").exists()
