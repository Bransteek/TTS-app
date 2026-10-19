# Especificación original (aportada por el usuario, 2026-10-16)

Texto íntegro de la especificación inicial del proyecto. El documento de diseño `2026-10-16-lector-tts-design.md` la concreta y, donde difieren, manda el documento de diseño.

---

Proyecto: Lector inteligente de texto a voz con Edge TTS y Piper

## 1. Rol del agente

Actúa como un ingeniero de software sénior especializado en Python, aplicaciones de escritorio, procesamiento de documentos, síntesis de voz, reproducción de audio en streaming y sistemas tolerantes a fallos.

Tu tarea es diseñar e implementar una aplicación de escritorio para Windows que permita leer automáticamente contenidos extensos procedentes de páginas web, libros digitales en PDF y otros documentos compatibles.

La aplicación debe priorizar las voces de Microsoft Edge TTS y cambiar automáticamente a Piper TTS cuando Edge TTS no esté disponible, presente errores o sufra interrupciones.

No te limites a proporcionar una explicación, una maqueta o pseudocódigo. Construye una aplicación funcional, organizada, documentada y preparada para ejecutarse en Windows.

## 2. Objetivo principal

El usuario debe poder introducir una URL o seleccionar un archivo PDF y pulsar un botón para comenzar a escuchar su contenido.

La aplicación debe:

- Extraer el contenido legible y organizarlo por capítulos, secciones, párrafos y frases.
- Detectar si existe un capítulo siguiente y avanzar automáticamente.
- Continuar la lectura hasta finalizar el contenido disponible o hasta que el usuario pulse Detener.
- Recordar de forma persistente el documento, capítulo, párrafo y posición de lectura.
- Reanudar la reproducción desde el último punto guardado después de cerrar la aplicación, reiniciar Windows o sufrir un error.
- Utilizar Edge TTS como motor principal.
- Cambiar automáticamente a Piper TTS cuando Edge TTS falle.
- Permitir cambiar la voz, el idioma, la velocidad y otros ajustes desde la interfaz.
- Reproducir el audio directamente, sin obligar al usuario a guardar archivos MP3.
- Funcionar durante sesiones prolongadas con un consumo de recursos razonable.

## 3. Plataforma y tecnologías

La aplicación estará orientada principalmente a Windows 10 y Windows 11.

Utiliza preferentemente:

- Python como lenguaje principal.
- PySide6 para crear una interfaz de escritorio moderna.
- SQLite para almacenar el historial, las preferencias y el progreso de lectura.
- Edge TTS para la generación de voz en línea.
- Piper TTS como motor de voz local de respaldo.
- PyMuPDF para extraer texto de archivos PDF.
- Una biblioteca de extracción de contenido web, como Trafilatura, para obtener el texto principal de páginas HTML.
- Un sistema de reproducción de audio que permita reproducir fragmentos en memoria y controlar la reproducción.
- Un gestor asíncrono de tareas para evitar que la interfaz se congele mientras se procesa o lee contenido.

Comprueba la compatibilidad y el estado de mantenimiento de las bibliotecas antes de utilizarlas. No inventes funciones de API ni asumas que dos motores de voz tienen interfaces idénticas.

Organiza el código por módulos y separa la interfaz gráfica, la extracción de documentos, la síntesis de voz, la reproducción, el almacenamiento y la lógica de navegación.

## 4. Fuentes de contenido

### 4.1. Lectura de páginas web

El usuario podrá pegar una URL en la aplicación.

El sistema deberá:

- Validar la URL.
- Obtener el contenido de la página.
- Extraer el texto principal evitando menús, publicidad, navegación, pies de página y elementos repetidos.
- Identificar, cuando sea posible, el título, los encabezados, los capítulos y los enlaces de navegación.
- Presentar una vista previa del contenido detectado antes de comenzar la lectura.
- Conservar la URL original y la información necesaria para reanudar la sesión.

Si la página tiene enlaces de capítulos anteriores, siguientes, índice o tabla de contenidos, intenta identificarlos automáticamente.

No sigas indiscriminadamente todos los enlaces de la página. Prioriza los enlaces que pertenezcan claramente a la misma obra o serie de capítulos.

### 4.2. Lectura de PDF

