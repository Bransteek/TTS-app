"""Máquina de estados de motores: reintentos, respaldo, recuperación y espera sin red."""
from __future__ import annotations

import asyncio

import pytest

from lector.errors import (
    EdgeUnavailable,
    EdgeVoiceNotFound,
    PiperModelMissing,
    SynthesisFailed,
)
from lector.models import Chunk, Sentence
from lector.persistence.database import open_database
from lector.persistence.repositories import ChunkLogRepo
from lector.tts.engine_manager import EngineConfig, EngineManager, EngineState
from tests.fakes import FakeClock, FakeEdge, FakePiper

EDGE_VOICE = "es-MX-DaliaNeural"
PIPER_VOICE = "es_ES-sharvard-medium#F"
EDGE = "Edge TTS"
PIPER = "Piper local"
S = EngineState


def make_chunk(n: int) -> Chunk:
    text = f"Frase número {n}."
    sentence = Sentence(index=n, paragraph_index=n, text=text, start=0, end=len(text), page=None)
    return Chunk(
        chunk_id=f"c{n}", doc_id="doc", chapter_index=0, paragraph_index=n,
        sentences=(sentence,), text=text, start=0, end=len(text),
    )


@pytest.fixture
def clock():
    return FakeClock()


def make_manager(edge, piper, clock, config=None, **kwargs):
    states: list[tuple[EngineState, str]] = []
    kwargs.setdefault("on_state", lambda s, a: states.append((s, a)))
    manager = EngineManager(
        edge, piper, config or EngineConfig(), clock=clock.now, sleep=clock.sleep, **kwargs
    )
    return manager, states


async def synth(manager, n: int, voice: str = EDGE_VOICE):
    # Guardia de tiempo real: si hubiera un bucle infinito, la prueba falla en vez de colgarse.
    async with asyncio.timeout(5):
        return await manager.synthesize(make_chunk(n), voice, PIPER_VOICE, 1.0)


async def fall_back_to_piper(manager, edge, n: int = 0) -> None:
    """Edge cae del todo para el fragmento `n` y el gestor pasa a Piper."""
    edge.error = EdgeUnavailable
    _, engine = await synth(manager, n)
    assert engine == "piper"


async def test_edge_is_initial_engine(clock):
    edge, piper = FakeEdge(), FakePiper()
    manager, states = make_manager(edge, piper, clock)
    assert manager.state is S.EDGE_ACTIVE

    clip, engine = await synth(manager, 0)

    assert engine == "edge"
    assert clip.marks == [(0, 0)]
    assert [c[0] for c in edge.calls] == ["c0"]
    assert piper.calls == []
    assert manager.state is S.EDGE_ACTIVE
    assert states == []  # sin cambio de estado no hay aviso


async def test_edge_failure_switches_to_piper_after_retries(clock):
    edge, piper = FakeEdge(error=EdgeUnavailable), FakePiper()
    manager, states = make_manager(edge, piper, clock)

    _, engine = await synth(manager, 0)

    assert len(edge.calls) == 4  # 1 intento + 3 reintentos
    assert clock.sleeps == [1, 2, 4]
    assert engine == "piper"
    assert manager.state is S.PIPER_ACTIVE
    assert states == [(S.EDGE_RETRYING, EDGE), (S.PIPER_ACTIVE, PIPER)]


async def test_piper_continues_from_pending_chunk(clock):
    edge, piper = FakeEdge(), FakePiper()
    manager, _ = make_manager(edge, piper, clock)
    assert (await synth(manager, 0))[1] == "edge"

    await fall_back_to_piper(manager, edge, n=1)

    assert [c[0] for c in edge.calls] == ["c0", "c1", "c1", "c1", "c1"]
    assert [c[0] for c in piper.calls] == ["c1"]
    assert piper.calls[0][1] == PIPER_VOICE


async def test_no_flapping_within_cooldown(clock):
    edge, piper = FakeEdge(clock=clock), FakePiper()
    manager, states = make_manager(edge, piper, clock)
    await fall_back_to_piper(manager, edge)
    opened = clock.now()

    clock.advance(30)
    edge.error = None  # Edge vuelve a los 30 s
    clock.advance(1)
    assert (await synth(manager, 1))[1] == "piper"

    # Pasado el primer intervalo (60 s) pero dentro del enfriamiento (120 s): sigue Piper.
    clock.t = opened + 119
    assert (await synth(manager, 2))[1] == "piper"

    assert edge.probes == []
    assert len(edge.calls) == 4
    assert manager.state is S.PIPER_ACTIVE
    assert states == [(S.EDGE_RETRYING, EDGE), (S.PIPER_ACTIVE, PIPER)]


async def test_edge_recovers_after_probe(clock):
    edge, piper = FakeEdge(clock=clock), FakePiper()
    manager, states = make_manager(edge, piper, clock)
    await fall_back_to_piper(manager, edge)
    opened = clock.now()
    edge.error = None

    clock.t = opened + 121
    _, engine = await synth(manager, 1)

    assert engine == "edge"
    assert manager.state is S.EDGE_ACTIVE
    assert edge.probes == [(EDGE_VOICE, opened + 121)]
    assert edge.calls[-1][0] == "c1"
    assert [c[0] for c in piper.calls] == ["c0"]
    assert states[-2:] == [(S.EDGE_RECOVERY_TEST, PIPER), (S.EDGE_ACTIVE, EDGE)]


