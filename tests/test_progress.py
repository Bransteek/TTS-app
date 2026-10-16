from __future__ import annotations

import sqlite3
import time

import pytest

from lector.content.segmenter import build_chapter_text
from lector.errors import DatabaseLocked, ResourceExhausted
from lector.models import Chapter, ChapterRef, Document, Position, SourceKind
from lector.persistence import repositories
from lector.persistence.database import open_database
from lector.persistence.repositories import (
    ChapterRepo, ChunkLogRepo, DocumentRepo, ProgressRecord, ProgressRepo,
    SettingsRepo, VoicesCacheRepo,
)

REFS = [ChapterRef(0, "Uno", "https://x/1"), ChapterRef(1, "Dos", "https://x/2")]


def make_doc() -> Document:
    return Document("doc1", SourceKind.URL, "https://x/1", "Novela", "https://x/1",
                    "https://x/", None, list(REFS))


def make_chapter() -> Chapter:
    text, paras, sents = build_chapter_text(
        [("Hola mundo. Segunda frase.", None), ("Otro párrafo aquí.", 3)])
    return Chapter(REFS[0], "Uno", text, paras, sents, REFS[1], None)


def make_record(**kw) -> ProgressRecord:
    base = dict(
        doc_id="doc1", kind=SourceKind.URL, origin="https://x/1", title="Novela",
        last_read_at=1000.5, position=Position(1, "https://x/2", 2, 5, 17, 3),
        chapter_url="https://x/2", last_chunk_id="doc1:1:2:5-5", edge_voice="es-MX-DaliaNeural",
        piper_voice="p", speed=1.25, session_state="PAUSED")
    base.update(kw)
    return ProgressRecord(**base)


@pytest.fixture
def conn(tmp_path):
    c = open_database(tmp_path / "lector.db")
    yield c
    c.close()


def test_progress_roundtrip(conn):
    DocumentRepo(conn).upsert(make_doc(), None, 10, 1.0)
    repo = ProgressRepo(conn)
    rec = make_record()
    repo.save(rec)
    assert repo.get("doc1") == rec
    assert repo.get("nope") is None
    listed = DocumentRepo(conn).list_recent()
    assert listed[0][0] == make_doc() and listed[0][1] == rec


def test_progress_survives_reopen(tmp_path):
    path = tmp_path / "lector.db"
    c = open_database(path)
    DocumentRepo(c).upsert(make_doc(), None, None, None)
    rec = make_record()
    ProgressRepo(c).save(rec)
    c.close()
    c2 = open_database(path)
    try:
        assert not c2.recovered_from_corruption
        assert ProgressRepo(c2).get("doc1") == rec
    finally:
        c2.close()


def test_chapter_text_rebuilds_identically(conn):
    DocumentRepo(conn).upsert(make_doc(), None, None, None)
    ch = make_chapter()
    repo = ChapterRepo(conn)
    repo.save("doc1", ch, True)
    got = repo.get_text("doc1", ch.ref.locator)
    assert got.text == ch.text and got.sentences == ch.sentences
    assert got.paragraphs == ch.paragraphs and got.next_ref == REFS[1]
    repo.purge_text("doc1")
    assert repo.get_text("doc1", ch.ref.locator) is None


def test_store_text_false_keeps_no_text(conn):
    ch = make_chapter()
    repo = ChapterRepo(conn)
    repo.save("doc1", ch, False)
    row = conn.execute("SELECT text, paragraphs_json, title FROM chapters WHERE doc_id='doc1'").fetchone()
    assert row[0] is None and row[1] is None and row[2] == "Uno"
    assert repo.get_text("doc1", ch.ref.locator) is None


def test_corrupt_db_is_quarantined(tmp_path):
    path = tmp_path / "lector.db"
    path.write_bytes(b"\x00garbage not sqlite" * 200)
    c = open_database(path)
    try:
        assert c.recovered_from_corruption is True
        assert list(tmp_path.glob("lector.db.corrupt-*"))
        assert c.execute("SELECT count(*) FROM documents").fetchone()[0] == 0
    finally:
        c.close()


def test_settings_defaults(conn):
    s = SettingsRepo(conn)
    assert s.get("edge_voice") == "es-MX-DaliaNeural"
    assert s.get("speed") == 1.0
    assert s.get("edge_retries") == 3
    assert s.get("preferred_engine") == "edge" and s.get("first_run_done") is False
    assert s.get("work_prefix_overrides") == {}
    s.set("speed", 1.5)
    s.set("work_prefix_overrides", {"a": "b"})
    assert s.get("speed") == 1.5 and s.get("work_prefix_overrides") == {"a": "b"}


def test_chunk_log_prune(conn):
    log = ChunkLogRepo(conn)
    log.record("old", "edge", "ok")
    log.record("new", "piper", "fail")
    conn.execute("UPDATE chunk_log SET at=? WHERE chunk_id='old'", (time.time() - 40 * 86400,))
    log.prune(30)
    ids = [r[0] for r in conn.execute("SELECT chunk_id FROM chunk_log")]
    assert ids == ["new"]


def test_voices_cache(conn):
    v = VoicesCacheRepo(conn)
    assert v.load() == []
    v.save([{"name": "a"}])
    v.save([{"name": "b"}])
    assert v.load() == [{"name": "b"}]


class _FailingConn:
    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def execute(self, *a, **k):
        self.calls += 1
        raise self.exc

    def commit(self): pass
    def rollback(self): pass


def test_disk_full_maps_to_resource_exhausted():
    repo = SettingsRepo(_FailingConn(sqlite3.OperationalError("database or disk is full")))
    with pytest.raises(ResourceExhausted):
        repo.set("speed", 2.0)


def test_enospc_maps_to_resource_exhausted():
    import errno
    repo = SettingsRepo(_FailingConn(OSError(errno.ENOSPC, "no space")))
    with pytest.raises(ResourceExhausted):
        repo.set("speed", 2.0)


def test_locked_retries_then_raises(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(repositories.time, "sleep", sleeps.append)
    fake = _FailingConn(sqlite3.OperationalError("database is locked"))
    with pytest.raises(DatabaseLocked):
        SettingsRepo(fake).set("speed", 2.0)
    assert sleeps == [0.2, 0.4, 0.8] and fake.calls == 4