El usuario podrá seleccionar un archivo PDF desde el explorador de archivos de Windows.

El sistema deberá:

- Extraer el texto respetando el orden de lectura.
- Detectar títulos, encabezados, capítulos y secciones cuando sea posible.
- Mantener la relación entre el texto extraído y el número de página original.
- Permitir comenzar desde el principio o desde una página o sección seleccionada.
- Evitar leer repetidamente encabezados, pies de página y números de página irrelevantes.
- Detectar documentos escaneados y ofrecer OCR cuando sea necesario.
- Informar al usuario si un PDF no contiene texto extraíble o requiere OCR.

El progreso debe registrar tanto el punto textual como la página original del documento.

### 4.3. Navegación automática entre capítulos

Implementa un sistema de navegación por capítulos con las siguientes prioridades:

1. Utilizar la tabla de contenidos o el índice del libro si existe.
2. Identificar enlaces de capítulo siguiente o anterior cuando se trate de una web.
3. Detectar patrones de URL consecutivas solo cuando sean razonablemente fiables.
4. Reconocer encabezados como «Capítulo 1», «Capítulo 2», «Parte I» y equivalentes.
5. Solicitar confirmación al usuario cuando haya varias rutas posibles o la detección sea ambigua.

La aplicación debe mantener un historial de capítulos visitados para evitar bucles, páginas duplicadas y repeticiones accidentales.

No debe inventar capítulos ni asumir que un enlace numéricamente consecutivo corresponde al siguiente capítulo.

Si no existe otro capítulo disponible, informa al usuario y finaliza la lectura de manera ordenada.

## 5. Sistema de voz: Edge TTS como motor principal

Implementa un proveedor de síntesis de voz para Edge TTS.

Configuración inicial recomendada:

- Voz predeterminada: es-MX-DaliaNeural.
- Idioma predeterminado: español de México.
- Velocidad inicial: 1.0x.
- Motor preferido: Edge TTS.
- Reproducción progresiva de fragmentos cuando la interfaz y la biblioteca lo permitan.

La voz Dalia debe seleccionarse por su identificador real y no por una etiqueta inventada.

Obtén y muestra las voces disponibles cuando sea posible. Si la voz solicitada no está disponible, muestra un aviso y permite seleccionar otra voz compatible.

No presupongas que Edge TTS ofrece una cuota ilimitada, disponibilidad permanente o funcionamiento sin conexión.

### 5.1. Control de velocidad

Permite ajustar la velocidad de reproducción desde la interfaz.

Incluye como mínimo: 0.75x, 1.0x, 1.25x, 1.5x, 1.75x, 2.0x.

Permite también un control deslizante o una entrada numérica para ajustes adicionales dentro de los límites admitidos por cada motor.

Utiliza la función nativa de velocidad del motor cuando corresponda. Si un motor no admite una velocidad concreta, utiliza una técnica de reproducción compatible o informa de la limitación.

No cambies el tono de la voz accidentalmente al ajustar la velocidad.

### 5.2. Control de voz durante la lectura

El usuario debe poder cambiar la voz y la velocidad desde la interfaz.

Cuando cambie la configuración durante una sesión:

- Aplica los nuevos ajustes al siguiente fragmento todavía no generado.
- Evita interrumpir innecesariamente el fragmento que se está reproduciendo.
- Si el usuario solicita aplicar los cambios inmediatamente, detén el fragmento actual de manera controlada y continúa desde el punto correcto.
- Guarda las preferencias para futuras sesiones.

## 6. Sistema de respaldo automático con Piper

Piper debe funcionar como motor local de respaldo.

El usuario podrá instalar o configurar un modelo de voz compatible y seleccionar una voz local.

Requisitos:

- Detectar si Piper está instalado y disponible.
- Comprobar si el modelo de voz seleccionado existe y puede cargarse.
- Permitir configurar una voz femenina en español como alternativa inicial.
- No afirmar que una voz Piper es exactamente igual a Dalia.
- Informar claramente de las voces y modelos instalados.
- Generar y reproducir audio en memoria siempre que la implementación lo permita.
- Evitar depender de una conexión a Internet para utilizar Piper una vez instalado y descargado el modelo.

Incluye una pantalla de configuración que explique cómo instalar el motor y descargar modelos de voz compatibles, respetando sus licencias.

