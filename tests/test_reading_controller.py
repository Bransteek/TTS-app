"""Controlador de lectura: flujo completo, controles, interrupciones y estados."""
from __future__ import annotations

import asyncio
import tempfile
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from lector.content.base import doc_id_for_url
from lector.errors import (
    AudioOutputError, DamagedDocument, EdgeUnavailable, PageUnreachable, PiperModelMissing,
)
from lector.models import Position
from lector.paths import data_dir
from lector.security.http_client import SafeFetcher
from lector.errors import AUDIO_OUTPUT_LOST
from lector.services.reading_controller import (
    CHAPTER_FINISHED, OUTPUT_RETRY_DELAYS_S, STALL_TIMEOUT_S, SessionState, resume_sentence,
)
from lector.tts.engine_manager import EngineState
from tests.controller_rig import (
    STORY_ID, WAIT_S, Outage, lector_tasks, settle, story, story_chapter, story_document,
    story_ref, story_rig, until,
)
from tests.fakes import SAMPLES_PER_SENTENCE, FakeSource
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
        doc, chapter, progress = await asyncio.wait_for(rig.ctl.open(WORK + "capitulo-99/"), WAIT_S)
        assert doc.doc_id == doc_id_for_url(WORK)
        assert progress is None
        assert rig.edge.calls == []  # la vista previa no lee

        await rig.play(doc.doc_id)
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
    doc, chapter, _ = await asyncio.wait_for(rig.ctl.open(str(path)), WAIT_S)
    await rig.play(doc.doc_id)
    await rig.hear(2)
    assert [s[1] for s in rig.events.sentences[:2]] == [s.text for s in chapter.sentences[:2]]
    assert "Primera frase del libro." in [s[1] for s in rig.events.sentences]


async def test_stop_cancels_pending_jobs(controller_rig):
    rig = story_rig(controller_rig)
    await rig.start(story_ref(1).locator)
    await rig.fill()
    await rig.hear(2)

    await rig.stop()

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
    await rig.stop()
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
    await rig.written(  # sin forzar nada: lo dejaron escrito los guardados obligatorios
        lambda r: resume_sentence(r.position) == 10 and r.session_state == "waiting_network"
    )

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
    await asyncio.wait_for(rig.ctl.pause(), WAIT_S)
    check()
    await asyncio.wait_for(rig.ctl.next_sentence(), WAIT_S)  # navegar mientras se cambia de motor
    check()
    await asyncio.wait_for(rig.ctl.resume(), WAIT_S)
    await asyncio.wait_for(rig.ctl.pause(), WAIT_S)
    rig.engine_gate.set()  # termina el cambio a Piper con la lectura en pausa
    await until(lambda: rig.player.queued_count() == 3)
    await settle()
    check()
    await asyncio.wait_for(rig.ctl.resume(), WAIT_S)
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

    await asyncio.wait_for(rig.ctl.next_sentence(), WAIT_S)
    await rig.hear(1, partial=True)
    await asyncio.wait_for(rig.ctl.prev_sentence(), WAIT_S)
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

    await asyncio.wait_for(rig.ctl.skip_seconds(10), WAIT_S)  # 2 frases de 5 s
    await rig.hear(1, partial=True)
    await asyncio.wait_for(rig.ctl.skip_seconds(-10), WAIT_S)
    await rig.hear(1, partial=True)
    await asyncio.wait_for(rig.ctl.skip_seconds(7), WAIT_S)  # cubrir 7 s exige 2 frases
    await rig.hear(1)

    assert rig.events.heard == [0, 1, 2, 4, 2, 4]


async def test_restart_chapter(controller_rig):
    rig = story_rig(controller_rig)
    await rig.start(story_ref(1).locator)
    await rig.hear(6)

    await asyncio.wait_for(rig.ctl.restart_chapter(), WAIT_S)
    record = await rig.saved()
    assert record.position.chapter_locator == story_ref(1).locator
    assert resume_sentence(record.position) == 0
    await rig.hear(1)

    assert rig.events.heard == [0, 1, 2, 3, 4, 5, 0]


async def test_next_chapter_manual_updates_persisted_position(controller_rig):
    rig = story_rig(controller_rig)
    await rig.start(story_ref(1).locator)
    await rig.hear(2)

    await asyncio.wait_for(rig.ctl.next_chapter(), WAIT_S)
    record = rig.repos.progress.get(STORY_ID)  # ya guardado al volver
    assert (record.position.chapter_locator, record.position.chapter_index) == (story_ref(2).locator, 2)
    assert resume_sentence(record.position) == 0
    await rig.hear(1)
    await asyncio.wait_for(rig.ctl.prev_chapter(), WAIT_S)
    await rig.hear(1)

    assert rig.events.heard_at == [("Cap 1", 0), ("Cap 1", 1), ("Cap 2", 0), ("Cap 1", 0)]
    record = await rig.saved()
    assert record.position.chapter_locator == story_ref(1).locator


async def test_speed_change_applies_to_next_unqueued_chunk(controller_rig):
    """§5.6: sin «Aplicar ya», el clip que suena acaba igual y la cola preparada se regenera."""
    rig = story_rig(controller_rig, chapters=1, sentences=20)
    await rig.start(story_ref(1).locator)
    await rig.fill()  # [0] [1-3] [4-6] en cola; [7-9] generado esperando hueco
    await rig.hear(2)  # suena [1-3]
    assert [c[2] for c in rig.edge.calls] == [1.0] * 5  # ya se generó también [10-12]

    await asyncio.wait_for(rig.ctl.set_speed(1.5, apply_now=False), WAIT_S)
    await rig.hear(18)

    assert rig.events.heard == list(range(20))  # sin cortes ni repeticiones
    speed_of = {call[0]: call[2] for call in rig.edge.calls}  # la última síntesis de cada id
    played = {sentence: speed_of[chunk_id] for chunk_id, sentence in rig.played}
    assert [played[i] for i in (1, 2, 3)] == [1.0] * 3  # el clip que sonaba termina igual
    assert {played[i] for i in range(4, 20)} == {1.5}  # todo lo posterior, a la nueva velocidad
    assert chunk_starts(rig.edge)[5] == 4  # se regenera desde el primer fragmento descartado
    assert rig.settings.get("speed") == 1.5


async def test_speed_is_clamped_to_engine_range(controller_rig):
    rig = story_rig(controller_rig, chapters=1)
    await rig.start(story_ref(1).locator)
    await asyncio.wait_for(rig.ctl.set_speed(3.0, apply_now=True), WAIT_S)
    await rig.hear(1)
    assert rig.edge.calls[-1][2] == 2.0
    assert rig.events.notices[-1][0] == "info"


async def test_apply_now_restarts_current_sentence(controller_rig):
    rig = story_rig(controller_rig, chapters=1)
    await rig.start(story_ref(1).locator)
    await rig.hear(3, partial=True)  # la 2 (dentro de [1-3]) suena a medias

    await asyncio.wait_for(rig.ctl.set_voice("es-ES-ElviraNeural", None, apply_now=True), WAIT_S)
    await rig.hear(2)

    assert rig.events.heard == [0, 1, 2, 2, 3]
    assert rig.edge.calls[-1][1] == "es-ES-ElviraNeural"
    first_new = next(c for c in rig.edge.calls if c[1] == "es-ES-ElviraNeural")
    assert first_new[0].endswith(":2-2")


