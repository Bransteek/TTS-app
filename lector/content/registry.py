"""Selección automática del adaptador de contenido según la entrada."""
from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

from lector.content.base import ContentSource
from lector.content.epub_source import EpubSource
from lector.content.generic_web import GenericWebSource
from lector.content.madara import MadaraSource, is_madara
from lector.content.pdf_source import PdfSource
from lector.errors import DamagedDocument
from lector.models import Chapter, Document
from lector.security.http_client import SafeFetcher
from lector.security.url_validator import validate_url


_HAS_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)
_MISSING_FILE = "No se encontró el archivo. Comprueba que sigue en su sitio."


def _require_file(locator: str) -> None:
    if not Path(locator).expanduser().is_file():
        raise DamagedDocument(_MISSING_FILE)


async def open_source(
    locator: str, fetcher: SafeFetcher, *, prefix_overrides: Mapping[str, str] | None = None
) -> tuple[ContentSource, Document, Chapter]:
    """Abre `locator` con el adaptador que le toca.

    `prefix_overrides` ({host: "/ruta/"}) es el prefijo de obra corregido por el
    usuario para webs genéricas (§5.2); Madara, PDF y EPUB lo ignoran.
    """
    lowered = locator.strip().lower()
    source: ContentSource
    is_url = bool(_HAS_SCHEME.match(lowered))
    if not is_url and lowered.endswith(".pdf"):
        _require_file(locator.strip())
        source = PdfSource()
        document, chapter = await source.open(locator.strip())
    elif not is_url and lowered.endswith(".epub"):
        _require_file(locator.strip())
        source = EpubSource()
        document, chapter = await source.open(locator.strip())
    else:
        url = validate_url(locator)
        result = await fetcher.get(url)
        if is_madara(result.text):
            source = MadaraSource(fetcher)
        else:
            host = urlsplit(url).netloc.lower()
            override = (prefix_overrides or {}).get(host)
            source = GenericWebSource(fetcher, work_prefix_override=override)
        document, chapter = await source.open(url, prefetched_html=result.text)
    return source, document, chapter
