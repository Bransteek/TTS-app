"""Punto de entrada del lector.

El montaje está en `lector.ui.app.build_app` (orden de arranque, instancia única,
errores no capturados y salida); aquí solo se arranca el bucle de Qt.
"""
from __future__ import annotations

import logging
import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from lector.ui.app import APP_NAME, AlreadyRunning, build_app

log = logging.getLogger("lector.main")

STARTUP_FAILED = "El lector no pudo arrancar. Los detalles están en el archivo lector.log."


def _show_startup_failure() -> None:
    """Único diálogo modal de la app: solo al arrancar, nunca con el juego delante."""
    try:
        app = QApplication.instance() or QApplication(sys.argv)  # viva mientras dura el aviso
        QMessageBox.critical(None, APP_NAME, STARTUP_FAILED)
        del app
    except Exception:
        log.exception("No se pudo mostrar el aviso de fallo al arrancar")


def main() -> int:
    try:
        ctx = build_app(sys.argv)
    except AlreadyRunning:
        return 0  # la instancia abierta ya muestra su ventana
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
