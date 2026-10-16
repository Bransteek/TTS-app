import hashlib

import pytest

from lector.content.epub_source import EpubSource
from lector.errors import DamagedDocument
from lector.models import SourceKind

LONG = "Esta es una frase de ejemplo suficientemente larga para contar como texto."


def para(tag: str) -> str:
    return f"<p>{tag} {LONG}</p>"


async def test_spine_order_and_nav_titles(make_epub):
    path = make_epub(
        chapters=[("b.xhtml", para("Segundo")), ("a.xhtml", para("Primero")), ("c.xhtml", para("Tercero"))],
        nav=[("a.xhtml", "Uno"), ("b.xhtml", "Dos"), ("c.xhtml", "Tres")],
        nonlinear=("c.xhtml",),
    )
    source = EpubSource()
    doc, first = await source.open(str(path))
    assert doc.kind is SourceKind.EPUB
    assert doc.origin == str(path.resolve())
    assert doc.title == "Mi novela"
    assert doc.file_hash == hashlib.sha256(path.read_bytes()).hexdigest()
    assert doc.doc_id == doc.file_hash[:16]
    assert [c.title for c in doc.chapters] == ["Dos", "Uno"]  # orden del spine, sin linear="no"
    assert [c.index for c in doc.chapters] == [0, 1]
    assert doc.chapters[0].locator == "spine:OEBPS/b.xhtml"
    assert first.ref == doc.chapters[0]
    assert first.prev_ref is None and first.next_ref == doc.chapters[1]
    second = await source.load_chapter(doc.chapters[1])
    assert second.prev_ref == doc.chapters[0] and second.next_ref is None
    assert second.text.startswith("Primero")


async def test_ncx_titles_epub2(make_epub):
    path = make_epub(
        chapters=[("a.xhtml", para("Uno")), ("b.xhtml", para("Dos"))],
        ncx=[("a.xhtml", "Capítulo A"), ("b.xhtml#x", "Capítulo B")],
    )
    doc, _ = await EpubSource().open(str(path))
    assert [c.title for c in doc.chapters] == ["Capítulo A", "Capítulo B"]


async def test_heading_and_section_fallback_titles(make_epub):
    path = make_epub(
        chapters=[("a.xhtml", "<h2>El inicio</h2>" + para("Uno")), ("b.xhtml", para("Dos"))],
    )
    doc, _ = await EpubSource().open(str(path))
    assert [c.title for c in doc.chapters] == ["El inicio", "Sección 2"]


async def test_ruby_stripped(make_epub):
    path = make_epub(
        chapters=[("a.xhtml", f"<p><ruby>漢<rt>kan</rt><rp>(</rp></ruby> {LONG}</p><script>var x=1;</script>")]
    )
    _, chapter = await EpubSource().open(str(path))
    assert chapter.text.startswith("漢 ")
    assert "kan" not in chapter.text and "var x" not in chapter.text


async def test_block_text_no_duplicates(make_epub):
    body = f"<blockquote><p>Cita {LONG}</p></blockquote><div>Div {LONG}</div><ul><li>Item {LONG}</li></ul>"
    _, chapter = await EpubSource().open(str(make_epub(chapters=[("a.xhtml", body)])))
    assert [p.text.split()[0] for p in chapter.paragraphs] == ["Cita", "Div", "Item"]


async def test_image_only_pages_skipped(make_epub):
    path = make_epub(
        chapters=[
            ("cover.xhtml", '<div><img src="c.jpg"/></div>'),
            ("a.xhtml", para("Uno")),
            ("ill.xhtml", "<p>1</p>"),
            ("b.xhtml", para("Dos")),
        ]
    )
    source = EpubSource()
    doc, first = await source.open(str(path))
    assert [c.locator for c in doc.chapters] == ["spine:OEBPS/a.xhtml", "spine:OEBPS/b.xhtml"]
    assert [c.index for c in doc.chapters] == [0, 1]
    assert first.text.startswith("Uno")
    assert first.next_ref == doc.chapters[1]


