"""Órdenes de la interfaz al hilo de trabajo y texto de sus errores para el usuario."""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from lector.errors import LectorError
from lector.services.worker import AsyncWorker, call_ui

log = logging.getLogger(__name__)

UNEXPECTED = "Ocurrió un problema inesperado. Inténtalo de nuevo."


def run_job(
    worker: AsyncWorker,
    coro: Coroutine[Any, Any, Any],
    on_ok: Callable[[Any], None],
    on_err: Callable[[BaseException], None],
) -> concurrent.futures.Future | None:
    """Manda `coro` al trabajador y entrega su resultado en el hilo de Qt (None si ya paró)."""
    try:
        future = worker.submit(coro)
    except RuntimeError:
        log.warning("El trabajador ya no está en marcha; se ignora la orden")
        return None
    call_ui(future, on_ok, on_err)
    return future


def error_text(exc: BaseException) -> str | None:
    """Texto para el usuario (None si fue una cancelación, que no se avisa)."""
    if isinstance(exc, asyncio.CancelledError):
        return None
    if isinstance(exc, LectorError):
        return exc.user_message
    log.error("Error inesperado en la configuración", exc_info=(type(exc), exc, exc.__traceback__))
    return UNEXPECTED
