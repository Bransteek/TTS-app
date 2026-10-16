"""Puente entre el hilo asyncio del lector y el hilo de la interfaz Qt."""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from collections.abc import Callable, Coroutine
from typing import Any

from PySide6.QtCore import QCoreApplication, QObject, Qt, Signal, Slot

from lector.services.reading_controller import SessionState
from lector.tts.engine_manager import EngineState

log = logging.getLogger(__name__)


class AsyncWorker:
    """Hilo daemon con su propio bucle asyncio; aquí vive el controlador."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._stopped = False

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            raise RuntimeError("El trabajador no está iniciado.")
        return self._loop

    def start(self) -> None:
        with self._lock:
            if self._stopped:
                raise RuntimeError("El trabajador ya se detuvo.")
            if self._thread is not None:
                return
            self._loop = asyncio.new_event_loop()
            self._loop.set_exception_handler(self._on_loop_exception)
            self._thread = threading.Thread(target=self._run, name="lector-worker", daemon=True)
            self._thread.start()
        self._ready.wait()

    def _run(self) -> None:
        loop = self._loop
        assert loop is not None
        asyncio.set_event_loop(loop)
        loop.call_soon(self._ready.set)
        try:
            loop.run_forever()
        finally:
            loop.close()

    @staticmethod
    def _on_loop_exception(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        log.error(
            "Error no controlado en el bucle del trabajador: %s",
            context.get("message"),
            exc_info=context.get("exception"),
        )

    def submit(self, coro: Coroutine[Any, Any, Any]) -> concurrent.futures.Future:
        with self._lock:
            if self._stopped or self._loop is None:
                coro.close()
                raise RuntimeError("El trabajador no está en marcha.")
            return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def stop(self, timeout: float = 5) -> None:
        """Cancela lo pendiente, detiene el bucle y espera al hilo. Es idempotente."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            loop, thread = self._loop, self._thread
        if loop is None or thread is None:
            return
        if thread.is_alive() and threading.current_thread() is not thread:
            async def _cancel_all() -> None:
                current = asyncio.current_task()
                pending = [t for t in asyncio.all_tasks() if t is not current]
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

            try:
                asyncio.run_coroutine_threadsafe(_cancel_all(), loop).result(timeout)
            except Exception:
                log.warning("No se pudieron cancelar todas las tareas a tiempo.", exc_info=True)
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout)
            if thread.is_alive():
                log.warning("El hilo del trabajador no terminó en %s s.", timeout)


class QtEventBridge(QObject):
    """Recibe los avisos del controlador (hilo del bucle) y los emite como señales Qt.

    Cumple `ControllerEvents` de forma estructural. Las señales se conectan a
    slots de objetos de la interfaz, así que Qt las entrega por cola en su hilo.
    """

    session_state_sig = Signal(str)
    engine_state_sig = Signal(str, str)
    document_sig = Signal(object, object)
    sentence_sig = Signal(str, str, int, int)
    notice_sig = Signal(str, str)
    needs_choice_sig = Signal(object)

    def on_session_state(self, state: SessionState) -> None:
        self.session_state_sig.emit(str(state))

    def on_engine_state(self, state: EngineState, active: str) -> None:
        self.engine_state_sig.emit(str(state), active)

    def on_document(self, doc: Any, chapter: Any) -> None:
        self.document_sig.emit(doc, chapter)

    def on_sentence(
        self, chapter_title: str, sentence_text: str, sentence_index: int, total: int
    ) -> None:
        self.sentence_sig.emit(chapter_title, sentence_text, sentence_index, total)

    def on_notice(self, level: str, message: str) -> None:
        self.notice_sig.emit(level, message)

    def on_needs_choice(self, candidates: Any) -> None:
        self.needs_choice_sig.emit(candidates)


class _UiDispatcher(QObject):
    """Vive en el hilo de la interfaz y ejecuta ahí las funciones que le llegan."""

    _call = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._call.connect(self._run, Qt.ConnectionType.QueuedConnection)

    def post(self, fn: Callable[[], None]) -> None:
        self._call.emit(fn)

    @Slot(object)
    def _run(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:
            log.exception("Error en un callback de la interfaz")


_dispatcher: _UiDispatcher | None = None


def _get_dispatcher() -> _UiDispatcher:
    global _dispatcher
    app = QCoreApplication.instance()
    if app is None:
        raise RuntimeError("call_ui necesita una aplicación Qt en marcha.")
    if _dispatcher is None or _dispatcher.thread() is not app.thread():
        _dispatcher = _UiDispatcher()
        _dispatcher.moveToThread(app.thread())
    return _dispatcher


def call_ui(
    future: concurrent.futures.Future,
    on_ok: Callable[[Any], None],
    on_err: Callable[[BaseException], None],
) -> None:
    """Llama a `on_ok(resultado)` o a `on_err(excepción)` (una sola) en el hilo de la interfaz.

    Una tarea cancelada cuenta como error. Se llama desde el hilo de la interfaz.
    """
    dispatcher = _get_dispatcher()

    def done(fut: concurrent.futures.Future) -> None:
        if fut.cancelled():
            exc: BaseException | None = asyncio.CancelledError()
        else:
            exc = fut.exception()
        if exc is None:
            result = fut.result()
            dispatcher.post(lambda: on_ok(result))
        else:
            dispatcher.post(lambda: on_err(exc))

    future.add_done_callback(done)
