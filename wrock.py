#!/usr/bin/env python3
"""
WROCK: Ultra-Fast Voice AI & Full PC Automation Assistant (Grok & Claude Powered)
Featuring Holographic Floating HUD Overlay, Multi-lingual Speech (Odia, Bengali, Hindi, English),
Double Clap Activation, Siri/Grok Wake-Word ("Wake up Wrock" / "Wrock"), SQLite Chat Logging,
and Full Windows PC Automation (Screenshots, Volume, System Status, App Controls).
"""

from __future__ import annotations

import hashlib
import logging
import os
import queue
import math
import shutil
import subprocess
import sys
import sqlite3
import tempfile
import threading
import time
import wave
import webbrowser
from pathlib import Path

from dotenv import load_dotenv
import numpy as np
import sounddevice as sd

# Try importing GUI and automation modules
try:
    import tkinter as tk
except ImportError:
    tk = None

try:
    import pyautogui
except ImportError:
    pyautogui = None

try:
    import psutil
except ImportError:
    psutil = None

# --- Tuning knobs & constants --------------------------------------------------
SAMPLE_RATE = 44100
BLOCK_MS = 40
CHANNELS = 1

SPIKE_RATIO = 3.5
COOLDOWN_S = 0.45
MIN_DOUBLE_GAP_S = 0.05
MAX_DOUBLE_GAP_S = 0.65
RETRIGGER_RATIO = 0.55
NOISE_FLOOR_ALPHA = 0.992
MIN_RMS = 0.004
QUIET_GATE_MULT = 2.2

INPUT_PROBE_S = 0.5
INPUT_SILENT_RMS = 0.0001

# Spotify / Music: Disabled auto-play on double clap unless requested
SONG_URI = ""

# Cursor window management
FOCUS_EXISTING_CURSOR_ON_DOUBLE_CLAP = True
OPEN_NEW_CURSOR_ON_DOUBLE_CLAP = False
CURSOR_OPEN_FULLSCREEN = False

# Google Chrome
OPEN_CLAUDE_CODE_IN_CHROME = False
OPEN_YOUTUBE_IN_CHROME = False
OPEN_CHROME_FULLSCREEN = False
CHROME_SEPARATE_SITE_PROFILES = False
CLAUDE_CHROME_MONITOR = 1
YOUTUBE_CHROME_MONITOR = 2

# Voice Wake Words (Siri / Grok Mode)
WAKE_WORDS = ["wake up wrock", "wake up, wrock", "wake up", "wrock", "hey wrock", "ok wrock"]

WROCK_WELCOME_ENABLED = True
WROCK_WELCOME_PHRASE = "Wrock online. Standing by for your command, King."
WROCK_AFTER_SONG_DELAY_S = 0.5
WROCK_WELCOME_CACHE_ENABLED = True

