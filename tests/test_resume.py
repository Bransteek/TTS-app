"""Progreso confirmado y reanudación: lo guardado es lo que sonó, no lo generado."""
from __future__ import annotations

import pytest

import asyncio

from lector.errors import FileMovedOrChanged, PageUnreachable
from lector.models import Position
from lector.services.reading_controller import SessionState, resume_sentence
from tests.controller_rig import STORY_ID, WAIT_S, Outage, settle, story_ref, story_rig, until
from tests.fakes import SAMPLES_PER_SENTENCE

S = SessionState


async def test_restart_resumes_at_last_confirmed_sentence(controller_rig, tmp_path):
    db = tmp_path / "lector.db"
    first = story_rig(controller_rig, db_path=db)
    await first.start(story_ref(1).locator)
    await first.hear(5)  # 0-4 enteras
    await first.hear(1, partial=True)  # la 5 a medias: confirma la 4 (guardado diferido)
    assert first.stored().position.sentence_index == 3
    first.save_clock.advance(2.1)  # pasan los 2 s: el diferido escribe sin forzarlo
    await first.written(lambda r: r.position.sentence_index == 4)

    record = first.stored()
    sentence4 = first.source.chapters[story_ref(1).locator].sentences[4]
    assert record.position.char_offset == sentence4.end
    assert record.last_chunk_id == "novela:1:0:4-6"

    # Cierre inesperado: sin shutdown(). Una sesión nueva sobre la misma base de datos.
    second = story_rig(controller_rig, db_path=db)
    doc, _, saved = await asyncio.wait_for(second.ctl.open(story_ref(1).locator), WAIT_S)
    assert resume_sentence(saved.position) == 5
    await second.play(doc.doc_id)
    await second.hear(1)

    assert second.events.heard == [5]
    assert second.edge.calls[0][0] == "novela:1:0:5-5"


async def test_generated_vs_played_distinction(controller_rig):
    rig = story_rig(controller_rig, chapters=1)
    await rig.start(story_ref(1).locator)
    await rig.fill()  # [0] [1-3] [4-6] generados y encolados
    assert len(rig.edge.calls) >= 3

    await rig.hear(1)  # solo suena el primero: guardado forzado al terminar el fragmento

    await rig.written(lambda r: r.position.sentence_index == 0)
    record = rig.stored()
    assert record.position.sentence_index == 0
    assert resume_sentence(record.position) == 1
    assert record.last_chunk_id == "novela:1:0:0-0"


async def test_pause_resume_keeps_progress(controller_rig):
    rig = story_rig(controller_rig, chapters=1)
    await rig.start(story_ref(1).locator)
    await rig.hear(2)  # la 1 sonó entera pero la 2 aún no empezó

    await asyncio.wait_for(rig.ctl.pause(), WAIT_S)
    assert rig.ctl.state is S.PAUSED
    record = rig.repos.progress.get(STORY_ID)  # guardado al pausar
    assert record.position.sentence_index == 0
    assert record.session_state == "paused"
    silence = rig.out.pull(SAMPLES_PER_SENTENCE)
    await settle()
    assert not silence.any()
    assert rig.events.heard == [0, 1]

    await asyncio.wait_for(rig.ctl.resume(), WAIT_S)
    assert rig.ctl.state is S.READING
    await rig.hear(1)

    assert rig.events.heard == [0, 1, 2]
    record = await rig.saved()
    assert record.position.sentence_index == 1


async def test_play_with_explicit_start(controller_rig):
    rig = story_rig(controller_rig)
    doc, _, _ = await asyncio.wait_for(rig.ctl.open(story_ref(1).locator), WAIT_S)
    start = Position(2, story_ref(2).locator, 0, 3, 0, None)  # la primera frase que se lee
    await rig.play(doc.doc_id, start)
    await rig.hear(1)

    assert rig.events.heard_at == [("Cap 2", 3)]
    record = await rig.saved()
    assert record.position.chapter_locator == story_ref(2).locator
    assert resume_sentence(record.position) == 4


async def test_resume_at_chapter_end_moves_to_next_chapter(controller_rig, tmp_path):
    db = tmp_path / "lector.db"
    first = story_rig(controller_rig, db_path=db, chapters=2, sentences=2)
    await first.start(story_ref(1).locator)
    await first.hear(2)
    await first.stop()

    second = story_rig(controller_rig, db_path=db, chapters=2, sentences=2)
    doc, _, _ = await asyncio.wait_for(second.ctl.open(story_ref(1).locator), WAIT_S)
    await second.play(doc.doc_id)
    await second.hear(1)
    assert second.events.heard_at == [("Cap 2", 0)]


async def test_file_moved_raises(controller_rig, make_pdf, tmp_path):
    path = make_pdf(pages=[[("Una frase de prueba. Otra frase más.", 11)]])
    db = tmp_path / "lector.db"
    rig = controller_rig(db_path=db)
    doc, _, _ = await asyncio.wait_for(rig.ctl.open(str(path)), WAIT_S)

    path.rename(tmp_path / "movido.pdf")
    later = controller_rig(db_path=db)  # otra sesión: el documento sale del historial
    with pytest.raises(FileMovedOrChanged) as moved:
        await later.play(doc.doc_id)
    assert moved.value.doc_id == doc.doc_id
    assert later.ctl.state is S.IDLE

    make_pdf(pages=[[("Un contenido distinto. Con otra frase.", 11)]])  # mismo nombre
    with pytest.raises(FileMovedOrChanged):
        await later.play(doc.doc_id)


