"""Panel de voz y velocidad y indicadores de la barra de estado de la ventana principal.

`VoiceStatusMixin` es parte de `MainWindow`: construye los indicadores (motor, Edge, Piper,
«Esperando conexión…»), los mantiene al día con los avisos del controlador y conecta el
panel `VoicePanel` con el controlador.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel

from lector.tts.engine_manager import EDGE_LABEL, PIPER_LABEL, EngineState
from lector.ui.playback_controls import edge_voice_items, piper_voice_items

log = logging.getLogger(__name__)

WAITING_TEXT = "Esperando conexión…"
EDGE_TEXT = {
    EngineState.EDGE_ACTIVE: "Conectado",
    EngineState.EDGE_RETRYING: "Reintentando…",
    EngineState.EDGE_RECOVERY_TEST: "Reintentando…",
    EngineState.PIPER_ACTIVE: "Sin conexión",
    EngineState.WAITING_NETWORK: "Sin conexión",
}


class VoiceStatusMixin:
    def _init_voice_status(self) -> None:
        self._preferred = "edge"
        self._engine_seen = False
        self._piper_short: dict[str, str] = {}

    def _build_status_bar(self) -> None:
        self.engine_label = QLabel(f"Motor activo: {EDGE_LABEL}")
        self.edge_label = QLabel(f"Edge: {EDGE_TEXT[EngineState.EDGE_ACTIVE]}")
        self.piper_label = QLabel()
        self.piper_label.setTextFormat(Qt.TextFormat.RichText)
        self.waiting_label = QLabel(WAITING_TEXT)
        self.waiting_label.hide()
        self._set_piper_label(None)
        bar = self.statusBar()
        for label in (self.waiting_label, self.engine_label, self.edge_label, self.piper_label):
            bar.addPermanentWidget(label)

    def _connect_voice(self) -> None:
        self.piper_label.linkActivated.connect(lambda _link: self.settings_requested.emit())
        ctl, v = self._ctl, self.voice
        v.preferred_changed.connect(self._on_preferred_changed)
        v.edge_voice_changed.connect(
            lambda voice: self._run(ctl.set_voice(voice, None, v.apply_now()))
        )
        v.piper_voice_changed.connect(self._on_piper_voice_changed)
        v.speed_changed.connect(lambda speed: self._run(ctl.set_speed(speed, v.apply_now())))

    def _apply_voice_settings(self, loaded) -> None:
        """Pone en el panel de voz y en la barra de estado lo leído de la configuración."""
        values = loaded.values
        self._preferred = values["preferred_engine"] or "edge"
        self.voice.set_preferred(self._preferred)
        if not self._engine_seen:
            self.engine_label.setText(
                f"Motor activo: {PIPER_LABEL if self._preferred == 'piper' else EDGE_LABEL}"
            )
        edge_voice = values["edge_voice"]
        self.voice.set_edge_voices(
            edge_voice_items(loaded.edge_voices, values["language"], edge_voice), edge_voice
        )
        piper = piper_voice_items(loaded.piper)
        self._piper_short = {data: short for _, data, short in piper}
        speaker = values["piper_speaker"]
        current = f"{values['piper_voice']}#{speaker}" if speaker else values["piper_voice"]
        self.voice.set_piper_voices([(text, data) for text, data, _ in piper], current)
        self._set_piper_label(current)
        self.voice.set_speed(float(values["speed"]))

    def _set_piper_label(self, voice: str | None) -> None:
        short = self._piper_short.get(voice) if voice else None
        if short:
            self.piper_label.setText(f"Piper: Listo ({short})")
        else:
            self.piper_label.setText('Piper: <a href="#configuracion">Sin voz instalada</a>')

    def _on_preferred_changed(self, name: str) -> None:
        self._preferred = name
        self._run(self._ctl.set_preferred_engine(name))

    def _on_piper_voice_changed(self, voice: str) -> None:
        self._set_piper_label(voice)
        self._run(self._ctl.set_voice(None, voice, self.voice.apply_now()))

    def _handle_engine_state(self, value: str, active: str) -> None:
        self._engine_seen = True
        self.engine_label.setText(f"Motor activo: {active}")
        try:
            state = EngineState(value.lower())
        except ValueError:
            log.warning("Estado de motor desconocido: %r", value)
            return
        if state is EngineState.PIPER_ACTIVE and self._preferred == "piper":
            return  # Piper por elección: no dice nada de la conexión con Edge
        self.edge_label.setText(f"Edge: {EDGE_TEXT[state]}")
