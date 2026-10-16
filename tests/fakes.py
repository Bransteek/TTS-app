"""Dobles de prueba compartidos (otras tareas añaden más aquí)."""
from __future__ import annotations

import asyncio
import types
from collections.abc import Callable

import numpy as np

from lector.tts.base import AudioClip

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


class FakeCommunicate:
    """Doble de `edge_tts.Communicate`.

    `script` es una lista de chunks (dicts como los que emite `stream()`) o una
    excepción que se lanza al empezar a iterar. `factory` hace de
    `communicate_factory`: registra en `calls` los (args, kwargs) de cada
    construcción y devuelve este mismo objeto.
    """

    def __init__(self, script: list[dict] | Exception):
        self.script = script
        self.calls: list[tuple[tuple, dict]] = []

    def factory(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self

    @property
    def kwargs(self) -> dict:
        return self.calls[-1][1]

    async def stream(self):
        if isinstance(self.script, Exception):
            raise self.script
        for chunk in self.script:
            yield chunk


class FakeAudioChunk:
    """Doble de `piper.AudioChunk`: solo lo que usa el motor."""

    def __init__(self, samples: np.ndarray, sample_rate: int):
        self.audio_int16_array = samples
        self.sample_rate = sample_rate


class FakePiperVoice:
    """Doble de `piper.PiperVoice`.

    Cada llamada a `synthesize` devuelve un chunk de 22050 Hz de `per_char`
    muestras por carácter; registra `(texto, syn_config)` en `calls`.
    Los textos en `silent` producen un chunk vacío.
    """

    def __init__(
        self,
        speaker_id_map: dict[str, int] | None = None,
        sample_rate: int = 22050,
        per_char: int = 100,
        silent: tuple[str, ...] = (),
    ):
        self.config = types.SimpleNamespace(
            speaker_id_map=speaker_id_map or {},
            num_speakers=len(speaker_id_map or {}) or 1,
            sample_rate=sample_rate,
        )
        self.sample_rate = sample_rate
        self.per_char = per_char
        self.silent = silent
        self.calls: list[tuple[str, object]] = []

    def synthesize(self, text, syn_config=None, include_alignments=False):
        self.calls.append((text, syn_config))
        n = 0 if text in self.silent else len(text) * self.per_char
        yield FakeAudioChunk(np.full(n, 1000, dtype=np.int16), self.sample_rate)


class FakeClock:
    """Reloj falso: `sleep` no espera de verdad, avanza el tiempo y anota la duración.

    Cede una vez el bucle de eventos (`asyncio.sleep(0)`) para que las tareas se
    puedan cancelar mientras "esperan".
    """

    def __init__(self, start: float = 0.0):
        self.t = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds
        await asyncio.sleep(0)


class _FakeEngine:
    """Motor TTS falso.

    Cada `synthesize` toma el siguiente resultado de `errors` (una excepción o
    `None` para éxito); cuando la cola se vacía usa `error` (None = éxito).
    `health_check` devuelve `healthy` o, si es None, `error is None`.
    Registra `calls` como (chunk_id, voz, velocidad, instante) y `probes` como
    (voz, instante), con el instante de `clock` si se da.
    """

    name = ""

    def __init__(
        self,
        *,
        error: type[BaseException] | BaseException | None = None,
        errors: list | tuple = (),
        healthy: bool | None = None,
        clock: FakeClock | None = None,
    ):
        self.error = error
        self.errors = list(errors)
        self.healthy = healthy
        self.clock = clock
        self.calls: list[tuple[str, str, float, float | None]] = []
        self.probes: list[tuple[str, float | None]] = []
        self.in_flight = 0
        self.max_in_flight = 0

    def _now(self) -> float | None:
        return self.clock.now() if self.clock else None

    async def synthesize(self, chunk, voice: str, speed: float) -> AudioClip:
        self.calls.append((chunk.chunk_id, voice, speed, self._now()))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0)
            outcome = self.errors.pop(0) if self.errors else self.error
            if outcome is not None:
                raise outcome
            return AudioClip(
                pcm=np.full(240, 1000, dtype=np.int16),
                marks=[(0, chunk.sentences[0].index)],
            )
        finally:
            self.in_flight -= 1

    async def health_check(self, voice: str) -> bool:
        self.probes.append((voice, self._now()))
        await asyncio.sleep(0)
        return self.error is None if self.healthy is None else self.healthy

    async def list_voices(self) -> list[dict]:
        return []


class FakeEdge(_FakeEngine):
    name = "edge"


class FakePiper(_FakeEngine):
    name = "piper"


class FakeSource:
    """Fuente de contenido falsa con capítulos en memoria (sin red).

    `chapters` mapea locator -> Chapter. `failures` es una cola de excepciones:
    cada `load_chapter` lanza la siguiente antes de devolver nada. `loads`
    registra los locators pedidos, en orden.
    """

    def __init__(self, chapters: dict | None = None, failures: list | tuple = ()):
        self.chapters = dict(chapters or {})
        self.failures = list(failures)
        self.loads: list[str] = []

    async def open(self, locator: str, prefetched_html: str | None = None):
        raise NotImplementedError

    async def load_chapter(self, ref):
        self.loads.append(ref.locator)
        await asyncio.sleep(0)
        if self.failures:
            raise self.failures.pop(0)
        return self.chapters[ref.locator]

    async def list_chapters(self) -> list:
        return [c.ref for c in self.chapters.values()]
