"""«Escuchar muestra» de una voz Piper: síntesis y reproducción en memoria."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from enum import StrEnum

from lector.audio.pcm import OUTPUT_RATE
from lector.audio.player import AudioOutput, AudioPlayer, QueuedClip, SoundDeviceOutput
from lector.models import Chunk, Sentence

log = logging.getLogger(__name__)

# Frases de la muestra: sin nada que se parezca a una promesa sobre la voz.
SAMPLE_SENTENCES = (
    "Hola, esta es una muestra de la voz sin conexión.",
    "Así sonará la lectura cuando no haya internet.",
)
SAMPLE_GRACE_S = 3.0  # margen sobre la duración de la muestra antes de darla por terminada
BUSY_POLL_S = 0.2  # cada cuánto se mira, mientras suena, si ha vuelto la lectura


class SampleResult(StrEnum):
    PLAYED = "played"  # sonó entera
    BUSY = "busy"  # la lectura estaba en marcha: no se sintetizó o no se reprodujo
    STOPPED = "stopped"  # se pidió parar (otra muestra, o se cerró la configuración)
    CUT = "cut"  # se cortó porque la lectura volvió mientras sonaba


def _sample_chunk() -> Chunk:
    sentences, start = [], 0
    for index, text in enumerate(SAMPLE_SENTENCES):
        sentences.append(Sentence(index, 0, text, start, start + len(text), None))
        start += len(text) + 1
    text = " ".join(SAMPLE_SENTENCES)
    return Chunk("muestra", "muestra", 0, 0, tuple(sentences), text, 0, len(text))


class _SampleEvents:
    def __init__(self, finished: asyncio.Future):
        self.finished = finished

    def on_sentence_started(self, chunk: Chunk, sentence_index: int) -> None:
        pass

    def on_chunk_finished(self, chunk: Chunk) -> None:
        if not self.finished.done():
            self.finished.set_result(None)

    def on_output_error(self, err: Exception) -> None:
        if not self.finished.done():
            self.finished.set_exception(err)


class SamplePlayer:
    """Sintetiza con Piper y reproduce en memoria, solo con la lectura parada o en pausa.

    Corre en el hilo de trabajo. `is_busy()` (también en el trabajador) se mira antes de
    sintetizar, antes de reproducir y cada `BUSY_POLL_S` mientras suena: si la lectura
    vuelve, la muestra se corta. `stop()` invalida también una síntesis en curso, así que
    después de cerrar la configuración no suena nada. Usa su propio `AudioPlayer` con su
    propia salida, que se cierra al terminar; el audio nunca se escribe en disco.

    Comparte el `PiperEngine` de la lectura (un solo modelo en memoria): escuchar otra voz
    descarga el modelo de la lectura, y al volver a leer con Piper se recarga (~1–2 s).
    """

    def __init__(
        self,
        engine,
        output_factory: Callable[[], AudioOutput] = SoundDeviceOutput,
        is_busy: Callable[[], bool] = lambda: False,
    ):
        self._engine = engine
        self._output_factory = output_factory
        self._is_busy = is_busy
        self._generation = 0  # cambia en cada play() y stop(): lo anterior ya no suena
        self._player: AudioPlayer | None = None
        self._finished: asyncio.Future | None = None

    async def play(self, voice: str, speed: float = 1.0) -> SampleResult:
        """Reproduce la muestra y vuelve cuando termina, se corta o se para."""
        if self._is_busy():
            return SampleResult.BUSY
        await self.stop()
        generation = self._generation
        chunk = _sample_chunk()
        clip = await self._engine.synthesize(chunk, voice, speed)
        if generation != self._generation:
            return SampleResult.STOPPED  # se paró (o se pidió otra) mientras se sintetizaba
        if self._is_busy():
            return SampleResult.BUSY
        loop = asyncio.get_running_loop()
        finished = loop.create_future()
        player = AudioPlayer(self._output_factory(), loop, events=_SampleEvents(finished))
        self._player, self._finished = player, finished
        try:
            await player.enqueue(QueuedClip(chunk, clip.pcm, clip.marks, "piper"))
            return await self._wait(finished, generation, len(clip.pcm) / OUTPUT_RATE)
        finally:
            player.close()
            if self._player is player:
                self._player = self._finished = None

    async def _wait(self, finished: asyncio.Future, generation: int, seconds: float) -> SampleResult:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds + SAMPLE_GRACE_S
        while not finished.done():
            if self._is_busy():
                return SampleResult.CUT
            remaining = deadline - loop.time()
            if remaining <= 0:
                log.warning("La muestra no terminó de sonar a tiempo; se corta")
                return SampleResult.PLAYED
            await asyncio.wait({finished}, timeout=min(BUSY_POLL_S, remaining))
        finished.result()  # un fallo de la salida de audio llega a quien pidió la muestra
        return SampleResult.PLAYED if generation == self._generation else SampleResult.STOPPED

    async def stop(self) -> None:
        """Corta la muestra que suene y anula la que se esté sintetizando."""
        self._generation += 1
        player, finished = self._player, self._finished
        self._player = self._finished = None
        if player is not None:
            player.close()
        if finished is not None and not finished.done():
            finished.set_result(None)
