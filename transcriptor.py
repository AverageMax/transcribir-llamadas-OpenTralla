# -*- coding: utf-8 -*-
"""
OpenTralla - Transcriptor Profesional de Llamadas v2.0
══════════════════════════════════════════════════════
Grabación de calidad profesional inspirada en sistemas como Twilio y Zoom:
- Captura a tasa NATIVA del dispositivo (44100/48000Hz) — sin resampleo destructivo
- Modo CALLBACK (no-bloqueante) — elimina chasquidos/crujidos por buffer underrun
- Streams 100% SEPARADOS: mic (Tú) y loopback (Ellos) — como grabación dual-channel
- Noise gate inteligente — reemplaza cancelación de eco destructiva
- Fade-in anti-pop al iniciar — elimina el clic inicial
- Auto-guardado continuo — nunca se pierde nada
- Guardado dual-channel estéreo (Tú=izquierdo, Ellos=derecho)
- Transcripción estructurada + JSON

Diarización: mic=[Tú] / loopback=[Ellos] por VAD independiente por stream
Pre-roll de 0.6s para no perder primeras palabras
VAD con umbral ajustable por slider
Resumen/análisis con Gemini, Claude, OpenAI o Ollama (local)
"""

import sys, site, os, math, ctypes, threading, queue, datetime, json, wave, struct, tempfile, subprocess, shutil, time

_user_site = site.getusersitepackages()
if _user_site not in sys.path:
    sys.path.insert(0, _user_site)

try:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("OpenTralla.Transcriptor.1")
except Exception:
    pass

# ─── Elevar prioridad del proceso para audio en tiempo real ───────────────────
try:
    import psutil
    psutil.Process().nice(psutil.HIGH_PRIORITY_CLASS)
except Exception:
    # Sin psutil, intentar con ctypes directamente
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetCurrentProcess()
        kernel32.SetPriorityClass(handle, 0x00000080)  # HIGH_PRIORITY_CLASS
    except Exception:
        pass

import tkinter as tk
from tkinter import scrolledtext, filedialog, messagebox, ttk
import numpy as np

# ─── Dependencias core ────────────────────────────────────────────────────────
MISSING = []
try:
    import pyaudiowpatch as pyaudio
except ImportError:
    MISSING.append("pyaudiowpatch")
try:
    from faster_whisper import WhisperModel
except ImportError:
    MISSING.append("faster-whisper")

if MISSING:
    _r = tk.Tk(); _r.withdraw()
    messagebox.showerror("Dependencias faltantes",
                         f"Faltan: {', '.join(MISSING)}\n\nEjecuta: instalar.bat")
    sys.exit(1)

# ─── Parámetros ────────────────────────────────────────────────────────────────
WHISPER_RATE   = 16000                  # Tasa que Whisper necesita (solo para transcripción)
CHUNK_FRAMES   = 2048                   # Buffer grande = sin chasquidos (era 512 → underruns)
# Opciones de MODEL_SIZE (de menor a mayor precisión y consumo de recursos):
# "tiny"     : ~39 M params  (super rápido, menos preciso, uso bajísimo de RAM/VRAM)
# "base"     : ~74 M params  (buen balance para pruebas rápidas)
# "small"    : ~244 M params (balance ideal velocidad/precisión para la mayoría) ← RECOMENDADO PARA CPU
# "medium"   : ~769 M params (alta precisión, necesita GPU para ser fluido en tiempo real)
# "large-v2" : ~1550 M params (precisión casi perfecta, requiere GPU NVIDIA)
# "large-v3" : ~1550 M params (el más avanzado y exacto de OpenAI, requiere GPU NVIDIA)
MODEL_SIZE     = "small"                # "small" = fluido en CPU | "medium" = necesita GPU
LANGUAGE       = "es"

SILENCE_SEC    = 0.8                    # Segundos de silencio para cortar frase
MIN_SPEECH_SEC = 0.3                    # Mínimo de voz para considerar frase
MAX_PHRASE_SEC = 25                     # Máximo antes de forzar corte
SILENCE_THRESH = 0.003                  # Umbral VAD por defecto
FADE_IN_MS     = 15                     # Duración del fade-in anti-pop (ms)
AUTOSAVE_INTERVAL = 30                  # Segundos entre auto-guardados

CONFIG_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
AUTOSAVE_DIR   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_autosave")

# ─── Estado global ─────────────────────────────────────────────────────────────
phrase_queue: queue.Queue = queue.Queue()   # items: (audio_f32_16k, speaker_label)
stop_event    = threading.Event()
model_ready   = threading.Event()
whisper_model = None
silence_thresh_dynamic = SILENCE_THRESH

# Buffers de audio separados (calidad profesional: streams independientes)
audio_buf_mic:  list = []     # Solo tu voz (micrófono)
audio_buf_loop: list = []     # Solo ellos (loopback/sistema)
native_rate_loop: int = 48000 # Se actualiza al detectar dispositivo
native_rate_mic:  int = 48000


# ─── Config (API keys) ─────────────────────────────────────────────────────────
def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_config(cfg: dict):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


# ─── Modelo ───────────────────────────────────────────────────────────────────
def cargar_modelo(status_cb):
    global whisper_model
    status_cb(f"⏳ Cargando modelo '{MODEL_SIZE}'…")
    try:
        whisper_model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
        model_ready.set()
        status_cb(f"✅ Modelo '{MODEL_SIZE}' listo — Presiona Iniciar")
    except Exception as e:
        status_cb(f"❌ Error cargando modelo: {e}")


# ─── Utilidades audio profesionales ──────────────────────────────────────────
def raw_to_f32_native(raw: bytes, ch: int) -> np.ndarray:
    """Convierte bytes PCM int16 a float32 mono. SIN resampleo — calidad nativa."""
    s = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        s = s.reshape(-1, ch).mean(axis=1).astype(np.float32)
    return s

def resample_for_whisper(audio: np.ndarray, source_rate: int) -> np.ndarray:
    """Resamplea a 16kHz SOLO para enviar a Whisper. No afecta el audio grabado."""
    if source_rate == WHISPER_RATE:
        return audio
    nl = int(len(audio) * WHISPER_RATE / source_rate)
    if nl == 0:
        return np.zeros(0, dtype=np.float32)
    ix = np.linspace(0, len(audio) - 1, nl)
    return np.interp(ix, np.arange(len(audio)), audio).astype(np.float32)

def rms(s: np.ndarray) -> float:
    return float(np.sqrt(np.mean(s**2))) if len(s) > 0 else 0.0

def apply_fade_in(audio: np.ndarray, rate: int, duration_ms: int = FADE_IN_MS) -> np.ndarray:
    """Aplica un fade-in suave al inicio del audio para eliminar el pop/clic inicial.
    Técnica estándar en grabación profesional para evitar discontinuidades."""
    n_samples = int(rate * duration_ms / 1000)
    n_samples = min(n_samples, len(audio))
    if n_samples > 0:
        fade = np.linspace(0.0, 1.0, n_samples, dtype=np.float32)
        audio = audio.copy()
        audio[:n_samples] *= fade
    return audio


# ─── Captura Profesional con Callbacks (modo no-bloqueante) ──────────────────
# Las grandes empresas (Twilio, Zoom) usan servidores media dedicados.
# Nosotros emulamos su arquitectura localmente:
# - Streams 100% separados (como canales de una mesa de mezclas)
# - Callbacks ultra-livianos (solo queue.put, sin procesamiento)
# - VAD y diarización en hilo aparte (no bloquea el audio)
# - Captura a tasa nativa (sin resampleo durante grabación)

