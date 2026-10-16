"""Repositorios sobre una conexión compartida protegida por un único Lock."""
from __future__ import annotations

import errno
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

from ..content.segmenter import build_chapter_text
from ..errors import DatabaseLocked, ResourceExhausted
from ..models import Chapter, ChapterRef, Document, Position, SourceKind
from .database import map_operational_error

T = TypeVar("T")

LOCK_RETRY_DELAYS = (0.2, 0.4, 0.8)

DEFAULT_SETTINGS: dict[str, Any] = {
    "edge_max_chars": 600,
    "edge_max_sentences": 3,
    "piper_max_chars": 300,
    "edge_retries": 3,
    "edge_backoff_s": [1, 2, 4],
    "edge_timeout_s": 15,
    "recovery_initial_s": 60,
    "recovery_max_s": 600,
    "piper_cooldown_s": 120,
    "wait_max_s": 60,
    "prefetch_ahead": 2,
    "queue_ahead_chunks": 2,
    "queue_max_seconds": 90,
    "skip_seconds": 10,
    "highlight_sentence": True,
    "continuous": True,
    "store_text": True,
    "http_max_bytes": 5_000_000,
    "http_max_redirects": 5,
    "edge_voice": "es-MX-DaliaNeural",
    "language": "es-MX",
    "speed": 1.0,
    "preferred_engine": "edge",
    "piper_voice": "es_ES-sharvard-medium",
    "piper_speaker": "F",
    "first_run_done": False,
    "work_prefix_overrides": {},
}


@dataclass
class ProgressRecord:
    doc_id: str
    kind: SourceKind
    origin: str
    title: str
    last_read_at: float
    position: Position
    chapter_url: str | None
    last_chunk_id: str | None
    edge_voice: str
    piper_voice: str
    speed: float
    session_state: str


