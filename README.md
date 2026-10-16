# Lector TTS

## Qué es

Lector TTS es un programa de escritorio que **lee en voz alta novelas** mientras tú haces otra cosa, por ejemplo jugar. Puedes pegar la dirección de un capítulo de una novela web, o abrir un archivo EPUB o un PDF con texto. El programa lee el capítulo y, cuando termina, pasa solo al siguiente.

Puedes minimizar la ventana: la lectura sigue desde la **bandeja del sistema** (los iconitos junto al reloj).

Tiene dos voces:

- **Edge TTS** (voz principal, `es-MX-DaliaNeural`): suena muy natural, pero necesita internet.
- **Piper** (voz de respaldo, local): suena más sencilla, pero funciona sin internet. Si Edge falla, el programa cambia a Piper y la lectura sigue.

Lo más importante para este programa es que **la lectura no se corte**.

## Requisitos

- Python 3.12 (no sirven versiones anteriores).
- Windows 10/11 o macOS.
- Internet para la voz Edge TTS y para descargar capítulos de la web.

## Instalación en Windows

1. Instala Python 3.12 desde <https://www.python.org/downloads/>. Marca la casilla «Add python.exe to PATH» durante la instalación.
2. Descarga este proyecto y abre una ventana de PowerShell dentro de su carpeta.
3. Crea el entorno e instala lo necesario (solo se hace una vez):

   ```
   py -3.12 -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   ```

4. Abre el programa:

   ```
   .venv\Scripts\python main.py
   ```

<!-- revisar tras T18/T19 -->
Aún no se ha podido comprobar la instalación en Windows (ver «Limitaciones reales»).

## Instalación en macOS

El Python que trae el Mac es antiguo; no lo uses. Instala el 3.12 con Homebrew:

```
brew install python@3.12
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py
```

## Edge TTS

Edge TTS es el servicio de voz de Microsoft. Hay que tener en cuenta:

- **Necesita internet.** Sin conexión no funciona.
- **El texto que se lee se envía a Microsoft** para convertirlo en voz. Si no quieres eso con algún texto, usa Piper.
- **No hay una cuota garantizada.** Es un servicio gratuito no oficial: Microsoft puede limitarlo o cortarlo sin avisar.

La voz por defecto es `es-MX-DaliaNeural` (español de México).

## Piper (voz local)

Piper funciona en tu propio equipo. Nada de lo que lee sale de él. Necesita un archivo de voz (modelo) descargado.

**Voz por defecto:** `es_ES-sharvard-medium`, hablante F (unos 76 MB). Es la más ligera, para no cargar el equipo mientras juegas.

**Voces que ofrece el programa:**

| Voz | Tamaño aprox. |
|---|---|
| `es_ES-sharvard-medium` (España, dos hablantes F y M) | 76 MB |
| `es_AR-daniela-high` (Argentina, femenina) | 114 MB |
| `es_MX-claude-high` (México, género sin verificar) | 63 MB |
| `es_MX-ald-medium` (México, masculina) | 63 MB |
| `es_ES-davefx-medium` (España, masculina) | 63 MB |

**Instalar una voz desde la app:** en la primera apertura, si no hay ninguna voz local, la ventana te ofrece descargar la de respaldo. También se pueden descargar desde la configuración.
<!-- revisar tras T18/T19 -->

**Instalar una voz a mano:** descarga los dos archivos de la voz (`NOMBRE.onnx` y `NOMBRE.onnx.json`) desde <https://huggingface.co/rhasspy/piper-voices> y ponlos juntos en la carpeta de modelos:

- Windows: `%APPDATA%\LectorTTS\piper-voices\`
- macOS: `~/Library/Application Support/LectorTTS/piper-voices/`

**Licencias de las voces:** cada voz tiene su propia licencia, según el conjunto de datos con el que se entrenó. Revísala antes de usarla fuera de tu uso personal.

## Uso

1. Abre el programa.
2. Pega la dirección de un capítulo, o abre un EPUB o un PDF.
3. Antes de leer, la ventana te muestra el título, el capítulo detectado y los primeros párrafos. Pulsa para leer.
4. Puedes pausar, cambiar la velocidad, saltar de frase o de capítulo. La frase que suena se resalta.
5. Si ya habías leído ese documento, te pregunta si quieres continuar donde lo dejaste, empezar de nuevo o elegir capítulo o página.

### En la bandeja del sistema

Al cerrar o minimizar la ventana, el programa **sigue leyendo** desde la bandeja. Una vez te lo avisa con un globo («Sigo leyendo en la bandeja del sistema»). Si pasas el ratón sobre el icono, ves la obra, el capítulo y la voz en uso.

Con clic derecho en el icono tienes:

- Pausar / Reanudar
- Frase siguiente
- Capítulo siguiente
- Detener
- Mostrar ventana
- Salir

Un clic sobre el icono abre la ventana. El icono es azul si lee y naranja si está en pausa.

**Para salir de verdad** usa «Salir» del menú de la bandeja; cerrar la ventana solo la esconde.

**Los avisos no te interrumpen.** Cuando algo necesita tu atención (por ejemplo, hay que elegir el capítulo siguiente, o un PDF es escaneado), el programa lo avisa con un globo de la bandeja y espera. No abre ventanas ni se pone delante de tu juego.

## Cómo se recupera el progreso

El programa guarda **lo que ya has oído**, no lo que ya está preparado. Esto importa porque prepara el audio un poco por adelantado (hasta unos 90 segundos).

- Si cierras el programa, se va la luz o se cuelga, al volver a abrirlo con «Continuar» retomas **en la última frase que se oyó**. Como mucho se repite esa frase.
- Si Edge falla, el programa reintenta tres veces (esperando 1, 2 y 4 segundos) y luego pasa a Piper con el mismo fragmento. En total, la pausa suele ser de unos 7 segundos.
- Mientras lee con Piper, prueba Edge cada cierto tiempo: la primera vez al minuto, y si sigue sin ir, espera el doble cada vez (hasta 10 minutos). Además, espera al menos 2 minutos después de cambiar a Piper. Cuando Edge responde, vuelve a él en el siguiente corte entre fragmentos.
- **Sin internet y sin voz Piper instalada**, el programa no se rinde: pasa a «Esperando conexión», reintenta cada poco (como máximo cada 60 segundos) y **sigue solo cuando vuelve la red**.
- No se guarda ningún archivo de audio en el disco.

## Configuración

Los ajustes (velocidad, voz, tamaños de fragmento, tiempos de reintento, etc.) se cambian desde la ventana de configuración. <!-- revisar tras T18/T19 -->

La lista completa de ajustes, con su valor por defecto y lo que hace cada uno, está en [docs/configuracion-ejemplo.md](docs/configuracion-ejemplo.md).

Tus datos (progreso, historial y ajustes) están en:

- Windows: `%APPDATA%\LectorTTS\`
- macOS: `~/Library/Application Support/LectorTTS/`

## Problemas frecuentes

**No suena nada.**
Revisa el volumen del sistema y la salida de audio elegida. Mira en la ventana o en el icono de la bandeja qué voz está en uso. Si dice «Esperando conexión», no hay internet y tampoco hay voz Piper instalada: instala una (ver arriba).

**Edge siempre está caído y usa Piper.**
Puede ser tu conexión, un cortafuegos, o que Microsoft limite el servicio. Prueba más tarde. Mientras tanto, la lectura sigue con Piper y el programa volverá a Edge solo cuando responda.

**Piper dice que no hay modelo.**
Falta el archivo de voz. Descárgalo desde la app o ponlo a mano en la carpeta `piper-voices` (ver sección Piper). Los dos archivos, `.onnx` y `.onnx.json`, deben estar juntos.

**El PDF es escaneado.**
Si el PDF es una foto de las páginas, no tiene texto que leer y el programa te avisa. **No hay OCR** (reconocimiento de texto en imágenes). Busca una versión del PDF con texto seleccionable.

**El sitio cambió y ya no encuentra el capítulo.**
Si una web rediseña sus páginas, el programa puede no entender la estructura. Prueba abrir otro capítulo; si tampoco funciona, el lector de ese sitio necesita una actualización.

**La base de datos está dañada.**
Si el programa no arranca o no recuerda tu progreso, cierra el programa y renombra el archivo `lector.db` de la carpeta de datos (por ejemplo a `lector.db.viejo`). Al abrir de nuevo se crea uno limpio. Perderás el historial y los ajustes, pero no tus voces descargadas.

**¿Dónde está el log?**
El programa guarda un registro de errores (un archivo `.log` que se va rotando) para poder investigar fallos. <!-- revisar tras T18/T19 --> Se espera en la carpeta de datos, pero el nombre exacto se confirmará cuando exista `main.py`.

## Limitaciones reales

- **Windows no se ha probado.** Todo el desarrollo y las pruebas se hicieron solo en macOS. Falta una comprobación manual en Windows.
- **Pendiente de comprobar por una persona** (no se puede automatizar):
  - Escuchar las voces Dalia (Edge) y Piper para ver cómo suenan.
  - Comprobar el comportamiento real de la bandeja, y que el programa **nunca le quita el foco a un juego en pantalla completa**.
  - Comprobar que, al pasar con alt-tab a un juego, la lectura empieza o sigue.
- Edge puede cortar o limitar las peticiones sin avisar.
- Al cambiar de motor puede haber una pausa de unos segundos.
- Las marcas de frase de Edge son aproximadas: la frase resaltada puede ir un poco desfasada.
- No hay OCR: los PDF escaneados no se leen.

## Licencias de terceros

El código de este proyecto es **MIT**. Usa estas bibliotecas, cada una con su licencia:

| Biblioteca | Licencia |
|---|---|
| PyMuPDF | AGPL |
| piper-tts | GPL |
| edge-tts | LGPL |
| PySide6 | LGPL |

Para uso personal (instalar el programa y usarlo tú) no hay ningún problema. Las condiciones de la AGPL y la GPL solo importarían si alguien **distribuyera un paquete que incluya esas bibliotecas** (por ejemplo, un instalador o un .exe). En ese caso habría que cumplir sus condiciones, como ofrecer el código fuente completo.
