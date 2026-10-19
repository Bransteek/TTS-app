"""Fuente de contenido para EPUB 2/3 (zipfile + lxml), un capítulo por documento del spine."""
from __future__ import annotations

import asyncio
import logging
import posixpath
import re
import zipfile
from dataclasses import dataclass
from html.entities import name2codepoint
from pathlib import Path
from urllib.parse import unquote, urldefrag

from lxml import etree

from lector.content.base import ContentSource
from lector.content.pdf_source import file_doc_id
from lector.content.segmenter import build_chapter_text
from lector.errors import DamagedDocument, DocumentTooLarge
from lector.models import Chapter, ChapterRef, Document, SourceKind

_MAX_MEMBER_BYTES = 20 * 1024 * 1024
# Tope de todo lo que se descomprime de un EPUB (§5.10): solo cuentan los miembros que se
# leen (XHTML, OPF, índice), nunca las imágenes. Una novela entera ocupa pocos MB de
# XHTML; 200 MB deja margen de sobra y evita que un archivo hostil agote la memoria.
_MAX_TOTAL_BYTES = 200 * 1024 * 1024
_MIN_TEXT_CHARS = 20
_LOCATOR_PREFIX = "spine:"
_DROPPED = {"script", "style", "rt", "rp"}
_BLOCKS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote"}
# Elementos que separan párrafos: los bloques de texto y la estructura que los contiene.
# El texto suelto entre ellos (p. ej. directamente en <body>) forma su propio párrafo.
_BREAKS = _BLOCKS | {
    "body", "section", "article", "main", "header", "footer", "aside", "nav", "figure",
    "figcaption", "table", "thead", "tbody", "tfoot", "tr", "td", "th", "ul", "ol", "dl",
    "dt", "dd", "pre", "hr", "hgroup", "details", "summary",
}
_HEADINGS = ("h1", "h2", "h3")
_CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
_HTML_TYPES = {"application/xhtml+xml", "text/html"}
_NCX_MIME = "application/x-dtbncx+xml"

log = logging.getLogger(__name__)


@dataclass
class _Section:
    ref: ChapterRef
    paragraphs: list[str]


def _local(element) -> str:
    tag = element.tag
    return etree.QName(tag).localname.lower() if isinstance(tag, str) else ""


def _check_name(name: str) -> None:
    if name.startswith("/") or ".." in name.replace("\\", "/").split("/"):
        raise DamagedDocument()


class _Budget:
    """Bytes descomprimidos que aún se pueden leer de este EPUB."""

    def __init__(self) -> None:
        self.left = _MAX_TOTAL_BYTES

    def take(self, size: int) -> None:
        self.left -= size
        if self.left < 0:
            raise DocumentTooLarge(
                f"El EPUB es demasiado grande (su texto ocupa más de "
                f"{_MAX_TOTAL_BYTES // (1024 * 1024)} MB) y no se puede abrir."
            )


def _read_member(archive: zipfile.ZipFile, name: str, budget: _Budget) -> bytes:
    _check_name(name)
    try:
        info = archive.getinfo(name)
        if info.file_size > _MAX_MEMBER_BYTES:
            raise DamagedDocument()
        with archive.open(info) as handle:
            data = handle.read(_MAX_MEMBER_BYTES + 1)
    except (KeyError, zipfile.BadZipFile, OSError, RuntimeError, EOFError) as exc:
        raise DamagedDocument() from exc
    if len(data) > _MAX_MEMBER_BYTES:
        raise DamagedDocument()
    budget.take(len(data))
    return data


def _parse_xml(data: bytes):
    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=True)
    try:
        root = etree.fromstring(data, parser)
    except etree.XMLSyntaxError:
        root = None
    if root is None:
        raise DamagedDocument()
    return root


def _resolve(base_dir: str, href: str) -> str:
    path = unquote(urldefrag(href)[0])
    return posixpath.normpath(posixpath.join(base_dir, path))


def _entity_text(raw: str | None) -> str:
    """«&nbsp;» -> carácter; las entidades desconocidas (p. ej. externas) se vuelven un espacio."""
    code = name2codepoint.get((raw or "").strip("&;"))
    return chr(code) if code is not None else " "


