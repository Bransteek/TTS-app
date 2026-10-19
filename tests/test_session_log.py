"""Registro (lector.log) de los cambios de la sesión: sin él, un fallo de la prueba manual
no se puede reconstruir. Nunca se registra el texto de la novela."""
from __future__ import annotations

import logging

from lector.errors import PageUnreachable
from lector.services.reading_controller import SessionState
from tests.controller_rig import STORY_ID, Outage, story_ref, story_rig, until

S = SessionState
CONTROLLER_LOG = "lector.services.reading_controller"


def messages(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.name == CONTROLLER_LOG and r.levelno == logging.INFO]


def assert_no_novel_text(caplog) -> None:
    assert not any("Frase" in m for m in messages(caplog))  # «Frase k del capítulo i.»


async def test_open_chapter_change_and_end_are_logged(controller_rig, caplog):
    caplog.set_level(logging.INFO, logger=CONTROLLER_LOG)
    rig = story_rig(controller_rig, chapters=2, sentences=2)
    await rig.start(story_ref(1).locator)
    await rig.hear(4)
    await until(lambda: rig.ctl.state is S.COMPLETED)

    log = messages(caplog)
    assert f"Abierto {story_ref(1).locator} (documento {STORY_ID}, capítulo índice 1)" in log
    assert f"Empieza la lectura del documento {STORY_ID}" in log
    assert f"Suena el capítulo {story_ref(1).locator}" in log
    assert f"Buscando el capítulo siguiente a {story_ref(1).locator}" in log
    assert f"Capítulo siguiente: {story_ref(2).locator}" in log
    assert f"Suena el capítulo {story_ref(2).locator}" in log
    assert f"Buscando el capítulo siguiente a {story_ref(2).locator}" in log
    assert "La lectura terminó: completed (No hay más capítulos publicados)" in log
    assert "Estado de la lectura: reading -> completed" in log
    assert_no_novel_text(caplog)


async def test_waiting_for_the_network_is_logged(controller_rig, caplog):
    caplog.set_level(logging.INFO, logger=CONTROLLER_LOG)
    rig = story_rig(controller_rig, chapters=2, sentences=2, prefetch_ahead=0)
    rig.source.failures = Outage(PageUnreachable())  # el capítulo 2 no llega
    await rig.start(story_ref(1).locator)
    await rig.hear(2)
    await until(lambda: rig.ctl.state is S.WAITING_NETWORK)
    rig.source.failures = []
    await until(lambda: rig.ctl.state is S.READING)

    log = messages(caplog)
    assert "Estado de la lectura: reading -> waiting_network" in log
    assert "Estado de la lectura: waiting_network -> reading" in log
    assert f"Capítulo siguiente: {story_ref(2).locator}" in log
    assert_no_novel_text(caplog)


# --- interfaz: lo que «Leer» hace o deja de hacer, y los globos de la bandeja ------------------

from tests.test_tray import rig  # noqa: E402,F401 - fixture
from tests.test_ui import (  # noqa: E402,F401 - fixtures y ayudas
    URL, conn, ctl, make_chapter, make_doc, make_window, read, wait_call, worker,
)

UI_LOG = "lector.ui.open_flow"
TRAY_LOG = "lector.ui.tray"


def ui_messages(caplog, logger: str) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.name == logger and r.levelno == logging.INFO]


def test_open_and_ignored_leer_are_logged(qtbot, make_window, ctl, caplog):
    caplog.set_level(logging.INFO, logger=UI_LOG)
    ctl.open_result = (make_doc(), make_chapter(), None)
    ctl.open_block_s = 0.3  # la apertura tarda: «Abriendo…»
    window = make_window(show=True)
    read(qtbot, window)
    window.url_edit.setText("https://example.com/otra/capitulo-1/")
    window._on_read_clicked()  # Enter en el campo mientras se abre (el botón está gris)
    wait_call(qtbot, ctl, "play")

    log = ui_messages(caplog, UI_LOG)
    assert f"Abriendo {URL}" in log
    assert ("Se ignora «Leer» con https://example.com/otra/capitulo-1/: "
            "aún se está abriendo otra dirección") in log


def test_balloons_and_repeats_are_logged(qtbot, rig, caplog):
    caplog.set_level(logging.INFO, logger=TRAY_LOG)
    rig.tray.notify("info", "No hay más capítulos publicados")
    rig.tray.notify("info", "No hay más capítulos publicados")

    log = ui_messages(caplog, TRAY_LOG)
    assert "Globo de la bandeja: No hay más capítulos publicados" in log
    assert "Globo repetido sin mostrar: No hay más capítulos publicados" in log
