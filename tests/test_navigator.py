from __future__ import annotations

import asyncio

import pytest

from lector.content.segmenter import build_chapter_text
from lector.errors import (
    AmbiguousNextChapter, ChapterLoopDetected, DamagedDocument, PageUnreachable,
)
from lector.models import Chapter, ChapterRef, Document, SourceKind
from lector.persistence.database import open_database
from lector.persistence.repositories import ChapterRepo
from lector.services.navigator import ChapterNavigator
from tests.fakes import FakeClock, FakeSource


def ref(i: int) -> ChapterRef:
    return ChapterRef(i, f"Cap {i}", f"https://x/{i}")


def chapter(i: int, *, nxt: int | None = None, prev: int | None = None, candidates=()) -> Chapter:
    text, paras, sents = build_chapter_text([(f"Texto del capítulo {i}.", None)])
    return Chapter(ref(i), f"Cap {i}", text, paras, sents,
                   ref(nxt) if nxt is not None else None,
                   ref(prev) if prev is not None else None,
                   [ref(c) for c in candidates])


def chain(n: int, *, loop_to: int | None = None) -> dict[str, Chapter]:
    out = {}
    for i in range(1, n + 1):
        nxt = i + 1 if i < n else loop_to
        out[ref(i).locator] = chapter(i, nxt=nxt, prev=i - 1 if i > 1 else None)
    return out


def document(kind: SourceKind = SourceKind.URL) -> Document:
    return Document("doc1", kind, "https://x/1", "Novela", "https://x/1", "https://x/", None, [])


@pytest.fixture
def conn(tmp_path):
    c = open_database(tmp_path / "lector.db")
    yield c
    c.close()


@pytest.fixture
def repo(conn):
    return ChapterRepo(conn)


def make_nav(source, repo, *, kind=SourceKind.URL, store_text=True, clock=None, **kw):
    clock = clock or FakeClock()
    return ChapterNavigator(source, document(kind), repo, store_text, sleep=clock.sleep, **kw)


async def settle(nav):
    await nav._prefetch_task


async def test_prefetches_two_ahead(repo):
    source = FakeSource(chain(5))
    nav = make_nav(source, repo)
    first = await nav.get(ref(1))
    nav.start_prefetch(first)
    await settle(nav)
    assert source.loads == ["https://x/1", "https://x/2", "https://x/3"]
    await nav.next_of(first)
    assert source.loads.count("https://x/2") == 1  # servido desde la caché
    await nav.close()


async def test_prefetch_cache_is_bounded(repo):
    source = FakeSource(chain(20))
    nav = make_nav(source, repo)
    current = await nav.get(ref(1))
    for _ in range(19):
        nav.start_prefetch(current)
        current = await nav.next_of(current)
        assert len(nav._cache) <= 4
    await nav.close()
    assert current.ref == ref(20)
    assert len(nav._cache) <= 4


async def test_new_prefetch_cancels_previous(repo):
    nav = make_nav(FakeSource(chain(5)), repo)
    first = await nav.get(ref(1))
    nav.start_prefetch(first)
    old = nav._prefetch_task
    nav.start_prefetch(first)
    await settle(nav)
    assert old.cancelled() or old.done()
    await nav.close()


async def test_loop_detected(repo):
    nav = make_nav(FakeSource(chain(3, loop_to=1)), repo)
    current = await nav.get(ref(1))
    nav.mark_visited(current.ref)
    for _ in range(2):
        current = await nav.next_of(current)
        nav.mark_visited(current.ref)
    with pytest.raises(ChapterLoopDetected):
        await nav.next_of(current)


async def test_end_of_novel_returns_none_for_files(repo):
    nav = make_nav(FakeSource(chain(1)), repo, kind=SourceKind.EPUB)
    last = await nav.get(ref(1))
    assert await nav.next_of(last) is None


async def test_end_of_novel_returns_none_for_web(repo):
    source = FakeSource(chain(1))
    nav = make_nav(source, repo)
    last = await nav.get(ref(1))
    assert await nav.next_of(last) is None
    assert source.loads == ["https://x/1", "https://x/1"]  # volvió a mirar


async def test_ambiguous_raises_with_candidates(repo):
    source = FakeSource({ref(1).locator: chapter(1, candidates=(2, 3))})
    nav = make_nav(source, repo)
    first = await nav.get(ref(1))
    with pytest.raises(AmbiguousNextChapter) as ei:
        await nav.next_of(first)
    assert ei.value.candidates == [ref(2), ref(3)]


async def test_network_down_waits_forever_then_returns(repo):
    source = FakeSource(chain(2))
    clock = FakeClock()
    nav = make_nav(source, repo, clock=clock)
    first = await nav.get(ref(1))
    source.failures = [PageUnreachable() for _ in range(10)]
    events = []
    nav.on_waiting = events.append
    second = await nav.next_of(first)
    assert second.ref == ref(2)
    assert events == [True, False]
    assert len(clock.sleeps) == 10
    assert max(clock.sleeps) == 60 and clock.sleeps[:3] == [1, 2, 4]
    assert clock.sleeps == sorted(clock.sleeps)


