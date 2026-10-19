"""Controlador de lectura: orquesta navegador, motores y reproductor.

Hilos: todo el controlador vive en el bucle asyncio del hilo de trabajo. El
reproductor entrega sus eventos en ese bucle (`call_soon_threadsafe`), el gestor
de motores y el navegador avisan desde tareas del mismo bucle, y los métodos
públicos se ejecutan ahí. Por eso el estado no necesita candados de hilo; un
`asyncio.Lock` serializa las operaciones que sustituyen al productor, de modo
que nunca hay dos vivos a la vez. Los eventos hacia la interfaz
(`ControllerEvents`) también salen en ese bucle: quien los reciba en Qt debe
reenviarlos a su hilo.

Progreso confirmado: `Position` describe la última frase que sonó entera
(`sentence_index == -1` si aún no sonó ninguna del capítulo). El punto de
reanudación es la frase siguiente (`resume_sentence`).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from enum import StrEnum, auto
from typing import Any, Protocol

from lector.audio.player import AudioPlayer, QueuedClip
from lector.content import registry
from lector.content.base import ContentSource
from lector.content.segmenter import is_speakable, make_chunks
from lector.errors import (
    AUDIO_OUTPUT_LOST,
    AmbiguousNextChapter,
    ChapterLoopDetected,
    EdgeVoiceUnavailable,
    LectorError,
)
from lector.models import Chapter, ChapterRef, Chunk, Document, Position, Sentence, SourceKind
from lector.persistence.repositories import (
    ChapterRepo,
    DocumentRepo,
    ProgressRecord,
    ProgressRepo,
    SettingsRepo,
)
from lector.services.document_opener import DeferredSource, SourceOpener, check_file, store_opened
from lector.services.navigator import ChapterNavigator, TextWrites
from lector.services.progress_writer import ProgressWriter, make_record
from lector.tts.base import AudioClip, clamp_speed
from lector.tts.engine_manager import EngineManager, EngineState

log = logging.getLogger(__name__)

CHARS_PER_SECOND = 14.0
# Esperas antes de cada reintento de abrir la salida de audio (unos 2 min en total): al
# despertar el equipo o cambiar de dispositivo, la salida puede tardar en volver.
OUTPUT_RETRY_DELAYS_S = (1, 2, 4, 8, 16, 30, 30, 30)
# Vigilancia de la salida: con audio que debería estar sonando, tantos segundos sin que
# la salida pida muestras es que se atascó sin avisar (p. ej. al quitar el dispositivo).
STALL_TIMEOUT_S = 5.0
STALL_CHECK_S = 1.0
NO_MORE_CHAPTERS = "No hay más capítulos publicados"
CHAPTER_FINISHED = "Terminó el capítulo."
FIRST_CHAPTER = "Este ya es el primer capítulo."
UNKNOWN_DOCUMENT = "No se encontró ese documento en el historial."
SPEED_ADJUSTED = "La velocidad se ajustó a {speed:g}, el valor permitido más cercano."

# Estados del gestor en los que el audio sale de Piper (límites de fragmento de Piper).
_PIPER_STATES = frozenset({EngineState.PIPER_ACTIVE, EngineState.EDGE_RECOVERY_TEST})
# Resultado de una navegación manual cancelada por stop(), shutdown() o play().
_ABORTED = object()


class SessionState(StrEnum):
    IDLE = auto()
    LOADING = auto()
    READING = auto()
    PAUSED = auto()
    STOPPED = auto()
    COMPLETED = auto()
    WAITING_NETWORK = auto()
    NEEDS_ATTENTION = auto()
    ERROR = auto()


class ControllerEvents(Protocol):
    """Avisos hacia la interfaz. Se llaman en el bucle asyncio del hilo de trabajo."""

    def on_session_state(self, state: SessionState) -> None: ...

    def on_engine_state(self, state: EngineState, active: str) -> None: ...

    def on_document(self, doc: Document, chapter: Chapter) -> None: ...

    def on_sentence(
        self, chapter_title: str, sentence_text: str, sentence_index: int, total: int
    ) -> None: ...

    def on_notice(self, level: str, message: str) -> None: ...  # "info" | "warn" | "error"

    def on_needs_choice(self, candidates: list[ChapterRef]) -> None: ...


def resume_sentence(position: Position) -> int:
    """Frase por la que se reanuda: la siguiente a la última confirmada."""
    return position.sentence_index + 1


@dataclass
class Repos:
    documents: DocumentRepo
    chapters: ChapterRepo
    progress: ProgressRepo

    @classmethod
    def from_connection(cls, conn) -> Repos:
        return cls(DocumentRepo(conn), ChapterRepo(conn), ProgressRepo(conn))


NavigatorFactory = Callable[[ContentSource, Document], ChapterNavigator]


@dataclass
class ControllerDeps:
    fetcher: Any  # SafeFetcher (None si solo se abren archivos)
    repos: Repos
    engine_manager: EngineManager
    player: AudioPlayer
    settings: SettingsRepo
    source_opener: SourceOpener = registry.open_source
    # None: ChapterNavigator con store_text, prefetch_ahead y wait_max_s de la configuración.
    navigator_factory: NavigatorFactory | None = None
    clock: Callable[[], float] = time.monotonic  # limita el guardado a uno cada 2 s
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep  # esperas propias (guardado, salida)


@dataclass
class _Settings:
    edge_max_chars: int
    edge_max_sentences: int
    piper_max_chars: int
    continuous: bool
    store_text: bool
    prefetch_ahead: int
    wait_max_s: float
    edge_voice: str
    piper_voice: str  # «nombre#hablante», como lo espera el motor
    speed: float


def _read_settings(repo: SettingsRepo) -> _Settings:
    get = repo.get
    piper, speaker = get("piper_voice"), get("piper_speaker")
    speed, _ = clamp_speed("edge", float(get("speed")))
    return _Settings(
        edge_max_chars=int(get("edge_max_chars")),
        edge_max_sentences=int(get("edge_max_sentences")),
        piper_max_chars=int(get("piper_max_chars")),
        continuous=bool(get("continuous")),
        store_text=bool(get("store_text")),
        prefetch_ahead=int(get("prefetch_ahead")),
        wait_max_s=float(get("wait_max_s")),
        edge_voice=get("edge_voice"),
        piper_voice=f"{piper}#{speaker}" if speaker else piper,
        speed=speed,
    )


@dataclass(eq=False)
class _Planned:
    """Un fragmento entregado al reproductor y el capítulo al que pertenece."""

    chunk: Chunk
    chapter: Chapter


@dataclass
class _Mark:
    """Última frase confirmada. `chapter` es una referencia hasta que se carga."""

    chapter: Chapter | ChapterRef
    position: Position
    chunk_id: str | None


@dataclass(eq=False)
class _Session:
    doc: Document
    source: ContentSource
    nav: ChapterNavigator
    first: Chapter | None  # el capítulo que devolvió la fuente al abrirla (si se abrió)


@dataclass
class _Launch:
    target: Chapter | ChapterRef
    sentence: int
    show: bool  # posición elegida: se muestra el capítulo en cuanto carga
    mark_on_load: bool  # la marca se calcula al cargar el capítulo


@dataclass
class _End:
    """Final pendiente del productor; se aplica cuando termina de sonar la cola."""

    state: SessionState
    notice: tuple[str, str] | None
    candidates: list[ChapterRef] | None = None
    continuous_off: bool = False
    # La sesión no termina: queda en pausa y «Reanudar» vuelve a lanzar el productor
    # desde el primer fragmento que no llegó a la cola (nada se salta).
    hold: bool = False


def _queued_clip(chunk: Chunk, clip: AudioClip, engine: str) -> QueuedClip | None:
    """Clip listo para el reproductor; None si no trae audio.

    Corrige marcas imperfectas en vez de dejar que un clip raro detenga la lectura.
    """
    size = len(clip.pcm)
    if size == 0:
        return None
    marks: list[tuple[int, int]] = []
    for offset, index in clip.marks:
        if 0 <= offset < size and (not marks or offset > marks[-1][0]):
            marks.append((offset, index))
    if not marks or marks[0][0] != 0:
        marks.insert(0, (0, chunk.sentences[0].index))
    return QueuedClip(chunk, clip.pcm, marks, engine)


_PAGES_LOCATOR = re.compile(r"^pages:(\d+)-(\d+)$")


def _page_range_has(locator: str, page: int) -> bool:
    """¿El capítulo de PDF «pages:<inicio>-<fin>» contiene `page`?"""
    match = _PAGES_LOCATOR.match(locator)
    return bool(match) and int(match.group(1)) <= page <= int(match.group(2))


def _previous_spoken(sentences: list[Sentence], index: int) -> int:
    """La frase anterior a `index` que suena: los separadores sin voz no están en ningún
    fragmento, así que volver a uno sería volver a la frase actual."""
    index -= 1
    while 0 < index < len(sentences) and not is_speakable(sentences[index].text):
        index -= 1
    return index


def _skip_index(sentences: list[Sentence], start: int, delta: float, speed: float) -> int:
    """Avanza o retrocede frase a frase hasta cubrir |delta| segundos estimados."""
    per_second = CHARS_PER_SECOND * speed
    index = min(start, len(sentences))
    covered = 0.0
    if delta >= 0:
        while covered < delta and index < len(sentences):
            covered += len(sentences[index].text) / per_second
            index += 1
    else:
        while covered < -delta and index > 0:
            index -= 1
            covered += len(sentences[index].text) / per_second
    return index


class ReadingController:
    """Sesión de lectura: un productor (síntesis -> cola) y el progreso confirmado.

    Implementa `PlayerEvents`: se registra en el reproductor y en el gestor de
    motores al crearse.
    """

    def __init__(self, deps: ControllerDeps, events: ControllerEvents):
        self._deps = deps
        self._events = events
        self._player = deps.player
        self._em = deps.engine_manager
        self._repos = deps.repos
        self._sleep = deps.sleep
        self._lock = asyncio.Lock()
        self._text_writes = TextWrites()  # vista previa y capítulo elegido (no del navegador)
        self._state = SessionState.IDLE
        self._closed = False

        self._settings: _Settings | None = None
        self._preview: tuple[ContentSource, Document, Chapter] | None = None
        self._session: _Session | None = None
        self._chapter_indexes: dict[str, int] = {}

        # Productor y posición.
        self._producer: asyncio.Task | None = None
        self._last_launch: _Launch | None = None
        self._next_unqueued: tuple[Chapter | ChapterRef, int] | None = None
        self._planned: dict[int, _Planned] = {}  # id(chunk) -> fragmento en el reproductor
        self._mark: _Mark | None = None
        self._end: _End | None = None
        self._epoch = 0  # cambia con cada salto, play o stop
        self._shown_locator: str | None = None
        self._nav_tasks: set[asyncio.Task] = set()  # navegaciones manuales en curso

        # Componentes del estado de sesión.
        self._active = False  # hay productor o audio de la sesión pendiente
        self._loading = False
        self._paused = False
        self._nav_waits = 0
        self._engine_waiting = False
        self._output_retries = 0  # reintentos de abrir la salida desde el último audio
        self._output_failed = False  # la salida falló y nadie la ha vuelto a abrir
        self._output_retry: asyncio.Task | None = None
        self._watchdog: asyncio.Task | None = None  # vigila que la salida no se atasque
        self._relaunch = False  # en pausa sin productor: «Reanudar» lo vuelve a lanzar

        self._progress = ProgressWriter(
            save=lambda record: self._repos.progress.save(record),
            build=self._build_record,
            clock=deps.clock,
            sleep=deps.sleep,
            on_error=self._on_save_error,
        )
        self._em.set_on_state(self._on_engine_state)
        self._em.set_on_notice(self._notice)
        self._player.set_events(self)

    @property
    def state(self) -> SessionState:
        return self._state

    # --- API pública ----------------------------------------------------------------

    async def open(self, locator: str) -> tuple[Document, Chapter, ProgressRecord | None]:
        """Vista previa: abre la fuente y guarda el documento, pero no reproduce."""
        await self._ensure_settings()
        source, doc, chapter = await self._open_source(locator)
        store = self._settings.store_text  # el valor vivo, tras la espera de la red
        await self._text_writes.run(
            store_opened, self._repos.documents, self._repos.chapters, doc, chapter, store,
            with_text=store,
        )
        progress = await asyncio.to_thread(self._repos.progress.get, doc.doc_id)
        self._preview = (source, doc, chapter)
        return doc, chapter, progress

    async def play(self, doc_id: str, start: Position | None = None) -> None:
        """Empieza a leer el documento.

        `start` es la PRIMERA frase que se lee (`start.sentence_index`), no la última
        confirmada: para continuar desde un `ProgressRecord` hay que pasar `None`, o
        una posición con `sentence_index = resume_sentence(record.position)`. Sin
        `start` se continúa donde se quedó o, si no hay progreso, desde el principio.

        Una novela web del historial se abre sin red con el texto guardado; si falta,
        la sesión espera la red (WAITING_NETWORK). Lanza los errores de apertura al
        llamador, p. ej. `FileMovedOrChanged` si un PDF o EPUB se movió o cambió.
        """
        if self._closed:
            return
        async with self._lock:
            await self._halt_locked()
            await self._save_now()
            self._set_state(SessionState.LOADING)
            try:
                self._settings = await asyncio.to_thread(_read_settings, self._deps.settings)
                progress = await asyncio.to_thread(self._repos.progress.get, doc_id)
                session = await self._session_for(doc_id)
                # Una sesión que sigue abierta adopta lo que diga ahora la configuración.
                await session.nav.set_store_text(self._settings.store_text)
            except BaseException:
                self._set_state(SessionState.IDLE)
                raise
            self._em.reset()  # el productor anterior ya está cancelado
            if self._paused:
                self._paused = False
                self._player.resume()  # quita la pausa (y reabre la salida si falló)
            self._output_retries = 0
            self._shown_locator = None
            self._mark = None
            if start is not None:
                target = self._target_for(session, start.chapter_locator, start.chapter_index)
                self._launch(_Launch(target, start.sentence_index, True, True))
            elif progress is not None:
                pos = progress.position
                target = self._target_for(session, pos.chapter_locator, pos.chapter_index)
                self._mark = _Mark(target, pos, progress.last_chunk_id)
                self._request_save(force=True)
                self._launch(_Launch(target, resume_sentence(pos), True, False))
            else:
                self._launch(_Launch(self._first_target(session), 0, True, True))

    async def pause(self) -> None:
        if not self._active or self._paused:
            return
        self._paused = True
        self._player.pause()
        self._refresh()
        await self._save_now()

    async def resume(self) -> None:
        if not self._active or not self._paused:
            return
        self._paused = False
        self._output_retries = 0
        self._output_failed = False
        self._player.resume()  # también reabre la salida si estaba parada por un fallo
        self._refresh()
        if self._relaunch:
            await self._relaunch_held()

    async def _relaunch_held(self) -> None:
        """Tras una pausa por falta de voz: se vuelve a probar todo desde lo que no sonó."""
        async with self._lock:
            if not self._relaunch or not self._active or self._closed or self._paused:
                return
            self._relaunch = False
            self._em.reset()  # quizá cambió la voz o se instaló Piper: se prueba de nuevo
            target, index = self._next_unqueued
            if isinstance(target, Chapter):
                self._launch(_Launch(target, index, False, False))
            elif self._last_launch is not None:
                self._launch(self._last_launch)

    async def stop(self) -> None:
        async with self._lock:
            if self._session is None or self._state in (SessionState.IDLE, SessionState.STOPPED):
                return
            await self._halt_locked()
            await self._session.nav.close()
            self._set_state(SessionState.STOPPED)
            await self._save_now()

    async def next_sentence(self) -> None:
        await self._seek(lambda base: (base[0], base[1] + 1) if base else None)

    async def prev_sentence(self) -> None:
        await self._seek(
            lambda base: (base[0], _previous_spoken(base[0].sentences, base[1])) if base else None
        )

    async def restart_chapter(self) -> None:
        await self._seek(lambda base: (base[0], 0) if base else None)

    async def skip_seconds(self, delta: float) -> None:
        def target(base):
            if base is None:
                return None
            chapter, index = base
            speed = self._settings.speed if self._settings else 1.0
            return chapter, _skip_index(chapter.sentences, index, delta, speed)

        await self._seek(target)

    async def next_chapter(self) -> None:
        if not self._controllable():
            return
        base = self._base()
        if base is None:
            return
        current, epoch, nav = base[0], self._epoch, self._session.nav
        nav.reset_visited(current.ref)  # un salto manual nunca es un ciclo por sí solo
        try:
            nxt = await self._navigate(nav.next_of(current))
        except AmbiguousNextChapter as exc:
            self._notice("warn", exc.user_message)
            self._emit("on_needs_choice", list(exc.candidates))
            return
        except LectorError as exc:
            self._notice("warn", exc.user_message)
            return
        if nxt is _ABORTED:
            return
        if nxt is None:
            self._notice("info", NO_MORE_CHAPTERS)
            return
        self._chapter_index(nxt, hint=self._chapter_index(current) + 1)
        await self._jump(nxt, epoch)

    async def prev_chapter(self) -> None:
        if not self._controllable():
            return
        base = self._base()
        if base is None:
            return
        current, epoch = base[0], self._epoch
        try:
            previous = await self._navigate(self._session.nav.prev_of(current))
        except LectorError as exc:
            self._notice("warn", exc.user_message)
            return
        if previous is _ABORTED:
            return
        if previous is None:
            self._notice("info", FIRST_CHAPTER)
            return
        self._chapter_index(previous, hint=self._chapter_index(current) - 1)
        await self._jump(previous, epoch)

    async def choose_next(self, ref: ChapterRef) -> None:
        """Sigue por `ref` y lo recuerda como «siguiente» del capítulo actual (§7.4)."""
        if not self._controllable():
            return
        base = self._base()
        current, epoch = (base[0] if base else None), self._epoch
        try:
            chapter = await self._navigate(self._session.nav.get(ref))
        except LectorError as exc:
            self._notice("warn", exc.user_message)
            return
        if chapter is _ABORTED:
            return
        if current is not None and current.ref.locator != chapter.ref.locator:
            await self._remember_next(current, chapter.ref)
        await self._jump(chapter, epoch)

    async def set_voice(
        self, edge_voice: str | None, piper_voice: str | None, apply_now: bool
    ) -> None:
        settings = await self._ensure_settings()
        if edge_voice is not None:
            settings.edge_voice = edge_voice
            await self._persist_setting("edge_voice", edge_voice)
        if piper_voice is not None:
            settings.piper_voice = piper_voice
            name, _, speaker = piper_voice.partition("#")
            await self._persist_setting("piper_voice", name)
            await self._persist_setting("piper_speaker", speaker or None)
        await self._apply_change(apply_now)

    async def set_speed(self, speed: float, apply_now: bool) -> None:
        settings = await self._ensure_settings()
        # El rango de Edge cabe dentro del de Piper: vale para los dos motores.
        value, clamped = clamp_speed("edge", speed)
        if clamped:
            self._notice("info", SPEED_ADJUSTED.format(speed=value))
        settings.speed = value
        await self._persist_setting("speed", value)
        await self._apply_change(apply_now)

    async def set_continuous(self, on: bool) -> None:
        settings = await self._ensure_settings()
        settings.continuous = on
        await self._persist_setting("continuous", on)
        if not on:
            return
        async with self._lock:
            # Aún suena el final de un capítulo leído sin lectura continua: se sigue.
            end, producer = self._end, self._producer
            if end is None or not end.continuous_off or (producer and not producer.done()):
                return
            target, index = self._next_unqueued
            if isinstance(target, Chapter):
                self._launch(_Launch(target, index, False, False))

    async def set_preferred_engine(self, name: str) -> None:
        """Motor preferido («edge» o «piper»): vale desde el siguiente fragmento y se guarda.

        Lo ya preparado en la cola suena con el motor anterior; el productor vuelve
        a trocear con los límites del nuevo motor al ver el cambio.
        """
        self._em.set_preferred(name)  # ValueError si el nombre no es válido
        await self._persist_setting("preferred_engine", name)

    async def set_store_text(self, on: bool) -> None:
        """Guardar o no el texto de los capítulos, también en la sesión en marcha.

        Al desactivarlo, vuelve cuando ya no queda ninguna escritura con texto en curso
        (navegador, vista previa o capítulo elegido): un borrado posterior es definitivo.
        """
        async with self._lock:  # no se cruza con un play() que relee la configuración
            settings = await self._ensure_settings()
            settings.store_text = on
            await self._persist_setting("store_text", on)
            if self._session is not None:
                await self._session.nav.set_store_text(on)
        if not on:
            await self._text_writes.drain()

    async def set_chunk_limits(
        self, edge_max_chars: int, edge_max_sentences: int, piper_max_chars: int
    ) -> None:
        """Tamaño de los fragmentos: vale desde el siguiente capítulo (o el siguiente play)."""
        settings = await self._ensure_settings()
        settings.edge_max_chars = edge_max_chars
        settings.edge_max_sentences = edge_max_sentences
        settings.piper_max_chars = piper_max_chars
        await self._persist_setting("edge_max_chars", edge_max_chars)
        await self._persist_setting("edge_max_sentences", edge_max_sentences)
        await self._persist_setting("piper_max_chars", piper_max_chars)

    async def locate_page(self, doc_id: str, page: int) -> Position | None:
        """Posición de la primera frase de `page` en el PDF abierto en vista previa o en lectura.

        Devuelve None si ese documento no está abierto o la página no existe.
        """
        if self._preview is not None and self._preview[1].doc_id == doc_id:
            source, doc, first = self._preview
        elif self._session is not None and self._session.doc.doc_id == doc_id:
            source, doc, first = self._session.source, self._session.doc, self._session.first
        else:
            return None
        ref = next((r for r in doc.chapters if _page_range_has(r.locator, page)), None)
        if ref is None:
            return None
        if first is not None and first.ref.locator == ref.locator:
            chapter = first
        else:
            chapter = await source.load_chapter(ref)
        sentence = next(
            (s for s in chapter.sentences if s.page is not None and s.page >= page), None
        )
        index = ref.index if ref.index is not None else 0
        if sentence is None:
            return Position(index, ref.locator, 0, 0, 0, page)
        return Position(index, ref.locator, sentence.paragraph_index, sentence.index,
                        sentence.start, page)

    async def shutdown(self, save_only: bool = False) -> None:
        """Cierra la sesión. Con `save_only` solo guarda el progreso confirmado."""
        if save_only:
            await self._save_now()
            return
        if self._closed:
            return
        async with self._lock:
            self._closed = True
            await self._halt_locked()
            if self._session is not None:
                await self._session.nav.close()
            self._player.close()
            await self._save_now()

    async def flush_progress(self) -> None:
        """Espera a que se escriba el último progreso pedido (adelanta una escritura diferida)."""
        await self._progress.flush()

    # --- Eventos del reproductor (PlayerEvents, en el bucle) --------------------------

    def on_sentence_started(self, chunk: Chunk, sentence_index: int) -> None:
        try:
            planned = self._planned.get(id(chunk))
            if planned is None:
                return
            self._audio_ok()
            k = next((j for j, s in enumerate(chunk.sentences) if s.index == sentence_index), 0)
            if k > 0:  # la anterior del fragmento sonó entera
                self._confirm(planned, chunk.sentences[k - 1])
                self._request_save()
            chapter = planned.chapter
            self._show(chapter)
            if 0 <= sentence_index < len(chapter.sentences):
                self._emit(
                    "on_sentence", chapter.title, chapter.sentences[sentence_index].text,
                    sentence_index, len(chapter.sentences),
                )
        except Exception:
            log.exception("Fallo al procesar el inicio de una frase")

    def on_chunk_finished(self, chunk: Chunk) -> None:
        try:
            planned = self._planned.pop(id(chunk), None)
            if planned is None:
                return
            self._audio_ok()
            if chunk.sentences:
                self._confirm(planned, chunk.sentences[-1])
            self._request_save(force=True)
            if self._end is not None and self._player.queued_count() == 0:
                self._apply_end()
        except Exception:
            log.exception("Fallo al procesar el final de un fragmento")

    def on_output_error(self, err: Exception) -> None:
        try:
            log.warning("La salida de audio falló: %s", err, exc_info=err)
            if self._closed:
                return
            self._output_failed = True
            if not self._active or self._paused:
                return  # sin sesión, o en pausa: play() o resume() la vuelven a abrir
            if self._output_retry is not None and not self._output_retry.done():
                return  # ya hay un reintento esperando
            if self._output_retries < len(OUTPUT_RETRY_DELAYS_S):
                delay = OUTPUT_RETRY_DELAYS_S[self._output_retries]
                self._output_retries += 1
                log.info("Se reintenta abrir la salida de audio en %s s (intento %d de %d)",
                         delay, self._output_retries, len(OUTPUT_RETRY_DELAYS_S))
                self._output_retry = asyncio.create_task(
                    self._retry_output(delay), name="lector:output-retry"
                )
                return
            log.warning("La salida de audio no volvió tras los reintentos; se pausa la lectura")
            self._paused = True
            self._player.pause()
            self._refresh()
            self._request_save(force=True)
            self._notice("warn", AUDIO_OUTPUT_LOST)
        except Exception:
            log.exception("Fallo al procesar un error de la salida de audio")

    async def _retry_output(self, delay: float) -> None:
        """Espera `delay` y vuelve a abrir la salida; si falla, llega otro on_output_error."""
        await self._sleep(delay)
        if not self._closed and not self._paused and self._active:
            self._output_failed = False  # se vuelve a abrir: la vigilancia cuenta desde aquí
            self._player.resume()

    def _audio_ok(self) -> None:
        self._output_retries = 0
        self._output_failed = False

    # --- Vigilancia de la salida --------------------------------------------------------

    def _start_watchdog(self) -> None:
        if self._closed or (self._watchdog is not None and not self._watchdog.done()):
            return
        self._watchdog = asyncio.create_task(self._watch_output(), name="lector:audio-watchdog")

    async def _stop_watchdog(self) -> None:
        task, self._watchdog = self._watchdog, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task})

    def _audio_expected(self) -> bool:
        """¿Debería estar sonando audio ahora mismo?

        Solo leyendo (no en pausa ni esperando la red), con la salida abierta y algo en
        el reproductor. Mientras se sintetiza o se espera un capítulo la cola está vacía.
        """
        retrying = self._output_retry is not None and not self._output_retry.done()
        return (
            self._active
            and self._state is SessionState.READING
            and not self._paused
            and not self._output_failed
            and not retrying
            and self._player.current() is not None
        )

    async def _watch_output(self) -> None:
        """Mientras hay sesión, mira cada STALL_CHECK_S que la salida siga pidiendo audio.

        Si con audio pendiente no avanza en STALL_TIMEOUT_S, la reinicia como tras un fallo
        (reintentos incluidos) y la lectura sigue desde el inicio de la frase que sonaba.
        """
        seen: tuple[int, float] | None = None  # (muestras entregadas, desde cuándo)
        while self._active and not self._closed:
            await self._sleep(STALL_CHECK_S)
            try:
                if not self._audio_expected():
                    seen = None
                    continue
                played, now = self._player.played_samples(), self._deps.clock()
                if seen is None or played != seen[0]:
                    seen = (played, now)
                    continue
                if now - seen[1] >= STALL_TIMEOUT_S:
                    log.warning("La salida de audio no pide muestras desde hace %.0f s; "
                                "se reinicia", now - seen[1])
                    seen = None
                    self._player.restart_output()
            except Exception:
                log.exception("Fallo en la vigilancia de la salida de audio")

    # --- Avisos del gestor de motores y del navegador ----------------------------------

    def _on_engine_state(self, state: EngineState, active: str) -> None:
        self._emit("on_engine_state", state, active)
        waiting = state is EngineState.WAITING_NETWORK
        if waiting != self._engine_waiting:
            self._engine_waiting = waiting
            self._refresh()

    def _on_nav_waiting(self, waiting: bool) -> None:
        self._nav_waits = self._nav_waits + 1 if waiting else max(0, self._nav_waits - 1)
        self._refresh()

    # --- Productor ----------------------------------------------------------------------

    def _launch(self, launch: _Launch) -> None:
        if self._output_failed and not self._paused:
            # La salida cayó sin sesión (COMPLETED, NEEDS_ATTENTION, ERROR…): todo lo
            # que vuelve a leer la reabre. Si falla otra vez, sigue el camino normal
            # de on_output_error (reintentos con espera creciente y luego pausa con aviso).
            self._output_failed = False
            self._player.resume()
        self._last_launch = launch
        self._next_unqueued = (launch.target, launch.sentence)
        self._end = None
        self._relaunch = False
        self._active = True
        self._loading = not isinstance(launch.target, Chapter)
        self._refresh()
        self._producer = asyncio.create_task(self._produce(launch), name="lector:producer")
        self._start_watchdog()

    async def _produce(self, launch: _Launch) -> None:
        nav = self._session.nav
        try:
            target = launch.target
            chapter = target if isinstance(target, Chapter) else await nav.get(target)
            index = launch.sentence
            self._chapter_ready(chapter, launch)
            while True:
                await self._produce_chapter(chapter, index)
                if not self._settings.continuous:
                    self._finish(_End(SessionState.COMPLETED, ("info", CHAPTER_FINISHED),
                                      continuous_off=True))
                    return
                try:
                    nxt = await nav.next_of(chapter)
                except AmbiguousNextChapter as exc:
                    self._finish(_End(SessionState.NEEDS_ATTENTION, ("warn", exc.user_message),
                                      list(exc.candidates)))
                    return
                except ChapterLoopDetected as exc:
                    self._finish(_End(SessionState.NEEDS_ATTENTION, ("warn", exc.user_message)))
                    return
                if nxt is None:
                    self._finish(_End(SessionState.COMPLETED, ("info", NO_MORE_CHAPTERS)))
                    return
                self._chapter_index(nxt, hint=self._chapter_index(chapter) + 1)
                nav.mark_visited(nxt.ref)
                nav.start_prefetch(nxt)
                chapter, index = nxt, 0
                self._next_unqueued = (chapter, 0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._closed:
                log.debug("Productor detenido durante el cierre: %s", exc)
                return
            if isinstance(exc, EdgeVoiceUnavailable):
                # Sin voz que pueda leer: se pausa al acabar la cola, sin saltar nada.
                log.warning("No hay voz disponible para seguir; la lectura queda en pausa")
                self._request_save(force=True)
                self._finish(_End(SessionState.PAUSED, ("warn", exc.user_message), hold=True))
                return
            await self._fail(exc)

    def _chapter_ready(self, chapter: Chapter, launch: _Launch) -> None:
        nav = self._session.nav
        self._next_unqueued = (chapter, launch.sentence)
        # Cada arranque empieza un recorrido nuevo: así un salto atrás no parece un ciclo.
        nav.reset_visited(chapter.ref)
        if launch.mark_on_load:
            self._set_mark_before(chapter, launch.sentence)
            self._request_save(force=True)
        elif self._mark is not None and not isinstance(self._mark.chapter, Chapter):
            self._mark.chapter = chapter
        self._loading = False
        self._refresh()
        if launch.show:
            self._show(chapter)
        nav.start_prefetch(chapter)

    async def _produce_chapter(self, chapter: Chapter, start: int) -> None:
        """Sintetiza y encola desde `start`; si cambia el motor, vuelve a trocear."""
        sentences = chapter.sentences
        chapter_index = self._chapter_index(chapter)
        index = start
        while index < len(sentences):
            kind = self._engine_kind()
            max_chars, max_sentences = self._limits(kind)
            chunks = make_chunks(
                self._session.doc.doc_id, chapter_index, sentences,
                max_chars, max_sentences, first_sentence=index,
            )
            if not chunks:
                return
            for chunk in chunks:
                await self._produce_chunk(chunk, chapter)
                index = chunk.sentences[-1].index + 1
                self._next_unqueued = (chapter, index)
                if self._engine_kind() != kind:
                    break  # el resto se trocea con los límites del motor nuevo

    async def _produce_chunk(self, chunk: Chunk, chapter: Chapter) -> None:
        settings = self._settings
        clip, engine = await self._em.synthesize(
            chunk, settings.edge_voice, settings.piper_voice, settings.speed
        )
        queued = _queued_clip(chunk, clip, engine)
        if queued is None:
            log.warning("El motor %s no devolvió audio para %s; se salta", engine, chunk.chunk_id)
            return
        self._planned[id(chunk)] = _Planned(chunk, chapter)
        try:
            accepted = await self._player.enqueue(queued)
        except BaseException:
            self._planned.pop(id(chunk), None)
            raise
        if not accepted:
            self._planned.pop(id(chunk), None)
            raise RuntimeError("El reproductor descartó un fragmento sin cancelar el productor")

    def _engine_kind(self) -> str:
        return "piper" if self._em.state in _PIPER_STATES else "edge"

    def _limits(self, kind: str) -> tuple[int, int]:
        s = self._settings
        if kind == "piper":
            return s.piper_max_chars, 1
        return s.edge_max_chars, s.edge_max_sentences

    def _finish(self, end: _End) -> None:
        """Fin del productor: se aplica cuando termina de sonar lo que queda en cola.

        Hasta entonces la sesión sigue activa: pausa, reanudar y detener funcionan.
        """
        self._end = end
        if self._player.queued_count() == 0:
            self._apply_end()

    def _apply_end(self) -> None:
        end, self._end = self._end, None
        if end is None:
            return
        if end.hold:
            self._loading = False
            self._relaunch = True
            if not self._paused:
                self._paused = True
                self._player.pause()
            self._refresh()
            self._request_save(force=True)
            if end.notice is not None:
                self._notice(*end.notice)
            return
        self._active = False
        self._loading = False
        self._set_state(end.state)
        self._request_save(force=True)
        if end.notice is not None:
            self._notice(*end.notice)
        if end.candidates is not None:
            self._emit("on_needs_choice", list(end.candidates))

    async def _fail(self, exc: Exception) -> None:
        """El productor murió: se guarda ya y el ERROR llega cuando acabe la cola."""
        log.error("La lectura se detuvo por un error inesperado", exc_info=exc)
        try:
            await self._save_now()
        except Exception:
            log.exception("No se pudo guardar el progreso tras el error")
        message = exc.user_message if isinstance(exc, LectorError) else LectorError.user_message
        self._finish(_End(SessionState.ERROR, ("error", message)))

    async def _cancel_producer(self) -> None:
        task, self._producer = self._producer, None
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.wait({task})

    async def _halt_locked(self) -> None:
        """Fin de la sesión actual: productor, navegaciones y audio; la marca pasa a la
        frase que sonaba."""
        self._epoch += 1
        await self._cancel_producer()  # antes de clear(): ningún enqueue se cuela después
        await self._cancel_navigation()
        retry, self._output_retry = self._output_retry, None
        if retry is not None and not retry.done():
            retry.cancel()  # play() o resume() reabren la salida si hace falta
            await asyncio.wait({retry})
        await self._stop_watchdog()
        playing = self._playing()
        self._player.clear()
        self._planned.clear()
        self._end = None
        self._relaunch = False
        if playing is not None:
            self._set_mark_before(*playing)
        self._active = False
        self._loading = False

    async def _navigate(self, coro):
        """Ejecuta una espera de navegación manual que stop(), shutdown() o play() cancelan.

        Devuelve `_ABORTED` si la cancelaron ellos (no quien llamó).
        """
        task = asyncio.create_task(coro, name="lector:navigate")
        self._nav_tasks.add(task)
        try:
            return await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if task.cancelled() and not (current is not None and current.cancelling()):
                return _ABORTED
            raise
        finally:
            self._nav_tasks.discard(task)

    async def _cancel_navigation(self) -> None:
        tasks = [t for t in self._nav_tasks if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks)
        self._nav_waits = 0  # una sesión nueva nunca hereda un WAITING_NETWORK

    async def _seek(self, target_fn) -> None:
        async with self._lock:
            if self._controllable():
                await self._restart_locked(target_fn)

    async def _jump(self, chapter: Chapter, epoch: int) -> None:
        async with self._lock:
            if epoch == self._epoch and self._controllable():
                await self._restart_locked(lambda base: (chapter, 0))

    async def _restart_locked(self, target_fn) -> None:
        """Corta el audio y vuelve a empezar en la frase que devuelve `target_fn(base)`.

        `base` es `(capítulo, frase)` de lo que suena o, si no suena nada, de la
        reanudación. Guarda la posición nueva en cuanto se decide.
        """
        self._epoch += 1
        await self._cancel_producer()
        target = target_fn(self._base())
        if target is None:  # el primer capítulo aún no ha cargado: nada que mover
            if self._last_launch is not None:
                self._launch(self._last_launch)
            return
        self._player.clear()
        self._planned.clear()
        chapter, index = target
        index = max(0, min(index, len(chapter.sentences)))
        self._set_mark_before(chapter, index)
        self._launch(_Launch(chapter, index, True, False))
        await self._save_now()  # la posición nueva queda guardada al volver

    async def _apply_change(self, apply_now: bool) -> None:
        """Voz o velocidad nuevas (§5.6).

        Con «Aplicar ya» se corta y se sigue desde el inicio de la frase que sonaba.
        Si no, el clip que suena termina igual y la cola preparada se descarta y se
        regenera desde su primera frase.
        """
        async with self._lock:
            if not self._active:
                return
            if apply_now:
                await self._restart_locked(lambda base: base)
                return
            producing = self._producer is not None and not self._producer.done()
            await self._cancel_producer()  # antes de descartar: nada se cuela después
            restart_at = None
            for chunk in self._player.drop_queued():
                planned = self._planned.pop(id(chunk), None)
                if restart_at is None and planned is not None:
                    restart_at = (planned.chapter, chunk.sentences[0].index)
            if restart_at is None and producing and self._next_unqueued is not None:
                restart_at = self._next_unqueued
            if restart_at is None:
                return  # solo queda el clip que suena: no hay nada que regenerar
            target, index = restart_at
            if isinstance(target, Chapter):
                self._launch(_Launch(target, index, False, False))
            elif self._last_launch is not None:
                self._launch(self._last_launch)

    async def _remember_next(self, chapter: Chapter, ref: ChapterRef) -> None:
        chapter.next_ref = ref  # también en memoria: el navegador ya no vuelve a preguntar
        chapter.next_candidates = []
        store = self._settings.store_text
        try:
            await self._text_writes.run(
                self._repos.chapters.save, self._session.doc.doc_id, chapter, store,
                with_text=store,
            )
        except Exception:
            log.warning("No se pudo recordar el capítulo elegido", exc_info=True)

    # --- Posición -----------------------------------------------------------------------

    def _playing(self) -> tuple[Chapter, int] | None:
        """(capítulo, frase) de lo que el reproductor está entregando, si hay algo."""
        current = self._player.current()
        if current is None:
            return None
        chunk, index = current
        planned = self._planned.get(id(chunk))
        return (planned.chapter, index) if planned is not None else None

    def _base(self) -> tuple[Chapter, int] | None:
        playing = self._playing()
        if playing is not None:
            return playing
        mark = self._mark
        if mark is not None and isinstance(mark.chapter, Chapter):
            return mark.chapter, resume_sentence(mark.position)
        return None

    def _controllable(self) -> bool:
        return (
            not self._closed
            and self._session is not None
            and self._state not in (SessionState.IDLE, SessionState.STOPPED)
        )

    def _confirm(self, planned: _Planned, sentence: Sentence) -> None:
        position = Position(
            chapter_index=planned.chunk.chapter_index,
            chapter_locator=planned.chapter.ref.locator,
            paragraph_index=sentence.paragraph_index,
            sentence_index=sentence.index,
            char_offset=sentence.end,
            page=self._page_of(planned.chapter, sentence),
        )
        self._mark = _Mark(planned.chapter, position, planned.chunk.chunk_id)

    def _set_mark_before(self, chapter: Chapter, index: int) -> None:
        """Marca como confirmada la frase anterior a `index` (-1 si es la primera)."""
        sentences = chapter.sentences
        index = max(0, min(index, len(sentences)))
        if index == 0:
            first = sentences[0] if sentences else None
            paragraph = first.paragraph_index if first else 0
            sentence_index, offset = -1, 0
            page = self._page_of(chapter, first) if first else None
        else:
            prev = sentences[index - 1]
            paragraph, sentence_index, offset = prev.paragraph_index, prev.index, prev.end
            page = self._page_of(chapter, prev)
        position = Position(
            self._chapter_index(chapter), chapter.ref.locator, paragraph, sentence_index, offset, page
        )
        self._mark = _Mark(chapter, position, None)

    @staticmethod
    def _page_of(chapter: Chapter, sentence: Sentence) -> int | None:
        if 0 <= sentence.paragraph_index < len(chapter.paragraphs):
            return chapter.paragraphs[sentence.paragraph_index].page
        return sentence.page

    def _chapter_index(self, chapter: Chapter, hint: int | None = None) -> int:
        """Índice del capítulo; si la fuente no lo sabe, se deduce del vecino."""
        ref = chapter.ref
        if ref.index is not None:
            return ref.index
        known = self._chapter_indexes.get(ref.locator)
        if known is not None:
            return known
        doc = self._session.doc if self._session else None
        value = next(
            (r.index for r in (doc.chapters if doc else [])
             if r.locator == ref.locator and r.index is not None),
            None,
        )
        if value is None:
            value = hint if hint is not None and hint >= 0 else 0
        self._chapter_indexes[ref.locator] = value
        return value

    def _show(self, chapter: Chapter) -> None:
        if chapter.ref.locator != self._shown_locator and self._session is not None:
            self._shown_locator = chapter.ref.locator
            self._emit("on_document", self._session.doc, chapter)

    # --- Sesión y documentos -----------------------------------------------------------

    async def _session_for(self, doc_id: str) -> _Session:
        if self._session is not None and self._session.doc.doc_id == doc_id:
            return self._session
        first: Chapter | None = None
        if self._preview is not None and self._preview[1].doc_id == doc_id:
            source, doc, first = self._preview
        else:
            doc = await asyncio.to_thread(self._repos.documents.get, doc_id)
            if doc is None:
                raise LectorError(UNKNOWN_DOCUMENT)
            if doc.kind is SourceKind.URL:
                # Sin red: el texto guardado basta y la fuente real se abre al necesitarla.
                source = DeferredSource(
                    lambda locator, _fetcher: self._open_source(locator), self._deps.fetcher, doc
                )
            else:
                stat = await asyncio.to_thread(self._repos.documents.file_stat, doc_id)
                await check_file(doc, stat, self._repos.documents)
                source, opened, first = await self._open_source(doc.origin)
                if opened.doc_id != doc_id:
                    log.warning("La fuente devolvió otro id (%s); se conserva %s", opened.doc_id, doc_id)
                doc = replace(opened, doc_id=doc_id)
        if self._session is not None:
            await self._session.nav.close()
        nav = self._make_navigator(source, doc)
        nav.on_waiting = self._on_nav_waiting
        self._nav_waits = 0
        self._chapter_indexes.clear()
        self._preview = None
        self._session = _Session(doc, source, nav, first)
        return self._session

    async def _open_source(self, locator: str):
        """Abre la fuente aplicando el prefijo de obra que el usuario corrigió para ese host."""
        try:
            overrides = await asyncio.to_thread(self._deps.settings.get, "work_prefix_overrides")
        except Exception:
            log.warning("No se pudieron leer los prefijos corregidos", exc_info=True)
            overrides = None
        return await self._deps.source_opener(
            locator, self._deps.fetcher, prefix_overrides=overrides or {}
        )

    def _make_navigator(self, source: ContentSource, doc: Document) -> ChapterNavigator:
        if self._deps.navigator_factory is not None:
            return self._deps.navigator_factory(source, doc)
        s = self._settings
        return ChapterNavigator(
            source, doc, self._repos.chapters, s.store_text,
            prefetch_ahead=s.prefetch_ahead, wait_max_s=s.wait_max_s,
        )

    def _target_for(self, session: _Session, locator: str, index: int | None) -> Chapter | ChapterRef:
        if session.first is not None and session.first.ref.locator == locator:
            return session.first
        for ref in session.doc.chapters:
            if ref.locator == locator:
                return ref
        return ChapterRef(index, "", locator)

    def _first_target(self, session: _Session) -> Chapter | ChapterRef:
        if session.first is not None:
            return session.first
        if session.doc.chapters:
            return session.doc.chapters[0]
        return ChapterRef(None, "", session.doc.origin)

    # --- Configuración -------------------------------------------------------------------

    async def _ensure_settings(self) -> _Settings:
        if self._settings is None:
            self._settings = await asyncio.to_thread(_read_settings, self._deps.settings)
        return self._settings

    async def _persist_setting(self, key: str, value: Any) -> None:
        try:
            await asyncio.to_thread(self._deps.settings.set, key, value)
        except Exception:
            log.warning("No se pudo guardar el ajuste %s", key, exc_info=True)

    # --- Estado y avisos -------------------------------------------------------------------

    def _refresh(self) -> None:
        if not self._active:
            return
        if self._paused:
            state = SessionState.PAUSED
        elif self._nav_waits or self._engine_waiting:
            state = SessionState.WAITING_NETWORK
        elif self._loading:
            state = SessionState.LOADING
        else:
            state = SessionState.READING
        self._set_state(state)

    def _set_state(self, state: SessionState) -> None:
        if state is self._state:
            return
        self._state = state
        self._emit("on_session_state", state)
        if state is SessionState.WAITING_NETWORK:
            self._request_save(force=True)

    def _emit(self, name: str, *args) -> None:
        try:
            getattr(self._events, name)(*args)
        except Exception:
            log.exception("Fallo en el aviso %s hacia la interfaz", name)

    def _notice(self, level: str, message: str) -> None:
        self._emit("on_notice", level, message)

    # --- Progreso (la escritura vive en ProgressWriter) ----------------------------------

    def _request_save(self, force: bool = False) -> None:
        if self._mark is not None and self._session is not None:
            self._progress.request(force)

    async def _save_now(self) -> None:
        self._request_save(force=True)
        await self._progress.flush()

    def _build_record(self) -> ProgressRecord | None:
        mark, session, settings = self._mark, self._session, self._settings
        if mark is None or session is None or settings is None:
            return None
        return make_record(
            session.doc, mark.position, mark.chunk_id,
            edge_voice=settings.edge_voice, piper_voice=settings.piper_voice,
            speed=settings.speed, session_state=str(self._state),
        )

    def _on_save_error(self, exc: Exception) -> None:
        message = exc.user_message if isinstance(exc, LectorError) else LectorError.user_message
        self._notice("warn", message)
