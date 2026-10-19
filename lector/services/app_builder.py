"""Montaje de los servicios del lector (lo que no es interfaz).

- `setup_logging`: registro rotativo en UTF-8 (`lector.log` en la carpeta de datos).
- `open_app_database`: abre la base de datos y deja el aviso para el usuario si estaba
  dañada o si no se pudo abrir (entonces se usa una en memoria y la app abre igual).
- `engine_config`, `default_fetcher`, `default_engines`: piezas leídas de la configuración.
- `build_services`: corre en el bucle del trabajador y monta descargador, motores,
  `EngineManager`, `AudioPlayer` y `ReadingController`.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from lector.audio.player import AudioPlayer
from lector.errors import LectorError
from lector.persistence.database import LectorConnection, open_database
from lector.persistence.repositories import ChunkLogRepo, SettingsRepo, VoicesCacheRepo
from lector.security.http_client import SafeFetcher
from lector.services.app_errors import ErrorGuard
from lector.services.audio_output import (
    LazyOutput,
    OutputFactory,
    default_output,
    open_output,
    reinit_portaudio,
)
from lector.services.reading_controller import ControllerDeps, ReadingController, Repos
from lector.tts.edge_engine import EdgeEngine
from lector.tts.engine_manager import EngineConfig, EngineManager
from lector.tts.piper_engine import PiperEngine, PiperModelManager

log = logging.getLogger(__name__)

LOG_MAX_BYTES = 2_000_000
LOG_BACKUPS = 3
LOG_FORMAT = "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"
QUIET_LOGGERS = ("httpx", "httpcore")  # registran cada URL a nivel INFO

DB_RECOVERED = (
    "La base de datos estaba dañada; se ha creado una nueva. "
    "Tu historial anterior se guardó como copia."
)
DB_UNAVAILABLE = (
    "No se pudo abrir tu historial. Puedes leer, pero esta vez no se guardará el progreso."
)

FetcherFactory = Callable[[SettingsRepo], Any]
EngineFactory = Callable[[SettingsRepo, PiperModelManager], tuple[Any, Any]]


def setup_logging(log_path: Path) -> logging.Handler:
    """Registro rotativo en UTF-8; si no se puede escribir, a la consola (si la hay)."""
    failed = False
    try:
        handler: logging.Handler = RotatingFileHandler(
            log_path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
        )
    except OSError:
        failed = True
        handler = logging.StreamHandler(sys.stderr)  # con pythonw no hay consola: se pierde
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.setLevel(logging.INFO)
    root = logging.getLogger()
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    if failed:
        log.error("No se pudo crear el archivo de registro %s", log_path, exc_info=True)
    return handler


def open_app_database(path: Path, notices: list[str]) -> LectorConnection:
    """Abre la base de datos; si estaba dañada, avisa. Si no se puede abrir, usa una en memoria."""
    try:
        conn = open_database(path)
    except (LectorError, OSError):
        log.error("No se pudo abrir la base de datos %s", path, exc_info=True)
        notices.append(DB_UNAVAILABLE)
        return open_database(Path(":memory:"))
    if conn.recovered_from_corruption:
        log.warning("La base de datos estaba dañada; se apartó y se creó una nueva")
        notices.append(DB_RECOVERED)
    return conn


def engine_config(settings: SettingsRepo) -> EngineConfig:
    """Se lee una vez al arrancar (la configuración avisa de que estos valores valen al reiniciar)."""
    preferred = settings.get("preferred_engine")
    preferred = preferred if preferred in ("edge", "piper") else "edge"
    try:
        return EngineConfig(
            preferred=preferred,
            edge_retries=int(settings.get("edge_retries")),
            backoff=tuple(float(s) for s in settings.get("edge_backoff_s")),
            recovery_initial_s=float(settings.get("recovery_initial_s")),
            recovery_max_s=float(settings.get("recovery_max_s")),
            piper_cooldown_s=float(settings.get("piper_cooldown_s")),
            wait_max_s=float(settings.get("wait_max_s")),
        )
    except (TypeError, ValueError):
        log.warning("Configuración de motores no válida; se usan los valores por defecto",
                    exc_info=True)
        return EngineConfig(preferred=preferred)


def default_fetcher(settings: SettingsRepo) -> SafeFetcher:
    return SafeFetcher(
        max_bytes=int(settings.get("http_max_bytes")),
        max_redirects=int(settings.get("http_max_redirects")),
    )


def default_engines(settings: SettingsRepo, models: PiperModelManager) -> tuple[Any, Any]:
    """El mismo EdgeEngine y el mismo PiperEngine sirven al gestor, a la configuración y a la muestra."""
    return EdgeEngine(timeout_s=float(settings.get("edge_timeout_s"))), PiperEngine(models)


@dataclass
class Services:
    """Lo que vive en el hilo de trabajo."""

    fetcher: Any
    edge: Any
    piper: Any
    engine_manager: EngineManager
    player: AudioPlayer
    controller: ReadingController
    audio_failed: bool


async def build_services(
    conn: LectorConnection,
    settings: SettingsRepo,
    models: PiperModelManager,
    events: Any,
    guard: ErrorGuard,
    output_factory: OutputFactory,
    fetcher_factory: FetcherFactory,
    engine_factory: EngineFactory,
) -> Services:
    """En el bucle del trabajador: `AudioPlayer` y el controlador necesitan ese bucle."""
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(guard.loop_hook)
    try:
        ChunkLogRepo(conn).prune()
    except Exception:
        log.warning("No se pudo podar el registro de fragmentos", exc_info=True)

    fetcher = fetcher_factory(settings)
    edge, piper = engine_factory(settings, models)
    try:
        edge.set_known_voices(VoicesCacheRepo(conn).load())
    except Exception:
        log.warning("No se pudo cargar la caché de voces de Edge", exc_info=True)
    manager = EngineManager(edge, piper, engine_config(settings), chunk_log=ChunkLogRepo(conn))

    # Reiniciar PortAudio solo tiene sentido con la salida real.
    reset = reinit_portaudio if output_factory is default_output else None
    output, audio_failed = await open_output(output_factory, reset=reset)
    player = AudioPlayer(
        output, loop,
        max_ahead=int(settings.get("queue_ahead_chunks")),
        max_seconds=float(settings.get("queue_max_seconds")),
    )
    controller = ReadingController(
        ControllerDeps(fetcher, Repos.from_connection(conn), manager, player, settings),
        events,
    )
    return Services(fetcher, edge, piper, manager, player, controller, audio_failed)


def sample_output_factory(output_factory: OutputFactory) -> OutputFactory:
    """Salida de «Escuchar muestra»: una nueva cada vez; un fallo llega como `AudioOutputError`.

    Sin reiniciar PortAudio: la lectura podría tener su stream abierto (en pausa).
    """
    return lambda: LazyOutput(output_factory)
