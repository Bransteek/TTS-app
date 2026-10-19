# Lector TTS de novelas — Plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Objetivo:** Aplicación de escritorio en PySide6 que lee en voz alta novelas web por capítulos, PDF y EPUB con Edge TTS. Cambia sola a Piper local si Edge falla, sigue funcionando minimizada en la bandeja y reanuda en la última frase escuchada.

**Arquitectura:**
- Tres hilos:
  - Hilo principal de Qt: solo la interfaz.
  - Hilo de trabajo con su propio bucle `asyncio`: descargas, extracción, motores TTS y `ReadingController`.
  - Hilo de PortAudio: el *callback* de sounddevice, que solo copia PCM.
- Comunicación entre hilos:
  - De la interfaz al trabajo: `AsyncWorker.submit()`.
  - Del trabajo a la interfaz: señales de Qt, emitidas por `QtEventBridge`.
- Todo el audio se normaliza a PCM int16 mono a 24 kHz en memoria y nunca se escribe en disco.

**Stack técnico:** Python 3.12, PySide6 6.12.0, edge-tts 7.3.1, piper-tts 1.8.0, PyMuPDF 1.28.2, trafilatura 2.3.1, lxml 6.1.3 + cssselect 1.6.0, httpx 0.28.1, miniaudio 1.71, sounddevice 0.5.6, numpy 2.5.4, SQLite (stdlib). Pruebas: pytest 9.1.1, pytest-asyncio 1.4.0, pytest-qt 4.5.0.

**Spec:** `docs/superpowers/specs/2026-10-16-lector-tts-design.md` (manda) y `docs/superpowers/specs/2026-10-16-especificacion-original.md` (requisitos de detalle §1–18). Léelos ambos antes de empezar.

## Restricciones globales

- **Python:** `>=3.12`. En el Mac de desarrollo (arm64, macOS 27) hay que instalarlo con `brew install python@3.12` y crear un venv en `.venv`. **Nunca** se usa el Python 3.9 del sistema.
- **Versiones fijadas** en `requirements.txt` exactamente como en «Stack técnico». Las de desarrollo van en `requirements-dev.txt`.
- **Paquete:** `lector/` en la raíz del repo. El punto de entrada es `main.py` en la raíz.
- **Idioma:** todo texto visible para el usuario va en español y en lenguaje no técnico. Los identificadores de código van en inglés.
- **Voces por defecto:**
  - Edge: `es-MX-DaliaNeural`, idioma `es-MX`, velocidad `1.0`, motor preferido Edge.
  - Piper: `es_ES-sharvard-medium` con hablante `F`.
- **Audio:** nunca se escribe audio en disco, ni MP3, ni WAV, ni archivos temporales. La frecuencia de salida es `OUTPUT_RATE = 24000`.
- **Datos de la app:**
  - Windows: `%APPDATA%\LectorTTS\`.
  - macOS: `~/Library/Application Support/LectorTTS/`.
  - La variable de entorno `LECTOR_DATA_DIR` sobrescribe la ubicación (se usa en las pruebas).
- **Valores por defecto** de la tabla `settings` (definidos en Task 8). Son los números del diseño:
  - Fragmentos: `edge_max_chars=600`, `edge_max_sentences=3`, `piper_max_chars=300`.
  - Reintentos de Edge: `edge_retries=3` con esperas de `1, 2, 4` s y `edge_timeout_s=15`.
  - Vuelta a Edge: `recovery_initial_s=60`, `recovery_max_s=600` y `piper_cooldown_s=120`.
  - Esperas sin red y precarga: `wait_max_s=60` y `prefetch_ahead=2`.
  - Cola de audio: `queue_ahead_chunks=2` y `queue_max_seconds=90`.
  - Interfaz y lectura: `skip_seconds=10`, `continuous=true`, `store_text=true`.
  - Descargas: `http_max_bytes=5_000_000` y `http_max_redirects=5`.
- **Pruebas sin red:** las pruebas nunca tocan la red. Se usan *fixtures* HTML guardadas y motores simulados.
- **Commits:**
  - Un commit por tarea, con el mensaje en español en imperativo.
  - Los commits terminan con `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.
  - No se hace `git push` sin pedirlo al usuario.

## Foco de revisión

Casos que el diseño implica y que es más probable que fallen en uso real. Cada uno tiene su prueba en la tarea indicada.

1. **Corte de red justo en el cambio de capítulo** (el capítulo N terminó y el N+1 aún no está descargado). La lectura debe quedar en `WAITING_NETWORK` y reanudarse sola desde la primera frase del N+1, sin repetir el final del N. *(Task 13, `test_waits_and_resumes_when_next_chapter_arrives_late`)*
2. **Edge tiene marcas de frase distintas a las del segmentador** (abreviaturas como «Sr.», diálogos «—¿Qué?—dijo»). El progreso no puede saltarse frases ni marcar como escuchada una frase que no sonó. *(Task 10, `test_marks_align_when_edge_merges_sentences`)*
3. **Se pulsa Pausa durante el cambio de motor o mientras se espera la red.** Al reanudar no deben sonar dos fragmentos a la vez ni quedar tareas huérfanas. *(Task 14, `test_pause_during_fallback_has_single_audio_stream`)*
4. **Sesión de varias horas.** La memoria no debe crecer: los clips reproducidos se liberan y la precarga no acumula más de 2 capítulos. *(Task 9, `test_played_clips_are_released`; Task 13, `test_prefetch_cache_is_bounded`)*
5. **Se reabre la app tras un cierre forzado a mitad de una frase.** Se reanuda en esa frase, no en el fragmento ni en el párrafo. *(Task 14, `test_restart_resumes_at_last_confirmed_sentence`)*

---

## Estructura de archivos

```
main.py                         # arranque: QApplication, worker, ventana, bandeja
requirements.txt / requirements-dev.txt / pyproject.toml (solo config pytest) / .gitignore
lector/
  __init__.py
  models.py                     # dataclasses de dominio (Task 1)
  errors.py                     # jerarquía de errores con mensajes en español (Task 1)
  paths.py                      # carpeta de datos por plataforma (Task 1)
  content/
    segmenter.py                # párrafos → frases → fragmentos (Task 2)
    base.py                     # protocolo ContentSource + helpers HTML (Task 4)
    madara.py                   # adaptador Madara (Task 4)
    generic_web.py              # trafilatura + detección de «siguiente» (Task 5)
    pdf_source.py               # PyMuPDF (Task 6)
    epub_source.py              # zipfile + lxml (Task 7)
    registry.py                 # elige el adaptador para una entrada (Task 7)
  security/
    url_validator.py            # validación anti-SSRF (Task 3)
    http_client.py              # SafeFetcher: descargas validadas (Task 3)
  persistence/
    database.py                 # conexión, esquema, recuperación de corrupción (Task 8)
    repositories.py             # Documents/Chapters/Progress/Settings/ChunkLog/Voices (Task 8)
  audio/
    pcm.py                      # decodificar MP3, remuestrear (Task 9)
    player.py                   # AudioPlayer + PlaybackQueue + salidas (Task 9)
  tts/
    base.py                     # TtsEngine, AudioClip, speed mapping (Task 10)
    edge_engine.py              # (Task 10)
    piper_engine.py             # + PiperModelManager (Task 11)
    engine_manager.py           # máquina de estados y circuit breaker (Task 12)
  services/
    navigator.py                # ChapterNavigator + Prefetcher (Task 13)
    reading_controller.py       # orquestación de la sesión (Task 14)
    worker.py                   # AsyncWorker + QtEventBridge (Task 15)
    recovery.py                 # oferta de reanudar, relocalizar archivos (Task 19)
  ui/
    main_window.py, playback_controls.py, preview_panel.py,
    resume_dialog.py, history_dialog.py (Task 16)
    tray.py (Task 17)
    settings_dialog.py, piper_setup.py (Task 18)
tests/
  fixtures/madara/{capitulo-1,capitulo-99,capitulo-100,chapters_ajax}.html
  fixtures/generic/*.html, fixtures/files/ (generados en conftest)
  fakes.py                      # FakeEdge, FakePiper, FakeClock, FakeOutput, FakeSource
  conftest.py, test_*.py
```

---

### Task 1: Esqueleto, modelos de dominio, errores y rutas

**Archivos:**
- Crear: `pyproject.toml`, `requirements.txt`, `requirements-dev.txt`, `.gitignore`, `lector/__init__.py` y un `__init__.py` en cada subpaquete, `lector/models.py`, `lector/errors.py`, `lector/paths.py`.
- Pruebas: `tests/test_models_errors.py`.

**Interfaces:**
- **Produce** en `lector/models.py`. Todas son dataclasses `frozen=True`, salvo `Chapter` y `Document`:
  - `SourceKind(StrEnum)`: `URL="url"`, `PDF="pdf"`, `EPUB="epub"`.
  - `ChapterRef(index: int | None, title: str, locator: str)`. `locator` es la URL absoluta del capítulo (web) o `"spine:<href>"` / `"pages:<start>-<end>"` (archivos).
  - `Sentence(index: int, paragraph_index: int, text: str, start: int, end: int, page: int | None)`. `index` es global dentro del capítulo. `start`/`end` son desplazamientos en `Chapter.text`.
  - `Paragraph(index: int, text: str, start: int, page: int | None)`.
  - `Chapter(ref: ChapterRef, title: str, text: str, paragraphs: list[Paragraph], sentences: list[Sentence], next_ref: ChapterRef | None, prev_ref: ChapterRef | None, next_candidates: list[ChapterRef])`. `next_candidates` solo tiene elementos cuando el «siguiente» es ambiguo.
  - `Document(doc_id: str, kind: SourceKind, origin: str, title: str, canonical_url: str | None, work_prefix: str | None, file_hash: str | None, chapters: list[ChapterRef])`.
  - `Chunk(chunk_id: str, doc_id: str, chapter_index: int, paragraph_index: int, sentences: tuple[Sentence, ...], text: str, start: int, end: int)`.
    - `chunk_id` tiene el formato `f"{doc_id}:{chapter_index}:{paragraph_index}:{first.index}-{last.index}"`.
  - `Position(chapter_index: int, chapter_locator: str, paragraph_index: int, sentence_index: int, char_offset: int, page: int | None)`.
- **Produce** en `lector/errors.py`: `LectorError(Exception)` con:
  - `user_message: str` (en español).
  - `recoverable: bool`.

  Subclases, con estos nombres exactos:

  | Clase | Recuperable |
  |---|---|
  | `InvalidUrl` | no |
  | `PageUnreachable` | sí |
  | `NoExtractableText` | no |
  | `NextChapterNotFound` | no |
  | `ChapterLoopDetected` | no |
  | `AmbiguousNextChapter(candidates: list[ChapterRef])` | no |
  | `DamagedDocument` | no |
  | `ScannedPdf` | no |
  | `EdgeUnavailable` | sí |
  | `EdgeVoiceNotFound` | no |
  | `PiperModelMissing` | no |
  | `SynthesisFailed` | sí |
  | `AudioOutputError` | sí |
  | `ResourceExhausted` | no |
  | `DatabaseLocked` | sí |
  | `DatabaseCorrupt` | no |
  | `FileMovedOrChanged(doc_id: str)` | no |

- **Produce** en `lector/paths.py`: `data_dir() -> Path` (crea la carpeta si no existe), `models_dir() -> Path` (`data_dir()/"piper-voices"`) y `db_path() -> Path` (`data_dir()/"lector.db"`).

