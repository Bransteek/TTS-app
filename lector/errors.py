"""Jerarquía de errores con mensajes en español para el usuario."""
from __future__ import annotations

from .models import ChapterRef


class LectorError(Exception):
    user_message: str = "Ocurrió un problema inesperado."
    recoverable: bool = False

    def __init__(self, message: str | None = None, *, recoverable: bool | None = None):
        if message is not None:
            self.user_message = message
        if recoverable is not None:
            self.recoverable = recoverable
        super().__init__(self.user_message)


class InvalidUrl(LectorError):
    user_message = "La dirección no es válida. Revisa que empiece por http:// o https://."


class PageUnreachable(LectorError):
    user_message = "No se pudo abrir la página. Revisa tu conexión a internet."
    recoverable = True


class NoExtractableText(LectorError):
    user_message = "No se encontró texto para leer en esta página."


class NextChapterNotFound(LectorError):
    user_message = "No se encontró el capítulo siguiente."


class ChapterLoopDetected(LectorError):
    user_message = "Se detectó un ciclo de capítulos y se detuvo la lectura."


class AmbiguousNextChapter(LectorError):
    user_message = "Hay varios capítulos posibles como siguiente. Elige uno."

    def __init__(self, candidates: list[ChapterRef], message: str | None = None, *, recoverable: bool | None = None):
        super().__init__(message, recoverable=recoverable)
        self.candidates = candidates


class DamagedDocument(LectorError):
    user_message = "El archivo está dañado y no se puede abrir."


class ScannedPdf(LectorError):
    user_message = "Este PDF parece ser una imagen escaneada y no contiene texto que leer."


class EdgeUnavailable(LectorError):
    user_message = "La voz en línea no está disponible por ahora."
    recoverable = True


class EdgeVoiceNotFound(LectorError):
    user_message = "No se encontró la voz en línea elegida."


class PiperModelMissing(LectorError):
    user_message = "Falta la voz sin conexión. Descárgala desde la configuración."


class SynthesisFailed(LectorError):
    user_message = "No se pudo convertir el texto en voz. Se reintentará."
    recoverable = True


class AudioOutputError(LectorError):
    user_message = "Hay un problema con la salida de audio. Revisa tus altavoces o auriculares."
    recoverable = True


class ResourceExhausted(LectorError):
    user_message = "El contenido es demasiado grande para procesarlo."


class DatabaseLocked(LectorError):
    user_message = "La base de datos está ocupada. Inténtalo de nuevo en unos segundos."
    recoverable = True


class DatabaseCorrupt(LectorError):
    user_message = "Los datos guardados están dañados."


class FileMovedOrChanged(LectorError):
    user_message = (
        'No se encuentra el archivo o ha cambiado. Pulsa "Buscar el archivo…" o ábrelo otra vez '
        'con "Abrir PDF/EPUB"; tu progreso se conserva.'
    )

    def __init__(self, doc_id: str, message: str | None = None, *, recoverable: bool | None = None):
        super().__init__(message, recoverable=recoverable)
        self.doc_id = doc_id
