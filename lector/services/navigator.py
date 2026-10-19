"""Navegación entre capítulos con caché, precarga y espera de red."""
from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from typing import TypeVar

from lector.content.base import ContentSource
from lector.errors import AmbiguousNextChapter, ChapterLoopDetected, LectorError
from lector.models import Chapter, ChapterRef, Document, SourceKind
from lector.persistence.repositories import ChapterRepo

log = logging.getLogger(__name__)

T = TypeVar("T")

_FIRST_WAIT_S = 1.0
TEXT_WRITE_DRAIN_S = 5.0  # tope de espera de las escrituras con texto al dejar de guardarlo


class TextWrites:
    """Escrituras en un hilo que pueden llevar el texto del capítulo.

    Al dejar de guardar el texto se esperan las que ya estaban en marcha con el valor
    anterior: así un borrado posterior no lo ve reaparecer.
    """

    def __init__(self) -> None:
        self._pending: set[asyncio.Future] = set()

    async def run(self, fn: Callable[..., T], *args, with_text: bool) -> T:
        future = asyncio.ensure_future(asyncio.to_thread(fn, *args))
        # Si quien espera se cancela, el hilo sigue: su error no debe quedar sin recoger.
        future.add_done_callback(lambda f: f.cancelled() or f.exception())
        if with_text:
            self._pending.add(future)
            future.add_done_callback(self._pending.discard)
        return await asyncio.shield(future)

    async def drain(self, timeout: float = TEXT_WRITE_DRAIN_S) -> None:
        if not self._pending:
            return
        _done, pending = await asyncio.wait(set(self._pending), timeout=timeout)
        if pending:
            log.warning("Quedan %d escrituras de texto sin terminar", len(pending))