load_dotenv(Path(__file__).resolve().parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("wrock")


# ==============================================================================
# 1. HOLOGRAPHIC FLOATING HUD OVERLAY WIDGET (Tkinter)
# ==============================================================================

class WrockHUDWidget:
    """
    Floating, transparent, non-intrusive holographic bottom-center HUD widget.
    Displays glowing orange energy orb (inspired by uploaded design), active status,
    transcribed user command, and assistant response stream.
    """
    def __init__(self) -> None:
        self.root: tk.Tk | None = None
        self.canvas: tk.Canvas | None = None
        self.state = "IDLE"  # IDLE, LISTENING, THINKING, SPEAKING
        self.status_text = "READY FOR KING"
        self.user_text = ""
        self.assistant_text = ""
        self.msg_queue: queue.Queue = queue.Queue()
        self.anim_step = 0
        self._drag_data = {"x": 0, "y": 0}
        self.running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if tk is None:
            log.warning("Tkinter not available; HUD widget disabled.")
            return
        self._thread = threading.Thread(target=self._run_gui, daemon=True)
        self._thread.start()

    def _run_gui(self) -> None:
        try:
            self.root = tk.Tk()
            self.root.title("Wrock HUD Assistant")
            self.root.overrideredirect(True)  # Frameless
            self.root.attributes("-topmost", True)  # Always on top
            
            # Transparent background setup for Windows / Linux
            bg_color = "#090a0f"
            self.root.config(bg=bg_color)
            if sys.platform == "win32":
                self.root.attributes("-transparentcolor", bg_color)
            elif sys.platform == "darwin":
                self.root.attributes("-transparent", True)

            # Dimensions & Bottom-Center Screen Positioning
            w, h = 480, 140
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            x = (sw - w) // 2
            y = sh - h - 55  # 55px above taskbar
            self.root.geometry(f"{w}x{h}+{x}+{y}")

            # Canvas for Holographic Drawing
            self.canvas = tk.Canvas(
                self.root,
                width=w,
                height=h,
                bg=bg_color,
                highlightthickness=0,
            )
            self.canvas.pack(fill="both", expand=True)

            # Make window draggable
            self.canvas.bind("<ButtonPress-1>", self._on_drag_start)
            self.canvas.bind("<B1-Motion>", self._on_drag_motion)

            self.running = True
            self._update_queue_loop()
            self._animate_loop()
            self.root.mainloop()
        except Exception as e:
            log.warning("HUD GUI exception: %s", e)

    def _on_drag_start(self, event: tk.Event) -> None:
        self._drag_data["x"] = event.x
        self._drag_data["y"] = event.y

    def _on_drag_motion(self, event: tk.Event) -> None:
        if not self.root:
            return
        deltax = event.x - self._drag_data["x"]
        deltay = event.y - self._drag_data["y"]
        x = self.root.winfo_x() + deltax
        y = self.root.winfo_y() + deltay
        self.root.geometry(f"+{x}+{y}")

    def update_state(self, state: str, status_text: str = "", user_text: str = "", assistant_text: str = "") -> None:
        self.msg_queue.put({
            "state": state,
            "status_text": status_text,
            "user_text": user_text,
            "assistant_text": assistant_text,
        })

    def _update_queue_loop(self) -> None:
        if not self.running or not self.root:
            return
        try:
            while not self.msg_queue.empty():
                msg = self.msg_queue.get_nowait()
                if msg.get("state"):
                    self.state = msg["state"]
                if msg.get("status_text") is not None and msg["status_text"] != "":
                    self.status_text = msg["status_text"]
                if msg.get("user_text") is not None and msg["user_text"] != "":
                    self.user_text = msg["user_text"]
                if msg.get("assistant_text") is not None and msg["assistant_text"] != "":
                    self.assistant_text = msg["assistant_text"]
        except Exception:
            pass
        self.root.after(50, self._update_queue_loop)

    def _animate_loop(self) -> None:
        if not self.running or not self.root or not self.canvas:
            return
        try:
            self.anim_step += 1
            self.canvas.delete("all")
            w, h = 480, 140

            # 1. Dark Futuristic Translucent Glass HUD Frame
            self.canvas.create_rectangle(
                5, 5, w - 5, h - 5,
                fill="#0d111a",
                outline="#ff6600",
                width=2,
            )
            # Corner glowing accents
            self.canvas.create_line(5, 20, 20, 5, fill="#00e5ff", width=2)
            self.canvas.create_line(w - 20, 5, w - 5, 20, fill="#00e5ff", width=2)

            # 2. Glowing Orange Holographic Energy Orb (Left Side: cx=60, cy=70)
            cx, cy = 65, 70
            pulse = math.sin(self.anim_step * 0.15) * 4

            # State Colors
            if self.state == "LISTENING":
                ring_color = "#00ffcc"
                core_color = "#00e5ff"
                state_badge = "[ LISTENING... ]"
            elif self.state == "THINKING":
                ring_color = "#ffaa00"
                core_color = "#ffffff"
                state_badge = "[ THINKING (GROK/CLAUDE)... ]"
            elif self.state == "SPEAKING":
                ring_color = "#ff3366"
                core_color = "#ff6600"
                state_badge = "[ SPEAKING... ]"
            else:  # IDLE
                ring_color = "#ff6600"
                core_color = "#ffaa00"
                state_badge = "[ IDLE - STANDBY ]"

            # Draw outer energy rings
            r_outer = 38 + pulse
            self.canvas.create_oval(
                cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer,
                outline=ring_color, width=2
            )
            r_mid = 26 + pulse * 0.5
            self.canvas.create_oval(
                cx - r_mid, cy - r_mid, cx + r_mid, cy + r_mid,
                outline="#ffaa00", width=1.5
            )
            # Inner bright glowing core
            r_core = 14 + pulse * 0.3
            self.canvas.create_oval(
                cx - r_core, cy - r_core, cx + r_core, cy + r_core,
                fill=core_color, outline="#ffffff"
            )

            # Orbiting Particle Dots
            angle = (self.anim_step * 0.1) % (2 * math.pi)
            px = cx + math.cos(angle) * (r_outer + 5)
            py = cy + math.sin(angle) * (r_outer + 5)
            self.canvas.create_oval(px - 3, py - 3, px + 3, py + 3, fill="#00e5ff", outline="")

            px2 = cx + math.cos(angle + math.pi) * (r_outer + 5)
            py2 = cy + math.sin(angle + math.pi) * (r_outer + 5)
            self.canvas.create_oval(px2 - 3, py2 - 3, px2 + 3, py2 + 3, fill="#ff9900", outline="")

            # 3. Waveform Audio Equalizer Bars (Center-Right x=120..180)
            if self.state in ("LISTENING", "SPEAKING"):
                for i in range(8):
                    bx = 125 + i * 7
                    bar_h = math.sin(self.anim_step * 0.3 + i) * 12 + 15
                    self.canvas.create_line(bx, cy + bar_h/2, bx, cy - bar_h/2, fill=ring_color, width=3)

            # 4. HUD Header Title & State
            self.canvas.create_text(
                195, 22,
                text="⚡ W.R.O.C.K. HUD ✦ GROK ONLINE",
                fill="#00e5ff",
                font=("Consolas", 10, "bold"),
                anchor="w",
            )
            self.canvas.create_text(
                195, 42,
                text=state_badge,
                fill=ring_color,
                font=("Consolas", 9, "bold"),
                anchor="w",
            )

            # 5. User Command Box & Assistant Response Text
            disp_user = self.user_text if self.user_text else (self.status_text or "Listening for 'Wake up Wrock'...")
            if len(disp_user) > 38:
                disp_user = disp_user[:35] + "..."

            self.canvas.create_text(
                195, 68,
                text=f"User: {disp_user}",
                fill="#e0e0e0",
                font=("Segoe UI", 9),
                anchor="w",
            )

            disp_resp = self.assistant_text if self.assistant_text else "Standing by for King's commands."
            if len(disp_resp) > 38:
                disp_resp = disp_resp[:35] + "..."

            self.canvas.create_text(
                195, 92,
                text=f"Wrock: {disp_resp}",
                fill="#ffaa00",
                font=("Segoe UI", 9, "italic"),
                anchor="w",
            )

            # Close button [x] top right
            self.canvas.create_text(
                w - 18, 18,
                text="✕",
                fill="#ff4444",
                font=("Consolas", 11, "bold"),
            )

        except Exception as e:
            log.debug("HUD render error: %s", e)

        self.root.after(40, self._animate_loop)


# Global HUD instance
hud_widget = WrockHUDWidget()


# ==============================================================================
# 2. AUDIO & TTS ENGINE
# ==============================================================================

def block_samples() -> int:
    n = int(SAMPLE_RATE * BLOCK_MS / 1000)
    return max(n, 1)


def rms_mono(block: np.ndarray) -> float:
    if block.ndim > 1:
        block = np.mean(block.astype(np.float64), axis=1)
    else:
        block = block.astype(np.float64)
    if block.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(block**2)))