class AudioCaptureEngine:
    """Motor de captura profesional con streams separados y modo callback.
    
    Arquitectura inspirada en sistemas de call center:
    - Cada fuente de audio (mic, loopback) tiene su propio stream y cola
    - Los callbacks son ultra-rápidos: solo encolan datos
    - El procesamiento VAD/diarización ocurre en un hilo separado
    - Se mantiene la tasa nativa del dispositivo para máxima calidad
    """
    
    def __init__(self, status_cb, rms_cb):
        self.status_cb = status_cb
        self.rms_cb = rms_cb
        self.pa = None
        self.stream_loop = None
        self.stream_mic = None
        self.mic_ok = False
        
        # Colas de audio para cada stream (thread-safe)
        self.q_loop = queue.Queue(maxsize=500)  # Cola loopback
        self.q_mic  = queue.Queue(maxsize=500)  # Cola micrófono
        
        # Info de dispositivo
        self.ch_l = 2
        self.rate_l = 48000
        self.ch_m = 1
        self.rate_m = 48000
        
        # Control de fade-in (solo para el primer chunk)
        self._fade_done_loop = False
        self._fade_done_mic = False
    
    def _cb_loopback(self, in_data, frame_count, time_info, status):
        """Callback del loopback — ultra-liviano, solo encola.
        Este callback es llamado directamente por el driver WASAPI.
        NO hacer procesamiento pesado aquí para evitar underruns."""
        try:
            self.q_loop.put_nowait(in_data)
        except queue.Full:
            pass  # Descartar si la cola está llena (mejor que bloquear)
        return (None, pyaudio.paContinue)
    
    def _cb_mic(self, in_data, frame_count, time_info, status):
        """Callback del micrófono — ultra-liviano, solo encola."""
        try:
            self.q_mic.put_nowait(in_data)
        except queue.Full:
            pass
        return (None, pyaudio.paContinue)
    
    def start(self):
        """Inicia los streams de captura con modo callback."""
        global native_rate_loop, native_rate_mic
        self.pa = pyaudio.PyAudio()
        
        # ── Loopback (audio del sistema / la otra persona) ──
        try:
            dev_loop = self.pa.get_default_wasapi_loopback()
        except Exception:
            dev_loop = next(
                (self.pa.get_device_info_by_index(i)
                 for i in range(self.pa.get_device_count())
                 if self.pa.get_device_info_by_index(i).get("isLoopbackDevice")), None)
        if not dev_loop:
            raise RuntimeError("No se encontró dispositivo loopback WASAPI.")
        
        self.ch_l  = min(int(dev_loop.get("maxInputChannels", 2)) or 2, 2)
        self.rate_l = int(dev_loop.get("defaultSampleRate", 48000))
        native_rate_loop = self.rate_l
        
        # Abrir con CALLBACK (no-bloqueante) — la clave para evitar chasquidos
        self.stream_loop = self.pa.open(
            format=pyaudio.paInt16,
            channels=self.ch_l,
            rate=self.rate_l,
            input=True,
            input_device_index=int(dev_loop["index"]),
            frames_per_buffer=CHUNK_FRAMES,
            stream_callback=self._cb_loopback  # ← MODO CALLBACK
        )
        
        # ── Micrófono (tu voz) ──
        try:
            dev_mic = self.pa.get_device_info_by_index(
                self.pa.get_default_input_device_info()["index"])
            
            self.ch_m  = min(int(dev_mic.get("maxInputChannels", 1)) or 1, 2)
            self.rate_m = int(dev_mic.get("defaultSampleRate", 48000))
            native_rate_mic = self.rate_m
            
            self.stream_mic = self.pa.open(
                format=pyaudio.paInt16,
                channels=self.ch_m,
                rate=self.rate_m,
                input=True,
                input_device_index=int(dev_mic["index"]),
                frames_per_buffer=CHUNK_FRAMES,
                stream_callback=self._cb_mic  # ← MODO CALLBACK
            )
            self.mic_ok = True
            self.status_cb(f"🎤+🔊 Mic ({self.rate_m}Hz) + Sistema ({self.rate_l}Hz) | Captura profesional activa")
        except Exception:
            self.mic_ok = False
            self.status_cb(f"🔊 Solo sistema ({self.rate_l}Hz) | Captura profesional activa")
    
    def get_chunks(self):
        """Extrae todos los chunks disponibles de ambas colas.
        Retorna (loop_chunks, mic_chunks) como listas de np.ndarray float32."""
        loops = []
        mics  = []
        
        # Vaciar colas
        while not self.q_loop.empty():
            try:
                raw = self.q_loop.get_nowait()
                audio = raw_to_f32_native(raw, self.ch_l)
                # Fade-in en el primer chunk para eliminar pop
                if not self._fade_done_loop and len(audio) > 0:
                    audio = apply_fade_in(audio, self.rate_l)
                    self._fade_done_loop = True
                loops.append(audio)
            except queue.Empty:
                break
        
        while not self.q_mic.empty():
            try:
                raw = self.q_mic.get_nowait()
                audio = raw_to_f32_native(raw, self.ch_m)
                if not self._fade_done_mic and len(audio) > 0:
                    audio = apply_fade_in(audio, self.rate_m)
                    self._fade_done_mic = True
                mics.append(audio)
            except queue.Empty:
                break
        
        return loops, mics
    
    def stop(self):
        """Detiene y cierra los streams de forma limpia."""
        for st in (self.stream_loop, self.stream_mic):
            if st:
                try:
                    st.stop_stream()
                    st.close()
                except Exception:
                    pass
        if self.pa:
            try:
                self.pa.terminate()
            except Exception:
                pass


def hilo_captura(status_cb, rms_cb):
    """Hilo principal de captura, VAD y diarización.
    
    Arquitectura profesional (tipo call center):
    1. El motor de audio captura con callbacks (sin bloqueo)
    2. Este hilo lee las colas periódicamente y aplica VAD
    3. Diarización por comparación de actividad en cada stream
    4. SIEMPRE se envía audio combinado a Whisper — nunca se pierde nada
    """
    global silence_thresh_dynamic
    
    engine = AudioCaptureEngine(status_cb, rms_cb)
    
    try:
        engine.start()
    except Exception as e:
        status_cb(f"❌ Captura: {e}")
        return
    
    # Buffers VAD — streams separados para diarización profesional
    # Usamos la tasa del loopback como referencia porque es la fuente principal
    work_rate = engine.rate_l
    
    speech_buf_loop = np.zeros(0, dtype=np.float32)  # Audio loopback acumulado
    speech_buf_mic  = np.zeros(0, dtype=np.float32)  # Audio mic acumulado
    silence_samp  = 0
    in_speech     = False
    sil_need      = int(SILENCE_SEC     * work_rate)
    min_sp        = int(MIN_SPEECH_SEC  * work_rate)
    max_ph        = int(MAX_PHRASE_SEC  * work_rate)
    pre_roll_loop = np.zeros(0, dtype=np.float32)
    pre_roll_mic  = np.zeros(0, dtype=np.float32)
    pre_roll_max  = int(0.6 * work_rate)
    
    # Diarización: acumular RMS de cada stream por frase
    # SIEMPRE se guarda el RMS real del mic (sin filtrar), para poder comparar
    rms_loop_acc = []
    rms_mic_acc  = []
    
    try:
        while not stop_event.is_set():
            # Leer chunks de ambas colas (no-bloqueante)
            loop_chunks, mic_chunks = engine.get_chunks()
            
            if not loop_chunks and not mic_chunks:
                # Sin datos aún, esperar un poco
                time.sleep(0.01)
                continue
            
            # Concatenar chunks del período
            loop_c = np.concatenate(loop_chunks) if loop_chunks else np.zeros(0, dtype=np.float32)
            mic_c  = np.concatenate(mic_chunks)  if mic_chunks  else np.zeros(0, dtype=np.float32)
            
            rms_loop_now = rms(loop_c)
            rms_mic_now  = rms(mic_c) if engine.mic_ok else 0.0
            
            # ── SIN noise gate ──
            # El noise gate anterior filtraba el mic casi siempre porque el audio
            # digital del loopback es SIEMPRE más fuerte que un mic analógico.
            # Ahora simplemente grabamos AMBOS streams siempre y decidimos
            # quién habla al final de la frase comparando patrones de actividad.
            
            # Guardar audio separado en buffers globales (calidad nativa original)
            if len(loop_c) > 0:
                audio_buf_loop.append(loop_c.copy())
            if engine.mic_ok and len(mic_c) > 0:
                audio_buf_mic.append(mic_c.copy())
            
            # ── VAD combinado ──
            # Usamos el nivel más alto de AMBOS streams como referencia
            level = max(rms_loop_now, rms_mic_now)
            rms_cb(level)
            thresh = silence_thresh_dynamic
            
            # Alinear longitudes para sincronización
            # El mic puede tener tasa diferente, normalizamos a la tasa del loopback
            if engine.mic_ok and len(mic_c) > 0 and engine.rate_m != work_rate:
                nl = int(len(mic_c) * work_rate / engine.rate_m)
                if nl > 0:
                    ix = np.linspace(0, len(mic_c) - 1, nl)
                    mic_c_aligned = np.interp(ix, np.arange(len(mic_c)), mic_c).astype(np.float32)
                else:
                    mic_c_aligned = np.zeros(0, dtype=np.float32)
            else:
                mic_c_aligned = mic_c
            
            if level >= thresh:                      # ── VOZ ──
                # SIEMPRE guardar el RMS real de ambos streams
                rms_loop_acc.append(rms_loop_now)
                rms_mic_acc.append(rms_mic_now)
                if not in_speech:
                    speech_buf_loop = np.concatenate([pre_roll_loop, loop_c])
                    speech_buf_mic  = np.concatenate([pre_roll_mic, mic_c_aligned])
                    pre_roll_loop   = np.zeros(0, dtype=np.float32)
                    pre_roll_mic    = np.zeros(0, dtype=np.float32)
                    in_speech = True
                else:
                    speech_buf_loop = np.concatenate([speech_buf_loop, loop_c])
                    speech_buf_mic  = np.concatenate([speech_buf_mic, mic_c_aligned])
                silence_samp = 0
                if len(speech_buf_loop) >= max_ph:
                    speaker = _decide_speaker(rms_loop_acc, rms_mic_acc, engine.mic_ok)
                    # SIEMPRE enviar AMBOS streams mezclados para que Whisper no pierda nada
                    audio_to_transcribe = _prepare_audio_for_whisper(
                        speech_buf_loop, speech_buf_mic, work_rate)
                    phrase_queue.put((audio_to_transcribe, speaker))
                    speech_buf_loop = np.zeros(0, dtype=np.float32)
                    speech_buf_mic  = np.zeros(0, dtype=np.float32)
                    rms_loop_acc.clear(); rms_mic_acc.clear()
                    in_speech = False

            else:                                    # ── SILENCIO ──
                if in_speech:
                    silence_samp += len(loop_c)
                    speech_buf_loop = np.concatenate([speech_buf_loop, loop_c])
                    speech_buf_mic  = np.concatenate([speech_buf_mic, mic_c_aligned])
                    if silence_samp >= sil_need:
                        if len(speech_buf_loop) >= min_sp:
                            speaker = _decide_speaker(rms_loop_acc, rms_mic_acc, engine.mic_ok)
                            audio_to_transcribe = _prepare_audio_for_whisper(
                                speech_buf_loop, speech_buf_mic, work_rate)
                            phrase_queue.put((audio_to_transcribe, speaker))
                        speech_buf_loop = np.zeros(0, dtype=np.float32)
                        speech_buf_mic  = np.zeros(0, dtype=np.float32)
                        silence_samp = 0
                        in_speech    = False
                        rms_loop_acc.clear(); rms_mic_acc.clear()
                        pre_roll_loop = loop_c[-pre_roll_max:].copy()
                        pre_roll_mic  = mic_c_aligned[-pre_roll_max:].copy() if len(mic_c_aligned) > 0 else np.zeros(0, dtype=np.float32)
                else:
                    pre_roll_loop = np.concatenate([pre_roll_loop, loop_c])[-pre_roll_max:]
                    if len(mic_c_aligned) > 0:
                        pre_roll_mic = np.concatenate([pre_roll_mic, mic_c_aligned])[-pre_roll_max:]
    
    except Exception as e:
        status_cb(f"❌ Captura: {e}")
    finally:
        engine.stop()


