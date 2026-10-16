"""Motor Piper local (respaldo sin conexión) y gestor de modelos de voz."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import threading
from collections.abc import Callable
from pathlib import Path

import httpx
import numpy as np

from ..audio.pcm import OUTPUT_RATE, resample
from ..errors import PageUnreachable, PiperModelMissing, SynthesisFailed
from ..models import Chunk
from .base import AudioClip, clamp_speed

log = logging.getLogger(__name__)

# Mismo patrón que piper.download_voices.URL_FORMAT; se duplica para no importar
# piper (y onnxruntime) al arrancar.
_VOICE_HOST = "huggingface.co"
_URL = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
    "{family}/{family}_{region}/{voice}/{quality}/{name}{ext}?download=true"
)
_TIMEOUT = httpx.Timeout(30.0, connect=15.0)
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

    def __init__(self, directory: Path, transport: httpx.AsyncBaseTransport | None = None):
        self.directory = Path(directory)
        self._transport = transport

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

    def _urls(self, name: str) -> list[tuple[str, str]]:
        m = re.match(r"^([A-Za-z]+)_([A-Za-z]+)-([A-Za-z0-9_]+)-([A-Za-z_]+)$", name)
        if not m:
            raise ValueError(f"Nombre de voz no válido: {name!r}")
        family, region, voice, quality = m.groups()
        out = []
        for ext in (".onnx", ".onnx.json"):
            url = _URL.format(family=family, region=region, voice=voice,
                              quality=quality, name=name, ext=ext)
            if httpx.URL(url).host != _VOICE_HOST:
                raise ValueError("Host de descarga no permitido")
            out.append((url, f"{name}{ext}"))
        return out

    async def download(
        self, name: str, progress: Callable[[float], None] | None = None
    ) -> None:
        """Descarga el modelo en un directorio temporal y lo publica al final.

        El `.json` se mueve el último: si existe, el modelo está completo.
        """
        urls = self._urls(name)
        self.directory.mkdir(parents=True, exist_ok=True)
        part = self.directory / f".part-{name}"
        shutil.rmtree(part, ignore_errors=True)
        part.mkdir()
        try:
            await self._fetch_all(urls, part, progress)
            onnx, cfg = part / urls[0][1], part / urls[1][1]
            if onnx.stat().st_size == 0 or cfg.stat().st_size == 0:
                raise PageUnreachable()
            try:
                json.loads(cfg.read_text(encoding="utf-8"))
            except ValueError as exc:
                raise PageUnreachable() from exc
            final_onnx, final_cfg = self._paths(name)
            # Una descarga anterior a medias no debe quedar como instalada.
            final_cfg.unlink(missing_ok=True)
            os.replace(onnx, final_onnx)
            os.replace(cfg, final_cfg)
        finally:
            shutil.rmtree(part, ignore_errors=True)
        if progress:
            progress(1.0)

    async def _fetch_all(self, urls, part: Path, progress) -> None:
        done = 0
        total = 0
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT, follow_redirects=True, transport=self._transport
            ) as client:
                for url, filename in urls:
                    async with client.stream("GET", url) as resp:
                        resp.raise_for_status()
                        length = int(resp.headers.get("content-length") or 0)
                        total += length
                        got = 0
                        with open(part / filename, "wb") as fh:
                            async for data in resp.aiter_bytes(65536):
                                fh.write(data)
                                got += len(data)
                                done += len(data)
                                if progress and total:
                                    progress(min(done / total, 0.99))
                        if length and got != length:
                            raise PageUnreachable()
        except (httpx.HTTPError, OSError) as exc:
            raise PageUnreachable() from exc


def split_voice(voice: str) -> tuple[str, str | None]:
    name, _, speaker = voice.partition("#")
    return name, (speaker or None)


class PiperEngine:
    name = "piper"

    def __init__(self, manager: PiperModelManager, loader=None):
        self._manager = manager
        self._loader = loader
        self._loaded_name: str | None = None
        self._loaded = None
        self._lock = threading.Lock()

    def _load(self, name: str):
        if not self._manager.is_installed(name):
            raise PiperModelMissing()
        if self._loaded_name != name:
            loader = self._loader
            if loader is None:
                try:
                    from piper import PiperVoice  # carga perezosa: onnxruntime puede fallar
                except Exception as exc:
                    raise PiperModelMissing(
                        "No se pudo cargar el motor de voz sin conexión."
                    ) from exc
                loader = PiperVoice.load
            try:
                self._loaded = loader(self._manager.model_path(name))
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

    @staticmethod
    def _synthesis_config():
        try:
            from piper import SynthesisConfig
        except Exception as exc:
            raise PiperModelMissing(
                "No se pudo cargar el motor de voz sin conexión."
            ) from exc
        return SynthesisConfig

    def _synthesize(
        self, chunk: Chunk, name: str, speaker: str | None, speed: float
    ) -> AudioClip:
        with self._lock:
            model = self._load(name)
            speaker_id = None
            if speaker is not None:
                id_map = getattr(model.config, "speaker_id_map", None) or {}
                if id_map:
                    speaker_id = id_map.get(speaker)
                    if speaker_id is None:
                        log.warning("Hablante Piper desconocido %r; se usa el predeterminado", speaker)
            config = self._synthesis_config()(length_scale=1 / speed, speaker_id=speaker_id)
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