def _input_devices() -> list[tuple[int, dict]]:
    return [
        (i, dev)
        for i, dev in enumerate(sd.query_devices())
        if dev["max_input_channels"] >= 1
    ]


def _choose_input_device(blocksize: int) -> int:
    default = sd.default.device[0]
    if default is not None and default >= 0:
        return default
    inputs = _input_devices()
    return inputs[0][0] if inputs else 0


def elevenlabs_env_config() -> tuple[str, str, str, int]:
    voice = (os.environ.get("ELEVENLABS_VOICE_ID") or "").strip()
    model = (os.environ.get("ELEVENLABS_MODEL_ID") or "eleven_multilingual_v2").strip()
    fmt = (os.environ.get("ELEVENLABS_OUTPUT_FORMAT") or "pcm_24000").strip()
    rate = 24000
    return voice, model, fmt, rate


def play_activation_chime() -> None:
    """Siri / Grok-style dual ascending activation chime."""
    if sys.platform == "win32":
        try:
            import winsound
            winsound.Beep(880, 100)   # Tone 1 (A5)
            winsound.Beep(1320, 140)  # Tone 2 (E6)
        except Exception:
            pass


def speak_text(text: str) -> None:
    """Speak text via ElevenLabs or offline pyttsx3 voice engine."""
    if not text.strip():
        return
    text = text.strip()

    # Pronunciation Fix: Replace W.R.O.C.K / W R O C K with natural "Wrock"
    spoken_text = (
        text.replace("W.R.O.C.K.", "Wrock")
        .replace("W R O C K", "Wrock")
        .replace("W-R-O-C-K", "Wrock")
    )
    log.info("🗣️ Wrock Speaking: %s", spoken_text)
    hud_widget.update_state("SPEAKING", assistant_text=spoken_text)

    # 1. Try ElevenLabs API if key present
    api_key = (os.environ.get("ELEVENLABS_API_KEY") or "").strip()
    vid, model_id, output_format, pcm_rate = elevenlabs_env_config()
    if api_key and vid:
        try:
            from elevenlabs.client import ElevenLabs
            client = ElevenLabs(api_key=api_key)
            chunks = client.text_to_speech.convert(
                voice_id=vid,
                text=spoken_text,
                model_id=model_id,
                output_format=output_format,
            )
            raw = b"".join(chunks)
            if raw:
                # Play raw audio PCM
                pcm_i16 = np.frombuffer(raw, dtype=np.int16)
                pcm_f = pcm_i16.astype(np.float32) / 32768.0
                sd.play(pcm_f, pcm_rate)
                sd.wait()
                hud_widget.update_state("IDLE")
                return
        except Exception as e:
            log.warning("ElevenLabs notice: %s. Switching to pyttsx3 voice fallback...", e)

    # 2. Offline pyttsx3 voice engine (100% reliable)
    try:
        import pyttsx3
        engine = pyttsx3.init()
        engine.setProperty("rate", 175)
        engine.say(spoken_text)
        engine.runAndWait()
    except Exception as e:
        log.warning("pyttsx3 fallback notice: %s", e)

    hud_widget.update_state("IDLE")


