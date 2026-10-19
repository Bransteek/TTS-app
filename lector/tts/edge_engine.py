"""Motor Edge TTS: síntesis en memoria con marcas de frase alineadas."""
from __future__ import annotations

import asyncio
import logging
import re

import edge_tts
import edge_tts.exceptions

from ..audio.pcm import OUTPUT_RATE, decode_mp3
from ..errors import EdgeNoAudio, EdgeUnavailable, EdgeVoiceNotFound
from ..models import Chunk, Sentence
from .base import AudioClip, edge_rate

log = logging.getLogger(__name__)

_TICKS_PER_SECOND = 10_000_000
_NON_ALNUM = re.compile(r"[\W_]+")


def _normalized_len(text: str) -> int:
    return len(_NON_ALNUM.sub("", text).lower())


def align_marks(
    sentences: tuple[Sentence, ...], boundaries: list[tuple[int, str]]
) -> list[tuple[int, int]]:
    """Asigna a cada frase el offset del boundary de Edge que la contiene.

    Edge puede unir o partir frases de otra manera; se alinea por la longitud
    normalizada (solo alfanuméricos) acumulada antes de cada frase y boundary.
    """
    first = sentences[0]
    if not boundaries:
        return [(0, first.index)]

    boundary_starts: list[int] = []
    acc = 0
    for _, text in boundaries:
        boundary_starts.append(acc)
        acc += _normalized_len(text)

    marks: list[tuple[int, int]] = []
    sentence_start = 0
    j = 0
    for k, sentence in enumerate(sentences):
        while j + 1 < len(boundaries) and boundary_starts[j + 1] <= sentence_start:
            j += 1
        offset = 0 if k == 0 else boundaries[j][0]
        # Sin avance no hay nada que confirmar: se conserva solo la primera frase.
        if not marks or offset > marks[-1][0]:
            marks.append((offset, sentence.index))
        sentence_start += _normalized_len(sentence.text)
    return marks


class EdgeEngine:
    name = "edge"

    def __init__(
        self,
        communicate_factory=edge_tts.Communicate,
        list_voices_fn=edge_tts.list_voices,
        timeout_s: float = 15.0,
    ):
        self._factory = communicate_factory
        self._list_voices_fn = list_voices_fn
        self._timeout_s = timeout_s
        self._known_voices: list[dict] = []

    def set_known_voices(self, voices: list[dict]) -> None:
        """Carga la lista de voces conocida (p. ej. desde la caché SQLite)."""
        self._known_voices = list(voices)

    async def list_voices(self) -> list[dict]:
        try:
            async with asyncio.timeout(self._timeout_s):
                voices = await self._list_voices_fn()
        except Exception as exc:
            raise EdgeUnavailable() from exc
        self._known_voices = list(voices)
        return voices

    async def health_check(self, voice: str) -> bool:
        try:
            await self._fetch(voice, "Hola.", 1.0)
        except Exception:
            log.info("Edge TTS no responde", exc_info=True)
            return False
        return True

    async def synthesize(self, chunk: Chunk, voice: str, speed: float) -> AudioClip:
        self._check_voice(voice)
        audio, boundaries = await self._fetch(voice, chunk.text, speed)
        pcm = await asyncio.to_thread(decode_mp3, audio)
        marks = [
            m
            for m in align_marks(chunk.sentences, boundaries)
            if m[0] < len(pcm)
        ]
        return AudioClip(pcm=pcm, marks=marks)

    def _check_voice(self, voice: str) -> None:
        if self._known_voices and not any(
            v.get("ShortName") == voice for v in self._known_voices
        ):
            raise EdgeVoiceNotFound()

    async def _fetch(
        self, voice: str, text: str, speed: float
    ) -> tuple[bytes, list[tuple[int, str]]]:
        parts: list[bytes] = []
        boundaries: list[tuple[int, str]] = []
        try:
            communicate = self._factory(
                text,
                voice,
                rate=edge_rate(speed),
                boundary="SentenceBoundary",
                connect_timeout=10,
                receive_timeout=int(self._timeout_s),
            )
        except ValueError as exc:
            # Voz o velocidad mal formadas: error de configuración, no se reintenta.
            raise EdgeVoiceNotFound() from exc
        except Exception as exc:
            raise EdgeUnavailable() from exc
        try:
            async with asyncio.timeout(self._timeout_s):
                async for item in communicate.stream():
                    kind = item.get("type")
                    if kind == "audio":
                        parts.append(item["data"])
                    elif kind == "SentenceBoundary":
                        offset = int(item["offset"] / _TICKS_PER_SECOND * OUTPUT_RATE)
                        boundaries.append((offset, item["text"]))
        except edge_tts.exceptions.NoAudioReceived as exc:
            # Edge respondió, pero no hay nada que decir en este texto: problema del
            # fragmento, no del servicio (no debe abrir el circuito).
            raise EdgeNoAudio() from exc
        except Exception as exc:  # nada crudo debe escapar de synthesize()
            raise EdgeUnavailable() from exc
        audio = b"".join(parts)
        if not audio:
            raise EdgeUnavailable()
        return audio, boundaries
