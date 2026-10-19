import datetime
import email.utils
import socket

import httpx
import pytest

from lector.errors import InvalidUrl, PageUnreachable, ServerBusy
from lector.security.http_client import FetchResult, SafeFetcher
from lector.security.url_validator import ensure_public_ip, resolve_public, validate_url

HTML = {"content-type": "text/html; charset=utf-8"}


def fake_resolver(mapping: dict[str, str] | None = None):
    mapping = mapping or {}

    async def resolver(host: str, port: int) -> str:
        return mapping.get(host, "93.184.216.34")

    return resolver


def make_fetcher(handler, mapping=None, **kwargs) -> SafeFetcher:
    return SafeFetcher(
        resolver=fake_resolver(mapping),
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def test_validate_url_normalizes():
    assert validate_url("HTTP://Example.COM/Path?q=1#frag") == "http://example.com/Path?q=1"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x", "javascript:alert(1)", "http://", "", "http://u:p@a.com/"])
def test_rejects_non_http(url):
    with pytest.raises(InvalidUrl):
        validate_url(url)


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1", "::ffff:10.0.0.1", "0.0.0.0"])
async def test_rejects_private_targets(ip):
    # resolve_public con IP literal no toca la red
    with pytest.raises(InvalidUrl):
        await resolve_public(f"[{ip}]" if ":" in ip else ip, 80)


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1", "::ffff:10.0.0.1"])
async def test_fetcher_rejects_resolver_returning_private(ip):
    def handler(request):
        raise AssertionError("no debe haber petición")

    fetcher = make_fetcher(handler, {"evil.test": ip})
    with pytest.raises(InvalidUrl):
        await fetcher.get("http://evil.test/")
    await fetcher.aclose()


async def test_get_ok():
    def handler(request):
        return httpx.Response(200, text="hola ñ", headers=HTML)

    fetcher = make_fetcher(handler)
    result = await fetcher.get("https://Example.com/a#x")
    await fetcher.aclose()
    assert result == FetchResult(url="https://example.com/a", status=200, text="hola ñ", content_type="text/html")


async def test_user_agent_and_no_ajax_header_on_get():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, text="x", headers=HTML)

    fetcher = make_fetcher(handler)
    await fetcher.get("http://a.test/")
    await fetcher.aclose()
    assert "Chrome" in seen[0].headers["user-agent"]
    assert "x-requested-with" not in seen[0].headers


async def test_redirect_to_private_blocked():
    def handler(request):
        return httpx.Response(302, headers={"location": "http://localhost/"})

    fetcher = make_fetcher(handler, {"localhost": "127.0.0.1"})
    with pytest.raises(InvalidUrl):
        await fetcher.get("http://a.test/")
    await fetcher.aclose()


async def test_redirect_followed_relative():
    def handler(request):
        if request.url.path == "/a":
            return httpx.Response(301, headers={"location": "/b"})
        return httpx.Response(200, text="fin", headers=HTML)

    fetcher = make_fetcher(handler)
    result = await fetcher.get("http://a.test/a")
    await fetcher.aclose()
    assert result.url == "http://a.test/b" and result.text == "fin"


async def test_too_many_redirects():
    def handler(request):
        return httpx.Response(302, headers={"location": "http://a.test/next"})

    fetcher = make_fetcher(handler)
    with pytest.raises(PageUnreachable):
        await fetcher.get("http://a.test/")
    await fetcher.aclose()


async def test_size_limit():
    def handler(request):
        return httpx.Response(200, content=b"a" * 6_000_000, headers=HTML)

    fetcher = make_fetcher(handler)
    with pytest.raises(PageUnreachable):
        await fetcher.get("http://a.test/")
    await fetcher.aclose()


async def test_content_type_rejected():
    def handler(request):
        return httpx.Response(200, content=b"\x00", headers={"content-type": "application/pdf"})

    fetcher = make_fetcher(handler)
    with pytest.raises(PageUnreachable):
        await fetcher.get("http://a.test/")
    await fetcher.aclose()


async def test_5xx_is_recoverable():
    fetcher = make_fetcher(lambda request: httpx.Response(503))
    with pytest.raises(PageUnreachable) as exc:
        await fetcher.get("http://a.test/")
    await fetcher.aclose()
    assert exc.value.recoverable is True


async def test_4xx_not_recoverable():
    fetcher = make_fetcher(lambda request: httpx.Response(404))
    with pytest.raises(PageUnreachable) as exc:
        await fetcher.get("http://a.test/")
    await fetcher.aclose()
    assert exc.value.recoverable is False


async def test_network_error_is_recoverable():
    def handler(request):
        raise httpx.ConnectError("boom")

    fetcher = make_fetcher(handler)
    with pytest.raises(PageUnreachable) as exc:
        await fetcher.get("http://a.test/")
    await fetcher.aclose()
    assert exc.value.recoverable is True


async def test_post_sends_ajax_header():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, text="ok", headers=HTML)

    fetcher = make_fetcher(handler)
    result = await fetcher.post("http://a.test/ajax", data={"k": "v"})
    await fetcher.aclose()
    assert seen[0].method == "POST"
    assert seen[0].headers["x-requested-with"] == "XMLHttpRequest"
    assert seen[0].content == b"k=v"
    assert result.text == "ok"