class _Wait:
    """Estado de espera de una operación en primer plano."""

    waiting = False


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
        if wait_max_s <= 0:
            raise ValueError("wait_max_s debe ser mayor que 0")
        if prefetch_ahead < 0:
            raise ValueError("prefetch_ahead no puede ser negativo")
        self._source = source
        self._document = document
        self._repo = chapter_repo
        self._store_text = store_text
        self._writes = TextWrites()
        self._prefetch_ahead = prefetch_ahead
        self._wait_max_s = wait_max_s
        self._sleep = sleep
        self._cache: OrderedDict[str, Chapter] = OrderedDict()
        self._cache_size = prefetch_ahead + 2
        # locator pedido -> locator final, cuando una redirección los distingue.
        self._alias: dict[str, str] = {}
        # locator -> [candado, usuarios]; se borra al terminar la última carga.
        self._locks: dict[str, list] = {}
        self._prefetch_task: asyncio.Task | None = None
        self.prefetch_errors: deque[LectorError] = deque(maxlen=10)
        self.visited: set[str] = set()
        self.on_waiting: Callable[[bool], None] = lambda waiting: None

    # --- API pública ---

    async def get(self, ref: ChapterRef) -> Chapter:
        wait = _Wait()
        try:
            return await self._get(ref, wait)
        finally:
            self._end_wait(wait)

    async def next_of(self, chapter: Chapter) -> Chapter | None:
        wait = _Wait()
        try:
            nxt = await self._resolve_next(chapter, wait)
            if nxt is None:
                return None
            if self._is_visited(nxt.locator):
                raise ChapterLoopDetected()
            loaded = await self._get(nxt, wait)
            if self._is_visited(loaded.ref.locator):  # redirección a uno ya leído
                raise ChapterLoopDetected()
            return loaded
        finally:
            self._end_wait(wait)

    async def prev_of(self, chapter: Chapter) -> Chapter | None:
        if chapter.prev_ref is None:
            return None
        previous = await self.get(chapter.prev_ref)
        # Retroceder rompe el recorrido hacia adelante: no es un ciclo.
        self.reset_visited(previous.ref)
        return previous

    def mark_visited(self, ref: ChapterRef) -> None:
        self.visited.add(ref.locator)

    async def set_store_text(self, on: bool) -> None:
        """Guardar o no el texto desde ya; al desactivarlo espera lo que se estaba guardando."""
        self._store_text = on
        if not on:
            await self._writes.drain()

    def reset_visited(self, ref: ChapterRef | None = None) -> None:
        self.visited.clear()
        if ref is not None:
            self.visited.add(ref.locator)

    def start_prefetch(self, chapter: Chapter) -> None:
        if self._prefetch_task is not None:
            self._prefetch_task.cancel()
        self._prefetch_task = asyncio.create_task(
            self._prefetch(chapter), name="lector:prefetch"
        )

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

    def _canon(self, locator: str) -> str:
        return self._alias.get(locator, locator)

    def _is_visited(self, locator: str) -> bool:
        target = self._canon(locator)
        return any(self._canon(v) == target for v in self.visited)

    async def _get(self, ref: ChapterRef, wait: _Wait) -> Chapter:
        return await self._with_wait(lambda: self._load_once(ref), wait)

    async def _resolve_next(self, chapter: Chapter, wait: _Wait) -> ChapterRef | None:
        if chapter.next_candidates:
            raise AmbiguousNextChapter(list(chapter.next_candidates))
        if chapter.next_ref is not None:
            return chapter.next_ref
        if self._document.kind != SourceKind.URL:
            return None
        # Web: puede haberse publicado un capítulo nuevo desde que se guardó.
        try:
            fresh = await self._with_wait(lambda: self._reload(chapter.ref), wait)
        except LectorError as exc:  # no recuperable: gana la copia guardada
            log.warning("No se pudo revisar el capítulo %s: %s", chapter.ref.locator, exc)
            return None
        if fresh.next_candidates:
            raise AmbiguousNextChapter(list(fresh.next_candidates))
        return fresh.next_ref

    async def _reload(self, ref: ChapterRef) -> Chapter:
        fresh = await self._source.load_chapter(ref)
        await self._persist(fresh)
        self._remember(fresh, ref.locator)
        return fresh

    async def _load_once(self, ref: ChapterRef) -> Chapter:
        entry = self._locks.setdefault(ref.locator, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                key = self._canon(ref.locator)
                cached = self._cache.get(key)
                if cached is not None:
                    self._cache.move_to_end(key)
                    return cached
                stored = await self._read_stored(ref.locator)
                if stored is not None:
                    self._remember(stored, ref.locator)
                    return stored
                chapter = await self._source.load_chapter(ref)
                await self._persist(chapter)
                self._remember(chapter, ref.locator)
                return chapter
        finally:
            entry[1] -= 1
            if entry[1] == 0 and self._locks.get(ref.locator) is entry:
                del self._locks[ref.locator]

    async def _read_stored(self, locator: str) -> Chapter | None:
        try:
            return await asyncio.to_thread(self._repo.get_text, self._document.doc_id, locator)
        except Exception as exc:
            log.warning("No se pudo leer el capítulo %s guardado: %s", locator, exc)
            return None

    async def _persist(self, chapter: Chapter) -> None:
        store = self._store_text  # el valor vivo: puede cambiar durante la lectura
        try:
            await self._writes.run(
                self._repo.save, self._document.doc_id, chapter, store, with_text=store
            )
        except LectorError as exc:
            log.warning("No se pudo guardar el capítulo %s: %s", chapter.ref.locator, exc)

    def _remember(self, chapter: Chapter, requested: str) -> None:
        final = chapter.ref.locator
        if requested != final:
            self._alias[requested] = final
        self._cache[final] = chapter
        self._cache.move_to_end(final)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    def _notify(self, waiting: bool) -> None:
        try:
            self.on_waiting(waiting)
        except Exception:
            log.exception("El aviso de espera de red falló")

    def _end_wait(self, wait: _Wait) -> None:
        if wait.waiting:
            wait.waiting = False
            self._notify(False)

    async def _with_wait(
        self, attempt: Callable[[], Awaitable[T]], wait: _Wait | None
    ) -> T:
        """Reintenta sin límite los errores recuperables con espera creciente.

        Con `wait` avisa (una vez) de que empieza la espera; el que creó `wait`
        avisa del final. Sin `wait` (precarga) no avisa nunca.
        """
        delay = _FIRST_WAIT_S
        while True:
            try:
                return await attempt()
            except LectorError as exc:
                if not exc.recoverable:
                    raise
            if wait is not None and not wait.waiting:
                wait.waiting = True
                self._notify(True)
            await self._sleep(min(delay, self._wait_max_s))
            delay = min(delay * 2, self._wait_max_s)

    async def _prefetch(self, chapter: Chapter) -> None:
        current = chapter
        try:
            for _ in range(self._prefetch_ahead):
                ref = current.next_ref
                if ref is None or current.next_candidates:
                    return
                current = await self._with_wait(
                    lambda ref=ref: self._load_once(ref), None
                )
        except asyncio.CancelledError:
            raise
        except LectorError as exc:
            # Un get en primer plano volverá a pedirlo y verá el error.
            self.prefetch_errors.append(exc)
        except Exception:
            log.exception("La precarga de capítulos falló")
