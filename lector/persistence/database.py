"""Apertura de la base SQLite, esquema y recuperación ante corrupción."""
from __future__ import annotations

import errno
import sqlite3
import threading
import time
from pathlib import Path

from ..errors import DatabaseCorrupt, DatabaseLocked, ResourceExhausted

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents(
    doc_id TEXT PRIMARY KEY, kind TEXT, origin TEXT, title TEXT, canonical_url TEXT,
    work_prefix TEXT, file_hash TEXT, file_size INTEGER, file_mtime REAL,
    chapters_json TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS chapters(
    doc_id TEXT, locator TEXT, idx INTEGER, title TEXT, text TEXT NULL,
    paragraphs_json TEXT NULL, next_locator TEXT NULL, prev_locator TEXT NULL,
    PRIMARY KEY(doc_id, locator));
CREATE TABLE IF NOT EXISTS progress(
    doc_id TEXT PRIMARY KEY REFERENCES documents, kind TEXT, origin TEXT, title TEXT,
    last_read_at REAL, chapter_index INTEGER, chapter_locator TEXT, chapter_url TEXT,
    page INTEGER, paragraph_index INTEGER, sentence_index INTEGER, char_offset INTEGER,
    last_chunk_id TEXT, edge_voice TEXT, piper_voice TEXT, speed REAL, session_state TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value_json TEXT);
CREATE TABLE IF NOT EXISTS chunk_log(
    id INTEGER PRIMARY KEY, chunk_id TEXT, engine TEXT, outcome TEXT, at REAL);
CREATE TABLE IF NOT EXISTS voices_cache(
    id INTEGER PRIMARY KEY CHECK(id=1), voices_json TEXT, fetched_at REAL);
"""


class LectorConnection(sqlite3.Connection):
    """Conexión con atributos extra (sqlite3.Connection no los admite)."""

    recovered_from_corruption: bool = False
    lock: threading.Lock

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.lock = threading.Lock()


def map_operational_error(exc: sqlite3.Error) -> Exception | None:
    """Traduce errores de SQLite a errores de dominio; None si no aplica."""
    msg = str(exc).lower()
    if isinstance(exc, sqlite3.OperationalError):
        if "database is locked" in msg:
            return DatabaseLocked()
        if "database or disk is full" in msg:
            return ResourceExhausted()
    return None


def _connect(path: Path) -> LectorConnection:
    conn = sqlite3.connect(
        str(path), factory=LectorConnection, check_same_thread=False, timeout=5.0
    )
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        row = conn.execute("PRAGMA quick_check").fetchone()
        if not row or row[0] != "ok":
            raise _QuickCheckFailed("quick_check failed")
    except BaseException:
        conn.close()
        raise
    return conn


def _quarantine(path: Path) -> None:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.corrupt-{stamp}")
    n = 1
    while target.exists():
        target = path.with_name(f"{path.name}.corrupt-{stamp}-{n}")
        n += 1
    path.rename(target)
    for suffix in ("-wal", "-shm"):
        side = path.with_name(path.name + suffix)
        if side.exists():
            side.rename(target.with_name(target.name + suffix))


_CORRUPTION_MARKERS = ("file is not a database", "malformed")


class _QuickCheckFailed(sqlite3.DatabaseError):
    pass


def _is_corruption(exc: sqlite3.Error) -> bool:
    if isinstance(exc, _QuickCheckFailed):
        return True
    msg = str(exc).lower()
    return any(m in msg for m in _CORRUPTION_MARKERS)


def open_database(path: Path) -> LectorConnection:
    path = Path(path)
    try:
        return _connect(path)
    except sqlite3.DatabaseError as exc:
        mapped = map_operational_error(exc)
        if mapped is not None:
            raise mapped from exc
        if not _is_corruption(exc):
            # Error ajeno a la integridad (solo lectura, E/S...): no se toca el archivo.
            raise DatabaseCorrupt(
                "No se pudo abrir la base de datos guardada. Revisa los permisos y el espacio en disco.",
                recoverable=False,
            ) from exc
        first = exc
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            raise ResourceExhausted() from exc
        raise
    try:
        _quarantine(path)
        conn = _connect(path)
    except (sqlite3.Error, OSError) as exc:
        raise DatabaseCorrupt() from first
    conn.recovered_from_corruption = True
    return conn
