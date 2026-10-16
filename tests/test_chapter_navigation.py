from pathlib import Path

import httpx
import pytest

from lector.content.base import doc_id_for_url
from lector.content.generic_web import (
    GenericWebSource,
    find_next_candidates,
    find_prev_candidates,
    guess_work_prefix,
)
from lector.errors import NoExtractableText
from lector.security.http_client import SafeFetcher

FIX = Path(__file__).parent / "fixtures" / "generic"
HOST = "https://lector.example"
PAGE_URL = f"{HOST}/novela/x/capitulo-5/"
PREFIX = "/novela/x/"
HTML_HEADERS = {"content-type": "text/html; charset=utf-8"}


def read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def page(nav: str = "", head: str = "") -> str:
    return read("page.html").replace("<!--NAV-->", nav).replace("<!--HEAD-->", head)


def nav_links(html: str) -> list[str]:
    return find_next_candidates(html, PAGE_URL, PREFIX)


def test_rel_next_preferred():
    html = read("rel_next.html")
    assert nav_links(html) == [f"{HOST}/novela/x/capitulo-6/"]


def test_text_link_siguiente():
    html = read("text_next.html")
    assert nav_links(html) == [f"{HOST}/novela/x/capitulo-6/"]


def test_external_next_ignored():
    html = page('<a href="https://otro-sitio.example/novela/x/capitulo-6/">Siguiente</a>')
    assert nav_links(html) == []


def test_other_work_ignored():
    html = page('<a href="/novela/y/capitulo-6/">Siguiente</a>')
    assert nav_links(html) == []


def test_ambiguous_two_candidates():
    html = page(
        '<a href="/novela/x/capitulo-7/">Siguiente</a>'
        '<a href="/novela/x/extra-especial/">Next</a>'
    )
    assert nav_links(html) == [
        f"{HOST}/novela/x/capitulo-7/",
        f"{HOST}/novela/x/extra-especial/",
    ]


def test_duplicates_collapse_to_one():
    html = page(
        '<a href="/novela/x/capitulo-6/">Siguiente</a>'
        '<a href="/novela/x/capitulo-6/#top">Next</a>'
    )
    assert nav_links(html) == [f"{HOST}/novela/x/capitulo-6/"]


def test_numeric_pattern_tiebreak():
    html = page(
        '<a href="/novela/x/extra-especial/">Siguiente</a>'
        '<a href="/novela/x/capitulo-6/">Next chapter</a>'
    )
    assert nav_links(html) == [f"{HOST}/novela/x/capitulo-6/"]


def test_numeric_pattern_never_invents():
    assert nav_links(page()) == []


def test_numeric_pattern_does_not_break_real_ambiguity():
    html = page(
        '<a href="/novela/x/extra-especial/">Siguiente</a>'
        '<a href="/novela/x/otro-extra/">Next</a>'
    )
    assert len(nav_links(html)) == 2


def test_work_prefix_parent_path():
    assert guess_work_prefix(PAGE_URL, page()) == PREFIX
    assert guess_work_prefix(f"{HOST}/novela/x/capitulo-5", page()) == PREFIX


def test_work_prefix_from_canonical():
    assert guess_work_prefix(PAGE_URL, read("canonical.html")) == "/novela/"


def test_work_prefix_from_index_link():
    assert guess_work_prefix(f"{HOST}/novela/x/y/capitulo-5/", read("index_link.html")) == PREFIX


def test_work_prefix_ignores_unrelated_index_link():
    html = page('<a href="/otra/obra/">Índice</a>')
    assert guess_work_prefix(PAGE_URL, html) == PREFIX


@pytest.mark.parametrize(
    "href",
    [
        f"{HOST}/novela/x/../y/capitulo-6/",
        f"{HOST}/novela/x/%2e%2e/y/capitulo-6/",
        f"{HOST}/novela/x/%2E%2E/y/capitulo-6/",
        f"{HOST}/novela/x/./../y/capitulo-6/",
    ],
)
def test_dot_segments_cannot_escape_prefix(href):
    assert nav_links(page(f'<a href="{href}">Siguiente</a>')) == []


def test_dot_segments_inside_prefix_are_normalized():
    html = page(f'<a href="{HOST}/novela/x/y/../capitulo-6/">Siguiente</a>')
    assert nav_links(html) == [f"{HOST}/novela/x/capitulo-6/"]


@pytest.mark.parametrize(
    "text",
    ["Siguiente capítulo »", "Next »", "SIGUIENTE   CAPITULO", "Capítulo siguiente →", "» ", "→", "Next chapter"],
)
def test_tolerant_next_text(text):
    assert nav_links(page(f'<a href="/novela/x/capitulo-6/">{text}</a>')) == [
        f"{HOST}/novela/x/capitulo-6/"
    ]


