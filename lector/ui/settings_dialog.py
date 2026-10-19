"""Configuración: voces de Edge, voces locales (Piper), lectura y privacidad.

Sin modalidad y sin botón «Guardar»: cada cambio se guarda solo, poco después de hacerlo
(y al cerrar). Hilos: la lectura inicial, el guardado, la lista de voces de Edge y el
borrado del texto van al hilo de trabajo con `worker.submit`, y vuelven con `call_ui`.
Lo que también cambia la sesión en marcha (voz, velocidad, lectura continua, guardar el
texto y tamaño de los fragmentos) se aplica además con el controlador; voz y velocidad con
`apply_now=False`, para no cortar lo que ya suena. Todo se guarda en `SettingsRepo`.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from lector.persistence.repositories import ChapterRepo, SettingsRepo, VoicesCacheRepo
from lector.services.worker import AsyncWorker
from lector.tts.base import EDGE_SPEED_RANGE
from lector.tts.piper_engine import PiperModelManager
from lector.services.sample_player import SamplePlayer
from lector.ui.jobs import error_text, run_job
from lector.ui.piper_setup import PiperDownloads, PiperSetupPanel
from lector.ui.playback_controls import edge_voice_items

log = logging.getLogger(__name__)

DALIA = "es-MX-DaliaNeural"
DALIA_MISSING = "La voz Dalia no está disponible ahora; elige otra voz en español"
PRIVACY_TEXT = (
    "Edge TTS envía el texto que se lee a los servidores de Microsoft. "
    "Piper lo procesa en tu equipo"
)
PURGE_QUESTION = (
    "¿Borrar también el texto de los capítulos que ya está guardado? "
    "El historial y el progreso se conservan."
)
PURGED = "Se borró el texto guardado de los capítulos. El historial y el progreso se conservan."
NO_VOICES = "No llegó ninguna voz. Inténtalo de nuevo más tarde."
NEEDS_REFRESH = "Pulsa «Actualizar lista» para ver las voces disponibles."
SAVED = "Cambios guardados."
OTHER_LANGUAGE = "Tu voz actual no es de este idioma; elige una."
RESTART_GROUP = "Se aplican al volver a abrir la app"
LATER_NOTE = (
    "El tamaño de los fragmentos vale desde el siguiente capítulo (o al volver a pulsar "
    "Leer). Los reintentos de Edge, la espera máxima de Edge, cuándo volver a probar Edge "
    "y el tiempo mínimo con Piper solo se aplican al volver a abrir la app."
)
SAVE_DELAY_MS = 400
DEFAULT_LANGUAGE = "es-MX"
# (clave, etiqueta, mínimo, máximo, paso, sufijo)
_CHUNK_SPINS = (
    ("edge_max_chars", "Fragmento de Edge (caracteres)", 100, 3000, 50, ""),
    ("edge_max_sentences", "Fragmento de Edge (frases)", 1, 10, 1, ""),
    ("piper_max_chars", "Fragmento de Piper (caracteres)", 50, 1500, 50, ""),
    ("skip_seconds", "Avance y retroceso", 1, 120, 1, " s"),
)
# EngineConfig y EdgeEngine se montan al arrancar la app: estos no llegan a la sesión.
_RESTART_SPINS = (
    ("edge_retries", "Reintentos de Edge", 0, 10, 1, ""),
    ("edge_timeout_s", "Espera máxima de Edge", 5, 120, 1, " s"),
    ("recovery_initial_s", "Volver a probar Edge tras", 10, 3600, 10, " s"),
    ("recovery_max_s", "Espera máxima entre pruebas de Edge", 10, 7200, 10, " s"),
    ("piper_cooldown_s", "Tiempo mínimo con Piper", 0, 3600, 10, " s"),
)
_CHUNK_KEYS = ("edge_max_chars", "edge_max_sentences", "piper_max_chars")
_KEYS = (tuple(key for key, *_ in _CHUNK_SPINS + _RESTART_SPINS)
         + ("edge_voice", "language", "speed", "continuous", "highlight_sentence", "store_text"))


@dataclass
class SettingsServices:
    """Lo que necesita la configuración (lo monta `MainWindow.enable_settings`)."""

    worker: AsyncWorker
    controller: Any  # ReadingController: set_voice, set_speed, set_continuous
    settings: SettingsRepo
    models: PiperModelManager
    voices: VoicesCacheRepo
    chapters: ChapterRepo
    downloads: PiperDownloads
    edge: Any = None  # EdgeEngine (list_voices, set_known_voices); None: sin «Actualizar lista»
    sample: SamplePlayer | None = None


def _read(settings: SettingsRepo, voices: VoicesCacheRepo) -> tuple[dict, list[dict]]:
    values = {key: settings.get(key) for key in _KEYS}
    try:
        cached = voices.load()
    except Exception:
        log.warning("No se pudo leer la caché de voces de Edge", exc_info=True)
        cached = []
    return values, cached


def _locales(voices: list[dict], current: str) -> list[str]:
    found = {v.get("Locale", "") for v in voices if v.get("Locale")}
    found.add(current)
    return sorted(found, key=lambda loc: (not loc.lower().startswith("es-"), loc))


class SettingsDialog(QDialog):
    """Pestañas «Voces», «Voces locales (Piper)», «Lectura» y «Privacidad».

    `saved` sale tras guardar algo que la ventana muestra (ella vuelve a leer la
    configuración). Al cerrarse guarda lo pendiente y corta la muestra; las descargas
    siguen (solo «Cancelar» las para).
    """

    saved = Signal()

    def __init__(self, services: SettingsServices, parent: QWidget | None = None):
        super().__init__(parent)
        self._s = services
        self._values: dict[str, Any] = {}
        self._edge_voices: list[dict] = []
        self._voices_known = False  # hay una lista real (de la caché o recién pedida)
        self._pending: dict[str, Any] = {}
        self.loaded = False
        self.setWindowTitle("Configuración")
        self.setModal(False)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_voices(), "Voces")
        self.piper_panel = PiperSetupPanel(
            services.worker, services.controller, services.settings, services.models,
            services.downloads, services.sample, speed=lambda: self.speed_spin.value(),
        )
        self.tabs.addTab(self.piper_panel, "Voces locales (Piper)")
        self.tabs.addTab(self._build_reading(), "Lectura")
        self.tabs.addTab(self._build_privacy(), "Privacidad")

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.close_button = buttons.button(QDialogButtonBox.StandardButton.Close)
        self.close_button.setText("Cerrar")
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(self.status_label)
        layout.addWidget(buttons)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(SAVE_DELAY_MS)
        self._save_timer.timeout.connect(self.flush)
        self.piper_panel.voice_changed.connect(self.saved.emit)
        self._connect()
        self._load()

    # --- construcción ------------------------------------------------------------------

    def _build_voices(self) -> QWidget:
        page = QWidget()
        self.language_combo = QComboBox()
        self.edge_combo = QComboBox()
        self.refresh_button = QPushButton("Actualizar lista")
        self.refresh_button.setEnabled(self._s.edge is not None)
        self.dalia_label = QLabel(DALIA_MISSING)
        self.dalia_label.setWordWrap(True)
        bold = self.dalia_label.font()
        bold.setBold(True)
        self.dalia_label.setFont(bold)
        self.dalia_label.hide()
        self.voices_status = QLabel()
        self.voices_status.setWordWrap(True)
        self.voice_hint = QLabel(OTHER_LANGUAGE)
        self.voice_hint.hide()
        form = QFormLayout(page)
        form.addRow("Idioma", self.language_combo)
        row = QHBoxLayout()
        row.addWidget(self.edge_combo, 1)
        row.addWidget(self.refresh_button)
        form.addRow("Voz en línea (Edge)", row)
        form.addRow(self.voice_hint)
        form.addRow(self.dalia_label)
        form.addRow(self.voices_status)
        return page

    def _build_reading(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(*EDGE_SPEED_RANGE)  # el rango de Edge cabe en el de Piper
        self.speed_spin.setSingleStep(0.05)
        self.speed_spin.setDecimals(2)
        self.speed_spin.setSuffix("×")
        form.addRow("Velocidad por defecto", self.speed_spin)
        self.spins: dict[str, QSpinBox] = {}
        self._add_spins(form, _CHUNK_SPINS)
        self.continuous_check = QCheckBox("Lectura continua: al terminar un capítulo, sigue "
                                          "con el siguiente")
        self.highlight_check = QCheckBox("Resaltar la frase que se lee")
        form.addRow(self.continuous_check)
        form.addRow(self.highlight_check)
        self.restart_group = QGroupBox(RESTART_GROUP)
        self._add_spins(QFormLayout(self.restart_group), _RESTART_SPINS)
        form.addRow(self.restart_group)
        note = QLabel(LATER_NOTE)
        note.setWordWrap(True)
        form.addRow(note)
        return page

    def _add_spins(self, form: QFormLayout, specs) -> None:
        for key, label, low, high, step, suffix in specs:
            spin = QSpinBox()
            spin.setRange(low, high)
            spin.setSingleStep(step)
            spin.setSuffix(suffix)
            self.spins[key] = spin
            form.addRow(label, spin)

    def _build_privacy(self) -> QWidget:
        page = QWidget()
        self.privacy_label = QLabel(PRIVACY_TEXT + ".")
        self.privacy_label.setWordWrap(True)
        self.store_text_check = QCheckBox("Guardar el texto de los capítulos")
        self.store_text_check.setToolTip(
            "Si no se guarda, solo se recuerdan el título, el capítulo y por dónde vas."
        )
        self.purge_frame = QFrame()
        self.purge_frame.setFrameShape(QFrame.Shape.StyledPanel)
        question = QLabel(PURGE_QUESTION)
        question.setWordWrap(True)
        self.purge_button = QPushButton("Borrar el texto guardado")
        self.keep_button = QPushButton("Conservarlo")
        purge_row = QHBoxLayout()
        purge_row.addStretch(1)
        purge_row.addWidget(self.purge_button)
        purge_row.addWidget(self.keep_button)
        purge_layout = QVBoxLayout(self.purge_frame)
        purge_layout.addWidget(question)
        purge_layout.addLayout(purge_row)
        self.purge_frame.hide()
        self.privacy_status = QLabel()
        self.privacy_status.setWordWrap(True)
        layout = QVBoxLayout(page)
        layout.addWidget(self.privacy_label)
        layout.addWidget(self.store_text_check)
        layout.addWidget(self.purge_frame)
        layout.addWidget(self.privacy_status)
        layout.addStretch(1)
        return page

    def _connect(self) -> None:
        # Solo cuentan las acciones del usuario: `activated` y `clicked`; las casillas
        # numéricas se rellenan con las señales bloqueadas.
        self.language_combo.activated.connect(self._on_language)
        self.edge_combo.activated.connect(self._on_edge_voice)
        self.refresh_button.clicked.connect(self._on_refresh)
        self.speed_spin.valueChanged.connect(lambda v: self._change("speed", round(v, 2)))
        for key, spin in self.spins.items():
            spin.valueChanged.connect(lambda v, k=key: self._change(k, int(v)))
        self.spins["recovery_initial_s"].valueChanged.connect(
            lambda v: self.spins["recovery_max_s"].setMinimum(v))
        self.continuous_check.clicked.connect(lambda on: self._change("continuous", on))
        self.highlight_check.clicked.connect(lambda on: self._change("highlight_sentence", on))
        self.store_text_check.clicked.connect(self._on_store_text)
        self.purge_button.clicked.connect(self._on_purge)
        self.keep_button.clicked.connect(self.purge_frame.hide)

    # --- carga -------------------------------------------------------------------------

    def _load(self) -> None:
        job = asyncio.to_thread(_read, self._s.settings, self._s.voices)
        if run_job(self._s.worker, job, self._apply, self._on_load_failed) is None:
            self.loaded = True

    def _apply(self, result: tuple[dict, list[dict]]) -> None:
        values, cached = result
        self._values = values
        self._edge_voices = cached
        self._voices_known = bool(cached)
        self.speed_spin.blockSignals(True)
        self.speed_spin.setValue(float(values["speed"]))
        self.speed_spin.blockSignals(False)
        for key, spin in self.spins.items():
            spin.blockSignals(True)
            spin.setValue(int(values[key]))
            spin.blockSignals(False)
        maximum = self.spins["recovery_max_s"]
        maximum.blockSignals(True)
        maximum.setMinimum(self.spins["recovery_initial_s"].value())
        maximum.setValue(int(values["recovery_max_s"]))
        maximum.blockSignals(False)
        self.continuous_check.setChecked(bool(values["continuous"]))
        highlight = values["highlight_sentence"]
        self.highlight_check.setChecked(True if highlight is None else bool(highlight))
        self.store_text_check.setChecked(bool(values["store_text"]))
        self._fill_voices()
        self.loaded = True

    def _on_load_failed(self, exc: BaseException) -> None:
        self.loaded = True
        message = error_text(exc)
        if message:
            self.status_label.setText(message)

    def _language(self) -> str:
        return self._values.get("language") or DEFAULT_LANGUAGE

    def _fill_voices(self) -> None:
        language = self._language()
        self.language_combo.clear()
        for locale in _locales(self._edge_voices, language):
            self.language_combo.addItem(locale, locale)
        self.language_combo.setCurrentIndex(self.language_combo.findData(language))
        self._fill_edge_combo()
        names = {v.get("ShortName") for v in self._edge_voices}
        self.dalia_label.setVisible(self._voices_known and DALIA not in names)
        if not self._voices_known:
            self.voices_status.setText(NEEDS_REFRESH)

    def _fill_edge_combo(self) -> None:
        current = self._values.get("edge_voice")
        self.edge_combo.clear()
        items = edge_voice_items(self._edge_voices, self._language(), current, exact_locale=True)
        for text, data in items:
            self.edge_combo.addItem(text, data)
        index = self.edge_combo.findData(current)
        self.edge_combo.setCurrentIndex(index)
        self.voice_hint.setVisible(index < 0 and bool(current))

    # --- cambios -----------------------------------------------------------------------

    def _change(self, key: str, value: Any) -> None:
        if not self.loaded:
            return
        self._values[key] = value
        self._pending[key] = value
        self._save_timer.start()

    def _on_language(self, index: int) -> None:
        self._change("language", self.language_combo.itemData(index))
        self._fill_edge_combo()  # marca la voz actual si es de este idioma; si no, lo avisa

    def _on_edge_voice(self, index: int) -> None:
        self._change("edge_voice", self.edge_combo.itemData(index))
        self.voice_hint.hide()

    def flush(self) -> None:
        """Guarda ya los cambios pendientes (también lo hace solo, poco después de cada uno)."""
        self._save_timer.stop()
        if not self._pending:
            return
        changes, self._pending = self._pending, {}
        settings, ctl = self._s.settings, self._s.controller

        limits = None
        if any(key in changes for key in _CHUNK_KEYS):
            limits = tuple(self.spins[key].value() for key in _CHUNK_KEYS)

        async def save() -> None:
            for key, value in changes.items():
                await asyncio.to_thread(settings.set, key, value)
            # Lo que también cambia la sesión en marcha va por el controlador.
            if "store_text" in changes:
                await ctl.set_store_text(changes["store_text"])
            if "edge_voice" in changes:
                await ctl.set_voice(changes["edge_voice"], None, False)
            if "speed" in changes:
                await ctl.set_speed(changes["speed"], False)
            if "continuous" in changes:
                await ctl.set_continuous(changes["continuous"])
            if limits is not None:
                await ctl.set_chunk_limits(*limits)

        def done(_result) -> None:
            self.status_label.setText(SAVED)
            self.saved.emit()

        run_job(self._s.worker, save(), done, self._on_error)

    def _on_error(self, exc: BaseException) -> None:
        message = error_text(exc)
        if message:
            self.status_label.setText(message)

    # --- voces de Edge -----------------------------------------------------------------

    def _on_refresh(self) -> None:
        edge, voices = self._s.edge, self._s.voices
        if edge is None:
            return

        async def refresh() -> list[dict]:
            found = await edge.list_voices()
            if found:
                edge.set_known_voices(found)
                await asyncio.to_thread(voices.save, found)
            return found

        self.refresh_button.setEnabled(False)
        self.voices_status.setText("Buscando voces…")
        if run_job(self._s.worker, refresh(), self._on_voices, self._on_voices_failed) is None:
            self.refresh_button.setEnabled(True)

    def _on_voices(self, found: list[dict]) -> None:
        self.refresh_button.setEnabled(True)
        if not found:
            self.voices_status.setText(NO_VOICES)
            return
        self._edge_voices = found
        self._voices_known = True
        self._fill_voices()
        self.voices_status.setText(f"Lista actualizada: {len(found)} voces.")
        self.saved.emit()  # la ventana muestra la lista nueva

    def _on_voices_failed(self, exc: BaseException) -> None:
        self.refresh_button.setEnabled(True)
        self.voices_status.setText(error_text(exc) or "")

    # --- privacidad --------------------------------------------------------------------

    def _on_store_text(self, on: bool) -> None:
        self._change("store_text", on)
        self.privacy_status.clear()
        self.purge_frame.setVisible(not on)  # aviso no modal: decide aquí mismo

    def _on_purge(self) -> None:
        self.purge_button.setEnabled(False)
        ctl, chapters = self._s.controller, self._s.chapters

        async def purge() -> None:
            # Primero la sesión en marcha deja de guardar texto (y termina lo que guardaba);
            # si no, el siguiente capítulo volvería a escribirlo justo después del borrado.
            await ctl.set_store_text(False)
            await asyncio.to_thread(chapters.purge_all_text)

        if run_job(self._s.worker, purge(), self._on_purged, self._on_purge_failed) is None:
            self.purge_button.setEnabled(True)

    def _on_purged(self, _result) -> None:
        self.purge_button.setEnabled(True)
        self.purge_frame.hide()
        self.privacy_status.setText(PURGED)

    def _on_purge_failed(self, exc: BaseException) -> None:
        self.purge_button.setEnabled(True)
        self.privacy_status.setText(error_text(exc) or "")

    # --- cierre ------------------------------------------------------------------------

    def done(self, result: int) -> None:
        # Las descargas siguen (el aviso y el indicador de Piper muestran el progreso):
        # solo «Cancelar» las para. La muestra sí se corta.
        self.flush()
        self.piper_panel.stop_sample()
        super().done(result)
