"""Desplegable «Capítulo:» de la ventana principal: la lista de capítulos del documento que se lee.

Sigue a la lectura sin saltar: lo que cambia el programa nunca emite `picked`. Solo
emite `picked` lo que hace el usuario: elegir en la lista o en las sugerencias, o
escribir el número o el nombre del capítulo y pulsar Intro. Lo escrito que no
coincide con ningún capítulo no hace nada, y al salir del desplegable vuelve a verse
el capítulo que suena. La rueda del ratón y las flechas no cambian de capítulo por sí
solas (un roce no corta la lectura): las flechas abren la lista.
"""
from __future__ import annotations

import re
import unicodedata

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFocusEvent, QKeyEvent, QWheelEvent
from PySide6.QtWidgets import QComboBox, QCompleter, QWidget

from lector.models import ChapterRef

NO_LIST_TIP = "Esta web no ofrece la lista de capítulos"
PICK_TIP = "Elige el capítulo que quieres oír. También puedes escribir su número y pulsar Intro."
_CHAPTER_WORD = r"(?:cap[ií]tulo|cap|chapter|ch|episodio|ep)\.?\s*0*"
_OPEN_KEYS = (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown)


def chapter_label(position: int, ref: ChapterRef) -> str:
    """Lo que se ve en la lista: el título, o «Capítulo N» si la fuente no lo da."""
    return ref.title.strip() or f"Capítulo {position + 1}"


def _fold(text: str) -> str:
    """Para comparar lo escrito: sin mayúsculas ni tildes («capitulo» vale por «Capítulo»)."""
    decomposed = unicodedata.normalize("NFD", text.strip().casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _same_locator(a: str, b: str) -> bool:
    return a.rstrip("/") == b.rstrip("/")


class ChapterPicker(QComboBox):
    picked = Signal(int)  # posición en `chapters`; solo por una acción del usuario

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.doc_id: str | None = None  # documento al que pertenece la lista
        self.chapters: list[ChapterRef] = []
        self._labels: list[str] = []
        self._current: ChapterRef | None = None  # el capítulo que suena
        self._current_title = ""
        # Intro sobre una sugerencia llega por dos caminos de Qt (la línea de texto y el
        # autocompletado): la misma elección solo se emite una vez por gesto.
        self._just_picked: int | None = None
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(24)
        self.setMaxVisibleItems(15)
        completer = QCompleter(self.model(), self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        completer.setMaxVisibleItems(15)
        self.setCompleter(completer)
        self.lineEdit().setPlaceholderText("Escribe un número de capítulo")
        self.lineEdit().returnPressed.connect(self._on_return)
        self.lineEdit().textEdited.connect(self._forget_pick)
        self.activated.connect(self._on_activated)
        self.setEnabled(False)

    # --- lo que hace el programa (nunca salta) ------------------------------------------------

    def set_chapters(self, doc_id: str, chapters: list[ChapterRef], current: ChapterRef,
                     title: str) -> None:
        """La lista de `doc_id` y el capítulo que suena; rehace la lista solo si cambió."""
        self.doc_id = doc_id
        self._current, self._current_title = current, title
        chapters = list(chapters)
        labels = [chapter_label(i, ref) for i, ref in enumerate(chapters)]
        if chapters != self.chapters or labels != self._labels:
            edit = self.lineEdit()
            typing = self._typing()
            typed = edit.text()
            self.blockSignals(True)
            try:
                self.clear()
                self.addItems(labels)
            finally:
                self.blockSignals(False)
            self.chapters, self._labels = chapters, labels
            if typing:
                edit.setText(typed)
                edit.setModified(True)
        has_list = bool(chapters)
        self.setEnabled(has_list)
        self.setToolTip(PICK_TIP if has_list else NO_LIST_TIP)
        if not self._typing():  # no pisa lo que el usuario está escribiendo
            self.show_current()

    def show_current(self) -> None:
        """Vuelve a mostrar el capítulo que suena (deja lo escrito sin efecto)."""
        self._forget_pick()
        index = self._index_of(self._current)
        self.blockSignals(True)
        try:
            self.setCurrentIndex(index)
            self.setEditText(self._labels[index] if index >= 0 else self._current_title)
        finally:
            self.blockSignals(False)

    def _index_of(self, ref: ChapterRef | None) -> int:
        if ref is None:
            return -1
        for i, known in enumerate(self.chapters):
            if _same_locator(known.locator, ref.locator):
                return i
        if ref.index is not None:
            for i, known in enumerate(self.chapters):
                if known.index == ref.index:
                    return i
        return -1

    def _typing(self) -> bool:
        edit = self.lineEdit()
        return edit.hasFocus() and edit.isModified()

    # --- lo que hace el usuario -----------------------------------------------------------------

    def _on_activated(self, index: int) -> None:
        if 0 <= index < len(self.chapters):
            self._pick(index)

    def _pick(self, index: int) -> None:
        if index == self._just_picked:
            return
        self._just_picked = index
        self.picked.emit(index)

    def _forget_pick(self) -> None:
        self._just_picked = None

    def showPopup(self) -> None:
        self._forget_pick()  # abrir la lista empieza otra elección
        super().showPopup()

    def _on_return(self) -> None:
        text = self.lineEdit().text()
        index = self.currentIndex()
        if index >= 0 and text == self.itemText(index):
            return  # nada escrito, o ya lo eligió la lista de sugerencias
        found = self.match(text)
        if found is None:
            return  # no es ningún capítulo: no se salta; al salir vuelve el que suena
        self.blockSignals(True)
        try:
            self.setCurrentIndex(found)
            self.setEditText(self._labels[found])
        finally:
            self.blockSignals(False)
        self._pick(found)

    def match(self, text: str) -> int | None:
        """Posición del capítulo que nombra `text` (nombre o número), o None.

        Primero el nombre exacto (sin mayúsculas ni tildes); un número solo busca ese
        número («99» no es el 199); otro texto, el primer capítulo que lo contiene.
        """
        needle = _fold(text)
        if not needle:
            return None
        folded = [_fold(label) for label in self._labels]
        if needle in folded:
            return folded.index(needle)
        if needle.isdecimal():
            number = int(needle)
            for pattern in (rf"{_CHAPTER_WORD}{number}(?!\d)", rf"(?<!\d)0*{number}(?!\d)"):
                regex = re.compile(pattern, re.IGNORECASE)
                for i, label in enumerate(self._labels):
                    if regex.search(label):
                        return i
            return None
        return next((i for i, label in enumerate(folded) if needle in label), None)

    def focusOutEvent(self, event: QFocusEvent) -> None:
        # El foco es del desplegable (la línea de texto le pasa el suyo). Se restaura antes
        # de que Qt mire el texto: salir del desplegable nunca salta. Abrir una lista no cuenta.
        if event.reason() != Qt.FocusReason.PopupFocusReason:
            self.show_current()
        super().focusOutEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in _OPEN_KEYS and not event.modifiers():
            self.showPopup()  # se elige en la lista; moverse con flechas no salta
            return
        super().keyPressEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        event.ignore()  # la rueda no cambia de capítulo