def _text_parts(node, parts: list[str]) -> None:
    """Añade a `parts` el texto de `node` (sin su cola), sin rt/rp/script/style."""
    if _local(node) in _DROPPED:
        return
    if node.text:
        parts.append(node.text)
    for child in node:
        if isinstance(child.tag, str):
            if _local(child) == "br":
                parts.append(" ")
            _text_parts(child, parts)
        elif child.tag is etree.Entity:
            parts.append(_entity_text(child.text))
        if child.tail:
            parts.append(child.tail)


def _clean(parts: list[str]) -> str:
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _own_text(element) -> str:
    """Texto del elemento sin rt/rp/script/style ni entidades sin resolver."""
    parts: list[str] = []
    _text_parts(element, parts)
    return _clean(parts)


def _is_break(node) -> bool:
    return isinstance(node.tag, str) and _local(node) in _BREAKS


def _paragraphs(root) -> list[str]:
    """Párrafos del <body> en orden: cada bloque final y cada tramo de texto suelto."""
    body = next(
        (e for e in root.iter() if isinstance(e.tag, str) and _local(e) == "body"), root
    )
    result: list[str] = []
    _collect(body, result)
    return result


def _collect(container, out: list[str]) -> None:
    """Recorre un contenedor de bloques; el texto entre bloques es un párrafo más."""
    run: list[str] = []

    def flush() -> None:
        text = _clean(run)
        if text:
            out.append(text)
        run.clear()

    if container.text:
        run.append(container.text)
    for child in container:
        if isinstance(child.tag, str) and _local(child) not in _DROPPED:
            if any(_is_break(d) for d in child.iterdescendants()):
                flush()
                _collect(child, out)
            elif _is_break(child):
                flush()
                text = _own_text(child)
                if text:
                    out.append(text)
            else:  # en línea (span, em, a, br…): sigue el tramo de texto suelto
                if _local(child) == "br":
                    run.append(" ")
                _text_parts(child, run)
        elif child.tag is etree.Entity:
            run.append(_entity_text(child.text))
        if child.tail:
            run.append(child.tail)
    flush()


def _first_heading(root) -> str:
    for element in root.iter():
        if isinstance(element.tag, str) and _local(element) in _HEADINGS:
            text = _own_text(element)
            if text:
                return text
    return ""


def _toc_titles(
    archive: zipfile.ZipFile, opf_dir: str, manifest: dict[str, dict], budget: _Budget
) -> dict[str, str]:
    """Ruta del miembro -> título, desde nav.xhtml (EPUB 3) o toc.ncx (EPUB 2)."""
    titles: dict[str, str] = {}
    nav = next((m for m in manifest.values() if "nav" in m["properties"].split()), None)
    if nav:
        root = _parse_xml(_read_member(archive, nav["path"], budget))
        nav_dir = posixpath.dirname(nav["path"])
        navs = [e for e in root.iter() if isinstance(e.tag, str) and _local(e) == "nav"]
        toc = next(
            (e for e in navs if any(v == "toc" for k, v in e.attrib.items() if k.endswith("type"))),
            navs[0] if navs else root,
        )
        for a in toc.iter():
            if isinstance(a.tag, str) and _local(a) == "a" and a.get("href"):
                text = _own_text(a)
                if text:
                    titles.setdefault(_resolve(nav_dir, a.get("href")), text)
    ncx = next((m for m in manifest.values() if m["type"] == _NCX_MIME), None)
    if ncx and not titles:
        root = _parse_xml(_read_member(archive, ncx["path"], budget))
        ncx_dir = posixpath.dirname(ncx["path"])
        for point in root.iter():
            if not isinstance(point.tag, str) or _local(point) != "navpoint":
                continue
            label = next((e for e in point.iter() if isinstance(e.tag, str) and _local(e) == "text"), None)
            content = next((e for e in point if isinstance(e.tag, str) and _local(e) == "content"), None)
            if label is not None and content is not None and content.get("src"):
                text = _own_text(label)
                if text:
                    titles.setdefault(_resolve(ncx_dir, content.get("src")), text)
    return titles


