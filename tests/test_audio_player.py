"""Reproductor en memoria: cola limitada, eventos de frase, pausa/limpieza y PCM."""
from __future__ import annotations

import asyncio
import gc
import threading
import weakref
from pathlib import Path

import numpy as np
import pytest

from lector.audio.pcm import OUTPUT_RATE, decode_mp3, resample
from lector.audio.player import AudioPlayer, QueuedClip
from lector.errors import AudioOutputError, SynthesisFailed
from lector.models import Chunk
from tests.fakes import FakeOutput

BEEP = Path(__file__).parent / "fixtures" / "audio" / "beep.mp3"


def make_chunk(n: int) -> Chunk:
    return Chunk(
        chunk_id=f"doc:0:0:{n}", doc_id="doc", chapter_index=0, paragraph_index=n,
        sentences=(), text=f"fragmento {n}", start=0, end=0,
    )


def make_clip(n: int, samples: int, marks: list[tuple[int, int]] | None = None, base: int = 0) -> QueuedClip:
    pcm = (np.arange(samples) % 20000 + base).astype(np.int16)
    return QueuedClip(make_chunk(n), pcm, marks or [(0, n)], "edge")


class Recorder:
    def __init__(self):
        self.events: list[tuple] = []
        self.threads: set[int] = set()

    def on_sentence_started(self, chunk, sentence_index):
        self.threads.add(threading.get_ident())
        self.events.append(("sentence", chunk.chunk_id, sentence_index))

    def on_chunk_finished(self, chunk):
        self.threads.add(threading.get_ident())
        self.events.append(("finished", chunk.chunk_id))

    def on_output_error(self, err):
        self.threads.add(threading.get_ident())
        self.events.append(("error", err))


async def settle() -> None:
    """Deja correr las llamadas pendientes en el bucle (eventos y notificaciones)."""
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.fixture
def rig():
    def build(**kwargs):
        out = FakeOutput()
        rec = Recorder()
        player = AudioPlayer(out, asyncio.get_running_loop(), rec, **kwargs)
        return player, out, rec

    return build


async def test_plays_clips_in_order_and_emits_events(rig):
    player, out, rec = rig()
    a = make_clip(0, 200, [(0, 0), (100, 1)], base=1)
    b = make_clip(1, 200, [(0, 2), (100, 3)], base=1000)
    expected = np.concatenate([a.pcm, b.pcm, np.zeros(624, np.int16)])
    await player.enqueue(a)
    await player.enqueue(b)

    # El callback corre en otro hilo, como el de PortAudio.
    block = await asyncio.to_thread(out.pull, 1024)
    await settle()

    np.testing.assert_array_equal(block, expected)
    assert [e[2] for e in rec.events if e[0] == "sentence"] == [0, 1, 2, 3]
    assert rec.events == [
        ("sentence", "doc:0:0:0", 0), ("sentence", "doc:0:0:0", 1), ("finished", "doc:0:0:0"),
        ("sentence", "doc:0:0:1", 2), ("sentence", "doc:0:0:1", 3), ("finished", "doc:0:0:1"),
    ]
    assert rec.threads == {threading.get_ident()}  # todos en el hilo del bucle
    assert player.current() is None
    assert player.queued_count() == 0


async def test_marks_emitted_when_crossed_across_blocks(rig):
    player, out, rec = rig()
    clip = make_clip(0, 1000, [(0, 5), (400, 6), (800, 7)])
    await player.enqueue(clip)
    out.pull(300)
    assert rec.events == []  # nunca se entregan dentro del callback de audio
    await settle()
    assert rec.events == [("sentence", "doc:0:0:0", 5)]
    assert player.current() == (clip.chunk, 5)
    out.pull(100)  # termina justo antes de la marca 400: aún no ha empezado
    await settle()
    assert player.current() == (clip.chunk, 5)
    out.pull(1)
    await settle()
    assert rec.events[-1] == ("sentence", "doc:0:0:0", 6)
    assert player.current() == (clip.chunk, 6)


@pytest.mark.parametrize("started", [False, True], ids=["queued", "playing"])
async def test_queue_is_bounded(rig, started):
    player, out, rec = rig(max_ahead=2)
    clips = [make_clip(i, 100) for i in range(4)]
    await asyncio.wait_for(player.enqueue(clips[0]), 0.05)
    if started:
        out.pull(50)  # el primero ya suena
    for clip in clips[1:3]:
        await asyncio.wait_for(player.enqueue(clip), 0.05)
    assert player.queued_count() == 3  # el que suena + 2 preparados

    fourth = asyncio.ensure_future(player.enqueue(clips[3]))
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(fourth), 0.05)
    if not started:
        out.pull(50)  # empieza a sonar el primero: sigue sin hueco
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(fourth), 0.05)

    out.pull(50)  # termina el primero
    await asyncio.wait_for(fourth, 1)
    assert player.queued_count() == 3


