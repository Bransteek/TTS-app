"""Punto de entrada del lector: monta la app, ofrece la última lectura y vigila los errores.

Orden de arranque:
1. Registro rotativo en `<datos>/lector.log`.
2. Base de datos (si estaba dañada se renombra y se avisa).
3. `QApplication`, sin salir al cerrar la última ventana (la app vive en la bandeja).
4. Hilo de trabajo (`AsyncWorker`).
5. En el trabajador: descargador, motores, `EngineManager`, `AudioPlayer` y
   `ReadingController`. Es la única espera bloqueante, con tope, antes de mostrar nada.
6. Ventana principal y bandeja.
7. Si hay una lectura a medias se ofrece continuarla; nunca se lee sin que se pida.

Errores no capturados: los de la interfaz (`sys.excepthook`), los del bucle del
trabajador (`loop.set_exception_handler`) y los de otros hilos (`threading.excepthook`)
se registran con su traza. Los dos primeros además guardan el progreso
(`shutdown(save_only=True)` en el trabajador, sin bloquear la interfaz) y avisan sin
modales. La app nunca se cierra por ellos.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from lector import paths
from lector.audio.player import AudioOutput, AudioPlayer, SoundDeviceOutput
from lector.errors import AudioOutputError, LectorError
from lector.persistence.database import LectorConnection, open_database
from lector.persistence.repositories import (
    ChunkLogRepo,
    DocumentRepo,
    SettingsRepo,
    VoicesCacheRepo,
)
from lector.security.http_client import SafeFetcher
from lector.services import recovery
from lector.services.reading_controller import ControllerDeps, ReadingController, Repos
from lector.services.sample_player import SamplePlayer
from lector.services.worker import AsyncWorker, QtEventBridge, call_ui
from lector.tts.edge_engine import EdgeEngine
from lector.tts.engine_manager import EngineConfig, EngineManager
from lector.tts.piper_engine import PiperEngine, PiperModelManager
from lector.ui.main_window import MainWindow
from lector.ui.open_flow import PLAYING_STATES
from lector.ui.resume_dialog import where_text
from lector.ui.tray import TrayController, draw_icon

log = logging.getLogger("lector.main")

APP_NAME = "Lector"
APP_USER_MODEL_ID = "LectorTTS.Lector"
LOG_NAME = "lector.log"
DB_NAME = "lector.db"
MODELS_DIRNAME = "piper-voices"
LOG_MAX_BYTES = 2_000_000
LOG_BACKUPS = 3
LOG_FORMAT = "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"
BUILD_TIMEOUT_S = 30.0  # montaje en el trabajador; solo se espera así al arrancar
SHUTDOWN_TIMEOUT_S = 5.0
WORKER_STOP_TIMEOUT_S = 2.0

DB_RECOVERED = (
    "La base de datos estaba dañada; se ha creado una nueva. "
    "Tu historial anterior se guardó como copia."
)
DB_UNAVAILABLE = (
    "No se pudo abrir tu historial. Puedes leer, pero esta vez no se guardará el progreso."
)
UNEXPECTED_ERROR = "Ocurrió un problema inesperado. Se guardó tu progreso y el lector sigue abierto."
STARTUP_FAILED = "El lector no pudo arrancar. Los detalles están en el archivo lector.log."
RESUME_TEXT = "Continuar {title} — {chapter}"

OutputFactory = Callable[[], AudioOutput]
FetcherFactory = Callable[[SettingsRepo], Any]
EngineFactory = Callable[[SettingsRepo, PiperModelManager], tuple[Any, Any]]
TrayFactory = Callable[..., Any]


class StartupError(Exception):
    """El lector no se pudo montar (ya se registró la causa)."""


# --- piezas por defecto (las pruebas las sustituyen) ------------------------------------------


def default_output() -> AudioOutput:
    """Salida real. Comprueba que haya un dispositivo de salida para avisar al arrancar."""
    import sounddevice as sd

    sd.query_devices(kind="output")  # sin dispositivo de salida por defecto, lanza
    return SoundDeviceOutput()


def default_fetcher(settings: SettingsRepo) -> SafeFetcher:
    return SafeFetcher(
        max_bytes=int(settings.get("http_max_bytes")),
        max_redirects=int(settings.get("http_max_redirects")),
    )


def default_engines(settings: SettingsRepo, models: PiperModelManager) -> tuple[Any, Any]:
    """El mismo EdgeEngine y el mismo PiperEngine sirven al gestor, a la configuración y a la muestra."""
    return EdgeEngine(timeout_s=float(settings.get("edge_timeout_s"))), PiperEngine(models)


def default_tray(window, bridge, worker, controller, icon: QIcon):
    if not QSystemTrayIcon.isSystemTrayAvailable():
        log.warning("No hay bandeja del sistema: cerrar la ventana saldrá del lector")
        return None
    return TrayController(window, bridge, worker, controller, icon)


class LazyOutput:
    """Salida que se crea al arrancar el sonido (no al montar la app).

    Se usa cuando no había salida de audio al arrancar: cada `start` lo vuelve a
    intentar y, si sigue sin haber, lanza `AudioOutputError`, que el controlador trata
    como cualquier fallo de la salida (un reintento y luego pausa con aviso).
    """

    def __init__(self, factory: OutputFactory):
        self._factory = factory
        self._output: AudioOutput | None = None

    def start(self, callback, on_error=None) -> None:
        if self._output is None:
            try:
                self._output = self._factory()
            except LectorError:
                raise
            except Exception as exc:
                raise AudioOutputError() from exc
        self._output.start(callback, on_error)

    def stop(self) -> None:
        if self._output is not None:
            self._output.stop()


# --- registro, base de datos y Windows -------------------------------------------------------


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


def set_windows_app_id(platform: str = sys.platform, shell32: Any = None) -> bool:
    """En Windows, da a la app su propio AppUserModelID para que los avisos de la bandeja
    lleven su nombre (y no el de Python). En otros sistemas no hace nada."""
    if platform != "win32":
        return False
    try:
        if shell32 is None:
            import ctypes

            shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
        shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
        return True
    except Exception:
        log.warning("No se pudo fijar el AppUserModelID de Windows", exc_info=True)
        return False


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


# --- errores no capturados ----------------------------------------------------------------------


class ErrorGuard:
    """Registra los errores no capturados, guarda el progreso y avisa sin cerrar la app.

    Si el propio manejador falla (o vuelve a entrar, p. ej. porque el aviso lanza dentro
    de un slot), solo se registra: nunca hay recursión.
    """

    def __init__(self) -> None:
        self.worker: AsyncWorker | None = None
        self.controller: Any = None
        self.bridge: QtEventBridge | None = None
        self._local = threading.local()
        self._save_lock = threading.Lock()
        self._pending_save: concurrent.futures.Future | None = None
        self._previous_hooks: tuple[Any, Any] | None = None

    def install(self) -> None:
        self._previous_hooks = (sys.excepthook, threading.excepthook)
        sys.excepthook = self.ui_hook
        threading.excepthook = self.thread_hook

    def uninstall(self) -> None:
        if self._previous_hooks is None:
            return
        previous_sys, previous_thread = self._previous_hooks
        self._previous_hooks = None
        if sys.excepthook == self.ui_hook:
            sys.excepthook = previous_sys
        if threading.excepthook == self.thread_hook:
            threading.excepthook = previous_thread

    # Hilo de la interfaz (también lo llama PySide con los errores de los slots).
    def ui_hook(self, exc_type, exc, tb) -> None:
        self._handle("en la interfaz", (exc_type, exc, tb))

    # Hilo del trabajador.
    def loop_hook(self, loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exc = context.get("exception")
        exc_info = (type(exc), exc, exc.__traceback__) if exc is not None else None
        self._handle(f"en el trabajador: {context.get('message')}", exc_info)

    def thread_hook(self, args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return
        try:
            name = args.thread.name if args.thread is not None else "?"
            log.error("Error no controlado en el hilo %s", name,
                      exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        except Exception:
            pass

    def _handle(self, where: str, exc_info) -> None:
        if getattr(self._local, "busy", False):
            try:
                log.error("Error mientras se atendía otro error (%s)", where, exc_info=exc_info)
            except Exception:
                pass
            return
        self._local.busy = True
        try:
            try:
                log.error("Error no controlado %s", where, exc_info=exc_info)
            except Exception:
                pass
            for step in (self._save, self._notify):
                try:
                    step()
                except Exception:
                    try:
                        log.exception("Falló el manejador de errores no capturados")
                    except Exception:
                        pass
        finally:
            self._local.busy = False

    def _save(self) -> None:
        """Pide al trabajador que guarde el progreso confirmado; no espera."""
        worker, controller = self.worker, self.controller
        if worker is None or controller is None:
            return
        with self._save_lock:
            pending = self._pending_save
            if pending is not None and not pending.done():
                return  # ya hay un guardado en camino
            try:
                future = worker.submit(controller.shutdown(save_only=True))
            except RuntimeError:
                log.warning("El trabajador ya no está en marcha; no se pudo guardar el progreso")
                return
            self._pending_save = future
        future.add_done_callback(_log_save_failure)

    def _notify(self) -> None:
        bridge = self.bridge
        if bridge is not None:
            bridge.on_notice("error", UNEXPECTED_ERROR)  # ventana y, sin el usuario, bandeja


def _log_save_failure(future: concurrent.futures.Future) -> None:
    if future.cancelled():
        return
    exc = future.exception()
    if exc is not None:
        log.error("No se pudo guardar el progreso tras un error",
                  exc_info=(type(exc), exc, exc.__traceback__))


# --- montaje -----------------------------------------------------------------------------------


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
    bridge: QtEventBridge,
    guard: ErrorGuard,
    output_factory: OutputFactory,
    fetcher_factory: FetcherFactory,
    engine_factory: EngineFactory,
) -> Services:
    """Se ejecuta en el bucle del trabajador: `AudioPlayer` y el controlador necesitan ese bucle."""
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

    audio_failed = False
    try:
        output = output_factory()
    except Exception:
        log.warning("No hay salida de audio al arrancar; se reintentará al leer", exc_info=True)
        audio_failed = True
        output = LazyOutput(output_factory)
    player = AudioPlayer(
        output, loop,
        max_ahead=int(settings.get("queue_ahead_chunks")),
        max_seconds=float(settings.get("queue_max_seconds")),
    )
    controller = ReadingController(
        ControllerDeps(fetcher, Repos.from_connection(conn), manager, player, settings),
        bridge,
    )
    return Services(fetcher, edge, piper, manager, player, controller, audio_failed)


@dataclass
class AppContext:
    app: QApplication
    data_dir: Path
    log_path: Path
    log_handler: logging.Handler
    conn: LectorConnection
    worker: AsyncWorker
    bridge: QtEventBridge
    services: Services
    window: MainWindow
    tray: Any
    guard: ErrorGuard
    notices: list[str] = field(default_factory=list)
    _previous_root_level: int = logging.WARNING
    _previous_quit_on_last: bool = True
    _quitting: bool = False
    _closed: bool = False

    @property
    def controller(self) -> ReadingController:
        return self.services.controller

    @property
    def player(self) -> AudioPlayer:
        return self.services.player

    def quit(self) -> None:
        """Salir de verdad. Con bandeja, lo hace ella (cierre, trabajador y `QApplication.quit`)."""
        if self.tray is not None:
            self.tray.quit()
            return
        if self._quitting:
            return
        self._quitting = True
        self.window.allow_close()
        self._shutdown_controller()
        try:
            self.worker.stop(timeout=WORKER_STOP_TIMEOUT_S)
        except Exception:
            log.exception("No se pudo detener el trabajador al salir")
        self.window.close()
        QApplication.quit()

    def _shutdown_controller(self) -> None:
        try:
            future = self.worker.submit(self.controller.shutdown())
        except RuntimeError:
            return  # el trabajador ya se detuvo
        try:
            future.result(timeout=SHUTDOWN_TIMEOUT_S)
        except Exception:
            log.exception("El cierre del controlador falló o tardó; se sale igualmente")

    def close(self) -> None:
        """Libera todo tras `app.exec()` (o al terminar una prueba). Es idempotente.

        Si el bucle de Qt terminó sin pasar por «Salir» (p. ej. al cerrar la sesión del
        sistema), aquí se cierra el controlador y se guarda el progreso.
        """
        if self._closed:
            return
        self._closed = True
        self._shutdown_controller()
        try:
            self.worker.stop(timeout=WORKER_STOP_TIMEOUT_S)
        except Exception:
            log.exception("No se pudo detener el trabajador")
        self.guard.uninstall()
        self.window.allow_close()
        self.window.close()
        self.window.deleteLater()
        self.bridge.deleteLater()
        self.app.setQuitOnLastWindowClosed(self._previous_quit_on_last)
        try:
            self.conn.close()
        except Exception:
            log.warning("No se pudo cerrar la base de datos", exc_info=True)
        root = logging.getLogger()
        root.removeHandler(self.log_handler)
        root.setLevel(self._previous_root_level)
        self.log_handler.close()

    # --- última lectura ---------------------------------------------------------------------

    def offer_last_session(self) -> None:
        """Lee la última sesión en el trabajador y la ofrece en la ventana, sin reproducir."""
        repos = Repos.from_connection(self.conn)
        try:
            future = self.worker.submit(asyncio.to_thread(_load_last_session, repos))
        except RuntimeError:
            return
        call_ui(future, self._show_last_session, _log_offer_failure)

    def _show_last_session(self, found) -> None:
        if found is None:
            return
        doc, record, chapter = found
        text = RESUME_TEXT.format(title=record.title or doc.title, chapter=chapter)
        self.window.show_notice("\n".join([*self.notices, text]),
                                "warn" if self.notices else "info")
        self.window.offer_resume(doc, record, chapter_title=chapter,
                                 auto_continue_when_hidden=False)


def _load_last_session(repos: Repos):
    """En un hilo del trabajador: (documento, progreso, capítulo) de la última lectura."""
    record = recovery.last_session(repos)
    if record is None:
        return None
    doc = repos.documents.get(record.doc_id)
    if doc is None:
        return None
    known = repos.chapters.title(record.doc_id, record.position.chapter_locator)
    return doc, record, where_text(doc, record.position, known)


def _log_offer_failure(exc: BaseException) -> None:
    if not isinstance(exc, asyncio.CancelledError):
        log.error("No se pudo leer la última sesión",
                  exc_info=(type(exc), exc, exc.__traceback__))


def build_app(
    argv: list[str],
    *,
    data_dir: Path | str | None = None,
    output_factory: OutputFactory | None = None,
    fetcher_factory: FetcherFactory | None = None,
    tray_factory: TrayFactory | None = None,
    engine_factory: EngineFactory | None = None,
) -> AppContext:
    """Monta la app completa y muestra la ventana. Lanza `StartupError` si no se puede."""
    root_dir = Path(data_dir) if data_dir is not None else paths.data_dir()
    root_dir.mkdir(parents=True, exist_ok=True)
    root_logger = logging.getLogger()
    previous_level = root_logger.level
    log_path = root_dir / LOG_NAME
    handler = setup_logging(log_path)
    log.info("Arrancando el lector (datos en %s)", root_dir)
    guard = ErrorGuard()
    guard.install()
    notices: list[str] = []
    conn: LectorConnection | None = None
    worker: AsyncWorker | None = None
    try:
        conn = open_app_database(root_dir / DB_NAME, notices)

        set_windows_app_id()
        app = QApplication.instance() or QApplication(list(argv))
        previous_quit_on_last = app.quitOnLastWindowClosed()
        app.setQuitOnLastWindowClosed(False)
        app.setApplicationName(APP_NAME)
        icon = draw_icon()
        app.setWindowIcon(icon)

        worker = AsyncWorker()
        worker.start()
        bridge = QtEventBridge()  # en el hilo de la interfaz
        guard.worker, guard.bridge = worker, bridge

        settings = SettingsRepo(conn)
        models = PiperModelManager(root_dir / MODELS_DIRNAME)
        output_factory = output_factory or default_output
        future = worker.submit(build_services(
            conn, settings, models, bridge, guard, output_factory,
            fetcher_factory or default_fetcher, engine_factory or default_engines,
        ))
        try:
            services = future.result(timeout=BUILD_TIMEOUT_S)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise StartupError(f"El montaje tardó más de {BUILD_TIMEOUT_S} s") from None
        guard.controller = services.controller
        if services.audio_failed:
            notices.append(AudioOutputError.user_message)

        controller = services.controller
        window = MainWindow(worker, bridge, controller, settings, models,
                            documents=DocumentRepo(conn), voices=VoicesCacheRepo(conn))
        sample = SamplePlayer(
            services.piper,
            output_factory=lambda: LazyOutput(output_factory),
            is_busy=lambda: controller.state in PLAYING_STATES,  # se evalúa en el trabajador
        )
        window.enable_settings(services.edge, sample)
        tray = (tray_factory or default_tray)(window, bridge, worker, controller, icon)
    except BaseException:
        log.critical("No se pudo arrancar el lector", exc_info=True)
        if worker is not None:
            worker.stop(timeout=WORKER_STOP_TIMEOUT_S)
        if conn is not None:
            conn.close()
        guard.uninstall()
        raise

    ctx = AppContext(
        app=app, data_dir=root_dir, log_path=log_path, log_handler=handler, conn=conn,
        worker=worker, bridge=bridge, services=services, window=window, tray=tray,
        guard=guard, notices=notices, _previous_root_level=previous_level,
        _previous_quit_on_last=previous_quit_on_last,
    )
    if tray is None:
        # Sin bandeja no hay dónde seguir leyendo: cerrar la ventana es salir.
        window.close_to_tray_requested.connect(ctx.quit)
    window.show()
    if notices:
        window.show_notice("\n".join(notices), "warn")
    ctx.offer_last_session()
    log.info("Lector listo")
    return ctx


def _show_startup_failure() -> None:
    try:
        app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841 (debe existir)
        QMessageBox.critical(None, APP_NAME, STARTUP_FAILED)
    except Exception:
        log.exception("No se pudo mostrar el aviso de fallo al arrancar")


def main() -> int:
    try:
        ctx = build_app(sys.argv)
    except Exception:
        log.critical("El lector no pudo arrancar", exc_info=True)
        _show_startup_failure()
        return 1
    try:
        return ctx.app.exec()
    finally:
        ctx.close()


if __name__ == "__main__":
    sys.exit(main())
