# 🐌 OpenTralla v2.2 — Grabadora + Transcriptor de Llamadas

![OpenTralla Interfaz](screenshot.png)

Graba y transcribe reuniones de **Google Meet, Zoom, Teams** (o cualquier audio del sistema) directamente en tu PC, **100% local y sin costo**, con separación de hablantes.

---

## 🚀 Novedades v2.2 — Graba primero, transcribe después

En v2.0 Whisper corría **durante** la llamada. En equipos justos eso saturaba la CPU y el efecto se oía al otro lado: **voz robotizada, cortes y ruido**. La v2.2 le da la vuelta al flujo.

| Antes (v2.0) | Ahora (v2.2) |
|---|---|
| 🔴 Whisper transcribía **durante** la llamada y comía CPU | ✅ **Modo grabadora** por defecto: durante la llamada solo se graba audio. Cero Whisper, cero VAD |
| 🔴 El modelo se cargaba al abrir la app (~30 s de espera) | ✅ **Carga perezosa**: la app abre al instante, Whisper se carga al pulsar «📝 Transcribir» |
| 🔴 El audio se acumulaba en RAM (~1,4 GB por hora) | ✅ **Se escribe a disco** según entra: una llamada de 3 h gasta lo mismo que una de 3 min |
| 🔴 Si la app se cerraba, el audio se perdía | ✅ El audio ya está en disco; al reabrir, la app **te ofrece la grabación pendiente** |
| 🔴 El audio de la llamada podía quedar **desfasado** del micrófono | ✅ Los huecos del loopback se rellenan con silencio: ambos canales quedan sincronizados |
| 🔴 Transcripción frase a frase, sin contexto | ✅ Whisper recibe la **llamada entera**: mejor contexto y mejor puntuación |

> ¿Prefieres verlo en vivo? El interruptor **⚡ Transcribir en vivo** sigue ahí. Está apagado por defecto y avisa del coste en CPU.

---

## 🚀 Uso

### El flujo normal (recomendado)

1. **Inicia la llamada** en Meet/Zoom/Teams
2. **▶ Iniciar** → OpenTralla graba tu micrófono y el audio de la llamada. Nada más: la CPU queda libre
3. **⏹ Detener** al colgar
4. **📝 Transcribir** → ahora sí carga Whisper y transcribe todo, con progreso en pantalla
5. **💾 Guardar** → genera `.txt`, `.json` y `.wav`

Mientras transcribe, ese mismo botón pasa a **⏹ Cancelar**. El texto va apareciendo a medida que avanza.

### Si quieres verlo en tiempo real

Activa **⚡ Transcribir en vivo** antes de empezar. Ten en cuenta que Whisper competirá por la CPU con la videollamada.

### Botones

| Botón | Qué hace |
|---|---|
| ▶ **Iniciar** | Empieza a grabar (mic + audio del sistema) |
| ⏹ **Detener** | Para la grabación |
| 📝 **Transcribir** | Transcribe la grabación terminada. Vuelve a pulsarlo para cancelar |
| 🤖 **Analizar con IA** | Resumen, puntos clave o tareas sobre la transcripción |
| 💾 **Guardar** | Exporta transcripción y audio donde tú elijas |
| 🗑️ **Limpiar** | Borra el texto de la ventana (la grabación sigue en disco) |
| 📂 **Grabaciones** | Abre la carpeta donde está el audio |

---

## ✨ Características

| Función | Detalle |
|---|---|
| 🎙️ **Diarización** | Distingue quién habla: `[Tú]` en azul (micrófono) y `[Ellos]` en verde (llamada) |
| 💾 **Grabación en disco** | El audio se escribe según entra. RAM plana y a prueba de cierres inesperados |
| ⚡ **Modo Callback** | Captura WASAPI no-bloqueante que elimina chasquidos y crujidos |
| 🔊 **Tasa Nativa** | Captura a la tasa real del dispositivo (44100/48000 Hz) — sin resampleo destructivo |
| 🔗 **Streams sincronizados** | Se rellenan con silencio los tramos en que el sistema no reproduce nada |
| 🛡️ **Anti-Pérdida** | Volcado a disco cada 10 s + recuperación de grabaciones pendientes al abrir |
| 🎬 **Grabar Pantalla** | Captura de video opcional de la zona que elijas (redimensionable) |
| 🤖 **Análisis con IA** | Resumir, extraer puntos clave o tareas con Gemini, Claude, OpenAI u Ollama |
| 📊 **VU meter** | Visualiza el nivel de audio en tiempo real |
| 🎚️ **Umbral ajustable** | Slider para calibrar la sensibilidad del VAD sin reiniciar |

---

## 🏗️ Cómo funciona por dentro

