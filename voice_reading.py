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
import signal
import shutil
import socket
import subprocess
import sys
import threading
import time
import tempfile
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

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

PIPER_KEEP_WARM_SECONDS_DEFAULT = 300

DEFAULT_CONFIG: dict[str, Any] = {
    "engine": "spd-say",
    "output_module": "",
    "audio_output_device": "",
    "piper_model": "",
    "piper_gpu_enabled": False,
    "piper_gpu_binary_path": str(Path.home() / "piper-gpu" / "bin" / "piper"),
    "rate": 0,  # speech-dispatcher scale: -100..100
    "pitch": 0,
    "language": "en",
    "ollama_url": "http://127.0.0.1:11434",
    "ollama_model": "gemma4:latest",
    "ollama_context_tokens": 2048,
    "ollama_timeout_seconds": 300,
    "ollama_keep_alive": "30m",
    "translation_target_language": "English",
    "translation_speak_aloud": False,
    "voice": "",
    "voice_type": "male1",
    "prefer_primary_selection": True,
    "capture_selected_text_via_copy": True,
    "show_panel_on_hotkey": True,
    "window_always_on_top": False,
    "window_opacity": 0.95,
    "reading_preprocess_mode": "off",
    "code_math_reading_mode": False,
    "highlight_words_enabled": False,
    "highlight_sentences_enabled": True,
    "highlight_passage_enabled": True,
    # Seconds between starting the audio player and hearing it.
    "highlight_delay_seconds": 0.06,
    "piper_keep_warm_seconds": PIPER_KEEP_WARM_SECONDS_DEFAULT,
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

# Sink-input properties that mark a stream as ours when it comes from the
# speech-dispatcher daemon rather than from a player we started ourselves.
SPEECH_STREAM_HINTS = ("speech-dispatcher", "speechd", "voice-reading")

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
        if isinstance(loaded, dict) and "window_always_on_top" in loaded and not loaded.get(
            "focus_safe_migration_v1", False
        ):
            data["window_always_on_top"] = False
            data["focus_safe_migration_v1"] = True
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


def detect_engines(config: "ConfigManager | None" = None) -> list[str]:
    engines: list[str] = []
    if shutil.which("spd-say"):
        engines.append("spd-say")
    if resolve_piper_binary(config):
        engines.append("piper")
    if shutil.which("espeak-ng"):
        engines.append("espeak-ng")
    if shutil.which("espeak"):
        engines.append("espeak")
    return engines


def resolve_piper_binary(config: "ConfigManager | None" = None) -> str:
    if config is not None and bool(config.get("piper_gpu_enabled", False)):
        gpu_path = Path(os.path.expanduser(str(config.get("piper_gpu_binary_path", "")).strip()))
        if gpu_path.exists() and os.access(gpu_path, os.X_OK):
            return str(gpu_path)
    path_bin = shutil.which("piper")
    if path_bin:
        return path_bin
    for candidate in LOCAL_PIPER_CANDIDATES:
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return ""


def piper_runtime_label(config: "ConfigManager") -> str:
    path = resolve_piper_binary(config)
    if not path:
        return "missing"
    gpu_path = Path(os.path.expanduser(str(config.get("piper_gpu_binary_path", "")).strip()))
    if bool(config.get("piper_gpu_enabled", False)) and gpu_path.exists() and Path(path) == gpu_path:
        return f"gpu:{path}"
    return f"cpu:{path}"


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


def list_ollama_models(base_url: str) -> list[str]:
    """Return locally installed Ollama model names for the Reader selector."""
    url = f"{base_url.rstrip('/')}/api/tags"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=2.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        log_line(f"ollama model list failed: {exc}")
        return []
    models = payload.get("models", []) if isinstance(payload, dict) else []
    names = {
        str(item.get("name", "")).strip()
        for item in models
        if isinstance(item, dict) and str(item.get("name", "")).strip()
    }
    return sorted(names, key=str.casefold)


def ollama_generation_options(config: "ConfigManager", output_tokens: int | None = None) -> dict[str, Any]:
    """Build native Ollama options, including the VRAM-sensitive context size."""
    try:
        context_tokens = int(config.get("ollama_context_tokens", 2048))
    except (TypeError, ValueError):
        context_tokens = 2048
    context_tokens = max(512, min(262144, context_tokens))
    options: dict[str, Any] = {"num_ctx": context_tokens}
    if output_tokens is not None:
        options["num_predict"] = max(1, int(output_tokens))
    return options


def format_piper_model_label(path_str: str) -> str:
    path = Path(path_str)
    base = path.stem
    parent = path.parent.name
    if parent and parent != ".":
        return f"{base} ({parent})"
    return base


LANGUAGE_FAMILIES = {
    "english": "en",
    "en": "en",
    "french": "fr",
    "fr": "fr",
    "italian": "it",
    "it": "it",
    "hebrew": "he",
    "he": "he",
}

DEFAULT_PIPER_VOICE_KEYS = {
    "en": "en_GB-cori-high",
    "fr": "fr_FR-upmc-medium",
    "it": "it_IT-paola-medium",
    "he": "he_IL-phonikud-medium",
}


def normalize_language_family(language: str) -> str:
    raw = language.strip().lower().replace("-", "_")
    return LANGUAGE_FAMILIES.get(raw, raw.split("_", 1)[0])


def piper_model_family(path_str: str) -> str:
    match = re.search(r"(?:^|[/_\\-])([a-z]{2})(?:[_\\-]|$)", path_str.lower())
    return match.group(1) if match else ""


def choose_piper_model_for_language(model_paths: list[str], language: str) -> str:
    """Choose the best installed Piper voice matching a language family."""
    family = normalize_language_family(language)
    candidates = [path for path in model_paths if piper_model_family(path) == family]
    if not candidates:
        return ""
    preferred_key = DEFAULT_PIPER_VOICE_KEYS.get(family, "")
    if preferred_key:
        preferred = [path for path in candidates if preferred_key.lower() in Path(path).name.lower()]
        if preferred:
            return preferred[0]
    quality_rank = {"high": 0, "medium": 1, "low": 2, "x_low": 3}
    candidates.sort(
        key=lambda path: (
            quality_rank.get(Path(path).stem.rsplit("-", 1)[-1].lower(), 9),
            Path(path).name.lower(),
        )
    )
    return candidates[0]


def piper_model_matches_language(path_str: str, language: str) -> bool:
    return bool(path_str) and piper_model_family(path_str) == normalize_language_family(language)


def list_output_devices() -> list[tuple[str, str]]:
    """Available sinks as (name, description), best-effort.

    An empty name means "whatever the system default is", which is the entry
    the reader starts on. The list is rebuilt every time the dropdown opens,
    because Bluetooth and HDMI outputs appear and disappear while it runs.
    """
    devices: list[tuple[str, str]] = [("", "System default")]
    if shutil.which("pactl") is None:
        return devices
    rc, out, _ = _run_capture(["pactl", "list", "sinks"], timeout=2.0)
    if rc != 0:
        return devices
    name = ""
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Name:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("Description:") and name:
            devices.append((name, line.split(":", 1)[1].strip()))
            name = ""
    return devices


def audio_output_env(device: str) -> dict[str, str] | None:
    """Environment that pins a child process to one PulseAudio sink."""
    device = device.strip()
    if not device:
        return None
    env = os.environ.copy()
    env["PULSE_SINK"] = device
    return env


def _sink_inputs_matching(hints: tuple[str, ...]) -> list[str]:
    """Indices of playback streams whose properties mention any of `hints`."""
    rc, out, _ = _run_capture(["pactl", "list", "sink-inputs"], timeout=2.0)
    if rc != 0:
        return []
    indices: list[str] = []
    index = ""
    block: list[str] = []
    for line in out.splitlines() + ["Sink Input #"]:
        stripped = line.strip()
        if stripped.startswith("Sink Input #"):
            if index and any(hint in "\n".join(block).lower() for hint in hints):
                indices.append(index)
            index = stripped.split("#", 1)[1].strip()
            block = []
        else:
            block.append(stripped)
    return indices


def route_speech_to_output_device(device: str, timeout: float = 2.0) -> None:
    """Move speech-dispatcher playback onto the chosen sink, best effort.

    speech-dispatcher speaks through its own daemon, so unlike paplay it cannot
    be pointed at a sink up front; its stream is moved once it shows up.
    """
    device = device.strip()
    if not device or shutil.which("pactl") is None:
        return
    deadline = time.monotonic() + timeout
    moved: set[str] = set()
    while time.monotonic() < deadline:
        # speech-dispatcher opens its streams in stages, so keep watching for
        # the whole window instead of stopping at the first one moved.
        for index in _sink_inputs_matching(SPEECH_STREAM_HINTS):
            if index in moved:
                continue
            rc, _, _ = _run_capture(["pactl", "move-sink-input", index, device], timeout=2.0)
            if rc == 0:
                moved.add(index)
        time.sleep(0.1)


def list_audio_player_commands(wav_path: str, device: str = "") -> list[list[str]]:
    """Players to try in order; `device` is a sink name ("" = system default)."""
    device = device.strip()
    pw_cmd = ["pw-play"] + ([f"--target={device}"] if device else []) + [wav_path]
    pa_cmd = ["paplay"] + ([f"--device={device}"] if device else []) + [wav_path]
    commands: list[list[str]] = []
    if device:
        # paplay speaks the same sink names as `pactl list sinks`, so it honours
        # an explicit choice most reliably; aplay is skipped because its ALSA
        # device names do not match those sinks at all.
        if shutil.which("paplay"):
            commands.append(pa_cmd)
        if shutil.which("pw-play"):
            commands.append(pw_cmd)
    else:
        if shutil.which("pw-play"):
            commands.append(pw_cmd)
        if shutil.which("aplay"):
            commands.append(["aplay", "-q", wav_path])
        if shutil.which("paplay"):
            commands.append(pa_cmd)
    if shutil.which("ffplay"):
        commands.append(["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", wav_path])
    if shutil.which("mpv"):
        mpv_cmd = ["mpv", "--really-quiet", "--no-video"]
        if device:
            mpv_cmd.append(f"--audio-device=pulse/{device}")
        commands.append(mpv_cmd + [wav_path])
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


PIPER_VOICES_BASE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
PIPER_VOICES_INDEX = SCRIPT_DIR / "piper_voices" / "index" / "voices.json"
PIPER_DOWNLOAD_DIR = SCRIPT_DIR / "piper_voices"

# Piper's upstream catalog still has no Hebrew voice.  This checkpoint is
# published by the Phonikud project in Piper's ONNX + JSON format.  It takes
# IPA, so the companion Phonikud ONNX G2P model is downloaded with it and used
# by the reader before invoking Piper.
HEBREW_PIPER_ENTRY: dict[str, Any] = {
    "key": "he_IL-phonikud-medium",
    "language": "he_IL",
    "language_family": "he",
    "language_name": "Hebrew",
    "language_native": "עברית",
    "country": "Israel",
    "speaker": "Phonikud",
    "num_speakers": 1,
    "is_default": True,
    "quality": "medium",
    "size_mb": 364,
    "onnx": "he/he_IL/phonikud/medium/he_IL-phonikud-medium.onnx",
    "json": "he/he_IL/phonikud/medium/he_IL-phonikud-medium.onnx.json",
    "source_base_url": "https://huggingface.co/Phonikud/phonikud-tts-checkpoints/resolve/main",
    "onnx_source": "model.onnx",
    "json_source": "model.config.json",
    "extra_files": [
        {
            "url": "https://huggingface.co/thewh1teagle/phonikud-onnx/resolve/main/phonikud-1.0.int8.onnx",
            "path": "he/phonikud-1.0.int8.onnx",
        }
    ],
}


def _voice_entry_installed(entry: dict[str, Any]) -> bool:
    paths = [entry["onnx"], entry["json"]]
    paths.extend(str(item["path"]) for item in entry.get("extra_files", []) if isinstance(item, dict))
    return all((PIPER_DOWNLOAD_DIR / path).exists() for path in paths)


def list_voice_catalog(locales: set[str] | None = None) -> list[dict[str, Any]]:
    """Piper voices from the bundled index plus the supported Hebrew voice."""
    try:
        data = json.loads(PIPER_VOICES_INDEX.read_text(encoding="utf-8"))
    except Exception:
        return []
    catalog: list[dict[str, Any]] = []
    for key, info in sorted(data.items()):
        language = str(info.get("language", {}).get("code", "")) if isinstance(info, dict) else ""
        if locales and language not in locales:
            continue
        files = info.get("files", {}) if isinstance(info, dict) else {}
        onnx_files = [name for name in files if name.endswith(".onnx")]
        if not onnx_files:
            continue
        rel_onnx = onnx_files[0]
        size_mb = sum(int(meta.get("size_bytes", 0)) for meta in files.values() if isinstance(meta, dict)) // (1024 * 1024)
        catalog.append(
            {
                "key": key,
                "language": language,
                "language_family": str(info.get("language", {}).get("family", "")),
                "language_name": str(info.get("language", {}).get("name_english", language)),
                "language_native": str(info.get("language", {}).get("name_native", "")),
                "country": str(info.get("language", {}).get("country_english", "")),
                "speaker": str(info.get("name", key)),
                "num_speakers": int(info.get("num_speakers", 1) or 1),
                "is_default": key == DEFAULT_PIPER_VOICE_KEYS.get(
                    str(info.get("language", {}).get("family", "")), ""
                ),
                "quality": str(info.get("quality", "")),
                "size_mb": size_mb,
                "onnx": rel_onnx,
                "json": rel_onnx + ".json",
                "installed": _voice_entry_installed({"onnx": rel_onnx, "json": rel_onnx + ".json"}),
            }
        )
    if not locales or HEBREW_PIPER_ENTRY["language"] in locales:
        hebrew = dict(HEBREW_PIPER_ENTRY)
        hebrew["installed"] = _voice_entry_installed(hebrew)
        catalog.append(hebrew)
    catalog.sort(key=lambda item: (str(item.get("language_name", "")).casefold(), str(item.get("key", "")).casefold()))
    return catalog


def list_gb_voice_catalog() -> list[dict[str, Any]]:
    """Backward-compatible British voice catalog."""
    return list_voice_catalog({"en_GB"})


def download_piper_voice(key: str, progress: Callable[[str], None] | None = None) -> tuple[bool, str]:
    """Fetch a voice and any required phonemizer files into piper_voices."""
    entry = next((item for item in list_voice_catalog() if item["key"] == key), None)
    if entry is None:
        return False, f"Unknown voice: {key} (see list-gb-voices)"
    if entry["installed"]:
        return True, f"{key} is already installed"

    files_to_download = [
        {
            "url": f"{entry.get('source_base_url', PIPER_VOICES_BASE_URL).rstrip('/')}/{entry.get('onnx_source', entry['onnx'])}",
            "path": entry["onnx"],
        },
        {
            "url": f"{entry.get('source_base_url', PIPER_VOICES_BASE_URL).rstrip('/')}/{entry.get('json_source', entry['json'])}",
            "path": entry["json"],
        },
    ]
    files_to_download.extend(entry.get("extra_files", []))
    for file_info in files_to_download:
        rel_path = str(file_info["path"])
        url = str(file_info["url"])
        dest = PIPER_DOWNLOAD_DIR / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(dest.suffix + ".part")
        try:
            if progress:
                progress(f"Downloading {Path(rel_path).name}...")
            with urllib.request.urlopen(url, timeout=60) as resp, part.open("wb") as out:
                done = 0
                while True:
                    block = resp.read(1024 * 256)
                    if not block:
                        break
                    out.write(block)
                    done += len(block)
                    if progress and done % (1024 * 1024 * 4) < 1024 * 256:
                        progress(f"Downloading {Path(rel_path).name}... {done // (1024 * 1024)} MB")
            part.replace(dest)
        except Exception as exc:
            part.unlink(missing_ok=True)
            return False, f"Download failed for {url}: {exc}"
    return True, f"Installed {key} ({entry['size_mb']} MB). Click Refresh Models to use it."


_HEBREW_PHONEMIZER: Any = None
_HEBREW_PHONEMIZER_LOCK = threading.Lock()


def prepare_piper_text(text: str, model_path: str) -> tuple[str, str]:
    """Return Piper input and an optional preparation error.

    The Phonikud Piper checkpoint consumes IPA rather than Hebrew graphemes.
    Keep the dependency optional for users who only use the other Piper
    voices, but fail clearly instead of sending unsupported Hebrew characters
    to Piper when the Hebrew voice is selected.
    """
    if normalize_language_family(piper_model_family(model_path)) != "he":
        return text, ""
    phonemizer_path = PIPER_DOWNLOAD_DIR / "he" / "phonikud-1.0.int8.onnx"
    if not phonemizer_path.exists():
        return "", "Hebrew voice phonemizer is not installed; download the Hebrew voice again."
    try:
        from phonikud_tts import Phonikud, phonemize
    except Exception as exc:
        return "", f"Hebrew voice needs phonikud-tts (install requirements-hebrew-tts.txt): {exc}"
    global _HEBREW_PHONEMIZER
    try:
        with _HEBREW_PHONEMIZER_LOCK:
            if _HEBREW_PHONEMIZER is None:
                _HEBREW_PHONEMIZER = Phonikud(str(phonemizer_path))
            with_diacritics = _HEBREW_PHONEMIZER.add_diacritics(text)
        return phonemize(with_diacritics), ""
    except Exception as exc:
        return "", f"Hebrew text preparation failed: {exc}"


# Document reading: the Reader text is spoken passage by passage. Passages
# ahead of the one playing are synthesized in the background, so text pasted
# below the current passage continues without a gap.
READER_LOOKAHEAD_PASSAGES = 4
READER_LOOKAHEAD_CHARS = 2400
READER_FIRST_PASSAGE_CHARS = 220
READER_PASSAGE_CHARS = 450
READER_PASSAGE_MIN_CHARS = 160

_PASSAGE_BOUNDARY_RE = re.compile(r"[.!?…]+[\"”’')\]]*(?=\s|$)|\n")


def next_passage_cut(text: str, max_chars: int, min_chars: int) -> tuple[int, bool]:
    """Offset where the next spoken passage of text ends.

    Passages end after a sentence or a line break, once they hold min_chars,
    and never pass max_chars unless no boundary exists. A blank line always
    ends one. Returns (cut, complete); complete is False when the passage
    runs to the end of text with no sentence end, i.e. it may still be typed.
    """
    length = len(text)
    if length == 0:
        return 0, True
    cut = 0
    for match in _PASSAGE_BOUNDARY_RE.finditer(text):
        end = match.end()
        if end > max_chars:
            break
        cut = end
        paragraph_break = match.group(0) == "\n" and text[end : end + 1] == "\n"
        if cut >= min_chars or paragraph_break:
            return cut, True
    if cut > 0:
        return cut, True
    if length <= max_chars:
        return length, False
    space = text.rfind(" ", 0, max_chars)
    return (space + 1 if space > 0 else max_chars), True


@dataclass
class SpeechPassage:
    passage_id: int
    text: str
    speak_text: str
    words: list[str] = field(default_factory=list)
    wav_path: str = ""
    duration: float = 0.0
    # [start, end] seconds per word, from the voice's phoneme durations.
    word_times: list[list[float]] | None = None
    state: str = "pending"  # pending -> ready | failed
    error: str = ""


@dataclass
class ReadingSession:
    session_id: int
    settings: dict[str, Any]
    passages: list[SpeechPassage] = field(default_factory=list)
    play_index: int = 0
    playing_id: int | None = None
    exhausted: bool = False
    cancelled: bool = False
    paused: bool = False
    replay_current: bool = False
    player: subprocess.Popen[str] | None = None
    cond: threading.Condition = field(default_factory=threading.Condition)


class PiperSynthServer:
    """A warm piper process in --json-input mode: one line in, one WAV out.

    Loading a voice costs ~0.5s on CPU and ~2.5s for the CUDA wrapper, which
    per-passage launches would pay every time.
    """

    def __init__(self, binary: str, model: str, length_scale: float) -> None:
        self.key = (binary, model, round(length_scale, 3))
        self._lines: "queue.Queue[str | None]" = queue.Queue()
        self._lock = threading.Lock()
        self.last_used = time.monotonic()
        self._proc = subprocess.Popen(
            [
                binary,
                "--model",
                model,
                "--json-input",
                "--output_dir",
                tempfile.gettempdir(),
                "--length_scale",
                f"{length_scale:.3f}",
                "--quiet",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()

    def _read_stdout(self) -> None:
        stdout = self._proc.stdout
        if stdout is None:
            return
        try:
            for line in stdout:
                self._lines.put(line.strip())
        except Exception:
            pass
        self._lines.put(None)

    def alive(self) -> bool:
        return self._proc.poll() is None

    def synthesize(self, text: str, out_path: str, timeout: float = 180.0) -> tuple[str, list[list[float]] | None]:
        """Write text to out_path; returns (error or "", word times or None).

        Word times come only from tools/piper_gpu_wrapper.py; the C++ piper
        ignores the "alignments" key and answers with the path alone."""
        request = {"text": text, "output_file": out_path, "alignments": True}
        with self._lock:
            if not self.alive():
                return "Piper process exited", None
            try:
                assert self._proc.stdin is not None
                self._proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
                self._proc.stdin.flush()
            except Exception as exc:
                return f"Piper input failed: {exc}", None
            try:
                line = self._lines.get(timeout=timeout)
            except queue.Empty:
                # A late reply would be paired with the next request.
                self.close()
                return "Piper timed out", None
            self.last_used = time.monotonic()
        if line is None:
            return "Piper process exited", None
        if line.startswith("ERROR"):
            return line, None
        word_times = None
        if line.startswith("{"):
            try:
                reply = json.loads(line)
                raw_times = reply.get("word_times")
                if isinstance(raw_times, list):
                    word_times = [[float(t[0]), float(t[1])] for t in raw_times]
            except Exception:
                word_times = None
        if not os.path.exists(out_path):
            return f"Piper did not write audio ({line[:120]})", None
        return "", word_times

    def close(self) -> None:
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.close()
        except Exception:
            pass
        if self._proc.poll() is None:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
            except Exception:
                try:
                    self._proc.terminate()
                except Exception:
                    pass


class ReaderController:
    """Reads text aloud passage by passage (see ReadingSession) and
    translates text with local Gemma through Ollama."""

    def __init__(
        self,
        config: ConfigManager,
        on_state_change: Callable[[bool, str], None] | None = None,
        on_translation: Callable[[str, str, str], None] | None = None,
        on_passage: Callable[[int, int, int], None] | None = None,
        on_need_text: Callable[[], None] | None = None,
    ) -> None:
        self.config = config
        self.on_state_change = on_state_change
        self.on_translation = on_translation
        self.on_passage = on_passage
        self.on_need_text = on_need_text
        # Set by the Reader window: reads then load the text there, and the
        # window feeds it back passage by passage; called as
        # (text, source, language, session_id).
        self.document_host: Callable[[str, str, str | None, int], None] | None = None
        self._session: ReadingSession | None = None
        self._piper_server: PiperSynthServer | None = None
        self._piper_server_lock = threading.Lock()
        self._piper_reaper_started = False
        self._piper_json_unsupported: set[str] = set()
        self._passage_audio_counter = 0
        self._lock = threading.Lock()
        self._speaking = False
        self._paused = False
        self._last_source = "none"
        self._run_id = 0
        self._last_text = ""
        self._last_translation = ""
        self._translation_lock = threading.Lock()
        self._translation_id = 0
        self._translation_cancel: threading.Event | None = None
        self._translation_response: Any | None = None

    def is_speaking(self) -> bool:
        with self._lock:
            return self._speaking

    def is_paused(self) -> bool:
        with self._lock:
            return self._paused

    def read_selection_async(self) -> tuple[bool, str]:
        if self.is_speaking():
            self.stop()
        text, source = get_selected_text(self.config)
        if not text:
            return False, "No selected text found"
        self._last_text = text.strip()
        self._last_source = source
        return self._read_text(text, source, None)

    def speak_text_async(self, text: str, language: str | None = None) -> tuple[bool, str]:
        """Speak text without recapturing the system selection."""
        text = text.strip()
        if not text:
            return False, "Nothing to speak"
        self._last_source = "translation"
        return self._read_text(text, "translation", language)

    def _read_text(self, text: str, source: str, language: str | None) -> tuple[bool, str]:
        session_id = self.begin_document_session(language=language)
        if self.document_host is not None:
            self.document_host(text, source, language, session_id)
        else:
            self._enqueue_whole_text(session_id, text)
        return True, "Reading started"

    def _enqueue_whole_text(self, session_id: int, text: str) -> None:
        """Without a Reader window, queue the whole text at once."""
        mode = resolve_preprocess_mode(self.config)
        rest = text
        passage_id = 0
        while rest.strip():
            rest = rest.lstrip()
            first = passage_id == 0
            cut, _complete = next_passage_cut(
                rest,
                READER_FIRST_PASSAGE_CHARS if first else READER_PASSAGE_CHARS,
                1 if first else READER_PASSAGE_MIN_CHARS,
            )
            if cut <= 0:
                break
            passage, rest = rest[:cut].strip(), rest[cut:]
            passage_id += 1
            processed, _changed, _mode = preprocess_text_for_mode(passage, mode)
            self.enqueue_passage(session_id, passage_id, passage, processed or passage)
        self.set_session_exhausted(session_id, True)

    def translate_selection_async(self, target_language: str, speak_aloud: bool = False) -> tuple[bool, str]:
        """Translate the last captured/selected text using the local Ollama model."""
        target_language = target_language.strip()
        if not target_language:
            return False, "Choose a target language first"
        text = self._last_text.strip()
        source = "last selection"
        if not text:
            text, source = get_selected_text(self.config)
            text = text.strip()
        if not text:
            return False, "No selected text found"
        self._last_text = text
        return self.translate_text_async(text, target_language, speak_aloud=speak_aloud, source=source)

    def translate_text_async(
        self,
        text: str,
        target_language: str,
        speak_aloud: bool = False,
        source: str = "reader text",
    ) -> tuple[bool, str]:
        """Translate given text with the local Ollama model."""
        target_language = target_language.strip()
        text = text.strip()
        if not target_language:
            return False, "Choose a target language first"
        if not text:
            return False, "Nothing to translate"
        previous_active = False
        with self._translation_lock:
            previous_event = self._translation_cancel
            previous_response = self._translation_response
            if previous_event is not None:
                previous_active = True
                previous_event.set()
            if previous_response is not None:
                try:
                    previous_response.close()
                except Exception:
                    pass
            self._translation_id += 1
            request_id = self._translation_id
            cancel_event = threading.Event()
            self._translation_cancel = cancel_event
            self._translation_response = None
        log_line(
            f"translation start target={target_language!r} chars={len(text)} model={self.config.get('ollama_model', 'gemma4:latest')!r}"
        )

        def _work() -> None:
            try:
                translated, error = self._translate_with_ollama(
                    text,
                    target_language,
                    cancel_event=cancel_event,
                    request_id=request_id,
                )
                if not self._translation_is_current(request_id, cancel_event):
                    return
                if error:
                    log_line(f"translation failed target={target_language!r}: {error}")
                    self._emit_translation("", target_language, error)
                    return
                self._last_translation = translated
                log_line(f"translation success target={target_language!r} chars={len(translated)}")
                self._emit_translation(translated, target_language, source)
                if speak_aloud:
                    self.speak_text_async(translated, language=target_language)
            finally:
                with self._translation_lock:
                    if self._translation_id == request_id and self._translation_cancel is cancel_event:
                        self._translation_cancel = None

        threading.Thread(target=_work, daemon=True).start()
        if previous_active:
            return True, f"Previous translation cancelled; translating to {target_language}..."
        return True, f"Translating to {target_language}..."

    def cancel_translation(self) -> tuple[bool, str]:
        """Cancel the active Ollama translation and close its HTTP stream."""
        with self._translation_lock:
            cancel_event = self._translation_cancel
            response = self._translation_response
        if cancel_event is None:
            return False, "No translation is running"
        cancel_event.set()
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        log_line("translation cancellation requested")
        return True, "Translation cancellation requested"

    def _translation_is_current(self, request_id: int, cancel_event: threading.Event) -> bool:
        with self._translation_lock:
            return (
                request_id == self._translation_id
                and self._translation_cancel is cancel_event
                and not cancel_event.is_set()
            )

    def _translate_with_ollama(
        self,
        text: str,
        target_language: str,
        *,
        cancel_event: threading.Event | None = None,
        request_id: int | None = None,
    ) -> tuple[str, str | None]:
        payload = {
            "model": str(self.config.get("ollama_model", "gemma4:latest")),
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a precise translator. Detect the source language and translate "
                        f"the text into {target_language}. Output only the translation, with no "
                        "explanation, labels, quotes, markdown, or transliteration. Preserve "
                        "paragraph breaks, names, numbers, punctuation, and Unicode scripts."
                    ),
                },
                {"role": "user", "content": text},
            ],
            "stream": True,
            "options": {
                **ollama_generation_options(self.config, max(256, min(4096, len(text) * 4))),
                "temperature": 0.1,
            },
            # Gemma4 can spend the whole response in its hidden reasoning
            # channel. Translation needs the visible answer only.
            "think": False,
        }
        payload["keep_alive"] = str(self.config.get("ollama_keep_alive", "30m"))
        timeout = int(self.config.get("ollama_timeout_seconds", 300))
        response: Any | None = None
        try:
            req = urllib.request.Request(
                f"{str(self.config.get('ollama_url', 'http://127.0.0.1:11434')).rstrip('/')}/api/chat",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            response = urllib.request.urlopen(req, timeout=timeout)
            with self._translation_lock:
                if (
                    cancel_event is not None
                    and request_id is not None
                    and (
                        request_id != self._translation_id
                        or self._translation_cancel is not cancel_event
                        or cancel_event.is_set()
                    )
                ):
                    response.close()
                    return "", "Translation cancelled"
                self._translation_response = response

            parts: list[str] = []
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    return "", "Translation cancelled"
                raw_line = response.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="ignore").strip()
                if not line or line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line == "[DONE]":
                    break
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices", []) if isinstance(chunk, dict) else []
                choice = choices[0] if choices and isinstance(choices[0], dict) else {}
                delta = choice.get("delta", {}) if isinstance(choice, dict) else {}
                content = delta.get("content", "") if isinstance(delta, dict) else ""
                if not content and isinstance(choice, dict):
                    message = choice.get("message", {})
                    content = message.get("content", "") if isinstance(message, dict) else ""
                # Native /api/chat streaming responses put the assistant
                # message at the top level, unlike the OpenAI-compatible
                # choices[].delta shape.
                if not content and isinstance(chunk, dict):
                    message = chunk.get("message", {})
                    content = message.get("content", "") if isinstance(message, dict) else ""
                if isinstance(content, list):
                    content = "".join(
                        str(item.get("text", "")) for item in content if isinstance(item, dict)
                    )
                if content:
                    parts.append(str(content))
            result = "".join(parts).strip()
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="ignore")
            except Exception:
                detail = str(exc)
            return "", f"Translation failed: {detail or exc}"
        except Exception as exc:
            if cancel_event is not None and cancel_event.is_set():
                return "", "Translation cancelled"
            return "", f"Translation failed: {exc}"
        finally:
            with self._translation_lock:
                if self._translation_response is response:
                    self._translation_response = None
        if not result:
            return "", "Gemma returned an empty translation"
        return result, None

    def _emit_translation(self, text: str, target_language: str, meta: str) -> None:
        if self.on_translation is not None:
            self.on_translation(text, target_language, meta)

    def _terminate_process(self, proc: subprocess.Popen[str] | None) -> None:
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.wait(timeout=0.7)
            return
        except Exception:
            pass
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def stop(self) -> tuple[bool, str]:
        with self._lock:
            session = self._session
        if session is None:
            return False, "Not reading"
        self._end_session(session, "stopped")
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
            session = self._session
        if session is None:
            return False, "Not reading"
        return self._pause_session(session)

    def resume(self) -> tuple[bool, str]:
        with self._lock:
            session = self._session
        if session is None:
            return False, "Not paused"
        return self._resume_session(session)

    def _estimate_words_per_minute(self, engine: str, rate: int, piper_length_scale: float) -> float:
        base = max(90, min(420, 175 + (rate * 2)))
        if engine == "piper":
            return max(80.0, min(500.0, float(base) / max(0.35, piper_length_scale)))
        return float(base)

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

    def _emit_state(self, speaking: bool, reason: str) -> None:
        if self.on_state_change is not None:
            self.on_state_change(speaking, reason)

    # ------------------------------------------------------------------
    # Document sessions: the GUI feeds passages with enqueue_passage() while
    # earlier ones play. Passage ids belong to the GUI (it maps them to text
    # ranges); on_passage reports (passage_id, word_index, word_total).

    def begin_document_session(self, language: str | None = None) -> int:
        """Stop whatever is reading and start an empty session to feed."""
        with self._lock:
            old_session = self._session
            classic_speaking = self._speaking and old_session is None
        if old_session is not None:
            self._end_session(old_session, "", emit=False)
        elif classic_speaking:
            self.stop()
        settings = self._resolve_speech_settings(language)
        with self._lock:
            self._run_id += 1
            session = ReadingSession(session_id=self._run_id, settings=settings)
            self._session = session
            self._speaking = True
            self._paused = False
        log_line(
            f"session {session.session_id} start engine={settings['engine']} "
            f"model={Path(settings['model']).name if settings['model'] else '-'} language={settings['language']!r}"
        )
        self._emit_state(True, "reading")
        threading.Thread(target=self._session_play_loop, args=(session,), daemon=True).start()
        if settings["engine"] == "piper":
            threading.Thread(target=self._session_synth_loop, args=(session,), daemon=True).start()
        return session.session_id

    def active_session_id(self) -> int | None:
        with self._lock:
            return self._session.session_id if self._session is not None else None

    def _session_for(self, session_id: int) -> ReadingSession | None:
        with self._lock:
            session = self._session
        if session is None or session.session_id != session_id:
            return None
        return session

    def enqueue_passage(self, session_id: int, passage_id: int, text: str, speak_text: str | None = None) -> bool:
        session = self._session_for(session_id)
        if session is None:
            return False
        speak = (text if speak_text is None else speak_text).strip()
        passage = SpeechPassage(
            passage_id=passage_id,
            text=text,
            speak_text=speak,
            words=re.findall(r"\S+", text),
        )
        with session.cond:
            if session.cancelled:
                return False
            session.passages.append(passage)
            session.exhausted = False
            session.cond.notify_all()
        return True

    def session_wants_more(self, session_id: int) -> bool:
        session = self._session_for(session_id)
        if session is None:
            return False
        with session.cond:
            if session.cancelled:
                return False
            upcoming = session.passages[session.play_index :]
            return (
                len(upcoming) < READER_LOOKAHEAD_PASSAGES
                and sum(len(p.speak_text) for p in upcoming) < READER_LOOKAHEAD_CHARS
            )

    def set_session_exhausted(self, session_id: int, exhausted: bool) -> None:
        session = self._session_for(session_id)
        if session is None:
            return
        with session.cond:
            session.exhausted = exhausted
            session.cond.notify_all()

    def truncate_session(self, session_id: int, from_passage_id: int) -> list[int]:
        """Drop from_passage_id and every later passage that has not started
        playing (their text was edited). Returns the dropped passage ids."""
        session = self._session_for(session_id)
        if session is None:
            return []
        with session.cond:
            index = next(
                (i for i, p in enumerate(session.passages) if p.passage_id == from_passage_id),
                None,
            )
            if index is None:
                return []
            first_unstarted = session.play_index + (1 if session.playing_id is not None else 0)
            start = max(index, first_unstarted)
            dropped = session.passages[start:]
            del session.passages[start:]
            session.exhausted = False
            session.cond.notify_all()
        for passage in dropped:
            self._remove_passage_audio(passage)
        return [p.passage_id for p in dropped]

    def shutdown(self) -> None:
        with self._lock:
            session = self._session
        if session is not None:
            self._end_session(session, "", emit=False)
        with self._piper_server_lock:
            server = self._piper_server
            self._piper_server = None
        if server is not None:
            server.close()

    def _resolve_speech_settings(self, language_override: str | None) -> dict[str, Any]:
        # A voice for the language, else a system voice rather than reading
        # Hebrew through an English model.
        engine = str(self.config.get("engine", "spd-say")).strip() or "spd-say"
        language = str(language_override or self.config.get("language", "en")).strip()
        piper_model = str(self.config.get("piper_model", "")).strip()
        selected_model = ""
        if engine == "piper":
            if piper_model and piper_model_matches_language(piper_model, language):
                selected_model = piper_model
            else:
                selected_model = choose_piper_model_for_language(list_piper_model_paths(), language)
            if not selected_model:
                for fallback_engine in ("espeak-ng", "espeak", "spd-say"):
                    if shutil.which(fallback_engine):
                        log_line(f"tts: no Piper voice for language={language!r}; falling back to {fallback_engine}")
                        engine = fallback_engine
                        break
        rate = int(self.config.get("rate", 0))
        return {
            "engine": engine,
            "model": selected_model,
            "binary": resolve_piper_binary(self.config) if engine == "piper" else "",
            "rate": rate,
            "pitch": int(self.config.get("pitch", 0)),
            "language": language,
            "speech_language": normalize_language_family(language),
            "voice": str(self.config.get("voice", "")).strip(),
            "voice_type": str(self.config.get("voice_type", "male1")).strip(),
            "output_module": str(self.config.get("output_module", "")).strip(),
            "device": str(self.config.get("audio_output_device", "")).strip(),
            # lower length_scale => faster; keep range conservative for intelligibility
            "length_scale": max(0.60, min(1.60, 1.00 - (rate / 220.0))),
        }

    def _emit_passage(self, passage_id: int, word_index: int, word_total: int) -> None:
        if self.on_passage is not None:
            self.on_passage(passage_id, word_index, word_total)

    def _emit_need_text(self) -> None:
        if self.on_need_text is not None:
            self.on_need_text()

    def _remove_passage_audio(self, passage: SpeechPassage) -> None:
        if passage.wav_path:
            try:
                os.unlink(passage.wav_path)
            except Exception:
                pass
            passage.wav_path = ""

    def _pause_session(self, session: ReadingSession) -> tuple[bool, str]:
        with session.cond:
            if session.cancelled:
                return False, "Not reading"
            if session.paused:
                return False, "Already paused"
            session.paused = True
            player = session.player
            live_engine = session.settings["engine"] == "spd-say"
            if player is not None and live_engine:
                session.replay_current = True
        if player is not None:
            if live_engine:
                # speech-dispatcher cannot be frozen; the passage is spoken
                # again from its start on resume.
                self._stop_speech_dispatcher()
                self._terminate_process(player)
            else:
                self._signal_process_group(player, signal.SIGSTOP)
        with self._lock:
            if self._session is session:
                self._speaking = False
                self._paused = True
        self._emit_state(False, "paused")
        return True, "Paused"

    def _resume_session(self, session: ReadingSession) -> tuple[bool, str]:
        with session.cond:
            if session.cancelled or not session.paused:
                return False, "Not paused"
            session.paused = False
            player = session.player
            session.cond.notify_all()
        if player is not None and player.poll() is None:
            self._signal_process_group(player, signal.SIGCONT)
        with self._lock:
            if self._session is session:
                self._speaking = True
                self._paused = False
        self._emit_state(True, "reading")
        return True, "Resumed"

    def _signal_process_group(self, proc: subprocess.Popen[str], sig: int) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except Exception:
            try:
                proc.send_signal(sig)
            except Exception:
                pass

    def _stop_speech_dispatcher(self) -> None:
        try:
            if shutil.which("spd-say"):
                subprocess.run(["spd-say", "-S"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def _end_session(self, session: ReadingSession, reason: str, emit: bool = True) -> None:
        with session.cond:
            already = session.cancelled
            session.cancelled = True
            player = session.player
            passages = list(session.passages)
            session.cond.notify_all()
        if player is not None:
            # A frozen (paused) player only acts on SIGTERM once continued.
            self._signal_process_group(player, signal.SIGCONT)
            self._terminate_process(player)
        if session.settings["engine"] == "spd-say":
            self._stop_speech_dispatcher()
        with self._lock:
            if self._session is session:
                self._session = None
                self._speaking = False
                self._paused = False
                self._run_id += 1
        for passage in passages:
            self._remove_passage_audio(passage)
        if emit and reason and not already:
            self._emit_state(False, reason)

    def _session_play_loop(self, session: ReadingSession) -> None:
        engine = session.settings["engine"]
        caught_up_since: float | None = None
        played_any = False
        failures_in_row = 0
        while True:
            passage: SpeechPassage | None = None
            finished = False
            with session.cond:
                while True:
                    if session.cancelled:
                        return
                    if not session.paused:
                        if session.play_index < len(session.passages):
                            caught_up_since = None
                            candidate = session.passages[session.play_index]
                            if engine != "piper" or candidate.state != "pending":
                                passage = candidate
                                break
                        elif session.exhausted:
                            # Give the GUI a moment in case text is arriving.
                            if caught_up_since is None:
                                caught_up_since = time.monotonic()
                            elif time.monotonic() - caught_up_since > 0.35:
                                finished = True
                                break
                        else:
                            caught_up_since = None
                    session.cond.wait(0.1)
                if passage is not None:
                    session.playing_id = passage.passage_id
            if finished:
                self._end_session(session, "done")
                return
            assert passage is not None
            self._emit_need_text()
            ok = self._play_passage(session, passage)
            with session.cond:
                if session.cancelled:
                    return
                session.playing_id = None
                if session.replay_current:
                    session.replay_current = False
                    continue
                if session.play_index < len(session.passages) and session.passages[session.play_index] is passage:
                    session.play_index += 1
                session.cond.notify_all()
            self._remove_passage_audio(passage)
            if not ok:
                detail = passage.error or "TTS failed"
                log_line(f"session {session.session_id} passage {passage.passage_id} failed: {detail!r}")
                failures_in_row += 1
                # One odd passage should not stop a long read; a broken engine
                # fails from the start or keeps failing.
                if not played_any or failures_in_row >= 3:
                    notify("Voice Reading", detail, urgency="critical")
                    self._end_session(session, "error")
                    return
            else:
                played_any = True
                failures_in_row = 0
            self._emit_need_text()

    def _play_passage(self, session: ReadingSession, passage: SpeechPassage) -> bool:
        settings = session.settings
        engine = settings["engine"]
        device = settings["device"]
        self._emit_passage(passage.passage_id, 0, len(passage.words))
        if not passage.speak_text.strip():
            return True

        if engine == "piper":
            if passage.state != "ready":
                return False
            if not passage.wav_path:
                return True  # nothing speakable (e.g. punctuation only)
            player_cmds = list_audio_player_commands(passage.wav_path, device)
            if not player_cmds:
                passage.error = "No audio player found (pw-play/aplay/paplay/ffplay/mpv)"
                return False
            self._start_passage_tracker(session, passage, passage.duration)
            last_error = ""
            for player_cmd in player_cmds:
                ok, last_error = self._run_session_process(
                    session,
                    player_cmd,
                    env=audio_output_env(device),
                )
                if ok or session.cancelled:
                    return True
            passage.error = last_error or "Audio player failed"
            return False

        wpm = self._estimate_words_per_minute(engine, settings["rate"], piper_length_scale=1.0)
        estimated = max(0.3, len(passage.words) * (60.0 / max(60.0, wpm)))
        if engine in {"espeak-ng", "espeak"} and shutil.which(engine):
            wpm_arg = max(90, min(420, 175 + (settings["rate"] * 2)))
            cmd = [engine, "-s", str(wpm_arg), "-p", str(max(0, min(99, 50 + settings["pitch"] // 2))), "--stdin"]
            if settings["voice"]:
                cmd.extend(["-v", settings["voice"]])
            elif settings["speech_language"]:
                cmd.extend(["-v", settings["speech_language"]])
            self._start_passage_tracker(session, passage, estimated)
            ok, error = self._run_session_process(
                session, cmd, env=audio_output_env(device), input_text=passage.speak_text
            )
            passage.error = error
            return ok or session.cancelled
        if engine == "spd-say" and shutil.which("spd-say"):
            cmd = ["spd-say", "-w", "-r", str(settings["rate"]), "-p", str(settings["pitch"]), "-N", "voice-reading", "-n", "main"]
            if settings["output_module"]:
                cmd.extend(["-o", settings["output_module"]])
            if settings["speech_language"]:
                cmd.extend(["-l", settings["speech_language"]])
            if settings["voice_type"]:
                cmd.extend(["-t", settings["voice_type"]])
            if settings["voice"]:
                cmd.extend(["-y", settings["voice"]])
            cmd.append(passage.speak_text)
            if device:
                threading.Thread(target=route_speech_to_output_device, args=(device,), daemon=True).start()
            self._start_passage_tracker(session, passage, estimated)
            ok, error = self._run_session_process(session, cmd)
            passage.error = error
            return ok or session.cancelled or session.replay_current
        passage.error = f"TTS engine not available: {engine}"
        return False

    def _run_session_process(
        self,
        session: ReadingSession,
        cmd: list[str],
        env: dict[str, str] | None = None,
        input_text: str | None = None,
    ) -> tuple[bool, str]:
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE if input_text is not None else None,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,
                text=True,
                env=env,
            )
        except Exception as exc:
            return False, str(exc)
        with session.cond:
            if session.cancelled:
                self._terminate_process(proc)
                return True, ""
            session.player = proc
            paused = session.paused
        if paused:
            self._signal_process_group(proc, signal.SIGSTOP)
        try:
            _, stderr_text = proc.communicate(input=input_text, timeout=None)
        except Exception as exc:
            stderr_text = str(exc)
        with session.cond:
            if session.player is proc:
                session.player = None
        if proc.returncode == 0:
            return True, ""
        return False, (stderr_text or "").strip() or f"{cmd[0]} failed"

    def _start_passage_tracker(self, session: ReadingSession, passage: SpeechPassage, duration: float) -> None:
        if not passage.words:
            return
        exact = passage.word_times is not None and len(passage.word_times) == len(passage.words)
        if exact:
            offsets = [start for start, _end in passage.word_times or []]
        else:
            offsets = self._estimate_word_offsets(passage.words, max(0.05, duration))
        try:
            # Time from starting the player to hearing its first sample.
            delay = float(self.config.get("highlight_delay_seconds", 0.06))
        except (TypeError, ValueError):
            delay = 0.06
        poll = 0.02 if exact else 0.08

        def _run() -> None:
            played = -delay
            last_tick = time.monotonic()
            last_index = 0
            while True:
                time.sleep(poll)
                with session.cond:
                    if session.cancelled or session.playing_id != passage.passage_id:
                        return
                    paused = session.paused
                now = time.monotonic()
                if not paused:
                    played += now - last_tick
                last_tick = now
                index = max(0, min(len(offsets) - 1, bisect.bisect_right(offsets, played) - 1))
                if index != last_index:
                    last_index = index
                    self._emit_passage(passage.passage_id, index, len(passage.words))

        threading.Thread(target=_run, daemon=True).start()

    def _session_synth_loop(self, session: ReadingSession) -> None:
        while True:
            with session.cond:
                target: SpeechPassage | None = None
                while target is None:
                    if session.cancelled:
                        return
                    target = next(
                        (p for p in session.passages[session.play_index :] if p.state == "pending"),
                        None,
                    )
                    if target is None:
                        session.cond.wait(0.5)
            result = self._synthesize_passage(session, target.speak_text)
            wav_path, duration, error = result[:3]
            word_times = result[3] if len(result) > 3 else None
            with session.cond:
                still_queued = any(p is target for p in session.passages)
                if error:
                    target.state = "failed"
                    target.error = error
                else:
                    target.state = "ready"
                    target.wav_path = wav_path
                    target.duration = duration
                    target.word_times = word_times
                session.cond.notify_all()
                cancelled = session.cancelled
            if cancelled or not still_queued:
                self._remove_passage_audio(target)

    def _synthesize_passage(
        self, session: ReadingSession, text: str
    ) -> tuple[str, float, str, list[list[float]] | None]:
        """Returns (wav_path, duration, error, word_times); an empty wav_path
        with no error means the passage has nothing to speak."""
        settings = session.settings
        binary = settings["binary"]
        model = settings["model"]
        if not binary:
            return "", 0.0, "Piper engine not found", None
        if not model:
            return "", 0.0, "No Piper model found. Add a .onnx voice model.", None
        prepared, error = prepare_piper_text(text, model)
        if error:
            return "", 0.0, error, None
        if not prepared.strip():
            return "", 0.0, "", None
        with self._piper_server_lock:
            self._passage_audio_counter += 1
            counter = self._passage_audio_counter
        wav_path = str(
            Path(tempfile.gettempdir()) / f"voice_reading_{os.getpid()}_{session.session_id}_{counter}.wav"
        )
        error = ""
        word_times: list[list[float]] | None = None
        if binary not in self._piper_json_unsupported:
            server = self._get_piper_server(binary, model, settings["length_scale"])
            if server is not None:
                error, word_times = server.synthesize(prepared, wav_path)
            else:
                error = "Piper failed to start"
            if error == "Piper process exited":
                # An older/other piper build without --json-input: fall back
                # to one process per passage for this binary.
                log_line(f"piper: {binary} has no usable --json-input mode; using one process per passage")
                self._piper_json_unsupported.add(binary)
        if binary in self._piper_json_unsupported:
            error = self._synthesize_passage_once(binary, model, settings["length_scale"], prepared, wav_path)
        duration = 0.0 if error else (self._wav_duration_seconds(wav_path) or 0.0)
        if error or duration <= 0.0:
            try:
                os.unlink(wav_path)
            except Exception:
                pass
            return "", 0.0, error, None  # no error + no audio: nothing to pronounce
        # Hebrew passages are spoken from their phonemized form, whose words
        # need not line up with the displayed ones.
        if prepared != text:
            word_times = None
        return wav_path, duration, "", word_times

    def _synthesize_passage_once(self, binary: str, model: str, length_scale: float, text: str, wav_path: str) -> str:
        try:
            result = subprocess.run(
                [binary, "--model", model, "--output_file", wav_path, "--length_scale", f"{length_scale:.3f}", "--quiet"],
                input=text,
                text=True,
                capture_output=True,
                timeout=180,
                check=False,
                start_new_session=True,
            )
        except Exception as exc:
            return f"Piper failed: {exc}"
        if result.returncode != 0 or not os.path.exists(wav_path):
            return (result.stderr or "").strip()[-400:] or "Piper failed"
        return ""

    def _get_piper_server(self, binary: str, model: str, length_scale: float) -> PiperSynthServer | None:
        key = (binary, model, round(length_scale, 3))
        with self._piper_server_lock:
            server = self._piper_server
            if server is not None and (server.key != key or not server.alive()):
                server.close()
                server = None
                self._piper_server = None
            if server is None:
                try:
                    server = PiperSynthServer(binary, model, length_scale)
                except Exception as exc:
                    log_line(f"piper: could not start {binary}: {exc}")
                    return None
                self._piper_server = server
                if not self._piper_reaper_started:
                    self._piper_reaper_started = True
                    threading.Thread(target=self._piper_reaper_loop, daemon=True).start()
            return server

    def _piper_reaper_loop(self) -> None:
        # A warm voice holds (V)RAM; release it once reading has been idle a while.
        while True:
            time.sleep(15)
            try:
                keep_warm = float(self.config.get("piper_keep_warm_seconds", PIPER_KEEP_WARM_SECONDS_DEFAULT))
            except (TypeError, ValueError):
                keep_warm = PIPER_KEEP_WARM_SECONDS_DEFAULT
            with self._lock:
                busy = self._session is not None
            with self._piper_server_lock:
                server = self._piper_server
                if server is None or busy or time.monotonic() - server.last_used < keep_warm:
                    continue
                self._piper_server = None
            server.close()
            log_line("piper: released idle voice")


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
        self._bound_inode: int | None = None
        self._last_toggle = 0.0

    def stop(self) -> None:
        self._stop_event.set()
        if self._socket is not None:
            try:
                self._socket.close()
            except Exception:
                pass
        # Only remove the socket file this server created: another instance
        # may have bound the path since, and deleting it would cut that
        # instance off from every hotkey.
        try:
            if self._bound_inode is not None and os.stat(SOCKET_PATH).st_ino == self._bound_inode:
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
            self._bound_inode = os.stat(SOCKET_PATH).st_ino
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
                        req_raw = conn.recv(1024 * 1024)
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

        if cmd == "translate":
            target = str(self.config.get("translation_target_language", "English")).strip() or "English"
            ok, message = self.controller.translate_selection_async(target, speak_aloud=False)
            if ok and self.on_show is not None:
                self.on_show(False)
            return {"ok": ok, "message": message, "speaking": self.controller.is_speaking()}

        if cmd == "speak_text":
            text = str(req.get("text", "")).strip()
            language = str(req.get("language", "")).strip() or None
            ok, message = self.controller.speak_text_async(text, language=language)
            if ok and self.on_show is not None and bool(self.config.get("show_panel_on_hotkey", True)):
                self.on_show(False)
            return {"ok": ok, "message": message, "speaking": self.controller.is_speaking()}

        if cmd == "cancel_translation":
            ok, message = self.controller.cancel_translation()
            return {"ok": ok, "message": message, "speaking": self.controller.is_speaking()}

        if cmd == "stop":
            ok, message = self.controller.stop()
            return {"ok": ok, "message": message, "speaking": self.controller.is_speaking()}

        if cmd == "show":
            if self.on_show is not None:
                self.on_show(True)
            return {"ok": True, "message": "shown"}

        if cmd == "status":
            return {
                "ok": True,
                "speaking": self.controller.is_speaking(),
                "engine": self.config.get("engine", ""),
                "piper_runtime": piper_runtime_label(self.config),
                "piper_gpu_enabled": bool(self.config.get("piper_gpu_enabled", False)),
            }

        return {"ok": False, "error": f"unknown command: {cmd}"}


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


def spawn_gui(read_on_launch: bool, translate_on_launch: bool = False) -> None:
    cmd = [sys.executable, str(Path(__file__).resolve()), "gui"]
    if read_on_launch:
        cmd.append("--read-on-launch")
    if translate_on_launch:
        cmd.append("--translate-on-launch")
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def reuse_existing_gui(read_on_launch: bool, translate_on_launch: bool = False) -> bool:
    status = send_ipc_with_retries({"cmd": "status"}, attempts=2, timeout=0.6)
    if status is None:
        return False
    if read_on_launch:
        send_ipc_with_retries({"cmd": "toggle"}, attempts=2, timeout=0.8)
    elif translate_on_launch:
        send_ipc_with_retries({"cmd": "translate"}, attempts=2, timeout=1.0)
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


def cmd_translate() -> int:
    resp = send_ipc_with_retries({"cmd": "translate"}, attempts=2, timeout=1.0)
    if resp is not None:
        print(resp.get("message", "ok"))
        return 0 if resp.get("ok", False) else 1
    spawn_gui(read_on_launch=False, translate_on_launch=True)
    print("Started Voice Reading and began translation")
    return 0


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

    gui_parser = sub.add_parser("gui", help="Open Voice Studio on the Reader tab")
    gui_parser.add_argument("--read-on-launch", action="store_true")
    gui_parser.add_argument("--translate-on-launch", action="store_true")

    sub.add_parser("toggle", help="Toggle read/stop via IPC (for hotkey)")
    sub.add_parser("read", help="Read selected text")
    sub.add_parser("translate", help="Translate selected text in the Reader GUI")
    sub.add_parser("stop", help="Stop reading")
    sub.add_parser("status", help="Show server status")
    sub.add_parser("list-gb-voices", help="List downloadable British piper voices")

    download_parser = sub.add_parser("download-voice", help="Download a British piper voice")
    download_parser.add_argument("voice", help="Voice key from list-gb-voices (example: en_GB-jenny_dioco-medium)")

    args = parser.parse_args()

    if args.command == "gui":
        if reuse_existing_gui(
            read_on_launch=bool(args.read_on_launch),
            translate_on_launch=bool(args.translate_on_launch),
        ):
            return 0
        studio_argv = [sys.executable, str(SCRIPT_DIR / "voice_studio.py")]
        if args.read_on_launch:
            studio_argv.append("--read-on-launch")
        elif args.translate_on_launch:
            studio_argv.append("--translate-on-launch")
        else:
            studio_argv.extend(["--tab", "reader"])
        os.execv(sys.executable, studio_argv)
    if args.command == "toggle":
        return cmd_toggle()
    if args.command == "read":
        return cmd_read()
    if args.command == "translate":
        return cmd_translate()
    if args.command == "stop":
        return cmd_stop()
    if args.command == "status":
        return cmd_status()
    if args.command == "list-gb-voices":
        catalog = list_gb_voice_catalog()
        if not catalog:
            print("Voice index not found")
            return 1
        for item in catalog:
            mark = "[installed]" if item["installed"] else ""
            print(f"{item['key']:<42} {item['quality']:<8} {item['size_mb']:>4} MB {mark}")
        return 0
    if args.command == "download-voice":
        ok, message = download_piper_voice(args.voice, progress=lambda msg: print(msg, end="\r", flush=True))
        print()
        print(message)
        return 0 if ok else 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
