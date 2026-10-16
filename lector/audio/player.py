"""Reproductor en memoria con cola limitada y seguimiento exacto de la frase que suena.

Hilos:
- `enqueue` y los métodos de control (`pause`, `resume`, `clear`, `close`) se llaman
  desde el bucle asyncio del hilo de trabajo.
- `_fill` es el callback de audio: corre en el hilo de PortAudio. Solo copia segmentos
  de numpy y avanza un índice; nunca espera al bucle.
- El estado compartido se protege con un `threading.Lock` que solo se retiene para
  operaciones O(1) y nunca mientras se espera algo.
- Los eventos y los avisos de hueco libre viajan al bucle con `loop.call_soon_threadsafe`.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from ..errors import AudioOutputError, LectorError
from ..models import Chunk
from .pcm import OUTPUT_RATE

log = logging.getLogger(__name__)


@dataclass(frozen=True, eq=False)
class QueuedClip:
    chunk: Chunk
    pcm: np.ndarray  # int16 mono a OUTPUT_RATE
    marks: list[tuple[int, int]]  # (muestra_inicial, sentence_index), crecientes, marks[0][0] == 0
    engine: str

    def __post_init__(self) -> None:
        if self.pcm.dtype != np.int16 or self.pcm.ndim != 1:
            raise ValueError("pcm must be a 1-D int16 array")
        if not self.marks or self.marks[0][0] != 0:
            raise ValueError("marks must start at sample 0")
        if any(b[0] < a[0] for a, b in zip(self.marks, self.marks[1:])):
            raise ValueError("marks must be in increasing sample order")


class PlayerEvents(Protocol):
    """Se llaman siempre en el bucle asyncio, nunca en el hilo de audio."""

    def on_sentence_started(self, chunk: Chunk, sentence_index: int) -> None: ...

    def on_chunk_finished(self, chunk: Chunk) -> None: ...

    def on_output_error(self, err: Exception) -> None: ...


class AudioOutput(Protocol):
    def start(
        self,
        callback: Callable[[np.ndarray], None],
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        """Arranca la salida. `callback(buf)` rellena un búfer int16 de `frames` muestras.

        `on_error(exc)` se llama (en cualquier hilo) si el stream falla después de arrancar.
        """
        ...

    def stop(self) -> None: ...


class _StreamState:
    __slots__ = ("stopping", "failure")

    def __init__(self) -> None:
        self.stopping = False
        self.failure: BaseException | None = None


class SoundDeviceOutput:
    """Salida real con PortAudio (sounddevice), mono int16."""

    def __init__(self, rate: int = OUTPUT_RATE, blocksize: int = 1024):
        self.rate = rate
        self.blocksize = blocksize
        self._stream = None
        self._state: _StreamState | None = None

    def start(
        self,
        callback: Callable[[np.ndarray], None],
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        import sounddevice as sd

        self.stop()
        state = _StreamState()

        def sd_callback(outdata, frames, time, status) -> None:
            try:
                callback(outdata[:, 0])
            except Exception as exc:  # un fallo aquí no debe dejar ruido en el altavoz
                outdata.fill(0)
                state.failure = exc
                raise sd.CallbackAbort from exc

        def finished() -> None:
            if state.stopping or on_error is None:
                return
            err = AudioOutputError()
            err.__cause__ = state.failure
            on_error(err)

        try:
            stream = sd.OutputStream(
                samplerate=self.rate,
                blocksize=self.blocksize,
                dtype="int16",
                channels=1,
                callback=sd_callback,
                finished_callback=finished,
            )
            stream.start()
        except Exception as exc:
            raise AudioOutputError() from exc
        self._stream, self._state = stream, state

    def stop(self) -> None:
        stream, state = self._stream, self._state
        self._stream = self._state = None
        if stream is None:
            return
        state.stopping = True
        try:
            stream.stop()
            stream.close()
        except Exception:
            log.warning("No se pudo cerrar la salida de audio", exc_info=True)


class AudioPlayer:
    def __init__(
        self,
        output: AudioOutput,
        loop: asyncio.AbstractEventLoop,
        events: PlayerEvents,
        max_ahead: int = 2,
        max_seconds: float = 90.0,
    ):
        self._output = output
        self._loop = loop
        self._events = events
        self._max_ahead = max_ahead
        self._max_samples = max_seconds * OUTPUT_RATE

        # Estado compartido con el hilo de audio (protegido por _lock).
        self._lock = threading.Lock()
        self._queue: deque[QueuedClip] = deque()
        self._current: QueuedClip | None = None
        self._pos = 0  # siguiente muestra de _current que se copiará
        self._next_mark = 0  # índice en _current.marks de la próxima marca por cruzar
        self._sentence = 0  # sentence_index de la última marca cruzada
        self._pending_samples = 0  # muestras de _current + _queue (clips completos)
        self._paused = False
        self._generation = 0  # cambia en clear()/close(): invalida eventos sin entregar

        # Solo en el bucle asyncio.
        self._cond: asyncio.Condition | None = None
        self._waiters = 0
        self._notify_tasks: set[asyncio.Task] = set()
        self._output_running = False

    # --- API del bucle -------------------------------------------------------------

    async def enqueue(self, clip: QueuedClip) -> None:
        """Encola el clip; espera si ya hay `max_ahead` clips además del que suena,
        o si con este se superarían `max_seconds` (salvo que no haya nada pendiente)."""
        if self._cond is None:
            self._cond = asyncio.Condition()
        async with self._cond:
            self._waiters += 1
            try:
                await self._cond.wait_for(lambda: self._has_room(len(clip.pcm)))
            finally:
                self._waiters -= 1
            with self._lock:
                self._queue.append(clip)
                self._pending_samples += len(clip.pcm)
        self._ensure_output()

    def pause(self) -> None:
        with self._lock:
            self._paused = True

    def resume(self) -> None:
        with self._lock:
            self._paused = False
        self._ensure_output()

    def clear(self) -> Chunk | None:
        """Vacía la cola y el clip actual; devuelve el `Chunk` que sonaba.

        Los eventos de los clips descartados que aún no se hayan entregado se descartan.
        """
        with self._lock:
            chunk = self._drop_all()
        self._post(self._wake_waiters)
        return chunk

    def current(self) -> tuple[Chunk, int] | None:
        with self._lock:
            if self._current is None:
                return None
            return self._current.chunk, self._sentence

    def queued_count(self) -> int:
        """Clips pendientes: el que suena (si hay) más los que esperan."""
        with self._lock:
            return len(self._queue) + (self._current is not None)

    def close(self) -> None:
        """Para la salida y descarta el audio. Un `enqueue`/`resume` posterior la reabre."""
        with self._lock:
            self._drop_all()
        if self._output_running:
            self._output_running = False
            self._stop_output()
        self._post(self._wake_waiters)

    # --- Hilo de audio ---------------------------------------------------------------

    def _fill(self, out: np.ndarray) -> None:
        """Callback de audio: rellena `out` (int16, `frames` muestras) sin bloquear."""
        frames = len(out)
        written = 0
        with self._lock:
            if not self._paused:
                while written < frames:
                    clip = self._current
                    if clip is None:
                        if not self._queue:
                            break
                        clip = self._current = self._queue.popleft()
                        self._pos = 0
                        self._next_mark = 0
                    marks = clip.marks
                    while self._next_mark < len(marks) and marks[self._next_mark][0] <= self._pos:
                        self._sentence = marks[self._next_mark][1]
                        self._next_mark += 1
                        self._emit(self._events.on_sentence_started, clip.chunk, self._sentence)
                    end = len(clip.pcm)
                    if self._next_mark < len(marks):
                        end = min(end, marks[self._next_mark][0])
                    take = min(frames - written, end - self._pos)
                    out[written:written + take] = clip.pcm[self._pos:self._pos + take]
                    written += take
                    self._pos += take
                    if self._pos >= len(clip.pcm):
                        self._current = None  # suelta la referencia al array
                        self._pending_samples -= len(clip.pcm)
                        self._emit(self._events.on_chunk_finished, clip.chunk)
                        self._post(self._wake_waiters)
        if written < frames:
            out[written:] = 0

    def _emit(self, handler: Callable, *args) -> None:
        # Se llama con _lock tomado: así un clear() posterior siempre ve una
        # generación más nueva que la de cualquier evento ya emitido.
        self._post(self._deliver, self._generation, handler, *args)

    def _report_output_failure(self, exc: Exception) -> None:
        """Puede llamarse desde cualquier hilo (p. ej. el de PortAudio)."""
        self._post(self._handle_output_failure, exc)

    # --- Internos ------------------------------------------------------------------

    def _post(self, fn: Callable, *args) -> None:
        try:
            self._loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:  # bucle cerrado: ya no hay a quién avisar
            pass

    def _drop_all(self) -> Chunk | None:
        chunk = self._current.chunk if self._current is not None else None
        self._current = None
        self._queue.clear()
        self._pos = self._next_mark = 0
        self._pending_samples = 0
        self._generation += 1
        return chunk

    def _has_room(self, samples: int) -> bool:
        with self._lock:
            pending = len(self._queue) + (self._current is not None)
            if pending == 0:
                return True
            if pending > self._max_ahead:
                return False
            return self._pending_samples + samples <= self._max_samples

    def _deliver(self, generation: int, handler: Callable, *args) -> None:
        if generation == self._generation:
            handler(*args)

    def _wake_waiters(self) -> None:
        if self._cond is None or self._waiters == 0:
            return
        task = self._loop.create_task(self._notify_all())
        self._notify_tasks.add(task)
        task.add_done_callback(self._notify_tasks.discard)

    async def _notify_all(self) -> None:
        async with self._cond:
            self._cond.notify_all()

    def _ensure_output(self) -> None:
        if self._output_running:
            return
        try:
            self._output.start(self._fill, on_error=self._report_output_failure)
        except Exception as exc:
            self._report_output_failure(exc)
            return
        self._output_running = True

    def _handle_output_failure(self, exc: Exception) -> None:
        if self._output_running:
            self._output_running = False
            self._stop_output()
        if not isinstance(exc, LectorError):
            err = AudioOutputError()
            err.__cause__ = exc
            exc = err
        self._events.on_output_error(exc)

    def _stop_output(self) -> None:
        try:
            self._output.stop()
        except Exception:
            log.warning("No se pudo parar la salida de audio", exc_info=True)
