"""Controlador de lectura: flujo completo, controles, interrupciones y estados."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import httpx
import pytest

from lector.content.base import doc_id_for_url
from lector.errors import AudioOutputError, EdgeUnavailable, PageUnreachable, PiperModelMissing
from lector.paths import data_dir
from lector.security.http_client import SafeFetcher
from lector.services.reading_controller import SessionState, resume_sentence
from lector.tts.engine_manager import EngineState
from tests.controller_rig import (
    STORY_ID, WAIT_S, Outage, lector_tasks, settle, story_chapter, story_ref, story_rig, until,
)
from tests.fakes import SAMPLES_PER_SENTENCE
from tests.test_madara import WORK, handler, resolver

S = SessionState


def chunk_starts(engine) -> list[int]:
    """Primera frase de cada fragmento pedido al motor (de su chunk_id)."""
    return [int(call[0].rsplit(":", 1)[1].split("-")[0]) for call in engine.calls]


# --- §16: flujo completo -----------------------------------------------------------


async def test_valid_url_is_processed_and_read(controller_rig):
    fetcher = SafeFetcher(resolver=resolver, transport=httpx.MockTransport(handler))
    try:
        rig = controller_rig(fetcher=fetcher)
        doc, chapter, progress = await rig.ctl.open(WORK + "capitulo-99/")
        assert doc.doc_id == doc_id_for_url(WORK)
        assert progress is None
        assert rig.edge.calls == []  # la vista previa no lee

        await rig.ctl.play(doc.doc_id, None)
        await rig.hear(1)

        title, text, index, total = rig.events.sentences[0]
        assert (title, text, index) == (chapter.title, chapter.sentences[0].text, 0)
        assert total == len(chapter.sentences)
        assert rig.events.documents[0][1].ref.locator == chapter.ref.locator
        assert S.READING in rig.events.states
        await rig.close()
    finally:
        await fetcher.aclose()


async def test_pdf_is_extracted_and_read(controller_rig, make_pdf):
    path = make_pdf(pages=[[("Capítulo 1", 20), ("Primera frase del libro. Segunda frase aquí.", 11)]])
    rig = controller_rig()
    doc, chapter, _ = await rig.ctl.open(str(path))
    await rig.ctl.play(doc.doc_id, None)
    await rig.hear(2)
    assert [s[1] for s in rig.events.sentences[:2]] == [s.text for s in chapter.sentences[:2]]
    assert "Primera frase del libro." in [s[1] for s in rig.events.sentences]


async def test_stop_cancels_pending_jobs(controller_rig):
    rig = story_rig(controller_rig)
    await rig.start(story_ref(1).locator)
    await rig.fill()
    await rig.hear(2)

    await asyncio.wait_for(rig.ctl.stop(), WAIT_S)

    assert lector_tasks() == []
    calls = len(rig.edge.calls)
    await settle()
    assert len(rig.edge.calls) == calls
    assert rig.player.queued_count() == 0
    assert rig.ctl.state is S.STOPPED
    record = rig.repos.progress.get(STORY_ID)
    assert resume_sentence(record.position) == 1  # la frase que sonaba
    assert record.session_state == "stopped"


async def test_no_audio_files_written(controller_rig, tmp_path, monkeypatch):
    monkeypatch.setenv("LECTOR_DATA_DIR", str(tmp_path / "datos"))
    tmp = Path(tempfile.gettempdir())

    def audio_files() -> set[Path]:
        found = set(tmp.glob("*.mp3")) | set(tmp.glob("*.wav"))
        for root in (tmp_path, data_dir()):
            found |= set(root.rglob("*.mp3")) | set(root.rglob("*.wav"))
        return found

    before = audio_files()
    rig = story_rig(controller_rig, chapters=2, sentences=4)
    await rig.start(story_ref(1).locator)
    await rig.hear(8)
    await rig.ctl.stop()
    assert rig.events.heard_at[-1] == ("Cap 2", 3)
    assert audio_files() == before


async def test_fallback_mid_chunk_resumes_at_current_sentence(controller_rig):
    rig = story_rig(controller_rig, chapters=1, sentences=20)
    await rig.start(story_ref(1).locator)
    await rig.fill()  # [0] [1-3] [4-6] en cola y [7-9] generado
    rig.edge.error = EdgeUnavailable  # Edge cae con el fragmento [10-12]

    await rig.hear(20)

    assert rig.events.heard == list(range(20))  # ni repite ni salta
    assert chunk_starts(rig.piper)[:3] == [10, 13, 14]  # mismo fragmento, luego límites de Piper
    assert all(start <= 10 for start in chunk_starts(rig.edge))
    assert (EngineState.PIPER_ACTIVE, "Piper local") in rig.events.engine
    assert rig.player.queued_count() == 0


async def test_both_engines_fail_session_recoverable(controller_rig):
    rig = story_rig(controller_rig, chapters=1, sentences=20)
    await rig.start(story_ref(1).locator)
    await rig.fill()
    rig.edge.error = EdgeUnavailable
    rig.piper.error = PiperModelMissing

    await rig.hear(10)  # suena todo lo que ya estaba generado
    await until(lambda: rig.ctl.state is S.WAITING_NETWORK)
    record = await rig.saved()
    assert resume_sentence(record.position) == 10
    assert record.session_state == "waiting_network"

    rig.edge.error = None  # vuelve la red
    await until(lambda: rig.ctl.state is S.READING)
    await rig.hear(3)

    assert rig.events.heard == list(range(13))
    assert S.WAITING_NETWORK in rig.events.states


# --- foco de revisión --------------------------------------------------------------


async def test_waits_and_resumes_when_next_chapter_arrives_late(controller_rig):
    rig = story_rig(controller_rig, chapters=2, sentences=3, prefetch_ahead=0)
    rig.source.failures = Outage(PageUnreachable())  # el capítulo 2 no llega
    await rig.start(story_ref(1).locator)
    await rig.hear(3)

    await until(lambda: rig.ctl.state is S.WAITING_NETWORK)
    assert rig.player.queued_count() == 0
    rig.source.failures = []  # vuelve la red
    await until(lambda: rig.ctl.state is S.READING)
    await rig.hear(1)

    assert rig.events.heard_at == [("Cap 1", 0), ("Cap 1", 1), ("Cap 1", 2), ("Cap 2", 0)]
    assert rig.events.documents[-1][1].ref == story_ref(2)
    assert S.COMPLETED not in rig.events.states


async def test_pause_during_fallback_has_single_audio_stream(controller_rig):
    rig = story_rig(controller_rig, chapters=1, sentences=30)
    rig.edge.error = EdgeUnavailable
    rig.engine_gate = asyncio.Event()  # los reintentos de Edge esperan hasta abrirla
    await rig.start(story_ref(1).locator)
    peaks = []

    def check() -> None:
        peaks.append((rig.producers(), rig.player.queued_count()))

    await until(lambda: rig.manager.state is EngineState.EDGE_RETRYING)
    await rig.ctl.pause()
    check()
    await rig.ctl.next_sentence()  # navegar mientras se cambia de motor
    check()
    await rig.ctl.resume()
    await rig.ctl.pause()
    rig.engine_gate.set()  # termina el cambio a Piper con la lectura en pausa
    await until(lambda: rig.player.queued_count() == 3)
    await settle()
    check()
    await rig.ctl.resume()
    await rig.hear(4)
    check()

    assert all(producers <= 1 and queued <= 3 for producers, queued in peaks)
    assert rig.edge.max_in_flight <= 1 and rig.piper.max_in_flight <= 1
    heard = rig.events.heard
    assert heard == list(range(heard[0], heard[0] + len(heard)))  # un solo flujo, en orden


# --- controles ---------------------------------------------------------------------


async def test_next_prev_sentence(controller_rig):
    rig = story_rig(controller_rig)
    await rig.start(story_ref(1).locator)
    await rig.hear(3, partial=True)  # la 2 suena a medias

    await rig.ctl.next_sentence()
    await rig.hear(1, partial=True)
    await rig.ctl.prev_sentence()
    await rig.hear(1)

    assert rig.events.heard == [0, 1, 2, 3, 2]
    ids = [c[0] for c in rig.edge.calls]
    assert ids.index("novela:1:0:3-3") < ids.index("novela:1:0:2-2")  # cada salto empieza ahí


async def test_skip_seconds_moves_by_estimated_duration(controller_rig):
    # Frases de 70 caracteres: 70 / 14 = 5 s cada una a velocidad 1.
    texts = []
    for k in range(10):
        head = f"Frase {k:02d} "
        texts.append(head + "a" * (69 - len(head)) + ".")
    assert {len(t) for t in texts} == {70}
    rig = story_rig(controller_rig, chapters=1)
    rig.source.chapters[story_ref(1).locator] = story_chapter(1, texts=texts, last=1)
    await rig.start(story_ref(1).locator)
    await rig.hear(3, partial=True)  # suena la 2

    await rig.ctl.skip_seconds(10)  # 2 frases de 5 s
    await rig.hear(1, partial=True)
    await rig.ctl.skip_seconds(-10)
    await rig.hear(1, partial=True)
    await rig.ctl.skip_seconds(7)  # cubrir 7 s exige 2 frases
    await rig.hear(1)

    assert rig.events.heard == [0, 1, 2, 4, 2, 4]


async def test_restart_chapter(controller_rig):
    rig = story_rig(controller_rig)
    await rig.start(story_ref(1).locator)
    await rig.hear(6)

    await rig.ctl.restart_chapter()
    record = await rig.saved()
    assert record.position.chapter_locator == story_ref(1).locator
    assert resume_sentence(record.position) == 0
    await rig.hear(1)

    assert rig.events.heard == [0, 1, 2, 3, 4, 5, 0]


async def test_next_chapter_manual_updates_persisted_position(controller_rig):
    rig = story_rig(controller_rig)
    await rig.start(story_ref(1).locator)
    await rig.hear(2)

    await rig.ctl.next_chapter()
    record = rig.repos.progress.get(STORY_ID)  # ya guardado al volver
    assert (record.position.chapter_locator, record.position.chapter_index) == (story_ref(2).locator, 2)
    assert resume_sentence(record.position) == 0
    await rig.hear(1)
    await rig.ctl.prev_chapter()
    await rig.hear(1)

    assert rig.events.heard_at == [("Cap 1", 0), ("Cap 1", 1), ("Cap 2", 0), ("Cap 1", 0)]
    record = await rig.saved()
    assert record.position.chapter_locator == story_ref(1).locator


async def test_speed_change_applies_to_next_unqueued_chunk(controller_rig):
    rig = story_rig(controller_rig, chapters=1)
    await rig.start(story_ref(1).locator)
    await rig.fill()  # [0] [1-3] [4-6] en cola; [7-9] generado esperando hueco
    assert [c[2] for c in rig.edge.calls] == [1.0] * 4

    await rig.ctl.set_speed(1.5, apply_now=False)
    await rig.hear(10)

    assert rig.events.heard == list(range(10))  # lo ya encolado sigue sonando
    regenerated = rig.edge.calls[4:]
    assert chunk_starts(rig.edge)[4] == 7
    assert regenerated and {c[2] for c in regenerated} == {1.5}
    assert rig.settings.get("speed") == 1.5


async def test_speed_is_clamped_to_engine_range(controller_rig):
    rig = story_rig(controller_rig, chapters=1)
    await rig.start(story_ref(1).locator)
    await rig.ctl.set_speed(3.0, apply_now=True)
    await rig.hear(1)
    assert rig.edge.calls[-1][2] == 2.0
    assert rig.events.notices[-1][0] == "info"


async def test_apply_now_restarts_current_sentence(controller_rig):
    rig = story_rig(controller_rig, chapters=1)
    await rig.start(story_ref(1).locator)
    await rig.hear(3, partial=True)  # la 2 (dentro de [1-3]) suena a medias

    await rig.ctl.set_voice("es-ES-ElviraNeural", None, apply_now=True)
    await rig.hear(2)

    assert rig.events.heard == [0, 1, 2, 2, 3]
    assert rig.edge.calls[-1][1] == "es-ES-ElviraNeural"
    first_new = next(c for c in rig.edge.calls if c[1] == "es-ES-ElviraNeural")
    assert first_new[0].endswith(":2-2")


async def test_continuous_off_stops_at_chapter_end(controller_rig):
    rig = story_rig(controller_rig, chapters=2, sentences=3)
    await rig.ctl.set_continuous(False)
    await rig.start(story_ref(1).locator)
    await rig.hear(2)
    await settle()
    assert rig.ctl.state is S.READING  # aún queda audio

    await rig.hear(1)
    await until(lambda: rig.ctl.state is S.COMPLETED)

    assert rig.events.heard_at == [("Cap 1", 0), ("Cap 1", 1), ("Cap 1", 2)]
    assert all(":1:" in c[0] for c in rig.edge.calls)  # nada del capítulo 2
    record = await rig.saved()
    assert resume_sentence(record.position) == 3


# --- fin de la novela, ambigüedad y bucles -----------------------------------------


async def test_last_chapter_completes_with_notice(controller_rig):
    rig = story_rig(controller_rig, chapters=1, sentences=2)
    await rig.start(story_ref(1).locator)
    await rig.hear(2)
    await until(lambda: rig.ctl.state is S.COMPLETED)
    assert ("info", "No hay más capítulos publicados") in rig.events.notices


async def test_ambiguous_next_needs_choice_then_continues(controller_rig):
    rig = story_rig(controller_rig, chapters=3, sentences=2)
    first = rig.source.chapters[story_ref(1).locator]
    first.next_ref = None  # sin «siguiente» claro: el navegador vuelve a consultar la página
    first.next_candidates = [story_ref(2), story_ref(3)]
    await rig.start(story_ref(1).locator)
    await rig.hear(2)
    await until(lambda: rig.ctl.state is S.NEEDS_ATTENTION)
    assert rig.events.choices == [[story_ref(2), story_ref(3)]]

    await rig.ctl.choose_next(story_ref(3))
    await rig.hear(1)
    assert rig.events.heard_at[-1] == ("Cap 3", 0)
    assert rig.ctl.state is S.READING


async def test_chapter_loop_needs_attention(controller_rig):
    rig = story_rig(controller_rig, chapters=2, sentences=1)
    second = rig.source.chapters[story_ref(2).locator]
    second.next_ref = story_ref(1)  # 2 -> 1: ciclo
    await rig.start(story_ref(1).locator)
    await rig.hear(2)
    await until(lambda: rig.ctl.state is S.NEEDS_ATTENTION)
    assert rig.events.notices[-1][0] == "warn"


# --- errores -----------------------------------------------------------------------


async def test_output_error_reopens_once_then_pauses(controller_rig):
    rig = story_rig(controller_rig, chapters=1)
    await rig.start(story_ref(1).locator)
    await rig.hear(1)

    rig.out.fail(AudioOutputError())
    await until(lambda: rig.out.starts == 2)  # se reabre una vez
    assert rig.ctl.state is S.READING

    rig.out.fail(AudioOutputError())
    await until(lambda: rig.ctl.state is S.PAUSED)
    assert rig.events.notices[-1] == ("warn", AudioOutputError.user_message)

    await rig.ctl.resume()  # el usuario reanuda: se reabre la salida
    await rig.hear(1)
    assert rig.events.heard == [0, 1]


async def test_unexpected_producer_error_goes_to_error_state(controller_rig, caplog):
    rig = story_rig(controller_rig, chapters=1)

    async def broken(*args):
        raise RuntimeError("fallo interno")

    rig.manager.synthesize = broken
    await rig.start(story_ref(1).locator)
    await until(lambda: rig.ctl.state is S.ERROR)

    assert rig.events.notices[-1] == ("error", "Ocurrió un problema inesperado.")
    assert "fallo interno" in caplog.text
    assert rig.repos.progress.get(STORY_ID) is not None  # el progreso quedó guardado


async def test_event_callback_errors_are_contained(controller_rig, caplog):
    rig = story_rig(controller_rig, chapters=1, sentences=30)

    def boom(*args):
        raise ValueError("interfaz rota")

    rig.events.on_sentence = boom
    rig.events.on_session_state = boom
    await rig.start(story_ref(1).locator)
    await rig.fill()
    for _ in range(3):
        rig.out.pull(SAMPLES_PER_SENTENCE)
        await settle()
    record = await rig.saved()
    assert resume_sentence(record.position) == 2  # las frases se siguen confirmando
    assert "interfaz rota" in caplog.text
    assert rig.producers() == 1  # la lectura sigue