@pytest.mark.parametrize("started", [False, True], ids=["queued", "playing"])
async def test_seconds_limit(rig, started):
    player, out, rec = rig(max_seconds=90)
    sixty = 60 * OUTPUT_RATE
    first, second = make_clip(0, sixty), make_clip(1, sixty)
    await asyncio.wait_for(player.enqueue(first), 0.05)  # cola vacía: entra aunque sea largo
    if started:
        out.pull(OUTPUT_RATE)  # el primero ya suena: cuenta igual

    pending = asyncio.ensure_future(player.enqueue(second))
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(pending), 0.05)
    if not started:
        out.pull(OUTPUT_RATE)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(pending), 0.05)

    out.pull(sixty - OUTPUT_RATE)
    await asyncio.wait_for(pending, 1)
    assert player.queued_count() == 1


async def test_pause_outputs_silence_and_keeps_position(rig):
    player, out, rec = rig()
    clip = make_clip(0, 1000, [(0, 0), (500, 1)], base=1)
    await player.enqueue(clip)
    np.testing.assert_array_equal(out.pull(300), clip.pcm[:300])
    assert player.current() == (clip.chunk, 0)

    player.pause()
    np.testing.assert_array_equal(out.pull(400), np.zeros(400, np.int16))
    assert player.current() == (clip.chunk, 0)

    player.resume()
    np.testing.assert_array_equal(out.pull(300), clip.pcm[300:600])
    assert player.current() == (clip.chunk, 1)
    await settle()
    assert [e for e in rec.events if e[0] == "sentence"] == [
        ("sentence", "doc:0:0:0", 0), ("sentence", "doc:0:0:0", 1),
    ]


async def test_clear_returns_current_chunk(rig):
    player, out, rec = rig()
    a, b = make_clip(0, 1000, base=1), make_clip(1, 1000, base=1)
    await player.enqueue(a)
    await player.enqueue(b)
    out.pull(10)

    assert player.clear() == a.chunk
    assert player.current() is None
    assert player.queued_count() == 0
    np.testing.assert_array_equal(out.pull(100), np.zeros(100, np.int16))
    assert player.clear() is None  # nada sonaba


async def test_clear_drops_events_not_yet_delivered(rig):
    player, out, rec = rig()
    a = make_clip(0, 100, [(0, 0), (50, 1)])
    await player.enqueue(a)
    out.pull(200)  # el clip suena entero, pero sus eventos aún no se entregaron
    player.clear()
    await settle()
    assert rec.events == []


async def test_clear_drops_blocked_enqueue(rig):
    player, out, rec = rig(max_ahead=0)
    assert await player.enqueue(make_clip(0, 1000)) is True
    pending = asyncio.ensure_future(player.enqueue(make_clip(1, 1000)))
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(pending), 0.05)
    player.clear()
    assert await asyncio.wait_for(pending, 1) is False  # era de la posición descartada
    assert player.queued_count() == 0
    assert player.current() is None
    assert await asyncio.wait_for(player.enqueue(make_clip(2, 1000)), 0.05) is True
    assert player.current()[0].chunk_id == "doc:0:0:2"


async def test_cleared_clips_are_released(rig):
    player, out, rec = rig()
    clips = [make_clip(n, 1000) for n in range(3)]
    refs = [weakref.ref(c.pcm) for c in clips]
    for clip in clips:
        await player.enqueue(clip)
    del clip, clips
    out.pull(10)
    player.clear()
    gc.collect()
    assert [r for r in refs if r() is not None] == []


async def test_next_clip_becomes_current_without_gap(rig):
    player, out, rec = rig()
    a = make_clip(0, 100, [(0, 0)])
    b = make_clip(1, 100, [(0, 1), (50, 2)])
    await player.enqueue(a)
    assert player.current() == (a.chunk, 0)  # encolado = actual, antes de sonar
    await player.enqueue(b)
    out.pull(100)  # A termina justo al final del bloque
    await settle()
    assert player.current() == (b.chunk, 1)
    assert player.clear() == b.chunk
    assert ("sentence", "doc:0:0:1", 1) not in rec.events  # aún no se escribió ninguna muestra de B


async def test_first_mark_of_next_clip_emitted_when_written(rig):
    player, out, rec = rig()
    await player.enqueue(make_clip(0, 100, [(0, 0)]))
    await player.enqueue(make_clip(1, 100, [(0, 1)]))
    out.pull(100)
    await settle()
    assert rec.events == [("sentence", "doc:0:0:0", 0), ("finished", "doc:0:0:0")]
    out.pull(1)
    await settle()
    assert rec.events[-1] == ("sentence", "doc:0:0:1", 1)


