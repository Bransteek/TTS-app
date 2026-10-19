"""Máquina de estados de motores: reintentos, respaldo, recuperación y espera sin red."""
from __future__ import annotations

import asyncio
import logging
import threading

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


async def test_missing_edge_voice_is_notified_once(clock):
    """Error de configuración (§5.5): se pasa a Piper y se avisa una sola vez."""
    from lector.errors import EDGE_VOICE_MISSING
    from lector.tts.edge_engine import EdgeEngine

    edge = EdgeEngine()
    edge.set_known_voices([{"ShortName": "es-ES-ElviraNeural"}])  # Dalia no está
    piper = FakePiper()
    manager, _ = make_manager(edge, piper, clock)
    notices: list[tuple[str, str]] = []
    manager.set_on_notice(lambda level, message: notices.append((level, message)))

    for n in range(3):
        assert (await synth(manager, n))[1] == "piper"
    assert notices == [("warn", EDGE_VOICE_MISSING)]

    # Otra voz que tampoco existe: es otro problema y se avisa otra vez (una).
    for n in range(3, 5):
        assert (await synth(manager, n, voice="es-AR-ElenaNeural"))[1] == "piper"
    assert notices == [("warn", EDGE_VOICE_MISSING)] * 2


@pytest.mark.parametrize("preferred", ["edge", "piper"])
async def test_missing_edge_voice_without_piper_stops_instead_of_waiting(clock, preferred):
    """Error de configuración sin voz local: no se espera sin fin (§5.5) ni se salta nada.

    Con Piper preferido y sin modelo, Edge es el respaldo; si su voz falta, igual.
    """
    from lector.errors import EdgeVoiceUnavailable
    from lector.tts.edge_engine import EdgeEngine

    edge = EdgeEngine()
    edge.set_known_voices([{"ShortName": "es-ES-ElviraNeural"}])
    manager, _ = make_manager(edge, FakePiper(error=PiperModelMissing), clock,
                              EngineConfig(preferred=preferred))
    notices: list[tuple[str, str]] = []
    manager.set_on_notice(lambda level, message: notices.append((level, message)))

    for n in range(2):
        with pytest.raises(EdgeVoiceUnavailable):
            await synth(manager, n)
    assert clock.sleeps == []  # sin esperas: no es la red
    # «Se lee con la voz sin conexión» sería falso: el aviso es el de la pausa.
    assert notices == []


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


async def test_cancelled_probe_counts_as_failed_probe(clock):
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

    # La prueba cancelada cuenta como fallida: el intervalo pasa a 120 s y el
    # siguiente fragmento (el usuario saltó) no vuelve a pagar la prueba.
    edge.error = None
    probed_at = clock.now()
    assert (await synth(manager, 2))[1] == "piper"
    assert len(edge.probes) == 1

    clock.t = probed_at + 119
    assert (await synth(manager, 3))[1] == "piper"
    clock.t = probed_at + 120
    assert (await synth(manager, 4))[1] == "edge"
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


async def test_half_open_failure_doubles_interval_without_retry_ladder(clock):
    # La prueba sale bien pero la síntesis real con Edge falla siempre.
    edge = FakeEdge(error=EdgeUnavailable, healthy=True, clock=clock)
    piper = FakePiper()
    manager, states = make_manager(edge, piper, clock)
    await fall_back_to_piper(manager, edge)
    opened = clock.now()

    n = 1
    while len(edge.probes) < 6:
        clock.advance(1)
        assert (await synth(manager, n))[1] == "piper"
        n += 1

    times = [opened] + [at for _, at in edge.probes]
    gaps = [b - a for a, b in zip(times, times[1:])]
    # 60 s de intervalo inicial, limitado por el enfriamiento de 120 s; luego se duplica.
    assert gaps == [120, 120, 240, 480, 600, 600]
    assert clock.sleeps == [1, 2, 4]  # sin escalera de reintentos tras el semiabierto
    assert len(edge.calls) == 4 + 6  # un único intento real por cada prueba superada
    assert [s for s, _ in states].count(S.EDGE_RETRYING) == 1
    assert manager.state is S.PIPER_ACTIVE


