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
from lector.errors import DamagedDocument
from lector.models import Chapter, ChapterRef, Document, SourceKind

_MAX_MEMBER_BYTES = 20 * 1024 * 1024
_MIN_TEXT_CHARS = 20
_LOCATOR_PREFIX = "spine:"
_DROPPED = {"script", "style", "rt", "rp"}
_BLOCKS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote"}
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


def _read_member(archive: zipfile.ZipFile, name: str) -> bytes:
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


def _own_text(element) -> str:
    """Texto del elemento sin rt/rp/script/style ni entidades sin resolver."""
    parts: list[str] = []

    def walk(node) -> None:
        if _local(node) in _DROPPED:
            return
        if node.text:
            parts.append(node.text)
        for child in node:
            if isinstance(child.tag, str):
                if _local(child) == "br":
                    parts.append(" ")
                walk(child)
            elif child.tag is etree.Entity:
                parts.append(_entity_text(child.text))
            if child.tail:
                parts.append(child.tail)

    walk(element)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _paragraphs(root) -> list[str]:
    result = []
    for element in root.iter():
        if not isinstance(element.tag, str) or _local(element) not in _BLOCKS:
            continue
        nested = any(
            isinstance(d.tag, str) and _local(d) in _BLOCKS for d in element.iterdescendants()
        )
        if nested:
            continue
        text = _own_text(element)
        if text:
            result.append(text)
    return result


def _first_heading(root) -> str:
    for element in root.iter():
        if isinstance(element.tag, str) and _local(element) in _HEADINGS:
            text = _own_text(element)
            if text:
                return text
    return ""


def _toc_titles(archive: zipfile.ZipFile, opf_dir: str, manifest: dict[str, dict]) -> dict[str, str]:
    """Ruta del miembro -> título, desde nav.xhtml (EPUB 3) o toc.ncx (EPUB 2)."""
    titles: dict[str, str] = {}
    nav = next((m for m in manifest.values() if "nav" in m["properties"].split()), None)
    if nav:
        root = _parse_xml(_read_member(archive, nav["path"]))
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
        root = _parse_xml(_read_member(archive, ncx["path"]))
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
    with archive:
        for name in archive.namelist():
            _check_name(name)
        container = _parse_xml(_read_member(archive, "META-INF/container.xml"))
        rootfile = next(
            (e for e in container.iter() if isinstance(e.tag, str) and _local(e) == "rootfile"), None
        )
        if rootfile is None or not rootfile.get("full-path"):
            raise DamagedDocument()
        opf_path = posixpath.normpath(rootfile.get("full-path"))
        opf = _parse_xml(_read_member(archive, opf_path))
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
        titles = _toc_titles(archive, opf_dir, manifest)
        sections: list[_Section] = []
        for idref in spine:
            item = manifest.get(idref)
            if item is None or item["type"] not in _HTML_TYPES:
                continue
            if item["path"] not in names:
                log.warning("EPUB: falta el miembro %s del spine; se omite", item["path"])
                continue
            try:
                root = _parse_xml(_read_member(archive, item["path"]))
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
