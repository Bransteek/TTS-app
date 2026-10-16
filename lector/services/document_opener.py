"""Apertura de documentos para una sesión de lectura.

- `store_opened`: guarda el documento y el capítulo de una vista previa.
- `check_file`: un PDF o EPUB del historial sigue en su sitio y sin cambios.
- `DeferredSource`: fuente web reabierta desde el historial sin tocar la red; la
  fuente real se abre en la primera carga de un capítulo que no esté guardado.
  Si no hay red, esa carga lanza un error recuperable y el navegador espera
  (WAITING_NETWORK) en lugar de que `play()` falle.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from lector.content.base import ContentSource
from lector.errors import LectorError
from lector.models import Chapter, ChapterRef, Document, SourceKind
from lector.persistence.repositories import ChapterRepo, DocumentRepo
from lector.services import recovery

log = logging.getLogger(__name__)

# Se llama como `opener(locator, fetcher)` o con `prefix_overrides={host: "/ruta/"}`.
SourceOpener = Callable[..., Awaitable[tuple[ContentSource, Document, Chapter]]]


def store_opened(
    documents: DocumentRepo, chapters: ChapterRepo, doc: Document, chapter: Chapter, store_text: bool
) -> None:
    """Síncrona (se llama en un hilo): documento al historial y capítulo abierto a la caché."""
    origin_path = size = mtime = None
    if doc.kind is not SourceKind.URL:
        origin_path = doc.origin
        try:
            stat = Path(doc.origin).stat()
            size, mtime = stat.st_size, stat.st_mtime
        except OSError:
            log.warning("No se pudo leer la fecha del archivo %s", doc.origin)
    documents.upsert(doc, origin_path, size, mtime)
    try:
        chapters.save(doc.doc_id, chapter, store_text)
    except LectorError as exc:
        log.warning("No se pudo guardar el capítulo abierto: %s", exc)


async def check_file(
    doc: Document, stored_stat: tuple[int, float] | None = None, documents: DocumentRepo | None = None
) -> None:
    """Lanza `FileMovedOrChanged` si el archivo ya no existe o su contenido cambió.

    Delega en `recovery.check_file` (única implementación) en un hilo. Si pasa y el
    (tamaño, mtime) actual difiere del guardado, y se da `documents`, lo refresca para
    que la próxima apertura tome la vía rápida.
    """
    path = await asyncio.to_thread(recovery.check_file, doc, doc.origin, stored_stat)
    if documents is None:
        return
    try:
        stat = await asyncio.to_thread(path.stat)
        if (stat.st_size, stat.st_mtime) != (tuple(stored_stat) if stored_stat else None):
            await asyncio.to_thread(
                documents.update_file_stat, doc.doc_id, stat.st_size, stat.st_mtime)
    except Exception as exc:  # refrescar es una optimización: nunca debe impedir abrir
        log.warning("No se pudo refrescar el estado del archivo: %s", exc)


class DeferredSource:
    """Fuente web que se abre de verdad en la primera carga (ver docstring del módulo).

    Al abrirse actualiza `doc` en el sitio (título, URL canónica, lista de capítulos)
    sin cambiar su identidad (`doc_id`, tipo y origen).
    """

    def __init__(self, opener: SourceOpener, fetcher: Any, doc: Document):
        self._opener = opener
        self._fetcher = fetcher
        self._doc = doc
        self._real: ContentSource | None = None
        self._lock = asyncio.Lock()

    async def open(self, locator: str, prefetched_html: str | None = None):
        raise NotImplementedError("DeferredSource se abre sola en la primera carga")

    async def load_chapter(self, ref: ChapterRef) -> Chapter:
        async with self._lock:
            if self._real is None:
                # Abrir por el capítulo pedido devuelve ese mismo capítulo: una sola descarga.
                real, opened, chapter = await self._opener(ref.locator, self._fetcher)
                self._opened(real, opened)
                if ref.index is not None and chapter.ref.index is None:
                    chapter.ref = ChapterRef(ref.index, chapter.ref.title, chapter.ref.locator)
                return chapter
        return await self._real.load_chapter(ref)

    async def list_chapters(self) -> list[ChapterRef]:
        async with self._lock:
            if self._real is None:
                real, opened, _ = await self._opener(self._doc.origin, self._fetcher)
                self._opened(real, opened)
        return await self._real.list_chapters()

    def _opened(self, real: ContentSource, opened: Document) -> None:
        self._real = real
        doc = self._doc
        doc.title = opened.title or doc.title
        doc.canonical_url = opened.canonical_url or doc.canonical_url
        doc.work_prefix = opened.work_prefix or doc.work_prefix
        if opened.chapters:
            doc.chapters = list(opened.chapters)
        doc.adapter = opened.adapter or doc.adapter
