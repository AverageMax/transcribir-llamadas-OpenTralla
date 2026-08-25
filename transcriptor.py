# -*- coding: utf-8 -*-
"""
OpenTralla - Grabadora + Transcriptor de Llamadas v2.4
══════════════════════════════════════════════════════
Flujo por defecto: GRABAR primero, TRANSCRIBIR después.
Durante la llamada solo se graba audio — nada de Whisper, nada de VAD — para
que la CPU quede libre y la videollamada no se degrade (voz robotizada/cortes).
Al colgar, el botón «Transcribir» procesa la grabación entera de una vez.

- Grabación a DISCO según entra: la RAM no crece con la duración de la llamada
- PCM crudo sin cabecera: a prueba de cierres inesperados, nada que reparar
- Streams sincronizados: se rellenan con silencio los huecos del loopback
  (WASAPI no entrega paquetes mientras el sistema no reproduce nada)
- Recuperación al abrir: si quedó una grabación sin transcribir, te la ofrece
- Captura a tasa NATIVA del dispositivo (44100/48000Hz) — sin resampleo destructivo
- Modo CALLBACK (no-bloqueante) — elimina chasquidos/crujidos por buffer underrun
- Streams 100% SEPARADOS: mic (Tú) y loopback (Ellos)
- Fade-in anti-pop al iniciar — elimina el clic inicial
- Carga perezosa del modelo: la app abre al instante
- Transcripción estructurada + JSON

Diarización: mic=[Tú] / loopback=[Ellos] comparando actividad por stream
Transcripción en vivo opcional (interruptor «⚡ Transcribir en vivo», apagado
por defecto) con VAD de umbral ajustable y pre-roll de 0.6s
Resumen/análisis con Gemini, Claude, OpenAI o Ollama (local)
"""

import sys, site, os, math, ctypes, threading, queue, datetime, json, wave, struct, tempfile, subprocess, shutil, time

_user_site = site.getusersitepackages()
if _user_site not in sys.path:
    sys.path.insert(0, _user_site)

try:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("OpenTralla.Transcriptor.1")
    # Establecer prioridad del proceso a BELOW_NORMAL_PRIORITY_CLASS (0x00004000)
    # Esto asegura que Zoom/Teams/Discord y el motor de audio tengan prioridad sobre Whisper en CPU
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00004000)
except Exception:
    pass

# Nota: No elevamos la prioridad del proceso entero a HIGH para evitar
# que el procesamiento pesado de Whisper en CPU sature el sistema y
# degrade el audio en aplicaciones de videollamada como Zoom o Teams.


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

# ─── Config y parámetros ────────────────────────────────────────────────────────
CONFIG_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
AUTOSAVE_DIR   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_autosave")
# Aquí van las grabaciones. El audio se escribe a disco mientras grabas, así que
# la RAM se mantiene plana aunque la llamada dure horas y nada se pierde si la
# app se cierra de golpe.
GRAB_DIR       = os.path.join(os.path.dirname(os.path.abspath(__file__)), "grabaciones")

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

_cfg = load_config()
MODEL_SIZE     = _cfg.get("whisper_model_size", "small")
WHISPER_THREADS = _cfg.get("whisper_cpu_threads", 2)
USAR_MIC        = _cfg.get("usar_mic", True)
# Transcripción en vivo: DESACTIVADA por defecto.
# Modo grabadora = la CPU queda libre durante la llamada (sin voz robotizada).
LIVE_TRANSCRIBE = _cfg.get("transcripcion_vivo", False)

WHISPER_RATE   = 16000                  # Tasa que Whisper necesita (solo para transcripción)
CHUNK_FRAMES   = 2048                   # Buffer grande = sin chasquidos (era 512 → underruns)
LANGUAGE       = "es"

SILENCE_SEC    = 0.8                    # Segundos de silencio para cortar frase
MIN_SPEECH_SEC = 0.3                    # Mínimo de voz para considerar frase
MAX_PHRASE_SEC = 25                     # Máximo antes de forzar corte
SILENCE_THRESH = 0.003                  # Umbral VAD por defecto
FADE_IN_MS     = 15                     # Duración del fade-in anti-pop (ms)
AUTOSAVE_INTERVAL = 30                  # Segundos entre auto-guardados
SYNC_INTERVAL  = 10                     # Segundos entre volcados forzados a disco
HUECO_MIN_SEG  = 0.25                   # Hueco mínimo que se rellena con silencio.
# 0.25 s va holgado sobre el jitter normal (los chunks llegan cada ~43 ms) y
# corrige el desfase de arranque entre mic y loopback, que abren en instantes
# distintos y dejaban los dos canales hasta 0.5 s descuadrados.
SIN_AUDIO_AVISO = 5                     # Segundos sin recibir audio antes de avisar
SIN_AUDIO_REPITE = 30                   # Cada cuánto se repite ese aviso
MIC_MUDO_RATIO = 0.25                   # Ceros digitales que delatan un mic silenciado
MIC_MUDO_MIN_SEG = 20                   # Audio mínimo antes de juzgar al micrófono
VACIO_F32      = np.zeros(0, dtype=np.float32)

# ─── Estado global ─────────────────────────────────────────────────────────────
phrase_queue: queue.Queue = queue.Queue()   # items: (audio_f32_16k, speaker_label)
stop_event    = threading.Event()
model_ready   = threading.Event()
whisper_model = None
silence_thresh_dynamic = SILENCE_THRESH
_loaded_model_key = None                      # (tamaño, hilos) del modelo en RAM
offline_cancel = threading.Event()            # cancelar la transcripción diferida

# El audio ya NO se acumula en memoria: se escribe a disco en la sesión activa
# (ver SesionGrabacion). Estas tasas las fija el motor al detectar el hardware.
native_rate_loop: int = 48000 # Se actualiza al detectar dispositivo
native_rate_mic:  int = 48000


# ─── Modelo ───────────────────────────────────────────────────────────────────
def get_whisper_device() -> tuple:
    """Detecta si CUDA está disponible en ctranslate2 para usar GPU."""
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", "float16"
    except Exception:
        pass
    return "cpu", "int8"

def cargar_modelo(status_cb):
    global whisper_model, _loaded_model_key
    model_ready.clear()
    _loaded_model_key = None
    whisper_model = None          # libera el modelo anterior antes de cargar otro
    status_cb(f"⏳ Cargando modelo '{MODEL_SIZE}'…")
    try:
        # Detectar si hay CUDA disponible en ctranslate2
        device, compute_type = get_whisper_device()
        
        if device == "cuda":
            status_cb(f"⏳ Cargando modelo '{MODEL_SIZE}' en GPU (CUDA)…")
            whisper_model = WhisperModel(MODEL_SIZE, device="cuda", compute_type=compute_type)
        else:
            status_cb(f"⏳ Cargando modelo '{MODEL_SIZE}' en CPU ({WHISPER_THREADS} hilos)…")
            whisper_model = WhisperModel(MODEL_SIZE, device="cpu", compute_type=compute_type, cpu_threads=WHISPER_THREADS)
            
        _loaded_model_key = (MODEL_SIZE, WHISPER_THREADS)
        model_ready.set()
        status_cb(f"✅ Modelo '{MODEL_SIZE}' listo ({device.upper()})")
    except Exception as e:
        status_cb(f"❌ Error cargando modelo: {e}")


def modelo_vigente() -> bool:
    """¿Hay un modelo cargado y coincide con la configuración actual?"""
    return model_ready.is_set() and _loaded_model_key == (MODEL_SIZE, WHISPER_THREADS)

def asegurar_modelo(status_cb) -> bool:
    """Carga perezosa: solo carga Whisper si realmente hace falta.
    En modo grabadora el modelo NO se toca hasta que pulsas «Transcribir»."""
    if modelo_vigente():
        return True
    cargar_modelo(status_cb)
    return model_ready.is_set()

