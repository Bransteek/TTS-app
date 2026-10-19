"""Fixtures compartidas: PDF de prueba generados con pymupdf (sin binarios en el repo)."""
from __future__ import annotations

import os
from pathlib import Path

import pymupdf
import pytest

# Qt sin pantalla: debe fijarse antes de que se cree cualquier QApplication.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

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


_CONTAINER = (
    '<?xml version="1.0"?><container version="1.0" '
    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
    '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
    "</rootfiles></container>"
)


def _xhtml(body: str, doctype: str = "") -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>' + doctype
        + '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head>'
        f"<body>{body}</body></html>"
    )


@pytest.fixture
def make_epub(tmp_path):
    """Construye un EPUB con zipfile. `chapters`: lista de (archivo, cuerpo XHTML)."""
    import zipfile

    def build(
        name: str = "libro.epub",
        chapters=(),
        *,
        nav: list[tuple[str, str]] | None = None,
        ncx: list[tuple[str, str]] | None = None,
        nonlinear: tuple[str, ...] = (),
        extra: dict[str, str] | None = None,
        doctype: str = "",
        title: str = "Mi novela",
    ) -> Path:
        items, refs = [], []
        for i, (fname, _) in enumerate(chapters):
            items.append(f'<item id="c{i}" href="{fname}" media-type="application/xhtml+xml"/>')
            lin = ' linear="no"' if fname in nonlinear else ""
            refs.append(f'<itemref idref="c{i}"{lin}/>')
        if nav is not None:
            items.append(
                '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" '
                'properties="nav"/>'
            )
        if ncx is not None:
            items.append('<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>')
        opf = (
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f"<dc:title>{title}</dc:title></metadata>"
            f"<manifest>{''.join(items)}</manifest>"
            f"<spine>{''.join(refs)}</spine></package>"
        )
        path = tmp_path / name
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("mimetype", "application/epub+zip")
            z.writestr("META-INF/container.xml", _CONTAINER)
            z.writestr("OEBPS/content.opf", opf)
            for fname, body in chapters:
                z.writestr(f"OEBPS/{fname}", _xhtml(body, doctype))
            if nav is not None:
                links = "".join(f'<li><a href="{h}">{t}</a></li>' for h, t in nav)
                z.writestr(
                    "OEBPS/nav.xhtml",
                    _xhtml(f'<nav xmlns:epub="http://www.idpf.org/2007/ops" epub:type="toc"><ol>{links}</ol></nav>'),
                )
            if ncx is not None:
                pts = "".join(
                    f'<navPoint id="n{i}"><navLabel><text>{t}</text></navLabel>'
                    f'<content src="{h}"/></navPoint>'
                    for i, (h, t) in enumerate(ncx)
                )
                z.writestr(
                    "OEBPS/toc.ncx",
                    '<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" '
                    f'version="2005-1"><navMap>{pts}</navMap></ncx>',
                )
            for member, data in (extra or {}).items():
                z.writestr(member, data)
        return path

    return build


@pytest.fixture
async def controller_rig(tmp_path):
    """Fábrica de montajes del controlador; todos se cierran al terminar la prueba."""
    from tests.controller_rig import ControllerRig

    rigs = []

    def build(*, db_path=None, **kwargs):
        rig = ControllerRig(db_path or tmp_path / "lector.db", **kwargs)
        rigs.append(rig)
        return rig

    yield build
    for rig in reversed(rigs):
        await rig.close()