def _read_epub(path: Path) -> tuple[str, list[_Section]]:
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise DamagedDocument() from exc
    budget = _Budget()
    with archive:
        for name in archive.namelist():
            _check_name(name)
        container = _parse_xml(_read_member(archive, "META-INF/container.xml", budget))
        rootfile = next(
            (e for e in container.iter() if isinstance(e.tag, str) and _local(e) == "rootfile"), None
        )
        if rootfile is None or not rootfile.get("full-path"):
            raise DamagedDocument()
        opf_path = posixpath.normpath(rootfile.get("full-path"))
        opf = _parse_xml(_read_member(archive, opf_path, budget))
        opf_dir = posixpath.dirname(opf_path)

        manifest: dict[str, dict] = {}
        spine: list[str] = []
        title = ""
        for element in opf.iter():
            if not isinstance(element.tag, str):
                continue
            name = _local(element)
            if name == "item" and element.get("id") and element.get("href"):
                manifest[element.get("id")] = {
                    "path": _resolve(opf_dir, element.get("href")),
                    "type": element.get("media-type", ""),
                    "properties": element.get("properties", ""),
                }
            elif name == "itemref" and element.get("idref") and element.get("linear") != "no":
                spine.append(element.get("idref"))
            elif name == "title" and not title:
                title = _own_text(element)

        names = set(archive.namelist())
        titles = _toc_titles(archive, opf_dir, manifest, budget)
        sections: list[_Section] = []
        for idref in spine:
            item = manifest.get(idref)
            if item is None or item["type"] not in _HTML_TYPES:
                continue
            if item["path"] not in names:
                log.warning("EPUB: falta el miembro %s del spine; se omite", item["path"])
                continue
            try:
                root = _parse_xml(_read_member(archive, item["path"], budget))
            except DamagedDocument:
                log.warning("EPUB: no se pudo leer %s; se omite", item["path"])
                continue
            paragraphs = _paragraphs(root)
            if sum(len(p) for p in paragraphs) < _MIN_TEXT_CHARS:
                continue  # páginas de ilustraciones
            index = len(sections)
            chapter_title = (
                titles.get(item["path"]) or _first_heading(root) or f"Sección {index + 1}"
            )
            ref = ChapterRef(index, chapter_title, _LOCATOR_PREFIX + item["path"])
            sections.append(_Section(ref, paragraphs))
        if not sections:
            raise DamagedDocument("El EPUB no contiene texto que leer.")
        return title, sections


class EpubSource(ContentSource):
    def __init__(self) -> None:
        self._sections: list[_Section] = []

    async def open(
        self, locator: str, prefetched_html: str | None = None
    ) -> tuple[Document, Chapter]:
        path = Path(locator).expanduser().resolve()
        try:
            doc_id, file_hash = await asyncio.to_thread(file_doc_id, path)
        except OSError as exc:
            raise DamagedDocument() from exc
        title, sections = await asyncio.to_thread(_read_epub, path)
        self._sections = sections
        index = [s.ref for s in sections]
        document = Document(
            doc_id=doc_id,
            kind=SourceKind.EPUB,
            origin=str(path),
            title=title or path.stem,
            canonical_url=None,
            work_prefix=None,
            file_hash=file_hash,
            chapters=list(index),
            adapter="epub",
        )
        return document, await self.load_chapter(index[0])

    async def load_chapter(self, ref: ChapterRef) -> Chapter:
        if not self._sections:
            raise DamagedDocument("Primero hay que abrir un EPUB.", recoverable=False)
        index = ref.index
        if index is None or not 0 <= index < len(self._sections):
            index = next(
                (s.ref.index for s in self._sections if s.ref.locator == ref.locator), None
            )
        if index is None:
            raise DamagedDocument("No se encontró ese capítulo en el EPUB.", recoverable=False)
        section = self._sections[index]
        text, paras, sentences = build_chapter_text([(p, None) for p in section.paragraphs])
        has_next = index + 1 < len(self._sections)
        return Chapter(
            ref=section.ref,
            title=section.ref.title,
            text=text,
            paragraphs=paras,
            sentences=sentences,
            next_ref=self._sections[index + 1].ref if has_next else None,
            prev_ref=self._sections[index - 1].ref if index else None,
        )

    async def list_chapters(self) -> list[ChapterRef]:
        return [s.ref for s in self._sections]
