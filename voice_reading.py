#!/usr/bin/env python3
"""Offline text-to-speech helper with GUI + hotkey toggle.

Main goals:
- Read selected text from anywhere with a global shortcut (e.g. Super+R).
- Keep a small GUI for read/stop state and TTS controls.
- Prefer PRIMARY selection; optional clipboard-copy fallback.
"""

from __future__ import annotations

import argparse
import bisect
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import tempfile
import wave
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import messagebox, ttk

APP_NAME = "Voice Reading"
SCRIPT_DIR = Path(__file__).resolve().parent
STATE_DIR = Path("/tmp/voice_typing")
SOCKET_PATH = "/tmp/voice_reading.sock"
LOG_PATH = STATE_DIR / "reading.log"
LOCAL_PIPER_CANDIDATES = [
    SCRIPT_DIR / "piper_runtime" / "piper" / "piper",
    SCRIPT_DIR / "piper" / "piper",
]


def resolve_config_dir() -> Path:
    candidates = [
        Path.home() / ".config" / "voice-typing",
        SCRIPT_DIR / ".voice-typing-config",
        Path("/tmp/voice-typing-config"),
    ]
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            continue
        probe = candidate / ".codex_write_probe"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return candidate
        except Exception:
            continue
    raise RuntimeError("Unable to create a writable config directory")


CONFIG_DIR = resolve_config_dir()
CONFIG_PATH = CONFIG_DIR / "reading_config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "engine": "spd-say",
    "output_module": "",
    "piper_model": "",
    "rate": 0,  # speech-dispatcher scale: -100..100
    "pitch": 0,
    "language": "en",
    "voice": "",
    "voice_type": "male1",
    "prefer_primary_selection": True,
    "capture_selected_text_via_copy": True,
    "show_panel_on_hotkey": True,
    "window_always_on_top": True,
    "window_opacity": 0.95,
    "ultra_precise_highlight_sync": False,
    "reading_preprocess_mode": "off",
    "code_math_reading_mode": False,
    "highlight_words_enabled": False,
    "highlight_sentences_enabled": True,
}

PREPROCESS_MODE_OFF = "off"
PREPROCESS_MODE_CODE_MATH = "code_math"
PREPROCESS_MODE_CODEX_OUTPUT = "codex_output"
PREPROCESS_MODE_MARKDOWN_RAW = "markdown_raw"
PREPROCESS_MODE_LABELS = {
    PREPROCESS_MODE_OFF: "Off (verbatim)",
    PREPROCESS_MODE_CODE_MATH: "Code/Math",
    PREPROCESS_MODE_CODEX_OUTPUT: "Codex output",
    PREPROCESS_MODE_MARKDOWN_RAW: "Markdown raw",
}
PREPROCESS_MODE_LABEL_TO_KEY = {label: key for key, label in PREPROCESS_MODE_LABELS.items()}
PREPROCESS_MODE_KEYS = set(PREPROCESS_MODE_LABELS.keys())

VOICE_TYPES = [
    "male1",
    "male2",
    "male3",
    "female1",
    "female2",
    "female3",
    "child_male",
    "child_female",
]


def log_line(message: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"[{ts}] {message}\n")
    except Exception:
        pass


def notify(summary: str, body: str = "", urgency: str = "normal") -> None:
    if shutil.which("notify-send") is None:
        return
    cmd = ["notify-send", "-u", urgency, summary]
    if body:
        cmd.append(body)
    try:
        subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


class ConfigManager:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._save_best_effort(dict(DEFAULT_CONFIG))
            return dict(DEFAULT_CONFIG)
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            loaded = {}
        data = dict(DEFAULT_CONFIG)
        data.update(loaded if isinstance(loaded, dict) else {})
        # Sanitize bad/legacy placeholder values.
        module = str(data.get("output_module", "")).strip()
        if module.upper() in {"OUTPUT", "MODULE", "MODULES"}:
            data["output_module"] = ""
        self._save_best_effort(data)
        return data

    def _save_best_effort(self, data: dict[str, Any]) -> None:
        try:
            self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def save(self) -> None:
        self._save_best_effort(self.data)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.save()


def _run_capture(cmd: list[str], timeout: float = 0.4) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout or "", result.stderr or ""
    except Exception as exc:
        return 1, "", str(exc)


def read_clipboard(primary: bool = False) -> str:
    if shutil.which("wl-paste") is None:
        return ""
    cmd = ["wl-paste", "--no-newline"]
    if primary:
        cmd.insert(1, "--primary")
    rc, out, _ = _run_capture(cmd, timeout=0.25)
    if rc != 0:
        return ""
    return out.strip()


