"""Escritor de progreso: una sola tarea, escrituras limitadas y `flush` completo."""
from __future__ import annotations

import asyncio

from lector.services.progress_writer import ProgressWriter
from tests.controller_rig import WAIT_S, lector_tasks, settle, until
from tests.fakes import ManualClock


async def test_flush_waits_for_a_replaced_deferred_write_to_finish():
    clock = ManualClock()
    release = asyncio.Event()
    saved: list[int] = []

    async def slow_to_cancel(delay: float) -> None:
        try:
            await clock.sleep(delay)
        except asyncio.CancelledError:
            await release.wait()  # la tarea cancelada tarda en terminar
            raise

    writer = ProgressWriter(
        saved.append, lambda: len(saved) + 1,
        clock=clock.now, sleep=slow_to_cancel, on_error=lambda exc: None,
    )
    await asyncio.wait_for(writer.save_now(), WAIT_S)  # primera escritura
    writer.request()  # dentro del intervalo: queda diferida, durmiendo
    await settle()
    assert saved == [1]

    flushing = asyncio.create_task(writer.flush())
    await until(lambda: saved == [1, 2])  # se adelantó la escritura diferida
    await settle()
    assert not flushing.done()  # pero la tarea sustituida aún no terminó

    release.set()
    await asyncio.wait_for(flushing, WAIT_S)
    assert lector_tasks() == []  # ninguna tarea `lector:save` sobrevive a flush()
