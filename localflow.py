"""
LocalFlow - private, offline push-to-talk dictation for Windows.

Hold the hotkey (default: Right Ctrl), speak, let go. Your speech is transcribed on
this PC by Whisper large-v3-turbo (via whisper.cpp) and typed into whichever app has
focus. No internet, no API keys, nothing leaves your computer.

Run setup.ps1 once, then LocalFlow.bat.
Debug:   python localflow.py --transcribe some_16khz_mono.wav
"""
from __future__ import annotations

import array
import ctypes
import ctypes.wintypes as wt
import io
import json
import logging
import logging.handlers
import math
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import wave
import winreg
import winsound
from pathlib import Path

APP_NAME = "LocalFlow"
CODE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = CODE_DIR / "config.json"
DATA_DIR = Path(os.environ["LOCALAPPDATA"]) / APP_NAME  # big files live here, not in Google Drive
BIN_DIR = DATA_DIR / "bin"
MODEL_DIR = DATA_DIR / "models"
LOG_PATH = DATA_DIR / "localflow.log"
ENGINE_LOG_PATH = DATA_DIR / "engine.log"
VAD_MODEL = "ggml-silero-v5.1.2.bin"
SAMPLE_RATE = 16000
TAIL_SECONDS = 0.2  # keep recording briefly after release so the last word isn't clipped

DEFAULT_CONFIG = {
    "hotkey": "right ctrl",
    "mode": "hold",
    "language": "en",
    "dictionary": [],
    "model": "ggml-large-v3-turbo-q5_0.bin",
    "threads": 8,
    "beam_size": 5,
    "speed_boost": True,
    "skip_silence": True,
    "insert_method": "paste",
    "add_trailing_space": True,
    "sounds": True,
    "overlay": True,
    "min_seconds": 0.4,
    "max_seconds": 300,
    "microphone": None,
}

STATUS_TEXT = {
    "loading": "Loading speech model...",
    "ready": "Ready",
    "listening": "Listening...",
    "transcribing": "Transcribing...",
    "error": "Error - see log",
}
STATUS_COLOR = {
    "loading": "#868e96",
    "ready": "#2f9e44",
    "listening": "#e03131",
    "transcribing": "#f08c00",
    "error": "#495057",
}

log = logging.getLogger(APP_NAME)


# --------------------------------------------------------------------------------------
# Win32 bindings (own WinDLL instances so we never clash with other libraries' argtypes)
# --------------------------------------------------------------------------------------
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _fn(dll, name, restype, *argtypes):
    f = getattr(dll, name)
    f.restype = restype
    f.argtypes = argtypes
    return f


ULONG_PTR = ctypes.c_size_t
LRESULT = ctypes.c_ssize_t


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                ("Affinity", ULONG_PTR), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)