- [ ] **Paso 1:** Instalar Python 3.12 (`brew install python@3.12`) y crear el entorno con `/opt/homebrew/bin/python3.12 -m venv .venv`. Escribir `requirements*.txt` con las versiones fijadas y ejecutar `.venv/bin/pip install -r requirements.txt -r requirements-dev.txt`.
  - Esperado: la instalación termina sin errores y `.venv/bin/python -c "import PySide6, edge_tts, piper, fitz, trafilatura, miniaudio, sounddevice"` no imprime nada.
- [ ] **Paso 2:** Escribir `pyproject.toml` con `[tool.pytest.ini_options]`: `testpaths=["tests"]`, `asyncio_mode="auto"`, `qt_api="pyside6"`. Escribir `.gitignore` con `.venv/`, `__pycache__/`, `*.db` y `.pytest_cache/`.
- [ ] **Paso 3:** Escribir las pruebas que deben fallar en `tests/test_models_errors.py`:
  - `test_every_error_has_spanish_message`: instancia cada subclase y comprueba `isinstance(e, LectorError)`, que `e.user_message` no esté vacío y que no contenga las palabras `"Exception"` ni `"Traceback"`.
  - `test_recoverable_flags`: comprueba que `PageUnreachable().recoverable is True` y `DatabaseCorrupt().recoverable is False`.
  - `test_data_dir_honours_env(tmp_path, monkeypatch)`: con `LECTOR_DATA_DIR` apuntando a `tmp_path`, comprueba que `db_path() == tmp_path/"lector.db"` y que `models_dir()` existe.
  - `test_chunk_id_format`: comprueba el formato de `chunk_id` con un `Chunk` construido a mano.
- [ ] **Paso 4:** Ejecutar `.venv/bin/pytest tests/test_models_errors.py -v`. Esperado: fallan con `ModuleNotFoundError`.
- [ ] **Paso 5:** Implementar los tres módulos. `paths.data_dir()` usa `%APPDATA%` en Windows y `~/Library/Application Support` en macOS. En cualquier otro sistema usa `$XDG_DATA_HOME` o `~/.local/share`. Construye el `chunk_id` una función `make_chunk_id(doc_id, chapter_index, paragraph_index, first, last) -> str`.
- [ ] **Paso 6:** Ejecutar las pruebas. Esperado: PASS.
- [ ] **Paso 7:** Commit `"Crear esqueleto del proyecto, modelos y errores"`.

### Task 2: Segmentador de texto

**Archivos:** Crear `lector/content/segmenter.py`. Pruebas: `tests/test_text_segmenter.py`.

**Interfaces:**
- **Consume:** `Paragraph`, `Sentence`, `Chunk` y `make_chunk_id` (Task 1).
- **Produce:**
  - `build_chapter_text(paragraph_texts: list[tuple[str, int | None]]) -> tuple[str, list[Paragraph], list[Sentence]]`. Cada tupla es `(texto, página)`. Une los párrafos con `"\n\n"` y calcula los desplazamientos.
  - `split_sentences(text: str) -> list[tuple[int, int]]`: devuelve los rangos `(start, end)` de cada frase dentro de `text`.
  - `make_chunks(doc_id: str, chapter_index: int, sentences: list[Sentence], max_chars: int, max_sentences: int, first_sentence: int = 0) -> list[Chunk]`. Los fragmentos nunca cruzan un párrafo. El primer fragmento del resultado tiene como máximo **1 frase**, para que el audio empiece rápido.

**Reglas de corte de frases:**
- Se corta tras `.`, `!`, `?`, `…` o `...`, opcionalmente seguidos de comillas o paréntesis de cierre (`»`, `”`, `"`, `)`), cuando después viene espacio y una mayúscula, `¿`, `¡`, `—` o `«`.
- No se corta tras las abreviaturas `Sr.`, `Sra.`, `Srta.`, `Dr.`, `Dra.`, `Ud.`, `Uds.`, `etc.`, `p. ej.`, `pág.`, `cap.`, `vol.` ni `núm.`.
- No se corta tras una inicial con punto (`J. R.`).
- No se corta dentro de `¿…?` ni de `¡…!` cuando no hay espacio y mayúscula después.
- Si una frase supera `max_chars`, se parte en la última coma o espacio antes del límite.

- [ ] **Paso 1:** Escribir las pruebas que deben fallar:
  - `test_dialogue_dash_kept`: `"—¿Tienes una copia física del certificado?\n—Está en casa."` (dos párrafos) produce 2 frases, con los textos intactos y el «—» incluido.
  - `test_abbreviations_not_split`: `"El Sr. Park llegó. Dijo hola."` produce 2 frases, y la primera es `"El Sr. Park llegó."`.
  - `test_question_inside_sentence`: `"¿Vienes? —preguntó ella."` produce 1 frase.
  - `test_offsets_roundtrip`: para cada `Sentence s`, se cumple `chapter_text[s.start:s.end] == s.text`.
  - `test_chunks_respect_limits`: con `max_chars=600` y `max_sentences=3`, ningún fragmento supera ninguno de los dos límites, ninguno cruza un párrafo y el primero tiene 1 frase.
  - `test_long_sentence_is_split`: una frase de 1500 caracteres con `max_chars=600` produce piezas de 600 caracteres o menos que, concatenadas, reconstruyen el texto original.
  - `test_chunks_start_at_first_sentence`: con `first_sentence=5`, el primer fragmento empieza en la frase 5.
  - `test_chunk_ids_stable`: dos llamadas con la misma entrada producen los mismos ids.
- [ ] **Paso 2:** Ejecutar `.venv/bin/pytest tests/test_text_segmenter.py -v`. Esperado: FAIL.
- [ ] **Paso 3:** Implementar con expresiones regulares (no NLP), siguiendo las reglas anteriores.
- [ ] **Paso 4:** Ejecutar las pruebas. Esperado: PASS.
- [ ] **Paso 5:** Commit `"Añadir segmentador de frases y fragmentos"`.

### Task 3: Validación de URL y descargas seguras

**Archivos:** Crear `lector/security/url_validator.py` y `lector/security/http_client.py`. Pruebas: `tests/test_security.py`.

**Interfaces:**
- **Consume:** `InvalidUrl` y `PageUnreachable`.
- **Produce:**
  - `validate_url(url: str) -> str`: devuelve la URL normalizada (esquema y host en minúsculas, sin fragmento) o lanza `InvalidUrl`.
  - `async resolve_public(host: str, port: int) -> str`: devuelve una IP pública o lanza `InvalidUrl`. Rechaza toda IP con `is_private`, `is_loopback`, `is_link_local`, `is_multicast`, `is_reserved` o `is_unspecified`, y también las IPv4 mapeadas en IPv6 que sean privadas.
  - `class SafeFetcher` con:
    - `__init__(max_bytes=5_000_000, max_redirects=5, timeout=15.0, resolver=resolve_public, transport: httpx.AsyncBaseTransport | None = None)`.
    - `async get(url) -> FetchResult`.
    - `async post(url, data: dict | None = None) -> FetchResult`.
    - `async aclose()`.
  - `FetchResult(url: str, status: int, text: str, content_type: str)`.

**Comportamiento:**
- Las redirecciones se siguen a mano (`follow_redirects=False`): cada `Location` pasa por `validate_url` y `resolve_public`.
- Se lee el cuerpo en *streaming* y se aborta con `PageUnreachable` en cuanto supera `max_bytes`.
- Solo se aceptan `text/html`, `application/xhtml+xml` y `text/plain`.
- Cabecera `User-Agent`: el de un Chrome de escritorio.
- Cabecera `X-Requested-With: XMLHttpRequest` solo en `post`.
- Los errores de red, los *timeouts* y los códigos 5xx lanzan `PageUnreachable(recoverable=True)`.
- Los códigos 4xx lanzan `PageUnreachable` con `recoverable=False`.
- Contra el *DNS rebinding*: conecta a la IP ya validada, enviando la cabecera `Host` original y el SNI correcto, mediante `httpx.AsyncHTTPTransport` con `local_address` sin tocar y la extensión `sni_hostname`. Si eso resulta inviable con httpx 0.28, el plan B es validar de nuevo la IP del *peer* después de conectar, con `response.extensions["network_stream"].get_extra_info("server_addr")`, y abortar si es privada. Documenta en el código qué opción quedó.

- [ ] **Paso 1:** Escribir las pruebas que deben fallar, todas con `httpx.MockTransport` y un *resolver* falso:
  - `test_rejects_non_http`: `file:///etc/passwd`, `ftp://x` y `javascript:alert(1)` lanzan `InvalidUrl`.
  - `test_rejects_private_targets`: los *hosts* que resuelven a `127.0.0.1`, `10.0.0.5`, `192.168.1.1`, `169.254.169.254`, `::1` y `::ffff:10.0.0.1` lanzan `InvalidUrl`.
  - `test_redirect_to_private_blocked`: una respuesta 302 hacia `http://localhost/` lanza `InvalidUrl`.
  - `test_too_many_redirects`: seis redirecciones seguidas lanzan `PageUnreachable`.
  - `test_size_limit`: un cuerpo de 6 MB lanza `PageUnreachable`.
  - `test_5xx_is_recoverable`: un 503 lanza `PageUnreachable` con `recoverable is True`.
  - `test_post_sends_ajax_header`: la petición capturada lleva `X-Requested-With`.
- [ ] **Paso 2:** Ejecutar `.venv/bin/pytest tests/test_security.py -v`. Esperado: FAIL.
- [ ] **Paso 3:** Implementar ambos módulos.
- [ ] **Paso 4:** Ejecutar las pruebas. Esperado: PASS.
- [ ] **Paso 5:** Commit `"Añadir validación anti-SSRF y cliente HTTP seguro"`.

### Task 4: Adaptador Madara (aresscanlationovel.com)

**Archivos:**
- Crear: `lector/content/base.py` y `lector/content/madara.py`.
- Crear las *fixtures* en `tests/fixtures/madara/`.
- Pruebas: `tests/test_madara.py`.

**Interfaces:**
- **Consume:** `SafeFetcher`, `build_chapter_text` y los modelos.
- **Produce** en `base.py`:
  - `class ContentSource(Protocol)`:
    - `async open(locator: str, prefetched_html: str | None = None) -> tuple[Document, Chapter]`. Devuelve el documento y el capítulo indicado; si el *locator* es la portada de una novela web, el capítulo es el 1. `prefetched_html` evita volver a descargar la página que ya bajó el registro de fuentes (Task 7).
    - `async load_chapter(ref: ChapterRef) -> Chapter`.
    - `async list_chapters() -> list[ChapterRef]`.
  - `doc_id_for_url(canonical_work_url: str) -> str`: los primeros 16 caracteres hexadecimales de SHA-256 de la URL normalizada, con `/` final.
  - `html_paragraphs(element) -> list[str]`: extrae los textos de `<p>`, colapsa espacios, descarta los vacíos y quita `<script>`, `<style>`, `<ins>` y los anuncios.
- **Produce** en `madara.py`:
  - `class MadaraSource(ContentSource)` con `__init__(fetcher: SafeFetcher)`.
  - `is_madara(html: str) -> bool`: es verdadero si aparece `"wp-content/themes/madara"` o si existen tanto `.reading-content` como `a.next_page` o `a.prev_page`.