async def test_file_unchanged_resumes_from_history(controller_rig, make_pdf, tmp_path):
    path = make_pdf(pages=[[("Una frase de prueba. Otra frase más.", 11)]])
    db = tmp_path / "lector.db"
    rig = controller_rig(db_path=db)
    doc, _, _ = await asyncio.wait_for(rig.ctl.open(str(path)), WAIT_S)

    later = controller_rig(db_path=db)
    await later.play(doc.doc_id)
    await later.hear(1)
    await until(lambda: later.ctl.state is S.READING)
    assert later.events.sentences[0][1] == "Una frase de prueba."


async def test_web_novel_resumes_offline_from_history(controller_rig, tmp_path):
    db = tmp_path / "lector.db"
    first = story_rig(controller_rig, db_path=db)
    await first.start(story_ref(1).locator)
    await first.hear(3)
    await first.stop()

    later = story_rig(controller_rig, db_path=db)  # otro día, sin conexión
    later.network["online"] = False
    later.source.failures = Outage(PageUnreachable())
    await later.play(STORY_ID)  # desde el historial, sin vista previa
    await later.hear(1)

    assert later.events.heard_at == [("Cap 1", 2)]  # el texto guardado basta
    assert later.ctl.state is S.READING


async def test_offline_resume_without_stored_text_waits_for_network(controller_rig, tmp_path):
    db = tmp_path / "lector.db"
    first = story_rig(controller_rig, db_path=db, prefetch_ahead=0)
    await first.start(story_ref(1).locator)  # solo el capítulo 1 queda guardado
    await first.stop()

    later = story_rig(controller_rig, db_path=db, prefetch_ahead=0)
    later.network["online"] = False
    await later.play(STORY_ID, Position(2, story_ref(2).locator, 0, 0, 0, None))
    await until(lambda: later.ctl.state is S.WAITING_NETWORK)  # espera, no falla

    later.network["online"] = True  # vuelve la red
    await later.hear(1)
    assert later.events.heard_at == [("Cap 2", 0)]


async def test_deferred_open_refreshes_document_from_site(controller_rig, tmp_path):
    db = tmp_path / "lector.db"
    first = story_rig(controller_rig, db_path=db, prefetch_ahead=0)
    await first.start(story_ref(1).locator)  # solo el capítulo 1 queda guardado
    await first.stop()

    later = story_rig(controller_rig, db_path=db, prefetch_ahead=0)
    later.document.title = "Novela (revisada)"  # el sitio cambió el título
    await later.play(STORY_ID, Position(2, story_ref(2).locator, 0, 0, 0, None))
    await later.hear(1)  # el capítulo 2 no estaba guardado: se abrió la fuente real
    await later.stop()

    assert later.stored().title == "Novela (revisada)"
    assert later.stored().position.chapter_locator == story_ref(2).locator


async def test_chosen_next_chapter_is_remembered(controller_rig, tmp_path):
    db = tmp_path / "lector.db"

    def ambiguous(rig) -> None:
        first = rig.source.chapters[story_ref(1).locator]
        first.next_ref = None
        first.next_candidates = [story_ref(2), story_ref(3)]

    rig = story_rig(controller_rig, db_path=db, chapters=3, sentences=2)
    ambiguous(rig)
    await rig.start(story_ref(1).locator)
    await rig.hear(2)
    await until(lambda: rig.ctl.state is S.NEEDS_ATTENTION)
    await asyncio.wait_for(rig.ctl.choose_next(story_ref(3)), WAIT_S)
    stored = rig.repos.chapters.get_text(STORY_ID, story_ref(1).locator)
    assert stored.next_ref.locator == story_ref(3).locator
    await rig.stop()

    later = story_rig(controller_rig, db_path=db, chapters=3, sentences=2)
    ambiguous(later)  # la página sigue siendo ambigua
    await later.play(STORY_ID, Position(1, story_ref(1).locator, 0, 0, 0, None))
    await later.hear(3)

    assert later.events.heard_at == [("Cap 1", 0), ("Cap 1", 1), ("Cap 3", 0)]
    assert later.events.choices == []  # no vuelve a preguntar


async def test_deferred_open_from_history_applies_prefix_overrides(controller_rig, tmp_path):
    """La corrección del prefijo también vale al reabrir la web desde el historial."""
    db = tmp_path / "lector.db"
    first = story_rig(controller_rig, db_path=db, prefetch_ahead=0)
    await first.start(story_ref(1).locator)  # solo el capítulo 1 queda guardado
    await first.stop()

    later = story_rig(controller_rig, db_path=db, prefetch_ahead=0)
    later.settings.set("work_prefix_overrides", {"novela.example": ["/obra/"]})
    await later.play(STORY_ID, Position(2, story_ref(2).locator, 0, 0, 0, None))
    await later.hear(1)  # el capítulo 2 no estaba guardado: se abrió la fuente real

    assert later.network["opens"] == [
        (story_ref(2).locator, {"prefix_overrides": {"novela.example": ["/obra/"]}}),
    ]