async def test_played_clips_are_released(rig):
    player, out, rec = rig()
    refs = []
    for n in range(50):
        clip = make_clip(n, 300, [(0, n)])
        refs.append(weakref.ref(clip.pcm))
        await player.enqueue(clip)
        del clip
        out.pull(300)
    await settle()
    gc.collect()
    assert len([e for e in rec.events if e[0] == "finished"]) == 50
    assert [r for r in refs if r() is not None] == []


async def test_audio_thread_and_loop_run_concurrently_without_deadlock(rig):
    player, out, rec = rig(max_ahead=2)
    stop = threading.Event()

    def audio_thread():
        while not stop.is_set():
            out.pull(64)
            stop.wait(0.0005)

    await player.enqueue(make_clip(0, 500, [(0, 0), (250, 1)]))
    worker = threading.Thread(target=audio_thread, daemon=True)
    worker.start()
    try:
        for n in range(1, 40):
            await asyncio.wait_for(player.enqueue(make_clip(n, 500, [(0, 2 * n), (250, 2 * n + 1)])), 5)
            if n % 7 == 0:
                player.pause()
                player.current()
                player.resume()
        async def all_finished():
            while len([e for e in rec.events if e[0] == "finished"]) < 40:
                await asyncio.sleep(0.005)
        await asyncio.wait_for(all_finished(), 5)
    finally:
        stop.set()
        worker.join(2)
    assert [e[2] for e in rec.events if e[0] == "sentence"] == list(range(80))
    assert [e[1] for e in rec.events if e[0] == "finished"] == [f"doc:0:0:{n}" for n in range(40)]
    assert rec.threads == {threading.get_ident()}


async def test_output_failure_reported_on_loop_and_output_reopens(rig):
    player, out, rec = rig()
    await player.enqueue(make_clip(0, 1000))
    assert out.starts == 1

    boom = RuntimeError("device lost")
    await asyncio.to_thread(out.fail, boom)
    await settle()
    errors = [e[1] for e in rec.events if e[0] == "error"]
    assert len(errors) == 1 and isinstance(errors[0], AudioOutputError)
    assert errors[0].__cause__ is boom
    assert rec.threads == {threading.get_ident()}
    assert not out.running

    await player.enqueue(make_clip(1, 1000))  # enqueue no reintenta abrir la salida
    assert out.attempts == 1 and not out.running

    player.resume()  # solo el controlador reabre; la cola se conserva
    assert out.starts == 2 and out.running
    assert player.queued_count() == 2


async def test_enqueue_does_not_retry_failed_device(rig):
    out = FakeOutput(start_error=OSError("no device"))
    rec = Recorder()
    player = AudioPlayer(out, asyncio.get_running_loop(), rec)
    for n in range(3):
        await player.enqueue(make_clip(n, 100))
    await settle()
    assert out.attempts == 1
    assert len([e for e in rec.events if e[0] == "error"]) == 1
    player.resume()
    await settle()
    assert out.attempts == 2
    assert len([e for e in rec.events if e[0] == "error"]) == 2
    assert player.queued_count() == 3


async def test_stale_output_error_is_ignored(rig):
    player, out, rec = rig()
    await player.enqueue(make_clip(0, 1000))
    old_on_error = out.on_error
    out.fail(RuntimeError("device lost"))
    old_on_error(RuntimeError("same failure reported twice"))
    await settle()
    assert len([e for e in rec.events if e[0] == "error"]) == 1

    player.resume()
    assert out.running
    old_on_error(RuntimeError("late failure from the old stream"))
    await settle()
    assert out.running and out.stops == 1  # la salida nueva sigue intacta
    assert len([e for e in rec.events if e[0] == "error"]) == 1


async def test_output_start_failure_is_reported_not_raised(rig):
    out = FakeOutput(start_error=OSError("no device"))
    rec = Recorder()
    player = AudioPlayer(out, asyncio.get_running_loop(), rec)
    await player.enqueue(make_clip(0, 100))
    await settle()
    errors = [e[1] for e in rec.events if e[0] == "error"]
    assert len(errors) == 1 and isinstance(errors[0], AudioOutputError)


async def test_close_stops_output_and_drops_audio(rig):
    player, out, rec = rig()
    await player.enqueue(make_clip(0, 1000))
    out.pull(10)
    player.close()
    assert out.stops == 1 and not out.running
    assert player.current() is None
    assert player.queued_count() == 0


