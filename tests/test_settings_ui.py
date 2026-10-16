"""Configuración, voces y primer arranque de Piper.

Se prueba con un controlador falso (métodos async que corren en el trabajador), un
AsyncWorker real, SQLite real y descargas servidas por `httpx.MockTransport` (sin red).
"""
from __future__ import annotations

import asyncio
import json
import threading

import httpx
import numpy as np
import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox

from lector.models import Chapter, ChapterRef, Document, Paragraph, Sentence, SourceKind
from lector.persistence.database import open_database
from lector.persistence.repositories import (
    ChapterRepo,
    DocumentRepo,
    SettingsRepo,
    VoicesCacheRepo,
)
from lector.services.worker import AsyncWorker, QtEventBridge
from lector.tts.piper_engine import PiperModelManager
from lector.ui.main_window import MainWindow
from lector.ui.piper_setup import (
    BANNER_TEXT,
    DEFAULT_MODEL,
    DOWNLOAD_FAILED,
    SAMPLE_BUSY,
    FirstRunBanner,
    PiperSetupPanel,
    SamplePlayer,
)
from lector.ui.settings_dialog import DALIA_MISSING, PRIVACY_TEXT, SettingsDialog
from tests.fakes import FakeOutput, FakePiper

WAIT_MS = 3000
DALIA = {"ShortName": "es-MX-DaliaNeural", "Locale": "es-MX", "Gender": "Female"}
JORGE = {"ShortName": "es-MX-JorgeNeural", "Locale": "es-MX", "Gender": "Male"}
ELVIRA = {"ShortName": "es-ES-ElviraNeural", "Locale": "es-ES", "Gender": "Female"}


# --- dobles ----------------------------------------------------------------------------


