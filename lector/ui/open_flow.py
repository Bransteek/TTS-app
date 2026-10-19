"""Flujo de apertura de la ventana principal: abrir, confirmar el prefijo, leer y detectar que el usuario se fue.

`OpenFlowMixin` es parte de `MainWindow` (usa sus widgets, `_run`, `_resume`, etc.); vive
aparte para que el flujo de apertura y la detección de «el usuario se fue» queden juntos.
Es un mixin y no un objeto delegado para que `self._play`, `self.isActiveWindow` y demás
sigan siendo atributos de la ventana (las pruebas y T17/T18 los usan así).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from urllib.parse import urlsplit

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QFileDialog

from lector.content.generic_web import set_prefix_override
from lector.errors import FileMovedOrChanged
from lector.models import Chapter, ChapterRef, Document, Position
from lector.persistence.repositories import ProgressRecord, SettingsRepo
from lector.services.recovery import relocate_and_store
from lector.services.reading_controller import SessionState
from lector.ui.preview_panel import validate_prefix
from lector.ui.resume_dialog import ResumeChoice, chapter_with_page

log = logging.getLogger(__name__)

NOTHING_TO_READ = "Pega la dirección de un capítulo o abre un archivo PDF o EPUB."
RELOCATE_MISMATCH = "Ese archivo no es el mismo documento. Elige el original."
DOCUMENT_GONE = ('Ese documento ya no está en tu historial. Ábrelo otra vez con "Abrir PDF/EPUB".')
RELOCATED_TEXT = "Archivo localizado. Se sigue leyendo donde lo dejaste."
STARTED_TEXT = "Se empezó a leer con el prefijo y el capítulo detectados."
LEAVE_CHECK_MS = 300  # la app sin foco tanto tiempo: el usuario se fue (no es un parpadeo)
PLAYING_STATES = frozenset({SessionState.READING, SessionState.LOADING,
                            SessionState.WAITING_NETWORK})


@dataclass
class _Preview:
    locator: str
    doc: Document
    chapter: Chapter


def _store_override(settings: SettingsRepo, chapter_url: str, path: str | None) -> None:
    """Recuerda el prefijo corregido de la obra de `chapter_url` (None: lo olvida).

    En el hilo de trabajo.
    """
    overrides = set_prefix_override(settings.get("work_prefix_overrides"), chapter_url, path)
    settings.set("work_prefix_overrides", overrides)


def _chapter_start(index: int, ref: ChapterRef, page: int | None = None) -> Position:
    return Position(ref.index if ref.index is not None else index, ref.locator, 0, 0, 0, page)


class OpenFlowMixin:
    """Abrir un capítulo o archivo, confirmar el prefijo, empezar a leer y notar que el usuario se fue."""

    def _init_open_flow(self) -> None:
        self._preview: _Preview | None = None
        self._opening = False
        self._open_gen = 0  # cada apertura nueva deja caducas las respuestas de las anteriores
        self._play_pending = False  # un play() en vuelo: no se manda otro
        self._confirm_after_leer = False  # «Leer» pidió leer y la vista previa espera confirmar
        # Posición elegida (capítulo, «empezar», …) que aún no cargó: un reintento vuelve a ella.
        self._pending_start: tuple[str, Position] | None = None
        self._relocate_doc: str | None = None  # documento cuyo archivo hay que volver a localizar
        self._relocating = False  # buscando el archivo (diálogo abierto o comprobando): un solo intento
        self._start_gen = 0  # sube con cada apertura o lectura nueva: una búsqueda vieja ya no lee
        self._own_dialogs = 0  # diálogos nativos propios abiertos (no cuentan como irse)
        self._leave_timer = QTimer(self)
        self._leave_timer.setSingleShot(True)
        self._leave_timer.setInterval(LEAVE_CHECK_MS)
        self._leave_timer.timeout.connect(self._check_user_left)

    # --- abrir y leer ------------------------------------------------------------------------

    def _on_read_clicked(self) -> None:
        locator = self.url_edit.text().strip()
        if not locator:
            self.show_notice(NOTHING_TO_READ)
            return
        if self._opening or self._play_pending:
            return  # doble clic: ya hay una orden en vuelo
        preview = self._preview
        if preview is None or preview.locator != locator:
            self._open(locator, autoplay=True)
            return
        doc = preview.doc
        if self._session_doc == doc.doc_id:
            # Ya se empezó a leer este documento: «Leer» no lo reinicia ni vuelve al
            # capítulo elegido al principio; sigue donde iba.
            if self._state in PLAYING_STATES:
                return
            if self._state is SessionState.PAUSED:
                self._run(self._ctl.resume())
                return
            if not self.preview.needs_confirmation():
                self._play(doc.doc_id, None)
                return
            # Se volvió a abrir y la vista previa espera confirmar: vale lo que se elija ahora.
        edited = self.preview.edited_prefix()
        if edited is not None:
            self._reopen_with_prefix(preview, edited)
            return
        picked = self.preview.selected_chapter()
        self._play(doc.doc_id, _chapter_start(*picked) if picked else None)

    def _on_open_file_clicked(self) -> None:
        with self.own_native_dialog():
            path, _ = QFileDialog.getOpenFileName(
                self, "Abrir PDF o EPUB", "", "Libros PDF o EPUB (*.pdf *.epub)"
            )
        if path:
            self.url_edit.setText(path)
            self._open(path, autoplay=False)

    # --- archivo movido: volver a localizarlo -------------------------------------------------

    def _offer_relocate(self, doc_id: str) -> None:
        """Ofrece «Buscar el archivo…» (no modal); sin el usuario delante espera a que vuelva."""
        self._relocate_doc = doc_id
        self._show_relocate_if_present()

    def _show_relocate_if_present(self) -> None:
        if self._relocate_doc is not None and self.user_present():
            self.relocate_button.setEnabled(not self._relocating)
            self.relocate_button.show()

    def _hide_relocate(self, *, keep_offer: bool = False) -> None:
        if not keep_offer:
            self._relocate_doc = None
        self.relocate_button.hide()

    def _on_relocate_clicked(self) -> None:
        doc_id = self._relocate_doc
        if doc_id is None or self._relocating:
            return
        self._relocating = True
        self.relocate_button.setEnabled(False)
        try:
            with self.own_native_dialog():
                path, _ = QFileDialog.getOpenFileName(
                    self, "Buscar el archivo", "", "Libros PDF o EPUB (*.pdf *.epub)"
                )
        except BaseException:
            self._relocating = False
            self.relocate_button.setEnabled(True)
            raise
        if not path:
            self._relocating = False
            self.relocate_button.setEnabled(True)
            return
        documents, gen = self._documents, self._start_gen

        async def relocate() -> bool:
            doc = await asyncio.to_thread(documents.get, doc_id)
            if doc is None:
                return False
            repos = SimpleNamespace(documents=documents)
            await asyncio.to_thread(relocate_and_store, repos, doc, path)
            return True

        def relocated(found: bool) -> None:
            self._relocating = False
            if not found:
                self._hide_relocate()
                self.show_notice(DOCUMENT_GONE, "warn")
                return
            if self._relocate_doc != doc_id or gen != self._start_gen:
                return  # ya se abrió otra cosa: el archivo queda guardado y no se lee nada
            self._hide_relocate()
            self._play(doc_id, None)  # retoma del progreso guardado
            self.show_notice(RELOCATED_TEXT)

        def failed(exc: BaseException) -> None:
            self._relocating = False
            self.relocate_button.setEnabled(True)
            if isinstance(exc, FileMovedOrChanged):
                if self._relocate_doc == doc_id and gen == self._start_gen:
                    self.show_notice(RELOCATE_MISMATCH, "warn")  # puede elegir otro
                    self._offer_relocate(doc_id)
            else:
                self._on_error(exc)

        if not self._run(relocate(), relocated, failed):
            self._relocating = False
            self.relocate_button.setEnabled(True)

    def _open(self, locator: str, autoplay: bool) -> None:
        self._start_open(locator, self._ctl.open(locator), autoplay, confirmed=False)

    def _reopen_with_prefix(self, preview: _Preview, typed: str) -> None:
        """Guarda el prefijo corregido de la obra y vuelve a abrir con él antes de leer.

        Un campo vacío olvida la corrección guardada (vuelve a la detección). El mismo
        prefijo (con o sin «/» final) no cambia nada: se lee lo ya abierto.
        """
        doc, chapter_url = preview.doc, preview.chapter.ref.locator
        path: str | None = None
        if typed:
            path, error = validate_prefix(typed, doc.origin, chapter_url)
            if error is not None:
                self.preview.show_prefix_error(error)
                return
            if path == urlsplit(doc.work_prefix or "").path.rstrip("/") + "/":
                self._play(doc.doc_id, None)
                return
        settings, documents, ctl, locator = self._settings, self._documents, self._ctl, preview.locator

        async def save_and_open():
            await asyncio.to_thread(_store_override, settings, chapter_url, path)
            result = await ctl.open(locator)
            if result[0].doc_id != doc.doc_id:
                # El documento del prefijo equivocado no queda en el historial si no se leyó.
                # Es limpieza: si falla, se avisa en el registro y se lee igual.
                try:
                    await asyncio.to_thread(documents.delete_if_unread, doc.doc_id)
                except Exception:
                    log.warning("No se pudo borrar el documento sin leer %s", doc.doc_id,
                                exc_info=True)
            return result

        self._start_open(locator, save_and_open(), autoplay=True, confirmed=True)

    def _start_open(self, locator: str, job: Coroutine, autoplay: bool, confirmed: bool) -> None:
        self._clear_notice()
        self._clear_choice()
        self._resume.cancel()
        self._confirm_after_leer = False
        self._pending_start = None
        self._open_gen += 1
        self._start_gen += 1
        gen = self._open_gen
        self._set_opening(True)

        def opened(result) -> None:
            if gen != self._open_gen:
                return  # caducada: después se abrió otra cosa (p. ej. desde el historial)
            self._set_opening(False)
            doc, chapter, progress = result
            self._preview = _Preview(locator, doc, chapter)
            fresh = progress is None
            self.preview.show_preview(
                doc, chapter,
                editable_prefix=fresh and not confirmed and doc.adapter == "generic",
                choose_chapter=fresh and doc.index_page,
            )
            self.preview.show()
            if progress is not None:
                known = chapter.title if chapter.ref.locator == progress.position.chapter_locator else None
                self.offer_resume(doc, progress, chapter_title=known)
            elif autoplay and not self.preview.needs_confirmation():
                self._play(doc.doc_id, None)
            elif autoplay:
                self._confirm_after_leer = True
                if not self.user_present():
                    self._auto_confirm()  # se fue mientras se abría: no espera a nadie

        def failed(exc: BaseException) -> None:
            if gen != self._open_gen:
                return
            self._set_opening(False)
            self._on_error(exc)

        if not self._run(job, opened, failed):
            self._set_opening(False)

    def _set_opening(self, busy: bool) -> None:
        self._opening = busy
        self.read_button.setEnabled(not busy)
        self.open_file_button.setEnabled(not busy)
        if busy:
            self.statusBar().showMessage("Abriendo…")
        else:
            self.statusBar().clearMessage()

    def _play(self, doc_id: str, start: Position | None) -> None:
        """Lee `doc_id` desde `start`; sin `start`, desde la posición elegida que no llegó a cargar."""
        if start is not None:
            self._pending_start = (doc_id, start)
        elif self._pending_start is not None and self._pending_start[0] == doc_id:
            start = self._pending_start[1]
        self._play_job(doc_id, self._ctl.play(doc_id, start))

    def _play_job(self, doc_id: str, job: Coroutine) -> None:
        """Manda una orden que empieza a leer `doc_id`.

        Mientras está en vuelo, «Leer» y Leer/Pausar no mandan otra (doble clic); una
        elección explícita (reanudar, historial) sí se manda.
        """
        self._session_doc = doc_id
        self._start_gen += 1
        self._clear_choice()  # una elección pendiente era de la lectura anterior
        self._confirm_after_leer = False
        self.preview.settle()  # ya no se espera confirmar el prefijo ni el capítulo

        def done(_result=None) -> None:
            self._play_pending = False

        def failed(exc: BaseException) -> None:
            done()
            self._on_error(exc)

        self._play_pending = self._run(job, done, failed)

    def _on_play_pause(self) -> None:
        # Pausar y seguir valen aunque haya un play() en vuelo; solo no se manda otro play.
        if self._state in PLAYING_STATES:
            self._run(self._ctl.pause())
        elif self._state is SessionState.PAUSED:
            self._run(self._ctl.resume())
        elif self._play_pending or self._opening:
            return
        elif self._session_doc is not None:
            self._play(self._session_doc, None)
        else:
            self._on_read_clicked()

    # --- reanudación y presencia del usuario ----------------------------------------------------

    def user_present(self) -> bool:
        """¿El usuario mira la app? Visible, sin minimizar y con el foco (aquí o en un diálogo)."""
        if not self.isVisible() or self.isMinimized():
            return False
        return self.isActiveWindow() or QApplication.activeWindow() is not None

    def _auto_confirm(self) -> None:
        """Sin el usuario delante tras «Leer»: empieza con lo detectado, sin esperar.

        Prefijo detectado en una web genérica; capítulo 1 al abrir desde el índice. Lo que
        el usuario hubiera escrito o elegido sin confirmar no cuenta.
        """
        preview = self._preview
        if not self._confirm_after_leer or preview is None:
            return
        self._confirm_after_leer = False
        if not self.preview.needs_confirmation():
            return
        doc = preview.doc
        start = None
        if not self.preview.chapter_list.isHidden() and doc.chapters:
            start = _chapter_start(0, doc.chapters[0])
        self._play(doc.doc_id, start)
        self.show_notice(STARTED_TEXT)

    def _user_left(self, hidden: bool) -> None:
        """El usuario se fue al juego: oculta o minimizada (`hidden`) o la app sin foco."""
        if self._quitting or self._own_dialogs:
            return
        self._auto_confirm()
        if hidden:
            self._resume.window_hidden()
        else:
            self._resume.window_deactivated()

    def _handle_app_state(self, state: Qt.ApplicationState) -> None:
        # Un diálogo de la propia app con el foco no deja la app inactiva. Se espera un poco
        # antes de decidir: un parpadeo del foco no cuenta como irse.
        if state == Qt.ApplicationState.ApplicationActive:
            self._leave_timer.stop()
        elif self.isVisible():  # oculta: ya se trató al ocultarla
            self._leave_timer.start()

    def _check_user_left(self) -> None:
        if self._own_dialogs:
            return
        if self.isVisible() and not self.user_present():
            self._user_left(hidden=self.isMinimized())

    @contextmanager
    def own_native_dialog(self) -> Iterator[None]:
        """Envuelve un diálogo nativo de la app (p. ej. `QFileDialog.getOpenFileName`).

        Mientras está abierto, Qt no ve ninguna ventana activa y la app queda inactiva:
        eso no es irse al juego. Al cerrarse se mira solo el estado real (si el usuario
        sigue fuera, la comprobación diferida lo detecta). Puede anidarse; T17 y T18
        deben usarlo con sus propios diálogos nativos.
        """
        self._own_dialogs += 1
        self._leave_timer.stop()
        try:
            yield
        finally:
            self._own_dialogs -= 1
            if not self._own_dialogs:
                self._leave_timer.stop()
                if self.isVisible() and not self.user_present():
                    self._leave_timer.start()

    def offer_resume(self, doc: Document, progress: ProgressRecord, *,
                     chapter_title: str | None = None,
                     auto_continue_when_hidden: bool = True) -> None:
        """Ofrece continuar, empezar o elegir capítulo o página.

        Sin el usuario delante (oculta, minimizada o sin foco) nunca abre un diálogo:
        con `auto_continue_when_hidden` continúa; sin él, la oferta espera a que se
        muestre la ventana (la recuperación al arrancar no lee sin que se lo pidan).
        """
        self._resume.offer(doc, progress, present=self.user_present(),
                           chapter_title=chapter_title,
                           auto_continue_when_hidden=auto_continue_when_hidden)

    def _apply_resume(self, doc: Document, progress: ProgressRecord, choice: ResumeChoice) -> None:
        if choice.kind == "continue":
            self._play(doc.doc_id, None)
        elif choice.kind == "start":
            self._play(doc.doc_id, self._start_of(doc, progress))
        elif choice.kind == "chapter" and choice.chapter is not None:
            index = doc.chapters.index(choice.chapter) if choice.chapter in doc.chapters else 0
            self._play(doc.doc_id, _chapter_start(index, choice.chapter))
        elif choice.kind == "page" and choice.page is not None:
            self._play_job(doc.doc_id, self._play_page(doc, choice.page))

    def _start_of(self, doc: Document, progress: ProgressRecord) -> Position:
        if doc.chapters:
            return _chapter_start(0, doc.chapters[0])
        # Web sin índice: el principio es el del capítulo abierto (o el guardado).
        if self._preview is not None and self._preview.doc.doc_id == doc.doc_id:
            return _chapter_start(0, self._preview.chapter.ref)
        pos = progress.position
        return Position(pos.chapter_index, pos.chapter_locator, 0, 0, 0, None)

    async def _play_page(self, doc: Document, page: int) -> None:
        """En el trabajador: busca la primera frase de la página y lee desde ahí."""
        position = await self._ctl.locate_page(doc.doc_id, page)
        if position is None:
            found = chapter_with_page(doc, page)
            position = _chapter_start(*found, page=page) if found else None
        await self._ctl.play(doc.doc_id, position)

    def _open_from_history(self, doc: Document, progress: ProgressRecord | None,
                           chapter_title: str | None = None) -> None:
        self._clear_notice()
        self._clear_choice()
        self._resume.cancel()
        self._confirm_after_leer = False
        self._pending_start = None
        self._start_gen += 1
        self._open_gen += 1  # lo que se estuviera abriendo ya no vale: manda el historial
        self._set_opening(False)
        self.url_edit.setText(doc.origin)
        self.preview.hide()
        self._preview = None
        if progress is not None:
            self.offer_resume(doc, progress, chapter_title=chapter_title)
        else:
            self._play(doc.doc_id, None)

