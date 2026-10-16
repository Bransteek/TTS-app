"""Fuente de contenido para PDF de texto (PyMuPDF), dividido en capítulos."""
from __future__ import annotations

import asyncio
import hashlib
import math
import re
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from lector.content.base import ContentSource
from lector.content.segmenter import build_chapter_text
from lector.errors import DamagedDocument, ScannedPdf
from lector.models import Chapter, ChapterRef, Document, SourceKind

_HEADING = re.compile(
    r"^(cap[ií]tulo|prólogo|prologo|epílogo|epilogo|interludio|parte|volumen)\b", re.I
)
_LOCATOR = re.compile(r"^pages:(\d+)-(\d+)$")
_ONLY_NUMBER = re.compile(r"^\d+$")
_DIGITS = re.compile(r"\d")
_HYPHEN_END = re.compile(r"[^\W\d_]-$")
_HEADING_SIZE_RATIO = 1.2
_MARGIN_RATIO = 0.08
_REPEAT_RATIO = 0.30
_SCAN_MIN_CHARS = 20
_SCAN_MIN_PAGE_RATIO = 0.10
_WHOLE_DOCUMENT = "Documento completo"


def file_doc_id(path: Path) -> tuple[str, str]:
    """(doc_id, sha256): doc_id son los 16 primeros hex del SHA-256 del contenido."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    full = digest.hexdigest()
    return full[:16], full


@dataclass(frozen=True)
class _Line:
    text: str
    size: float
    y0: float
    y1: float


@dataclass
class _Page:
    number: int  # real, desde 1
    height: float
    blocks: list[list[_Line]]  # solo bloques de texto


@dataclass
class _Extracted:
    title: str
    pages: list[_Page]
    toc: list[tuple[str, int]]  # entradas de nivel 1: (título, página)


def _normalize(text: str) -> str:
    return _DIGITS.sub("#", re.sub(r"\s+", " ", text).strip())


def _read_pdf(path: Path) -> _Extracted:
    """Trabajo bloqueante de PyMuPDF; se ejecuta en un hilo."""
    try:
        doc = pymupdf.open(path)
    except Exception as exc:
        raise DamagedDocument() from exc
    try:
        if doc.needs_pass and not doc.authenticate(""):
            raise DamagedDocument("El PDF está protegido con contraseña y no se puede abrir.")
        if doc.page_count == 0:
            raise DamagedDocument()
        try:
            pages = [_read_page(page) for page in doc]
            toc = [(t.strip(), p) for lvl, t, p, *_ in doc.get_toc() if lvl == 1 and t.strip()]
        except Exception as exc:
            raise DamagedDocument() from exc
        title = (doc.metadata or {}).get("title") or ""
        return _Extracted(title.strip(), pages, toc)
    finally:
        doc.close()


def _read_page(page: pymupdf.Page) -> _Page:
    blocks: list[list[_Line]] = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        lines = []
        for line in block["lines"]:
            spans = [s for s in line["spans"] if s["text"].strip()]
            text = "".join(s["text"] for s in line["spans"]).strip()
            if not text:
                continue
            size = max(s["size"] for s in spans)
            lines.append(_Line(text, round(size, 1), line["bbox"][1], line["bbox"][3]))
        if lines:
            blocks.append(lines)
    return _Page(page.number + 1, page.rect.height, blocks)


def _is_scanned(pages: list[_Page]) -> bool:
    with_text = sum(
        1
        for page in pages
        if sum(len(line.text) for block in page.blocks for line in block) >= _SCAN_MIN_CHARS
    )
    return with_text / len(pages) < _SCAN_MIN_PAGE_RATIO


def _in_margin(line: _Line, height: float) -> bool:
    return line.y1 <= height * _MARGIN_RATIO or line.y0 >= height * (1 - _MARGIN_RATIO)


def _strip_running_lines(pages: list[_Page]) -> None:
    """Quita encabezados/pies repetidos y líneas que son solo un número de página."""
    seen: dict[str, int] = {}
    for page in pages:
        keys = {
            _normalize(line.text)
            for block in page.blocks
            for line in block
            if _in_margin(line, page.height)
        }
        for key in keys:
            seen[key] = seen.get(key, 0) + 1
    # Con pocas páginas, un texto en el margen de una sola página no es «repetido».
    threshold = max(2, math.ceil(_REPEAT_RATIO * len(pages)))
    repeated = {key for key, count in seen.items() if count >= threshold}
    for page in pages:
        kept = []
        for block in page.blocks:
            lines = [
                line
                for line in block
                if not _ONLY_NUMBER.match(line.text)
                and not (_in_margin(line, page.height) and _normalize(line.text) in repeated)
            ]
            if lines:
                kept.append(lines)
        page.blocks = kept


def _median_size(pages: list[_Page]) -> float:
    """Mediana del tamaño de letra ponderada por número de caracteres."""
    weights: dict[float, int] = {}
    for page in pages:
        for block in page.blocks:
            for line in block:
                weights[line.size] = weights.get(line.size, 0) + len(line.text)
    if not weights:
        return 0.0
    half = sum(weights.values()) / 2
    running = 0
    for size in sorted(weights):
        running += weights[size]
        if running >= half:
            return size
    return 0.0


def _detect_headings(pages: list[_Page]) -> list[tuple[str, int]]:
    median = _median_size(pages)
    found: list[tuple[str, int]] = []
    for page in pages:
        for block in page.blocks:
            hit = next(
                (
                    line
                    for line in block
                    if _HEADING.match(line.text) and line.size >= _HEADING_SIZE_RATIO * median
                ),
                None,
            )
            if hit:
                found.append((hit.text, page.number))
                break  # un capítulo por página como máximo
    return found


def _join_lines(lines: list[_Line]) -> str:
    text = lines[0].text
    for line in lines[1:]:
        if _HYPHEN_END.search(text) and line.text[:1].islower():
            text = text[:-1] + line.text
        else:
            text = f"{text} {line.text}"
    return text


def _build_index(extracted: _Extracted) -> list[ChapterRef]:
    total = len(extracted.pages)
    starts = [(t, p) for t, p in extracted.toc if 1 <= p <= total]
    if not starts:
        starts = _detect_headings(extracted.pages)
    if not starts:
        return [ChapterRef(0, _WHOLE_DOCUMENT, f"pages:1-{total}")]
    starts.sort(key=lambda item: item[1])  # estable: conserva el orden del índice
    unique: list[tuple[str, int]] = []
    for title, first in starts:
        if not unique or unique[-1][1] != first:  # misma página: gana la primera entrada
            unique.append((title, first))
    refs = []
    for i, (title, first) in enumerate(unique):
        if i == 0:
            first = 1  # el texto previo al primer encabezado se conserva
        last = unique[i + 1][1] - 1 if i + 1 < len(unique) else total
        refs.append(ChapterRef(i, title, f"pages:{first}-{last}"))
    return refs


class PdfSource(ContentSource):
    def __init__(self) -> None:
        self._pages: list[_Page] = []
        self._index: list[ChapterRef] = []

    async def open(
        self, locator: str, prefetched_html: str | None = None
    ) -> tuple[Document, Chapter]:
        path = Path(locator).expanduser().resolve()
        try:
            doc_id, file_hash = await asyncio.to_thread(file_doc_id, path)
        except OSError as exc:
            raise DamagedDocument() from exc
        extracted = await asyncio.to_thread(_read_pdf, path)
        if _is_scanned(extracted.pages):
            raise ScannedPdf()
        _strip_running_lines(extracted.pages)
        self._pages = extracted.pages
        self._index = _build_index(extracted)
        document = Document(
            doc_id=doc_id,
            kind=SourceKind.PDF,
            origin=str(path),
            title=extracted.title or path.stem,
            canonical_url=None,
            work_prefix=None,
            file_hash=file_hash,
            chapters=list(self._index),
            adapter="pdf",
        )
        return document, await self.load_chapter(self._index[0])

    async def load_chapter(self, ref: ChapterRef) -> Chapter:
        match = _LOCATOR.match(ref.locator)
        if not self._index or not match:
            raise DamagedDocument("Primero hay que abrir un PDF.", recoverable=False)
        first, last = int(match.group(1)), int(match.group(2))
        index = ref.index
        if index is None or not 0 <= index < len(self._index):
            index = next((r.index for r in self._index if r.locator == ref.locator), None)
        if index is None:
            raise DamagedDocument("No se encontró ese capítulo en el PDF.", recoverable=False)
        entry = self._index[index]
        first, last = (int(g) for g in _LOCATOR.match(entry.locator).groups())
        paragraphs = [
            (_join_lines(block), page.number)
            for page in self._pages[first - 1 : last]
            for block in page.blocks
        ]
        text, paras, sentences = build_chapter_text(paragraphs)
        has_next = index + 1 < len(self._index)
        return Chapter(
            ref=entry,
            title=entry.title,
            text=text,
            paragraphs=paras,
            sentences=sentences,
            next_ref=self._index[index + 1] if has_next else None,
            prev_ref=self._index[index - 1] if index else None,
        )

    async def list_chapters(self) -> list[ChapterRef]:
        return list(self._index)