async def test_non_recoverable_propagates_immediately(repo):
    source = FakeSource(chain(2), failures=[DamagedDocument()])
    clock = FakeClock()
    nav = make_nav(source, repo, clock=clock)
    with pytest.raises(DamagedDocument):
        await nav.get(ref(1))
    assert clock.sleeps == []


async def test_waiting_callback_that_raises_does_not_break(repo):
    source = FakeSource(chain(1), failures=[PageUnreachable()])
    nav = make_nav(source, repo)

    def boom(_):
        raise RuntimeError("ui")

    nav.on_waiting = boom
    assert (await nav.get(ref(1))).ref == ref(1)


async def test_cancellation_propagates_while_waiting(repo):
    source = FakeSource(chain(1), failures=[PageUnreachable()] * 1000)

    async def hang(_):
        await asyncio.Event().wait()

    nav = ChapterNavigator(source, document(), repo, True, sleep=hang)
    task = asyncio.create_task(nav.get(ref(1)))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_prefetch_is_silent_and_retries_network(repo):
    source = FakeSource(chain(3))
    clock = FakeClock()
    nav = make_nav(source, repo, clock=clock)
    first = await nav.get(ref(1))
    source.failures = [PageUnreachable()] * 3
    events = []
    nav.on_waiting = events.append
    nav.start_prefetch(first)
    await settle(nav)
    assert events == []
    assert len(clock.sleeps) == 3
    assert source.loads[-2:] == ["https://x/2", "https://x/3"]
    await nav.close()


async def test_prefetch_stores_non_recoverable_and_foreground_surfaces_it(repo):
    source = FakeSource(chain(3))
    nav = make_nav(source, repo)
    first = await nav.get(ref(1))
    source.failures = [DamagedDocument()]
    nav.start_prefetch(first)
    await settle(nav)  # no lanza
    assert len(nav.prefetch_errors) == 1
    source.failures = [DamagedDocument()]
    with pytest.raises(DamagedDocument):
        await nav.next_of(first)
    await nav.close()


async def test_store_text_false_not_persisted(repo, conn):
    nav = make_nav(FakeSource(chain(2)), repo, store_text=False)
    first = await nav.get(ref(1))
    await nav.get(ref(2))
    rows = conn.execute("SELECT text, paragraphs_json, next_locator FROM chapters ORDER BY idx").fetchall()
    assert rows == [(None, None, "https://x/2"), (None, None, None)]
    assert repo.get_text("doc1", first.ref.locator) is None


async def test_get_prefers_repo_over_source(repo):
    source = FakeSource(chain(2))
    await make_nav(source, repo).get(ref(1))
    source.loads.clear()
    got = await make_nav(source, repo).get(ref(1))  # navegador nuevo: caché vacía
    assert got.text == "Texto del capítulo 1."
    assert source.loads == []


async def test_prev_of(repo):
    nav = make_nav(FakeSource(chain(3)), repo)
    second = await nav.get(ref(2))
    assert (await nav.prev_of(second)).ref == ref(1)
    assert await nav.prev_of(await nav.get(ref(1))) is None


async def test_reopen_rechecks_next_for_last_chapter(repo):
    source = FakeSource(chain(1))
    nav = make_nav(source, repo)
    last = await nav.get(ref(1))
    assert await nav.next_of(last) is None
    # Se publica un capítulo nuevo.
    source.chapters[ref(1).locator] = chapter(1, nxt=2)
    source.chapters[ref(2).locator] = chapter(2, prev=1)
    # Reabrir: navegador nuevo; el capítulo viejo sale del repositorio sin «siguiente».
    reopened = make_nav(source, repo)
    stale = await reopened.get(ref(1))
    assert stale.next_ref is None
    source.loads.clear()
    nxt = await reopened.next_of(stale)
    assert nxt.ref == ref(2)
    assert source.loads[0] == "https://x/1"  # releyó el último capítulo de la red
    assert reopened._cache[ref(1).locator].next_ref == ref(2)  # la copia nueva reemplaza a la vieja


async def test_cached_next_not_refetched_for_web(repo):
    source = FakeSource(chain(2))
    nav = make_nav(source, repo)
    first = await nav.get(ref(1))
    await nav.next_of(first)
    assert source.loads.count("https://x/1") == 1


async def test_close_cancels_prefetch(repo):
    source = FakeSource(chain(3), failures=[PageUnreachable()] * 1000)

    async def hang(_):
        await asyncio.Event().wait()

    nav = ChapterNavigator(source, document(), repo, True, sleep=hang)
    nav.start_prefetch(chapter(0, nxt=1))
    await asyncio.sleep(0.01)
    await nav.close()
    assert nav._prefetch_task is None
