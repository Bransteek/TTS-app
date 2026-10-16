"""Recuperación: archivo movido o modificado, reubicación y última sesión."""
from __future__ import annotations

import hashlib
import os
from types import SimpleNamespace

import pytest

from lector.errors import FileMovedOrChanged
from lector.models import Document, Position, SourceKind
from lector.persistence.database import open_database
from lector.persistence.repositories import DocumentRepo, ProgressRecord, ProgressRepo
from lector.services import recovery


def _doc(path, content: bytes) -> Document:
    return Document("d1", SourceKind.PDF, str(path), "Libro", None, None,
                    hashlib.sha256(content).hexdigest(), [])


def _record(doc_id: str, at: float) -> ProgressRecord:
    return ProgressRecord(doc_id, SourceKind.PDF, f"/x/{doc_id}.pdf", doc_id, at,
                          Position(0, "p1", 0, 0, 0, 1), None, None, "v", "p", 1.0, "paused")


def test_moved_file_detected(tmp_path):
    f = tmp_path / "a.pdf"
    f.write_bytes(b"contenido")
    doc = _doc(f, b"contenido")
    assert recovery.check_file(doc, str(f)) == f
    f.rename(tmp_path / "b.pdf")
    with pytest.raises(FileMovedOrChanged):
        recovery.check_file(doc, str(f))


def test_modified_file_detected(tmp_path):
    f = tmp_path / "a.pdf"
    f.write_bytes(b"contenido")
    doc = _doc(f, b"contenido")
    st = f.stat()
    stored = (st.st_size, st.st_mtime)
    assert recovery.check_file(doc, str(f), stored) == f
    f.write_bytes(b"otro contenido distinto")
    with pytest.raises(FileMovedOrChanged):
        recovery.check_file(doc, str(f), stored)
    with pytest.raises(FileMovedOrChanged):
        recovery.check_file(doc, str(f))  # sin tamaño/fecha guardados: solo el hash


def test_touched_file_with_same_hash_is_accepted_and_fast_path_skips_hash(tmp_path, monkeypatch):
    f = tmp_path / "a.pdf"
    f.write_bytes(b"contenido")
    doc = _doc(f, b"contenido")
    st = f.stat()
    os.utime(f, (st.st_atime, st.st_mtime + 100))  # mtime cambia, el contenido no
    assert recovery.check_file(doc, str(f), (st.st_size, st.st_mtime)) == f
    # Tamaño y fecha iguales: no se lee el archivo para el hash.
    st = f.stat()
    monkeypatch.setattr(recovery, "_sha256", lambda p: pytest.fail("no debe calcular el hash"))
    assert recovery.check_file(doc, str(f), (st.st_size, st.st_mtime)) == f


def test_relocate_accepts_same_hash(tmp_path):
    old = tmp_path / "a.pdf"
    old.write_bytes(b"contenido")
    doc = _doc(old, b"contenido")
    new = tmp_path / "sub" / "b.pdf"
    new.parent.mkdir()
    new.write_bytes(b"contenido")
    moved = recovery.relocate(doc, new)
    assert moved.origin == str(new)
    assert moved.doc_id == doc.doc_id and moved.file_hash == doc.file_hash


def test_relocate_rejects_different_file(tmp_path):
    old = tmp_path / "a.pdf"
    old.write_bytes(b"contenido")
    doc = _doc(old, b"contenido")
    other = tmp_path / "otro.pdf"
    other.write_bytes(b"otra cosa")
    with pytest.raises(FileMovedOrChanged):
        recovery.relocate(doc, other)
    with pytest.raises(FileMovedOrChanged):
        recovery.relocate(doc, tmp_path / "no-existe.pdf")


def test_last_session(tmp_path):
    conn = open_database(tmp_path / "lector.db")
    documents, progress = DocumentRepo(conn), ProgressRepo(conn)
    repos = SimpleNamespace(documents=documents, progress=progress)
    assert recovery.last_session(repos) is None
    for doc_id, at in (("a", 100.0), ("b", 300.0), ("c", 200.0)):
        documents.upsert(Document(doc_id, SourceKind.PDF, f"/x/{doc_id}.pdf", doc_id, None,
                                  None, "h", []), None, 1, 1.0)
        progress.save(_record(doc_id, at))
    documents.upsert(Document("nuevo", SourceKind.PDF, "/x/n.pdf", "n", None, None, "h", []),
                     None, 1, 1.0)  # sin progreso: no cuenta
    assert recovery.last_session(repos).doc_id == "b"


def test_update_origin_and_file_stat_persist(tmp_path):
    conn = open_database(tmp_path / "lector.db")
    documents, progress = DocumentRepo(conn), ProgressRepo(conn)
    doc = Document("a", SourceKind.PDF, "/x/a.pdf", "a", None, None, "h", [])
    documents.upsert(doc, None, 5, 2.5)
    progress.save(_record("a", 1.0))
    assert documents.file_stat("a") == (5, 2.5)
    assert documents.file_stat("zz") is None
    documents.update_origin("a", "/y/a.pdf", 7, 3.5)
    assert documents.get("a").origin == "/y/a.pdf"
    assert documents.file_stat("a") == (7, 3.5)
    assert progress.get("a").origin == "/y/a.pdf"
