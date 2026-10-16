"""Fixtures compartidas: PDF de prueba generados con pymupdf (sin binarios en el repo)."""
from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

PAGE_W, PAGE_H = 595, 842


def _write_pdf(
    path: Path,
    pages: list[list[tuple[str, float]]],
    *,
    header: str | None = None,
    numbers: bool = False,
    toc: list | None = None,
) -> Path:
    """Cada página es una lista de (texto, tamaño); cada texto va en su propio bloque."""
    doc = pymupdf.open()
    for number, items in enumerate(pages, start=1):
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        if header:
            page.insert_textbox(pymupdf.Rect(50, 20, 545, 50), header, fontsize=9)
        if numbers:
            page.insert_textbox(pymupdf.Rect(250, 800, 350, 825), str(number), fontsize=9)
        y = 100.0
        for text, size in items:
            rows = sum(1 + len(part) // 60 for part in text.split("\n"))
            height = 20 + size * 1.4 * rows
            page.insert_textbox(pymupdf.Rect(50, y, 545, y + height), text, fontsize=size)
            y += height + 8
    if toc:
        doc.set_toc(toc)
    doc.save(path)
    doc.close()
    return path


@pytest.fixture
def make_pdf(tmp_path):
    def build(name: str = "novela.pdf", pages=(), **kwargs) -> Path:
        return _write_pdf(tmp_path / name, list(pages), **kwargs)

    return build
