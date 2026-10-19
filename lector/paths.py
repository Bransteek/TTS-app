"""Rutas de datos de la aplicación por plataforma."""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIRNAME = "LectorTTS"


def data_dir() -> Path:
    override = os.environ.get("LECTOR_DATA_DIR")
    if override:
        path = Path(override)
    elif sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        path = Path(base) / APP_DIRNAME
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / APP_DIRNAME
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        path = Path(base) / APP_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path
