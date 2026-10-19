"""Bandeja del sistema: el único control de la app mientras el usuario juega.

Reglas: la ventana oculta o sin foco nunca se activa por un aviso; los avisos y las
decisiones pendientes salen como globo de la bandeja (único lugar que llama a
`showMessage`) y quedan en `pending_attention` hasta que el usuario abra la ventana.
«Mostrar ventana» y el clic en el icono sí la traen al frente: lo pidió el usuario.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, QTimer, Slot
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap, QPolygonF
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from lector.errors import LectorError
from lector.services.reading_controller import SessionState
from lector.services.worker import AsyncWorker, QtEventBridge, call_ui
from lector.ui.main_window import MainWindow
from lector.tts.engine_manager import EDGE_LABEL, PIPER_LABEL
from lector.ui.open_flow import PLAYING_STATES
from lector.ui.quit_flow import SHUTDOWN_TIMEOUT_S, WORKER_STOP_TIMEOUT_S, shutdown_and_stop

log = logging.getLogger(__name__)

STILL_READING = "Sigo leyendo en la bandeja del sistema"
CHOICE_PENDING = "Hay que elegir el capítulo siguiente. Abre la ventana para decidir."
APP_NAME = "Lector"
MAX_PENDING = 20
BALLOON_REPEAT_S = 30.0

_ENGINE_NAMES = {"edge": EDGE_LABEL, "piper": PIPER_LABEL}

_LEVEL_ICONS = {
    "info": QSystemTrayIcon.MessageIcon.Information,
    "warn": QSystemTrayIcon.MessageIcon.Warning,
    "error": QSystemTrayIcon.MessageIcon.Critical,
}


def draw_icon(paused: bool = False, size: int = 64) -> QIcon:
    """Círculo con un triángulo (leyendo) o con dos barras (en pausa), sin archivos."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#d98324") if paused else QColor("#2f7de1"))
        margin = size * 0.04
        painter.drawEllipse(QRectF(margin, margin, size - 2 * margin, size - 2 * margin))
        painter.setBrush(QColor("white"))
        if paused:
            bar_w, bar_h = size * 0.13, size * 0.42
            top = (size - bar_h) / 2
            painter.drawRect(QRectF(size * 0.34, top, bar_w, bar_h))
            painter.drawRect(QRectF(size * 0.53, top, bar_w, bar_h))
        else:
            painter.drawPolygon(QPolygonF([
                QPointF(size * 0.38, size * 0.28),
                QPointF(size * 0.38, size * 0.72),
                QPointF(size * 0.74, size * 0.50),
            ]))
    finally:
        painter.end()
    return QIcon(pixmap)