SetWindowsHookExW = _fn(user32, "SetWindowsHookExW", wt.HHOOK, ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD)
CallNextHookEx = _fn(user32, "CallNextHookEx", LRESULT, wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM)
UnhookWindowsHookEx = _fn(user32, "UnhookWindowsHookEx", wt.BOOL, wt.HHOOK)
GetMessageW = _fn(user32, "GetMessageW", wt.BOOL, ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT)
PostThreadMessageW = _fn(user32, "PostThreadMessageW", wt.BOOL, wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM)
SendInput = _fn(user32, "SendInput", wt.UINT, wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
MapVirtualKeyW = _fn(user32, "MapVirtualKeyW", wt.UINT, wt.UINT, wt.UINT)
MessageBoxW = _fn(user32, "MessageBoxW", ctypes.c_int, wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.UINT)
OpenClipboard = _fn(user32, "OpenClipboard", wt.BOOL, wt.HWND)
CloseClipboard = _fn(user32, "CloseClipboard", wt.BOOL)
EmptyClipboard = _fn(user32, "EmptyClipboard", wt.BOOL)
EnumClipboardFormats = _fn(user32, "EnumClipboardFormats", wt.UINT, wt.UINT)
GetClipboardData = _fn(user32, "GetClipboardData", wt.HANDLE, wt.UINT)
SetClipboardData = _fn(user32, "SetClipboardData", wt.HANDLE, wt.UINT, wt.HANDLE)
RegisterClipboardFormatW = _fn(user32, "RegisterClipboardFormatW", wt.UINT, wt.LPCWSTR)
GetClipboardSequenceNumber = _fn(user32, "GetClipboardSequenceNumber", wt.DWORD)
CreateWindowExW = _fn(user32, "CreateWindowExW", wt.HWND, wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                      ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                      wt.HWND, wt.HMENU, wt.HINSTANCE, ctypes.c_void_p)
DestroyWindow = _fn(user32, "DestroyWindow", wt.BOOL, wt.HWND)
GetParent = _fn(user32, "GetParent", wt.HWND, wt.HWND)
SystemParametersInfoW = _fn(user32, "SystemParametersInfoW", wt.BOOL, wt.UINT, wt.UINT, ctypes.c_void_p, wt.UINT)
_get_long = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
_set_long = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
_get_long.restype, _get_long.argtypes = ctypes.c_ssize_t, (wt.HWND, ctypes.c_int)
_set_long.restype, _set_long.argtypes = ctypes.c_ssize_t, (wt.HWND, ctypes.c_int, ctypes.c_ssize_t)

GetCurrentThreadId = _fn(kernel32, "GetCurrentThreadId", wt.DWORD)
GetModuleHandleW = _fn(kernel32, "GetModuleHandleW", wt.HMODULE, wt.LPCWSTR)
GlobalAlloc = _fn(kernel32, "GlobalAlloc", wt.HGLOBAL, wt.UINT, ctypes.c_size_t)
GlobalFree = _fn(kernel32, "GlobalFree", wt.HGLOBAL, wt.HGLOBAL)
GlobalLock = _fn(kernel32, "GlobalLock", ctypes.c_void_p, wt.HGLOBAL)
GlobalUnlock = _fn(kernel32, "GlobalUnlock", wt.BOOL, wt.HGLOBAL)
GlobalSize = _fn(kernel32, "GlobalSize", ctypes.c_size_t, wt.HGLOBAL)
CreateMutexW = _fn(kernel32, "CreateMutexW", wt.HANDLE, ctypes.c_void_p, wt.BOOL, wt.LPCWSTR)
CloseHandle = _fn(kernel32, "CloseHandle", wt.BOOL, wt.HANDLE)
CreateJobObjectW = _fn(kernel32, "CreateJobObjectW", wt.HANDLE, ctypes.c_void_p, wt.LPCWSTR)
SetInformationJobObject = _fn(kernel32, "SetInformationJobObject", wt.BOOL, wt.HANDLE, ctypes.c_int,
                              ctypes.c_void_p, wt.DWORD)
AssignProcessToJobObject = _fn(kernel32, "AssignProcessToJobObject", wt.BOOL, wt.HANDLE, wt.HANDLE)

WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_SYSKEYDOWN, WM_QUIT = 0x0100, 0x0104, 0x0012
LLKHF_INJECTED = 0x10
INPUT_KEYBOARD, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 1, 0x0002, 0x0004
VK_CONTROL, VK_RETURN, VK_ESCAPE, VK_V = 0x11, 0x0D, 0x1B, 0x56
CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002


def message_box(text: str, error: bool = False) -> None:
    MessageBoxW(None, text, APP_NAME, 0x10 if error else 0x40)


# --------------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------------
_KEY_ALIASES = {
    "control": "ctrl", "left control": "left ctrl", "right control": "right ctrl",
    "win": "windows", "left win": "left windows", "right win": "right windows",
    "alt gr": "right alt", "altgr": "right alt", "apps": "menu", "capslock": "caps lock",
    "scrolllock": "scroll lock", "escape": "esc",
}
_KEY_CODES = {
    "ctrl": {0xA2, 0xA3}, "left ctrl": {0xA2}, "right ctrl": {0xA3},
    "shift": {0xA0, 0xA1}, "left shift": {0xA0}, "right shift": {0xA1},
    "alt": {0xA4, 0xA5}, "left alt": {0xA4}, "right alt": {0xA5},
    "windows": {0x5B, 0x5C}, "left windows": {0x5B}, "right windows": {0x5C},
    "caps lock": {0x14}, "scroll lock": {0x91}, "pause": {0x13}, "insert": {0x2D},
    "menu": {0x5D}, "space": {0x20}, "tab": {0x09},
    **{f"f{i}": {0x6F + i} for i in range(1, 25)},
}


def parse_hotkey(text: str) -> list[frozenset[int]]:
    """'right ctrl' or 'ctrl+windows' -> one set of virtual-key codes per key."""
    parts = []
    for raw in str(text).lower().split("+"):
        name = _KEY_ALIASES.get(raw.strip(), raw.strip())
        if name in _KEY_CODES:
            parts.append(frozenset(_KEY_CODES[name]))
        elif len(name) == 1 and name.isalnum():
            parts.append(frozenset({ord(name.upper())}))
        else:
            raise ValueError(f"Unknown hotkey key '{raw.strip()}'")
    if not parts:
        raise ValueError("Hotkey is empty")
    return parts


def hotkey_label(text: str) -> str:
    return " + ".join(p.strip().title() for p in str(text).split("+"))


def load_config() -> tuple[dict, list[str]]:
    cfg, problems = dict(DEFAULT_CONFIG), []
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            if not isinstance(user, dict):
                raise ValueError("top level must be { ... }")
        except (OSError, ValueError) as e:
            problems.append(f"config.json could not be read ({e}); using defaults.")
        else:
            for k, v in user.items():
                if k in cfg:
                    cfg[k] = v
                elif not k.startswith("_"):
                    problems.append(f"Unknown setting '{k}' in config.json (ignored).")
    else:
        try:
            CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass

    try:
        parse_hotkey(cfg["hotkey"])
    except ValueError as e:
        problems.append(f"{e}; using {DEFAULT_CONFIG['hotkey']}.")
        cfg["hotkey"] = DEFAULT_CONFIG["hotkey"]
    for key, allowed in (("mode", ("hold", "toggle")), ("insert_method", ("paste", "type"))):
        if cfg[key] not in allowed:
            problems.append(f"'{key}' must be one of {allowed}; using {DEFAULT_CONFIG[key]}.")
            cfg[key] = DEFAULT_CONFIG[key]
    if not isinstance(cfg["dictionary"], list):
        cfg["dictionary"] = [str(cfg["dictionary"])]
    return cfg, problems


# --------------------------------------------------------------------------------------
# Speech engine: whisper.cpp server running locally, model kept loaded in RAM
# --------------------------------------------------------------------------------------
def audio_ctx_for(seconds: float) -> int:
    """Whisper's encoder always processes a 30 s window (1500 frames). For a short clip we
    only encode the part that holds audio (+ margin): ~3x faster on CPU, same text."""
    if seconds >= 28:
        return 0  # long recording: use the full window
    frames = int(seconds * 50) + 64  # 50 frames per second + ~1.3 s margin
    frames = (frames + 63) // 64 * 64
    return max(384, min(1500, frames))


def build_prompt(words) -> str:
    words = [str(w).strip() for w in words if str(w).strip()]
    return ("Vocabulary: " + ", ".join(words) + ".") if words else ""


def clean_text(text: str) -> str:
    text = re.sub(r"\[[^\]]*\]", " ", text)  # [BLANK_AUDIO], [Music], ...
    text = re.sub(r"\s+", " ", text).strip()
    if re.fullmatch(r"[\(\*]?\s*(silence|music|noise|applause|inaudible)\s*[\)\*]?\.?", text, re.I):
        return ""
    return text


def pcm_to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def multipart(fields: dict, wav: bytes) -> tuple[bytes, str]:
    boundary = "----LocalFlow" + uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        out += (f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n').encode("utf-8")
    out += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
            f"Content-Type: audio/wav\r\n\r\n").encode("utf-8")
    out += wav + f"\r\n--{boundary}--\r\n".encode("utf-8")
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _tail(path: Path, n: int = 12) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
    except OSError:
        return ""


def _kill_with_parent(proc: subprocess.Popen):
    """Put the child in a job object so Windows kills it if LocalFlow exits or crashes."""
    job = CreateJobObjectW(None, None)
    if not job:
        return None
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
    AssignProcessToJobObject(job, int(proc._handle))
    return job  # handle stays open for our lifetime


class Engine:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self.job = None
        self.ready = threading.Event()
        self.error: str | None = None
        self._lock = threading.Lock()
        self._http = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never use a proxy

    def start(self) -> None:
        exe, model = BIN_DIR / "whisper-server.exe", MODEL_DIR / self.cfg["model"]
        for p in (exe, model):
            if not p.exists():
                raise FileNotFoundError(f"{p} is missing. Run setup.ps1 first.")
        self.port = _free_port()
        args = [str(exe), "-m", str(model), "-t", str(int(self.cfg["threads"])),
                "-bs", str(int(self.cfg["beam_size"])), "-nt",
                "--host", "127.0.0.1", "--port", str(self.port)]
        vad = MODEL_DIR / VAD_MODEL
        if self.cfg["skip_silence"] and vad.exists():
            args += ["--vad", "-vm", str(vad)]
        log.info("starting engine on port %d", self.port)
        with open(ENGINE_LOG_PATH, "wb") as logf:
            self.proc = subprocess.Popen(args, cwd=BIN_DIR, stdin=subprocess.DEVNULL, stdout=logf,
                                         stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        self.job = _kill_with_parent(self.proc)

        deadline = time.monotonic() + 180
        while True:
            if self.proc.poll() is not None:
                raise RuntimeError("Speech engine stopped while loading:\n" + _tail(ENGINE_LOG_PATH))
            try:
                with self._http.open(f"http://127.0.0.1:{self.port}/", timeout=2) as r:
                    if r.status == 200:
                        break
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise TimeoutError("Speech engine did not start within 3 minutes.")
            time.sleep(0.25)
        try:  # first run allocates buffers; do it now instead of on your first dictation
            self._infer(b"\0\0" * SAMPLE_RATE, warmup=True)
        except Exception:
            log.warning("warm-up failed", exc_info=True)
        self.ready.set()
        log.info("engine ready")

    def wait_ready(self, timeout: float = 300) -> None:
        deadline = time.monotonic() + timeout
        while not self.ready.wait(0.25):
            if self.error or time.monotonic() > deadline:
                raise RuntimeError(self.error or "Speech engine is not ready.")

    def transcribe(self, pcm: bytes) -> str:
        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                log.warning("engine not running - restarting it")
                self.ready.clear()
                self.start()
            return self._infer(pcm)

    def _infer(self, pcm: bytes, warmup: bool = False) -> str:
        seconds = len(pcm) / 2 / SAMPLE_RATE
        fields = {"response_format": "json", "temperature": "0.0", "no_timestamps": "true",
                  "language": str(self.cfg["language"] or "auto")}
        prompt = build_prompt(self.cfg["dictionary"])
        if prompt and not warmup:
            fields["prompt"] = prompt
        if self.cfg["speed_boost"]:
            fields["audio_ctx"] = str(audio_ctx_for(seconds))
        if warmup:
            fields["vad"] = "false"
        body, ctype = multipart(fields, pcm_to_wav(pcm))
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/inference", data=body,
                                     headers={"Content-Type": ctype}, method="POST")
        with self._http.open(req, timeout=600) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"engine error: {data['error']}")
        return data.get("text", "") if isinstance(data, dict) else ""

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# --------------------------------------------------------------------------------------
# Microphone
# --------------------------------------------------------------------------------------
class Recorder:
    def __init__(self, device=None):
        import sounddevice  # noqa: F401  load PortAudio now so the first key press is instant
        self.device = device
        self.stream = None
        self.chunks: list[bytes] = []
        self.level = 0.0

    @property
    def active(self) -> bool:
        return self.stream is not None

    def start(self) -> None:
        import sounddevice as sd
        self.chunks, self.level = [], 0.0
        stream = sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                                   device=self.device, callback=self._callback)
        stream.start()
        self.stream = stream

    def _callback(self, indata, frames, time_info, status) -> None:
        data = bytes(indata)
        self.chunks.append(data)
        samples = array.array("h", data)
        if samples:
            self.level = max(max(samples), -min(samples)) / 32768

    def stop(self) -> bytes:
        stream, self.stream = self.stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                log.warning("closing microphone failed", exc_info=True)
        data, self.chunks = b"".join(self.chunks), []
        return data


