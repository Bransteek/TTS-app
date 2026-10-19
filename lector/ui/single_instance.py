"""Una sola instancia del lector por usuario.

Una segunda copia (p. ej. el acceso directo pulsado con la app en la bandeja) leería con
otra voz a la vez y mezclaría dos escritores de progreso en la misma base de datos. Por
eso, antes de abrir la base de datos:

- se toma `QLockFile(<datos>/lector.lock)` con `tryLock(0)`. Un cerrojo de un proceso
  muerto se detecta por su PID y se recupera. El tiempo de caducidad se pone a 0 para que
  un cerrojo vivo nunca caduque por antigüedad (Qt lo haría a los 30 s);
- si otra instancia lo tiene, se conecta a su `QLocalServer` y le manda «show». La
  primera instancia trae su ventana al frente (lo pidió el usuario: puede tomar el
  foco), y la segunda sale sin abrir nada.

Si el cerrojo no se puede usar (permisos, disco), se arranca igual y se registra.
"""
from __future__ import annotations

import getpass
import hashlib
import logging
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QLockFile
from PySide6.QtNetwork import QLocalServer, QLocalSocket

log = logging.getLogger(__name__)

SHOW_MESSAGE = b"show\n"
CONNECT_TIMEOUT_MS = 1000


def default_server_name(data_dir: Path) -> str:
    """Nombre del canal local, distinto por usuario y por carpeta de datos."""
    try:
        user = getpass.getuser()
    except Exception:
        user = ""
    digest = hashlib.sha256(f"{user}|{Path(data_dir).resolve()}".encode()).hexdigest()
    return f"lector-tts-{digest[:16]}"


class SingleInstance:
    def __init__(self, lock_path: Path, server_name: str):
        self._lock = QLockFile(str(lock_path))
        self._lock.setStaleLockTime(0)  # solo el PID decide si un cerrojo está abandonado
        self._server_name = server_name
        self._server: QLocalServer | None = None
        self._locked = False

    def acquire(self) -> bool:
        """True si esta es la única instancia (o si el cerrojo no se puede usar)."""
        if self._lock.tryLock(0):
            self._locked = True
            return True
        if self._lock.error() == QLockFile.LockError.LockFailedError:
            return False
        log.warning("No se pudo usar el cerrojo de instancia única (%s); se arranca igual",
                    self._lock.error())
        return True

    def notify_running(self) -> bool:
        """Pide a la instancia que ya corre que muestre su ventana. True si la recibió."""
        socket = QLocalSocket()
        socket.connectToServer(self._server_name)
        if not socket.waitForConnected(CONNECT_TIMEOUT_MS):
            log.warning("Hay otra instancia, pero no respondió: %s", socket.errorString())
            return False
        socket.write(SHOW_MESSAGE)
        sent = socket.waitForBytesWritten(CONNECT_TIMEOUT_MS)
        socket.disconnectFromServer()
        return sent

    def listen(self, on_show: Callable[[], None]) -> bool:
        """Atiende las peticiones de otras instancias (en el hilo de la interfaz)."""
        QLocalServer.removeServer(self._server_name)  # resto de un cierre brusco (Unix)
        server = QLocalServer()
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not server.listen(self._server_name):
            log.warning("No se pudo escuchar a otras instancias: %s", server.errorString())
            return False
        server.newConnection.connect(lambda: self._accept(server, on_show))
        self._server = server
        return True

    def _accept(self, server: QLocalServer, on_show: Callable[[], None]) -> None:
        while server.hasPendingConnections():
            socket = server.nextPendingConnection()
            received = bytearray()

            def read(sock=socket, buf=received) -> None:
                buf.extend(bytes(sock.readAll().data()))
                if SHOW_MESSAGE.strip() in buf:
                    buf.clear()
                    try:
                        on_show()
                    except Exception:
                        log.exception("No se pudo mostrar la ventana a petición de otra instancia")

            socket.readyRead.connect(read)
            socket.disconnected.connect(socket.deleteLater)
            if socket.bytesAvailable():
                read()

    def release(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        if self._locked:
            self._lock.unlock()
            self._locked = False
