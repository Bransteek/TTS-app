"""Montaje de la app completa (`build_app`) y su ciclo de vida (`AppContext`).

Orden de arranque:
1. Registro rotativo en `<datos>/lector.log`.
2. Instancia única: si ya hay un lector abierto, se le pide que muestre su ventana y se
   lanza `AlreadyRunning` sin abrir la base de datos.
3. Base de datos (si estaba dañada se renombra y se avisa).
4. `QApplication`, sin salir al cerrar la última ventana (la app vive en la bandeja).
5. Hilo de trabajo (`AsyncWorker`).
6. En el trabajador: descargador, motores, `EngineManager`, `AudioPlayer` y
   `ReadingController` (`build_services`). Es la única espera bloqueante, con tope.
7. Ventana principal y bandeja.
8. Si hay una lectura a medias se ofrece continuarla; nunca se lee sin que se pida.

Los errores no capturados los atiende `ErrorGuard` (ver `lector.services.app_errors`).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from lector import paths
from lector.audio.player import AudioPlayer
from lector.errors import AudioOutputError
from lector.persistence.database import LectorConnection
from lector.persistence.repositories import DocumentRepo, SettingsRepo, VoicesCacheRepo
from lector.services import recovery
from lector.services.app_builder import (
    EngineFactory,
    FetcherFactory,
    Services,
    build_services,
    default_engines,
    default_fetcher,
    open_app_database,
    sample_output_factory,
    setup_logging,
)
from lector.services.app_errors import ErrorGuard
from lector.services.audio_output import OutputFactory, default_output
from lector.services.reading_controller import ReadingController, Repos
from lector.services.sample_player import SamplePlayer
from lector.services.worker import AsyncWorker, QtEventBridge, call_ui
from lector.tts.piper_engine import PiperModelManager
from lector.ui.main_window import MainWindow
from lector.ui.open_flow import PLAYING_STATES
from lector.ui.quit_flow import WORKER_STOP_TIMEOUT_S, shutdown_and_stop
from lector.ui.resume_dialog import where_text
from lector.ui.single_instance import SingleInstance, default_server_name
from lector.ui.tray import TrayController, draw_icon

log = logging.getLogger(__name__)

APP_NAME = "Lector"
APP_USER_MODEL_ID = "LectorTTS.Lector"
LOG_NAME = "lector.log"
DB_NAME = "lector.db"
LOCK_NAME = "lector.lock"
MODELS_DIRNAME = "piper-voices"
BUILD_TIMEOUT_S = 30.0  # montaje en el trabajador; solo se espera así al arrancar
CLOSE_SHUTDOWN_TIMEOUT_S = 5.0
RESUME_TEXT = "Continuar {title} — {chapter}"

TrayFactory = Callable[..., Any]


class StartupError(Exception):
    """El lector no se pudo montar (la causa ya está en el registro)."""


class AlreadyRunning(Exception):
    """Ya hay un lector abierto; se le pidió que muestre su ventana."""


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


def default_tray(window, bridge, worker, controller, icon: QIcon):
    if not QSystemTrayIcon.isSystemTrayAvailable():
        log.warning("No hay bandeja del sistema: cerrar la ventana saldrá del lector")
        return None
    return TrayController(window, bridge, worker, controller, icon)


def bring_to_front(window: MainWindow) -> None:
    """Otra instancia pidió mostrar la ventana: es una acción del usuario."""
    window.showNormal()
    window.raise_()
    window.activateWindow()


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
    instance: SingleInstance
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
        """Salir de verdad. Con bandeja, lo hace ella; sin bandeja, el mismo camino compartido."""
        if self.tray is not None:
            self.tray.quit()
            return
        if self._quitting:
            return
        self._quitting = True
        shutdown_and_stop(self.window, self.worker, self.controller)
        self.window.close()
        QApplication.quit()

    def close(self) -> None:
        """Libera todo tras `app.exec()` (o al terminar una prueba). Es idempotente.

        Si el bucle de Qt terminó sin pasar por «Salir» (p. ej. al cerrar la sesión del
        sistema), aquí se cierra el controlador. Se espera de forma bloqueante y no con
        `quit_flow`: con `exec()` ya terminado, un bucle anidado podría no correr.
        """
        if self._closed:
            return
        self._closed = True
        try:
            future = self.worker.submit(self.controller.shutdown())
        except RuntimeError:
            future = None  # el trabajador ya se detuvo al salir
        if future is not None:
            try:
                future.result(timeout=CLOSE_SHUTDOWN_TIMEOUT_S)
            except Exception:
                log.exception("El cierre del controlador falló o tardó")
        try:
            self.worker.stop(timeout=WORKER_STOP_TIMEOUT_S)
        except Exception:
            log.exception("No se pudo detener el trabajador")
        self.guard.uninstall()
        self.instance.release()
        self.window.allow_close()
        self.window.close()
        self.window.deleteLater()
        self.bridge.deleteLater()
        self.app.setQuitOnLastWindowClosed(self._previous_quit_on_last)
        try:
            self.conn.close()
        except Exception:
            log.warning("No se pudo cerrar la base de datos", exc_info=True)
        _remove_logging(self.log_handler, self._previous_root_level)

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


def _remove_logging(handler: logging.Handler, previous_level: int) -> None:
    root = logging.getLogger()
    root.removeHandler(handler)
    root.setLevel(previous_level)
    handler.close()


def build_app(
    argv: list[str],
    *,
    data_dir: Path | str | None = None,
    output_factory: OutputFactory | None = None,
    fetcher_factory: FetcherFactory | None = None,
    tray_factory: TrayFactory | None = None,
    engine_factory: EngineFactory | None = None,
    lock_path: Path | None = None,
    server_name: str | None = None,
) -> AppContext:
    """Monta la app y muestra la ventana.

    Lanza `AlreadyRunning` si ya hay un lector abierto (tras pedirle que se muestre) y
    `StartupError` si no se pudo montar; en ambos casos deja todo liberado.
    """
    root_dir = Path(data_dir) if data_dir is not None else paths.data_dir()
    root_dir.mkdir(parents=True, exist_ok=True)
    previous_level = logging.getLogger().level
    log_path = root_dir / LOG_NAME
    handler = setup_logging(log_path)
    log.info("Arrancando el lector (datos en %s)", root_dir)

    instance = SingleInstance(lock_path or root_dir / LOCK_NAME,
                              server_name or default_server_name(root_dir))
    if not instance.acquire():
        log.info("Ya hay un lector abierto: se le pide que muestre su ventana")
        instance.notify_running()
        _remove_logging(handler, previous_level)
        raise AlreadyRunning()

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
            output_factory=sample_output_factory(output_factory),
            is_busy=lambda: controller.state in PLAYING_STATES,  # se evalúa en el trabajador
        )
        window.enable_settings(services.edge, sample)
        tray = (tray_factory or default_tray)(window, bridge, worker, controller, icon)
    except BaseException as exc:
        log.critical("No se pudo arrancar el lector", exc_info=True)
        if worker is not None:
            worker.stop(timeout=WORKER_STOP_TIMEOUT_S)
        if conn is not None:
            conn.close()
        guard.uninstall()
        instance.release()
        _remove_logging(handler, previous_level)
        if isinstance(exc, Exception) and not isinstance(exc, StartupError):
            raise StartupError(str(exc)) from exc
        raise

    ctx = AppContext(
        app=app, data_dir=root_dir, log_path=log_path, log_handler=handler, conn=conn,
        worker=worker, bridge=bridge, services=services, window=window, tray=tray,
        guard=guard, instance=instance, notices=notices,
        _previous_root_level=previous_level, _previous_quit_on_last=previous_quit_on_last,
    )
    instance.listen(lambda: bring_to_front(window))
    if tray is None:
        # Sin bandeja no hay dónde seguir leyendo: cerrar la ventana es salir.
        window.close_to_tray_requested.connect(ctx.quit)
    window.show()
    if notices:
        window.show_notice("\n".join(notices), "warn")
    ctx.offer_last_session()
    log.info("Lector listo")
    return ctx
