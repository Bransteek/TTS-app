from pathlib import Path

import httpx
import pytest

from lector.content.base import doc_id_for_url, html_paragraphs
from lector.content.madara import MadaraSource, is_madara
from lector.security.http_client import SafeFetcher

FIX = Path(__file__).parent / "fixtures" / "madara"
WORK = (
    "https://aresscanlationovel.com/novels/"
    "a-partir-de-ahora-pedir-matrimonio-es-la-unica-forma-de-sobrevivir/"
)
HTML = {"content-type": "text/html; charset=utf-8"}


def read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if request.method == "POST" and url == WORK + "ajax/chapters/":
        return httpx.Response(200, text=read("chapters_ajax.html"), headers=HTML)
    if request.method == "GET" and url.startswith(WORK + "capitulo-"):
        n = url.removeprefix(WORK + "capitulo-").rstrip("/")
        if (FIX / f"capitulo-{n}.html").exists():
            return httpx.Response(200, text=read(f"capitulo-{n}.html"), headers=HTML)
    return httpx.Response(404, text="no", headers=HTML)


async def resolver(host: str, port: int) -> str:
    return "93.184.216.34"


@pytest.fixture
async def source():
    fetcher = SafeFetcher(resolver=resolver, transport=httpx.MockTransport(handler))
    yield MadaraSource(fetcher)
    await fetcher.aclose()


def test_detects_madara():
    assert is_madara(read("capitulo-99.html")) is True
    assert is_madara("<html></html>") is False


def test_detects_madara_by_structure():
    html = '<div class="reading-content"></div><a class="next_page" href="/x/">n</a>'
    assert is_madara(html) is True


def test_html_paragraphs_strips_noise():
    import lxml.html

    el = lxml.html.fromstring(
        '<div><p>  Hola \n  mundo </p><p> </p><script>x</script><ins><p>anuncio</p></ins>'
        '<div class="adsbygoogle"><p>otro</p></div><p>Fin</p></div>'
    )
    assert html_paragraphs(el) == ["Hola mundo", "Fin"]


def test_doc_id_is_normalized():
    assert doc_id_for_url(WORK) == doc_id_for_url(WORK.rstrip("/"))
    assert len(doc_id_for_url(WORK)) == 16


async def test_chapter_99_text(source):
    _, chapter = await source.open(WORK + "capitulo-99/")
    assert len(chapter.paragraphs) == 117
    assert chapter.paragraphs[0].text.startswith("Park Kyung-Joo habló seriamente")
    assert chapter.paragraphs[-1].text == "—Espero que esto solucione las cosas, al menos un poco."
    assert chapter.title == "Capítulo 99"


async def test_chapter_99_nav(source):
    _, chapter = await source.open(WORK + "capitulo-99/")
    assert chapter.next_ref.locator.endswith("/capitulo-100/")
    assert chapter.prev_ref.locator.endswith("/capitulo-98/")


async def test_last_chapter_has_no_next(source):
    _, chapter = await source.open(WORK + "capitulo-100/")
    assert chapter.next_ref is None
    assert chapter.next_candidates == []


async def test_first_chapter_has_no_prev(source):
    _, chapter = await source.open(WORK + "capitulo-1/")
    assert chapter.prev_ref is None


async def test_chapter_list_from_ajax(source):
    await source.open(WORK + "capitulo-99/")
    chapters = await source.list_chapters()
    assert len(chapters) == 100
    assert [c.index for c in chapters] == list(range(100))
    assert chapters[0].locator.endswith("/capitulo-1/")
    assert chapters[99].locator.endswith("/capitulo-100/")


async def test_doc_id_same_for_any_chapter(source):
    doc1, _ = await source.open(WORK + "capitulo-1/")
    doc99, _ = await source.open(WORK + "capitulo-99/")
    assert doc1.doc_id == doc99.doc_id == doc_id_for_url(WORK)
    assert doc1.canonical_url == WORK


async def test_open_work_url_starts_at_chapter_1(source):
    doc, chapter = await source.open(WORK)
    assert chapter.ref.locator.endswith("/capitulo-1/")
    assert chapter.ref.index == 0
    assert chapter.prev_ref is None
    assert chapter.next_ref.index == 1
    assert len(doc.chapters) == 100
    assert doc.title == "A partir de ahora, pedir matrimonio es la única forma de sobrevivir"


async def test_open_uses_prefetched_html(source):
    _, chapter = await source.open(WORK + "capitulo-99/", prefetched_html=read("capitulo-99.html"))
    assert len(chapter.paragraphs) == 117


async def test_load_chapter_by_ref(source):
    await source.open(WORK)
    refs = await source.list_chapters()
    chapter = await source.load_chapter(refs[98])
    assert chapter.ref.index == 98
    assert chapter.prev_ref.index == 97


async def test_chapter_list_titles(source):
    await source.open(WORK + "capitulo-99/")
    chapters = await source.list_chapters()
    assert all(c.title for c in chapters)
    assert chapters[99].title == "Capítulo 100"
    assert chapters[0].title == "Capítulo 1"


async def test_open_chapter_loads_index():
    fetcher = SafeFetcher(resolver=resolver, transport=httpx.MockTransport(handler))
    src = MadaraSource(fetcher)
    doc, chapter = await src.open(WORK + "capitulo-99/")
    await fetcher.aclose()
    assert len(doc.chapters) == 100
    assert chapter.ref.index == 98
    assert chapter.next_ref.index == 99
    assert chapter.prev_ref.index == 97


async def test_open_chapter_survives_ajax_failure():
    def failing(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(503, text="x", headers=HTML)
        return handler(request)

    fetcher = SafeFetcher(resolver=resolver, transport=httpx.MockTransport(failing))
    doc, chapter = await MadaraSource(fetcher).open(WORK + "capitulo-99/")
    await fetcher.aclose()
    assert doc.chapters == []
    assert chapter.ref.index is None
    assert chapter.next_ref.locator.endswith("/capitulo-100/")


async def test_open_reports_adapter_and_index_page(source):
    """§4: abrir la página de la obra (índice) se distingue de abrir un capítulo."""
    work_doc, _ = await source.open(WORK)
    chapter_doc, _ = await source.open(WORK + "capitulo-99/")
    assert work_doc.adapter == chapter_doc.adapter == "madara"
    assert work_doc.index_page is True
    assert chapter_doc.index_page is False
