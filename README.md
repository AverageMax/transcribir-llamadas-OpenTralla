# 🐌 OpenTralla — Transcriptor de Llamadas en Tiempo Real

![OpenTralla Interfaz](screenshot.png)

Transcribe reuniones de **Google Meet, Zoom, Teams** (o cualquier audio del sistema) directamente en tu PC, **100% local y sin costo**.

---

## ✨ Funciones

| Función | Detalle |
|---|---|
| 🎙️ **Diarización** | Distingue quién habla: `[Tú]` en azul (micrófono) y `[Ellos]` en verde (llamada) |
| ⚡ **Tiempo real** | Transcripción frase por frase con VAD inteligente |
| 🔊 **Audio mixto** | Captura simultánea de micrófono + audio del sistema (WASAPI loopback) |
| 🎬 **Grabar Pantalla** | Captura de video opcional de la zona que elijas con RegionPicker (redimensionable) |
| 🤖 **Análisis con IA** | Resumir, extraer puntos clave o tareas con Gemini, Claude, OpenAI u Ollama (opcional) |
| 📊 **VU meter** | Visualiza el nivel de audio en tiempo real |
| 🎚️ **Umbral ajustable** | Slider para calibrar la sensibilidad del VAD sin reiniciar |
| 💾 **Guardar** | Exporta la transcripción a `.txt`, audio a `.wav`, y si grabas pantalla, genera un `.mp4` sincronizado |

---

## ⚙️ Instalación

### 1. Requisitos
- Python 3.9+
- Windows 10/11 (WASAPI loopback)

### 2. Instalar dependencias
```
instalar.bat
```

### 3. Iniciar la app
```
iniciar.bat
```

Al primer inicio se descarga automáticamente el modelo `small` de Whisper (~500 MB). Espera a que el estado diga **"Modelo listo"**.

---

## 🚀 Uso

1. **Inicia la llamada** en Meet/Zoom/Teams
2. **Presiona ▶ Iniciar**
3. Las frases aparecen en tiempo real con los hablantes etiquetados
4. **Presiona ⏹ Detener** cuando termines
5. **💾 Guardar** → guarda automáticamente los archivos con el mismo nombre y marca de tiempo (`opentralla_YYYYMMDD_HHMMSS`):
   - `.txt` — transcripción completa de la charla
   - `.wav` — audio grabado de la sesión (16kHz mono, excelente calidad)
   - `.mp4` — (SOLO si activaste Grabar Pantalla) video fluido y perfectamente sincronizado.
6. Opcionalmente, analiza con **🤖 Analizar con IA**

---

## 🎚️ Calibración del umbral VAD

El **slider "Umbral"** en la barra inferior controla cuándo se considera que hay voz:

- **VU meter en cero todo el tiempo** → baja el slider
- **Transcribe ruido de fondo** → sube el slider
- Valor recomendado: el VU meter debe moverse con voz pero quedarse quieto en silencio

---

## 🤖 Análisis con IA (opcional)

Haz clic en **"🤖 Analizar con IA"** después de transcribir. Soporta múltiples proveedores:

### Proveedores disponibles

