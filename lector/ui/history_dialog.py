"""Historial: documentos abiertos recientemente, con su capítulo y la fecha de lectura."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from lector.models import Document, SourceKind
from lector.persistence.repositories import DocumentRepo, ProgressRecord
from lector.services.worker import AsyncWorker, call_ui
from lector.ui.resume_dialog import where_text

log = logging.getLogger(__name__)

KIND_TEXT = {SourceKind.URL: "Web", SourceKind.PDF: "PDF", SourceKind.EPUB: "EPUB"}
HEADERS = ("Título", "Tipo", "Capítulo", "Fecha")


def _row(doc: Document, progress: ProgressRecord | None) -> tuple[str, str, str, str]:
    if progress is None:
        chapter, date = "Sin empezar", "—"
    else:
        chapter = where_text(doc, progress.position)
        date = datetime.fromtimestamp(progress.last_read_at).strftime("%d/%m/%Y %H:%M")
    return doc.title or doc.origin, KIND_TEXT.get(doc.kind, str(doc.kind)), chapter, date


class HistoryDialog(QDialog):
    """Sin modalidad. Un doble clic emite `document_chosen(doc, progreso o None)` y cierra."""

    document_chosen = Signal(object, object)

    def __init__(
        self,
        worker: AsyncWorker,
        documents: DocumentRepo,
        parent: QWidget | None = None,
        on_error: Callable[[BaseException], None] | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Historial")
        self.resize(640, 360)
        self._rows: list[tuple[Document, ProgressRecord | None]] = []
        self._on_error = on_error

        self.status_label = QLabel("Cargando…")
        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, len(HEADERS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.table.cellDoubleClicked.connect(lambda row, _column: self._choose(row))

        layout = QVBoxLayout(self)
        layout.addWidget(self.status_label)
        layout.addWidget(self.table)
        layout.addWidget(QLabel("Haz doble clic en un documento para abrirlo."))

        # La consulta va al hilo de trabajo: la interfaz nunca toca la base de datos.
        call_ui(worker.submit(asyncio.to_thread(documents.list_recent)), self._fill, self._failed)

    def _fill(self, rows: list[tuple[Document, ProgressRecord | None]]) -> None:
        self._rows = list(rows)
        self.table.setRowCount(len(self._rows))
        for r, (doc, progress) in enumerate(self._rows):
            for c, text in enumerate(_row(doc, progress)):
                self.table.setItem(r, c, QTableWidgetItem(text))
        if self._rows:
            self.status_label.hide()
        else:
            self.status_label.setText("Todavía no has abierto ningún documento.")

    def _failed(self, exc: BaseException) -> None:
        self.status_label.setText("No se pudo cargar el historial.")
        if self._on_error is not None:
            self._on_error(exc)
        elif not isinstance(exc, asyncio.CancelledError):
            log.error("No se pudo cargar el historial", exc_info=(type(exc), exc, exc.__traceback__))

    def _choose(self, row: int) -> None:
        if 0 <= row < len(self._rows):
            doc, progress = self._rows[row]
            self.document_chosen.emit(doc, progress)
            self.accept()