def liberar_modelo():
    """Saca el modelo de la RAM. Se usa al pasar a modo grabadora para dejar
    la memoria y la CPU libres durante la videollamada."""
    global whisper_model, _loaded_model_key
    model_ready.clear()
    whisper_model = None
    _loaded_model_key = None


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

def resample_largo(audio: np.ndarray, source_rate: int, target_rate: int = WHISPER_RATE,
                   bloque_seg: int = 60) -> np.ndarray:
    """Resamplea grabaciones LARGAS (una llamada entera) sin reventar la RAM.

    - Si la tasa es múltiplo exacta (48000→16000) promedia cada N muestras:
      es un downsample con antialiasing, mejor que interpolar.
    - Si no (44100→16000) interpola por bloques de 60 s en vez de generar
      un índice gigante de toda la grabación de una sola vez.
    """
    if len(audio) == 0:
        return np.zeros(0, dtype=np.float32)
    if source_rate == target_rate:
        return audio.astype(np.float32, copy=False)
    if source_rate % target_rate == 0:
        f = source_rate // target_rate
        n = (len(audio) // f) * f
        if n == 0:
            return np.zeros(0, dtype=np.float32)
        return audio[:n].reshape(-1, f).mean(axis=1).astype(np.float32)
    partes = []
    paso = source_rate * bloque_seg
    for i in range(0, len(audio), paso):
        seg = audio[i:i + paso]
        nl = int(round(len(seg) * target_rate / source_rate))
        if nl <= 0:
            continue
        ix = np.linspace(0, len(seg) - 1, nl)
        partes.append(np.interp(ix, np.arange(len(seg)), seg).astype(np.float32))
    return np.concatenate(partes) if partes else np.zeros(0, dtype=np.float32)

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


# ─── Grabación en disco (streams separados, RAM plana) ───────────────────────
# El audio se vuelca a disco según entra, en PCM crudo int16 mono por stream.
# Sin cabeceras a propósito: si la app se cierra de golpe el archivo sigue siendo
# válido hasta el último byte escrito, no hay nada que reparar. El WAV final se
# genera al guardar, leyendo por bloques.

def f32_a_pcm16(a: np.ndarray) -> np.ndarray:
    """float32 [-1,1] → PCM int16.

    Se REDONDEA, no se trunca: astype() por sí solo corta hacia cero y se come
    el bit menos significativo, convirtiendo el suelo de ruido en ceros exactos.
    Eso añade distorsión en los pasajes flojos y además falsearía la detección
    de micrófono silenciado.
    """
    return np.clip(np.rint(a * 32767.0), -32768, 32767).astype(np.int16)


def _leer_bloques_pcm(path: str, muestras: int):
    """Generador de bloques float32 leídos de un .pcm int16 mono."""
    if not os.path.exists(path):
        return
    with open(path, "rb") as f:
        while True:
            raw = f.read(muestras * 2)
            if not raw:
                return
            if len(raw) % 2:          # corte a mitad de muestra (cierre brusco)
                raw = raw[:-1]
            if not raw:
                return
            yield np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def leer_pcm_a_16k(path: str, source_rate: int, bloque_seg: int = 60) -> np.ndarray:
    """Lee un .pcm entero y lo devuelve YA remuestreado a 16 kHz.

    Nunca carga la grabación completa a tasa nativa: para una llamada de 1 h el
    pico de RAM baja de ~690 MB por stream a ~115 MB.
    """
    partes = [resample_largo(b, source_rate)
              for b in _leer_bloques_pcm(path, source_rate * bloque_seg)]
    partes = [p for p in partes if len(p) > 0]
    return np.concatenate(partes) if partes else np.zeros(0, dtype=np.float32)


class SesionGrabacion:
    """Una llamada grabada, guardada en su propia carpeta dentro de grabaciones/.

    Contenido de la carpeta:
      ellos.pcm   audio de la videollamada (loopback del sistema)
      tu.pcm      tu micrófono
      sesion.json tasas de muestreo, hora de inicio y si hubo micrófono
      .listo      marca de "ya transcrita/guardada" (para no volver a ofrecerla)
    """

    def __init__(self, carpeta: str):
        self.carpeta   = carpeta
        self.path_loop = os.path.join(carpeta, "ellos.pcm")
        self.path_mic  = os.path.join(carpeta, "tu.pcm")
        self.path_meta = os.path.join(carpeta, "sesion.json")
        self.rate_loop = 48000
        self.rate_mic  = 48000
        self.mic_ok    = False
        self.inicio    = None
        self._f_loop   = None
        self._f_mic    = None
        self._lock     = threading.Lock()
        self._t0       = None   # reloj de referencia para mantener la sincronía
        self._n_loop   = 0      # muestras escritas en ellos.pcm
        self._n_mic    = 0      # muestras escritas en tu.pcm
        self.mic_muestras = 0   # muestras reales llegadas del micrófono
        self.mic_ceros    = 0   # cuántas de ellas eran cero digital exacto

    # ── creación / apertura ──
    @staticmethod
    def nueva() -> "SesionGrabacion":
        ahora = datetime.datetime.now()
        base = os.path.join(GRAB_DIR, "sesion_" + ahora.strftime("%Y%m%d_%H%M%S"))
        # Si paras y vuelves a grabar dentro del mismo segundo, el nombre
        # coincidiría y la grabación nueva pisaría la anterior. Nunca.
        carpeta, n = base, 2
        while os.path.exists(carpeta):
            carpeta = f"{base}_{n}"
            n += 1
        os.makedirs(carpeta, exist_ok=True)
        s = SesionGrabacion(carpeta)
        s.inicio = ahora
        return s

    @staticmethod
    def abrir(carpeta: str) -> "SesionGrabacion":
        """Carga una sesión que ya está en disco (recuperación tras un cierre)."""
        s = SesionGrabacion(carpeta)
        try:
            with open(s.path_meta, encoding="utf-8") as f:
                meta = json.load(f)
            s.rate_loop = int(meta.get("rate_loop", 48000))
            s.rate_mic  = int(meta.get("rate_mic", 48000))
            s.mic_ok    = bool(meta.get("mic_ok", False))
            if meta.get("inicio"):
                s.inicio = datetime.datetime.fromisoformat(meta["inicio"])
        except Exception:
            pass
        return s

    # ── escritura (desde el hilo de captura) ──
    def empezar(self, rate_loop: int, rate_mic: int, mic_ok: bool):
        self.rate_loop, self.rate_mic, self.mic_ok = rate_loop, rate_mic, mic_ok
        os.makedirs(self.carpeta, exist_ok=True)
        with self._lock:
            self._f_loop = open(self.path_loop, "wb")
            self._f_mic  = open(self.path_mic, "wb") if mic_ok else None
            self._t0     = time.monotonic()
            self._n_loop = self._n_mic = 0
            self.mic_muestras = self.mic_ceros = 0
        self._guardar_meta()

    def _guardar_meta(self):
        try:
            with open(self.path_meta, "w", encoding="utf-8") as f:
                json.dump({
                    "app": "OpenTralla",
                    "inicio": self.inicio.isoformat() if self.inicio else None,
                    "rate_loop": self.rate_loop,
                    "rate_mic": self.rate_mic,
                    "mic_ok": self.mic_ok,
                    "formato": "PCM crudo int16 mono, sin cabecera",
                }, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"No se pudo escribir sesion.json: {e}")

    @staticmethod
    def _rellenar(f, escritas: int, rate: int, transcurrido: float, n_chunk: int) -> int:
        """Tapa con silencio el hueco que deja un stream mudo, y devuelve cuántas
        muestras de relleno escribió.

        Hace falta porque WASAPI NO entrega paquetes de loopback mientras el
        sistema no está reproduciendo nada. Sin este relleno, el audio de la
        videollamada se pegaría al principio del archivo y quedaría desplazado
        respecto a tu micrófono: las voces sonarían descolocadas en el WAV y la
        diarización atribuiría las frases al hablante equivocado.
        """
        faltan = int(transcurrido * rate) - escritas - n_chunk
        if faltan < int(rate * HUECO_MIN_SEG):
            return 0
        cero = np.zeros(min(faltan, rate), dtype=np.int16).tobytes()
        restantes = faltan
        while restantes > 0:
            n = min(restantes, rate)
            f.write(cero[:n * 2])
            restantes -= n
        return faltan

    def escribir(self, loop_c: np.ndarray, mic_c: np.ndarray):
        """Vuelca al disco los chunks del período, manteniendo ambos streams
        alineados en el tiempo."""
        with self._lock:
            if self._t0 is None:
                return
            transcurrido = time.monotonic() - self._t0
            if self._f_loop is not None:
                self._n_loop += self._rellenar(self._f_loop, self._n_loop,
                                               self.rate_loop, transcurrido, len(loop_c))
                if len(loop_c) > 0:
                    self._f_loop.write(f32_a_pcm16(loop_c).tobytes())
                    self._n_loop += len(loop_c)
            if self._f_mic is not None:
                self._n_mic += self._rellenar(self._f_mic, self._n_mic,
                                              self.rate_mic, transcurrido, len(mic_c))
                if len(mic_c) > 0:
                    pcm = f32_a_pcm16(mic_c)
                    # Un micrófono real SIEMPRE entrega suelo de ruido. Si llega
                    # cero digital exacto es que algo lo está silenciando por
                    # software (cancelación de ruido con IA, mute, un efecto).
                    self.mic_muestras += len(pcm)
                    self.mic_ceros    += int(np.count_nonzero(pcm == 0))
                    self._f_mic.write(pcm.tobytes())
                    self._n_mic += len(mic_c)

    def sincronizar(self):
        """Fuerza el volcado al disco — lo que ya está escrito sobrevive a un
        cierre brusco, un cuelgue o un corte de luz."""
        with self._lock:
            for f in (self._f_loop, self._f_mic):
                if f is None:
                    continue
                try:
                    f.flush()
                    os.fsync(f.fileno())
                except Exception:
                    pass

    def cerrar(self):
        self.sincronizar()
        with self._lock:
            for f in (self._f_loop, self._f_mic):
                if f is not None:
                    try: f.close()
                    except Exception: pass
            self._f_loop = self._f_mic = None
            self._t0 = None
        self._guardar_meta()

    # ── lectura / estado ──
    @staticmethod
    def _muestras(path: str) -> int:
        try:
            return os.path.getsize(path) // 2
        except OSError:
            return 0

    def n_loop(self) -> int: return self._muestras(self.path_loop)
    def n_mic(self)  -> int: return self._muestras(self.path_mic)

    def tiene_audio(self) -> bool:
        return self.n_loop() > 0 or self.n_mic() > 0

    def mic_silenciado(self) -> float:
        """Proporción de cero digital exacto que ha entregado el micrófono.

        Por encima de ~0.25 el micrófono no está grabando sonido de verdad:
        algo lo está recortando y la voz sale entrecortada («robotizada»)
        tanto en la grabación como en la videollamada.
        """
        if self.mic_muestras < MIC_MUDO_MIN_SEG * self.rate_mic:
            return 0.0
        return self.mic_ceros / self.mic_muestras

    def duracion_seg(self) -> float:
        d_loop = self.n_loop() / self.rate_loop if self.rate_loop else 0.0
        d_mic  = self.n_mic()  / self.rate_mic  if self.rate_mic  else 0.0
        return max(d_loop, d_mic)

    def tamano_mb(self) -> float:
        total = 0
        for p in (self.path_loop, self.path_mic):
            try: total += os.path.getsize(p)
            except OSError: pass
        return total / (1024 * 1024)

    def marcar_listo(self):
        """Marca la sesión como ya atendida para no volver a ofrecerla al abrir."""
        try:
            with open(os.path.join(self.carpeta, ".listo"), "w", encoding="utf-8") as f:
                f.write(datetime.datetime.now().isoformat())
        except Exception:
            pass

    def borrar(self) -> bool:
        """Borra la carpeta de la sesión (solo se llama si el usuario lo pide)."""
        try:
            shutil.rmtree(self.carpeta)
            return True
        except Exception as e:
            print(f"No se pudo borrar {self.carpeta}: {e}")
            return False


def sesiones_pendientes() -> list:
    """Grabaciones en disco con audio y sin marcar como atendidas, de la más
    reciente a la más vieja. Es la red de seguridad si la app se cerró sola."""
    if not os.path.isdir(GRAB_DIR):
        return []
    out = []
    try:
        nombres = sorted(os.listdir(GRAB_DIR), reverse=True)
    except OSError:
        return []
    for nombre in nombres:
        carpeta = os.path.join(GRAB_DIR, nombre)
        if not nombre.startswith("sesion_") or not os.path.isdir(carpeta):
            continue
        if os.path.exists(os.path.join(carpeta, ".listo")):
            continue
        s = SesionGrabacion.abrir(carpeta)
        if s.tiene_audio():
            out.append(s)
    return out


def escribir_wav_mezcla(sesion: SesionGrabacion, destino: str,
                        ganancia: float = 0.7, bloque_seg: int = 30) -> bool:
    """Genera el WAV final de la llamada mezclando ambos streams.

    Se lee y se escribe por bloques, así que la RAM no crece con la duración:
    una llamada de 3 horas se guarda con el mismo consumo que una de 3 minutos.
    """
    n_loop, n_mic = sesion.n_loop(), sesion.n_mic()
    if n_loop == 0 and n_mic == 0:
        return False
    # El loopback manda: es la referencia temporal del resto de la app
    rate = sesion.rate_loop if n_loop else sesion.rate_mic
    g_loop = _leer_bloques_pcm(sesion.path_loop, rate * bloque_seg) if n_loop else iter(())
    g_mic  = _leer_bloques_pcm(sesion.path_mic, int(sesion.rate_mic * bloque_seg)) if n_mic else iter(())
    try:
        with wave.open(destino, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            while True:
                b_loop = next(g_loop, None)
                b_mic  = next(g_mic,  None)
                if b_loop is None and b_mic is None:
                    break
                if b_mic is not None and len(b_mic) > 0 and sesion.rate_mic != rate:
                    b_mic = resample_largo(b_mic, sesion.rate_mic, rate)
                n = max(len(b_loop) if b_loop is not None else 0,
                        len(b_mic)  if b_mic  is not None else 0)
                if n == 0:
                    continue
                mez = np.zeros(n, dtype=np.float32)
                if b_loop is not None:
                    mez[:len(b_loop)] += b_loop * ganancia
                if b_mic is not None:
                    mez[:len(b_mic)] += b_mic * ganancia
                wf.writeframes(f32_a_pcm16(np.clip(mez, -1, 1)).tobytes())
        return True
    except Exception as e:
        print(f"Error escribiendo {destino}: {e}")
        return False


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
    
    def __init__(self, status_cb, rms_cb, usar_mic=True):
        self.status_cb = status_cb
        self.rms_cb = rms_cb
        self.usar_mic = usar_mic
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
        if self.usar_mic:
            try:
                dev_mic = None
                # Intentar buscar el micrófono por defecto en la API de WASAPI para evitar mezclar APIs y causar distorsión
                for i in range(self.pa.get_host_api_count()):
                    info = self.pa.get_host_api_info_by_index(i)
                    if info.get("type") == 13 or "WASAPI" in info.get("name", ""):
                        dev_idx = info.get("defaultInputDevice")
                        if dev_idx is not None and dev_idx >= 0:
                            try:
                                dev_mic = self.pa.get_device_info_by_index(dev_idx)
                            except Exception:
                                pass
                        break
                
                # Fallback
                if not dev_mic:
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
        else:
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


def hilo_captura(status_cb, rms_cb, usar_mic=True, live=False, sesion=None):
    """Hilo principal de captura. Con live=True además hace VAD y diarización.

    El audio siempre se escribe a `sesion` (disco) según entra, nunca se acumula
    en RAM: una llamada de 3 horas consume lo mismo que una de 3 minutos.

    MODO GRABADORA (live=False, por defecto): solo se vuelca el audio a disco.
    Cero Whisper, cero VAD — la CPU queda libre para Zoom/Teams durante la
    llamada, sin voz robotizada.

    MODO EN VIVO (live=True):
    
    Arquitectura profesional (tipo call center):
    1. El motor de audio captura con callbacks (sin bloqueo)
    2. Este hilo lee las colas periódicamente y aplica VAD
    3. Diarización por comparación de actividad en cada stream
    4. SIEMPRE se envía audio combinado a Whisper — nunca se pierde nada
    """
    global silence_thresh_dynamic
    
    engine = AudioCaptureEngine(status_cb, rms_cb, usar_mic)
    
    try:
        engine.start()
        if sesion is not None:
            sesion.empezar(engine.rate_l, engine.rate_m, engine.mic_ok)
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
    
    reconnect_attempts = 0
    max_reconnect_attempts = 5
    last_vu      = 0.0   # limita los refrescos del VU meter (no saturar el hilo Tk)
    ultimo_sync  = 0.0   # último volcado forzado a disco
    ultimo_audio = time.monotonic()   # última vez que llegó audio de verdad
    ultimo_grito = 0.0   # última vez que avisamos de que no llega nada
    aviso_mic    = False # ya avisamos de micrófono silenciado
    
    try:
        while not stop_event.is_set():
            # Verificar si los streams siguen activos o si se detuvieron por un cambio de hardware
            loop_active = engine.stream_loop and engine.stream_loop.is_active()
            mic_active = not engine.mic_ok or (engine.stream_mic and engine.stream_mic.is_active())
            
            if not loop_active or not mic_active:
                if reconnect_attempts < max_reconnect_attempts:
                    reconnect_attempts += 1
                    status_cb(f"⏳ Audio desconectado. Intentando reconectar ({reconnect_attempts}/{max_reconnect_attempts})…")
                    time.sleep(1.5)
                    try:
                        engine.stop()
                        engine = AudioCaptureEngine(status_cb, rms_cb, usar_mic)
                        engine.start()
                        
                        # Actualizar tasas e info con el nuevo hardware de audio
                        work_rate = engine.rate_l
                        sil_need      = int(SILENCE_SEC     * work_rate)
                        min_sp        = int(MIN_SPEECH_SEC  * work_rate)
                        max_ph        = int(MAX_PHRASE_SEC  * work_rate)
                        pre_roll_max  = int(0.6 * work_rate)
                        
                        # Limpiar buffers de frase activa para evitar desalineación
                        speech_buf_loop = np.zeros(0, dtype=np.float32)
                        speech_buf_mic  = np.zeros(0, dtype=np.float32)
                        rms_loop_acc.clear()
                        rms_mic_acc.clear()
                        in_speech = False
                        
                        status_cb("🔴 Escuchando…")
                        continue
                    except Exception as re_err:
                        print(f"Error al reconectar dispositivos de audio: {re_err}")
                        continue
                else:
                    raise RuntimeError("Los dispositivos de audio no responden tras varios intentos.")
            else:
                # Si reconectó con éxito, reiniciar contador de intentos
                reconnect_attempts = 0

            # Leer chunks de ambas colas (no-bloqueante)
            loop_chunks, mic_chunks = engine.get_chunks()
            
            if not loop_chunks and not mic_chunks:
                # No llega nada. Aun así hay que tocar la sesión: el relleno
                # mantiene la línea de tiempo, y la vigilancia avisa en pantalla
                # para que la grabación no se quede muda «por detrás» sin que
                # nadie se entere (que es justo lo que pasó el 24/08).
                if sesion is not None:
                    sesion.escribir(VACIO_F32, VACIO_F32)
                ahora = time.monotonic()
                mudo = ahora - ultimo_audio
                if mudo >= SIN_AUDIO_AVISO and ahora - ultimo_grito >= SIN_AUDIO_REPITE:
                    status_cb(f"⚠️ NO LLEGA AUDIO desde hace {int(mudo)}s — "
                              "revisa el micrófono y el sonido del sistema")
                    ultimo_grito = ahora
                time.sleep(0.01)
                continue
            
            # Sí llegó audio: reiniciar la vigilancia
            if ultimo_grito:
                status_cb("🔴 Grabando — el audio volvió")
                ultimo_grito = 0.0
            ultimo_audio = time.monotonic()

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
            
            # ── Volcado a disco ──
            # El audio va directo al archivo de la sesión, a calidad nativa.
            # No se acumula nada en RAM, y lo ya escrito sobrevive a un cierre
            # brusco, un cuelgue o un corte de luz.
            ahora = time.time()
            if sesion is not None:
                sesion.escribir(loop_c, mic_c if engine.mic_ok else VACIO_F32)
                if ahora - ultimo_sync >= SYNC_INTERVAL:
                    sesion.sincronizar()
                    ultimo_sync = ahora
            
            # VU meter — máximo ~12 refrescos/s para no saturar el hilo de Tk
            level = max(rms_loop_now, rms_mic_now)
            if ahora - last_vu >= 0.08:
                rms_cb(level)
                last_vu = ahora

            # ── ¿El micrófono está entregando silencio digital? ──
            if (not aviso_mic and sesion is not None and engine.mic_ok
                    and sesion.mic_silenciado() > MIC_MUDO_RATIO):
                aviso_mic = True
                pct = int(sesion.mic_silenciado() * 100)
                status_cb(f"⚠️ Tu micrófono entrega {pct}% de silencio digital — "
                          "desactiva la cancelación de ruido con IA (tu voz sale cortada)")

            # ── MODO GRABADORA ──
            # Sin transcripción en vivo el hilo termina aquí: no hay VAD,
            # ni diarización, ni Whisper. Solo se copia audio a memoria.
            if not live:
                continue

            # ── VAD combinado (solo con transcripción en vivo) ──
            # Usamos el nivel más alto de AMBOS streams como referencia
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

            # ── Los dos streams tienen que avanzar a la vez ──
            # WASAPI no entrega ni un paquete de loopback mientras el sistema no
            # reproduce nada. El VAD medía el tiempo con len(loop_c), así que al
            # hablar tú a solas el contador de silencio no avanzaba nunca: la
            # frase no se cerraba y NO llegaba nada a Whisper (la ventana se
            # quedaba en blanco). Rellenando el que venga corto, el reloj del VAD
            # avanza siempre y ambos buffers quedan alineados, que además evita
            # que _prepare_audio_for_whisper recorte la frase al más corto.
            n_periodo = max(len(loop_c), len(mic_c_aligned))
            if len(loop_c) < n_periodo:
                loop_c = np.concatenate(
                    [loop_c, np.zeros(n_periodo - len(loop_c), dtype=np.float32)])
            if len(mic_c_aligned) < n_periodo:
                mic_c_aligned = np.concatenate(
                    [mic_c_aligned, np.zeros(n_periodo - len(mic_c_aligned), dtype=np.float32)])

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
        if sesion is not None:
            sesion.cerrar()


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


# ─── Transcripción DIFERIDA (al final de la llamada) ─────────────────────────
# Este es el camino por defecto: se graba la llamada limpia y Whisper corre
# después, con la CPU libre. Además sale MEJOR que en vivo, porque Whisper
# recibe la grabación entera y tiene el contexto completo de la conversación.

def _rms_ventanas(sig: np.ndarray, t0: float, t1: float,
                  rate: int = WHISPER_RATE, win: float = 0.1) -> list:
    """RMS por ventanas de 100 ms dentro de un rango temporal del audio."""
    i0 = max(0, int(t0 * rate))
    i1 = min(len(sig), int(t1 * rate))
    if i1 <= i0:
        return []
    paso = max(1, int(win * rate))
    return [rms(sig[i:i + paso]) for i in range(i0, i1, paso)]


def _hablante_de_rango(loop16: np.ndarray, mic16: np.ndarray,
                       t0: float, t1: float, mic_ok: bool) -> str:
    """Decide quién habla en un segmento ya transcrito, comparando la actividad
    de cada stream en esa franja de tiempo. Reutiliza la misma lógica de ratios
    adaptativos que la diarización en vivo."""
    if not mic_ok:
        return ""
    return _decide_speaker(_rms_ventanas(loop16, t0, t1),
                           _rms_ventanas(mic16,  t0, t1), True)


def transcribir_grabacion(sesion, text_cb, status_cb, done_cb, inicio=None):
    """Transcribe la grabación COMPLETA que hay en disco.

    sesion                       — SesionGrabacion a transcribir
    text_cb(ts, hablante, texto) — se llama por cada frase reconocida
    done_cb(estado, mensaje)     — "ok" | "vacio" | "cancelado" | "error"
    inicio                       — datetime del arranque de la grabación
    """
    offline_cancel.clear()
    try:
        if sesion is None or not sesion.tiene_audio():
            done_cb("vacio", "No hay ninguna grabación en disco.")
            return
        if not asegurar_modelo(status_cb):
            done_cb("error", "No se pudo cargar el modelo Whisper.")
            return
        if offline_cancel.is_set():
            done_cb("cancelado", "Cancelado antes de empezar.")
            return

        status_cb("⚙️ Leyendo la grabación del disco…")
        # Se lee por bloques y ya remuestreado a 16 kHz: la llamada entera a tasa
        # nativa nunca llega a estar en memoria.
        loop16 = leer_pcm_a_16k(sesion.path_loop, sesion.rate_loop)
        mic16  = leer_pcm_a_16k(sesion.path_mic,  sesion.rate_mic)
        mic_ok = len(mic16) > 0

        n = max(len(loop16), len(mic16))
        if n == 0:
            done_cb("vacio", "La grabación está vacía.")
            return
        # Igualar longitudes para poder comparar ambos streams muestra a muestra
        if len(loop16) < n:
            loop16 = np.concatenate([loop16, np.zeros(n - len(loop16), dtype=np.float32)])
        if len(mic16) < n:
            mic16 = np.concatenate([mic16, np.zeros(n - len(mic16), dtype=np.float32)])

        # Whisper recibe AMBOS streams mezclados para no perder nada
        mezcla = np.clip(loop16 * 0.6 + mic16 * 0.6, -1, 1).astype(np.float32)
        total_seg = n / WHISPER_RATE
        status_cb(f"⚙️ Transcribiendo {total_seg/60:.1f} min de grabación…")

        segs, _info = whisper_model.transcribe(
            mezcla, language=LANGUAGE,
            beam_size=5, temperature=0.0,
            # VAD de Silero: se salta los silencios → mucho más rápido
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
            # Evita que Whisper se enganche repitiendo frases en audios largos
            condition_on_previous_text=False,
        )

        n_frases = 0
        for seg in segs:
            if offline_cancel.is_set():
                done_cb("cancelado", f"Cancelado. Se conservaron {n_frases} frases.")
                return
            texto = seg.text.strip()
            if not texto:
                continue
            hablante = _hablante_de_rango(loop16, mic16, seg.start, seg.end, mic_ok)
            if inicio:
                ts = (inicio + datetime.timedelta(seconds=seg.start)).strftime("%H:%M:%S")
            else:
                ts = time.strftime("%H:%M:%S", time.gmtime(seg.start))
            text_cb(ts, hablante, texto)
            n_frases += 1
            pct = min(100, int(seg.end / total_seg * 100)) if total_seg > 0 else 0
            status_cb(f"⚙️ Transcribiendo… {pct}%   ({n_frases} frases)")

        done_cb("ok", f"Transcripción terminada: {n_frases} frases.")
    except Exception as e:
        done_cb("error", str(e))


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
    MUTED = "#94a3b8"; AMBER = "#fbbf24"; BLUE = "#38bdf8"
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
        self._end_time     = None
        self._transcribiendo  = False   # transcripción diferida en curso
        self._cargando_modelo = False
        self.sesion           = None    # SesionGrabacion en curso o cargada
        self._hilo_captura    = None
        root.title("🐌 OpenTralla — Grabadora + Transcriptor v2.4")
        root.geometry("960x700")
        root.minsize(700, 540)
        root.configure(bg=self.BG)
        self._icon_ref = aplicar_icono(root)
        self._build_ui()
        # Carga perezosa: en modo grabadora Whisper NO se carga hasta que pulsas
        # «Transcribir». La app abre al instante y la RAM queda libre.
        if LIVE_TRANSCRIBE:
            self._cargar_modelo_async()
        else:
            self._status("✅ Listo para grabar — Whisper se carga al transcribir")
        self._estado_botones()
        # Red de seguridad: ¿quedó una grabación sin transcribir de la vez pasada?
        self.root.after(400, self._revisar_pendientes)

    def _build_ui(self):
        C, F, FB = self, ("Segoe UI",10), ("Segoe UI",11,"bold")

        # Encabezado
        hdr = tk.Frame(self.root, bg=C.ACCENT, pady=11)
        hdr.pack(side="top", fill="x")
        tk.Label(hdr, text="🐌  OpenTralla v2.4",
                 font=("Segoe UI",15,"bold"), fg=C.FG, bg=C.ACCENT).pack(side="left", padx=16)
        tk.Label(hdr, text="Graba primero, transcribe después  |  Dual-Channel  |  100% local",
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
        self.btn_trans = tk.Button(bf, text="📝  Transcribir", bg=C.BLUE, fg="#0d1117",
                                   command=self.transcribir_offline, state="disabled", **kw)
        self.btn_trans.pack(side="left", padx=4)
        tk.Button(bf, text="🤖  Analizar con IA", bg="#7c3aed", fg=C.FG,
                  command=self.abrir_ia, **kw).pack(side="left", padx=4)
        tk.Button(bf, text="💾  Guardar", bg=C.ACCENT, fg=C.FG,
                  command=self.guardar, **kw).pack(side="left", padx=4)
        tk.Button(bf, text="🗑️  Limpiar", bg="#334155", fg=C.FG,
                  command=self.limpiar, **kw).pack(side="left", padx=4)
        tk.Button(bf, text="📂  Grabaciones", bg="#334155", fg=C.FG,
                  command=self.abrir_carpeta, **kw).pack(side="left", padx=4)
        self.var_rec = tk.StringVar(value="")
        tk.Label(bf, textvariable=self.var_rec,
                 font=("Segoe UI",13,"bold"), fg=C.RED, bg=C.PANEL).pack(side="right", padx=14)

        # Fila de opciones (siempre visible)
        opt_row = tk.Frame(self.root, bg="#0f1b30", pady=5)
        opt_row.pack(side="bottom", fill="x")

        # ── Interruptor de transcripción en vivo (APAGADO por defecto) ──
        self.var_live = tk.BooleanVar(value=LIVE_TRANSCRIBE)
        self.chk_live = tk.Checkbutton(
            opt_row, text="  ⚡  Transcribir en vivo",
            variable=self.var_live,
            font=("Segoe UI",9,"bold"), fg=C.BLUE, bg="#0f1b30",
            activeforeground=C.BLUE, activebackground="#0f1b30",
            selectcolor="#1e293b", cursor="hand2",
            command=self._on_live_toggle,
        )
        self.chk_live.pack(side="left", padx=(14, 2))
        self.lbl_live_info = tk.Label(
            opt_row,
            text=("⚠️ usa CPU durante la llamada" if LIVE_TRANSCRIBE
                  else "CPU libre — transcribes al final"),
            font=("Segoe UI",8),
            fg=(C.RED if LIVE_TRANSCRIBE else "#22c55e"), bg="#0f1b30")
        self.lbl_live_info.pack(side="left", padx=(0, 4))

        self.var_screen = tk.BooleanVar(value=False)
        self.chk_screen = tk.Checkbutton(
            opt_row, text="  🎥  Grabar pantalla",
            variable=self.var_screen,
            font=("Segoe UI",9,"bold"), fg=C.AMBER, bg="#0f1b30",
            activeforeground=C.AMBER, activebackground="#0f1b30",
            selectcolor="#1e293b", cursor="hand2",
            command=self._on_screen_toggle,
        )
        self.chk_screen.pack(side="left", padx=(6, 5))

        self.var_mic = tk.BooleanVar(value=USAR_MIC)
        self.chk_mic = tk.Checkbutton(
            opt_row, text="  🎤  Mi voz (mic)",
            variable=self.var_mic,
            font=("Segoe UI",9,"bold"), fg=C.AMBER, bg="#0f1b30",
            activeforeground=C.AMBER, activebackground="#0f1b30",
            selectcolor="#1e293b", cursor="hand2",
            command=self._on_mic_toggle,
        )
        self.chk_mic.pack(side="left", padx=5)
        
        self.lbl_screen_status = tk.Label(
            opt_row, text="",
            font=("Segoe UI",8), fg=C.MUTED, bg="#0f1b30")
        self.lbl_screen_status.pack(side="left", padx=2)

        # Selector de modelo Whisper
        tk.Label(opt_row, text="  |   ⚙️ Modelo:", font=("Segoe UI",9,"bold"), fg=C.FG, bg="#0f1b30").pack(side="left", padx=(8,2))
        self.var_whisper_size = tk.StringVar(value=MODEL_SIZE)
        self.cb_whisper_size = ttk.Combobox(
            opt_row, textvariable=self.var_whisper_size,
            values=["tiny", "base", "small", "medium"], state="readonly", width=7
        )
        self.cb_whisper_size.pack(side="left", padx=2)
        self.cb_whisper_size.bind("<<ComboboxSelected>>", self._on_whisper_model_change)

        # Selector de hilos de CPU
        tk.Label(opt_row, text="  |   🧵 Hilos CPU:", font=("Segoe UI",9,"bold"), fg=C.FG, bg="#0f1b30").pack(side="left", padx=(8,2))
        self.var_whisper_threads = tk.StringVar(value=str(WHISPER_THREADS))
        self.cb_whisper_threads = ttk.Combobox(
            opt_row, textvariable=self.var_whisper_threads,
            values=["1", "2", "3", "4", "6", "8"], state="readonly", width=3
        )
        self.cb_whisper_threads.pack(side="left", padx=2)
        self.cb_whisper_threads.bind("<<ComboboxSelected>>", self._on_whisper_threads_change)

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
        device, _ = get_whisper_device()
        device_info = "GPU CUDA" if device == "cuda" else f"CPU ({WHISPER_THREADS} hilos)"
        self.lbl_motor_info = tk.Label(
            vu_frame,
            text=f"Motor: faster-whisper {MODEL_SIZE} ({device_info})  |  [Tú]=azul  [Ellos]=verde  |  🐌 OpenTralla v2.4",
            font=("Segoe UI",8), fg=C.MUTED, bg="#0f1b30"
        )
        self.lbl_motor_info.pack(pady=(2,0))

        # Leyenda de colores
        leg = tk.Frame(self.root, bg=C.BG, pady=3)
        leg.pack(side="bottom", fill="x")
        tk.Label(leg, text="●", fg=C.COLOR_TU, bg=C.BG, font=("Segoe UI",10,"bold")).pack(side="left", padx=(16,2))
        tk.Label(leg, text="Tú (micrófono)", fg=C.MUTED, bg=C.BG, font=("Segoe UI",9)).pack(side="left")
        tk.Label(leg, text="   ●", fg=C.COLOR_ELLOS, bg=C.BG, font=("Segoe UI",10,"bold")).pack(side="left", padx=(8,2))
        tk.Label(leg, text="Ellos (llamada/sistema)", fg=C.MUTED, bg=C.BG, font=("Segoe UI",9)).pack(side="left")
        tk.Label(leg, text="   💾 El audio se escribe a disco en grabaciones/ mientras hablas", fg="#22c55e", bg=C.BG, font=("Segoe UI",9)).pack(side="left", padx=8)

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
    def _autosave(self, force=False):
        """Guarda automáticamente la transcripción cada N segundos.
        Si la app se cierra inesperadamente, no se pierde nada."""
        if not self.grabando and not force:
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
            fin = self._end_time or datetime.datetime.now()
            delta = fin - self._start_time
            horas, rem = divmod(int(delta.total_seconds()), 3600)
            mins, secs = divmod(rem, 60)
            duracion = f"{horas:02d}:{mins:02d}:{secs:02d}"
        
        header = (
            "════════════════════════════════════════════════════════════════\n"
            "  OpenTralla v2.4 — Transcripción de Llamada\n"
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
            fin = self._end_time or datetime.datetime.now()
            delta = fin - self._start_time
            horas, rem = divmod(int(delta.total_seconds()), 3600)
            mins, secs = divmod(rem, 60)
            duracion = f"{horas:02d}:{mins:02d}:{secs:02d}"
        
        return {
            "version": "2.4",
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
        if self.grabando or self._transcribiendo: return
        if LIVE_TRANSCRIBE and not modelo_vigente():
            messagebox.showwarning("Espera",
                "El modelo aún se está cargando.\n\n"
                "Si tienes prisa, desactiva «⚡ Transcribir en vivo» y graba ya mismo.")
            return
        if self.sesion is not None and self.sesion.tiene_audio() and not self.transcripcion:
            if not messagebox.askyesno("Grabación sin transcribir",
                "La grabación anterior todavía no está transcrita.\n\n"
                f"No se borra, queda guardada en:\n{self.sesion.carpeta}\n\n"
                "¿Empezar una grabación nueva?"):
                return

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
        self._end_time   = None
        stop_event.clear()
        offline_cancel.clear()
        # Nueva sesión en disco: aquí se va escribiendo el audio mientras hablas
        self.sesion = SesionGrabacion.nueva()
        self._start_time = self.sesion.inicio
        while not phrase_queue.empty():
            try: phrase_queue.get_nowait()
            except: break
        self._estado_botones()
        self._parpadear()
        usar_mic = self.var_mic.get()

        if LIVE_TRANSCRIBE:
            # Auto-guardado del texto (solo tiene sentido si hay texto en vivo)
            self._autosave_id = self.root.after(AUTOSAVE_INTERVAL * 1000, self._autosave)
            self.var_autosave.set("🛡️ Auto-guardado activo")
            threading.Thread(target=hilo_transcripcion,
                             args=(self._agregar, self._status), daemon=True).start()
        else:
            # Modo grabadora: saca Whisper de la RAM para no estorbar la llamada
            if model_ready.is_set():
                liberar_modelo()
            self.var_autosave.set("🎙️ Grabando audio (sin transcribir)")

        self._hilo_captura = threading.Thread(
            target=hilo_captura,
            args=(self._status, self._update_vu, usar_mic, LIVE_TRANSCRIBE, self.sesion),
            daemon=True)
        self._hilo_captura.start()

    def detener(self):
        if not self.grabando: return
        stop_event.set()
        self.grabando = False
        self._end_time = datetime.datetime.now()
        # Esperar a que el hilo de captura vacíe sus buffers y cierre los archivos
        if self._hilo_captura is not None:
            self._hilo_captura.join(timeout=3.0)
            self._hilo_captura = None
        if self.sesion is not None:
            self.sesion.cerrar()   # idempotente, por si el hilo no llegó a hacerlo
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
        # Auto-guardado final al detener (solo si hubo transcripción en vivo)
        if self.transcripcion:
            self._autosave(force=True)
            self.var_autosave.set(f"🛡️ Último guardado: {datetime.datetime.now().strftime('%H:%M:%S')}")
        self.var_rec.set("")
        self._draw_vu(0.0)
        self._estado_botones()
        if self.sesion is not None:
            mins = self.sesion.duracion_seg() / 60
            self.var_autosave.set(
                f"💾 En disco: {os.path.basename(self.sesion.carpeta)} "
                f"({self.sesion.tamano_mb():.0f} MB)")
            if not LIVE_TRANSCRIBE:
                self._status(f"✅ Grabación lista ({mins:.1f} min) — "
                             "pulsa «📝 Transcribir» cuando quieras")
            self._avisar_calidad()

    def _avisar_calidad(self):
        """Avisa al colgar si la grabación salió comprometida.

        Vale mucho más enterarse ahora que descubrirlo media hora después,
        o peor, que te lo diga la persona con la que hablabas.
        """
        s = self.sesion
        if s is None:
            return
        problemas = []

        ratio = s.mic_silenciado()
        if ratio > MIC_MUDO_RATIO:
            problemas.append(
                f"🎤 Tu micrófono entregó {int(ratio*100)}% de silencio digital.\n\n"
                "Un micrófono real nunca da silencio absoluto: algo lo está\n"
                "recortando por software. Casi siempre es la cancelación de\n"
                "ruido con IA del portátil.\n\n"
                "En MyASUS → Audio y visual → «Micrófono con cancelación de\n"
                "ruido con IA», elige «Grabación auténtica».\n\n"
                "OJO: con eso activo tu voz sale entrecortada TAMBIÉN para la\n"
                "persona con la que hablas, no solo en la grabación.")

        esperado = 0.0
        if self._start_time and self._end_time:
            esperado = (self._end_time - self._start_time).total_seconds()
        real = s.duracion_seg()
        if esperado > 60 and real < esperado * 0.9:
            problemas.append(
                f"⏱️ Se perdió grabación: {real/60:.1f} min de los "
                f"{esperado/60:.1f} min que estuvo grabando.\n\n"
                "El sistema dejó de entregar audio, normalmente porque se quedó\n"
                "sin memoria o sin CPU. Cierra pestañas de Chrome antes de la\n"
                "próxima llamada.")

        if problemas:
            messagebox.showwarning("Revisa esta grabación", "\n\n────────\n\n".join(problemas))

    def _cargar_modelo_async(self):
        self._cargando_modelo = True
        self._estado_botones()
        
        def on_status(msg):
            self._status(msg)
            # Volver a habilitar el botón cuando el modelo termine de cargar (ya sea con éxito o error)
            if "✅" in msg or "❌" in msg:
                device, _ = get_whisper_device()
                device_info = "GPU CUDA" if device == "cuda" else f"CPU ({WHISPER_THREADS} hilos)"
                motor_info = f"Motor: faster-whisper {MODEL_SIZE} ({device_info})  |  [Tú]=azul  [Ellos]=verde  |  🐌 OpenTralla v2.4"
                self.root.after(0, lambda info=motor_info: self.lbl_motor_info.config(text=info))
                self._cargando_modelo = False
                self.root.after(0, self._estado_botones)
                
        threading.Thread(target=cargar_modelo, args=(on_status,), daemon=True).start()

    def _on_whisper_model_change(self, _=None):
        global MODEL_SIZE
        if self.grabando:
            messagebox.showwarning("Grabación activa", "No puedes cambiar el modelo durante la grabación.")
            self.var_whisper_size.set(MODEL_SIZE)
            return
        
        new_size = self.var_whisper_size.get()
        if new_size == MODEL_SIZE:
            return
            
        MODEL_SIZE = new_size
        cfg = load_config()
        cfg["whisper_model_size"] = MODEL_SIZE
        save_config(cfg)
        
        # Carga perezosa: solo recargamos ya si la transcripción en vivo está activa
        liberar_modelo()
        if LIVE_TRANSCRIBE:
            self._cargar_modelo_async()
        else:
            self._status(f"⚙️ Modelo '{MODEL_SIZE}' se cargará al pulsar «Transcribir»")

    def _on_live_toggle(self):
        """Enciende/apaga la transcripción en tiempo real durante la llamada."""
        global LIVE_TRANSCRIBE
        if self.grabando or self._transcribiendo:
            messagebox.showwarning("Ocupado",
                "No puedes cambiar el modo mientras graba o transcribe.")
            self.var_live.set(LIVE_TRANSCRIBE)
            return
        LIVE_TRANSCRIBE = self.var_live.get()
        cfg = load_config()
        cfg["transcripcion_vivo"] = LIVE_TRANSCRIBE
        save_config(cfg)

        if LIVE_TRANSCRIBE:
            self.lbl_live_info.config(text="⚠️ usa CPU durante la llamada", fg=self.RED)
            messagebox.showwarning("Transcripción en vivo",
                "Whisper correrá DURANTE la llamada.\n\n"
                "En equipos justos eso consume CPU y puede degradar el audio de "
                "Zoom/Teams (voz robotizada, cortes).\n\n"
                "Si notas problemas, desactívalo y usa «📝 Transcribir» al colgar: "
                "sale igual de bien o mejor, porque Whisper ve la llamada entera.")
            if not modelo_vigente():
                self._cargar_modelo_async()
        else:
            self.lbl_live_info.config(text="CPU libre — transcribes al final", fg="#22c55e")
            liberar_modelo()   # devuelve la RAM del modelo
            self._status("✅ Modo grabadora — Whisper se carga al transcribir")
        self._estado_botones()

    def _on_mic_toggle(self):
        global USAR_MIC
        USAR_MIC = self.var_mic.get()
        cfg = load_config()
        cfg["usar_mic"] = USAR_MIC
        save_config(cfg)

    def _on_whisper_threads_change(self, _=None):
        global WHISPER_THREADS
        if self.grabando:
            messagebox.showwarning("Grabación activa", "No puedes cambiar los hilos durante la grabación.")
            self.var_whisper_threads.set(str(WHISPER_THREADS))
            return
        
        new_threads = int(self.var_whisper_threads.get())
        if new_threads == WHISPER_THREADS:
            return
            
        WHISPER_THREADS = new_threads
        cfg = load_config()
        cfg["whisper_cpu_threads"] = WHISPER_THREADS
        save_config(cfg)
        
        # Carga perezosa: solo recargamos ya si la transcripción en vivo está activa
        liberar_modelo()
        if LIVE_TRANSCRIBE:
            self._cargar_modelo_async()
        else:
            self._status(f"⚙️ Modelo '{MODEL_SIZE}' se cargará al pulsar «Transcribir»")

    # ──────────────────────────────────────────────────────────────────────────
    # Transcripción diferida (el flujo por defecto)
    # ──────────────────────────────────────────────────────────────────────────
    def _estado_botones(self):
        """Único sitio que decide qué botón está activo en cada momento."""
        hay_audio = bool(self.sesion is not None and self.sesion.tiene_audio())
        if self._transcribiendo:
            self.btn_ini.config(state="disabled")
            self.btn_det.config(state="disabled")
            self.btn_trans.config(state="normal", text="⏹  Cancelar", bg=self.RED)
        elif self.grabando:
            self.btn_ini.config(state="disabled")
            self.btn_det.config(state="normal")
            self.btn_trans.config(state="disabled", text="📝  Transcribir", bg=self.BLUE)
        else:
            self.btn_ini.config(state="disabled" if self._cargando_modelo else "normal")
            self.btn_det.config(state="disabled")
            self.btn_trans.config(state="normal" if hay_audio else "disabled",
                                  text="📝  Transcribir", bg=self.BLUE)

    def _revisar_pendientes(self):
        """Al abrir la app: ofrecer la última grabación que quedó sin transcribir.
        Es lo que te salva si la app se cerró sola o se fue la luz."""
        # Nunca pisar una grabación en curso o ya cargada
        if self.grabando or self._transcribiendo or self.sesion is not None:
            return
        try:
            pendientes = sesiones_pendientes()
        except Exception:
            return
        if not pendientes:
            return
        s = pendientes[0]
        mins = s.duracion_seg() / 60
        cuando = s.inicio.strftime("%d/%m/%Y %H:%M") if s.inicio else "fecha desconocida"
        extra = (f"\n\n(Hay {len(pendientes)} grabaciones sin transcribir en total; "
                 "las demás siguen en la carpeta.)") if len(pendientes) > 1 else ""
        if messagebox.askyesno("Grabación sin transcribir",
            f"Encontré una grabación guardada que no llegaste a transcribir:\n\n"
            f"    {cuando}  —  {mins:.1f} min  ({s.tamano_mb():.0f} MB)\n"
            f"    {s.carpeta}\n\n"
            f"¿La cargo para poder transcribirla ahora?{extra}"):
            self.sesion = s
            self._start_time = s.inicio
            if s.inicio:
                self._end_time = s.inicio + datetime.timedelta(seconds=s.duracion_seg())
            self._estado_botones()
            self._status(f"📂 Grabación cargada ({mins:.1f} min) — pulsa «📝 Transcribir»")
            self.var_autosave.set(f"💾 {os.path.basename(s.carpeta)}")

    def abrir_carpeta(self):
        """Abre en el Explorador la carpeta donde se guardan las grabaciones."""
        destino = GRAB_DIR
        if self.sesion is not None and os.path.isdir(self.sesion.carpeta):
            destino = self.sesion.carpeta
        try:
            os.makedirs(destino, exist_ok=True)
            os.startfile(destino)
        except Exception as e:
            messagebox.showwarning("No se pudo abrir la carpeta", f"{destino}\n\n{e}")

    def transcribir_offline(self):
        """Transcribe la grabación ya terminada. Segunda pulsación = cancelar."""
        if self._transcribiendo:
            offline_cancel.set()
            self._status("⏹️ Cancelando transcripción…")
            return
        if self.grabando:
            messagebox.showwarning("Grabación activa",
                "Pulsa «⏹ Detener» antes de transcribir.")
            return
        if self.sesion is None or not self.sesion.tiene_audio():
            messagebox.showinfo("Sin grabación",
                "No hay audio grabado.\n\n"
                "Pulsa «▶ Iniciar», haz la llamada, detén y vuelve aquí.")
            return
        if self.transcripcion:
            if not messagebox.askyesno("Ya hay texto",
                "La ventana ya tiene una transcripción.\n\n"
                "¿Borrarla y transcribir la grabación desde cero?"):
                return
            self.limpiar()

        self._transcribiendo = True
        self._estado_botones()
        threading.Thread(
            target=transcribir_grabacion,
            args=(self.sesion, self._agregar, self._status, self._offline_done,
                  self._start_time),
            daemon=True).start()

    def _offline_done(self, estado: str, mensaje: str):
        """Cierre de la transcripción diferida (llega desde el hilo de trabajo)."""
        def _fin():
            self._transcribiendo = False
            self._estado_botones()
            if estado == "ok":
                self._status(f"✅ {mensaje}   —   pulsa «💾 Guardar»")
                self._autosave(force=True)
                if self.sesion is not None:
                    self.sesion.marcar_listo()
            elif estado == "cancelado":
                self._status(f"⏹️ {mensaje}")
            elif estado == "vacio":
                self._status("⚠️ La grabación está vacía")
                messagebox.showwarning("Sin audio", mensaje)
            else:
                self._status(f"❌ {mensaje}")
                messagebox.showerror("Error transcribiendo", mensaje)
        self.root.after(0, _fin)

    def abrir_ia(self):
        transcript = "".join(self.transcripcion)
        if not transcript.strip():
            messagebox.showinfo("Sin transcripción",
                                "No hay texto transcrito aún.\n\nInicia y detén la grabación primero.")
            return
        AIDialog(self.root, transcript)

    def guardar(self):
        hay_audio = bool(self.sesion is not None and self.sesion.tiene_audio())
        if not self.transcripcion and not hay_audio:
            messagebox.showinfo("Sin contenido",
                "No hay nada que guardar todavía.\n\nGraba una llamada primero.")
            return
        solo_audio = not self.transcripcion
        if solo_audio:
            if not messagebox.askyesno("Sin transcripción",
                "Esta grabación todavía no está transcrita.\n\n"
                "¿Guardar solo el audio (.wav)?\n\n"
                "También puedes cancelar, pulsar «📝 Transcribir» y guardar después."):
                return
        ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = filedialog.asksaveasfilename(
            defaultextension=".wav" if solo_audio else ".txt",
            filetypes=([("Audio WAV","*.wav"),("Todos","*.*")] if solo_audio
                       else [("Texto","*.txt"),("Todos","*.*")]),
            initialfile=f"opentralla_{ts}." + ("wav" if solo_audio else "txt"))
        if not path: return
        
        base = os.path.splitext(path)[0]

        # 1. Transcripción formateada (.txt) — solo si ya hay texto
        ok_txt    = False
        ok_json   = False
        json_path = base + ".json"
        if self.transcripcion:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self._generar_transcripcion_formateada())
            ok_txt = True

            # 1b. Transcripción estructurada (.json)
            try:
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump(self._generar_json_estructurado(), f, indent=2, ensure_ascii=False)
                ok_json = True
            except Exception as e:
                print(f"Error guardando JSON estructurado: {e}")

        # 2. Audio completo de la llamada (.wav) — mezcla de ambos canales
        wav_path = base + ".wav"
        ok_wav = escribir_wav_mezcla(self.sesion, wav_path) if self.sesion else False

        # 3. Video (.mp4) — SOLO si la grabación de pantalla estaba activa
        ok_mp4, mp4_path = False, ""
        if self._screen_rec and self._screen_rec.video_path and os.path.exists(self._screen_rec.video_path):
            mp4_path = base + ".mp4"
            if os.path.exists(wav_path):
                ok_mp4 = self._muxear_video(self._screen_rec.video_path, wav_path, mp4_path)

        msg = (f"📄 Transcripción:\n{path}" if ok_txt
               else "📄 Sin transcripción todavía (solo se guardó el audio).\n"
                    "Pulsa «📝 Transcribir» y vuelve a guardar.")
        if ok_json:
            msg += f"\n\n📊 JSON estructurado:\n{json_path}"
        if ok_wav:
            msg += f"\n\n🔊 Audio de la llamada:\n{wav_path}"
        else:
            msg += "\n\n⚠️ No se pudo guardar el audio (¿grabación vacía?)"
        if ok_mp4:
            msg += f"\n\n🎬 Video con pantalla:\n{mp4_path}"
        elif self._screen_rec:
            msg += "\n\n⚠️ No se pudo generar el video (verifica que ffmpeg.exe esté en la carpeta)"
        messagebox.showinfo("Guardado", msg)

        # El audio ya está a salvo en el .wav: ofrecer liberar los temporales
        if ok_wav and self.sesion is not None:
            self.sesion.marcar_listo()
            mb = self.sesion.tamano_mb()
            if mb >= 1 and messagebox.askyesno("Liberar espacio",
                f"El audio ya quedó guardado en:\n{wav_path}\n\n"
                f"¿Borro los archivos de trabajo de la grabación ({mb:.0f} MB)?\n"
                f"{self.sesion.carpeta}\n\n"
                "(Si dices que no, se quedan por si quieres volver a transcribirla.)"):
                if self.sesion.borrar():
                    self.sesion = None
                    self._estado_botones()

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
        if self._transcribiendo:
            if not messagebox.askyesno("Transcripción en curso",
                "Se está transcribiendo la grabación.\n\n¿Cerrar de todas formas?"):
                return
            offline_cancel.set()
        stop_event.set()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app  = TranscriptorApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