async def test_interval_resets_only_after_real_edge_success(clock):
    config = EngineConfig(piper_cooldown_s=0)
    edge = FakeEdge(error=EdgeUnavailable, healthy=True, clock=clock)
    piper = FakePiper()
    manager, _ = make_manager(edge, piper, clock, config)
    await fall_back_to_piper(manager, edge)
    opened = clock.now()

    # Prueba correcta + fallo real: el intervalo pasa a 120 s.
    clock.t = opened + 60
    assert (await synth(manager, 1))[1] == "piper"
    reopened = clock.now()
    clock.t = reopened + 119
    assert (await synth(manager, 2))[1] == "piper"
    assert len(edge.probes) == 1

    # Prueba correcta + fragmento real por Edge: el circuito se cierra del todo.
    edge.error = None
    clock.t = reopened + 120
    assert (await synth(manager, 3))[1] == "edge"
    assert manager.state is S.EDGE_ACTIVE

    # Una caída nueva vuelve a tener escalera de reintentos e intervalo de 60 s.
    edge.error = EdgeUnavailable
    edge.healthy = False
    sleeps_before = len(clock.sleeps)
    assert (await synth(manager, 4))[1] == "piper"
    assert clock.sleeps[sleeps_before:] == [1, 2, 4]
    opened_again = clock.now()
    clock.t = opened_again + 59
    await synth(manager, 5)
    assert len(edge.probes) == 2
    clock.t = opened_again + 60
    await synth(manager, 6)
    assert len(edge.probes) == 3


async def test_reset_during_wait_discards_stale_transitions(clock):
    edge, piper = FakeEdge(error=EdgeUnavailable), FakePiper(error=PiperModelMissing)
    manager, states = make_manager(edge, piper, clock)

    task = asyncio.create_task(manager.synthesize(make_chunk(0), EDGE_VOICE, PIPER_VOICE, 1.0))
    async with asyncio.timeout(5):
        while len(clock.sleeps) < 5:
            await asyncio.sleep(0)
    assert manager.state is S.WAITING_NETWORK

    manager.reset()
    assert manager.state is S.EDGE_ACTIVE
    piper.error = None
    async with asyncio.timeout(5):
        _, engine = await task

    assert engine == "piper"  # la síntesis en curso aún devuelve su clip
    assert manager.state is S.EDGE_ACTIVE  # pero no aplica sus transiciones
    assert states[-1] == (S.EDGE_ACTIVE, EDGE)

    edge.error = None
    assert (await synth(manager, 1))[1] == "edge"
    assert edge.probes == []


async def test_preferred_piper_falls_back_to_edge_but_stays_on_piper(clock):
    edge, piper = FakeEdge(), FakePiper(errors=[PiperModelMissing])
    manager, states = make_manager(edge, piper, clock, EngineConfig(preferred="piper"))

    _, engine = await synth(manager, 0)
    assert engine == "edge"
    assert clock.sleeps == []  # respaldo inmediato, sin espera
    assert manager.state is S.PIPER_ACTIVE
    assert states == []

    # El siguiente fragmento vuelve a intentar Piper primero.
    assert (await synth(manager, 1))[1] == "piper"
    assert [c[0] for c in piper.calls] == ["c0", "c1"]


async def test_preferred_piper_wait_loop_edge_success_stays_on_piper(clock):
    edge = FakeEdge(errors=[EdgeUnavailable] * 2)
    piper = FakePiper(errors=[PiperModelMissing] * 2)
    manager, states = make_manager(edge, piper, clock, EngineConfig(preferred="piper"))

    _, engine = await synth(manager, 0)

    assert engine == "edge"
    assert clock.sleeps == [1, 2]
    assert manager.state is S.PIPER_ACTIVE
    assert states == [(S.WAITING_NETWORK, EDGE), (S.PIPER_ACTIVE, PIPER)]
    assert (await synth(manager, 1))[1] == "piper"