# ==============================================================================
# 3. DATABASE & MULTILINGUAL SCRIPT DETECTION
# ==============================================================================

class WrockDatabase:
    """SQLite Database for storing chat history and commands."""
    def __init__(self, db_path: str = "wrock_chats.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS chat_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                        user_input TEXT,
                        assistant_response TEXT,
                        detected_language TEXT,
                        action_taken TEXT
                    )
                """)
                conn.commit()
        except Exception as e:
            log.warning("Database init notice: %s", e)

    def log_interaction(self, user_input: str, response: str, language: str = "en", action: str = "chat") -> None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO chat_history (user_input, assistant_response, detected_language, action_taken)
                    VALUES (?, ?, ?, ?)
                """, (user_input, response, language, action))
                conn.commit()
                log.info("💾 Saved interaction to SQLite database [wrock_chats.db]")
        except Exception as e:
            log.warning("Database log notice: %s", e)

wrock_db = WrockDatabase()


def detect_script_lang(text: str) -> str:
    """Detect language based on Unicode script or transliterated keywords (Odia, Bengali, Hindi, English)."""
    t = text.strip()
    if not t:
        return "en"
    for char in t:
        cp = ord(char)
        if 0x0B00 <= cp <= 0x0B7F:
            return "or"  # Odia script
        elif 0x0980 <= cp <= 0x09FF:
            return "bn"  # Bengali script
        elif 0x0900 <= cp <= 0x097F:
            return "hi"  # Devanagari script

    t_lower = t.lower()
    if any(w in t_lower.split() for w in {"kemitia", "achanti", "namaskar", "kan", "khabar", "odisha", "bhala", "ghara", "kholideba"}):
        return "or"
    if any(w in t_lower.split() for w in {"kemon", "achhen", "khobor", "amra", "bangla", "bhalo", "ki", "dada", "khule"}):
        return "bn"
    if any(w in t_lower.split() for w in {"kaise", "hai", "bhai", "kya", "batao", "karo", "aaj", "suniye", "khol", "namaste", "shukriya", "bajaao", "karna", "haazir", "sab", "apna", "mujhe", "tumhari"}):
        return "hi"

    try:
        from langdetect import detect
        d = detect(t)
        if d:
            return d
    except Exception:
        pass

    return "en"


# ==============================================================================
# 4. CLAUDE & GROK AI MULTI-LINGUAL BRAIN
# ==============================================================================

