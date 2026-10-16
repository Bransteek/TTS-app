from __future__ import annotations

import asyncio

import numpy as np
import pytest

from lector.audio.pcm import OUTPUT_RATE
from lector.audio.player import QueuedClip
from lector.errors import PiperModelMissing, SynthesisFailed
from lector.models import Chunk, Sentence
from lector.tts.piper_engine import PiperEngine, PiperModelManager
from tests.fakes import FakePiperVoice

MODEL = "es_ES-sharvard-medium"


def install(directory, name=MODEL, speakers=None):
    import json

    (directory / f"{name}.onnx").write_bytes(b"")
    cfg = {"speaker_id_map": speakers} if speakers is not None else {}
    (directory / f"{name}.onnx.json").write_text(json.dumps(cfg))


def chunk_of(*texts: str) -> Chunk:
    out, pos = [], 0
    for i, t in enumerate(texts):
        out.append(Sentence(i + 10, 0, t, pos, pos + len(t), None))
        pos += len(t) + 1
    s = tuple(out)
    return Chunk("d:0:0:10-12", "d", 0, 0, s, " ".join(texts), s[0].start, s[-1].end)


def make_engine(tmp_path, fake=None, speakers=None):
    install(tmp_path, speakers=speakers)
    fake = fake or FakePiperVoice(speaker_id_map=speakers or {})
    loads: list = []

    def loader(path):
        loads.append(path)
        return fake

    return PiperEngine(PiperModelManager(tmp_path), loader=loader), fake, loads


def test_installed_detection(tmp_path):
    install(tmp_path, "es_ES-a-medium")
    (tmp_path / "es_ES-b-medium.onnx").write_bytes(b"")  # sin .json
    (tmp_path / "es_ES-c-medium.onnx.json").write_text("{}")  # sin .onnx
    mgr = PiperModelManager(tmp_path)
    assert mgr.installed() == ["es_ES-a-medium"]
    assert mgr.is_installed("es_ES-a-medium")
    assert not mgr.is_installed("es_ES-b-medium")


def test_speakers_read_from_config(tmp_path):
    install(tmp_path, speakers={"F": 0, "M": 1})
    assert PiperModelManager(tmp_path).speakers(MODEL) == ["F", "M"]
    assert PiperModelManager(tmp_path).speakers("no_existe") == []


async def test_missing_model_raises(tmp_path):
    engine = PiperEngine(PiperModelManager(tmp_path), loader=lambda p: FakePiperVoice())
    with pytest.raises(PiperModelMissing):
        await engine.synthesize(chunk_of("Hola."), MODEL, 1.0)
    assert await engine.health_check(MODEL) is False


async def test_speed_maps_to_length_scale(tmp_path):
    engine, fake, _ = make_engine(tmp_path)
    await engine.synthesize(chunk_of("Hola."), MODEL, 2.0)
    assert fake.calls[0][1].length_scale == 0.5


async def test_one_mark_per_sentence_and_resampled(tmp_path):
    engine, fake, _ = make_engine(tmp_path)
    clip = await engine.synthesize(chunk_of("a" * 22, "b" * 44), MODEL, 1.0)
    # 2200 muestras a 22050 Hz -> 2395 a 24 kHz; 4400 -> 4789; total 7184
    assert [t for t, _ in fake.calls] == ["a" * 22, "b" * 44]
    assert clip.marks == [(0, 10), (2395, 11)]
    assert len(clip.pcm) == 7184
    assert clip.pcm.dtype == np.int16
    QueuedClip(chunk_of("x"), clip.pcm, clip.marks, "piper")  # válido para el reproductor


async def test_empty_sentence_has_no_duplicate_mark(tmp_path):
    fake = FakePiperVoice(silent=("...",))
    engine, _, _ = make_engine(tmp_path, fake=fake)
    clip = await engine.synthesize(chunk_of("...", "Hola mundo"), MODEL, 1.0)
    assert clip.marks == [(0, 11)]
    clip = await engine.synthesize(chunk_of("Hola mundo", "...", "Adiós"), MODEL, 1.0)
    assert [i for _, i in clip.marks] == [10, 12]
    assert all(b[0] > a[0] for a, b in zip(clip.marks, clip.marks[1:]))


async def test_all_empty_fails(tmp_path):
    fake = FakePiperVoice(silent=("...",))
    engine, _, _ = make_engine(tmp_path, fake=fake)
    with pytest.raises(SynthesisFailed):
        await engine.synthesize(chunk_of("..."), MODEL, 1.0)


async def test_speaker_id_from_map(tmp_path):
    engine, fake, _ = make_engine(tmp_path, speakers={"F": 3, "M": 7})
    await engine.synthesize(chunk_of("Hola."), f"{MODEL}#M", 1.0)
    assert fake.calls[-1][1].speaker_id == 7
    await engine.synthesize(chunk_of("Hola."), f"{MODEL}#F", 1.0)
    assert fake.calls[-1][1].speaker_id == 3
    await engine.synthesize(chunk_of("Hola."), MODEL, 1.0)
    assert fake.calls[-1][1].speaker_id is None
    with pytest.raises(SynthesisFailed):
        await engine.synthesize(chunk_of("Hola."), f"{MODEL}#Z", 1.0)


async def test_model_loaded_once_and_reloaded_on_change(tmp_path):
    engine, _, loads = make_engine(tmp_path, speakers={"F": 0, "M": 1})
    install(tmp_path, "es_MX-ald-medium")
    await engine.synthesize(chunk_of("Hola."), f"{MODEL}#F", 1.0)
    await engine.synthesize(chunk_of("Hola."), f"{MODEL}#M", 1.0)
    assert len(loads) == 1
    await engine.synthesize(chunk_of("Hola."), "es_MX-ald-medium", 1.0)
    assert len(loads) == 2


async def test_loader_and_synth_errors_are_mapped(tmp_path):
    install(tmp_path)

    def bad_loader(path):
        raise RuntimeError("onnx roto")

    engine = PiperEngine(PiperModelManager(tmp_path), loader=bad_loader)
    with pytest.raises(SynthesisFailed):
        await engine.synthesize(chunk_of("Hola."), MODEL, 1.0)
    assert await engine.health_check(MODEL) is False


async def test_cancellation_propagates(tmp_path):
    engine, _, _ = make_engine(tmp_path)
    task = asyncio.ensure_future(engine.synthesize(chunk_of("Hola."), MODEL, 1.0))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_download_uses_download_voice(tmp_path, monkeypatch):
    calls, progress = [], []
    monkeypatch.setattr(
        "lector.tts.piper_engine.download_voice",
        lambda name, directory: calls.append((name, directory)),
    )
    await PiperModelManager(tmp_path).download(MODEL, progress.append)
    assert calls == [(MODEL, tmp_path)]
    assert progress[0] == 0.0 and progress[-1] == 1.0


async def test_download_rejects_bad_name(tmp_path):
    with pytest.raises(ValueError):
        await PiperModelManager(tmp_path).download("../evil", None)


def test_catalog_shape():
    names = [v["name"] for v in PiperModelManager.CATALOG]
    assert "es_ES-sharvard-medium" in names and len(names) >= 5
    assert all({"name", "label", "gender", "size_mb", "license_note"} <= v.keys()
               for v in PiperModelManager.CATALOG)
