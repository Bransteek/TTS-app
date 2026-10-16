"""Ventana principal: vista previa, controles, voz, indicadores, reanudación e historial.

Se prueba con un controlador falso que registra las llamadas (métodos async que
corren en el hilo del trabajador), un AsyncWorker real y un QtEventBridge real.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from lector.content.base import doc_id_for_url
from lector.errors import FileMovedOrChanged, PageUnreachable
from lector.models import Chapter, ChapterRef, Document, Paragraph, Position, Sentence, SourceKind
from lector.persistence.database import open_database
from lector.persistence.repositories import (
    DocumentRepo,
    ProgressRecord,
    ProgressRepo,
    SettingsRepo,
    VoicesCacheRepo,
)
from lector.services.reading_controller import SessionState
from lector.services.worker import AsyncWorker, QtEventBridge
from lector.tts.engine_manager import EngineState
from lector.tts.piper_engine import PiperModelManager
from lector.ui.history_dialog import HistoryDialog
from lector.ui.main_window import GENERIC_ERROR, MainWindow
from lector.ui.resume_dialog import ResumeChoice, ResumeDialog

WAIT_MS = 2000
URL = "https://example.com/novela/capitulo-1/"
URL2 = "https://example.com/novela/capitulo-2/"
URL3 = "https://example.com/novela/capitulo-3/"


# --- datos de prueba -------------------------------------------------------------------


def make_chapter(locator: str = URL, title: str = "Capítulo 1", paragraphs: int = 7) -> Chapter:
    paras, sentences, text, offset = [], [], [], 0
    for i in range(paragraphs):
        body = f"Párrafo número {i + 1}."
        paras.append(Paragraph(i, body, offset, None))
        sentences.append(Sentence(i, i, body, offset, offset + len(body), None))
        text.append(body)
        offset += len(body) + 1
    return Chapter(
        ref=ChapterRef(0, title, locator), title=title, text="\n".join(text),
        paragraphs=paras, sentences=sentences,
        next_ref=ChapterRef(1, "Capítulo 2", URL2), prev_ref=None,
    )


def make_doc(chapters: list[ChapterRef] | None = None, kind: SourceKind = SourceKind.URL,
             doc_id: str = "doc-1", title: str = "La novela", adapter: str | None = None,
             index_page: bool = False) -> Document:
    if chapters is None:
        chapters = [ChapterRef(0, "Capítulo 1", URL), ChapterRef(1, "Capítulo 2", URL2),
                    ChapterRef(2, "Capítulo 3", URL3)]
    origin = "https://example.com/novela/" if kind is SourceKind.URL else "/tmp/libro.pdf"
    if adapter is None:
        adapter = {SourceKind.URL: "madara", SourceKind.PDF: "pdf", SourceKind.EPUB: "epub"}[kind]
    return Document(doc_id, kind, origin, title, origin, origin, None, chapters,
                    adapter=adapter, index_page=index_page)


def generic_doc(doc_id: str = "doc-g", prefix: str = "https://example.com/novela/") -> Document:
    return Document(doc_id, SourceKind.URL, prefix, "Web genérica", prefix, prefix, None, [],
                    adapter="generic")


def pdf_doc() -> Document:
    return make_doc(
        [ChapterRef(0, "Uno", "pages:1-4"), ChapterRef(1, "Dos", "pages:5-9")],
        kind=SourceKind.PDF, title="Libro",
    )


def make_progress(doc: Document, chapter_index: int = 1, sentence: int = 4) -> ProgressRecord:
    ref = doc.chapters[chapter_index]
    return ProgressRecord(
        doc_id=doc.doc_id, kind=doc.kind, origin=doc.origin, title=doc.title,
        last_read_at=1_760_000_000.0,
        position=Position(chapter_index, ref.locator, sentence, sentence, 0, None),
        chapter_url=ref.locator, last_chunk_id=None, edge_voice="es-MX-DaliaNeural",
        piper_voice="es_ES-sharvard-medium#F", speed=1.0, session_state="reading",
    )


# --- controlador falso -----------------------------------------------------------------


class FakeController:
    """Registra cada llamada como (nombre, *argumentos); corre en el hilo del trabajador."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.threads: set[threading.Thread] = set()
        # Un resultado (o excepción) para todas las aperturas, o una lista: uno por apertura.
        self.open_result: tuple | BaseException | list | None = None
        self.open_block_s = 0.0  # bloquea el hilo del trabajador (apertura lenta)
        self.play_error: BaseException | None = None
        self.play_gate: threading.Event | None = None  # si se da, play() espera a que se abra
        self.page_position: Position | None = None

    def _record(self, *call) -> None:
        self.threads.add(threading.current_thread())
        self.calls.append(call)

    async def open(self, locator):
        self._record("open", locator)
        if self.open_block_s:
            time.sleep(self.open_block_s)
        result = self.open_result.pop(0) if isinstance(self.open_result, list) else self.open_result
        if isinstance(result, BaseException):
            raise result
        return result

    async def play(self, doc_id, start=None):
        self._record("play", doc_id, start)
        if self.play_gate is not None:
            await asyncio.to_thread(self.play_gate.wait, 2)
        if self.play_error is not None:
            raise self.play_error

    async def locate_page(self, doc_id, page):
        self._record("locate_page", doc_id, page)
        return self.page_position


def _recorder(name: str):
    async def method(self, *args):
        self._record(name, *args)

    method.__name__ = name
    return method


for _name in ("pause", "resume", "stop", "next_sentence", "prev_sentence", "restart_chapter",
              "skip_seconds", "next_chapter", "prev_chapter", "choose_next", "set_voice",
              "set_speed", "set_continuous", "set_preferred_engine"):
    setattr(FakeController, _name, _recorder(_name))


class SpySettings(SettingsRepo):
    """SettingsRepo que anota en qué hilo se lee o escribe."""

    def __init__(self, conn):
        super().__init__(conn)
        self.threads: set[threading.Thread] = set()

    def get(self, key):
        self.threads.add(threading.current_thread())
        return super().get(key)

    def set(self, key, value):
        self.threads.add(threading.current_thread())
        super().set(key, value)


class SpyModels(PiperModelManager):
    def __init__(self, directory):
        super().__init__(directory)
        self.threads: set[threading.Thread] = set()

    def installed(self):
        self.threads.add(threading.current_thread())
        return super().installed()

    def speakers(self, name):
        self.threads.add(threading.current_thread())
        return super().speakers(name)


def install_piper(directory, name="es_ES-sharvard-medium", speakers=None):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.onnx").write_bytes(b"x")
    cfg = {"speaker_id_map": speakers} if speakers is not None else {}
    (directory / f"{name}.onnx.json").write_text(json.dumps(cfg))


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
def make_window(qtbot, worker, conn, ctl, tmp_path):
    def build(*, show: bool = False, models: PiperModelManager | None = None,
              settings: SettingsRepo | None = None) -> MainWindow:
        bridge = QtEventBridge()
        window = MainWindow(
            worker, bridge, ctl, settings or SpySettings(conn),
            models or SpyModels(tmp_path / "voces"),
        )
        window.test_bridge = bridge
        qtbot.addWidget(window)
        qtbot.waitUntil(lambda: window.settings_loaded, timeout=WAIT_MS)
        if show:
            with qtbot.waitExposed(window):
                window.show()
            activate(qtbot, window)
        return window

    return build


def wait_call(qtbot, ctl: FakeController, name: str, *args) -> tuple:
    """Espera (con tope) a que el controlador reciba esa llamada y la devuelve."""
    def found():
        return [c for c in ctl.calls if c[0] == name and (not args or c[1:] == args)]

    qtbot.waitUntil(lambda: bool(found()), timeout=WAIT_MS)
    return found()[-1]


def activate(qtbot, window: MainWindow) -> None:
    """El usuario vuelve a la ventana (offscreen no devuelve el foco al cerrar un diálogo)."""
    with qtbot.waitActive(window):
        window.activateWindow()


def read(qtbot, window: MainWindow, locator: str = URL) -> None:
    window.url_edit.setText(locator)
    window.read_button.click()


