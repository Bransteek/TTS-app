"""Recuperación de red con los componentes reales: SafeFetcher + MadaraSource + navegador.

Revisión final, crítico 1 e importante 1. Nada toca la red: `socket.getaddrinfo` se
sustituye por uno que falla con el `socket.gaierror` real (lo que dan macOS y Windows
sin Wi-Fi) y el sitio es un `httpx.MockTransport` con las páginas guardadas.
"""
from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import httpx
import pytest

from lector.content.madara import MadaraSource
from lector.content.registry import open_source
from lector.errors import InvalidUrl, PageUnreachable
from lector.models import ChapterRef
from lector.persistence.database import open_database
from lector.persistence.repositories import ChapterRepo
from lector.security.http_client import SafeFetcher
from lector.services.document_opener import DeferredSource
from lector.services.navigator import ChapterNavigator

FIX = Path(__file__).parent / "fixtures" / "madara"
WORK = (
    "https://aresscanlationovel.com/novels/"
    "a-partir-de-ahora-pedir-matrimonio-es-la-unica-forma-de-sobrevivir/"
)
CH99 = WORK + "capitulo-99/"
CH100 = WORK + "capitulo-100/"
HTML = {"content-type": "text/html; charset=utf-8"}
PUBLIC_IP = "93.184.216.34"


def read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


class Site:
    """El sitio Madara de las pruebas; `override[url]` da respuestas especiales en orden."""

    def __init__(self) -> None:
        self.override: dict[str, list[httpx.Response]] = {}
        self.gets: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url).replace(PUBLIC_IP, "aresscanlationovel.com")
        if request.method == "GET":
            self.gets.append(url)
        queued = self.override.get(url)
        if queued:
            return queued.pop(0)
        if request.method == "POST" and url == WORK + "ajax/chapters/":
            return httpx.Response(200, text=read("chapters_ajax.html"), headers=HTML)
        if request.method == "GET" and url.startswith(WORK + "capitulo-"):
            n = url.removeprefix(WORK + "capitulo-").rstrip("/")
            if (FIX / f"capitulo-{n}.html").exists():
                return httpx.Response(200, text=read(f"capitulo-{n}.html"), headers=HTML)
        return httpx.Response(404, text="no", headers=HTML)


class Dns:
    """`socket.getaddrinfo` falso: sin red lanza el gaierror real; con red, una IP pública."""

    def __init__(self) -> None:
        self.down = False
        self.failures = 0

    def __call__(self, host, port, family=0, type=0, proto=0, flags=0):
        if self.down:
            self.failures += 1
            raise socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided, or not known")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, port))]


class Clock:
    """Espera falsa; tras `restore_after` esperas vuelve la red."""

    def __init__(self, dns: Dns, restore_after: int | None = None) -> None:
        self.dns = dns
        self.restore_after = restore_after
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if self.restore_after is not None and len(self.sleeps) >= self.restore_after:
            self.dns.down = False
        await asyncio.sleep(0)


@pytest.fixture
def dns(monkeypatch):
    fake = Dns()
    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return fake


@pytest.fixture
def site():
    return Site()


@pytest.fixture
async def fetcher(site):
    fetcher = SafeFetcher(transport=httpx.MockTransport(site))  # resolver real (resolve_public)
    yield fetcher
    await fetcher.aclose()


@pytest.fixture
def repo(tmp_path):
    conn = open_database(tmp_path / "lector.db")
    yield ChapterRepo(conn)
    conn.close()


def navigator(source, doc, repo, clock: Clock) -> tuple[ChapterNavigator, list[bool]]:
    nav = ChapterNavigator(source, doc, repo, store_text=False, sleep=clock.sleep)
    waits: list[bool] = []
    nav.on_waiting = waits.append
    return nav, waits


# --- Crítico 1: DNS caído ----------------------------------------------------------------


async def test_offline_dns_chapter_change_waits_and_resumes(dns, fetcher, repo):
    source = MadaraSource(fetcher)
    doc, current = await source.open(CH99)
    dns.down = True  # se cae el Wi-Fi mientras suena el capítulo 99
    clock = Clock(dns, restore_after=3)
    nav, waits = navigator(source, doc, repo, clock)

    nxt = await nav.next_of(current)

    assert nxt.ref.locator == CH100
    assert dns.failures == 3
    assert clock.sleeps == [1, 2, 4]
    assert waits == [True, False]  # «Esperando conexión» y vuelta a leer, sin error
    await nav.close()