class TrayController(QObject):
    def __init__(
        self,
        window: MainWindow,
        bridge: QtEventBridge,
        worker: AsyncWorker,
        controller,
        icon: QIcon,
        *,
        tray: Any = None,
        quit_app: Callable[[], None] | None = None,
        shutdown_timeout_s: float = SHUTDOWN_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
    ):
        super().__init__(window)
        self._window = window
        self._worker = worker
        self._ctl = controller
        self._quit_app = quit_app or QApplication.quit
        self._shutdown_timeout_s = shutdown_timeout_s
        self._icon = icon
        self._paused_icon = draw_icon(paused=True)
        self._state = SessionState.IDLE
        self._title = ""
        self._chapter = ""
        self._engine = ""
        self._told_still_reading = False
        self._quitting = False
        self._clock = clock
        self._last_balloon: dict[str, float] = {}
        self.pending_attention: list[tuple[str, object]] = []

        self.tray = tray if tray is not None else QSystemTrayIcon(self)
        self._build_menu()
        self._update_texts()

        bridge.session_state_sig.connect(self._on_session_state)
        bridge.engine_state_sig.connect(self._on_engine_state)
        bridge.document_sig.connect(self._on_document)
        bridge.sentence_sig.connect(self._on_sentence)
        bridge.notice_sig.connect(self._on_notice)
        bridge.needs_choice_sig.connect(self._on_needs_choice)
        window.close_to_tray_requested.connect(self._on_close_to_tray)
        self.tray.activated.connect(self._on_activated)
        window.installEventFilter(self)
        self.tray.show()

    # --- menú -----------------------------------------------------------------------------

    def _build_menu(self) -> None:
        self.menu = QMenu()  # sin padre: lo posee el icono de la bandeja, no la ventana
        self.toggle_action = QAction("Pausar", self.menu)
        self.next_sentence_action = QAction("Frase siguiente", self.menu)
        self.next_chapter_action = QAction("Capítulo siguiente", self.menu)
        self.stop_action = QAction("Detener", self.menu)
        self.show_action = QAction("Mostrar ventana", self.menu)
        self.quit_action = QAction("Salir", self.menu)
        for action in (self.toggle_action, self.next_sentence_action, self.next_chapter_action,
                       self.stop_action):
            self.menu.addAction(action)
        self.menu.addSeparator()
        self.menu.addAction(self.show_action)
        self.menu.addAction(self.quit_action)
        self.toggle_action.triggered.connect(self._on_toggle)
        self.next_sentence_action.triggered.connect(lambda: self._run(self._ctl.next_sentence()))
        self.next_chapter_action.triggered.connect(lambda: self._run(self._ctl.next_chapter()))
        self.stop_action.triggered.connect(lambda: self._run(self._ctl.stop()))
        self.show_action.triggered.connect(self.show_window)
        self.quit_action.triggered.connect(self.quit)
        self.tray.setContextMenu(self.menu)

    def _run(self, coro) -> None:
        if self._quitting:
            coro.close()
            return
        try:
            future = self._worker.submit(coro)
        except RuntimeError:
            coro.close()
            log.warning("El trabajador ya no está en marcha; se ignora la orden de la bandeja")
            return
        call_ui(future, lambda _result: None, self._on_command_error)

    def _on_command_error(self, exc: BaseException) -> None:
        if isinstance(exc, LectorError):
            self.notify("error", exc.user_message)
        else:
            log.error("Error en una orden de la bandeja",
                      exc_info=(type(exc), exc, exc.__traceback__))

    def _on_toggle(self) -> None:
        if self._state in PLAYING_STATES:
            self._run(self._ctl.pause())
        elif self._state is SessionState.PAUSED:
            self._run(self._ctl.resume())

    # --- estado, texto del menú y tooltip -------------------------------------------------------

    def _update_texts(self) -> None:
        paused = self._state is SessionState.PAUSED
        self.toggle_action.setText("Reanudar" if paused else "Pausar")
        self.tray.setIcon(self._paused_icon if paused else self._icon)
        where = " — ".join(part for part in (self._title, self._chapter) if part)
        engine = _ENGINE_NAMES.get(self._engine, self._engine)
        tip = " · ".join(part for part in (where, engine) if part) or APP_NAME
        self.tray.setToolTip(tip)

    @Slot(str)
    def _on_session_state(self, value: str) -> None:
        try:
            self._state = SessionState(value)
        except ValueError:
            log.warning("Estado de sesión desconocido: %s", value)
            return
        self._update_texts()

    @Slot(str, str)
    def _on_engine_state(self, _state: str, active: str) -> None:
        self._engine = active
        self._update_texts()

    @Slot(object, object)
    def _on_document(self, doc, chapter) -> None:
        self._title = doc.title
        self._chapter = chapter.title
        self._update_texts()

    @Slot(str, str, int, int)
    def _on_sentence(self, chapter_title: str, _text: str, _index: int, _total: int) -> None:
        if chapter_title and chapter_title != self._chapter:
            self._chapter = chapter_title
            self._update_texts()

    # --- avisos ----------------------------------------------------------------------------------

    def notify(self, level: str, message: str) -> None:
        """Globo de la bandeja. No toca la ventana ni abre diálogos."""
        now = self._clock()
        last = self._last_balloon.get(message)
        if last is not None and now - last < BALLOON_REPEAT_S:
            return  # el mismo globo salió hace poco: no se repite
        self._last_balloon[message] = now
        if len(self._last_balloon) > 4 * MAX_PENDING:
            self._last_balloon = {m: t for m, t in self._last_balloon.items()
                                  if now - t < BALLOON_REPEAT_S}
        icon = _LEVEL_ICONS.get(level, QSystemTrayIcon.MessageIcon.Information)
        self.tray.showMessage(APP_NAME, message, icon)

    @Slot(str, str)
    def _on_notice(self, level: str, message: str) -> None:
        if self._window.user_present():
            return  # ya lo muestra la ventana
        self._add_pending("notice", (level, message))
        self.notify(level, message)

    def _add_pending(self, kind: str, payload: object) -> None:
        entry = (kind, payload)
        if self.pending_attention and self.pending_attention[-1] == entry:
            return
        self.pending_attention.append(entry)
        del self.pending_attention[:-MAX_PENDING]

    @Slot(object)
    def _on_needs_choice(self, candidates) -> None:
        if self._window.user_present():
            return
        self._add_pending("choice", list(candidates))
        self.notify("info", CHOICE_PENDING)

    @Slot()
    def _on_close_to_tray(self) -> None:
        if not self._told_still_reading:
            self._told_still_reading = True
            self.notify("info", STILL_READING)

    # --- ventana ----------------------------------------------------------------------------------

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._window:
            kind = event.type()
            if kind == QEvent.Type.Show:
                self.pending_attention.clear()  # la ventana presenta lo pendiente en su showEvent
            elif kind == QEvent.Type.ActivationChange and self._window.isActiveWindow():
                self.pending_attention.clear()
            elif kind == QEvent.Type.WindowStateChange and self._window.isMinimized():
                QTimer.singleShot(0, self._hide_if_minimized)
        return super().eventFilter(watched, event)

    def _hide_if_minimized(self) -> None:
        # Sin emitir close_to_tray_requested (no es un cierre) y nunca durante una salida real.
        if self._window.isMinimized() and not self._quitting:
            self._window.hide()

    @Slot()
    def show_window(self) -> None:
        """Lo pidió el usuario (menú o clic): se restaura aunque estuviera minimizada."""
        self._window.showNormal()
        self._window.raise_()
        self._window.activateWindow()

    @Slot(object)
    def _on_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_window()

    # --- salir ---------------------------------------------------------------------------------------

    @Slot()
    def quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        shutdown_and_stop(self._window, self._worker, self._ctl,
                          shutdown_timeout_s=self._shutdown_timeout_s,
                          worker_timeout_s=WORKER_STOP_TIMEOUT_S)
        self.tray.hide()
        self._quit_app()
