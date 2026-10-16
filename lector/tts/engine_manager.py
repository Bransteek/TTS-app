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
        self._c = self._new_circuit()

    @property
    def state(self) -> EngineState:
        return self._c.state

    def set_on_state(self, on_state: Callable[[EngineState, str], None]) -> None:
        """Sustituye el aviso de cambio de estado (lo registra el controlador de lectura)."""
        self._on_state = on_state

    def reset(self) -> None:
        """Vuelve al estado inicial (sesión nueva): circuito cerrado y motor preferido.

        Una síntesis en curso puede devolver su clip, pero sus transiciones ya
        no se aplican.
        """
        old, self._c = self._c, self._new_circuit()
        if old.state is not self._c.state:
            self._notify(self._c.state)

    async def synthesize(
        self, chunk: Chunk, edge_voice: str, piper_voice: str, speed: float
    ) -> tuple[AudioClip, str]:
        """Sintetiza `chunk` con el motor que toque; devuelve el clip y el nombre del motor.

        No devuelve hasta conseguir audio de algún motor. Solo sale con
        `CancelledError` si se cancela la tarea.
        """
        async with self._lock:
            c = self._c
            try:
                return await self._synthesize(c, chunk, edge_voice, piper_voice, speed)
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
            except EdgeVoiceNotFound as exc:
                log.warning("Voz de Edge no encontrada (%s); se usa Piper", voice)
                self._open_circuit(c, failed_voice=voice)
                self._set_state(c, EngineState.PIPER_ACTIVE)
                await self._record_error(chunk, self._edge, exc)
                return None
            except Exception as exc:  # cualquier otro fallo de Edge es transitorio
                self._log_failure(self._edge, exc)
                await self._record_error(chunk, self._edge, exc)
                continue
            self._log_success(self._edge)
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

    async def _attempt(
        self, engine: TtsEngine, chunk: Chunk, voice: str, speed: float
    ) -> AudioClip | None:
        try:
            clip = await engine.synthesize(chunk, voice, speed)
        except Exception as exc:
            self._log_failure(engine, exc)
            await self._record_error(chunk, engine, exc)
            return None
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
