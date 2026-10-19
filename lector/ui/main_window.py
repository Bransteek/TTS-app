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

from PySide6.QtCore import QEvent, Qt, Signal, Slot
from PySide6.QtGui import QAction, QCloseEvent, QHideEvent, QPalette, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
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
from lector.models import ChapterRef
from lector.persistence.repositories import (
    ChapterRepo,
    DocumentRepo,
    SettingsRepo,
    VoicesCacheRepo,
)
from lector.services.reading_controller import SessionState
from lector.services.sample_player import SamplePlayer
from lector.services.worker import AsyncWorker, QtEventBridge, call_ui
from lector.tts.piper_engine import PiperModelManager
from lector.ui.history_dialog import HistoryDialog
from lector.ui.open_flow import PLAYING_STATES, OpenFlowMixin
from lector.ui.piper_setup import FirstRunBanner, PiperDownloads
from lector.ui.playback_controls import PlaybackControls, VoicePanel
from lector.ui.preview_panel import ChoicePanel, PreviewPanel
from lector.ui.resume_dialog import ResumeDialog, ResumeOffer
from lector.ui.settings_dialog import SettingsDialog, SettingsServices
from lector.ui.status_panels import VoiceStatusMixin

log = logging.getLogger(__name__)

GENERIC_ERROR = (
    "Ocurrió un problema inesperado. La lectura se ha pausado y tu progreso está guardado."
)
MODEL_INSTALLED = "Voz sin conexión instalada: si se corta internet, la lectura sigue con ella."
SETTINGS_LOAD_FAILED = "No se pudo cargar la configuración. Se usan los valores por defecto."
CONTINUING = "Se sigue leyendo donde lo dejaste."
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