# --- pruebas del brief -----------------------------------------------------------------


def test_read_button_opens_preview_then_plays(qtbot, make_window, ctl):
    doc, chapter = make_doc(), make_chapter()
    ctl.open_result = (doc, chapter, None)
    window = make_window(show=True)
    assert window.url_edit.placeholderText() == "Pega la URL de un capítulo o de la novela"
    assert window.open_file_button.text() == "Abrir PDF/EPUB…"
    assert window.preview.isHidden()

    read(qtbot, window)

    wait_call(qtbot, ctl, "play", "doc-1", None)
    assert [c[0] for c in ctl.calls] == ["open", "play"]
    assert ctl.calls[0] == ("open", URL)
    assert window.preview.isVisible()
    assert window.preview.title_label.text() == "La novela"
    assert window.preview.chapter_label.text() == "Capítulo 1"
    shown = window.preview.paragraphs_label.text()
    assert "Párrafo número 5." in shown and "Párrafo número 6." not in shown  # solo 5
    assert window.preview.next_label.text() == "Siguiente detectado: Capítulo 2"
    assert window.preview.prefix_edit.isHidden()  # Madara: tiene índice de capítulos

    # Con la vista previa ya abierta, «Leer» lee sin volver a abrir.
    ctl.calls.clear()
    window.read_button.click()
    wait_call(qtbot, ctl, "play", "doc-1", None)
    assert [c[0] for c in ctl.calls] == ["play"]


@pytest.mark.parametrize(
    ("button", "expected"),
    [
        ("stop_button", ("stop",)),
        ("prev_chapter_button", ("prev_chapter",)),
        ("prev_sentence_button", ("prev_sentence",)),
        ("skip_back_button", ("skip_seconds", -10)),
        ("skip_forward_button", ("skip_seconds", 10)),
        ("next_sentence_button", ("next_sentence",)),
        ("next_chapter_button", ("next_chapter",)),
        ("restart_button", ("restart_chapter",)),
    ],
)
def test_controls_call_controller(qtbot, make_window, ctl, button, expected):
    window = make_window()
    getattr(window.controls, button).click()
    assert wait_call(qtbot, ctl, expected[0]) == expected


def test_play_pause_toggles_with_session_state(qtbot, make_window, ctl):
    window = make_window()
    controls = window.controls
    assert controls.skip_back_button.text() == "⟲ −10 s"
    assert controls.skip_forward_button.text() == "⟳ +10 s"
    assert controls.play_pause_button.text() == "Leer"

    window.test_bridge.session_state_sig.emit("reading")
    assert controls.play_pause_button.text() == "Pausar"
    controls.play_pause_button.click()
    wait_call(qtbot, ctl, "pause")

    window.test_bridge.session_state_sig.emit("paused")
    assert controls.play_pause_button.text() == "Leer"
    controls.play_pause_button.click()
    wait_call(qtbot, ctl, "resume")

    controls.continuous_check.click()  # por defecto está activada
    assert wait_call(qtbot, ctl, "set_continuous") == ("set_continuous", False)


def test_speed_presets_and_apply_now(qtbot, make_window, ctl):
    window = make_window()
    voice = window.voice
    assert sorted(voice.speed_buttons) == [0.75, 1.0, 1.25, 1.5, 1.75, 2.0]
    spin = voice.speed_spin
    assert (spin.minimum(), spin.maximum(), spin.singleStep()) == (0.5, 2.5, 0.05)
    assert voice.apply_now_check.text() == "Aplicar ya"

    voice.speed_buttons[1.5].click()
    assert wait_call(qtbot, ctl, "set_speed") == ("set_speed", 1.5, False)
    assert spin.value() == 1.5

    voice.apply_now_check.setChecked(True)
    voice.speed_buttons[2.0].click()
    assert wait_call(qtbot, ctl, "set_speed", 2.0, True)

    spin.setValue(1.3)  # escribir en la casilla: se aplica tras una breve pausa
    assert wait_call(qtbot, ctl, "set_speed", 1.3, True)
    assert [c for c in ctl.calls if c[0] == "set_speed"] == [
        ("set_speed", 1.5, False), ("set_speed", 2.0, True), ("set_speed", 1.3, True),
    ]


def test_engine_indicator_updates_on_signal(qtbot, make_window, worker):
    window = make_window()
    assert "Edge TTS" in window.engine_label.text()
    assert "Conectado" in window.edge_label.text()

    window.test_bridge.engine_state_sig.emit("PIPER_ACTIVE", "Piper local")

    assert "Piper local" in window.engine_label.text()
    assert "Sin conexión" in window.edge_label.text()

    # Por el camino real: el aviso sale del hilo del trabajador.
    async def emit():
        window.test_bridge.on_engine_state(EngineState.EDGE_RETRYING, "Edge TTS")

    worker.submit(emit()).result(timeout=2)
    qtbot.waitUntil(lambda: "Reintentando…" in window.edge_label.text(), timeout=WAIT_MS)
    assert "Edge TTS" in window.engine_label.text()

    window.test_bridge.engine_state_sig.emit("waiting_network", "Edge TTS")
    assert "Sin conexión" in window.edge_label.text()
    window.test_bridge.engine_state_sig.emit("edge_active", "Edge TTS")
    assert "Conectado" in window.edge_label.text()


def test_resume_dialog_three_options(qtbot):
    doc = pdf_doc()
    dialog = ResumeDialog(doc, make_progress(doc, chapter_index=1))
    qtbot.addWidget(dialog)
    dialog.show()
    assert dialog.continue_button.text() == "Continuar desde donde lo dejé"
    assert dialog.start_button.text() == "Empezar desde el principio"
    assert dialog.choose_button.text() == "Elegir capítulo o página"
    assert "Dos" in dialog.where_label.text()  # dónde lo dejó
    assert dialog.chapter_list.isHidden() and dialog.page_spin.isHidden()

    with qtbot.waitSignal(dialog.chosen, timeout=WAIT_MS) as got:
        dialog.continue_button.click()
    assert got.args == [ResumeChoice("continue")]

    with qtbot.waitSignal(dialog.chosen, timeout=WAIT_MS) as got:
        dialog.start_button.click()
    assert got.args == [ResumeChoice("start")]
    assert not dialog.isVisible()  # elegir cierra el diálogo

    dialog.show()
    dialog.choose_button.click()
    assert dialog.chapter_list.isVisible() and dialog.page_spin.isVisible()
    assert [dialog.chapter_list.item(i).text() for i in range(dialog.chapter_list.count())] == [
        "Uno", "Dos",
    ]
    assert (dialog.page_spin.minimum(), dialog.page_spin.maximum()) == (1, 9)
    dialog.chapter_list.setCurrentRow(1)
    with qtbot.waitSignal(dialog.chosen, timeout=WAIT_MS) as got:
        dialog.chapter_button.click()
    assert got.args == [ResumeChoice("chapter", chapter=doc.chapters[1])]

    dialog.show()
    dialog.page_spin.setValue(7)
    with qtbot.waitSignal(dialog.chosen, timeout=WAIT_MS) as got:
        dialog.page_button.click()
    assert got.args == [ResumeChoice("page", page=7)]


def test_resume_dialog_without_pages_for_web(qtbot):
    doc = make_doc()
    dialog = ResumeDialog(doc, make_progress(doc))
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.choose_button.click()
    assert dialog.chapter_list.isVisible()
    assert dialog.page_spin.isHidden() and dialog.page_button.isHidden()

    generic = make_doc(chapters=[])  # web genérica: sin índice, no hay nada que elegir
    record = make_progress(doc)
    dialog2 = ResumeDialog(generic, record)
    qtbot.addWidget(dialog2)
    assert not dialog2.choose_button.isEnabled()


