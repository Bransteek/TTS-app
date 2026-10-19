"""Salida de audio al arrancar: sondeo con tope y reintentos que encuentran dispositivos nuevos.

- `default_output()` comprueba que haya un dispositivo de salida y devuelve una
  `SoundDeviceOutput`.
- `open_output()` hace ese sondeo en un hilo propio y daemon, con tope de
  `AUDIO_PROBE_TIMEOUT_S`: un PortAudio lento o colgado nunca impide arrancar ni salir
  (un hilo del executor de asyncio se esperaría sin límite al cerrar el intérprete). Si
  falla o tarda, la app abre igual con un aviso.
- `ProbeState` sabe si hay un sondeo o una creación de salida en curso. Mientras lo hay,
  `LazyOutput.start` no reinicia PortAudio ni vuelve a llamar a la fábrica (competiría con
  la llamada colgada): lanza `AudioOutputError` en el acto.
- `LazyOutput` envuelve la salida. Tras un fallo (al arrancar, al abrir el stream o con
  el stream ya sonando), el siguiente `start` reinicia PortAudio (para ver un dispositivo
  enchufado después) y crea una salida nueva. Si sigue sin haber, lanza
  `AudioOutputError`, que el controlador trata como cualquier fallo de la salida (un
  reintento y luego pausa con aviso).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Callable, Iterator
from typing import Any

from lector.audio.player import AudioOutput, SoundDeviceOutput
from lector.errors import AudioOutputError, LectorError

log = logging.getLogger(__name__)

AUDIO_PROBE_TIMEOUT_S = 5.0
PROBE_THREAD_NAME = "lector-audio-probe"

OutputFactory = Callable[[], AudioOutput]


def default_output() -> AudioOutput:
    """Salida real. Comprueba que haya un dispositivo de salida para poder avisar al arrancar."""
    import sounddevice as sd

    sd.query_devices(kind="output")  # sin dispositivo de salida por defecto, lanza
    return SoundDeviceOutput()


def reinit_portaudio(sd: Any = None) -> bool:
    """Reinicia PortAudio para que vuelva a listar los dispositivos. Nunca lanza.

    Usa funciones privadas de sounddevice (`_terminate`/`_initialize`): es la forma
    conocida de refrescar la lista sin reiniciar el proceso.
    """
    try:
        if sd is None:
            import sounddevice as sd
        sd._terminate()
        sd._initialize()
        return True
    except Exception:
        log.warning("No se pudo reiniciar PortAudio", exc_info=True)
        return False


class ProbeState:
    """Cuenta los sondeos o creaciones de salida en curso (de cualquier hilo)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = 0

    def busy(self) -> bool:
        with self._lock:
            return self._running > 0

    def enter(self) -> None:
        with self._lock:
            self._running += 1

    def leave(self) -> None:
        with self._lock:
            self._running = max(0, self._running - 1)

    @contextlib.contextmanager
    def running(self) -> Iterator[None]:
        self.enter()
        try:
            yield
        finally:
            self.leave()


class LazyOutput:
    """Salida que se rehace tras un fallo; ver el docstring del módulo."""

    def __init__(
        self,
        factory: OutputFactory,
        *,
        output: AudioOutput | None = None,
        failed: bool = False,
        reset: Callable[[], Any] | None = None,
        probe: ProbeState | None = None,
    ):
        self._factory = factory
        self._output = output
        self._failed = failed
        self._reset = reset
        self._probe = probe if probe is not None else ProbeState()

    def probe_busy(self) -> bool:
        return self._probe.busy()

    def start(self, callback, on_error=None) -> None:
        if (self._failed or self._output is None) and self._probe.busy():
            # Otra llamada a PortAudio sigue colgada: ni reiniciarlo ni llamar a la fábrica.
            self._failed = True
            raise AudioOutputError()
        if self._failed:
            if self._output is not None:
                try:
                    self._output.stop()
                except Exception:
                    log.debug("No se pudo parar la salida anterior", exc_info=True)
            self._output = None
            if self._reset is not None:
                self._reset()
        try:
            if self._output is None:
                with self._probe.running():
                    self._output = self._factory()

            def failed(exc: Exception) -> None:  # cualquier hilo (p. ej. el de PortAudio)
                self._failed = True
                if on_error is not None:
                    on_error(exc)

            self._output.start(callback, failed)
        except LectorError:
            self._failed = True
            raise
        except Exception as exc:
            self._failed = True
            raise AudioOutputError() from exc
        self._failed = False

    def stop(self) -> None:
        if self._output is not None:
            self._output.stop()


def _probe_in_daemon_thread(factory: OutputFactory, probe: ProbeState) -> asyncio.Future:
    """Llama a `factory` en un hilo daemon y devuelve un futuro del bucle actual."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()

    def deliver(ok: bool, value: Any) -> None:
        if future.done():
            return  # ya se dio por perdido (tope vencido)
        if ok:
            future.set_result(value)
        else:
            future.set_exception(value)

    def run() -> None:
        try:
            result: tuple[bool, Any] = (True, factory())
        except BaseException as exc:  # noqa: BLE001 (se entrega al bucle)
            result = (False, exc)
        finally:
            probe.leave()
        try:
            loop.call_soon_threadsafe(deliver, *result)
        except RuntimeError:
            pass  # el bucle ya se cerró: nadie espera el resultado

    probe.enter()
    try:
        threading.Thread(target=run, name=PROBE_THREAD_NAME, daemon=True).start()
    except BaseException:
        probe.leave()
        raise
    return future


async def open_output(
    factory: OutputFactory,
    *,
    timeout_s: float | None = None,
    reset: Callable[[], Any] | None = None,
    probe: ProbeState | None = None,
) -> tuple[LazyOutput, bool]:
    """Sondea la salida en un hilo daemon con tope. Devuelve (salida, ¿falló?); nunca lanza."""
    timeout_s = AUDIO_PROBE_TIMEOUT_S if timeout_s is None else timeout_s
    probe = probe if probe is not None else ProbeState()
    try:
        output = await asyncio.wait_for(_probe_in_daemon_thread(factory, probe), timeout_s)
    except TimeoutError:
        log.warning("La salida de audio tardó más de %s s en responder; se reintentará al leer",
                    timeout_s)
        return LazyOutput(factory, failed=True, reset=reset, probe=probe), True
    except Exception:
        log.warning("No hay salida de audio al arrancar; se reintentará al leer", exc_info=True)
        return LazyOutput(factory, failed=True, reset=reset, probe=probe), True
    return LazyOutput(factory, output=output, reset=reset, probe=probe), False
