"""Progreso confirmado y reanudación: lo guardado es lo que sonó, no lo generado."""
from __future__ import annotations

import pytest

from lector.errors import FileMovedOrChanged
from lector.models import Position
from lector.services.reading_controller import SessionState, resume_sentence
from tests.controller_rig import STORY_ID, settle, story_ref, story_rig, until
from tests.fakes import SAMPLES_PER_SENTENCE

S = SessionState


async def test_restart_resumes_at_last_confirmed_sentence(controller_rig, tmp_path):
    db = tmp_path / "lector.db"
    first = story_rig(controller_rig, db_path=db)
    await first.start(story_ref(1).locator)
    await first.hear(5)  # 0-4 enteras
    await first.hear(1, partial=True)  # la 5 a medias: confirma la 4

    record = await first.saved()
    assert record.position.sentence_index == 4
    sentence4 = first.source.chapters[story_ref(1).locator].sentences[4]
    assert record.position.char_offset == sentence4.end
    assert record.last_chunk_id == "novela:1:0:4-6"

    # Cierre inesperado: sin shutdown(). Una sesión nueva sobre la misma base de datos.
    second = story_rig(controller_rig, db_path=db)
    doc, _, saved = await second.ctl.open(story_ref(1).locator)
    assert resume_sentence(saved.position) == 5
    await second.ctl.play(doc.doc_id, None)
    await second.hear(1)

    assert second.events.heard == [5]
    assert second.edge.calls[0][0] == "novela:1:0:5-5"


async def test_generated_vs_played_distinction(controller_rig):
    rig = story_rig(controller_rig, chapters=1)
    await rig.start(story_ref(1).locator)
    await rig.fill()  # [0] [1-3] [4-6] generados y encolados
    assert len(rig.edge.calls) >= 3

    await rig.hear(1)  # solo suena el primero

    record = await rig.saved()
    assert record.position.sentence_index == 0
    assert resume_sentence(record.position) == 1
    assert record.last_chunk_id == "novela:1:0:0-0"


async def test_pause_resume_keeps_progress(controller_rig):
    rig = story_rig(controller_rig, chapters=1)
    await rig.start(story_ref(1).locator)
    await rig.hear(2)  # la 1 sonó entera pero la 2 aún no empezó

    await rig.ctl.pause()
    assert rig.ctl.state is S.PAUSED
    record = rig.repos.progress.get(STORY_ID)  # guardado al pausar
    assert record.position.sentence_index == 0
    assert record.session_state == "paused"
    silence = rig.out.pull(SAMPLES_PER_SENTENCE)
    await settle()
    assert not silence.any()
    assert rig.events.heard == [0, 1]

    await rig.ctl.resume()
    assert rig.ctl.state is S.READING
    await rig.hear(1)

    assert rig.events.heard == [0, 1, 2]
    record = await rig.saved()
    assert record.position.sentence_index == 1


async def test_play_with_explicit_start(controller_rig):
    rig = story_rig(controller_rig)
    doc, _, _ = await rig.ctl.open(story_ref(1).locator)
    start = Position(2, story_ref(2).locator, 0, 3, 0, None)
    await rig.ctl.play(doc.doc_id, start)
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
    await first.ctl.stop()

    second = story_rig(controller_rig, db_path=db, chapters=2, sentences=2)
    doc, _, _ = await second.ctl.open(story_ref(1).locator)
    await second.ctl.play(doc.doc_id, None)
    await second.hear(1)
    assert second.events.heard_at == [("Cap 2", 0)]


async def test_file_moved_raises(controller_rig, make_pdf, tmp_path):
    path = make_pdf(pages=[[("Una frase de prueba. Otra frase más.", 11)]])
    db = tmp_path / "lector.db"
    rig = controller_rig(db_path=db)
    doc, _, _ = await rig.ctl.open(str(path))

    path.rename(tmp_path / "movido.pdf")
    later = controller_rig(db_path=db)  # otra sesión: el documento sale del historial
    with pytest.raises(FileMovedOrChanged) as moved:
        await later.ctl.play(doc.doc_id, None)
    assert moved.value.doc_id == doc.doc_id
    assert later.ctl.state is S.IDLE

    make_pdf(pages=[[("Un contenido distinto. Con otra frase.", 11)]])  # mismo nombre
    with pytest.raises(FileMovedOrChanged):
        await later.ctl.play(doc.doc_id, None)


async def test_file_unchanged_resumes_from_history(controller_rig, make_pdf, tmp_path):
    path = make_pdf(pages=[[("Una frase de prueba. Otra frase más.", 11)]])
    db = tmp_path / "lector.db"
    rig = controller_rig(db_path=db)
    doc, _, _ = await rig.ctl.open(str(path))

    later = controller_rig(db_path=db)
    await later.ctl.play(doc.doc_id, None)
    await later.hear(1)
    await until(lambda: later.ctl.state is S.READING)
    assert later.events.sentences[0][1] == "Una frase de prueba."
