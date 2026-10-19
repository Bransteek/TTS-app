import inspect

import pytest

from lector import errors
from lector.errors import (
    AmbiguousNextChapter,
    DatabaseCorrupt,
    FileMovedOrChanged,
    LectorError,
    PageUnreachable,
)
from lector.models import Chunk, Sentence, make_chunk_id
from lector.paths import data_dir, db_path, models_dir


def _all_error_classes():
    return [
        c
        for _, c in inspect.getmembers(errors, inspect.isclass)
        if issubclass(c, LectorError) and c is not LectorError
    ]


def _instantiate(cls):
    if cls is AmbiguousNextChapter:
        return cls(candidates=[])
    if cls is FileMovedOrChanged:
        return cls(doc_id="x")
    return cls()


def test_every_error_has_spanish_message():
    classes = _all_error_classes()
    assert len(classes) == 21
    for cls in classes:
        e = _instantiate(cls)
        assert isinstance(e, LectorError)
        assert e.user_message
        assert "Exception" not in e.user_message
        assert "Traceback" not in e.user_message


def test_recoverable_flags():
    assert PageUnreachable().recoverable is True
    assert DatabaseCorrupt().recoverable is False


def test_instance_overrides():
    e = PageUnreachable("Mensaje propio", recoverable=False)
    assert e.recoverable is False
    assert e.user_message == "Mensaje propio"
    assert PageUnreachable().recoverable is True


def test_data_dir_honours_env(tmp_path, monkeypatch):
    target = tmp_path / "datos"
    monkeypatch.setenv("LECTOR_DATA_DIR", str(target))
    assert data_dir() == target
    assert db_path() == target / "lector.db"
    assert models_dir().is_dir()
    assert models_dir() == target / "piper-voices"


def _sentence(i):
    return Sentence(index=i, paragraph_index=2, text=f"F{i}.", start=0, end=3, page=None)


def test_chunk_id_format():
    s = (_sentence(4), _sentence(5), _sentence(6))
    chunk = Chunk(
        chunk_id=make_chunk_id("doc", 3, 2, s[0], s[-1]),
        doc_id="doc",
        chapter_index=3,
        paragraph_index=2,
        sentences=s,
        text="F4. F5. F6.",
        start=0,
        end=11,
    )
    assert chunk.chunk_id == "doc:3:2:4-6"