async def test_path_traversal_rejected(make_epub):
    path = make_epub(chapters=[("a.xhtml", para("Uno"))], extra={"../evil.xhtml": "<html/>"})
    with pytest.raises(DamagedDocument):
        await EpubSource().open(str(path))


async def test_oversized_member_rejected(make_epub, monkeypatch):
    import lector.content.epub_source as mod

    monkeypatch.setattr(mod, "_MAX_MEMBER_BYTES", 100)
    path = make_epub(chapters=[("a.xhtml", para("Uno") * 5)])
    with pytest.raises(DamagedDocument):
        await EpubSource().open(str(path))


async def test_xxe_not_resolved(make_epub):
    doctype = '<!DOCTYPE html [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
    path = make_epub(chapters=[("a.xhtml", f"<p>&x; {LONG}</p>")], doctype=doctype)
    _, chapter = await EpubSource().open(str(path))
    assert "root:" not in chapter.text and "/bin/" not in chapter.text
    assert LONG in chapter.text


async def test_not_a_zip_is_damaged(tmp_path):
    bad = tmp_path / "x.epub"
    bad.write_bytes(b"no soy un zip")
    with pytest.raises(DamagedDocument):
        await EpubSource().open(str(bad))


async def test_load_chapter_before_open(make_epub):
    from lector.models import ChapterRef

    with pytest.raises(DamagedDocument):
        await EpubSource().load_chapter(ChapterRef(0, "x", "spine:a"))


XHTML_DTD = (
    '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" '
    '"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">'
)


@pytest.mark.parametrize("doctype", ["", XHTML_DTD])
async def test_whitespace_and_named_entities(make_epub, doctype):
    body = "<p>uno&nbsp;dos&mdash;tres<br/>cuatro&hellip; &iquest;qu&eacute;?</p>"
    path = make_epub(chapters=[("a.xhtml", body + para("Pad"))], doctype=doctype)
    _, chapter = await EpubSource().open(str(path))
    assert chapter.paragraphs[0].text == "uno dos—tres cuatro… ¿qué?"


async def test_bad_spine_items_skipped(make_epub, caplog):
    import zipfile

    path = make_epub(
        chapters=[("a.xhtml", para("Uno")), ("gone.xhtml", para("X")), ("b.xhtml", para("Dos"))]
    )
    # Reescribe el zip sin gone.xhtml y con un item de imagen en el spine.
    out = path.with_name("roto.epub")
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(out, "w") as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename.endswith("gone.xhtml"):
                continue
            if info.filename.endswith("content.opf"):
                data = data.replace(
                    b'href="b.xhtml" media-type="application/xhtml+xml"',
                    b'href="b.xhtml" media-type="application/xhtml+xml"/><item id="img" '
                    b'href="i.png" media-type="image/png"',
                ).replace(b"</spine>", b'<itemref idref="img"/></spine>')
            dst.writestr(info.filename, data)
    with caplog.at_level("WARNING"):
        doc, _ = await EpubSource().open(str(out))
    assert [c.title for c in doc.chapters] == ["Sección 1", "Sección 2"]
    assert "gone.xhtml" in caplog.text


async def test_all_spine_items_unreadable_is_damaged(make_epub):
    import zipfile

    path = make_epub(chapters=[("a.xhtml", para("Uno"))])
    out = path.with_name("vacio.epub")
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(out, "w") as dst:
        for info in src.infolist():
            if not info.filename.endswith("a.xhtml"):
                dst.writestr(info.filename, src.read(info.filename))
    with pytest.raises(DamagedDocument):
        await EpubSource().open(str(out))


async def test_open_reports_epub_adapter(make_epub):
    path = make_epub(chapters=[("a.xhtml", para("Primero"))])
    doc, _ = await EpubSource().open(str(path))
    assert doc.adapter == "epub" and doc.index_page is False
