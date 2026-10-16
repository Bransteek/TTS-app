"""Vista previa del documento abierto y panel para elegir el capítulo siguiente."""
from __future__ import annotations

from urllib.parse import urlsplit

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from lector.models import Chapter, ChapterRef, Document

PREVIEW_PARAGRAPHS = 5
HINT_PREFIX = "Revisa el prefijo de la obra y pulsa «Leer» para empezar."
HINT_CHAPTER = "Elige el capítulo y pulsa «Leer» para empezar."
PREFIX_NEEDS_URL = "Escribe el prefijo como una dirección completa, por ejemplo https://{host}/novela/."
PREFIX_OTHER_HOST = "El prefijo tiene que ser de la misma web ({host})."


def validate_prefix(text: str, origin: str) -> tuple[str | None, str | None]:
    """Comprueba el prefijo escrito: devuelve (ruta «/…/», None) o (None, mensaje para el usuario).

    Tiene que ser una dirección http(s) completa de la misma web que el documento.
    """
    host = urlsplit(origin).netloc.lower()
    parts = urlsplit(text.strip())
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return None, PREFIX_NEEDS_URL.format(host=host)
    if parts.netloc.lower() != host:
        return None, PREFIX_OTHER_HOST.format(host=host)
    path = parts.path or "/"
    if not path.endswith("/"):
        path += "/"
    return path, None


def next_text(chapter: Chapter) -> str:
    if chapter.next_candidates:
        found = "hay varios posibles"
    elif chapter.next_ref is not None:
        found = chapter.next_ref.title or chapter.next_ref.locator
    else:
        found = "ninguno"
    return f"Siguiente detectado: {found}"


def _bold(label: QLabel) -> QLabel:
    font = label.font()
    font.setBold(True)
    label.setFont(font)
    return label


