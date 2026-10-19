# Lector TTS de novelas — Documento de diseño

Fecha: 2026-10-16
Repo: https://github.com/Bransteek/TTS-app (licencia MIT)
Especificación original: [`2026-10-16-especificacion-original.md`](2026-10-16-especificacion-original.md) (secciones 1–18, texto íntegro del usuario). Sigue siendo la referencia de requisitos. Este documento la concreta, fija prioridades y registra las decisiones tomadas. Donde este documento y la especificación difieran, manda este documento.

Convención: **[usuario]** = lo decidió el usuario. **[decisión de diseño]** = lo decidí yo; el usuario puede revisarlo. Lo que no lleva marca viene de la especificación original.

---

## 1. Intención

El usuario lee novelas web por capítulos (caso real: https://aresscanlationovel.com/) y, a veces, novelas ligeras japonesas en PDF o EPUB. Quiere **escucharlas mientras juega o hace otra cosa, con la app minimizada**, capítulo tras capítulo, sin tocar nada. **[usuario]**

El criterio de éxito es la **continuidad desatendida**: la lectura no se corta y, si algo falla, sigue sola.

## 2. Objetivos, no objetivos y antiobjetivos

### Objetivos
- Leer en voz alta novelas web por capítulos, PDF con texto y EPUB **[usuario: EPUB añadido]**, avanzando solo al capítulo siguiente.
- Usar Edge TTS (`es-MX-DaliaNeural`) como motor principal y Piper local como respaldo automático.
- Guardar el progreso **reproducido** (no el generado) y reanudar en la misma frase tras cerrar la app, reiniciar o sufrir un fallo.
- Funcionar minimizada en la bandeja del sistema. **[usuario]**
- Plataforma principal: Windows 10/11. También debe funcionar en macOS. **[usuario]**

### No objetivos (fuera de esta versión)
- Atajos de teclado globales y teclas multimedia. Por ahora, el control es solo desde la bandeja y la ventana. **[usuario]**
- OCR. Los PDF escaneados se detectan y se informa al usuario, pero no se procesan. **[decisión de diseño — el usuario nunca ha encontrado PDF escaneados]**
- Instalador `.exe` o `.app` empaquetado. Se ejecuta con Python desde un entorno virtual. **[decisión de diseño — uso personal; el usuario no lo objetó]**
- Sincronización entre equipos, cuentas de usuario o servidor local.
- Linux. No se prohíbe, pero no se prueba.
- Leer textos en otros idiomas. Todo el contenido es **en español**, incluidas las novelas ligeras japonesas, que el usuario lee traducidas. **[usuario]** Por eso no se incluyen voces ni separadores de frases japoneses. El ajuste «Idioma» solo filtra la lista de voces de Edge.

### Antiobjetivos (sería un fracaso aunque "funcione")
- **Que la lectura se corte y no siga sola.** **[usuario]** Es el criterio principal: cualquier decisión de diseño se resuelve a favor de la continuidad.
- Que la app robe el foco o abra ventanas encima del juego.
- Que invente capítulos, se salte uno o entre en bucle.
- Que repita párrafos enteros tras un fallo.
- Que consuma tanta CPU o memoria que afecte a un juego.

## 3. Restricciones

- **Python 3.12.** Las versiones actuales de PySide6 6.12 y PyMuPDF 1.28 piden ≥ 3.10, y numpy 2.5 pide ≥ 3.12. En el Mac de desarrollo solo está Python 3.9 del sistema, así que hay que instalar 3.12. En Windows, el README indicará cómo instalar Python 3.12 desde python.org.
- `requirements.txt` con versiones fijadas. Bibliotecas verificadas en PyPI el 2026-10-16:

| Uso | Biblioteca | Versión | Licencia | Nota |
|---|---|---|---|---|
| UI | PySide6 | 6.12.0 | LGPL-3 | `QSystemTrayIcon` para la bandeja y las notificaciones |
| Edge TTS | edge-tts | 7.3.1 | LGPL-3 | `Communicate(text, voice, rate="+0%", boundary="SentenceBoundary")`, `stream()` async, `list_voices()`. Salida: MP3 24 kHz mono 48 kbps |
| Piper | piper-tts | 1.8.0 | GPL-3 | Wheels para win_amd64, macOS arm64/x86_64; incluye espeak-ng. `PiperVoice.load(model_path)`, `synthesize()` → `AudioChunk` (int16), `SynthesisConfig(length_scale=…)` |
| PDF | PyMuPDF | 1.28.2 | AGPL-3 | Texto con orden de lectura, índice (`get_toc`), número de página |
| Web genérica | trafilatura | 2.3.1 | Apache-2.0 | Extracción del texto principal en sitios sin adaptador |
| HTML | lxml (+ selectores CSS) | 6.1.3 | BSD | Adaptador Madara y EPUB |
| HTTP | httpx | 0.28.1 | BSD | Descargas con validación SSRF |
| Decodificar MP3 | miniaudio | 1.71 | MIT | `decode()` en memoria a PCM |
| Salida de audio | sounddevice | 0.5.6 | MIT | PortAudio; flujo de salida con *callback* que permite saber exactamente qué muestra suena |
| Pruebas | pytest, pytest-qt, pytest-asyncio | — | — | |

- **Licencias.** El código propio sigue siendo MIT. PyMuPDF (AGPL) y piper-tts (GPL) solo afectarían si se **distribuyera** un paquete que los incluya; para uso personal desde el código fuente no hay problema. El README lo dejará documentado. **[decisión de diseño]**
- **EPUB sin EbookLib.** EbookLib es AGPL y lleva un año sin actualizarse. Un EPUB es un ZIP con XHTML, así que se lee con `zipfile` + `lxml`: se lee el *spine* del OPF y el índice `nav.xhtml` o NCX. **[decisión de diseño]**
- **Las pruebas no dependen de la red.** Se usan motores simulados y HTML guardado como *fixtures*.
- **Windows no se puede probar en esta máquina.** El desarrollo y las pruebas se hacen en macOS. En Windows hay que hacer una prueba manual y documentar en el README lo que no se haya verificado.

## 4. Caso principal: aresscanlationovel.com (reconocimiento hecho el 2026-10-16)

- Es WordPress con el tema **Madara**, alojado en Hostinger. **No tiene Cloudflare ni captcha**: responde 200 con un User-Agent de navegador normal.
- **Texto del capítulo.** Está dentro de `.reading-content` como `<p>` normales: 117 párrafos en el capítulo 99, sin imágenes. Los diálogos empiezan con «—». No hace falta ejecutar JavaScript.
- **Navegación.** Cada capítulo tiene `a.next_page` y `a.prev_page` con la URL absoluta. En el último capítulo publicado (el 100) solo existe `prev_page`, así que la ausencia de `next_page` significa que no hay más capítulos.
- **Índice.** La página de la novela no incluye la lista de capítulos; se obtiene con `POST {url_novela}ajax/chapters/`. Esa petición devolvió los 100 capítulos como `li.wp-manga-chapter.free-chap`, ordenados de más reciente a más antiguo.
- **URLs.** Siguen el patrón `/novels/{slug}/capitulo-N/`. Este patrón **no** se usa para adivinar capítulos; solo como señal secundaria de confirmación.
- **Entrada del usuario.** Puede pegar la URL de un capítulo o la URL de la novela. Si es la de la novela, se carga el índice y se le deja elegir el capítulo, con el capítulo 1 como opción por defecto.

## 5. Arquitectura

La estructura de la sección 15 de la especificación se mantiene, con estos añadidos:
- `content/sources/`: adaptadores de sitio.
- `content/epub_extractor.py`.
- `ui/tray.py`.
- `services/prefetcher.py`.

### 5.1 Hilos y concurrencia **[decisión de diseño]**

- **Hilo principal de Qt.** Solo se ocupa de la interfaz.
- **Hilo de trabajo con un bucle `asyncio` propio.** Hace las descargas, Edge TTS, la sincronización de Piper (dentro de `run_in_executor`, porque es CPU) y el controlador de lectura. Se comunica con la interfaz mediante señales de Qt: comandos hacia el hilo de trabajo y eventos de estado hacia la interfaz.
- **Hilo de audio de PortAudio.** Lo crea sounddevice. Su *callback* solo copia PCM de un búfer y nunca bloquea.

Así la interfaz no se congela, sin depender de `qasync`.

### 5.2 Fuentes de contenido

Interfaz común `ContentSource`:
- `load(entrada) -> Document`
- `get_chapter(ref) -> Chapter` (con párrafos y metadatos)
- `next_ref(chapter)` y `prev_ref(chapter)`
- `list_chapters()` (opcional)

Adaptadores:
1. **MadaraSource.** Se detecta por marcadores del tema Madara: `wp-content/themes/madara`, `.reading-content` y `a.next_page`/`a.prev_page`. Aplica las reglas de la §4. Prioridad 1.
2. **GenericWebSource.** Usa trafilatura para el texto. Busca el capítulo siguiente en este orden:
   1. `<link rel="next">` o `<a rel="next">`.
   2. Un enlace cuyo texto sea «Siguiente», «Capítulo siguiente», «Next», «Next chapter», «»» o «→».
   3. Un patrón de URL numérico, solo como confirmación de lo anterior.

   Solo acepta enlaces del **mismo host y con el mismo prefijo de ruta de la obra**. Si hay varios candidatos que no coinciden entre sí, el caso es **ambiguo** y se trata según la §7, punto 4.

   **Identidad de la obra en sitios genéricos** **[decisión de diseño]**:
   - Por defecto, el prefijo de la obra es la ruta padre de la URL del capítulo (`/novela/x/capitulo-5/` → `/novela/x/`). Si coincide con un `<link rel="canonical">` o con un enlace de índice de la página, esa coincidencia lo confirma.
   - La identidad del documento es `host + prefijo`.
   - La **vista previa** muestra el prefijo detectado y el usuario puede corregirlo antes de empezar a leer. La corrección se recuerda para ese host.
3. **PdfSource** (PyMuPDF):
   - **Capítulos.** Usa el índice del PDF (`get_toc`). Si no hay índice, busca encabezados con expresiones regulares como «Capítulo N», «Prólogo», «Epílogo», «Interludio», «Parte I» o «Volumen», apoyándose en el tamaño de letra.
   - **Limpieza.** Quita encabezados, pies y números de página: líneas que se repiten en la misma zona de muchas páginas.
   - **Ilustraciones.** Salta las páginas sin texto.
   - **Escaneados.** Si casi ninguna página tiene texto extraíble, avisa de que el PDF es escaneado y no lo lee.
   - **Página real.** Cada párrafo guarda su número de página.
4. **EpubSource** (`zipfile` + `lxml`):
   - **Capítulos.** Recorre el *spine* en orden. Los títulos salen del índice `nav`/NCX; si no hay, del primer `h1`–`h3`.
   - **Limpieza.** Elimina `<rt>`/`<rp>` (por si una traducción conserva notas de lectura con furigana) y salta los documentos que solo tienen imágenes.
   - **Seguridad.** Nunca ejecuta scripts del EPUB. Rechaza rutas `..` y no sigue entidades externas de XML.

### 5.3 Segmentación

- Divide el texto en párrafos y luego en frases, sin perder la puntuación ni los diálogos con «—».
- Agrupa las frases en **fragmentos por motor**:
  - **Edge:** de 1 a 3 frases y unos 600 caracteres como máximo. Un primer fragmento corto ayuda a que el audio empiece rápido. edge-tts divide internamente a 4096 bytes, pero los fragmentos se mantienen mucho más pequeños para reducir la latencia y la pérdida si falla una petición.
  - **Piper:** una frase por fragmento, o varias frases cortas juntas.
- Cada fragmento tiene un id estable: `doc_id:chapter_idx:para_idx:sent_start-sent_end`. También guarda los desplazamientos de texto, el motor usado y su estado de generación y de reproducción.
- Los valores exactos son ajustables y deben aparecer en la configuración.

### 5.4 Motores TTS

Interfaz común `TtsEngine`:
- `async synthesize(text, voice, speed) -> AudioClip`. El resultado tiene PCM int16, la frecuencia de muestreo y las marcas de frase (offset en muestras → índice de frase), cuando el motor las da.
- `async health_check()`.
- `list_voices()`.

**Edge**
- Hace *stream* del MP3 completo del fragmento y lo decodifica en memoria con miniaudio.
- Las marcas `SentenceBoundary` dan el offset de cada frase.
- **Velocidad:** `rate=f"{round((speed-1)*100):+d}%"`. El tono no se toca.
- Si una respuesta llega vacía, se trata como error.

**Piper**
- Carga el modelo una sola vez y lo mantiene cargado.
- **Velocidad:** `length_scale = 1/speed`. El tono no cambia.
- Las marcas de frase son triviales, porque se sintetiza frase a frase.

**Límites de velocidad**
- Valores rápidos de la interfaz: 0.75, 1.0, 1.25, 1.5, 1.75 y 2.0, más un deslizador.
- Los límites reales de cada motor se verifican de forma empírica y se recortan a ellos. Si un valor no es posible, se avisa al usuario.
- Nunca se cambia la velocidad estirando el audio.

**Remuestreo**
- Todo el audio se lleva a una frecuencia de salida fija, 24 kHz por ejemplo, antes de ponerlo en la cola. Así solo hace falta un flujo de salida y el cambio de motor no reabre el dispositivo.

**Voz Piper por defecto**
- Opciones de voz femenina en español: `es_AR-daniela-high` (unos 114 MB), `es_ES-sharvard-medium` (hablante F) y `es_MX-claude-high` (su género está sin verificar).
- Se elige por escucha: hay que generar una muestra de cada una y que la escuche el usuario. Hasta entonces, la voz por defecto es `es_ES-sharvard-medium` hablante F, porque es *medium* y consume menos CPU mientras se juega. **[decisión de diseño, provisional]**
- La app nunca dice que se parezca a Dalia.
- **Primer arranque.** Si no hay ningún modelo Piper instalado, la ventana muestra un aviso no modal: «Sin voz local, un corte de internet dejará la lectura en espera. ¿Descargar la voz de respaldo (~76 MB)?». Al aceptar, se descarga el modelo por defecto. Esto se ofrece en el primer arranque porque en ese momento la ventana está abierta y el usuario no está jugando. Si lo rechaza, el aviso sigue visible en el indicador de Piper. **[decisión de diseño — el respaldo es clave para el antiobjetivo principal]**

**Descarga de modelos**
- La pantalla de configuración permite descargar modelos de `huggingface.co/rhasspy/piper-voices` (cada uno es un `.onnx` más su `.onnx.json`).
- Muestra el tamaño y la licencia antes de descargar.
- Los modelos se guardan en la carpeta de datos de la app.

### 5.5 Máquina de estados de motores

Estados:
- Los de la especificación: `EDGE_ACTIVE`, `EDGE_RETRYING`, `PIPER_ACTIVE`, `EDGE_RECOVERY_TEST`, `PAUSED`, `STOPPED`, `COMPLETED` y `ERROR`.
- Uno nuevo: **`WAITING_NETWORK`**.

Reglas **[decisión de diseño sobre los números; ajustables]**:

- **Reintentos de Edge.** Si Edge falla (conexión, *timeout* de 15 s, respuesta vacía, error de protocolo), hay **3 reintentos** con espera de 1 s, 2 s y 4 s. Si sigue fallando, el circuito se abre y se pasa a `PIPER_ACTIVE` en el **siguiente fragmento pendiente**.
- **Prueba de recuperación.** En `PIPER_ACTIVE`, se hace una prueba breve de Edge sintetizando una palabra:
  - La primera vez, a los 60 s.
  - Si falla, el intervalo se duplica hasta un máximo de 10 min.
  - Si la prueba sale bien, vuelve a Edge en el siguiente límite entre fragmentos.
  - Después de cambiar a Piper, debe pasar un enfriamiento mínimo de 2 min antes de volver a Edge.
- **Fallos ambos motores.** Si fallan Edge y Piper a la vez (por ejemplo, porque no hay modelo Piper instalado), el estado pasa a `WAITING_NETWORK`:
  - Se reintenta Edge con espera creciente hasta un **máximo de 60 s, sin límite de intentos**.
  - En cuanto funcione, la lectura se reanuda sola. **[usuario: "que siga intentando sola y retome cuando vuelva"]**
  - La bandeja muestra «Esperando conexión…».
  - Este estado sustituye al «pausar» de la §14 de la especificación.
- **Errores de configuración.** Si el fallo es de configuración y no de red (por ejemplo, la voz de Edge no existe), no se reintenta sin fin. Se pasa a Piper y se notifica una sola vez.
- **Un solo motor activo.** Nunca suenan dos motores a la vez. Al cambiar de motor, las peticiones del otro se cancelan y el audio pendiente que ya no se necesita se descarta.
- **Registro.** Cada fragmento reproducido queda registrado con el motor que se usó.

### 5.6 Cola y reproducción

- **Cola limitada.** Contiene como máximo el fragmento que suena más 2 fragmentos preparados, y nunca más de unos 90 s de audio. Los búferes ya reproducidos se liberan.
- **Sin archivos de audio.** No se escribe audio en disco en ningún momento. Hay una prueba que lo verifica.
- **Seguimiento exacto.** El reproductor sabe en todo momento qué muestra está sonando y, con las marcas, **qué frase se está oyendo**. Esto alimenta el resaltado y el progreso confirmado.
- **Pausa.** Detiene el flujo de salida y conserva la cola y la posición dentro del fragmento.
- **Reanudar.** Continúa desde la misma muestra.
- **Detener.** Vacía la cola, cancela las tareas y guarda el progreso.
- **Cambio de voz o velocidad.**
  - Por defecto, se aplica al siguiente fragmento todavía no generado: la cola preparada se descarta y se regenera.
  - Con «Aplicar ya», se corta el fragmento actual y se continúa desde el **inicio de la frase que estaba sonando**.
- **Si se corta un fragmento** (por error o por un cambio de motor), la lectura continúa desde el inicio de la frase que estaba sonando, nunca desde el inicio del fragmento ni del párrafo.

### 5.7 Navegación y precarga

- **Precarga.** Mientras suena el capítulo N, se descargan y extraen **N+1 y N+2**, se guardan en memoria y en SQLite (salvo que el modo privacidad lo impida). **[decisión de diseño, derivada del antiobjetivo principal]**
- **Fallos de descarga.**
  - Si falla la precarga, se reintenta con espera creciente (máximo 60 s) **sin límite**.
  - Si se acaba el texto disponible y el capítulo siguiente aún no se ha descargado, el estado pasa a `WAITING_NETWORK` y la lectura retoma sola en cuanto llegue. **[usuario]**
- **Historial de capítulos.** Se guardan las URL canónicas visitadas. Si un «siguiente» apunta a un capítulo ya visitado en esta cadena, se trata como **bucle**: se detiene y se notifica.
- **Final de la novela.** Si un capítulo no tiene «siguiente», la lectura pasa a `COMPLETED` con una notificación del tipo «No hay más capítulos publicados».
- **Volver más tarde.** Al reabrir la sesión otro día, se vuelve a consultar el «siguiente» del último capítulo, porque puede haberse publicado uno nuevo.

### 5.8 Persistencia (SQLite en modo WAL)

**Ubicación**
- Windows: `%APPDATA%\LectorTTS\`.
- macOS: `~/Library/Application Support/LectorTTS/`.

**Tablas**
- `documents`: id estable, tipo (url/pdf/epub), origen, URL canónica, título, *hash* del archivo, tamaño y fecha de modificación.
- `chapters`: id del documento, índice, título, URL o página inicial, y texto (opcional por privacidad).
- `progress`: una fila por documento, con todos los campos de la §10 de la especificación más el motor y la velocidad.
- `chunk_log`: id del fragmento, motor, momento y resultado. Es una tabla rotativa que se poda a los últimos N días.
- `settings` y `voices_cache`.

**Identidad del documento**
- URL: se usa la URL canónica normalizada de la **novela**, no la del capítulo.
- PDF y EPUB: se usa el *hash* SHA-256 del contenido. Si la ruta guardada ya no existe o el *hash* cambió, se pide al usuario que vuelva a localizar el archivo.

**Progreso confirmado**
- Solo avanza cuando el reproductor confirma que una frase **sonó completa**.
- Se guarda como mucho una vez cada 2 s, y siempre al terminar un fragmento, al pausar, al detener o al cerrar.
- Tras un cierre inesperado se repite, como mucho, la frase en curso.

**Errores de la base de datos**
- Si la base de datos está bloqueada, se reintenta.
- Si está dañada, se renombra a `.corrupt`, se crea una nueva y se avisa al usuario.

**Privacidad**
- Hay una opción para no guardar el texto extraído. En ese caso solo se guardan los metadatos y el progreso, y la precarga vive solo en memoria.

### 5.9 Interfaz

**Ventana principal**
- Campo de URL y botones «Abrir PDF/EPUB» y «Leer».
- Título del documento, capítulo actual y la frase en curso resaltada.
- Barra de progreso.
- Controles: leer/pausar/reanudar, detener, frase anterior/siguiente, retroceder/avanzar un intervalo configurable, reiniciar capítulo y capítulo anterior/siguiente.
- Interruptor de lectura continua.
- Selectores de motor preferido, voz Edge, voz Piper y velocidad.
- Indicadores de Edge (conectado / reintentando / caído), de Piper (listo / sin modelo) y del motor activo («Edge TTS» o «Piper local»).
- Acceso al historial.

**Vista previa**
- Antes de leer, muestra el título, el capítulo detectado, los primeros párrafos y el «siguiente» detectado.

**Al abrir un documento ya leído**
- Ofrece «Continuar desde donde lo dejé», «Empezar desde el principio» y «Elegir capítulo o página».

**Bandeja del sistema** **[usuario: control solo por iconos]**
- Al cerrar o minimizar, la app va a la bandeja y **sigue leyendo**.
- El menú de la bandeja tiene: reanudar/pausar, frase siguiente, capítulo siguiente, detener, mostrar ventana y salir.
- El *tooltip* muestra el capítulo y el motor.

**Sin robar el foco**
- Cualquier evento que requiera atención (capítulo ambiguo, bucle, final, PDF escaneado, fallo de configuración) se avisa con `QSystemTrayIcon.showMessage`.
- Nunca se abre un diálogo modal mientras la app está minimizada.
- Si hace falta una decisión, se pausa y queda pendiente para cuando el usuario abra la ventana.

**Configuración**
- Voces y modelos Piper (estado, descarga y guía de instalación con licencias).
- Idioma, velocidad, tamaño de fragmentos, reintentos y tiempos del circuito.
- Privacidad, con el aviso de que **Edge TTS envía el texto a Microsoft** y Piper lo procesa localmente.
- Intervalo de avance y retroceso.

**Mensajes**
- Todos los mensajes al usuario van en español, en lenguaje no técnico.

### 5.10 Seguridad

**Validación de URL (anti-SSRF)**
- Solo se aceptan `http` y `https`.
- Se resuelve el DNS y se rechazan las direcciones privadas, *loopback*, *link-local*, *multicast* y reservadas, tanto IPv4 como IPv6.
- Se conecta a la IP ya validada, para evitar *DNS rebinding*.
- Las redirecciones se siguen manualmente (5 como máximo) y cada salto se valida.
- Límite de 5 MB por respuesta y *timeouts* de conexión y lectura.
- Nunca se ejecuta JavaScript.

**PDF y EPUB**
- Se tratan como contenido no confiable. Los errores de extracción quedan aislados y no tumban la app.

**Envío de texto**
- El texto solo sale hacia el motor Edge, y únicamente si está activo.

### 5.11 Errores

- Hay un error específico para cada caso de la §14 de la especificación, cada uno con un mensaje para el usuario.
- **Errores recuperables:** red, un capítulo concreto o un fragmento. Nunca terminan la sesión. Se registran en un archivo de log rotativo y la lectura continúa o espera.
- **Errores no recuperables:** documento dañado, bucle o ambigüedad. Pausan, guardan el progreso y notifican.

## 6. Pruebas

Se cubre la lista completa de la §16 de la especificación, con motores simulados (`FakeEdge` y `FakePiper`, con fallos programables) y HTML real del sitio guardado como *fixture*: capítulos 1, 99 y 100 y la respuesta AJAX del índice.

Pruebas añadidas:
- Adaptador Madara:
  - Detecta `next_page` y `prev_page` y el último capítulo sin «siguiente».
  - Parsea el índice AJAX.
  - Reconoce los diálogos con «—».
- EPUB: elimina el furigana, salta las páginas de imágenes y sigue el orden del *spine*.
- Corte de red:
  - Sin precarga: el estado pasa a `WAITING_NETWORK` y la lectura se reanuda sola al volver la red.
  - Con precarga: la lectura sigue sin pausa.
- Cambio de motor a mitad de un fragmento: se reanuda desde el inicio de la frase en curso y no se repite el párrafo.
- Cola limitada y sin archivos de audio en disco: se comprueba que el directorio temporal y el de datos no contienen `.mp3` ni `.wav`.
- Interfaz: un `qtbot` comprueba que el bucle de eventos responde mientras un motor simulado tarda.

Además, una **prueba manual de humo** en Mac antes de entregar: leer de verdad el capítulo 99 del sitio con Edge, desconectar la red, comprobar que pasa a Piper y reconectar.

## 7. Qué queda a criterio del constructor

1. La lista exacta de módulos y nombres de clases, dentro de la arquitectura de la §5.
2. Los valores exactos de tamaño de fragmento, *timeouts* y tamaño de cola, empezando por los propuestos.
3. El algoritmo de división en frases. Basta con expresiones regulares con reglas para abreviaturas en español; no hace falta NLP.
4. **Capítulo ambiguo:** pausa más notificación. Al abrir la ventana se muestran los candidatos para que el usuario elija, y esa elección se recuerda para la obra.
5. El diseño visual, que debe ser limpio y sobrio, con tema claro u oscuro según el sistema.
6. El orden interno de implementación, que debe seguir la §18 de la especificación, priorizando el flujo completo del sitio Madara antes que PDF y EPUB.

## 8. Entrega

- El código fuente en el repo.
- `requirements.txt`.
- Un README en español con:
  - Instalación en Windows y macOS.
  - Instalación de Piper y sus voces.
  - Configuración de Edge.
  - Cómo funciona la recuperación del progreso.
  - Ejemplos de configuración.
  - Diagnóstico de problemas frecuentes.
  - Limitaciones reales y licencias.
- Pruebas automáticas que pasen en macOS.
- Indicación explícita de lo que no se haya verificado en Windows.
