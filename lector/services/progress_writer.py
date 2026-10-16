"""Escritura del progreso confirmado en SQLite.

Una sola tarea escritora (`lector:save`): nunca hay dos escrituras a la vez ni
llegan desordenadas. Como mucho escribe una vez cada `interval` segundos, salvo
que se fuerce; una petición limitada no se pierde, se escribe en cuanto pasa el
intervalo. El registro se construye en el momento de escribir, así que siempre
se guarda lo último.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable

from lector.models import Document, Position, SourceKind
from lector.persistence.repositories import ProgressRecord

log = logging.getLogger(__name__)

SAVE_INTERVAL_S = 2.0


def make_record(
    doc: Document,
    position: Position,
    chunk_id: str | None,
    *,
    edge_voice: str,
    piper_voice: str,
    speed: float,
    session_state: str,
) -> ProgressRecord:
    return ProgressRecord(
        doc_id=doc.doc_id,
        kind=doc.kind,
        origin=doc.origin,
        title=doc.title,
        last_read_at=time.time(),
        position=position,
        chapter_url=position.chapter_locator if doc.kind is SourceKind.URL else None,
        last_chunk_id=chunk_id,
        edge_voice=edge_voice,
        piper_voice=piper_voice,
        speed=speed,
        session_state=session_state,
    )


class ProgressWriter:
    def __init__(
        self,
        save: Callable[[ProgressRecord], None],
        build: Callable[[], ProgressRecord | None],
        *,
        clock: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
        on_error: Callable[[Exception], None],
        interval: float = SAVE_INTERVAL_S,
    ):
        """`save` es síncrona (se llama en un hilo); `build` devuelve None si no hay nada que guardar.

        `on_error` se llama en el bucle con el primer fallo de cada racha.
        """
        self._save = save
        self._build = build
        self._clock = clock
        self._sleep = sleep
        self._on_error = on_error
        self._interval = interval
        self._task: asyncio.Task | None = None
        self._replaced: asyncio.Task | None = None  # diferida cancelada que aún no terminó
        self._sleeping = False  # la tarea espera el intervalo; aún no escribe
        self._dirty = False
        self._last_write = -math.inf
        self._failing = False

    def request(self, force: bool = False) -> None:
        """Pide guardar: al momento si `force` o si ya pasó el intervalo; si no, cuando pase."""
        self._dirty = True
        task = self._task
        if task is not None and not task.done():
            if not (force and self._sleeping):
                return  # la escritura en curso (o la diferida) recoge este cambio
            task.cancel()  # está esperando, no escribiendo: se adelanta
            self._replaced = task
        delay = 0.0
        if not force:
            delay = max(0.0, self._interval - (self._clock() - self._last_write))
        # Se fija aquí, no dentro de la tarea: así se sabe antes de que empiece a correr.
        self._sleeping = delay > 0
        self._task = asyncio.create_task(self._run(delay), name="lector:save")

    async def save_now(self) -> None:
        self.request(force=True)
        await self.flush()

    async def flush(self) -> None:
        """Espera a que se escriba lo último pedido (adelanta una escritura diferida)."""
        while True:
            task = self._task
            if task is None or task.done():
                break
            if self._sleeping:
                self.request(force=True)
                continue
            await asyncio.wait({task})
        replaced, self._replaced = self._replaced, None
        if replaced is not None and not replaced.done():
            await asyncio.wait({replaced})

    async def _run(self, delay: float) -> None:
        if delay > 0:
            await self._sleep(delay)
            self._sleeping = False
        while self._dirty:
            self._dirty = False
            record = self._build()
            if record is None:
                return
            self._last_write = self._clock()
            try:
                await asyncio.to_thread(self._save, record)
            except Exception as exc:
                log.warning("No se pudo guardar el progreso", exc_info=True)
                if not self._failing:
                    self._failing = True
                    self._on_error(exc)
            else:
                self._failing = False