def test_error_shows_user_message(qtbot, make_window, ctl, caplog):
    window = make_window(show=True)
    ctl.open_result = PageUnreachable()
    read(qtbot, window)
    qtbot.waitUntil(lambda: window.notice_label.text() == PageUnreachable.user_message,
                    timeout=WAIT_MS)
    assert window.notice_label.isVisible()
    assert window.read_button.isEnabled()

    ctl.open_result = (make_doc(), make_chapter(), None)
    ctl.play_error = FileMovedOrChanged("doc-1")
    read(qtbot, window, URL2)
    qtbot.waitUntil(lambda: window.notice_label.text() == FileMovedOrChanged.user_message,
                    timeout=WAIT_MS)

    ctl.open_result = RuntimeError("fallo interno")
    with caplog.at_level(logging.ERROR, logger="lector.ui.main_window"):
        read(qtbot, window, URL3)
        qtbot.waitUntil(lambda: window.notice_label.text() == GENERIC_ERROR, timeout=WAIT_MS)
    assert GENERIC_ERROR == (
        "Ocurrió un problema inesperado. La lectura se ha pausado y tu progreso está guardado."
    )
    logged = [r for r in caplog.records if r.exc_info and isinstance(r.exc_info[1], RuntimeError)]
    assert logged, "el error inesperado debe quedar en el log con su traza"


def test_ui_responsive_while_opening(qtbot, make_window, ctl):
    ctl.open_result = (make_doc(), make_chapter(), None)
    ctl.open_block_s = 2.0  # bloquea el hilo del trabajador dos segundos
    window = make_window(show=True)
    ticks: list[float] = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start(20)
    try:
        read(qtbot, window)
        assert not window.read_button.isEnabled()  # no se puede abrir dos veces a la vez
        qtbot.waitUntil(lambda: len(ticks) >= 20, timeout=1500)
        assert not any(c[0] == "play" for c in ctl.calls)  # la apertura sigue en curso
        assert window.preview.isHidden()
    finally:
        timer.stop()
    wait_call(qtbot, ctl, "play", "doc-1", None)
    assert window.preview.isVisible()
    assert ctl.threads and threading.main_thread() not in ctl.threads


# --- reanudación desde la ventana ---------------------------------------------------------


def test_open_with_progress_offers_resume(qtbot, make_window, ctl):
    doc = make_doc()
    ctl.open_result = (doc, make_chapter(), make_progress(doc))
    window = make_window(show=True)

    read(qtbot, window)
    qtbot.waitUntil(lambda: window.resume_dialog is not None and window.resume_dialog.isVisible(),
                    timeout=WAIT_MS)
    assert not any(c[0] == "play" for c in ctl.calls)  # espera la decisión
    window.resume_dialog.continue_button.click()
    wait_call(qtbot, ctl, "play", "doc-1", None)

    activate(qtbot, window)
    read(qtbot, window, URL2)  # otra apertura: empezar desde el principio
    qtbot.waitUntil(lambda: window.resume_dialog is not None and window.resume_dialog.isVisible(),
                    timeout=WAIT_MS)
    ctl.calls.clear()
    window.resume_dialog.start_button.click()
    _, _, start = wait_call(qtbot, ctl, "play")
    assert start == Position(0, URL, 0, 0, 0, None)


def test_resume_choice_chapter_and_page(qtbot, make_window, ctl):
    doc = pdf_doc()
    chapter = make_chapter("pages:1-4", "Uno")
    ctl.open_result = (doc, chapter, make_progress(doc))
    ctl.page_position = Position(1, "pages:5-9", 3, 12, 40, 7)
    window = make_window(show=True)

    read(qtbot, window, "/tmp/libro.pdf")
    qtbot.waitUntil(lambda: window.resume_dialog is not None and window.resume_dialog.isVisible(),
                    timeout=WAIT_MS)
    dialog = window.resume_dialog
    dialog.choose_button.click()
    dialog.chapter_list.setCurrentRow(1)
    dialog.chapter_button.click()
    _, _, start = wait_call(qtbot, ctl, "play")
    assert start == Position(1, "pages:5-9", 0, 0, 0, None)
    assert not dialog.isVisible()

    ctl.calls.clear()
    activate(qtbot, window)
    window.offer_resume(doc, make_progress(doc))
    window.resume_dialog.choose_button.click()
    window.resume_dialog.page_spin.setValue(7)
    window.resume_dialog.page_button.click()
    _, _, start = wait_call(qtbot, ctl, "play")
    assert ctl.calls[0] == ("locate_page", "doc-1", 7)
    assert start == Position(1, "pages:5-9", 3, 12, 40, 7)


def test_resume_while_hidden_continues_without_dialog(qtbot, make_window, ctl):
    doc = make_doc()
    ctl.open_result = (doc, make_chapter(), make_progress(doc))
    window = make_window(show=False)  # p. ej. se minimizó a la bandeja mientras abría

    read(qtbot, window)

    wait_call(qtbot, ctl, "play", "doc-1", None)
    assert window.resume_dialog is None


# --- vista previa, lectura y voz ------------------------------------------------------------


def test_document_and_sentence_signals_update_reading_view(qtbot, make_window):
    window = make_window(show=True)
    bridge = window.test_bridge
    bridge.document_sig.emit(make_doc(), make_chapter(title="Capítulo 7"))
    bridge.sentence_sig.emit("Capítulo 7", "Había una vez un lector.", 2, 10)

    assert window.doc_title_label.text() == "La novela"
    assert window.chapter_label.text() == "Capítulo 7"
    assert window.sentence_label.text() == "Había una vez un lector."
    assert window.sentence_label.wordWrap()
    assert window.sentence_label.autoFillBackground()  # resaltado activado por defecto
    assert (window.progress_bar.value(), window.progress_bar.maximum()) == (3, 10)
    assert window.preview.isHidden()  # ya se está leyendo


def test_highlight_can_be_disabled_in_settings(qtbot, make_window, conn):
    SettingsRepo(conn).set("highlight_sentence", False)
    window = make_window()
    window.test_bridge.sentence_sig.emit("Cap", "Una frase.", 0, 3)
    assert not window.sentence_label.autoFillBackground()


def test_waiting_network_notice(qtbot, make_window):
    window = make_window(show=True)
    assert window.waiting_label.isHidden()
    window.test_bridge.session_state_sig.emit("waiting_network")
    assert window.waiting_label.isVisible()
    assert window.waiting_label.text() == "Esperando conexión…"
    window.test_bridge.session_state_sig.emit("reading")
    assert window.waiting_label.isHidden()


def test_voice_panel_lists_voices_and_calls_controller(qtbot, make_window, ctl, conn, tmp_path):
    VoicesCacheRepo(conn).save([
        {"ShortName": "es-MX-DaliaNeural", "Locale": "es-MX", "Gender": "Female"},
        {"ShortName": "es-ES-AlvaroNeural", "Locale": "es-ES", "Gender": "Male"},
        {"ShortName": "en-US-AriaNeural", "Locale": "en-US", "Gender": "Female"},
    ])
    install_piper(tmp_path / "voces", speakers={"F": 0, "M": 1})
    window = make_window(models=SpyModels(tmp_path / "voces"))
    voice = window.voice

    edge = [voice.edge_combo.itemData(i) for i in range(voice.edge_combo.count())]
    assert sorted(edge) == ["es-ES-AlvaroNeural", "es-MX-DaliaNeural"]  # solo español
    assert voice.edge_combo.currentData() == "es-MX-DaliaNeural"
    piper = [voice.piper_combo.itemData(i) for i in range(voice.piper_combo.count())]
    assert piper == ["es_ES-sharvard-medium#F", "es_ES-sharvard-medium#M"]
    assert voice.piper_combo.currentData() == "es_ES-sharvard-medium#F"
    assert window.piper_label.text().startswith("Piper: Listo (")
    assert ctl.calls == []  # cargar la configuración no llama al controlador

    i = edge.index("es-ES-AlvaroNeural")
    voice.edge_combo.setCurrentIndex(i)
    voice.edge_combo.activated.emit(i)
    assert wait_call(qtbot, ctl, "set_voice") == ("set_voice", "es-ES-AlvaroNeural", None, False)

    voice.piper_combo.setCurrentIndex(1)
    voice.piper_combo.activated.emit(1)
    wait_call(qtbot, ctl, "set_voice", None, "es_ES-sharvard-medium#M", False)
    assert "M" in window.piper_label.text()

    engines = [voice.engine_combo.itemData(i) for i in range(voice.engine_combo.count())]
    assert engines == ["edge", "piper"]
    voice.engine_combo.setCurrentIndex(1)
    voice.engine_combo.activated.emit(1)
    wait_call(qtbot, ctl, "set_preferred_engine", "piper")


