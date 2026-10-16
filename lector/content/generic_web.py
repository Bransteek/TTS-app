"""Adaptador para sitios genéricos: texto con trafilatura y detección prudente del «siguiente»."""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any
from urllib.parse import urldefrag, urljoin, urlsplit

import lxml.html
import trafilatura

from lector.content.base import ContentSource, doc_id_for_url
from lector.content.segmenter import build_chapter_text
from lector.errors import NoExtractableText
from lector.models import Chapter, ChapterRef, Document, SourceKind
from lector.security.http_client import SafeFetcher

_NEXT_WORDS = {"siguiente", "siguiente capitulo", "capitulo siguiente", "next", "next chapter"}
_PREV_WORDS = {
    "anterior", "capitulo anterior", "anterior capitulo", "previous", "prev",
    "previous chapter", "prev chapter",
}
_NEXT_ARROWS = {"»", "→", ">"}
_PREV_ARROWS = {"«", "←", "<"}
_DECORATION = " \t\r\n»«→←<>.,:;|-–—·•"
_INDEX_TEXTS = {"índice", "indice", "tabla de contenidos", "index"}
_LAST_NUMBER = re.compile(r"(\d+)(?!.*\d)")


def _norm_text(element) -> str:
    return re.sub(r"\s+", " ", element.text_content()).strip().lower()


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _matches(element, words: set[str], arrows: set[str]) -> bool:
    """Igualdad exacta tras normalizar (sin acentos ni flechas alrededor); nunca subcadenas."""
    text = _fold(_norm_text(element))
    core = re.sub(r"\s+", " ", text.strip(_DECORATION))
    return core in words if core else text in arrows


def _clean_path(path: str) -> str:
    """Resuelve «.» y «..» (también codificados como %2e) como lo haría el servidor."""
    segments: list[str] = []
    raw = re.sub(r"(?i)%2e", ".", path).split("/")
    for i, seg in enumerate(raw):
        if seg == "..":
            if len(segments) > 1:
                segments.pop()
            if i == len(raw) - 1:
                segments.append("")
        elif seg == ".":
            if i == len(raw) - 1:
                segments.append("")
        else:
            segments.append(seg)
    return "/".join(segments) or "/"


def _parse(html: str):
    if not html.strip():
        return None
    try:
        return lxml.html.fromstring(html)
    except (lxml.etree.ParserError, ValueError):
        return None


def _without_chrome(html: str) -> str:
    """Quita navegación y pies: trafilatura cae al <body> entero si no halla contenido."""
    tree = _parse(html)
    if tree is None:
        return html
    for node in list(tree.iter("nav", "aside", "footer")):
        node.drop_tree()
    return lxml.html.tostring(tree, encoding="unicode")


def _with_slash(path: str) -> str:
    return path if path.endswith("/") else path + "/"


# --- Prefijos de obra corregidos por el usuario (§5.2) ------------------------------------
# Se guardan por host como lista de rutas: {"example.com": ["/novela/x/", "/otra/"]}.
# Cada ruta es la corrección de una obra; el formato antiguo {host: "/ruta/"} se lee
# como una lista de un elemento.


def _host_prefixes(overrides: Mapping[str, Any] | None, host: str) -> list[str]:
    value = (overrides or {}).get(host)
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [_with_slash(p) for p in value if isinstance(p, str) and p.startswith("/")]


def _under(url_path: str, prefix: str) -> bool:
    return _with_slash(url_path or "/").startswith(prefix)


def prefix_override_for(overrides: Mapping[str, Any] | None, url: str) -> str | None:
    """Prefijo corregido que corresponde a `url`: el más largo que la contiene, o None."""
    parts = urlsplit(url)
    matches = [p for p in _host_prefixes(overrides, parts.netloc.lower()) if _under(parts.path, p)]
    return max(matches, key=len, default=None)


def set_prefix_override(
    overrides: Mapping[str, Any] | None, url: str, prefix: str | None
) -> dict[str, list[str]]:
    """Copia de `overrides` donde la obra de `url` usa `prefix` (None: vuelve a la detección).

    Se quitan las correcciones anteriores que contenían `url` (eran de esta obra).
    """
    parts = urlsplit(url)
    host = parts.netloc.lower()
    out = {h: _host_prefixes(overrides, h) for h in (overrides or {})}
    kept = [p for p in out.get(host, []) if not _under(parts.path, p)]
    if prefix is not None and _with_slash(prefix) not in kept:
        kept.append(_with_slash(prefix))
    if kept:
        out[host] = kept
    else:
        out.pop(host, None)
    return out


def _parent_path(url: str) -> str:
    path = urlsplit(url).path.rstrip("/")
    return path.rsplit("/", 1)[0] + "/"


def _strip(url: str) -> str:
    return urldefrag(url)[0]


def _increment_last_number(url: str) -> str | None:
    parts = urlsplit(url)
    match = _LAST_NUMBER.search(parts.path)
    if not match:
        return None
    path = parts.path[: match.start()] + str(int(match.group(1)) + 1) + parts.path[match.end():]
    return parts._replace(path=path, fragment="").geturl()


def _same_url(a: str, b: str) -> bool:
    return _strip(a).rstrip("/") == _strip(b).rstrip("/")


def guess_work_prefix(chapter_url: str, html: str) -> str:
    """Ruta padre de la URL del capítulo, o una más corta indicada por canonical/índice."""
    parts = urlsplit(chapter_url)
    prefix = _parent_path(chapter_url)
    current = _with_slash(parts.path)
    tree = _parse(html)
    if tree is None:
        return prefix
    hrefs = [link.get("href") for link in tree.cssselect("link[rel=canonical][href]")]
    hrefs += [
        a.get("href") for a in tree.cssselect("a[href]") if _norm_text(a) in _INDEX_TEXTS
    ]
    for href in hrefs:
        target = urlsplit(urljoin(chapter_url, href.strip()))
        if target.netloc.lower() != parts.netloc.lower():
            continue
        path = _with_slash(target.path)
        if path != "/" and len(path) < len(prefix) and current.startswith(path):
            prefix = path
    return prefix