@pytest.mark.parametrize("kwargs", [
    {"wait_max_s": 0},
    {"wait_max_s": -1},
    {"backoff": ()},
    {"edge_retries": -1},
    {"recovery_initial_s": 0},
    {"recovery_initial_s": 100, "recovery_max_s": 50},
    {"piper_cooldown_s": -1},
    {"preferred": "otro"},
])
def test_config_validation(kwargs):
    with pytest.raises(ValueError):
        EngineConfig(**kwargs)


def test_config_accepts_edge_cases():
    EngineConfig(edge_retries=0, piper_cooldown_s=0, recovery_initial_s=600, recovery_max_s=600)


async def test_chunk_log_runs_off_event_loop_thread(clock):
    loop_thread = threading.get_ident()
    threads: list[int] = []

    class RecordingLog:
        def record(self, chunk_id, engine, outcome):
            threads.append(threading.get_ident())

    manager, _ = make_manager(FakeEdge(), FakePiper(), clock, chunk_log=RecordingLog())
    await synth(manager, 0)

    assert len(threads) == 1
    assert threads[0] != loop_thread


async def test_long_outage_logs_traceback_once_per_engine(clock, caplog):
    caplog.set_level(logging.DEBUG, logger="lector.tts.engine_manager")
    edge = FakeEdge(errors=[EdgeUnavailable] * 20)
    piper = FakePiper(error=PiperModelMissing)
    manager, _ = make_manager(edge, piper, clock)

    assert (await synth(manager, 0))[1] == "edge"

    with_trace = [r for r in caplog.records if r.exc_info]
    assert sorted(r.getMessage() for r in with_trace) == [
        "El motor edge falló", "El motor piper falló",
    ]
    repeats = [r for r in caplog.records if "sigue fallando" in r.getMessage()]
    assert len(repeats) == (20 - 1) + (17 - 1)  # el resto, una línea DEBUG por intento
    assert all(r.levelno == logging.DEBUG for r in repeats)


async def test_set_on_state_replaces_callback(clock):
    edge, piper = FakeEdge(error=EdgeUnavailable), FakePiper()
    manager, first = make_manager(edge, piper, clock)
    later: list[tuple[EngineState, str]] = []
    manager.set_on_state(lambda s, a: later.append((s, a)))

    await synth(manager, 0)

    assert first == []
    assert later == [(S.EDGE_RETRYING, EDGE), (S.PIPER_ACTIVE, PIPER)]


# --- Motor preferido elegido en la interfaz (Task 16) -----------------------------------


async def test_set_preferred_applies_to_later_chunks(clock):
    edge, piper = FakeEdge(), FakePiper()
    manager, states = make_manager(edge, piper, clock)
    _, engine = await synth(manager, 0)
    assert engine == "edge"

    manager.set_preferred("piper")

    assert manager.state is S.PIPER_ACTIVE
    assert states == [(S.PIPER_ACTIVE, PIPER)]
    _, engine = await synth(manager, 1)
    assert engine == "piper"
    assert [c[0] for c in edge.calls] == ["c0"]  # sin pruebas de Edge en modo Piper

    manager.set_preferred("edge")

    assert manager.state is S.EDGE_ACTIVE
    _, engine = await synth(manager, 2)
    assert engine == "edge"
    assert states[-1] == (S.EDGE_ACTIVE, EDGE)


async def test_set_preferred_same_value_keeps_circuit(clock):
    edge, piper = FakeEdge(), FakePiper()
    manager, states = make_manager(edge, piper, clock)
    await fall_back_to_piper(manager, edge)
    assert manager.state is S.PIPER_ACTIVE
    before = list(states)

    manager.set_preferred("edge")  # ya era la preferencia: Edge sigue en cuarentena

    assert manager.state is S.PIPER_ACTIVE
    assert states == before


def test_set_preferred_rejects_unknown_engine(clock):
    manager, _ = make_manager(FakeEdge(), FakePiper(), clock)
    with pytest.raises(ValueError):
        manager.set_preferred("google")
    assert manager.state is S.EDGE_ACTIVE
