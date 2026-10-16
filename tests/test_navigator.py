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
    gate = asyncio.Event()
    gate.set()
    nav = make_nav(FakeSource(chain(5), gate=gate), repo)
    first = await nav.get(ref(1))
    gate.clear()  # la fuente se queda colgada
    nav.start_prefetch(first)
    await asyncio.sleep(0.01)
    old = nav._prefetch_task
    nav.start_prefetch(first)
    await asyncio.sleep(0.01)
    assert old is not nav._prefetch_task
    assert old.cancelled()
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


@pytest.mark.parametrize("kind", [SourceKind.EPUB, SourceKind.PDF])
async def test_end_of_novel_returns_none_for_files(repo, kind):
    source = FakeSource(chain(1))
    nav = make_nav(source, repo, kind=kind)
    last = await nav.get(ref(1))
    assert await nav.next_of(last) is None
    assert source.loads == ["https://x/1"]  # sin recarga


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


async def test_reopen_rechecks_next_for_last_chapter(repo, conn):
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
    row = conn.execute("SELECT next_locator FROM chapters WHERE locator='https://x/1'").fetchone()
    assert row == ("https://x/2",)  # el repositorio guarda el «siguiente» nuevo


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
    old = nav._prefetch_task
    await nav.close()
    assert nav._prefetch_task is None
    assert old.done() and old.cancelled()


async def test_prefetch_and_foreground_load_chapter_once(repo):
    gate = asyncio.Event()
    gate.set()
    source = FakeSource(chain(3), gate=gate)
    nav = make_nav(source, repo)
    first = await nav.get(ref(1))
    gate.clear()
    nav.start_prefetch(first)
    await asyncio.sleep(0.01)  # la precarga ya está pidiendo el capítulo 2
    fg = asyncio.create_task(nav.next_of(first))
    await asyncio.sleep(0.01)
    gate.set()
    got = await fg
    await settle(nav)
    assert got.ref == ref(2)
    assert source.loads.count("https://x/2") == 1
    await nav.close()


async def test_cancel_while_load_blocked_propagates_and_leaves_nothing(repo):
    gate = asyncio.Event()  # nunca se activa
    nav = make_nav(FakeSource(chain(2), gate=gate), repo)
    task = asyncio.create_task(nav.get(ref(1)))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert nav._locks == {}
    assert asyncio.all_tasks() == {asyncio.current_task()}


async def test_going_back_does_not_raise_false_loop(repo):
    nav = make_nav(FakeSource(chain(3)), repo)
    one = await nav.get(ref(1))
    nav.mark_visited(one.ref)
    two = await nav.next_of(one)
    nav.mark_visited(two.ref)
    back = await nav.prev_of(two)
    assert back.ref == ref(1)
    assert nav.visited == {ref(1).locator}
    again = await nav.next_of(back)
    assert again.ref == ref(2)


async def test_reset_visited(repo):
    nav = make_nav(FakeSource(chain(2)), repo)
    nav.mark_visited(ref(1))
    nav.mark_visited(ref(2))
    nav.reset_visited(ref(2))
    assert nav.visited == {ref(2).locator}
    nav.reset_visited()
    assert nav.visited == set()


async def test_waits_and_resumes_when_next_chapter_arrives_late(repo):
    source = FakeSource(chain(3))
    clock = FakeClock()
    nav = make_nav(source, repo, clock=clock)
    first = await nav.get(ref(1))
    source.failures = [PageUnreachable() for _ in range(6)]
    events = []
    nav.on_waiting = events.append
    nav.start_prefetch(first)  # la precarga de N+1 está en marcha cuando cae la red
    got = await nav.next_of(first)
    await settle(nav)
    assert got.ref == ref(2)
    assert got.sentences[0].text == "Texto del capítulo 2."
    assert events == [True, False]  # la precarga no avisa
    assert source.failures == []
    await nav.close()


