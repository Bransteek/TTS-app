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
from dataclasses import dataclass
from enum import StrEnum, auto
from typing import TYPE_CHECKING

from ..errors import EdgeVoiceNotFound
from ..models import Chunk
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
        self._close_circuit()
        self._state = self._initial_state()

    @property
    def state(self) -> EngineState:
        return self._state

    def reset(self) -> None:
        """Vuelve al estado inicial (sesión nueva): circuito cerrado y motor preferido."""
        self._close_circuit()
        self._set_state(self._initial_state())

    async def synthesize(
        self, chunk: Chunk, edge_voice: str, piper_voice: str, speed: float
    ) -> tuple[AudioClip, str]:
        """Sintetiza `chunk` con el motor que toque; devuelve el clip y el nombre del motor.

        No devuelve hasta conseguir audio de algún motor. Solo sale con
        `CancelledError` si se cancela la tarea.
        """
        async with self._lock:
            try:
                return await self._synthesize(chunk, edge_voice, piper_voice, speed)
            except asyncio.CancelledError:
                # Los estados transitorios no deben sobrevivir a la cancelación.
                if self._state is EngineState.EDGE_RETRYING:
                    self._set_state(EngineState.EDGE_ACTIVE)
                elif self._state is EngineState.EDGE_RECOVERY_TEST:
                    self._set_state(EngineState.PIPER_ACTIVE)
                raise

    async def _synthesize(
        self, chunk: Chunk, edge_voice: str, piper_voice: str, speed: float
    ) -> tuple[AudioClip, str]:
        if self._failed_voice is not None and edge_voice != self._failed_voice:
            log.info("Cambió la voz de Edge; se vuelve a intentar con Edge")
            self._close_circuit()
            self._set_state(EngineState.EDGE_ACTIVE)

        if self._state is EngineState.WAITING_NETWORK:
            # Una espera anterior se canceló: se reintenta ya, sin esperar antes.
            return await self._wait_for_any(
                chunk, edge_voice, piper_voice, speed, try_first=True
            )

        if self._state is EngineState.PIPER_ACTIVE and self._probe_due():
            await self._probe_edge(edge_voice)

        if self._state is EngineState.EDGE_ACTIVE:
            result = await self._edge_with_retries(chunk, edge_voice, speed)
            if result is not None:
                return result

        clip = await self._attempt(self._piper, chunk, piper_voice, speed)
        if clip is not None:
            return clip, self._piper.name
        return await self._wait_for_any(
            chunk, edge_voice, piper_voice, speed, try_first=False
        )

    async def _edge_with_retries(
        self, chunk: Chunk, voice: str, speed: float
    ) -> tuple[AudioClip, str] | None:
        """Intenta Edge con reintentos; si no lo consigue, abre el circuito y devuelve None."""
        cfg = self._config
        for attempt in range(cfg.edge_retries + 1):
            if attempt:
                self._set_state(EngineState.EDGE_RETRYING)
                await self._sleep(cfg.backoff[min(attempt - 1, len(cfg.backoff) - 1)])
            try:
                clip = await self._edge.synthesize(chunk, voice, speed)
            except EdgeVoiceNotFound as exc:
                self._record_error(chunk, self._edge, exc)
                log.warning("Voz de Edge no encontrada (%s); se usa Piper", voice)
                self._open_circuit(failed_voice=voice)
                self._set_state(EngineState.PIPER_ACTIVE)
                return None
            except Exception as exc:  # cualquier otro fallo de Edge es transitorio
                self._record_error(chunk, self._edge, exc)
                log.info("Edge falló (intento %d)", attempt + 1, exc_info=True)
                continue
            self._record(chunk, self._edge, "ok")
            self._set_state(EngineState.EDGE_ACTIVE)
            return clip, self._edge.name
        log.warning("Edge no responde tras %d reintentos; se usa Piper", cfg.edge_retries)
        self._open_circuit()
        self._set_state(EngineState.PIPER_ACTIVE)
        return None

    async def _probe_edge(self, voice: str) -> None:
        self._set_state(EngineState.EDGE_RECOVERY_TEST)
        try:
            healthy = await self._edge.health_check(voice)
        except Exception:
            log.info("La prueba de Edge lanzó un error", exc_info=True)
            healthy = False
        if healthy:
            log.info("Edge volvió a responder; se cierra el circuito")
            self._close_circuit()
            self._set_state(EngineState.EDGE_ACTIVE)
            return
        cfg = self._config
        self._interval = min(self._interval * 2, cfg.recovery_max_s)
        self._next_probe_at = self._clock() + self._interval
        self._set_state(EngineState.PIPER_ACTIVE)

    async def _wait_for_any(
        self,
        chunk: Chunk,
        edge_voice: str,
        piper_voice: str,
        speed: float,
        *,
        try_first: bool,
    ) -> tuple[AudioClip, str]:
        """Espera sin límite probando Edge y luego Piper; sale con el primer éxito."""
        self._set_state(EngineState.WAITING_NETWORK)
        delay = 1.0
        if not try_first:
            await self._sleep(min(delay, self._config.wait_max_s))
            delay *= 2
        while True:
            clip = await self._attempt(self._edge, chunk, edge_voice, speed)
            if clip is not None:
                self._close_circuit()
                self._set_state(EngineState.EDGE_ACTIVE)
                return clip, self._edge.name
            clip = await self._attempt(self._piper, chunk, piper_voice, speed)
            if clip is not None:
                # El circuito de Edge sigue abierto: las pruebas de recuperación siguen.
                self._set_state(EngineState.PIPER_ACTIVE)
                return clip, self._piper.name
            await self._sleep(min(delay, self._config.wait_max_s))
            delay = min(delay * 2, self._config.wait_max_s)

    async def _attempt(
        self, engine: TtsEngine, chunk: Chunk, voice: str, speed: float
    ) -> AudioClip | None:
        try:
            clip = await engine.synthesize(chunk, voice, speed)
        except Exception as exc:
            self._record_error(chunk, engine, exc)
            log.info("El motor %s falló", engine.name, exc_info=True)
            return None
        self._record(chunk, engine, "ok")
        return clip

    # --- circuito ---------------------------------------------------------

    def _initial_state(self) -> EngineState:
        if self._config.preferred == "piper":
            return EngineState.PIPER_ACTIVE  # preferencia del usuario: sin pruebas
        return EngineState.EDGE_ACTIVE

    def _close_circuit(self) -> None:
        self._opened_at = -math.inf
        self._next_probe_at = math.inf
        self._interval = self._config.recovery_initial_s
        self._failed_voice: str | None = None

    def _open_circuit(self, failed_voice: str | None = None) -> None:
        now = self._clock()
        self._opened_at = now
        self._interval = self._config.recovery_initial_s
        self._failed_voice = failed_voice
        # Con la voz inexistente no hay pruebas hasta que cambie la voz.
        self._next_probe_at = math.inf if failed_voice is not None else now + self._interval

    def _probe_due(self) -> bool:
        now = self._clock()
        return (
            now >= self._next_probe_at
            and now - self._opened_at >= self._config.piper_cooldown_s
        )

    # --- avisos y registro ------------------------------------------------

    def _set_state(self, state: EngineState) -> None:
        if state is self._state:
            return
        log.info("Motor: %s -> %s", self._state, state)
        self._state = state
        try:
            self._on_state(state, _ACTIVE_LABEL[state])
        except Exception:
            log.exception("Fallo en el aviso de cambio de estado del motor")

    def _record_error(self, chunk: Chunk, engine: TtsEngine, exc: BaseException) -> None:
        self._record(chunk, engine, f"error:{type(exc).__name__}")

    def _record(self, chunk: Chunk, engine: TtsEngine, outcome: str) -> None:
        if self._chunk_log is None:
            return
        try:
            self._chunk_log.record(chunk.chunk_id, engine.name, outcome)
        except Exception:
            log.warning("No se pudo registrar el fragmento en el historial", exc_info=True)