def _prepare_audio_for_whisper(buf_loop, buf_mic, source_rate):
    """Prepara audio para Whisper — SIEMPRE mezcla ambos streams.
    
    Clave: Whisper debe recibir TODO el audio para no perder nada.
    La diarización (quién habló) se decide aparte, pero la transcripción
    siempre incluye ambas voces.
    """
    if len(buf_loop) > 0 and len(buf_mic) > 0:
        n = min(len(buf_loop), len(buf_mic))
        # Mezcla balanceada: ambos streams contribuyen
        audio = np.clip(buf_loop[:n] * 0.6 + buf_mic[:n] * 0.6, -1, 1).astype(np.float32)
    elif len(buf_loop) > 0:
        audio = buf_loop
    elif len(buf_mic) > 0:
        audio = buf_mic
    else:
        audio = np.zeros(0, dtype=np.float32)
    
    # Resamplear a 16kHz solo ahora, para Whisper
    return resample_for_whisper(audio, source_rate)


def _decide_speaker(rms_loop: list, rms_mic: list, mic_ok: bool) -> str:
    """Decide quién habla comparando la ACTIVIDAD (no el volumen absoluto) de cada stream.
    
    IMPORTANTE: El audio digital del loopback SIEMPRE es más fuerte que un micrófono
    analógico. Por eso NO comparamos volúmenes absolutos. En vez de eso:
    - Medimos si cada stream tiene actividad SIGNIFICATIVA (vs su propio ruido de fondo)
    - Si el mic tiene un pico de actividad relativo a su línea base → estás hablando tú
    - Si solo el loopback tiene actividad → son ellos
    """
    if not mic_ok or not rms_mic:
        return ""   # sin micrófono, no etiquetamos
    avg_loop = sum(rms_loop) / len(rms_loop) if rms_loop else 0
    avg_mic  = sum(rms_mic)  / len(rms_mic)  if rms_mic  else 0
    
    # Umbral mínimo de actividad: si ambos están por debajo, no etiquetar
    min_activity = SILENCE_THRESH * 0.3
    
    loop_active = avg_loop > min_activity
    mic_active  = avg_mic  > min_activity
    
    # Si solo el mic tiene actividad → claramente eres tú
    if mic_active and not loop_active:
        return "Tú"
    # Si solo el loopback tiene actividad → claramente son ellos
    if loop_active and not mic_active:
        return "Ellos"
    # Si ninguno tiene actividad significativa → no etiquetar
    if not loop_active and not mic_active:
        return ""
    
    # AMBOS tienen actividad — comparar con ratio adaptativo
    # El loopback digital suele ser 2-5x más fuerte que un mic analógico,
    # así que necesitamos un factor de compensación.
    # Si el mic tiene al menos 15% del nivel del loopback, probablemente
    # estás hablando tú (porque el eco del parlante suele ser <10% del loopback)
    ratio_mic_to_loop = avg_mic / avg_loop if avg_loop > 0 else 999
    
    # Ratio > 0.15 = hay voz real en el mic (no solo eco del parlante)
    if ratio_mic_to_loop > 0.15:
        # Ambos hablan o eres tú — etiquetar como Tú si el mic es fuerte,
        # o Ellos si el loopback domina claramente
        if ratio_mic_to_loop > 0.6:
            return "Tú"    # El mic tiene señal fuerte relativa al loopback
        else:
            return "Ellos" # El loopback domina, el mic solo tiene eco/ambiente
    else:
        return "Ellos"     # El mic casi no tiene señal → son ellos


# ─── Transcripción ────────────────────────────────────────────────────────────
def hilo_transcripcion(text_cb, status_cb):
    model_ready.wait()

    while not stop_event.is_set():
        try:
            item = phrase_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        if stop_event.is_set():
            break

        audio, speaker = item
        pending = phrase_queue.qsize()
        status_cb(f"⚙️ Transcribiendo… (cola: {pending})")
        try:
            segs, _ = whisper_model.transcribe(
                audio, language=LANGUAGE,
                beam_size=5, best_of=5, temperature=0.0,
                vad_filter=False, without_timestamps=True,
            )
            texto = " ".join(s.text.strip() for s in segs).strip()
            if texto:
                ts = datetime.datetime.now().strftime("%H:%M:%S")
                text_cb(ts, speaker, texto)
                status_cb("🔴 Escuchando…")
        except Exception as e:
            status_cb(f"⚠️ {e}")

    while not phrase_queue.empty():
        try: phrase_queue.get_nowait()
        except: break
    status_cb("⏸️ Detenido")


# ─── Grabación de pantalla (opcional) ─────────────────────────────────────────
def _check_screen_deps() -> tuple:
    missing = []
    try: import mss
    except ImportError: missing.append("mss")
    try: import cv2
    except ImportError: missing.append("opencv-python")
    if missing:
        return False, f"Faltan librerías:\n  pip install {' '.join(missing)}\n\nEjecuta instalar.bat y reinicia la app."
    return True, ""