async def test_close_is_terminal(rig):
    player, out, rec = rig(max_ahead=0)
    await player.enqueue(make_clip(0, 1000))
    blocked = asyncio.ensure_future(player.enqueue(make_clip(1, 1000)))
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(blocked), 0.05)

    player.close()
    with pytest.raises(AudioOutputError) as woken:
        await asyncio.wait_for(blocked, 1)
    assert woken.value.recoverable is False
    with pytest.raises(AudioOutputError):
        await player.enqueue(make_clip(2, 1000))
    player.resume()
    out.fail(RuntimeError("late failure after close"))
    await settle()
    assert out.starts == 1 and not out.running
    assert player.queued_count() == 0
    assert [e for e in rec.events if e[0] == "error"] == []


def test_queued_clip_validates_marks():
    pcm = np.zeros(10, np.int16)
    with pytest.raises(ValueError):
        QueuedClip(make_chunk(0), pcm, [], "edge")
    with pytest.raises(ValueError):
        QueuedClip(make_chunk(0), pcm, [(5, 0)], "edge")
    with pytest.raises(ValueError):
        QueuedClip(make_chunk(0), pcm, [(0, 0), (8, 1), (4, 2)], "edge")
    with pytest.raises(ValueError):
        QueuedClip(make_chunk(0), pcm, [(0, 0), (4, 1), (4, 2)], "edge")  # no estrictamente creciente
    with pytest.raises(ValueError):
        QueuedClip(make_chunk(0), pcm, [(0, 0), (10, 1)], "edge")  # marca fuera del pcm
    with pytest.raises(ValueError):
        QueuedClip(make_chunk(0), np.zeros(0, np.int16), [(0, 0)], "edge")
    with pytest.raises(ValueError):
        QueuedClip(make_chunk(0), pcm.astype(np.float32), [(0, 0)], "edge")
    QueuedClip(make_chunk(0), pcm, [(0, 0), (9, 1)], "edge")  # válido


def test_decode_mp3_roundtrip():
    pcm = decode_mp3(BEEP.read_bytes())
    assert pcm.dtype == np.int16
    assert pcm.ndim == 1
    assert 0.8 * OUTPUT_RATE <= len(pcm) <= 3 * OUTPUT_RATE
    assert np.abs(pcm.astype(np.int32)).max() > 1000  # hay voz, no silencio


@pytest.mark.parametrize("data", [b"", b"esto no es un mp3" * 10], ids=["empty", "garbage"])
def test_decode_mp3_rejects_garbage(data):
    with pytest.raises(SynthesisFailed):
        decode_mp3(data)


def test_resample_length():
    pcm = (np.sin(np.arange(22050) / 10) * 10000).astype(np.int16)
    out = resample(pcm, 22050)
    assert out.dtype == np.int16
    assert abs(len(out) - 24000) <= 1


def test_resample_same_rate_and_empty():
    pcm = np.arange(100, dtype=np.int16)
    np.testing.assert_array_equal(resample(pcm, OUTPUT_RATE), pcm)
    assert len(resample(np.zeros(0, np.int16), 22050)) == 0


async def test_set_events_redirects_events():
    out = FakeOutput()
    player = AudioPlayer(out, asyncio.get_running_loop())  # sin destinatario todavía
    rec = Recorder()
    player.set_events(rec)
    await player.enqueue(make_clip(0, 100))
    out.pull(100)
    await settle()
    assert rec.events == [("sentence", "doc:0:0:0", 0), ("finished", "doc:0:0:0")]


async def test_drop_queued_keeps_current_clip_and_its_events(rig):
    player, out, rec = rig(max_seconds=90)
    forty = 40 * OUTPUT_RATE
    a = make_clip(0, forty, [(0, 0), (OUTPUT_RATE, 1)])
    b = make_clip(1, forty)
    await player.enqueue(a)
    await player.enqueue(b)
    out.pull(10)  # suena a
    c = make_clip(2, forty)
    blocked = asyncio.ensure_future(player.enqueue(c))  # 40 + 40 + 40 > 90 s: espera
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(blocked), 0.05)

    assert player.drop_queued() == [b.chunk]

    # Las muestras de b ya no cuentan: c cabe y entra (no lo descarta un clear()).
    assert await asyncio.wait_for(blocked, 1) is True
    assert player.queued_count() == 2
    assert player.current() == (a.chunk, 0)
    assert player.drop_queued() == [c.chunk]
    out.pull(forty)  # el clip que sonaba termina con todos sus eventos
    await settle()
    assert rec.events == [
        ("sentence", "doc:0:0:0", 0), ("sentence", "doc:0:0:0", 1), ("finished", "doc:0:0:0"),
    ]
    assert player.current() is None and player.drop_queued() == []