class PreviewPanel(QFrame):
    """Título, capítulo, primeros párrafos y «siguiente» detectado, antes de leer.

    En una web genérica sin progreso deja corregir el prefijo de la obra (§5.2) y,
    al abrir una novela desde su índice, elegir el capítulo de inicio (§4).
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.title_label = _bold(QLabel())
        self.title_label.setWordWrap(True)
        self.chapter_label = QLabel()
        self.chapter_label.setWordWrap(True)
        self.paragraphs_label = QLabel()
        self.paragraphs_label.setWordWrap(True)
        self.paragraphs_label.setTextFormat(Qt.TextFormat.PlainText)
        self.paragraphs_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self.paragraphs_label)
        scroll.setMaximumHeight(160)
        self.next_label = QLabel()
        self.next_label.setWordWrap(True)
        self.prefix_label = QLabel("Prefijo de la obra")
        self.prefix_edit = QLineEdit()
        self.prefix_edit.setToolTip(
            "Parte de la dirección que comparten todos los capítulos de esta obra."
        )
        self.prefix_error_label = _bold(QLabel())
        self.prefix_error_label.setWordWrap(True)
        self.chapter_list_label = QLabel("Empezar por:")
        self.chapter_list = QListWidget()
        self.chapter_list.setMaximumHeight(140)
        self.hint_label = QLabel()
        self.hint_label.setWordWrap(True)
        self._prefix_shown = ""
        self._doc: Document | None = None
        for widget in (self.prefix_label, self.prefix_edit, self.prefix_error_label,
                       self.chapter_list_label, self.chapter_list, self.hint_label):
            widget.hide()

        prefix_row = QHBoxLayout()
        prefix_row.addWidget(self.prefix_label)
        prefix_row.addWidget(self.prefix_edit, 1)

        layout = QVBoxLayout(self)
        layout.addWidget(self.title_label)
        layout.addWidget(self.chapter_label)
        layout.addWidget(scroll)
        layout.addWidget(self.next_label)
        layout.addLayout(prefix_row)
        layout.addWidget(self.prefix_error_label)
        layout.addWidget(self.chapter_list_label)
        layout.addWidget(self.chapter_list)
        layout.addWidget(self.hint_label)

        self.prefix_edit.textEdited.connect(lambda _text: self.prefix_error_label.hide())

    def show_preview(self, doc: Document, chapter: Chapter, *, editable_prefix: bool = False,
                     choose_chapter: bool = False) -> None:
        self._doc = doc
        self.title_label.setText(doc.title or chapter.title)
        self.chapter_label.setText(chapter.title)
        first = [p.text for p in chapter.paragraphs[:PREVIEW_PARAGRAPHS]]
        self.paragraphs_label.setText("\n\n".join(first))
        self.next_label.setText(next_text(chapter))

        self._prefix_shown = (doc.work_prefix or "") if editable_prefix else ""
        self.prefix_edit.setText(self._prefix_shown)
        self.prefix_label.setVisible(editable_prefix)
        self.prefix_edit.setVisible(editable_prefix)
        self.prefix_error_label.hide()

        choose_chapter = choose_chapter and bool(doc.chapters)
        self.chapter_list.clear()
        if choose_chapter:
            for i, ref in enumerate(doc.chapters):
                self.chapter_list.addItem(ref.title or f"Capítulo {i + 1}")
            self.chapter_list.setCurrentRow(0)  # el capítulo 1, por defecto
        self.chapter_list_label.setVisible(choose_chapter)
        self.chapter_list.setVisible(choose_chapter)

        hint = HINT_PREFIX if editable_prefix else HINT_CHAPTER if choose_chapter else ""
        self.hint_label.setText(hint)
        self.hint_label.setVisible(bool(hint))

    def edited_prefix(self) -> str | None:
        """El prefijo escrito si el usuario lo cambió (solo cuando el campo está a la vista)."""
        if self.prefix_edit.isHidden():
            return None
        text = self.prefix_edit.text().strip()
        return text if text != self._prefix_shown else None

    def show_prefix_error(self, message: str) -> None:
        self.prefix_error_label.setText(message)
        self.prefix_error_label.show()

    def selected_chapter(self) -> tuple[int, ChapterRef] | None:
        """Capítulo elegido en la lista (solo al abrir desde el índice de la obra)."""
        if self._doc is None or self.chapter_list.isHidden():
            return None
        row = self.chapter_list.currentRow()
        if not 0 <= row < len(self._doc.chapters):
            return None
        return row, self._doc.chapters[row]

    def needs_confirmation(self) -> bool:
        """True mientras la vista previa espera que el usuario revise y pulse «Leer»."""
        return not self.hint_label.isHidden()


class ChoicePanel(QFrame):
    """Lista no modal de candidatos cuando el capítulo siguiente es ambiguo."""

    chosen = Signal(object)  # ChapterRef

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.message_label = QLabel("Hay varios capítulos posibles como siguiente. Elige uno:")
        self.message_label.setWordWrap(True)
        self.list = QListWidget()
        self.list.setMaximumHeight(140)
        self.choose_button = QPushButton("Seguir por este capítulo")
        self.choose_button.setEnabled(False)
        self._refs: list[ChapterRef] = []

        layout = QVBoxLayout(self)
        layout.addWidget(self.message_label)
        layout.addWidget(self.list)
        layout.addWidget(self.choose_button, 0, Qt.AlignmentFlag.AlignRight)

        self.list.currentRowChanged.connect(lambda row: self.choose_button.setEnabled(row >= 0))
        self.list.itemDoubleClicked.connect(lambda _item: self._choose())
        self.choose_button.clicked.connect(self._choose)

    def set_candidates(self, refs: list[ChapterRef]) -> None:
        self._refs = list(refs)
        self.list.clear()
        for i, ref in enumerate(self._refs):
            item = QListWidgetItem(ref.title or ref.locator)
            item.setData(Qt.ItemDataRole.UserRole, i)
            item.setToolTip(ref.locator)
            self.list.addItem(item)

    def _choose(self) -> None:
        item = self.list.currentItem()
        if item is None:
            return
        self.hide()
        self.chosen.emit(self._refs[item.data(Qt.ItemDataRole.UserRole)])