## 7. Lógica de conmutación automática entre Edge TTS y Piper

Esta es una de las funciones más importantes del proyecto.

Implementa una máquina de estados explícita para gestionar los motores de voz.

Estados sugeridos: EDGE_ACTIVE, EDGE_RETRYING, PIPER_ACTIVE, EDGE_RECOVERY_TEST, PAUSED, STOPPED, COMPLETED, ERROR.

Funcionamiento requerido:

- Al comenzar, intenta utilizar Edge TTS.
- Genera fragmentos de texto moderados y los envía al motor principal.
- Reproduce el audio recibido progresivamente cuando sea posible.
- Detecta errores de conexión, tiempos de espera, respuestas vacías, errores de protocolo y fallos de síntesis.
- Ante un error recuperable, realiza un número limitado de reintentos con espera progresiva.
- Si Edge TTS continúa fallando, cambia automáticamente a Piper.
- Continúa leyendo el texto pendiente sin reiniciar el documento.
- Registra qué fragmentos se leyeron con cada motor.
- Cuando Edge TTS vuelva a estar disponible, permite comprobar su recuperación mediante una prueba breve.
- Si la prueba funciona, vuelve a utilizar Edge TTS en un límite seguro entre fragmentos.

No cambies repetidamente entre motores ante errores transitorios. Implementa un período de enfriamiento y un mecanismo de circuit breaker para evitar ciclos constantes de reconexión.

No vuelvas a leer un fragmento completo automáticamente si ya se ha reproducido parcialmente, salvo que no sea posible recuperar el punto exacto. En ese caso, prioriza continuar desde el comienzo de la frase pendiente y evita repetir párrafos enteros.

Muestra siempre el motor activo en la interfaz: «Edge TTS» o «Piper local».

### 7.1. Evitar interrupciones audibles

Implementa una cola de reproducción con búfer limitado.

Cuando Edge TTS funcione correctamente, mantén preparado el siguiente fragmento para reducir las pausas entre frases.

Si Edge TTS falla:

- Conserva el texto pendiente en memoria.
- Cancela las solicitudes fallidas que ya no sean necesarias.
- Cambia a Piper.
- Genera y reproduce el siguiente fragmento disponible.
- Mantén el progreso y la posición de lectura.
- Evita que dos motores reproduzcan audio simultáneamente.

No prometas una transición totalmente imperceptible: una caída de red puede producir una breve pausa. El objetivo es minimizarla y evitar perder el punto de lectura.

## 8. Lectura continua y segmentación del texto

El sistema debe leer textos extensos sin intentar enviar un libro entero en una única petición.

Divide el contenido por párrafos y frases, conservando la puntuación y el orden original.

Después, agrupa las frases en fragmentos adecuados para cada motor.

Respeta los límites documentados de cada servicio y biblioteca. No utilices un único tamaño de fragmento para todos los motores si sus restricciones son diferentes.

Cada fragmento debe tener un identificador estable y registrar:

- Documento de origen.
- Capítulo.
- Índice del párrafo.
- Posición inicial y final dentro del texto.
- Motor utilizado.
- Estado de generación.
- Estado de reproducción.

Mantén una cola con un tamaño limitado para controlar el consumo de memoria y evitar generar grandes cantidades de audio que el usuario todavía no ha escuchado.

Cuando termine un fragmento, comienza automáticamente el siguiente.

Cuando termine un capítulo, busca el siguiente capítulo válido.

Continúa hasta que se agote el contenido disponible o el usuario detenga la lectura.

## 9. Reproducción sin guardar archivos de audio

La aplicación no debe obligar al usuario a guardar archivos MP3 o WAV.

El audio generado debe reproducirse desde memoria o mediante un búfer temporal administrado por el sistema de reproducción.

Requisitos:

- No crear una biblioteca permanente de archivos de audio.
- Liberar los búferes de audio ya reproducidos.
- Limitar la memoria utilizada por la cola de reproducción.
- Cancelar correctamente los trabajos pendientes cuando el usuario detenga la lectura.
- Evitar fugas de memoria durante sesiones de muchas horas.
- Permitir mantener el historial de progreso sin almacenar el audio.

