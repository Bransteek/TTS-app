"""Piezas del montaje: configuración de motores, base de datos y registro."""
from __future__ import annotations

import logging

from lector.persistence.database import open_database
from lector.persistence.repositories import SettingsRepo
from lector.services.app_builder import (
    DB_UNAVAILABLE,
    engine_config,
    open_app_database,
    setup_logging,
)
from lector.tts.engine_manager import EngineConfig


def settings_repo(tmp_path) -> SettingsRepo:
    return SettingsRepo(open_database(tmp_path / "lector.db"))


def test_engine_config_maps_every_setting(tmp_path):
    settings = settings_repo(tmp_path)
    for key, value in {
        "preferred_engine": "piper", "edge_retries": 5, "edge_backoff_s": [0.5, 1, 3],
        "recovery_initial_s": 30, "recovery_max_s": 300, "piper_cooldown_s": 45,
        "wait_max_s": 20,
    }.items():
        settings.set(key, value)
    assert engine_config(settings) == EngineConfig(
        preferred="piper", edge_retries=5, backoff=(0.5, 1.0, 3.0), recovery_initial_s=30.0,
        recovery_max_s=300.0, piper_cooldown_s=45.0, wait_max_s=20.0,
    )


def test_engine_config_defaults_match_the_settings_defaults(tmp_path):
    assert engine_config(settings_repo(tmp_path)) == EngineConfig()


def test_invalid_engine_settings_fall_back_to_defaults_keeping_preference(tmp_path, caplog):
    settings = settings_repo(tmp_path)
    settings.set("preferred_engine", "piper")
    settings.set("recovery_max_s", 1)  # menor que recovery_initial_s: no válido
    with caplog.at_level(logging.WARNING):
        config = engine_config(settings)
    assert config == EngineConfig(preferred="piper")
    assert "no válida" in caplog.text


def test_unknown_preferred_engine_becomes_edge(tmp_path):
    settings = settings_repo(tmp_path)
    settings.set("preferred_engine", "otro")
    assert engine_config(settings).preferred == "edge"


def test_unopenable_database_falls_back_to_memory_with_a_notice(tmp_path):
    path = tmp_path / "lector.db"
    path.mkdir()  # una carpeta donde debería estar el archivo
    notices: list[str] = []
    conn = open_app_database(path, notices)
    assert notices == [DB_UNAVAILABLE]
    SettingsRepo(conn).set("speed", 1.5)
    assert SettingsRepo(conn).get("speed") == 1.5
    conn.close()


def test_setup_logging_quiets_http_libraries(tmp_path):
    root = logging.getLogger()
    level = root.level
    handler = setup_logging(tmp_path / "lector.log")
    try:
        assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
        assert logging.getLogger("httpcore").getEffectiveLevel() == logging.WARNING
    finally:
        root.removeHandler(handler)
        handler.close()
        root.setLevel(level)
