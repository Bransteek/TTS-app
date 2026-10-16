"""Cliente HTTP asíncrono seguro (anti-SSRF) para descargar páginas web.

Defensa contra DNS rebinding: OPCIÓN A (implementada). Cada salto se resuelve
una sola vez con el resolver, se valida la IP y la conexión se hace a esa IP
(`PinnedTransport`): se reescribe el host de la URL por la IP, se conserva la
cabecera `Host` original y se pasa la extensión httpcore `sni_hostname` para el
SNI y la verificación del certificado. httpx 0.28.1 / httpcore lo permiten
(`httpcore/_async/connection.py` lee `request.extensions["sni_hostname"]`).
Si se inyecta otro transporte (pruebas), se usa tal cual: la IP validada solo
viaja en la extensión `pinned_ip` y el transporte inyectado la ignora.
"""
from __future__ import annotations

import asyncio
import codecs
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

from ..errors import InvalidUrl, PageUnreachable
from .url_validator import ensure_public_ip, resolve_public, validate_url

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
ALLOWED_TYPES = {"text/html", "application/xhtml+xml", "text/plain"}
REDIRECT_CODES = {301, 302, 303, 307, 308}


@dataclass(frozen=True)
class FetchResult:
    url: str
    status: int
    text: str
    content_type: str


class PinnedTransport(httpx.AsyncBaseTransport):
    """Conecta a la IP validada (extensión `pinned_ip`) manteniendo Host y SNI originales."""

    def __init__(self) -> None:
        # Sin keep-alive: una conexión fijada a una IP no se reutiliza para otro host.
        self._inner = httpx.AsyncHTTPTransport(limits=httpx.Limits(max_keepalive_connections=0))

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        ip = request.extensions.get("pinned_ip")
        if ip:
            hostname = request.url.host
            request.extensions = {**request.extensions, "sni_hostname": hostname}
            request.url = request.url.copy_with(host=ip)  # la cabecera Host ya está fijada
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


class SafeFetcher:
    def __init__(
        self,
        max_bytes: int = 5_000_000,
        max_redirects: int = 5,
        timeout: float = 15.0,
        resolver=resolve_public,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._timeout = timeout
        self._resolver = resolver
        self._client = httpx.AsyncClient(
            transport=transport or PinnedTransport(),
            follow_redirects=False,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
        )

    async def get(self, url: str) -> FetchResult:
        return await self._fetch("GET", url, None)

    async def post(self, url: str, data: dict | None = None) -> FetchResult:
        return await self._fetch("POST", url, data or {})

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _fetch(self, method: str, url: str, data: dict | None) -> FetchResult:
        current = validate_url(url)
        for _ in range(self._max_redirects + 1):
            parts = urlsplit(current)
            port = parts.port or (443 if parts.scheme == "https" else 80)
            ip = ensure_public_ip(await self._resolver(parts.hostname, port))
            headers = {"X-Requested-With": "XMLHttpRequest"} if method == "POST" else {}
            request = self._client.build_request(
                method, current, data=data, headers=headers, extensions={"pinned_ip": ip}
            )
            try:
                async with asyncio.timeout(self._timeout):
                    response = await self._client.send(request, stream=True)
                    try:
                        location = response.headers.get("location")
                        if response.status_code in REDIRECT_CODES and location:
                            current = validate_url(urljoin(current, location))
                            if response.status_code in (301, 302, 303):
                                method, data = "GET", None
                            continue
                        return await self._read(current, response)
                    finally:
                        await response.aclose()
            except (httpx.HTTPError, TimeoutError, OSError):
                raise PageUnreachable(recoverable=True) from None
        raise PageUnreachable("La página redirige demasiadas veces.", recoverable=False)

    async def _read(self, url: str, response: httpx.Response) -> FetchResult:
        status = response.status_code
        if status >= 500:
            raise PageUnreachable(recoverable=True)
        if status >= 400:
            raise PageUnreachable(recoverable=False)
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type not in ALLOWED_TYPES:
            raise PageUnreachable("La dirección no apunta a una página de texto.", recoverable=False)
        too_big = PageUnreachable("La página es demasiado grande.", recoverable=False)
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > self._max_bytes:
            raise too_big
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > self._max_bytes:
                raise too_big
        text = bytes(body).decode(_codec(response.charset_encoding), errors="replace")
        return FetchResult(url=url, status=status, text=text, content_type=content_type)


def _codec(name: str | None) -> str:
    try:
        return codecs.lookup(name).name if name else "utf-8"
    except LookupError:
        return "utf-8"
