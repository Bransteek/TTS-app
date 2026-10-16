"""PCM en memoria: decodificar MP3 y remuestrear a la frecuencia de salida."""
from __future__ import annotations

import miniaudio
import numpy as np

from ..errors import SynthesisFailed

OUTPUT_RATE = 24000


def decode_mp3(data: bytes) -> np.ndarray:
    """Decodifica un MP3 completo a PCM int16 mono a `OUTPUT_RATE`, sin tocar disco."""
    try:
        decoded = miniaudio.decode(
            data,
            output_format=miniaudio.SampleFormat.SIGNED16,
            nchannels=1,
            sample_rate=OUTPUT_RATE,
        )
    except miniaudio.MiniaudioError as exc:
        raise SynthesisFailed() from exc
    pcm = np.frombuffer(decoded.samples, dtype=np.int16)
    if pcm.size == 0:
        raise SynthesisFailed()
    return pcm


def resample(pcm: np.ndarray, src_rate: int, dst_rate: int = OUTPUT_RATE) -> np.ndarray:
    """Remuestrea PCM mono por interpolación lineal y devuelve int16."""
    if src_rate == dst_rate or pcm.size == 0:
        return pcm.astype(np.int16, copy=False)
    n_out = round(pcm.size * dst_rate / src_rate)
    positions = np.arange(n_out) * (src_rate / dst_rate)
    out = np.interp(positions, np.arange(pcm.size), pcm)
    return np.clip(np.rint(out), -32768, 32767).astype(np.int16)
