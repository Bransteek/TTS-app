"""Texto que no se puede pronunciar (separadores de escena como «***»).

Edge responde `NoAudioReceived` a un texto sin letras ni cifras. Eso es un problema del
fragmento, no una caída de Edge: nunca debe abrir el circuito, reintentar sin fin ni
detener la lectura. Las pruebas usan el `EdgeEngine` y el `EngineManager` reales y
sustituyen solo el WebSocket de edge-tts (`Communicate.__stream`): `stream()` es el real.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import edge_tts
import edge_tts.exceptions
import pytest

from lector.content.segmenter import build_chapter_text, make_chunks
from lector.errors import PiperModelMissing
from lector.models import Chapter, Chunk, Sentence
from lector.services.reading_controller import SessionState
from lector.tts.edge_engine import EdgeEngine
from lector.tts.engine_manager import EngineConfig, EngineManager, EngineState
from tests.controller_rig import WAIT_S, settle, story_document, story_ref, until
from tests.controller_rig import fake_opener
from tests.fakes import FakeClock, FakePiper, FakeSource

BEEP = (Path(__file__).parent / "fixtures" / "audio" / "beep.mp3").read_bytes()
TICKS = 10_000_000
EDGE_VOICE = "es-MX-DaliaNeural"
PIPER_VOICE = "es_ES-sharvard-medium#F"
SCENE_BREAKS = ["***", "…", "—", "◆◆◆", "＊＊＊", "~~~", "* * *"]


def _no_letters(text: str) -> bool:
    return not any(ch.isalnum() for ch in text)


@pytest.fixture
def edge_service(monkeypatch):
    """Servicio de Edge en memoria: responde como el real, sin red.

    Un texto que `mute(texto)` marca (por defecto, sin letras ni cifras) lanza el
    `NoAudioReceived` real de edge-tts, como hace Microsoft con «***».
    """

    class Service:
        calls: list[str] = []
        mute = staticmethod(_no_letters)

    service = Service()
    service.calls = []

    async def fake_stream(self):
        text = self.state["partial_text"].decode("utf-8")
        service.calls.append(text)
        await asyncio.sleep(0)
        if service.mute(text):
            raise edge_tts.exceptions.NoAudioReceived(
                "No audio was received. Please verify that your parameters are correct."
            )
        yield {"type": "SentenceBoundary", "offset": 0, "duration": TICKS, "text": text}
        yield {"type": "audio", "data": BEEP}

    monkeypatch.setattr(edge_tts.Communicate, "_Communicate__stream", fake_stream)
    return service


def chunk_of(text: str, index: int = 0) -> Chunk:
    sentence = Sentence(index, index, text, 0, len(text), None)
    return Chunk(f"doc:0:{index}:{index}-{index}", "doc", 0, index, (sentence,), text, 0, len(text))


# --- segmentación ------------------------------------------------------------------------


@pytest.mark.parametrize("separator", SCENE_BREAKS)
def test_chunks_skip_text_without_letters_or_digits(separator):
    _, _, sentences = build_chapter_text(
        [("Primera escena.", None), (separator, None), ("Segunda escena.", None)]
    )
    assert [s.index for s in sentences] == [0, 1, 2]  # los índices no cambian

    chunks = make_chunks("d", 0, sentences, max_chars=600, max_sentences=3)
    assert [[s.index for s in c.sentences] for c in chunks] == [[0], [2]]
    assert [c.text for c in chunks] == ["Primera escena.", "Segunda escena."]


def test_chunks_keep_text_with_any_letter_or_digit():
    _, _, sentences = build_chapter_text(
        [("— 3 —", None), ("…¿Eh?", None), ("Ñ", None), ("Ж", None)]
    )
    chunks = make_chunks("d", 0, sentences, max_chars=600, max_sentences=3)
    assert [s.index for c in chunks for s in c.sentences] == [0, 1, 2, 3]


def test_chunks_from_a_separator_only_tail_are_empty():
    _, _, sentences = build_chapter_text([("Fin de la escena.", None), ("***", None)])
    assert make_chunks("d", 0, sentences, 600, 3, first_sentence=1) == []


# --- EdgeEngine y EngineManager reales -----------------------------------------------------


async def test_edge_engine_reports_no_audio_for_a_scene_break(edge_service):
    from lector.errors import EdgeNoAudio

    with pytest.raises(EdgeNoAudio) as info:
        await EdgeEngine().synthesize(chunk_of("***"), EDGE_VOICE, 1.0)
    assert isinstance(info.value.__cause__, edge_tts.exceptions.NoAudioReceived)


def make_manager(piper):
    clock = FakeClock()
    states: list[EngineState] = []
    manager = EngineManager(
        EdgeEngine(), piper, EngineConfig(), clock=clock.now, sleep=clock.sleep,
        on_state=lambda s, a: states.append(s),
    )
    return manager, states, clock


async def test_no_audio_without_piper_skips_the_chunk_and_keeps_edge(edge_service):
    manager, states, clock = make_manager(FakePiper(error=PiperModelMissing))

    async with asyncio.timeout(5):  # nunca un reintento sin fin
        clip, engine = await manager.synthesize(chunk_of("***"), EDGE_VOICE, PIPER_VOICE, 1.0)
    assert len(clip.pcm) == 0  # se salta: el controlador no encola nada
    assert edge_service.calls == ["***"]  # sin reintentos
    assert clock.sleeps == []
    assert states == [] and manager.state is EngineState.EDGE_ACTIVE  # el circuito sigue cerrado

    clip, engine = await manager.synthesize(chunk_of("Sigue la historia.", 1),
                                            EDGE_VOICE, PIPER_VOICE, 1.0)
    assert engine == "edge" and len(clip.pcm) > 0


async def test_no_audio_with_piper_uses_piper_for_that_chunk_only(edge_service):
    piper = FakePiper()
    manager, states, clock = make_manager(piper)

    clip, engine = await manager.synthesize(chunk_of("◆◆◆"), EDGE_VOICE, PIPER_VOICE, 1.0)
    assert engine == "piper" and len(clip.pcm) > 0
    assert [c[0] for c in piper.calls] == ["doc:0:0:0-0"]
    assert states == [] and manager.state is EngineState.EDGE_ACTIVE

    clip, engine = await manager.synthesize(chunk_of("Vuelve Edge.", 1), EDGE_VOICE, PIPER_VOICE, 1.0)
    assert engine == "edge"
    assert len(piper.calls) == 1


async def test_no_audio_while_waiting_for_network_does_not_retry_forever(edge_service):
    manager, states, clock = make_manager(FakePiper(error=PiperModelMissing))
    real = edge_tts.Communicate._Communicate__stream

    async def offline(self):
        raise OSError("sin red")
        yield  # pragma: no cover

    edge_tts.Communicate._Communicate__stream = offline
    try:
        task = asyncio.create_task(
            manager.synthesize(chunk_of("Frase normal."), EDGE_VOICE, PIPER_VOICE, 1.0)
        )
        await until(lambda: manager.state is EngineState.WAITING_NETWORK)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        edge_tts.Communicate._Communicate__stream = real

    # Vuelve la red y el siguiente fragmento es un separador: se salta, no se espera más.
    async with asyncio.timeout(5):
        clip, _ = await manager.synthesize(chunk_of("~~~"), EDGE_VOICE, PIPER_VOICE, 1.0)
    assert len(clip.pcm) == 0
    clip, engine = await manager.synthesize(chunk_of("Ya hay red.", 1), EDGE_VOICE, PIPER_VOICE, 1.0)
    assert engine == "edge" and manager.state is EngineState.EDGE_ACTIVE


# --- lectura completa con el controlador ---------------------------------------------------


def _chapter(paragraphs: list[str]) -> Chapter:
    text, paras, sents = build_chapter_text([(p, None) for p in paragraphs])
    return Chapter(story_ref(1), "Cap 1", text, paras, sents, None, None)


async def _read_to_the_end(rig) -> None:
    async with asyncio.timeout(WAIT_S * 2):
        while rig.ctl.state is not SessionState.COMPLETED:
            if rig.player.queued_count():
                rig.out.pull(48_000)
            await asyncio.sleep(0.001)


@pytest.mark.parametrize("piper_installed", [False, True], ids=["sin_piper", "con_piper"])
async def test_reading_continues_past_a_scene_break(controller_rig, edge_service, piper_installed):
    chapter = _chapter(["Primera escena.", "***", "Segunda escena.", "＊＊＊", "Final."])
    source = FakeSource({chapter.ref.locator: chapter})
    piper = FakePiper() if piper_installed else FakePiper(error=PiperModelMissing)
    rig = controller_rig(opener=fake_opener(source, story_document({chapter.ref.locator: chapter})),
                         edge=EdgeEngine(), piper=piper)
    await asyncio.wait_for(rig.ctl.set_chunk_limits(600, 1, 300), WAIT_S)
    await asyncio.wait_for(rig.ctl.set_continuous(False), WAIT_S)
    await rig.start(chapter.ref.locator)

    await _read_to_the_end(rig)

    assert rig.events.heard == [0, 2, 4]
    assert "***" not in edge_service.calls and "＊＊＊" not in edge_service.calls
    assert SessionState.WAITING_NETWORK not in rig.events.states
    assert [s for s, _ in rig.events.engine] == []  # Edge nunca dejó de estar activo
    assert piper.calls == []
    record = await rig.saved()
    assert record.position.sentence_index == 4


async def test_reading_skips_a_sentence_edge_cannot_voice(controller_rig, edge_service):
    """Respaldo del respaldo: si Edge no da audio para una frase con letras, se salta."""
    edge_service.mute = lambda text: text == "Frase muda."
    chapter = _chapter(["Antes.", "Frase muda.", "Después."])
    source = FakeSource({chapter.ref.locator: chapter})
    rig = controller_rig(opener=fake_opener(source, story_document({chapter.ref.locator: chapter})),
                         edge=EdgeEngine(), piper=FakePiper(error=PiperModelMissing))
    await asyncio.wait_for(rig.ctl.set_chunk_limits(600, 1, 300), WAIT_S)
    await asyncio.wait_for(rig.ctl.set_continuous(False), WAIT_S)
    await rig.start(chapter.ref.locator)

    await _read_to_the_end(rig)
    await settle()

    assert rig.events.heard == [0, 2]
    assert edge_service.calls.count("Frase muda.") == 1  # un intento, sin reintentos
    assert SessionState.WAITING_NETWORK not in rig.events.states
    assert rig.manager.state is EngineState.EDGE_ACTIVE