Se permite guardar metadatos, texto extraído y progreso de lectura en SQLite. El almacenamiento de texto completo debe poder desactivarse para los usuarios que prioricen la privacidad.

## 10. Memoria y recuperación del progreso

Utiliza SQLite para guardar el progreso de lectura de manera persistente.

Para cada sesión, almacena:

- Identificador del documento.
- Tipo de documento: URL, PDF u otro formato compatible.
- URL original o ruta del archivo.
- Título del contenido.
- Fecha de la última lectura.
- Capítulo actual.
- URL del capítulo actual, si corresponde.
- Número de página del PDF, si corresponde.
- Índice del párrafo.
- Índice de la frase.
- Desplazamiento exacto dentro del texto extraído.
- Identificador del último fragmento confirmado como reproducido.
- Preferencias de voz y velocidad.
- Estado de la sesión.

Guarda el progreso de forma periódica y en los límites entre frases o fragmentos.

No confundas el último fragmento generado con el último fragmento realmente reproducido. El sistema debe registrar el progreso de reproducción, no simplemente el progreso de generación.

Al cerrar la aplicación o producirse un error, guarda la última posición confirmada.

Al volver a abrir la aplicación, ofrece:

- «Continuar desde donde lo dejé».
- «Empezar desde el principio».
- «Elegir capítulo o página».

Si la aplicación se cierra inesperadamente, debe recuperar el progreso desde el último punto confirmado, aceptando que puede ser necesario repetir unas pocas palabras.

Para las URL, calcula una identidad estable del documento y guarda también el título y la URL canónica cuando esté disponible.

Para los PDF locales, detecta si el archivo se ha movido o modificado y solicita al usuario volver a localizarlo cuando sea necesario.

## 11. Controles de reproducción

Incluye los siguientes controles:

- Leer.
- Pausar.
- Reanudar.
- Detener.
- Ir a la frase anterior.
- Ir a la frase siguiente.
- Retroceder un intervalo configurable.
- Avanzar un intervalo configurable.
- Reiniciar el capítulo.
- Ir al capítulo anterior.
- Ir al capítulo siguiente.
- Continuar automáticamente al terminar un capítulo.

Al pausar, detén la reproducción audible y conserva la cola y el progreso.

Al reanudar, continúa desde el punto correcto.

Al detener, cancela la reproducción y las solicitudes pendientes sin borrar el progreso guardado.

La navegación manual debe actualizar la posición persistente para que la siguiente sesión continúe desde el nuevo punto.

## 12. Interfaz gráfica

Diseña una interfaz limpia, moderna y fácil de utilizar en Windows.

La ventana principal debe incluir:

- Campo para introducir una URL.
- Botón para seleccionar un PDF.
- Botón para comenzar la lectura.
- Título del documento.
- Nombre del capítulo actual.
- Texto de la frase que se está leyendo, con resaltado visual opcional.
- Barra de progreso.
- Controles de reproducción.
- Selector de motor de voz.
- Selector de voz.
- Control de velocidad.
- Indicador de conexión a Edge TTS.
- Indicador del estado de Piper.
- Indicador de lectura continua.
- Acceso al historial y a las sesiones anteriores.

Incluye una pantalla de configuración para administrar voces, modelos locales, idioma, velocidad, límites de fragmentos, reintentos y preferencias de privacidad.

La interfaz debe permanecer receptiva durante la descarga de páginas, la extracción de PDF, la generación de voz y la reproducción.

Todos los mensajes de error deben ser comprensibles para un usuario no técnico.

## 13. Seguridad y privacidad

Al procesar URL:

- Bloquea solicitudes a localhost, redes privadas y direcciones internas no autorizadas para prevenir SSRF.
- Valida redirecciones y destinos de las solicitudes.
- Limita el tamaño de las páginas descargadas.
- Evita ejecutar scripts o contenido activo de páginas web.
- No sigas automáticamente enlaces externos que no correspondan a la obra.

Para los PDF:

- Trata el documento como contenido no confiable.
- Evita ejecutar contenido incrustado.
- Gestiona errores de extracción sin bloquear la aplicación.

No envíes el texto a servicios externos distintos del motor de voz seleccionado.

Indica claramente que Edge TTS transmite el texto al servicio en línea y que Piper puede procesarlo localmente.

No almacenes credenciales innecesarias ni expongas el servidor local en la red sin una configuración explícita y segura.

