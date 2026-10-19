"""Gestor de motores: Edge primero, Piper de respaldo y espera sin red.

Máquina de estados explícita con circuit breaker. La lectura nunca se detiene:
si Edge cae se usa Piper con el mismo fragmento, se sondea Edge para volver a
él y, si fallan los dos, se espera indefinidamente hasta que alguno responda.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from enum import StrEnum, auto
from typing import TYPE_CHECKING

import numpy as np

from ..content.segmenter import is_speakable
from ..errors import EDGE_VOICE_MISSING, EdgeNoAudio, EdgeVoiceNotFound, EdgeVoiceUnavailable
from ..models import Chunk, Sentence
from .base import AudioClip, TtsEngine

if TYPE_CHECKING:
    from ..persistence.repositories import ChunkLogRepo

log = logging.getLogger(__name__)

EDGE_LABEL = "Edge TTS"
PIPER_LABEL = "Piper local"


class EngineState(StrEnum):
    EDGE_ACTIVE = auto()
    EDGE_RETRYING = auto()
    PIPER_ACTIVE = auto()
    EDGE_RECOVERY_TEST = auto()
    WAITING_NETWORK = auto()


# Motor que se muestra como activo en cada estado. Durante la prueba de
# recuperación el audio sigue saliendo de Piper; en la espera sin red el
# siguiente intento es con Edge.
_ACTIVE_LABEL = {
    EngineState.EDGE_ACTIVE: EDGE_LABEL,
    EngineState.EDGE_RETRYING: EDGE_LABEL,
    EngineState.PIPER_ACTIVE: PIPER_LABEL,
    EngineState.EDGE_RECOVERY_TEST: PIPER_LABEL,
    EngineState.WAITING_NETWORK: EDGE_LABEL,
}


class _ContentFailure(Exception):
    """Edge no dio audio para el texto de este fragmento: no cuenta como caída de Edge."""


class _VoiceMissing(Exception):
    """La voz de Edge elegida no existe: error de configuración, no de red."""


# Edge responde NoAudioReceived tanto a «***» como a CUALQUIER texto si la voz no existe (o
# Microsoft la retiró). Los separadores ya no llegan a Edge, así que tantos fallos seguidos con
# texto que tiene letras significan que falta la voz.
NO_AUDIO_STREAK = 2
# Texto conocido con el que se comprueba la voz antes de saltar un fragmento sin voz local.
VOICE_CHECK_TEXT = "Hola."


@dataclass(frozen=True)
class EngineConfig:
    edge_retries: int = 3
    backoff: tuple[float, ...] = (1, 2, 4)
    recovery_initial_s: float = 60
    recovery_max_s: float = 600
    piper_cooldown_s: float = 120
    wait_max_s: float = 60
    preferred: str = "edge"

    def __post_init__(self) -> None:
        if self.preferred not in ("edge", "piper"):
            raise ValueError(f"Motor preferido no válido: {self.preferred!r}")
        if self.edge_retries < 0:
            raise ValueError("edge_retries no puede ser negativo")
        if not self.backoff:
            raise ValueError("backoff no puede estar vacío")
        if self.recovery_initial_s <= 0:
            raise ValueError("recovery_initial_s debe ser mayor que 0")
        if self.recovery_max_s < self.recovery_initial_s:
            raise ValueError("recovery_max_s debe ser >= recovery_initial_s")
        if self.piper_cooldown_s < 0:
            raise ValueError("piper_cooldown_s no puede ser negativo")
        if self.wait_max_s <= 0:
            raise ValueError("wait_max_s debe ser mayor que 0")


@dataclass
class _Circuit:
    """Estado mutable de una sesión.

    `reset()` lo sustituye por uno nuevo (una generación nueva): una síntesis
    en curso sigue modificando su objeto antiguo, que ya no afecta al gestor.
    """

    state: EngineState
    interval: float
    opened_at: float = -math.inf
    next_probe_at: float = math.inf
    failed_voice: str | None = None
    # Tras una prueba correcta y hasta que un fragmento real salga por Edge.
    half_open: bool = False


class EngineManager:
    def __init__(
        self,
        edge: TtsEngine,
        piper: TtsEngine,
        config: EngineConfig,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_state: Callable[[EngineState, str], None] = lambda s, e: None,
        chunk_log: ChunkLogRepo | None = None,
    ):
        self._edge = edge
        self._piper = piper
        self._config = config
        self._clock = clock
        self._sleep = sleep
        self._on_state = on_state
        self._chunk_log = chunk_log
        self._lock = asyncio.Lock()
        self._failing: set[str] = set()  # motores con una racha de fallos en curso
        self._on_notice: Callable[[str, str], None] = lambda level, message: None
        self._missing_voice: str | None = None  # voz de Edge inexistente ya avisada
        self._no_audio_streak = 0  # fragmentos con letras seguidos sin audio de Edge
        self._last_no_audio: str | None = None  # chunk_id del último (no cuenta dos veces)
        self._c = self._new_circuit()

    @property
    def state(self) -> EngineState:
        return self._c.state

    def set_on_state(self, on_state: Callable[[EngineState, str], None]) -> None:
        """Sustituye el aviso de cambio de estado (lo registra el controlador de lectura)."""
        self._on_state = on_state

    def set_on_notice(self, on_notice: Callable[[str, str], None]) -> None:
        """Aviso para el usuario (nivel, mensaje); lo registra el controlador de lectura."""
        self._on_notice = on_notice

    @property
    def preferred(self) -> str:
        return self._config.preferred

    def set_preferred(self, name: str) -> None:
        """Cambia el motor preferido («edge» o «piper»); vale desde el siguiente fragmento.

        Si cambia, el circuito vuelve al estado inicial de la nueva preferencia
        (como `reset()`): una síntesis en curso termina con su motor, pero sus
        transiciones ya no se aplican.
        """
        if name not in ("edge", "piper"):
            raise ValueError(f"Motor preferido no válido: {name!r}")
        if name == self._config.preferred:
            return
        self._config = replace(self._config, preferred=name)
        self.reset()

    def reset(self) -> None:
        """Vuelve al estado inicial (sesión nueva): circuito cerrado y motor preferido.

        Una síntesis en curso puede devolver su clip, pero sus transiciones ya
        no se aplican.
        """
        old, self._c = self._c, self._new_circuit()
        self._no_audio_streak = 0
        self._last_no_audio = None
        if old.state is not self._c.state:
            self._notify(self._c.state)

    async def synthesize(
        self, chunk: Chunk, edge_voice: str, piper_voice: str, speed: float
    ) -> tuple[AudioClip, str]:
        """Sintetiza `chunk` con el motor que toque; devuelve el clip y el nombre del motor.

        No devuelve hasta conseguir audio de algún motor. Sale con `CancelledError` si
        se cancela la tarea, o con `EdgeVoiceUnavailable` si la voz de Edge no existe y
        Piper tampoco puede leer (no tiene sentido esperar: hay que cambiar la voz o
        instalar una local).

        Si Edge responde sin audio (`EdgeNoAudio`), puede ser el texto o la voz: Microsoft
        responde igual a una voz inexistente. Un fallo aislado es del fragmento: Piper
        solo para él o, sin Piper, se comprueba la voz con un texto conocido y, si
        funciona, se salta el fragmento (clip vacío), sin abrir el circuito.
        `NO_AUDIO_STREAK` fallos seguidos con texto (o la comprobación sin audio) son una
        voz que falta: se trata como `EdgeVoiceNotFound` y nunca se salta nada.
        """
        async with self._lock:
            c = self._c

            def first():
                return self._synthesize(c, chunk, edge_voice, piper_voice, speed)

            def content():
                return self._content_fallback(c, chunk, edge_voice, piper_voice, speed)

            def voice_gone():
                return self._voice_gone(c, chunk, edge_voice, piper_voice, speed)

            step = first
            try:
                while True:
                    try:
                        return await step()
                    except _ContentFailure:
                        step = content
                    except _VoiceMissing:
                        step = voice_gone
            except asyncio.CancelledError:
                # Los reintentos no deben sobrevivir a la cancelación.
                if c.state is EngineState.EDGE_RETRYING:
                    self._set_state(c, EngineState.EDGE_ACTIVE)
                raise

    async def _synthesize(
        self, c: _Circuit, chunk: Chunk, edge_voice: str, piper_voice: str, speed: float
    ) -> tuple[AudioClip, str]:
        if c.failed_voice is not None and edge_voice != c.failed_voice:
            log.info("Cambió la voz de Edge; se vuelve a intentar con Edge")
            self._close_circuit(c)
            self._set_state(c, EngineState.EDGE_ACTIVE)

        if c.state is EngineState.WAITING_NETWORK:
            # Una espera anterior se canceló: se reintenta ya, sin esperar antes.
            return await self._wait_for_any(
                c, chunk, edge_voice, piper_voice, speed, try_first=True
            )

        if c.state is EngineState.PIPER_ACTIVE and self._probe_due(c):
            await self._probe_edge(c, edge_voice)

        if c.state is EngineState.EDGE_ACTIVE:
            result = await self._edge_with_retries(c, chunk, edge_voice, speed)
            if result is not None:
                return result

        clip = await self._attempt(self._piper, chunk, piper_voice, speed)
        if clip is not None:
            return clip, self._piper.name
        if c.failed_voice is not None:
            raise EdgeVoiceUnavailable()  # la voz de Edge no existe y Piper no puede
        if self._config.preferred == "piper":
            # Edge no se ha probado aún en esta llamada: es el respaldo inmediato.
            # El estado sigue en Piper, que es lo que eligió el usuario.
            clip = await self._attempt(self._edge, chunk, edge_voice, speed)
            if clip is not None:
                return clip, self._edge.name
        return await self._wait_for_any(
            c, chunk, edge_voice, piper_voice, speed, try_first=False
        )

    async def _edge_with_retries(
        self, c: _Circuit, chunk: Chunk, voice: str, speed: float
    ) -> tuple[AudioClip, str] | None:
        """Intenta Edge con reintentos; si no lo consigue, abre el circuito y devuelve None.

        En semiabierto (tras una prueba correcta) no hay reintentos: un fallo
        reabre el circuito con el intervalo duplicado.
        """
        cfg = self._config
        retries = 0 if c.half_open else cfg.edge_retries
        for attempt in range(retries + 1):
            if attempt:
                self._set_state(c, EngineState.EDGE_RETRYING)
                await self._sleep(cfg.backoff[min(attempt - 1, len(cfg.backoff) - 1)])
            try:
                clip = await self._edge.synthesize(chunk, voice, speed)
            except EdgeNoAudio as exc:
                await self._record_error(chunk, self._edge, exc)
                if c.state is EngineState.EDGE_RETRYING:
                    self._set_state(c, EngineState.EDGE_ACTIVE)  # Edge sí respondió
                raise _ContentFailure() from exc
            except EdgeVoiceNotFound as exc:
                await self._record_error(chunk, self._edge, exc)
                if c.state is EngineState.EDGE_RETRYING:
                    self._set_state(c, EngineState.EDGE_ACTIVE)
                raise _VoiceMissing() from exc
            except Exception as exc:  # cualquier otro fallo de Edge es transitorio
                self._log_failure(self._edge, exc)
                await self._record_error(chunk, self._edge, exc)
                continue
            self._edge_ok()
            self._close_circuit(c)  # un fragmento real por Edge: intervalo a cero
            self._set_state(c, EngineState.EDGE_ACTIVE)
            await self._record(chunk, self._edge, "ok")
            return clip, self._edge.name
        if c.half_open:
            log.info("Edge pasó la prueba pero falló con el fragmento; se vuelve a Piper")
            self._reopen_doubled(c)
        else:
            log.warning("Edge no responde tras %d reintentos; se usa Piper", retries)
            self._open_circuit(c)
        self._set_state(c, EngineState.PIPER_ACTIVE)
        return None

    async def _probe_edge(self, c: _Circuit, voice: str) -> None:
        self._set_state(c, EngineState.EDGE_RECOVERY_TEST)
        try:
            healthy = await self._edge.health_check(voice)
        except asyncio.CancelledError:
            # Cuenta como prueba fallida: saltar varias veces no repite la espera.
            self._probe_failed(c)
            raise
        except Exception:
            log.debug("La prueba de Edge lanzó un error", exc_info=True)
            healthy = False
        if not healthy:
            self._probe_failed(c)
            return
        log.info("Edge pasó la prueba de recuperación")
        c.half_open = True
        c.next_probe_at = math.inf
        self._set_state(c, EngineState.EDGE_ACTIVE)

    def _probe_failed(self, c: _Circuit) -> None:
        c.interval = min(c.interval * 2, self._config.recovery_max_s)
        c.next_probe_at = self._clock() + c.interval
        self._set_state(c, EngineState.PIPER_ACTIVE)

    async def _wait_for_any(
        self,
        c: _Circuit,
        chunk: Chunk,
        edge_voice: str,
        piper_voice: str,
        speed: float,
        *,
        try_first: bool,
    ) -> tuple[AudioClip, str]:
        """Espera sin límite probando Edge y luego Piper; sale con el primer éxito."""
        wait_max = self._config.wait_max_s
        self._set_state(c, EngineState.WAITING_NETWORK)
        delay = 1.0
        if not try_first:
            await self._sleep(min(delay, wait_max))
            delay = min(delay * 2, wait_max)
        while True:
            clip = await self._attempt(self._edge, chunk, edge_voice, speed)
            if clip is not None:
                if self._config.preferred == "piper":
                    self._set_state(c, EngineState.PIPER_ACTIVE)
                else:
                    self._close_circuit(c)
                    self._set_state(c, EngineState.EDGE_ACTIVE)
                return clip, self._edge.name
            clip = await self._attempt(self._piper, chunk, piper_voice, speed)
            if clip is not None:
                # El circuito de Edge sigue abierto: las pruebas de recuperación siguen.
                self._set_state(c, EngineState.PIPER_ACTIVE)
                return clip, self._piper.name
            await self._sleep(min(delay, wait_max))
            delay = min(delay * 2, wait_max)

    async def _content_fallback(
        self, c: _Circuit, chunk: Chunk, edge_voice: str, piper_voice: str, speed: float
    ) -> tuple[AudioClip, str]:
        """Edge no dio audio para este fragmento: ¿es el texto o la voz?

        Ver `synthesize`. Lanza `_VoiceMissing` si es la voz.
        """
        speakable = is_speakable(chunk.text)
        if speakable and chunk.chunk_id != self._last_no_audio:
            self._last_no_audio = chunk.chunk_id
            self._no_audio_streak += 1
            if self._no_audio_streak >= NO_AUDIO_STREAK:
                log.warning("Edge no da audio para %d fragmentos seguidos: falta la voz %s",
                            self._no_audio_streak, edge_voice)
                raise _VoiceMissing()
        log.warning("Edge no dio audio para el fragmento %s; se prueba Piper", chunk.chunk_id)
        clip = await self._attempt(self._piper, chunk, piper_voice, speed)
        if clip is not None:
            return clip, self._piper.name
        if speakable:
            # Sin voz local, antes de saltar nada se comprueba la voz con un texto conocido.
            verdict = await self._check_voice(edge_voice, speed)
            if verdict == "no_audio":
                log.warning("La voz %s tampoco da audio con un texto conocido: falta la voz",
                            edge_voice)
                raise _VoiceMissing()
            if verdict == "error":
                # No se sabe (p. ej. se fue la red): se espera, sin saltar el fragmento.
                return await self._wait_for_any(
                    c, chunk, edge_voice, piper_voice, speed, try_first=False
                )
        log.warning("Se salta el fragmento %s: ningún motor pudo leerlo", chunk.chunk_id)
        return AudioClip(pcm=np.zeros(0, dtype=np.int16), marks=[]), self._edge.name

    async def _check_voice(self, voice: str, speed: float) -> str:
        """Prueba la voz de Edge con `VOICE_CHECK_TEXT`: «ok», «no_audio» o «error»."""
        text = VOICE_CHECK_TEXT
        sentence = Sentence(0, 0, text, 0, len(text), None)
        probe = Chunk("comprobacion-voz", "", 0, 0, (sentence,), text, 0, len(text))
        try:
            await self._edge.synthesize(probe, voice, speed)
        except (EdgeNoAudio, EdgeVoiceNotFound):
            return "no_audio"
        except Exception:
            log.info("No se pudo comprobar la voz de Edge", exc_info=True)
            return "error"
        self._edge_ok()
        return "ok"

    async def _voice_gone(
        self, c: _Circuit, chunk: Chunk, edge_voice: str, piper_voice: str, speed: float
    ) -> tuple[AudioClip, str]:
        """La voz de Edge no existe: circuito abierto con esa voz, y Piper o se para.

        No se vuelve a probar Edge hasta que cambie la voz. Con Piper, se avisa una vez y
        Piper lee este fragmento; sin Piper, `EdgeVoiceUnavailable` (nunca se salta nada).
        """
        log.warning("Voz de Edge no disponible (%s); se usa Piper", edge_voice)
        self._open_circuit(c, failed_voice=edge_voice)
        self._set_state(c, EngineState.PIPER_ACTIVE)
        clip = await self._attempt(self._piper, chunk, piper_voice, speed)
        if clip is None:
            raise EdgeVoiceUnavailable()
        self._voice_missing(edge_voice)
        return clip, self._piper.name

    async def _attempt(
        self, engine: TtsEngine, chunk: Chunk, voice: str, speed: float
    ) -> AudioClip | None:
        try:
            clip = await engine.synthesize(chunk, voice, speed)
        except EdgeNoAudio as exc:
            await self._record_error(chunk, engine, exc)
            raise _ContentFailure() from exc
        except EdgeVoiceNotFound as exc:
            await self._record_error(chunk, engine, exc)
            raise _VoiceMissing() from exc
        except Exception as exc:
            self._log_failure(engine, exc)
            await self._record_error(chunk, engine, exc)
            return None
        if engine is self._edge:
            self._edge_ok()
        else:
            self._log_success(engine)
        await self._record(chunk, engine, "ok")
        return clip

    # --- circuito ---------------------------------------------------------

    def _new_circuit(self) -> _Circuit:
        if self._config.preferred == "piper":
            state = EngineState.PIPER_ACTIVE  # preferencia del usuario: sin pruebas
        else:
            state = EngineState.EDGE_ACTIVE
        return _Circuit(state=state, interval=self._config.recovery_initial_s)

    def _close_circuit(self, c: _Circuit) -> None:
        c.opened_at = -math.inf
        c.next_probe_at = math.inf
        c.interval = self._config.recovery_initial_s
        c.failed_voice = None
        c.half_open = False

    def _open_circuit(self, c: _Circuit, failed_voice: str | None = None) -> None:
        now = self._clock()
        c.opened_at = now
        c.interval = self._config.recovery_initial_s
        c.failed_voice = failed_voice
        c.half_open = False
        # Con la voz inexistente no hay pruebas hasta que cambie la voz.
        c.next_probe_at = math.inf if failed_voice is not None else now + c.interval

    def _reopen_doubled(self, c: _Circuit) -> None:
        now = self._clock()
        c.opened_at = now
        c.interval = min(c.interval * 2, self._config.recovery_max_s)
        c.next_probe_at = now + c.interval
        c.half_open = False

    def _probe_due(self, c: _Circuit) -> bool:
        now = self._clock()
        return now >= c.next_probe_at and now - c.opened_at >= self._config.piper_cooldown_s

    # --- avisos, registro y logs ------------------------------------------

    def _set_state(self, c: _Circuit, state: EngineState) -> None:
        if state is c.state:
            return
        c.state = state
        if c is not self._c:
            log.debug("Transición descartada tras reset(): %s", state)
            return
        self._notify(state)

    def _notify(self, state: EngineState) -> None:
        log.info("Motor: %s", state)
        try:
            self._on_state(state, _ACTIVE_LABEL[state])
        except Exception:
            log.exception("Fallo en el aviso de cambio de estado del motor")

    def _voice_missing(self, voice: str) -> None:
        """La voz de Edge no existe: error de configuración, se avisa una vez por voz."""
        if voice == self._missing_voice:
            return
        self._missing_voice = voice
        try:
            self._on_notice("warn", EDGE_VOICE_MISSING)
        except Exception:
            log.exception("Fallo en el aviso de voz de Edge inexistente")

    def _edge_ok(self) -> None:
        self._missing_voice = None  # si vuelve a faltar más tarde, se avisa de nuevo
        self._no_audio_streak = 0
        self._last_no_audio = None
        self._log_success(self._edge)

    def _log_failure(self, engine: TtsEngine, exc: Exception) -> None:
        """Traza completa solo en el primer fallo de una racha; después, una línea DEBUG."""
        if engine.name in self._failing:
            log.debug("El motor %s sigue fallando: %s: %s", engine.name, type(exc).__name__, exc)
            return
        self._failing.add(engine.name)
        log.warning("El motor %s falló", engine.name, exc_info=exc)

    def _log_success(self, engine: TtsEngine) -> None:
        if engine.name in self._failing:
            self._failing.discard(engine.name)
            log.info("El motor %s vuelve a funcionar", engine.name)

    async def _record_error(self, chunk: Chunk, engine: TtsEngine, exc: BaseException) -> None:
        await self._record(chunk, engine, f"error:{type(exc).__name__}")

    async def _record(self, chunk: Chunk, engine: TtsEngine, outcome: str) -> None:
        if self._chunk_log is None:
            return
        try:
            # Fuera del bucle: con la base de datos bloqueada puede tardar segundos.
            await asyncio.to_thread(
                self._chunk_log.record, chunk.chunk_id, engine.name, outcome
            )
        except Exception:
            log.warning("No se pudo registrar el fragmento en el historial", exc_info=True)