```
┌──────────────┐   Callback    ┌───────────┐
│  Micrófono    │──(no bloquea)→│ Cola Mic  │──┐
│  (tu voz)     │               └───────────┘  │   ┌──────────────────────┐
└──────────────┘                               ├──→│ grabaciones/sesion_…  │
┌──────────────┐   Callback    ┌───────────┐   │   │   tu.pcm  ellos.pcm   │
│  Loopback     │──(no bloquea)→│ Cola Loop │──┘   └──────────┬───────────┘
│  (WASAPI)     │               └───────────┘                 │
└──────────────┘                                              │
                                        ┌─────────────────────▼──────────────┐
   ⚡ solo si activas «en vivo»          │  📝 Transcribir (al colgar)         │
   ┌──────────────────────┐             │  · lee por bloques a 16 kHz         │
   │ VAD + Whisper por    │             │  · Whisper sobre la llamada entera  │
   │ frases, en la llamada│             │  · diarización por franjas de tiempo│
   └──────────────────────┘             └─────────────────────────────────────┘
```

**Por qué a disco y en PCM crudo:** un `.pcm` sin cabecera no se puede corromper. Si la app se cierra de golpe, el archivo sigue siendo válido hasta el último byte escrito — no hay ninguna cabecera que reparar. El `.wav` final se genera al guardar, leyendo por bloques.

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

La app abre al instante. El modelo de Whisper se descarga la primera vez que pulses **📝 Transcribir** (solo necesitas internet esa vez).

---

## 📦 Archivos

### Mientras grabas — `grabaciones/sesion_AAAAMMDD_HHMMSS/`

| Archivo | Contenido |
|---|---|
| `ellos.pcm` | 🔊 Audio de la videollamada (loopback del sistema) |
| `tu.pcm` | 🎤 Tu micrófono |
| `sesion.json` | Tasas de muestreo, hora de inicio y si hubo micrófono |

Esta carpeta está en `.gitignore`: el audio de tus llamadas nunca se sube al repositorio.

### Al pulsar 💾 Guardar

| Archivo | Contenido |
|---|---|
| `NOMBRE.txt` | Transcripción formateada con cabecera y separadores por hablante |
| `NOMBRE.json` | Transcripción estructurada con turnos y estadísticas |
| `NOMBRE.wav` | 🔊 Audio de la llamada (ambos canales mezclados) |
| `NOMBRE.mp4` | 🎬 Video sincronizado (solo si activaste grabación de pantalla) |

Después de guardar, la app te ofrece borrar los archivos de trabajo de la sesión para liberar espacio. Si dices que no, se quedan por si quieres volver a transcribirla.

### 📄 Formato de la transcripción (.txt)
```
════════════════════════════════════════════════════════
  OpenTralla v2.2 — Transcripción de Llamada
  Fecha: 20/04/2026 15:30:45
  Duración: 00:45:23
  Motor: faster-whisper | Captura: Dual-channel WASAPI callback
════════════════════════════════════════════════════════

──── TÚ (Micrófono) ────────────────────────────────────
[15:30:52] [Tú] Bueno, entonces hablemos del proyecto...

──── ELLOS (Llamada) ────────────────────────────────────
[15:31:05] [Ellos] Sí, perfecto. Les cuento que...
```

### 📊 Formato JSON (.json)
```json
{
  "version": "2.2",
  "app": "OpenTralla",
  "fecha": "2026-04-20T15:30:45",
  "duracion": "00:45:23",
  "turnos": [
    {"hora": "15:30:52", "hablante": "Tú", "texto": "Bueno, entonces..."},
    {"hora": "15:31:05", "hablante": "Ellos", "texto": "Sí, perfecto..."}
  ],
  "estadisticas": {
    "total_turnos": 42,
    "turnos_tu": 20,
    "turnos_ellos": 22
  }
}
```

---

## 🛡️ Sistema Anti-Pérdida

- **El audio** se vuelca a disco cada 10 segundos mientras grabas. Si se cierra la app, se cuelga Windows o se va la luz, lo grabado hasta ese momento está a salvo.
- **Al reabrir la app**, si encuentra una grabación que no llegaste a transcribir, te la ofrece para cargarla.
- **La transcripción** se auto-guarda cada 30 segundos en `_autosave/` (se conservan las últimas 3 copias).

---

## 🎚️ Calibración del umbral VAD

> Solo afecta al modo **⚡ en vivo**. En modo grabadora no hay VAD: se graba todo.

El **slider "Umbral"** controla cuándo se considera que hay voz:

- **VU meter en cero todo el tiempo** → baja el slider
- **Transcribe ruido de fondo** → sube el slider

---

## 🤖 Análisis con IA (opcional)

Haz clic en **"🤖 Analizar con IA"** después de transcribir. Soporta múltiples proveedores:

| Proveedor | Costo | Cómo obtener clave |
|---|---|---|
| **Gemini** | Gratis (cuota diaria) | [aistudio.google.com](https://aistudio.google.com) → Get API key |
| **Claude** | Pago | [console.anthropic.com](https://console.anthropic.com) |
| **OpenAI** | Pago | [platform.openai.com](https://platform.openai.com) |
| **Ollama** | Gratis (local) | [ollama.com](https://ollama.com) → `ollama pull llama3` |

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

Se eligen desde el desplegable **⚙️ Modelo** de la barra inferior; la elección se guarda en `config.json`.

| Modelo | Velocidad | Precisión | RAM extra | Recomendado para |
|---|---|---|---|---|
| `tiny` | ⚡ Súper veloz | Baja | ~400 MB | PC muy viejas |
| `base` | ⚡ Veloz | Aceptable | ~500 MB | Pruebas rápidas |
| **`small`** ✅ | 🟢 Rápido | Buena | ~1 GB | **Por defecto — buen equilibrio en CPU** |
| `medium` | 🟡 Normal | Muy alta | ~2.5 GB | GPU NVIDIA recomendada |

> Como ahora la transcripción va **después** de la llamada, puedes permitirte un modelo más grande sin miedo a estropear el audio: ya no compite con Zoom por la CPU.

Si tienes una GPU NVIDIA con CUDA, OpenTralla la detecta y la usa sola.

---

## 🔧 Diarización: cómo funciona

Sin herramientas externas ni modelos extra — **detección por ratios adaptativos**:

- **`[Tú]`** → el micrófono tiene actividad significativa relativa al loopback
- **`[Ellos]`** → solo el loopback tiene actividad (o el mic solo capta eco débil)
- **Sin etiqueta** → sin micrófono conectado

> **Nota técnica:** el audio digital del loopback siempre es más fuerte que un micrófono analógico. OpenTralla compensa usando ratios de actividad en vez de comparar volúmenes absolutos.

En modo diferido la diarización se hace por franjas de tiempo sobre la grabación completa, comparando la actividad de cada canal en el tramo de cada frase.

Para mejorarla: asegúrate de que tu micrófono sea el dispositivo de entrada predeterminado en Windows.

---

## 🛠️ Solución de problemas

| Problema | Solución |
|---|---|
| **La otra persona me oye robotizado o con cortes** | Apaga **⚡ Transcribir en vivo**. Si sigue igual, es tu diadema, no la app (ver abajo) |
| Suena mal con diadema Bluetooth | Al usar su micrófono, Windows la pasa a modo *Manos libres* y baja la calidad. Es una limitación del Bluetooth: usa auriculares por cable para llamadas importantes |
| No transcribe nada | ¿Pulsaste **📝 Transcribir** después de detener? En modo grabadora el texto no aparece durante la llamada |
| No grabó el audio de la llamada | Verifica que el sonido salga por el dispositivo predeterminado de Windows |
| Solo captura un lado | Activa el checkbox **🎤 Mi voz (mic)** y revisa el micrófono en Windows → Configuración → Sonido |
| Transcribe ruido (modo en vivo) | Sube el slider de umbral |
| La transcripción tarda mucho | Normal: es proporcional a la llamada. Usa un modelo más pequeño o pulsa ⏹ Cancelar |
| Se perdió la grabación | Reabre la app: te ofrecerá la grabación pendiente. También está en `grabaciones/` |
| Se perdió la transcripción | Revisa la carpeta `_autosave/` |
| Se me llena el disco | Pulsa **📂 Grabaciones** y borra las sesiones viejas. Al guardar, la app también te ofrece limpiarlas |
| Error de API IA | Verifica que la clave sea válida y que instalaste la librería |

---

## 📦 Dependencias principales

- [`pyaudiowpatch`](https://github.com/s0d3s/PyAudioWPatch) — captura WASAPI loopback con callbacks
- [`faster-whisper`](https://github.com/SYSTRAN/faster-whisper) — transcripción ASR local
- `numpy` — procesamiento de audio

---

## 📜 Historial

### v2.0 Pro — Reescritura del motor de audio

Inspirada en técnicas de grabación de llamadas de **Twilio**, **Zoom** y call centers:

| Problema en v1 | Solución en v2.0 |
|---|---|
| 🔴 **Chasquidos/crujidos** al iniciar grabación | ✅ **Modo Callback WASAPI** — el driver entrega datos sin bloqueo |
| 🔴 Buffer de 512 frames causaba **underruns** | ✅ Buffer de **2048 frames** |
| 🔴 Resampleo forzado a 16 kHz **destruía calidad** | ✅ Captura a **tasa nativa** del dispositivo |
| 🔴 **Se perdía tu voz** — la cancelación de eco rompía la señal | ✅ **Detección por ratios adaptativos** |
| 🔴 Pop/clic al presionar Iniciar | ✅ **Fade-in de 15 ms** anti-pop |
| 🔴 Transcripción plana sin formato | ✅ **Formato con separadores** + **JSON estructurado** |

---

## 🤝 Contribuciones (Open Source)

¡Este proyecto es 100% de código abierto (*Open Source*)!
Si tienes ideas para mejorar OpenTralla, has encontrado algún error, o quieres agregar nuevas y geniales funcionalidades, ¡tus contribuciones son más que bienvenidas!

Siéntete libre de hacer un *fork* del repositorio, trabajar en tus mejoras y enviar un *Pull Request*. ¡Hagamos crecer esta herramienta juntos!
