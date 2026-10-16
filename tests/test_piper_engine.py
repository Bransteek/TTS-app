from __future__ import annotations

import asyncio

import httpx
import numpy as np
import pytest

from lector.audio.pcm import OUTPUT_RATE
from lector.audio.player import QueuedClip
from lector.errors import PageUnreachable, PiperModelMissing, SynthesisFailed
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
    await engine.synthesize(chunk_of("Hola."), f"{MODEL}#Z", 1.0)  # typo: no mata el respaldo
    assert fake.calls[-1][1].speaker_id is None


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


async def test_cancellation_during_synthesis_propagates(tmp_path):
    import threading

    started, release = threading.Event(), threading.Event()

    class Slow(FakePiperVoice):
        def synthesize(self, text, syn_config=None, include_alignments=False):
            started.set()
            release.wait(5)
            return super().synthesize(text, syn_config)

    engine, _, _ = make_engine(tmp_path, fake=Slow())
    task = asyncio.ensure_future(engine.synthesize(chunk_of("Hola."), MODEL, 1.0))
    while not started.is_set():
        await asyncio.sleep(0.01)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("speed,scale", [(10.0, 0.4), (0.1, 2.0)])
async def test_speed_clamped_at_limits(tmp_path, speed, scale):
    engine, fake, _ = make_engine(tmp_path)
    await engine.synthesize(chunk_of("Hola."), MODEL, speed)
    assert fake.calls[0][1].length_scale == pytest.approx(scale)


def make_transport(onnx=b"x" * 200_000, cfg=b'{"speaker_id_map": {"F": 0}}', fail_onnx_at=None, hits=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if hits is not None:
            hits.append(str(request.url))
        if request.url.path.endswith(".onnx.json"):
            return httpx.Response(200, content=cfg)
        if fail_onnx_at is not None:
            raise httpx.ReadError("corte", request=request)
        return httpx.Response(200, content=onnx)

    return httpx.MockTransport(handler)


async def test_download_success_with_progress(tmp_path):
    hits, progress = [], []
    mgr = PiperModelManager(tmp_path, transport=make_transport(hits=hits))
    await mgr.download(MODEL, progress.append)
    assert mgr.is_installed(MODEL) and mgr.speakers(MODEL) == ["F"]
    assert all("huggingface.co/rhasspy/piper-voices" in u and "es_ES/sharvard/medium" in u for u in hits)
    assert len(progress) > 2 and progress[-1] == 1.0
    assert progress == sorted(progress)
    assert not list(tmp_path.glob(".part-*"))


async def test_download_failure_leaves_nothing(tmp_path):
    mgr = PiperModelManager(tmp_path, transport=make_transport(fail_onnx_at=1))
    with pytest.raises(PageUnreachable):
        await mgr.download(MODEL)
    assert not mgr.is_installed(MODEL)
    assert list(tmp_path.iterdir()) == []


async def test_download_truncated_is_rejected(tmp_path):
    def handler(request):
        if request.url.path.endswith(".json"):
            return httpx.Response(200, content=b"{}")
        return httpx.Response(200, headers={"content-length": "100"}, content=b"")

    mgr = PiperModelManager(tmp_path, transport=httpx.MockTransport(handler))
    with pytest.raises(PageUnreachable):
        await mgr.download(MODEL)
    assert list(tmp_path.iterdir()) == []


async def test_download_bad_json_rejected(tmp_path):
    mgr = PiperModelManager(tmp_path, transport=make_transport(cfg=b"<html>"))
    with pytest.raises(PageUnreachable):
        await mgr.download(MODEL)
    assert list(tmp_path.iterdir()) == []


async def test_download_cancellation_cleans_up(tmp_path):
    started = asyncio.Event()

    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * 1000
            started.set()
            await asyncio.sleep(30)

    def handler(request):
        return httpx.Response(200, stream=Slow())

    mgr = PiperModelManager(tmp_path, transport=httpx.MockTransport(handler))
    task = asyncio.ensure_future(mgr.download(MODEL))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(tmp_path.iterdir()) == []


async def test_download_retry_after_failure_succeeds(tmp_path):
    bad = PiperModelManager(tmp_path, transport=make_transport(fail_onnx_at=1))
    with pytest.raises(PageUnreachable):
        await bad.download(MODEL)
    good = PiperModelManager(tmp_path, transport=make_transport())
    await good.download(MODEL)
    assert good.is_installed(MODEL)


async def test_download_rejects_bad_name(tmp_path):
    with pytest.raises(ValueError):
        await PiperModelManager(tmp_path).download("../evil", None)


def test_catalog_shape():
    names = [v["name"] for v in PiperModelManager.CATALOG]
    assert "es_ES-sharvard-medium" in names and len(names) >= 5
    assert all({"name", "label", "gender", "size_mb", "license_note"} <= v.keys()
               for v in PiperModelManager.CATALOG)


async def test_broken_piper_import_maps_to_missing(tmp_path, monkeypatch):
    import sys

    install(tmp_path)
    monkeypatch.setitem(sys.modules, "piper", None)  # import -> ImportError
    engine = PiperEngine(PiperModelManager(tmp_path))
    with pytest.raises(PiperModelMissing):
        await engine.synthesize(chunk_of("Hola."), MODEL, 1.0)
    assert await engine.health_check(MODEL) is False
