"""Diálogo al abrir un documento ya empezado: continuar, empezar o elegir capítulo o página."""
from __future__ import annotations

import re
from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from lector.models import ChapterRef, Document, Position, SourceKind
from lector.persistence.repositories import ProgressRecord

_PAGES = re.compile(r"^pages:(\d+)-(\d+)$")


@dataclass(frozen=True)
class ResumeChoice:
    kind: str  # "continue" | "start" | "chapter" | "page"
    chapter: ChapterRef | None = None
    page: int | None = None


def pdf_page_count(doc: Document) -> int:
    """Última página según los capítulos «pages:<inicio>-<fin>» (0 si no es un PDF)."""
    if doc.kind is not SourceKind.PDF:
        return 0
    ends = [int(m.group(2)) for r in doc.chapters if (m := _PAGES.match(r.locator))]
    return max(ends, default=0)


def chapter_with_page(doc: Document, page: int) -> tuple[int, ChapterRef] | None:
    """Capítulo de PDF que contiene `page`, con su posición en el índice."""
    for i, ref in enumerate(doc.chapters):
        m = _PAGES.match(ref.locator)
        if m and int(m.group(1)) <= page <= int(m.group(2)):
            return i, ref
    return None


def chapter_name(ref: ChapterRef, fallback_index: int) -> str:
    return ref.title or f"Capítulo {fallback_index + 1}"


def where_text(doc: Document, position: Position) -> str:
    """Nombre del capítulo en el que se quedó la lectura."""
    for i, ref in enumerate(doc.chapters):
        if ref.locator == position.chapter_locator:
            return chapter_name(ref, i)
    return f"Capítulo {position.chapter_index + 1}"


class ResumeDialog(QDialog):
    """Sin modalidad: no bloquea la ventana. Emite `chosen(ResumeChoice)` y se cierra."""

    chosen = Signal(object)

    def __init__(self, doc: Document, progress: ProgressRecord, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Continuar la lectura")
        self._doc = doc

        title = QLabel(doc.title or "Documento")
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        title.setWordWrap(True)
        self.where_label = QLabel(f"Ibas por: {where_text(doc, progress.position)}")
        self.where_label.setWordWrap(True)

        self.continue_button = QPushButton("Continuar desde donde lo dejé")
        self.start_button = QPushButton("Empezar desde el principio")
        self.choose_button = QPushButton("Elegir capítulo o página")
        self.continue_button.setDefault(True)
        if not doc.chapters:
            self.choose_button.setEnabled(False)
            self.choose_button.setToolTip("Esta web no tiene un índice de capítulos.")

        self.chapter_list = QListWidget()
        for i, ref in enumerate(doc.chapters):
            item = QListWidgetItem(chapter_name(ref, i))
            item.setData(Qt.ItemDataRole.UserRole, i)
            self.chapter_list.addItem(item)
        self.chapter_button = QPushButton("Leer este capítulo")
        self.chapter_button.setEnabled(False)

        pages = pdf_page_count(doc)
        self.page_label = QLabel("Página")
        self.page_spin = QSpinBox()
        self.page_spin.setRange(1, max(1, pages))
        self.page_button = QPushButton("Leer desde esta página")
        self._picker = [self.chapter_list, self.chapter_button]
        if pages:
            self._picker += [self.page_label, self.page_spin, self.page_button]
        for widget in (self.chapter_list, self.chapter_button, self.page_label,
                       self.page_spin, self.page_button):
            widget.hide()

        page_row = QHBoxLayout()
        page_row.addWidget(self.page_label)
        page_row.addWidget(self.page_spin)
        page_row.addWidget(self.page_button)
        page_row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(self.where_label)
        layout.addWidget(self.continue_button)
        layout.addWidget(self.start_button)
        layout.addWidget(self.choose_button)
        layout.addWidget(self.chapter_list)
        layout.addWidget(self.chapter_button)
        layout.addLayout(page_row)

        self.continue_button.clicked.connect(lambda: self._choose(ResumeChoice("continue")))
        self.start_button.clicked.connect(lambda: self._choose(ResumeChoice("start")))
        self.choose_button.clicked.connect(self._show_picker)
        self.chapter_list.currentRowChanged.connect(
            lambda row: self.chapter_button.setEnabled(row >= 0)
        )
        self.chapter_list.itemDoubleClicked.connect(lambda _item: self._choose_chapter())
        self.chapter_button.clicked.connect(self._choose_chapter)
        self.page_button.clicked.connect(
            lambda: self._choose(ResumeChoice("page", page=self.page_spin.value()))
        )

    def _show_picker(self) -> None:
        for widget in self._picker:
            widget.show()
        if self._doc.chapters and self.chapter_list.currentRow() < 0:
            self.chapter_list.setCurrentRow(0)

    def _choose_chapter(self) -> None:
        item = self.chapter_list.currentItem()
        if item is None:
            return
        ref = self._doc.chapters[item.data(Qt.ItemDataRole.UserRole)]
        self._choose(ResumeChoice("chapter", chapter=ref))

    def _choose(self, choice: ResumeChoice) -> None:
        self.chosen.emit(choice)
        self.accept()