class MainWindow(OpenFlowMixin, VoiceStatusMixin, QMainWindow):
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
        self._chapters = ChapterRepo(settings.connection)
        self._voices = voices or VoicesCacheRepo(settings.connection)

        self._state = SessionState.IDLE
        self._session_doc: str | None = None  # documento de la sesión (play o document_sig)
        self._session_chapter: str | None = None  # capítulo que suena (document_sig)
        self._init_open_flow()
        self._init_voice_status()
        self._highlight = True
        self._notice_level: str | None = None
        self.settings_loaded = False
        self.history_dialog: HistoryDialog | None = None
        self.pending_choice: list[ChapterRef] | None = None
        self._choice_tag: tuple[str | None, str | None] | None = None  # (doc, capítulo)
        self._quitting = False
        self._resume = ResumeOffer(self, self._apply_resume, lambda: self.show_notice(CONTINUING))

        self.setWindowTitle("Lector")
        self._build()
        self._connect(bridge)
        self.reload_settings()

    @property
    def resume_dialog(self) -> ResumeDialog | None:
        return self._resume.dialog

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
        # Hueco para avisos propios de la app (p. ej. el de primer arranque de T18): `add_top_banner`.
        self._banner_layout = QVBoxLayout()
        self._banner_layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self._banner_layout)
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

        self._build_status_bar()

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
        self.choice_panel.chosen.connect(self._on_choice_chosen)
        app = QApplication.instance()
        if app is not None:
            app.applicationStateChanged.connect(self._on_app_state)

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

        self._connect_voice()

    def add_top_banner(self, widget: QWidget) -> None:
        """Pone `widget` arriba del todo de la ventana, sobre el aviso y la entrada (p. ej. el banner de T18)."""
        self._banner_layout.addWidget(widget)

    # --- configuración y voz local (T18) ----------------------------------------------------

    def enable_settings(self, edge=None, sample: SamplePlayer | None = None) -> None:
        """Conecta «Configuración…» y el enlace de Piper con el diálogo y pone el aviso de primer arranque.

        `edge` es el EdgeEngine de la app (para «Actualizar lista»); `sample`, el reproductor de
        muestras. Se llama una vez, al montar la app (T19).
        """
        self._edge, self._sample = edge, sample
        self.settings_dialog: SettingsDialog | None = None
        self.downloads = PiperDownloads(self._worker, self._models, self)
        self.downloads.finished.connect(self._on_model_downloaded)
        self.settings_requested.connect(self.show_settings)
        self.first_run_banner = FirstRunBanner(self._worker, self._settings, self._models,
                                               self.downloads)
        self.add_top_banner(self.first_run_banner)
        self.first_run_banner.check()

    @Slot()
    def show_settings(self) -> None:
        """Abre la configuración sin modalidad (si ya está abierta, la trae al frente)."""
        dialog = self.settings_dialog
        if dialog is not None and dialog.isVisible():
            dialog.raise_()
            return
        if dialog is not None:
            dialog.deleteLater()
        services = SettingsServices(
            self._worker, self._ctl, self._settings, self._models, self._voices,
            self._chapters, self.downloads, edge=self._edge, sample=self._sample,
        )
        dialog = SettingsDialog(services, self)
        dialog.saved.connect(self.reload_settings)
        self.settings_dialog = dialog
        dialog.show()

    @Slot(str, bool, str)
    def _on_model_downloaded(self, _name: str, ok: bool, _message: str) -> None:
        if ok:
            self.reload_settings()  # el indicador de Piper pasa a «Listo»
            if self._notice_level not in ("warn", "error"):  # no tapa un aviso importante
                self.show_notice(MODEL_INSTALLED)

    # --- configuración (leída en el hilo de trabajo) -------------------------------------------

    def reload_settings(self) -> None:
        """Vuelve a leer la configuración, las voces de Edge y los modelos de Piper (p. ej. tras T18)."""
        self.settings_loaded = False
        job = asyncio.to_thread(_read_state, self._settings, self._voices, self._models)
        self._run(job, self._apply_loaded, on_err=self._on_load_failed)

    def _apply_loaded(self, loaded: _Loaded) -> None:
        values = loaded.values
        self._apply_voice_settings(loaded)
        self.controls.set_continuous(bool(values["continuous"]))
        self.controls.set_skip_seconds(int(values["skip_seconds"]))
        highlight = values["highlight_sentence"]
        self._highlight = True if highlight is None else bool(highlight)
        self.sentence_label.setAutoFillBackground(self._highlight)
        self.settings_loaded = True

    def _on_load_failed(self, exc: BaseException) -> None:
        self.settings_loaded = True  # la ventana sigue usable con los valores por defecto
        if isinstance(exc, asyncio.CancelledError):
            return
        log.error("No se pudo cargar la configuración",
                  exc_info=(type(exc), exc, exc.__traceback__))
        self.show_notice(SETTINGS_LOAD_FAILED, "warn")

    # --- llamadas al trabajador -------------------------------------------------------------

    def _run(
        self,
        coro: Coroutine[Any, Any, Any],
        on_ok: Callable[[Any], None] | None = None,
        on_err: Callable[[BaseException], None] | None = None,
    ) -> bool:
        try:
            future = self._worker.submit(coro)
        except RuntimeError:
            log.warning("El trabajador ya no está en marcha; se ignora la orden")
            return False
        self._call(future, on_ok, on_err)
        return True

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
        self._notice_level = level

    def _clear_notice(self) -> None:
        self.notice_label.clear()
        self.notice_label.hide()
        self._notice_level = None

    # --- historial --------------------------------------------------------------------------------

    def _show_history(self) -> None:
        if self.history_dialog is not None and self.history_dialog.isVisible():
            self.history_dialog.raise_()
            return
        if self.history_dialog is not None:
            self.history_dialog.deleteLater()
        dialog = HistoryDialog(self._worker, self._documents, self, on_error=self._on_error,
                               chapters=self._chapters)
        dialog.document_chosen.connect(self._open_from_history)
        self.history_dialog = dialog
        dialog.show()

    # --- avisos del controlador ---------------------------------------------------------------

    @Slot(str)
    def _on_session_state(self, value: str) -> None:
        try:
            state = SessionState(value.lower())
        except ValueError:
            log.warning("Estado de sesión desconocido: %r", value)
            return
        previous, self._state = self._state, state
        self.controls.set_playing(state in PLAYING_STATES)
        self.waiting_label.setVisible(state is SessionState.WAITING_NETWORK)
        back_to_reading = state is SessionState.READING and previous is not SessionState.READING
        if back_to_reading and self._notice_level in ("warn", "error"):
            self._clear_notice()  # el problema avisado ya pasó

    # `@Slot` solo funciona en una clase de Qt: los de los mixins se declaran aquí y delegan.
    @Slot(Qt.ApplicationState)
    def _on_app_state(self, state: Qt.ApplicationState) -> None:
        self._handle_app_state(state)

    @Slot(str, str)
    def _on_engine_state(self, value: str, active: str) -> None:
        self._handle_engine_state(value, active)

    @Slot(object, object)
    def _on_document(self, doc, chapter) -> None:
        self._session_doc = doc.doc_id
        self._session_chapter = chapter.ref.locator
        self._pending_start = None  # la lectura ya tiene posición: la guarda el controlador
        if self._choice_tag is not None and self._choice_tag != (doc.doc_id, chapter.ref.locator):
            self._clear_choice()  # los candidatos eran de otro documento o capítulo
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

    # --- capítulo siguiente ambiguo -------------------------------------------------------------

    @Slot(object)
    def _on_needs_choice(self, candidates) -> None:
        refs = list(candidates)
        self._choice_tag = (self._session_doc, self._session_chapter)
        if self.isVisible():
            self._show_choice(refs)
        else:
            self.pending_choice = refs  # se muestra al abrir la ventana

    def _show_choice(self, refs: list[ChapterRef]) -> None:
        self.pending_choice = None
        self.choice_panel.set_candidates(refs)
        self.choice_panel.show()

    def _clear_choice(self) -> None:
        self.pending_choice = None
        self._choice_tag = None
        self.choice_panel.hide()

    def _on_choice_chosen(self, ref: ChapterRef) -> None:
        tag = self._choice_tag
        self._clear_choice()
        if tag is None or tag[0] != self._session_doc:
            log.info("Se ignora una elección de capítulo de otra lectura")
            return
        self._run(self._ctl.choose_next(ref))

    # --- ventana ------------------------------------------------------------------------------

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if self.pending_choice:
            self._show_choice(self.pending_choice)
        self._resume.window_shown()

    def hideEvent(self, event: QHideEvent) -> None:
        super().hideEvent(event)
        self._user_left(hidden=True)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            if self.isMinimized():
                self._user_left(hidden=True)
            elif self.isVisible():
                self._resume.window_shown()
        elif event.type() == QEvent.Type.ActivationChange:
            # Igual que la app sin foco: un diálogo propio con el foco no cuenta (user_present).
            if self.isActiveWindow():
                self._leave_timer.stop()
            elif self.isVisible():
                self._leave_timer.start()

    def allow_close(self) -> None:
        """Al salir de verdad (menú «Salir» de la bandeja, T19): cerrar ya no oculta."""
        self._quitting = True

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._quitting:
            event.accept()
            return
        event.ignore()
        self._hide_to_tray()

    def _hide_to_tray(self) -> None:
        """Cerrar no sale de la app: se oculta en la bandeja y la lectura sigue (punto de T17/T19).

        Aquí (cierre), en `hideEvent` y en `changeEvent` (minimizar) se decide qué pasa al
        irse el usuario; los avisos de la app se muestran con `show_notice`.
        """
        self.hide()
        self.close_to_tray_requested.emit()
