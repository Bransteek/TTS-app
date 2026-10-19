"""Bandeja del sistema: nunca roba el foco y es el único control con la ventana oculta."""
from __future__ import annotations

import asyncio
import logging
import warnings

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QSystemTrayIcon, QWidget

from lector.models import ChapterRef
from lector.persistence.database import open_database
from lector.services.worker import AsyncWorker, QtEventBridge
from lector.ui.main_window import MainWindow
from lector.ui.tray import (
    CHOICE_PENDING,
    MAX_PENDING,
    RESTART_TEXT,
    STILL_READING,
    WAITING_NOTICE,
    TrayController,
    draw_icon,
)
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


class TrayFakeController(FakeController):
    def __init__(self, events: list | None = None):
        super().__init__()
        self.events = events if events is not None else []
        self.shutdown_error: BaseException | None = None
        self.shutdown_hang = False

    async def shutdown(self, save_only=False):
        self._record("shutdown")
        self.events.append("shutdown")
        if self.shutdown_hang:
            await asyncio.sleep(30)
        if self.shutdown_error is not None:
            raise self.shutdown_error


@pytest.fixture
def rig(qtbot, tmp_path):
    worker = AsyncWorker()
    worker.start()
    conn = open_database(tmp_path / "lector.db")
    events: list = []
    ctl = TrayFakeController(events)
    bridge = QtEventBridge()
    window = MainWindow(worker, bridge, ctl, SpySettings(conn), SpyModels(tmp_path / "voces"))
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: window.settings_loaded, timeout=WAIT_MS)
    quits: list[bool] = []
    fake = FakeTray()
    now = [1000.0]
    stops: list[float] = []
    real_stop = worker.stop

    def recording_stop(timeout: float = 5) -> None:
        events.append("stop")
        stops.append(timeout)
        real_stop(timeout=timeout)

    worker.stop = recording_stop

    def quit_app() -> None:
        events.append("quit")
        quits.append(True)

    tray = TrayController(window, bridge, worker, ctl, QIcon(), tray=fake, quit_app=quit_app,
                          shutdown_timeout_s=0.5, clock=lambda: now[0])

    class Rig:
        pass

    r = Rig()
    r.worker, r.ctl, r.bridge, r.window, r.tray, r.fake, r.quits = (
        worker, ctl, bridge, window, tray, fake, quits)
    r.events, r.now, r.stops = events, now, stops
    yield r
    real_stop(timeout=2)
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


def _forbid_focus_and_modals(monkeypatch, called):
    for cls, names in ((QWidget, ("activateWindow", "raise_", "showNormal", "showMaximized",
                                  "showFullScreen", "setFocus")),
                       (QDialog, ("exec", "open", "show")),
                       (QMessageBox, ("warning", "information", "critical", "question"))):
        for name in names:
            monkeypatch.setattr(cls, name, lambda *a, _n=name, **k: called.append(_n) or 0)


@pytest.mark.parametrize("visible_inactive", [False, True], ids=["hidden", "visible-inactive"])
def test_notice_and_choice_never_steal_focus(qtbot, rig, monkeypatch, visible_inactive):
    window = rig.window
    if visible_inactive:  # el juego está delante: la ventana se ve pero otra tiene el foco
        with qtbot.waitExposed(window):
            window.show()
        with warnings.catch_warnings():  # deprecado, pero es la forma de «el foco está en otro proceso»
            warnings.simplefilter("ignore")
            QApplication.setActiveWindow(None)
        assert window.isVisible() and not window.isActiveWindow()
    called: list[str] = []
    _forbid_focus_and_modals(monkeypatch, called)
    before = {w for w in QApplication.topLevelWidgets() if w.isVisible()}
    rig.bridge.notice_sig.emit("warn", "La voz en línea no está disponible por ahora.")
    rig.bridge.needs_choice_sig.emit([ChapterRef(None, "A", "u1"), ChapterRef(None, "B", "u2")])
    qtbot.waitUntil(lambda: len(rig.fake.messages) == 2, timeout=WAIT_MS)
    qtbot.wait(50)
    assert called == []
    assert {w for w in QApplication.topLevelWidgets() if w.isVisible()} == before
    assert not window.isActiveWindow()
    assert window.isVisible() is visible_inactive
    assert [kind for kind, _ in rig.tray.pending_attention] == ["notice", "choice"]


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
    rig.bridge.engine_state_sig.emit("edge_active", "Edge TTS")  # lo que envía el gestor
    qtbot.waitUntil(lambda: rig.fake.tooltip == "La novela — Capítulo 3 · Edge TTS", timeout=WAIT_MS)
    normal = rig.fake.icon
    rig.bridge.session_state_sig.emit("paused")
    qtbot.waitUntil(lambda: rig.fake.icon is not normal, timeout=WAIT_MS)
    assert not draw_icon().isNull() and not draw_icon(True).isNull()


def test_quit_shuts_down_in_order(qtbot, rig):
    show(qtbot, rig.window)
    closes: list[bool] = []
    rig.window.close_to_tray_requested.connect(lambda: closes.append(True))
    rig.tray.quit_action.trigger()
    assert rig.events == ["shutdown", "stop", "quit"]
    assert rig.quits == [True]
    assert not rig.fake.visible
    rig.window.close()  # tras allow_close la ventana cierra de verdad, sin ir a la bandeja
    assert not rig.window.isVisible()
    assert closes == []
    assert rig.fake.messages == []