async def test_offline_dns_prefetch_keeps_retrying_until_network_returns(dns, fetcher, repo, site):
    source = MadaraSource(fetcher)
    doc, current = await source.open(CH99)
    dns.down = True
    clock = Clock(dns, restore_after=5)
    nav, waits = navigator(source, doc, repo, clock)

    nav.start_prefetch(current)
    await nav._prefetch_task

    assert list(nav.prefetch_errors) == []
    assert dns.failures == 5
    gets = len(site.gets)
    assert (await nav.next_of(current)).ref.locator == CH100
    assert len(site.gets) == gets  # servido desde la precarga
    assert waits == []  # la precarga no avisa
    await nav.close()


async def test_continue_from_history_offline_waits_and_resumes(dns, fetcher, repo):
    # Abrir y guardar el documento con red, como en una sesión anterior.
    _, doc, _ = await open_source(CH99, fetcher)
    dns.down = True
    deferred = DeferredSource(lambda locator, f: open_source(locator, f), fetcher, doc)
    clock = Clock(dns, restore_after=2)
    nav, waits = navigator(deferred, doc, repo, clock)

    chapter = await nav.get(ChapterRef(99, "Capítulo 100", CH100))

    assert chapter.ref.locator == CH100
    assert dns.failures == 2
    assert waits == [True, False]
    await nav.close()


async def test_continue_from_history_retries_a_page_without_text(dns, fetcher, repo, site):
    _, doc, _ = await open_source(CH99, fetcher)
    # Primera respuesta: una página vacía, que no parece Madara y no tiene texto.
    site.override[CH100] = [httpx.Response(200, text="<html><body></body></html>", headers=HTML)]
    deferred = DeferredSource(lambda locator, f: open_source(locator, f), fetcher, doc)
    clock = Clock(dns)
    nav, waits = navigator(deferred, doc, repo, clock)

    chapter = await nav.get(ChapterRef(99, "Capítulo 100", CH100))

    assert chapter.ref.locator == CH100 and chapter.paragraphs
    assert clock.sleeps == [1]
    assert waits == [True, False]
    await nav.close()


async def test_private_address_still_ends_with_invalid_url(dns, monkeypatch, fetcher, repo):
    source = MadaraSource(fetcher)
    doc, current = await source.open(CH99)

    def private(host, port, *args, **kwargs):  # el nombre pasa a apuntar a la red local
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", port))]

    monkeypatch.setattr(socket, "getaddrinfo", private)
    clock = Clock(dns)
    nav, waits = navigator(source, doc, repo, clock)

    with pytest.raises(InvalidUrl):
        await nav.next_of(current)
    assert clock.sleeps == [] and waits == []
    await nav.close()


# --- Importante 1: 429, cuerpo vacío y página sin .reading-content -----------------------


async def test_rate_limited_chapter_honours_retry_after(dns, fetcher, repo, site):
    source = MadaraSource(fetcher)
    doc, current = await source.open(CH99)
    site.override[CH100] = [httpx.Response(429, headers={"retry-after": "7"})]
    clock = Clock(dns)
    nav, waits = navigator(source, doc, repo, clock)

    nxt = await nav.next_of(current)

    assert nxt.ref.locator == CH100
    assert clock.sleeps == [7]
    assert waits == [True, False]
    await nav.close()


async def test_retry_after_is_capped_by_max_wait(dns, fetcher, repo, site):
    source = MadaraSource(fetcher)
    doc, current = await source.open(CH99)
    site.override[CH100] = [httpx.Response(503, headers={"retry-after": "86400"})]
    clock = Clock(dns)
    nav, _ = navigator(source, doc, repo, clock)

    await nav.next_of(current)

    assert clock.sleeps == [60]
    await nav.close()


@pytest.mark.parametrize("body", ["", "   \n  "])
async def test_empty_200_body_is_retried(dns, fetcher, repo, site, body):
    source = MadaraSource(fetcher)
    doc, current = await source.open(CH99)
    site.override[CH100] = [httpx.Response(200, text=body, headers=HTML)]
    clock = Clock(dns)
    nav, waits = navigator(source, doc, repo, clock)

    nxt = await nav.next_of(current)

    assert nxt.ref.locator == CH100
    assert clock.sleeps == [1]
    assert waits == [True, False]
    await nav.close()


INTERSTITIAL = "<html><body><p>Un momento, comprobando tu navegador…</p></body></html>"


async def test_page_without_reading_content_is_retried(dns, fetcher, repo, site):
    source = MadaraSource(fetcher)
    doc, current = await source.open(CH99)
    site.override[CH100] = [httpx.Response(200, text=INTERSTITIAL, headers=HTML)] * 2
    clock = Clock(dns)
    nav, waits = navigator(source, doc, repo, clock)

    nxt = await nav.next_of(current)

    assert nxt.ref.locator == CH100
    assert clock.sleeps == [1, 2]
    assert waits == [True, False]
    await nav.close()


