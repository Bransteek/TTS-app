"""Dobles de prueba compartidos (otras tareas añaden más aquí)."""
from __future__ import annotations

from collections.abc import Callable

import numpy as np

# Valor de relleno del búfer antes de llamar al callback: si el reproductor
# deja alguna muestra sin escribir, la prueba lo ve en lugar de ver ceros.
UNWRITTEN = 12345


class FakeOutput:
    """Salida de audio falsa: guarda el callback y lo invoca de forma síncrona con `pull`.

    Entrega al callback la misma vista que `SoundDeviceOutput`: la columna 0 de un
    bloque int16 de forma `(frames, 1)`.
    """

    def __init__(self, *, start_error: Exception | None = None):
        self.start_error = start_error
        self.callback: Callable[[np.ndarray], None] | None = None
        self.on_error: Callable[[Exception], None] | None = None
        self.attempts = 0  # llamadas a start(), incluidas las que fallan
        self.starts = 0  # arranques con éxito
        self.stops = 0

    @property
    def running(self) -> bool:
        return self.callback is not None

    def start(
        self,
        callback: Callable[[np.ndarray], None],
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        self.attempts += 1
        if self.start_error is not None:
            raise self.start_error
        self.callback = callback
        self.on_error = on_error
        self.starts += 1

    def stop(self) -> None:
        self.stops += 1
        self.callback = None

    def pull(self, frames: int) -> np.ndarray:
        if self.callback is None:
            raise RuntimeError("output not started")
        block = np.full((frames, 1), UNWRITTEN, dtype=np.int16)
        self.callback(block[:, 0])
        return block[:, 0]

    def fail(self, exc: Exception) -> None:
        """Simula que el stream real se cae (lo que haría PortAudio en su hilo)."""
        assert self.on_error is not None
        self.on_error(exc)