async def test_continuous_off_stops_at_chapter_end(controller_rig):
    rig = story_rig(controller_rig, chapters=2, sentences=3)
    await asyncio.wait_for(rig.ctl.set_continuous(False), WAIT_S)
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

    await asyncio.wait_for(rig.ctl.choose_next(story_ref(3)), WAIT_S)
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


async def exhaust_output_retries(rig) -> None:
    """Con la salida sin volver, deja pasar todas las esperas de reintento (unos 2 min)."""
    for delay in OUTPUT_RETRY_DELAYS_S:
        await settle()
        attempts = rig.out.attempts
        rig.save_clock.advance(delay)
        await until(lambda: rig.out.attempts == attempts + 1 or rig.ctl.state is S.PAUSED)


async def test_output_retries_back_off_for_two_minutes_then_pause(controller_rig):
    assert OUTPUT_RETRY_DELAYS_S[:6] == (1, 2, 4, 8, 16, 30)
    assert 110 <= sum(OUTPUT_RETRY_DELAYS_S) <= 130  # unos 2 minutos en total
    rig = story_rig(controller_rig, chapters=1, sentences=20)
    await rig.start(story_ref(1).locator)
    await rig.hear(1)

    rig.out.start_error = AudioOutputError()  # el dispositivo tarda en volver (p. ej. al despertar)
    rig.out.fail(AudioOutputError())
    for delay in OUTPUT_RETRY_DELAYS_S:
        await settle()
        assert rig.ctl.state is S.READING  # sin pausa ni aviso mientras reintenta
        attempts = rig.out.attempts
        rig.save_clock.advance(delay - 0.01)
        await settle()
        assert rig.out.attempts == attempts  # aún no: espera creciente
        rig.save_clock.advance(0.01)
        await until(lambda: rig.out.attempts == attempts + 1)

    await until(lambda: rig.ctl.state is S.PAUSED)
    assert rig.events.notices[-1] == ("warn", AUDIO_OUTPUT_LOST)
    assert [n for n in rig.events.notices if n[0] == "warn"] == [("warn", AUDIO_OUTPUT_LOST)]

    rig.out.start_error = None
    await asyncio.wait_for(rig.ctl.resume(), WAIT_S)  # el usuario reanuda: se reabre la salida
    await rig.hear(1)
    assert rig.events.heard == [0, 1]


async def test_output_that_comes_back_resets_the_back_off(controller_rig):
    rig = story_rig(controller_rig, chapters=1, sentences=20)
    await rig.start(story_ref(1).locator)
    await rig.hear(1)

    rig.out.start_error = AudioOutputError()
    rig.out.fail(AudioOutputError())
    await settle()
    rig.save_clock.advance(1)
    await until(lambda: rig.out.attempts == 2)
    rig.out.start_error = None  # vuelve el dispositivo
    rig.save_clock.advance(2)
    await until(lambda: rig.out.running)
    await rig.hear(1)  # suena: la cuenta de reintentos vuelve a cero

    rig.out.fail(AudioOutputError())
    await settle()
    rig.save_clock.advance(1)  # otra vez la primera espera, no la de 4 s
    await until(lambda: rig.out.running)
    # La frase 1 ya había salido entera, pero la 2 aún no empezó: se repite la 1 (lo que
    # quedaba en el búfer del dispositivo pudo perderse) y luego sigue la 2.
    rig.out.pull(SAMPLES_PER_SENTENCE)
    await settle()
    assert rig.events.heard == [0, 1]
    await rig.hear(1)
    assert rig.events.heard == [0, 1, 2]
    assert rig.ctl.state is S.READING


async def test_missing_edge_voice_is_notified_once_and_reading_goes_on(controller_rig):
    from lector.errors import EDGE_VOICE_MISSING
    from lector.tts.edge_engine import EdgeEngine

    edge = EdgeEngine()
    edge.set_known_voices([{"ShortName": "es-ES-ElviraNeural"}])  # la voz elegida no existe
    rig = story_rig(controller_rig, chapters=1, sentences=6, edge=edge)
    await rig.start(story_ref(1).locator)
    await rig.hear(4)

    assert rig.events.heard == [0, 1, 2, 3]  # sigue con Piper
    assert rig.events.notices.count(("warn", EDGE_VOICE_MISSING)) == 1


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


async def test_output_error_without_session_is_ignored(controller_rig):
    rig = story_rig(controller_rig, chapters=1, sentences=2)
    await rig.start(story_ref(1).locator)
    await rig.hear(2)
    await until(lambda: rig.ctl.state is S.COMPLETED)

    for _ in range(2):
        rig.out.fail(AudioOutputError())
        await settle()
        rig.save_clock.advance(1.0)
        await settle()

    assert rig.ctl.state is S.COMPLETED
    assert [n for n in rig.events.notices if n[0] == "warn"] == []
    # La siguiente sesión vuelve a abrir la salida y suena (no quedó en pausa).
    await rig.play(STORY_ID, Position(1, story_ref(1).locator, 0, 0, 0, None))
    await rig.hear(1)
    assert rig.ctl.state is S.READING
    assert rig.events.heard[-1] == 0


async def test_producer_failure_reports_error_after_tail(controller_rig):
    rig = story_rig(controller_rig, chapters=2, sentences=10, prefetch_ahead=0)
    rig.source.failures = [DamagedDocument()]  # el capítulo 2 está dañado
    await rig.start(story_ref(1).locator)
    await rig.fill()
    await rig.hear(1)
    await until(lambda: rig.producers() == 0)  # el productor ya falló

    assert rig.ctl.state is S.READING  # aún suena el final del capítulo 1
    assert rig.player.queued_count() > 0
    await asyncio.wait_for(rig.ctl.pause(), WAIT_S)
    assert rig.ctl.state is S.PAUSED
    await asyncio.wait_for(rig.ctl.resume(), WAIT_S)
    await rig.hear(9)

    await until(lambda: rig.ctl.state is S.ERROR)
    assert rig.events.heard == list(range(10))
    assert rig.events.notices[-1] == ("error", DamagedDocument.user_message)
    await rig.written(lambda r: r.session_state == "error" and r.position.sentence_index == 9)


async def test_failure_saves_progress_at_once(controller_rig):
    rig = story_rig(controller_rig, chapters=2, sentences=4, prefetch_ahead=0)
    rig.source.gate = asyncio.Event()  # el capítulo 2 tarda...
    rig.source.failures = [DamagedDocument()]  # ...y llega dañado
    await rig.start(story_ref(1).locator)
    await rig.hear(3)  # [0] termina (guardado forzado); la 2 confirma la 1 (diferido)
    await settle()
    assert rig.stored().position.sentence_index == 0  # aún no han pasado los 2 s

    rig.source.gate.set()
    await rig.written(lambda r: r.position.sentence_index == 1)  # el fallo guarda al momento