def query_ai_assistant(user_prompt: str) -> str:
    """Query Anthropic Claude or xAI Grok API with universal multi-lingual response capability."""
    lang = detect_script_lang(user_prompt)
    
    # 1. Try Anthropic Claude API first if key exists
    anthropic_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    workspace_id = (os.environ.get("ANTHROPIC_WORKSPACE_ID") or "").strip()
    if anthropic_key:
        try:
            import anthropic
            headers = {}
            if workspace_id:
                headers["anthropic-workspace-id"] = workspace_id
            client = anthropic.Anthropic(api_key=anthropic_key, default_headers=headers if headers else None)
            sys_msg = (
                "You are Wrock, an ultra-fast AI voice assistant for your King. "
                "You control the user's PC and talk with Grok's confident, witty style. "
                "ALWAYS RESPOND IN THE EXACT SAME LANGUAGE AND SCRIPT THAT THE USER SPOKE IN (Odia, Bengali, Hindi, Hinglish, English). "
                "Keep responses intelligent, concise (1-2 sentences), sharp, and respectful to your King."
            )
            res = client.messages.create(
                model="claude-3-5-sonnet-20241022",
                max_tokens=120,
                system=sys_msg,
                messages=[{"role": "user", "content": user_prompt}]
            )
            if res.content and len(res.content) > 0:
                reply = res.content[0].text.strip()
                if reply:
                    return reply
        except Exception as e:
            log.warning("Anthropic Claude API notice: %s", e)

    # 2. Try xAI Grok API
    xai_key = (os.environ.get("XAI_API_KEY") or "").strip()
    if xai_key:
        try:
            import requests
            headers = {"Authorization": f"Bearer {xai_key}", "Content-Type": "application/json"}
            payload = {
                "model": "grok-beta",
                "messages": [
                    {"role": "system", "content": "You are Wrock, an AI assistant like Grok for King. Answer in exact user language in 1-2 sentences."},
                    {"role": "user", "content": user_prompt}
                ],
                "max_tokens": 120
            }
            res = requests.post("https://api.x.ai/v1/chat/completions", headers=headers, json=payload, timeout=5)
            if res.status_code == 200:
                content = res.json()["choices"][0]["message"]["content"].strip()
                if content:
                    return content
        except Exception as e:
            log.warning("xAI Grok API notice: %s", e)

    # 3. High-Quality Local Multi-Lingual Fallback Engine
    prompt_lower = user_prompt.lower()
    if lang == "or":
        if "kemitia" in prompt_lower or "achanti" in prompt_lower:
            return "Mo bhala achhi King! Aapana kemiti achanti? Bataantu aaji kana kariba?"
        else:
            return "Haan King! Wrock aapanka seba re haajir achhi!"
    elif lang == "bn":
        if "kemon" in prompt_lower or "achhen" in prompt_lower:
            return "Ami bhalo achhi King! Apni kemon achhen? Bolun ajke ki korbo?"
        else:
            return "Haan King! Wrock apnar sebay prostut achhe!"
    elif lang == "hi":
        if "kaise ho" in prompt_lower:
            return "Ek number King! Full charging mode me hu, batao aaj kya scene hai?"
        else:
            return "Sahi baat hai King! Wrock haazir hai. Aapki aagya sar aankhon par!"
    else:
        if "how are you" in prompt_lower:
            return "I am doing great, King! Standing by for your commands."
        elif "who are you" in prompt_lower:
            return "I am Wrock, your personal Grok and Claude powered AI command assistant!"
        elif "what can you do" in prompt_lower:
            return "I can control your PC, capture screenshots, manage volume, check system status, open apps, and assist you in any language, King!"
        else:
            return "At your service, King! Tell me what you need."


# ==============================================================================
# 5. FULL PC AUTOMATION HANDLERS
# ==============================================================================

def take_screenshot() -> str:
    """Capture screen and open it."""
    if pyautogui is None:
        return "PyAutoGUI not installed."
    pics_dir = os.path.join(os.path.expanduser("~"), "Pictures")
    if not os.path.exists(pics_dir):
        pics_dir = os.path.expanduser("~")
    filename = os.path.join(pics_dir, f"wrock_screenshot_{int(time.time())}.png")
    pyautogui.screenshot(filename)
    if sys.platform == "win32":
        try:
            os.startfile(filename)
        except Exception:
            pass
    return f"Screenshot saved to {filename}"


