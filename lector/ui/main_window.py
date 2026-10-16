"""Ventana principal: entrada, vista previa, lectura en curso, controles, voz y estado.

Hilos: la ventana vive en el hilo de Qt y nunca hace E/S. Cada llamada al
controlador (y cada lectura o escritura de la configuración, de la caché de
voces o de los modelos de Piper) va al hilo de trabajo con `worker.submit`, y
su resultado vuelve aquí con `call_ui`. Los avisos del controlador llegan como
señales del `QtEventBridge`, entregadas en cola en este hilo.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QAction, QCloseEvent, QPalette, QShowEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from lector.errors import LectorError
from lector.models import ChapterRef, Document, Position
from lector.persistence.repositories import (
    DocumentRepo,
    ProgressRecord,
    SettingsRepo,
    VoicesCacheRepo,
)
from lector.services.reading_controller import SessionState
from lector.services.worker import AsyncWorker, QtEventBridge, call_ui
from lector.tts.engine_manager import EDGE_LABEL, PIPER_LABEL, EngineState
from lector.tts.piper_engine import PiperModelManager
from lector.ui.history_dialog import HistoryDialog
from lector.ui.playback_controls import PlaybackControls, VoicePanel
from lector.ui.preview_panel import ChoicePanel, PreviewPanel
from lector.ui.resume_dialog import ResumeChoice, ResumeDialog, chapter_with_page

log = logging.getLogger(__name__)

GENERIC_ERROR = (
    "Ocurrió un problema inesperado. La lectura se ha pausado y tu progreso está guardado."
)
NOTHING_TO_READ = "Pega la dirección de un capítulo o abre un archivo PDF o EPUB."
CONTINUING = "Se sigue leyendo donde lo dejaste."
PREFIX_SAVED = "Prefijo guardado. Se usará la próxima vez que abras esta web."
WAITING_TEXT = "Esperando conexión…"
EDGE_TEXT = {
    EngineState.EDGE_ACTIVE: "Conectado",
    EngineState.EDGE_RETRYING: "Reintentando…",
    EngineState.EDGE_RECOVERY_TEST: "Reintentando…",
    EngineState.PIPER_ACTIVE: "Sin conexión",
    EngineState.WAITING_NETWORK: "Sin conexión",
}
_PLAYING_STATES = frozenset({SessionState.READING, SessionState.LOADING,
                             SessionState.WAITING_NETWORK})
_GENDER = {"Female": "mujer", "Male": "hombre"}
_SETTING_KEYS = ("edge_voice", "language", "piper_voice", "piper_speaker", "speed",
                 "continuous", "skip_seconds", "preferred_engine", "highlight_sentence")


@dataclass
class _Loaded:
    values: dict[str, Any]
    edge_voices: list[dict]
    piper: list[tuple[str, list[str]]]  # (modelo instalado, hablantes)


def _read_state(settings: SettingsRepo, voices: VoicesCacheRepo,
                models: PiperModelManager) -> _Loaded:
    """Lee la configuración, la caché de voces y los modelos (en el hilo de trabajo)."""
    values = {key: settings.get(key) for key in _SETTING_KEYS}
    try:
        edge_voices = voices.load()
    except Exception:
        log.warning("No se pudo leer la caché de voces de Edge", exc_info=True)
        edge_voices = []
    piper = [(name, models.speakers(name)) for name in models.installed()]
    return _Loaded(values, edge_voices, piper)


def _edge_items(voices: list[dict], language: str, current: str) -> list[tuple[str, str]]:
    family = (language or "es").split("-")[0].lower()
    items = []
    for v in sorted(voices, key=lambda v: (v.get("Locale", ""), v.get("ShortName", ""))):
        short, locale = v.get("ShortName", ""), v.get("Locale", "")
        if not short or locale.split("-")[0].lower() != family:
            continue
        name = short.rsplit("-", 1)[-1].removesuffix("Neural") or short
        gender = _GENDER.get(v.get("Gender", ""))
        items.append((f"{name} — {locale}" + (f", {gender}" if gender else ""), short))
    if current and all(data != current for _, data in items):
        items.insert(0, (current, current))  # la voz elegida siempre aparece
    return items


def _piper_items(installed: list[tuple[str, list[str]]]) -> list[tuple[str, str, str]]:
    """(texto del desplegable, voz «nombre#hablante», nombre corto para el indicador)."""
    labels = {m["name"]: m["label"] for m in PiperModelManager.CATALOG}
    out = []
    for name, speakers in installed:
        label = labels.get(name, name)
        short = label.split(" (")[0]
        if speakers:
            out += [(f"{label} – voz {s}", f"{name}#{s}", f"{short} {s}") for s in speakers]
        else:
            out.append((label, name, short))
    return out


def _chapter_start(index: int, ref: ChapterRef, page: int | None = None) -> Position:
    return Position(ref.index if ref.index is not None else index, ref.locator, 0, 0, 0, page)


class MainWindow(QMainWindow):
    close_to_tray_requested = Signal()  # al cerrar: la app pasa a la bandeja y sigue leyendo
    settings_requested = Signal()  # «Configuración…» o el enlace de Piper (diálogo de T18)

    def __init__(
        self,
        worker: AsyncWorker,
        bridge: QtEventBridge,
        controller,
        settings: SettingsRepo,
        models: PiperModelManager,
        *,
        documents: DocumentRepo | None = None,
        voices: VoicesCacheRepo | None = None,
    ):
        super().__init__()
        self._worker = worker
        self._ctl = controller
        self._settings = settings
        self._models = models
        self._documents = documents or DocumentRepo(settings.connection)
        self._voices = voices or VoicesCacheRepo(settings.connection)

        self._state = SessionState.IDLE
        self._doc_id: str | None = None
        self._preview: tuple[str, Document, Any] | None = None  # (locator, doc, capítulo)
        self._opening = False
        self._preferred = "edge"
        self._engine_seen = False
        self._highlight = True
        self._piper_short: dict[str, str] = {}
        self.settings_loaded = False
        self.resume_dialog: ResumeDialog | None = None
        self.history_dialog: HistoryDialog | None = None
        self.pending_choice: list[ChapterRef] | None = None
        self._quitting = False

        self.setWindowTitle("Lector")
        self._build()
        self._connect(bridge)
        self.reload_settings()

    # --- construcción -------------------------------------------------------------------

    def _build(self) -> None:
        self.notice_label = QLabel()
        self.notice_label.setWordWrap(True)
        self.notice_label.setFrameShape(QFrame.Shape.StyledPanel)
        self.notice_label.setMargin(6)
        self.notice_label.hide()

        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("Pega la URL de un capítulo o de la novela")
        self.open_file_button = QPushButton("Abrir PDF/EPUB…")
        self.read_button = QPushButton("Leer")
        self.read_button.setDefault(True)
        input_row = QHBoxLayout()
        input_row.addWidget(self.url_edit, 1)
        input_row.addWidget(self.open_file_button)
        input_row.addWidget(self.read_button)

        self.preview = PreviewPanel()
        self.preview.hide()
        self.choice_panel = ChoicePanel()
        self.choice_panel.hide()

        self.doc_title_label = QLabel()
        font = self.doc_title_label.font()
        font.setBold(True)
        self.doc_title_label.setFont(font)
        self.doc_title_label.setWordWrap(True)
        self.chapter_label = QLabel()
        self.chapter_label.setWordWrap(True)
        self.sentence_label = QLabel()
        self.sentence_label.setWordWrap(True)
        self.sentence_label.setTextFormat(Qt.TextFormat.PlainText)
        self.sentence_label.setMargin(10)
        self.sentence_label.setMinimumHeight(80)
        big = self.sentence_label.font()
        big.setPointSizeF(big.pointSizeF() * 1.4)
        self.sentence_label.setFont(big)
        palette = self.sentence_label.palette()
        soft = palette.color(QPalette.ColorRole.Highlight)
        soft.setAlpha(45)  # fondo suave sacado del color de resaltado del sistema
        palette.setColor(QPalette.ColorRole.Window, soft)
        self.sentence_label.setPalette(palette)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%v / %m")

        self.controls = PlaybackControls()
        self.voice = VoicePanel()

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(self.notice_label)
        layout.addLayout(input_row)
        layout.addWidget(self.preview)
        layout.addWidget(self.choice_panel)
        layout.addWidget(self.doc_title_label)
        layout.addWidget(self.chapter_label)
        layout.addWidget(self.sentence_label)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.controls)
        layout.addWidget(self.voice)
        layout.addStretch(1)
        self.setCentralWidget(central)

        self.engine_label = QLabel(f"Motor activo: {EDGE_LABEL}")
        self.edge_label = QLabel(f"Edge: {EDGE_TEXT[EngineState.EDGE_ACTIVE]}")
        self.piper_label = QLabel()
        self.piper_label.setTextFormat(Qt.TextFormat.RichText)
        self.waiting_label = QLabel(WAITING_TEXT)
        self.waiting_label.hide()
        self._set_piper_label(None)
        bar = self.statusBar()
        for label in (self.waiting_label, self.engine_label, self.edge_label, self.piper_label):
            bar.addPermanentWidget(label)

        menu = self.menuBar().addMenu("Lector")
        self.history_action = QAction("Historial…", self)
        self.settings_action = QAction("Configuración…", self)
        menu.addAction(self.history_action)
        menu.addAction(self.settings_action)

    def _connect(self, bridge: QtEventBridge) -> None:
        bridge.session_state_sig.connect(self._on_session_state)
        bridge.engine_state_sig.connect(self._on_engine_state)
        bridge.document_sig.connect(self._on_document)
        bridge.sentence_sig.connect(self._on_sentence)
        bridge.notice_sig.connect(self._on_notice)
        bridge.needs_choice_sig.connect(self._on_needs_choice)

        self.read_button.clicked.connect(self._on_read_clicked)
        self.url_edit.returnPressed.connect(self._on_read_clicked)
        self.open_file_button.clicked.connect(self._on_open_file_clicked)
        self.history_action.triggered.connect(self._show_history)
        self.settings_action.triggered.connect(self.settings_requested.emit)
        self.piper_label.linkActivated.connect(lambda _link: self.settings_requested.emit())
        self.preview.prefix_edited.connect(self._save_prefix)
        self.choice_panel.chosen.connect(lambda ref: self._run(self._ctl.choose_next(ref)))

        c, ctl = self.controls, self._ctl
        c.play_pause_button.clicked.connect(self._on_play_pause)
        c.stop_button.clicked.connect(lambda: self._run(ctl.stop()))
        c.prev_chapter_button.clicked.connect(lambda: self._run(ctl.prev_chapter()))
        c.prev_sentence_button.clicked.connect(lambda: self._run(ctl.prev_sentence()))
        c.skip_back_button.clicked.connect(lambda: self._run(ctl.skip_seconds(-c.skip_seconds)))
        c.skip_forward_button.clicked.connect(lambda: self._run(ctl.skip_seconds(c.skip_seconds)))
        c.next_sentence_button.clicked.connect(lambda: self._run(ctl.next_sentence()))
        c.next_chapter_button.clicked.connect(lambda: self._run(ctl.next_chapter()))
        c.restart_button.clicked.connect(lambda: self._run(ctl.restart_chapter()))
        c.continuous_check.clicked.connect(lambda on: self._run(ctl.set_continuous(on)))

        v = self.voice
        v.preferred_changed.connect(self._on_preferred_changed)
        v.edge_voice_changed.connect(
            lambda voice: self._run(ctl.set_voice(voice, None, v.apply_now()))
        )
        v.piper_voice_changed.connect(self._on_piper_voice_changed)
        v.speed_changed.connect(lambda speed: self._run(ctl.set_speed(speed, v.apply_now())))

    # --- configuración (leída en el hilo de trabajo) -------------------------------------------

    def reload_settings(self) -> None:
        """Vuelve a leer la configuración, las voces de Edge y los modelos de Piper (p. ej. tras T18)."""
        self.settings_loaded = False
        job = asyncio.to_thread(_read_state, self._settings, self._voices, self._models)
        self._run(job, self._apply_loaded, on_err=self._on_load_failed)

    def _apply_loaded(self, loaded: _Loaded) -> None:
        values = loaded.values
        self._preferred = values["preferred_engine"] or "edge"
        self.voice.set_preferred(self._preferred)
        if not self._engine_seen:
            self.engine_label.setText(
                f"Motor activo: {PIPER_LABEL if self._preferred == 'piper' else EDGE_LABEL}"
            )
        edge_voice = values["edge_voice"]
        self.voice.set_edge_voices(_edge_items(loaded.edge_voices, values["language"], edge_voice),
                                   edge_voice)
        piper = _piper_items(loaded.piper)
        self._piper_short = {data: short for _, data, short in piper}
        speaker = values["piper_speaker"]
        current = f"{values['piper_voice']}#{speaker}" if speaker else values["piper_voice"]
        self.voice.set_piper_voices([(text, data) for text, data, _ in piper], current)
        self._set_piper_label(current)
        self.voice.set_speed(float(values["speed"]))
        self.controls.set_continuous(bool(values["continuous"]))
        self.controls.set_skip_seconds(int(values["skip_seconds"]))
        highlight = values["highlight_sentence"]
        self._highlight = True if highlight is None else bool(highlight)
        self.sentence_label.setAutoFillBackground(self._highlight)
        self.settings_loaded = True

    def _on_load_failed(self, exc: BaseException) -> None:
        self.settings_loaded = True  # la ventana sigue usable con los valores por defecto
        self._on_error(exc)

    def _set_piper_label(self, voice: str | None) -> None:
        short = self._piper_short.get(voice) if voice else None
        if short:
            self.piper_label.setText(f"Piper: Listo ({short})")
        else:
            self.piper_label.setText('Piper: <a href="#configuracion">Sin voz instalada</a>')

    # --- llamadas al trabajador -------------------------------------------------------------

    def _run(
        self,
        coro: Coroutine[Any, Any, Any],
        on_ok: Callable[[Any], None] | None = None,
        on_err: Callable[[BaseException], None] | None = None,
    ) -> None:
        try:
            future = self._worker.submit(coro)
        except RuntimeError:
            log.warning("El trabajador ya no está en marcha; se ignora la orden")
            return
        self._call(future, on_ok, on_err)

    def _call(self, future, on_ok=None, on_err=None) -> None:
        call_ui(future, on_ok or (lambda _result: None), on_err or self._on_error)

    def _on_error(self, exc: BaseException) -> None:
        if isinstance(exc, asyncio.CancelledError):
            log.debug("Orden cancelada (cierre del trabajador)")
            return
        if isinstance(exc, LectorError):
            self.show_notice(exc.user_message, "error")
            return
        log.error("Error inesperado en una orden de la interfaz",
                  exc_info=(type(exc), exc, exc.__traceback__))
        self.show_notice(GENERIC_ERROR, "error")

    def show_notice(self, message: str, level: str = "info") -> None:
        """Aviso no modal dentro de la ventana (nunca bloquea, aunque esté minimizada)."""
        font = self.notice_label.font()
        font.setBold(level in ("warn", "error"))
        self.notice_label.setFont(font)
        self.notice_label.setText(message)
        self.notice_label.show()

    def _clear_notice(self) -> None:
        self.notice_label.clear()
        self.notice_label.hide()

    # --- abrir y leer ------------------------------------------------------------------------

    def _on_read_clicked(self) -> None:
        locator = self.url_edit.text().strip()
        if not locator:
            self.show_notice(NOTHING_TO_READ)
            return
        if self._opening:
            return
        if self._preview is not None and self._preview[0] == locator:
            self._play(self._preview[1].doc_id, None)
            return
        self._open(locator, autoplay=True)

    def _on_open_file_clicked(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Abrir PDF o EPUB", "", "Libros PDF o EPUB (*.pdf *.epub)"
        )
        if path:
            self.url_edit.setText(path)
            self._open(path, autoplay=False)

    def _open(self, locator: str, autoplay: bool) -> None:
        self._clear_notice()
        self._set_opening(True)

        def opened(result) -> None:
            self._set_opening(False)
            doc, chapter, progress = result
            self._preview = (locator, doc, chapter)
            self._doc_id = doc.doc_id
            self.preview.show_preview(doc, chapter)
            self.preview.show()
            if progress is not None:
                self.offer_resume(doc, progress)
            elif autoplay:
                self._play(doc.doc_id, None)

        def failed(exc: BaseException) -> None:
            self._set_opening(False)
            self._on_error(exc)

        self._run(self._ctl.open(locator), opened, failed)

    def _set_opening(self, busy: bool) -> None:
        self._opening = busy
        self.read_button.setEnabled(not busy)
        self.open_file_button.setEnabled(not busy)
        if busy:
            self.statusBar().showMessage("Abriendo…")
        else:
            self.statusBar().clearMessage()

    def _play(self, doc_id: str, start: Position | None) -> None:
        self._doc_id = doc_id
        self._run(self._ctl.play(doc_id, start))

    def _on_play_pause(self) -> None:
        if self._state in _PLAYING_STATES:
            self._run(self._ctl.pause())
        elif self._state is SessionState.PAUSED:
            self._run(self._ctl.resume())
        elif self._doc_id is not None:
            self._play(self._doc_id, None)
        else:
            self._on_read_clicked()

    # --- reanudación ----------------------------------------------------------------------------

    def offer_resume(self, doc: Document, progress: ProgressRecord) -> None:
        """Ofrece continuar, empezar o elegir. Con la ventana oculta o minimizada, continúa."""
        if not self.isVisible() or self.isMinimized():
            self._play(doc.doc_id, None)
            self.show_notice(CONTINUING)
            return
        if self.resume_dialog is not None:
            self.resume_dialog.close()
            self.resume_dialog.deleteLater()
        dialog = ResumeDialog(doc, progress, self)
        dialog.chosen.connect(lambda choice: self._apply_resume(doc, progress, choice))
        self.resume_dialog = dialog
        dialog.show()

    def _apply_resume(self, doc: Document, progress: ProgressRecord, choice: ResumeChoice) -> None:
        if choice.kind == "continue":
            self._play(doc.doc_id, None)
        elif choice.kind == "start":
            self._play(doc.doc_id, self._start_of(doc, progress))
        elif choice.kind == "chapter" and choice.chapter is not None:
            index = doc.chapters.index(choice.chapter) if choice.chapter in doc.chapters else 0
            self._play(doc.doc_id, _chapter_start(index, choice.chapter))
        elif choice.kind == "page" and choice.page is not None:
            self._doc_id = doc.doc_id
            self._run(self._play_page(doc, choice.page))

    def _start_of(self, doc: Document, progress: ProgressRecord) -> Position:
        if doc.chapters:
            return _chapter_start(0, doc.chapters[0])
        # Web sin índice: el principio es el del capítulo abierto (o el guardado).
        if self._preview is not None and self._preview[1].doc_id == doc.doc_id:
            ref = self._preview[2].ref
            return _chapter_start(0, ref)
        pos = progress.position
        return Position(pos.chapter_index, pos.chapter_locator, 0, 0, 0, None)

    async def _play_page(self, doc: Document, page: int) -> None:
        """En el trabajador: busca la primera frase de la página y lee desde ahí."""
        position = await self._ctl.locate_page(doc.doc_id, page)
        if position is None:
            found = chapter_with_page(doc, page)
            position = _chapter_start(*found, page=page) if found else None
        await self._ctl.play(doc.doc_id, position)

    # --- historial y prefijo ------------------------------------------------------------------

    def _show_history(self) -> None:
        if self.history_dialog is not None and self.history_dialog.isVisible():
            self.history_dialog.raise_()
            return
        if self.history_dialog is not None:
            self.history_dialog.deleteLater()
        dialog = HistoryDialog(self._worker, self._documents, self, on_error=self._on_error)
        dialog.document_chosen.connect(self._open_from_history)
        self.history_dialog = dialog
        dialog.show()

    def _open_from_history(self, doc: Document, progress: ProgressRecord | None) -> None:
        self._clear_notice()
        self.url_edit.setText(doc.origin)
        self.preview.hide()
        self._preview = None
        self._doc_id = doc.doc_id
        if progress is not None:
            self.offer_resume(doc, progress)
        else:
            self._play(doc.doc_id, None)

    def _save_prefix(self, text: str) -> None:
        if self._preview is None:
            return
        host = urlsplit(self._preview[1].origin).netloc
        path = urlsplit(text).path if "://" in text else text
        if path and not path.startswith("/"):
            path = "/" + path
        if path and not path.endswith("/"):
            path += "/"
        settings = self._settings

        def save() -> None:
            overrides = dict(settings.get("work_prefix_overrides") or {})
            if path:
                overrides[host] = path
            else:
                overrides.pop(host, None)
            settings.set("work_prefix_overrides", overrides)

        self._run(asyncio.to_thread(save), lambda _r: self.show_notice(PREFIX_SAVED))

    def _on_preferred_changed(self, name: str) -> None:
        self._preferred = name
        self._run(self._ctl.set_preferred_engine(name))

    def _on_piper_voice_changed(self, voice: str) -> None:
        self._set_piper_label(voice)
        self._run(self._ctl.set_voice(None, voice, self.voice.apply_now()))

    # --- avisos del controlador ---------------------------------------------------------------

    @Slot(str)
    def _on_session_state(self, value: str) -> None:
        try:
            state = SessionState(value.lower())
        except ValueError:
            log.warning("Estado de sesión desconocido: %r", value)
            return
        self._state = state
        self.controls.set_playing(state in _PLAYING_STATES)
        self.waiting_label.setVisible(state is SessionState.WAITING_NETWORK)

    @Slot(str, str)
    def _on_engine_state(self, value: str, active: str) -> None:
        self._engine_seen = True
        self.engine_label.setText(f"Motor activo: {active}")
        try:
            state = EngineState(value.lower())
        except ValueError:
            log.warning("Estado de motor desconocido: %r", value)
            return
        if state is EngineState.PIPER_ACTIVE and self._preferred == "piper":
            return  # Piper por elección: no dice nada de la conexión con Edge
        self.edge_label.setText(f"Edge: {EDGE_TEXT[state]}")

    @Slot(object, object)
    def _on_document(self, doc, chapter) -> None:
        self._doc_id = doc.doc_id
        self.doc_title_label.setText(doc.title)
        self.chapter_label.setText(chapter.title)
        self.preview.hide()

    @Slot(str, str, int, int)
    def _on_sentence(self, chapter_title: str, text: str, index: int, total: int) -> None:
        if chapter_title:
            self.chapter_label.setText(chapter_title)
        self.sentence_label.setText(text)
        self.sentence_label.setAutoFillBackground(self._highlight)
        self.progress_bar.setRange(0, max(1, total))
        self.progress_bar.setValue(min(index + 1, max(1, total)))

    @Slot(str, str)
    def _on_notice(self, level: str, message: str) -> None:
        self.show_notice(message, level)

    @Slot(object)
    def _on_needs_choice(self, candidates) -> None:
        refs = list(candidates)
        if self.isVisible():
            self._show_choice(refs)
        else:
            self.pending_choice = refs  # se muestra al abrir la ventana

    def _show_choice(self, refs: list[ChapterRef]) -> None:
        self.pending_choice = None
        self.choice_panel.set_candidates(refs)
        self.choice_panel.show()

    # --- ventana ------------------------------------------------------------------------------

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if self.pending_choice:
            self._show_choice(self.pending_choice)

    def allow_close(self) -> None:
        """Al salir de verdad (menú «Salir» de la bandeja, T19): cerrar ya no oculta."""
        self._quitting = True

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._quitting:
            event.accept()
            return
        # Cerrar no sale de la app: se oculta en la bandeja y la lectura sigue (T17/T19).
        event.ignore()
        self.hide()
        self.close_to_tray_requested.emit()