def test_piper_without_voice_links_to_settings(qtbot, make_window):
    window = make_window()
    assert "Sin voz instalada" in window.piper_label.text()
    assert not window.voice.piper_combo.isEnabled()
    with qtbot.waitSignal(window.settings_requested, timeout=WAIT_MS):
        window.piper_label.linkActivated.emit("#configuracion")
    with qtbot.waitSignal(window.settings_requested, timeout=WAIT_MS):
        window.settings_action.trigger()
    assert window.settings_action.text() == "Configuración…"
    assert window.history_action.text() == "Historial…"


def test_settings_and_models_never_touched_on_ui_thread(qtbot, make_window, ctl, conn, tmp_path):
    install_piper(tmp_path / "voces", speakers={"F": 0})
    settings, models = SpySettings(conn), SpyModels(tmp_path / "voces")
    window = make_window(settings=settings, models=models)
    window.voice.engine_combo.setCurrentIndex(1)
    window.voice.engine_combo.activated.emit(1)
    window.voice.speed_buttons[1.25].click()
    window.reload_settings()
    qtbot.waitUntil(lambda: window.settings_loaded, timeout=WAIT_MS)
    wait_call(qtbot, ctl, "set_speed")
    assert settings.threads and models.threads
    assert threading.main_thread() not in settings.threads | models.threads


# --- capítulo ambiguo -------------------------------------------------------------------------


def test_ambiguous_next_choice_in_panel_when_visible(qtbot, make_window, ctl):
    window = make_window(show=True)
    refs = [ChapterRef(None, "Capítulo 2", URL2), ChapterRef(None, "", URL3)]
    window.test_bridge.needs_choice_sig.emit(refs)

    panel = window.choice_panel
    assert panel.isVisible()
    assert [panel.list.item(i).text() for i in range(panel.list.count())] == ["Capítulo 2", URL3]
    panel.list.setCurrentRow(1)
    panel.choose_button.click()
    assert wait_call(qtbot, ctl, "choose_next") == ("choose_next", refs[1])
    assert panel.isHidden()


def test_ambiguous_next_choice_waits_until_window_is_shown(qtbot, make_window, ctl):
    window = make_window(show=False)
    refs = [ChapterRef(None, "Capítulo 2", URL2), ChapterRef(None, "Capítulo 2 (otra)", URL3)]
    window.test_bridge.needs_choice_sig.emit(refs)
    assert window.choice_panel.isHidden()
    assert window.pending_choice == refs

    with qtbot.waitExposed(window):
        window.show()

    assert window.choice_panel.isVisible()
    assert window.choice_panel.list.count() == 2
    assert window.pending_choice is None
    assert ctl.calls == []  # elegir lo decide el usuario


# --- historial, cierre y avisos ------------------------------------------------------------------


def test_history_dialog_lists_and_opens_document(qtbot, make_window, ctl, conn):
    doc = make_doc()
    DocumentRepo(conn).upsert(doc, None, None, None)
    ProgressRepo(conn).save(make_progress(doc, chapter_index=1))
    window = make_window(show=True)

    window.history_action.trigger()
    dialog = window.history_dialog
    qtbot.waitUntil(lambda: dialog.table.rowCount() == 1, timeout=WAIT_MS)
    headers = [dialog.table.horizontalHeaderItem(i).text() for i in range(4)]
    assert headers == ["Título", "Tipo", "Capítulo", "Fecha"]
    row = [dialog.table.item(0, i).text() for i in range(4)]
    assert row[:3] == ["La novela", "Web", "Capítulo 2"]
    assert row[3]  # fecha legible

    dialog.table.cellDoubleClicked.emit(0, 0)
    qtbot.waitUntil(lambda: window.resume_dialog is not None and window.resume_dialog.isVisible(),
                    timeout=WAIT_MS)
    window.resume_dialog.continue_button.click()
    wait_call(qtbot, ctl, "play", "doc-1", None)


def test_history_dialog_without_progress_plays_from_start(qtbot, worker, conn):
    doc = make_doc(kind=SourceKind.EPUB)
    DocumentRepo(conn).upsert(doc, None, None, None)
    dialog = HistoryDialog(worker, DocumentRepo(conn))
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog.table.rowCount() == 1, timeout=WAIT_MS)
    assert [dialog.table.item(0, i).text() for i in range(3)] == ["La novela", "EPUB", "Sin empezar"]
    with qtbot.waitSignal(dialog.document_chosen, timeout=WAIT_MS) as got:
        dialog.table.cellDoubleClicked.emit(0, 2)
    assert got.args[0].doc_id == "doc-1" and got.args[1] is None and got.args[2] is None


def test_close_hides_to_tray(qtbot, make_window):
    window = make_window(show=True)
    with qtbot.waitSignal(window.close_to_tray_requested, timeout=WAIT_MS):
        accepted = window.close()
    assert not accepted
    assert not window.isVisible()
    assert QApplication.instance() is not None

    window.show()
    window.allow_close()  # al salir de la app (T19) la ventana sí se cierra
    with qtbot.assertNotEmitted(window.close_to_tray_requested):
        assert window.close()


def test_controller_notice_is_shown(qtbot, make_window):
    window = make_window(show=True)
    window.test_bridge.notice_sig.emit("info", "No hay más capítulos publicados")
    assert window.notice_label.text() == "No hay más capítulos publicados"
    assert window.notice_label.isVisible()


def test_cancelled_call_shows_nothing(qtbot, make_window, ctl, worker):
    window = make_window(show=True)

    async def never():
        await asyncio.Event().wait()

    fut = worker.submit(never())
    window._call(fut)
    fut.cancel()
    qtbot.wait(100)
    assert window.notice_label.isHidden()


# --- Fix round 1: prefijo de la obra (§5.2) ----------------------------------------------------


def test_generic_web_waits_for_prefix_confirmation(qtbot, make_window, ctl):
    doc = generic_doc()
    ctl.open_result = (doc, make_chapter(), None)
    window = make_window(show=True)

    read(qtbot, window)

    qtbot.waitUntil(lambda: window.preview.isVisible(), timeout=WAIT_MS)
    qtbot.wait(100)
    assert [c[0] for c in ctl.calls] == ["open"]  # no lee sin confirmar
    assert window.preview.prefix_edit.isVisible()
    assert window.preview.prefix_label.text() == "Prefijo de la obra"
    assert window.preview.prefix_edit.text() == "https://example.com/novela/"
    assert "Leer" in window.preview.hint_label.text()

    window.read_button.click()  # confirma sin cambios: lee lo ya abierto
    wait_call(qtbot, ctl, "play", "doc-g", None)
    assert [c[0] for c in ctl.calls] == ["open", "play"]


def test_prefix_field_hidden_for_madara_without_index(qtbot, make_window, ctl):
    ctl.open_result = (make_doc(chapters=[], adapter="madara"), make_chapter(), None)
    window = make_window(show=True)
    read(qtbot, window)
    wait_call(qtbot, ctl, "play", "doc-1", None)
    assert window.preview.prefix_edit.isHidden()


@pytest.mark.parametrize(
    ("typed", "message"),
    [
        ("obras/mi-novela/", "dirección completa"),
        ("ftp://example.com/obras/", "dirección completa"),
        ("https://otra-web.com/obras/", "misma web"),
        ("https://example.com/obras/mi-novela/", "no está dentro"),
    ],
)
def test_invalid_prefix_is_rejected_inline(qtbot, make_window, ctl, conn, typed, message):
    ctl.open_result = (generic_doc(), make_chapter(), None)
    window = make_window(show=True)
    read(qtbot, window)
    qtbot.waitUntil(lambda: window.preview.prefix_edit.isVisible(), timeout=WAIT_MS)

    window.preview.prefix_edit.setText(typed)
    window.read_button.click()

    qtbot.waitUntil(lambda: window.preview.prefix_error_label.isVisible(), timeout=WAIT_MS)
    assert message in window.preview.prefix_error_label.text()
    qtbot.wait(100)
    assert [c[0] for c in ctl.calls] == ["open"]
    assert SettingsRepo(conn).get("work_prefix_overrides") == {}