| Proveedor | Costo | Cómo obtener clave |
|---|---|---|
| **Gemini** | Gratis (cuota diaria) | [aistudio.google.com](https://aistudio.google.com) → Get API key |
| **Claude** | Pago | [console.anthropic.com](https://console.anthropic.com) |
| **OpenAI** | Pago | [platform.openai.com](https://platform.openai.com) |
| **Ollama** | Gratis (local) | Ver abajo |

### Ollama (gratis, local, sin API key)

1. Descarga [ollama.com](https://ollama.com)
2. Instala y corre en terminal:
   ```
   ollama pull llama3
   ```
3. En la app selecciona **Ollama** y pon la URL `http://localhost:11434`
4. En "Modelo" escribe `llama3`

### Instalar biblioteca según proveedor

Solo instala la que vayas a usar:

```bash
# Gemini
pip install google-generativeai

# Claude
pip install anthropic

# OpenAI
pip install openai

# Ollama → no necesita librería extra
```

### Acciones disponibles

| Acción | Qué hace |
|---|---|
| **Resumen ejecutivo** | Resumen corto y conciso de la llamada |
| **Puntos clave** | Lista de los temas y puntos principales tratados |
| **Tareas pendientes** | Extrae compromisos, tareas y próximos pasos |
| **Preguntas y respuestas** | Identifica preguntas formuladas y sus respuestas |
| **Prompt personalizado** | Escribe cualquier instrucción libre |

Las API keys se guardan localmente en `config.json` (excluido de git, nunca se sube).

---

## 🔬 Modelos de Whisper

| Modelo | Velocidad | Precisión | RAM / VRAM extra | Notas |
|---|---|---|---|---|
| `tiny` | Súper veloz | Baja | ~400 MB | Ideal para PC muy viejas |
| `base` | Veloz | Aceptable | ~500 MB | Bueno para dictados simples |
| `small` | Rápido | Buena | ~1 GB | Balance ideal por defecto |
| `medium` ✅ | Normal | Muy alta | ~2.5 GB extra | Recomendado para llamadas exigentes |
| `large-v2` / `v3` | Lento* | Excelente | ~6 GB | Precisión humana superior |

> *`large-v3` en CPU tarda 30-60s por frase. Para usarlo en tiempo real necesitas GPU NVIDIA.

### Cambiar modelo

Edita en la línea 45 de `transcriptor.py`:
```python
MODEL_SIZE = "medium"   # Cambia a "small", "tiny", o "large-v3" según tu PC
```

### Usar GPU NVIDIA (más rápido)

```python
whisper_model = WhisperModel(MODEL_SIZE, device="cuda", compute_type="float16")
```

---

## 🔧 Diarización: cómo funciona

Sin herramientas externas ni modelos extra — compara el volumen de tu micrófono vs el audio del sistema por cada frase:

- **`[Tú]`** → el micrófono sonó más fuerte que el sistema
- **`[Ellos]`** → el sistema (llamada) sonó más fuerte que el micrófono
- **Sin etiqueta** → solo hay audio del sistema (sin micrófono conectado)

Para mejorar la diarización: asegúrate de que tu micrófono esté configurado como dispositivo de entrada predeterminado en Windows.

---

## 🛠️ Solución de problemas

| Problema | Solución |
|---|---|
| No transcribe nada | Baja el slider de umbral. Verifica que el VU meter se mueva |
| Transcribe ruido | Sube el slider de umbral |
| Se comen palabras | Normal en videos muy rápidos; en llamadas reales es mínimo |
| Solo captura un lado | Verifica el micrófono en → Configuración de Windows → Sonido |
| Modelo lento | Usa `small` (defecto) o un modelo aun menor como `tiny` |
| Error de API IA | Verifica que la clave sea válida y que instalaste la librería |
| Se detiene o "cuelga" en llamadas largas (o al usar audífonos Bluetooth) | Windows aveces desconecta el audio por un milisegundo. Agregué un parche de "auto-sanación" al código para que OpenTralla lo detecte, reconecte el audio por debajo en 1 segundo y siga grabando la llamada automáticamente sin que te des cuenta. |

---

## 📦 Dependencias principales

- [`pyaudiowpatch`](https://github.com/s0d3s/PyAudioWPatch) — captura WASAPI loopback
- [`faster-whisper`](https://github.com/SYSTRAN/faster-whisper) — transcripción ASR
   - `numpy` — procesamiento de audio

---

## 🤝 Contribuciones (Open Source)

¡Este proyecto es 100% de código abierto (*Open Source*)! 
Si tienes ideas para mejorar OpenTralla, has encontrado algún error, o quieres agregar nuevas y geniales funcionalidades (como soporte para más modelos de IA, mejoras en la interfaz, o mayor control sobre la grabación de pantalla), ¡tus contribuciones son más que bienvenidas! 

Siéntete libre de hacer un *fork* del repositorio, trabajar en tus mejoras y enviar un *Pull Request*. ¡Hagamos crecer esta herramienta juntos!