async def test_page_without_reading_content_gives_up_after_about_a_minute(dns, fetcher, repo, site):
    source = MadaraSource(fetcher)
    doc, current = await source.open(CH99)
    site.override[CH100] = [httpx.Response(200, text=INTERSTITIAL, headers=HTML)] * 50
    clock = Clock(dns)
    nav, waits = navigator(source, doc, repo, clock)

    with pytest.raises(PageUnreachable) as exc:
        await nav.next_of(current)

    assert exc.value.recoverable is False
    assert exc.value.user_message == "No se encontró el texto del capítulo."
    assert 60 <= sum(clock.sleeps) <= 70
    assert len(clock.sleeps) <= 7
    assert waits == [True, False]
    await nav.close()


async def test_prefetch_gives_up_on_page_without_text(dns, fetcher, repo, site):
    source = MadaraSource(fetcher)
    doc, current = await source.open(CH99)
    site.override[CH100] = [httpx.Response(200, text=INTERSTITIAL, headers=HTML)] * 50
    clock = Clock(dns)
    nav, waits = navigator(source, doc, repo, clock)

    nav.start_prefetch(current)
    await nav._prefetch_task

    [error] = nav.prefetch_errors
    assert isinstance(error, PageUnreachable) and error.recoverable is False
    assert 60 <= sum(clock.sleeps) <= 70
    assert waits == []
    await nav.close()


# --- DNS que no contesta: el mismo tope de tiempo que la petición ------------------------

DNS_TIMEOUT_S = 0.2  # tope corto e inyectado: la prueba no espera los 15 s reales
HUNG_DNS_MAX_S = 3.0  # el hilo del resolver se suelta solo pase lo que pase


class HungDns(Dns):
    """`socket.getaddrinfo` que no vuelve mientras `hung` (como macOS al volver el Wi-Fi)."""

    def __init__(self) -> None:
        super().__init__()
        self.hung = False
        self.release = __import__("threading").Event()
        self.calls = 0

    def __call__(self, host, port, family=0, type=0, proto=0, flags=0):
        self.calls += 1
        if self.hung:
            self.release.wait(HUNG_DNS_MAX_S)
        return super().__call__(host, port, family, type, proto, flags)


@pytest.fixture
def hung_dns(monkeypatch):
    fake = HungDns()
    monkeypatch.setattr(socket, "getaddrinfo", fake)
    yield fake
    fake.release.set()


@pytest.fixture
async def quick_fetcher(site):
    fetcher = SafeFetcher(transport=httpx.MockTransport(site), timeout=DNS_TIMEOUT_S)
    yield fetcher
    await fetcher.aclose()


async def test_dns_that_never_answers_is_a_recoverable_network_error(hung_dns, quick_fetcher):
    hung_dns.hung = True
    try:
        start = asyncio.get_running_loop().time()
        with pytest.raises(PageUnreachable) as caught:
            await asyncio.wait_for(quick_fetcher.get(CH99), 2)
        elapsed = asyncio.get_running_loop().time() - start
    finally:
        hung_dns.release.set()
    assert caught.value.recoverable
    assert elapsed < 1.0  # el tope de la petición, no la espera del resolver


async def test_open_with_dns_that_never_answers_fails_recoverably(hung_dns, quick_fetcher,
                                                                  controller_rig):
    rig = controller_rig(fetcher=quick_fetcher, opener=open_source)
    hung_dns.hung = True
    try:
        with pytest.raises(PageUnreachable) as caught:
            await asyncio.wait_for(rig.ctl.open(CH99), 2)
    finally:
        hung_dns.release.set()
    assert caught.value.recoverable  # «Leer» deja de estar en «Abriendo…» y avisa


class UnhangClock:
    """Espera falsa: tras la primera espera el DNS vuelve a contestar."""

    def __init__(self, dns: HungDns) -> None:
        self.dns = dns
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.dns.hung = False
        self.dns.release.set()
        await asyncio.sleep(0)


async def test_next_chapter_with_dns_that_never_answers_waits_and_resumes(hung_dns, quick_fetcher,
                                                                          repo):
    source = MadaraSource(quick_fetcher)
    doc, current = await source.open(CH99)
    hung_dns.hung = True  # el resolver se cuelga justo al cambiar de capítulo
    clock = UnhangClock(hung_dns)
    nav = ChapterNavigator(source, doc, repo, store_text=False, sleep=clock.sleep)
    waits: list[bool] = []
    nav.on_waiting = waits.append
    try:
        nxt = await asyncio.wait_for(nav.next_of(current), 2)
    finally:
        hung_dns.release.set()

    assert nxt.ref.locator == CH100
    assert waits == [True, False]  # avisó de la espera (WAITING_NETWORK) en vez de callar
    assert clock.sleeps == [1]
