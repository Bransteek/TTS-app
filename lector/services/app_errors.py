"""Errores no capturados: se registran, se guarda el progreso y se avisa sin cerrar la app.

`ErrorGuard` atiende tres ganchos:
- `sys.excepthook` (hilo de la interfaz; PySide también lo llama con los errores de los slots);
- el manejador de excepciones del bucle del trabajador (`loop_hook`);
- `threading.excepthook` (cualquier otro hilo; `SystemExit` se ignora).

En los tres casos registra la traza completa, pide al trabajador
`controller.shutdown(save_only=True)` sin esperar (nunca bloquea la interfaz) y emite un
aviso por el puente: la ventana lo muestra y, sin el usuario delante, la bandeja. Si el
propio manejador falla o vuelve a entrar, solo registra: nunca hay recursión.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import sys
import threading
from typing import Any

log = logging.getLogger(__name__)

UNEXPECTED_ERROR = (
    "Ocurrió un problema inesperado. Se está guardando tu progreso; la app sigue abierta."
)


class ErrorGuard:
    def __init__(self) -> None:
        self.worker: Any = None  # AsyncWorker, cuando ya está en marcha
        self.controller: Any = None  # ReadingController, cuando ya está montado
        self.bridge: Any = None  # QtEventBridge
        self._local = threading.local()
        self._save_lock = threading.Lock()
        self._pending_save: concurrent.futures.Future | None = None
        self._previous_hooks: tuple[Any, Any] | None = None

    def install(self) -> None:
        self._previous_hooks = (sys.excepthook, threading.excepthook)
        sys.excepthook = self.ui_hook
        threading.excepthook = self.thread_hook

    def uninstall(self) -> None:
        if self._previous_hooks is None:
            return
        previous_sys, previous_thread = self._previous_hooks
        self._previous_hooks = None
        if sys.excepthook == self.ui_hook:
            sys.excepthook = previous_sys
        if threading.excepthook == self.thread_hook:
            threading.excepthook = previous_thread

    def ui_hook(self, exc_type, exc, tb) -> None:
        self._handle("en la interfaz", (exc_type, exc, tb))

    def loop_hook(self, loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exc = context.get("exception")
        exc_info = (type(exc), exc, exc.__traceback__) if exc is not None else None
        self._handle(f"en el trabajador: {context.get('message')}", exc_info)

    def thread_hook(self, args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread is not None else "?"
        self._handle(f"en el hilo {name}", (args.exc_type, args.exc_value, args.exc_traceback))

    def _handle(self, where: str, exc_info) -> None:
        if getattr(self._local, "busy", False):
            try:
                log.error("Error mientras se atendía otro error (%s)", where, exc_info=exc_info)
            except Exception:
                pass
            return
        self._local.busy = True
        try:
            try:
                log.error("Error no controlado %s", where, exc_info=exc_info)
            except Exception:
                pass
            for step in (self._save, self._notify):
                try:
                    step()
                except Exception:
                    try:
                        log.exception("Falló el manejador de errores no capturados")
                    except Exception:
                        pass
        finally:
            self._local.busy = False

    def _save(self) -> None:
        """Pide al trabajador que guarde el progreso confirmado; no espera."""
        worker, controller = self.worker, self.controller
        if worker is None or controller is None:
            return
        with self._save_lock:
            pending = self._pending_save
            if pending is not None and not pending.done():
                return  # ya hay un guardado en camino
            try:
                future = worker.submit(controller.shutdown(save_only=True))
            except RuntimeError:
                log.warning("El trabajador ya no está en marcha; no se pudo guardar el progreso")
                return
            self._pending_save = future
        future.add_done_callback(_log_save_failure)

    def _notify(self) -> None:
        bridge = self.bridge
        if bridge is not None:
            bridge.on_notice("error", UNEXPECTED_ERROR)  # ventana y, sin el usuario, bandeja


def _log_save_failure(future: concurrent.futures.Future) -> None:
    if future.cancelled():
        return
    exc = future.exception()
    if exc is not None:
        log.error("No se pudo guardar el progreso tras un error",
                  exc_info=(type(exc), exc, exc.__traceback__))