def test_edited_prefix_is_saved_then_reopened_before_reading(qtbot, make_window, ctl, conn):
    wrong = generic_doc()
    corrected = generic_doc("doc-corregido", "https://example.com/")
    DocumentRepo(conn).upsert(wrong, None, None, None)  # lo guardó la primera apertura
    ctl.open_result = [(wrong, make_chapter(), None), (corrected, make_chapter(), None)]
    settings = SpySettings(conn)
    window = make_window(show=True, settings=settings)
    read(qtbot, window)
    qtbot.waitUntil(lambda: window.preview.prefix_edit.isVisible(), timeout=WAIT_MS)

    window.preview.prefix_edit.setText("https://example.com")
    window.read_button.click()

    wait_call(qtbot, ctl, "play", "doc-corregido", None)
    assert [c[0] for c in ctl.calls] == ["open", "open", "play"]
    assert ctl.calls[1] == ("open", URL)  # se vuelve a abrir con la corrección
    assert SettingsRepo(conn).get("work_prefix_overrides") == {"example.com": ["/"]}
    assert threading.main_thread() not in settings.threads
    assert window.preview.prefix_error_label.isHidden()
    # El documento con el prefijo equivocado no tenía progreso: no queda en el historial.
    qtbot.waitUntil(lambda: DocumentRepo(conn).get("doc-g") is None, timeout=WAIT_MS)


@pytest.fixture
def real_rig(worker, tmp_path):
    """ReadingController real (montaje de pruebas) construido en el bucle del trabajador."""
    import httpx

    from lector.content.registry import open_source
    from lector.security.http_client import SafeFetcher
    from tests.controller_rig import ControllerRig
    from tests.test_chapter_navigation import PAGE_URL, make_handler, page
    from tests.test_chapter_navigation import resolver as generic_resolver

    pages = {PAGE_URL: page('<a href="/novela/x/capitulo-6/">Siguiente</a>'),
             OTHER_WORK_URL: page('<a href="/otra/y/capitulo-2/">Siguiente</a>')}
    holder = {}

    async def build():
        fetcher = SafeFetcher(resolver=generic_resolver,
                              transport=httpx.MockTransport(make_handler(pages)))
        holder["fetcher"] = fetcher
        return ControllerRig(tmp_path / "real.db", fetcher=fetcher, opener=open_source)

    rig = worker.submit(build()).result(timeout=2)
    yield rig

    async def close():
        try:
            await rig.close()
        finally:
            await holder["fetcher"].aclose()

    worker.submit(close()).result(timeout=3)


OTHER_WORK_URL = "https://lector.example/otra/y/capitulo-1/"  # otra obra del mismo host


def test_prefix_correction_end_to_end_with_real_controller(qtbot, worker, real_rig, tmp_path):
    from tests.test_chapter_navigation import HOST, PAGE_URL

    bridge = QtEventBridge()
    window = MainWindow(worker, bridge, real_rig.ctl, real_rig.settings, PiperModelManager(tmp_path / "v"))
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: window.settings_loaded, timeout=WAIT_MS)
    with qtbot.waitExposed(window):
        window.show()

    read(qtbot, window, PAGE_URL)
    qtbot.waitUntil(lambda: window.preview.prefix_edit.isVisible(), timeout=WAIT_MS)
    assert window.preview.prefix_edit.text() == HOST + "/novela/x/"
    assert real_rig.ctl.state is SessionState.IDLE  # espera la confirmación

    window.preview.prefix_edit.setText(HOST + "/novela")
    window.read_button.click()

    qtbot.waitUntil(lambda: bool(real_rig.edge.calls), timeout=WAIT_MS)  # ya lee
    host = HOST.split("://", 1)[1]
    assert real_rig.settings.get("work_prefix_overrides") == {host: ["/novela/"]}
    doc_id = doc_id_for_url(host + "/novela/")
    assert real_rig.repos.documents.get(doc_id).work_prefix == HOST + "/novela/"
    assert window.preview.next_label.text() == (
        "Siguiente detectado: https://lector.example/novela/x/capitulo-6/"
    )
    wrong_id = doc_id_for_url(host + "/novela/x/")
    qtbot.waitUntil(lambda: real_rig.repos.documents.get(wrong_id) is None, timeout=WAIT_MS)

    # La corrección se usa en la siguiente apertura de esa obra...
    doc, _, _ = worker.submit(real_rig.ctl.open(PAGE_URL)).result(timeout=2)
    assert doc.work_prefix == HOST + "/novela/" and doc.doc_id == doc_id
    # ...pero no en otra obra del mismo host.
    other, _, _ = worker.submit(real_rig.ctl.open(OTHER_WORK_URL)).result(timeout=2)
    assert other.work_prefix == HOST + "/otra/y/"


# --- Fix round 1: novela abierta desde su índice (§4) -------------------------------------------


def test_work_page_lets_user_pick_chapter_with_first_preselected(qtbot, make_window, ctl):
    doc = make_doc(index_page=True)
    ctl.open_result = (doc, make_chapter(), None)
    window = make_window(show=True)
    read(qtbot, window, "https://example.com/novela/")

    qtbot.waitUntil(lambda: window.preview.chapter_list.isVisible(), timeout=WAIT_MS)
    qtbot.wait(100)
    assert [c[0] for c in ctl.calls] == ["open"]
    chapters = window.preview.chapter_list
    assert [chapters.item(i).text() for i in range(chapters.count())] == [
        "Capítulo 1", "Capítulo 2", "Capítulo 3",
    ]
    assert chapters.currentRow() == 0

    chapters.setCurrentRow(2)
    window.read_button.click()
    _, _, start = wait_call(qtbot, ctl, "play")
    assert start == Position(2, URL3, 0, 0, 0, None)


def test_chapter_page_hides_chapter_list(qtbot, make_window, ctl):
    ctl.open_result = (make_doc(), make_chapter(), None)
    window = make_window(show=True)
    read(qtbot, window)
    wait_call(qtbot, ctl, "play", "doc-1", None)
    assert window.preview.chapter_list.isHidden()


def test_work_page_default_starts_at_chapter_one(qtbot, make_window, ctl):
    ctl.open_result = (make_doc(index_page=True), make_chapter(), None)
    window = make_window(show=True)
    read(qtbot, window, "https://example.com/novela/")
    qtbot.waitUntil(lambda: window.preview.chapter_list.isVisible(), timeout=WAIT_MS)
    window.read_button.click()
    _, _, start = wait_call(qtbot, ctl, "play")
    assert start == Position(0, URL, 0, 0, 0, None)


# --- Fix round 1: oferta de reanudar y ventana oculta -------------------------------------------


def wait_dialog(qtbot, window):
    qtbot.waitUntil(lambda: window.resume_dialog is not None and window.resume_dialog.isVisible(),
                    timeout=WAIT_MS)
    return window.resume_dialog


def test_inactive_window_continues_instead_of_dialog(qtbot, make_window, ctl, monkeypatch):
    doc = make_doc()
    ctl.open_result = (doc, make_chapter(), make_progress(doc))
    window = make_window(show=True)
    monkeypatch.setattr(window, "isActiveWindow", lambda: False)
    monkeypatch.setattr(QApplication, "activeWindow", staticmethod(lambda: None))  # el juego delante

    read(qtbot, window)

    wait_call(qtbot, ctl, "play", "doc-1", None)
    assert window.resume_dialog is None
    assert window.notice_label.text() == "Se sigue leyendo donde lo dejaste."


