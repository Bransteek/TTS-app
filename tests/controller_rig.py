"""Montaje del controlador de lectura para pruebas: base de datos real en tmp_path,
motores y salida de audio falsos, y fuentes en memoria."""
from __future__ import annotations

import asyncio
from collections.abc import Callable

from lector.audio.player import AudioPlayer
from lector.content.registry import open_source
from lector.content.segmenter import build_chapter_text
from lector.errors import PageUnreachable
from lector.models import Chapter, ChapterRef, Document, SourceKind
from lector.persistence.database import open_database
from lector.persistence.repositories import ProgressRecord, SettingsRepo
from lector.services.navigator import ChapterNavigator
from lector.services.reading_controller import ControllerDeps, ReadingController, Repos
from lector.tts.engine_manager import EngineConfig, EngineManager
from tests.fakes import (
    SAMPLES_PER_SENTENCE, FakeClock, FakeEdge, FakeOutput, FakePiper, FakeSource, ManualClock,
)

STORY_ID = "novela"
WAIT_S = 2.0  # tope de tiempo real de cada espera: una regresión falla, no se cuelga


async def until(cond: Callable[[], bool], timeout: float = WAIT_S) -> None:
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.001)


async def settle(rounds: int = 50) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


def lector_tasks() -> list[asyncio.Task]:
    return [t for t in asyncio.all_tasks() if t.get_name().startswith("lector:")]


# --- historia en memoria ----------------------------------------------------------


def story_ref(i: int) -> ChapterRef:
    return ChapterRef(i, f"Cap {i}", f"https://novela.example/cap-{i}/")


def story_chapter(
    i: int, sentences: int = 10, *, last: int | None = None, texts: list[str] | None = None
) -> Chapter:
    """Capítulo `i` con un único párrafo de frases «Frase k del capítulo i.»."""
    texts = texts or [f"Frase {k} del capítulo {i}." for k in range(sentences)]
    text, paras, sents = build_chapter_text([(" ".join(texts), None)])
    nxt = story_ref(i + 1) if last is None or i < last else None
    prev = story_ref(i - 1) if i > 1 else None
    return Chapter(story_ref(i), f"Cap {i}", text, paras, sents, nxt, prev)


def story(chapters: int = 3, sentences: int = 10) -> dict[str, Chapter]:
    return {
        story_ref(i).locator: story_chapter(i, sentences, last=chapters)
        for i in range(1, chapters + 1)
    }


def story_document(chapters: dict[str, Chapter]) -> Document:
    refs = [c.ref for c in chapters.values()]
    return Document(STORY_ID, SourceKind.URL, "https://novela.example/", "Novela",
                    "https://novela.example/", "https://novela.example/", None, refs)


def fake_opener(source: FakeSource, document: Document, network: dict | None = None):
    """Abre la fuente en memoria; con `network["online"]` a False falla como sin red."""

    async def opener(locator: str, fetcher):
        if network is not None and not network["online"]:
            raise PageUnreachable()
        return source, document, source.chapters[locator]

    return opener


class _Tap:
    """Reenvía los eventos del reproductor al controlador y anota qué fragmento sonó."""

    def __init__(self, target, played: list[tuple[str, int]]):
        self._target = target
        self._played = played

    def on_sentence_started(self, chunk, sentence_index) -> None:
        self._played.append((chunk.chunk_id, sentence_index))
        self._target.on_sentence_started(chunk, sentence_index)

    def on_chunk_finished(self, chunk) -> None:
        self._target.on_chunk_finished(chunk)

    def on_output_error(self, err) -> None:
        self._target.on_output_error(err)


# --- eventos ----------------------------------------------------------------------


class EventLog:
    def __init__(self) -> None:
        self.states: list = []
        self.engine: list = []
        self.documents: list = []
        self.sentences: list[tuple[str, str, int, int]] = []
        self.notices: list[tuple[str, str]] = []
        self.choices: list = []

    def on_session_state(self, state) -> None:
        self.states.append(state)

    def on_engine_state(self, state, active) -> None:
        self.engine.append((state, active))

    def on_document(self, doc, chapter) -> None:
        self.documents.append((doc, chapter))

    def on_sentence(self, chapter_title, sentence_text, sentence_index, total) -> None:
        self.sentences.append((chapter_title, sentence_text, sentence_index, total))

    def on_notice(self, level, message) -> None:
        self.notices.append((level, message))

    def on_needs_choice(self, candidates) -> None:
        self.choices.append(candidates)

    @property
    def heard(self) -> list[int]:
        return [s[2] for s in self.sentences]

    @property
    def heard_at(self) -> list[tuple[str, int]]:
        return [(s[0], s[2]) for s in self.sentences]


# --- montaje ----------------------------------------------------------------------


