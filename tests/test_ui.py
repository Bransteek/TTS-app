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
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

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
             doc_id: str = "doc-1", title: str = "La novela") -> Document:
    if chapters is None:
        chapters = [ChapterRef(0, "Capítulo 1", URL), ChapterRef(1, "Capítulo 2", URL2),
                    ChapterRef(2, "Capítulo 3", URL3)]
    origin = "https://example.com/novela/" if kind is SourceKind.URL else "/tmp/libro.pdf"
    return Document(doc_id, kind, origin, title, origin, origin, None, chapters)


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
        self.open_result: tuple | BaseException | None = None
        self.open_block_s = 0.0  # bloquea el hilo del trabajador (apertura lenta)
        self.play_error: BaseException | None = None
        self.page_position: Position | None = None

    def _record(self, *call) -> None:
        self.threads.add(threading.current_thread())
        self.calls.append(call)

    async def open(self, locator):
        self._record("open", locator)
        if self.open_block_s:
            time.sleep(self.open_block_s)
        if isinstance(self.open_result, BaseException):
            raise self.open_result
        return self.open_result

    async def play(self, doc_id, start=None):
        self._record("play", doc_id, start)
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
            window.show()
            qtbot.waitExposed(window)
        return window

    return build


def wait_call(qtbot, ctl: FakeController, name: str, *args) -> tuple:
    """Espera (con tope) a que el controlador reciba esa llamada y la devuelve."""
    def found():
        return [c for c in ctl.calls if c[0] == name and (not args or c[1:] == args)]

    qtbot.waitUntil(lambda: bool(found()), timeout=WAIT_MS)
    return found()[-1]


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


def test_prefix_field_only_for_generic_web_and_saved(qtbot, make_window, ctl, conn):
    doc = make_doc(chapters=[])
    ctl.open_result = (doc, make_chapter(), None)
    settings = SpySettings(conn)
    window = make_window(show=True, settings=settings)
    read(qtbot, window)
    wait_call(qtbot, ctl, "play")

    prefix = window.preview.prefix_edit
    assert prefix.isVisible()
    assert window.preview.prefix_label.text() == "Prefijo de la obra"
    assert prefix.text() == "https://example.com/novela/"
    prefix.setText("https://example.com/obras/mi-novela/")
    prefix.editingFinished.emit()
    qtbot.waitUntil(
        lambda: SettingsRepo(conn).get("work_prefix_overrides") == {"example.com": "/obras/mi-novela/"},
        timeout=WAIT_MS,
    )
    assert threading.main_thread() not in settings.threads


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

    window.show()
    qtbot.waitExposed(window)

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
    assert got.args[0].doc_id == "doc-1" and got.args[1] is None


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
