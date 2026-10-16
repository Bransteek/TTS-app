"""Puente asyncio <-> Qt: hilo de trabajo, señales en cola y callbacks en la interfaz."""
from __future__ import annotations

import asyncio
import logging
import threading

import pytest
from PySide6.QtCore import QObject, QThread, QTimer, Slot

from lector.services.reading_controller import ControllerEvents, SessionState
from lector.services.worker import AsyncWorker, QtEventBridge, call_ui
from lector.tts.engine_manager import EngineState
from tests.fakes import FakeEdge


@pytest.fixture
def worker():
    w = AsyncWorker()
    w.start()
    yield w
    w.stop()


class Receiver(QObject):
    def __init__(self):
        super().__init__()
        self.got: list[tuple] = []
        self.threads: list[QThread] = []

    @Slot(str)
    def on_state(self, s):
        self.got.append((s,))
        self.threads.append(QThread.currentThread())


def test_submit_runs_on_worker_thread(worker):
    async def ident():
        return threading.get_ident()

    assert worker.submit(ident()).result(timeout=2) != threading.get_ident()


def test_signals_delivered_on_ui_thread(qtbot, worker):
    bridge = QtEventBridge()
    rx = Receiver()
    bridge.session_state_sig.connect(rx.on_state)

    async def emit():
        bridge.on_session_state(SessionState.READING)

    worker.submit(emit()).result(timeout=2)
    qtbot.waitUntil(lambda: bool(rx.got), timeout=2000)
    assert rx.got == [("reading",)]
    assert rx.threads[0] is QThread.currentThread()


def test_all_signals_carry_plain_values(qtbot):
    bridge = QtEventBridge()
    seen = {}
    bridge.engine_state_sig.connect(lambda a, b: seen.update(engine=(a, b)))
    bridge.document_sig.connect(lambda d, c: seen.update(doc=(d, c)))
    bridge.sentence_sig.connect(lambda *a: seen.update(sentence=a))
    bridge.notice_sig.connect(lambda *a: seen.update(notice=a))
    bridge.needs_choice_sig.connect(lambda c: seen.update(choice=c))
    bridge.on_engine_state(EngineState.PIPER_ACTIVE, "Piper local")
    bridge.on_document("D", "C")
    bridge.on_sentence("T", "hola", 2, 9)
    bridge.on_notice("warn", "ojo")
    bridge.on_needs_choice(["a", "b"])
    assert seen == {
        "engine": ("piper_active", "Piper local"),
        "doc": ("D", "C"),
        "sentence": ("T", "hola", 2, 9),
        "notice": ("warn", "ojo"),
        "choice": ["a", "b"],
    }


def test_bridge_is_accepted_as_controller_events():
    def takes(events: ControllerEvents) -> ControllerEvents:
        return events

    bridge = QtEventBridge()
    assert takes(bridge) is bridge
    for name in (
        "on_session_state", "on_engine_state", "on_document",
        "on_sentence", "on_notice", "on_needs_choice",
    ):
        assert callable(getattr(bridge, name))


def test_ui_stays_responsive_during_slow_synthesis(qtbot, worker):
    ticks = []
    timer = QTimer()
    timer.setInterval(50)
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start()

    async def slow():
        edge = FakeEdge()
        await asyncio.sleep(2)  # síntesis lenta simulada en el hilo de trabajo
        return edge.name

    fut = worker.submit(slow())
    qtbot.waitUntil(lambda: len(ticks) >= 20, timeout=3000)
    assert not fut.done()
    fut.cancel()
    timer.stop()


def test_call_ui_ok_on_ui_thread(qtbot, worker):
    out = []

    async def val():
        return 42

    call_ui(worker.submit(val()), lambda r: out.append((r, QThread.currentThread())),
            lambda e: out.append(("err", e)))
    qtbot.waitUntil(lambda: bool(out), timeout=2000)
    assert out == [(42, QThread.currentThread())]


def test_call_ui_error_receives_exception(qtbot, worker):
    out = []

    async def boom():
        raise ValueError("mal")

    call_ui(worker.submit(boom()), lambda r: out.append("ok"),
            lambda e: out.append((e, QThread.currentThread())))
    qtbot.waitUntil(lambda: bool(out), timeout=2000)
    assert len(out) == 1 and isinstance(out[0][0], ValueError)
    assert out[0][1] is QThread.currentThread()


def test_call_ui_cancelled_counts_as_error(qtbot, worker):
    out = []

    async def forever():
        await asyncio.sleep(60)

    fut = worker.submit(forever())
    call_ui(fut, lambda r: out.append("ok"), lambda e: out.append(e))
    fut.cancel()
    qtbot.waitUntil(lambda: bool(out), timeout=2000)
    assert len(out) == 1 and isinstance(out[0], BaseException)


def test_call_ui_callback_that_raises_is_logged(qtbot, worker, caplog):
    out = []

    async def val():
        return 1

    def bad(_):
        raise RuntimeError("callback roto")

    with caplog.at_level(logging.ERROR, logger="lector.services.worker"):
        call_ui(worker.submit(val()), bad, lambda e: None)
        qtbot.waitUntil(lambda: any("callback roto" in (r.exc_text or "") or r.exc_info
                                    for r in caplog.records), timeout=2000)
    # la aplicación sigue viva y atiende otro callback
    call_ui(worker.submit(val()), out.append, lambda e: None)
    qtbot.waitUntil(lambda: bool(out), timeout=2000)


def test_stop_cancels_pending_and_is_idempotent():
    w = AsyncWorker()
    w.start()
    started = threading.Event()

    async def forever():
        started.set()
        await asyncio.sleep(60)

    fut = w.submit(forever())
    assert started.wait(2)
    w.stop()
    assert fut.cancelled()
    assert not w._thread.is_alive()
    w.stop()  # segunda llamada sin efecto


def test_submit_after_stop_raises():
    w = AsyncWorker()
    w.start()
    w.stop()

    async def noop():
        pass

    with pytest.raises(RuntimeError):
        w.submit(noop())


def test_loop_exceptions_are_logged(worker, caplog):
    async def leak():
        asyncio.get_running_loop().call_soon(lambda: 1 / 0)
        await asyncio.sleep(0.05)

    with caplog.at_level(logging.ERROR, logger="lector.services.worker"):
        worker.submit(leak()).result(timeout=2)
    assert any("ZeroDivisionError" in r.getMessage() or r.exc_info for r in caplog.records)
