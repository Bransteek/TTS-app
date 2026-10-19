"""Salida de audio al arrancar: sondeo con tope y reintento que reinicia PortAudio."""
from __future__ import annotations

import threading
import time

import pytest

from lector.errors import AudioOutputError
from lector.services.audio_output import LazyOutput, open_output, reinit_portaudio
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


def test_failed_lazy_output_reinitializes_portaudio_before_each_retry():
    events: list[str] = []
    factory = Factory(failures=1)
    lazy = LazyOutput(factory, failed=True, reset=lambda: events.append("reset"))

    with pytest.raises(AudioOutputError):
        lazy.start(lambda block: None)  # sigue sin dispositivo
    assert events == ["reset"]

    lazy.start(lambda block: None)  # se enchufó: el reintento lo encuentra
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

    lazy.start(lambda block: None)
    assert events == ["reset"]
    assert factory.outputs and factory.outputs[0].running


def test_start_failure_of_existing_output_is_retried_with_a_fresh_one():
    events: list[str] = []
    broken = FakeOutput(start_error=AudioOutputError())
    factory = Factory(failures=0)
    lazy = LazyOutput(factory, output=broken, reset=lambda: events.append("reset"))
    with pytest.raises(AudioOutputError):
        lazy.start(lambda block: None)
    lazy.start(lambda block: None)
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
