"""Tras una redirección, el localizador del capítulo es la URL final (sin red real)."""
from lector.content.generic_web import GenericWebSource
from lector.content.madara import MadaraSource
from lector.content.registry import open_source
from lector.security.http_client import FetchResult

TYPED = "https://example.com/c/123"
FINAL = "https://example.com/novela/x/cap-5/"
BODY = "".join(f"<p>Frase número {i} del capítulo de prueba, con texto suficiente.</p>" for i in range(8))
NAV = '<a class="prev_page" href="../cap-4/">ant</a><a class="next_page" href="../cap-6/">sig</a>'
MADARA = f'<html><body><div class="reading-content">{BODY}</div>{NAV}</body></html>'
GENERIC = (
    "<html><head><title>Cap 5</title></head><body><article>" + BODY + "</article>"
    '<a href="../cap-4/">Anterior</a><a href="../cap-6/">Siguiente</a></body></html>'
)


class RedirectFetcher:
    def __init__(self, html: str) -> None:
        self.html, self.calls = html, []

    async def get(self, url: str) -> FetchResult:
        self.calls.append(url)
        return FetchResult(FINAL, 200, self.html, "text/html")

    async def post(self, url: str) -> FetchResult:
        return FetchResult(url, 200, "<ul></ul>", "text/html")


async def test_madara_uses_final_url_as_locator():
    source, _, chapter = await open_source(TYPED, RedirectFetcher(MADARA))
    assert isinstance(source, MadaraSource)
    assert chapter.ref.locator == FINAL
    assert chapter.next_ref.locator == "https://example.com/novela/x/cap-6/"
    assert chapter.prev_ref.locator == "https://example.com/novela/x/cap-4/"


async def test_generic_uses_final_url_as_locator():
    source, _, chapter = await open_source(TYPED, RedirectFetcher(GENERIC))
    assert isinstance(source, GenericWebSource)
    assert chapter.ref.locator == FINAL
    assert chapter.next_ref.locator.startswith("https://example.com/novela/x/")
    assert chapter.prev_ref.locator.startswith("https://example.com/novela/x/")