def test_quit_uses_short_worker_timeout_and_is_idempotent(qtbot, rig):
    rig.tray.quit()
    rig.tray.quit()
    assert rig.stops == [2.0]
    assert rig.events.count("quit") == 1


def test_commands_ignored_while_quitting(qtbot, rig):
    rig.tray.quit()
    rig.ctl.calls.clear()
    rig.tray.next_sentence_action.trigger()
    rig.tray.stop_action.trigger()
    qtbot.wait(100)
    assert rig.ctl.calls == []


def test_pending_attention_dedupes_and_caps(qtbot, rig):
    for _ in range(3):
        rig.bridge.notice_sig.emit("warn", "igual")
    qtbot.waitUntil(lambda: bool(rig.tray.pending_attention), timeout=WAIT_MS)
    qtbot.wait(50)
    assert len(rig.tray.pending_attention) == 1
    for i in range(MAX_PENDING + 10):
        rig.bridge.notice_sig.emit("info", f"aviso {i}")
    qtbot.waitUntil(lambda: rig.tray.pending_attention[-1][1][1] == f"aviso {MAX_PENDING + 9}",
                    timeout=WAIT_MS)
    assert len(rig.tray.pending_attention) == MAX_PENDING


def test_identical_balloons_are_rate_limited(qtbot, rig):
    rig.tray.notify("warn", "caída")
    rig.tray.notify("warn", "caída")
    assert len(rig.fake.messages) == 1
    rig.tray.notify("warn", "otra")
    assert len(rig.fake.messages) == 2
    rig.now[0] += 31
    rig.tray.notify("warn", "caída")
    assert len(rig.fake.messages) == 3


def test_tooltip_without_chapter_has_no_dangling_dash(qtbot, rig):
    rig.bridge.document_sig.emit(make_doc(), make_chapter(title=""))
    qtbot.waitUntil(lambda: rig.fake.tooltip == "La novela", timeout=WAIT_MS)
    rig.bridge.engine_state_sig.emit("piper_active", "Piper local")
    qtbot.waitUntil(lambda: rig.fake.tooltip == "La novela · Piper local", timeout=WAIT_MS)


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


# --- reanudar desde la bandeja y «Esperando conexión…» ------------------------------------


@pytest.mark.parametrize("state", ["error", "completed", "stopped"])
def test_toggle_restarts_reading_after_the_session_ended(qtbot, rig, state):
    t, ctl = rig.tray, rig.ctl
    rig.bridge.document_sig.emit(make_doc(), make_chapter(title="Capítulo 3"))
    rig.bridge.session_state_sig.emit("reading")
    qtbot.waitUntil(lambda: t.toggle_action.text() == "Pausar", timeout=WAIT_MS)
    rig.bridge.session_state_sig.emit(state)
    qtbot.waitUntil(lambda: t.toggle_action.text() == RESTART_TEXT, timeout=WAIT_MS)
    assert t.toggle_action.isEnabled()

    t.toggle_action.trigger()
    # Desde el progreso confirmado: play() sin posición, como «Leer» en la ventana.
    assert wait_call(qtbot, ctl, "play") == ("play", "doc-1", None)
    assert not any(c[0] in ("pause", "resume") for c in ctl.calls)


def test_toggle_is_not_a_dead_pause_without_a_session(qtbot, rig):
    t = rig.tray
    assert t.toggle_action.text() == "Pausar" and not t.toggle_action.isEnabled()
    rig.bridge.session_state_sig.emit("stopped")  # sin documento: no hay qué reanudar
    qtbot.waitUntil(lambda: t.toggle_action.text() == RESTART_TEXT, timeout=WAIT_MS)
    t.toggle_action.trigger()
    qtbot.wait(100)
    assert not any(c[0] == "play" for c in rig.ctl.calls)


def test_tooltip_shows_waiting_for_connection(qtbot, rig):
    rig.bridge.document_sig.emit(make_doc(), make_chapter(title="Capítulo 3"))
    rig.bridge.engine_state_sig.emit("waiting_network", "Edge TTS")
    rig.bridge.session_state_sig.emit("waiting_network")
    qtbot.waitUntil(lambda: rig.fake.tooltip == "La novela — Capítulo 3 · Esperando conexión…",
                    timeout=WAIT_MS)
    assert rig.tray.toggle_action.text() == "Pausar"  # se puede pausar mientras espera
    rig.bridge.engine_state_sig.emit("edge_active", "Edge TTS")
    rig.bridge.session_state_sig.emit("reading")
    qtbot.waitUntil(lambda: rig.fake.tooltip == "La novela — Capítulo 3 · Edge TTS",
                    timeout=WAIT_MS)


def test_waiting_for_connection_shows_a_balloon_while_away(qtbot, rig):
    rig.bridge.session_state_sig.emit("waiting_network")
    qtbot.waitUntil(lambda: bool(rig.fake.messages), timeout=WAIT_MS)
    assert rig.fake.messages == [("Lector", WAITING_NOTICE)]
    rig.bridge.session_state_sig.emit("reading")
    rig.bridge.session_state_sig.emit("waiting_network")  # parpadeo: no se repite el globo
    qtbot.wait(100)
    assert len(rig.fake.messages) == 1


def test_waiting_for_connection_balloon_not_shown_to_a_present_user(qtbot, rig):
    show(qtbot, rig.window)
    rig.bridge.session_state_sig.emit("waiting_network")
    qtbot.waitUntil(lambda: "Esperando" in rig.fake.tooltip, timeout=WAIT_MS)
    assert rig.fake.messages == []