@pytest.mark.parametrize("how", ["close", "minimize", "hide"])
def test_pending_dialog_continues_when_window_goes_away(qtbot, make_window, ctl, how):
    doc = make_doc()
    ctl.open_result = (doc, make_chapter(), make_progress(doc))
    window = make_window(show=True)
    read(qtbot, window)
    dialog = wait_dialog(qtbot, window)

    {"close": window.close, "minimize": window.showMinimized, "hide": window.hide}[how]()

    wait_call(qtbot, ctl, "play", "doc-1", None)
    assert not dialog.isVisible()
    assert window.notice_label.text() == "Se sigue leyendo donde lo dejaste."
    qtbot.wait(100)
    assert [c[0] for c in ctl.calls].count("play") == 1


def test_offer_without_auto_continue_waits_for_window(qtbot, make_window, ctl):
    doc = make_doc()
    window = make_window(show=False)

    window.offer_resume(doc, make_progress(doc), auto_continue_when_hidden=False)

    qtbot.wait(100)
    assert ctl.calls == [] and window.resume_dialog is None
    with qtbot.waitExposed(window):
        window.show()
    dialog = wait_dialog(qtbot, window)

    window.hide()  # sigue pendiente: no se lee sin que el usuario lo pida
    qtbot.wait(100)
    assert not dialog.isVisible() and ctl.calls == []
    window.show()
    dialog = wait_dialog(qtbot, window)
    dialog.start_button.click()
    _, _, start = wait_call(qtbot, ctl, "play")
    assert start == Position(0, URL, 0, 0, 0, None)


def test_resume_dialog_shows_known_chapter_title(qtbot, make_window, ctl):
    doc = generic_doc()
    chapter = make_chapter(URL, "Capítulo 7: El regreso")
    record = make_progress(make_doc())  # guardado en URL2...
    record.position = Position(6, URL, 3, 3, 0, None)  # ...pero aquí en el capítulo abierto
    ctl.open_result = (doc, chapter, record)
    window = make_window(show=True)
    read(qtbot, window)
    assert "Capítulo 7: El regreso" in wait_dialog(qtbot, window).where_label.text()


def test_history_shows_stored_chapter_title(qtbot, worker, conn):
    from lector.persistence.repositories import ChapterRepo

    doc = generic_doc()
    DocumentRepo(conn).upsert(doc, None, None, None)
    ChapterRepo(conn).save(doc.doc_id, make_chapter(URL, "Capítulo 7: El regreso"), False)
    record = make_progress(make_doc())
    record.doc_id, record.position = doc.doc_id, Position(6, URL, 3, 3, 0, None)
    ProgressRepo(conn).save(record)
    dialog = HistoryDialog(worker, DocumentRepo(conn), chapters=ChapterRepo(conn))
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog.table.rowCount() == 1, timeout=WAIT_MS)
    assert dialog.table.item(0, 2).text() == "Capítulo 7: El regreso"
    with qtbot.waitSignal(dialog.document_chosen, timeout=WAIT_MS) as got:
        dialog.table.cellDoubleClicked.emit(0, 0)
    assert got.args[2] == "Capítulo 7: El regreso"


# --- Fix round 1: elección de capítulo caducada ------------------------------------------------


def test_choice_panel_cleared_when_another_document_opens(qtbot, make_window, ctl):
    window = make_window(show=True)
    refs = [ChapterRef(None, "A", URL2), ChapterRef(None, "B", URL3)]
    window.test_bridge.needs_choice_sig.emit(refs)
    assert window.choice_panel.isVisible()

    ctl.open_result = (make_doc(doc_id="otro"), make_chapter(), None)
    read(qtbot, window, URL3)
    wait_call(qtbot, ctl, "play")
    window.test_bridge.document_sig.emit(make_doc(doc_id="otro"), make_chapter())

    assert window.choice_panel.isHidden()
    assert window.pending_choice is None


def test_choice_cleared_when_chapter_changes(qtbot, make_window, ctl):
    window = make_window(show=True)
    bridge = window.test_bridge
    bridge.document_sig.emit(make_doc(), make_chapter(URL, "Capítulo 1"))
    bridge.needs_choice_sig.emit([ChapterRef(None, "A", URL2), ChapterRef(None, "B", URL3)])
    bridge.document_sig.emit(make_doc(), make_chapter(URL, "Capítulo 1"))  # mismo capítulo
    assert window.choice_panel.isVisible()

    bridge.document_sig.emit(make_doc(), make_chapter(URL2, "Capítulo 2"))

    assert window.choice_panel.isHidden()


def test_pending_choice_dropped_when_session_changes_while_hidden(qtbot, make_window, ctl, conn):
    doc = make_doc()
    DocumentRepo(conn).upsert(doc, None, None, None)
    window = make_window(show=True)
    window.test_bridge.document_sig.emit(doc, make_chapter())
    window.hide()
    window.test_bridge.needs_choice_sig.emit([ChapterRef(None, "A", URL2)])
    assert window.pending_choice is not None
    window.test_bridge.document_sig.emit(make_doc(doc_id="otro"), make_chapter())

    with qtbot.waitExposed(window):
        window.show()

    assert window.choice_panel.isHidden() and window.pending_choice is None


def test_choose_next_only_for_the_session_of_the_candidates(qtbot, make_window, ctl):
    window = make_window(show=True)
    window.test_bridge.document_sig.emit(make_doc(), make_chapter())
    refs = [ChapterRef(None, "A", URL2), ChapterRef(None, "B", URL3)]
    window.test_bridge.needs_choice_sig.emit(refs)
    window._play("otro", None)  # empieza otra lectura antes de que llegue su documento

    assert window.choice_panel.isHidden()
    window.choice_panel.chosen.emit(refs[0])  # aunque llegara una elección vieja
    wait_call(qtbot, ctl, "play", "otro", None)
    qtbot.wait(100)
    assert not any(c[0] == "choose_next" for c in ctl.calls)


# --- Fix round 1: menores ------------------------------------------------------------------------


def test_read_does_not_restart_document_already_reading(qtbot, make_window, ctl):
    ctl.open_result = (make_doc(), make_chapter(), None)
    window = make_window(show=True)
    read(qtbot, window)
    wait_call(qtbot, ctl, "play", "doc-1", None)
    for state in ("loading", "reading", "waiting_network"):
        window.test_bridge.session_state_sig.emit(state)
        window.read_button.click()
    qtbot.wait(100)
    assert [c[0] for c in ctl.calls] == ["open", "play"]

    window.test_bridge.session_state_sig.emit("paused")
    window.read_button.click()  # en pausa, «Leer» reanuda
    wait_call(qtbot, ctl, "resume")
    assert [c[0] for c in ctl.calls].count("play") == 1


def test_double_click_sends_a_single_play(qtbot, make_window, ctl):
    ctl.open_result = (generic_doc(), make_chapter(), None)
    ctl.play_gate = threading.Event()
    window = make_window(show=True)
    read(qtbot, window)
    qtbot.waitUntil(lambda: window.preview.prefix_edit.isVisible(), timeout=WAIT_MS)
    try:
        window.read_button.click()
        window.read_button.click()
        window.controls.play_pause_button.click()
        wait_call(qtbot, ctl, "play")
        qtbot.wait(100)
        assert [c[0] for c in ctl.calls].count("play") == 1
    finally:
        ctl.play_gate.set()


def test_notice_cleared_when_reading_resumes_after_warning(qtbot, make_window):
    window = make_window(show=True)
    bridge = window.test_bridge
    bridge.session_state_sig.emit("reading")
    bridge.notice_sig.emit("warn", "La voz en línea no está disponible por ahora.")
    bridge.session_state_sig.emit("waiting_network")
    assert window.notice_label.isVisible()

    bridge.session_state_sig.emit("reading")

    assert window.notice_label.isHidden()
    bridge.notice_sig.emit("info", "Terminó el capítulo.")
    bridge.session_state_sig.emit("completed")
    bridge.session_state_sig.emit("reading")
    assert window.notice_label.isVisible()  # los avisos informativos se quedan