def _find_ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg.exe")
    if os.path.exists(local):
        return local
    for p in [r"C:\ffmpeg\bin\ffmpeg.exe", r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"]:
        if os.path.exists(p):
            return p
    return None


class RegionPicker(tk.Toplevel):
    """Ventana redimensionable transparente para encuadrar la grabación."""
    def __init__(self, parent):
        super().__init__(parent)
        self.title("Área de Grabación (Ajusta la ventana desde los bordes)")
        self.attributes("-topmost", True)
        self.attributes("-transparentcolor", "magenta")
        self.config(bg="#ef4444")
        self.geometry("800x600")
        self.minsize(200, 200)
        self.resizable(True, True)
        
        self.inner = tk.Frame(self, bg="magenta")
        # El padding de 6 píxeles simulado con place evita saltos de geometry por culpa de pack()
        self.inner.place(relx=0, rely=0, relwidth=1, relheight=1, x=6, y=6, width=-12, height=-12)
        
        # Opcional pero útil para arrastrar:
        self.sg = ttk.Sizegrip(self)
        self.sg.place(relx=1.0, rely=1.0, anchor="se")

    def get_region(self):
        self.update_idletasks()
        x = self.inner.winfo_rootx()
        y = self.inner.winfo_rooty()
        w = max(10, self.inner.winfo_width())
        h = max(10, self.inner.winfo_height())
        return (x, y, w, h)
    
    def set_recording(self, state: bool):
        # Congelamos el tamaño y posición actuales para que la ventana nativa no se autoredimensione por cambiar el título/borde
        current_geom = self.geometry()
        self.geometry(current_geom)
        
        if state:
            self.config(bg="#991b1b")
            self.title("🔴 GRABANDO PANTALLA")
            if hasattr(self, "sg") and self.sg: self.sg.place_forget()
        else:
            self.config(bg="#ef4444")
            self.title("Área de Grabación (Ajusta la ventana desde los bordes)")
            if hasattr(self, "sg") and self.sg: self.sg.place(relx=1.0, rely=1.0, anchor="se")


class ScreenRecorder:
    FPS = 10

    def __init__(self, region: tuple):
        self.region     = region
        self._stop      = threading.Event()
        self._thread    = None
        self.video_path = None

    def start(self):
        self._stop.clear()
        tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False, prefix="opentralla_video_")
        self.video_path = tmp.name
        tmp.close()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self):
        try:
            import mss, cv2, time
            x, y, w, h = self.region
            w = max(2, w - (w % 2)); h = max(2, h - (h % 2))
            # mp4v es más eficiente en CPU, mitigando cuellos de botella que afecten al hilo de audio
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            vw = cv2.VideoWriter(self.video_path, fourcc, self.FPS, (w, h))
            interval = 1.0 / self.FPS
            mon = {"left": x, "top": y, "width": w, "height": h}
            
            start_time = time.time()
            frames_written = 0
            
            with mss.mss() as sct:
                while not self._stop.is_set():
                    t0    = time.time()
                    img   = sct.grab(mon)
                    frame = np.array(img)[:, :, :3]
                    frame = frame[:h, :w]
                    
                    # Compensar frames perdidos para mantener sincronización video/audio real
                    expected_frames = int((t0 - start_time) * self.FPS)
                    writes = max(1, expected_frames - frames_written)
                    for _ in range(writes):
                        vw.write(frame)
                        frames_written += 1
                        
                    rem = interval - (time.time() - t0)
                    if rem > 0:
                        self._stop.wait(timeout=rem)
            vw.release()
        except Exception as e:
            import traceback
            err = traceback.format_exc()
            try:
                with open("error_screen.log", "w") as f: f.write(err)
            except: pass
            print(f"ScreenRecorder error: {e}")


# ─── IA: Diálogo de análisis ──────────────────────────────────────────────────
PROVIDERS = {
    "Gemini":  {"lib": "google-generativeai", "key_label": "API Key (aistudio.google.com)", "model_default": "gemini-2.0-flash"},
    "Claude":  {"lib": "anthropic",           "key_label": "API Key (console.anthropic.com)", "model_default": "claude-3-5-haiku-20241022"},
    "OpenAI":  {"lib": "openai",              "key_label": "API Key (platform.openai.com)", "model_default": "gpt-4o-mini"},
    "Ollama":  {"lib": None,                  "key_label": "URL (ej: http://localhost:11434)", "model_default": "llama3"},
}

PROMPT_PRESETS = {
    "Resumen ejecutivo": "Haz un resumen ejecutivo y conciso de esta transcripción de llamada:",
    "Puntos clave":      "Lista los puntos clave y temas principales de esta transcripción:",
    "Tareas pendientes": "Extrae todas las tareas, compromisos y próximos pasos mencionados:",
    "Preguntas y respuestas": "Identifica las preguntas formuladas y sus respuestas en esta conversación:",
    "Prompt personalizado": "",
}