async def test_redirecting_cycle_is_detected(repo):
    # /2 y /1?x redirigen: el capítulo final de /1?x es /1, que ya se leyó.
    chapters = {
        "https://x/1": chapter(1, nxt=2),
        "https://x/2": chapter(2, prev=1),
    }
    chapters["https://x/2"].next_ref = ChapterRef(1, "Cap 1", "https://x/1?x")
    chapters["https://x/1?x"] = chapters["https://x/1"]
    nav = make_nav(FakeSource(chapters), repo)
    one = await nav.get(ref(1))
    nav.mark_visited(one.ref)
    two = await nav.next_of(one)
    nav.mark_visited(two.ref)
    with pytest.raises(ChapterLoopDetected):
        await nav.next_of(two)


async def test_redirected_chapter_is_cached_under_requested_locator(repo):
    final = chapter(1)
    source = FakeSource({"https://x/1?x": final})
    nav = make_nav(source, repo)
    requested = ChapterRef(1, "Cap 1", "https://x/1?x")
    assert (await nav.get(requested)) is final
    assert (await nav.get(requested)) is final
    assert (await nav.get(final.ref)) is final
    assert source.loads == ["https://x/1?x"]


@pytest.mark.parametrize("kw", [{"wait_max_s": 0}, {"wait_max_s": -1}, {"prefetch_ahead": -1}])
def test_invalid_arguments_raise(repo, kw):
    with pytest.raises(ValueError):
        make_nav(FakeSource(), repo, **kw)


async def test_repo_read_failure_falls_back_to_source(repo, monkeypatch, caplog):
    def broken(*a):
        raise RuntimeError("disco")

    monkeypatch.setattr(repo, "get_text", broken)
    source = FakeSource(chain(1))
    nav = make_nav(source, repo)
    with caplog.at_level("WARNING"):
        assert (await nav.get(ref(1))).ref == ref(1)
    assert source.loads == ["https://x/1"]
    assert "disco" in caplog.text


async def test_recheck_non_recoverable_returns_none(repo, caplog):
    source = FakeSource(chain(1))
    nav = make_nav(source, repo)
    last = await nav.get(ref(1))
    source.failures = [DamagedDocument()]
    with caplog.at_level("WARNING"):
        assert await nav.next_of(last) is None
    assert caplog.records


async def test_recheck_recoverable_still_waits(repo):
    source = FakeSource(chain(1))
    clock = FakeClock()
    nav = make_nav(source, repo, clock=clock)
    last = await nav.get(ref(1))
    source.failures = [PageUnreachable()] * 2
    assert await nav.next_of(last) is None
    assert len(clock.sleeps) == 2


async def test_single_waiting_pair_per_next_of(repo):
    source = FakeSource(chain(1))
    nav = make_nav(source, repo)
    last = await nav.get(ref(1))
    events = []
    nav.on_waiting = events.append
    # El re-chequeo espera y, tras recibir un «siguiente» nuevo, la carga también.
    source.chapters[ref(1).locator] = chapter(1, nxt=2)
    source.chapters[ref(2).locator] = chapter(2, prev=1)
    source.failures = [PageUnreachable()]

    original = source.load_chapter
    calls = {"n": 0}

    async def flaky(r):
        calls["n"] += 1
        if calls["n"] == 3:  # primera carga de /2 tras el re-chequeo
            source.failures = [PageUnreachable()]
        return await original(r)

    source.load_chapter = flaky
    got = await nav.next_of(last)
    assert got.ref == ref(2)
    assert events == [True, False]


async def test_prefetch_task_is_named(repo):
    nav = make_nav(FakeSource(chain(2)), repo)
    nav.start_prefetch(await nav.get(ref(1)))
    assert nav._prefetch_task.get_name() == "lector:prefetch"
    await nav.close()


async def test_prefetch_errors_bounded_and_locks_released(repo):
    source = FakeSource(chain(3))
    nav = make_nav(source, repo)
    for _ in range(15):
        source.failures = [DamagedDocument()]
        nav.start_prefetch(chapter(0, nxt=1))
        await settle(nav)
    assert len(nav.prefetch_errors) == 10
    assert nav._locks == {}
    await nav.close()
