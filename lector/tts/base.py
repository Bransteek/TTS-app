"""Contrato común de los motores TTS y utilidades de velocidad."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np

from ..models import Chunk

# Provisionales hasta la verificación con la red real (ver informe de la tarea 10).
EDGE_SPEED_RANGE = (0.5, 2.0)
PIPER_SPEED_RANGE = (0.5, 2.5)


@dataclass(frozen=True, eq=False)
class AudioClip:
    """Audio sintetizado: PCM int16 mono a `OUTPUT_RATE` y sus marcas de frase."""

    pcm: np.ndarray
    marks: list[tuple[int, int]]  # (muestra_inicial, sentence_index)


class TtsEngine(Protocol):
    name: str  # "edge" | "piper"

    async def synthesize(self, chunk: Chunk, voice: str, speed: float) -> AudioClip: ...

    async def health_check(self, voice: str) -> bool: ...

    async def list_voices(self) -> list[dict]: ...


def edge_rate(speed: float) -> str:
    return f"{round((speed - 1) * 100):+d}%"


_RANGES = {"edge": EDGE_SPEED_RANGE, "piper": PIPER_SPEED_RANGE}


def clamp_speed(engine: str, speed: float) -> tuple[float, bool]:
    """Devuelve la velocidad dentro del rango del motor y si hubo que recortarla."""
    low, high = _RANGES[engine]
    clamped = min(max(speed, low), high)
    return clamped, clamped != speed