class _Repo:
    """Base: ejecuta operaciones bajo el Lock con reintento y mapeo de errores."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        lock = getattr(conn, "lock", None)
        self._lock = lock if lock is not None else threading.Lock()

    @property
    def connection(self) -> sqlite3.Connection:
        """La conexión compartida (para crear otros repositorios sobre la misma base)."""
        return self._conn

    def _run(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        attempt = 0
        while True:
            try:
                with self._lock:
                    try:
                        result = fn(self._conn)
                        self._conn.commit()
                        return result
                    except BaseException:
                        self._conn.rollback()
                        raise
            except sqlite3.OperationalError as exc:
                mapped = map_operational_error(exc)
                if isinstance(mapped, DatabaseLocked):
                    if attempt < len(LOCK_RETRY_DELAYS):
                        time.sleep(LOCK_RETRY_DELAYS[attempt])
                        attempt += 1
                        continue
                    raise mapped from exc
                if mapped is not None:
                    raise mapped from exc
                raise
            except OSError as exc:
                if exc.errno == errno.ENOSPC:
                    raise ResourceExhausted() from exc
                raise


def _ref_to_list(ref: ChapterRef) -> list:
    return [ref.index, ref.title, ref.locator]


class DocumentRepo(_Repo):
    def upsert(
        self,
        doc: Document,
        origin_path: str | None,
        file_size: int | None,
        file_mtime: float | None,
    ) -> None:
        chapters_json = json.dumps([_ref_to_list(c) for c in doc.chapters], ensure_ascii=False)
        origin = origin_path if origin_path is not None else doc.origin

        def op(c: sqlite3.Connection) -> None:
            c.execute(
                """INSERT INTO documents(doc_id, kind, origin, title, canonical_url, work_prefix,
                       file_hash, file_size, file_mtime, chapters_json, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(doc_id) DO UPDATE SET kind=excluded.kind, origin=excluded.origin,
                       title=excluded.title, canonical_url=excluded.canonical_url,
                       work_prefix=excluded.work_prefix, file_hash=excluded.file_hash,
                       file_size=excluded.file_size, file_mtime=excluded.file_mtime,
                       chapters_json=excluded.chapters_json""",
                (doc.doc_id, str(doc.kind), origin, doc.title, doc.canonical_url,
                 doc.work_prefix, doc.file_hash, file_size, file_mtime, chapters_json,
                 time.time()),
            )

        self._run(op)

    @staticmethod
    def _row_to_doc(row: sqlite3.Row | tuple) -> Document:
        (doc_id, kind, origin, title, canonical_url, work_prefix, file_hash, chapters_json) = row
        chapters = [ChapterRef(i, t, loc) for i, t, loc in json.loads(chapters_json or "[]")]
        return Document(doc_id, SourceKind(kind), origin, title, canonical_url,
                        work_prefix, file_hash, chapters)

    _DOC_COLS = ("d.doc_id, d.kind, d.origin, d.title, d.canonical_url, d.work_prefix, "
                 "d.file_hash, d.chapters_json")

    def get(self, doc_id: str) -> Document | None:
        def op(c: sqlite3.Connection):
            return c.execute(
                f"SELECT {self._DOC_COLS} FROM documents d WHERE d.doc_id=?", (doc_id,)
            ).fetchone()

        row = self._run(op)
        return self._row_to_doc(row) if row else None

    def list_recent(self, limit: int = 50) -> list[tuple[Document, ProgressRecord | None]]:
        def op(c: sqlite3.Connection):
            return c.execute(
                f"""SELECT {self._DOC_COLS}, {_PROGRESS_COLS_P}
                    FROM documents d LEFT JOIN progress p ON p.doc_id = d.doc_id
                    ORDER BY COALESCE(p.last_read_at, d.created_at) DESC LIMIT ?""",
                (limit,),
            ).fetchall()

        out: list[tuple[Document, ProgressRecord | None]] = []
        for row in self._run(op):
            doc = self._row_to_doc(row[:8])
            prog = _row_to_progress(row[8:]) if row[8] is not None else None
            out.append((doc, prog))
        return out


_PROGRESS_FIELDS = (
    "doc_id, kind, origin, title, last_read_at, chapter_index, chapter_locator, chapter_url, "
    "page, paragraph_index, sentence_index, char_offset, last_chunk_id, edge_voice, "
    "piper_voice, speed, session_state"
)
_PROGRESS_COLS_P = ", ".join(f"p.{f.strip()}" for f in _PROGRESS_FIELDS.split(","))


def _row_to_progress(r) -> ProgressRecord:
    return ProgressRecord(
        doc_id=r[0], kind=SourceKind(r[1]), origin=r[2], title=r[3], last_read_at=r[4],
        position=Position(chapter_index=r[5], chapter_locator=r[6], paragraph_index=r[9],
                          sentence_index=r[10], char_offset=r[11], page=r[8]),
        chapter_url=r[7], last_chunk_id=r[12], edge_voice=r[13], piper_voice=r[14],
        speed=r[15], session_state=r[16],
    )


class ChapterRepo(_Repo):
    def save(self, doc_id: str, chapter: Chapter, store_text: bool) -> None:
        text = chapter.text if store_text else None
        paragraphs_json = (
            json.dumps([[p.text, p.page] for p in chapter.paragraphs], ensure_ascii=False)
            if store_text else None
        )
        next_loc = chapter.next_ref.locator if chapter.next_ref else None
        prev_loc = chapter.prev_ref.locator if chapter.prev_ref else None

        def op(c: sqlite3.Connection) -> None:
            c.execute(
                """INSERT OR REPLACE INTO chapters(doc_id, locator, idx, title, text,
                       paragraphs_json, next_locator, prev_locator) VALUES(?,?,?,?,?,?,?,?)""",
                (doc_id, chapter.ref.locator, chapter.ref.index, chapter.title, text,
                 paragraphs_json, next_loc, prev_loc),
            )

        self._run(op)

    def get_text(self, doc_id: str, locator: str) -> Chapter | None:
        def op(c: sqlite3.Connection):
            row = c.execute(
                """SELECT idx, title, text, paragraphs_json, next_locator, prev_locator
                   FROM chapters WHERE doc_id=? AND locator=?""",
                (doc_id, locator),
            ).fetchone()
            if row is None or row[2] is None or row[3] is None:
                return None
            doc = c.execute(
                "SELECT chapters_json FROM documents WHERE doc_id=?", (doc_id,)
            ).fetchone()
            return row, (doc[0] if doc else None)

        res = self._run(op)
        if res is None:
            return None
        (idx, title, _text, paragraphs_json, next_loc, prev_loc), chapters_json = res
        known = {loc: ChapterRef(i, t, loc)
                 for i, t, loc in json.loads(chapters_json or "[]")}

        def ref_for(loc: str | None) -> ChapterRef | None:
            if loc is None:
                return None
            return known.get(loc) or ChapterRef(None, "", loc)

        text, paragraphs, sentences = build_chapter_text(
            [(t, p) for t, p in json.loads(paragraphs_json)]
        )
        return Chapter(
            ref=known.get(locator) or ChapterRef(idx, title, locator),
            title=title, text=text, paragraphs=paragraphs, sentences=sentences,
            next_ref=ref_for(next_loc), prev_ref=ref_for(prev_loc),
        )

    def purge_text(self, doc_id: str) -> None:
        self._run(lambda c: c.execute(
            "UPDATE chapters SET text=NULL, paragraphs_json=NULL WHERE doc_id=?", (doc_id,)))


class ProgressRepo(_Repo):
    def save(self, rec: ProgressRecord) -> None:
        pos = rec.position

        def op(c: sqlite3.Connection) -> None:
            c.execute(
                f"""INSERT OR REPLACE INTO progress({_PROGRESS_FIELDS})
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rec.doc_id, str(rec.kind), rec.origin, rec.title, rec.last_read_at,
                 pos.chapter_index, pos.chapter_locator, rec.chapter_url, pos.page,
                 pos.paragraph_index, pos.sentence_index, pos.char_offset,
                 rec.last_chunk_id, rec.edge_voice, rec.piper_voice, rec.speed,
                 rec.session_state),
            )

        self._run(op)

    def get(self, doc_id: str) -> ProgressRecord | None:
        row = self._run(lambda c: c.execute(
            f"SELECT {_PROGRESS_FIELDS} FROM progress WHERE doc_id=?", (doc_id,)).fetchone())
        return _row_to_progress(row) if row else None


