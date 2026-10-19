"""Validación de URL y resolución DNS anti-SSRF."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit

from ..errors import InvalidUrl, PageUnreachable


def validate_url(url: str) -> str:
    """Devuelve la URL normalizada (esquema y host en minúsculas, sin fragmento)."""
    try:
        parts = urlsplit((url or "").strip())
        host = parts.hostname
        parts.port  # noqa: B018 - lanza ValueError si el puerto es inválido
    except ValueError:
        raise InvalidUrl() from None
    if parts.scheme.lower() not in ("http", "https") or not host:
        raise InvalidUrl()
    if parts.username is not None or parts.password is not None:
        raise InvalidUrl()
    netloc = f"[{host}]" if ":" in host else host
    if parts.port is not None:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme.lower(), netloc, parts.path, parts.query, ""))


def ensure_public_ip(address: str) -> str:
    """Devuelve la IP si es pública; lanza InvalidUrl si no (incluye IPv4 mapeada en IPv6).

    Pública es «global» (`is_global`): así quedan fuera también CGNAT (100.64/10) y los
    demás rangos especiales. Multicast se comprueba aparte porque algunos rangos
    multicast cuentan como globales.
    """
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        raise InvalidUrl() from None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise InvalidUrl()
    return address


async def resolve_public(host: str, port: int) -> str:
    """Resuelve `host` y devuelve una IP pública; rechaza si alguna dirección no lo es.

    Un fallo del resolver (`socket.gaierror` y otros `OSError`) es lo que dan macOS y
    Windows sin red: es recuperable (`PageUnreachable`) para que la lectura espere y
    siga sola. Solo un nombre imposible de codificar o una IP no pública es `InvalidUrl`.
    """
    host = host.strip("[]")
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except UnicodeError:
        raise InvalidUrl() from None
    except OSError:  # incluye socket.gaierror
        raise PageUnreachable(recoverable=True) from None
    addresses = [info[4][0] for info in infos]
    if not addresses:
        raise PageUnreachable(recoverable=True)
    for address in addresses:  # una sola dirección privada invalida el host
        ensure_public_ip(address)
    return addresses[0]
