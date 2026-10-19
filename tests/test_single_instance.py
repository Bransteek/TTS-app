"""Una sola instancia: la segunda pide a la primera que se muestre y no abre nada."""
from __future__ import annotations

import subprocess
import sys

from lector.ui.single_instance import SingleInstance, default_server_name

WAIT_MS = 3000


def server_name(tmp_path) -> str:
    return default_server_name(tmp_path)


def test_second_instance_is_refused_and_asks_the_first_to_show(qapp, qtbot, tmp_path):
    lock, name = tmp_path / "lector.lock", server_name(tmp_path)
    first = SingleInstance(lock, name)
    assert first.acquire()
    shown = []
    assert first.listen(lambda: shown.append(True))

    second = SingleInstance(lock, name)
    assert not second.acquire()
    assert second.notify_running()
    qtbot.waitUntil(lambda: shown == [True], timeout=WAIT_MS)

    first.release()
    third = SingleInstance(lock, name)
    assert third.acquire()  # al salir la primera, se puede volver a abrir
    third.release()


def test_stale_lock_of_a_dead_process_is_taken_over(tmp_path):
    lock = tmp_path / "lector.lock"
    code = (
        "import os, sys\n"
        "from PySide6.QtCore import QLockFile\n"
        "l = QLockFile(sys.argv[1])\n"
        "assert l.tryLock(0)\n"
        "os._exit(0)\n"  # sin liberar: el archivo queda con un PID muerto
    )
    subprocess.run([sys.executable, "-c", code, str(lock)], check=True, timeout=30)
    assert lock.exists()

    instance = SingleInstance(lock, server_name(tmp_path))
    assert instance.acquire()
    instance.release()


def test_notify_without_a_listening_first_instance_returns_false(qapp, tmp_path):
    assert SingleInstance(tmp_path / "x.lock", server_name(tmp_path)).notify_running() is False


def test_unusable_lock_does_not_block_startup(tmp_path):
    blocker = tmp_path / "no-es-carpeta"
    blocker.write_text("x")
    instance = SingleInstance(blocker / "lector.lock", server_name(tmp_path))
    assert instance.acquire()  # sin poder bloquear, se arranca igual (se registra)
    instance.release()


def test_listen_replaces_a_leftover_server(qapp, qtbot, tmp_path):
    from PySide6.QtNetwork import QLocalServer

    name = server_name(tmp_path)
    leftover = QLocalServer()  # lo que deja un cierre brusco (en Unix, el archivo del socket)
    assert leftover.listen(name)
    instance = SingleInstance(tmp_path / "a.lock", name)
    assert instance.acquire()
    shown = []
    assert instance.listen(lambda: shown.append(True))
    assert SingleInstance(tmp_path / "a.lock", name).notify_running()
    qtbot.waitUntil(lambda: shown == [True], timeout=WAIT_MS)
    instance.release()
    leftover.close()


def test_server_name_is_stable_and_per_data_dir(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    assert default_server_name(a) == default_server_name(a)
    assert default_server_name(a) != default_server_name(b)
    assert default_server_name(a).startswith("lector-tts-")