class SettingsRepo(_Repo):
    def get(self, key: str) -> Any:
        row = self._run(lambda c: c.execute(
            "SELECT value_json FROM settings WHERE key=?", (key,)).fetchone())
        if row is not None:
            try:
                return json.loads(row[0])
            except (TypeError, ValueError):
                pass
        default = DEFAULT_SETTINGS.get(key)
        # Copia para que el llamador no mute los valores por defecto compartidos.
        return json.loads(json.dumps(default))

    def set(self, key: str, value: Any) -> None:
        self._run(lambda c: c.execute(
            "INSERT OR REPLACE INTO settings(key, value_json) VALUES(?,?)",
            (key, json.dumps(value, ensure_ascii=False))))


class ChunkLogRepo(_Repo):
    def record(self, chunk_id: str, engine: str, outcome: str) -> None:
        self._run(lambda c: c.execute(
            "INSERT INTO chunk_log(chunk_id, engine, outcome, at) VALUES(?,?,?,?)",
            (chunk_id, engine, outcome, time.time())))

    def prune(self, older_than_days: int = 30) -> None:
        cutoff = time.time() - older_than_days * 86400
        self._run(lambda c: c.execute("DELETE FROM chunk_log WHERE at < ?", (cutoff,)))


class VoicesCacheRepo(_Repo):
    def save(self, voices: list[dict]) -> None:
        self._run(lambda c: c.execute(
            "INSERT OR REPLACE INTO voices_cache(id, voices_json, fetched_at) VALUES(1,?,?)",
            (json.dumps(voices, ensure_ascii=False), time.time())))

    def load(self) -> list[dict]:
        row = self._run(lambda c: c.execute(
            "SELECT voices_json FROM voices_cache WHERE id=1").fetchone())
        return json.loads(row[0]) if row else []