def change_volume(action: str) -> str:
    """Adjust Windows master volume."""
    if pyautogui is None:
        return "Volume control unavailable."
    if action == "up":
        for _ in range(5):
            pyautogui.press("volumeup")
        return "Volume increased, King."
    elif action == "down":
        for _ in range(5):
            pyautogui.press("volumedown")
        return "Volume decreased, King."
    elif action == "mute":
        pyautogui.press("volumemute")
        return "Muted audio, King."
    return "Volume adjusted."


def get_system_status() -> str:
    """Get CPU, RAM, and battery stats."""
    if psutil is None:
        return "System status info unavailable."
    cpu = psutil.cpu_percent(interval=0.2)
    ram = psutil.virtual_memory().percent
    battery_info = ""
    if hasattr(psutil, "sensors_battery"):
        bat = psutil.sensors_battery()
        if bat:
            plug = "Plugged in" if bat.power_plugged else "On battery"
            battery_info = f", Battery: {bat.percent}% ({plug})"
    return f"CPU at {cpu} percent, RAM usage is {ram} percent{battery_info}"


def lock_pc() -> str:
    """Lock the Windows workstation."""
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.LockWorkStation()
        return "PC locked, King."
    return "Lock function supported on Windows."


def type_text(text: str) -> str:
    """Type out text automatically."""
    if pyautogui is None:
        return "Typing unavailable."
    pyautogui.typewrite(text, interval=0.03)
    return f"Typed: {text}"


def close_active_window() -> str:
    """Close active foreground window."""
    if pyautogui is None:
        return "Close window unavailable."
    pyautogui.hotkey("alt", "f4")
    return "Closed active window."


def open_url_in_chrome(url: str) -> None:
    if not url:
        return
    try:
        webbrowser.open(url)
    except Exception as e:
        log.warning("Web browser error: %s", e)


def open_cursor_window() -> None:
    try:
        exe = shutil.which("cursor") or os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "cursor", "Cursor.exe")
        if os.path.isfile(exe):
            subprocess.Popen([exe])
            log.info("Launched Cursor IDE: %s", exe)
    except Exception as e:
        log.warning("Could not launch Cursor: %s", e)


def process_voice_command(cmd: str) -> None:
    """Process incoming voice command and execute PC actions or AI queries."""
    cmd = cmd.lower().strip()
    lang = detect_script_lang(cmd)
    log.info("⚡ Executing Wrock Command: %r (Language: %s)", cmd, lang)
    hud_widget.update_state("THINKING", user_text=cmd)

    action = "chat"
    reply = ""

    # PC Automation Commands
    if "screenshot" in cmd or "capture screen" in cmd or "take picture" in cmd:
        action = "screenshot"
        res = take_screenshot()
        reply = "Screenshot captured and saved for you, King!"
    elif "volume up" in cmd or "increase volume" in cmd or "louder" in cmd:
        action = "volume_up"
        reply = change_volume("up")
    elif "volume down" in cmd or "decrease volume" in cmd or "quieter" in cmd:
        action = "volume_down"
        reply = change_volume("down")
    elif "mute" in cmd or "silence" in cmd:
        action = "volume_mute"
        reply = change_volume("mute")
    elif "status" in cmd or "cpu" in cmd or "ram" in cmd or "battery" in cmd or "laptop status" in cmd:
        action = "system_status"
        reply = get_system_status()
    elif "lock pc" in cmd or "lock computer" in cmd or "lock screen" in cmd or "sleep pc" in cmd:
        action = "lock_pc"
        reply = lock_pc()
    elif cmd.startswith("type ") or cmd.startswith("write "):
        action = "type_text"
        txt = cmd.replace("type", "", 1).replace("write", "", 1).strip()
        reply = type_text(txt)
    elif "close window" in cmd or "close app" in cmd:
        action = "close_window"
        reply = close_active_window()
    elif "file explorer" in cmd or "my computer" in cmd or "open files" in cmd:
        action = "open_explorer"
        subprocess.Popen(["explorer.exe"])
        reply = "Opening File Explorer for you, King!"
    elif "task manager" in cmd:
        action = "open_taskmgr"
        subprocess.Popen(["taskmgr.exe"])
        reply = "Opening Task Manager, King!"
    elif "youtube" in cmd:
        action = "open_youtube"
        open_url_in_chrome("https://www.youtube.com")
        reply = "Opening YouTube for you, King!"
    elif "claude" in cmd:
        action = "open_claude"
        open_url_in_chrome("https://claude.ai/new")
        reply = "Opening Claude AI for you, King!"
    elif "cursor" in cmd or "code" in cmd or "ide" in cmd:
        action = "open_cursor"
        open_cursor_window()
        reply = "Opening Cursor IDE for you, King!"
    elif "spotify" in cmd or "music" in cmd or "song" in cmd:
        action = "play_music"
        webbrowser.open("https://open.spotify.com")
        reply = "Opening Spotify music, King!"
    elif "calculator" in cmd or "calc" in cmd:
        action = "open_calculator"
        subprocess.Popen(["calc.exe"])
        reply = "Opening Calculator for you, King."
    elif "notepad" in cmd:
        action = "open_notepad"
        subprocess.Popen(["notepad.exe"])
        reply = "Opening Notepad for you, King."
    elif "time" in cmd or "samay" in cmd:
        action = "tell_time"
        now_str = time.strftime("%I:%M %p")
        reply = f"Current time is {now_str}, King."
    elif "date" in cmd or "taarikh" in cmd:
        action = "tell_date"
        date_str = time.strftime("%A, %B %d, %Y")
        reply = f"Today is {date_str}, King."
    elif "search" in cmd or "google" in cmd:
        action = "web_search"
        q = cmd.replace("search", "").replace("google", "").replace("for", "").strip()
        open_url_in_chrome(f"https://www.google.com/search?q={q}" if q else "https://www.google.com")
        reply = f"Searching Google for {q or 'your topic'}, King!"
    else:
        action = "ai_query"
        reply = query_ai_assistant(cmd)

    # Save to SQLite database
    wrock_db.log_interaction(user_input=cmd, response=reply, language=lang, action=action)

    log.info("🤖 Wrock: %s", reply)
    speak_text(reply)