class FakeController:
    """Anota (nombre, *args) y el hilo de cada llamada."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.threads: set[threading.Thread] = set()

    def _record(self, *call) -> None:
        self.threads.add(threading.current_thread())
        self.calls.append(call)


def _recorder(name: str):
    async def method(self, *args):
        self._record(name, *args)

    method.__name__ = name
    return method


for _name in ("play", "pause", "resume", "stop", "set_voice", "set_speed", "set_continuous",
              "set_preferred_engine", "skip_seconds"):
    setattr(FakeController, _name, _recorder(_name))


class FakeEdgeVoices:
    """Lo que el diálogo usa de EdgeEngine: `list_voices` y `set_known_voices`."""

    def __init__(self, voices: list[dict]):
        self.voices = voices
        self.known: list[dict] | None = None
        self.threads: set[threading.Thread] = set()

    async def list_voices(self) -> list[dict]:
        self.threads.add(threading.current_thread())
        return list(self.voices)

    def set_known_voices(self, voices: list[dict]) -> None:
        self.threads.add(threading.current_thread())
        self.known = list(voices)


class SpySettings(SettingsRepo):
    def __init__(self, conn):
        super().__init__(conn)
        self.threads: set[threading.Thread] = set()

    def get(self, key):
        self.threads.add(threading.current_thread())
        return super().get(key)

    def set(self, key, value):
        self.threads.add(threading.current_thread())
        super().set(key, value)


class FakeSample:
    """Doble de SamplePlayer: anota las voces pedidas; `busy` simula lectura en curso."""

    def __init__(self) -> None:
        self.busy = False
        self.played: list[tuple[str, float]] = []
        self.threads: set[threading.Thread] = set()

    async def play(self, voice: str, speed: float = 1.0) -> bool:
        self.threads.add(threading.current_thread())
        if self.busy:
            return False
        self.played.append((voice, speed))
        return True

    async def stop(self) -> None:
        pass


class SlowBody(httpx.AsyncByteStream):
    """Cuerpo que llega en trozos con pausa (o que se queda colgado tras el primero)."""

    def __init__(self, parts: list[bytes], delay: float, hang: bool = False):
        self.parts, self.delay, self.hang = parts, delay, hang

    async def __aiter__(self):
        for part in self.parts:
            await asyncio.sleep(self.delay)
            yield part
            if self.hang:
                await asyncio.sleep(30)


def make_transport(*, threads=None, delay=0.04, parts=10, hang=False, fail=False,
                   cfg=b'{"speaker_id_map": {"F": 0, "M": 1}}'):
    body = [b"x" * 20_000] * parts

    def handler(request: httpx.Request) -> httpx.Response:
        if threads is not None:
            threads.add(threading.current_thread())
        assert request.url.host == "huggingface.co"
        if fail:
            raise httpx.ConnectError("sin red", request=request)
        if request.url.path.endswith(".onnx.json"):
            return httpx.Response(200, content=cfg)
        return httpx.Response(
            200, headers={"content-length": str(sum(map(len, body)))},
            stream=SlowBody(body, delay, hang),
        )

    return httpx.MockTransport(handler)


def install_piper(directory, name=DEFAULT_MODEL, speakers=None):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.onnx").write_bytes(b"x")
    cfg = {"speaker_id_map": speakers} if speakers is not None else {}
    (directory / f"{name}.onnx.json").write_text(json.dumps(cfg))


def make_doc(doc_id: str) -> Document:
    origin = f"https://example.com/{doc_id}/"
    return Document(doc_id, SourceKind.URL, origin, doc_id, origin, origin, None,
                    [ChapterRef(0, "Uno", origin + "1/")])


def make_chapter(locator: str) -> Chapter:
    body = "Una frase guardada."
    return Chapter(
        ref=ChapterRef(0, "Uno", locator), title="Uno", text=body,
        paragraphs=[Paragraph(0, body, 0, None)],
        sentences=[Sentence(0, 0, body, 0, len(body), None)],
        next_ref=None, prev_ref=None,
    )


# --- montaje ---------------------------------------------------------------------------


@pytest.fixture
def worker():
    w = AsyncWorker()
    w.start()
    yield w
    w.stop(timeout=2)


@pytest.fixture
def conn(tmp_path):
    c = open_database(tmp_path / "lector.db")
    yield c
    c.close()


@pytest.fixture
def ctl():
    return FakeController()


@pytest.fixture
def edge():
    return FakeEdgeVoices([DALIA, JORGE, ELVIRA])


@pytest.fixture
def sample():
    return FakeSample()


@pytest.fixture
def models_dir(tmp_path):
    return tmp_path / "voces"


@pytest.fixture
def make_window(qtbot, worker, conn, ctl, edge, sample, models_dir):
    def build(*, transport=None, settings=None, enable=True) -> MainWindow:
        window = MainWindow(worker, QtEventBridge(), ctl, settings or SpySettings(conn),
                            PiperModelManager(models_dir, transport=transport))
        qtbot.addWidget(window)
        qtbot.waitUntil(lambda: window.settings_loaded, timeout=WAIT_MS)
        if enable:
            window.enable_settings(edge, sample)
            qtbot.waitUntil(lambda: window.first_run_banner.checked, timeout=WAIT_MS)
        return window

    return build


def open_dialog(qtbot, window: MainWindow) -> SettingsDialog:
    window.settings_action.trigger()
    dialog = window.settings_dialog
    assert dialog is not None
    qtbot.waitUntil(lambda: dialog.loaded and dialog.piper_panel.loaded, timeout=WAIT_MS)
    return dialog


def choose(combo, data) -> None:
    """Como si el usuario eligiera ese elemento (solo `activated` cuenta como elección)."""
    index = combo.findData(data)
    assert index >= 0, data
    combo.setCurrentIndex(index)
    combo.activated.emit(index)


def panel_row(panel: PiperSetupPanel, name: str) -> int:
    for row in range(panel.table.rowCount()):
        if panel.model_at(row) == name:
            return row
    raise AssertionError(name)


@pytest.fixture(autouse=True)
def no_blocking_boxes(monkeypatch):
    """Ningún QMessageBox: todo aviso de esta pantalla es no modal."""
    def fail(*_args, **_kwargs):
        raise AssertionError("QMessageBox no permitido")

    for name in ("exec", "question", "warning", "information", "critical"):
        monkeypatch.setattr(QMessageBox, name, fail)


# --- pruebas del brief -----------------------------------------------------------------


def test_settings_persist(qtbot, make_window, ctl, conn):
    spy = SpySettings(conn)
    VoicesCacheRepo(conn).save([DALIA, JORGE, ELVIRA])
    window = make_window(settings=spy)
    dialog = open_dialog(qtbot, window)
    assert not dialog.isModal() and dialog.isVisible()
    assert [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())] == [
        "Voces", "Voces locales (Piper)", "Lectura", "Privacidad"]

    # Idioma por defecto es-MX: solo voces de México.
    assert dialog.language_combo.currentData() == "es-MX"
    shown = [dialog.edge_combo.itemData(i) for i in range(dialog.edge_combo.count())]
    assert shown == ["es-MX-DaliaNeural", "es-MX-JorgeNeural"]
    assert dialog.edge_combo.currentData() == "es-MX-DaliaNeural"

    choose(dialog.edge_combo, "es-MX-JorgeNeural")
    dialog.speed_spin.setValue(1.25)
    dialog.spins["edge_max_chars"].setValue(450)
    dialog.spins["piper_max_chars"].setValue(250)
    dialog.spins["edge_retries"].setValue(5)
    dialog.spins["recovery_initial_s"].setValue(30)
    dialog.spins["skip_seconds"].setValue(15)
    dialog.continuous_check.click()
    dialog.highlight_check.click()
    dialog.close()  # cerrar guarda lo pendiente

    plain = SettingsRepo(conn)
    expected = {"edge_voice": "es-MX-JorgeNeural", "speed": 1.25, "edge_max_chars": 450,
                "piper_max_chars": 250, "edge_retries": 5, "recovery_initial_s": 30,
                "skip_seconds": 15, "continuous": False, "highlight_sentence": False}
    qtbot.waitUntil(lambda: all(plain.get(k) == v for k, v in expected.items()), timeout=WAIT_MS)
    # Voz, velocidad y lectura continua van por el controlador, sin cortar lo que suena.
    qtbot.waitUntil(lambda: ("set_continuous", False) in ctl.calls, timeout=WAIT_MS)
    assert ("set_voice", "es-MX-JorgeNeural", None, False) in ctl.calls
    assert ("set_speed", 1.25, False) in ctl.calls
    assert threading.main_thread() not in ctl.threads
    assert threading.main_thread() not in spy.threads  # toda la E/S fuera del hilo de Qt

    # La ventana recarga: el intervalo nuevo ya se ve en los botones.
    qtbot.waitUntil(lambda: window.controls.skip_seconds == 15, timeout=WAIT_MS)

    # Al volver a abrir, se ve lo guardado.
    again = open_dialog(qtbot, window)
    assert again is not dialog
    assert again.edge_combo.currentData() == "es-MX-JorgeNeural"
    assert again.speed_spin.value() == pytest.approx(1.25)
    assert again.spins["edge_max_chars"].value() == 450
    assert not again.continuous_check.isChecked()
    assert not again.highlight_check.isChecked()


def test_dalia_missing_warning(qtbot, make_window, edge, conn):
    edge.voices = [JORGE, ELVIRA]
    window = make_window()
    dialog = open_dialog(qtbot, window)
    assert dialog.dalia_label.isHidden()  # caché vacía: aún no se sabe nada
    assert dialog.refresh_button.text() == "Actualizar lista"

    dialog.refresh_button.click()

    qtbot.waitUntil(lambda: not dialog.dalia_label.isHidden(), timeout=WAIT_MS)
    assert dialog.dalia_label.text() == DALIA_MISSING
    assert DALIA_MISSING == "La voz Dalia no está disponible ahora; elige otra voz en español"
    assert edge.known == [JORGE, ELVIRA]  # el motor ya no acepta la voz que no existe
    assert threading.main_thread() not in edge.threads
    qtbot.waitUntil(lambda: VoicesCacheRepo(conn).load() == [JORGE, ELVIRA], timeout=WAIT_MS)
    shown = [dialog.edge_combo.itemData(i) for i in range(dialog.edge_combo.count())]
    assert "es-MX-JorgeNeural" in shown

    # Con Dalia de vuelta, el aviso desaparece.
    edge.voices = [DALIA, JORGE]
    dialog.refresh_button.click()
    qtbot.waitUntil(lambda: dialog.dalia_label.isHidden(), timeout=WAIT_MS)


def test_first_run_banner_when_no_models(qtbot, make_window, conn):
    window = make_window()
    banner = window.first_run_banner
    assert isinstance(banner, FirstRunBanner)
    assert not banner.isHidden()
    assert banner.text_label.text() == BANNER_TEXT
    assert BANNER_TEXT == ("Sin voz local, un corte de internet dejará la lectura en espera. "
                           "¿Descargar la voz de respaldo (~76 MB)?")
    assert banner.download_button.text() == "Descargar"
    assert banner.later_button.text() == "Ahora no"
    assert banner.progress_bar.isHidden()
    # Está arriba del todo, en el hueco de avisos de la ventana.
    assert window._banner_layout.indexOf(banner) == 0

    banner.later_button.click()

    assert banner.isHidden()
    assert "Sin voz instalada" in window.piper_label.text()
    qtbot.waitUntil(lambda: SettingsRepo(conn).get("first_run_done") is True, timeout=WAIT_MS)

    # Rechazado una vez: no vuelve a salir, aunque siga sin voz.
    window2 = make_window()
    assert window2.first_run_banner.isHidden()
    assert "Sin voz instalada" in window2.piper_label.text()


def test_banner_hidden_when_model_installed(qtbot, make_window, models_dir):
    install_piper(models_dir, speakers={"F": 0, "M": 1})
    window = make_window()
    assert window.first_run_banner.checked
    assert window.first_run_banner.isHidden()


def test_privacy_toggle_purges_text(qtbot, make_window, conn):
    docs, chapters = DocumentRepo(conn), ChapterRepo(conn)
    for doc_id in ("a", "b"):
        doc = make_doc(doc_id)
        docs.upsert(doc, None, None, None)
        chapters.save(doc_id, make_chapter(doc.chapters[0].locator), True)
    window = make_window()
    dialog = open_dialog(qtbot, window)
    assert PRIVACY_TEXT == ("Edge TTS envía el texto que se lee a los servidores de Microsoft. "
                            "Piper lo procesa en tu equipo")
    assert PRIVACY_TEXT in dialog.privacy_label.text()
    check = dialog.store_text_check
    assert check.text() == "Guardar el texto de los capítulos"
    assert check.isChecked() and dialog.purge_frame.isHidden()

    check.click()  # desactivar: se ofrece borrar lo guardado, dentro del diálogo

    assert not dialog.purge_frame.isHidden()
    qtbot.waitUntil(lambda: SettingsRepo(conn).get("store_text") is False, timeout=WAIT_MS)
    assert chapters.get_text("a", "https://example.com/a/1/") is not None  # aún no se borró

    dialog.purge_button.click()

    def stored():
        return conn.execute(
            "SELECT COUNT(*) FROM chapters WHERE text IS NOT NULL OR paragraphs_json IS NOT NULL"
        ).fetchone()[0]

    qtbot.waitUntil(lambda: stored() == 0, timeout=WAIT_MS)
    qtbot.waitUntil(lambda: dialog.purge_frame.isHidden(), timeout=WAIT_MS)
    assert "borró" in dialog.privacy_status.text()
    # Los títulos, el historial y el progreso se conservan.
    assert chapters.title("a", "https://example.com/a/1/") == "Uno"
    assert len(docs.list_recent()) == 2


def test_download_runs_off_ui_thread(qtbot, make_window, models_dir, conn):
    handler_threads: set[threading.Thread] = set()
    window = make_window(transport=make_transport(threads=handler_threads))
    banner = window.first_run_banner
    seen: list[float] = []
    window.downloads.progress.connect(lambda name, fraction: seen.append(fraction))
    ticks: list[bool] = []
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(window.downloads.is_active(DEFAULT_MODEL)))
    timer.start()

    banner.download_button.click()

    assert window.downloads.is_active(DEFAULT_MODEL)
    assert not banner.download_button.isEnabled()
    assert banner.later_button.text() == "Cancelar"
    qtbot.waitUntil(lambda: not banner.progress_bar.isHidden() and 0 < banner.progress_bar.value() < 100,
                    timeout=WAIT_MS)
    qtbot.waitUntil(lambda: banner.isHidden(), timeout=WAIT_MS)
    timer.stop()

    assert handler_threads and threading.main_thread() not in handler_threads
    assert sum(ticks) >= 10  # la interfaz siguió respondiendo durante la descarga
    assert any(0 < f < 1 for f in seen)
    assert PiperModelManager(models_dir).installed() == [DEFAULT_MODEL]
    assert not list(models_dir.glob(".part-*"))
    qtbot.waitUntil(lambda: window.piper_label.text().startswith("Piper: Listo ("), timeout=WAIT_MS)
    qtbot.waitUntil(lambda: SettingsRepo(conn).get("first_run_done") is True, timeout=WAIT_MS)


# --- más casos -------------------------------------------------------------------------


def test_settings_open_non_modal_from_menu_and_link(qtbot, make_window):
    window = make_window()
    window.piper_label.linkActivated.emit("#configuracion")
    dialog = window.settings_dialog
    assert isinstance(dialog, SettingsDialog)
    assert dialog.isVisible() and not dialog.isModal()
    window.settings_action.trigger()
    assert window.settings_dialog is dialog  # el abierto se trae al frente, no se duplica
    qtbot.waitUntil(lambda: dialog.loaded, timeout=WAIT_MS)


def test_settings_not_wired_until_enabled(qtbot, make_window):
    window = make_window(enable=False)
    window.settings_action.trigger()
    assert getattr(window, "settings_dialog", None) is None


def test_banner_download_failure_keeps_offer(qtbot, make_window, models_dir):
    window = make_window(transport=make_transport(fail=True))
    banner = window.first_run_banner
    banner.download_button.click()
    qtbot.waitUntil(lambda: banner.error_label.text() == DOWNLOAD_FAILED, timeout=WAIT_MS)
    assert not banner.isHidden()
    assert banner.download_button.isEnabled()
    assert banner.later_button.text() == "Ahora no"
    assert banner.progress_bar.isHidden()
    assert PiperModelManager(models_dir).installed() == []
    assert not window.downloads.is_active(DEFAULT_MODEL)


def test_banner_cancel_stops_download(qtbot, make_window, models_dir):
    window = make_window(transport=make_transport(hang=True))
    banner = window.first_run_banner
    banner.download_button.click()
    qtbot.waitUntil(lambda: (models_dir / f".part-{DEFAULT_MODEL}").exists(), timeout=WAIT_MS)
    banner.later_button.click()  # «Cancelar»
    qtbot.waitUntil(lambda: not window.downloads.is_active(DEFAULT_MODEL), timeout=WAIT_MS)
    qtbot.waitUntil(lambda: not (models_dir / f".part-{DEFAULT_MODEL}").exists(), timeout=WAIT_MS)
    assert not banner.isHidden() and banner.download_button.isEnabled()
    assert banner.later_button.text() == "Ahora no"
    assert PiperModelManager(models_dir).installed() == []


def test_piper_tab_lists_catalog_and_downloads(qtbot, make_window, models_dir):
    window = make_window(transport=make_transport())
    dialog = open_dialog(qtbot, window)
    panel = dialog.piper_panel
    assert panel.table.rowCount() == len(PiperModelManager.CATALOG)
    headers = [panel.table.horizontalHeaderItem(c).text() for c in range(panel.table.columnCount())]
    assert headers == ["Voz", "Género", "Tamaño", "Estado"]
    row = panel_row(panel, DEFAULT_MODEL)
    assert panel.table.item(row, 2).text() == "76 MB"
    assert panel.table.item(row, 3).text() == "Sin descargar"
    help_text = panel.help_label.text()
    for part in ("sin internet", "huggingface.co/rhasspy/piper-voices", "licencia", "MODEL_CARD",
                 "Dalia"):
        assert part in help_text
    assert [b.text() for b in (panel.download_button, panel.sample_button, panel.use_button)] == [
        "Descargar", "Escuchar muestra", "Usar"]

    panel.table.selectRow(row)
    assert panel.download_button.isEnabled()
    assert not panel.sample_button.isEnabled() and not panel.use_button.isEnabled()
    panel.download_button.click()

    qtbot.waitUntil(lambda: panel.table.item(row, 3).text() in ("Instalada", "En uso"), timeout=WAIT_MS)
    assert PiperModelManager(models_dir).installed() == [DEFAULT_MODEL]
    # Modelo con dos hablantes: aparece el selector.
    qtbot.waitUntil(lambda: not panel.speaker_combo.isHidden(), timeout=WAIT_MS)
    speakers = [panel.speaker_combo.itemData(i) for i in range(panel.speaker_combo.count())]
    assert speakers == ["F", "M"]
    assert panel.sample_button.isEnabled() and panel.use_button.isEnabled()
    # El banner de primer arranque también se va.
    qtbot.waitUntil(lambda: window.first_run_banner.isHidden(), timeout=WAIT_MS)


def test_closing_dialog_cancels_download(qtbot, make_window, models_dir):
    window = make_window(transport=make_transport(hang=True))
    dialog = open_dialog(qtbot, window)
    panel = dialog.piper_panel
    name = "es_MX-claude-high"
    row = panel_row(panel, name)
    panel.table.selectRow(row)
    panel.download_button.click()
    qtbot.waitUntil(lambda: (models_dir / f".part-{name}").exists(), timeout=WAIT_MS)
    qtbot.waitUntil(lambda: panel.table.item(row, 3).text().startswith("Descargando"), timeout=WAIT_MS)
    assert not panel.cancel_button.isHidden()

    dialog.close()

    qtbot.waitUntil(lambda: not window.downloads.is_active(name), timeout=WAIT_MS)
    qtbot.waitUntil(lambda: not (models_dir / f".part-{name}").exists(), timeout=WAIT_MS)
    assert PiperModelManager(models_dir).installed() == []


def test_use_piper_voice_applies_and_persists(qtbot, make_window, models_dir, ctl, conn):
    install_piper(models_dir, speakers={"F": 0, "M": 1})
    window = make_window()
    dialog = open_dialog(qtbot, window)
    panel = dialog.piper_panel
    row = panel_row(panel, DEFAULT_MODEL)
    assert panel.table.item(row, 3).text() == "En uso"  # la voz por defecto
    panel.table.selectRow(row)
    assert panel.speaker_combo.currentData() == "F"
    panel.speaker_combo.setCurrentIndex(panel.speaker_combo.findData("M"))

    panel.use_button.click()

    qtbot.waitUntil(lambda: ("set_voice", None, f"{DEFAULT_MODEL}#M", False) in ctl.calls,
                    timeout=WAIT_MS)
    plain = SettingsRepo(conn)
    qtbot.waitUntil(lambda: plain.get("piper_speaker") == "M", timeout=WAIT_MS)
    assert plain.get("piper_voice") == DEFAULT_MODEL
    qtbot.waitUntil(lambda: window.piper_label.text() == "Piper: Listo (Sharvard M)", timeout=WAIT_MS)


def test_sample_button_plays_or_reports_busy(qtbot, make_window, models_dir, sample):
    install_piper(models_dir, speakers={"F": 0, "M": 1})
    window = make_window()
    dialog = open_dialog(qtbot, window)
    panel = dialog.piper_panel
    panel.table.selectRow(panel_row(panel, DEFAULT_MODEL))

    panel.sample_button.click()
    qtbot.waitUntil(lambda: bool(sample.played) and panel.sample_button.isEnabled(), timeout=WAIT_MS)
    assert sample.played[0][0] == f"{DEFAULT_MODEL}#F"
    assert threading.main_thread() not in sample.threads

    sample.busy = True  # la lectura principal suena: no se interrumpe
    panel.sample_button.click()
    qtbot.waitUntil(lambda: panel.status_label.text() == SAMPLE_BUSY, timeout=WAIT_MS)
    assert len(sample.played) == 1


def test_privacy_keep_text_does_not_purge(qtbot, make_window, conn):
    doc = make_doc("a")
    DocumentRepo(conn).upsert(doc, None, None, None)
    ChapterRepo(conn).save("a", make_chapter(doc.chapters[0].locator), True)
    window = make_window()
    dialog = open_dialog(qtbot, window)
    dialog.store_text_check.click()
    assert not dialog.purge_frame.isHidden()
    dialog.keep_button.click()
    assert dialog.purge_frame.isHidden()
    dialog.store_text_check.click()  # volver a activarlo no ofrece nada
    assert dialog.purge_frame.isHidden()
    qtbot.waitUntil(lambda: SettingsRepo(conn).get("store_text") is True, timeout=WAIT_MS)
    assert ChapterRepo(conn).get_text("a", doc.chapters[0].locator) is not None


# --- muestra de voz --------------------------------------------------------------------


async def test_sample_plays_in_memory_only_when_idle():
    output = FakeOutput()
    engine = FakePiper()
    busy = False
    player = SamplePlayer(engine, output_factory=lambda: output, is_busy=lambda: busy)

    task = asyncio.ensure_future(player.play("es_ES-sharvard-medium#F", 1.0))
    async with asyncio.timeout(2):
        while not output.running:
            await asyncio.sleep(0.01)
        heard = []
        while not task.done():
            heard.append(output.pull(240))
            await asyncio.sleep(0.01)
    assert await task is True
    assert engine.calls and engine.calls[0][1] == "es_ES-sharvard-medium#F"
    assert any(np.any(block == 1000) for block in heard)  # sonó lo sintetizado
    assert not output.running  # su salida se cierra al terminar

    busy = True
    assert await player.play("es_ES-sharvard-medium#F") is False
    assert len(engine.calls) == 1  # con la lectura en marcha ni siquiera sintetiza
