#!/usr/bin/env python3
"""Hold a key to record, release to transcribe, then type or copy.

Uses the Linux input layer for the hotkey, PulseAudio/PipeWire (`parec`) for
the mic, and AT-SPI to insert text when an editable field is focused.
Otherwise the transcript goes to the clipboard.
"""

from __future__ import annotations

import json
import os
import queue
import select
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
import uuid
import wave
from pathlib import Path

import tkinter as tk
from tkinter import ttk

CONFIG_DIR = Path.home() / ".config" / "panda-hold-to-talk"
CONFIG_PATH = CONFIG_DIR / "config.json"
AUTOSTART_PATH = Path.home() / ".config" / "autostart" / "hold-to-talk.desktop"
LAUNCHER_PATH = Path.home() / ".local" / "share" / "applications" / "hold-to-talk.desktop"
SAMPLE_RATE = 16000
MIN_SECONDS = 0.3
MAX_SECONDS = 90
# Linux input-event-codes.h KEY_FN. Many laptop firmwares never emit this.
DEFAULT_KEY_CODE = 464

SARVAM_URL = "https://api.sarvam.ai/speech-to-text"
OPENAI_URL = "https://api.openai.com/v1/audio/transcriptions"
LOCAL_MODELS = ("tiny", "base", "small", "medium")
SARVAM_MODES = ("transcribe", "translit", "translate", "verbatim", "codemix")


def default_config() -> dict:
    return {
        "hotkey_code": DEFAULT_KEY_CODE,
        "hotkey_name": "KEY_FN",
        "engine": "local",
        "local_model": "base",
        "sarvam_api_key": "",
        "sarvam_mode": "transcribe",
        "openai_api_key": "",
    }


def load_config() -> dict:
    cfg = default_config()
    try:
        saved = json.loads(CONFIG_PATH.read_text())
        if isinstance(saved, dict):
            cfg.update({k: saved[k] for k in cfg if k in saved})
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def app_dir() -> Path:
    return Path(__file__).resolve().parent


def logo_path() -> Path:
    return app_dir() / "logo.png"


def desktop_entry() -> str:
    script = app_dir() / "run.sh"
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Hold to Talk\n"
        "Comment=Hold a key to dictate into the focused text field\n"
        f"Exec={script}\n"
        f"Icon={logo_path()}\n"
        "Terminal=false\n"
        "Categories=Utility;Audio;\n"
        "StartupNotify=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def install_launcher() -> None:
    LAUNCHER_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAUNCHER_PATH.write_text(desktop_entry())


def autostart_enabled() -> bool:
    if not AUTOSTART_PATH.exists():
        return False
    text = AUTOSTART_PATH.read_text()
    if "Hidden=true" in text or "X-GNOME-Autostart-enabled=false" in text:
        return False
    return True


def set_autostart(enabled: bool) -> None:
    AUTOSTART_PATH.parent.mkdir(parents=True, exist_ok=True)
    if enabled:
        AUTOSTART_PATH.write_text(desktop_entry())
        return
    disabled = desktop_entry().replace(
        "X-GNOME-Autostart-enabled=true\n",
        "X-GNOME-Autostart-enabled=false\nHidden=true\n",
    )
    AUTOSTART_PATH.write_text(disabled)


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(CONFIG_PATH)
    os.chmod(CONFIG_PATH, 0o600)


def pcm_to_wav(pcm: bytes) -> bytes:
    import io

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm)
    return buf.getvalue()


def copy_clipboard(text: str) -> None:
    errors = []
    for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"]):
        try:
            subprocess.run(cmd, input=text.encode(), check=True, timeout=3)
            return
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(str(exc))
    raise RuntimeError("clipboard copy failed: " + "; ".join(errors))


def _selection_range(text_iface):
    try:
        count = text_iface.get_n_selections()
    except Exception:
        return None
    if not count:
        return None
    try:
        sel = text_iface.get_selection(0)
    except Exception:
        return None
    if isinstance(sel, tuple):
        if len(sel) == 3:
            ok, start, end = sel
            if not ok:
                return None
            return int(start), int(end)
        if len(sel) == 2:
            return int(sel[0]), int(sel[1])
    return None


