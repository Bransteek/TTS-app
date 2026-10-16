"""Voces locales (Piper): descargas, muestra, panel de modelos y aviso de primer arranque.

Hilos: nada de esto hace E/S en el hilo de Qt. Las descargas, las lecturas de los
modelos y de la configuración y la síntesis de la muestra van al hilo de trabajo con
`worker.submit`; los resultados vuelven con `call_ui` y el progreso con una señal que
Qt entrega en cola en el hilo de la interfaz.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from lector.audio.pcm import OUTPUT_RATE
from lector.audio.player import AudioOutput, AudioPlayer, QueuedClip, SoundDeviceOutput
from lector.errors import LectorError
from lector.models import Chunk, Sentence
from lector.persistence.repositories import DEFAULT_SETTINGS, SettingsRepo
from lector.services.worker import AsyncWorker, call_ui
from lector.tts.piper_engine import PiperModelManager

log = logging.getLogger(__name__)

DEFAULT_MODEL: str = DEFAULT_SETTINGS["piper_voice"]  # la voz de respaldo (~76 MB)
BANNER_TEXT = (
    "Sin voz local, un corte de internet dejará la lectura en espera. "
    "¿Descargar la voz de respaldo (~76 MB)?"
)
DOWNLOAD_FAILED = (
    "No se pudo descargar la voz. Revisa tu conexión a internet e inténtalo de nuevo."
)
UNEXPECTED = "Ocurrió un problema inesperado. Inténtalo de nuevo."
SAMPLE_BUSY = "Pausa o detén la lectura para escuchar una muestra."
SAMPLE_PLAYING = "Escuchando la muestra…"
HELP_TEXT = (
    "Piper es una voz que funciona en tu equipo, sin internet: si se corta la conexión, "
    "la lectura sigue con ella en lugar de quedarse en espera. "
    "Los modelos se descargan de huggingface.co/rhasspy/piper-voices y se guardan en la "
    "carpeta de datos de la app. Cada voz tiene su propia licencia, indicada en su "
    "MODEL_CARD. Ninguna de estas voces es igual a Dalia."
)
STATUS_MISSING = "Sin descargar"
STATUS_INSTALLED = "Instalada"
STATUS_IN_USE = "En uso"
STATUS_DOWNLOADING = "Descargando… {pct} %"
GENDER_TEXT = {"F": "Mujer", "M": "Hombre", "F/M": "Mujer y hombre", "?": "Sin confirmar"}

# Frases de la muestra: sin nada que se parezca a una promesa sobre la voz.
SAMPLE_SENTENCES = (
    "Hola, esta es una muestra de la voz sin conexión.",
    "Así sonará la lectura cuando no haya internet.",
)
SAMPLE_GRACE_S = 3.0  # margen sobre la duración de la muestra antes de darla por terminada


def run_job(
    worker: AsyncWorker,
    coro: Coroutine[Any, Any, Any],
    on_ok: Callable[[Any], None],
    on_err: Callable[[BaseException], None],
) -> concurrent.futures.Future | None:
    """Manda `coro` al trabajador y entrega su resultado en el hilo de Qt (None si ya paró)."""
    try:
        future = worker.submit(coro)
    except RuntimeError:
        log.warning("El trabajador ya no está en marcha; se ignora la orden")
        return None
    call_ui(future, on_ok, on_err)
    return future


def error_text(exc: BaseException) -> str | None:
    """Texto para el usuario (None si fue una cancelación, que no se avisa)."""
    if isinstance(exc, asyncio.CancelledError):
        return None
    if isinstance(exc, LectorError):
        return exc.user_message
    log.error("Error inesperado en la configuración", exc_info=(type(exc), exc, exc.__traceback__))
    return UNEXPECTED


# --- descargas -----------------------------------------------------------------------------


class PiperDownloads(QObject):
    """Descargas de modelos en el trabajador; la comparten el aviso y la configuración.

    `progress(nombre, fracción)` llega en el hilo de Qt; `finished(nombre, ok, mensaje)` sale
    una vez por descarga (mensaje vacío si se canceló). El gestor de modelos limpia los
    ficheros a medias al fallar o al cancelar, y solo descarga de huggingface.co.
    """

    progress = Signal(str, float)
    finished = Signal(str, bool, str)
    _raw_progress = Signal(str, float)  # emitida desde el hilo de trabajo

    def __init__(self, worker: AsyncWorker, models: PiperModelManager,
                 parent: QObject | None = None):
        super().__init__(parent)
        self._worker = worker
        self._models = models
        self._futures: dict[str, concurrent.futures.Future] = {}
        self._fraction: dict[str, float] = {}
        self._raw_progress.connect(self._on_raw_progress)  # en cola: el emisor es otro hilo

    def is_active(self, name: str) -> bool:
        return name in self._futures

    def fraction(self, name: str) -> float | None:
        return self._fraction.get(name) if name in self._futures else None

    def start(self, name: str) -> bool:
        """Empieza a descargar `name`; False si ya se estaba descargando o no se pudo."""
        if name in self._futures:
            return False
        last = [-1]

        def report(fraction: float) -> None:  # hilo de trabajo
            pct = int(fraction * 100)
            if pct == last[0]:
                return
            last[0] = pct
            try:
                self._raw_progress.emit(name, fraction)
            except RuntimeError:  # la interfaz ya se cerró
                pass

        future = run_job(self._worker, self._models.download(name, report),
                         lambda _r: self._done(name, future, None),
                         lambda exc: self._done(name, future, exc))
        if future is None:
            return False
        self._futures[name] = future
        self._fraction[name] = 0.0
        return True

    def cancel(self, name: str) -> None:
        future = self._futures.get(name)
        if future is not None:
            future.cancel()  # cancela la tarea en el bucle; el gestor borra el «.part»

    @Slot(str, float)
    def _on_raw_progress(self, name: str, fraction: float) -> None:
        if name in self._futures:  # un aviso tardío de una descarga ya terminada no cuenta
            self._fraction[name] = fraction
            self.progress.emit(name, fraction)

    def _done(self, name: str, future, exc: BaseException | None) -> None:
        if self._futures.get(name) is not future:
            return
        del self._futures[name]
        self._fraction.pop(name, None)
        if exc is None:
            self.finished.emit(name, True, "")
        elif isinstance(exc, asyncio.CancelledError):
            self.finished.emit(name, False, "")
        else:
            log.warning("No se pudo descargar la voz %s", name,
                        exc_info=(type(exc), exc, exc.__traceback__))
            self.finished.emit(name, False, DOWNLOAD_FAILED)


# --- muestra -------------------------------------------------------------------------------


def _sample_chunk() -> Chunk:
    sentences, start = [], 0
    for index, text in enumerate(SAMPLE_SENTENCES):
        sentences.append(Sentence(index, 0, text, start, start + len(text), None))
        start += len(text) + 1
    text = " ".join(SAMPLE_SENTENCES)
    return Chunk("muestra", "muestra", 0, 0, tuple(sentences), text, 0, len(text))


class _SampleEvents:
    def __init__(self, finished: asyncio.Future):
        self.finished = finished

    def on_sentence_started(self, chunk: Chunk, sentence_index: int) -> None:
        pass

    def on_chunk_finished(self, chunk: Chunk) -> None:
        if not self.finished.done():
            self.finished.set_result(None)

    def on_output_error(self, err: Exception) -> None:
        if not self.finished.done():
            self.finished.set_exception(err)


class SamplePlayer:
    """«Escuchar muestra»: sintetiza con Piper y lo reproduce en memoria.

    Corre en el hilo de trabajo. Solo suena con la lectura principal parada o en pausa
    (`is_busy()` se consulta en el trabajador antes de sintetizar y antes de reproducir),
    así que nunca la interrumpe. Usa su propio `AudioPlayer` con su propia salida, que se
    cierra al terminar; el audio nunca se escribe en disco.
    """

    def __init__(
        self,
        engine,
        output_factory: Callable[[], AudioOutput] = SoundDeviceOutput,
        is_busy: Callable[[], bool] = lambda: False,
    ):
        self._engine = engine  # el PiperEngine de la app (un solo modelo cargado en memoria)
        self._output_factory = output_factory
        self._is_busy = is_busy
        self._player: AudioPlayer | None = None
        self._finished: asyncio.Future | None = None

    async def play(self, voice: str, speed: float = 1.0) -> bool:
        """Reproduce la muestra y vuelve al terminar. False si la lectura está en marcha."""
        if self._is_busy():
            return False
        await self.stop()
        chunk = _sample_chunk()
        clip = await self._engine.synthesize(chunk, voice, speed)
        if self._is_busy():
            return False
        loop = asyncio.get_running_loop()
        finished = loop.create_future()
        player = AudioPlayer(self._output_factory(), loop, events=_SampleEvents(finished))
        self._player, self._finished = player, finished
        try:
            await player.enqueue(QueuedClip(chunk, clip.pcm, clip.marks, "piper"))
            seconds = len(clip.pcm) / OUTPUT_RATE
            try:
                await asyncio.wait_for(asyncio.shield(finished), seconds + SAMPLE_GRACE_S)
            except TimeoutError:
                log.warning("La muestra no terminó de sonar a tiempo; se corta")
        finally:
            player.close()
            if self._player is player:
                self._player = self._finished = None
        return True

    async def stop(self) -> None:
        """Corta la muestra que suene (al cerrar la configuración o al pedir otra)."""
        player, finished = self._player, self._finished
        self._player = self._finished = None
        if player is not None:
            player.close()
        if finished is not None and not finished.done():
            finished.set_result(None)


# --- aviso de primer arranque ------------------------------------------------------------


def _first_run_needed(settings: SettingsRepo, models: PiperModelManager) -> bool:
    return not models.installed() and not settings.get("first_run_done")


class FirstRunBanner(QFrame):
    """Aviso no modal arriba de la ventana: ofrece la voz de respaldo si no hay ninguna.

    Sale si no hay ningún modelo instalado y el usuario no lo rechazó antes
    (`first_run_done`). «Ahora no» lo guarda y el indicador de Piper sigue diciendo
    «Sin voz instalada». Con la descarga en curso, el segundo botón la cancela.
    """

    def __init__(self, worker: AsyncWorker, settings: SettingsRepo,
                 models: PiperModelManager, downloads: PiperDownloads,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._worker = worker
        self._settings = settings
        self._models = models
        self._downloads = downloads
        self.checked = False  # True cuando ya se decidió si mostrarlo

        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.text_label = QLabel(BANNER_TEXT)
        self.text_label.setWordWrap(True)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.hide()
        self.download_button = QPushButton("Descargar")
        self.later_button = QPushButton("Ahora no")
        buttons = QHBoxLayout()
        buttons.addWidget(self.progress_bar, 1)
        buttons.addStretch(0)
        buttons.addWidget(self.download_button)
        buttons.addWidget(self.later_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.text_label)
        layout.addWidget(self.error_label)
        layout.addLayout(buttons)
        self.hide()

        self.download_button.clicked.connect(self._on_download)
        self.later_button.clicked.connect(self._on_later)
        downloads.progress.connect(self._on_progress)
        downloads.finished.connect(self._on_finished)

    def check(self) -> None:
        """Decide en el trabajador si hace falta el aviso y lo muestra si es así."""
        job = asyncio.to_thread(_first_run_needed, self._settings, self._models)
        if run_job(self._worker, job, self._on_checked, self._on_check_failed) is None:
            self.checked = True

    def _on_checked(self, needed: bool) -> None:
        self.checked = True
        self.setVisible(bool(needed))
        if needed and self._downloads.is_active(DEFAULT_MODEL):
            self._set_downloading(self._downloads.fraction(DEFAULT_MODEL) or 0.0)

    def _on_check_failed(self, exc: BaseException) -> None:
        self.checked = True
        error_text(exc)  # se registra; sin aviso: el indicador de Piper ya lo dice

    def _on_download(self) -> None:
        self.error_label.hide()
        self._downloads.start(DEFAULT_MODEL)
        if self._downloads.is_active(DEFAULT_MODEL):
            self._set_downloading(self._downloads.fraction(DEFAULT_MODEL) or 0.0)
        else:
            self._show_error(DOWNLOAD_FAILED)

    def _on_later(self) -> None:
        if self._downloads.is_active(DEFAULT_MODEL):
            self._downloads.cancel(DEFAULT_MODEL)  # vuelve a la oferta al llegar `finished`
            return
        self.hide()
        self._mark_done()

    def _set_downloading(self, fraction: float) -> None:
        self.download_button.setEnabled(False)
        self.later_button.setText("Cancelar")
        self.progress_bar.setValue(int(fraction * 100))
        self.progress_bar.show()

    def _set_offer(self) -> None:
        self.download_button.setEnabled(True)
        self.later_button.setText("Ahora no")
        self.progress_bar.hide()

    def _show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.show()

    @Slot(str, float)
    def _on_progress(self, name: str, fraction: float) -> None:
        if name == DEFAULT_MODEL and not self.isHidden():
            self._set_downloading(fraction)

    @Slot(str, bool, str)
    def _on_finished(self, name: str, ok: bool, message: str) -> None:
        if ok:  # ya hay una voz local (esta u otra): el aviso sobra
            if not self.isHidden():
                self.hide()
            self._mark_done()
            return
        if name != DEFAULT_MODEL:
            return
        self._set_offer()
        if message:
            self._show_error(message)

    def _mark_done(self) -> None:
        job = asyncio.to_thread(self._settings.set, "first_run_done", True)
        run_job(self._worker, job, lambda _r: None, error_text)


# --- pestaña de voces locales ------------------------------------------------------------


def _read_models(models: PiperModelManager, settings: SettingsRepo):
    rows = [(m["name"], models.is_installed(m["name"]), models.speakers(m["name"]))
            for m in PiperModelManager.CATALOG]
    return rows, settings.get("piper_voice"), settings.get("piper_speaker")


def _persist_piper(settings: SettingsRepo, name: str, speaker: str | None) -> None:
    settings.set("piper_voice", name)
    settings.set("piper_speaker", speaker)


class PiperSetupPanel(QWidget):
    """Tabla del `CATALOG` con «Descargar», «Escuchar muestra» y «Usar».

    `voice_changed` sale tras «Usar» o tras una descarga (la ventana recarga sus voces).
    """

    voice_changed = Signal()

    def __init__(self, worker: AsyncWorker, controller, settings: SettingsRepo,
                 models: PiperModelManager, downloads: PiperDownloads,
                 sample: SamplePlayer | None = None, parent: QWidget | None = None,
                 speed: Callable[[], float] = lambda: 1.0):
        super().__init__(parent)
        self._worker = worker
        self._ctl = controller
        self._settings = settings
        self._models = models
        self._downloads = downloads
        self._sample = sample
        self._speed = speed
        self._installed: dict[str, list[str]] = {}  # modelo instalado -> hablantes
        self._current: tuple[str | None, str | None] = (None, None)
        self._started: set[str] = set()  # descargas pedidas desde aquí (se cancelan al cerrar)
        self._sampling = False
        self.loaded = False

        catalog = PiperModelManager.CATALOG
        self.table = QTableWidget(len(catalog), 4)
        self.table.setHorizontalHeaderLabels(["Voz", "Género", "Tamaño", "Estado"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for row, model in enumerate(catalog):
            name_item = QTableWidgetItem(model["label"])
            name_item.setData(Qt.ItemDataRole.UserRole, model["name"])
            name_item.setToolTip(model["name"])
            self.table.setItem(row, 0, name_item)
            self.table.setItem(row, 1, QTableWidgetItem(GENDER_TEXT.get(model["gender"],
                                                                        model["gender"])))
            self.table.setItem(row, 2, QTableWidgetItem(f"{model['size_mb']} MB"))
            self.table.setItem(row, 3, QTableWidgetItem(STATUS_MISSING))

        self.details_label = QLabel()
        self.details_label.setWordWrap(True)
        self.speaker_label = QLabel("Hablante")
        self.speaker_combo = QComboBox()
        self.speaker_label.hide()
        self.speaker_combo.hide()
        self.download_button = QPushButton("Descargar")
        self.cancel_button = QPushButton("Cancelar descarga")
        self.cancel_button.hide()
        self.sample_button = QPushButton("Escuchar muestra")
        self.use_button = QPushButton("Usar")
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.help_label = QLabel(HELP_TEXT)
        self.help_label.setWordWrap(True)
        self.help_label.setTextFormat(Qt.TextFormat.PlainText)

        actions = QHBoxLayout()
        actions.addWidget(self.speaker_label)
        actions.addWidget(self.speaker_combo)
        actions.addStretch(1)
        for button in (self.download_button, self.cancel_button, self.sample_button,
                       self.use_button):
            actions.addWidget(button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.details_label)
        layout.addLayout(actions)
        layout.addWidget(self.status_label)
        layout.addWidget(self.help_label)

        self.table.itemSelectionChanged.connect(self._update_selection)
        self.download_button.clicked.connect(self._on_download)
        self.cancel_button.clicked.connect(self._on_cancel)
        self.sample_button.clicked.connect(self._on_sample)
        self.use_button.clicked.connect(self._on_use)
        downloads.progress.connect(self._on_progress)
        downloads.finished.connect(self._on_finished)
        self._update_selection()
        self.refresh()

    # --- datos -------------------------------------------------------------------------

    def model_at(self, row: int) -> str:
        return self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)

    def selected_model(self) -> str | None:
        rows = self.table.selectionModel().selectedRows()
        return self.model_at(rows[0].row()) if rows else None

    def refresh(self) -> None:
        """Vuelve a leer en el trabajador qué modelos hay y cuál se usa."""
        job = asyncio.to_thread(_read_models, self._models, self._settings)
        if run_job(self._worker, job, self._on_models, self._on_error) is None:
            self.loaded = True

    def _on_models(self, result) -> None:
        rows, voice, speaker = result
        self._installed = {name: speakers for name, ok, speakers in rows if ok}
        self._current = (voice, speaker)
        for row in range(self.table.rowCount()):
            self._update_status(row)
        self.loaded = True
        self._update_selection()

    def _update_status(self, row: int) -> None:
        name = self.model_at(row)
        fraction = self._downloads.fraction(name)
        if fraction is not None:
            text = STATUS_DOWNLOADING.format(pct=int(fraction * 100))
        elif name in self._installed:
            text = STATUS_IN_USE if name == self._current[0] else STATUS_INSTALLED
        else:
            text = STATUS_MISSING
        self.table.item(row, 3).setText(text)

    def _row_of(self, name: str) -> int | None:
        for row in range(self.table.rowCount()):
            if self.model_at(row) == name:
                return row
        return None

    def _update_selection(self) -> None:
        name = self.selected_model()
        model = next((m for m in PiperModelManager.CATALOG if m["name"] == name), None)
        installed = name in self._installed
        downloading = name is not None and self._downloads.is_active(name)
        self.download_button.setEnabled(name is not None and not installed and not downloading)
        self.cancel_button.setVisible(downloading)
        self.sample_button.setEnabled(installed and self._sample is not None
                                      and not self._sampling)
        self.use_button.setEnabled(installed)
        if model is None:
            self.details_label.setText("Elige una voz de la lista.")
        else:
            self.details_label.setText(
                f"{model['label']}: {model['size_mb']} MB. Licencia: la indicada en su "
                f"MODEL_CARD. {model['license_note']}"
            )
        speakers = self._installed.get(name, []) if installed else []
        self.speaker_combo.blockSignals(True)
        self.speaker_combo.clear()
        for speaker in speakers:
            self.speaker_combo.addItem(f"Voz {speaker}", speaker)
        voice, current_speaker = self._current
        wanted = current_speaker if name == voice else None
        if wanted is None and "F" in speakers:
            wanted = "F"
        index = self.speaker_combo.findData(wanted) if wanted is not None else -1
        self.speaker_combo.setCurrentIndex(index if index >= 0 else (0 if speakers else -1))
        self.speaker_combo.blockSignals(False)
        many = len(speakers) > 1
        self.speaker_label.setVisible(many)
        self.speaker_combo.setVisible(many)

    def _voice_for(self, name: str) -> tuple[str, str | None]:
        speaker = self.speaker_combo.currentData() if self._installed.get(name) else None
        return (f"{name}#{speaker}" if speaker else name), speaker

    # --- acciones ------------------------------------------------------------------------

    def _on_download(self) -> None:
        name = self.selected_model()
        if name is None:
            return
        self.status_label.clear()
        if self._downloads.start(name):
            self._started.add(name)
        self._refresh_row(name)
        self._update_selection()

    def _on_cancel(self) -> None:
        name = self.selected_model()
        if name is not None:
            self._downloads.cancel(name)

    def cancel_downloads(self) -> None:
        """Cancela las descargas pedidas desde este panel (al cerrar la configuración)."""
        for name in list(self._started):
            self._downloads.cancel(name)
        self._started.clear()

    def stop_sample(self) -> None:
        if self._sample is not None and self._sampling:
            run_job(self._worker, self._sample.stop(), lambda _r: None, error_text)

    def _on_sample(self) -> None:
        name = self.selected_model()
        if name is None or self._sample is None or name not in self._installed:
            return
        voice, _ = self._voice_for(name)
        self._sampling = True
        self.status_label.setText(SAMPLE_PLAYING)
        self._update_selection()
        future = run_job(self._worker, self._sample.play(voice, self._speed()),
                         self._on_sample_done, self._on_sample_failed)
        if future is None:
            self._on_sample_done(True)

    def _on_sample_done(self, played: bool) -> None:
        self._sampling = False
        self.status_label.setText("" if played else SAMPLE_BUSY)
        self._update_selection()

    def _on_sample_failed(self, exc: BaseException) -> None:
        self._sampling = False
        self.status_label.setText(error_text(exc) or "")
        self._update_selection()

    def _on_use(self) -> None:
        name = self.selected_model()
        if name is None or name not in self._installed:
            return
        voice, speaker = self._voice_for(name)
        label = next(m["label"] for m in PiperModelManager.CATALOG if m["name"] == name)

        async def use() -> None:
            await asyncio.to_thread(_persist_piper, self._settings, name, speaker)
            await self._ctl.set_voice(None, voice, False)  # sin cortar lo que ya suena

        def done(_result) -> None:
            self.status_label.setText(f"Sin conexión se leerá con {label}.")
            self.refresh()
            self.voice_changed.emit()

        run_job(self._worker, use(), done, self._on_error)

    def _on_error(self, exc: BaseException) -> None:
        self.loaded = True
        message = error_text(exc)
        if message:
            self.status_label.setText(message)

    # --- descargas -----------------------------------------------------------------------

    def _refresh_row(self, name: str) -> None:
        row = self._row_of(name)
        if row is not None:
            self._update_status(row)

    @Slot(str, float)
    def _on_progress(self, name: str, _fraction: float) -> None:
        self._refresh_row(name)
        if name == self.selected_model():
            self._update_selection()

    @Slot(str, bool, str)
    def _on_finished(self, name: str, ok: bool, message: str) -> None:
        self._started.discard(name)
        self._refresh_row(name)
        if message:
            self.status_label.setText(message)
        if ok:
            self.refresh()
            self.voice_changed.emit()
        self._update_selection()