# --------------------------------------------------------------------------------------
# Typing the text into the focused app
# --------------------------------------------------------------------------------------
def _key(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    i = INPUT(type=INPUT_KEYBOARD)
    i.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
    return i


def _send(inputs: list[INPUT]) -> None:
    arr = (INPUT * len(inputs))(*inputs)
    if SendInput(len(inputs), arr, ctypes.sizeof(INPUT)) != len(inputs):
        raise OSError(f"Windows blocked the key presses (error {ctypes.get_last_error()}).")


def send_ctrl_v() -> None:
    sc_ctrl, sc_v = MapVirtualKeyW(VK_CONTROL, 0), MapVirtualKeyW(VK_V, 0)
    _send([_key(VK_CONTROL, sc_ctrl), _key(VK_V, sc_v),
           _key(VK_V, sc_v, KEYEVENTF_KEYUP), _key(VK_CONTROL, sc_ctrl, KEYEVENTF_KEYUP)])


def type_text(text: str) -> None:
    inputs: list[INPUT] = []
    for ch in text.replace("\r\n", "\n"):
        if ch == "\n":
            inputs += [_key(VK_RETURN), _key(VK_RETURN, flags=KEYEVENTF_KEYUP)]
            continue
        units = ch.encode("utf-16-le")
        for k in range(0, len(units), 2):
            u = int.from_bytes(units[k:k + 2], "little")
            inputs += [_key(0, u, KEYEVENTF_UNICODE), _key(0, u, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)]
    for k in range(0, len(inputs), 200):
        _send(inputs[k:k + 200])


_SKIP_FORMATS = {2, 3, 9, 14, 0x80, 0x82, 0x83, 0x8E}  # GDI handles, not plain memory


class _Clipboard:
    """Open the clipboard with a hidden owner window (SetClipboardData needs one)."""

    def __enter__(self):
        self.hwnd = CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, None, None, None, None)
        for _ in range(50):
            if OpenClipboard(self.hwnd):
                return self
            time.sleep(0.02)
        DestroyWindow(self.hwnd)
        raise OSError("The clipboard is busy (another app is holding it).")

    def __exit__(self, *exc):
        CloseClipboard()
        DestroyWindow(self.hwnd)