**Reglas** (diseño §4):
- **Texto:** `.reading-content` → `html_paragraphs`.
- **Siguiente y anterior:** `a.next_page[href]` y `a.prev_page[href]`. Si no hay `next_page`, entonces `next_ref = None`.
- **URL de la obra:** la URL del capítulo sin el último segmento `capitulo-N/`. También vale el enlace de `.breadcrumb` que apunte a `/novels/{slug}/`.
- **Título de la obra:** el texto de ese enlace de la *breadcrumb*. **Título del capítulo:** el atributo `title` del enlace actual o el último elemento de la *breadcrumb*.
- **Índice:** se pide con `fetcher.post(f"{work_url}ajax/chapters/")`. Se toman los `li.wp-manga-chapter a[href]`, se **invierte** el orden (el sitio los da de más nuevo a más antiguo) y se quitan los duplicados por `href`. El `index` empieza en 0.
- **Entrada con la URL de la portada:** se carga el índice y el capítulo actual es `chapters[0]`.

- [ ] **Paso 1:** Descargar las *fixtures*:

  ```bash
  B=https://aresscanlationovel.com/novels/a-partir-de-ahora-pedir-matrimonio-es-la-unica-forma-de-sobrevivir
  UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
  mkdir -p tests/fixtures/madara
  for n in 1 99 100; do curl -sL -A "$UA" -o tests/fixtures/madara/capitulo-$n.html $B/capitulo-$n/; done
  curl -sL -A "$UA" -X POST -H "X-Requested-With: XMLHttpRequest" -o tests/fixtures/madara/chapters_ajax.html $B/ajax/chapters/
  ```

  - Esperado: 4 archivos, cada uno de más de 40 KB.
  - Si el sitio cambió y algún archivo no cumple las reglas, para y avisa: no adaptes las pruebas a ciegas.
- [ ] **Paso 2:** Escribir las pruebas que deben fallar. Todas usan `httpx.MockTransport`, que sirve las *fixtures* según la URL:
  - `test_detects_madara`: `is_madara(capitulo-99) is True` y `is_madara("<html></html>") is False`.
  - `test_chapter_99_text`: el capítulo 99 tiene 117 párrafos. El primero empieza por `"Park Kyung-Joo habló seriamente"` y el último es `"—Espero que esto solucione las cosas, al menos un poco."`.
  - `test_chapter_99_nav`: `next_ref.locator` termina en `/capitulo-100/` y `prev_ref.locator` termina en `/capitulo-98/`.
  - `test_last_chapter_has_no_next`: el capítulo 100 tiene `next_ref is None` y `next_candidates == []`.
  - `test_first_chapter_has_no_prev`: el capítulo 1 tiene `prev_ref is None`.
  - `test_chapter_list_from_ajax`: la lista tiene 100 elementos, en orden ascendente; `[0]` termina en `/capitulo-1/` y `[99]` en `/capitulo-100/`.
  - `test_doc_id_same_for_any_chapter`: abrir el capítulo 1 y el 99 da el mismo `doc_id`.
  - `test_open_work_url_starts_at_chapter_1`.
- [ ] **Paso 3:** Ejecutar `.venv/bin/pytest tests/test_madara.py -v`. Esperado: FAIL.
- [ ] **Paso 4:** Implementar con `lxml.html` y `cssselect`.
- [ ] **Paso 5:** Ejecutar las pruebas. Esperado: PASS.
- [ ] **Paso 6:** Commit `"Añadir adaptador Madara con fixtures reales"`.

### Task 5: Adaptador web genérico

**Archivos:**
- Crear: `lector/content/generic_web.py`.
- *Fixtures*: `tests/fixtures/generic/`, HTML escrito a mano, con 5 o 6 archivos pequeños.
- Pruebas: `tests/test_chapter_navigation.py`.

**Interfaces:**
- **Consume:** `SafeFetcher`, `ContentSource`, `doc_id_for_url` y `html_paragraphs`.
- **Produce:**
  - `class GenericWebSource(ContentSource)` con `__init__(fetcher, work_prefix_override: str | None = None)`.
  - `find_next_candidates(html: str, page_url: str, work_prefix: str) -> list[str]`.
  - `guess_work_prefix(chapter_url: str, html: str) -> str`.

**Reglas** (diseño §5.2):
- **Texto:** `trafilatura.extract(html, output_format="txt", include_comments=False, include_tables=False, favor_precision=True)`, partido en párrafos por líneas en blanco. Si queda vacío, lanza `NoExtractableText`.
- **Candidatos a «siguiente»,** en orden de prioridad:
  1. `link[rel=next]` y `a[rel=next]`.
  2. `<a>` cuyo texto normalizado sea uno de estos: `siguiente`, `capitulo siguiente`, `capítulo siguiente`, `next`, `next chapter`, `»`, `→`, `siguiente »`, `siguiente →`.
- **Filtro:** de los candidatos se descartan los que no son del mismo *host* o no empiezan por `work_prefix`, y los duplicados.
- **Resultado:**
  - Si queda exactamente 1 URL, es `next_ref`.
  - Si quedan más de 1 y apuntan a URLs distintas, `next_ref = None` y `next_candidates` recibe todas.
  - Si no queda ninguna, `next_ref = None`.
- **Patrón numérico de URL:** nunca crea candidatos por sí solo. Solo **desempata** cuando hay 2 o más candidatos y exactamente uno de ellos es la URL actual con su último número incrementado en 1.
- **Prefijo de la obra:** es la ruta padre de la URL del capítulo. Si un `<link rel=canonical>` o un enlace de texto «índice», «tabla de contenidos» o «index» apunta a una ruta padre más corta que es prefijo de la URL actual, se usa esa ruta.
- **Identidad del documento:** `doc_id_for_url(host + prefijo)`.
- **Corrección manual:** si llega `work_prefix_override`, prevalece sobre lo detectado.

- [ ] **Paso 1:** Escribir las pruebas que deben fallar:
  - `test_rel_next_preferred`.
  - `test_text_link_siguiente`.
  - `test_external_next_ignored`: un «Siguiente» que apunta a otro *host* da `next_ref is None`.
  - `test_other_work_ignored`: un enlace a otra ruta de obra se ignora.
  - `test_ambiguous_two_candidates`: produce `next_candidates` con 2 elementos.
  - `test_numeric_pattern_tiebreak`.
  - `test_numeric_pattern_never_invents`: una página sin enlaces da `next_ref is None`, aunque la URL termine en `-5/`.
  - `test_no_text_raises`: una página solo con `<nav>` lanza `NoExtractableText`.
  - `test_work_prefix_parent_path`: `/novela/x/capitulo-5/` da `/novela/x/`.
- [ ] **Paso 2:** Ejecutar `.venv/bin/pytest tests/test_chapter_navigation.py -v`. Esperado: FAIL.
- [ ] **Paso 3:** Implementar.
- [ ] **Paso 4:** Ejecutar las pruebas. Esperado: PASS.
- [ ] **Paso 5:** Commit `"Añadir adaptador web genérico con detección de capítulo siguiente"`.

### Task 6: Fuente PDF

**Archivos:** Crear `lector/content/pdf_source.py`. Pruebas: `tests/test_pdf_source.py`. Los PDF de prueba se generan en `conftest.py` con `fitz` (sin binarios en el repo).

**Interfaces:**
- **Consume:** `ContentSource`, `build_chapter_text`, `DamagedDocument` y `ScannedPdf`.
- **Produce:**
  - `class PdfSource(ContentSource)`, con `open(path_str)`. El trabajo de PyMuPDF se ejecuta en `asyncio.to_thread`.
  - `file_doc_id(path: Path) -> tuple[str, str]`: devuelve `(doc_id, sha256)`, donde `doc_id` son los primeros 16 caracteres hexadecimales del SHA-256 del contenido. Se reutiliza en EPUB.

**Reglas** (diseño §5.2.3):
- **Errores al abrir:** si `fitz.open` falla, o si el documento está cifrado y `authenticate("")` falla, se lanza `DamagedDocument`.
- **Escaneado:** si menos del 10 % de las páginas tiene 20 caracteres o más de texto, se lanza `ScannedPdf`.
- **Capítulos:**
  - Si hay índice, se usan las entradas de nivel 1 de `doc.get_toc()`; cada capítulo va de su página a la anterior a la siguiente entrada.
  - Si no hay índice, se recorren los bloques de `page.get_text("dict")` buscando líneas que cumplan `^(cap[ií]tulo|prólogo|prologo|epílogo|epilogo|interludio|parte|volumen)\b` y cuyo tamaño de letra sea al menos 1,2 veces la mediana del documento.
  - Si no se encuentra ninguno, el capítulo único se llama «Documento completo».
- **Encabezados y pies:** se eliminan las líneas situadas en el 8 % superior o inferior de la página cuyo texto normalizado (dígitos sustituidos por `#`) se repite en al menos el 30 % de las páginas. También las líneas que solo contienen un número.
- **Párrafos:** se unen los bloques de texto. Las palabras cortadas con guion a final de línea se reconstruyen.
- **Página real:** cada `Paragraph.page` guarda el número de página real, empezando en 1.
- **Locator del capítulo:** `"pages:<a>-<b>"`.

- [ ] **Paso 1:** Escribir las pruebas que deben fallar, con *fixtures* que generan PDF con `fitz`:
  - `test_extracts_text_with_pages`: en un PDF de 3 páginas, cada párrafo tiene su página correcta.
  - `test_toc_chapters`: un PDF con `set_toc([[1,"Prólogo",1],[1,"Capítulo 1",2]])` da 2 capítulos con esos títulos.
  - `test_heading_detection_without_toc`.
  - `test_running_header_removed`: un encabezado «Mi Novela — Vol 1» y un número de página en cada página no aparecen en el texto.
  - `test_scanned_pdf_detected`: un PDF solo con rectángulos dibujados lanza `ScannedPdf`.
  - `test_damaged_pdf`: unos *bytes* basura lanzan `DamagedDocument`.
  - `test_hyphen_rejoin`.
- [ ] **Paso 2:** Ejecutar las pruebas. Esperado: FAIL.
- [ ] **Paso 3:** Implementar.
- [ ] **Paso 4:** Ejecutar las pruebas. Esperado: PASS.
- [ ] **Paso 5:** Commit `"Añadir extracción de PDF por capítulos"`.

### Task 7: Fuente EPUB y registro de fuentes

**Archivos:** Crear `lector/content/epub_source.py` y `lector/content/registry.py`. Pruebas: `tests/test_epub_source.py` y `tests/test_registry.py`. Los EPUB de prueba se construyen en `conftest.py` con `zipfile`.

**Interfaces:**
- **Consume:** `file_doc_id`, `PdfSource`, `MadaraSource`, `GenericWebSource`, `is_madara` y `SafeFetcher`.
- **Produce:**
  - `class EpubSource(ContentSource)`.
  - `async open_source(locator: str, fetcher: SafeFetcher) -> tuple[ContentSource, Document, Chapter]` en `registry.py`, que elige el adaptador así:
    - `.pdf` → `PdfSource`.
    - `.epub` → `EpubSource`.
    - Cualquier otra entrada → `validate_url`, después `fetcher.get` y después `MadaraSource` si `is_madara`; si no, `GenericWebSource`. El HTML ya descargado se pasa al adaptador en `open(locator, prefetched_html=...)` para no pedirlo dos veces.

