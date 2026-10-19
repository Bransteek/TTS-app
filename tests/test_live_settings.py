"""Ajustes que llegan a la sesión en marcha: guardar o no el texto, y tamaño de fragmentos."""
from __future__ import annotations

import asyncio
import threading

import pytest

from lector.persistence.database import open_database
from lector.persistence.repositories import ChapterRepo
from lector.services.navigator import ChapterNavigator
from lector.services.reading_controller import SessionState as S
from tests.controller_rig import WAIT_S, story, story_document, story_ref, story_rig, until
from tests.fakes import FakeSource


def with_text(rig) -> int:
    return rig.conn.execute(
        "SELECT COUNT(*) FROM chapters WHERE text IS NOT NULL OR paragraphs_json IS NOT NULL"
    ).fetchone()[0]


def rows(rig) -> set[str]:
    return {r[0] for r in rig.conn.execute("SELECT locator FROM chapters").fetchall()}


async def turn_off_and_purge(rig) -> None:
    """Lo que hace «Borrar el texto guardado»: primero el controlador, luego el borrado."""
    await asyncio.wait_for(rig.ctl.set_store_text(False), WAIT_S)
    await asyncio.to_thread(rig.repos.chapters.purge_all_text)


@pytest.mark.parametrize("prefetch_ahead", [0, 2])
async def test_store_text_off_reaches_live_continuous_session(controller_rig, prefetch_ahead):
    rig = story_rig(controller_rig, chapters=6, sentences=2, prefetch_ahead=prefetch_ahead)
    await rig.start(story_ref(1).locator)
    await until(lambda: with_text(rig) >= 1 + prefetch_ahead)

    await turn_off_and_purge(rig)

    assert with_text(rig) == 0
    assert rig.settings.get("store_text") is False
    # Lectura continua: siguen llegando capítulos (cargados al seguir, o precargados).
    await rig.hear(6)
    later = {story_ref(i).locator for i in (4, 5)}
    await until(lambda: later <= rows(rig))
    assert rig.ctl.state is S.READING
    assert with_text(rig) == 0

    # Una vista previa nueva tampoco guarda el texto.
    await asyncio.wait_for(rig.ctl.open(story_ref(6).locator), WAIT_S)
    assert story_ref(6).locator in rows(rig)
    assert with_text(rig) == 0


async def test_store_text_off_reaches_remembered_choice(controller_rig):
    rig = story_rig(controller_rig, chapters=3, sentences=2)
    first = rig.source.chapters[story_ref(1).locator]
    first.next_ref = None
    first.next_candidates = [story_ref(2), story_ref(3)]
    await rig.start(story_ref(1).locator)
    await turn_off_and_purge(rig)

    await rig.hear(2)
    await until(lambda: rig.ctl.state is S.NEEDS_ATTENTION)
    await asyncio.wait_for(rig.ctl.choose_next(story_ref(3)), WAIT_S)

    row = rig.conn.execute(
        "SELECT next_locator, text, paragraphs_json FROM chapters WHERE locator=?",
        (story_ref(1).locator,),
    ).fetchone()
    assert row == (story_ref(3).locator, None, None)  # se recuerda la elección, sin el texto
    assert with_text(rig) == 0


async def test_play_applies_store_text_to_existing_session(controller_rig):
    rig = story_rig(controller_rig, chapters=4, sentences=2, prefetch_ahead=0)
    await rig.start(story_ref(1).locator)
    await rig.stop()
    rig.settings.set("store_text", False)  # cambiado por otra vía mientras tanto
    await asyncio.to_thread(rig.repos.chapters.purge_all_text)
    await rig.play()
    await rig.hear(3)
    await until(lambda: story_ref(2).locator in rows(rig))
    assert with_text(rig) == 0


class BlockingChapters(ChapterRepo):
    """Repositorio cuyo guardado se queda esperando en su hilo hasta que se suelte."""

    def __init__(self, conn):
        super().__init__(conn)
        self.release = threading.Event()
        self.saving = threading.Event()

    def save(self, doc_id, chapter, store_text):
        self.saving.set()
        assert self.release.wait(WAIT_S)
        super().save(doc_id, chapter, store_text)


async def test_navigator_store_text_off_waits_for_write_in_flight(tmp_path):
    conn = open_database(tmp_path / "lector.db")
    try:
        content = story(2)
        repo = BlockingChapters(conn)
        nav = ChapterNavigator(FakeSource(content), story_document(content), repo, True)
        loading = asyncio.ensure_future(nav.get(story_ref(1)))
        await until(repo.saving.is_set)

        turning_off = asyncio.ensure_future(nav.set_store_text(False))
        await asyncio.sleep(0.05)
        assert not turning_off.done()  # aún se está escribiendo texto con el valor anterior

        repo.release.set()
        await asyncio.wait_for(turning_off, WAIT_S)
        await asyncio.wait_for(loading, WAIT_S)
        # Al volver, la escritura con texto ya terminó: un borrado posterior es definitivo.
        repo.purge_all_text()
        await nav.close()
        assert conn.execute("SELECT COUNT(*) FROM chapters WHERE text IS NOT NULL").fetchone()[0] == 0
    finally:
        conn.close()


async def test_chunk_limits_apply_without_play(controller_rig):
    rig = story_rig(controller_rig, chapters=2, sentences=4)
    await rig.start(story_ref(1).locator)
    await asyncio.wait_for(rig.ctl.set_chunk_limits(450, 1, 120), WAIT_S)
    assert rig.ctl._limits("edge") == (450, 1)
    assert rig.ctl._limits("piper") == (120, 1)
    assert (rig.settings.get("edge_max_chars"), rig.settings.get("edge_max_sentences"),
            rig.settings.get("piper_max_chars")) == (450, 1, 120)
