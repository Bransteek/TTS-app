"""Segmentación de texto: párrafos -> frases -> fragmentos para TTS."""
from __future__ import annotations

import re

from lector.models import Chunk, Paragraph, Sentence, make_chunk_id

# Tope duro de una Sentence; independiente de los límites de cada motor.
MAX_SENTENCE_CHARS = 600

_UPPER = "A-ZÁÉÍÓÚÑÜ"
_BOUNDARY = re.compile(
    r"(?:\.\.\.|[.!?…])+[»”\"')]*(?=\s+(?:[" + _UPPER + r"¿¡«]|—))"
)
_WORD_BEFORE = re.compile(r"(\w+)$")
_ABBREVIATIONS = {
    "sr", "sra", "srta", "dr", "dra", "ud", "uds", "etc", "pág", "cap", "vol", "núm",
}


def _is_real_boundary(text: str, match: re.Match[str]) -> bool:
    chunk = match.group(0)
    after = text[match.end():]
    stripped = after.lstrip()
    # «¿Vienes? —preguntó ella.»: el guion introduce un inciso, no un diálogo nuevo.
    if stripped.startswith("—") and len(stripped) > 1 and stripped[1].islower():
        return False
    if chunk != ".":
        return True
    before = text[: match.start()]
    if before.lower().endswith("p. ej"):
        return False
    word = _WORD_BEFORE.search(before)
    if word:
        w = word.group(1)
        if w.lower() in _ABBREVIATIONS:
            return False
        if len(w) == 1 and w.isupper():
            return False
    return True


def split_sentences(text: str) -> list[tuple[int, int]]:
    """Rangos (start, end) de cada frase, sin espacios en los extremos."""
    ranges: list[tuple[int, int]] = []
    pos = 0
    for m in _BOUNDARY.finditer(text):
        if not _is_real_boundary(text, m):
            continue
        ranges.append((pos, m.end()))
        pos = m.end()
    ranges.append((pos, len(text)))
    result = []
    for a, b in ranges:
        seg = text[a:b]
        lead = len(seg) - len(seg.lstrip())
        trail = len(seg) - len(seg.rstrip())
        if a + lead < b - trail:
            result.append((a + lead, b - trail))
    return result


def _split_long(text: str, start: int, end: int, cap: int) -> list[tuple[int, int]]:
    """Parte [start, end) en trozos de <= cap, en la última coma o espacio."""
    pieces: list[tuple[int, int]] = []
    pos = start
    while end - pos > cap:
        window = text[pos : pos + cap + 1]
        cut = window.rfind(",")
        if cut > 0:
            cut += 1  # la coma se queda en la pieza
        else:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = cap
        cut = min(cut, cap)
        piece_end = pos + cut
        piece = text[pos:piece_end].rstrip()
        pieces.append((pos, pos + len(piece)))
        pos = piece_end
        while pos < end and text[pos].isspace():
            pos += 1
    if pos < end:
        pieces.append((pos, end))
    return pieces


def build_chapter_text(
    paragraph_texts: list[tuple[str, int | None]],
) -> tuple[str, list[Paragraph], list[Sentence]]:
    """Une párrafos con «\\n\\n» y calcula párrafos y frases con desplazamientos."""
    parts: list[str] = []
    paragraphs: list[Paragraph] = []
    sentences: list[Sentence] = []
    offset = 0
    for raw, page in paragraph_texts:
        ptext = raw.strip()
        if not ptext:
            continue
        if parts:
            offset += 2  # separador «\n\n»
        p_index = len(paragraphs)
        paragraphs.append(Paragraph(index=p_index, text=ptext, start=offset, page=page))
        for a, b in split_sentences(ptext):
            for pa, pb in _split_long(ptext, a, b, MAX_SENTENCE_CHARS):
                sentences.append(
                    Sentence(
                        index=len(sentences),
                        paragraph_index=p_index,
                        text=ptext[pa:pb],
                        start=offset + pa,
                        end=offset + pb,
                        page=page,
                    )
                )
        parts.append(ptext)
        offset += len(ptext)
    return "\n\n".join(parts), paragraphs, sentences


def make_chunks(
    doc_id: str,
    chapter_index: int,
    sentences: list[Sentence],
    max_chars: int,
    max_sentences: int,
    first_sentence: int = 0,
) -> list[Chunk]:
    """Agrupa frases en fragmentos sin cruzar párrafos.

    El primer fragmento lleva una sola frase para empezar a sonar rápido. Una
    frase más larga que max_chars va sola en su fragmento.
    """
    chunks: list[Chunk] = []
    group: list[Sentence] = []

    def flush() -> None:
        if not group:
            return
        chunks.append(
            Chunk(
                chunk_id=make_chunk_id(
                    doc_id, chapter_index, group[0].paragraph_index, group[0], group[-1]
                ),
                doc_id=doc_id,
                chapter_index=chapter_index,
                paragraph_index=group[0].paragraph_index,
                sentences=tuple(group),
                text=" ".join(s.text for s in group),
                start=group[0].start,
                end=group[-1].end,
            )
        )
        group.clear()

    for s in sentences:
        if s.index < first_sentence:
            continue
        if group:
            limit = 1 if not chunks else max_sentences
            joined = len(" ".join(g.text for g in group)) + 1 + len(s.text)
            if (
                s.paragraph_index != group[0].paragraph_index
                or len(group) >= limit
                or joined > max_chars
            ):
                flush()
        group.append(s)
    flush()
    return chunks
