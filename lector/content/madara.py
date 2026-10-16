"""Adaptador para sitios con el tema WordPress Madara (aresscanlationovel.com)."""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

import lxml.html

from lector.content.base import ContentSource, doc_id_for_url, html_paragraphs
from lector.content.segmenter import build_chapter_text
from lector.errors import PageUnreachable
from lector.models import Chapter, ChapterRef, Document, SourceKind
from lector.security.http_client import SafeFetcher

_WORK_PATH = re.compile(r"^/novels/[^/]+/?$")


def is_madara(html: str) -> bool:
    if "wp-content/themes/madara" in html:
        return True
    if not html.strip():
        return False
    try:
        doc = lxml.html.fromstring(html)
    except (lxml.etree.ParserError, ValueError):
        return False
    return bool(doc.cssselect(".reading-content")) and bool(
        doc.cssselect("a.next_page, a.prev_page")
    )


def _with_slash(url: str) -> str:
    return url if url.endswith("/") else url + "/"


def _is_work_cover(url: str) -> bool:
    return bool(_WORK_PATH.match(urlsplit(url).path))


class MadaraSource(ContentSource):
    def __init__(self, fetcher: SafeFetcher) -> None:
        self._fetcher = fetcher
        self._work_url: str | None = None
        self._index: list[ChapterRef] | None = None

    async def open(
        self, locator: str, prefetched_html: str | None = None
    ) -> tuple[Document, Chapter]:
        locator = _with_slash(locator)
        if _is_work_cover(locator):
            self._work_url = locator
            self._index = None
            chapters = await self.list_chapters()
            if not chapters:
                raise PageUnreachable("No se encontraron capítulos en esa novela.", recoverable=False)
            result = await self._fetcher.get(chapters[0].locator)
            tree = lxml.html.fromstring(result.text)
            chapter = self._parse(tree, result.url)
            title = self._breadcrumb_work_title(tree) or chapter.title
            return self._document(locator, title, chapters, index_page=True), chapter
        html, url = await self._page(locator, prefetched_html)
        tree = lxml.html.fromstring(html)
        work_url = self._find_work_url(tree, url)
        if work_url != self._work_url:
            self._work_url, self._index = work_url, None
        try:
            chapters = await self.list_chapters()
        except PageUnreachable:
            chapters = []
        chapter = self._parse(tree, url)
        return self._document(work_url, self._breadcrumb_work_title(tree) or "", chapters), chapter

    async def load_chapter(self, ref: ChapterRef) -> Chapter:
        result = await self._fetcher.get(ref.locator)
        chapter = self._parse(lxml.html.fromstring(result.text), result.url)
        if ref.index is not None:
            chapter.ref = ChapterRef(ref.index, chapter.ref.title, chapter.ref.locator)
        return chapter

    async def list_chapters(self) -> list[ChapterRef]:
        if self._index is not None:
            return list(self._index)
        if self._work_url is None:
            raise PageUnreachable("Primero hay que abrir una novela.", recoverable=False)
        result = await self._fetcher.post(f"{self._work_url}ajax/chapters/")
        tree = lxml.html.fromstring(result.text)
        titles: dict[str, str] = {}
        for a in reversed(tree.cssselect("li.wp-manga-chapter a[href]")):
            href = urljoin(self._work_url, a.get("href").strip())
            title = re.sub(r"\s+", " ", a.text_content()).strip()
            if not titles.get(href):
                titles[href] = title
        refs = [ChapterRef(i, t, h) for i, (h, t) in enumerate(titles.items())]
        self._index = refs
        return list(refs)

    # --- internos ---------------------------------------------------------

    async def _page(self, url: str, prefetched: str | None) -> tuple[str, str]:
        if prefetched is not None:
            return prefetched, url
        result = await self._fetcher.get(url)
        return result.text, result.url

    def _document(
        self, work_url: str, title: str, chapters: list[ChapterRef], index_page: bool = False
    ) -> Document:
        return Document(
            doc_id=doc_id_for_url(work_url),
            kind=SourceKind.URL,
            origin=work_url,
            title=title,
            canonical_url=work_url,
            work_prefix=work_url,
            file_hash=None,
            chapters=chapters,
            adapter="madara",
            index_page=index_page,
        )

    @staticmethod
    def _work_link(tree):
        for a in tree.cssselect(".breadcrumb a[href]"):
            if _WORK_PATH.match(urlsplit(a.get("href")).path):
                return a
        return None

    def _breadcrumb_work_title(self, tree) -> str | None:
        link = self._work_link(tree)
        return re.sub(r"\s+", " ", link.text_content()).strip() if link is not None else None

    def _find_work_url(self, tree, chapter_url: str) -> str:
        link = self._work_link(tree)
        if link is not None:
            return _with_slash(urljoin(chapter_url, link.get("href")))
        parts = urlsplit(chapter_url)
        path = parts.path.rstrip("/").rsplit("/", 1)[0] + "/"
        return f"{parts.scheme}://{parts.netloc}{path}"

    def _parse(self, tree, url: str) -> Chapter:
        content = tree.cssselect(".reading-content")
        if not content:
            raise PageUnreachable("No se encontró el texto del capítulo.", recoverable=False)
        text, paragraphs, sentences = build_chapter_text(
            [(p, None) for p in html_paragraphs(content[0])]
        )
        crumbs = tree.cssselect(".breadcrumb li")
        title = re.sub(r"\s+", " ", crumbs[-1].text_content()).strip() if crumbs else ""
        known = {r.locator: r.index for r in self._index or []}
        locator = _with_slash(url)
        ref = ChapterRef(known.get(locator), title, locator)
        return Chapter(
            ref=ref,
            title=title,
            text=text,
            paragraphs=paragraphs,
            sentences=sentences,
            next_ref=self._nav_ref(tree, "a.next_page[href]", url, known),
            prev_ref=self._nav_ref(tree, "a.prev_page[href]", url, known),
        )

    @staticmethod
    def _nav_ref(tree, selector: str, base: str, known: dict) -> ChapterRef | None:
        links = tree.cssselect(selector)
        if not links:
            return None
        a = links[0]
        href = _with_slash(urljoin(base, a.get("href").strip()))
        title = (a.get("title") or re.sub(r"\s+", " ", a.text_content())).strip()
        return ChapterRef(known.get(href), title, href)