def _clip_snapshot() -> list[tuple[int, bytes]]:
    items, fmt = [], 0
    while True:
        fmt = EnumClipboardFormats(fmt)
        if not fmt:
            return items
        if fmt in _SKIP_FORMATS or 0x200 <= fmt <= 0x3FF:
            continue
        h = GetClipboardData(fmt)
        size = GlobalSize(h) if h else 0
        p = GlobalLock(h) if size else None
        if not p:
            continue
        try:
            items.append((fmt, ctypes.string_at(p, size)))
        finally:
            GlobalUnlock(h)


def _clip_put(fmt: int, data: bytes) -> None:
    h = GlobalAlloc(GMEM_MOVEABLE, max(1, len(data)))
    if not h:
        return
    p = GlobalLock(h)
    ctypes.memmove(p, data, len(data))
    GlobalUnlock(h)
    if not SetClipboardData(fmt, h):
        GlobalFree(h)


def _clip_keep_out_of_history() -> None:
    """Ask Windows clipboard history (Win+V) and cloud sync to ignore this content."""
    _clip_put(RegisterClipboardFormatW("ExcludeClipboardContentFromMonitorProcessing"), b"\0")
    _clip_put(RegisterClipboardFormatW("CanIncludeInClipboardHistory"), (0).to_bytes(4, "little"))
    _clip_put(RegisterClipboardFormatW("CanUploadToCloudClipboard"), (0).to_bytes(4, "little"))


