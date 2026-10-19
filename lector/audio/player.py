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
    marks: list[tuple[int, int]]  # (muestra_inicial, sentence_index): 0, estrictamente crecientes, < len(pcm)
    engine: str

    def __post_init__(self) -> None:
        if self.pcm.dtype != np.int16 or self.pcm.ndim != 1:
            raise ValueError("pcm must be a 1-D int16 array")
        if not self.marks or self.marks[0][0] != 0:
            raise ValueError("marks must start at sample 0")
        if any(b[0] <= a[0] for a, b in zip(self.marks, self.marks[1:])):
            raise ValueError("mark offsets must be strictly increasing")
        if self.marks[-1][0] >= len(self.pcm):
            raise ValueError("every mark must fall inside pcm")


class PlayerEvents(Protocol):
    """Se llaman siempre en el bucle asyncio, nunca en el hilo de audio."""

    def on_sentence_started(self, chunk: Chunk, sentence_index: int) -> None: ...

    def on_chunk_finished(self, chunk: Chunk) -> None: ...

    def on_output_error(self, err: Exception) -> None: ...


class _NoEvents:
    """Destinatario provisional hasta que alguien llame a `set_events`."""

    def on_sentence_started(self, chunk: Chunk, sentence_index: int) -> None:
        pass

    def on_chunk_finished(self, chunk: Chunk) -> None:
        pass

    def on_output_error(self, err: Exception) -> None:
        log.warning("Fallo de la salida de audio sin destinatario: %s", err)


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
    """Reproductor con cola limitada.

    Ciclo de vida de la salida:
    - El primer `enqueue` arranca la salida (un solo intento).
    - Si la salida falla (o se atasca: `restart_output()`), se para y se avisa una vez con
      `on_output_error`. La cola se conserva, el clip actual vuelve al inicio de la frase
      que sonaba y **solo** `resume()` (lo llama el controlador) vuelve a arrancarla.
    - `close()` es definitivo: para la salida, descarta el audio y desde entonces
      `enqueue` lanza `AudioOutputError(recoverable=False)` (también a quien estaba
      esperando) y `resume()` no hace nada.
    """

    def __init__(
        self,
        output: AudioOutput,
        loop: asyncio.AbstractEventLoop,
        events: PlayerEvents | None = None,
        max_ahead: int = 2,
        max_seconds: float = 90.0,
    ):
        self._output = output
        self._loop = loop
        self._events: PlayerEvents = events if events is not None else _NoEvents()
        self._max_ahead = max_ahead
        self._max_samples = max_seconds * OUTPUT_RATE

        # Estado compartido con el hilo de audio (protegido por _lock).
        self._lock = threading.Lock()
        self._queue: deque[QueuedClip] = deque()
        self._current: QueuedClip | None = None
        self._pos = 0  # siguiente muestra de _current que se copiará
        self._next_mark = 0  # índice en _current.marks de la próxima marca por cruzar
        self._sentence = 0  # frase de _current que suena (o la primera, si aún no empezó)
        self._pending_samples = 0  # muestras de _current + _queue (clips completos)
        self._paused = False
        self._generation = 0  # cambia en clear()/close(): invalida eventos y enqueues previos
        self._played = 0  # muestras de audio entregadas a la salida (no el silencio)

        # Solo en el bucle asyncio.
        self._cond: asyncio.Condition | None = None
        self._waiters = 0
        self._notify_tasks: set[asyncio.Task] = set()
        self._closed = False
        self._output_running = False
        self._start_attempted = False
        self._output_gen = 0  # cambia en cada arranque, fallo atendido o close()

    # --- API del bucle -------------------------------------------------------------

    def set_events(self, events: PlayerEvents) -> None:
        """Cambia el destinatario de los eventos (el controlador se registra al crearse)."""
        self._events = events

    async def enqueue(self, clip: QueuedClip) -> bool:
        """Encola el clip; espera si ya hay `max_ahead` clips además del que suena,
        o si con este se superarían `max_seconds` (salvo que no haya nada pendiente).

        Devuelve `False` (sin encolar) si un `clear()` ocurrió mientras esperaba: el
        clip pertenece a una posición ya descartada. Lanza `AudioOutputError` con
        `recoverable=False` si el reproductor está cerrado o se cierra mientras espera.
        """
        if self._closed:
            raise AudioOutputError(recoverable=False)
        if self._cond is None:
            self._cond = asyncio.Condition()
        generation = self._generation
        samples = len(clip.pcm)
        async with self._cond:
            self._waiters += 1
            try:
                await self._cond.wait_for(
                    lambda: self._closed
                    or self._generation != generation
                    or self._has_room(samples)
                )
            finally:
                self._waiters -= 1
            if self._closed:
                raise AudioOutputError(recoverable=False)
            if self._generation != generation:
                return False
            with self._lock:
                self._queue.append(clip)
                self._pending_samples += samples
                self._promote_locked()
        if not self._start_attempted:
            self._start_output()
        return True

    def pause(self) -> None:
        with self._lock:
            self._paused = True

    def resume(self) -> None:
        """Reanuda y, si la salida estaba parada por un fallo, la vuelve a arrancar."""
        with self._lock:
            self._paused = False
        self._start_output()

    def clear(self) -> Chunk | None:
        """Vacía la cola y el clip actual; devuelve el `Chunk` que sonaba.

        Los eventos de los clips descartados que aún no se hayan entregado se descartan,
        y los `enqueue` que estaban esperando devuelven `False` sin encolar.
        """
        chunk, dropped = self._drop_all()
        del dropped  # los arrays se liberan aquí, fuera del candado
        self._post(self._wake_waiters)
        return chunk

    def drop_queued(self) -> list[Chunk]:
        """Descarta los clips en espera, pero no el que suena; devuelve sus `Chunk` en orden.

        No cambia la generación: el clip actual sigue sonando con todos sus eventos, y
        un `enqueue` que esperaba sitio puede entrar en el hueco (quien llama debe
        cancelar antes al productor si no quiere eso).
        """
        with self._lock:
            dropped, self._queue = self._queue, deque()
            self._pending_samples -= sum(len(clip.pcm) for clip in dropped)
        chunks = [clip.chunk for clip in dropped]
        del dropped  # los arrays se liberan aquí, fuera del candado
        self._post(self._wake_waiters)
        return chunks

    def current(self) -> tuple[Chunk, int] | None:
        """`(chunk, sentence_index)` de lo que el callback está entregando a la salida.

        Puede ir por delante de lo que realmente se oye como mucho un bloque
        (`blocksize`) más la latencia de la salida. Solo es `None` si no hay audio
        pendiente.
        """
        with self._lock:
            if self._current is None:
                return None
            return self._current.chunk, self._sentence

    def played_samples(self) -> int:
        """Muestras de audio entregadas a la salida desde que se creó (el silencio no cuenta).

        Si hay audio pendiente, sin pausa y la salida en marcha, y este número no crece,
        la salida se atascó sin avisar (lo vigila el controlador).
        """
        with self._lock:
            return self._played

    def restart_output(self) -> None:
        """La salida dejó de pedir audio sin avisar: se trata como un fallo de la salida.

        Se para, se marca para reabrirla de cero (como tras desenchufar el dispositivo) y
        se avisa con `on_output_error`; quien la vuelva a abrir con `resume()` sigue desde
        el inicio de la frase que sonaba.
        """
        if self._closed or not self._output_running:
            return
        invalidate = getattr(self._output, "invalidate", None)
        if invalidate is not None:
            invalidate()
        self._handle_output_failure(self._output_gen, AudioOutputError())

    def queued_count(self) -> int:
        """Clips pendientes: el que suena (si hay) más los que esperan."""
        with self._lock:
            return len(self._queue) + (self._current is not None)

    def close(self) -> None:
        """Cierre definitivo: para la salida y descarta el audio (ver docstring de clase)."""
        self._closed = True
        _, dropped = self._drop_all()
        del dropped
        self._output_gen += 1  # un fallo que llegue tarde ya no es de nadie
        if self._output_running:
            self._output_running = False
            self._stop_output()
        self._post(self._wake_waiters)

    # --- Hilo de audio ---------------------------------------------------------------

    def _fill(self, out: np.ndarray) -> None:
        """Callback de audio: rellena `out` (int16, `frames` muestras) sin bloquear.

        Nunca libera arrays con el candado tomado: los clips terminados se sueltan
        al final, ya fuera del candado.
        """
        frames = len(out)
        written = 0
        finished: list[QueuedClip] | None = None
        with self._lock:
            clip = None if self._paused else self._current
            while clip is not None and written < frames:
                marks = clip.marks
                while self._next_mark < len(marks) and marks[self._next_mark][0] <= self._pos:
                    self._sentence = marks[self._next_mark][1]
                    self._next_mark += 1
                    self._emit(self._events.on_sentence_started, clip.chunk, self._sentence)
                end = len(clip.pcm)
                if self._next_mark < len(marks):
                    end = marks[self._next_mark][0]
                take = min(frames - written, end - self._pos)
                out[written:written + take] = clip.pcm[self._pos:self._pos + take]
                written += take
                self._pos += take
                if self._pos >= len(clip.pcm):
                    self._pending_samples -= len(clip.pcm)
                    self._emit(self._events.on_chunk_finished, clip.chunk)
                    self._post(self._wake_waiters)
                    if finished is None:
                        finished = []
                    finished.append(clip)
                    self._current = None
                    self._promote_locked()
                    clip = self._current
            clip = None
            self._played += written
        if written < frames:
            out[written:] = 0
        finished = None  # última referencia a los clips terminados: se liberan aquí

    def _emit(self, handler: Callable, *args) -> None:
        # Se llama con _lock tomado: así un clear() posterior siempre ve una
        # generación más nueva que la de cualquier evento ya emitido.
        self._post(self._deliver, self._generation, handler, *args)

    # --- Internos ------------------------------------------------------------------

    def _post(self, fn: Callable, *args) -> None:
        try:
            self._loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:  # bucle cerrado: ya no hay a quién avisar
            pass

    def _promote_locked(self) -> None:
        """Con _lock tomado: si no hay clip actual, el siguiente de la cola pasa a serlo.

        Su primera marca se emite cuando el callback escribe sus primeras muestras.
        """
        if self._current is None and self._queue:
            clip = self._current = self._queue.popleft()
            self._pos = 0
            self._next_mark = 0
            self._sentence = clip.marks[0][1]

    def _drop_all(self) -> tuple[Chunk | None, tuple]:
        """Separa todo el audio bajo el candado; quien llama lo suelta fuera de él."""
        fresh: deque[QueuedClip] = deque()
        with self._lock:
            current, queue = self._current, self._queue
            self._current = None
            self._queue = fresh
            self._pos = self._next_mark = 0
            self._pending_samples = 0
            self._generation += 1
        chunk = current.chunk if current is not None else None
        return chunk, (current, queue)

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

    def _start_output(self) -> None:
        if self._closed or self._output_running:
            return
        self._start_attempted = True
        self._output_gen += 1
        gen = self._output_gen

        def on_error(exc: Exception) -> None:  # cualquier hilo (p. ej. el de PortAudio)
            self._post(self._handle_output_failure, gen, exc)

        try:
            self._output.start(self._fill, on_error=on_error)
        except Exception as exc:
            self._post(self._handle_output_failure, gen, exc)
            return
        self._output_running = True

    def _handle_output_failure(self, gen: int, exc: Exception) -> None:
        if gen != self._output_gen:
            return  # fallo de una salida anterior: no toca la actual
        self._output_gen += 1  # cada arranque avisa como mucho una vez
        if self._output_running:
            self._output_running = False
            self._stop_output()
        self._rewind_to_sentence()
        if not isinstance(exc, LectorError):
            err = AudioOutputError()
            err.__cause__ = exc
            exc = err
        self._events.on_output_error(exc)

    def _rewind_to_sentence(self) -> None:
        """Tras un corte, el clip actual vuelve al inicio de la frase que sonaba (§5.6).

        Lo ya entregado a la salida pudo no oírse (búfer del dispositivo). No se vuelve a
        emitir el inicio de la frase: ya se avisó.
        """
        with self._lock:
            clip = self._current
            if clip is not None and self._next_mark > 0:
                self._pos = clip.marks[self._next_mark - 1][0]

    def _stop_output(self) -> None:
        try:
            self._output.stop()
        except Exception:
            log.warning("No se pudo parar la salida de audio", exc_info=True)