**Reglas** (diseño §5.2.4):
- **OPF:** se localiza con `META-INF/container.xml`, se lee el *spine* en orden y se ignoran los `linear="no"`.
- **Títulos:** salen de `nav.xhtml` (EPUB 3) o `toc.ncx` (EPUB 2). Si no hay, del primer `h1`, `h2` o `h3`. Si tampoco, «Sección N».
- **Seguridad:**
  - Se parsea con `lxml.etree.XMLParser(resolve_entities=False, no_network=True)`.
  - Se rechazan los nombres de miembro con `..` o que empiecen por `/`.
  - Los miembros de más de 20 MB lanzan `DamagedDocument`.
- **Limpieza:**
  - Se eliminan `script`, `style`, `rt` y `rp`.
  - El texto de bloque se toma de `p`, `div` sin hijos de bloque, `h1`–`h6`, `li` y `blockquote`.
  - Los documentos del *spine* con menos de 20 caracteres de texto (páginas de ilustraciones) se saltan.
- **Locator:** `"spine:<href>"`.

- [ ] **Paso 1:** Escribir las pruebas que deben fallar:
  - `test_spine_order_and_nav_titles`.
  - `test_ncx_titles_epub2`.
  - `test_ruby_stripped`: `<ruby>漢<rt>kan</rt></ruby>` produce `漢`.
  - `test_image_only_pages_skipped`.
  - `test_path_traversal_rejected`: un miembro llamado `../evil.xhtml` lanza `DamagedDocument`.
  - `test_xxe_not_resolved`: un `<!ENTITY x SYSTEM "file:///etc/passwd">` no aparece en el texto.
  - En `test_registry.py`: `test_picks_madara_for_madara_html`, `test_picks_generic_otherwise`, `test_picks_pdf_and_epub_by_extension` y `test_invalid_url_raises`.
- [ ] **Paso 2:** Ejecutar las pruebas. Esperado: FAIL.
- [ ] **Paso 3:** Implementar.
- [ ] **Paso 4:** Ejecutar las pruebas. Esperado: PASS.
- [ ] **Paso 5:** Commit `"Añadir EPUB y selección automática de fuente"`.

### Task 8: Persistencia SQLite

**Archivos:** Crear `lector/persistence/database.py` y `lector/persistence/repositories.py`. Pruebas: `tests/test_progress.py`.

**Interfaces:**
- **Consume:** los modelos, `db_path`, `DatabaseLocked` y `DatabaseCorrupt`.
- **Produce** en `database.py`:
  - `open_database(path: Path) -> sqlite3.Connection`.
    - Configura `PRAGMA journal_mode=WAL`, `foreign_keys=ON` y `busy_timeout=5000`.
    - Crea el esquema y comprueba `PRAGMA quick_check`.
    - Si la base está corrupta, la renombra a `lector.db.corrupt-<timestamp>`, crea una nueva y deja `conn.recovered_from_corruption = True`. Como `sqlite3.Connection` no admite atributos nuevos, devuelve una subclase `LectorConnection(sqlite3.Connection)` creada con `factory=`.
- **Produce** en `repositories.py`. Cada repositorio recibe la conexión en el constructor y se usa **solo desde el hilo de trabajo**, con `check_same_thread=False` y un `threading.Lock`:
  - `DocumentRepo`: `upsert(doc: Document, origin_path: str | None, file_size: int | None, file_mtime: float | None)`, `get(doc_id) -> Document | None`, `list_recent(limit=50) -> list[tuple[Document, ProgressRecord | None]]`.
  - `ChapterRepo`: `save(doc_id, chapter: Chapter, store_text: bool)`, `get_text(doc_id, locator) -> Chapter | None`, `purge_text(doc_id)`.
  - `ProgressRepo`: `save(rec: ProgressRecord)` y `get(doc_id) -> ProgressRecord | None`, donde:
    - `ProgressRecord(doc_id, kind, origin, title, last_read_at: float, position: Position, chapter_url: str | None, last_chunk_id: str | None, edge_voice: str, piper_voice: str, speed: float, session_state: str)`.
  - `SettingsRepo`: `get(key) -> Any` (con los valores por defecto de las restricciones globales más `edge_voice`, `language`, `speed`, `preferred_engine="edge"`, `piper_voice`, `piper_speaker="F"`, `first_run_done=False`, `work_prefix_overrides={}`) y `set(key, value)`. El valor se guarda como JSON.
  - `ChunkLogRepo`: `record(chunk_id, engine: str, outcome: str)` y `prune(older_than_days=30)`.
  - `VoicesCacheRepo`: `save(voices: list[dict])` y `load() -> list[dict]`.
- **Errores:**
  - `sqlite3.OperationalError("database is locked")` se reintenta 3 veces, con 0,2 s, 0,4 s y 0,8 s de espera, y después se lanza `DatabaseLocked`.
  - `sqlite3.OperationalError` con «database or disk is full» y `OSError` con `errno.ENOSPC` se convierten en `ResourceExhausted`.
  - En el controlador (Task 14), un `MemoryError` también se convierte en `ResourceExhausted`: la sesión pasa a `PAUSED` y se muestra el aviso.

**Esquema:**

```sql
documents(doc_id TEXT PRIMARY KEY, kind TEXT, origin TEXT, title TEXT, canonical_url TEXT, work_prefix TEXT, file_hash TEXT, file_size INTEGER, file_mtime REAL, chapters_json TEXT, created_at REAL)
chapters(doc_id TEXT, locator TEXT, idx INTEGER, title TEXT, text TEXT NULL, paragraphs_json TEXT NULL, next_locator TEXT NULL, prev_locator TEXT NULL, PRIMARY KEY(doc_id, locator))
progress(doc_id TEXT PRIMARY KEY REFERENCES documents, kind TEXT, origin TEXT, title TEXT, last_read_at REAL, chapter_index INTEGER, chapter_locator TEXT, chapter_url TEXT, page INTEGER, paragraph_index INTEGER, sentence_index INTEGER, char_offset INTEGER, last_chunk_id TEXT, edge_voice TEXT, piper_voice TEXT, speed REAL, session_state TEXT)
settings(key TEXT PRIMARY KEY, value_json TEXT)
chunk_log(id INTEGER PRIMARY KEY, chunk_id TEXT, engine TEXT, outcome TEXT, at REAL)
voices_cache(id INTEGER PRIMARY KEY CHECK(id=1), voices_json TEXT, fetched_at REAL)
```

- [ ] **Paso 1:** Escribir las pruebas que deben fallar:
  - `test_progress_roundtrip`.
  - `test_progress_survives_reopen`: guardar, cerrar la conexión, volver a abrir y leer da el mismo registro.
  - `test_store_text_false_keeps_no_text`: `chapters.text IS NULL` y existe la fila de metadatos.
  - `test_corrupt_db_is_quarantined`: tras escribir *bytes* basura en `lector.db`, `open_database` funciona, existe un `lector.db.corrupt-*` y `recovered_from_corruption` es `True`.
  - `test_settings_defaults`: `SettingsRepo.get("edge_voice") == "es-MX-DaliaNeural"`, `get("speed") == 1.0` y `get("edge_retries") == 3`.
  - `test_chunk_log_prune`.
  - `test_disk_full_maps_to_resource_exhausted`: un `sqlite3.OperationalError("database or disk is full")` simulado lanza `ResourceExhausted`.
- [ ] **Paso 2:** Ejecutar las pruebas. Esperado: FAIL.
- [ ] **Paso 3:** Implementar.
- [ ] **Paso 4:** Ejecutar las pruebas. Esperado: PASS.
- [ ] **Paso 5:** Commit `"Añadir base de datos SQLite y repositorios"`.

### Task 9: Audio en memoria: PCM, cola limitada y reproductor

**Archivos:** Crear `lector/audio/pcm.py` y `lector/audio/player.py`, más `tests/fakes.py` (con `FakeOutput`). Pruebas: `tests/test_audio_player.py`.

**Interfaces:**
- **Produce** en `pcm.py`:
  - `OUTPUT_RATE = 24000`.
  - `decode_mp3(data: bytes) -> np.ndarray`: usa `miniaudio.decode(data, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=OUTPUT_RATE)` y devuelve `np.frombuffer(..., int16)`. Si la decodificación falla o el resultado está vacío, lanza `SynthesisFailed`.
  - `resample(pcm: np.ndarray, src_rate: int, dst_rate: int = OUTPUT_RATE) -> np.ndarray` (interpolación lineal con `np.interp`, devuelve int16).
- **Produce** en `player.py`:
  - `QueuedClip(chunk: Chunk, pcm: np.ndarray, marks: list[tuple[int, int]], engine: str)`. `marks` es una lista de `(muestra_inicial, sentence_index)` en orden creciente, y `marks[0][0] == 0`.
  - `PlayerEvents(Protocol)`, cuyos métodos se llaman **en el bucle asyncio**, nunca en el hilo de audio:
    - `on_sentence_started(chunk: Chunk, sentence_index: int)`.
    - `on_chunk_finished(chunk: Chunk)`.
    - `on_output_error(err: Exception)`.
  - `AudioOutput(Protocol)`: `start(callback: Callable[[np.ndarray], None])` (el *callback* rellena un búfer int16 de `frames` muestras), `stop()`.
    - Implementación real: `SoundDeviceOutput(rate=OUTPUT_RATE, blocksize=1024)`, con `sounddevice.OutputStream(dtype="int16", channels=1)`.
  - `class AudioPlayer` con `__init__(output: AudioOutput, loop: asyncio.AbstractEventLoop, events: PlayerEvents, max_ahead: int = 2, max_seconds: float = 90.0)` y estos métodos:
    - `async enqueue(clip: QueuedClip)`: espera mientras la cola ya tenga `max_ahead` clips **además** del que suena, o mientras la suma supere `max_seconds` (salvo que la cola esté vacía).
    - `pause()`: el *callback* escribe silencio y no avanza.
    - `resume()`.
    - `clear() -> Chunk | None`: vacía la cola y el clip actual, y devuelve el `Chunk` que sonaba.
    - `current() -> tuple[Chunk, int] | None`: devuelve `(chunk, sentence_index que suena)`.
    - `queued_count() -> int`.
    - `close()`.

**Reglas:**
- El *callback* de audio **no reserva memoria ni bloquea**: copia segmentos de numpy y avanza un índice.
- Los eventos se pasan al bucle con `loop.call_soon_threadsafe`.
- Cuando un clip termina, se emite `on_chunk_finished`, se suelta la referencia al array y se despierta a quien espera en `enqueue` mediante un `asyncio.Condition`, notificado con `call_soon_threadsafe`.
- `on_sentence_started` se emite al cruzar cada `marks[i][0]`, incluida la marca 0.
- Si el *stream* real falla, se llama a `on_output_error`.

- [ ] **Paso 1:** Escribir `FakeOutput` en `tests/fakes.py`: guarda el *callback* y ofrece `pull(frames: int) -> np.ndarray`, que lo invoca de forma síncrona.
- [ ] **Paso 2:** Escribir las pruebas que deben fallar:
  - `test_plays_clips_in_order_and_emits_events`: 2 clips con 2 marcas cada uno; con `pull` se recorren y los eventos de frase salen en orden `[0, 1, 2, 3]`, seguidos de `on_chunk_finished` para cada uno.
  - `test_queue_is_bounded`: con `max_ahead=2`, el cuarto `enqueue` sigue pendiente (`asyncio.wait_for(..., 0.05)` da *timeout*) hasta que se consume un clip.
  - `test_seconds_limit`: dos clips de 60 s y `max_seconds=90`, el segundo espera.
  - `test_pause_outputs_silence_and_keeps_position`: tras `pause`, `pull` devuelve ceros y `current()` no cambia; tras `resume`, continúa desde la misma muestra.
  - `test_clear_returns_current_chunk`.
  - `test_played_clips_are_released`: tras terminar 50 clips, se cumple que `len(gc.get_referrers(...))` o un `weakref` a cada array está muerto.
  - `test_decode_mp3_roundtrip`: decodifica un MP3 mínimo de prueba generado con `miniaudio` si puede codificar. Si no, usa una *fixture* binaria de 1 s a 24 kHz guardada en `tests/fixtures/audio/beep.mp3`, generada una vez con `edge-tts` o `ffmpeg` durante el desarrollo y añadida al repo. **Es el único archivo de audio permitido, y solo en `tests/`.**
  - `test_resample_length`: 22050 muestras a 22050 Hz dan 24000 muestras ±1.