async def test_probe_interval_doubles_and_caps(clock):
    # Sin enfriamiento, para que cada prueba ocurra justo al vencer el intervalo.
    config = EngineConfig(piper_cooldown_s=0)
    edge, piper = FakeEdge(clock=clock), FakePiper()
    manager, _ = make_manager(edge, piper, clock, config)
    await fall_back_to_piper(manager, edge)
    opened = clock.now()

    n = 1
    while len(edge.probes) < 6:
        clock.advance(1)
        assert (await synth(manager, n))[1] == "piper"
        n += 1

    times = [opened] + [at for _, at in edge.probes]
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert gaps == [60, 120, 240, 480, 600, 600]
    assert len(edge.calls) == 4  # las pruebas fallidas no sintetizan con Edge
    assert manager.state is S.PIPER_ACTIVE


async def test_both_fail_waits_then_resumes(clock):
    # 4 fallos en la fase de reintentos y 9 dentro de la espera; el 14.º intento funciona.
    edge = FakeEdge(errors=[EdgeUnavailable] * 13)
    piper = FakePiper(error=PiperModelMissing)
    manager, states = make_manager(edge, piper, clock)

    _, engine = await synth(manager, 0)

    assert engine == "edge"
    assert clock.sleeps == [1, 2, 4] + [1, 2, 4, 8, 16, 32, 60, 60, 60, 60]
    assert len(edge.calls) == 14
    assert {c[0] for c in edge.calls + piper.calls} == {"c0"}
    assert manager.state is S.EDGE_ACTIVE
    assert states == [
        (S.EDGE_RETRYING, EDGE),
        (S.PIPER_ACTIVE, PIPER),
        (S.WAITING_NETWORK, EDGE),
        (S.EDGE_ACTIVE, EDGE),
    ]

    # El circuito quedó cerrado: el siguiente fragmento va directo por Edge.
    assert (await synth(manager, 1))[1] == "edge"
    assert edge.probes == []


async def test_waiting_piper_success_keeps_circuit_open(clock):
    edge = FakeEdge(error=EdgeUnavailable, clock=clock)
    piper = FakePiper(errors=[SynthesisFailed])
    manager, states = make_manager(edge, piper, clock)

    _, engine = await synth(manager, 0)
    assert engine == "piper"
    assert clock.sleeps == [1, 2, 4, 1]
    assert manager.state is S.PIPER_ACTIVE
    assert states[-2:] == [(S.WAITING_NETWORK, EDGE), (S.PIPER_ACTIVE, PIPER)]

    # El circuito sigue abierto: la recuperación por sondeo sigue programada.
    edge.error = None
    clock.advance(121)
    assert (await synth(manager, 1))[1] == "edge"
    assert len(edge.probes) == 1


async def test_voice_not_found_no_retries(clock):
    edge, piper = FakeEdge(error=EdgeVoiceNotFound), FakePiper()
    manager, _ = make_manager(edge, piper, clock)

    _, engine = await synth(manager, 0)
    assert engine == "piper"
    assert len(edge.calls) == 1
    assert clock.sleeps == []
    assert manager.state is S.PIPER_ACTIVE

    # Con la misma voz no se vuelve a probar Edge nunca.
    clock.advance(10_000)
    assert (await synth(manager, 1))[1] == "piper"
    assert edge.probes == []
    assert len(edge.calls) == 1

    # Al cambiar de voz se reinicia el circuito y se prueba Edge en esa misma llamada.
    edge.error = None
    _, engine = await synth(manager, 2, voice="es-ES-ElviraNeural")
    assert engine == "edge"
    assert edge.calls[-1][:2] == ("c2", "es-ES-ElviraNeural")
    assert manager.state is S.EDGE_ACTIVE


async def test_chunk_log_records_engine_per_chunk(clock, tmp_path):
    conn = open_database(tmp_path / "lector.db")
    try:
        edge, piper = FakeEdge(), FakePiper()
        manager, _ = make_manager(edge, piper, clock, chunk_log=ChunkLogRepo(conn))

        await synth(manager, 0)
        edge.errors = [EdgeUnavailable] * 3 + [SynthesisFailed]
        await synth(manager, 1)

        rows = conn.execute(
            "SELECT chunk_id, engine, outcome FROM chunk_log ORDER BY rowid"
        ).fetchall()
    finally:
        conn.close()
    assert [tuple(r) for r in rows] == [
        ("c0", "edge", "ok"),
        ("c1", "edge", "error:EdgeUnavailable"),
        ("c1", "edge", "error:EdgeUnavailable"),
        ("c1", "edge", "error:EdgeUnavailable"),
        ("c1", "edge", "error:SynthesisFailed"),
        ("c1", "piper", "ok"),
    ]