def insert_into_focused(text: str) -> bool:
    """Insert into the focused editable via AT-SPI. False if nothing editable is focused."""
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    if not getattr(insert_into_focused, "_inited", False):
        Atspi.init()
        insert_into_focused._inited = True

    desktop = Atspi.get_desktop(0)
    focused = None
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if app is None:
            continue
        try:
            states = app.get_state_set()
        except Exception:
            continue
        if not states.contains(Atspi.StateType.ACTIVE):
            continue
        focused = _find_focused(app, Atspi, 0)
        if focused is not None:
            break
    if focused is None:
        return False

    try:
        editable = focused.get_editable_text()
        text_iface = focused.get_text()
    except Exception:
        return False
    if editable is None or text_iface is None:
        return False

    try:
        states = focused.get_state_set()
        if states.contains(Atspi.StateType.EDITABLE) is False:
            # Interface present but the widget is not an editable field.
            role = focused.get_role()
            if role not in (
                Atspi.Role.ENTRY,
                Atspi.Role.PASSWORD_TEXT,
                Atspi.Role.TEXT,
                Atspi.Role.PARAGRAPH,
                Atspi.Role.DOCUMENT_FRAME,
                Atspi.Role.TERMINAL,
                Atspi.Role.EDITBAR,
            ):
                return False
    except Exception:
        return False

    span = _selection_range(text_iface)
    if span is not None:
        start, end = span
        if end < start:
            start, end = end, start
        try:
            editable.delete_text(start, end)
        except Exception:
            pass
        offset = start
    else:
        try:
            offset = text_iface.get_caret_offset()
        except Exception:
            offset = -1
        if offset is None or offset < 0:
            try:
                offset = text_iface.get_character_count()
            except Exception:
                offset = 0
    payload = text.encode("utf-8")
    return bool(editable.insert_text(int(offset), text, len(payload)))


def _find_focused(acc, Atspi, depth: int):
    if acc is None or depth > 40:
        return None
    try:
        states = acc.get_state_set()
    except Exception:
        return None
    if depth > 0 and not states.contains(Atspi.StateType.SHOWING):
        return None
    if states.contains(Atspi.StateType.FOCUSED):
        return acc
    try:
        count = acc.get_child_count()
    except Exception:
        return None
    for i in range(count):
        try:
            child = acc.get_child_at_index(i)
        except Exception:
            continue
        found = _find_focused(child, Atspi, depth + 1)
        if found is not None:
            return found
    return None