def paste_text(text: str) -> None:
    """Put text on the clipboard, press Ctrl+V, then put back what you had copied before."""
    with _Clipboard():
        saved = _clip_snapshot()
        EmptyClipboard()
        _clip_put(CF_UNICODETEXT, (text + "\0").encode("utf-16-le"))
        _clip_keep_out_of_history()
    ours = GetClipboardSequenceNumber()
    try:
        send_ctrl_v()
        time.sleep(0.4)  # give the target app time to read the clipboard
    finally:
        if GetClipboardSequenceNumber() == ours:  # if something else changed it meanwhile, leave it
            with _Clipboard():
                EmptyClipboard()
                for fmt, data in saved:
                    _clip_put(fmt, data)
                if saved:
                    _clip_keep_out_of_history()  # don't add a duplicate of your old item to Win+V


def insert_text(text: str, method: str) -> None:
    if method == "type":
        type_text(text)
    else:
        paste_text(text)


# --------------------------------------------------------------------------------------
# Global hotkey (low-level keyboard hook; sees exact left/right keys, ignores our own input)
# --------------------------------------------------------------------------------------
class KeyHook(threading.Thread):
    def __init__(self, callback):
        super().__init__(name="key-hook", daemon=True)
        self.callback = callback
        self.started_ok = threading.Event()
        self.failed = False
        self._tid = 0
        self._proc = HOOKPROC(self._handle)  # keep a reference or Windows calls freed memory

    def _handle(self, code, wparam, lparam):
        if code == 0:
            kb = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if not kb.flags & LLKHF_INJECTED:
                try:
                    self.callback(kb.vkCode, wparam in (WM_KEYDOWN, WM_SYSKEYDOWN))
                except Exception:
                    log.exception("key handler failed")
        return CallNextHookEx(None, code, wparam, lparam)

    def run(self):
        self._tid = GetCurrentThreadId()
        hook = SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, GetModuleHandleW(None), 0)
        self.failed = not hook
        self.started_ok.set()
        if not hook:
            log.error("keyboard hook failed (error %d)", ctypes.get_last_error())
            return
        msg = wt.MSG()
        while GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass
        UnhookWindowsHookEx(hook)

    def stop(self):
        if self._tid:
            PostThreadMessageW(self._tid, WM_QUIT, 0, 0)


# --------------------------------------------------------------------------------------
# Feedback: sounds, overlay pill, tray icon
# --------------------------------------------------------------------------------------
def _tone_file(name: str, freqs: tuple[int, ...]) -> Path:
    path = DATA_DIR / f"{name}.wav"
    if not path.exists():
        sr, frames = 22050, array.array("h")
        for f in freqs:
            n = int(sr * 0.06)
            for i in range(n):
                env = min(1.0, i / (sr * 0.004), (n - i) / (sr * 0.02))
                frames.append(int(0.16 * 32767 * env * math.sin(2 * math.pi * f * i / sr)))
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(frames.tobytes())
    return path


def play(path: Path) -> None:
    try:
        winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except RuntimeError:
        pass


def work_area() -> tuple[int, int, int, int]:
    r = wt.RECT()
    SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)  # SPI_GETWORKAREA
    return r.left, r.top, r.right, r.bottom