@pytest.mark.parametrize("text", ["Siguiente novela", "Next episode", "Ver siguiente capítulo recomendado"])
def test_next_text_requires_exact_match(text):
    assert nav_links(page(f'<a href="/novela/x/capitulo-6/">{text}</a>')) == []


def prev_links(html: str) -> list[str]:
    return find_prev_candidates(html, PAGE_URL, PREFIX)


def test_prev_rel():
    html = page(
        '<link rel="prev" href="/novela/x/capitulo-4/">'
        '<a href="/novela/x/capitulo-3/">Anterior</a>'
    )
    assert prev_links(html) == [f"{HOST}/novela/x/capitulo-4/"]


@pytest.mark.parametrize("text", ["Anterior", "« Anterior", "Capítulo anterior", "Previous chapter", "←", "Prev"])
def test_prev_text(text):
    html = page(f'<a href="/novela/x/capitulo-4/">{text}</a>')
    assert prev_links(html) == [f"{HOST}/novela/x/capitulo-4/"]


def test_prev_filters_and_no_numeric_tiebreak():
    html = page(
        '<a href="/novela/y/capitulo-4/">Anterior</a>'
        '<a href="/novela/x/capitulo-4/">Previous</a>'
        '<a href="/novela/x/extra/">Prev</a>'
    )
    assert len(prev_links(html)) == 2


async def test_open_sets_prev_ref():
    fetcher, source = await make_source({PAGE_URL: read("text_next.html")})
    try:
        _, chapter = await source.open(PAGE_URL)
    finally:
        await fetcher.aclose()
    assert chapter.prev_ref.locator == f"{HOST}/novela/x/capitulo-4/"


# --- fuente completa ---------------------------------------------------------


def make_handler(pages: dict[str, str]):
    def handler(request: httpx.Request) -> httpx.Response:
        body = pages.get(str(request.url))
        if body is None:
            return httpx.Response(404, text="no", headers=HTML_HEADERS)
        return httpx.Response(200, text=body, headers=HTML_HEADERS)

    return handler


async def resolver(host: str, port: int) -> str:
    return "93.184.216.34"


async def make_source(pages, **kwargs):
    fetcher = SafeFetcher(resolver=resolver, transport=httpx.MockTransport(make_handler(pages)))
    return fetcher, GenericWebSource(fetcher, **kwargs)


async def test_no_text_raises():
    fetcher, source = await make_source({PAGE_URL: read("no_text.html")})
    try:
        with pytest.raises(NoExtractableText):
            await source.open(PAGE_URL)
    finally:
        await fetcher.aclose()


async def test_open_extracts_chapter_and_next():
    fetcher, source = await make_source({PAGE_URL: read("text_next.html")})
    try:
        doc, chapter = await source.open(PAGE_URL)
    finally:
        await fetcher.aclose()
    assert len(chapter.paragraphs) >= 3
    assert chapter.paragraphs[1].text.startswith("La lluvia caía sobre el viejo puerto")
    assert "Sitio de ejemplo" not in chapter.text
    assert chapter.next_ref.locator == f"{HOST}/novela/x/capitulo-6/"
    assert chapter.next_candidates == []
    assert doc.work_prefix == HOST + PREFIX
    assert doc.doc_id == doc_id_for_url("lector.example/novela/x/")
    assert doc.chapters == []


async def test_open_with_prefetched_html_skips_network():
    fetcher, source = await make_source({})
    try:
        _, chapter = await source.open(PAGE_URL, prefetched_html=read("text_next.html"))
    finally:
        await fetcher.aclose()
    assert chapter.next_ref is not None


async def test_open_ambiguous_fills_candidates():
    html = page(
        '<a href="/novela/x/capitulo-7/">Siguiente</a>'
        '<a href="/novela/x/extra-especial/">Next</a>'
    )
    fetcher, source = await make_source({PAGE_URL: html})
    try:
        _, chapter = await source.open(PAGE_URL)
    finally:
        await fetcher.aclose()
    assert chapter.next_ref is None
    assert len(chapter.next_candidates) == 2


async def test_work_prefix_override_wins():
    html = page('<a href="/novela/x/capitulo-6/">Siguiente</a>')
    fetcher, source = await make_source({PAGE_URL: html}, work_prefix_override="/novela/z/")
    try:
        doc, chapter = await source.open(PAGE_URL)
    finally:
        await fetcher.aclose()
    assert doc.work_prefix == HOST + "/novela/z/"
    assert chapter.next_ref is None


async def test_load_chapter_and_empty_index():
    nxt = f"{HOST}/novela/x/capitulo-6/"
    fetcher, source = await make_source({PAGE_URL: read("text_next.html"), nxt: page()})
    try:
        _, chapter = await source.open(PAGE_URL)
        loaded = await source.load_chapter(chapter.next_ref)
        assert await source.list_chapters() == []
    finally:
        await fetcher.aclose()
    assert loaded.ref.locator == nxt
    assert loaded.next_ref is None
