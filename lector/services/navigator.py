"""Navegación entre capítulos con caché, precarga y espera de red."""
from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import TypeVar

from lector.content.base import ContentSource
from lector.errors import AmbiguousNextChapter, ChapterLoopDetected, LectorError
from lector.models import Chapter, ChapterRef, Document, SourceKind
from lector.persistence.repositories import ChapterRepo

log = logging.getLogger(__name__)

T = TypeVar("T")

_FIRST_WAIT_S = 1.0


class ChapterNavigator:
    def __init__(
        self,
        source: ContentSource,
        document: Document,
        chapter_repo: ChapterRepo,
        store_text: bool,
        prefetch_ahead: int = 2,
        wait_max_s: float = 60,
        sleep=asyncio.sleep,
    ):
        self._source = source
        self._document = document
        self._repo = chapter_repo
        self._store_text = store_text
        self._prefetch_ahead = prefetch_ahead
        self._wait_max_s = wait_max_s
        self._sleep = sleep
        self._cache: OrderedDict[str, Chapter] = OrderedDict()
        self._cache_size = prefetch_ahead + 2
        self._locks: dict[str, asyncio.Lock] = {}
        self._prefetch_task: asyncio.Task | None = None
        self.prefetch_errors: list[LectorError] = []
        self.visited: set[str] = set()
        self.on_waiting: Callable[[bool], None] = lambda waiting: None

    # --- API pública ---

    async def get(self, ref: ChapterRef) -> Chapter:
        return await self._with_wait(lambda: self._load_once(ref), notify=True)

    async def next_of(self, chapter: Chapter) -> Chapter | None:
        nxt = await self._resolve_next(chapter)
        if nxt is None:
            return None
        if nxt.locator in self.visited:
            raise ChapterLoopDetected()
        return await self.get(nxt)

    async def prev_of(self, chapter: Chapter) -> Chapter | None:
        if chapter.prev_ref is None:
            return None
        return await self.get(chapter.prev_ref)

    def mark_visited(self, ref: ChapterRef) -> None:
        self.visited.add(ref.locator)

    def start_prefetch(self, chapter: Chapter) -> None:
        if self._prefetch_task is not None:
            self._prefetch_task.cancel()
        self._prefetch_task = asyncio.create_task(self._prefetch(chapter))

    async def close(self) -> None:
        task, self._prefetch_task = self._prefetch_task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise

    # --- internos ---

    async def _resolve_next(self, chapter: Chapter) -> ChapterRef | None:
        if chapter.next_candidates:
            raise AmbiguousNextChapter(list(chapter.next_candidates))
        if chapter.next_ref is not None:
            return chapter.next_ref
        if self._document.kind != SourceKind.URL:
            return None
        # Web: puede haberse publicado un capítulo nuevo desde que se guardó.
        fresh = await self._with_wait(lambda: self._reload(chapter.ref), notify=True)
        if fresh.next_candidates:
            raise AmbiguousNextChapter(list(fresh.next_candidates))
        return fresh.next_ref

    async def _reload(self, ref: ChapterRef) -> Chapter:
        fresh = await self._source.load_chapter(ref)
        await self._persist(fresh)
        self._remember(fresh)
        return fresh

    async def _load_once(self, ref: ChapterRef) -> Chapter:
        lock = self._locks.setdefault(ref.locator, asyncio.Lock())
        async with lock:
            cached = self._cache.get(ref.locator)
            if cached is not None:
                self._cache.move_to_end(ref.locator)
                return cached
            stored = await asyncio.to_thread(
                self._repo.get_text, self._document.doc_id, ref.locator
            )
            if stored is not None:
                self._remember(stored)
                return stored
            chapter = await self._source.load_chapter(ref)
            await self._persist(chapter)
            self._remember(chapter)
            return chapter

    async def _persist(self, chapter: Chapter) -> None:
        try:
            await asyncio.to_thread(
                self._repo.save, self._document.doc_id, chapter, self._store_text
            )
        except LectorError as exc:
            log.warning("No se pudo guardar el capítulo %s: %s", chapter.ref.locator, exc)

    def _remember(self, chapter: Chapter) -> None:
        self._cache[chapter.ref.locator] = chapter
        self._cache.move_to_end(chapter.ref.locator)
        while len(self._cache) > self._cache_size:
            evicted, _ = self._cache.popitem(last=False)
            self._locks.pop(evicted, None)

    def _notify(self, waiting: bool) -> None:
        try:
            self.on_waiting(waiting)
        except Exception:
            log.exception("El aviso de espera de red falló")

    async def _with_wait(self, attempt: Callable[[], Awaitable[T]], *, notify: bool) -> T:
        """Reintenta sin límite los errores recuperables con espera creciente."""
        delay = _FIRST_WAIT_S
        waiting = False
        try:
            while True:
                try:
                    return await attempt()
                except LectorError as exc:
                    if not exc.recoverable:
                        raise
                if notify and not waiting:
                    waiting = True
                    self._notify(True)
                await self._sleep(min(delay, self._wait_max_s))
                delay = min(delay * 2, self._wait_max_s)
        finally:
            if waiting:
                self._notify(False)

    async def _prefetch(self, chapter: Chapter) -> None:
        current = chapter
        try:
            for _ in range(self._prefetch_ahead):
                ref = current.next_ref
                if ref is None or current.next_candidates:
                    return
                current = await self._with_wait(
                    lambda ref=ref: self._load_once(ref), notify=False
                )
        except asyncio.CancelledError:
            raise
        except LectorError as exc:
            # Un get en primer plano volverá a pedirlo y verá el error.
            self.prefetch_errors.append(exc)
        except Exception:
            log.exception("La precarga de capítulos falló")