async def test_progress_save_is_throttled(controller_rig):
    rig = story_rig(controller_rig, chapters=1)
    await rig.start(story_ref(1).locator)
    await rig.fill()
    await rig.hear(1)  # [0] termina: guardado forzado
    await rig.written(lambda r: r.position.sentence_index == 0)

    await rig.hear(3, partial=True)  # 1, 2 y 3 empiezan: confirman 1 y 2 sin forzar
    await settle()
    assert rig.stored().position.sentence_index == 0  # como mucho un guardado cada 2 s

    rig.save_clock.advance(2.1)
    await rig.written(lambda r: r.position.sentence_index == 2)  # el diferido escribe lo último


async def test_entering_waiting_network_saves_at_once(controller_rig):
    rig = story_rig(controller_rig, chapters=1, sentences=20)
    await rig.start(story_ref(1).locator)
    await rig.fill()
    rig.edge.error = EdgeUnavailable
    rig.piper.error = PiperModelMissing
    rig.engine_gate = asyncio.Event()  # el cambio de motor espera hasta abrirla
    await rig.hear(1)  # [0] termina: guardado forzado aún en «reading»
    await rig.written(lambda r: r.position.sentence_index == 0 and r.session_state == "reading")

    rig.engine_gate.set()  # falla también Piper: sin red
    await until(lambda: rig.ctl.state is S.WAITING_NETWORK)
    await rig.written(lambda r: r.session_state == "waiting_network")


async def test_play_while_playing_keeps_a_single_producer(controller_rig):
    rig = story_rig(controller_rig, chapters=2, sentences=20)
    await rig.start(story_ref(1).locator)
    await rig.fill()
    await rig.hear(2, partial=True)  # la 1 suena a medias

    await rig.play(STORY_ID)
    await settle()
    assert rig.producers() == 1
    await rig.hear(2)
    assert rig.events.heard == [0, 1, 1, 2]


async def test_stale_manual_jump_is_discarded(controller_rig):
    rig = story_rig(controller_rig, chapters=3, sentences=30, prefetch_ahead=0)
    await rig.start(story_ref(1).locator)
    await rig.hear(1)
    rig.source.failures = Outage(PageUnreachable())
    jump = asyncio.create_task(rig.ctl.next_chapter())  # espera al capítulo 2 sin red
    await until(lambda: rig.ctl.state is S.WAITING_NETWORK)

    await asyncio.wait_for(rig.ctl.restart_chapter(), WAIT_S)  # una orden más nueva gana
    rig.source.failures = []
    await asyncio.wait_for(jump, WAIT_S)
    await rig.hear(1)

    assert rig.events.heard_at[-1] == ("Cap 1", 0)
    assert rig.stored().position.chapter_locator == story_ref(1).locator


async def test_stop_cancels_dangling_navigation(controller_rig):
    rig = story_rig(controller_rig, chapters=3, sentences=30, prefetch_ahead=0)
    await rig.start(story_ref(1).locator)
    await rig.hear(1)
    rig.source.failures = Outage(PageUnreachable())
    jump = asyncio.create_task(rig.ctl.next_chapter())
    await until(lambda: rig.ctl.state is S.WAITING_NETWORK)

    await rig.stop()
    assert await asyncio.wait_for(jump, WAIT_S) is None  # terminó sin error
    assert lector_tasks() == []

    await rig.play(STORY_ID)  # sesión nueva, aún sin red para el capítulo 2
    await rig.hear(1)
    assert rig.ctl.state is S.READING  # sin un WAITING_NETWORK heredado
    rig.source.failures = []
    await settle()
    assert rig.events.heard_at[-1][0] == "Cap 1"


async def test_continuous_on_during_tail_continues_reading(controller_rig):
    rig = story_rig(controller_rig, chapters=2, sentences=3)
    await asyncio.wait_for(rig.ctl.set_continuous(False), WAIT_S)
    await rig.start(story_ref(1).locator)
    await rig.hear(1)
    await until(lambda: rig.producers() == 0)  # el capítulo ya está todo en cola

    await asyncio.wait_for(rig.ctl.set_continuous(True), WAIT_S)
    await rig.hear(3)

    assert rig.events.heard_at == [("Cap 1", 0), ("Cap 1", 1), ("Cap 1", 2), ("Cap 2", 0)]
    assert S.COMPLETED not in rig.events.states


# --- sondas del revisor -----------------------------------------------------------


async def test_events_from_audio_thread(controller_rig):
    rig = story_rig(controller_rig, chapters=2, sentences=6)
    await rig.start(story_ref(1).locator)
    for _ in range(12):
        await until(lambda: rig.player.queued_count() > 0)
        await asyncio.to_thread(rig.out.pull, SAMPLES_PER_SENTENCE)
        await settle(20)
    assert [i for _, i in rig.events.heard_at] == list(range(6)) * 2


async def test_stop_while_paused_and_blocked(controller_rig):
    rig = story_rig(controller_rig, chapters=1, sentences=30)
    await rig.start(story_ref(1).locator)
    await rig.fill()
    await asyncio.wait_for(rig.ctl.pause(), WAIT_S)
    await rig.stop()
    assert lector_tasks() == []
    await rig.play(STORY_ID)
    assert rig.ctl.state is S.READING
    await rig.hear(1)
    assert rig.events.heard == [0]


async def test_waiting_engine_pause_then_recover(controller_rig):
    rig = story_rig(controller_rig, chapters=1, sentences=20)
    await rig.start(story_ref(1).locator)
    await rig.fill()
    rig.edge.error = EdgeUnavailable
    rig.piper.error = PiperModelMissing
    await rig.hear(10)
    await until(lambda: rig.ctl.state is S.WAITING_NETWORK)
    await asyncio.wait_for(rig.ctl.pause(), WAIT_S)
    assert rig.ctl.state is S.PAUSED
    rig.edge.error = None
    await until(lambda: rig.player.queued_count() > 0)
    await settle()
    assert rig.ctl.state is S.PAUSED  # se resolvió la espera, pero el usuario pausó
    await asyncio.wait_for(rig.ctl.resume(), WAIT_S)
    await rig.hear(2)
    assert rig.events.heard == list(range(12))


# --- salida caída fuera de sesión y órdenes que relanzan ----------------------------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (lambda ctl: ctl.next_chapter(), ("Cap 2", 0)),
        (lambda ctl: ctl.restart_chapter(), ("Cap 1", 0)),
        (lambda ctl: ctl.prev_sentence(), ("Cap 1", 1)),
        (lambda ctl: ctl.skip_seconds(-5), ("Cap 1", 0)),
    ],
    ids=["next_chapter", "restart_chapter", "prev_sentence", "skip_seconds"],
)
async def test_command_after_output_error_while_completed_reopens_output(
    controller_rig, command, expected
):
    rig = story_rig(controller_rig, chapters=2, sentences=2)
    await asyncio.wait_for(rig.ctl.set_continuous(False), WAIT_S)
    await rig.start(story_ref(1).locator)
    await rig.hear(2)
    await until(lambda: rig.ctl.state is S.COMPLETED)
    rig.out.fail(AudioOutputError())  # ignorado: no hay sesión
    await settle()
    assert not rig.out.running

    await asyncio.wait_for(command(rig.ctl), WAIT_S)
    assert rig.ctl.state is S.READING
    await rig.hear(1)  # falla si la salida sigue parada

    assert rig.out.running
    assert rig.events.heard_at[-1] == expected
    assert [n for n in rig.events.notices if n[0] == "warn"] == []