def listen_and_process_voice_command() -> None:
    """Activate Siri-style listener upon wake word or double clap."""
    play_activation_chime()
    hud_widget.update_state("LISTENING", status_text="LISTENING FOR COMMAND...")
    log.info("🎙️ [Wrock Activated!] Listening for your voice command...")
    try:
        import speech_recognition as sr
        r = sr.Recognizer()
        with sr.Microphone() as source:
            r.adjust_for_ambient_noise(source, duration=0.3)
            log.info("🎤 Listening... Speak your command now!")
            audio = r.listen(source, timeout=6, phrase_time_limit=10)
        cmd = r.recognize_google(audio).lower().strip()
        log.info("🗣️ Recognized Command: %r", cmd)
        process_voice_command(cmd)
    except Exception as e:
        log.info("No voice command heard (%s). Wrock standing by.", e)
        hud_widget.update_state("IDLE", status_text="STANDBY FOR WAKE WORD")
        speak_text("Koi command nahi mila King. Wrock tayyar hai jab bhi bulaoge.")


def run_double_clap_actions() -> None:
    listen_and_process_voice_command()


def voice_wake_word_loop() -> None:
    """Background thread monitoring microphone for wake words like 'Wake up Wrock' or 'Wrock'."""
    try:
        import speech_recognition as sr
        r = sr.Recognizer()
        log.info("🎙️ Voice Wake-Word Engine Active (Say 'Wake up Wrock' or 'Wrock' anytime!)")
        with sr.Microphone() as source:
            r.adjust_for_ambient_noise(source, duration=0.5)
            while True:
                try:
                    audio = r.listen(source, timeout=4, phrase_time_limit=5)
                    text = r.recognize_google(audio).lower().strip()
                    if text:
                        log.info("🗣️ Heard speech: %r", text)
                    if any(w in text for w in WAKE_WORDS):
                        log.info("⚡ Wake-word detected: %r!", text)
                        listen_and_process_voice_command()
                except (sr.WaitTimeoutError, sr.UnknownValueError):
                    pass
                except Exception as e:
                    log.debug("Wake loop notice: %s", e)
                    time.sleep(0.5)
    except Exception as e:
        log.warning("Voice wake-word engine notice: %s", e)


# ==============================================================================
# 6. MAIN CLAP LISTENER & ENTRY POINT
# ==============================================================================