- [ ] **Paso 3:** Ejecutar `.venv/bin/pytest tests/test_audio_player.py -v`. Esperado: FAIL.
- [ ] **Paso 4:** Implementar.
- [ ] **Paso 5:** Ejecutar las pruebas. Esperado: PASS.
- [ ] **Paso 6:** Prueba manual: un *script* temporal en el *scratchpad*, no en el repo, reproduce un tono de 1 s con `SoundDeviceOutput`. Esperado: se oye el tono.
- [ ] **Paso 7:** Commit `"Añadir reproductor de audio en memoria con cola limitada"`.

### Task 10: Motor base y Edge TTS

**Archivos:** Crear `lector/tts/base.py` y `lector/tts/edge_engine.py`. Pruebas: `tests/test_edge_engine.py`.

**Interfaces:**
- **Consume:** `decode_mp3`, `Chunk`, `EdgeUnavailable`, `EdgeVoiceNotFound` y `SynthesisFailed`.
- **Produce** en `base.py`:
  - `AudioClip(pcm: np.ndarray, marks: list[tuple[int, int]])`, siempre a `OUTPUT_RATE`.
  - `class TtsEngine(Protocol)`, con `name: str` (`"edge"` o `"piper"`) y estos métodos:
    - `async synthesize(chunk: Chunk, voice: str, speed: float) -> AudioClip`.
    - `async health_check(voice: str) -> bool`.
    - `async list_voices() -> list[dict]`.
  - `edge_rate(speed: float) -> str`: devuelve `f"{round((speed - 1) * 100):+d}%"`.
  - `EDGE_SPEED_RANGE = (0.5, 2.0)` y `PIPER_SPEED_RANGE = (0.5, 2.5)`. Son provisionales: el paso 6 verifica los límites reales de Edge y ajusta la constante si hace falta.
  - `clamp_speed(engine: str, speed: float) -> tuple[float, bool]`: devuelve la velocidad recortada y si hubo que recortarla.
- **Produce** en `edge_engine.py`:
  - `class EdgeEngine(TtsEngine)`, con `__init__(communicate_factory=edge_tts.Communicate, list_voices_fn=edge_tts.list_voices, timeout_s: float = 15.0)`.
  - `align_marks(sentences: tuple[Sentence, ...], boundaries: list[tuple[int, str]]) -> list[tuple[int, int]]`. Cada *boundary* es `(offset_en_muestras, texto)`.

**Reglas:**
- `synthesize` crea `communicate_factory(chunk.text, voice, rate=edge_rate(speed), boundary="SentenceBoundary", connect_timeout=10, receive_timeout=int(timeout_s))`.
- Itera `stream()` dentro de `asyncio.timeout(timeout_s)`:
  - Concatena los `data` de los *chunks* con `type == "audio"`.
  - Para cada `type == "SentenceBoundary"`, guarda `(int(offset / 10_000_000 * OUTPUT_RATE), text)`. Los *offsets* vienen en *ticks* de 100 ns.
- **Errores:**
  - Si no llega audio, lanza `EdgeUnavailable`.
  - `aiohttp.ClientError`, `asyncio.TimeoutError`, `OSError` y `edge_tts.exceptions.EdgeTTSException` se convierten en `EdgeUnavailable` y conservan la causa.
  - Si la voz no está en la lista cacheada (cuando la lista existe), se lanza `EdgeVoiceNotFound` **antes** de llamar a la red.
- **`align_marks`** (algoritmo):
  1. Normaliza: quita todo lo que no sea alfanumérico y pasa a minúsculas.
  2. Calcula `S_k`, la longitud normalizada acumulada **antes** de cada frase `k`.
  3. Calcula `B_j`, la longitud normalizada acumulada antes de cada *boundary* `j`.
  4. A cada frase `k` le asigna el *offset* del *boundary* `j` con mayor `B_j <= S_k`.
  5. Fuerza `marks[0] = (0, first.index)`.
  6. Si dos frases quedan con el mismo *offset*, conserva solo la primera, porque una marca sin avance no confirma nada.
  7. Si no hay *boundaries*, devuelve `[(0, first.index)]`.
- **`health_check`:** sintetiza `"Hola."` y devuelve `True` si llega audio. Cualquier excepción devuelve `False`.

- [ ] **Paso 1:** Escribir `FakeCommunicate` en `tests/fakes.py`. Recibe una lista programada de *chunks* o una excepción, y registra los `kwargs` con los que se construye.
- [ ] **Paso 2:** Escribir las pruebas que deben fallar:
  - `test_rate_string`: `edge_rate(1.0) == "+0%"`, `edge_rate(1.5) == "+50%"` y `edge_rate(0.75) == "-25%"`.
  - `test_default_voice_and_no_pitch_change`: los `kwargs` registrados no incluyen `pitch` distinto de `"+0Hz"`.
  - `test_boundaries_become_marks`: 3 frases y 3 *boundaries* con *offsets* de 0, 1 s y 2 s dan marcas `[(0, i0), (24000, i1), (48000, i2)]`.
  - `test_marks_align_when_edge_merges_sentences`: 3 frases y 2 *boundaries* (Edge unió la 2 y la 3) dan 2 marcas, y la frase 3 no tiene marca propia.
  - `test_empty_audio_raises_unavailable`.
  - `test_network_error_raises_unavailable`.
  - `test_unknown_voice_raises_before_network`.
- [ ] **Paso 3:** Ejecutar las pruebas. Esperado: FAIL.
- [ ] **Paso 4:** Implementar.
- [ ] **Paso 5:** Ejecutar las pruebas. Esperado: PASS.
- [ ] **Paso 6:** Prueba manual con red real, con un *script* temporal en el *scratchpad*:
  - Sintetiza con `es-MX-DaliaNeural` el primer párrafo de `capitulo-99.html` y lo reproduce con `AudioPlayer` + `SoundDeviceOutput`.
  - Esperado: se oye a Dalia y no queda ningún `.mp3` ni `.wav` en el repo, en `data_dir()` ni en el directorio temporal; compruébalo con `find`.
  - Prueba también `rate="+100%"` y `"-50%"`. Si Edge rechaza alguno, ajusta `EDGE_SPEED_RANGE` y anota el límite real.
- [ ] **Paso 7:** Commit `"Añadir motor Edge TTS con marcas de frase"`.

### Task 11: Motor Piper y gestor de modelos

**Archivos:** Crear `lector/tts/piper_engine.py`. Pruebas: `tests/test_piper_engine.py`.

**Interfaces:**
- **Consume:** `resample`, `AudioClip`, `models_dir`, `PiperModelMissing` y `SynthesisFailed`.
- **Produce:**
  - `class PiperModelManager` con `__init__(directory: Path)` y estos métodos:
    - `installed() -> list[str]`: los nombres de voz que tienen `.onnx` y `.onnx.json`.
    - `is_installed(name) -> bool`.
    - `speakers(name) -> list[str]`: lee `speaker_id_map` del `.onnx.json`.
    - `async download(name, progress: Callable[[float], None] | None = None)`: usa `piper.download_voices.download_voice(name, directory)` en `asyncio.to_thread`.
    - `CATALOG`: una lista de dicts `{name, label, gender, size_mb, license_note}` para `es_ES-sharvard-medium` (F/M, 76 MB), `es_AR-daniela-high` (114 MB), `es_MX-claude-high` (63 MB, género sin verificar), `es_MX-ald-medium` (63 MB) y `es_ES-davefx-medium` (63 MB).
  - `class PiperEngine(TtsEngine)` con `__init__(manager: PiperModelManager, loader=PiperVoice.load)`.
    - Mantiene **un** modelo cargado y solo lo recarga si cambia la voz.
    - `synthesize` se ejecuta en `asyncio.to_thread`:
      - Llama a `voice.synthesize(sentence.text, SynthesisConfig(length_scale=1/speed, speaker_id=<del mapa>))` **frase a frase**.
      - Concatena los `AudioChunk.audio_int16_array` y los remuestrea de `chunk.sample_rate` a `OUTPUT_RATE`.
      - Cada frase empieza en su propia marca.
    - El parámetro `voice` usa el formato `"es_ES-sharvard-medium#F"`: el nombre del modelo y, opcionalmente, el hablante.

- [ ] **Paso 1:** Escribir `FakePiperVoice` en `tests/fakes.py`. Devuelve `AudioChunk`s falsos de 22050 Hz cuya duración es proporcional al texto, y registra el `syn_config`.
- [ ] **Paso 2:** Escribir las pruebas que deben fallar:
  - `test_missing_model_raises`.
  - `test_installed_detection`: crea dos archivos vacíos `.onnx` y `.onnx.json` en `tmp_path`.
  - `test_speed_maps_to_length_scale`: con `speed=2.0`, `length_scale` es `0.5`.
  - `test_one_mark_per_sentence_and_resampled`: un fragmento de 2 frases da 2 marcas, y los *offsets* están a 24 kHz.
  - `test_speaker_id_from_map`: `"#F"` se traduce al id de `speaker_id_map`.
  - `test_model_loaded_once`.
- [ ] **Paso 3:** Ejecutar las pruebas. Esperado: FAIL.
- [ ] **Paso 4:** Implementar.
- [ ] **Paso 5:** Ejecutar las pruebas. Esperado: PASS.
- [ ] **Paso 6:** Prueba manual:
  - Descarga `es_ES-sharvard-medium` con `PiperModelManager.download` a un `LECTOR_DATA_DIR` temporal del *scratchpad*.
  - Sin red (desactivando el wifi o con un *proxy* falso), sintetiza y reproduce una frase.
  - Esperado: se oye la voz y no se crea ningún archivo de audio.
  - Genera también una muestra de cada voz femenina del `CATALOG` para que el usuario elija. Las muestras se reproducen, no se guardan.
- [ ] **Paso 7:** Commit `"Añadir motor Piper y gestor de modelos"`.

### Task 12: Gestor de motores (máquina de estados y circuit breaker)

**Archivos:** Crear `lector/tts/engine_manager.py`. Añadir `FakeEdge`, `FakePiper` y `FakeClock` a `tests/fakes.py`. Pruebas: `tests/test_engine_fallback.py`.

**Interfaces:**
- **Consume:** `TtsEngine`, `AudioClip`, `ChunkLogRepo` y los errores.
- **Produce:**
  - `EngineState(StrEnum)`: `EDGE_ACTIVE`, `EDGE_RETRYING`, `PIPER_ACTIVE`, `EDGE_RECOVERY_TEST` y `WAITING_NETWORK`.
  - `EngineConfig(edge_retries=3, backoff=(1, 2, 4), recovery_initial_s=60, recovery_max_s=600, piper_cooldown_s=120, wait_max_s=60, preferred="edge")`.
  - `class EngineManager` con `__init__(edge: TtsEngine, piper: TtsEngine, config: EngineConfig, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, on_state: Callable[[EngineState, str], None] = lambda s, e: None, chunk_log: ChunkLogRepo | None = None)` y estos métodos:
    - `async synthesize(chunk: Chunk, edge_voice: str, piper_voice: str, speed: float) -> tuple[AudioClip, str]`: devuelve el clip y el nombre del motor que lo generó.
    - `state` (propiedad).
    - `reset()`: se usa al empezar una sesión nueva.

