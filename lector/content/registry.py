"""Selección automática del adaptador de contenido según la entrada."""
from __future__ import annotations

from lector.content.base import ContentSource
from lector.content.epub_source import EpubSource
from lector.content.generic_web import GenericWebSource
from lector.content.madara import MadaraSource, is_madara
from lector.content.pdf_source import PdfSource
from lector.models import Chapter, Document
from lector.security.http_client import SafeFetcher
from lector.security.url_validator import validate_url


async def open_source(
    locator: str, fetcher: SafeFetcher
) -> tuple[ContentSource, Document, Chapter]:
    lowered = locator.strip().lower()
    source: ContentSource
    if lowered.endswith(".pdf"):
        source = PdfSource()
        document, chapter = await source.open(locator.strip())
    elif lowered.endswith(".epub"):
        source = EpubSource()
        document, chapter = await source.open(locator.strip())
    else:
        url = validate_url(locator)
        result = await fetcher.get(url)
        source = MadaraSource(fetcher) if is_madara(result.text) else GenericWebSource(fetcher)
        document, chapter = await source.open(url, prefetched_html=result.text)
    return source, document, chapter