class ControllerRig:
    def __init__(self, db_path, *, opener=None, fetcher=None, clock=None, **nav_options):
        self.clock = clock or FakeClock()
        self.conn = open_database(db_path)
        self.repos = Repos.from_connection(self.conn)
        self.settings = SettingsRepo(self.conn)
        self.edge = FakeEdge(clock=self.clock)
        self.piper = FakePiper()
        # Si se activa, las esperas del gestor (reintentos) se quedan bloqueadas hasta abrirla.
        self.engine_gate: asyncio.Event | None = None

        async def engine_sleep(seconds: float) -> None:
            if self.engine_gate is not None:
                await self.engine_gate.wait()
            await self.clock.sleep(seconds)

        self.manager = EngineManager(
            self.edge, self.piper, EngineConfig(), clock=self.clock.now, sleep=engine_sleep
        )
        # Reloj propio del controlador (guardado cada 2 s, espera antes de reabrir la
        # salida): solo avanza cuando la prueba llama a save_clock.advance().
        self.save_clock = ManualClock()
        self.out = FakeOutput()
        self.player = AudioPlayer(self.out, asyncio.get_running_loop(), max_ahead=2)
        self.events = EventLog()

        def navigator(source, document):
            return ChapterNavigator(
                source, document, self.repos.chapters, True, sleep=self.clock.sleep, **nav_options
            )

        deps = ControllerDeps(
            fetcher=fetcher,
            repos=self.repos,
            engine_manager=self.manager,
            player=self.player,
            settings=self.settings,
            source_opener=opener or open_source,
            navigator_factory=navigator,
            clock=self.save_clock.now,
            sleep=self.save_clock.sleep,
        )
        self.ctl = ReadingController(deps, self.events)
        self.played: list[tuple[str, int]] = []  # (chunk_id, frase) de cada frase que empezó
        self.player.set_events(_Tap(self.ctl, self.played))

    async def start(self, locator: str, start=None) -> Document:
        doc, _, _ = await asyncio.wait_for(self.ctl.open(locator), WAIT_S)
        await self.play(doc.doc_id, start)
        return doc

    async def play(self, doc_id: str = STORY_ID, start=None) -> None:
        await asyncio.wait_for(self.ctl.play(doc_id, start), WAIT_S)

    async def stop(self) -> None:
        await asyncio.wait_for(self.ctl.stop(), WAIT_S)

    def stored(self, doc_id: str = STORY_ID) -> ProgressRecord | None:
        """Lo que hay en la base de datos ahora mismo (sin forzar ninguna escritura)."""
        return self.repos.progress.get(doc_id)

    async def written(self, cond: Callable[[ProgressRecord], bool], doc_id: str = STORY_ID) -> None:
        """Espera (con tope) a que la base de datos contenga un progreso que cumpla `cond`."""

        def check() -> bool:
            record = self.repos.progress.get(doc_id)
            return record is not None and cond(record)

        await until(check)

    async def fill(self) -> None:
        """Espera a que el productor llene la cola (el que suena + 2 preparados)."""
        await until(lambda: self.player.queued_count() == 3)
        await settle()

    async def hear(self, sentences: int = 1, *, partial: bool = False) -> None:
        """Hace sonar `sentences` frases; con `partial`, la última solo hasta la mitad.

        Cada `pull` cruza exactamente una marca de frase, así que tras una frase a
        medias solo se puede seguir con frases enteras (o con un salto, que vuelve a
        empezar el audio desde cero).
        """
        for n in range(sentences):
            frames = SAMPLES_PER_SENTENCE
            if partial and n == sentences - 1:
                frames //= 2
            await until(lambda: self.player.queued_count() > 0)
            before = len(self.events.sentences)
            self.out.pull(frames)
            await until(lambda: len(self.events.sentences) > before)
            await settle(5)

    async def saved(self, doc_id: str = STORY_ID) -> ProgressRecord | None:
        await asyncio.wait_for(self.ctl.flush_progress(), WAIT_S)
        return self.repos.progress.get(doc_id)

    def producers(self) -> int:
        return sum(1 for t in lector_tasks() if t.get_name() == "lector:producer" and not t.done())

    async def close(self) -> None:
        try:
            await asyncio.wait_for(self.ctl.shutdown(), WAIT_S)
        finally:
            self.conn.close()


class Outage(list):
    """Cola de fallos para FakeSource que nunca se agota: corte de red hasta sustituirla."""

    def __init__(self, error: Exception):
        super().__init__([error])
        self.error = error

    def pop(self, index: int = -1):
        return self.error


def story_rig(build, chapters: int = 3, sentences: int = 10, **kwargs):
    """Rig con una historia de `chapters` capítulos servida por una FakeSource."""
    content = story(chapters, sentences)
    source = FakeSource(content)
    network = {"online": True}
    document = story_document(content)
    rig = build(opener=fake_opener(source, document, network), **kwargs)
    rig.source = source
    rig.document = document  # lo que devuelve el sitio al abrirlo
    rig.network = network
    return rig
