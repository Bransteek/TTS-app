"""Bandeja del sistema: nunca roba el foco y es el único control con la ventana oculta."""
from __future__ import annotations

import asyncio
import logging

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from lector.models import ChapterRef
from lector.services.worker import AsyncWorker, QtEventBridge
from lector.ui.main_window import MainWindow
from lector.ui.tray import CHOICE_PENDING, STILL_READING, TrayController, draw_icon
from tests.test_ui import (
    WAIT_MS,
    FakeController,
    SpyModels,
    SpySettings,
    activate,
    make_chapter,
    make_doc,
    wait_call,
)
from lector.persistence.database import open_database


class FakeTray(QObject):
    activated = Signal(object)

    def __init__(self):
        super().__init__()
        self.messages: list[tuple[str, str]] = []
        self.tooltip = ""
        self.icon = None
        self.menu = None
        self.visible = False

    def setIcon(self, icon):
        self.icon = icon

    def setToolTip(self, text):
        self.tooltip = text

    def setContextMenu(self, menu):
        self.menu = menu

    def showMessage(self, title, message, icon=None, ms=10000):
        self.messages.append((title, message))

    def show(self):
        self.visible = True

    def hide(self):
        self.visible = False


class TrayController_(FakeController):
    def __init__(self):
        super().__init__()
        self.shutdown_error: BaseException | None = None
        self.shutdown_hang = False

    async def shutdown(self, save_only=False):
        self._record("shutdown")
        if self.shutdown_hang:
            await asyncio.sleep(30)
        if self.shutdown_error is not None:
            raise self.shutdown_error


@pytest.fixture
def rig(qtbot, tmp_path):
    worker = AsyncWorker()
    worker.start()
    conn = open_database(tmp_path / "lector.db")
    ctl = TrayController_()
    bridge = QtEventBridge()
    window = MainWindow(worker, bridge, ctl, SpySettings(conn), SpyModels(tmp_path / "voces"))
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: window.settings_loaded, timeout=WAIT_MS)
    quits: list[bool] = []
    fake = FakeTray()
    tray = TrayController(window, bridge, worker, ctl, QIcon(), tray=fake,
                          quit_app=lambda: quits.append(True), shutdown_timeout_s=0.5)

    class Rig:
        pass

    r = Rig()
    r.worker, r.ctl, r.bridge, r.window, r.tray, r.fake, r.quits = (
        worker, ctl, bridge, window, tray, fake, quits)
    yield r
    worker.stop(timeout=2)
    conn.close()


def show(qtbot, window):
    with qtbot.waitExposed(window):
        window.show()
    activate(qtbot, window)


def test_close_hides_and_keeps_reading(qtbot, rig):
    show(qtbot, rig.window)
    rig.window.close()
    assert not rig.window.isVisible()
    assert not any(c[0] == "stop" for c in rig.ctl.calls)
    assert QApplication.instance() is not None
    assert rig.fake.messages == [("Lector", STILL_READING)]
    show(qtbot, rig.window)
    rig.window.close()
    assert len(rig.fake.messages) == 1  # solo la primera vez


def test_minimize_hides_to_tray_and_show_restores(qtbot, rig):
    show(qtbot, rig.window)
    rig.window.showMinimized()
    qtbot.waitUntil(lambda: not rig.window.isVisible(), timeout=WAIT_MS)
    rig.tray.show_window()
    assert rig.window.isVisible()
    assert not rig.window.isMinimized()


def test_notice_while_hidden_never_activates_window(qtbot, rig, monkeypatch):
    called = []
    monkeypatch.setattr(QWidget, "activateWindow", lambda self: called.append("activate"))
    monkeypatch.setattr(QWidget, "raise_", lambda self: called.append("raise"))
    monkeypatch.setattr(QMessageBox, "exec", lambda self: called.append("exec") or 0)
    rig.bridge.notice_sig.emit("warn", "La voz en línea no está disponible por ahora.")
    rig.bridge.needs_choice_sig.emit([ChapterRef(None, "A", "u1"), ChapterRef(None, "B", "u2")])
    qtbot.waitUntil(lambda: len(rig.fake.messages) == 2, timeout=WAIT_MS)
    assert called == []
    assert rig.fake.messages[0][1] == "La voz en línea no está disponible por ahora."
    assert rig.fake.messages[1][1] == CHOICE_PENDING
    assert [kind for kind, _ in rig.tray.pending_attention] == ["notice", "choice"]
    assert not rig.window.isVisible()


