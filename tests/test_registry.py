import pytest

from lector.content.epub_source import EpubSource
from lector.content.generic_web import GenericWebSource
from lector.content.madara import MadaraSource
from lector.content.pdf_source import PdfSource
from lector.content.registry import open_source
from lector.errors import InvalidUrl
from lector.models import Chapter, Document, SourceKind
from lector.security.http_client import FetchResult

MADARA = (
    '<html><body><div class="reading-content"><p>Hola mundo de prueba.</p></div>'
    '<a class="next_page" href="/novels/x/capitulo-2/">sig</a></body></html>'
)
PLAIN = "<html><head><title>Cap 1</title></head><body><p>Hola mundo de prueba largo.</p></body></html>"


class FakeFetcher:
    def __init__(self, html: str) -> None:
        self.html = html
        self.calls: list[str] = []

    async def get(self, url: str) -> FetchResult:
        self.calls.append(url)
        return FetchResult(url, 200, self.html, "text/html")


@pytest.fixture(autouse=True)
def stub_adapters(monkeypatch):
    """Los adaptadores se sustituyen por dobles que anotan el HTML recibido."""
    seen = {}

    async def fake_open(self, locator, prefetched_html=None):
        seen["cls"], seen["html"], seen["locator"] = type(self), prefetched_html, locator
        return Document("id", SourceKind.URL, locator, "t", None, None, None, []), Chapter.__new__(Chapter)

    for cls in (MadaraSource, GenericWebSource, PdfSource, EpubSource):
        monkeypatch.setattr(cls, "open", fake_open)
    return seen


async def test_picks_madara_for_madara_html(stub_adapters):
    fetcher = FakeFetcher(MADARA)
    source, _, _ = await open_source("https://Example.com/novels/x/capitulo-1/", fetcher)
    assert isinstance(source, MadaraSource)
    assert stub_adapters["html"] == MADARA and fetcher.calls == ["https://example.com/novels/x/capitulo-1/"]


async def test_picks_generic_otherwise(stub_adapters):
    fetcher = FakeFetcher(PLAIN)
    source, _, _ = await open_source("https://example.com/c/1", fetcher)
    assert isinstance(source, GenericWebSource)
    assert stub_adapters["html"] == PLAIN and len(fetcher.calls) == 1


@pytest.mark.parametrize(
    "locator, cls",
    [("a.pdf", PdfSource), ("a.PDF", PdfSource), ("a.epub", EpubSource), ("B.Epub", EpubSource)],
)
async def test_picks_pdf_and_epub_by_extension(locator, cls, stub_adapters, tmp_path):
    locator = str(tmp_path / locator)
    (tmp_path / locator).write_bytes(b"x")
    fetcher = FakeFetcher(PLAIN)
    source, _, _ = await open_source(locator, fetcher)
    assert isinstance(source, cls)
    assert fetcher.calls == [] and stub_adapters["html"] is None


@pytest.mark.parametrize("bad", ["ftp://example.com/x", "no es url", "javascript:alert(1)", ""])
async def test_invalid_url_raises(bad):
    with pytest.raises(InvalidUrl):
        await open_source(bad, FakeFetcher(PLAIN))


async def test_url_ending_in_pdf_takes_web_path(stub_adapters):
    fetcher = FakeFetcher(PLAIN)
    source, _, _ = await open_source("https://example.com/libro.PDF", fetcher)
    assert isinstance(source, GenericWebSource) and len(fetcher.calls) == 1


@pytest.mark.parametrize("name", ["falta.pdf", "falta.epub"])
async def test_missing_file_message(name, tmp_path):
    from lector.errors import LectorError

    with pytest.raises(LectorError) as info:
        await open_source(str(tmp_path / name), FakeFetcher(PLAIN))
    assert info.value.user_message == "No se encontró el archivo. Comprueba que sigue en su sitio."


async def test_generic_source_gets_prefix_override_for_its_host(stub_adapters):
    overrides = {"example.com": "/obras/x/", "otra.example": "/no/"}  # formato antiguo
    source, _, _ = await open_source(
        "https://Example.com/obras/x/c/1", FakeFetcher(PLAIN), prefix_overrides=overrides
    )
    assert isinstance(source, GenericWebSource)
    assert source._override == "/obras/x/"
    source, _, _ = await open_source("https://example.com/obras/x/c/1", FakeFetcher(PLAIN))
    assert source._override is None


async def test_prefix_override_only_for_the_work_it_belongs_to(stub_adapters):
    overrides = {"example.com": ["/obras/x/"]}
    source, _, _ = await open_source(
        "https://example.com/obras/x/cap-1", FakeFetcher(PLAIN), prefix_overrides=overrides
    )
    assert source._override == "/obras/x/"
    source, _, _ = await open_source(
        "https://example.com/obras/y/cap-1", FakeFetcher(PLAIN), prefix_overrides=overrides
    )
    assert source._override is None  # otra obra del mismo host: detección normal


class RedirectFetcher(FakeFetcher):
    """El servidor redirige: la página llega desde otra dirección."""

    def __init__(self, html: str, final_url: str) -> None:
        super().__init__(html)
        self.final_url = final_url

    async def get(self, url: str) -> FetchResult:
        self.calls.append(url)
        return FetchResult(self.final_url, 200, self.html, "text/html")


@pytest.mark.parametrize(
    ("typed", "final", "expected"),
    [
        # Enlace corto que redirige al capítulo: vale la URL final.
        ("https://example.com/c/123", "https://example.com/obras/x/capitulo-5/", "/obras/x/"),
        # La URL escrita está bajo el prefijo y la final no: vale la escrita.
        ("https://example.com/obras/x/capitulo-5", "https://example.com/ver?id=5", "/obras/x/"),
        # Las dos están bajo prefijos distintos: gana el más largo.
        ("https://example.com/obras/c-5", "https://example.com/obras/x/c-5/", "/obras/x/"),
        # Ninguna: detección normal.
        ("https://example.com/c/123", "https://example.com/otra/c-5/", None),
    ],
)
async def test_prefix_override_matches_typed_or_redirected_url(stub_adapters, typed, final,
                                                               expected):
    overrides = {"example.com": ["/obras/", "/obras/x/"]} if "obras/c-5" in typed else {
        "example.com": ["/obras/x/"]}
    source, _, _ = await open_source(
        typed, RedirectFetcher(PLAIN, final), prefix_overrides=overrides
    )
    assert isinstance(source, GenericWebSource)
    assert source._override == expected
