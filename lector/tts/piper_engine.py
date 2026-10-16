"""Motor Piper local (respaldo sin conexión) y gestor de modelos de voz."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from collections.abc import Callable
from pathlib import Path

import numpy as np
from piper import PiperVoice, SynthesisConfig
from piper.download_voices import download_voice

from ..audio.pcm import OUTPUT_RATE, resample
from ..errors import PiperModelMissing, SynthesisFailed
from ..models import Chunk
from .base import AudioClip, clamp_speed

log = logging.getLogger(__name__)

_VOICE_NAME = re.compile(r"^[A-Za-z]+_[A-Za-z]+-[A-Za-z0-9_]+-[A-Za-z_]+$")


class PiperModelManager:
    CATALOG: list[dict] = [
        {"name": "es_ES-sharvard-medium", "label": "Sharvard (España)", "gender": "F/M",
         "size_mb": 76, "license_note": "Dos hablantes: F y M (voz «es_ES-sharvard-medium#F»)."},
        {"name": "es_AR-daniela-high", "label": "Daniela (Argentina)", "gender": "F",
         "size_mb": 114, "license_note": "Calidad alta; revisar la licencia del conjunto de datos."},
        {"name": "es_MX-claude-high", "label": "Claude (México)", "gender": "?",
         "size_mb": 63, "license_note": "Género sin verificar; calidad alta."},
        {"name": "es_MX-ald-medium", "label": "Ald (México)", "gender": "M",
         "size_mb": 63, "license_note": "Revisar la licencia del conjunto de datos."},
        {"name": "es_ES-davefx-medium", "label": "DaveFX (España)", "gender": "M",
         "size_mb": 63, "license_note": "Revisar la licencia del conjunto de datos."},
    ]

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def _paths(self, name: str) -> tuple[Path, Path]:
        return (
            self.directory / f"{name}.onnx",
            self.directory / f"{name}.onnx.json",
        )

    def model_path(self, name: str) -> Path:
        return self._paths(name)[0]

    def installed(self) -> list[str]:
        if not self.directory.is_dir():
            return []
        return sorted(
            p.name.removesuffix(".onnx")
            for p in self.directory.glob("*.onnx")
            if (self.directory / f"{p.name}.json").is_file()
        )

    def is_installed(self, name: str) -> bool:
        if not _VOICE_NAME.match(name):
            return False
        onnx, cfg = self._paths(name)
        return onnx.is_file() and cfg.is_file()

    def speakers(self, name: str) -> list[str]:
        if not self.is_installed(name):
            return []
        try:
            data = json.loads(self._paths(name)[1].read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        mapping = data.get("speaker_id_map") if isinstance(data, dict) else None
        return list(mapping) if isinstance(mapping, dict) else []

    def speaker_id(self, name: str, speaker: str) -> int | None:
        data = json.loads(self._paths(name)[1].read_text(encoding="utf-8"))
        return (data.get("speaker_id_map") or {}).get(speaker)

    async def download(
        self, name: str, progress: Callable[[float], None] | None = None
    ) -> None:
        if not _VOICE_NAME.match(name):
            raise ValueError(f"Nombre de voz no válido: {name!r}")
        self.directory.mkdir(parents=True, exist_ok=True)
        if progress:
            progress(0.0)
        # download_voice no informa del avance: solo se notifica inicio y fin.
        await asyncio.to_thread(download_voice, name, self.directory)
        if progress:
            progress(1.0)


def split_voice(voice: str) -> tuple[str, str | None]:
    name, _, speaker = voice.partition("#")
    return name, (speaker or None)


class PiperEngine:
    name = "piper"

    def __init__(self, manager: PiperModelManager, loader=PiperVoice.load):
        self._manager = manager
        self._loader = loader
        self._loaded_name: str | None = None
        self._loaded = None
        self._lock = threading.Lock()

    def _load(self, name: str):
        if not self._manager.is_installed(name):
            raise PiperModelMissing()
        if self._loaded_name != name:
            try:
                self._loaded = self._loader(self._manager.model_path(name))
            except Exception as exc:
                self._loaded, self._loaded_name = None, None
                raise SynthesisFailed(recoverable=False) from exc
            self._loaded_name = name
        return self._loaded

    async def list_voices(self) -> list[dict]:
        out = []
        for name in self._manager.installed():
            speakers = self._manager.speakers(name) or [None]
            for sp in speakers:
                out.append({"ShortName": name if sp is None else f"{name}#{sp}",
                            "Model": name, "Speaker": sp})
        return out

    async def health_check(self, voice: str) -> bool:
        name, _ = split_voice(voice)
        try:
            await asyncio.to_thread(self._load_locked, name)
        except Exception:
            log.info("Piper no está disponible", exc_info=True)
            return False
        return True

    def _load_locked(self, name: str):
        with self._lock:
            return self._load(name)

    async def synthesize(self, chunk: Chunk, voice: str, speed: float) -> AudioClip:
        name, speaker = split_voice(voice)
        speed, _ = clamp_speed("piper", speed)
        try:
            return await asyncio.to_thread(self._synthesize, chunk, name, speaker, speed)
        except (PiperModelMissing, SynthesisFailed):
            raise
        except Exception as exc:  # nada crudo debe escapar de synthesize()
            raise SynthesisFailed() from exc

    def _synthesize(
        self, chunk: Chunk, name: str, speaker: str | None, speed: float
    ) -> AudioClip:
        with self._lock:
            model = self._load(name)
            speaker_id = None
            if speaker is not None:
                id_map = getattr(model.config, "speaker_id_map", None) or {}
                if id_map:
                    if speaker not in id_map:
                        raise SynthesisFailed(recoverable=False)
                    speaker_id = id_map[speaker]
            config = SynthesisConfig(length_scale=1 / speed, speaker_id=speaker_id)
            parts: list[np.ndarray] = []
            marks: list[tuple[int, int]] = []
            total = 0
            for sentence in chunk.sentences:
                samples = [
                    resample(c.audio_int16_array, c.sample_rate, OUTPUT_RATE)
                    for c in model.synthesize(sentence.text, config)
                ]
                pcm = np.concatenate(samples) if samples else np.zeros(0, np.int16)
                if pcm.size == 0:
                    continue  # sin muestras no hay marca (evita offsets duplicados)
                marks.append((total, sentence.index))
                parts.append(pcm)
                total += pcm.size
        if not parts:
            raise SynthesisFailed()
        return AudioClip(pcm=np.concatenate(parts), marks=marks)
