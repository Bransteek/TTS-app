"""Recuperación: archivos movidos o modificados y última sesión de lectura.

Todas las funciones son síncronas (leen disco y la base de datos): quien llama las
ejecuta con `asyncio.to_thread`.
"""
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import replace
from pathlib import Path

from lector.errors import FileMovedOrChanged
from lector.models import Document
from lector.persistence.repositories import ProgressRecord

log = logging.getLogger(__name__)

_BLOCK = 1 << 20


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(_BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def check_file(
    doc: Document, stored_path: str, stored_stat: tuple[int, float] | None = None
) -> Path:
    """Devuelve la ruta si el archivo sigue siendo el mismo; si no, lanza `FileMovedOrChanged`.

    Lanza si no existe, o si su tamaño o fecha de modificación difieren de `stored_stat`
    (tamaño, mtime) Y además su SHA-256 no coincide con `doc.file_hash`. Si tamaño y
    fecha coinciden no se calcula el hash. Sin `stored_stat` solo cuenta el hash.
    Síncrona: llamar con `asyncio.to_thread`.
    """
    path = Path(stored_path)
    try:
        stat = path.stat()
    except OSError as exc:
        raise FileMovedOrChanged(doc.doc_id) from exc
    if not path.is_file():
        raise FileMovedOrChanged(doc.doc_id)
    if stored_stat is not None and (stat.st_size, stat.st_mtime) == tuple(stored_stat):
        return path
    if not doc.file_hash:
        return path
    try:
        digest = _sha256(path)
    except OSError as exc:
        raise FileMovedOrChanged(doc.doc_id) from exc
    if digest != doc.file_hash:
        raise FileMovedOrChanged(doc.doc_id)
    return path


def relocate(doc: Document, new_path: str | os.PathLike) -> Document:
    """Documento con `origin` apuntando a `new_path`, si el archivo es el mismo.

    Valida que el SHA-256 de `new_path` sea `doc.file_hash`; si no (o no se puede leer),
    lanza `FileMovedOrChanged` (mensaje de usuario en español). No persiste: quien llama
    guarda con `DocumentRepo.update_origin`. Síncrona: llamar con `asyncio.to_thread`.
    """
    path = Path(new_path)
    try:
        digest = _sha256(path)
    except OSError as exc:
        raise FileMovedOrChanged(doc.doc_id) from exc
    if digest != doc.file_hash:
        raise FileMovedOrChanged(doc.doc_id)
    return replace(doc, origin=str(path))


def last_session(repos) -> ProgressRecord | None:
    """Registro de progreso más reciente, o None si nunca se leyó nada.

    Síncrona: llamar con `asyncio.to_thread`.
    """
    for _doc, progress in repos.documents.list_recent(limit=50):
        if progress is not None:
            return progress
    return None