**Transiciones:**

| Estado y evento | Acción y estado siguiente |
|---|---|
| Inicio (`preferred=="edge"`) | `EDGE_ACTIVE` |
| Inicio (`preferred=="piper"`) | `PIPER_ACTIVE` sin pruebas de recuperación. Es la preferencia del usuario, no un fallo. |
| `EDGE_ACTIVE` y `EdgeUnavailable` | `EDGE_RETRYING`: espera `backoff[i]` y reintenta, hasta `edge_retries` veces. Si todos fallan, abre el circuito, guarda `opened_at = clock()` y `next_probe_at = opened_at + recovery_initial_s`, y sintetiza **el mismo fragmento** con Piper (`PIPER_ACTIVE`). |
| `EDGE_ACTIVE` y `EdgeVoiceNotFound` | Sin reintentos, pasa a `PIPER_ACTIVE`, con `next_probe_at = +inf` hasta que cambie la voz. |
| `PIPER_ACTIVE`, antes de cada fragmento, si `clock() >= next_probe_at` y `clock() - opened_at >= piper_cooldown_s` | `EDGE_RECOVERY_TEST`: `edge.health_check`. Si sale bien, cierra el circuito y pasa a `EDGE_ACTIVE`, con lo que **este** fragmento ya va por Edge. Si falla, el intervalo se duplica (`interval = min(interval * 2, recovery_max_s)`) y se vuelve a `PIPER_ACTIVE`. |
| Piper lanza `PiperModelMissing` o `SynthesisFailed` | `WAITING_NETWORK`: bucle infinito con espera `min(2**n, wait_max_s)` que prueba Edge (`synthesize` real) y después Piper. Sale con el primer éxito. Si el bucle se cancela, propaga `CancelledError`. |

- Cada cambio de estado llama a `on_state(state, motor_activo)`. El motor activo es `"Edge TTS"` o `"Piper local"`.
- Cada fragmento sintetizado se registra en `chunk_log.record(chunk_id, engine, "ok")`. Los fallos se registran como `"error:<Clase>"`.

- [ ] **Paso 1:** Escribir las pruebas que deben fallar. Todas usan `FakeClock`, que avanza el tiempo cuando se llama a `sleep`, para que las pruebas sean instantáneas:
  - `test_edge_is_initial_engine`.
  - `test_edge_failure_switches_to_piper_after_retries`: hay exactamente 1 intento más 3 reintentos, las esperas son `[1, 2, 4]` y el fragmento sale por Piper.
  - `test_piper_continues_from_pending_chunk`: el `chunk_id` que recibe Piper es el que falló.
  - `test_no_flapping_within_cooldown`: Edge se recupera a los 30 s, pero el siguiente fragmento a los 31 s sigue por Piper.
  - `test_edge_recovers_after_probe`: a los 121 s con un *health check* correcto, el fragmento sale por Edge y el estado es `EDGE_ACTIVE`.
  - `test_probe_interval_doubles_and_caps`: los intervalos son 60, 120, 240, 480, 600 y 600.
  - `test_both_fail_waits_then_resumes`: Edge caído y Piper sin modelo pasan a `WAITING_NETWORK`; las esperas son 1, 2, 4… hasta 60. Cuando Edge vuelve, devuelve el clip con `"edge"`.
  - `test_voice_not_found_no_retries`.
  - `test_chunk_log_records_engine_per_chunk`.
- [ ] **Paso 2:** Ejecutar `.venv/bin/pytest tests/test_engine_fallback.py -v`. Esperado: FAIL.
- [ ] **Paso 3:** Implementar.
- [ ] **Paso 4:** Ejecutar las pruebas. Esperado: PASS.
- [ ] **Paso 5:** Commit `"Añadir máquina de estados de motores con circuit breaker"`.

### Task 13: Navegador de capítulos y precarga

**Archivos:** Crear `lector/services/navigator.py`. Añadir `FakeSource` a `tests/fakes.py`. Pruebas: `tests/test_navigator.py`.

**Interfaces:**
- **Consume:** `ContentSource`, `ChapterRepo`, `PageUnreachable`, `ChapterLoopDetected`, `AmbiguousNextChapter` y `NextChapterNotFound`.
- **Produce:**
  - `class ChapterNavigator` con `__init__(source: ContentSource, document: Document, chapter_repo: ChapterRepo, store_text: bool, prefetch_ahead: int = 2, wait_max_s: float = 60, sleep=asyncio.sleep)` y estos métodos:
    - `async get(ref: ChapterRef) -> Chapter`. Busca en este orden: la caché en memoria, después `chapter_repo` y por último `source.load_chapter`. La caché en memoria guarda como máximo `prefetch_ahead + 2` capítulos, con política LRU.
    - `async next_of(chapter: Chapter) -> Chapter | None`:
      - Devuelve `None` si `next_ref is None` y no hay candidatos (fin de la novela).
      - Lanza `AmbiguousNextChapter` si hay candidatos.
      - Lanza `ChapterLoopDetected` si el locator ya está en `visited`.
      - Si falla la red, espera **sin límite** con espera creciente hasta `wait_max_s` y llama a `on_waiting(True)` y después a `on_waiting(False)`.
    - `async prev_of(chapter) -> Chapter | None`.
    - `start_prefetch(chapter)`: lanza una tarea que carga `next` y `next(next)`, hasta `prefetch_ahead`. Los errores no recuperables se guardan y no se propagan; los recuperables se reintentan sin límite.
    - `mark_visited(ref)`.
    - `visited: set[str]`.
    - `on_waiting: Callable[[bool], None]`.
    - `async close()`: cancela la precarga.
  - Para las fuentes de archivo, `next_ref` se rellena en el adaptador como el capítulo `index+1` del índice. Si en Tasks 6 y 7 no se hizo así, ajústalo allí.
  - Al volver a abrir un documento web, `next_of` del último capítulo **siempre** consulta la red, porque puede haberse publicado un capítulo nuevo. Solo se usa la caché si el capítulo guardado ya tenía `next_locator`.

- [ ] **Paso 1:** Escribir las pruebas que deben fallar:
  - `test_prefetches_two_ahead`.
  - `test_prefetch_cache_is_bounded`: tras recorrer 20 capítulos, la caché en memoria tiene 4 elementos o menos.
  - `test_loop_detected`: con `next` de 3 → 1, se lanza `ChapterLoopDetected`.
  - `test_end_of_novel_returns_none`.
  - `test_ambiguous_raises_with_candidates`.
  - `test_network_down_waits_forever_then_returns`: `FakeSource` falla 10 veces y después funciona; `on_waiting` recibe `[True, False]` y las esperas tienen un tope de 60.
  - `test_waits_and_resumes_when_next_chapter_arrives_late`: se integra en Task 14 con el mismo nombre. Aquí solo se valida la parte del navegador.
  - `test_store_text_false_not_persisted`.
  - `test_reopen_rechecks_next_for_last_chapter`.
- [ ] **Paso 2:** Ejecutar las pruebas. Esperado: FAIL.
- [ ] **Paso 3:** Implementar.
- [ ] **Paso 4:** Ejecutar las pruebas. Esperado: PASS.
- [ ] **Paso 5:** Commit `"Añadir navegación de capítulos con precarga y espera de red"`.

### Task 14: Controlador de lectura

**Archivos:** Crear `lector/services/reading_controller.py`. Pruebas: `tests/test_resume.py` y `tests/test_reading_controller.py`.

**Interfaces:**
- **Consume:** `ChapterNavigator`, `EngineManager`, `AudioPlayer`/`QueuedClip`/`PlayerEvents`, `make_chunks`, `ProgressRepo`, `DocumentRepo`, `SettingsRepo` y `clamp_speed`.
- **Produce:**
  - `SessionState(StrEnum)`: `IDLE`, `LOADING`, `READING`, `PAUSED`, `STOPPED`, `COMPLETED`, `WAITING_NETWORK`, `NEEDS_ATTENTION` y `ERROR`. Los estados de sesión de la especificación viven aquí; los de motor están en `EngineState`.
  - `ControllerEvents(Protocol)`:
    - `on_session_state(state: SessionState)`.
    - `on_engine_state(state: EngineState, active: str)`.
    - `on_document(doc: Document, chapter: Chapter)`.
    - `on_sentence(chapter_title: str, sentence_text: str, sentence_index: int, total: int)`.
    - `on_notice(level: str, message: str)`, con `level` igual a `"info"`, `"warn"` o `"error"`.
    - `on_needs_choice(candidates: list[ChapterRef])`.
  - `class ReadingController(PlayerEvents)` con `__init__(deps: ControllerDeps, events: ControllerEvents)`. `ControllerDeps` es una dataclass con `fetcher`, `repos`, `engine_manager`, `player`, `source_opener = registry.open_source`, `navigator_factory` y `settings`.
  - Métodos públicos, todos `async`:
    - `open(locator) -> tuple[Document, Chapter, ProgressRecord | None]`: solo es la vista previa; no reproduce.
    - `play(doc_id: str, start: Position | None)`: si `start` es `None`, usa el progreso guardado o el principio.
    - `pause()`, `resume()` y `stop()`.
    - `next_sentence()` y `prev_sentence()`.
    - `skip_seconds(delta: float)`: la duración se estima como `len(text) / (14.0 * speed)` segundos por frase, y se avanza o retrocede frase a frase hasta cubrir `|delta|`.
    - `restart_chapter()`, `next_chapter()` y `prev_chapter()`.
    - `choose_next(ref: ChapterRef)`.
    - `set_voice(edge_voice: str | None, piper_voice: str | None, apply_now: bool)`.
    - `set_speed(speed: float, apply_now: bool)`.
    - `set_continuous(on: bool)`.
    - `shutdown(save_only: bool = False)`. Con `save_only=True` solo guarda el progreso confirmado y no cancela nada; se usa desde el manejador de errores no capturados (Task 19).

**Bucle de producción:** una sola tarea `_producer` por sesión.
1. Obtiene el capítulo actual y hace `make_chunks` desde `position.sentence_index`, con los límites del motor **activo** (`edge_max_chars` / `edge_max_sentences`, o `piper_max_chars` / 1).
2. Por cada fragmento: `engine_manager.synthesize` → `player.enqueue(QueuedClip(...))`.
3. Al terminar un capítulo:
   - Si `continuous` está apagado, pasa a `COMPLETED` cuando termine el audio.
   - Si no, llama a `navigator.next_of`:
     - `None` → `COMPLETED` y aviso «No hay más capítulos publicados».
     - `AmbiguousNextChapter` → `NEEDS_ATTENTION` y `on_needs_choice`.
     - `ChapterLoopDetected` → `NEEDS_ATTENTION` y aviso.
4. Si el motor activo cambia a mitad de capítulo, los fragmentos se recalculan desde la siguiente frase no encolada.