## 14. Gestión de errores

Implementa errores específicos para:

- URL no válida.
- Página inaccesible.
- Página sin texto extraíble.
- Capítulo siguiente no encontrado.
- Bucle de capítulos detectado.
- PDF dañado o protegido.
- PDF escaneado que requiere OCR.
- Edge TTS no disponible.
- Voz Edge TTS no encontrada.
- Modelo Piper no instalado.
- Fallo de síntesis.
- Error de reproducción de audio.
- Falta de espacio o memoria.
- Base de datos bloqueada o dañada.

Los errores recuperables no deben terminar toda la sesión de lectura.

Si ambos motores fallan, pausa la sesión, guarda el progreso y permite reanudarla posteriormente.

## 15. Arquitectura sugerida

Organiza el proyecto con una estructura similar a esta:

```
reader_app/
  main.py
  requirements.txt
  README.md
  src/
    ui/
      main_window.py
      settings_dialog.py
      playback_controls.py
    content/
      web_extractor.py
      pdf_extractor.py
      chapter_navigator.py
      text_segmenter.py
    tts/
      base_engine.py
      edge_engine.py
      piper_engine.py
      engine_manager.py
    audio/
      playback_queue.py
      audio_player.py
    persistence/
      database.py
      progress_repository.py
      settings_repository.py
    services/
      reading_controller.py
      recovery_service.py
    security/
      url_validator.py
  tests/
    test_text_segmenter.py
    test_progress.py
    test_chapter_navigation.py
    test_engine_fallback.py
    test_resume.py
```

Puedes modificar la estructura si existe una razón técnica clara, pero conserva la separación de responsabilidades.

## 16. Pruebas obligatorias

Implementa pruebas automatizadas para verificar que:

- Una URL válida se puede procesar y leer.
- Un PDF con texto se puede extraer y leer.
- La aplicación detecta correctamente capítulos y enlaces siguientes.
- Los ciclos de navegación no producen lecturas infinitas.
- Edge TTS es el motor inicial.
- Un error de Edge TTS provoca el cambio a Piper.
- Piper continúa desde el fragmento pendiente.
- Edge TTS puede volver a utilizarse después de recuperarse.
- Pausar y reanudar conserva el progreso.
- Detener cancela los trabajos pendientes.
- Reiniciar la aplicación recupera el último punto confirmado.
- El sistema no guarda archivos de audio permanentes.
- La cola de reproducción tiene un tamaño limitado.
- Los fallos de ambos motores permiten recuperar la sesión.
- La interfaz no se bloquea durante la generación de audio.

Utiliza dobles de prueba o motores simulados para las pruebas automatizadas. No dependas de una conexión real a Microsoft para ejecutar toda la suite de pruebas.

## 17. Entrega final

Entrega:

- Código fuente completo.
- Aplicación funcional para Windows.
- Archivo de dependencias.
- Instrucciones de instalación y ejecución.
- Guía de instalación de Piper y sus voces.
- Instrucciones para configurar Edge TTS.
- Explicación del sistema de recuperación del progreso.
- Pruebas automatizadas.
- Ejemplos de configuración.
- Diagnóstico de problemas frecuentes.

No afirmes que una función está implementada si solo existe su interfaz visual.

Antes de finalizar, ejecuta las pruebas disponibles, corrige los errores encontrados y documenta las limitaciones reales de los motores de voz.

## 18. Orden de implementación

Trabaja en este orden:

1. Crear la estructura del proyecto y una ventana funcional.
2. Implementar la extracción de texto de PDF y páginas web.
3. Implementar Edge TTS y verificar la reproducción sin archivos permanentes.
4. Implementar Piper y comprobar la generación local.
5. Implementar la cola de reproducción y el cambio automático entre motores.
6. Implementar la detección de capítulos siguientes.
7. Implementar el guardado y la recuperación del progreso.
8. Añadir los controles de reproducción y configuración.
9. Implementar las medidas de seguridad y recuperación de errores.
10. Ejecutar las pruebas y documentar la instalación.

Prioriza una aplicación funcional y robusta sobre funciones decorativas. Si una integración presenta una limitación técnica, explica el problema y ofrece una solución comprobable en lugar de simular que funciona.
