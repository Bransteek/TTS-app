"""Salida de audio: sondeo con tope al arrancar y reapertura (que reinicia PortAudio) en un
hilo daemon con tope, sin bloquear nunca el bucle del trabajador."""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

import numpy as np

from lector.audio.player import AudioPlayer, QueuedClip
from lector.errors import AudioOutputError
from lector.models import Chunk
from lector.services.audio_output import (
    PROBE_THREAD_NAME,
    LazyOutput,
    ProbeState,
    open_output,
    reinit_portaudio,
)
from tests.fakes import FakeOutput


class StubSoundDevice:
    def __init__(self, fail: bool = False):
        self.calls: list[str] = []
        self.fail = fail

    def _terminate(self):
        self.calls.append("terminate")
        if self.fail:
            raise RuntimeError("PortAudio no responde")

    def _initialize(self):
        self.calls.append("initialize")


def test_reinit_portaudio_terminates_then_initializes():
    sd = StubSoundDevice()
    assert reinit_portaudio(sd) is True
    assert sd.calls == ["terminate", "initialize"]


def test_reinit_portaudio_is_guarded():
    sd = StubSoundDevice(fail=True)
    assert reinit_portaudio(sd) is False  # no lanza


class Factory:
    """Falla las primeras `failures` veces y luego da una `FakeOutput`."""

    def __init__(self, failures: int):
        self.failures = failures
        self.calls = 0
        self.outputs: list[FakeOutput] = []

    def __call__(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise OSError("sin dispositivo de salida")
        out = FakeOutput()
        self.outputs.append(out)
        return out


def wait_until(cond, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "no ocurrió a tiempo"
        time.sleep(0.005)


def reopen(lazy: LazyOutput, timeout: float = 3.0) -> list[Exception]:
    """Reabre (en su hilo) y espera a que termine; devuelve los fallos avisados."""
    errors: list[Exception] = []
    lazy.start(lambda block: None, on_error=errors.append)  # vuelve en el acto
    wait_until(lambda: errors or not lazy.probe_busy(), timeout)
    return errors


def test_failed_lazy_output_reinitializes_portaudio_before_each_retry():
    events: list[str] = []
    factory = Factory(failures=1)
    lazy = LazyOutput(factory, failed=True, reset=lambda: events.append("reset"))

    errors = reopen(lazy)  # sigue sin dispositivo: se avisa por on_error, no se lanza
    assert len(errors) == 1 and isinstance(errors[0], AudioOutputError)
    assert isinstance(errors[0].__cause__, OSError)
    assert events == ["reset"]

    assert reopen(lazy) == []  # se enchufó: el reintento lo encuentra
    assert events == ["reset", "reset"]
    assert factory.outputs[0].running


def test_working_output_is_reused_without_reinitializing():
    events: list[str] = []
    out = FakeOutput()
    lazy = LazyOutput(lambda: pytest.fail("no debe crear otra"), output=out,
                      reset=lambda: events.append("reset"))
    lazy.start(lambda block: None)
    lazy.stop()
    lazy.start(lambda block: None)
    assert events == []
    assert out.starts == 2


def test_runtime_failure_marks_output_for_reinitialization():
    events: list[str] = []
    factory = Factory(failures=0)
    first = FakeOutput()
    lazy = LazyOutput(factory, output=first, reset=lambda: events.append("reset"))
    errors = []
    lazy.start(lambda block: None, on_error=errors.append)
    first.fail(AudioOutputError())  # el stream se cae (p. ej. se desenchufa)
    assert len(errors) == 1

    assert reopen(lazy) == []
    assert events == ["reset"]
    assert factory.outputs and factory.outputs[0].running


def test_start_failure_of_existing_output_is_retried_with_a_fresh_one():
    events: list[str] = []
    broken = FakeOutput(start_error=AudioOutputError())
    factory = Factory(failures=0)
    lazy = LazyOutput(factory, output=broken, reset=lambda: events.append("reset"))
    with pytest.raises(AudioOutputError):
        lazy.start(lambda block: None)
    assert reopen(lazy) == []
    assert events == ["reset"]
    assert factory.outputs[0].running


async def test_open_output_gives_up_on_a_hanging_probe():
    release = threading.Event()

    def hangs():
        release.wait(3)
        return FakeOutput()

    started = time.monotonic()
    try:
        output, failed = await open_output(hangs, timeout_s=0.2)
    finally:
        release.set()
    assert failed is True
    assert time.monotonic() - started < 2
    assert isinstance(output, LazyOutput)


async def test_open_output_reports_a_failing_probe():
    output, failed = await open_output(Factory(failures=5), timeout_s=2)
    assert failed is True and isinstance(output, LazyOutput)


async def test_open_output_wraps_a_working_output():
    factory = Factory(failures=0)
    output, failed = await open_output(factory, timeout_s=2)
    assert failed is False
    output.start(lambda block: None)
    assert factory.calls == 1 and factory.outputs[0].running


async def test_hanging_probe_runs_in_a_daemon_thread():
    release = threading.Event()

    def hangs():
        release.wait(3)
        return FakeOutput()

    try:
        await open_output(hangs, timeout_s=0.1)
        probes = [t for t in threading.enumerate() if t.name == PROBE_THREAD_NAME]
        assert probes and all(t.daemon for t in probes)  # no retiene el proceso al salir
    finally:
        release.set()


async def test_start_during_a_hung_probe_fails_fast_without_reset_or_factory():
    release = threading.Event()
    events: list[str] = []
    calls = []

    def hangs():
        calls.append(1)
        release.wait(3)
        return FakeOutput()

    try:
        output, failed = await open_output(hangs, timeout_s=0.1,
                                           reset=lambda: events.append("reset"))
        assert failed
        started = time.monotonic()
        with pytest.raises(AudioOutputError):
            output.start(lambda block: None)
        assert time.monotonic() - started < 0.5
        assert events == [] and calls == [1]  # ni Pa_Terminate ni otra llamada colgada
    finally:
        release.set()
    # Al terminar el sondeo, el siguiente intento ya reinicia y crea la salida.
    deadline = time.monotonic() + 3
    while output.probe_busy() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert await asyncio.to_thread(reopen, output) == []
    assert events == ["reset"] and calls == [1, 1]


def test_lazy_output_sharing_a_busy_probe_fails_fast():
    probe = ProbeState()
    calls = []
    with probe.running():
        lazy = LazyOutput(lambda: calls.append(1) or FakeOutput(), probe=probe)
        with pytest.raises(AudioOutputError):
            lazy.start(lambda block: None)
    assert calls == []
    assert reopen(lazy) == []
    assert calls == [1]


def test_working_output_still_starts_while_a_probe_is_busy():
    probe = ProbeState()
    out = FakeOutput()
    lazy = LazyOutput(lambda: pytest.fail("no debe crear otra"), output=out, probe=probe)
    with probe.running():
        lazy.start(lambda block: None)
    assert out.running


# --- reapertura sin bloquear el bucle del trabajador --------------------------------------


class Recorder:
    def __init__(self):
        self.errors: list[Exception] = []

    def on_sentence_started(self, chunk, sentence_index):
        pass

    def on_chunk_finished(self, chunk):
        pass

    def on_output_error(self, err):
        self.errors.append(err)


def make_clip() -> QueuedClip:
    chunk = Chunk("d:0:0:0-0", "d", 0, 0, (), "texto", 0, 0)
    return QueuedClip(chunk, np.zeros(2400, dtype=np.int16), [(0, 0)], "edge")


async def ticks_during(seconds: float) -> int:
    """Vueltas del bucle en `seconds`: si algo lo bloquea, apenas hay."""
    count = 0
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        await asyncio.sleep(0.01)
        count += 1
    return count


@pytest.mark.parametrize("hang_in", ["factory", "reset"])
async def test_reopen_that_never_returns_does_not_block_the_worker_loop(hang_in):
    release = threading.Event()
    late: list[FakeOutput] = []

    def hangs(*_):
        release.wait(5)

    def factory():
        if hang_in == "factory":
            hangs()
        out = FakeOutput()
        late.append(out)
        return out

    lazy = LazyOutput(factory, failed=True, reset=hangs if hang_in == "reset" else None,
                      reopen_timeout_s=0.2)
    rec = Recorder()
    player = AudioPlayer(lazy, asyncio.get_running_loop(), rec)
    try:
        started = time.monotonic()
        await asyncio.wait_for(player.enqueue(make_clip()), 1)  # arranca la salida
        assert time.monotonic() - started < 0.1  # no esperó a PortAudio

        assert await ticks_during(0.5) > 20  # el bucle sigue respondiendo
        # Vencido el tope, cuenta como fallo de la salida: el controlador reintenta.
        assert len(rec.errors) == 1 and isinstance(rec.errors[0], AudioOutputError)

        # Mientras siga colgado, otro intento falla en el acto (sin tocar PortAudio).
        player.resume()
        await ticks_during(0.05)
        assert len(rec.errors) == 2
    finally:
        release.set()
    await asyncio.to_thread(wait_until, lambda: not lazy.probe_busy())
    # La salida que llegó tarde se descarta: nunca suena junto a otra.
    assert all(not out.running for out in late)

    player.resume()  # ya no está colgado: la reapertura funciona
    await asyncio.to_thread(wait_until, lambda: any(out.running for out in late))
    assert len(rec.errors) == 2
    player.close()


async def test_reopen_failure_is_reported_through_on_error_without_raising():
    lazy = LazyOutput(Factory(failures=10), failed=True)
    rec = Recorder()
    player = AudioPlayer(lazy, asyncio.get_running_loop(), rec)
    await player.enqueue(make_clip())
    await asyncio.to_thread(wait_until, lambda: not lazy.probe_busy())
    await ticks_during(0.05)
    assert len(rec.errors) == 1 and isinstance(rec.errors[0].__cause__, OSError)
    player.close()


def test_stop_of_a_hanging_stream_is_bounded_and_the_next_start_reopens():
    release = threading.Event()
    events: list[str] = []

    class HangingStop(FakeOutput):
        def stop(self):
            release.wait(5)
            super().stop()

    hung = HangingStop()
    factory = Factory(failures=0)
    lazy = LazyOutput(factory, output=hung, reset=lambda: events.append("reset"),
                      stop_timeout_s=0.1)
    lazy.start(lambda block: None)
    try:
        started = time.monotonic()
        lazy.stop()
        assert time.monotonic() - started < 1
        with pytest.raises(AudioOutputError):  # PortAudio sigue colgado: falla en el acto
            lazy.start(lambda block: None)
        assert events == []
    finally:
        release.set()
    wait_until(lambda: not lazy.probe_busy())
    assert reopen(lazy) == []
    assert events == ["reset"] and factory.outputs[0].running


def test_output_opened_after_the_timeout_is_discarded():
    release = threading.Event()
    factory = Factory(failures=0)

    def slow_factory():
        release.wait(5)
        return factory()

    lazy = LazyOutput(slow_factory, failed=True, reopen_timeout_s=0.1)
    errors: list[Exception] = []
    lazy.start(lambda block: None, on_error=errors.append)
    wait_until(lambda: bool(errors))
    assert isinstance(errors[0], AudioOutputError)  # vencido el tope: fallo de la salida
    release.set()
    wait_until(lambda: not lazy.probe_busy())
    assert factory.outputs and not factory.outputs[0].running  # llegó tarde: se paró
    assert len(errors) == 1

    assert reopen(lazy) == []  # el siguiente intento abre una salida nueva
    assert len(factory.outputs) == 2 and factory.outputs[1].running


def test_invalidated_output_is_reopened_from_scratch():
    """Un stream atascado sin avisar (vigilancia del controlador) se rehace de cero."""
    events: list[str] = []
    stuck = FakeOutput()
    factory = Factory(failures=0)
    lazy = LazyOutput(factory, output=stuck, reset=lambda: events.append("reset"))
    lazy.start(lambda block: None)
    lazy.invalidate()
    lazy.stop()
    assert reopen(lazy) == []
    assert events == ["reset"] and stuck.starts == 1 and factory.outputs[0].running


async def test_player_restart_after_a_stall_reopens_portaudio_from_scratch():
    events: list[str] = []
    stuck = FakeOutput()
    factory = Factory(failures=0)
    lazy = LazyOutput(factory, output=stuck, reset=lambda: events.append("reset"))
    rec = Recorder()
    player = AudioPlayer(lazy, asyncio.get_running_loop(), rec)
    await player.enqueue(make_clip())
    assert stuck.running

    player.restart_output()  # la vigilancia del controlador vio la salida atascada
    assert len(rec.errors) == 1 and not stuck.running
    player.resume()
    await asyncio.to_thread(wait_until, lambda: bool(factory.outputs) and factory.outputs[0].running)
    assert events == ["reset"]  # se reinició PortAudio: el dispositivo nuevo es visible
    assert stuck.starts == 1
    player.close()