class Overlay:
    """Small pill above the taskbar. Never takes focus, clicks pass through it."""
    KEY = "#010203"  # this colour is made fully transparent

    def __init__(self, root, enabled: bool):
        import tkinter as tk
        self.root, self.enabled, self.visible, self.shown_state = root, enabled, False, None
        root.title(APP_NAME)
        root.overrideredirect(True)
        root.configure(bg=self.KEY)
        root.attributes("-alpha", 0.0)
        root.attributes("-topmost", True)
        root.attributes("-transparentcolor", self.KEY)
        self.s = s = root.winfo_fpixels("1i") / 96.0
        self.w, self.h = int(180 * s), int(38 * s)
        left, _top, right, bottom = work_area()
        root.geometry(f"{self.w}x{self.h}+{(left + right - self.w) // 2}+{bottom - self.h - int(28 * s)}")
        self.canvas = tk.Canvas(root, width=self.w, height=self.h, bg=self.KEY, highlightthickness=0, bd=0)
        self.canvas.pack()
        root.update_idletasks()
        hwnd = GetParent(root.winfo_id()) or root.winfo_id()
        ex = _get_long(hwnd, -20)  # GWL_EXSTYLE
        ex |= 0x08000000 | 0x80 | 0x20 | 0x80000 | 0x8  # NOACTIVATE|TOOLWINDOW|TRANSPARENT|LAYERED|TOPMOST
        _set_long(hwnd, -20, ex & ~0x40000)  # minus APPWINDOW
        if not enabled:
            root.withdraw()

    def render(self, state: str, elapsed: float = 0.0, level: float = 0.0) -> None:
        if not self.enabled:
            return
        c, s, w, h = self.canvas, self.s, self.w, self.h
        c.delete("all")
        bg = "#1c1c20"
        c.create_oval(0, 0, h, h, fill=bg, outline=bg)
        c.create_oval(w - h, 0, w, h, fill=bg, outline=bg)
        c.create_rectangle(h / 2, 0, w - h / 2, h, fill=bg, outline=bg)
        cx, cy = h / 2 + 6 * s, h / 2
        if state == "listening":
            rad, color = (4 + 5 * min(1.0, level * 4)) * s, "#ef4444"
            secs = int(elapsed)
            label = f"Listening   {secs // 60}:{secs % 60:02d}"
        else:
            rad, color, label = 5 * s, "#f59e0b", "Transcribing..."
        c.create_oval(cx - rad, cy - rad, cx + rad, cy + rad, fill=color, outline=color)
        c.create_text(cx + 16 * s, cy, text=label, anchor="w", fill="#f4f4f5", font=("Segoe UI", 10))
        if not self.visible:
            self.root.attributes("-alpha", 0.93)
            self.visible = True

    def hide(self) -> None:
        if self.visible:
            self.root.attributes("-alpha", 0.0)
            self.visible = False


def make_tray_image(color: str):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((1, 1, 63, 63), fill=color)
    d.rounded_rectangle((24, 11, 40, 37), radius=8, fill="white")
    d.arc((16, 20, 48, 46), start=0, end=180, fill="white", width=4)
    d.line((32, 46, 32, 53), fill="white", width=4)
    d.line((23, 53, 41, 53), fill="white", width=4)
    return img


# --------------------------------------------------------------------------------------
# Start with Windows (HKCU Run key - no admin needed)
# --------------------------------------------------------------------------------------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_command() -> str:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    # Google Drive may mount a little after login, so wait up to 3 minutes for the script.
    boot = ("import os,sys,time,runpy;p=sys.argv[1];"
            "[time.sleep(2) for _ in range(90) if not os.path.exists(p)];"
            "sys.argv=[p];runpy.run_path(p,run_name='__main__')")
    return f'"{pythonw}" -c "{boot}" "{Path(__file__).resolve()}"'


def autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(enable: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enable:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass


# --------------------------------------------------------------------------------------
# The app
# --------------------------------------------------------------------------------------
class App:
    def __init__(self, cfg: dict, problems: list[str]):
        self.cfg, self.problems = cfg, problems
        self.hotkey = parse_hotkey(cfg["hotkey"])
        self.hint = (f"Hold {hotkey_label(cfg['hotkey'])} and speak" if cfg["mode"] == "hold"
                     else f"Tap {hotkey_label(cfg['hotkey'])} to start / stop")
        self.engine = Engine(cfg)
        self.recorder = Recorder(cfg["microphone"])
        self.events: queue.Queue = queue.Queue()   # key events -> controller
        self.jobs: queue.Queue = queue.Queue()     # recordings -> transcriber
        self.ui: queue.Queue = queue.Queue()       # commands -> Tk thread
        self.quitting = threading.Event()
        self.tray_ready = threading.Event()
        self.tray = None
        self.lock = threading.Lock()
        self.busy = 0
        self.engine_error: str | None = None
        self.state = None
        self.tray_state = None
        # recording state (owned by the controller thread)
        self.held: set[int] = set()
        self.chord = False
        self.rec_t0 = 0.0
        self.announce_at = 0.0
        self.announced = False
        self.stop_at: float | None = None
        self.sound_start = _tone_file("start", (660, 880))
        self.sound_stop = _tone_file("stop", (880, 660))

    # ---- lifecycle -------------------------------------------------------------------
    def run(self) -> None:
        import tkinter as tk
        self.root = tk.Tk()
        self.root.report_callback_exception = lambda *a: log.error("UI error", exc_info=a)
        self.overlay = Overlay(self.root, bool(self.cfg["overlay"]))
        for target, name in ((self._run_tray, "tray"), (self._start_engine, "engine"),
                             (self._controller, "controller"), (self._transcriber, "transcriber")):
            threading.Thread(target=target, name=name, daemon=True).start()
        self.hook = KeyHook(self._on_key)
        self.hook.start()
        self.hook.started_ok.wait(5)
        if self.hook.failed:
            message_box("Could not listen for the hotkey. See the log for details.", error=True)
            return
        self.root.after(60, self._ui_tick)
        try:
            self.root.mainloop()
        finally:
            self._shutdown()

    def _shutdown(self) -> None:
        self.quitting.set()
        self.hook.stop()
        self.recorder.stop()
        self.jobs.put(None)
        self.engine.stop()
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        log.info("stopped")

    def quit(self) -> None:
        self.ui.put("quit")

    def restart(self) -> None:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--restarted"],
                         creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                         close_fds=True)
        self.quit()

    def _start_engine(self) -> None:
        t0 = time.monotonic()
        try:
            self.engine.start()
        except Exception as e:
            self.engine_error = self.engine.error = str(e)
            log.exception("engine failed to start")
            self.notify(f"Speech engine failed to start: {e}")
            return
        log.info("ready after %.1fs", time.monotonic() - t0)
        self.notify(f"Ready. {self.hint}.")

    # ---- keyboard -> recording state machine ------------------------------------------
    def _on_key(self, vk: int, down: bool) -> None:  # runs inside the hook: keep it tiny
        self.events.put((vk, down))

    def _controller(self) -> None:
        while not self.quitting.is_set():
            try:
                vk, down = self.events.get(timeout=0.03)
            except queue.Empty:
                pass
            else:
                try:
                    self._handle_key(vk, down, time.monotonic())
                except Exception:
                    log.exception("hotkey handling failed")
            try:
                self._tick(time.monotonic())
            except Exception:
                log.exception("recording tick failed")

    def _handle_key(self, vk: int, down: bool, now: float) -> None:
        part = next((i for i, keys in enumerate(self.hotkey) if vk in keys), None)
        if part is None:
            if down and vk == VK_ESCAPE and self.recorder.active:
                self._cancel("escape")
            elif down and self.held:
                # another key together with the hotkey = a normal shortcut (e.g. Ctrl+C), not dictation
                self.chord = True
                if self.cfg["mode"] == "hold" and self.recorder.active:
                    self._cancel("shortcut")
            return
        if down:
            if part in self.held:
                return  # auto-repeat while held
            self.held.add(part)
            if len(self.held) == len(self.hotkey) and not self.chord:
                self._combo_down(now)
        elif part in self.held:
            complete = len(self.held) == len(self.hotkey)
            self.held.discard(part)
            if complete and not self.chord:
                self._combo_up(now)
            if not self.held:
                self.chord = False

    def _combo_down(self, now: float) -> None:
        if self.cfg["mode"] == "hold":
            if self.recorder.active:
                self.stop_at = None  # pressed again during the release tail: keep going
            else:
                self._start(now, announce_delay=0.15)  # stay silent if this turns out to be a shortcut

    def _combo_up(self, now: float) -> None:
        if not self.recorder.active:
            if self.cfg["mode"] == "toggle":
                self._start(now, announce_delay=0)
            return
        if self.cfg["mode"] == "hold" and now - self.rec_t0 < self.cfg["min_seconds"]:
            self._cancel("too short")
        else:
            self.stop_at = now + TAIL_SECONDS

    def _start(self, now: float, announce_delay: float) -> None:
        try:
            self.recorder.start()
        except Exception as e:
            log.exception("microphone failed")
            self.notify(f"Microphone problem: {e}")
            return
        self.rec_t0, self.announce_at, self.announced, self.stop_at = now, now + announce_delay, False, None

    def _tick(self, now: float) -> None:
        if not self.recorder.active:
            return
        if not self.announced and now >= self.announce_at:
            self.announced = True
            if self.cfg["sounds"]:
                play(self.sound_start)
        if self.stop_at is not None and now >= self.stop_at:
            self._finish(now)
        elif now - self.rec_t0 > float(self.cfg["max_seconds"]):
            self.notify(f"Stopped after {self.cfg['max_seconds']} seconds (max_seconds).")
            self._finish(now)

    def _finish(self, now: float) -> None:
        duration = now - self.rec_t0 - TAIL_SECONDS
        pcm = self.recorder.stop()
        self.stop_at, self.announced = None, False
        if duration < float(self.cfg["min_seconds"]) or not pcm:
            return
        if self.cfg["sounds"]:
            play(self.sound_stop)
        with self.lock:
            self.busy += 1
        self.jobs.put(pcm)

    def _cancel(self, why: str) -> None:
        self.recorder.stop()
        self.stop_at, self.announced = None, False
        log.info("recording cancelled (%s)", why)

    # ---- transcription ---------------------------------------------------------------
    def _transcriber(self) -> None:
        while True:
            pcm = self.jobs.get()
            if pcm is None:
                return
            try:
                self.engine.wait_ready()
                t0 = time.monotonic()
                text = clean_text(self.engine.transcribe(pcm))
                log.info("%.1fs of audio -> %d words in %.2fs",
                         len(pcm) / 2 / SAMPLE_RATE, len(text.split()), time.monotonic() - t0)
                if text and not self.quitting.is_set():
                    if self.cfg["add_trailing_space"]:
                        text += " "
                    insert_text(text, self.cfg["insert_method"])
            except Exception as e:
                log.exception("transcription failed")
                self.notify(f"Transcription failed: {e}")
            finally:
                with self.lock:
                    self.busy -= 1

    # ---- UI (Tk main thread) -----------------------------------------------------------
    def current_state(self) -> str:
        if self.recorder.active and self.announced:
            return "listening"
        if self.busy:
            return "transcribing"
        if self.engine_error:
            return "error"
        return "ready" if self.engine.ready.is_set() else "loading"

    def _ui_tick(self) -> None:
        try:
            while True:
                if self.ui.get_nowait() == "quit":
                    self.root.quit()
                    return
        except queue.Empty:
            pass
        state = self.current_state()
        self.state = state
        if state != self.tray_state and self.tray_ready.is_set():
            self.tray_state = state
            self._update_tray()
        if state == "listening":
            self.overlay.render("listening", time.monotonic() - self.rec_t0, self.recorder.level)
        elif state == "transcribing":
            if self.overlay.shown_state != "transcribing":
                self.overlay.render("transcribing")
        else:
            self.overlay.hide()
        self.overlay.shown_state = state
        self.root.after(60, self._ui_tick)

    def _run_tray(self) -> None:
        import pystray
        self.tray_images = {k: make_tray_image(v) for k, v in STATUS_COLOR.items()}
        item = pystray.MenuItem
        menu = pystray.Menu(
            item(lambda _: f"{APP_NAME}: {STATUS_TEXT.get(self.state or 'loading')}", None, enabled=False),
            item(lambda _: self.hint, None, enabled=False),
            pystray.Menu.SEPARATOR,
            item("Settings", lambda: subprocess.Popen(["notepad.exe", str(CONFIG_PATH)])),
            item("Restart (apply settings)", self.restart),
            item("Start with Windows", self._toggle_autostart, checked=lambda _: autostart_enabled()),
            item("Open log folder", lambda: os.startfile(DATA_DIR)),
            pystray.Menu.SEPARATOR,
            item("Quit", self.quit),
        )
        self.tray = pystray.Icon(APP_NAME, self.tray_images["loading"], APP_NAME, menu)

        def setup(icon):
            icon.visible = True
            self.tray_ready.set()
            for p in self.problems:
                self.notify(p)

        self.tray.run(setup=setup)

    def _toggle_autostart(self) -> None:
        try:
            set_autostart(not autostart_enabled())
        except OSError as e:
            self.notify(f"Could not change autostart: {e}")
        if self.tray:
            self.tray.update_menu()

    def _update_tray(self) -> None:
        try:
            self.tray.icon = self.tray_images[self.state]
            self.tray.title = f"{APP_NAME} - {STATUS_TEXT[self.state]}"
            self.tray.update_menu()
        except Exception:
            log.debug("tray update failed", exc_info=True)

    def notify(self, message: str) -> None:
        log.info("notify: %s", message)
        if self.tray_ready.wait(5) and self.tray is not None:
            try:
                self.tray.notify(message, APP_NAME)
            except Exception:
                log.debug("notification failed", exc_info=True)


