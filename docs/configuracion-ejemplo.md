# Ajustes de ejemplo

Estos son todos los ajustes del programa con su **valor por defecto**. Los valores por defecto sirven bien: solo cambia algo si tienes un motivo.
<!-- revisar tras T18/T19 -->
Los ajustes se cambian desde la ventana de configuración. Los que no aparezcan allí se guardan en la base de datos del programa y tienen estos valores mientras no los cambies.

## Voz y velocidad

| Ajuste | Por defecto | Qué hace |
|---|---|---|
| `preferred_engine` | `"edge"` | Voz que se intenta primero: `"edge"` (internet) o `"piper"` (local). |
| `edge_voice` | `"es-MX-DaliaNeural"` | Voz de Edge TTS. |
| `language` | `"es-MX"` | Idioma de las voces. |
| `speed` | `1.0` | Velocidad de lectura. 1.0 es normal, 1.5 es una vez y media más rápido. El tono no cambia. |
| `piper_voice` | `"es_ES-sharvard-medium"` | Voz local de respaldo. |
| `piper_speaker` | `"F"` | Hablante de la voz Piper (esa voz tiene F y M). |

## Cómo se corta el texto

El texto se manda a la voz en trozos pequeños.

| Ajuste | Por defecto | Qué hace |
|---|---|---|
| `edge_max_chars` | `600` | Máximo de letras por trozo para Edge. |
| `edge_max_sentences` | `3` | Máximo de frases por trozo para Edge. |
| `piper_max_chars` | `300` | Máximo de letras por trozo para Piper. |

## Cuando Edge falla

| Ajuste | Por defecto | Qué hace |
|---|---|---|
| `edge_retries` | `3` | Cuántas veces reintenta Edge antes de pasar a Piper. |
| `edge_backoff_s` | `[1, 2, 4]` | Segundos de espera antes de cada reintento. |
| `edge_timeout_s` | `15` | Segundos que espera una respuesta de Edge antes de darla por perdida. |
| `recovery_initial_s` | `60` | Segundos hasta la primera prueba para volver a Edge desde Piper. |
| `recovery_max_s` | `600` | Máximo de espera entre pruebas (si fallan, el intervalo se duplica hasta este tope). |
| `piper_cooldown_s` | `120` | Mínimo de segundos con Piper antes de volver a Edge, para no cambiar de voz a cada rato. |
| `wait_max_s` | `60` | Si no hay ni internet ni Piper, máximo de segundos entre intentos mientras espera la conexión. |

## Audio por adelantado

| Ajuste | Por defecto | Qué hace |
|---|---|---|
| `prefetch_ahead` | `2` | Cuántos trozos prepara por adelantado. |
| `queue_ahead_chunks` | `2` | Trozos de audio listos en espera, además del que suena. |
| `queue_max_seconds` | `90` | Tope de segundos de audio preparado en espera. |

## Lectura e interfaz

| Ajuste | Por defecto | Qué hace |
|---|---|---|
| `skip_seconds` | `10` | Segundos que salta al avanzar o retroceder. |
| `highlight_sentence` | `true` | Resalta en la ventana la frase que suena. |
| `continuous` | `true` | Pasa solo al capítulo siguiente al terminar. |
| `store_text` | `true` | Guarda el texto leído para poder retomar y mostrar el historial. |
| `first_run_done` | `false` | Indica si ya se mostró el aviso de primera vez (voz de respaldo). Se pone en `true` solo. |
<!-- revisar tras T18/T19 -->

## Descargas de páginas web

| Ajuste | Por defecto | Qué hace |
|---|---|---|
| `http_max_bytes` | `5000000` | Tamaño máximo de una página descargada (5 MB). Protege contra páginas enormes. |
| `http_max_redirects` | `5` | Máximo de redirecciones que sigue. |
| `work_prefix_overrides` | `{}` | Avanzado: correcciones manuales para decidir qué páginas de un sitio son de la misma obra. Normalmente vacío. |

## Datos de la app

- Windows: `%APPDATA%\LectorTTS\`
- macOS: `~/Library/Application Support/LectorTTS/`
