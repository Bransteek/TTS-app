"""Modelos de dominio compartidos por todas las capas."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class SourceKind(StrEnum):
    URL = "url"
    PDF = "pdf"
    EPUB = "epub"


@dataclass(frozen=True)
class ChapterRef:
    index: int | None
    title: str
    # URL absoluta (web), "spine:<href>" o "pages:<start>-<end>" (archivos).
    locator: str


@dataclass(frozen=True)
class Sentence:
    index: int  # global dentro del capítulo
    paragraph_index: int
    text: str
    start: int  # desplazamientos en Chapter.text
    end: int
    page: int | None


@dataclass(frozen=True)
class Paragraph:
    index: int
    text: str
    start: int
    page: int | None


@dataclass
class Chapter:
    ref: ChapterRef
    title: str
    text: str
    paragraphs: list[Paragraph]
    sentences: list[Sentence]
    next_ref: ChapterRef | None
    prev_ref: ChapterRef | None
    # Solo tiene elementos cuando el «siguiente» es ambiguo.
    next_candidates: list[ChapterRef] = field(default_factory=list)


@dataclass
class Document:
    doc_id: str
    kind: SourceKind
    origin: str
    title: str
    canonical_url: str | None
    work_prefix: str | None
    file_hash: str | None
    chapters: list[ChapterRef]


def make_chunk_id(
    doc_id: str,
    chapter_index: int,
    paragraph_index: int,
    first: Sentence,
    last: Sentence,
) -> str:
    return f"{doc_id}:{chapter_index}:{paragraph_index}:{first.index}-{last.index}"


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    doc_id: str
    chapter_index: int
    paragraph_index: int
    sentences: tuple[Sentence, ...]
    text: str
    start: int
    end: int


@dataclass(frozen=True)
class Position:
    chapter_index: int
    chapter_locator: str
    paragraph_index: int
    sentence_index: int
    char_offset: int
    page: int | None