async def test_choose_next_after_output_error_while_needing_attention_reopens_output(
    controller_rig,
):
    rig = story_rig(controller_rig, chapters=3, sentences=2)
    first = rig.source.chapters[story_ref(1).locator]
    first.next_ref = None
    first.next_candidates = [story_ref(2), story_ref(3)]
    await rig.start(story_ref(1).locator)
    await rig.hear(2)
    await until(lambda: rig.ctl.state is S.NEEDS_ATTENTION)
    rig.out.fail(AudioOutputError())
    await settle()

    await asyncio.wait_for(rig.ctl.choose_next(story_ref(3)), WAIT_S)
    await rig.hear(1)

    assert rig.out.running
    assert rig.ctl.state is S.READING
    assert rig.events.heard_at[-1] == ("Cap 3", 0)


async def test_failed_reopen_on_relaunch_follows_output_error_path(controller_rig):
    rig = story_rig(controller_rig, chapters=2, sentences=2)
    await asyncio.wait_for(rig.ctl.set_continuous(False), WAIT_S)
    await rig.start(story_ref(1).locator)
    await rig.hear(2)
    await until(lambda: rig.ctl.state is S.COMPLETED)
    rig.out.fail(AudioOutputError())
    await settle()
    rig.out.start_error = AudioOutputError()  # el dispositivo sigue sin responder

    await asyncio.wait_for(rig.ctl.next_chapter(), WAIT_S)
    await settle()
    attempts = rig.out.attempts
    assert rig.ctl.state is S.READING  # primer fallo: reintentos con espera creciente
    await exhaust_output_retries(rig)
    await until(lambda: rig.ctl.state is S.PAUSED)
    assert rig.out.attempts == attempts + len(OUTPUT_RETRY_DELAYS_S)
    assert rig.events.notices[-1] == ("warn", AUDIO_OUTPUT_LOST)

    rig.out.start_error = None
    await asyncio.wait_for(rig.ctl.resume(), WAIT_S)
    await rig.hear(1)
    assert rig.events.heard_at[-1] == ("Cap 2", 0)


# --- marcas y saltos en el borde entre frases y capítulos ---------------------------


async def test_stop_right_after_a_sentence_ends_does_not_repeat_it(controller_rig):
    rig = story_rig(controller_rig, chapters=1, sentences=4)
    await asyncio.wait_for(rig.ctl.set_continuous(False), WAIT_S)
    await rig.start(story_ref(1).locator)
    await until(lambda: rig.producers() == 0)  # todo encolado: stop() no cede el turno
    await rig.hear(2)
    rig.out.pull(SAMPLES_PER_SENTENCE)  # la frase 1 termina y empieza la 2; aviso pendiente
    async with asyncio.timeout(WAIT_S):  # sin wait_for: ni un turno antes de parar
        await rig.ctl.stop()

    assert resume_sentence(rig.stored().position) == 2


async def test_restart_chapter_at_start_of_next_chapter_restarts_that_chapter(controller_rig):
    rig = story_rig(controller_rig, chapters=3, sentences=2)
    await rig.start(story_ref(1).locator)
    await rig.hear(3, partial=True)  # la marca sigue en el capítulo 1
    assert rig.events.heard_at[-1] == ("Cap 2", 0)

    await asyncio.wait_for(rig.ctl.restart_chapter(), WAIT_S)
    await rig.hear(1)

    assert rig.events.heard_at[-1] == ("Cap 2", 0)


async def test_next_sentence_at_start_of_next_chapter_stays_in_that_chapter(controller_rig):
    rig = story_rig(controller_rig, chapters=3, sentences=4)
    await rig.start(story_ref(1).locator)
    await rig.hear(5, partial=True)
    assert rig.events.heard_at[-1] == ("Cap 2", 0)

    await asyncio.wait_for(rig.ctl.next_sentence(), WAIT_S)
    await rig.hear(1)

    assert rig.events.heard_at[-1] == ("Cap 2", 1)


# --- supervivientes de la ronda 1 ---------------------------------------------------


async def test_speed_change_with_empty_queue_relaunches_producer(controller_rig):
    rig = story_rig(controller_rig, chapters=1, sentences=20)
    await rig.start(story_ref(1).locator)
    await rig.fill()
    rig.edge.error = EdgeUnavailable
    rig.piper.error = PiperModelMissing
    await rig.hear(10)
    await until(lambda: rig.ctl.state is S.WAITING_NETWORK)
    await until(lambda: rig.player.queued_count() == 0)

    await asyncio.wait_for(rig.ctl.set_speed(1.5, apply_now=False), WAIT_S)
    assert rig.producers() == 1  # nada que descartar, pero el productor sigue
    rig.edge.error = None
    rig.piper.error = None
    await rig.hear(3)

    assert rig.events.heard == list(range(13))
    assert rig.edge.calls[-1][2] == 1.5


async def test_cancelling_the_caller_of_a_navigation_propagates(controller_rig):
    rig = story_rig(controller_rig, chapters=3, sentences=30, prefetch_ahead=0)
    await rig.start(story_ref(1).locator)
    await rig.hear(1)
    rig.source.failures = Outage(PageUnreachable())
    task = asyncio.create_task(rig.ctl.next_chapter())
    await until(lambda: rig.ctl.state is S.WAITING_NETWORK)

    task.cancel()
    await asyncio.wait({task}, timeout=WAIT_S)

    assert task.cancelled()  # quien llamó ve su cancelación, no un retorno silencioso
    await settle()
    assert rig.ctl.state is S.READING


async def test_stop_during_output_retry_cancels_it(controller_rig):
    rig = story_rig(controller_rig, chapters=1, sentences=20)
    await rig.start(story_ref(1).locator)
    await rig.hear(1)
    rig.out.fail(AudioOutputError())
    await settle()

    await rig.stop()

    assert lector_tasks() == []
    rig.save_clock.advance(1.0)
    await settle()
    assert not rig.out.running  # el reintento no reabre la salida de una sesión parada
    await rig.play(STORY_ID)
    await rig.hear(1)
    assert rig.ctl.state is S.READING


async def test_seek_while_paused_after_output_failure_stays_paused(controller_rig):
    rig = story_rig(controller_rig, chapters=1, sentences=20)
    await rig.start(story_ref(1).locator)
    await rig.hear(1)
    rig.out.start_error = AudioOutputError()
    rig.out.fail(AudioOutputError())
    await exhaust_output_retries(rig)  # no vuelve: pausa con aviso
    await until(lambda: rig.ctl.state is S.PAUSED)
    rig.out.start_error = None

    await asyncio.wait_for(rig.ctl.next_sentence(), WAIT_S)
    await settle()
    assert rig.ctl.state is S.PAUSED  # saltar no quita la pausa
    assert not rig.out.running

    await asyncio.wait_for(rig.ctl.resume(), WAIT_S)
    await rig.hear(1)
    assert rig.events.heard[-1] == 2


# --- Task 16: motor preferido y salto a una página -----------------------------------


