"""Salida de audio al arrancar: sondeo con tope y reintentos que encuentran dispositivos nuevos.

- `default_output()` comprueba que haya un dispositivo de salida y devuelve una
  `SoundDeviceOutput`.
- `open_output()` hace ese sondeo en un hilo, con tope de `AUDIO_PROBE_TIMEOUT_S`: un
  PortAudio lento o colgado nunca impide arrancar. Si falla o tarda, la app abre igual
  con un aviso.
- `LazyOutput` envuelve la salida. Tras un fallo (al arrancar, al abrir el stream o con
  el stream ya sonando), el siguiente `start` reinicia PortAudio (para ver un dispositivo
  enchufado después) y crea una salida nueva. Si sigue sin haber, lanza
  `AudioOutputError`, que el controlador trata como cualquier fallo de la salida (un
  reintento y luego pausa con aviso).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from lector.audio.player import AudioOutput, SoundDeviceOutput
from lector.errors import AudioOutputError, LectorError

log = logging.getLogger(__name__)

AUDIO_PROBE_TIMEOUT_S = 5.0

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


class LazyOutput:
    """Salida que se rehace tras un fallo; ver el docstring del módulo."""

    def __init__(
        self,
        factory: OutputFactory,
        *,
        output: AudioOutput | None = None,
        failed: bool = False,
        reset: Callable[[], Any] | None = None,
    ):
        self._factory = factory
        self._output = output
        self._failed = failed
        self._reset = reset

    def start(self, callback, on_error=None) -> None:
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


async def open_output(
    factory: OutputFactory,
    *,
    timeout_s: float | None = None,
    reset: Callable[[], Any] | None = None,
) -> tuple[LazyOutput, bool]:
    """Sondea la salida en un hilo con tope. Devuelve (salida, ¿falló?); nunca lanza."""
    timeout_s = AUDIO_PROBE_TIMEOUT_S if timeout_s is None else timeout_s
    try:
        output = await asyncio.wait_for(asyncio.to_thread(factory), timeout_s)
    except TimeoutError:
        log.warning("La salida de audio tardó más de %s s en responder; se reintentará al leer",
                    timeout_s)
        return LazyOutput(factory, failed=True, reset=reset), True
    except Exception:
        log.warning("No hay salida de audio al arrancar; se reintentará al leer", exc_info=True)
        return LazyOutput(factory, failed=True, reset=reset), True
    return LazyOutput(factory, output=output, reset=reset), False
