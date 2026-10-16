import hashlib

import pymupdf
import pytest

from lector.content.pdf_source import PdfSource, file_doc_id
from lector.errors import DamagedDocument, ScannedPdf
from lector.models import SourceKind

BODY = 11


def prose(tag: str) -> str:
    return f"{tag} es una frase larga de ejemplo para que la página tenga texto suficiente."


async def test_extracts_text_with_pages(make_pdf):
    path = make_pdf(pages=[[(prose("Uno"), BODY)], [(prose("Dos"), BODY)], [(prose("Tres"), BODY)]])
    doc, chapter = await PdfSource().open(str(path))
    assert doc.kind is SourceKind.PDF
    assert doc.origin == str(path.resolve())
    assert [(p.text.split()[0], p.page) for p in chapter.paragraphs] == [
        ("Uno", 1), ("Dos", 2), ("Tres", 3),
    ]
    assert chapter.ref.locator == "pages:1-3"
    assert [s.page for s in chapter.sentences] == [1, 2, 3]


async def test_toc_chapters(make_pdf):
    path = make_pdf(
        pages=[[(prose("Uno"), BODY)], [(prose("Dos"), BODY)], [(prose("Tres"), BODY)]],
        toc=[[1, "Prólogo", 1], [2, "Sub", 2], [1, "Capítulo 1", 2]],
    )
    source = PdfSource()
    doc, first = await source.open(str(path))
    assert [c.title for c in doc.chapters] == ["Prólogo", "Capítulo 1"]
    assert [c.locator for c in doc.chapters] == ["pages:1-1", "pages:2-3"]
    assert [c.index for c in doc.chapters] == [0, 1]
    assert first.ref == doc.chapters[0]
    assert first.prev_ref is None and first.next_ref == doc.chapters[1]
    second = await source.load_chapter(doc.chapters[1])
    assert [p.page for p in second.paragraphs] == [2, 3]
    assert second.prev_ref == doc.chapters[0] and second.next_ref is None
    assert await source.list_chapters() == doc.chapters


async def test_heading_detection_without_toc(make_pdf):
    path = make_pdf(
        pages=[
            [("Capítulo 1", 20), (prose("Uno"), BODY)],
            [(prose("Dos"), BODY)],
            [("Epílogo", 20), (prose("Tres"), BODY)],
        ]
    )
    doc, _ = await PdfSource().open(str(path))
    assert [(c.title, c.locator) for c in doc.chapters] == [
        ("Capítulo 1", "pages:1-2"),
        ("Epílogo", "pages:3-3"),
    ]


async def test_small_font_heading_word_is_not_a_chapter(make_pdf):
    path = make_pdf(pages=[[("Capítulo 1 del libro, en letra normal.", BODY), (prose("Uno"), BODY)]])
    doc, _ = await PdfSource().open(str(path))
    assert [c.title for c in doc.chapters] == ["Documento completo"]
    assert doc.chapters[0].locator == "pages:1-1"


async def test_running_header_removed(make_pdf):
    pages = [[(prose(n), BODY)] for n in ("Uno", "Dos", "Tres", "Cuatro")]
    path = make_pdf(pages=pages, header="Mi Novela — Vol 1", numbers=True)
    _, chapter = await PdfSource().open(str(path))
    assert "Mi Novela" not in chapter.text
    assert [p.text.split()[0] for p in chapter.paragraphs] == ["Uno", "Dos", "Tres", "Cuatro"]


async def test_scanned_pdf_detected(tmp_path):
    doc = pymupdf.open()
    for _ in range(3):
        page = doc.new_page()
        page.draw_rect(pymupdf.Rect(50, 50, 500, 700), fill=(0.5, 0.5, 0.5))
    path = tmp_path / "scan.pdf"
    doc.save(path)
    doc.close()
    with pytest.raises(ScannedPdf):
        await PdfSource().open(str(path))


async def test_damaged_pdf(tmp_path):
    path = tmp_path / "roto.pdf"
    path.write_bytes(b"esto no es un pdf \x00\x01\x02" * 20)
    with pytest.raises(DamagedDocument):
        await PdfSource().open(str(path))


async def test_missing_file_is_damaged(tmp_path):
    with pytest.raises(DamagedDocument):
        await PdfSource().open(str(tmp_path / "no_existe.pdf"))


async def test_encrypted_pdf_is_damaged(tmp_path):
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "texto secreto " * 5)
    path = tmp_path / "cifrado.pdf"
    doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="clave", owner_pw="dueno")
    doc.close()
    with pytest.raises(DamagedDocument) as err:
        await PdfSource().open(str(path))
    assert "contraseña" in err.value.user_message


async def test_same_page_toc_entries_do_not_duplicate(make_pdf):
    path = make_pdf(
        pages=[[(prose(n), BODY)] for n in ("Uno", "Dos", "Tres")],
        toc=[[1, "A", 1], [1, "B", 2], [1, "C", 2], [1, "D", 3]],
    )
    source = PdfSource()
    doc, _ = await source.open(str(path))
    assert [(c.title, c.locator) for c in doc.chapters] == [
        ("A", "pages:1-1"), ("B", "pages:2-2"), ("D", "pages:3-3"),
    ]
    seen = []
    for ref in doc.chapters:
        chapter = await source.load_chapter(ref)
        assert (chapter.ref.index, chapter.title) == (ref.index, ref.title)
        seen += [p.text for p in chapter.paragraphs]
    assert len(seen) == len(set(seen)) == 3


async def test_load_chapter_unknown_ref(make_pdf):
    from lector.models import ChapterRef

    path = make_pdf(pages=[[(prose("Uno"), BODY)]])
    source = PdfSource()
    await source.open(str(path))
    with pytest.raises(DamagedDocument):
        await source.load_chapter(ChapterRef(9, "x", "pages:5-6"))


async def test_hyphen_rejoin(make_pdf):
    text = "Ella caminaba por la ca-\nlle con una tran-\nquilidad absoluta y un gesto anti-\nGuerra."
    path = make_pdf(pages=[[(text, BODY)]])
    _, chapter = await PdfSource().open(str(path))
    assert "calle" in chapter.text and "tranquilidad" in chapter.text
    assert "ca-" not in chapter.text
    assert "anti- Guerra" in chapter.text  # mayúscula siguiente: no es corte de palabra


async def test_doc_id_and_hash(make_pdf):
    path = make_pdf(pages=[[(prose("Uno"), BODY)]])
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert file_doc_id(path) == (digest[:16], digest)
    doc, _ = await PdfSource().open(str(path))
    assert (doc.doc_id, doc.file_hash) == (digest[:16], digest)


async def test_open_reports_pdf_adapter(make_pdf):
    path = make_pdf(pages=[[(prose("Uno"), BODY)]])
    doc, _ = await PdfSource().open(str(path))
    assert doc.adapter == "pdf" and doc.index_page is False
