"""Contrato común de las fuentes de contenido y utilidades de extracción de HTML."""
from __future__ import annotations

import hashlib
import re
from typing import Protocol

from lector.models import Chapter, ChapterRef, Document

_AD_CLASS = re.compile(r"(?:^|[\s_-])(?:ads?|adsbygoogle|advert\w*|publicidad)(?:$|[\s_-])", re.I)
_REMOVED_TAGS = ("script", "style", "ins", "noscript")


class ContentSource(Protocol):
    async def open(
        self, locator: str, prefetched_html: str | None = None
    ) -> tuple[Document, Chapter]: ...

    async def load_chapter(self, ref: ChapterRef) -> Chapter: ...

    async def list_chapters(self) -> list[ChapterRef]: ...


def doc_id_for_url(canonical_work_url: str) -> str:
    """Primeros 16 hex de SHA-256 de la URL normalizada (con «/» final)."""
    url = canonical_work_url.strip()
    if not url.endswith("/"):
        url += "/"
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def html_paragraphs(element) -> list[str]:
    """Textos de los <p> de `element`, con espacios colapsados y sin anuncios ni vacíos."""
    for bad in list(element.iter(*_REMOVED_TAGS)):
        bad.drop_tree()
    for node in list(element.iter()):
        if not isinstance(node.tag, str):
            continue
        marker = f"{node.get('class', '')} {node.get('id', '')}"
        if _AD_CLASS.search(marker) and node is not element:
            node.drop_tree()
    result = []
    for p in element.iter("p"):
        text = re.sub(r"\s+", " ", p.text_content()).strip()
        if text:
            result.append(text)
    return result