def main() -> int:
    blocksize = block_samples()
    noise_floor = 1e-4
    last_logged_double = 0.0
    first_clap_time: float | None = None
    spike_armed = True

    log.info("🚀 Starting Wrock Assistant (HUD + Double Clap + Voice Wake-Word Engine)...")
    
    # Start Holographic HUD Widget
    hud_widget.start()

    input_idx = _choose_input_device(blocksize)

    # Start background voice wake-word thread ("Wake up Wrock" / "Wrock")
    threading.Thread(target=voice_wake_word_loop, daemon=True).start()

    try:
        with sd.InputStream(
            device=input_idx,
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            blocksize=blocksize,
        ) as stream:
            while True:
                data, overflowed = stream.read(blocksize)
                level = rms_mono(data)

                quiet_gate = noise_floor * QUIET_GATE_MULT
                if level < quiet_gate:
                    noise_floor = NOISE_FLOOR_ALPHA * noise_floor + (1.0 - NOISE_FLOOR_ALPHA) * level
                    noise_floor = max(noise_floor, 1e-7)

                threshold = max(noise_floor * SPIKE_RATIO, MIN_RMS)
                now = time.monotonic()
                retrigger_level = threshold * RETRIGGER_RATIO

                if level < retrigger_level:
                    spike_armed = True

                if first_clap_time is not None and (now - first_clap_time) > MAX_DOUBLE_GAP_S:
                    first_clap_time = None

                if (
                    spike_armed
                    and level >= threshold
                    and (now - last_logged_double) >= COOLDOWN_S
                ):
                    spike_armed = False
                    if first_clap_time is None:
                        first_clap_time = now
                        log.info("👏 [Clap 1/2 Detected!] (rms=%.4f) — Clap again within %.2fs!", level, MAX_DOUBLE_GAP_S)
                    else:
                        gap = now - first_clap_time
                        if MIN_DOUBLE_GAP_S <= gap <= MAX_DOUBLE_GAP_S:
                            first_clap_time = None
                            last_logged_double = now
                            log.info("💥 [Double Clap Verified!] Gap=%.3fs — Activating Wrock voice assistant!", gap)
                            threading.Thread(target=run_double_clap_actions, daemon=True).start()
                        else:
                            first_clap_time = now

    except KeyboardInterrupt:
        log.info("Stopped.")
        return 0
    except sd.PortAudioError as e:
        log.error("Audio error: %s", e)
        return 1

    return 0


def test_mic_live() -> None:
    """Print live RMS audio level visualizer bar."""
    blocksize = block_samples()
    input_idx = _choose_input_device(blocksize)
    print("\n--- 🎤 LIVE MIC TEST (Make sounds or clap to see volume meter) ---")
    print("Press Ctrl+C to exit test mode.\n")
    try:
        with sd.InputStream(
            device=input_idx,
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            blocksize=blocksize,
        ) as stream:
            while True:
                data, _ = stream.read(blocksize)
                level = rms_mono(data)
                bars = "█" * int(level * 500)
                spike_mark = " 💥 SPIKE!" if level >= MIN_RMS else ""
                print(f"\rRMS: {level:.5f} |{bars:<50}|{spike_mark}", end="", flush=True)
                time.sleep(0.02)
    except KeyboardInterrupt:
        print("\nMic test stopped.\n")


def print_chat_history() -> None:
    """Print conversation history from wrock_chats.db."""
    print("\n=======================================================")
    print("       WROCK AI ASSISTANT -- CONVERSATION HISTORY       ")
    print("=======================================================\n")
    try:
        with sqlite3.connect("wrock_chats.db") as conn:
            cursor = conn.cursor()
            rows = cursor.execute(
                "SELECT timestamp, user_input, assistant_response, detected_language, action_taken "
                "FROM chat_history ORDER BY id DESC LIMIT 25"
            ).fetchall()
            if not rows:
                print("No history found in database yet.")
                return
            for ts, inp, resp, lang, act in rows:
                print(f"[{ts}] [{lang.upper()}] ({act})")
                print(f"  User: {inp}")
                print(f"  Wrock: {resp}\n")
    except Exception as e:
        print(f"Could not read database history: {e}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        arg = sys.argv[1].lower()
        if arg in ("--test-actions", "-a", "test-actions"):
            print("🚀 Running all Wrock actions test...")
            run_double_clap_actions()
            time.sleep(3)
            sys.exit(0)
        elif arg in ("--test-mic", "-m", "test-mic"):
            test_mic_live()
            sys.exit(0)
        elif arg in ("--history", "-h", "history"):
            print_chat_history()
            sys.exit(0)

    sys.exit(main())
