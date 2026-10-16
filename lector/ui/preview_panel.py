"""Vista previa del documento abierto y panel para elegir el capítulo siguiente."""
from __future__ import annotations

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

from lector.models import Chapter, ChapterRef, Document, SourceKind

PREVIEW_PARAGRAPHS = 5


def is_generic_web(doc: Document) -> bool:
    """Web sin índice de capítulos (adaptador genérico): el prefijo de la obra es editable."""
    return doc.kind is SourceKind.URL and not doc.chapters


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
    """Título, capítulo, primeros párrafos y «siguiente» detectado, antes de leer."""

    prefix_edited = Signal(str)  # el usuario cambió el prefijo de la obra (web genérica)

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
        self._prefix_shown = ""

        prefix_row = QHBoxLayout()
        prefix_row.addWidget(self.prefix_label)
        prefix_row.addWidget(self.prefix_edit, 1)

        layout = QVBoxLayout(self)
        layout.addWidget(self.title_label)
        layout.addWidget(self.chapter_label)
        layout.addWidget(scroll)
        layout.addWidget(self.next_label)
        layout.addLayout(prefix_row)

        self.prefix_edit.editingFinished.connect(self._on_prefix_done)

    def show_preview(self, doc: Document, chapter: Chapter) -> None:
        self.title_label.setText(doc.title or chapter.title)
        self.chapter_label.setText(chapter.title)
        first = [p.text for p in chapter.paragraphs[:PREVIEW_PARAGRAPHS]]
        self.paragraphs_label.setText("\n\n".join(first))
        self.next_label.setText(next_text(chapter))
        generic = is_generic_web(doc)
        self._prefix_shown = (doc.work_prefix or "") if generic else ""
        self.prefix_edit.setText(self._prefix_shown)
        self.prefix_label.setVisible(generic)
        self.prefix_edit.setVisible(generic)

    def _on_prefix_done(self) -> None:
        text = self.prefix_edit.text().strip()
        if text != self._prefix_shown:
            self._prefix_shown = text
            self.prefix_edited.emit(text)


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
