"""Salida de audio al arrancar: sondeo con tope y reintentos que encuentran dispositivos nuevos.

- `default_output()` comprueba que haya un dispositivo de salida y devuelve una
  `SoundDeviceOutput`.
- `open_output()` hace ese sondeo en un hilo propio y daemon, con tope de
  `AUDIO_PROBE_TIMEOUT_S`: un PortAudio lento o colgado nunca impide arrancar ni salir
  (un hilo del executor de asyncio se esperaría sin límite al cerrar el intérprete). Si
  falla o tarda, la app abre igual con un aviso.
- `ProbeState` sabe si hay un sondeo, una reapertura o una parada de la salida en curso.
  Mientras lo hay, `LazyOutput.start` no reinicia PortAudio ni vuelve a llamar a la
  fábrica (competiría con la llamada colgada): lanza `AudioOutputError` en el acto.
- `LazyOutput` envuelve la salida. Tras un fallo (al arrancar, al abrir el stream o con
  el stream ya sonando), el siguiente `start` reinicia PortAudio (para ver un dispositivo
  enchufado después) y crea una salida nueva, en un hilo daemon con tope: el bucle del
  trabajador nunca espera a PortAudio. Si sigue sin haber salida o vence el tope, avisa
  con `on_error` (`AudioOutputError`), que el controlador trata como cualquier fallo de
  la salida (reintentos con espera creciente y luego pausa con aviso).
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

AUDIO_PROBE_TIMEOUT_S = 5.0  # sondeo al arrancar y cada reapertura de la salida
OUTPUT_STOP_TIMEOUT_S = 1.0  # parar un stream: más que esto es que PortAudio se colgó
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


class _Reopen:
    """Una reapertura en curso. Pasa una sola vez de «pending» a «ok» o a «dead»."""

    __slots__ = ("lock", "state")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state = "pending"

    def settle(self, state: str) -> bool:
        with self.lock:
            if self.state != "pending":
                return False
            self.state = state
            return True


class LazyOutput:
    """Salida que se rehace tras un fallo; ver el docstring del módulo.

    Una salida sana arranca en el acto. Reabrirla (tras un fallo, o sin salida aún)
    reinicia PortAudio y llama a la fábrica: eso puede colgarse justo cuando se cambia
    de dispositivo o se despierta el equipo, así que se hace en un hilo daemon y
    `start` vuelve enseguida. Si falla, o no termina en `reopen_timeout_s`, se avisa con
    `on_error` (desde otro hilo), como cualquier fallo de la salida. Una salida que
    llega después del tope se para y se descarta. `stop` espera como mucho
    `stop_timeout_s` a que se pare el stream; si no, lo abandona y la próxima vez se
    reabre.
    """

    def __init__(
        self,
        factory: OutputFactory,
        *,
        output: AudioOutput | None = None,
        failed: bool = False,
        reset: Callable[[], Any] | None = None,
        probe: ProbeState | None = None,
        reopen_timeout_s: float = AUDIO_PROBE_TIMEOUT_S,
        stop_timeout_s: float = OUTPUT_STOP_TIMEOUT_S,
    ):
        self._factory = factory
        self._output = output
        self._failed = failed
        self._reset = reset
        self._probe = probe if probe is not None else ProbeState()
        self._reopen_timeout_s = reopen_timeout_s
        self._stop_timeout_s = stop_timeout_s
        self._lock = threading.Lock()  # _output, _failed y _attempt (los tocan otros hilos)
        self._attempt: _Reopen | None = None

    def probe_busy(self) -> bool:
        return self._probe.busy()

    def start(self, callback, on_error=None) -> None:
        with self._lock:
            output = None if self._failed else self._output
        if output is None:
            if self._probe.busy():
                # Otra llamada a PortAudio sigue colgada: ni reiniciarlo ni llamar a la fábrica.
                self._failed = True
                raise AudioOutputError()
            self._reopen(callback, on_error)
            return
        try:
            output.start(callback, self._watch(None, on_error))
        except LectorError:
            self._failed = True
            raise
        except Exception as exc:
            self._failed = True
            raise AudioOutputError() from exc

    def stop(self) -> None:
        with self._lock:
            attempt, self._attempt = self._attempt, None
            output = self._output
        if attempt is not None:
            attempt.settle("dead")  # una reapertura en curso ya no es de nadie
        if output is None:
            return
        if not self._stop_bounded(output):
            with self._lock:
                if self._output is output:
                    self._output = None
                    self._failed = True

    # --- internos ---------------------------------------------------------------------

    def _watch(self, attempt: _Reopen | None, on_error):
        """Aviso de fallo del stream ya en marcha (llega desde el hilo de PortAudio)."""

        def failed(exc: Exception) -> None:
            if attempt is not None:
                with attempt.lock:
                    if attempt.state == "dead":
                        return  # salida abandonada: su fallo ya no importa
                    if attempt.state == "pending":
                        attempt.state = "dead"  # falló antes de quedar instalada
            self._failed = True
            if on_error is not None:
                on_error(exc)

        return failed

    def _reopen(self, callback, on_error) -> None:
        attempt = _Reopen()
        with self._lock:
            old, self._output = self._output, None
            self._failed = True  # hasta que la salida nueva arranque
            self._attempt = attempt

        def report(exc: Exception) -> None:
            if attempt.settle("dead") and on_error is not None:
                on_error(exc)

        def on_timeout() -> None:
            if attempt.state == "pending":
                log.warning("Reabrir la salida de audio tardó más de %s s; se reintentará",
                            self._reopen_timeout_s)
            report(AudioOutputError())

        def work() -> None:
            try:
                if old is not None:
                    try:
                        old.stop()
                    except Exception:
                        log.debug("No se pudo parar la salida anterior", exc_info=True)
                if self._reset is not None:
                    self._reset()
                try:
                    output = self._factory()
                    output.start(callback, self._watch(attempt, on_error))
                except Exception as exc:
                    timer.cancel()
                    if not isinstance(exc, LectorError):
                        err = AudioOutputError()
                        err.__cause__ = exc
                        exc = err
                    report(exc)
                    return
                timer.cancel()
                if attempt.settle("ok"):
                    with self._lock:
                        if self._attempt is attempt:
                            self._output = output
                            self._failed = False
                            return
                log.info("La salida de audio se reabrió tarde; se descarta")
                try:
                    output.stop()
                except Exception:
                    log.debug("No se pudo parar la salida descartada", exc_info=True)
            finally:
                self._probe.leave()

        timer = threading.Timer(self._reopen_timeout_s, on_timeout)
        timer.daemon = True
        self._probe.enter()
        try:
            timer.start()
            threading.Thread(target=work, name=PROBE_THREAD_NAME, daemon=True).start()
        except BaseException:
            timer.cancel()
            self._probe.leave()
            raise

    def _stop_bounded(self, output: AudioOutput) -> bool:
        """Para `output` en un hilo daemon y espera como mucho `stop_timeout_s`."""
        done = threading.Event()

        def run() -> None:
            try:
                output.stop()
            except Exception:
                log.warning("No se pudo parar la salida de audio", exc_info=True)
            finally:
                self._probe.leave()
                done.set()

        self._probe.enter()
        try:
            threading.Thread(target=run, name=PROBE_THREAD_NAME, daemon=True).start()
        except BaseException:
            self._probe.leave()
            raise
        if done.wait(self._stop_timeout_s):
            return True
        log.warning("La salida de audio no se paró en %s s; se abandona", self._stop_timeout_s)
        return False


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
