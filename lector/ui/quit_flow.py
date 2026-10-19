"""Salir de verdad: lo comparten la bandeja («Salir») y la app sin bandeja (cerrar la ventana).

Orden: la ventana ya cierra de verdad (`allow_close`), se cierra el controlador en el
trabajador con tope de tiempo (sin congelar la interfaz: bucle de eventos anidado) y se
detiene el trabajador. Si el cierre falla o tarda, se registra y se sale igualmente.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QEventLoop, QTimer

from lector.services.worker import call_ui

log = logging.getLogger(__name__)

SHUTDOWN_TIMEOUT_S = 5.0
WORKER_STOP_TIMEOUT_S = 2.0  # peor caso al salir: 5 s de cierre + 2 s aquí


def shutdown_controller(worker, controller, timeout_s: float = SHUTDOWN_TIMEOUT_S) -> None:
    """Cierra el controlador con tope de tiempo; si falla o tarda, se sale igual."""
    try:
        future = worker.submit(controller.shutdown())
    except RuntimeError:
        log.warning("El trabajador ya no está en marcha; no hay nada que cerrar")
        return
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    outcome: list[BaseException | None] = []

    def finished(result: BaseException | None) -> None:
        outcome.append(result)
        loop.quit()

    call_ui(future, lambda _r: finished(None), finished)
    timer.start(int(timeout_s * 1000))
    if not outcome:  # call_ui siempre avisa en diferido, pero por si acaso
        loop.exec()
    timer.stop()
    if not outcome:
        log.error("El cierre del controlador tardó más de %s s; se sale igualmente", timeout_s)
    elif outcome[0] is not None:
        exc = outcome[0]
        log.error("El cierre del controlador falló; se sale igualmente",
                  exc_info=(type(exc), exc, exc.__traceback__))


def shutdown_and_stop(window, worker, controller, *,
                      shutdown_timeout_s: float = SHUTDOWN_TIMEOUT_S,
                      worker_timeout_s: float = WORKER_STOP_TIMEOUT_S) -> None:
    """Todo menos `QApplication.quit()` (cada llamador decide cómo salir)."""
    window.allow_close()
    shutdown_controller(worker, controller, shutdown_timeout_s)
    try:
        worker.stop(timeout=worker_timeout_s)
    except Exception:
        log.exception("No se pudo detener el trabajador al salir")