**Progreso confirmado:**
- `on_sentence_started(chunk, i)` confirma la frase **anterior**.
- `on_chunk_finished(chunk)` confirma la última frase del fragmento.
- Al confirmar se actualiza `Position`. `char_offset` es el `end` de la frase confirmada, y `page` es la de su párrafo.
- Se guarda con `ProgressRepo.save`, como mucho cada 2 s, y **siempre** en `on_chunk_finished`, `pause`, `stop` y `shutdown`.
- El **punto de reanudación** es la frase siguiente a la última confirmada.

**Interrupciones:**
- **Pausa:** `player.pause()` y guarda el progreso. El productor sigue llenando la cola hasta el límite y después se bloquea en `enqueue`.
- **`stop`, navegación manual, cambio con `apply_now=True` o cambio de motor:**
  - Cancela el productor (`task.cancel()` y `await`).
  - Llama a `player.clear()`, que devuelve el fragmento que sonaba.
  - Calcula la reanudación como el **inicio de la frase que estaba sonando** (`player.current()` antes del `clear`).
  - Lanza un productor nuevo, salvo en `stop`.
  - Nunca hay dos productores vivos a la vez: se protege con un `asyncio.Lock`.
- **Navegación manual:** guarda la nueva posición en cuanto se ejecuta.
- **Estado del motor:**
  - `WAITING_NETWORK` del gestor de motores o del navegador → `on_session_state(WAITING_NETWORK)`. Cuando se resuelve, vuelve a `READING`.
  - Un error recuperable de audio (`on_output_error`) cierra y vuelve a abrir la salida una vez. Si vuelve a fallar, pasa a `PAUSED` con un aviso.
- **Archivos:** al reanudar un PDF o EPUB, si el archivo no existe o su *hash* cambió, se lanza `FileMovedOrChanged`, que la interfaz traduce en «Vuelve a localizar el archivo».

- [ ] **Paso 1:** Escribir las pruebas que deben fallar. Usan `FakeOutput`, `FakeEdge`, `FakePiper`, `FakeClock`, `FakeSource` y una base de datos en `tmp_path`. Las de la §16 de la especificación son:
  - `test_valid_url_is_processed_and_read`: con `FakeSource` del adaptador Madara sobre las *fixtures*, se reproduce la primera frase del capítulo 99.
  - `test_pdf_is_extracted_and_read`.
  - `test_pause_resume_keeps_progress`.
  - `test_stop_cancels_pending_jobs`: después de `stop`, no hay tareas vivas del controlador (`asyncio.all_tasks()` filtrado por nombre `"lector:*"`) y `FakeEdge` no recibe más llamadas.
  - `test_restart_resumes_at_last_confirmed_sentence`: se confirman las frases 0–4 y la 5 suena a la mitad; se cierra sin `shutdown` (se simula un *crash*) y un controlador nuevo arranca en la frase 5.
  - `test_generated_vs_played_distinction`: con 3 fragmentos generados y solo 1 reproducido, el progreso queda en ese 1.
  - `test_no_audio_files_written`: tras una sesión, `tmp_path`, `data_dir()` y `tempfile.gettempdir()` no contienen archivos `*.mp3` ni `*.wav` nuevos.
  - `test_both_engines_fail_session_recoverable`: se pasa a `WAITING_NETWORK`, el progreso queda guardado y, cuando Edge vuelve, se continúa en la frase correcta.
  - `test_fallback_mid_chunk_resumes_at_current_sentence`: no se repite el párrafo.

  Del foco de revisión:
  - `test_waits_and_resumes_when_next_chapter_arrives_late`.
  - `test_pause_during_fallback_has_single_audio_stream`: como mucho hay 1 productor vivo y la cola tiene `max_ahead + 1` clips o menos.

  De controles:
  - `test_next_prev_sentence`.
  - `test_skip_seconds_moves_by_estimated_duration`.
  - `test_restart_chapter`.
  - `test_next_chapter_manual_updates_persisted_position`.
  - `test_speed_change_applies_to_next_unqueued_chunk`.
  - `test_apply_now_restarts_current_sentence`.
  - `test_continuous_off_stops_at_chapter_end`.
  - `test_file_moved_raises`.
- [ ] **Paso 2:** Ejecutar `.venv/bin/pytest tests/test_resume.py tests/test_reading_controller.py -v`. Esperado: FAIL.
- [ ] **Paso 3:** Implementar. Todas las tareas `asyncio` se crean con `name="lector:<rol>"`.
- [ ] **Paso 4:** Ejecutar las pruebas. Esperado: PASS.
- [ ] **Paso 5:** Ejecutar la suite completa, `.venv/bin/pytest -q`. Esperado: todo PASS.
- [ ] **Paso 6:** Commit `"Añadir controlador de lectura con progreso confirmado"`.

### Task 15: Puente entre asyncio y Qt

**Archivos:** Crear `lector/services/worker.py`. Pruebas: `tests/test_worker.py`.

**Interfaces:**
- **Consume:** `ControllerEvents`, `SessionState` y `EngineState`.
- **Produce:**
  - `class AsyncWorker`:
    - `start()`: lanza un hilo *daemon* con `asyncio.new_event_loop()`.
    - `loop` (propiedad).
    - `submit(coro) -> concurrent.futures.Future`: usa `asyncio.run_coroutine_threadsafe`.
    - `stop(timeout=5)`.
  - `class QtEventBridge(QObject, ControllerEvents)`. Tiene una `Signal` por cada método de `ControllerEvents`, con el mismo nombre más el sufijo `_sig`:
    - `session_state_sig(str)`.
    - `engine_state_sig(str, str)`.
    - `document_sig(object, object)`.
    - `sentence_sig(str, str, int, int)`.
    - `notice_sig(str, str)`.
    - `needs_choice_sig(object)`.

    Cada método emite su señal. Qt las entrega en el hilo de la interfaz mediante una conexión en cola.
  - `call_ui(future, on_ok, on_err)`: helper que conecta el resultado de un `Future` con *callbacks* en el hilo de la interfaz, usando `QMetaObject.invokeMethod` o una señal interna.

- [ ] **Paso 1:** Escribir las pruebas que deben fallar, con `qtbot`:
  - `test_submit_runs_on_worker_thread`: `threading.get_ident()` dentro de la corrutina es distinto del de la interfaz.
  - `test_signals_delivered_on_ui_thread`.
  - `test_ui_stays_responsive_during_slow_synthesis`: un `FakeEdge` tarda 2 s. Durante ese tiempo, un `QTimer` de 50 ms dispara 20 veces o más (`qtbot.waitUntil`).
- [ ] **Paso 2:** Ejecutar las pruebas. Esperado: FAIL.
- [ ] **Paso 3:** Implementar.
- [ ] **Paso 4:** Ejecutar las pruebas. Esperado: PASS.
- [ ] **Paso 5:** Commit `"Añadir puente entre asyncio y Qt"`.

### Task 16: Ventana principal, controles, vista previa, reanudación e historial

**Archivos:** Crear `lector/ui/main_window.py`, `playback_controls.py`, `preview_panel.py`, `resume_dialog.py` y `history_dialog.py`. Pruebas: `tests/test_ui.py`.

**Interfaces:**
- **Consume:** `AsyncWorker`, `QtEventBridge`, `ReadingController` y `SettingsRepo`.
- **Produce:** `class MainWindow(QMainWindow)` con `__init__(worker, bridge, controller, settings, models: PiperModelManager)` y la señal `close_to_tray_requested`.

**Contenido de la ventana** (diseño §5.9):
- **Fila de entrada:** un `QLineEdit` para la URL (*placeholder* «Pega la URL de un capítulo o de la novela»), el botón «Abrir PDF/EPUB…» (`QFileDialog`, filtro `*.pdf *.epub`) y el botón «Leer».
- **Vista previa:** `PreviewPanel`, visible tras abrir. Muestra el título, el capítulo, los primeros 5 párrafos, el «Siguiente detectado: …» y, solo en webs genéricas, un `QLineEdit` editable con «Prefijo de la obra».
- **Lectura en curso:** el título del documento, el capítulo y un `QLabel` grande, con ajuste de línea, que muestra la frase actual resaltada (fondo suave; el resaltado se puede desactivar en la configuración). Debajo, un `QProgressBar` (frase / total del capítulo).
- **`PlaybackControls`:** los botones Leer/Pausar (alternan), Detener, ⏮ capítulo anterior, ◀◀ frase anterior, ⟲ −Ns, ⟳ +Ns, ▶▶ frase siguiente, ⏭ capítulo siguiente y Reiniciar capítulo, más el interruptor «Lectura continua».
- **Panel de voz:**
  - Motor preferido: Edge o Piper.
  - Un `QComboBox` con las voces de Edge (de `VoicesCacheRepo`, filtradas por idioma) y otro con las voces de Piper instaladas.
  - Botones de velocidad 0.75, 1.0, 1.25, 1.5, 1.75 y 2.0, un `QDoubleSpinBox` de 0.5 a 2.5 con paso 0.05 y un *checkbox* «Aplicar ya».
- **Barra de estado:** tres indicadores:
  - **Motor activo:** «Edge TTS» o «Piper local».
  - **Edge:** «Conectado», «Reintentando…» o «Sin conexión».
  - **Piper:** «Listo (<voz>)» o «Sin voz instalada», con enlace a la configuración.
  - En `WAITING_NETWORK`, un aviso adicional: «Esperando conexión…».
- **Menú:** «Historial…» y «Configuración…».

**Diálogos:**
- `ResumeDialog`: se muestra en `open()` si hay progreso. Ofrece «Continuar desde donde lo dejé», «Empezar desde el principio» y «Elegir capítulo o página». La última opción abre una lista de capítulos y, si es PDF, un *spinbox* de página.
- `HistoryDialog`: muestra `DocumentRepo.list_recent()` en una tabla con título, tipo, capítulo y fecha. Al hacer doble clic, abre el documento.
- `AmbiguousNextChapter` y `ChapterLoopDetected`: si la ventana está visible, se elige con un `QListWidget` no modal dentro del panel. Si no, la decisión queda pendiente y se muestra al abrir la ventana.

**Reglas:**
- Ningún manejador del hilo de la interfaz hace E/S: todo pasa por `worker.submit`.
- Los mensajes de error usan `LectorError.user_message`. Cualquier otra excepción muestra «Ocurrió un problema inesperado. La lectura se ha pausado y tu progreso está guardado.».

- [ ] **Paso 1:** Escribir las pruebas que deben fallar, con `qtbot` y un controlador falso que registra las llamadas:
  - `test_read_button_opens_preview_then_plays`.
  - `test_controls_call_controller`: cada botón invoca su método.
  - `test_speed_presets_and_apply_now`.
  - `test_engine_indicator_updates_on_signal`: `engine_state_sig("PIPER_ACTIVE", "Piper local")` hace que el texto muestre «Piper local».
  - `test_resume_dialog_three_options`.
  - `test_error_shows_user_message`.
  - `test_ui_responsive_while_opening`: `open` tarda 2 s y la ventana sigue procesando eventos.
- [ ] **Paso 2:** Ejecutar las pruebas. Esperado: FAIL.
- [ ] **Paso 3:** Implementar. Estilo sobrio, con la paleta del sistema (tema claro u oscuro automático de Qt) y sin hojas de estilo complejas.
- [ ] **Paso 4:** Ejecutar las pruebas. Esperado: PASS.
- [ ] **Paso 5:** Commit `"Añadir ventana principal y controles de reproducción"`.

### Task 17: Bandeja del sistema y notificaciones sin robar el foco