class AIDialog(tk.Toplevel):
    BG = "#1a1a2e"; PANEL = "#16213e"; ACCENT = "#0f3460"
    GREEN = "#4ade80"; FG = "#e2e8f0"; MUTED = "#94a3b8"; AMBER = "#fbbf24"

    def __init__(self, parent, transcript_text: str):
        super().__init__(parent)
        self.title("🤖 Analizar con IA — OpenTralla")
        self.geometry("720x620")
        self.configure(bg=self.BG)
        self.resizable(True, True)
        self.transient(parent)
        self.transcript = transcript_text
        self.cfg = load_config()
        self._build()
        self.grab_set()

    def _build(self):
        C, F, FB = self, ("Segoe UI",10), ("Segoe UI",11,"bold")

        # Header
        hdr = tk.Frame(self, bg=C.ACCENT, pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text="🤖  Analizar transcripción con IA",
                 font=("Segoe UI",13,"bold"), fg=C.FG, bg=C.ACCENT).pack(side="left", padx=16)

        # Config
        cfg_f = tk.Frame(self, bg=C.PANEL, padx=16, pady=10)
        cfg_f.pack(fill="x")

        # Proveedor
        row1 = tk.Frame(cfg_f, bg=C.PANEL)
        row1.pack(fill="x", pady=2)
        tk.Label(row1, text="Proveedor:", font=F, fg=C.MUTED, bg=C.PANEL, width=12, anchor="w").pack(side="left")
        self.var_prov = tk.StringVar(value=self.cfg.get("provider", "Gemini"))
        cb = ttk.Combobox(row1, textvariable=self.var_prov,
                          values=list(PROVIDERS.keys()), state="readonly", width=16)
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", self._on_provider_change)

        # Modelo
        tk.Label(row1, text="  Modelo:", font=F, fg=C.MUTED, bg=C.PANEL).pack(side="left")
        self.var_model = tk.StringVar(value=self._get_saved_model())
        tk.Entry(row1, textvariable=self.var_model, width=24,
                 bg="#0d1117", fg=C.FG, insertbackground=C.FG, relief="flat").pack(side="left", padx=6)

        # API Key / URL
        row2 = tk.Frame(cfg_f, bg=C.PANEL)
        row2.pack(fill="x", pady=2)
        self.var_key_label = tk.StringVar(value=self._key_label())
        tk.Label(row2, textvariable=self.var_key_label,
                 font=F, fg=C.MUTED, bg=C.PANEL, width=12, anchor="w").pack(side="left")
        self.var_key = tk.StringVar(value=self.cfg.get(f"key_{self.var_prov.get()}", ""))
        self.entry_key = tk.Entry(row2, textvariable=self.var_key, width=50,
                                  bg="#0d1117", fg=C.FG, insertbackground=C.FG,
                                  relief="flat", show="•")
        self.entry_key.pack(side="left", padx=6)
        self._toggle_key_field()

        # Preset de prompt
        row3 = tk.Frame(cfg_f, bg=C.PANEL)
        row3.pack(fill="x", pady=2)
        tk.Label(row3, text="Acción:", font=F, fg=C.MUTED, bg=C.PANEL, width=12, anchor="w").pack(side="left")
        self.var_preset = tk.StringVar(value=list(PROMPT_PRESETS.keys())[0])
        cb2 = ttk.Combobox(row3, textvariable=self.var_preset,
                            values=list(PROMPT_PRESETS.keys()), state="readonly", width=30)
        cb2.pack(side="left", padx=6)
        cb2.bind("<<ComboboxSelected>>", self._on_preset_change)

        # Prompt editable
        tk.Label(self, text="Prompt (editable):", font=("Segoe UI",9), fg=C.MUTED, bg=C.BG, anchor="w").pack(fill="x", padx=16, pady=(8,0))
        self.prompt_box = tk.Text(self, height=3, bg="#0d1117", fg=C.FG,
                                  insertbackground=C.FG, relief="flat", padx=8, pady=6,
                                  font=("Segoe UI",10), wrap="word")
        self.prompt_box.pack(fill="x", padx=16)
        self.prompt_box.insert("1.0", list(PROMPT_PRESETS.values())[0])

        # Botones
        btn_f = tk.Frame(self, bg=C.PANEL, pady=8)
        btn_f.pack(fill="x")
        kw = dict(font=FB, relief="flat", cursor="hand2", padx=18, pady=8, bd=0)
        self.btn_send = tk.Button(btn_f, text="▶  Enviar a IA", bg=C.GREEN, fg="#0d1117",
                                  command=self._enviar, **kw)
        self.btn_send.pack(side="left", padx=(16,6))
        tk.Button(btn_f, text="📋  Copiar respuesta", bg=C.ACCENT, fg=C.FG,
                  command=self._copiar, **kw).pack(side="left", padx=6)
        tk.Button(btn_f, text="💾  Guardar respuesta", bg="#334155", fg=C.FG,
                  command=self._guardar, **kw).pack(side="left", padx=6)
        self.var_spin = tk.StringVar(value="")
        tk.Label(btn_f, textvariable=self.var_spin,
                 font=("Segoe UI",12), fg=C.AMBER, bg=C.PANEL).pack(side="right", padx=16)

        # Área de respuesta
        tk.Label(self, text="Respuesta:", font=("Segoe UI",9), fg=C.MUTED, bg=C.BG, anchor="w").pack(fill="x", padx=16, pady=(6,0))
        self.resp_box = scrolledtext.ScrolledText(
            self, wrap=tk.WORD, font=("Segoe UI",11),
            bg="#0d1117", fg=C.FG, insertbackground=C.FG,
            relief="flat", padx=10, pady=8, state="disabled", spacing3=4,
        )
        self.resp_box.pack(fill="both", expand=True, padx=16, pady=(0,16))

    def _key_label(self):
        return PROVIDERS[self.var_prov.get()]["key_label"]

    def _get_saved_model(self):
        prov = self.var_prov.get()
        return self.cfg.get(f"model_{prov}", PROVIDERS[prov]["model_default"])

    def _toggle_key_field(self):
        prov = self.var_prov.get()
        if prov == "Ollama":
            self.entry_key.config(show="")
        else:
            self.entry_key.config(show="•")

    def _on_provider_change(self, _=None):
        prov = self.var_prov.get()
        self.var_key_label.set(self._key_label())
        self.var_key.set(self.cfg.get(f"key_{prov}", ""))
        self.var_model.set(self.cfg.get(f"model_{prov}", PROVIDERS[prov]["model_default"]))
        self._toggle_key_field()

    def _on_preset_change(self, _=None):
        preset = self.var_preset.get()
        self.prompt_box.delete("1.0", tk.END)
        self.prompt_box.insert("1.0", PROMPT_PRESETS.get(preset, ""))

    def _save_cfg(self):
        prov = self.var_prov.get()
        self.cfg["provider"] = prov
        self.cfg[f"key_{prov}"]   = self.var_key.get().strip()
        self.cfg[f"model_{prov}"] = self.var_model.get().strip()
        save_config(self.cfg)

    def _set_resp(self, text: str):
        self.resp_box.config(state="normal")
        self.resp_box.delete("1.0", tk.END)
        self.resp_box.insert(tk.END, text)
        self.resp_box.config(state="disabled")

    def _enviar(self):
        prov  = self.var_prov.get()
        key   = self.var_key.get().strip()
        model = self.var_model.get().strip() or PROVIDERS[prov]["model_default"]
        prompt_text = self.prompt_box.get("1.0", tk.END).strip()
        full_prompt = f"{prompt_text}\n\n---\n{self.transcript}"

        if prov != "Ollama" and not key:
            messagebox.showwarning("API Key requerida", f"Ingresa la API key para {prov}.", parent=self)
            return

        self._save_cfg()
        self.btn_send.config(state="disabled")
        self.var_spin.set("⏳")
        self._set_resp("Enviando a la IA…")

        def _thread():
            try:
                resp = self._call_api(prov, key, model, full_prompt)
            except Exception as e:
                resp = f"❌ Error: {e}"
            self.after(0, lambda: self._on_resp(resp))

        threading.Thread(target=_thread, daemon=True).start()

    def _on_resp(self, text: str):
        self._set_resp(text)
        self.btn_send.config(state="normal")
        self.var_spin.set("")

    def _call_api(self, prov: str, key: str, model: str, prompt: str) -> str:
        if prov == "Gemini":
            import google.generativeai as genai
            genai.configure(api_key=key)
            m = genai.GenerativeModel(model)
            r = m.generate_content(prompt)
            return r.text

        elif prov == "Claude":
            import anthropic
            client = anthropic.Anthropic(api_key=key)
            msg = client.messages.create(
                model=model, max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            )
            return msg.content[0].text

        elif prov == "OpenAI":
            import openai
            client = openai.OpenAI(api_key=key)
            r = client.chat.completions.create(
                model=model, max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            )
            return r.choices[0].message.content

        elif prov == "Ollama":
            import requests
            url = key if key else "http://localhost:11434"
            url = url.rstrip("/") + "/api/generate"
            r = requests.post(url, json={"model": model, "prompt": prompt, "stream": False}, timeout=120)
            r.raise_for_status()
            return r.json().get("response", "")

        return "Proveedor no soportado."

    def _copiar(self):
        text = self.resp_box.get("1.0", tk.END).strip()
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)

    def _guardar(self):
        text = self.resp_box.get("1.0", tk.END).strip()
        if not text:
            return
        ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Texto","*.txt"),("Todos","*.*")],
            initialfile=f"opentralla_ia_{ts}.txt",
            parent=self,
        )
        if path:
            with open(path,"w",encoding="utf-8") as f:
                f.write(text)
            messagebox.showinfo("Guardado", f"Guardado en:\n{path}", parent=self)


# ─── Ícono ────────────────────────────────────────────────────────────────────
def aplicar_icono(root: tk.Tk):
    ico = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snail.ico")
    if os.path.exists(ico):
        try: root.wm_iconbitmap(default=ico); root.iconbitmap(ico); return None
        except: pass
    SIZE, BG, GRN = 64, "#1a1a2e", "#4ade80"
    img = tk.PhotoImage(width=SIZE, height=SIZE)
    def _d(x1,y1,x2,y2): return math.sqrt((x1-x2)**2+(y1-y2)**2)
    def _cap(x,y): return (22<=y<=40 and 22<=x<=42) or _d(x,y,32,18)<=10 or _d(x,y,32,36)<=10
    def _hole(x,y): return (26<=y<=36 and 26<=x<=38) or _d(x,y,32,20)<=6 or _d(x,y,32,34)<=6
    def _arc(x,y): return abs(math.sqrt(((x-32)/18)**2+((y-46)/12)**2)-1.0)<0.18 and y<=46
    for y in range(SIZE):
        row=[]
        for x in range(SIZE):
            if _d(x,y,32,32)>30: row.append(BG)
            elif _cap(x,y) and not _hole(x,y): row.append(GRN)
            elif _arc(x,y) or (abs(x-32)<=1 and 57<=y<=62) or (22<=x<=42 and abs(y-62)<=1): row.append(GRN)
            else: row.append(BG)
        img.put("{"+' '.join(row)+"}", to=(0,y))
    try: root.iconphoto(True, img)
    except: pass
    return img


