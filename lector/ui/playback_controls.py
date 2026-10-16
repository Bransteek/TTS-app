"""Botones de reproducción y panel de voz (motor, voces y velocidad)."""
from __future__ import annotations

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from lector.tts.piper_engine import PiperModelManager

SPEED_PRESETS = (0.75, 1.0, 1.25, 1.5, 1.75, 2.0)
SPEED_MIN, SPEED_MAX, SPEED_STEP = 0.5, 2.5, 0.05
SPEED_DEBOUNCE_MS = 400  # al escribir en la casilla, se aplica tras esta pausa
ENGINES = (("Edge (en línea)", "edge"), ("Piper (sin conexión)", "piper"))


def _button(text: str, tip: str | None = None) -> QPushButton:
    button = QPushButton(text)
    if tip:
        button.setToolTip(tip)
        button.setAccessibleName(tip)
    return button


class PlaybackControls(QWidget):
    """Solo presenta los botones; la ventana los conecta con el controlador."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.play_pause_button = _button("Leer")
        self.stop_button = _button("Detener")
        self.prev_chapter_button = _button("⏮", "Capítulo anterior")
        self.prev_sentence_button = _button("◀◀", "Frase anterior")
        self.skip_back_button = _button("⟲")
        self.skip_forward_button = _button("⟳")
        self.next_sentence_button = _button("▶▶", "Frase siguiente")
        self.next_chapter_button = _button("⏭", "Capítulo siguiente")
        self.restart_button = _button("Reiniciar capítulo")
        self.continuous_check = QCheckBox("Lectura continua")
        self.continuous_check.setChecked(True)
        self.continuous_check.setToolTip("Al terminar un capítulo, sigue con el siguiente.")
        self.skip_seconds = 10
        self.set_skip_seconds(self.skip_seconds)

        row = QHBoxLayout()
        for button in (self.play_pause_button, self.stop_button, self.prev_chapter_button,
                       self.prev_sentence_button, self.skip_back_button,
                       self.skip_forward_button, self.next_sentence_button,
                       self.next_chapter_button):
            row.addWidget(button)
        second = QHBoxLayout()
        second.addWidget(self.restart_button)
        second.addStretch(1)
        second.addWidget(self.continuous_check)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(row)
        layout.addLayout(second)

    def set_playing(self, playing: bool) -> None:
        """Alterna el botón: «Pausar» mientras se lee, «Leer» en cualquier otro caso."""
        self.play_pause_button.setText("Pausar" if playing else "Leer")

    def set_skip_seconds(self, seconds: int) -> None:
        self.skip_seconds = seconds
        self.skip_back_button.setText(f"⟲ −{seconds} s")
        self.skip_back_button.setToolTip(f"Retroceder {seconds} segundos")
        self.skip_forward_button.setText(f"⟳ +{seconds} s")
        self.skip_forward_button.setToolTip(f"Avanzar {seconds} segundos")

    def set_continuous(self, on: bool) -> None:
        self.continuous_check.setChecked(on)


class VoicePanel(QGroupBox):
    """Motor preferido, voces de Edge y Piper y velocidad.

    Las señales solo salen por acciones del usuario, nunca al cargar valores.
    """

    preferred_changed = Signal(str)
    edge_voice_changed = Signal(str)
    piper_voice_changed = Signal(str)
    speed_changed = Signal(float)

    def __init__(self, parent: QWidget | None = None):
        super().__init__("Voz", parent)
        self.engine_combo = QComboBox()
        for text, name in ENGINES:
            self.engine_combo.addItem(text, name)
        self.edge_combo = QComboBox()
        self.piper_combo = QComboBox()
        self.piper_combo.setPlaceholderText("Elige una voz")

        self.speed_buttons: dict[float, QPushButton] = {}
        speed_row = QHBoxLayout()
        for value in SPEED_PRESETS:
            button = _button(f"{value:g}×", f"Velocidad {value:g}")
            button.clicked.connect(lambda _checked=False, v=value: self._preset(v))
            self.speed_buttons[value] = button
            speed_row.addWidget(button)
        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(SPEED_MIN, SPEED_MAX)
        self.speed_spin.setSingleStep(SPEED_STEP)
        self.speed_spin.setDecimals(2)
        self.speed_spin.setValue(1.0)
        self.speed_spin.setSuffix("×")
        speed_row.addWidget(self.speed_spin)
        self.apply_now_check = QCheckBox("Aplicar ya")
        self.apply_now_check.setToolTip(
            "Cambia la voz o la velocidad en la frase actual, sin esperar a lo ya preparado."
        )
        speed_row.addWidget(self.apply_now_check)
        speed_row.addStretch(1)

        form = QFormLayout(self)
        form.addRow("Motor preferido", self.engine_combo)
        form.addRow("Voz en línea (Edge)", self.edge_combo)
        form.addRow("Voz sin conexión (Piper)", self.piper_combo)
        form.addRow("Velocidad", speed_row)

        self._speed_timer = QTimer(self)
        self._speed_timer.setSingleShot(True)
        self._speed_timer.setInterval(SPEED_DEBOUNCE_MS)
        self._speed_timer.timeout.connect(self._emit_speed)
        self.speed_spin.valueChanged.connect(lambda _v: self._speed_timer.start())

        # `activated` solo salta cuando elige el usuario.
        self.engine_combo.activated.connect(
            lambda i: self.preferred_changed.emit(self.engine_combo.itemData(i))
        )
        self.edge_combo.activated.connect(
            lambda i: self.edge_voice_changed.emit(self.edge_combo.itemData(i))
        )
        self.piper_combo.activated.connect(
            lambda i: self.piper_voice_changed.emit(self.piper_combo.itemData(i))
        )

    def apply_now(self) -> bool:
        return self.apply_now_check.isChecked()

    def set_preferred(self, name: str) -> None:
        index = self.engine_combo.findData(name)
        if index >= 0:
            self.engine_combo.setCurrentIndex(index)

    def set_edge_voices(self, items: list[tuple[str, str]], current: str | None) -> None:
        _fill(self.edge_combo, items, current)

    def set_piper_voices(self, items: list[tuple[str, str]], current: str | None) -> None:
        _fill(self.piper_combo, items, current)
        self.piper_combo.setEnabled(bool(items))

    def set_speed(self, value: float) -> None:
        """Muestra la velocidad sin avisar al controlador."""
        self.speed_spin.blockSignals(True)
        self.speed_spin.setValue(value)
        self.speed_spin.blockSignals(False)

    def _preset(self, value: float) -> None:
        self._speed_timer.stop()
        self.set_speed(value)
        self.speed_changed.emit(value)

    def _emit_speed(self) -> None:
        self.speed_changed.emit(round(self.speed_spin.value(), 2))


_GENDER = {"Female": "mujer", "Male": "hombre"}


def edge_voice_items(voices: list[dict], language: str, current: str) -> list[tuple[str, str]]:
    """(texto, ShortName) de las voces de Edge del idioma; la voz elegida siempre aparece."""
    family = (language or "es").split("-")[0].lower()
    items = []
    for v in sorted(voices, key=lambda v: (v.get("Locale", ""), v.get("ShortName", ""))):
        short, locale = v.get("ShortName", ""), v.get("Locale", "")
        if not short or locale.split("-")[0].lower() != family:
            continue
        name = short.rsplit("-", 1)[-1].removesuffix("Neural") or short
        gender = _GENDER.get(v.get("Gender", ""))
        items.append((f"{name} — {locale}" + (f", {gender}" if gender else ""), short))
    if current and all(data != current for _, data in items):
        items.insert(0, (current, current))
    return items


def piper_voice_items(installed: list[tuple[str, list[str]]]) -> list[tuple[str, str, str]]:
    """(texto del desplegable, voz «nombre#hablante», nombre corto para el indicador)."""
    labels = {m["name"]: m["label"] for m in PiperModelManager.CATALOG}
    out = []
    for name, speakers in installed:
        label = labels.get(name, name)
        short = label.split(" (")[0]
        if speakers:
            out += [(f"{label} – voz {s}", f"{name}#{s}", f"{short} {s}") for s in speakers]
        else:
            out.append((label, name, short))
    return out


def _fill(combo: QComboBox, items: list[tuple[str, str]], current: str | None) -> None:
    """Rellena un desplegable (texto, dato) y selecciona `current` si está."""
    combo.blockSignals(True)
    combo.clear()
    for text, data in items:
        combo.addItem(text, data)
    combo.setCurrentIndex(combo.findData(current) if current is not None else -1)
    combo.blockSignals(False)