**Archivos:** Crear `lector/ui/tray.py`. Modificar `lector/ui/main_window.py`. Pruebas: `tests/test_tray.py`.

**Interfaces:**
- **Consume:** `MainWindow`, `QtEventBridge` y `ReadingController` (mediante `worker.submit`).
- **Produce:** `class TrayController(QObject)` con `__init__(window, bridge, worker, controller, icon: QIcon)`, `notify(level, message)` y `pending_attention: list[tuple[str, object]]`.

**Reglas:**
- **Menú de la bandeja:** Reanudar/Pausar (el texto cambia según el estado), Frase siguiente, Capítulo siguiente, Detener, Mostrar ventana y Salir.
- **Tooltip:** `"<título> — <capítulo> · <motor activo>"`.
- **Icono:** uno sencillo dibujado con `QPainter` (un círculo con un triángulo), para no depender de archivos. El estado de pausa usa una variante.
- **Cerrar la ventana:** `closeEvent` hace `hide()` e ignora el evento; solo «Salir» cierra la app, llamando a `controller.shutdown()` y después a `worker.stop()`.
  - La primera vez muestra la notificación «Sigo leyendo en la bandeja del sistema».
- **Minimizar:** también oculta la ventana en la bandeja.
- **Notificaciones:** `notice_sig` y `needs_choice_sig` llaman a `QSystemTrayIcon.showMessage` y **nunca** a `QMessageBox`, `raise_()` ni `activateWindow()` mientras la ventana está oculta.
  - Las decisiones pendientes se guardan en `pending_attention` y se presentan en `showEvent`.
- **Clic en el icono:** muestra la ventana.

- [ ] **Paso 1:** Escribir las pruebas que deben fallar:
  - `test_close_hides_and_keeps_reading`: tras `close()`, la ventana no es visible, el controlador no recibió `stop` y `QApplication` sigue viva.
  - `test_notice_while_hidden_never_activates_window`: con `monkeypatch` sobre `QWidget.activateWindow`, `raise_` y `QMessageBox.exec`, ninguno se llama, y `showMessage` sí.
  - `test_pending_choice_shown_on_open`.
  - `test_tray_menu_actions_call_controller`.
  - `test_quit_shuts_down`.

  Si `QSystemTrayIcon.isSystemTrayAvailable()` es `False` en el entorno de pruebas, se usa un doble de `QSystemTrayIcon` inyectado.
- [ ] **Paso 2:** Ejecutar las pruebas. Esperado: FAIL.
- [ ] **Paso 3:** Implementar.
- [ ] **Paso 4:** Ejecutar las pruebas. Esperado: PASS.
- [ ] **Paso 5:** Commit `"Añadir bandeja del sistema y notificaciones"`.

### Task 18: Configuración, voces y primer arranque de Piper

**Archivos:** Crear `lector/ui/settings_dialog.py` y `lector/ui/piper_setup.py`. Modificar `main_window.py`. Pruebas: `tests/test_settings_ui.py`.

**Interfaces:**
- **Consume:** `SettingsRepo`, `PiperModelManager`, `EdgeEngine.list_voices`, `VoicesCacheRepo` y `ChapterRepo.purge_text`.
- **Produce:**
  - `class SettingsDialog(QDialog)`.
  - `class PiperSetupPanel(QWidget)`.
  - `class FirstRunBanner(QFrame)`: un aviso no modal en la parte superior de la ventana.

**Pestañas de `SettingsDialog`:**
1. **Voces.** Idioma (filtro por *locale* de Edge, `es-MX` por defecto) y voz de Edge.
   - El botón «Actualizar lista» llama a `list_voices` y guarda el resultado en caché.
   - Si `es-MX-DaliaNeural` no aparece en la lista, se avisa con «La voz Dalia no está disponible ahora; elige otra voz en español».
2. **Voces locales (Piper).**
   - Una tabla con el `CATALOG` (nombre, género, tamaño y estado), con los botones «Descargar», «Escuchar muestra» (sintetiza y reproduce en memoria) y «Usar».
   - El selector de hablante, cuando el modelo tiene varios.
   - Un texto de ayuda: qué es Piper, que funciona sin internet, que los modelos vienen de `huggingface.co/rhasspy/piper-voices`, que cada voz tiene su propia licencia (indicada en su `MODEL_CARD`) y que ninguna es igual a Dalia.
3. **Lectura.** Velocidad por defecto, tamaño de fragmentos de Edge y Piper, reintentos, tiempos del circuito, intervalo de avance y retroceso, lectura continua y resaltado.
4. **Privacidad.**
   - El texto: «Edge TTS envía el texto que se lee a los servidores de Microsoft. Piper lo procesa en tu equipo».
   - El *checkbox* «Guardar el texto de los capítulos». Al desactivarlo, se ofrece borrar el texto ya guardado con `purge_text`.

**`FirstRunBanner`:**
- Aparece si `PiperModelManager.installed()` está vacío.
- Texto: «Sin voz local, un corte de internet dejará la lectura en espera. ¿Descargar la voz de respaldo (~76 MB)?».
- Botones «Descargar» (con progreso) y «Ahora no».
- Si se rechaza, el indicador de Piper sigue mostrando «Sin voz instalada».

- [ ] **Paso 1:** Escribir las pruebas que deben fallar:
  - `test_settings_persist`.
  - `test_dalia_missing_warning`.
  - `test_first_run_banner_when_no_models`.
  - `test_banner_hidden_when_model_installed`.
  - `test_privacy_toggle_purges_text`.
  - `test_download_runs_off_ui_thread`.
- [ ] **Paso 2:** Ejecutar las pruebas. Esperado: FAIL.
- [ ] **Paso 3:** Implementar.
- [ ] **Paso 4:** Ejecutar las pruebas. Esperado: PASS.
- [ ] **Paso 5:** Commit `"Añadir configuración, voces y primer arranque de Piper"`.

### Task 19: Arranque, recuperación y registro

**Archivos:** Crear `main.py` y `lector/services/recovery.py`. Pruebas: `tests/test_recovery.py`.

**Interfaces:**
- **Consume:** todo lo anterior.
- **Produce:**
  - `recovery.check_file(doc: Document, stored_path: str) -> Path`: lanza `FileMovedOrChanged` si el archivo no existe o si cambió su tamaño o fecha de modificación y, además, el SHA-256 no coincide.
  - `recovery.relocate(doc, new_path) -> Document`: valida el *hash* y actualiza el origen.
  - `recovery.last_session(repos) -> ProgressRecord | None`.
  - `main.main() -> int`.

**Arranque en `main.py`:**
1. Configura `logging` con `RotatingFileHandler(data_dir()/"lector.log", maxBytes=2_000_000, backupCount=3)`.
2. Abre la base de datos. Si se recuperó de una corrupción, avisa con «La base de datos estaba dañada; se ha creado una nueva. Tu historial anterior se guardó como copia».
3. Crea `QApplication` con `setQuitOnLastWindowClosed(False)`.
4. Arranca `AsyncWorker`.
5. Construye en el hilo de trabajo `SafeFetcher`, los motores, `EngineManager`, `AudioPlayer(SoundDeviceOutput)` y `ReadingController`.
6. Crea `MainWindow` y `TrayController`.
7. Si hay una última sesión, muestra en la ventana el aviso «Continuar <título> — <capítulo>». **No** se reproduce sin que el usuario lo pida.

**Errores no capturados:** `sys.excepthook` y `loop.set_exception_handler` registran el error, guardan el progreso con `controller.shutdown(save_only=True)` y avisan sin cerrar la app.

- [ ] **Paso 1:** Escribir las pruebas que deben fallar:
  - `test_moved_file_detected`.
  - `test_modified_file_detected`.
  - `test_relocate_accepts_same_hash`.
  - `test_relocate_rejects_different_file`.
  - `test_last_session`.
- [ ] **Paso 2:** Ejecutar las pruebas. Esperado: FAIL.
- [ ] **Paso 3:** Implementar `recovery.py` y `main.py`.
- [ ] **Paso 4:** Ejecutar las pruebas. Esperado: PASS.
- [ ] **Paso 5:** Ejecutar la suite completa con `.venv/bin/pytest -q`. Esperado: todo PASS y 0 *warnings* de corrutinas no esperadas.
- [ ] **Paso 6:** Commit `"Añadir arranque de la app, recuperación y registro"`.

### Task 20: Prueba de humo real, README y cierre

**Archivos:** Crear `README.md` (en español) y `docs/configuracion-ejemplo.md`.

- [ ] **Paso 1:** Prueba de humo manual en el Mac, con `.venv/bin/python main.py`:
  1. Pegar `https://aresscanlationovel.com/novels/a-partir-de-ahora-pedir-matrimonio-es-la-unica-forma-de-sobrevivir/capitulo-99/`, leer y oír a Dalia.
  2. Cerrar la ventana y comprobar que sigue leyendo desde la bandeja.
  3. Desactivar el wifi: en unos 7 s pasa a «Piper local» y sigue leyendo.
  4. Reactivar el wifi: tras 2 minutos o más vuelve a «Edge TTS».
  5. Dejar que termine el capítulo 99: pasa solo al 100. Al terminar el 100, aparece la notificación «No hay más capítulos publicados».
  6. Salir y reabrir: «Continuar» retoma en la última frase.
  7. Abrir un EPUB y un PDF de prueba.

  Anota cada resultado. Si algo falla, usa superpowers:systematic-debugging antes de tocar código.
- [ ] **Paso 2:** Comprobar que no se escribió audio: `find ~/Library/Application\ Support/LectorTTS "$TMPDIR" . -name "*.mp3" -o -name "*.wav" -newer requirements.txt | grep -v tests/fixtures`. Esperado: sin resultados.
- [ ] **Paso 3:** Escribir `README.md` con estas secciones:
  - Qué es.
  - Requisitos: Python 3.12, Windows 10/11 o macOS.
  - Instalación en Windows (python.org, `py -3.12 -m venv .venv`, `.venv\Scripts\pip install -r requirements.txt`, `.venv\Scripts\python main.py`).
  - Instalación en macOS.
  - Edge TTS: qué es, que requiere internet, que no hay cuota garantizada y que envía el texto a Microsoft.
  - Piper: instalación de voces desde la app o a mano, ubicación de los modelos y licencias.
  - Uso, incluido el funcionamiento en la bandeja.
  - Cómo funciona la recuperación del progreso: lo reproducido frente a lo generado, la frase en curso y el comportamiento sin red.
  - Configuración (enlace al documento de ejemplo).
  - Problemas frecuentes: no suena nada, Edge siempre caído, Piper sin modelo, el PDF escaneado, el sitio cambió de estructura, la base de datos dañada y dónde está el log.
  - Limitaciones reales:
    - Edge puede cortar o limitar las peticiones sin aviso.
    - La pausa al cambiar de motor puede ser de unos segundos.
    - Las marcas de frase de Edge son aproximadas.
    - No hay OCR.
    - Windows no se ha probado en el desarrollo si así fue.
  - Licencias de terceros: PyMuPDF AGPL, piper-tts GPL, edge-tts LGPL y PySide6 LGPL. El código propio es MIT.
- [ ] **Paso 4:** Ejecutar `.venv/bin/pytest -q` una última vez. Esperado: todo PASS. Pega el resumen en el mensaje final al usuario.
- [ ] **Paso 5:** Commit `"Añadir README y documentación de uso"`.
- [ ] **Paso 6:** Preguntar al usuario si quiere hacer `git push` a `origin main` o abrir una rama o PR. No hacer *push* sin respuesta.
