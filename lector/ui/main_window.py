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
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from PySide6.QtCore import QEvent, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QCloseEvent, QHideEvent, QPalette, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
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

from lector.content.generic_web import set_prefix_override
from lector.errors import LectorError
from lector.models import Chapter, ChapterRef, Document, Position
from lector.persistence.repositories import (
    ChapterRepo,
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
from lector.ui.playback_controls import (
    PlaybackControls,
    VoicePanel,
    edge_voice_items,
    piper_voice_items,
)
from lector.ui.preview_panel import ChoicePanel, PreviewPanel, validate_prefix
from lector.ui.resume_dialog import ResumeChoice, ResumeDialog, ResumeOffer, chapter_with_page

log = logging.getLogger(__name__)

GENERIC_ERROR = (
    "Ocurrió un problema inesperado. La lectura se ha pausado y tu progreso está guardado."
)
SETTINGS_LOAD_FAILED = "No se pudo cargar la configuración. Se usan los valores por defecto."
NOTHING_TO_READ = "Pega la dirección de un capítulo o abre un archivo PDF o EPUB."
CONTINUING = "Se sigue leyendo donde lo dejaste."
STARTED_TEXT = "Se empezó a leer con el prefijo y el capítulo detectados."
LEAVE_CHECK_MS = 300  # la app sin foco tanto tiempo: el usuario se fue (no es un parpadeo)
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
_SETTING_KEYS = ("edge_voice", "language", "piper_voice", "piper_speaker", "speed",
                 "continuous", "skip_seconds", "preferred_engine", "highlight_sentence")


@dataclass
class _Loaded:
    values: dict[str, Any]
    edge_voices: list[dict]
    piper: list[tuple[str, list[str]]]  # (modelo instalado, hablantes)


@dataclass
class _Preview:
    locator: str
    doc: Document
    chapter: Chapter


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


def _store_override(settings: SettingsRepo, chapter_url: str, path: str | None) -> None:
    """Recuerda el prefijo corregido de la obra de `chapter_url` (None: lo olvida).

    En el hilo de trabajo.
    """
    overrides = set_prefix_override(settings.get("work_prefix_overrides"), chapter_url, path)
    settings.set("work_prefix_overrides", overrides)


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
        self._chapters = ChapterRepo(settings.connection)
        self._voices = voices or VoicesCacheRepo(settings.connection)

        self._state = SessionState.IDLE
        self._session_doc: str | None = None  # documento de la sesión (play o document_sig)
        self._session_chapter: str | None = None  # capítulo que suena (document_sig)
        self._preview: _Preview | None = None
        self._opening = False
        self._open_gen = 0  # cada apertura nueva deja caducas las respuestas de las anteriores
        self._play_pending = False  # un play() en vuelo: no se manda otro
        self._confirm_after_leer = False  # «Leer» pidió leer y la vista previa espera confirmar
        # Posición elegida (capítulo, «empezar», …) que aún no cargó: un reintento vuelve a ella.
        self._pending_start: tuple[str, Position] | None = None
        self._own_dialogs = 0  # diálogos nativos propios abiertos (no cuentan como irse)
        self._preferred = "edge"
        self._engine_seen = False
        self._highlight = True
        self._piper_short: dict[str, str] = {}
        self._notice_level: str | None = None
        self.settings_loaded = False
        self.history_dialog: HistoryDialog | None = None
        self.pending_choice: list[ChapterRef] | None = None
        self._choice_tag: tuple[str | None, str | None] | None = None  # (doc, capítulo)
        self._quitting = False
        self._resume = ResumeOffer(self, self._apply_resume, lambda: self.show_notice(CONTINUING))
        self._leave_timer = QTimer(self)
        self._leave_timer.setSingleShot(True)
        self._leave_timer.setInterval(LEAVE_CHECK_MS)
        self._leave_timer.timeout.connect(self._check_user_left)

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
        self.voice.set_edge_voices(
            edge_voice_items(loaded.edge_voices, values["language"], edge_voice), edge_voice
        )
        piper = piper_voice_items(loaded.piper)
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
        if isinstance(exc, asyncio.CancelledError):
            return
        log.error("No se pudo cargar la configuración",
                  exc_info=(type(exc), exc, exc.__traceback__))
        self.show_notice(SETTINGS_LOAD_FAILED, "warn")

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

    # --- abrir y leer ------------------------------------------------------------------------

    def _on_read_clicked(self) -> None:
        locator = self.url_edit.text().strip()
        if not locator:
            self.show_notice(NOTHING_TO_READ)
            return
        if self._opening or self._play_pending:
            return  # doble clic: ya hay una orden en vuelo
        preview = self._preview
        if preview is None or preview.locator != locator:
            self._open(locator, autoplay=True)
            return
        doc = preview.doc
        if self._session_doc == doc.doc_id:
            # Ya se empezó a leer este documento: «Leer» no lo reinicia ni vuelve al
            # capítulo elegido al principio; sigue donde iba.
            if self._state in _PLAYING_STATES:
                return
            if self._state is SessionState.PAUSED:
                self._run(self._ctl.resume())
                return
            if not self.preview.needs_confirmation():
                self._play(doc.doc_id, None)
                return
            # Se volvió a abrir y la vista previa espera confirmar: vale lo que se elija ahora.
        edited = self.preview.edited_prefix()
        if edited is not None:
            self._reopen_with_prefix(preview, edited)
            return
        picked = self.preview.selected_chapter()
        self._play(doc.doc_id, _chapter_start(*picked) if picked else None)

    def _on_open_file_clicked(self) -> None:
        with self.own_native_dialog():
            path, _ = QFileDialog.getOpenFileName(
                self, "Abrir PDF o EPUB", "", "Libros PDF o EPUB (*.pdf *.epub)"
            )
        if path:
            self.url_edit.setText(path)
            self._open(path, autoplay=False)

    def _open(self, locator: str, autoplay: bool) -> None:
        self._start_open(locator, self._ctl.open(locator), autoplay, confirmed=False)

    def _reopen_with_prefix(self, preview: _Preview, typed: str) -> None:
        """Guarda el prefijo corregido de la obra y vuelve a abrir con él antes de leer.

        Un campo vacío olvida la corrección guardada (vuelve a la detección). El mismo
        prefijo (con o sin «/» final) no cambia nada: se lee lo ya abierto.
        """
        doc, chapter_url = preview.doc, preview.chapter.ref.locator
        path: str | None = None
        if typed:
            path, error = validate_prefix(typed, doc.origin, chapter_url)
            if error is not None:
                self.preview.show_prefix_error(error)
                return
            if path == urlsplit(doc.work_prefix or "").path.rstrip("/") + "/":
                self._play(doc.doc_id, None)
                return
        settings, documents, ctl, locator = self._settings, self._documents, self._ctl, preview.locator

        async def save_and_open():
            await asyncio.to_thread(_store_override, settings, chapter_url, path)
            result = await ctl.open(locator)
            if result[0].doc_id != doc.doc_id:
                # El documento del prefijo equivocado no queda en el historial si no se leyó.
                # Es limpieza: si falla, se avisa en el registro y se lee igual.
                try:
                    await asyncio.to_thread(documents.delete_if_unread, doc.doc_id)
                except Exception:
                    log.warning("No se pudo borrar el documento sin leer %s", doc.doc_id,
                                exc_info=True)
            return result

        self._start_open(locator, save_and_open(), autoplay=True, confirmed=True)

    def _start_open(self, locator: str, job: Coroutine, autoplay: bool, confirmed: bool) -> None:
        self._clear_notice()
        self._clear_choice()
        self._resume.cancel()
        self._confirm_after_leer = False
        self._pending_start = None
        self._open_gen += 1
        gen = self._open_gen
        self._set_opening(True)

        def opened(result) -> None:
            if gen != self._open_gen:
                return  # caducada: después se abrió otra cosa (p. ej. desde el historial)
            self._set_opening(False)
            doc, chapter, progress = result
            self._preview = _Preview(locator, doc, chapter)
            fresh = progress is None
            self.preview.show_preview(
                doc, chapter,
                editable_prefix=fresh and not confirmed and doc.adapter == "generic",
                choose_chapter=fresh and doc.index_page,
            )
            self.preview.show()
            if progress is not None:
                known = chapter.title if chapter.ref.locator == progress.position.chapter_locator else None
                self.offer_resume(doc, progress, chapter_title=known)
            elif autoplay and not self.preview.needs_confirmation():
                self._play(doc.doc_id, None)
            elif autoplay:
                self._confirm_after_leer = True
                if not self.user_present():
                    self._auto_confirm()  # se fue mientras se abría: no espera a nadie

        def failed(exc: BaseException) -> None:
            if gen != self._open_gen:
                return
            self._set_opening(False)
            self._on_error(exc)

        if not self._run(job, opened, failed):
            self._set_opening(False)

    def _set_opening(self, busy: bool) -> None:
        self._opening = busy
        self.read_button.setEnabled(not busy)
        self.open_file_button.setEnabled(not busy)
        if busy:
            self.statusBar().showMessage("Abriendo…")
        else:
            self.statusBar().clearMessage()

    def _play(self, doc_id: str, start: Position | None) -> None:
        """Lee `doc_id` desde `start`; sin `start`, desde la posición elegida que no llegó a cargar."""
        if start is not None:
            self._pending_start = (doc_id, start)
        elif self._pending_start is not None and self._pending_start[0] == doc_id:
            start = self._pending_start[1]
        self._play_job(doc_id, self._ctl.play(doc_id, start))

    def _play_job(self, doc_id: str, job: Coroutine) -> None:
        """Manda una orden que empieza a leer `doc_id`.

        Mientras está en vuelo, «Leer» y Leer/Pausar no mandan otra (doble clic); una
        elección explícita (reanudar, historial) sí se manda.
        """
        self._session_doc = doc_id
        self._clear_choice()  # una elección pendiente era de la lectura anterior
        self._confirm_after_leer = False
        self.preview.settle()  # ya no se espera confirmar el prefijo ni el capítulo

        def done(_result=None) -> None:
            self._play_pending = False

        def failed(exc: BaseException) -> None:
            done()
            self._on_error(exc)

        self._play_pending = self._run(job, done, failed)

    def _on_play_pause(self) -> None:
        # Pausar y seguir valen aunque haya un play() en vuelo; solo no se manda otro play.
        if self._state in _PLAYING_STATES:
            self._run(self._ctl.pause())
        elif self._state is SessionState.PAUSED:
            self._run(self._ctl.resume())
        elif self._play_pending or self._opening:
            return
        elif self._session_doc is not None:
            self._play(self._session_doc, None)
        else:
            self._on_read_clicked()

    # --- reanudación ----------------------------------------------------------------------------

    def user_present(self) -> bool:
        """¿El usuario mira la app? Visible, sin minimizar y con el foco (aquí o en un diálogo)."""
        if not self.isVisible() or self.isMinimized():
            return False
        return self.isActiveWindow() or QApplication.activeWindow() is not None

    def _auto_confirm(self) -> None:
        """Sin el usuario delante tras «Leer»: empieza con lo detectado, sin esperar.

        Prefijo detectado en una web genérica; capítulo 1 al abrir desde el índice. Lo que
        el usuario hubiera escrito o elegido sin confirmar no cuenta.
        """
        preview = self._preview
        if not self._confirm_after_leer or preview is None:
            return
        self._confirm_after_leer = False
        if not self.preview.needs_confirmation():
            return
        doc = preview.doc
        start = None
        if not self.preview.chapter_list.isHidden() and doc.chapters:
            start = _chapter_start(0, doc.chapters[0])
        self._play(doc.doc_id, start)
        self.show_notice(STARTED_TEXT)

    def _user_left(self, hidden: bool) -> None:
        """El usuario se fue al juego: oculta o minimizada (`hidden`) o la app sin foco."""
        if self._quitting or self._own_dialogs:
            return
        self._auto_confirm()
        if hidden:
            self._resume.window_hidden()
        else:
            self._resume.window_deactivated()

    @Slot(Qt.ApplicationState)
    def _on_app_state(self, state: Qt.ApplicationState) -> None:
        # Un diálogo de la propia app con el foco no deja la app inactiva. Se espera un poco
        # antes de decidir: un parpadeo del foco no cuenta como irse.
        if state == Qt.ApplicationState.ApplicationActive:
            self._leave_timer.stop()
        elif self.isVisible():  # oculta: ya se trató al ocultarla
            self._leave_timer.start()

    def _check_user_left(self) -> None:
        if self._own_dialogs:
            return
        if self.isVisible() and not self.user_present():
            self._user_left(hidden=self.isMinimized())

    @contextmanager
    def own_native_dialog(self) -> Iterator[None]:
        """Envuelve un diálogo nativo de la app (p. ej. `QFileDialog.getOpenFileName`).

        Mientras está abierto, Qt no ve ninguna ventana activa y la app queda inactiva:
        eso no es irse al juego. Al cerrarse se mira solo el estado real (si el usuario
        sigue fuera, la comprobación diferida lo detecta). Puede anidarse; T17 y T18
        deben usarlo con sus propios diálogos nativos.
        """
        self._own_dialogs += 1
        self._leave_timer.stop()
        try:
            yield
        finally:
            self._own_dialogs -= 1
            if not self._own_dialogs:
                self._leave_timer.stop()
                if self.isVisible() and not self.user_present():
                    self._leave_timer.start()

    def offer_resume(self, doc: Document, progress: ProgressRecord, *,
                     chapter_title: str | None = None,
                     auto_continue_when_hidden: bool = True) -> None:
        """Ofrece continuar, empezar o elegir capítulo o página.

        Sin el usuario delante (oculta, minimizada o sin foco) nunca abre un diálogo:
        con `auto_continue_when_hidden` continúa; sin él, la oferta espera a que se
        muestre la ventana (la recuperación al arrancar no lee sin que se lo pidan).
        """
        self._resume.offer(doc, progress, present=self.user_present(),
                           chapter_title=chapter_title,
                           auto_continue_when_hidden=auto_continue_when_hidden)

    def _apply_resume(self, doc: Document, progress: ProgressRecord, choice: ResumeChoice) -> None:
        if choice.kind == "continue":
            self._play(doc.doc_id, None)
        elif choice.kind == "start":
            self._play(doc.doc_id, self._start_of(doc, progress))
        elif choice.kind == "chapter" and choice.chapter is not None:
            index = doc.chapters.index(choice.chapter) if choice.chapter in doc.chapters else 0
            self._play(doc.doc_id, _chapter_start(index, choice.chapter))
        elif choice.kind == "page" and choice.page is not None:
            self._play_job(doc.doc_id, self._play_page(doc, choice.page))

    def _start_of(self, doc: Document, progress: ProgressRecord) -> Position:
        if doc.chapters:
            return _chapter_start(0, doc.chapters[0])
        # Web sin índice: el principio es el del capítulo abierto (o el guardado).
        if self._preview is not None and self._preview.doc.doc_id == doc.doc_id:
            return _chapter_start(0, self._preview.chapter.ref)
        pos = progress.position
        return Position(pos.chapter_index, pos.chapter_locator, 0, 0, 0, None)

    async def _play_page(self, doc: Document, page: int) -> None:
        """En el trabajador: busca la primera frase de la página y lee desde ahí."""
        position = await self._ctl.locate_page(doc.doc_id, page)
        if position is None:
            found = chapter_with_page(doc, page)
            position = _chapter_start(*found, page=page) if found else None
        await self._ctl.play(doc.doc_id, position)

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

    def _open_from_history(self, doc: Document, progress: ProgressRecord | None,
                           chapter_title: str | None = None) -> None:
        self._clear_notice()
        self._clear_choice()
        self._resume.cancel()
        self._confirm_after_leer = False
        self._pending_start = None
        self._open_gen += 1  # lo que se estuviera abriendo ya no vale: manda el historial
        self._set_opening(False)
        self.url_edit.setText(doc.origin)
        self.preview.hide()
        self._preview = None
        if progress is not None:
            self.offer_resume(doc, progress, chapter_title=chapter_title)
        else:
            self._play(doc.doc_id, None)

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
        previous, self._state = self._state, state
        self.controls.set_playing(state in _PLAYING_STATES)
        self.waiting_label.setVisible(state is SessionState.WAITING_NETWORK)
        back_to_reading = state is SessionState.READING and previous is not SessionState.READING
        if back_to_reading and self._notice_level in ("warn", "error"):
            self._clear_notice()  # el problema avisado ya pasó

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
        # Cerrar no sale de la app: se oculta en la bandeja y la lectura sigue (T17/T19).
        event.ignore()
        self.hide()
        self.close_to_tray_requested.emit()