class BrokenSettings(SettingsRepo):
    def get(self, key):
        raise RuntimeError("disco roto")


def test_settings_load_failure_message(qtbot, make_window, conn, caplog):
    with caplog.at_level(logging.ERROR):
        window = make_window(show=True, settings=BrokenSettings(conn))
    assert window.notice_label.text() == (
        "No se pudo cargar la configuración. Se usan los valores por defecto."
    )
    assert any(r.exc_info for r in caplog.records)


@pytest.mark.parametrize(
    ("state", "label", "expected"),
    [
        ("loading", "Pausar", ("pause",)),
        ("waiting_network", "Pausar", ("pause",)),
        ("needs_attention", "Leer", ("play", "doc-1", None)),
        ("error", "Leer", ("play", "doc-1", None)),
        ("completed", "Leer", ("play", "doc-1", None)),
    ],
)
def test_play_pause_label_and_action_per_state(qtbot, make_window, ctl, state, label, expected):
    window = make_window()
    window.test_bridge.document_sig.emit(make_doc(), make_chapter())
    window.test_bridge.session_state_sig.emit(state)
    assert window.controls.play_pause_button.text() == label
    window.controls.play_pause_button.click()
    assert wait_call(qtbot, ctl, expected[0]) == expected


def test_minimize_state_change_alone_continues(qtbot, make_window, ctl, monkeypatch):
    """Algunas plataformas solo avisan del minimizado con WindowStateChange (sin hideEvent)."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QWindowStateChangeEvent

    doc = make_doc()
    ctl.open_result = (doc, make_chapter(), make_progress(doc))
    window = make_window(show=True)
    read(qtbot, window)
    wait_dialog(qtbot, window)

    monkeypatch.setattr(window, "isMinimized", lambda: True)
    QApplication.sendEvent(window, QWindowStateChangeEvent(Qt.WindowState.WindowNoState))

    assert not window.resume_dialog.isVisible()  # en el acto, sin esperar a otra comprobación
    wait_call(qtbot, ctl, "play", "doc-1", None)


def test_choice_panel_cleared_when_opening_even_without_reading(qtbot, make_window, ctl):
    window = make_window(show=True)
    window.test_bridge.needs_choice_sig.emit([ChapterRef(None, "A", URL2)])
    ctl.open_result = (generic_doc(), make_chapter(), None)  # espera confirmación: no hay play

    read(qtbot, window, URL3)

    qtbot.waitUntil(lambda: window.preview.isVisible(), timeout=WAIT_MS)
    assert window.choice_panel.isHidden()
    assert not any(c[0] == "play" for c in ctl.calls)


# --- Fix round 2: «Leer» y el usuario se va (sin confirmar) -------------------------------------

STARTED_TEXT = "Se empezó a leer con el prefijo y el capítulo detectados."


def leave(window, monkeypatch, how: str) -> None:
    """El usuario se va al juego: a la bandeja, minimiza o la app pierde el foco."""
    if how == "hidden":
        window.close()
    elif how == "minimized":
        window.showMinimized()
    else:
        monkeypatch.setattr(window, "isActiveWindow", lambda: False)
        monkeypatch.setattr(QApplication, "activeWindow", staticmethod(lambda: None))
        QApplication.instance().applicationStateChanged.emit(Qt.ApplicationState.ApplicationInactive)


def detected_start(kind: str):
    return None if kind == "generic" else Position(0, URL, 0, 0, 0, None)


def open_for(kind: str):
    if kind == "generic":
        return generic_doc(), URL
    return make_doc(index_page=True), "https://example.com/novela/"


@pytest.mark.parametrize("how", ["hidden", "inactive", "minimized"])
@pytest.mark.parametrize("kind", ["generic", "work_page"])
def test_leer_then_user_leaves_before_open_returns(qtbot, make_window, ctl, monkeypatch, how, kind):
    doc, locator = open_for(kind)
    ctl.open_result = (doc, make_chapter(), None)
    ctl.open_block_s = 0.3
    window = make_window(show=True)

    read(qtbot, window, locator)
    leave(window, monkeypatch, how)

    _, doc_id, start = wait_call(qtbot, ctl, "play")
    assert (doc_id, start) == (doc.doc_id, detected_start(kind))
    assert window.notice_label.text() == STARTED_TEXT
    assert not window.preview.needs_confirmation()


@pytest.mark.parametrize("how", ["hidden", "inactive", "minimized"])
@pytest.mark.parametrize("kind", ["generic", "work_page"])
def test_pending_confirmation_auto_confirms_when_user_leaves(qtbot, make_window, ctl, monkeypatch,
                                                              how, kind):
    doc, locator = open_for(kind)
    ctl.open_result = (doc, make_chapter(), None)
    window = make_window(show=True)
    read(qtbot, window, locator)
    qtbot.waitUntil(lambda: window.preview.needs_confirmation(), timeout=WAIT_MS)
    if kind == "work_page":
        window.preview.chapter_list.setCurrentRow(2)  # lo detectado manda: el capítulo 1
    else:
        window.preview.prefix_edit.setText("https://example.com/")  # lo detectado manda

    leave(window, monkeypatch, how)

    _, doc_id, start = wait_call(qtbot, ctl, "play")
    assert (doc_id, start) == (doc.doc_id, detected_start(kind))
    assert [c[0] for c in ctl.calls] == ["open", "play"]
    assert window.notice_label.text() == STARTED_TEXT


def test_own_dialog_taking_focus_is_not_leaving(qtbot, make_window, ctl, monkeypatch):
    ctl.open_result = (generic_doc(), make_chapter(), None)
    window = make_window(show=True)
    read(qtbot, window)
    qtbot.waitUntil(lambda: window.preview.needs_confirmation(), timeout=WAIT_MS)
    window.history_action.trigger()  # el historial (de la app) toma el foco
    monkeypatch.setattr(window, "isActiveWindow", lambda: False)
    monkeypatch.setattr(QApplication, "activeWindow", staticmethod(lambda: window.history_dialog))
    QApplication.instance().applicationStateChanged.emit(Qt.ApplicationState.ApplicationInactive)

    qtbot.wait(600)

    assert [c[0] for c in ctl.calls] == ["open"]
    assert window.preview.needs_confirmation()


def test_open_file_without_leer_never_auto_confirms(qtbot, make_window, ctl, monkeypatch):
    """Solo «Leer» pide leer: abrir un archivo para verlo no empieza aunque el usuario se vaya."""
    from lector.ui import main_window as mw

    ctl.open_result = (make_doc(index_page=True), make_chapter(), None)
    monkeypatch.setattr(mw.QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: ("/tmp/libro.epub", "")))
    window = make_window(show=True)
    window.open_file_button.click()
    qtbot.waitUntil(lambda: window.preview.needs_confirmation(), timeout=WAIT_MS)

    window.close()

    qtbot.wait(300)
    assert [c[0] for c in ctl.calls] == ["open"]


def test_resume_dialog_continues_when_app_loses_focus(qtbot, make_window, ctl, monkeypatch):
    doc = make_doc()
    ctl.open_result = (doc, make_chapter(), make_progress(doc))
    window = make_window(show=True)
    read(qtbot, window)
    dialog = wait_dialog(qtbot, window)

    leave(window, monkeypatch, "inactive")  # alt-tab al juego, sin minimizar

    wait_call(qtbot, ctl, "play", "doc-1", None)
    assert not dialog.isVisible()
    assert window.notice_label.text() == "Se sigue leyendo donde lo dejaste."


def test_resume_dialog_without_auto_continue_stays_when_app_loses_focus(qtbot, make_window, ctl,
                                                                        monkeypatch):
    doc = make_doc()
    window = make_window(show=True)
    window.offer_resume(doc, make_progress(doc), auto_continue_when_hidden=False)
    dialog = wait_dialog(qtbot, window)

    leave(window, monkeypatch, "inactive")

    qtbot.wait(600)
    assert ctl.calls == [] and dialog.isVisible()


def test_app_focus_back_before_the_check_is_not_leaving(qtbot, make_window, ctl, monkeypatch):
    """Un parpadeo del foco (inactiva y activa enseguida) no cuenta como irse."""
    ctl.open_result = (generic_doc(), make_chapter(), None)
    window = make_window(show=True)
    read(qtbot, window)
    qtbot.waitUntil(lambda: window.preview.needs_confirmation(), timeout=WAIT_MS)
    app = QApplication.instance()
    with monkeypatch.context() as away:  # sin foco solo un instante
        away.setattr(window, "isActiveWindow", lambda: False)
        away.setattr(QApplication, "activeWindow", staticmethod(lambda: None))
        app.applicationStateChanged.emit(Qt.ApplicationState.ApplicationInactive)
    app.applicationStateChanged.emit(Qt.ApplicationState.ApplicationActive)

    qtbot.wait(600)

    assert [c[0] for c in ctl.calls] == ["open"]


# --- Fix round 2: prefijo (normalización y borrado) ----------------------------------------------


def test_same_prefix_without_slash_is_a_no_op(qtbot, make_window, ctl, conn):
    ctl.open_result = (generic_doc(), make_chapter(), None)
    window = make_window(show=True)
    read(qtbot, window)
    qtbot.waitUntil(lambda: window.preview.prefix_edit.isVisible(), timeout=WAIT_MS)

    window.preview.prefix_edit.setText("https://example.com/novela")
    window.read_button.click()

    wait_call(qtbot, ctl, "play", "doc-g", None)
    assert [c[0] for c in ctl.calls] == ["open", "play"]  # sin reabrir
    assert SettingsRepo(conn).get("work_prefix_overrides") == {}


def test_clearing_the_prefix_removes_the_override_for_that_work(qtbot, make_window, ctl, conn):
    SettingsRepo(conn).set("work_prefix_overrides", {"example.com": ["/novela/", "/otra/"]})
    detected = generic_doc("doc-detectado", "https://example.com/novela/capitulo-1/")
    ctl.open_result = [(generic_doc(), make_chapter(), None), (detected, make_chapter(), None)]
    window = make_window(show=True)
    read(qtbot, window)
    qtbot.waitUntil(lambda: window.preview.prefix_edit.isVisible(), timeout=WAIT_MS)

    window.preview.prefix_edit.setText("")
    window.read_button.click()

    wait_call(qtbot, ctl, "play", "doc-detectado", None)
    assert [c[0] for c in ctl.calls] == ["open", "open", "play"]
    assert SettingsRepo(conn).get("work_prefix_overrides") == {"example.com": ["/otra/"]}


def test_corrected_prefix_keeps_wrong_document_if_it_has_progress(qtbot, make_window, ctl, conn):
    wrong = generic_doc()
    DocumentRepo(conn).upsert(wrong, None, None, None)
    record = make_progress(make_doc())
    record.doc_id = wrong.doc_id
    ProgressRepo(conn).save(record)
    corrected = generic_doc("doc-corregido", "https://example.com/")
    ctl.open_result = [(wrong, make_chapter(), None), (corrected, make_chapter(), None)]
    window = make_window(show=True)
    read(qtbot, window)
    qtbot.waitUntil(lambda: window.preview.prefix_edit.isVisible(), timeout=WAIT_MS)

    window.preview.prefix_edit.setText("https://example.com/")
    window.read_button.click()

    wait_call(qtbot, ctl, "play", "doc-corregido", None)
    qtbot.wait(200)
    assert DocumentRepo(conn).get("doc-g") is not None


def test_clearing_prefix_without_saved_override_keeps_the_same_document(qtbot, make_window, ctl,
                                                                       conn):
    doc = generic_doc()
    DocumentRepo(conn).upsert(doc, None, None, None)
    ctl.open_result = (doc, make_chapter(), None)  # sin corrección guardada: lo mismo
    window = make_window(show=True)
    read(qtbot, window)
    qtbot.waitUntil(lambda: window.preview.prefix_edit.isVisible(), timeout=WAIT_MS)

    window.preview.prefix_edit.setText("")
    window.read_button.click()

    wait_call(qtbot, ctl, "play", "doc-g", None)
    qtbot.wait(200)
    assert DocumentRepo(conn).get("doc-g") is not None  # es el que se lee: no se borra


# --- Fix round 2: índice tras parar, reapertura caducada y pausa ---------------------------------


@pytest.mark.parametrize("ended", ["stopped", "completed", "needs_attention", "error"])
def test_leer_on_work_page_after_session_ends_continues(qtbot, make_window, ctl, ended):
    doc = make_doc(index_page=True)
    ctl.open_result = (doc, make_chapter(), None)
    window = make_window(show=True)
    read(qtbot, window, "https://example.com/novela/")
    qtbot.waitUntil(lambda: window.preview.chapter_list.isVisible(), timeout=WAIT_MS)
    window.read_button.click()
    wait_call(qtbot, ctl, "play", "doc-1", Position(0, URL, 0, 0, 0, None))
    assert not window.preview.needs_confirmation()
    assert window.preview.chapter_list.isHidden()
    bridge = window.test_bridge
    bridge.document_sig.emit(doc, make_chapter(URL3, "Capítulo 3"))  # ya va por el capítulo 3
    bridge.session_state_sig.emit("reading")
    bridge.session_state_sig.emit(ended)
    ctl.calls.clear()

    window.read_button.click()  # misma URL en el campo

    assert wait_call(qtbot, ctl, "play") == ("play", "doc-1", None)  # sigue donde iba


def test_history_pick_wins_over_prefix_reopen_in_flight(qtbot, make_window, ctl):
    corrected = generic_doc("doc-corregido", "https://example.com/")
    ctl.open_result = [(generic_doc(), make_chapter(), None), (corrected, make_chapter(), None)]
    window = make_window(show=True)
    read(qtbot, window)
    qtbot.waitUntil(lambda: window.preview.prefix_edit.isVisible(), timeout=WAIT_MS)
    ctl.open_block_s = 0.4
    window.preview.prefix_edit.setText("https://example.com/")
    window.read_button.click()
    wait_call(qtbot, ctl, "open")
    qtbot.waitUntil(lambda: len([c for c in ctl.calls if c[0] == "open"]) == 2, timeout=WAIT_MS)

    window._open_from_history(make_doc(doc_id="hist", kind=SourceKind.EPUB), None, None)

    wait_call(qtbot, ctl, "play", "hist", None)
    qtbot.wait(700)  # la reapertura termina después: se descarta
    assert [c for c in ctl.calls if c[0] == "play"] == [("play", "hist", None)]
    assert window.read_button.isEnabled()


def test_pause_allowed_while_play_is_pending(qtbot, make_window, ctl):
    ctl.open_result = (make_doc(), make_chapter(), None)
    ctl.play_gate = threading.Event()
    window = make_window(show=True)
    try:
        read(qtbot, window)
        wait_call(qtbot, ctl, "play")
        window.test_bridge.session_state_sig.emit("loading")
        window.controls.play_pause_button.click()
        wait_call(qtbot, ctl, "pause")
        window.read_button.click()  # un segundo play sigue bloqueado
        qtbot.wait(100)
        assert [c[0] for c in ctl.calls].count("play") == 1
    finally:
        ctl.play_gate.set()


def test_window_losing_activation_to_another_app_auto_confirms(qtbot, make_window, ctl,
                                                               monkeypatch):
    """Sin aviso de la app (p. ej. algunas plataformas): basta con que la ventana pierda el foco."""
    from PySide6.QtCore import QEvent

    ctl.open_result = (generic_doc(), make_chapter(), None)
    window = make_window(show=True)
    read(qtbot, window)
    qtbot.waitUntil(lambda: window.preview.needs_confirmation(), timeout=WAIT_MS)
    monkeypatch.setattr(window, "isActiveWindow", lambda: False)
    monkeypatch.setattr(QApplication, "activeWindow", staticmethod(lambda: None))
    QApplication.sendEvent(window, QEvent(QEvent.Type.ActivationChange))

    wait_call(qtbot, ctl, "play", "doc-g", None)
    assert window.notice_label.text() == STARTED_TEXT
