from __future__ import annotations

from pathlib import Path

import aiohttp
import edge_tts.exceptions
import pytest

from lector.audio.pcm import OUTPUT_RATE
from lector.errors import EdgeUnavailable, EdgeVoiceNotFound, SynthesisFailed
from lector.models import Chunk, Sentence
from lector.tts.base import (
    EDGE_SPEED_RANGE,
    PIPER_SPEED_RANGE,
    clamp_speed,
    edge_rate,
)
from lector.tts.edge_engine import EdgeEngine, align_marks
from tests.fakes import FakeCommunicate

# Dos pitidos seguidos (~3,7 s) para que las marcas de 1 s y 2 s caigan dentro.
AUDIO = (Path(__file__).parent / "fixtures" / "audio" / "beep.mp3").read_bytes() * 2
VOICE = "es-MX-DaliaNeural"
TICKS = 10_000_000


def sentences(*texts: str) -> tuple[Sentence, ...]:
    out, pos = [], 0
    for i, t in enumerate(texts):
        out.append(Sentence(i + 10, 0, t, pos, pos + len(t), None))
        pos += len(t) + 1
    return tuple(out)


def chunk_of(*texts: str) -> Chunk:
    s = sentences(*texts)
    return Chunk("d:0:0:10-12", "d", 0, 0, s, " ".join(texts), s[0].start, s[-1].end)


def audio_item(data: bytes = AUDIO) -> dict:
    return {"type": "audio", "data": data}


def boundary(seconds: float, text: str) -> dict:
    return {"type": "SentenceBoundary", "offset": int(seconds * TICKS), "duration": TICKS, "text": text}


TEXTS = ("Hola mundo.", "¿Cómo estás?", "Muy bien, gracias.")


def test_rate_string():
    assert edge_rate(1.0) == "+0%"
    assert edge_rate(1.5) == "+50%"
    assert edge_rate(0.75) == "-25%"


def test_clamp_speed():
    assert clamp_speed("edge", 1.0) == (1.0, False)
    assert clamp_speed("edge", 3.0) == (EDGE_SPEED_RANGE[1], True)
    assert clamp_speed("piper", 0.1) == (PIPER_SPEED_RANGE[0], True)


async def test_default_voice_and_no_pitch_change():
    fake = FakeCommunicate([audio_item(), boundary(0, TEXTS[0])])
    await EdgeEngine(fake.factory).synthesize(chunk_of(TEXTS[0]), VOICE, 1.5)
    (args, _) = fake.calls[-1]
    assert args == (TEXTS[0], VOICE)
    assert "pitch" not in fake.kwargs
    assert fake.kwargs["rate"] == "+50%"
    assert fake.kwargs["boundary"] == "SentenceBoundary"
    assert fake.kwargs["connect_timeout"] == 10
    assert fake.kwargs["receive_timeout"] == 15


async def test_boundaries_become_marks():
    fake = FakeCommunicate(
        [audio_item(), boundary(0, TEXTS[0]), boundary(1, TEXTS[1]), boundary(2, TEXTS[2])]
    )
    clip = await EdgeEngine(fake.factory).synthesize(chunk_of(*TEXTS), VOICE, 1.0)
    assert clip.marks == [(0, 10), (OUTPUT_RATE, 11), (2 * OUTPUT_RATE, 12)]
    assert clip.pcm.dtype.name == "int16"


async def test_marks_beyond_pcm_are_dropped():
    fake = FakeCommunicate([audio_item(), boundary(0, TEXTS[0]), boundary(60, TEXTS[1])])
    clip = await EdgeEngine(fake.factory).synthesize(chunk_of(*TEXTS[:2]), VOICE, 1.0)
    assert clip.marks == [(0, 10)]


async def test_marks_align_when_edge_merges_sentences():
    fake = FakeCommunicate(
        [audio_item(), boundary(0, TEXTS[0]), boundary(1, TEXTS[1] + " " + TEXTS[2])]
    )
    clip = await EdgeEngine(fake.factory).synthesize(chunk_of(*TEXTS), VOICE, 1.0)
    assert clip.marks == [(0, 10), (OUTPUT_RATE, 11)]


def test_align_without_boundaries():
    assert align_marks(sentences(*TEXTS), []) == [(0, 10)]


def test_align_when_edge_splits_a_sentence():
    s = sentences("Uno dos tres cuatro.", "Cinco.")
    b = [(0, "Uno dos"), (100, "tres cuatro."), (200, "Cinco.")]
    assert align_marks(s, b) == [(0, 10), (200, 11)]


def test_align_ignores_punctuation_and_case():
    s = sentences("¡HOLA!", "Adiós…")
    assert align_marks(s, [(0, "hola"), (500, "adios")]) == [(0, 10), (500, 11)]