# --------------------------------------------------------------------------------------
def setup_logging() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s")
    fh = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=512_000, backupCount=1, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if sys.stderr is not None:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)
    threading.excepthook = lambda a: log.error("thread crashed", exc_info=(a.exc_type, a.exc_value, a.exc_traceback))


def single_instance(wait: bool):
    deadline = time.monotonic() + (20 if wait else 0)
    while True:
        ctypes.set_last_error(0)
        h = CreateMutexW(None, False, "Local\\LocalFlow.SingleInstance")
        if h and ctypes.get_last_error() != 183:  # ERROR_ALREADY_EXISTS
            return h
        if h:
            CloseHandle(h)
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.5)


def cli_transcribe(path: str) -> None:
    cfg, problems = load_config()
    for p in problems:
        print("config:", p)
    with wave.open(path, "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (SAMPLE_RATE, 1, 2):
            sys.exit("Need a 16 kHz, mono, 16-bit WAV file.")
        pcm = w.readframes(w.getnframes())
    engine = Engine(cfg)
    t0 = time.monotonic()
    engine.start()
    print(f"engine ready in {time.monotonic() - t0:.1f}s")
    try:
        for _ in range(2):
            t0 = time.monotonic()
            text = clean_text(engine.transcribe(pcm))
            print(f"[{time.monotonic() - t0:.2f}s for {len(pcm) / 2 / SAMPLE_RATE:.1f}s of audio] {text}")
    finally:
        engine.stop()


def main() -> None:
    setup_logging()
    if len(sys.argv) >= 3 and sys.argv[1] == "--transcribe":
        cli_transcribe(sys.argv[2])
        return
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except (OSError, AttributeError):
        pass
    mutex = single_instance(wait="--restarted" in sys.argv)
    if not mutex:
        message_box("LocalFlow is already running. Look for the microphone icon in the system tray.")
        return
    cfg, problems = load_config()
    log.info("starting (hotkey=%s, mode=%s, language=%s)", cfg["hotkey"], cfg["mode"], cfg["language"])
    try:
        App(cfg, problems).run()
    except Exception as e:
        log.exception("fatal error")
        message_box(f"LocalFlow stopped because of an error:\n\n{e}\n\nLog: {LOG_PATH}", error=True)


if __name__ == "__main__":
    main()