def _nav_links(
    tree, page_url: str, work_prefix: str, rel: str, words: set[str], arrows: set[str]
) -> list[str]:
    """Enlaces rel=<rel> o de texto reconocido, del mismo host y dentro de la obra."""
    work_prefix = _with_slash(work_prefix)
    host = urlsplit(page_url).netloc.lower()

    def keep(hrefs: list[str]) -> list[str]:
        found: list[str] = []
        for href in hrefs:
            if not href or not href.strip():
                continue
            parts = urlsplit(_strip(urljoin(page_url, href.strip())))
            if parts.scheme not in ("http", "https") or parts.netloc.lower() != host:
                continue
            parts = parts._replace(path=_clean_path(parts.path))
            if not _with_slash(parts.path).startswith(work_prefix):
                continue
            url = parts.geturl()
            if _same_url(url, page_url) or url in found:
                continue
            found.append(url)
        return found

    by_rel = keep(
        [el.get("href") for el in tree.cssselect(f"link[rel~={rel}][href], a[rel~={rel}][href]")]
    )
    return by_rel or keep(
        [a.get("href") for a in tree.cssselect("a[href]") if _matches(a, words, arrows)]
    )


def find_next_candidates(html: str, page_url: str, work_prefix: str) -> list[str]:
    """URLs posibles de «siguiente capítulo»; una sola si es inequívoca."""
    tree = _parse(html)
    if tree is None:
        return []
    candidates = _nav_links(tree, page_url, work_prefix, "next", _NEXT_WORDS, _NEXT_ARROWS)
    if len(candidates) >= 2:
        expected = _increment_last_number(page_url)
        if expected is not None:
            matching = [c for c in candidates if _same_url(c, expected)]
            if len(matching) == 1:
                return matching
    return candidates


def find_prev_candidates(html: str, page_url: str, work_prefix: str) -> list[str]:
    """URLs posibles de «capítulo anterior» (sin desempate numérico)."""
    tree = _parse(html)
    if tree is None:
        return []
    return _nav_links(tree, page_url, work_prefix, "prev", _PREV_WORDS, _PREV_ARROWS)


class GenericWebSource(ContentSource):
    def __init__(self, fetcher: SafeFetcher, work_prefix_override: str | None = None) -> None:
        self._fetcher = fetcher
        self._override = _with_slash(work_prefix_override) if work_prefix_override else None
        self._prefix: str | None = None

    async def open(
        self, locator: str, prefetched_html: str | None = None
    ) -> tuple[Document, Chapter]:
        if prefetched_html is not None:
            html, url = prefetched_html, locator
        else:
            result = await self._fetcher.get(locator)
            html, url = result.text, result.url
        chapter = self._parse(html, url)
        parts = urlsplit(url)
        host = parts.netloc.lower()  # «Example.com» y «example.com» son la misma obra
        work_url = f"{parts.scheme}://{host}{self._prefix}"
        document = Document(
            doc_id=doc_id_for_url(host + self._prefix),
            kind=SourceKind.URL,
            origin=work_url,
            title=chapter.title,
            canonical_url=work_url,
            work_prefix=work_url,
            file_hash=None,
            chapters=[],
            adapter="generic",
        )
        return document, chapter

    async def load_chapter(self, ref: ChapterRef) -> Chapter:
        result = await self._fetcher.get(ref.locator)
        chapter = self._parse(result.text, result.url, keep_prefix=True)
        if ref.index is not None:
            chapter.ref = ChapterRef(ref.index, chapter.ref.title, chapter.ref.locator)
        return chapter

    async def list_chapters(self) -> list[ChapterRef]:
        # Sin índice fiable en sitios genéricos: se navega con «siguiente».
        return []

    # --- internos ---------------------------------------------------------

    def _parse(self, html: str, url: str, keep_prefix: bool = False) -> Chapter:
        raw = trafilatura.extract(
            _without_chrome(html),
            output_format="txt",
            include_comments=False,
            include_tables=False,
            favor_precision=True,
        )
        lines = [line.strip() for line in (raw or "").splitlines()]
        texts = [(line, None) for line in lines if line]
        if not texts:
            raise NoExtractableText()
        if self._override:
            self._prefix = self._override
        elif not (keep_prefix and self._prefix):
            self._prefix = guess_work_prefix(url, html)
        text, paragraphs, sentences = build_chapter_text(texts)
        title = self._title(html)
        urls = find_next_candidates(html, url, self._prefix)
        refs = [ChapterRef(None, "", u) for u in urls]
        prev = find_prev_candidates(html, url, self._prefix)
        return Chapter(
            ref=ChapterRef(None, title, url),
            title=title,
            text=text,
            paragraphs=paragraphs,
            sentences=sentences,
            next_ref=refs[0] if len(refs) == 1 else None,
            prev_ref=ChapterRef(None, "", prev[0]) if len(prev) == 1 else None,
            next_candidates=refs if len(refs) > 1 else [],
        )

    @staticmethod
    def _title(html: str) -> str:
        tree = _parse(html)
        if tree is None:
            return ""
        for selector in ("h1", "title"):
            found = tree.cssselect(selector)
            if found:
                title = re.sub(r"\s+", " ", found[0].text_content()).strip()
                if title:
                    return title
        return ""