async def test_empty_audio_raises_unavailable():
    fake = FakeCommunicate([boundary(0, TEXTS[0])])
    with pytest.raises(EdgeUnavailable):
        await EdgeEngine(fake.factory).synthesize(chunk_of(TEXTS[0]), VOICE, 1.0)


@pytest.mark.parametrize(
    "exc",
    [
        aiohttp.ClientConnectionError("x"),
        TimeoutError(),
        OSError("red"),
        edge_tts.exceptions.WebSocketError("x"),
    ],
    ids=["aiohttp", "timeout", "oserror", "edge"],
)
async def test_network_error_raises_unavailable(exc):
    fake = FakeCommunicate(exc)
    with pytest.raises(EdgeUnavailable) as info:
        await EdgeEngine(fake.factory).synthesize(chunk_of(TEXTS[0]), VOICE, 1.0)
    assert info.value.__cause__ is exc


async def test_stream_timeout_raises_unavailable():
    import asyncio

    class Slow:
        async def stream(self):
            await asyncio.sleep(5)
            yield audio_item()

    engine = EdgeEngine(lambda *a, **k: Slow(), timeout_s=0.05)
    with pytest.raises(EdgeUnavailable):
        await engine.synthesize(chunk_of(TEXTS[0]), VOICE, 1.0)


async def test_unknown_voice_raises_before_network():
    fake = FakeCommunicate([audio_item()])
    engine = EdgeEngine(fake.factory)
    engine.set_known_voices([{"ShortName": VOICE}])
    with pytest.raises(EdgeVoiceNotFound):
        await engine.synthesize(chunk_of(TEXTS[0]), "xx-XX-NopeNeural", 1.0)
    assert fake.calls == []


async def test_unknown_list_does_not_block():
    fake = FakeCommunicate([audio_item(), boundary(0, TEXTS[0])])
    clip = await EdgeEngine(fake.factory).synthesize(chunk_of(TEXTS[0]), "cualquiera", 1.0)
    assert clip.marks == [(0, 10)]


async def test_list_voices_populates_cache():
    async def fake_list():
        return [{"ShortName": VOICE, "Locale": "es-MX"}]

    fake = FakeCommunicate([audio_item(), boundary(0, TEXTS[0])])
    engine = EdgeEngine(fake.factory, fake_list)
    assert await engine.list_voices() == [{"ShortName": VOICE, "Locale": "es-MX"}]
    with pytest.raises(EdgeVoiceNotFound):
        await engine.synthesize(chunk_of(TEXTS[0]), "otra", 1.0)


async def test_list_voices_network_error():
    async def boom():
        raise aiohttp.ClientConnectionError("x")

    with pytest.raises(EdgeUnavailable):
        await EdgeEngine(FakeCommunicate([]).factory, boom).list_voices()


async def test_health_check():
    ok = FakeCommunicate([audio_item()])
    assert await EdgeEngine(ok.factory).health_check(VOICE) is True
    assert ok.calls[-1][0][0] == "Hola."
    assert await EdgeEngine(FakeCommunicate(OSError()).factory).health_check(VOICE) is False
    assert await EdgeEngine(FakeCommunicate([]).factory).health_check(VOICE) is False


async def test_malformed_voice_value_error_maps_to_voice_not_found():
    exc = ValueError("Invalid voice 'x'")

    def factory(*a, **k):
        raise exc

    with pytest.raises(EdgeVoiceNotFound) as info:
        await EdgeEngine(factory).synthesize(chunk_of(TEXTS[0]), "x", 1.0)
    assert info.value.__cause__ is exc


async def test_unexpected_error_in_factory_maps_to_unavailable():
    exc = RuntimeError("boom")

    def factory(*a, **k):
        raise exc

    with pytest.raises(EdgeUnavailable) as info:
        await EdgeEngine(factory).synthesize(chunk_of(TEXTS[0]), VOICE, 1.0)
    assert info.value.__cause__ is exc


async def test_unexpected_error_in_stream_maps_to_unavailable():
    fake = FakeCommunicate(RuntimeError("boom"))
    with pytest.raises(EdgeUnavailable):
        await EdgeEngine(fake.factory).synthesize(chunk_of(TEXTS[0]), VOICE, 1.0)


async def test_bad_mp3_raises_synthesis_failed():
    fake = FakeCommunicate([audio_item(b"esto no es un mp3" * 10), boundary(0, TEXTS[0])])
    with pytest.raises(SynthesisFailed):
        await EdgeEngine(fake.factory).synthesize(chunk_of(TEXTS[0]), VOICE, 1.0)


async def test_list_voices_times_out():
    import asyncio

    async def slow():
        await asyncio.sleep(5)

    with pytest.raises(EdgeUnavailable):
        await EdgeEngine(FakeCommunicate([]).factory, slow, timeout_s=0.05).list_voices()