@pytest.mark.parametrize("error", [SynthesisFailed, RuntimeError, ValueError])
async def test_unexpected_edge_errors_are_retried_as_transient(clock, error):
    edge, piper = FakeEdge(error=error), FakePiper()
    manager, _ = make_manager(edge, piper, clock)

    _, engine = await synth(manager, 0)

    assert engine == "piper"
    assert len(edge.calls) == 4
    assert clock.sleeps == [1, 2, 4]


async def test_unexpected_piper_error_enters_waiting(clock):
    edge = FakeEdge(errors=[EdgeUnavailable] * 4)
    piper = FakePiper(error=RuntimeError)
    manager, states = make_manager(edge, piper, clock)

    _, engine = await synth(manager, 0)

    assert engine == "edge"
    assert (S.WAITING_NETWORK, EDGE) in states


async def test_preferred_piper_starts_on_piper_without_probes(clock):
    edge, piper = FakeEdge(), FakePiper()
    manager, states = make_manager(edge, piper, clock, EngineConfig(preferred="piper"))
    assert manager.state is S.PIPER_ACTIVE

    for n in range(3):
        assert (await synth(manager, n))[1] == "piper"
        clock.advance(1_000)

    assert edge.calls == [] and edge.probes == []
    assert states == []


async def test_reset_returns_to_initial_state(clock):
    edge, piper = FakeEdge(clock=clock), FakePiper()
    manager, states = make_manager(edge, piper, clock)
    await fall_back_to_piper(manager, edge)
    edge.error = None

    manager.reset()

    assert manager.state is S.EDGE_ACTIVE
    assert states[-1] == (S.EDGE_ACTIVE, EDGE)
    assert (await synth(manager, 1))[1] == "edge"
    assert edge.probes == []


async def test_cancel_during_wait_propagates_and_manager_recovers(clock):
    edge, piper = FakeEdge(error=EdgeUnavailable), FakePiper(error=PiperModelMissing)
    manager, _ = make_manager(edge, piper, clock)

    task = asyncio.create_task(manager.synthesize(make_chunk(0), EDGE_VOICE, PIPER_VOICE, 1.0))
    async with asyncio.timeout(5):
        while len(clock.sleeps) < 8:
            await asyncio.sleep(0)
    assert manager.state is S.WAITING_NETWORK
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # El lock quedó libre y el siguiente fragmento prueba Edge sin esperar antes.
    edge.error = None
    sleeps = len(clock.sleeps)
    assert (await synth(manager, 1))[1] == "edge"
    assert len(clock.sleeps) == sleeps
    assert manager.state is S.EDGE_ACTIVE


async def test_cancel_during_retry_restores_edge_active(clock):
    edge, piper = FakeEdge(error=EdgeUnavailable), FakePiper()
    manager, states = make_manager(edge, piper, clock)

    task = asyncio.create_task(manager.synthesize(make_chunk(0), EDGE_VOICE, PIPER_VOICE, 1.0))
    async with asyncio.timeout(5):
        while not clock.sleeps:
            await asyncio.sleep(0)
    assert manager.state is S.EDGE_RETRYING
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert manager.state is S.EDGE_ACTIVE
    assert states == [(S.EDGE_RETRYING, EDGE), (S.EDGE_ACTIVE, EDGE)]
    assert piper.calls == []


async def test_cancel_during_probe_restores_piper_active(clock):
    edge, piper = FakeEdge(clock=clock), FakePiper()
    manager, _ = make_manager(edge, piper, clock)
    await fall_back_to_piper(manager, edge)
    clock.advance(121)

    task = asyncio.create_task(manager.synthesize(make_chunk(1), EDGE_VOICE, PIPER_VOICE, 1.0))
    async with asyncio.timeout(5):
        while not edge.probes:
            await asyncio.sleep(0)
    assert manager.state is S.EDGE_RECOVERY_TEST
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert manager.state is S.PIPER_ACTIVE
    # La prueba no se consumió: el siguiente fragmento vuelve a sondear.
    edge.error = None
    assert (await synth(manager, 2))[1] == "edge"
    assert len(edge.probes) == 2


async def test_synthesize_calls_are_serialized(clock):
    edge, piper = FakeEdge(), FakePiper()
    manager, _ = make_manager(edge, piper, clock)

    async with asyncio.timeout(5):
        results = await asyncio.gather(*(
            manager.synthesize(make_chunk(n), EDGE_VOICE, PIPER_VOICE, 1.0) for n in range(4)
        ))

    assert [engine for _, engine in results] == ["edge"] * 4
    assert edge.max_in_flight == 1


async def test_callback_and_log_failures_do_not_stop_reading(clock):
    class BrokenLog:
        def record(self, *args):
            raise RuntimeError("disco lleno")

    def broken_on_state(state, active):
        raise RuntimeError("fallo de la interfaz")

    edge, piper = FakeEdge(error=EdgeUnavailable), FakePiper()
    manager, _ = make_manager(
        edge, piper, clock, on_state=broken_on_state, chunk_log=BrokenLog()
    )

    _, engine = await synth(manager, 0)

    assert engine == "piper"
    assert manager.state is S.PIPER_ACTIVE