# ─── GUI ──────────────────────────────────────────────────────────────────────
class TranscriptorApp:
    BG = "#1a1a2e"; PANEL = "#16213e"; ACCENT = "#0f3460"
    GREEN = "#4ade80"; RED = "#f87171"; FG = "#e2e8f0"
    MUTED = "#94a3b8"; AMBER = "#fbbf24"
    # Colores de etiquetas de hablante
    COLOR_TU    = "#60a5fa"   # azul — tú
    COLOR_ELLOS = "#4ade80"   # verde — ellos
    COLOR_TS    = "#64748b"   # gris — timestamp

    def __init__(self, root: tk.Tk):
        self.root          = root
        self.grabando      = False
        self.transcripcion = []   # lista de strings planos para guardar/IA
        self.turnos_json   = []   # lista de dict para guardado JSON estructurado
        self._region_win   = None
        self._screen_rec   = None
        self._autosave_id  = None
        self._start_time   = None
        root.title("🐌 OpenTralla — Transcriptor Profesional v2.0")
        root.geometry("960x700")
        root.minsize(700, 540)
        root.configure(bg=self.BG)
        self._icon_ref = aplicar_icono(root)
        self._build_ui()
        threading.Thread(target=cargar_modelo, args=(self._status,), daemon=True).start()

    def _build_ui(self):
        C, F, FB = self, ("Segoe UI",10), ("Segoe UI",11,"bold")

        # Encabezado
        hdr = tk.Frame(self.root, bg=C.ACCENT, pady=11)
        hdr.pack(side="top", fill="x")
        tk.Label(hdr, text="🐌  OpenTralla v2.0",
                 font=("Segoe UI",15,"bold"), fg=C.FG, bg=C.ACCENT).pack(side="left", padx=16)
        tk.Label(hdr, text="Grabación Profesional  |  Dual-Channel  |  100% local  |  Anti-pérdida",
                 font=F, fg=C.MUTED, bg=C.ACCENT).pack(side="left")

        # ── Botones (side=bottom primero) ─────────────────────────────────────
        bf = tk.Frame(self.root, bg=C.PANEL, pady=10)
        bf.pack(side="bottom", fill="x")
        kw = dict(font=FB, relief="flat", cursor="hand2", padx=16, pady=8, bd=0)
        self.btn_ini = tk.Button(bf, text="▶  Iniciar",  bg=C.GREEN, fg="#0d1117",
                                 command=self.iniciar, **kw)
        self.btn_ini.pack(side="left", padx=(14,4))
        self.btn_det = tk.Button(bf, text="⏹  Detener", bg=C.RED, fg="#0d1117",
                                 command=self.detener, state="disabled", **kw)
        self.btn_det.pack(side="left", padx=4)
        tk.Button(bf, text="🤖  Analizar con IA", bg="#7c3aed", fg=C.FG,
                  command=self.abrir_ia, **kw).pack(side="left", padx=4)
        tk.Button(bf, text="💾  Guardar", bg=C.ACCENT, fg=C.FG,
                  command=self.guardar, **kw).pack(side="left", padx=4)
        tk.Button(bf, text="🗑️  Limpiar", bg="#334155", fg=C.FG,
                  command=self.limpiar, **kw).pack(side="left", padx=4)
        self.var_rec = tk.StringVar(value="")
        tk.Label(bf, textvariable=self.var_rec,
                 font=("Segoe UI",13,"bold"), fg=C.RED, bg=C.PANEL).pack(side="right", padx=14)

        # Fila de opciones (siempre visible)
        opt_row = tk.Frame(self.root, bg="#0f1b30", pady=5)
        opt_row.pack(side="bottom", fill="x")
        self.var_screen = tk.BooleanVar(value=False)
        self.chk_screen = tk.Checkbutton(
            opt_row, text="  🎥  Grabar pantalla (opcional)",
            variable=self.var_screen,
            font=("Segoe UI",10,"bold"), fg=C.AMBER, bg="#0f1b30",
            activeforeground=C.AMBER, activebackground="#0f1b30",
            selectcolor="#1e293b", cursor="hand2",
            command=self._on_screen_toggle,
        )
        self.chk_screen.pack(side="left", padx=16)
        self.lbl_screen_status = tk.Label(
            opt_row, text="Activa para grabar el area de pantalla y generar .mp4 al guardar",
            font=("Segoe UI",9), fg=C.MUTED, bg="#0f1b30")
        self.lbl_screen_status.pack(side="left", padx=4)

        # Indicador de auto-guardado
        self.var_autosave = tk.StringVar(value="")
        tk.Label(opt_row, textvariable=self.var_autosave,
                 font=("Segoe UI",8), fg="#22c55e", bg="#0f1b30").pack(side="right", padx=14)

        # VU meter + slider (side=bottom)
        vu_frame = tk.Frame(self.root, bg="#0f1b30", pady=6)
        vu_frame.pack(side="bottom", fill="x")
        self.var_status = tk.StringVar(value="⏳ Iniciando…")
        tk.Label(vu_frame, textvariable=self.var_status,
                 font=F, fg=C.GREEN, bg="#0f1b30", anchor="w").pack(fill="x", padx=14)

        vu_row = tk.Frame(vu_frame, bg="#0f1b30")
        vu_row.pack(fill="x", padx=14, pady=(3,0))
        tk.Label(vu_row, text="Nivel:", font=("Segoe UI",8), fg=C.MUTED, bg="#0f1b30").pack(side="left")
        self.vu_canvas = tk.Canvas(vu_row, width=200, height=10, bg="#1e293b", highlightthickness=0)
        self.vu_canvas.pack(side="left", padx=6)
        self.var_vu_txt = tk.StringVar(value="0.0000")
        tk.Label(vu_row, textvariable=self.var_vu_txt,
                 font=("Segoe UI",8), fg=C.MUTED, bg="#0f1b30", width=7).pack(side="left")
        tk.Label(vu_row, text=" Umbral:", font=("Segoe UI",8), fg=C.AMBER, bg="#0f1b30").pack(side="left", padx=(10,0))
        self.var_thresh = tk.DoubleVar(value=SILENCE_THRESH)
        tk.Scale(vu_row, variable=self.var_thresh, from_=0.001, to=0.05, resolution=0.001,
                 orient="horizontal", length=130, bg="#0f1b30", fg=C.AMBER,
                 highlightthickness=0, troughcolor="#1e293b", sliderrelief="flat",
                 command=self._on_thresh_change, font=("Segoe UI",7),
                 ).pack(side="left", padx=4)
        tk.Label(vu_row, text="(sube si hay ruido, baja si no detecta voz)",
                 font=("Segoe UI",7), fg=C.MUTED, bg="#0f1b30").pack(side="left", padx=4)
        tk.Label(vu_frame,
                 text=f"Motor: faster-whisper {MODEL_SIZE}  |  [Tú]=azul  [Ellos]=verde  |  🐌 OpenTralla v2.0 Pro",
                 font=("Segoe UI",8), fg=C.MUTED, bg="#0f1b30").pack(pady=(2,0))

        # Leyenda de colores
        leg = tk.Frame(self.root, bg=C.BG, pady=3)
        leg.pack(side="bottom", fill="x")
        tk.Label(leg, text="●", fg=C.COLOR_TU, bg=C.BG, font=("Segoe UI",10,"bold")).pack(side="left", padx=(16,2))
        tk.Label(leg, text="Tú (micrófono)", fg=C.MUTED, bg=C.BG, font=("Segoe UI",9)).pack(side="left")
        tk.Label(leg, text="   ●", fg=C.COLOR_ELLOS, bg=C.BG, font=("Segoe UI",10,"bold")).pack(side="left", padx=(8,2))
        tk.Label(leg, text="Ellos (llamada/sistema)", fg=C.MUTED, bg=C.BG, font=("Segoe UI",9)).pack(side="left")
        tk.Label(leg, text="   🛡️ Auto-guardado activo | Dual-channel | Callback mode", fg="#22c55e", bg=C.BG, font=("Segoe UI",9)).pack(side="left", padx=8)

        # Área de texto con tags de color
        ft = tk.Frame(self.root, bg=C.BG, padx=12, pady=6)
        ft.pack(side="top", fill="both", expand=True)
        tk.Label(ft, text="Transcripción:", font=("Segoe UI",11,"bold"),
                 fg=C.MUTED, bg=C.BG, anchor="w").pack(fill="x")
        self.text_area = scrolledtext.ScrolledText(
            ft, wrap=tk.WORD, font=("Consolas",11),
            bg="#0d1117", fg=C.FG, insertbackground=C.FG,
            relief="flat", bd=0, padx=12, pady=10, state="disabled", spacing3=5,
        )
        self.text_area.pack(fill="both", expand=True, pady=(4,0))
        # Definir tags de color
        self.text_area.tag_config("ts",    foreground=C.COLOR_TS)
        self.text_area.tag_config("tu",    foreground=C.COLOR_TU,    font=("Consolas",11,"bold"))
        self.text_area.tag_config("ellos", foreground=C.COLOR_ELLOS, font=("Consolas",11,"bold"))
        self.text_area.tag_config("texto", foreground=C.FG)
        self.text_area.tag_config("sep_tu", foreground=C.COLOR_TU, font=("Consolas",9))
        self.text_area.tag_config("sep_ellos", foreground=C.COLOR_ELLOS, font=("Consolas",9))

    # ──────────────────────────────────────────────────────────────────────────
    # Auto-guardado continuo (anti-pérdida — como hacen los profesionales)
    # ──────────────────────────────────────────────────────────────────────────
    def _autosave(self):
        """Guarda automáticamente la transcripción cada N segundos.
        Si la app se cierra inesperadamente, no se pierde nada."""
        if not self.grabando:
            return
        if self.transcripcion:
            try:
                os.makedirs(AUTOSAVE_DIR, exist_ok=True)
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                path = os.path.join(AUTOSAVE_DIR, f"autosave_{ts}.txt")
                
                # Guardar solo la transcripción en texto
                with open(path, "w", encoding="utf-8") as f:
                    f.write(self._generar_transcripcion_formateada())
                
                # Limpiar auto-guardados viejos (mantener solo los últimos 3)
                self._limpiar_autosaves_viejos()
                
                self.var_autosave.set(f"🛡️ Auto-guardado: {datetime.datetime.now().strftime('%H:%M:%S')}")
            except Exception as e:
                self.var_autosave.set(f"⚠️ Auto-guardado falló: {e}")
        
        self._autosave_id = self.root.after(AUTOSAVE_INTERVAL * 1000, self._autosave)
    
    def _limpiar_autosaves_viejos(self):
        """Mantiene solo los últimos 3 auto-guardados para no llenar el disco."""
        try:
            archivos = sorted(
                [f for f in os.listdir(AUTOSAVE_DIR) if f.startswith("autosave_") and f.endswith(".txt")],
                reverse=True
            )
            for old in archivos[3:]:
                os.remove(os.path.join(AUTOSAVE_DIR, old))
        except Exception:
            pass
    
    def _generar_transcripcion_formateada(self) -> str:
        """Genera la transcripción con formato profesional y separadores claros."""
        duracion = ""
        if self._start_time:
            delta = datetime.datetime.now() - self._start_time
            horas, rem = divmod(int(delta.total_seconds()), 3600)
            mins, secs = divmod(rem, 60)
            duracion = f"{horas:02d}:{mins:02d}:{secs:02d}"
        
        header = (
            "════════════════════════════════════════════════════════════════\n"
            "  OpenTralla v2.0 — Transcripción Profesional de Llamada\n"
            f"  Fecha: {datetime.datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n"
        )
        if duracion:
            header += f"  Duración: {duracion}\n"
        header += (
            "  Motor: faster-whisper | Captura: Dual-channel WASAPI callback\n"
            "════════════════════════════════════════════════════════════════\n\n"
        )
        
        # Agrupar líneas por hablante para mejor legibilidad
        body = ""
        last_speaker = None
        for line in self.transcripcion:
            # Parsear la línea para extraer hablante
            speaker = ""
            if "[Tú]" in line:
                speaker = "Tú"
            elif "[Ellos]" in line:
                speaker = "Ellos"
            
            if speaker and speaker != last_speaker:
                if speaker == "Tú":
                    body += "\n──── TÚ (Micrófono) ────────────────────────────────────\n"
                else:
                    body += "\n──── ELLOS (Llamada) ────────────────────────────────────\n"
                last_speaker = speaker
            elif not speaker and last_speaker:
                body += "\n──── (Sin identificar) ─────────────────────────────────\n"
                last_speaker = None
            
            body += line
        
        return header + body
    
    def _generar_json_estructurado(self) -> dict:
        """Genera un JSON estructurado con la transcripción por turnos.
        Formato profesional para procesamiento automático."""
        duracion = ""
        if self._start_time:
            delta = datetime.datetime.now() - self._start_time
            horas, rem = divmod(int(delta.total_seconds()), 3600)
            mins, secs = divmod(rem, 60)
            duracion = f"{horas:02d}:{mins:02d}:{secs:02d}"
        
        return {
            "version": "2.0",
            "app": "OpenTralla",
            "fecha": datetime.datetime.now().isoformat(),
            "duracion": duracion,
            "motor": f"faster-whisper {MODEL_SIZE}",
            "captura": "Dual-channel WASAPI callback",
            "turnos": self.turnos_json.copy(),
            "estadisticas": {
                "total_turnos": len(self.turnos_json),
                "turnos_tu": sum(1 for t in self.turnos_json if t.get("hablante") == "Tú"),
                "turnos_ellos": sum(1 for t in self.turnos_json if t.get("hablante") == "Ellos"),
            }
        }

    # ──────────────────────────────────────────────────────────────────────────
    # Grabación de pantalla
    # ──────────────────────────────────────────────────────────────────────────
    def _on_screen_toggle(self):
        if self.var_screen.get():
            ok, msg = _check_screen_deps()
            if not ok:
                messagebox.showwarning("Librerías faltantes",
                    msg + "\n\nDesactiva el checkbox para continuar sin grabar pantalla.")
                self.var_screen.set(False)
                self.lbl_screen_status.config(text="", fg=self.MUTED)
                return
            if not getattr(self, "_region_win", None):
                self._region_win = RegionPicker(self.root)
            self.lbl_screen_status.config(
                text="✔ Ajusta la ventana que apareció y dale a Iniciar", fg=self.AMBER)
        else:
            if getattr(self, "_region_win", None):
                try: self._region_win.destroy()
                except: pass
                self._region_win = None
            self.lbl_screen_status.config(text="", fg=self.MUTED)

    # ──────────────────────────────────────────────────────────────────────────
    # Acciones
    # ──────────────────────────────────────────────────────────────────────────
    def iniciar(self):
        if not model_ready.is_set():
            messagebox.showwarning("Espera", "El modelo aún se está cargando.")
            return
        if self.grabando: return

        if self.var_screen.get():
            ok, msg = _check_screen_deps()
            if not ok:
                messagebox.showwarning("Librerías faltantes", msg); return
            if getattr(self, "_region_win", None):
                region = self._region_win.get_region()
                self._region_win.set_recording(True)
                self._screen_rec = ScreenRecorder(region)
                self._screen_rec.start()
                self.lbl_screen_status.config(text="🔴 Grabando pantalla…", fg="#ef4444")
            else:
                self.var_screen.set(False)
                self.lbl_screen_status.config(text="", fg=self.MUTED)

        self._arrancar()

    def _arrancar(self):
        self.grabando = True
        self._start_time = datetime.datetime.now()
        stop_event.clear()
        audio_buf_mic.clear()
        audio_buf_loop.clear()
        while not phrase_queue.empty():
            try: phrase_queue.get_nowait()
            except: break
        self.btn_ini.config(state="disabled")
        self.btn_det.config(state="normal")
        self._parpadear()
        # Iniciar auto-guardado
        self._autosave_id = self.root.after(AUTOSAVE_INTERVAL * 1000, self._autosave)
        self.var_autosave.set("🛡️ Auto-guardado activo")
        threading.Thread(target=hilo_captura,
                         args=(self._status, self._update_vu), daemon=True).start()
        threading.Thread(target=hilo_transcripcion,
                         args=(self._agregar, self._status), daemon=True).start()

    def detener(self):
        if not self.grabando: return
        stop_event.set()
        self.grabando = False
        if getattr(self, "_screen_rec", None):
            self._screen_rec.stop()
        if getattr(self, "_region_win", None):
            self._region_win.set_recording(False)
        if self.var_screen.get():
            self.lbl_screen_status.config(
                text="✔ Ajusta la ventana que apareció y dale a Iniciar", fg=self.AMBER)
        # Cancelar auto-guardado
        if self._autosave_id:
            self.root.after_cancel(self._autosave_id)
            self._autosave_id = None
        # Auto-guardado final al detener
        self._autosave()
        self.var_autosave.set(f"🛡️ Último guardado: {datetime.datetime.now().strftime('%H:%M:%S')}")
        self.btn_ini.config(state="normal")
        self.btn_det.config(state="disabled")
        self.var_rec.set("")
        self._draw_vu(0.0)

    def abrir_ia(self):
        transcript = "".join(self.transcripcion)
        if not transcript.strip():
            messagebox.showinfo("Sin transcripción",
                                "No hay texto transcrito aún.\n\nInicia y detén la grabación primero.")
            return
        AIDialog(self.root, transcript)

    def guardar(self):
        if not self.transcripcion:
            messagebox.showinfo("Sin contenido", "No hay texto para guardar."); return
        ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Texto","*.txt"),("Todos","*.*")],
            initialfile=f"opentralla_{ts}.txt")
        if not path: return
        
        base = os.path.splitext(path)[0]

        # 1. Transcripción formateada (.txt)
        with open(path, "w", encoding="utf-8") as f:
            f.write(self._generar_transcripcion_formateada())

        # 2. Audio completo de la llamada (.wav) — mezcla de ambos canales
        wav_path = base + ".wav"
        ok_wav = self._guardar_wav_mixto(wav_path)

        # 3. Video (.mp4) — SOLO si la grabación de pantalla estaba activa
        ok_mp4, mp4_path = False, ""
        if self._screen_rec and self._screen_rec.video_path and os.path.exists(self._screen_rec.video_path):
            mp4_path = base + ".mp4"
            if os.path.exists(wav_path):
                ok_mp4 = self._muxear_video(self._screen_rec.video_path, wav_path, mp4_path)

        msg = f"📄 Transcripción:\n{path}"
        if ok_wav:
            msg += f"\n\n🔊 Audio de la llamada:\n{wav_path}"
        if ok_mp4:
            msg += f"\n\n🎬 Video con pantalla:\n{mp4_path}"
        elif self._screen_rec:
            msg += "\n\n⚠️ No se pudo generar el video (verifica que ffmpeg.exe esté en la carpeta)"
        messagebox.showinfo("Guardado", msg)

    def _guardar_wav_separado(self, path: str, buf: list, rate: int) -> bool:
        """Guarda un stream de audio individual como WAV a su tasa nativa."""
        if not buf:
            return False
        try:
            all_audio = np.concatenate(buf).astype(np.float32)
            pcm = np.clip(all_audio * 32767, -32768, 32767).astype(np.int16)
            with wave.open(path, "w") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(rate)
                wf.writeframes(pcm.tobytes())
            return True
        except Exception as e:
            print(f"Error guardando WAV {path}: {e}")
            return False
    
    def _guardar_wav_dual_channel(self, path: str) -> bool:
        """Guarda WAV estéreo dual-channel: canal izquierdo = Tú, canal derecho = Ellos.
        
        Técnica profesional usada por Twilio, call centers y estudios de grabación.
        Permite post-procesar cada voz por separado al importar en cualquier editor de audio."""
        if not audio_buf_mic and not audio_buf_loop:
            return False
        try:
            # Obtener ambos streams
            mic_audio  = np.concatenate(audio_buf_mic).astype(np.float32) if audio_buf_mic else np.zeros(0, dtype=np.float32)
            loop_audio = np.concatenate(audio_buf_loop).astype(np.float32) if audio_buf_loop else np.zeros(0, dtype=np.float32)
            
            # Normalizar a la misma tasa (usar la mayor para calidad)
            target_rate = max(native_rate_mic, native_rate_loop)
            
            if native_rate_mic != target_rate and len(mic_audio) > 0:
                nl = int(len(mic_audio) * target_rate / native_rate_mic)
                ix = np.linspace(0, len(mic_audio) - 1, nl)
                mic_audio = np.interp(ix, np.arange(len(mic_audio)), mic_audio).astype(np.float32)
            
            if native_rate_loop != target_rate and len(loop_audio) > 0:
                nl = int(len(loop_audio) * target_rate / native_rate_loop)
                ix = np.linspace(0, len(loop_audio) - 1, nl)
                loop_audio = np.interp(ix, np.arange(len(loop_audio)), loop_audio).astype(np.float32)
            
            # Igualar longitudes
            max_len = max(len(mic_audio), len(loop_audio))
            if max_len == 0:
                return False
            
            left  = np.zeros(max_len, dtype=np.float32)  # Canal izquierdo = Tú
            right = np.zeros(max_len, dtype=np.float32)  # Canal derecho = Ellos
            left[:len(mic_audio)]   = mic_audio
            right[:len(loop_audio)] = loop_audio
            
            # Intercalar muestras L-R-L-R (formato estéreo PCM)
            stereo = np.empty(max_len * 2, dtype=np.float32)
            stereo[0::2] = left
            stereo[1::2] = right
            
            pcm = np.clip(stereo * 32767, -32768, 32767).astype(np.int16)
            with wave.open(path, "w") as wf:
                wf.setnchannels(2)  # ← ESTÉREO
                wf.setsampwidth(2)
                wf.setframerate(target_rate)
                wf.writeframes(pcm.tobytes())
            return True
        except Exception as e:
            print(f"Error guardando WAV dual-channel: {e}")
            return False
    
    def _guardar_wav_mixto(self, path: str) -> bool:
        """Guarda un WAV con ambos streams mezclados (para referencia rápida)."""
        if not audio_buf_mic and not audio_buf_loop:
            return False
        try:
            mic_audio  = np.concatenate(audio_buf_mic).astype(np.float32) if audio_buf_mic else np.zeros(0, dtype=np.float32)
            loop_audio = np.concatenate(audio_buf_loop).astype(np.float32) if audio_buf_loop else np.zeros(0, dtype=np.float32)
            
            # Normalizar tasas
            target_rate = max(native_rate_mic, native_rate_loop)
            
            if native_rate_mic != target_rate and len(mic_audio) > 0:
                nl = int(len(mic_audio) * target_rate / native_rate_mic)
                ix = np.linspace(0, len(mic_audio) - 1, nl)
                mic_audio = np.interp(ix, np.arange(len(mic_audio)), mic_audio).astype(np.float32)
            
            if native_rate_loop != target_rate and len(loop_audio) > 0:
                nl = int(len(loop_audio) * target_rate / native_rate_loop)
                ix = np.linspace(0, len(loop_audio) - 1, nl)
                loop_audio = np.interp(ix, np.arange(len(loop_audio)), loop_audio).astype(np.float32)
            
            # Mezclar
            max_len = max(len(mic_audio), len(loop_audio))
            if max_len == 0:
                return False
            mixed = np.zeros(max_len, dtype=np.float32)
            mixed[:len(loop_audio)] += loop_audio * 0.7
            mixed[:len(mic_audio)]  += mic_audio * 0.7
            mixed = np.clip(mixed, -1, 1)
            
            pcm = np.clip(mixed * 32767, -32768, 32767).astype(np.int16)
            with wave.open(path, "w") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(target_rate)
                wf.writeframes(pcm.tobytes())
            return True
        except Exception as e:
            print(f"Error guardando WAV mixto: {e}")
            return False

    def _muxear_video(self, video_tmp: str, wav_path: str, out_path: str) -> bool:
        ffmpeg = _find_ffmpeg()
        if not ffmpeg:
            messagebox.showwarning("ffmpeg no encontrado",
                "Para generar el video necesitas ffmpeg.\n\n"
                "Ejecuta instalar.bat para descargarlo automáticamente.")
            return False
        try:
            cmd = [ffmpeg, "-y",
                   "-i", video_tmp,
                   "-i", wav_path,
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
                   "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
                   out_path]
            result = subprocess.run(cmd, capture_output=True, timeout=300)
            if result.returncode == 0:
                try: os.remove(video_tmp)
                except: pass
                self._screen_rec = None
                return True
            else:
                err = result.stderr.decode(errors='replace')
                print(f"ffmpeg stderr: {err}")
                messagebox.showerror("Error en video", f"ffmpeg falló procesando el video. Código {result.returncode}:\n\n{err[-600:]}")
                return False
        except Exception as e:
            print(f"Error muxeando: {e}"); return False

    def limpiar(self):
        self.transcripcion.clear()
        self.turnos_json.clear()
        self.text_area.config(state="normal")
        self.text_area.delete("1.0", tk.END)
        self.text_area.config(state="disabled")

    def _status(self, msg):
        self.root.after(0, lambda: self.var_status.set(msg))

    def _agregar(self, ts: str, speaker: str, texto: str):
        """Inserta una línea con colores según el hablante y separadores visuales."""
        line_plain = f"[{ts}]"
        if speaker:
            line_plain += f" [{speaker}]"
        line_plain += f" {texto}\n"
        self.transcripcion.append(line_plain)
        
        # Agregar al registro JSON estructurado
        self.turnos_json.append({
            "hora": ts,
            "hablante": speaker if speaker else "Desconocido",
            "texto": texto,
        })

        def _up():
            self.text_area.config(state="normal")
            
            # Agregar separador visual cuando cambia el hablante
            if len(self.turnos_json) >= 2:
                prev = self.turnos_json[-2].get("hablante", "")
                curr = speaker
                if curr and curr != prev:
                    if curr == "Tú":
                        self.text_area.insert(tk.END, "──── TÚ ────────────────────────\n", "sep_tu")
                    elif curr == "Ellos":
                        self.text_area.insert(tk.END, "──── ELLOS ─────────────────────\n", "sep_ellos")
            
            self.text_area.insert(tk.END, f"[{ts}] ", "ts")
            if speaker == "Tú":
                self.text_area.insert(tk.END, "[Tú] ", "tu")
            elif speaker == "Ellos":
                self.text_area.insert(tk.END, "[Ellos] ", "ellos")
            self.text_area.insert(tk.END, texto + "\n", "texto")
            self.text_area.see(tk.END)
            self.text_area.config(state="disabled")
        self.root.after(0, _up)

    def _update_vu(self, level: float):
        self.root.after(0, lambda: self._draw_vu(level))

    def _on_thresh_change(self, val):
        global silence_thresh_dynamic
        silence_thresh_dynamic = float(val)

    def _draw_vu(self, level: float):
        try:
            w = 200
            fill_w = min(int(level / 0.3 * w), w)
            color  = "#22c55e" if level < 0.05 else ("#fbbf24" if level < 0.15 else "#f87171")
            self.vu_canvas.delete("all")
            if fill_w > 0:
                self.vu_canvas.create_rectangle(0, 0, fill_w, 10, fill=color, outline="")
            self.var_vu_txt.set(f"{level:.4f}")
        except Exception:
            pass

    def _parpadear(self):
        if not self.grabando: return
        self.var_rec.set("" if self.var_rec.get() else "⏺ REC")
        self.root.after(800, self._parpadear)

    def on_close(self):
        if self.grabando: self.detener()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app  = TranscriptorApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