async def test_set_preferred_engine_persists_and_applies_to_later_chunks(controller_rig):
    rig = story_rig(controller_rig, chapters=1, sentences=20)
    await rig.start(story_ref(1).locator)
    await rig.fill()  # [0] [1-3] [4-6] en cola y [7-9] generado por Edge

    await asyncio.wait_for(rig.ctl.set_preferred_engine("piper"), WAIT_S)

    assert rig.settings.get("preferred_engine") == "piper"
    assert rig.manager.state is EngineState.PIPER_ACTIVE
    await rig.hear(20)
    assert rig.events.heard == list(range(20))  # el cambio no corta ni repite
    assert chunk_starts(rig.piper)[:3] == [10, 11, 12]  # límites de Piper desde el siguiente
    assert all(start <= 7 for start in chunk_starts(rig.edge))
    assert (EngineState.PIPER_ACTIVE, "Piper local") in rig.events.engine


async def test_set_preferred_engine_rejects_unknown_name(controller_rig):
    rig = controller_rig()
    with pytest.raises(ValueError):
        await asyncio.wait_for(rig.ctl.set_preferred_engine("otro"), WAIT_S)
    assert rig.settings.get("preferred_engine") == "edge"


async def test_locate_page_finds_first_sentence_of_that_page(controller_rig, make_pdf):
    path = make_pdf(pages=[
        [("Primera página del libro con bastante texto. Otra frase de la primera página.", 11)],
        [("Comienza la segunda página del libro. Sigue la segunda página.", 11)],
        [("Tercera página con su propio texto. Fin del libro de prueba.", 11)],
    ])
    rig = controller_rig()
    doc, chapter, _ = await asyncio.wait_for(rig.ctl.open(str(path)), WAIT_S)

    pos = await asyncio.wait_for(rig.ctl.locate_page(doc.doc_id, 2), WAIT_S)

    first_on_2 = next(s for s in chapter.sentences if s.page == 2)
    assert pos.page == 2
    assert pos.chapter_locator == chapter.ref.locator
    assert pos.sentence_index == first_on_2.index
    await rig.play(doc.doc_id, pos)
    await rig.hear(1)
    assert rig.events.sentences[0][1] == first_on_2.text
    assert await asyncio.wait_for(rig.ctl.locate_page("otro-doc", 2), WAIT_S) is None


async def test_open_applies_saved_prefix_override_only_to_its_work(controller_rig):
    """§5.2: la corrección se recuerda para esa obra del host; otra obra del mismo host no cambia."""
    from tests.test_chapter_navigation import HOST, PAGE_URL, make_handler, page
    from tests.test_chapter_navigation import resolver as generic_resolver

    other = f"{HOST}/otra/y/capitulo-1/"
    pages = {PAGE_URL: page('<a href="/novela/x/capitulo-6/">Siguiente</a>'),
             other: page('<a href="/otra/y/capitulo-2/">Siguiente</a>')}
    fetcher = SafeFetcher(resolver=generic_resolver,
                          transport=httpx.MockTransport(make_handler(pages)))
    try:
        rig = controller_rig(fetcher=fetcher)
        doc_y0, _, _ = await asyncio.wait_for(rig.ctl.open(other), WAIT_S)
        doc, _, _ = await asyncio.wait_for(rig.ctl.open(PAGE_URL), WAIT_S)
        assert doc.work_prefix == HOST + "/novela/x/"
        host = HOST.split("://", 1)[1]
        rig.settings.set("work_prefix_overrides", {host: ["/novela/"]})

        doc_x, chapter_x, _ = await asyncio.wait_for(rig.ctl.open(PAGE_URL), WAIT_S)
        doc_y, chapter_y, _ = await asyncio.wait_for(rig.ctl.open(other), WAIT_S)

        assert doc_x.work_prefix == HOST + "/novela/"
        assert doc_x.doc_id == doc_id_for_url(host + "/novela/")
        assert chapter_x.next_ref.locator == f"{HOST}/novela/x/capitulo-6/"
        assert (doc_y.doc_id, doc_y.work_prefix) == (doc_y0.doc_id, doc_y0.work_prefix)
        assert doc_y.doc_id != doc_x.doc_id
        assert chapter_y.next_ref.locator == f"{HOST}/otra/y/capitulo-2/"

        rig.settings.set("work_prefix_overrides", {host: "/novela/"})  # formato antiguo
        doc_old, _, _ = await asyncio.wait_for(rig.ctl.open(PAGE_URL), WAIT_S)
        assert doc_old.work_prefix == HOST + "/novela/"
        await rig.close()
    finally:
        await fetcher.aclose()


# --- salida de audio atascada (vigilancia) ---------------------------------------------


async def tick(rig, seconds: float, step: float = 1.0) -> None:
    """Hace pasar `seconds` del reloj del controlador de `step` en `step`."""
    elapsed = 0.0
    while elapsed < seconds:
        rig.save_clock.advance(step)
        elapsed += step
        await settle()