def write_clipboard(text: str) -> bool:
    if shutil.which("wl-copy") is None:
        return False
    try:
        proc = subprocess.Popen(
            ["wl-copy", "--type", "text/plain;charset=utf-8"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        if proc.stdin is not None:
            proc.stdin.write(text)
            proc.stdin.close()
        proc.wait(timeout=0.4)
        return proc.returncode == 0
    except Exception:
        return False


def _ydotool_key(seq: list[str]) -> bool:
    if shutil.which("ydotool") is None:
        return False
    env = os.environ.copy()
    if not env.get("YDOTOOL_SOCKET"):
        default_socket = f"/run/user/{os.getuid()}/.ydotool_socket"
        if os.path.exists(default_socket):
            env["YDOTOOL_SOCKET"] = default_socket
        elif os.path.exists("/tmp/.ydotool_socket"):
            env["YDOTOOL_SOCKET"] = "/tmp/.ydotool_socket"
    try:
        rc = subprocess.run(
            ["ydotool", "key", *seq],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=0.5,
            env=env,
        ).returncode
        return rc == 0
    except Exception:
        return False


def capture_selection_by_copy() -> str:
    old_clip = read_clipboard(primary=False)

    # Ctrl+C, then Ctrl+Shift+C fallback for terminals.
    sequences = [
        ["29:1", "46:1", "46:0", "29:0"],
        ["29:1", "42:1", "46:1", "46:0", "42:0", "29:0"],
    ]
    for seq in sequences:
        if not _ydotool_key(seq):
            continue
        time.sleep(0.07)
        new_clip = read_clipboard(primary=False)
        if new_clip and new_clip != old_clip:
            if old_clip:
                write_clipboard(old_clip)
            return new_clip
    if old_clip:
        write_clipboard(old_clip)
    return ""


def get_selected_text(config: ConfigManager) -> tuple[str, str]:
    if bool(config.get("prefer_primary_selection", True)):
        primary = read_clipboard(primary=True)
        if primary:
            return primary, "primary"
    if bool(config.get("capture_selected_text_via_copy", True)):
        copied = capture_selection_by_copy()
        if copied:
            return copied, "copy"
    clip = read_clipboard(primary=False)
    if clip:
        return clip, "clipboard"
    return "", "none"


def _line_is_code_heavy(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith(("```", ">>>", "$ ")):
        return True
    symbol_hits = sum(stripped.count(ch) for ch in "{}[]()<>;=:+-*/\\|")
    if symbol_hits >= 5 and len(stripped) >= 24:
        return True
    lowered = stripped.lower()
    code_prefixes = (
        "def ",
        "class ",
        "function ",
        "import ",
        "from ",
        "return ",
        "public ",
        "private ",
        "const ",
        "let ",
        "var ",
        "if ",
        "for ",
        "while ",
        "#include ",
    )
    return lowered.startswith(code_prefixes) and symbol_hits >= 2


def _append_line_end_punctuation(text: str) -> tuple[str, bool]:
    lines = text.splitlines()
    changed = False
    out: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            out.append("")
            continue
        if line[-1] not in ".!?":
            line = f"{line}."
            changed = True
        out.append(line)
    return "\n".join(out).strip(), changed


def preprocess_for_code_math_mode(text: str, enabled: bool) -> tuple[str, bool]:
    clean = text.strip()
    if not enabled or not clean:
        return clean, False

    changed = False
    out = clean

    # Remove large fenced code blocks completely for "code-less" reading.
    out, n = re.subn(r"```[\s\S]*?```", " code block omitted. ", out)
    changed = changed or n > 0

    # Replace URLs with compact verbal form.
    def _url_repl(m: re.Match[str]) -> str:
        url = m.group(0)
        domain = re.sub(r"^https?://", "", url).split("/")[0]
        return f" link {domain} "

    out, n = re.subn(r"https?://[^\s)]+", _url_repl, out)
    changed = changed or n > 0

    # Replace POSIX/Windows paths with a short spoken form.
    def _path_repl(m: re.Match[str]) -> str:
        raw = m.group(0)
        token = raw.rstrip(".,;:)")
        base = Path(token.replace("\\", "/")).name or "path"
        return f" path to {base} "

    out, n = re.subn(r"(?<!\w)(?:~|/|\./|\../)[A-Za-z0-9._/\-]+", _path_repl, out)
    changed = changed or n > 0
    out, n = re.subn(r"(?<!\w)[A-Za-z]:\\[^\s,;:)]*", _path_repl, out)
    changed = changed or n > 0

    # Common programming/math operators to speech-friendly words.
    replacements = [
        ("!==", " strictly not equal "),
        ("===", " strictly equal "),
        ("::", " scope "),
        ("->", " arrow "),
        ("=>", " maps to "),
        (">=", " greater or equal "),
        ("<=", " less or equal "),
        ("!=", " not equal "),
        ("==", " equals "),
        ("&&", " and "),
        ("||", " or "),
        ("++", " plus plus "),
        ("--", " minus minus "),
    ]
    for src, dst in replacements:
        if src in out:
            out = out.replace(src, dst)
            changed = True

    # Simple math speech helpers.
    out, n = re.subn(r"\b([A-Za-z])\s*\^\s*2\b", r"\1 squared", out)
    changed = changed or n > 0
    out, n = re.subn(r"\b([A-Za-z])\s*\^\s*3\b", r"\1 cubed", out)
    changed = changed or n > 0
    out, n = re.subn(r"(?<=\d)\s*/\s*(?=\d)", " over ", out)
    changed = changed or n > 0

    # Turn snake_case tokens into natural words.
    def _snake_repl(m: re.Match[str]) -> str:
        return m.group(0).replace("_", " ")

    out, n = re.subn(r"\b[A-Za-z]+(?:_[A-Za-z0-9]+)+\b", _snake_repl, out)
    changed = changed or n > 0

    # Aggressively collapse code-heavy lines.
    lines = out.splitlines()
    collapsed: list[str] = []
    for line in lines:
        if _line_is_code_heavy(line):
            collapsed.append("code line omitted.")
            changed = True
        else:
            collapsed.append(line)
    out = "\n".join(collapsed)

    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out, changed


def preprocess_for_codex_output_mode(text: str, enabled: bool) -> tuple[str, bool]:
    clean = text.strip()
    if not enabled or not clean:
        return clean, False

    changed = False
    out = clean

    # Strip fenced code blocks from long summaries.
    out, n = re.subn(r"```[\s\S]*?```", " code block omitted. ", out)
    changed = changed or n > 0

    # Keep line numbers but drop heavy path noise: `src/file.py:42` -> `line 42`.
    def _file_ref_repl(m: re.Match[str]) -> str:
        line = m.group(1)
        return f" line {line} "

    out, n = re.subn(
        r"`?(?:[A-Za-z]:\\|~?/|\.{1,2}/)?[A-Za-z0-9._/\-]+\.[A-Za-z0-9_+\-]+:(\d+)(?::\d+)?`?",
        _file_ref_repl,
        out,
    )
    changed = changed or n > 0

    # Drop standalone path mentions that are not useful when listening.
    out, n = re.subn(r"`?(?:[A-Za-z]:\\|~?/|\.{1,2}/)[^\s`]+`?", " file path ", out)
    changed = changed or n > 0

    # Parenthesized location hints are usually noisy in spoken summaries.
    out, n = re.subn(r"\(\s*(?:line\s+\d+|L\d+|C\d+|[^)]*?:\d+(?::\d+)?)\s*\)", " ", out, flags=re.I)
    changed = changed or n > 0

    # Clean markdown-style formatting artifacts from assistant output.
    out, n = re.subn(r"`([^`]+)`", r"\1", out)
    changed = changed or n > 0
    out, n = re.subn(r"\*\*([^*]+)\*\*", r"\1", out)
    changed = changed or n > 0
    out, n = re.subn(r"^\s*[-*+]\s+", "", out, flags=re.M)
    changed = changed or n > 0
    out, n = re.subn(r"^\s*\d+\.\s+", "", out, flags=re.M)
    changed = changed or n > 0

    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"\s+([,.;:!?])", r"\1", out)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    with_periods, punct_changed = _append_line_end_punctuation(out)
    changed = changed or punct_changed
    return with_periods, changed


def preprocess_for_markdown_raw_mode(text: str, enabled: bool) -> tuple[str, bool]:
    clean = text.strip()
    if not enabled or not clean:
        return clean, False

    changed = False
    out = clean

    # Remove fenced block delimiters while keeping content readable.
    out, n = re.subn(r"^```[^\n]*\n?", "", out, flags=re.M)
    changed = changed or n > 0
    out, n = re.subn(r"^```\s*$", "", out, flags=re.M)
    changed = changed or n > 0

    # Links/images: keep visible text, drop URL.
    out, n = re.subn(r"!\[([^\]]*)\]\([^)]+\)", r"\1", out)
    changed = changed or n > 0
    out, n = re.subn(r"\[([^\]]+)\]\([^)]+\)", r"\1", out)
    changed = changed or n > 0

    # Headings, blockquotes, and list markers.
    out, n = re.subn(r"^\s{0,3}#{1,6}\s*", "", out, flags=re.M)
    changed = changed or n > 0
    out, n = re.subn(r"^\s*>\s?", "", out, flags=re.M)
    changed = changed or n > 0
    out, n = re.subn(r"^\s*[-*+]\s+", "", out, flags=re.M)
    changed = changed or n > 0
    out, n = re.subn(r"^\s*\d+\.\s+", "", out, flags=re.M)
    changed = changed or n > 0

    # Drop table separators and flatten table row pipes.
    out, n = re.subn(r"^\s*\|?[-: ]+\|[-|: ]+\s*$", "", out, flags=re.M)
    changed = changed or n > 0
    out, n = re.subn(r"\|", " ", out)
    changed = changed or n > 0

    # Remove emphasis/code markers while preserving content.
    for pattern in [
        (r"`([^`]+)`", r"\1"),
        (r"\*\*([^*]+)\*\*", r"\1"),
        (r"__([^_]+)__", r"\1"),
        (r"\*([^*]+)\*", r"\1"),
        (r"_([^_]+)_", r"\1"),
        (r"~~([^~]+)~~", r"\1"),
    ]:
        out, n = re.subn(pattern[0], pattern[1], out)
        changed = changed or n > 0

    # Remove horizontal rule lines.
    out, n = re.subn(r"^\s*([-*_]\s*){3,}\s*$", "", out, flags=re.M)
    changed = changed or n > 0

    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"\s+([,.;:!?])", r"\1", out)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    with_periods, punct_changed = _append_line_end_punctuation(out)
    changed = changed or punct_changed
    return with_periods, changed


def resolve_preprocess_mode(config: "ConfigManager") -> str:
    raw = str(config.get("reading_preprocess_mode", "")).strip().lower()
    if raw in PREPROCESS_MODE_KEYS:
        return raw
    return PREPROCESS_MODE_CODE_MATH if bool(config.get("code_math_reading_mode", False)) else PREPROCESS_MODE_OFF


def preprocess_text_for_mode(text: str, mode: str) -> tuple[str, bool, str]:
    normalized = mode if mode in PREPROCESS_MODE_KEYS else PREPROCESS_MODE_OFF
    if normalized == PREPROCESS_MODE_CODE_MATH:
        processed, changed = preprocess_for_code_math_mode(text, True)
        return processed, changed, normalized
    if normalized == PREPROCESS_MODE_CODEX_OUTPUT:
        processed, changed = preprocess_for_codex_output_mode(text, True)
        return processed, changed, normalized
    if normalized == PREPROCESS_MODE_MARKDOWN_RAW:
        processed, changed = preprocess_for_markdown_raw_mode(text, True)
        return processed, changed, normalized
    return text.strip(), False, PREPROCESS_MODE_OFF


def split_sentences_for_timing(text: str) -> list[str]:
    clean = text.strip()
    if not clean:
        return []
    parts = re.findall(r"[^.!?]+[.!?]*", clean, flags=re.S)
    return [p.strip() for p in parts if p.strip()]


def detect_engines() -> list[str]:
    engines: list[str] = []
    if shutil.which("spd-say"):
        engines.append("spd-say")
    if resolve_piper_binary():
        engines.append("piper")
    if shutil.which("espeak-ng"):
        engines.append("espeak-ng")
    if shutil.which("espeak"):
        engines.append("espeak")
    return engines


def resolve_piper_binary() -> str:
    path_bin = shutil.which("piper")
    if path_bin:
        return path_bin
    for candidate in LOCAL_PIPER_CANDIDATES:
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return ""


def list_spd_module_files() -> list[str]:
    modules_dir = Path("/etc/speech-dispatcher/modules")
    if not modules_dir.exists():
        return []
    modules: list[str] = []
    for entry in sorted(modules_dir.glob("*.conf")):
        name = entry.stem.strip()
        if not name:
            continue
        modules.append(name)
    return modules


def list_spd_output_modules() -> list[str]:
    if shutil.which("spd-say") is None:
        return []
    rc, out, _ = _run_capture(["spd-say", "-O"], timeout=1.0)
    if rc != 0:
        return list_spd_module_files()
    modules: list[str] = []
    for raw in out.splitlines():
        line = raw.strip()
        if not line:
            continue
        token = line.split()[0]
        if not token:
            continue
        # Ignore headers like "OUTPUT" or separators.
        if token.upper() in {"OUTPUT", "MODULE", "MODULES"}:
            continue
        if set(token) <= {"-", "="}:
            continue
        modules.append(token)
    # Deduplicate while preserving order.
    seen: set[str] = set()
    ordered: list[str] = []
    for mod in modules:
        if mod in seen:
            continue
        seen.add(mod)
        ordered.append(mod)
    if ordered:
        return ordered
    return list_spd_module_files()


def list_spd_synthesis_voices(output_module: str = "") -> list[str]:
    if shutil.which("spd-say") is None:
        return []
    cmd = ["spd-say"]
    if output_module:
        cmd.extend(["-o", output_module])
    cmd.append("-L")
    rc, out, _ = _run_capture(cmd, timeout=1.0)
    if rc != 0:
        return []
    voices: list[str] = []
    for raw in out.splitlines():
        line = raw.strip()
        if not line:
            continue
        token = line.split()[0]
        if not token:
            continue
        if token.upper() in {"NAME", "VOICE", "VOICES"}:
            continue
        if set(token) <= {"-", "="}:
            continue
        voices.append(token)
    seen: set[str] = set()
    ordered: list[str] = []
    for voice in voices:
        if voice in seen:
            continue
        seen.add(voice)
        ordered.append(voice)
    return ordered


def pick_preferred_rhvoice_voice(voices: list[str]) -> str:
    if not voices:
        return ""
    lowered = {v.lower(): v for v in voices}
    for preferred in ["clb", "bdl", "alan", "slt"]:
        if preferred in lowered:
            return lowered[preferred]
    return voices[0]


def list_rhvoice_voices() -> list[str]:
    voices_root = Path("/usr/share/RHVoice/voices")
    if not voices_root.exists():
        return []
    names = sorted(p.name for p in voices_root.iterdir() if p.is_dir())
    return names


def list_piper_model_paths() -> list[str]:
    search_roots = [
        Path.home() / ".local" / "share" / "piper" / "voices",
        Path.home() / ".local" / "share" / "piper",
        Path.home() / ".cache" / "piper",
        Path("/usr/share/piper/voices"),
        Path("/usr/share/piper"),
        Path("/usr/share/piper-voices"),
        Path("/opt/piper/voices"),
        SCRIPT_DIR / "piper_voices",
    ]
    found: list[str] = []
    seen: set[str] = set()
    for root in search_roots:
        if not root.exists():
            continue
        try:
            for entry in root.rglob("*.onnx"):
                try:
                    abs_path = str(entry.resolve())
                except Exception:
                    abs_path = str(entry)
                if abs_path in seen:
                    continue
                seen.add(abs_path)
                found.append(abs_path)
        except Exception:
            continue
    found.sort(key=lambda p: (Path(p).name.lower(), p.lower()))
    return found


def format_piper_model_label(path_str: str) -> str:
    path = Path(path_str)
    base = path.stem
    parent = path.parent.name
    if parent and parent != ".":
        return f"{base} ({parent})"
    return base


def list_audio_player_commands(wav_path: str) -> list[list[str]]:
    commands: list[list[str]] = []
    if shutil.which("pw-play"):
        commands.append(["pw-play", wav_path])
    if shutil.which("aplay"):
        commands.append(["aplay", "-q", wav_path])
    if shutil.which("paplay"):
        commands.append(["paplay", wav_path])
    if shutil.which("ffplay"):
        commands.append(["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", wav_path])
    if shutil.which("mpv"):
        commands.append(["mpv", "--really-quiet", "--no-video", wav_path])
    return commands


def choose_preferred_piper_model(model_paths: list[str]) -> str:
    if not model_paths:
        return ""
    priorities = [
        "en_GB-cori-high",
        "en_GB-cori-medium",
        "en_GB-alba-medium",
        "en_GB-aru-medium",
    ]
    lower_map = {p.lower(): p for p in model_paths}
    for pref in priorities:
        for p in model_paths:
            if pref.lower() in p.lower():
                return p
    # Prefer higher quality markers if present.
    for marker in ["-high.onnx", "/high/", "-medium.onnx", "/medium/"]:
        for p in model_paths:
            if marker in p.lower():
                return p
    return model_paths[0]


class ReaderController:
    def __init__(
        self,
        config: ConfigManager,
        on_state_change: Callable[[bool, str], None] | None = None,
        on_text: Callable[[str, str], None] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> None:
        self.config = config
        self.on_state_change = on_state_change
        self.on_text = on_text
        self.on_progress = on_progress
        self._lock = threading.Lock()
        self._speaking = False
        self._proc: subprocess.Popen[str] | None = None
        self._last_source = "none"
        self._paused = False
        self._resume_text = ""
        self._resume_word_index = 0
        self._full_words: list[str] = []
        self._word_total = 0
        self._word_index = -1
        self._cancel_reason = ""
        self._cancel_state_pre_emitted = False
        self._progress_stop = threading.Event()
        self._progress_thread: threading.Thread | None = None

    def is_speaking(self) -> bool:
        with self._lock:
            return self._speaking

    def is_paused(self) -> bool:
        with self._lock:
            return self._paused

    def read_selection_async(self) -> tuple[bool, str]:
        with self._lock:
            if self._speaking:
                return False, "Already reading"

        text, source = get_selected_text(self.config)
        if not text:
            return False, "No selected text found"
        preprocess_mode = resolve_preprocess_mode(self.config)
        processed, changed, mode_used = preprocess_text_for_mode(text, preprocess_mode)
        speak_text = processed if processed else text
        words = re.findall(r"\S+", speak_text)
        with self._lock:
            self._paused = False
            self._resume_text = ""
            self._resume_word_index = 0
            self._full_words = words
            self._word_total = len(words)
            self._word_index = 0 if words else -1
        self._last_source = source
        if mode_used == PREPROCESS_MODE_OFF:
            ui_source = source
        elif changed:
            ui_source = f"{source}+{mode_used}"
        else:
            ui_source = f"{source}+{mode_used}(no_change)"
        self._emit_text(speak_text, ui_source)
        if words:
            self._emit_progress(0, len(words))
        return self._start_async(speak_text, start_word_index=0)

    def _start_async(self, text: str, start_word_index: int = 0) -> tuple[bool, str]:
        with self._lock:
            if self._speaking:
                return False, "Already reading"
            self._speaking = True
            self._paused = False
            self._cancel_reason = ""
            self._cancel_state_pre_emitted = False
        self._emit_state(True, "reading")
        threading.Thread(target=self._worker, args=(text, start_word_index), daemon=True).start()
        return True, "Reading started"

    def stop(self) -> tuple[bool, str]:
        with self._lock:
            speaking = self._speaking
            proc = self._proc
        if not speaking:
            return False, "Not reading"

        self._progress_stop.set()
        try:
            if shutil.which("spd-say"):
                subprocess.run(["spd-say", "-S"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
        with self._lock:
            self._speaking = False
            self._proc = None
            self._paused = False
            self._resume_text = ""
            self._resume_word_index = 0
            self._cancel_reason = "stopped"
            self._cancel_state_pre_emitted = True
        self._emit_state(False, "stopped")
        return True, "Stopped"

    def toggle(self) -> tuple[bool, str]:
        if self.is_speaking():
            return self.stop()
        return self.read_selection_async()

    def pause_or_resume(self) -> tuple[bool, str]:
        if self.is_speaking():
            return self.pause()
        return self.resume()

    def pause(self) -> tuple[bool, str]:
        with self._lock:
            speaking = self._speaking
            proc = self._proc
            total_words = self._word_total
            word_index = self._word_index
            words = list(self._full_words)
        if not speaking:
            return False, "Not reading"

        self._progress_stop.set()
        try:
            if shutil.which("spd-say"):
                subprocess.run(["spd-say", "-S"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass

        next_index = min(total_words, max(0, word_index + 1))
        remaining = words[next_index:] if words else []
        with self._lock:
            self._speaking = False
            self._proc = None
            self._paused = bool(remaining)
            self._resume_word_index = next_index
            self._resume_text = " ".join(remaining) if remaining else ""
            self._cancel_reason = "paused"
            self._cancel_state_pre_emitted = True
        if remaining:
            self._emit_state(False, "paused")
            self._emit_progress(next_index, total_words)
            return True, "Paused"
        self._emit_state(False, "done")
        return True, "Done"

    def resume(self) -> tuple[bool, str]:
        with self._lock:
            if self._speaking:
                return False, "Already reading"
            if not self._paused:
                return False, "Not paused"
            resume_text = self._resume_text
            start_idx = self._resume_word_index
        if not resume_text.strip():
            return False, "Nothing to resume"
        return self._start_async(resume_text, start_word_index=start_idx)

    def _estimate_words_per_minute(self, engine: str, rate: int, piper_length_scale: float) -> float:
        base = max(90, min(420, 175 + (rate * 2)))
        if engine == "piper":
            return max(80.0, min(500.0, float(base) / max(0.35, piper_length_scale)))
        return float(base)

    def _start_progress_tracker(
        self,
        start_index: int,
        total_words: int,
        wpm: float,
        total_duration_sec: float | None = None,
        word_offsets_sec: list[float] | None = None,
        start_delay_sec: float = 0.0,
    ) -> None:
        self._progress_stop.set()
        self._progress_stop = threading.Event()
        if total_words <= 0:
            return

        def _run() -> None:
            remaining_words = max(1, total_words - max(0, start_index))
            offsets = list(word_offsets_sec) if word_offsets_sec else []
            if not offsets:
                if total_duration_sec is not None and total_duration_sec > 0:
                    sec_per_word = max(0.02, float(total_duration_sec) / float(remaining_words))
                else:
                    sec_per_word = max(0.05, 60.0 / max(60.0, wpm))
            else:
                sec_per_word = 0.0
            started = time.monotonic()
            last_idx = -1
            while not self._progress_stop.wait(0.08):
                with self._lock:
                    speaking = self._speaking
                if not speaking:
                    break
                elapsed = max(0.0, time.monotonic() - started - max(0.0, start_delay_sec))
                if offsets:
                    rel_idx = bisect.bisect_right(offsets, elapsed) - 1
                    rel_idx = max(0, min(remaining_words - 1, rel_idx))
                    idx = min(total_words - 1, start_index + rel_idx)
                else:
                    idx = min(total_words - 1, start_index + int(elapsed / sec_per_word))
                if idx != last_idx:
                    with self._lock:
                        self._word_index = idx
                    self._emit_progress(idx, total_words)
                    last_idx = idx

        self._progress_thread = threading.Thread(target=_run, daemon=True)
        self._progress_thread.start()

    def _estimate_word_weight(self, token: str) -> float:
        cleaned = re.sub(r"[^A-Za-z]", "", token).lower()
        vowel_groups = re.findall(r"[aeiouy]+", cleaned)
        syllables = max(1, len(vowel_groups)) if cleaned else 1
        weight = float(syllables)
        if len(cleaned) >= 8:
            weight += 0.25
        if token.endswith((".", "!", "?")):
            weight += 0.75
        elif token.endswith((",", ";", ":")):
            weight += 0.35
        return max(0.4, weight)

    def _estimate_word_offsets(
        self,
        words: list[str],
        total_duration_sec: float,
    ) -> list[float]:
        if not words:
            return []
        duration = max(0.02, float(total_duration_sec))
        weights = [self._estimate_word_weight(w) for w in words]
        total_weight = sum(weights)
        if total_weight <= 0:
            step = duration / float(len(words))
            return [i * step for i in range(len(words))]
        scale = duration / total_weight
        offsets: list[float] = []
        acc = 0.0
        for w in weights:
            offsets.append(acc)
            acc += w * scale
        return offsets

    def _estimate_word_offsets_by_sentence(
        self,
        text: str,
        total_words: list[str],
        sentence_durations: list[float],
    ) -> list[float]:
        if not total_words or not sentence_durations:
            return []
        sentences = split_sentences_for_timing(text)
        if not sentences:
            return []
        pair_count = min(len(sentences), len(sentence_durations))
        if pair_count <= 0:
            return []
        offsets: list[float] = []
        abs_t = 0.0
        word_cursor = 0
        for idx in range(pair_count):
            sent = sentences[idx]
            dur = max(0.02, float(sentence_durations[idx]))
            sent_words = re.findall(r"\S+", sent)
            if not sent_words:
                abs_t += dur
                continue
            local = self._estimate_word_offsets(sent_words, dur)
            for local_t in local:
                if word_cursor >= len(total_words):
                    break
                offsets.append(abs_t + local_t)
                word_cursor += 1
            abs_t += dur
            if word_cursor >= len(total_words):
                break
        if word_cursor < len(total_words):
            rem = len(total_words) - word_cursor
            step = 0.16
            for i in range(rem):
                offsets.append(abs_t + (i * step))
        return offsets[: len(total_words)]

    def _parse_piper_sentence_durations(self, stderr_text: str) -> list[float]:
        if not stderr_text:
            return []
        vals: list[float] = []
        for line in stderr_text.splitlines():
            m = re.search(r"Synthesized\s+([0-9]+(?:\.[0-9]+)?)\s+second\(s\)\s+of\s+audio", line)
            if not m:
                continue
            try:
                vals.append(float(m.group(1)))
            except Exception:
                continue
        return vals

    def _wav_duration_seconds(self, wav_path: str) -> float | None:
        try:
            with wave.open(wav_path, "rb") as wf:
                rate = float(wf.getframerate() or 0)
                frames = float(wf.getnframes() or 0)
                if rate <= 0:
                    return None
                return frames / rate
        except Exception:
            return None

    def _worker(self, text: str, start_word_index: int) -> None:
        engine = str(self.config.get("engine", "spd-say")).strip() or "spd-say"
        output_module = str(self.config.get("output_module", "")).strip()
        piper_model = str(self.config.get("piper_model", "")).strip()
        rate = int(self.config.get("rate", 0))
        pitch = int(self.config.get("pitch", 0))
        language = str(self.config.get("language", "en")).strip()
        voice = str(self.config.get("voice", "")).strip()
        voice_type = str(self.config.get("voice_type", "male1")).strip()
        ultra_sync = bool(self.config.get("ultra_precise_highlight_sync", False))

        rc = 1
        err_text = ""
        proc: subprocess.Popen[str] | None = None
        proc_input: str | None = None
        wav_to_remove: str | None = None
        already_waited = False
        cancelled_reason = ""
        cancel_state_pre_emitted = False
        length_scale = 1.0
        with self._lock:
            total_words = self._word_total
            full_words = list(self._full_words)
        if total_words <= 0:
            full_words = re.findall(r"\S+", text)
            total_words = len(full_words)
        remaining_words = full_words[max(0, start_word_index):]
        if engine != "piper":
            wpm_est = self._estimate_words_per_minute(engine, rate, piper_length_scale=length_scale)
            est_duration = max(0.05, len(remaining_words) * (60.0 / max(60.0, wpm_est)))
            offsets = self._estimate_word_offsets(remaining_words, est_duration) if ultra_sync else []
            self._start_progress_tracker(
                start_index=max(0, start_word_index),
                total_words=total_words,
                wpm=wpm_est,
                total_duration_sec=est_duration,
                word_offsets_sec=offsets,
                start_delay_sec=0.06 if ultra_sync else 0.0,
            )
        try:
            if engine == "spd-say" and shutil.which("spd-say"):
                cmd = ["spd-say", "-w", "-r", str(rate), "-p", str(pitch), "-N", "voice-reading", "-n", "main"]
                if output_module:
                    cmd.extend(["-o", output_module])
                if language:
                    cmd.extend(["-l", language])
                if voice_type:
                    cmd.extend(["-t", voice_type])
                if voice:
                    cmd.extend(["-y", voice])
                cmd.append(text)
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            elif engine in {"espeak-ng", "espeak"} and shutil.which(engine):
                # Map -100..100 onto a useful words-per-minute range.
                wpm = max(90, min(420, 175 + (rate * 2)))
                cmd = [engine, "-s", str(wpm), "-p", str(max(0, min(99, 50 + pitch // 2))), "--stdin"]
                if voice:
                    cmd.extend(["-v", voice])
                elif language:
                    cmd.extend(["-v", language])
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                proc_input = text
            elif engine == "piper":
                piper_bin = resolve_piper_binary()
                if not piper_bin:
                    err_text = "Piper engine not found"
                    proc = None
                else:
                    models = list_piper_model_paths()
                    selected_model = piper_model if piper_model and Path(piper_model).exists() else choose_preferred_piper_model(models)
                    if not selected_model:
                        err_text = "No Piper model found. Add a .onnx voice model."
                    else:
                        # lower length_scale => faster; keep range conservative for intelligibility
                        length_scale = max(0.60, min(1.60, 1.00 - (rate / 220.0)))
                        with tempfile.NamedTemporaryFile(prefix="voice_reading_", suffix=".wav", delete=False) as tmp:
                            wav_to_remove = tmp.name
                        cmd = [
                            piper_bin,
                            "--model",
                            selected_model,
                            "--output_file",
                            wav_to_remove,
                            "--length_scale",
                            f"{length_scale:.3f}",
                        ]
                        if ultra_sync:
                            cmd.append("--debug")
                        else:
                            cmd.append("--quiet")
                        proc = subprocess.Popen(
                            cmd,
                            stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE,
                            text=True,
                        )
                        with self._lock:
                            self._proc = proc
                        proc_input = text
                        _, stderr_text = proc.communicate(input=proc_input, timeout=None)
                        proc_input = None
                        rc = proc.returncode
                        err_text = (stderr_text or "").strip()
                        already_waited = True
                        if rc == 0:
                            wav_duration = self._wav_duration_seconds(wav_to_remove) if wav_to_remove else None
                            sentence_count = len(re.findall(r"[.!?]+", text))
                            if wav_duration is not None and sentence_count > 0:
                                wav_duration = max(0.05, wav_duration - (0.16 * sentence_count))
                            sentence_durs = self._parse_piper_sentence_durations(err_text) if ultra_sync else []
                            if ultra_sync and sentence_durs:
                                offsets = self._estimate_word_offsets_by_sentence(
                                    text=text,
                                    total_words=remaining_words,
                                    sentence_durations=sentence_durs,
                                )
                            elif ultra_sync and wav_duration is not None and wav_duration > 0:
                                offsets = self._estimate_word_offsets(remaining_words, wav_duration)
                            else:
                                offsets = []
                            self._start_progress_tracker(
                                start_index=max(0, start_word_index),
                                total_words=total_words,
                                wpm=self._estimate_words_per_minute(engine, rate, piper_length_scale=length_scale),
                                total_duration_sec=wav_duration,
                                word_offsets_sec=offsets,
                                start_delay_sec=0.12 if ultra_sync else 0.04,
                            )
                            player_cmds = list_audio_player_commands(wav_to_remove)
                            if not player_cmds:
                                rc = 1
                                err_text = "No audio player found (pw-play/aplay/paplay/ffplay/mpv)"
                            else:
                                last_error = ""
                                played = False
                                for player_cmd in player_cmds:
                                    proc = subprocess.Popen(
                                        player_cmd,
                                        stdout=subprocess.DEVNULL,
                                        stderr=subprocess.PIPE,
                                        text=True,
                                    )
                                    with self._lock:
                                        self._proc = proc
                                    _, play_stderr = proc.communicate(timeout=None)
                                    already_waited = True
                                    if proc.returncode == 0:
                                        rc = 0
                                        err_text = ""
                                        played = True
                                        break
                                    last_error = (play_stderr or "").strip() or f"{player_cmd[0]} failed"
                                if not played:
                                    rc = 1
                                    err_text = last_error
            else:
                err_text = f"TTS engine not available: {engine}"

            with self._lock:
                self._proc = proc

            if proc is not None and not already_waited:
                _, stderr_text = proc.communicate(input=proc_input, timeout=None)
                rc = proc.returncode
                err_text = (stderr_text or "").strip()
        except Exception as exc:
            err_text = str(exc)
        finally:
            self._progress_stop.set()
            if wav_to_remove:
                try:
                    os.unlink(wav_to_remove)
                except Exception:
                    pass
            with self._lock:
                cancelled_reason = self._cancel_reason
                cancel_state_pre_emitted = self._cancel_state_pre_emitted
                self._speaking = False
                self._proc = None
                self._cancel_reason = ""
                self._cancel_state_pre_emitted = False

        if cancelled_reason:
            if not cancel_state_pre_emitted:
                self._emit_state(False, cancelled_reason)
        elif rc == 0:
            if total_words > 0:
                with self._lock:
                    self._word_index = total_words - 1
                self._emit_progress(total_words - 1, total_words)
            self._emit_state(False, "done")
        else:
            detail = err_text or "TTS failed"
            log_line(f"tts failed (engine={engine}, source={self._last_source}, detail={detail!r})")
            notify("Voice Reading", detail, urgency="critical")
            self._emit_state(False, "error")

    def _emit_state(self, speaking: bool, reason: str) -> None:
        if self.on_state_change is not None:
            self.on_state_change(speaking, reason)

    def _emit_text(self, text: str, source: str) -> None:
        if self.on_text is not None:
            self.on_text(text, source)

    def _emit_progress(self, index: int, total: int) -> None:
        if self.on_progress is not None:
            self.on_progress(index, total)


class IpcServer(threading.Thread):
    def __init__(
        self,
        controller: ReaderController,
        config: ConfigManager,
        on_show: Callable[[bool], None] | None = None,
    ) -> None:
        super().__init__(daemon=True)
        self.controller = controller
        self.config = config
        self.on_show = on_show
        self._socket: socket.socket | None = None
        self._stop_event = threading.Event()
        self._last_toggle = 0.0

    def stop(self) -> None:
        self._stop_event.set()
        if self._socket is not None:
            try:
                self._socket.close()
            except Exception:
                pass
        try:
            if os.path.exists(SOCKET_PATH):
                os.unlink(SOCKET_PATH)
        except Exception:
            pass

    def run(self) -> None:
        try:
            if os.path.exists(SOCKET_PATH):
                os.unlink(SOCKET_PATH)
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(SOCKET_PATH)
            os.chmod(SOCKET_PATH, 0o600)
            server.listen(8)
            server.settimeout(0.4)
            self._socket = server

            while not self._stop_event.is_set():
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue
                except Exception:
                    break
                with conn:
                    try:
                        req_raw = conn.recv(4096)
                        req = json.loads(req_raw.decode("utf-8")) if req_raw else {}
                        resp = self._handle(req)
                    except Exception as exc:
                        resp = {"ok": False, "error": str(exc)}
                    try:
                        conn.sendall(json.dumps(resp).encode("utf-8"))
                    except Exception:
                        pass
        finally:
            self.stop()

    def _handle(self, req: dict[str, Any]) -> dict[str, Any]:
        cmd = str(req.get("cmd", "")).strip()
        if cmd == "toggle":
            now = time.time()
            if now - self._last_toggle < 0.15:
                return {"ok": True, "message": "Ignored duplicate toggle", "speaking": self.controller.is_speaking()}
            self._last_toggle = now

            was_speaking = self.controller.is_speaking()
            ok, message = self.controller.toggle()
            if (
                ok
                and not was_speaking
                and self.on_show is not None
                and bool(self.config.get("show_panel_on_hotkey", True))
            ):
                self.on_show(False)
            return {"ok": ok, "message": message, "speaking": self.controller.is_speaking()}

        if cmd == "read":
            ok, message = self.controller.read_selection_async()
            if (
                ok
                and self.on_show is not None
                and bool(self.config.get("show_panel_on_hotkey", True))
            ):
                self.on_show(False)
            return {"ok": ok, "message": message, "speaking": self.controller.is_speaking()}

        if cmd == "stop":
            ok, message = self.controller.stop()
            return {"ok": ok, "message": message, "speaking": self.controller.is_speaking()}

        if cmd == "show":
            if self.on_show is not None:
                self.on_show(True)
            return {"ok": True, "message": "shown"}

        if cmd == "status":
            return {"ok": True, "speaking": self.controller.is_speaking(), "engine": self.config.get("engine", "")}

        return {"ok": False, "error": f"unknown command: {cmd}"}


class VoiceReadingGui:
    def __init__(self, read_on_launch: bool) -> None:
        self.config = ConfigManager(CONFIG_PATH)
        self.events: "queue.Queue[tuple[str, Any, Any]]" = queue.Queue()

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("760x560")
        self.root.minsize(340, 180)
        self.root.configure(bg="#0f1115")
        self.root.withdraw()
        self.root.resizable(True, True)
        try:
            self.root.wm_focusmodel("passive")
        except Exception:
            pass
        self._configure_style()

        self.status_var = tk.StringVar(value="Idle")
        self.source_var = tk.StringVar(value="Source: none")
        self.pause_btn_var = tk.StringVar(value="Pause")
        self.rate_var = tk.IntVar(value=int(self.config.get("rate", 0)))
        self.pitch_var = tk.IntVar(value=int(self.config.get("pitch", 0)))
        self.lang_var = tk.StringVar(value=str(self.config.get("language", "en")))
        self.voice_var = tk.StringVar(value=str(self.config.get("voice", "")))
        self.voice_type_var = tk.StringVar(value=str(self.config.get("voice_type", "male1")))
        self.output_module_var = tk.StringVar(value=str(self.config.get("output_module", "")))
        self._piper_model_map: dict[str, str] = {}
        available = detect_engines()
        preferred = str(self.config.get("engine", "spd-say"))
        self.engine_var = tk.StringVar(value=preferred if preferred in available else (available[0] if available else "spd-say"))

        self.prefer_primary_var = tk.BooleanVar(value=bool(self.config.get("prefer_primary_selection", True)))
        self.copy_fallback_var = tk.BooleanVar(value=bool(self.config.get("capture_selected_text_via_copy", True)))
        self.show_hotkey_var = tk.BooleanVar(value=bool(self.config.get("show_panel_on_hotkey", True)))
        self.always_on_top_var = tk.BooleanVar(value=bool(self.config.get("window_always_on_top", True)))
        self.ultra_precise_sync_var = tk.BooleanVar(value=bool(self.config.get("ultra_precise_highlight_sync", False)))
        selected_mode = resolve_preprocess_mode(self.config)
        self.preprocess_mode_var = tk.StringVar(value=PREPROCESS_MODE_LABELS.get(selected_mode, PREPROCESS_MODE_LABELS[PREPROCESS_MODE_OFF]))
        legacy_strict_sentence = bool(self.config.get("strict_sentence_highlight", True))
        self.highlight_words_var = tk.BooleanVar(
            value=bool(self.config.get("highlight_words_enabled", not legacy_strict_sentence))
        )
        self.highlight_sentences_var = tk.BooleanVar(
            value=bool(self.config.get("highlight_sentences_enabled", legacy_strict_sentence))
        )
        self._is_paused = False
        self._word_spans: list[tuple[int, int]] = []
        self._sentence_spans: list[tuple[int, int]] = []
        self._word_to_sentence: list[int] = []
        self._last_highlighted_word: Any = None
        self._reading_panel_collapsed = False

        self.controller = ReaderController(
            config=self.config,
            on_state_change=lambda speaking, reason: self.events.put(("state", speaking, reason)),
            on_text=lambda text, source: self.events.put(("text", text, source)),
            on_progress=lambda idx, total: self.events.put(("progress", idx, total)),
        )
        self.ipc = IpcServer(
            controller=self.controller,
            config=self.config,
            on_show=lambda active: self.events.put(("show", None, active)),
        )

        self._build_ui()
        self._apply_window_presentation()
        self.root.bind("<Configure>", self._on_window_resize)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.ipc.start()
        self._tick()
        if read_on_launch:
            self.controller.read_selection_async()
            if bool(self.config.get("show_panel_on_hotkey", True)):
                self._show_window(active=False)
        else:
            self._show_window(active=True)

    def _configure_style(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".", font=("DejaVu Sans Mono", 10))
        style.configure("VR.Root.TFrame", background="#0f1115")
        style.configure("VR.Card.TFrame", background="#11231c")
        style.configure("VR.Status.TLabel", font=("DejaVu Sans Mono", 11, "bold"), foreground="#ecfdf5", background="#11231c")
        style.configure("VR.Meta.TLabel", foreground="#34d399", background="#0f1115")
        style.configure("VR.TButton", padding=(10, 4), background="#059669", foreground="#ecfdf5", borderwidth=0)
        style.map("VR.TButton", background=[("pressed", "#047857"), ("active", "#10b981")])
        style.configure("VR.TCheckbutton", background="#171a20", foreground="#cbd5e1")

    def _apply_window_presentation(self) -> None:
        try:
            keep_on_top = bool(self.always_on_top_var.get()) or self.controller.is_speaking()
            self.root.attributes("-topmost", keep_on_top)
        except Exception:
            pass
        try:
            opacity = float(self.config.get("window_opacity", 0.95))
            opacity = max(0.35, min(1.0, opacity))
            self.root.attributes("-alpha", opacity)
        except Exception:
            pass

    def _build_ui(self) -> None:
        root = ttk.Frame(self.root, padding=10, style="VR.Root.TFrame")
        root.pack(fill="both", expand=True)

        top = ttk.Frame(root, style="VR.Card.TFrame", padding=(8, 8))
        top.pack(fill="x")
        self.dot = tk.Canvas(top, width=12, height=12, highlightthickness=0, bd=0, bg="#11231c")
        self.dot.pack(side="left", padx=(0, 6))
        self.dot_indicator = self.dot.create_oval(1, 1, 11, 11, fill="#22c55e", outline="")
        ttk.Label(top, textvariable=self.status_var, style="VR.Status.TLabel").pack(side="left")
        ttk.Button(top, text="Read Selection", style="VR.TButton", command=self._read_from_gui).pack(side="right")
        self.pause_btn = ttk.Button(
            top,
            textvariable=self.pause_btn_var,
            style="VR.TButton",
            command=self._pause_resume_from_gui,
            state="disabled",
        )
        self.pause_btn.pack(side="right", padx=(0, 6))
        ttk.Button(top, text="Stop", style="VR.TButton", command=self._stop_from_gui).pack(side="right", padx=(0, 6))

        ttk.Label(root, textvariable=self.source_var, style="VR.Meta.TLabel").pack(anchor="w", pady=(4, 6))

        self.reading_frame = ttk.Frame(root, style="VR.Card.TFrame", padding=(8, 8))
        self.reading_frame.pack(fill="both", expand=True, pady=(0, 6))
        ttk.Label(self.reading_frame, text="Selected Text", style="VR.Meta.TLabel").pack(anchor="w")
        self.reading_box = tk.Text(
            self.reading_frame,
            wrap="word",
            height=5,
            bg="#0b1410",
            fg="#ecfdf5",
            insertbackground="#ecfdf5",
            highlightthickness=0,
            relief="solid",
            bd=1,
        )
        self.reading_box.pack(fill="both", expand=True, pady=(4, 0))
        self.reading_box.configure(state="disabled")
        self.reading_box.tag_configure("current_word", background="#14532d", foreground="#ecfdf5", font=("DejaVu Sans Mono", 10, "bold"))
        self.reading_box.tag_configure("current_sentence", background="#123524", foreground="#ecfdf5")

        self.controls = ttk.Frame(root, style="VR.Card.TFrame", padding=(8, 8))
        self.controls.pack(fill="x", expand=False)

        row1 = ttk.Frame(self.controls, style="VR.Card.TFrame")
        row1.pack(fill="x", pady=(2, 2))
        ttk.Label(row1, text="Engine:", style="VR.Meta.TLabel").pack(side="left")
        engine_combo = ttk.Combobox(row1, textvariable=self.engine_var, state="readonly")
        engine_combo["values"] = detect_engines() or ["spd-say", "espeak-ng", "espeak"]
        engine_combo.pack(side="left", padx=(6, 12))
        engine_combo.bind("<<ComboboxSelected>>", self._on_engine_changed)
        ttk.Label(row1, text="Model:", style="VR.Meta.TLabel").pack(side="left")
        self.module_combo = ttk.Combobox(row1, textvariable=self.output_module_var, state="readonly", width=28)
        self.module_combo.pack(side="left", padx=(6, 12))
        self.module_combo.bind("<<ComboboxSelected>>", self._on_module_changed)
        ttk.Label(row1, text="Voice Type:", style="VR.Meta.TLabel").pack(side="left")
        vt_combo = ttk.Combobox(row1, textvariable=self.voice_type_var, state="readonly")
        vt_combo["values"] = VOICE_TYPES
        vt_combo.pack(side="left", padx=(6, 0))
        vt_combo.bind("<<ComboboxSelected>>", self._save_options)

        row2 = ttk.Frame(self.controls, style="VR.Card.TFrame")
        row2.pack(fill="x", pady=(2, 2))
        ttk.Label(row2, text="Language:", style="VR.Meta.TLabel").pack(side="left")
        ttk.Entry(row2, textvariable=self.lang_var, width=8).pack(side="left", padx=(6, 12))
        ttk.Label(row2, text="Voice:", style="VR.Meta.TLabel").pack(side="left")
        self.voice_combo = ttk.Combobox(row2, textvariable=self.voice_var, width=20)
        self.voice_combo.pack(side="left", padx=(6, 0), fill="x", expand=True)
        self.voice_combo.bind("<<ComboboxSelected>>", self._save_options)

        row2b = ttk.Frame(self.controls, style="VR.Card.TFrame")
        row2b.pack(fill="x", pady=(2, 2))
        ttk.Label(row2b, text="Reading Mode:", style="VR.Meta.TLabel").pack(side="left")
        preprocess_combo = ttk.Combobox(row2b, textvariable=self.preprocess_mode_var, state="readonly", width=28)
        preprocess_combo["values"] = [PREPROCESS_MODE_LABELS[key] for key in PREPROCESS_MODE_LABELS]
        preprocess_combo.pack(side="left", padx=(6, 12))
        preprocess_combo.bind("<<ComboboxSelected>>", self._save_options)

        row3 = ttk.Frame(self.controls, style="VR.Card.TFrame")
        row3.pack(fill="x", pady=(2, 2))
        ttk.Label(row3, text="Rate", style="VR.Meta.TLabel").pack(side="left")
        tk.Scale(
            row3,
            variable=self.rate_var,
            from_=-100,
            to=100,
            orient="horizontal",
            showvalue=True,
            command=lambda _v: self._save_options(),
            bg="#171a20",
            fg="#cbd5e1",
            troughcolor="#0b0d10",
            highlightthickness=0,
        ).pack(side="left", fill="x", expand=True, padx=(8, 8))

        row4 = ttk.Frame(self.controls, style="VR.Card.TFrame")
        row4.pack(fill="x", pady=(2, 2))
        ttk.Label(row4, text="Pitch", style="VR.Meta.TLabel").pack(side="left")
        tk.Scale(
            row4,
            variable=self.pitch_var,
            from_=-100,
            to=100,
            orient="horizontal",
            showvalue=True,
            command=lambda _v: self._save_options(),
            bg="#171a20",
            fg="#cbd5e1",
            troughcolor="#0b0d10",
            highlightthickness=0,
        ).pack(side="left", fill="x", expand=True, padx=(8, 8))

        ttk.Checkbutton(
            self.controls,
            text="Prefer primary selection",
            variable=self.prefer_primary_var,
            command=self._save_options,
            style="VR.TCheckbutton",
        ).pack(anchor="w", pady=(2, 0))
        ttk.Checkbutton(
            self.controls,
            text="Use copy fallback when selection is unavailable",
            variable=self.copy_fallback_var,
            command=self._save_options,
            style="VR.TCheckbutton",
        ).pack(anchor="w", pady=(2, 0))
        ttk.Checkbutton(
            self.controls,
            text="Show panel on hotkey",
            variable=self.show_hotkey_var,
            command=self._save_options,
            style="VR.TCheckbutton",
        ).pack(anchor="w", pady=(2, 0))
        ttk.Checkbutton(
            self.controls,
            text="Keep panel always on top",
            variable=self.always_on_top_var,
            command=self._save_options,
            style="VR.TCheckbutton",
        ).pack(anchor="w", pady=(2, 0))
        ttk.Checkbutton(
            self.controls,
            text="Ultra-precise highlight sync (slower start)",
            variable=self.ultra_precise_sync_var,
            command=self._save_options,
            style="VR.TCheckbutton",
        ).pack(anchor="w", pady=(2, 0))
        ttk.Checkbutton(
            self.controls,
            text="Highlight words",
            variable=self.highlight_words_var,
            command=self._save_options,
            style="VR.TCheckbutton",
        ).pack(anchor="w", pady=(2, 0))
        ttk.Checkbutton(
            self.controls,
            text="Highlight sentences",
            variable=self.highlight_sentences_var,
            command=self._save_options,
            style="VR.TCheckbutton",
        ).pack(anchor="w", pady=(2, 0))

        footer = ttk.Frame(root, style="VR.Root.TFrame")
        footer.pack(fill="x", pady=(6, 0))
        ttk.Button(footer, text="Apply", style="VR.TButton", command=self._save_options).pack(side="right")
        ttk.Button(footer, text="Refresh Models", style="VR.TButton", command=self._refresh_model_lists).pack(
            side="right", padx=(0, 6)
        )
        ttk.Button(footer, text="Help", style="VR.TButton", command=self._show_help).pack(side="right", padx=(0, 6))

        self._refresh_model_lists()

    def _show_help(self) -> None:
        msg = (
            "Read Aloud Hotkey Flow:\n"
            "- Select text with mouse.\n"
            "- Press Super+R to read.\n"
            "- Press Super+R again to stop.\n\n"
            "Pause/Resume is available from GUI and resumes from the current place.\n"
            "Use Engine + Model + Voice for quality tuning.\n"
            "Math reading quality depends on selected engine/voice.\n"
            "Reading mode options: Off, Code/Math, Codex output, Markdown raw.\n"
            "Highlight words/sentences can be toggled independently."
        )
        messagebox.showinfo("Voice Reading Help", msg)

    def _refresh_model_lists(self) -> None:
        if self.engine_var.get().strip() == "spd-say":
            modules = list_spd_output_modules()
            if not modules:
                modules = [""]
            self.module_combo.configure(state="readonly")
            self.module_combo["values"] = modules
            self.voice_combo.configure(state="normal")
            current_module = self.output_module_var.get().strip()
            if current_module and current_module in modules:
                self.output_module_var.set(current_module)
            else:
                preferred = "rhvoice" if "rhvoice" in modules else modules[0]
                self.output_module_var.set(preferred)

            selected_module = self.output_module_var.get().strip()
            if selected_module.lower() == "rhvoice":
                voices = list_rhvoice_voices()
                if not voices:
                    voices = list_spd_synthesis_voices(selected_module)
            else:
                voices = list_spd_synthesis_voices(selected_module)
            if not voices:
                voices = [self.voice_var.get().strip()]
            self.voice_combo["values"] = [v for v in voices if v]
            current_voice = self.voice_var.get().strip()
            if current_voice and current_voice in voices:
                self.voice_var.set(current_voice)
            elif selected_module.lower() == "rhvoice":
                self.voice_var.set(pick_preferred_rhvoice_voice([v for v in voices if v]))
            elif voices and voices[0]:
                self.voice_var.set(voices[0])
        elif self.engine_var.get().strip() == "piper":
            self._piper_model_map = {}
            model_paths = list_piper_model_paths()
            preferred_path = choose_preferred_piper_model(model_paths)
            labels: list[str] = []
            for model_path in model_paths:
                label = format_piper_model_label(model_path)
                unique = label
                suffix = 2
                while unique in self._piper_model_map:
                    unique = f"{label} #{suffix}"
                    suffix += 1
                self._piper_model_map[unique] = model_path
                labels.append(unique)

            self.module_combo.configure(state="readonly")
            self.module_combo["values"] = labels or [""]
            saved_path = str(self.config.get("piper_model", "")).strip()
            selected_label = ""
            if saved_path:
                for label, path in self._piper_model_map.items():
                    if path == saved_path:
                        selected_label = label
                        break
            if not selected_label and preferred_path:
                for label, path in self._piper_model_map.items():
                    if path == preferred_path:
                        selected_label = label
                        break
            if not selected_label and labels:
                selected_label = labels[0]
            self.output_module_var.set(selected_label)

            self.voice_combo["values"] = []
            self.voice_combo.configure(state="disabled")
            self.voice_type_var.set("male1")
        else:
            self.output_module_var.set("")
            self.module_combo["values"] = [""]
            self.module_combo.configure(state="disabled")
            # For espeak engines, leave voice editable (language voice codes).
            self.voice_combo["values"] = []
            self.voice_combo.configure(state="normal")

    def _on_engine_changed(self, _event: Any = None) -> None:
        self._refresh_model_lists()
        self._save_options()

    def _on_module_changed(self, _event: Any = None) -> None:
        engine = self.engine_var.get().strip()
        if engine == "spd-say":
            selected_module = self.output_module_var.get().strip()
            if selected_module.lower() == "rhvoice":
                voices = list_rhvoice_voices()
                if not voices:
                    voices = list_spd_synthesis_voices(selected_module)
            else:
                voices = list_spd_synthesis_voices(selected_module)
            if voices:
                self.voice_combo["values"] = voices
                if self.voice_var.get().strip() not in voices:
                    if selected_module.lower() == "rhvoice":
                        self.voice_var.set(pick_preferred_rhvoice_voice(voices))
                    else:
                        self.voice_var.set(voices[0])
        elif engine == "piper":
            self.config.set("piper_model", self._piper_model_map.get(self.output_module_var.get().strip(), ""))
        self._save_options()

    def _save_options(self, _event: Any = None) -> None:
        engine = self.engine_var.get().strip()
        self.config.set("engine", engine)
        if engine == "piper":
            model_path = self._piper_model_map.get(self.output_module_var.get().strip(), "")
            self.config.set("piper_model", model_path)
            self.config.set("output_module", "")
        else:
            self.config.set("output_module", self.output_module_var.get().strip())
        self.config.set("rate", int(self.rate_var.get()))
        self.config.set("pitch", int(self.pitch_var.get()))
        self.config.set("language", self.lang_var.get().strip())
        self.config.set("voice", self.voice_var.get().strip())
        self.config.set("voice_type", self.voice_type_var.get().strip())
        self.config.set("prefer_primary_selection", bool(self.prefer_primary_var.get()))
        self.config.set("capture_selected_text_via_copy", bool(self.copy_fallback_var.get()))
        self.config.set("show_panel_on_hotkey", bool(self.show_hotkey_var.get()))
        self.config.set("window_always_on_top", bool(self.always_on_top_var.get()))
        self.config.set("ultra_precise_highlight_sync", bool(self.ultra_precise_sync_var.get()))
        mode_label = self.preprocess_mode_var.get().strip()
        mode_key = PREPROCESS_MODE_LABEL_TO_KEY.get(mode_label, PREPROCESS_MODE_OFF)
        self.config.set("reading_preprocess_mode", mode_key)
        # Legacy compatibility for older builds/scripts.
        self.config.set("code_math_reading_mode", mode_key == PREPROCESS_MODE_CODE_MATH)
        self.config.set("highlight_words_enabled", bool(self.highlight_words_var.get()))
        self.config.set("highlight_sentences_enabled", bool(self.highlight_sentences_var.get()))
        self._apply_window_presentation()
        if not bool(self.highlight_words_var.get()) and not bool(self.highlight_sentences_var.get()):
            self.reading_box.configure(state="normal")
            self.reading_box.tag_remove("current_word", "1.0", "end")
            self.reading_box.tag_remove("current_sentence", "1.0", "end")
            self.reading_box.configure(state="disabled")
            self._last_highlighted_word = None

    def _read_from_gui(self) -> None:
        ok, message = self.controller.read_selection_async()
        if not ok:
            self.status_var.set(message)
            notify("Voice Reading", message, urgency="critical")

    def _pause_resume_from_gui(self) -> None:
        ok, message = self.controller.pause_or_resume()
        if not ok:
            self.status_var.set(message)

    def _stop_from_gui(self) -> None:
        ok, message = self.controller.stop()
        if not ok:
            self.status_var.set(message)

    def _tick(self) -> None:
        self._drain_events()
        self.root.after(200, self._tick)

    def _drain_events(self) -> None:
        while True:
            try:
                event, data, meta = self.events.get_nowait()
            except queue.Empty:
                return

            if event == "state":
                speaking = bool(data)
                reason = str(meta)
                if speaking:
                    self.status_var.set("Reading...")
                    self.dot.itemconfig(self.dot_indicator, fill="#dc2626")
                    self._is_paused = False
                    self.pause_btn_var.set("Pause")
                    self.pause_btn.configure(state="normal")
                else:
                    self.dot.itemconfig(self.dot_indicator, fill="#16a34a")
                    status = {
                        "done": "Ready",
                        "stopped": "Stopped",
                        "paused": "Paused",
                        "error": "Error",
                    }.get(reason, "Idle")
                    self.status_var.set(status)
                    if reason == "paused":
                        self._is_paused = True
                        self.pause_btn_var.set("Resume")
                        self.pause_btn.configure(state="normal")
                    else:
                        self._is_paused = False
                        self.pause_btn_var.set("Pause")
                        self.pause_btn.configure(state="disabled")
                self._apply_window_presentation()

            if event == "text":
                text = str(data).strip()
                source = str(meta)
                preview = text.replace("\n", " ")
                if len(preview) > 72:
                    preview = preview[:72] + "..."
                self.source_var.set(f"Source: {source} | {preview}")
                self._set_reading_text(text)
                self._highlight_progress(0)

            if event == "progress":
                try:
                    idx = int(data)
                    total = int(meta)
                except Exception:
                    idx = 0
                    total = 0
                if total > 0:
                    self._highlight_progress(idx)

            if event == "show":
                self._show_window(active=bool(meta) if meta is not None else True)

    def _set_reading_text(self, text: str) -> None:
        self.reading_box.configure(state="normal")
        self.reading_box.delete("1.0", "end")
        self.reading_box.insert("1.0", text)
        self.reading_box.tag_remove("current_word", "1.0", "end")
        self.reading_box.tag_remove("current_sentence", "1.0", "end")
        self.reading_box.configure(state="disabled")
        self._word_spans = [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]
        self._sentence_spans = [(m.start(), m.end()) for m in re.finditer(r"[^.!?]+[.!?]*", text, flags=re.S) if m.group(0).strip()]
        self._word_to_sentence = []
        if self._sentence_spans and self._word_spans:
            s_idx = 0
            for w_start, _ in self._word_spans:
                while s_idx + 1 < len(self._sentence_spans) and w_start >= self._sentence_spans[s_idx][1]:
                    s_idx += 1
                self._word_to_sentence.append(s_idx)
        self._last_highlighted_word = None

    def _highlight_progress(self, word_index: int) -> None:
        if not self._word_spans:
            return
        highlight_words = bool(self.highlight_words_var.get())
        highlight_sentences = bool(self.highlight_sentences_var.get())
        idx = max(0, min(word_index, len(self._word_spans) - 1))
        sent_idx: int | None = None
        if self._word_to_sentence and idx < len(self._word_to_sentence):
            sent_idx = self._word_to_sentence[idx]
        cache_key = (
            idx if highlight_words else None,
            sent_idx if highlight_sentences else None,
            highlight_words,
            highlight_sentences,
        )
        if cache_key == self._last_highlighted_word:
            return
        self.reading_box.configure(state="normal")
        self.reading_box.tag_remove("current_word", "1.0", "end")
        self.reading_box.tag_remove("current_sentence", "1.0", "end")
        scroll_target = "1.0"
        if highlight_sentences and sent_idx is not None and sent_idx < len(self._sentence_spans):
            s_start, s_end = self._sentence_spans[sent_idx]
            s_start_idx = f"1.0+{s_start}c"
            s_end_idx = f"1.0+{s_end}c"
            self.reading_box.tag_add("current_sentence", s_start_idx, s_end_idx)
            scroll_target = s_start_idx
        if highlight_words:
            start_char, end_char = self._word_spans[idx]
            w_start_idx = f"1.0+{start_char}c"
            w_end_idx = f"1.0+{end_char}c"
            self.reading_box.tag_add("current_word", w_start_idx, w_end_idx)
            scroll_target = w_start_idx
        if highlight_words or highlight_sentences:
            self.reading_box.see(scroll_target)
        self.reading_box.configure(state="disabled")
        self._last_highlighted_word = cache_key

    def _highlight_word(self, word_index: int) -> None:
        # Backward-compatible wrapper for older call sites.
        self._highlight_progress(word_index)

    def _show_window(self, active: bool = True) -> None:
        try:
            self.root.deiconify()
            self._apply_window_presentation()
            if active:
                self.root.lift()
        except Exception:
            pass

    def _on_window_resize(self, event: Any) -> None:
        if event.widget is not self.root:
            return
        try:
            compact = self.root.winfo_height() < 320
        except Exception:
            return
        if compact and not self._reading_panel_collapsed:
            try:
                self.reading_frame.pack_forget()
                self._reading_panel_collapsed = True
            except Exception:
                pass
        elif (not compact) and self._reading_panel_collapsed:
            try:
                self.reading_frame.pack(fill="both", expand=True, pady=(0, 6), before=self.controls)
                self._reading_panel_collapsed = False
            except Exception:
                pass

    def _on_close(self) -> None:
        if self.controller.is_speaking():
            self.controller.stop()
        self.ipc.stop()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def send_ipc(request: dict[str, Any], timeout: float = 1.0) -> dict[str, Any] | None:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(SOCKET_PATH)
        client.sendall(json.dumps(request).encode("utf-8"))
        raw = client.recv(65536)
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    finally:
        try:
            client.close()
        except Exception:
            pass


def send_ipc_with_retries(request: dict[str, Any], attempts: int = 2, timeout: float = 0.8) -> dict[str, Any] | None:
    for idx in range(max(1, attempts)):
        resp = send_ipc(request, timeout=timeout)
        if resp is not None:
            return resp
        if idx < attempts - 1:
            time.sleep(0.12)
    return None


def spawn_gui(read_on_launch: bool) -> None:
    cmd = [sys.executable, str(Path(__file__).resolve()), "gui"]
    if read_on_launch:
        cmd.append("--read-on-launch")
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def reuse_existing_gui(read_on_launch: bool) -> bool:
    status = send_ipc_with_retries({"cmd": "status"}, attempts=2, timeout=0.6)
    if status is None:
        return False
    if read_on_launch:
        send_ipc_with_retries({"cmd": "toggle"}, attempts=2, timeout=0.8)
    else:
        send_ipc_with_retries({"cmd": "show"}, attempts=1, timeout=0.6)
    return True


def cmd_toggle() -> int:
    resp = send_ipc_with_retries({"cmd": "toggle"}, attempts=2, timeout=0.8)
    if resp is not None:
        print(resp.get("message", "ok"))
        return 0 if resp.get("ok", False) else 1
    spawn_gui(read_on_launch=True)
    print("Started Voice Reading and began read-aloud")
    return 0


def cmd_read() -> int:
    resp = send_ipc_with_retries({"cmd": "read"}, attempts=2, timeout=0.8)
    if resp is not None:
        print(resp.get("message", "ok"))
        return 0 if resp.get("ok", False) else 1
    spawn_gui(read_on_launch=True)
    print("Started Voice Reading and began read-aloud")
    return 0


def cmd_stop() -> int:
    resp = send_ipc_with_retries({"cmd": "stop"}, attempts=2, timeout=0.8)
    if resp is None:
        print("No running Voice Reading GUI server")
        return 1
    print(resp.get("message", "ok"))
    return 0 if resp.get("ok", False) else 1


def cmd_status() -> int:
    resp = send_ipc_with_retries({"cmd": "status"}, attempts=2, timeout=0.6)
    if resp is None:
        print("Voice Reading GUI server is not running")
        return 1
    print(json.dumps(resp, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline text-to-speech reader")
    sub = parser.add_subparsers(dest="command", required=True)

    gui_parser = sub.add_parser("gui", help="Run Voice Reading GUI")
    gui_parser.add_argument("--read-on-launch", action="store_true")

    sub.add_parser("toggle", help="Toggle read/stop via IPC (for hotkey)")
    sub.add_parser("read", help="Read selected text")
    sub.add_parser("stop", help="Stop reading")
    sub.add_parser("status", help="Show server status")

    args = parser.parse_args()

    if args.command == "gui":
        if reuse_existing_gui(read_on_launch=bool(args.read_on_launch)):
            return 0
        app = VoiceReadingGui(read_on_launch=bool(args.read_on_launch))
        app.run()
        return 0
    if args.command == "toggle":
        return cmd_toggle()
    if args.command == "read":
        return cmd_read()
    if args.command == "stop":
        return cmd_stop()
    if args.command == "status":
        return cmd_status()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