def transcribe_sarvam(wav: bytes, api_key: str, mode: str) -> str:
    if not api_key.strip():
        raise RuntimeError("Add a Sarvam API key first")
    boundary = f"----HoldToTalk{uuid.uuid4().hex}"
    parts = []

    def field(name: str, value: str) -> None:
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode()
        )

    field("model", "saaras:v3")
    field("mode", mode or "transcribe")
    parts.append(
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="utterance.wav"\r\n'
            "Content-Type: audio/wav\r\n\r\n"
        ).encode()
    )
    parts.append(wav)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)
    req = urllib.request.Request(
        SARVAM_URL,
        data=body,
        headers={
            "api-subscription-key": api_key.strip(),
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    return _http_json_text(req, "transcript")


def transcribe_openai(wav: bytes, api_key: str) -> str:
    if not api_key.strip():
        raise RuntimeError("Add an OpenAI API key first")
    boundary = f"----HoldToTalk{uuid.uuid4().hex}"
    parts = [
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="model"\r\n\r\n'
            "whisper-1\r\n"
        ).encode(),
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="utterance.wav"\r\n'
            "Content-Type: audio/wav\r\n\r\n"
        ).encode(),
        wav,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    req = urllib.request.Request(
        OPENAI_URL,
        data=b"".join(parts),
        headers={
            "Authorization": f"Bearer {api_key.strip()}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    return _http_json_text(req, "text")


def _http_json_text(req: urllib.request.Request, field: str) -> str:
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"network error: {exc.reason}") from exc
    data = json.loads(raw)
    text = data.get(field) or ""
    if not isinstance(text, str):
        raise RuntimeError(f"unexpected response: {raw[:300]}")
    return text.strip()


_whisper_lock = threading.Lock()
_whisper_model = None
_whisper_model_name = None


def transcribe_local(wav: bytes, model_name: str) -> str:
    global _whisper_model, _whisper_model_name
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "Local model needs faster-whisper. Run ./run.sh once so it installs."
        ) from exc
    with _whisper_lock:
        if _whisper_model is None or _whisper_model_name != model_name:
            _whisper_model = WhisperModel(model_name, device="cpu", compute_type="int8")
            _whisper_model_name = model_name
        model = _whisper_model
    fd, path = tempfile.mkstemp(suffix=".wav")
    try:
        os.write(fd, wav)
        os.close(fd)
        fd = -1
        segments, _info = model.transcribe(path, vad_filter=True)
        return " ".join(seg.text.strip() for seg in segments).strip()
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(path)
        except OSError:
            pass


class Recorder:
    def __init__(self, on_clip):
        self._on_clip = on_clip
        self._proc = None
        self._thread = None
        self._lock = threading.Lock()
        self.recording = False

    def start(self) -> None:
        with self._lock:
            if self.recording:
                return
            self.recording = True
            proc = subprocess.Popen(
                [
                    "parec",
                    "--raw",
                    "--format=s16le",
                    "--rate",
                    str(SAMPLE_RATE),
                    "--channels",
                    "1",
                    "--latency-msec=40",
                    "--client-name=HoldToTalk",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            self._proc = proc
            self._thread = threading.Thread(target=self._read, args=(proc,), daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            proc = self._proc
            self.recording = False
            self._proc = None
        if proc is not None and proc.poll() is None:
            proc.terminate()

    def _read(self, proc: subprocess.Popen) -> None:
        chunks = []
        max_bytes = SAMPLE_RATE * 2 * MAX_SECONDS
        try:
            assert proc.stdout is not None
            while True:
                block = proc.stdout.read(SAMPLE_RATE * 2 // 5)
                if not block:
                    break
                chunks.append(block)
                if sum(len(c) for c in chunks) >= max_bytes:
                    proc.terminate()
                    break
        except Exception:
            pass
        finally:
            if proc.poll() is None:
                proc.kill()
            pcm = b"".join(chunks)
            with self._lock:
                if self._proc is proc:
                    self._proc = None
                    self.recording = False
            self._on_clip(pcm)


class KeyListener(threading.Thread):
    def __init__(self, app: "App"):
        super().__init__(daemon=True)
        self.app = app
        self._devices = []

    def run(self) -> None:
        try:
            import evdev
            from evdev import ecodes
        except ImportError:
            self.app.ui_queue.put(
                ("error", "python evdev is missing. Run ./run.sh so the venv installs it.")
            )
            return
        self._evdev = evdev
        self._ecodes = ecodes
        while True:
            self._refresh()
            if not self._devices:
                self.app.ui_queue.put(
                    ("error", "No keyboard devices. Check read access to /dev/input/event*.")
                )
                select.select([], [], [], 2.0)
                continue
            readable, _, _ = select.select(self._devices, [], [], 0.4)
            for dev in readable:
                try:
                    for event in dev.read():
                        self._handle(event)
                except OSError:
                    self._devices = [d for d in self._devices if d is not dev]

    def _refresh(self) -> None:
        evdev = self._evdev
        ecodes = self._ecodes
        open_paths = {d.path for d in self._devices}
        for path in evdev.list_devices():
            if path in open_paths:
                continue
            try:
                dev = evdev.InputDevice(path)
            except OSError:
                continue
            keys = dev.capabilities().get(ecodes.EV_KEY, [])
            if ecodes.KEY_A not in keys and ecodes.KEY_SPACE not in keys:
                continue
            self._devices.append(dev)

    def _handle(self, event) -> None:
        ecodes = self._ecodes
        if event.type != ecodes.EV_KEY or event.value == 2:
            return
        code = event.code
        name = ecodes.KEY.get(code)
        if isinstance(name, list):
            name = name[0]
        if not isinstance(name, str) or name.startswith("BTN_"):
            return
        if self.app.capture_next and event.value == 1:
            if code == ecodes.KEY_ESC:
                self.app.ui_queue.put(("capture_cancel", None))
            else:
                self.app.ui_queue.put(("capture_key", (code, name)))
            return
        if code != self.app.hotkey_code:
            return
        if event.value == 1:
            self.app.ui_queue.put(("down", None))
        elif event.value == 0:
            self.app.ui_queue.put(("up", None))


class App:
    def __init__(self):
        self.cfg = load_config()
        self.hotkey_code = int(self.cfg["hotkey_code"])
        self.capture_next = False
        self.ui_queue: queue.Queue = queue.Queue()
        self.audio_queue: queue.Queue = queue.Queue()
        self.recorder = Recorder(self._on_pcm)
        self._cfg_lock = threading.Lock()
        self._live = dict(self.cfg)
        self._held = False

        self.root = tk.Tk()
        self.root.title("Hold to Talk")
        self.root.configure(bg="#161616")
        self._apply_window_icon()
        self.root.geometry("460x720")
        self.root.minsize(420, 560)

        self._build_overlay()
        self._build_ui()
        self._load_into_form()
        threading.Thread(target=self._transcribe_loop, daemon=True).start()
        KeyListener(self).start()
        self.root.after(50, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self._quit)

    def _apply_window_icon(self) -> None:
        self._logo_small = None
        path = logo_path()
        if not path.exists():
            return
        try:
            image = tk.PhotoImage(file=str(path))
        except tk.TclError:
            return
        self._logo_image = image
        self.root.iconphoto(True, image)
        factor = max(1, image.width() // 48)
        self._logo_small = image.subsample(factor, factor)

    def _build_overlay(self) -> None:
        self.overlay = tk.Toplevel(self.root)
        self.overlay.overrideredirect(True)
        self.overlay.attributes("-topmost", True)
        self.overlay.configure(bg="#1a1a1a")
        try:
            self.overlay.attributes("-type", "notification")
        except tk.TclError:
            pass
        frame = tk.Frame(self.overlay, bg="#1a1a1a", padx=16, pady=10)
        frame.pack()
        self.dot = tk.Canvas(frame, width=18, height=18, bg="#1a1a1a", highlightthickness=0)
        self.dot.pack(side="left")
        self.dot_id = self.dot.create_oval(2, 2, 16, 16, fill="#e23b3b", outline="")
        self.overlay_label = tk.Label(
            frame,
            text="Listening",
            fg="#ffffff",
            bg="#1a1a1a",
            font=("Sans", 16, "bold"),
        )
        self.overlay_label.pack(side="left", padx=(10, 0))
        self.overlay.withdraw()

    def _place_overlay(self) -> None:
        self.overlay.update_idletasks()
        w = self.overlay.winfo_width()
        h = self.overlay.winfo_height()
        sw = self.overlay.winfo_screenwidth()
        sh = self.overlay.winfo_screenheight()
        x = max(0, (sw - w) // 2)
        y = sh - h - 80
        self.overlay.geometry(f"+{x}+{y}")
        self._drop_overlay_focus()

    def _drop_overlay_focus(self) -> None:
        """Ask X11 not to give this OSD keyboard focus."""
        try:
            import ctypes

            class XWMHints(ctypes.Structure):
                _fields_ = [
                    ("flags", ctypes.c_long),
                    ("input", ctypes.c_long),
                    ("initial_state", ctypes.c_long),
                    ("icon_pixmap", ctypes.c_ulong),
                    ("icon_window", ctypes.c_ulong),
                    ("icon_x", ctypes.c_int),
                    ("icon_y", ctypes.c_int),
                    ("icon_mask", ctypes.c_ulong),
                    ("window_group", ctypes.c_ulong),
                ]

            x11 = ctypes.cdll.LoadLibrary("libX11.so.6")
            dpy = x11.XOpenDisplay(None)
            if not dpy:
                return
            hints = XWMHints()
            hints.flags = 1
            hints.input = 0
            x11.XSetWMHints(dpy, self.overlay.winfo_id(), ctypes.byref(hints))
            x11.XFlush(dpy)
            x11.XCloseDisplay(dpy)
        except Exception:
            pass

    def _show_overlay(self, text: str, color: str) -> None:
        self.overlay_label.configure(text=text)
        self.dot.itemconfigure(self.dot_id, fill=color)
        self.overlay.deiconify()
        self._place_overlay()

    def _hide_overlay(self) -> None:
        self.overlay.withdraw()

    def _build_ui(self) -> None:
        bg = "#161616"
        fg = "#ffffff"
        muted = "#c8c8c8"
        pad = {"padx": 16, "pady": 4}

        header = tk.Frame(self.root, bg=bg)
        header.pack(anchor="w", padx=16, pady=(16, 4))
        if self._logo_small is not None:
            tk.Label(header, image=self._logo_small, bg=bg).pack(side="left", padx=(0, 10))
        tk.Label(header, text="Hold to Talk", fg=fg, bg=bg, font=("Sans", 20, "bold")).pack(side="left")

        self.status = tk.Label(
            self.root, text="Idle", fg=fg, bg=bg, font=("Sans", 14), wraplength=420, justify="left"
        )
        self.status.pack(anchor="w", **pad)

        self.detail = tk.Label(
            self.root, text="", fg=muted, bg=bg, font=("Sans", 11), wraplength=420, justify="left"
        )
        self.detail.pack(anchor="w", padx=16, pady=(0, 8))

        box = tk.LabelFrame(self.root, text="Hotkey", fg=fg, bg=bg, font=("Sans", 12))
        box.pack(fill="x", padx=16, pady=6)
        row = tk.Frame(box, bg=bg)
        row.pack(fill="x", padx=8, pady=8)
        self.key_label = tk.Label(row, text="KEY_FN", fg=fg, bg=bg, font=("Sans", 14, "bold"))
        self.key_label.pack(side="left")
        self.capture_btn = tk.Button(
            row, text="Change key", command=self._begin_capture, font=("Sans", 12)
        )
        self.capture_btn.pack(side="right")
        self.key_hint = tk.Label(
            box,
            text="Hold to record. Release to transcribe.",
            fg=muted,
            bg=bg,
            font=("Sans", 11),
            wraplength=400,
            justify="left",
        )
        self.key_hint.pack(anchor="w", padx=8, pady=(0, 8))
        self.autostart = tk.BooleanVar(value=autostart_enabled())
        tk.Checkbutton(
            box,
            text="Open when I log in",
            variable=self.autostart,
            command=self._toggle_autostart,
            fg=fg,
            bg=bg,
            selectcolor="#2a2a2a",
            activebackground=bg,
            activeforeground=fg,
            font=("Sans", 12),
        ).pack(anchor="w", padx=8, pady=(0, 8))

        eng = tk.LabelFrame(self.root, text="Speech engine", fg=fg, bg=bg, font=("Sans", 12))
        eng.pack(fill="x", padx=16, pady=6)
        self.engine = tk.StringVar(value="local")
        for value, label in (
            ("local", "Local Whisper"),
            ("sarvam", "Sarvam"),
            ("openai", "OpenAI Whisper"),
        ):
            tk.Radiobutton(
                eng,
                text=label,
                variable=self.engine,
                value=value,
                command=self._save_form,
                fg=fg,
                bg=bg,
                selectcolor="#2a2a2a",
                activebackground=bg,
                activeforeground=fg,
                font=("Sans", 12),
            ).pack(anchor="w", padx=8, pady=2)

        model_row = tk.Frame(eng, bg=bg)
        model_row.pack(fill="x", padx=8, pady=(4, 8))
        tk.Label(model_row, text="Local model", fg=fg, bg=bg, font=("Sans", 12)).pack(side="left")
        self.local_model = ttk.Combobox(model_row, values=LOCAL_MODELS, state="readonly", width=12)
        self.local_model.pack(side="left", padx=8)
        self.local_model.bind("<<ComboboxSelected>>", lambda _e: self._save_form())

        mode_row = tk.Frame(eng, bg=bg)
        mode_row.pack(fill="x", padx=8, pady=(0, 8))
        tk.Label(mode_row, text="Sarvam mode", fg=fg, bg=bg, font=("Sans", 12)).pack(side="left")
        self.sarvam_mode = ttk.Combobox(mode_row, values=SARVAM_MODES, state="readonly", width=14)
        self.sarvam_mode.pack(side="left", padx=8)
        self.sarvam_mode.bind("<<ComboboxSelected>>", lambda _e: self._save_form())

        keys = tk.LabelFrame(self.root, text="API keys", fg=fg, bg=bg, font=("Sans", 12))
        keys.pack(fill="x", padx=16, pady=6)
        tk.Label(keys, text="Sarvam", fg=fg, bg=bg, font=("Sans", 12)).pack(anchor="w", padx=8, pady=(8, 0))
        self.sarvam_key = tk.Entry(keys, show="*", font=("Sans", 12))
        self.sarvam_key.pack(fill="x", padx=8, pady=4)
        tk.Label(keys, text="OpenAI", fg=fg, bg=bg, font=("Sans", 12)).pack(anchor="w", padx=8)
        self.openai_key = tk.Entry(keys, show="*", font=("Sans", 12))
        self.openai_key.pack(fill="x", padx=8, pady=4)
        actions = tk.Frame(keys, bg=bg)
        actions.pack(fill="x", padx=8, pady=(0, 8))
        tk.Button(actions, text="Save keys", command=self._save_form, font=("Sans", 12)).pack(side="left")
        self.show_keys = tk.BooleanVar(value=False)
        tk.Checkbutton(
            actions,
            text="Show keys",
            variable=self.show_keys,
            command=self._toggle_keys,
            fg=fg,
            bg=bg,
            selectcolor="#2a2a2a",
            activebackground=bg,
            activeforeground=fg,
            font=("Sans", 11),
        ).pack(side="left", padx=12)

        self.transcript = tk.Label(
            self.root,
            text="",
            fg=fg,
            bg=bg,
            font=("Sans", 12),
            wraplength=420,
            justify="left",
        )
        self.transcript.pack(anchor="w", padx=16, pady=8)

    def _load_into_form(self) -> None:
        self.engine.set(self.cfg["engine"] if self.cfg["engine"] in ("local", "sarvam", "openai") else "local")
        self.local_model.set(self.cfg["local_model"] if self.cfg["local_model"] in LOCAL_MODELS else "base")
        self.sarvam_mode.set(
            self.cfg["sarvam_mode"] if self.cfg["sarvam_mode"] in SARVAM_MODES else "transcribe"
        )
        self.sarvam_key.insert(0, self.cfg.get("sarvam_api_key") or "")
        self.openai_key.insert(0, self.cfg.get("openai_api_key") or "")
        self.key_label.configure(text=self.cfg.get("hotkey_name") or "KEY_FN")
        self._note_fn()

    def _note_fn(self) -> None:
        if self.hotkey_code != DEFAULT_KEY_CODE:
            return
        self.key_hint.configure(
            text=(
                "Hold to record, release to transcribe. "
                "Many laptops never send the Fn key to Linux. "
                "If holding Fn does nothing, use Change key."
            )
        )

    def _on_pcm(self, pcm: bytes) -> None:
        if len(pcm) < int(SAMPLE_RATE * 2 * MIN_SECONDS):
            self.ui_queue.put(("error", "Hold a little longer"))
            return
        self.audio_queue.put(pcm)

    def _publish_cfg(self) -> None:
        try:
            live = self._form_config()
        except tk.TclError:
            return
        with self._cfg_lock:
            self._live = live

    def _form_config(self) -> dict:
        engine = self.engine.get()
        model = self.local_model.get() or "base"
        mode = self.sarvam_mode.get() or "transcribe"
        return {
            "hotkey_code": self.hotkey_code,
            "hotkey_name": self.key_label.cget("text"),
            "engine": engine,
            "local_model": model,
            "sarvam_api_key": self.sarvam_key.get().strip(),
            "sarvam_mode": mode,
            "openai_api_key": self.openai_key.get().strip(),
        }

    def _save_form(self) -> None:
        self.cfg = self._form_config()
        save_config(self.cfg)
        self.status.configure(text="Saved")

    def _toggle_autostart(self) -> None:
        set_autostart(bool(self.autostart.get()))
        if self.autostart.get():
            self.status.configure(text="Will open on next login")
        else:
            self.status.configure(text="Will not open on login")

    def _toggle_keys(self) -> None:
        show = "" if self.show_keys.get() else "*"
        self.sarvam_key.configure(show=show)
        self.openai_key.configure(show=show)

    def _begin_capture(self) -> None:
        self.capture_next = True
        self.capture_btn.configure(text="Press a key…")
        self.status.configure(text="Press the new hotkey. Esc cancels.")

    def _set_status(self, text: str) -> None:
        self.status.configure(text=text)

    def _poll(self) -> None:
        self._publish_cfg()
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                self._on_ui(kind, payload)
        except queue.Empty:
            pass
        self.root.after(50, self._poll)

    def _on_ui(self, kind: str, payload) -> None:
        if kind == "down":
            if self.capture_next:
                return
            self._held = True
            try:
                self.recorder.start()
            except OSError as exc:
                self._held = False
                self._set_status(f"Mic failed: {exc}")
                return
            self._set_status("Listening")
            self._show_overlay("Listening", "#e23b3b")
        elif kind == "up":
            if not self._held and not self.recorder.recording:
                return
            self._held = False
            self.recorder.stop()
            self._set_status("Transcribing…")
            self._show_overlay("Transcribing", "#e0a020")
        elif kind == "capture_key":
            code, name = payload
            self.capture_next = False
            self.hotkey_code = code
            self.key_label.configure(text=name)
            self.capture_btn.configure(text="Change key")
            self._save_form()
            self._set_status(f"Hotkey is {name}")
            self.key_hint.configure(text="Hold to record. Release to transcribe.")
        elif kind == "capture_cancel":
            self.capture_next = False
            self.capture_btn.configure(text="Change key")
            self._set_status("Idle")
        elif kind == "status":
            self._set_status(str(payload))
        elif kind == "error":
            self._hide_overlay()
            self._set_status(str(payload))
        elif kind == "text":
            self._deliver(str(payload))
        elif kind == "done":
            where, text = payload
            self._hide_overlay()
            self.transcript.configure(text=text)
            if where == "field":
                self._set_status("Typed into the focused field")
            elif where == "clipboard":
                self._set_status("Copied to the clipboard")
            else:
                self._set_status(where)

    def _deliver(self, text: str) -> None:
        try:
            inserted = insert_into_focused(text)
        except Exception as exc:
            inserted = False
            copy_clipboard(text)
            self._on_ui("done", (f"Copied to clipboard ({exc})", text))
            return
        if inserted:
            self._on_ui("done", ("field", text))
            return
        copy_clipboard(text)
        self._on_ui("done", ("clipboard", text))

    def _transcribe_loop(self) -> None:
        while True:
            pcm = self.audio_queue.get()
            with self._cfg_lock:
                cfg = dict(self._live)
            try:
                wav = pcm_to_wav(pcm)
                engine = cfg.get("engine") or "local"
                if engine == "sarvam":
                    text = transcribe_sarvam(
                        wav,
                        cfg.get("sarvam_api_key") or "",
                        cfg.get("sarvam_mode") or "transcribe",
                    )
                elif engine == "openai":
                    text = transcribe_openai(wav, cfg.get("openai_api_key") or "")
                else:
                    model = cfg.get("local_model") or "base"
                    self.ui_queue.put(("status", f"Loading local model {model}…"))
                    text = transcribe_local(wav, model)
                if not text:
                    self.ui_queue.put(("error", "No speech recognized"))
                    continue
                self.ui_queue.put(("text", text))
            except Exception as exc:
                self.ui_queue.put(("error", str(exc)))

    def _quit(self) -> None:
        self.recorder.stop()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    missing = []
    try:
        import gi  # noqa: F401

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi  # noqa: F401
    except Exception:
        missing.append("sudo apt install python3-gi gir1.2-atspi-2.0")
    if missing:
        raise SystemExit(
            "Missing system libraries:\n  " + "\n  ".join(missing)
        )
    if subprocess.call(["which", "parec"], stdout=subprocess.DEVNULL) != 0:
        raise SystemExit("parec is missing. Install pipewire-pulse or pulseaudio-utils.")
    install_launcher()
    if not AUTOSTART_PATH.exists():
        set_autostart(True)
    App().run()


if __name__ == "__main__":
    main()