async def test_stalled_output_is_restarted_and_resumes_the_current_sentence(controller_rig):
    rig = story_rig(controller_rig, chapters=1, sentences=20)
    await rig.start(story_ref(1).locator)
    await rig.hear(2, partial=True)  # la frase 1 va por la mitad…
    assert rig.events.heard == [0, 1]

    # …y PortAudio deja de pedir audio sin avisar (dispositivo quitado en CoreAudio).
    await tick(rig, STALL_TIMEOUT_S - 1)
    assert rig.out.starts == 1  # todavía no: unos segundos sin progreso
    await tick(rig, 2)
    await until(lambda: rig.out.stops == 1)  # la vigilancia para la salida…
    await tick(rig, 1)
    await until(lambda: rig.out.running and rig.out.starts == 2)  # …y la vuelve a abrir
    assert rig.ctl.state is S.READING
    assert rig.events.notices == []

    rig.out.pull(SAMPLES_PER_SENTENCE // 2 + 1)  # desde el inicio de la frase 1
    await settle()
    assert rig.events.heard == [0, 1]
    await rig.hear(1)
    assert rig.events.heard == [0, 1, 2]


async def test_watchdog_ignores_synthesis_waits(controller_rig):
    rig = story_rig(controller_rig, chapters=1, sentences=20)
    await rig.start(story_ref(1).locator)
    await rig.fill()
    rig.engine_gate = asyncio.Event()  # los reintentos de Edge quedan en espera
    rig.edge.error = EdgeUnavailable
    while rig.player.queued_count():  # se vacía la cola: no hay audio que deba sonar
        await rig.hear(1)
    await settle()
    assert rig.player.queued_count() == 0

    await tick(rig, 3 * STALL_TIMEOUT_S)
    assert rig.out.stops == 0 and rig.out.starts == 1
    assert rig.ctl.state is S.READING
    rig.engine_gate.set()


async def test_watchdog_ignores_network_waits_and_pause(controller_rig):
    rig = story_rig(controller_rig, chapters=1, sentences=20)
    await rig.start(story_ref(1).locator)
    await rig.fill()
    rig.edge.error = EdgeUnavailable
    rig.piper.error = PiperModelMissing
    await rig.hear(1)  # hay hueco: el siguiente fragmento no se puede sintetizar
    await until(lambda: rig.ctl.state is S.WAITING_NETWORK)

    await tick(rig, 3 * STALL_TIMEOUT_S)  # queda audio en cola, pero se espera la red
    assert rig.out.stops == 0

    rig.edge.error = rig.piper.error = None
    await until(lambda: rig.ctl.state is S.READING)
    await asyncio.wait_for(rig.ctl.pause(), WAIT_S)
    await tick(rig, 3 * STALL_TIMEOUT_S)  # en pausa no se espera progreso
    assert rig.out.stops == 0
    assert rig.events.notices == []


async def test_watchdog_does_not_fire_while_audio_plays(controller_rig):
    rig = story_rig(controller_rig, chapters=1, sentences=30)
    await rig.start(story_ref(1).locator)
    for _ in range(3 * int(STALL_TIMEOUT_S)):
        await rig.hear(1)
        await tick(rig, 1)
    assert rig.out.stops == 0 and rig.out.starts == 1


async def test_stop_cancels_the_watchdog(controller_rig):
    rig = story_rig(controller_rig, chapters=1, sentences=20)
    await rig.start(story_ref(1).locator)
    await rig.hear(1)
    assert any(t.get_name() == "lector:audio-watchdog" for t in lector_tasks())
    await rig.stop()
    assert lector_tasks() == []


async def test_output_that_stalls_again_after_a_restart_is_restarted_again(controller_rig):
    rig = story_rig(controller_rig, chapters=1, sentences=20)
    await rig.start(story_ref(1).locator)
    await rig.hear(2, partial=True)

    await tick(rig, STALL_TIMEOUT_S + 2)
    await until(lambda: rig.out.stops == 1)
    await tick(rig, 1)
    await until(lambda: rig.out.starts == 2)  # se reabrió, pero tampoco pide audio
    await tick(rig, STALL_TIMEOUT_S + 2)
    await until(lambda: rig.out.stops == 2)
    await tick(rig, 2)  # segunda espera de la serie
    await until(lambda: rig.out.starts == 3)
    rig.out.pull(SAMPLES_PER_SENTENCE)  # la frase 1 otra vez desde su inicio
    await rig.hear(1)
    assert rig.events.heard == [0, 1, 2] and rig.ctl.state is S.READING


# --- desplegable de capítulos: saltar al principio de un capítulo de la lista ------------


def go_to(rig, chapter: int, index: int | None = None):
    """Lo que manda el desplegable: documento, dirección del capítulo y su posición en la lista."""
    position = chapter - 1 if index is None else index
    return rig.ctl.go_to_chapter(STORY_ID, story_ref(chapter).locator, position)


async def test_go_to_chapter_jumps_to_its_start_and_saves(controller_rig):
    rig = story_rig(controller_rig, chapters=3)
    await rig.start(story_ref(1).locator)
    await rig.hear(3)

    jumped = await asyncio.wait_for(go_to(rig, 3), WAIT_S)

    assert jumped is True
    record = rig.stored()  # ya guardado al volver
    assert (record.position.chapter_locator, record.position.chapter_index) == (story_ref(3).locator, 3)
    assert resume_sentence(record.position) == 0
    await rig.hear(2)
    assert rig.events.heard_at[-2:] == [("Cap 3", 0), ("Cap 3", 1)]
    assert rig.events.documents[-1][1].ref.locator == story_ref(3).locator
    assert rig.ctl.state is S.READING


async def test_go_to_chapter_while_paused_stays_paused(controller_rig):
    rig = story_rig(controller_rig, chapters=3)
    await rig.start(story_ref(1).locator)
    await rig.hear(2)
    await asyncio.wait_for(rig.ctl.pause(), WAIT_S)
    paused_at = len(rig.events.states)

    assert await asyncio.wait_for(go_to(rig, 2), WAIT_S) is True
    await settle()

    assert rig.ctl.state is S.PAUSED  # elegir capítulo no quita la pausa
    assert rig.events.states[paused_at:] == []
    await until(lambda: rig.player.queued_count() > 0)
    heard = len(rig.events.sentences)
    rig.out.pull(SAMPLES_PER_SENTENCE)  # en pausa la salida solo da silencio
    await settle()
    assert len(rig.events.sentences) == heard
    assert rig.stored().position.chapter_locator == story_ref(2).locator
    assert resume_sentence(rig.stored().position) == 0
    await asyncio.wait_for(rig.ctl.resume(), WAIT_S)
    await rig.hear(1)
    assert rig.events.heard_at[-1] == ("Cap 2", 0)


async def test_go_to_chapter_after_stop_starts_reading_it(controller_rig):
    rig = story_rig(controller_rig, chapters=3)
    await rig.start(story_ref(1).locator)
    await rig.hear(2)
    await rig.stop()

    assert await asyncio.wait_for(go_to(rig, 3), WAIT_S) is True
    await rig.hear(1)

    assert rig.events.heard_at[-1] == ("Cap 3", 0)
    assert rig.ctl.state is S.READING


async def test_go_to_chapter_after_the_end_starts_reading_it(controller_rig):
    rig = story_rig(controller_rig, chapters=2, sentences=2)
    await rig.start(story_ref(1).locator)
    await rig.hear(4)
    await until(lambda: rig.ctl.state is S.COMPLETED)

    assert await asyncio.wait_for(go_to(rig, 1), WAIT_S) is True
    await rig.hear(1)

    assert rig.events.heard_at[-1] == ("Cap 1", 0)
    assert rig.ctl.state is S.READING


async def test_go_to_chapter_after_an_error_starts_reading_it(controller_rig):
    rig = story_rig(controller_rig, chapters=2)
    synthesize = rig.manager.synthesize

    async def broken(*args):
        raise RuntimeError("fallo interno")

    rig.manager.synthesize = broken
    await rig.start(story_ref(1).locator)
    await until(lambda: rig.ctl.state is S.ERROR)
    rig.manager.synthesize = synthesize

    assert await asyncio.wait_for(go_to(rig, 2), WAIT_S) is True
    await rig.hear(1)

    assert rig.events.heard_at[-1] == ("Cap 2", 0)
    assert rig.ctl.state is S.READING


async def test_go_to_chapter_while_waiting_for_the_network(controller_rig):
    """Sin red para el capítulo siguiente: elegir uno ya guardado lee sin esperar."""
    rig = story_rig(controller_rig, chapters=3, sentences=2, prefetch_ahead=0)
    await rig.start(story_ref(1).locator)
    rig.source.failures = Outage(PageUnreachable())  # el capítulo 2 no llega
    await rig.hear(2)
    await until(lambda: rig.ctl.state is S.WAITING_NETWORK)

    assert await asyncio.wait_for(go_to(rig, 1), WAIT_S) is True
    await rig.hear(1)

    assert rig.events.heard_at[-1] == ("Cap 1", 0)  # (luego vuelve a esperar al capítulo 2)


async def test_go_to_chapter_follows_the_address_not_the_position(controller_rig):
    """La lista pudo cambiar (un capítulo insertado): manda la dirección elegida."""
    rig = story_rig(controller_rig, chapters=3)
    await rig.start(story_ref(1).locator)
    await rig.hear(1)

    assert await asyncio.wait_for(go_to(rig, 3, index=0), WAIT_S) is True
    await rig.hear(1)

    assert rig.events.heard_at[-1] == ("Cap 3", 0)
    assert rig.stored().position.chapter_index == 3


async def test_go_to_chapter_of_another_document_does_nothing(controller_rig):
    rig = story_rig(controller_rig, chapters=3)
    await rig.start(story_ref(1).locator)
    await rig.hear(2)

    assert await asyncio.wait_for(
        rig.ctl.go_to_chapter("otro", story_ref(3).locator, 2), WAIT_S) is False
    assert await asyncio.wait_for(rig.ctl.go_to_chapter(STORY_ID, "", 2), WAIT_S) is False
    await rig.hear(1)

    assert rig.events.heard_at[-1] == ("Cap 1", 2)  # sigue donde iba


async def test_go_to_chapter_without_session_does_nothing(controller_rig):
    rig = story_rig(controller_rig, chapters=3)
    assert await asyncio.wait_for(go_to(rig, 1), WAIT_S) is False
    assert rig.ctl.state is S.IDLE


async def test_go_to_chapter_that_cannot_load_keeps_reading_and_warns(controller_rig):
    rig = story_rig(controller_rig, chapters=3, prefetch_ahead=0)
    await rig.start(story_ref(1).locator)
    await rig.hear(1)
    rig.source.failures = [DamagedDocument()]

    assert await asyncio.wait_for(go_to(rig, 3), WAIT_S) is False
    await rig.hear(1)

    assert rig.events.notices[-1][0] == "warn"
    assert rig.events.heard_at[-1] == ("Cap 1", 1)


# --- elegir capítulo sin red ----------------------------------------------------------------


async def test_pick_during_outage_while_reading_waits_visibly_then_jumps(controller_rig):
    rig = story_rig(controller_rig, chapters=3, sentences=20, prefetch_ahead=0)
    await rig.start(story_ref(1).locator)
    await rig.hear(1)
    rig.source.failures = Outage(PageUnreachable())

    pick = asyncio.create_task(go_to(rig, 3))
    await until(lambda: rig.ctl.state is S.WAITING_NETWORK)  # se ve «Esperando conexión…»
    assert not pick.done()
    rig.source.failures = []  # vuelve la red

    assert await asyncio.wait_for(pick, WAIT_S) is True
    await rig.hear(1)
    assert rig.events.heard_at[-1] == ("Cap 3", 0)
    assert rig.ctl.state is S.READING


@pytest.mark.parametrize("command", ["stop", "play"])
async def test_stop_or_play_cancel_a_pick_waiting_for_the_network(controller_rig, command):
    rig = story_rig(controller_rig, chapters=3, sentences=20, prefetch_ahead=0)
    await rig.start(story_ref(1).locator)
    await rig.hear(2)
    rig.source.failures = Outage(PageUnreachable())
    pick = asyncio.create_task(go_to(rig, 3))
    await until(lambda: rig.ctl.state is S.WAITING_NETWORK)

    if command == "stop":
        await rig.stop()
    else:
        await rig.play(STORY_ID)  # «Seguir leyendo» desde la última frase oída

    assert await asyncio.wait_for(pick, WAIT_S) is False
    if command == "stop":
        assert rig.ctl.state is S.STOPPED and lector_tasks() == []
    else:
        await rig.hear(1)
        assert rig.events.heard_at[-1][0] == "Cap 1"
    assert rig.stored().position.chapter_locator == story_ref(1).locator


async def completed_after_first_chapter(rig) -> None:
    await asyncio.wait_for(rig.ctl.set_continuous(False), WAIT_S)
    await rig.start(story_ref(1).locator)
    await rig.hear(2)
    await until(lambda: rig.ctl.state is S.COMPLETED)


async def test_pick_during_outage_after_the_end_waits_visibly_then_reads(controller_rig):
    rig = story_rig(controller_rig, chapters=3, sentences=2, prefetch_ahead=0)
    await completed_after_first_chapter(rig)
    rig.source.failures = Outage(PageUnreachable())

    # Empieza a leer en el acto: no se queda colgado esperando el capítulo.
    assert await asyncio.wait_for(go_to(rig, 3), WAIT_S) is True
    await until(lambda: rig.ctl.state is S.WAITING_NETWORK)
    rig.source.failures = []

    await until(lambda: rig.ctl.state is S.READING)
    await rig.hear(1)
    assert rig.events.heard_at[-1] == ("Cap 3", 0)


async def test_stop_and_continue_after_a_pick_waiting_for_the_network(controller_rig):
    rig = story_rig(controller_rig, chapters=3, sentences=2, prefetch_ahead=0)
    await completed_after_first_chapter(rig)
    rig.source.failures = Outage(PageUnreachable())
    assert await asyncio.wait_for(go_to(rig, 3), WAIT_S) is True
    await until(lambda: rig.ctl.state is S.WAITING_NETWORK)

    await rig.stop()

    assert rig.ctl.state is S.STOPPED and lector_tasks() == []
    assert rig.stored().position.chapter_locator == story_ref(1).locator  # lo último oído
    rig.source.failures = []
    await rig.play(STORY_ID)  # «Seguir leyendo»
    await until(lambda: rig.ctl.state in (S.READING, S.COMPLETED))


async def drain_while_a_pick_waits(rig):
    """Lectura continua apagada, sin red: se elige el capítulo 3 y el 1 termina de sonar."""
    await asyncio.wait_for(rig.ctl.set_continuous(False), WAIT_S)
    await rig.start(story_ref(1).locator)
    await rig.hear(1)
    rig.source.failures = Outage(PageUnreachable())
    pick = asyncio.create_task(go_to(rig, 3))
    await until(lambda: rig.ctl.state is S.WAITING_NETWORK)
    await rig.hear(3)  # se acaba la cola: el capítulo 1 ya sonó entero
    await settle()
    return pick


async def test_queue_draining_while_a_pick_waits_keeps_waiting_then_reads(controller_rig):
    rig = story_rig(controller_rig, chapters=3, sentences=4, prefetch_ahead=0)
    pick = await drain_while_a_pick_waits(rig)

    assert rig.ctl.state is S.WAITING_NETWORK  # sigue «Esperando conexión…», no «terminado»
    assert S.COMPLETED not in rig.events.states
    assert ("info", CHAPTER_FINISHED) not in rig.events.notices
    assert not pick.done()
    rig.settings.set("speed", 1.5)  # cambió mientras esperaba: el play() lo vuelve a leer
    rig.source.failures = []  # vuelve la red

    assert await asyncio.wait_for(pick, WAIT_S) is True
    await rig.hear(1)
    assert rig.events.heard_at[-1] == ("Cap 3", 0)
    assert rig.edge.calls[-1][2] == 1.5  # pasó por play(): configuración releída
    assert rig.ctl.state is S.READING
    assert S.PAUSED not in rig.events.states  # sin pausa del usuario no se inventa una


async def test_pause_during_a_held_pick_is_kept_when_it_lands(controller_rig):
    """Sin red, el capítulo 1 acabó y se pulsa Pausar: al volver la red, el 3 queda en pausa."""
    rig = story_rig(controller_rig, chapters=3, sentences=4, prefetch_ahead=0)
    pick = await drain_while_a_pick_waits(rig)
    await asyncio.wait_for(rig.ctl.pause(), WAIT_S)
    assert rig.ctl.state is S.PAUSED
    paused_at = len(rig.events.states)
    rig.source.failures = []  # vuelve la red

    assert await asyncio.wait_for(pick, WAIT_S) is True
    await until(lambda: rig.player.queued_count() > 0)
    await settle()

    assert rig.ctl.state is S.PAUSED
    assert rig.events.states[paused_at:] == []  # ni un instante «Cargando» ni «Leyendo»
    heard = len(rig.events.sentences)
    rig.out.pull(SAMPLES_PER_SENTENCE)  # en pausa la salida solo da silencio
    await settle()
    assert len(rig.events.sentences) == heard
    assert rig.stored().position.chapter_locator == story_ref(3).locator
    assert resume_sentence(rig.stored().position) == 0
    await asyncio.wait_for(rig.ctl.resume(), WAIT_S)
    await rig.hear(1)
    assert rig.events.heard_at[-1] == ("Cap 3", 0)


async def test_play_queued_behind_shutdown_does_not_start(controller_rig):
    rig = story_rig(controller_rig, chapters=2)
    await rig.start(story_ref(1).locator)
    await rig.hear(1)
    before = len(rig.events.states)

    async with rig.ctl._lock:  # una orden en curso: las dos siguientes esperan en fila
        closing = asyncio.create_task(rig.ctl.shutdown())
        await settle()
        playing = asyncio.create_task(rig.ctl.play(STORY_ID))
        await settle()
    await asyncio.wait_for(asyncio.gather(closing, playing), WAIT_S)
    await settle()

    assert rig.producers() == 0 and lector_tasks() == []
    assert rig.events.states[before:] == []  # ni «cargando» ni «leyendo» tras cerrar
    assert rig.ctl._active is False


async def test_pick_failing_after_the_queue_drained_ends_the_reading(controller_rig):
    rig = story_rig(controller_rig, chapters=3, sentences=4, prefetch_ahead=0)
    pick = await drain_while_a_pick_waits(rig)

    rig.source.failures = [DamagedDocument()]  # al volver a probar, el capítulo no sirve

    assert await asyncio.wait_for(pick, WAIT_S) is False
    await until(lambda: rig.ctl.state is S.COMPLETED)  # ahora sí: terminó el capítulo
    assert ("info", CHAPTER_FINISHED) in rig.events.notices
    assert any(level == "warn" for level, _ in rig.events.notices)  # y por qué no se saltó


async def test_newer_command_while_a_pick_waits_after_the_queue_drained(controller_rig):
    """Una orden más nueva gana al salto viejo, y la lectura termina como debía."""
    rig = story_rig(controller_rig, chapters=3, sentences=4, prefetch_ahead=0)
    pick = await drain_while_a_pick_waits(rig)
    await asyncio.wait_for(rig.ctl.next_sentence(), WAIT_S)  # ya no queda frase: vuelve a acabar
    await settle()
    assert rig.ctl.state is S.WAITING_NETWORK

    rig.source.failures = []

    assert await asyncio.wait_for(pick, WAIT_S) is False  # el salto quedó caduco
    await until(lambda: rig.ctl.state is S.COMPLETED)  # no se queda esperando para siempre
    assert rig.events.heard_at[-1][0] == "Cap 1"


async def test_missing_voice_pauses_at_once_even_while_a_pick_waits(controller_rig):
    """La pausa por falta de voz no espera al salto: el aviso sale ya, y el salto la respeta."""
    from lector.errors import EdgeVoiceUnavailable

    rig = story_rig(controller_rig, chapters=3, sentences=20, prefetch_ahead=0)
    await rig.start(story_ref(1).locator)
    await rig.fill()
    rig.source.failures = Outage(PageUnreachable())
    pick = asyncio.create_task(go_to(rig, 3))
    await until(lambda: rig.ctl.state is S.WAITING_NETWORK)

    async def no_voice(*args):
        raise EdgeVoiceUnavailable()

    rig.manager.synthesize = no_voice
    for _ in range(20):  # suena lo que ya estaba preparado; lo siguiente no tiene voz
        if rig.ctl.state is S.PAUSED or rig.player.queued_count() == 0:
            break
        await rig.hear(1)
    await until(lambda: rig.ctl.state is S.PAUSED)

    assert rig.events.notices[-1] == ("warn", EdgeVoiceUnavailable.user_message)
    assert not pick.done()
    rig.source.failures = []
    assert await asyncio.wait_for(pick, WAIT_S) is True
    await settle()
    assert rig.ctl.state is S.PAUSED  # el salto no quita la pausa
    assert rig.stored().position.chapter_locator == story_ref(3).locator


async def test_stop_after_the_queue_drained_while_a_pick_waits(controller_rig):
    rig = story_rig(controller_rig, chapters=3, sentences=4, prefetch_ahead=0)
    pick = await drain_while_a_pick_waits(rig)

    await rig.stop()

    assert await asyncio.wait_for(pick, WAIT_S) is False
    assert rig.ctl.state is S.STOPPED and lector_tasks() == []
    assert S.COMPLETED not in rig.events.states
    assert rig.stored().position.chapter_locator == story_ref(1).locator


# --- la lista de capítulos se renueva al volver a abrir ---------------------------------------


def growing_story(controller_rig):
    content = story(chapters=4)
    source = FakeSource(content)
    refs = [c.ref for c in content.values()]
    published = {"doc": replace(story_document(content), chapters=refs[:3])}

    async def opener(locator, fetcher, **kwargs):
        return source, published["doc"], source.chapters[locator]

    return controller_rig(opener=opener), published, refs


async def test_reopening_the_document_refreshes_its_chapter_list(controller_rig):
    """La obra publicó capítulos nuevos desde que se abrió: la sesión usa la lista nueva."""
    rig, published, refs = growing_story(controller_rig)
    await rig.start(story_ref(1).locator)
    await rig.hear(1)
    assert len(rig.events.documents[-1][0].chapters) == 3
    await rig.stop()

    published["doc"] = replace(published["doc"], chapters=refs)  # ya hay un capítulo 4
    await rig.start(story_ref(1).locator)
    await rig.hear(1)

    assert rig.events.documents[-1][0].chapters == refs
    assert await asyncio.wait_for(go_to(rig, 4), WAIT_S) is True
    await rig.hear(1)
    assert rig.events.heard_at[-1] == ("Cap 4", 0)


async def test_reopening_while_reading_refreshes_the_list_at_once(controller_rig):
    rig, published, refs = growing_story(controller_rig)
    await rig.start(story_ref(1).locator)
    await rig.hear(1)

    published["doc"] = replace(published["doc"], chapters=refs)
    await asyncio.wait_for(rig.ctl.open(story_ref(1).locator), WAIT_S)  # sin volver a leer
    await asyncio.wait_for(rig.ctl.next_chapter(), WAIT_S)

    assert rig.events.documents[-1][0].chapters == refs


async def test_reopening_without_a_list_keeps_the_known_one(controller_rig):
    rig, published, refs = growing_story(controller_rig)
    await rig.start(story_ref(1).locator)
    await rig.hear(1)

    published["doc"] = replace(published["doc"], chapters=[])  # esta vez la web no la dio
    await asyncio.wait_for(rig.ctl.open(story_ref(1).locator), WAIT_S)
    await asyncio.wait_for(rig.ctl.next_chapter(), WAIT_S)

    assert rig.events.documents[-1][0].chapters == refs[:3]