def test_pending_choice_shown_on_open(qtbot, rig):
    refs = [ChapterRef(None, "A", "u1"), ChapterRef(None, "B", "u2")]
    rig.bridge.needs_choice_sig.emit(refs)
    qtbot.waitUntil(lambda: bool(rig.tray.pending_attention), timeout=WAIT_MS)
    rig.tray.show_window()
    assert rig.window.choice_panel.isVisible()
    assert rig.tray.pending_attention == []


def test_visible_and_active_window_gets_no_balloon(qtbot, rig):
    show(qtbot, rig.window)
    rig.bridge.notice_sig.emit("info", "Hola")
    qtbot.waitUntil(lambda: rig.window.notice_label.text() == "Hola", timeout=WAIT_MS)
    assert rig.fake.messages == []
    assert rig.tray.pending_attention == []


def test_tray_menu_actions_call_controller(qtbot, rig):
    t, ctl = rig.tray, rig.ctl
    assert [a.text() for a in t.menu.actions() if a.text()] == [
        "Pausar", "Frase siguiente", "Capítulo siguiente", "Detener", "Mostrar ventana", "Salir"]
    rig.bridge.session_state_sig.emit("reading")
    qtbot.waitUntil(lambda: t._state.value == "reading", timeout=WAIT_MS)
    t.toggle_action.trigger()
    wait_call(qtbot, ctl, "pause")
    rig.bridge.session_state_sig.emit("paused")
    qtbot.waitUntil(lambda: t.toggle_action.text() == "Reanudar", timeout=WAIT_MS)
    t.toggle_action.trigger()
    wait_call(qtbot, ctl, "resume")
    t.next_sentence_action.trigger()
    wait_call(qtbot, ctl, "next_sentence")
    t.next_chapter_action.trigger()
    wait_call(qtbot, ctl, "next_chapter")
    t.stop_action.trigger()
    wait_call(qtbot, ctl, "stop")
    t.show_action.trigger()
    assert rig.window.isVisible()


def test_icon_click_shows_window(qtbot, rig):
    from PySide6.QtWidgets import QSystemTrayIcon
    rig.fake.activated.emit(QSystemTrayIcon.ActivationReason.Trigger)
    assert rig.window.isVisible()


def test_tooltip_and_icon_follow_state(qtbot, rig):
    doc = make_doc()
    rig.bridge.document_sig.emit(doc, make_chapter(title="Capítulo 3"))
    rig.bridge.engine_state_sig.emit("edge_active", "edge")
    qtbot.waitUntil(lambda: rig.fake.tooltip == "La novela — Capítulo 3 · edge", timeout=WAIT_MS)
    normal = rig.fake.icon
    rig.bridge.session_state_sig.emit("paused")
    qtbot.waitUntil(lambda: rig.fake.icon is not normal, timeout=WAIT_MS)
    assert not draw_icon().isNull() and not draw_icon(True).isNull()


def test_quit_shuts_down(qtbot, rig):
    rig.tray.quit_action.trigger()
    assert any(c[0] == "shutdown" for c in rig.ctl.calls)
    assert rig.quits == [True]
    assert not rig.fake.visible
    rig.window.close()  # tras allow_close la ventana cierra de verdad
    assert not rig.window.isVisible()


def test_quit_still_quits_when_shutdown_fails(qtbot, rig, caplog):
    rig.ctl.shutdown_error = RuntimeError("boom")
    with caplog.at_level(logging.ERROR):
        rig.tray.quit()
    assert rig.quits == [True]
    assert "falló" in caplog.text


def test_quit_still_quits_when_shutdown_hangs(qtbot, rig, caplog):
    rig.ctl.shutdown_hang = True
    with caplog.at_level(logging.ERROR):
        rig.tray.quit()
    assert rig.quits == [True]
    assert "tardó" in caplog.text
