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
    instance = SingleInstance(tmp_path / "x.lock", server_name(tmp_path))
    assert instance.notify_running(retry_s=0.2) is False


class Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def test_notify_retries_while_the_first_instance_is_still_starting(tmp_path):
    clock = Clock()
    instance = SingleInstance(tmp_path / "x.lock", server_name(tmp_path),
                              clock=clock.now, sleep=clock.sleep)
    answers = iter([False, False, False, True])
    instance._send_show = lambda: next(answers)
    assert instance.notify_running(retry_s=5.0) is True
    assert len(clock.sleeps) == 3 and clock.t < 5.0


def test_notify_gives_up_after_the_retry_window(tmp_path):
    clock = Clock()
    instance = SingleInstance(tmp_path / "x.lock", server_name(tmp_path),
                              clock=clock.now, sleep=clock.sleep)
    tries = []
    instance._send_show = lambda: tries.append(1) or False
    assert instance.notify_running(retry_s=5.0) is False
    assert 5.0 <= clock.t < 6.0
    assert len(tries) >= 10


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


def test_unusable_lock_never_steals_a_running_instance_server(qapp, qtbot, tmp_path):
    name = server_name(tmp_path)
    running = SingleInstance(tmp_path / "lector.lock", name)
    assert running.acquire()
    shown_running, shown_other = [], []
    assert running.listen(lambda: shown_running.append(True))

    blocker = tmp_path / "no-es-carpeta"
    blocker.write_text("x")
    other = SingleInstance(blocker / "lector.lock", name)
    assert other.acquire()  # cerrojo inusable: arranca igual...
    other.listen(lambda: shown_other.append(True))  # ...pero sin quitarle el canal a nadie

    assert SingleInstance(tmp_path / "z.lock", name).notify_running(retry_s=0.5)
    qtbot.waitUntil(lambda: shown_running == [True], timeout=WAIT_MS)
    assert shown_other == []
    other.release()
    running.release()


def test_server_name_is_stable_and_per_data_dir(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    assert default_server_name(a) == default_server_name(a)
    assert default_server_name(a) != default_server_name(b)
    assert default_server_name(a).startswith("lector-tts-")