async def test_size_limit_streaming_aborts_early():
    consumed = 0

    async def body():
        nonlocal consumed
        for _ in range(100):  # 100 MB potenciales, sin Content-Length
            consumed += 1
            yield b"a" * 1_000_000

    def handler(request):
        return httpx.Response(200, content=body(), headers=HTML)

    fetcher = make_fetcher(handler)
    with pytest.raises(PageUnreachable):
        await fetcher.get("http://a.test/")
    await fetcher.aclose()
    assert consumed < 10


async def test_unencodable_host_is_invalid_url():
    fetcher = make_fetcher(lambda request: httpx.Response(200, text="x", headers=HTML))
    with pytest.raises(InvalidUrl):
        await fetcher.get("http://☃.net/")
    await fetcher.aclose()


async def test_redirect_to_unencodable_host_is_invalid_url():
    def handler(request):
        return httpx.Response(302, headers=[(b"location", "http://☃.net/".encode())])

    fetcher = make_fetcher(handler)
    with pytest.raises(InvalidUrl):
        await fetcher.get("http://a.test/")
    await fetcher.aclose()


async def test_resolver_oserror_is_recoverable_unreachable():
    async def resolver(host, port):
        raise OSError("dns caído")

    fetcher = SafeFetcher(resolver=resolver, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with pytest.raises(PageUnreachable) as exc:
        await fetcher.get("http://a.test/")
    await fetcher.aclose()
    assert exc.value.recoverable is True


# --- DNS sin red: error recuperable, no «dirección no válida» (revisión final, crítico 1) ---


def _offline_getaddrinfo(*args, **kwargs):
    # Lo que devuelven macOS y Windows con el Wi-Fi apagado.
    raise socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided, or not known")


async def test_resolve_public_dns_failure_is_recoverable(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _offline_getaddrinfo)
    with pytest.raises(PageUnreachable) as exc:
        await resolve_public("aresscanlationovel.com", 443)
    assert not isinstance(exc.value, InvalidUrl)
    assert exc.value.recoverable is True


async def test_fetcher_with_real_resolver_offline_is_recoverable(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _offline_getaddrinfo)

    def handler(request):
        raise AssertionError("sin DNS no debe haber petición")

    fetcher = SafeFetcher(transport=httpx.MockTransport(handler))  # resolver real
    with pytest.raises(PageUnreachable) as exc:
        await fetcher.get("https://aresscanlationovel.com/novels/x/capitulo-1/")
    await fetcher.aclose()
    assert exc.value.recoverable is True


async def test_resolver_returning_private_ip_still_invalid(monkeypatch):
    def private(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port))]

    monkeypatch.setattr(socket, "getaddrinfo", private)
    with pytest.raises(InvalidUrl):
        await resolve_public("evil.test", 80)


async def test_host_that_cannot_be_encoded_is_invalid_url():
    # Etiqueta no ASCII de más de 63 caracteres: getaddrinfo falla al codificarla en IDNA,
    # antes de consultar la red.
    with pytest.raises(InvalidUrl):
        await resolve_public("ñ" * 64 + ".com", 80)


# --- 408/425/429: el servidor pide esperar (revisión final, importante 1) ---


@pytest.mark.parametrize("status", [408, 425, 429])
async def test_busy_statuses_are_recoverable(status):
    fetcher = make_fetcher(lambda request: httpx.Response(status))
    with pytest.raises(ServerBusy) as exc:
        await fetcher.get("http://a.test/")
    await fetcher.aclose()
    assert exc.value.recoverable is True
    assert exc.value.retry_after is None


@pytest.mark.parametrize("status", [429, 503])
async def test_retry_after_seconds(status):
    fetcher = make_fetcher(lambda request: httpx.Response(status, headers={"retry-after": "120"}))
    with pytest.raises(ServerBusy) as exc:
        await fetcher.get("http://a.test/")
    await fetcher.aclose()
    assert exc.value.retry_after == 120


async def test_retry_after_http_date():
    when = email.utils.format_datetime(
        datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=30), usegmt=True
    )
    fetcher = make_fetcher(lambda request: httpx.Response(429, headers={"retry-after": when}))
    with pytest.raises(ServerBusy) as exc:
        await fetcher.get("http://a.test/")
    await fetcher.aclose()
    assert 25 <= exc.value.retry_after <= 31


@pytest.mark.parametrize("value", ["pronto", "-5", "", "nan", "1e999", "Mon, 32 Foo 2026"])
async def test_retry_after_garbage_is_ignored(value):
    fetcher = make_fetcher(lambda request: httpx.Response(429, headers={"retry-after": value}))
    with pytest.raises(ServerBusy) as exc:
        await fetcher.get("http://a.test/")
    await fetcher.aclose()
    assert exc.value.retry_after is None


@pytest.mark.parametrize("status", [400, 403, 404, 410])
async def test_other_4xx_still_not_recoverable(status):
    fetcher = make_fetcher(lambda request: httpx.Response(status))
    with pytest.raises(PageUnreachable) as exc:
        await fetcher.get("http://a.test/")
    await fetcher.aclose()
    assert exc.value.recoverable is False


# --- Rangos no globales (CGNAT 100.64/10 y otros) (revisión final, menor 2) ---


@pytest.mark.parametrize(
    "ip", ["100.64.0.1", "100.127.255.254", "::ffff:100.64.0.1", "192.0.0.8", "198.18.0.1", "fc00::1"]
)
def test_non_global_ranges_rejected(ip):
    with pytest.raises(InvalidUrl):
        ensure_public_ip(ip)


@pytest.mark.parametrize("ip", ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"])
def test_global_ips_accepted(ip):
    assert ensure_public_ip(ip) == ip
