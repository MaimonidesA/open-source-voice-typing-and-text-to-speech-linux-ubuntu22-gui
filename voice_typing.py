#!/usr/bin/env python3
"""Offline voice typing controller with GUI + hotkey toggle.

Main goals:
- Visible recording state in a small GUI.
- Win+H (or any shortcut) can start/stop recording through IPC.
- Transcript goes back to the original focused field when possible.
- Safe fallback to clipboard when direct insertion is unavailable.
"""

from __future__ import annotations

import argparse
import ast
import base64
import fcntl
import importlib.util
import json
import os
import queue
import re
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import messagebox, ttk

try:
    import pyatspi  # type: ignore
except Exception:
    pyatspi = None

APP_NAME = "Voice Typing"
SCRIPT_DIR = Path(__file__).resolve().parent


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
CONFIG_PATH = CONFIG_DIR / "config.json"
STATE_DIR = Path("/tmp/voice_typing")
SOCKET_PATH = "/tmp/voice_typing.sock"
AUDIO_PATH = STATE_DIR / "recording.wav"
CHUNK_DIR = STATE_DIR / "chunks"
TRANSCRIPT_PREFIX = STATE_DIR / "transcript"
ROUTE_LOG_PATH = STATE_DIR / "routing.log"
SPAWN_LOCK_PATH = "/tmp/voice_typing_spawn.lock"
TOGGLE_DEBOUNCE_PATH = "/tmp/voice_typing_toggle.debounce"
TOGGLE_DEBOUNCE_SECONDS = 0.8
MIN_HOTKEY_RECORDING_SECONDS_BEFORE_STOP = 1.5
GEMMA4_MAX_AUDIO_SECONDS = 30
SUPPORTED_TRANSCRIPTION_ENGINES = ("whisper", "ollama", "gemma4-transformers")
ENGINE_ALIASES = {
    "gemma": "ollama",
    "gemma4": "ollama",
    "gemma-4": "ollama",
}
LEGACY_WHISPER_CLI_PATH = "~/whisper.cpp/build/bin/whisper-cli"
CUDA_WHISPER_CLI_PATH = "~/whisper.cpp/build-cuda/bin/whisper-cli"


def prefer_cuda_whisper_cli(data: dict[str, Any]) -> None:
    if (
        str(data.get("whisper_cli_path", "")).strip() == LEGACY_WHISPER_CLI_PATH
        and Path(os.path.expanduser(CUDA_WHISPER_CLI_PATH)).exists()
    ):
        data["whisper_cli_path"] = CUDA_WHISPER_CLI_PATH


DEFAULT_CONFIG: dict[str, Any] = {
    "transcription_engine": "whisper",
    "whisper_cli_path": LEGACY_WHISPER_CLI_PATH,
    "models_dir": "~/whisper.cpp/models",
    "model": "",
    "gemma_model": "google/gemma-4-E4B-it",
    "gemma_model_path": "",
    "gemma_backend": "pipeline",
    "gemma_device_map": "auto",
    "gemma_dtype": "auto",
    "gemma_max_new_tokens": 192,
    "gemma_transcribe_prompt": (
        "Transcribe the following speech segment in its original language. "
        "Follow these specific instructions for formatting the answer:\n"
        "* Only output the transcription, with no newlines.\n"
        "* When transcribing numbers, write the digits, i.e. write 1.7 and not one point seven, "
        "and write 3 instead of three."
    ),
    "ollama_url": "http://127.0.0.1:11434",
    "ollama_model": "gemma4:latest",
    "ollama_timeout_seconds": 300,
    "ollama_keep_alive": "30m",
    "ollama_max_tokens": 512,
    "ollama_disable_thinking": True,
    "ollama_normalize_audio": True,
    "assistant_answer_mode": False,
    "answer_speak_aloud": True,
    "answer_voice_model": "",
    "ollama_ask_system_prompt": (
        "You are a helpful local assistant. Answer briefly in plain text only - "
        "no markdown, no headers, no bullet symbols. Your answer is typed "
        "directly into the user's focused text field."
    ),
    "ollama_answer_max_tokens": 600,
    "ollama_transcribe_prompt": (
        "Transcribe this audio segment. Only output the spoken words. "
        "If there is no speech, output nothing."
    ),
    "recording_max_seconds": 1800,
    "recording_warn_before_seconds": 30,
    "transcribe_timeout_seconds": 90,
    "audio_input_device": "",
    "save_recordings": True,
    "recordings_dir": "~/.local/share/voice-typing/recordings",
    "recordings_keep": 30,
    "streaming_transcription": False,
    "streaming_chunk_seconds": 28,
    "streaming_route_partials": True,
    "no_gpu": False,
    "language": "en",
    "voice_commands_enabled": True,
    "voice_command_prefixes": ["voice typing", "computer", "hey antonio", "good morning antonio"],
    "assistant_wake_mode": False,
    "assistant_wake_phrases": ["hey antonio", "good morning antonio"],
    "assistant_awake_seconds": 20,
    "assistant_ask_target_when_uncertain": True,
    "voice_prompts_enabled": False,
    "ask_when_focus_uncertain": False,
    "auto_paste_current_focus": False,
    "copy_to_clipboard_when_started_from_gui": False,
    "direct_type_fallback_for_hotkey": True,
    "paste_fallback_for_hotkey": True,
    "restore_clipboard_after_paste": False,
    "window_always_on_top": True,
    "show_panel_on_hotkey_start": True,
    "window_opacity": 0.95,
}

PROFILE_CONFIGS: dict[str, dict[str, Any]] = {
    "stable": {
        "transcription_engine": "whisper",
        # Long dictation is normal use; the archive keeps audio safe anyway.
        "recording_max_seconds": 1800,
        "recording_warn_before_seconds": 30,
        "streaming_transcription": False,
        "streaming_chunk_seconds": 28,
        "streaming_route_partials": True,
        "assistant_wake_mode": False,
        "voice_prompts_enabled": False,
        "assistant_answer_mode": False,
    },
    "ask-gemma": {
        # Best of both: Whisper transcribes the spoken question (it is far
        # more accurate than Gemma on accented speech), gemma4 via Ollama
        # answers it, and the answer is routed like a transcript.
        "transcription_engine": "whisper",
        "recording_max_seconds": 180,
        "recording_warn_before_seconds": 15,
        "streaming_transcription": False,
        "assistant_wake_mode": False,
        "voice_prompts_enabled": False,
        "assistant_answer_mode": True,
    },
    "gemma-agent": {
        # Offline agent pipeline: gemma4 through Ollama does the speech
        # recognition so the whole stack runs without whisper.cpp or network.
        # Gemma audio prompts are capped at 30s, so rolling chunks stay on.
        "transcription_engine": "ollama",
        "recording_max_seconds": 180,
        "recording_warn_before_seconds": 15,
        "streaming_transcription": True,
        "streaming_chunk_seconds": 28,
        "streaming_route_partials": True,
        "assistant_wake_mode": False,
        "voice_prompts_enabled": False,
        "assistant_answer_mode": False,
    },
    "antonio": {
        # Wake-word detection needs a reliable speech recognizer. The local
        # Ollama Gemma audio model is kept available for later command
        # reasoning, but Whisper is currently the better front-end STT.
        "transcription_engine": "whisper",
        "recording_max_seconds": 0,
        "recording_warn_before_seconds": 0,
        "streaming_transcription": True,
        "streaming_chunk_seconds": 5,
        "streaming_route_partials": True,
        "assistant_wake_mode": True,
        "assistant_wake_phrases": ["hey antonio", "good morning antonio"],
        "voice_command_prefixes": ["voice typing", "computer", "hey antonio", "good morning antonio"],
        "assistant_awake_seconds": 20,
        "assistant_ask_target_when_uncertain": True,
        "voice_prompts_enabled": True,
        "voice_commands_enabled": True,
    },
}


@dataclass
class FocusTarget:
    accessible: Any
    source_app: str
    source_pid: int | None = None
    source_signature: str = ""


class ConfigManager:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            data = dict(DEFAULT_CONFIG)
            prefer_cuda_whisper_cli(data)
            self._select_default_model(data)
            self._save_best_effort(data)
            return data

        try:
            with self.path.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
        except Exception:
            loaded = {}

        data = dict(DEFAULT_CONFIG)
        data.update(loaded)
        prefer_cuda_whisper_cli(data)
        self._select_default_model(data)
        self._save_best_effort(data)
        return data

    def _save(self, data: dict[str, Any]) -> None:
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def _save_best_effort(self, data: dict[str, Any]) -> None:
        try:
            self._save(data)
        except Exception:
            pass

    def save(self) -> None:
        self._save_best_effort(self.data)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.save()

    def models_dir(self) -> Path:
        return Path(os.path.expanduser(str(self.get("models_dir"))))

    def whisper_cli_path(self) -> Path:
        return Path(os.path.expanduser(str(self.get("whisper_cli_path"))))

    def model_path(self) -> Path:
        model = str(self.get("model", "")).strip()
        if not model:
            return Path("")
        if model.startswith("/"):
            return Path(model)
        return self.models_dir() / model

    def transcription_engine(self) -> str:
        engine = str(self.get("transcription_engine", "whisper")).strip().lower()
        engine = ENGINE_ALIASES.get(engine, engine)
        if engine not in SUPPORTED_TRANSCRIPTION_ENGINES:
            return "whisper"
        return engine

    def gemma_model_ref(self) -> str:
        local_path = str(self.get("gemma_model_path", "")).strip()
        if local_path:
            return os.path.expanduser(local_path)
        return str(self.get("gemma_model", "google/gemma-4-E4B-it")).strip()

    def ollama_url(self) -> str:
        return str(self.get("ollama_url", "http://127.0.0.1:11434")).rstrip("/")

    def ollama_model(self) -> str:
        return str(self.get("ollama_model", "gemma4:latest")).strip()

    def list_models(self) -> list[str]:
        models_dir = self.models_dir()
        if not models_dir.exists():
            return []
        return sorted(
            p.name
            for p in models_dir.iterdir()
            if p.is_file() and p.suffix == ".bin" and p.name.startswith("ggml-")
        )

    def _select_default_model(self, data: dict[str, Any]) -> None:
        models_dir = Path(os.path.expanduser(str(data.get("models_dir", ""))))
        if not models_dir.exists():
            return

        models = sorted(
            p.name
            for p in models_dir.iterdir()
            if p.is_file() and p.suffix == ".bin" and p.name.startswith("ggml-")
        )
        if not models:
            return

        current = str(data.get("model", "")).strip()
        if current and (models_dir / current).exists():
            return

        preferred = [
            "ggml-small.en.bin",
            "ggml-base.en.bin",
            "ggml-small.bin",
            "ggml-base.bin",
        ]
        for candidate in preferred:
            if candidate in models:
                data["model"] = candidate
                return

        data["model"] = models[0]


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


def speak_prompt(text: str, enabled: bool) -> None:
    if not enabled:
        return
    if shutil.which("spd-say") is None:
        return
    try:
        subprocess.Popen(
            ["spd-say", text],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


def log_routing(message: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with ROUTE_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"[{ts}] {message}\n")
    except Exception:
        pass


def audio_duration_seconds(path: Path) -> float:
    """Duration of a PCM WAV file; 0.0 if it cannot be determined."""
    try:
        import wave

        with wave.open(str(path), "rb") as wav:
            frames = wav.getnframes()
            rate = wav.getframerate() or 1
            return frames / float(rate)
    except Exception:
        return 0.0


def analyze_audio_quality(path: Path) -> tuple[str, str]:
    """Cheap microphone-quality check via `sox ... -n stats` (reads the PCM
    once in C; negligible load). Returns (verdict, detail), empty on failure.
    Verdicts: good | quiet | very_quiet | clipping | silent."""
    try:
        result = subprocess.run(
            ["sox", str(path), "-n", "stats"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except Exception:
        return "", ""
    rms_db: float | None = None
    peak_db: float | None = None
    for line in (result.stderr or "").splitlines():
        parts = line.split()
        if line.startswith("RMS lev dB") and len(parts) >= 4:
            try:
                rms_db = float(parts[3])
            except ValueError:
                pass
        elif line.startswith("Pk lev dB") and len(parts) >= 4:
            try:
                peak_db = float(parts[3])
            except ValueError:
                pass
    if rms_db is None or peak_db is None:
        return "", ""
    detail = f"RMS {rms_db:.0f} dB, peak {peak_db:.0f} dB"
    if rms_db < -60:
        return "silent", detail
    if peak_db > -1.0:
        return "clipping", detail
    if rms_db < -45:
        return "very_quiet", detail
    if rms_db < -35:
        return "quiet", detail
    return "good", detail


AUDIO_QUALITY_LABELS = {
    "good": "Mic OK",
    "quiet": "Mic quiet — consider raising input volume",
    "very_quiet": "Mic very quiet — check microphone setup",
    "silent": "No signal — wrong input device?",
    "clipping": "Mic clipping — lower input gain",
}


def sox_input_args(config: Any) -> tuple[list[str], dict[str, str] | None]:
    device = str(config.get("audio_input_device", "")).strip()
    if not device or device in {"default", "-d"}:
        return ["-d"], None
    if device.startswith("pulse:"):
        env = os.environ.copy()
        env["PULSE_SOURCE"] = device.split(":", 1)[1]
        return ["-t", "alsa", "pulse"], env
    if device.startswith("alsa:"):
        device = device.split(":", 1)[1]
    return ["-t", "alsa", device], None


def _ollama_loaded_models(base_url: str, timeout: float = 3.0) -> list[dict[str, Any]]:
    """Models currently loaded in memory (GET /api/ps); empty on failure."""
    try:
        req = urllib.request.Request(f"{base_url}/api/ps")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []
    models = payload.get("models", []) if isinstance(payload, dict) else []
    return [item for item in models if isinstance(item, dict)]


def list_ollama_models(base_url: str, timeout: float = 3.0) -> list[str]:
    """Return model names known to the local Ollama server (empty on failure)."""
    try:
        req = urllib.request.Request(f"{base_url}/api/tags")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []
    models = payload.get("models", []) if isinstance(payload, dict) else []
    names = [str(item.get("name", "")) for item in models if isinstance(item, dict)]
    return sorted(name for name in names if name)


def _normalize_command_text(text: str) -> str:
    lowered = text.lower()
    lowered = re.sub(r"[^a-z0-9\s-]", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def strip_leading_phrase(text: str, phrases: list[str]) -> tuple[bool, str]:
    stripped = text.strip()
    for phrase in sorted((p for p in phrases if str(p).strip()), key=len, reverse=True):
        words = [re.escape(part) for part in _normalize_command_text(str(phrase)).split()]
        if not words:
            continue
        pattern = r"^\s*" + r"[\s,.;:!?-]+".join(words) + r"[\s,.;:!?-]*(.*)$"
        match = re.match(pattern, stripped, flags=re.IGNORECASE)
        if match:
            return True, match.group(1).strip()
    return False, text


def parse_voice_command(text: str, prefixes: list[str]) -> tuple[str, str] | None:
    """Return (command, argument) when transcript is a control command."""
    normalized = _normalize_command_text(text)
    if not normalized:
        return None

    command_text = ""
    normalized_prefixes = [_normalize_command_text(prefix) for prefix in prefixes if str(prefix).strip()]
    for prefix in normalized_prefixes:
        if normalized == prefix:
            command_text = ""
            break
        if normalized.startswith(prefix + " "):
            command_text = normalized[len(prefix) :].strip()
            break
    else:
        # A tiny set of direct commands is allowed without prefix because they
        # are unlikely dictation phrases and are useful while hands-free.
        direct_commands = ("stop listening", "stop recording", "cancel dictation")
        if normalized in direct_commands:
            command_text = normalized
        else:
            return None

    if not command_text:
        return "status", ""
    if command_text in {"stop", "stop listening", "stop recording", "finish", "finish listening"}:
        return "stop_listening", ""
    if command_text in {"cancel", "cancel dictation", "discard", "discard that"}:
        return "cancel", ""
    if command_text in {"paste last", "apply last", "insert last"}:
        return "paste_last", ""
    if command_text in {"copy last", "copy that"}:
        return "copy_last", ""
    if command_text in {"focus original", "restore focus", "go back"}:
        return "focus_original", ""
    if command_text in {"list windows", "open windows", "what windows are open", "which windows are open"}:
        return "list_windows", ""
    if command_text in {"where can you write", "where should you write", "where can you type"}:
        return "prompt_target", ""
    if command_text.startswith("write "):
        return "dictate", command_text.removeprefix("write ").strip()
    if command_text.startswith("type "):
        return "dictate", command_text.removeprefix("type ").strip()
    if command_text.startswith("use "):
        return "focus_window", command_text.removeprefix("use ").strip()
    if command_text.startswith("switch to "):
        return "focus_window", command_text.removeprefix("switch to ").strip()
    if command_text.startswith("focus "):
        return "focus_window", command_text.removeprefix("focus ").strip()
    return None


def _find_focused_accessible(node: Any) -> Any | None:
    try:
        state = node.getState()
        if state and state.contains(pyatspi.STATE_FOCUSED):
            return node
    except Exception:
        pass

    try:
        child_count = int(node.childCount)
    except Exception:
        return None

    for index in range(child_count):
        try:
            child = node.getChildAtIndex(index)
        except Exception:
            continue
        found = _find_focused_accessible(child)
        if found is not None:
            return found
    return None


def _app_process_id(app: Any) -> int | None:
    try:
        return int(app.get_process_id())
    except Exception:
        return None


def _app_is_active(app: Any) -> bool:
    try:
        state = app.getState()
        return bool(state and state.contains(pyatspi.STATE_ACTIVE))
    except Exception:
        return False


def _is_non_target_app_name(name: str) -> bool:
    normalized = (name or "").strip().lower()
    # GNOME shell frequently appears focused during global-shortcut handling.
    return normalized in {
        "gnome-shell",
        "voice typing",
    }


def focus_target_to_hint(target: FocusTarget | None) -> dict[str, Any] | None:
    if target is None:
        return None
    return {
        "source_app": target.source_app,
        "source_pid": target.source_pid,
        "source_signature": target.source_signature,
    }


def focus_target_from_hint(hint: dict[str, Any] | None) -> FocusTarget | None:
    if not hint:
        return None
    app = str(hint.get("source_app", "")).strip()
    if not app:
        return None
    pid_raw = hint.get("source_pid")
    pid: int | None = None
    if isinstance(pid_raw, int):
        pid = pid_raw
    elif isinstance(pid_raw, str) and pid_raw.strip().isdigit():
        pid = int(pid_raw.strip())
    sig = str(hint.get("source_signature", "") or "")
    return FocusTarget(accessible=None, source_app=app, source_pid=pid, source_signature=sig)


def capture_focus_target_for_hotkey(retries: int = 8, delay_s: float = 0.04) -> FocusTarget | None:
    # Hotkey handling can momentarily focus shell; sample quickly for a better start target.
    best: FocusTarget | None = None
    for _ in range(max(1, retries)):
        target = capture_focus_target()
        if target is not None:
            best = target
            if not _is_non_target_app_name(target.source_app):
                return target
        time.sleep(delay_s)
    return best


def focus_signature(target: FocusTarget | None) -> tuple[int | None, str, str]:
    if target is None:
        return (None, "", "")
    return (
        target.source_pid,
        (target.source_app or "").strip().lower(),
        (target.source_signature or "").strip().lower(),
    )


def _signature_parts(target: FocusTarget | None) -> list[str]:
    _pid, _app, sig = focus_signature(target)
    if not sig:
        return []
    return [p.strip() for p in sig.split(" > ") if p.strip()]


def _common_prefix_len(parts_a: list[str], parts_b: list[str]) -> int:
    count = 0
    for a, b in zip(parts_a, parts_b):
        if a != b:
            break
        count += 1
    return count


def same_focus_target(a: FocusTarget | None, b: FocusTarget | None) -> bool:
    if a is None and b is None:
        return True
    pid_a, app_a, sig_a = focus_signature(a)
    pid_b, app_b, sig_b = focus_signature(b)

    # Strong match.
    if sig_a and sig_b and sig_a == sig_b:
        return True

    # Tolerant match: same process/app and same window-path prefix.
    if pid_a is not None and pid_b is not None and pid_a == pid_b:
        if app_a and app_b and app_a == app_b:
            parts_a = _signature_parts(a)
            parts_b = _signature_parts(b)
            if not parts_a or not parts_b:
                return True
            return _common_prefix_len(parts_a, parts_b) >= 3
        return True

    # Last-resort match when pid is unavailable.
    if app_a and app_b and app_a == app_b:
        parts_a = _signature_parts(a)
        parts_b = _signature_parts(b)
        if parts_a and parts_b:
            return _common_prefix_len(parts_a, parts_b) >= 3
        return True
    return False


def has_focus_target_identity(target: FocusTarget | None) -> bool:
    if target is None:
        return False
    if target.source_pid is not None:
        return True
    if (target.source_app or "").strip():
        return True
    if (target.source_signature or "").strip():
        return True
    return False


def _accessible_signature(node: Any, depth_limit: int = 8) -> str:
    parts: list[str] = []
    current = node
    depth = 0
    while current is not None and depth < depth_limit:
        try:
            role = str(current.getRoleName() or "?")
        except Exception:
            role = "?"
        try:
            name = str(getattr(current, "name", "") or "")
        except Exception:
            name = ""
        try:
            index = int(current.getIndexInParent())
        except Exception:
            index = -1
        parts.append(f"{role}:{name}:{index}")
        try:
            current = current.parent
        except Exception:
            break
        depth += 1
    return " > ".join(parts)


def _walk_accessible_tree(root: Any, node_limit: int = 5000) -> list[Any]:
    nodes: list[Any] = []
    stack: list[Any] = [root]
    while stack and len(nodes) < node_limit:
        node = stack.pop()
        nodes.append(node)
        try:
            child_count = int(node.childCount)
        except Exception:
            child_count = 0
        # Reverse push for stable left-to-right traversal.
        for idx in range(child_count - 1, -1, -1):
            try:
                child = node.getChildAtIndex(idx)
            except Exception:
                continue
            stack.append(child)
    return nodes


def _normalized_sig_part(part: str) -> str:
    # part is "role:name:index"
    chunks = part.split(":")
    if len(chunks) >= 3:
        role = chunks[0].strip().lower()
        index = chunks[-1].strip()
        return f"{role}:{index}"
    return part.strip().lower()


def _signature_prefix_score(sig_a: str, sig_b: str) -> int:
    parts_a = [p for p in (sig_a or "").split(" > ") if p]
    parts_b = [p for p in (sig_b or "").split(" > ") if p]
    score = 0
    for left, right in zip(parts_a, parts_b):
        if _normalized_sig_part(left) != _normalized_sig_part(right):
            break
        score += 1
    return score


def resolve_focus_target_from_hint(hint: FocusTarget | None) -> FocusTarget | None:
    if hint is None or pyatspi is None:
        return None

    try:
        desktop = pyatspi.Registry.getDesktop(0)
        app_count = int(desktop.childCount)
    except Exception:
        return None

    app_name_hint = (hint.source_app or "").strip().lower()
    best_match: tuple[int, FocusTarget] | None = None

    for index in range(app_count):
        try:
            app = desktop.getChildAtIndex(index)
        except Exception:
            continue
        app_name = (getattr(app, "name", "") or "").strip().lower()
        app_pid = _app_process_id(app)

        if hint.source_pid is not None and app_pid != hint.source_pid:
            continue
        if hint.source_pid is None and app_name_hint and app_name and app_name != app_name_hint:
            continue

        focused = _find_focused_accessible(app)
        if focused is not None and not hint.source_signature:
            return FocusTarget(accessible=focused, source_app=getattr(app, "name", "") or "unknown", source_pid=app_pid)

        for node in _walk_accessible_tree(app, node_limit=5000):
            sig = _accessible_signature(node)
            score = _signature_prefix_score(sig, hint.source_signature)
            if score <= 0:
                continue
            candidate = FocusTarget(
                accessible=node,
                source_app=getattr(app, "name", "") or "unknown",
                source_pid=app_pid,
                source_signature=sig,
            )
            if best_match is None or score > best_match[0]:
                best_match = (score, candidate)

    if best_match is None:
        return None
    # Require sufficient structural match to avoid jumping to unrelated controls.
    if best_match[0] < 3:
        return None
    return best_match[1]


def capture_focus_target() -> FocusTarget | None:
    if pyatspi is None:
        return None

    try:
        desktop = pyatspi.Registry.getDesktop(0)
    except Exception:
        return None

    try:
        app_count = int(desktop.childCount)
    except Exception:
        app_count = 0

    candidates: list[tuple[int, FocusTarget]] = []
    own_pid = os.getpid()

    for index in range(app_count):
        try:
            app = desktop.getChildAtIndex(index)
        except Exception:
            continue
        found = _find_focused_accessible(app)
        if found is not None:
            app_name = getattr(app, "name", "unknown") or "unknown"
            app_pid = _app_process_id(app)
            target = FocusTarget(
                accessible=found,
                source_app=app_name,
                source_pid=app_pid,
                source_signature=_accessible_signature(found),
            )

            # Rank targets: active apps first, and prefer non-self process.
            rank = 0
            if not _app_is_active(app):
                rank += 10
            if app_pid is not None and app_pid == own_pid:
                rank += 5
            if _is_non_target_app_name(app_name):
                rank += 100
            candidates.append((rank, target))

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    chosen = candidates[0][1]
    if _is_non_target_app_name(chosen.source_app):
        return None
    return chosen


def insert_text_into_target(target: FocusTarget, text: str) -> bool:
    if not text:
        return False

    try:
        editable = target.accessible.queryEditableText()
    except Exception:
        return False

    try:
        text_iface = target.accessible.queryText()
        offset = int(text_iface.caretOffset)
        if offset < 0:
            offset = int(text_iface.characterCount)
    except Exception:
        offset = 0

    try:
        editable.insertText(offset, text, len(text))
        return True
    except Exception:
        return False


def focus_target_element(target: FocusTarget) -> bool:
    try:
        component = target.accessible.queryComponent()
        component.grabFocus()
        return True
    except Exception:
        return False


def activate_window_by_pid(pid: int | None) -> bool:
    """Activate a window by PID using GNOME Shell D-Bus eval (compositor-level focus)."""
    if pid is None:
        return False
    if shutil.which("gdbus") is None:
        return False
    js = (
        f"(function() {{"
        f"  let dominated = global.get_window_actors().find("
        f"    a => a.meta_window.get_pid() === {pid}"
        f"  );"
        f"  if (dominated) {{"
        f"    dominated.meta_window.activate(global.get_current_time());"
        f"    return 'activated';"
        f"  }}"
        f"  return 'not_found';"
        f"}})()"
    )
    try:
        result = subprocess.run(
            [
                "gdbus", "call", "--session",
                "--dest", "org.gnome.Shell",
                "--object-path", "/org/gnome/Shell",
                "--method", "org.gnome.Shell.Eval",
                js,
            ],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
        ok = result.returncode == 0 and "activated" in (result.stdout or "")
        if ok:
            log_routing(f"gdbus: activated window for pid={pid}")
        return ok
    except Exception as exc:
        log_routing(f"gdbus: activation failed for pid={pid} ({exc})")
        return False


def activate_window_by_query(query: str) -> bool:
    """Best-effort focus by title or WM class for voice commands like "focus firefox"."""
    query = query.strip().lower()
    if not query:
        return False
    if shutil.which("gdbus") is None:
        return False
    quoted_query = json.dumps(query)
    js = (
        "(function() {"
        f"  let q = {quoted_query};"
        "  let actors = global.get_window_actors();"
        "  for (let actor of actors) {"
        "    let w = actor.meta_window;"
        "    let hay = ((w.get_title && w.get_title()) || '') + ' ' + "
        "              ((w.get_wm_class && w.get_wm_class()) || '');"
        "    if (hay.toLowerCase().includes(q)) {"
        "      w.activate(global.get_current_time());"
        "      return 'activated';"
        "    }"
        "  }"
        "  return 'not_found';"
        "})()"
    )
    try:
        result = subprocess.run(
            [
                "gdbus", "call", "--session",
                "--dest", "org.gnome.Shell",
                "--object-path", "/org/gnome/Shell",
                "--method", "org.gnome.Shell.Eval",
                js,
            ],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
        ok = result.returncode == 0 and "activated" in (result.stdout or "")
        if ok:
            log_routing(f"gdbus: activated window for query={query!r}")
        else:
            log_routing(f"gdbus: no window matched query={query!r}")
        return ok
    except Exception as exc:
        log_routing(f"gdbus: activation failed for query={query!r} ({exc})")
        return False


def _extract_gdbus_eval_string(stdout: str) -> str | None:
    text = stdout.strip()
    match = re.match(r"^\((true|false),\s*(.*)\)$", text, flags=re.DOTALL)
    if not match or match.group(1) != "true":
        return None
    raw_value = match.group(2).strip()
    try:
        value = ast.literal_eval(raw_value)
    except Exception:
        return None
    return str(value)


def list_open_windows(limit: int = 8) -> list[dict[str, Any]]:
    if shutil.which("gdbus") is not None:
        js = (
            "(function() {"
            "  let windows = global.get_window_actors().map((actor, idx) => {"
            "    let w = actor.meta_window;"
            "    return {"
            "      index: idx + 1,"
            "      title: ((w.get_title && w.get_title()) || ''),"
            "      wm_class: ((w.get_wm_class && w.get_wm_class()) || ''),"
            "      pid: w.get_pid()"
            "    };"
            "  }).filter(w => w.title || w.wm_class);"
            f"  return JSON.stringify(windows.slice(0, {int(limit)}));"
            "})()"
        )
        try:
            result = subprocess.run(
                [
                    "gdbus", "call", "--session",
                    "--dest", "org.gnome.Shell",
                    "--object-path", "/org/gnome/Shell",
                    "--method", "org.gnome.Shell.Eval",
                    js,
                ],
                capture_output=True,
                text=True,
                timeout=1.0,
                check=False,
            )
            if result.returncode == 0:
                json_text = _extract_gdbus_eval_string(result.stdout or "")
                if json_text:
                    parsed = json.loads(json_text)
                    if isinstance(parsed, list):
                        return [item for item in parsed if isinstance(item, dict)]
            else:
                log_routing(f"gdbus: window list failed rc={result.returncode} stderr={result.stderr.strip()!r}")
        except Exception as exc:
            log_routing(f"gdbus: window list failed ({exc})")

    return list_open_windows_atspi(limit=limit)


def list_open_windows_atspi(limit: int = 8) -> list[dict[str, Any]]:
    if pyatspi is None:
        return []
    # pyatspi/dbind can abort the process when the accessibility bus is not
    # available, so probe it in a child process and treat crashes as no result.
    code = r"""
import json
import pyatspi

limit = int(__import__("sys").argv[1])
desktop = pyatspi.Registry.getDesktop(0)
windows = []
seen = set()
skip = {"gnome-shell", "mutter", "voice typing"}
for index in range(int(desktop.childCount)):
    if len(windows) >= limit:
        break
    app = desktop.getChildAtIndex(index)
    app_name = str(getattr(app, "name", "") or "").strip()
    if not app_name or app_name.lower() in skip:
        continue
    app_pid = None
    try:
        app_pid = int(app.queryApplication().get_process_id())
    except Exception:
        pass
    found = False
    try:
        child_count = int(app.childCount)
    except Exception:
        child_count = 0
    for child_index in range(child_count):
        if len(windows) >= limit:
            break
        try:
            child = app.getChildAtIndex(child_index)
            role = str(child.getRoleName() or "").lower()
            name = str(getattr(child, "name", "") or "").strip()
        except Exception:
            continue
        if role not in {"frame", "window", "dialog"} and not name:
            continue
        title = name or app_name
        key = (title, app_name, app_pid)
        if key in seen:
            continue
        seen.add(key)
        windows.append({"index": len(windows) + 1, "title": title, "wm_class": app_name, "pid": app_pid})
        found = True
    if not found and len(windows) < limit:
        key = (app_name, app_name, app_pid)
        if key not in seen:
            seen.add(key)
            windows.append({"index": len(windows) + 1, "title": app_name, "wm_class": app_name, "pid": app_pid})
print(json.dumps(windows))
"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", code, str(int(limit))],
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
    except Exception as exc:
        log_routing(f"atspi: window list child failed ({exc})")
        return []
    if result.returncode != 0:
        log_routing(f"atspi: window list child failed rc={result.returncode} stderr={result.stderr.strip()!r}")
        return []
    try:
        parsed = json.loads(result.stdout)
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def format_window_list_for_speech(windows: list[dict[str, Any]]) -> str:
    if not windows:
        return "I cannot read the open windows right now."
    names: list[str] = []
    for item in windows:
        title = str(item.get("title", "")).strip()
        wm_class = str(item.get("wm_class", "")).strip()
        label = title or wm_class
        if wm_class and wm_class.lower() not in label.lower():
            label = f"{wm_class}: {label}"
        if label:
            names.append(label[:80])
    if not names:
        return "I cannot read the open windows right now."
    return "Open windows are: " + "; ".join(names)


def copy_to_clipboard(text: str) -> bool:
    ok, _ = _offer_clipboard_text(text, paste_once=False)
    return ok


def read_clipboard_text() -> str | None:
    if shutil.which("wl-paste") is None:
        return None
    try:
        result = subprocess.run(
            ["wl-paste", "--no-newline"],
            check=False,
            capture_output=True,
            text=True,
            timeout=0.15,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _normalize_clip_text(value: str) -> str:
    return value.replace("\r\n", "\n").rstrip("\n")


def _offer_clipboard_text(text: str, paste_once: bool) -> tuple[bool, subprocess.Popen[str] | None]:
    if shutil.which("wl-copy") is None:
        return False, None

    cmd = ["wl-copy", "--type", "text/plain;charset=utf-8"]
    if paste_once:
        cmd.append("--paste-once")

    try:
        proc: subprocess.Popen[str] = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except Exception as exc:
        log_routing(f"wl-copy spawn failed ({exc})")
        return False, None

    try:
        if proc.stdin is not None:
            proc.stdin.write(text)
            proc.stdin.close()
    except Exception as exc:
        log_routing(f"wl-copy write failed ({exc})")
        try:
            proc.kill()
        except Exception:
            pass
        return False, None

    time.sleep(0.03)
    rc = proc.poll()
    if rc not in (None, 0):
        stderr_text = ""
        try:
            if proc.stderr is not None:
                stderr_text = proc.stderr.read().strip()
        except Exception:
            pass
        log_routing(f"wl-copy failed early (rc={rc}, stderr={stderr_text!r})")
        return False, None

    expected = _normalize_clip_text(text)
    # Keep clipboard verification bounded and fast to avoid hotkey jitter on
    # compositors where wl-paste may block or lag.
    verify_rounds = 2 if paste_once else 5
    for _ in range(verify_rounds):
        current = read_clipboard_text()
        if current is not None and _normalize_clip_text(current) == expected:
            return True, proc if paste_once else None
        time.sleep(0.02)

    if paste_once:
        # For paste-once flow, rely on the active wl-copy owner even if we
        # cannot immediately re-read clipboard contents under Wayland policy.
        log_routing("wl-copy verification skipped (paste_once fast path)")
        return True, proc

    log_routing("wl-copy did not publish expected clipboard content")
    try:
        proc.terminate()
    except Exception:
        pass
    return False, None


def _discover_ydotool_socket() -> str | None:
    candidates: list[str] = []
    from_env = os.environ.get("YDOTOOL_SOCKET", "").strip()
    if from_env:
        candidates.append(from_env)
    candidates.extend(
        [
            f"/run/user/{os.getuid()}/.ydotool_socket",
            "/tmp/.ydotool_socket",
        ]
    )
    seen: set[str] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        try:
            mode = os.stat(path).st_mode
        except Exception:
            continue
        if stat.S_ISSOCK(mode):
            return path
    return None


def _run_ydotool_command(
    cmd: list[str],
    *,
    input_text: str | None = None,
    timeout: float = 1.5,
) -> tuple[bool, str]:
    env = os.environ.copy()
    ydotool_socket = _discover_ydotool_socket()
    if ydotool_socket:
        env["YDOTOOL_SOCKET"] = ydotool_socket
    try:
        result = subprocess.run(
            cmd,
            input=input_text,
            text=(input_text is not None),
            check=False,
            timeout=timeout,
            env=env,
            capture_output=True,
        )
        if result.returncode == 0:
            return True, ""
        return False, (
            f"rc={result.returncode}, socket={ydotool_socket or 'auto'}, "
            f"stderr={result.stderr.strip()!r}"
        )
    except Exception as exc:
        return False, f"exception={exc}"


def _is_terminal_like_focus(target: FocusTarget | None) -> bool:
    if target is None:
        return False
    app = (target.source_app or "").strip().lower()
    if any(token in app for token in ("terminal", "alacritty", "kitty", "wezterm", "foot", "konsole", "xterm")):
        return True
    sig = (target.source_signature or "").strip().lower()
    return "terminal" in sig


def paste_current_focus_via_ydotool(target: FocusTarget | None = None) -> bool:
    if shutil.which("ydotool") is None:
        return False

    # Terminals often require Ctrl+Shift+V instead of Ctrl+V.
    key_sequences: list[list[str]] = []
    if _is_terminal_like_focus(target):
        key_sequences.append(["ydotool", "key", "29:1", "42:1", "47:1", "47:0", "42:0", "29:0"])
        key_sequences.append(["ydotool", "key", "42:1", "110:1", "110:0", "42:0"])  # Shift+Insert
    else:
        key_sequences.append(["ydotool", "key", "29:1", "47:1", "47:0", "29:0"])  # Ctrl+V

    for seq in key_sequences:
        ok, detail = _run_ydotool_command(seq, timeout=1.5)
        if ok:
            return True
        log_routing(f"ydotool paste failed ({detail})")
    return False


def type_text_via_ydotool(text: str) -> bool:
    if not text:
        return False
    if shutil.which("ydotool") is None:
        return False
    timeout_seconds = max(2.0, min(20.0, 1.5 + (len(text) / 45.0)))
    ok, detail = _run_ydotool_command(
        ["ydotool", "type", "-d", "0", "-f", "-"],
        input_text=text,
        timeout=timeout_seconds,
    )
    if ok:
        return True
    log_routing(f"ydotool type failed ({detail})")
    return False


def paste_text_via_clipboard(text: str, restore_clipboard: bool, target: FocusTarget | None = None) -> bool:
    ok, offer_proc = _offer_clipboard_text(text, paste_once=True)
    if not ok:
        return False
    time.sleep(0.02)
    if not paste_current_focus_via_ydotool(target=target):
        if offer_proc is not None:
            try:
                offer_proc.terminate()
            except Exception:
                pass
        return False
    if offer_proc is not None:
        try:
            offer_proc.wait(timeout=1.4)
        except subprocess.TimeoutExpired:
            # Paste likely did not happen if clipboard offer was never consumed.
            try:
                offer_proc.terminate()
            except Exception:
                pass
            log_routing("clipboard offer was not consumed after paste shortcut")
            return False
    if restore_clipboard:
        # Restoring immediately can race Ctrl+V in target apps and paste stale content.
        log_routing("clipboard restore skipped to avoid stale-paste race")
    return True


class SentenceSpeaker:
    """Speaks queued sentences with the bundled piper TTS in a background
    worker so streamed answers play while the model is still generating.
    Synthesis of one sentence is ~0.03x realtime; nothing blocks the GUI."""

    def __init__(self, config: ConfigManager) -> None:
        self.config = config
        self._queue: "queue.Queue[str | None]" = queue.Queue()
        self._player: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()
        self._generation = 0
        self._busy = False
        threading.Thread(target=self._worker, daemon=True).start()

    def is_busy(self) -> bool:
        return self._busy or not self._queue.empty()

    def _piper_binary(self) -> Path | None:
        candidates = [
            Path(__file__).resolve().parent / "piper_runtime" / "piper" / "piper",
            Path(__file__).resolve().parent / "piper" / "piper",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        found = shutil.which("piper")
        return Path(found) if found else None

    def _voice_model(self) -> Path | None:
        configured = str(self.config.get("answer_voice_model", "")).strip()
        if configured:
            path = Path(os.path.expanduser(configured))
            if path.exists():
                return path
        # Reuse the reading app's chosen voice when available.
        try:
            reading_config = json.loads((CONFIG_DIR / "reading_config.json").read_text(encoding="utf-8"))
            path = Path(os.path.expanduser(str(reading_config.get("piper_model", ""))))
            if path.exists():
                return path
        except Exception:
            pass
        voices_root = Path(__file__).resolve().parent / "piper_voices"
        for preferred in ("jenny_dioco", "cori", "alba"):
            for candidate in voices_root.rglob(f"*{preferred}*.onnx"):
                return candidate
        return None

    def available(self) -> bool:
        return self._piper_binary() is not None and self._voice_model() is not None

    def speak(self, sentence: str) -> None:
        sentence = sentence.strip()
        if sentence:
            self._queue.put(sentence)

    def stop(self) -> None:
        with self._lock:
            self._generation += 1
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
            if self._player is not None and self._player.poll() is None:
                try:
                    self._player.terminate()
                except Exception:
                    pass
            self._player = None

    def _worker(self) -> None:
        while True:
            sentence = self._queue.get()
            if sentence is None:
                continue
            self._busy = True
            with self._lock:
                generation = self._generation
            piper = self._piper_binary()
            model = self._voice_model()
            if piper is None or model is None:
                continue
            wav_path = STATE_DIR / "answer_tts.wav"
            try:
                synth = subprocess.run(
                    [str(piper), "--model", str(model), "--output_file", str(wav_path)],
                    input=sentence,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
                if synth.returncode != 0 or not wav_path.exists():
                    continue
                with self._lock:
                    if generation != self._generation:
                        continue
                    player_cmd = next(
                        ([cmd, str(wav_path)] for cmd in ("pw-play", "paplay", "aplay") if shutil.which(cmd)),
                        None,
                    )
                    if player_cmd is None:
                        continue
                    self._player = subprocess.Popen(
                        player_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
                    player = self._player
                player.wait()
            except Exception:
                pass
            finally:
                self._busy = False


class RecorderController:
    def __init__(
        self,
        config: ConfigManager,
        on_state_change: Callable[[bool, str], None] | None = None,
        on_transcript: Callable[[str, str], None] | None = None,
        on_audio_quality: Callable[[str, str], None] | None = None,
    ) -> None:
        self.config = config
        self.on_state_change = on_state_change
        self.on_transcript = on_transcript
        self.on_audio_quality = on_audio_quality
        self.speaker = SentenceSpeaker(config)

        self._lock = threading.Lock()
        self._recording = False
        self._process: subprocess.Popen[str] | None = None
        self._started_at = 0.0
        self._focus_target: FocusTarget | None = None
        self._focus_target_at_start: FocusTarget | None = None
        self._capture_focus = True
        self._started_from_gui = False
        self._last_route_note = ""
        self._transcribing = False
        self._last_known_focus_target: FocusTarget | None = None
        self._last_known_focus_at = 0.0
        self._last_transcript: str = ""
        self._recording_mode = "single"
        self._stop_recording_event: threading.Event | None = None
        self._chunk_queue: "queue.Queue[tuple[int, Path] | None] | None" = None
        self._transcriber_lock = threading.Lock()
        self._gemma_pipe: Any | None = None
        self._gemma_model: Any | None = None
        self._gemma_processor: Any | None = None
        self._gemma_loaded_ref = ""
        self._assistant_awake_until = 0.0
        self._assistant_awaiting_target = False
        self._assistant_pending_transcript = ""

        if pyatspi is not None:
            threading.Thread(target=self._focus_tracker_loop, daemon=True).start()

    def is_recording(self) -> bool:
        with self._lock:
            return self._recording

    def is_transcribing(self) -> bool:
        with self._lock:
            return self._transcribing

    def started_at(self) -> float:
        with self._lock:
            return self._started_at

    def _remember_focus_target(self, target: FocusTarget | None) -> None:
        if target is None:
            return
        if target.source_pid == os.getpid():
            return
        if not has_focus_target_identity(target):
            return
        with self._lock:
            self._last_known_focus_target = target
            self._last_known_focus_at = time.time()

    def _recent_focus_target(self, max_age_seconds: float = 4.0) -> FocusTarget | None:
        now = time.time()
        with self._lock:
            target = self._last_known_focus_target
            seen_at = self._last_known_focus_at
        if target is None:
            return None
        if now - seen_at > max_age_seconds:
            return None
        return target

    def _focus_tracker_loop(self) -> None:
        # Track last real external focus so hotkeys that briefly steal focus still
        # have a stable start target.  Poll aggressively (50ms) so the cached
        # target is almost always fresh when a hotkey fires.
        while True:
            time.sleep(0.05)
            candidate = capture_focus_target()
            if candidate is None:
                continue
            self._remember_focus_target(candidate)

    def restore_last_external_focus(self, max_age_seconds: float = 8.0) -> bool:
        target = self._recent_focus_target(max_age_seconds=max_age_seconds)
        if target is None:
            return False
        if target.source_pid == os.getpid():
            return False
        compositor_ok = activate_window_by_pid(target.source_pid)
        atspi_ok = focus_target_element(target)
        if compositor_ok or atspi_ok:
            log_routing(
                "ui: restored external focus after passive panel show "
                f"app={target.source_app!r} pid={target.source_pid} "
                f"compositor={compositor_ok} atspi={atspi_ok}"
            )
            return True
        return False

    def restore_original_focus_target(self, max_age_seconds: float = 10.0) -> bool:
        # Prefer the recording start target, then resolved hint, then recent focus cache.
        candidates: list[FocusTarget | None] = [self._focus_target, self._focus_target_at_start]
        if self._focus_target is None and self._focus_target_at_start is not None:
            resolved = resolve_focus_target_from_hint(self._focus_target_at_start)
            candidates.append(resolved)
        candidates.append(self._recent_focus_target(max_age_seconds=max_age_seconds))

        for candidate in candidates:
            if candidate is None:
                continue
            if candidate.source_pid == os.getpid():
                continue
            # Activate at compositor level first, then AT-SPI element focus.
            compositor_ok = activate_window_by_pid(candidate.source_pid)
            atspi_ok = focus_target_element(candidate)
            if compositor_ok or atspi_ok:
                log_routing(
                    "focus: restored original target "
                    f"app={candidate.source_app!r} pid={candidate.source_pid} "
                    f"compositor={compositor_ok} atspi={atspi_ok}"
                )
                return True
        return False

    def recent_focus_hint(self, max_age_seconds: float = 8.0) -> dict[str, Any] | None:
        return focus_target_to_hint(self._recent_focus_target(max_age_seconds=max_age_seconds))

    def has_recent_focus(self, max_age_seconds: float = 8.0) -> bool:
        return self._recent_focus_target(max_age_seconds=max_age_seconds) is not None

    def last_transcript(self) -> str:
        return self._last_transcript

    def paste_last_transcript(self) -> tuple[bool, str]:
        """Type the last transcript into the currently focused field via ydotool.

        This does NOT use the clipboard at all — text is injected directly
        through the kernel uinput layer, so the user's clipboard is preserved.
        """
        text = self._last_transcript.strip()
        if not text:
            return False, "No transcript available"
        if self._recording:
            return False, "Recording in progress"
        if self._transcribing:
            return False, "Transcription in progress"
        if type_text_via_ydotool(text):
            log_routing(f"paste_last: typed {len(text)} chars via ydotool")
            return True, "Typed into current focus"
        # Fallback: copy to clipboard without auto-pasting so user can Ctrl+V.
        if copy_to_clipboard(text):
            log_routing("paste_last: ydotool failed, copied to clipboard instead")
            return True, "Copied to clipboard (ydotool unavailable)"
        return False, "Could not type or copy transcript"

    def _engine(self) -> str:
        return self.config.transcription_engine()

    def _should_use_chunked_recording(self) -> bool:
        # Gemma 4 accepts at most 30 seconds of audio per prompt, so it always
        # uses segmented capture. Whisper can opt in for live partial routing.
        return self._engine() in {"gemma4-transformers", "ollama"} or bool(
            self.config.get("streaming_transcription", False)
        )

    def _configured_max_seconds(self) -> int:
        try:
            return int(self.config.get("recording_max_seconds", 180))
        except Exception:
            return 180

    def _chunk_seconds(self) -> float:
        try:
            seconds = float(self.config.get("streaming_chunk_seconds", 28))
        except Exception:
            seconds = 28.0
        seconds = max(1.0, seconds)
        if self._engine() in {"gemma4-transformers", "ollama"}:
            seconds = min(seconds, float(GEMMA4_MAX_AUDIO_SECONDS))
        return seconds

    def _transcriber_preflight_error(self) -> str | None:
        engine = self._engine()
        if engine == "ollama":
            model_name = self.config.ollama_model()
            if not model_name:
                return "Ollama model is not configured"
            names = list_ollama_models(self.config.ollama_url())
            if not names:
                return "Ollama is not reachable (is `ollama serve` running?)"
            if model_name not in names:
                return f"Ollama model not found: {model_name}"
            return None

        if engine == "gemma4-transformers":
            missing = [
                package
                for package in ("transformers", "accelerate", "soundfile")
                if importlib.util.find_spec(package) is None
            ]
            if missing:
                return "Gemma 4 backend missing Python packages: " + ", ".join(missing)
            model_ref = self.config.gemma_model_ref()
            if model_ref.startswith("/") or model_ref.startswith("~"):
                model_path = Path(os.path.expanduser(model_ref))
                if not model_path.exists():
                    return f"Gemma model path not found: {model_path}"
            return None

        whisper_cli = self.config.whisper_cli_path()
        if not whisper_cli.exists():
            return f"whisper-cli not found: {whisper_cli}"
        model_path = self.config.model_path()
        if not model_path.exists():
            return f"Model not found: {model_path}"
        return None

    def _report_audio_quality(self, audio_path: Path) -> str:
        verdict, detail = analyze_audio_quality(audio_path)
        if not verdict:
            return ""
        label = AUDIO_QUALITY_LABELS.get(verdict, verdict)
        if self.on_audio_quality is not None:
            try:
                self.on_audio_quality(verdict, f"{label} ({detail})")
            except Exception:
                pass
        if verdict not in ("good",):
            log_routing(f"audio_quality: {verdict} {detail} file={audio_path.name}")
        return label

    def recordings_dir(self) -> Path:
        return Path(
            os.path.expanduser(str(self.config.get("recordings_dir", "~/.local/share/voice-typing/recordings")))
        )

    def last_recording_path(self) -> Path | None:
        try:
            wavs = sorted(self.recordings_dir().glob("*.wav"))
        except Exception:
            return None
        return wavs[-1] if wavs else None

    def archive_recording(self, sources: list[Path]) -> Path | None:
        """Copy (or concatenate) captured audio into the recordings archive so
        a failed or interrupted transcription never loses speech."""
        if not bool(self.config.get("save_recordings", True)):
            return None
        sources = [p for p in sources if p.exists() and p.stat().st_size > 0]
        if not sources:
            return None
        try:
            dest_dir = self.recordings_dir()
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / time.strftime("recording_%Y%m%d_%H%M%S.wav")
            if len(sources) == 1:
                shutil.copy2(sources[0], dest)
            else:
                result = subprocess.run(
                    ["sox"] + [str(p) for p in sources] + [str(dest)],
                    capture_output=True,
                    text=True,
                    timeout=300,
                    check=False,
                )
                if result.returncode != 0:
                    # Concatenation failed; keep the chunks individually.
                    for index, source in enumerate(sources):
                        shutil.copy2(source, dest_dir / f"{dest.stem}_part{index:03d}.wav")
                    dest = dest_dir / f"{dest.stem}_part000.wav"
            self._prune_recordings(dest_dir)
            log_routing(f"archive_recording: saved {dest}")
            return dest
        except Exception as exc:
            log_routing(f"archive_recording failed: {exc}")
            return None

    def _prune_recordings(self, dest_dir: Path) -> None:
        try:
            keep = int(self.config.get("recordings_keep", 30))
        except Exception:
            keep = 30
        if keep <= 0:
            return
        try:
            wavs = sorted(dest_dir.glob("*.wav"))
            for old in wavs[:-keep]:
                old.unlink(missing_ok=True)
        except Exception:
            pass

    def retranscribe_path_async(self, audio_path: Path | None = None) -> tuple[bool, str]:
        """Re-run transcription on an archived recording (newest by default).
        Result goes to the GUI transcript box and the clipboard; it is never
        auto-typed because the original focus context is long gone."""
        if audio_path is None:
            audio_path = self.last_recording_path()
        if audio_path is None or not audio_path.exists():
            return False, "No saved recordings found"
        with self._lock:
            if self._recording:
                return False, "Recording in progress"
            if self._transcribing:
                return False, "Transcribing previous recording"
            self._transcribing = True

        def _work() -> None:
            try:
                notify("Voice typing", f"Re-transcribing {audio_path.name}...")
                text, err = self._transcribe_audio_path(audio_path)
                if err:
                    notify("Voice typing", err, urgency="critical")
                    return
                transcript = text.strip()
                if not transcript:
                    notify("Voice typing", "Transcript is empty")
                    self._emit_transcript("", "empty")
                    return
                with self._lock:
                    self._last_transcript = transcript
                self._emit_transcript(transcript, "recognized")
                copied = copy_to_clipboard(transcript)
                self._emit_transcript(transcript, "clipboard" if copied else "gui_only")
                notify(
                    "Voice typing",
                    "Re-transcribed and copied to clipboard" if copied else "Re-transcribed (see GUI)",
                )
            finally:
                with self._lock:
                    self._transcribing = False

        threading.Thread(target=_work, daemon=True).start()
        return True, f"Re-transcribing {audio_path.name}"

    def ask_ollama(self, question: str) -> tuple[str, str | None]:
        """Send a text question to the local Ollama model and return a
        plain-text answer (used by ask-gemma mode and the `ask` CLI)."""
        question = question.strip()
        if not question:
            return "", "Empty question"
        payload = {
            "model": self.config.ollama_model(),
            "messages": [
                {
                    "role": "system",
                    "content": str(
                        self.config.get("ollama_ask_system_prompt", DEFAULT_CONFIG["ollama_ask_system_prompt"])
                    ),
                },
                {"role": "user", "content": question},
            ],
            "stream": False,
            "temperature": 0.3,
            "max_tokens": int(self.config.get("ollama_answer_max_tokens", 600)),
        }
        if bool(self.config.get("ollama_disable_thinking", True)):
            payload["reasoning_effort"] = "none"
        timeout_sec = int(self.config.get("ollama_timeout_seconds", 120))
        try:
            req = urllib.request.Request(
                f"{self.config.ollama_url()}/v1/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="ignore")
            except Exception:
                body = str(exc)
            return "", f"Ollama answer failed: {body or exc}"
        except Exception as exc:
            return "", f"Ollama answer failed: {exc}"
        try:
            content = data.get("choices", [])[0].get("message", {}).get("content", "")
        except Exception:
            content = ""
        answer = str(content).strip()
        if not answer:
            return "", "Ollama returned an empty answer"
        return answer, None

    def ask_ollama_stream(self, question: str, on_sentence: Callable[[str], None]) -> tuple[str, str | None]:
        """Like ask_ollama, but streams tokens and emits complete sentences
        through on_sentence as they form, so TTS can start speaking while the
        model is still generating."""
        question = question.strip()
        if not question:
            return "", "Empty question"
        payload = {
            "model": self.config.ollama_model(),
            "messages": [
                {
                    "role": "system",
                    "content": str(
                        self.config.get("ollama_ask_system_prompt", DEFAULT_CONFIG["ollama_ask_system_prompt"])
                    ),
                },
                {"role": "user", "content": question},
            ],
            "stream": True,
            "temperature": 0.3,
            "max_tokens": int(self.config.get("ollama_answer_max_tokens", 600)),
        }
        if bool(self.config.get("ollama_disable_thinking", True)):
            payload["reasoning_effort"] = "none"
        timeout_sec = int(self.config.get("ollama_timeout_seconds", 120))

        sentence_end = re.compile(r"(?<=[.!?:;])\s+")
        full: list[str] = []
        buffer = ""

        def _flush(force: bool = False) -> None:
            nonlocal buffer
            while True:
                match = sentence_end.search(buffer)
                if match:
                    sentence, buffer = buffer[: match.start()], buffer[match.end() :]
                    if sentence.strip():
                        on_sentence(sentence.strip())
                    continue
                break
            if force and buffer.strip():
                on_sentence(buffer.strip())
                buffer = ""

        try:
            req = urllib.request.Request(
                f"{self.config.ollama_url()}/v1/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="ignore").strip()
                    if not line.startswith("data:"):
                        continue
                    data_part = line[5:].strip()
                    if data_part == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_part)
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        token = str(delta.get("content") or "")
                    except Exception:
                        continue
                    if token:
                        full.append(token)
                        buffer += token
                        _flush()
        except Exception as exc:
            if not full:
                return "", f"Ollama answer failed: {exc}"
        _flush(force=True)
        answer = "".join(full).strip()
        if not answer:
            return "", "Ollama returned an empty answer"
        return answer, None

    def warm_up_transcriber_async(self) -> None:
        """Ask Ollama to load the model now so the first chunk does not pay
        the multi-second cold-load penalty. No-op when Ollama is unused."""
        if self._engine() != "ollama" and not bool(self.config.get("assistant_answer_mode", False)):
            return
        model_name = self.config.ollama_model()
        if not model_name:
            return

        def _ping() -> None:
            payload = {
                "model": model_name,
                "keep_alive": str(self.config.get("ollama_keep_alive", "30m")),
            }
            try:
                req = urllib.request.Request(
                    f"{self.config.ollama_url()}/api/generate",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=120):
                    pass
            except Exception:
                pass

        threading.Thread(target=_ping, daemon=True).start()

    def start_recording(
        self,
        capture_focus: bool,
        started_from_gui: bool,
        focus_hint: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        return self.start_recording_with_target(
            capture_focus=capture_focus,
            started_from_gui=started_from_gui,
            focus_target=None,
            focus_target_at_start_hint=focus_target_from_hint(focus_hint),
        )

    def start_recording_with_target(
        self,
        capture_focus: bool,
        started_from_gui: bool,
        focus_target: FocusTarget | None,
        focus_target_at_start_hint: FocusTarget | None = None,
    ) -> tuple[bool, str]:
        cached_focus_for_start = self._recent_focus_target(max_age_seconds=5.0) if capture_focus else None
        captured_focus_for_memory: FocusTarget | None = None
        used_recent_focus_cache = False
        with self._lock:
            if self._recording:
                return False, "Already recording"
            if self._transcribing:
                return False, "Transcribing previous recording"

            STATE_DIR.mkdir(parents=True, exist_ok=True)
            CHUNK_DIR.mkdir(parents=True, exist_ok=True)
            if AUDIO_PATH.exists():
                AUDIO_PATH.unlink(missing_ok=True)

            if shutil.which("sox") is None:
                return False, "SoX is not installed or not in PATH"

            preflight_error = self._transcriber_preflight_error()
            if preflight_error:
                return False, preflight_error

            # A new take should not compete with a spoken answer (and the mic
            # must not pick the TTS voice up).
            self.speaker.stop()

            # Load the Ollama model while audio is still being captured.
            self.warm_up_transcriber_async()

            self._recording_mode = "chunked" if self._should_use_chunked_recording() else "single"
            self._stop_recording_event = None
            self._chunk_queue = None
            self._last_transcript = ""
            input_args, input_env = sox_input_args(self.config)

            if self._recording_mode == "single":
                sox_cmd = [
                    "sox",
                    "-q",
                    *input_args,
                    "-r",
                    "16000",
                    "-c",
                    "1",
                    "-b",
                    "16",
                    str(AUDIO_PATH),
                ]
                max_seconds = self._configured_max_seconds()
                if max_seconds > 0:
                    sox_cmd.extend(["trim", "0", str(max_seconds)])

                try:
                    self._process = subprocess.Popen(
                        sox_cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        text=True,
                        env=input_env,
                    )
                except Exception as exc:
                    return False, f"Failed to start recording: {exc}"
            else:
                for old_chunk in CHUNK_DIR.glob("chunk_*.wav"):
                    try:
                        old_chunk.unlink(missing_ok=True)
                    except Exception:
                        pass
                self._process = None
                self._stop_recording_event = threading.Event()
                self._chunk_queue = queue.Queue()

            self._recording = True
            self._started_at = time.time()
            self._capture_focus = capture_focus
            self._started_from_gui = started_from_gui
            self._focus_target = focus_target if capture_focus else None
            if (
                capture_focus
                and self._focus_target is not None
                and self._focus_target.source_pid == os.getpid()
            ):
                # Avoid storing our own GUI as the hotkey target.
                self._focus_target = None
            if capture_focus and self._focus_target is None:
                cached = cached_focus_for_start
                if cached is not None:
                    self._focus_target = cached
                    used_recent_focus_cache = True
            valid_hint = focus_target_at_start_hint if has_focus_target_identity(focus_target_at_start_hint) else None
            self._focus_target_at_start = self._focus_target or valid_hint
            self._last_route_note = ""
            captured_focus_for_memory = self._focus_target

        self._remember_focus_target(captured_focus_for_memory)

        if used_recent_focus_cache and self._focus_target is not None:
            log_routing(
                "start_recording: recovered target from focus tracker "
                f"app={self._focus_target.source_app!r} pid={self._focus_target.source_pid} "
                f"sig={self._focus_target.source_signature[:120]!r}"
            )
        elif self._focus_target is None and self._focus_target_at_start is None:
            log_routing("start_recording: no focus target captured for hotkey flow")
            threading.Thread(target=self._refresh_focus_target_soon, daemon=True).start()
        elif self._focus_target is None and self._focus_target_at_start is not None:
            log_routing(
                "start_recording: using focus hint "
                f"app={self._focus_target_at_start.source_app!r} pid={self._focus_target_at_start.source_pid} "
                f"sig={self._focus_target_at_start.source_signature[:120]!r}"
            )
            threading.Thread(target=self._refresh_focus_target_soon, daemon=True).start()
        else:
            log_routing(
                "start_recording: captured target "
                f"app={self._focus_target.source_app!r} pid={self._focus_target.source_pid} "
                f"sig={self._focus_target.source_signature[:120]!r}"
            )

        notify("Voice typing", "Recording started")
        self._emit_state(True, "recording")
        threading.Thread(target=self._watch_limit_warning, daemon=True).start()
        if self._recording_mode == "chunked":
            threading.Thread(target=self._record_chunk_loop, daemon=True).start()
            threading.Thread(target=self._chunk_transcribe_loop, daemon=True).start()
        else:
            threading.Thread(target=self._watch_sox_process, daemon=True).start()
        return True, "Recording started"

    def stop_recording(self, reason: str = "manual_stop") -> tuple[bool, str]:
        process: subprocess.Popen[str] | None
        stop_event: threading.Event | None
        mode: str
        with self._lock:
            if not self._recording:
                return False, "Not recording"
            self._recording = False
            self._transcribing = True
            mode = self._recording_mode
            stop_event = self._stop_recording_event
            process = self._process
            self._process = None

        if stop_event is not None:
            stop_event.set()

        if process is not None:
            try:
                process.terminate()
                process.wait(timeout=2)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass

        self._emit_state(False, reason)
        if mode == "chunked":
            return True, "Stopping and transcribing remaining audio"
        return self._finish_transcription(reason)

    def stop_recording_async(self, reason: str = "manual_stop") -> tuple[bool, str]:
        process: subprocess.Popen[str] | None
        stop_event: threading.Event | None
        mode: str
        with self._lock:
            if not self._recording:
                return False, "Not recording"
            self._recording = False
            self._transcribing = True
            mode = self._recording_mode
            stop_event = self._stop_recording_event
            process = self._process
            self._process = None

        if stop_event is not None:
            stop_event.set()

        if process is not None:
            try:
                process.terminate()
                process.wait(timeout=2)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass

        self._emit_state(False, reason)
        if mode != "chunked":
            threading.Thread(target=self._finish_transcription, args=(reason,), daemon=True).start()
        return True, "Stopping and transcribing"

    def toggle_recording(
        self,
        capture_focus: bool,
        started_from_gui: bool,
        focus_hint: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        if self.is_recording():
            with self._lock:
                started_via_gui = self._started_from_gui
                started_at = self._started_at
            # Global shortcuts can fire twice on some layouts/compositors.
            # Ignore too-early stop toggles for hotkey-started recordings.
            if not started_via_gui:
                elapsed = max(0.0, time.time() - started_at)
                if elapsed < MIN_HOTKEY_RECORDING_SECONDS_BEFORE_STOP:
                    log_routing(
                        "toggle: ignored early stop toggle "
                        f"(elapsed={elapsed:.2f}s, min={MIN_HOTKEY_RECORDING_SECONDS_BEFORE_STOP:.2f}s)"
                    )
                    return True, "Ignored accidental double-trigger"
            return self.stop_recording_async(reason="manual_stop")
        return self.start_recording(
            capture_focus=capture_focus,
            started_from_gui=started_from_gui,
            focus_hint=focus_hint,
        )

    def _refresh_focus_target_soon(self) -> None:
        # Global shortcuts can briefly focus gnome-shell; retry for a real text target.
        for _ in range(100):
            time.sleep(0.15)
            with self._lock:
                if not self._recording:
                    return
                if self._focus_target is not None:
                    return
            candidate = capture_focus_target_for_hotkey(retries=4, delay_s=0.03)
            if candidate is None:
                continue
            if candidate.source_pid == os.getpid():
                continue
            self._remember_focus_target(candidate)
            with self._lock:
                if not self._recording or self._focus_target is not None:
                    return
                self._focus_target = candidate
                self._focus_target_at_start = candidate
            log_routing(
                "start_recording: delayed capture target "
                f"app={candidate.source_app!r} pid={candidate.source_pid} sig={candidate.source_signature[:120]!r}"
            )
            return

    def _watch_limit_warning(self) -> None:
        warn_before = int(self.config.get("recording_warn_before_seconds", 15))
        max_seconds = self._configured_max_seconds()

        if max_seconds <= 0 or warn_before <= 0 or warn_before >= max_seconds:
            return

        time.sleep(max_seconds - warn_before)
        if self.is_recording():
            notify(
                "Voice typing",
                f"Recording will stop in {warn_before} seconds (limit: {max_seconds}s)",
                urgency="low",
            )
            self._emit_state(True, "warning")

    def _watch_sox_process(self) -> None:
        process: subprocess.Popen[str] | None
        with self._lock:
            process = self._process

        if process is None:
            return

        process.wait()

        with self._lock:
            # If recording is already false, this was an expected manual stop.
            if not self._recording:
                return
            self._recording = False
            self._process = None

        self._emit_state(False, "limit_or_error")
        self._finish_transcription("limit_reached")

    def _record_chunk_loop(self) -> None:
        queue_ref = self._chunk_queue
        stop_event = self._stop_recording_event
        if queue_ref is None or stop_event is None:
            return

        started_at = time.time()
        seq = 0
        limit_reached = False

        try:
            while not stop_event.is_set():
                max_seconds = self._configured_max_seconds()
                elapsed = time.time() - started_at
                if max_seconds > 0 and elapsed >= max_seconds:
                    limit_reached = True
                    break

                duration = self._chunk_seconds()
                if max_seconds > 0:
                    duration = max(0.5, min(duration, max_seconds - elapsed))

                chunk_path = CHUNK_DIR / f"chunk_{int(started_at)}_{seq:04d}.wav"
                chunk_path.unlink(missing_ok=True)
                input_args, input_env = sox_input_args(self.config)
                cmd = [
                    "sox",
                    "-q",
                    *input_args,
                    "-r",
                    "16000",
                    "-c",
                    "1",
                    "-b",
                    "16",
                    str(chunk_path),
                    "trim",
                    "0",
                    f"{duration:.3f}",
                ]
                try:
                    process = subprocess.Popen(
                        cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        text=True,
                        env=input_env,
                    )
                except Exception as exc:
                    log_routing(f"chunk_record: failed to start sox ({exc})")
                    notify("Voice typing", f"Failed to start audio chunk: {exc}", urgency="critical")
                    break

                with self._lock:
                    if not self._recording:
                        stop_event.set()
                    else:
                        self._process = process

                if stop_event.is_set():
                    try:
                        process.terminate()
                    except Exception:
                        pass

                rc = process.wait()

                with self._lock:
                    if self._process is process:
                        self._process = None
                    still_recording = self._recording

                try:
                    if chunk_path.exists() and chunk_path.stat().st_size > 0:
                        queue_ref.put((seq, chunk_path))
                except Exception:
                    pass

                if not still_recording or stop_event.is_set():
                    break
                if rc != 0:
                    stderr = ""
                    try:
                        stderr = (process.stderr.read() if process.stderr else "") or ""
                    except Exception:
                        stderr = ""
                    log_routing(f"chunk_record: sox exited rc={rc} stderr={stderr.strip()!r}")
                    break

                seq += 1

            if limit_reached:
                with self._lock:
                    if self._recording:
                        self._recording = False
                        self._transcribing = True
                self._emit_state(False, "limit_or_error")
        finally:
            queue_ref.put(None)

    def _chunk_transcribe_loop(self) -> None:
        queue_ref = self._chunk_queue
        if queue_ref is None:
            return

        saw_transcript = False
        last_error = ""
        route_partials = bool(self.config.get("streaming_route_partials", True))

        while True:
            item = queue_ref.get()
            if item is None:
                break
            seq, audio_path = item
            # Live mic feedback while still talking (sox stats; milliseconds).
            if self._report_audio_quality(audio_path) == AUDIO_QUALITY_LABELS["silent"]:
                continue
            text, err = self._transcribe_audio_path(audio_path)
            if err:
                last_error = err
                log_routing(f"chunk_transcribe: chunk={seq} error={err!r}")
                notify("Voice typing", err, urgency="critical")
                continue
            transcript = text.strip()
            if not transcript:
                continue
            if self._handle_voice_command(transcript):
                continue
            prepared = self._prepare_assistant_transcript(transcript)
            if not prepared:
                continue
            if self._handle_voice_command(prepared):
                continue
            saw_transcript = True
            full_transcript = self._append_last_transcript(prepared)
            self._emit_transcript(full_transcript, "recognized")
            if route_partials:
                route = self._route_or_hold_transcript(prepared)
                if route is not None:
                    self._emit_transcript(full_transcript, route)

        final_transcript = self.last_transcript().strip()
        if final_transcript and not route_partials:
            route = self._route_or_hold_transcript(final_transcript)
            if route is not None:
                self._emit_transcript(final_transcript, route)

        # Keep the full session audio so a bad chunk transcription can be
        # retried later against the whole recording.
        self.archive_recording(sorted(CHUNK_DIR.glob("chunk_*.wav")))

        with self._lock:
            self._transcribing = False

        if saw_transcript:
            notify("Voice typing", "Streaming transcript ready")
        elif last_error:
            notify("Voice typing", last_error, urgency="critical")
        else:
            notify("Voice typing", "Transcript is empty")
            self._emit_transcript("", "empty")

    def _append_last_transcript(self, transcript: str) -> str:
        transcript = transcript.strip()
        with self._lock:
            if self._last_transcript:
                self._last_transcript = f"{self._last_transcript.rstrip()} {transcript}"
            else:
                self._last_transcript = transcript
            return self._last_transcript

    def _assistant_wake_phrases(self) -> list[str]:
        raw = self.config.get("assistant_wake_phrases", ["hey antonio", "good morning antonio"])
        phrases = [str(item) for item in raw] if isinstance(raw, list) else ["hey antonio"]
        return [phrase for phrase in phrases if phrase.strip()]

    def _assistant_prompts_enabled(self) -> bool:
        return bool(self.config.get("voice_prompts_enabled", False))

    def _assistant_wake_mode_enabled(self) -> bool:
        return bool(self.config.get("assistant_wake_mode", False))

    def _prepare_assistant_transcript(self, transcript: str) -> str | None:
        if not self._assistant_wake_mode_enabled():
            return transcript.strip()

        woke, remainder = strip_leading_phrase(transcript, self._assistant_wake_phrases())
        now = time.time()
        if woke:
            awake_seconds = float(self.config.get("assistant_awake_seconds", 20))
            self._assistant_awake_until = now + max(3.0, awake_seconds)
            if not remainder:
                self._prompt_for_write_target(include_windows=not self._has_write_target())
                return None
            return remainder.strip()

        if now > self._assistant_awake_until:
            log_routing(f"assistant: ignored transcript while asleep: {transcript[:120]!r}")
            return None
        return transcript.strip()

    def _has_write_target(self) -> bool:
        candidates = [
            self._focus_target,
            self._focus_target_at_start,
            self._recent_focus_target(max_age_seconds=20.0),
        ]
        for candidate in candidates:
            if candidate is not None and candidate.source_pid != os.getpid() and has_focus_target_identity(candidate):
                return True

        candidate = capture_focus_target_for_hotkey(retries=2, delay_s=0.02)
        if candidate is None or candidate.source_pid == os.getpid():
            return False
        self._remember_focus_target(candidate)
        self._focus_target = candidate
        self._focus_target_at_start = candidate
        return True

    def _prompt_for_write_target(self, include_windows: bool = True) -> None:
        self._assistant_awaiting_target = True
        prompt = "Where should I write?"
        if include_windows:
            window_text = format_window_list_for_speech(list_open_windows())
            prompt = f"{prompt} {window_text}"
        notify("Voice typing", prompt)
        speak_prompt(prompt, self._assistant_prompts_enabled())
        self._emit_transcript("", "awaiting_target")

    def _route_or_hold_transcript(self, text: str) -> str | None:
        if (
            self._assistant_wake_mode_enabled()
            and bool(self.config.get("assistant_ask_target_when_uncertain", True))
            and not self._has_write_target()
        ):
            self._assistant_pending_transcript = text.strip()
            self._prompt_for_write_target(include_windows=True)
            log_routing("assistant: held transcript while waiting for target window")
            return "awaiting_target"
        return self._route_transcript(text)

    def _focus_window_and_flush_pending(self, query: str) -> bool:
        ok = activate_window_by_query(query)
        if not ok:
            return False

        time.sleep(0.20)
        candidate = capture_focus_target_for_hotkey(retries=8, delay_s=0.04)
        if candidate is not None and candidate.source_pid != os.getpid():
            self._remember_focus_target(candidate)
            self._focus_target = candidate
            self._focus_target_at_start = candidate

        self._assistant_awaiting_target = False
        pending = self._assistant_pending_transcript.strip()
        self._assistant_pending_transcript = ""
        if pending:
            route = self._route_transcript(pending)
            self._emit_transcript(self.last_transcript() or pending, route)
        return True

    def _handle_voice_command(self, transcript: str) -> bool:
        if not bool(self.config.get("voice_commands_enabled", True)):
            return False
        raw_prefixes = self.config.get("voice_command_prefixes", ["voice typing", "computer"])
        prefixes = [str(item) for item in raw_prefixes] if isinstance(raw_prefixes, list) else ["voice typing"]
        for phrase in self._assistant_wake_phrases():
            if phrase not in prefixes:
                prefixes.append(phrase)
        parsed = parse_voice_command(transcript, prefixes)
        if parsed is None:
            if self._assistant_awaiting_target and self._focus_window_and_flush_pending(transcript):
                notify("Voice typing", f"Focused {transcript}")
                return True
            return False

        command, argument = parsed
        log_routing(f"voice_command: {command} arg={argument!r}")

        if command == "status":
            woke, _ = strip_leading_phrase(transcript, self._assistant_wake_phrases())
            if woke:
                awake_seconds = float(self.config.get("assistant_awake_seconds", 20))
                self._assistant_awake_until = time.time() + max(3.0, awake_seconds)
            if woke and self._assistant_wake_mode_enabled() and not self._has_write_target():
                self._prompt_for_write_target(include_windows=True)
            else:
                notify("Voice typing", "Listening")
                speak_prompt("Listening.", bool(self.config.get("voice_prompts_enabled", False)))
            return True
        if command == "prompt_target":
            self._prompt_for_write_target(include_windows=True)
            return True
        if command == "list_windows":
            window_text = format_window_list_for_speech(list_open_windows())
            notify("Voice typing", window_text)
            speak_prompt(window_text, self._assistant_prompts_enabled())
            return True
        if command == "dictate":
            text = argument.strip()
            if text:
                full_transcript = self._append_last_transcript(text)
                self._emit_transcript(full_transcript, "recognized")
                route = self._route_or_hold_transcript(text)
                if route is not None:
                    self._emit_transcript(full_transcript, route)
            return True
        if command == "stop_listening":
            self.stop_recording_async(reason="voice_command_stop")
            notify("Voice typing", "Stopping by voice command")
            return True
        if command == "cancel":
            with self._lock:
                self._last_transcript = ""
            self._assistant_pending_transcript = ""
            self._assistant_awaiting_target = False
            notify("Voice typing", "Dictation discarded")
            self._emit_transcript("", "empty")
            return True
        if command == "paste_last":
            ok, message = self.paste_last_transcript()
            notify("Voice typing", message, urgency="normal" if ok else "critical")
            return True
        if command == "copy_last":
            text = self.last_transcript().strip()
            if text and copy_to_clipboard(text):
                notify("Voice typing", "Last transcript copied to clipboard")
            else:
                notify("Voice typing", "No transcript available", urgency="critical")
            return True
        if command == "focus_original":
            ok = self.restore_original_focus_target(max_age_seconds=20.0)
            notify("Voice typing", "Focus restored" if ok else "Could not restore focus")
            return True
        if command == "focus_window":
            ok = self._focus_window_and_flush_pending(argument)
            if ok:
                notify("Voice typing", f"Focused {argument}")
            else:
                notify("Voice typing", f"No window matched {argument}", urgency="critical")
                speak_prompt(
                    f"I could not find a window matching {argument}.",
                    bool(self.config.get("voice_prompts_enabled", False)),
                )
            return True
        return False

    def _finish_transcription(self, reason: str) -> tuple[bool, str]:
        try:
            if not AUDIO_PATH.exists() or AUDIO_PATH.stat().st_size == 0:
                notify("Voice typing", "No audio captured")
                return False, "No audio captured"

            # Archive first: whatever happens next, the speech is safe.
            archived = self.archive_recording([AUDIO_PATH])
            saved_hint = f" Audio saved as {archived.name}; use Retry Last Audio." if archived else ""
            quality_label = self._report_audio_quality(AUDIO_PATH)
            quality_hint = f" ({quality_label})" if quality_label and quality_label != "Mic OK" else ""
            if quality_label == AUDIO_QUALITY_LABELS["silent"]:
                message = "No signal from selected microphone"
                notify("Voice typing", message + saved_hint + quality_hint, urgency="critical")
                self._emit_transcript("", "empty")
                return False, message

            notify("Voice typing", "Transcribing...")
            text, err = self._transcribe_audio_path(AUDIO_PATH)
            if err:
                notify("Voice typing", err + saved_hint + quality_hint, urgency="critical")
                return False, err

            transcript = text.strip()
            if not transcript:
                notify("Voice typing", "Transcript is empty" + saved_hint + quality_hint)
                self._emit_transcript("", "empty")
                return False, "Transcript is empty"

            display_text = transcript
            if bool(self.config.get("assistant_answer_mode", False)):
                # Ask-gemma mode: the transcript is a question; route the
                # model's answer instead of the transcript itself.
                self._emit_transcript(f"Q: {transcript}", "recognized")
                notify("Voice typing", "Asking Gemma...")
                speak_aloud = bool(self.config.get("answer_speak_aloud", True)) and self.speaker.available()
                if speak_aloud:
                    self.speaker.stop()  # silence any previous answer
                    answer, ask_err = self.ask_ollama_stream(transcript, self.speaker.speak)
                else:
                    answer, ask_err = self.ask_ollama(transcript)
                if ask_err:
                    # Never lose the question: fall back to routing it as text.
                    notify("Voice typing", f"{ask_err}. Routing the question text instead.", urgency="critical")
                else:
                    transcript = answer
                    display_text = f"Q: {display_text}\n\nA: {answer}"

            # Store transcript so it can be pasted later via the paste-last hotkey.
            self._last_transcript = transcript

            # Surface transcript to UI immediately, even if insertion/clipboard routing fails.
            self._emit_transcript(display_text, "recognized")
            route = self._route_transcript(transcript)
            self._emit_transcript(display_text, route)

            if self._capture_focus and not self._started_from_gui:
                # Keep keyboard focus on original target so user can continue typing.
                self.restore_original_focus_target(max_age_seconds=12.0)

            summary = "Transcript ready"
            if route == "focused_target":
                summary = "Inserted into original text field"
            elif route == "focused_current_target":
                summary = "Inserted into current focused field"
            elif route == "focused_target_pasted":
                summary = "Pasted into original focused field"
            elif route == "clipboard":
                summary = "Copied to clipboard"
            elif route == "clipboard+pasted":
                summary = "Copied and pasted into current focus"
            elif route == "ydotool_typed":
                summary = "Typed into current focus via virtual keyboard"
            elif route == "gui_only":
                summary = "Transcript available in GUI"
            elif route == "clipboard_only":
                summary = "Copied to clipboard (paste unavailable)"

            if self._last_route_note:
                summary = f"{summary}. {self._last_route_note}"

            if reason == "limit_reached":
                notify("Voice typing", f"Stopped at recording limit. {summary}")
            else:
                notify("Voice typing", summary)
            return True, summary
        finally:
            with self._lock:
                self._transcribing = False

    def _transcribe_audio_path(self, audio_path: Path) -> tuple[str, str | None]:
        with self._transcriber_lock:
            if self._engine() == "ollama":
                return self._transcribe_audio_ollama(audio_path)
            if self._engine() == "gemma4-transformers":
                return self._transcribe_audio_gemma4(audio_path)
            return self._transcribe_audio_whisper(audio_path)

    def _transcribe_audio_whisper(self, audio_path: Path) -> tuple[str, str | None]:
        whisper_cli = self.config.whisper_cli_path()
        if not whisper_cli.exists():
            return "", f"whisper-cli not found: {whisper_cli}"

        model_path = self.config.model_path()
        if not model_path.exists():
            return "", f"Model not found: {model_path}"

        out_prefix = TRANSCRIPT_PREFIX if audio_path == AUDIO_PATH else STATE_DIR / f"transcript_{audio_path.stem}"
        txt_path = Path(str(out_prefix) + ".txt")
        txt_path.unlink(missing_ok=True)

        cmd = [
            str(whisper_cli),
            "-m",
            str(model_path),
            "-f",
            str(audio_path),
            "-nt",
            "-of",
            str(out_prefix),
            "-l",
            str(self.config.get("language", "en")),
        ]
        if bool(self.config.get("no_gpu", True)):
            cmd.append("--no-gpu")

        # A fixed timeout silently killed long recordings; scale with the
        # audio length so a 30-minute take is allowed to finish.
        base_timeout = int(self.config.get("transcribe_timeout_seconds", 90))
        timeout_sec = max(base_timeout, int(audio_duration_seconds(audio_path) * 3) + 60)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return "", f"Transcription timed out after {timeout_sec}s"
        except Exception as exc:
            return "", f"Failed to run whisper-cli: {exc}"

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            return "", stderr or "whisper-cli failed"

        if txt_path.exists():
            try:
                text = txt_path.read_text(encoding="utf-8", errors="ignore")
                return text.strip(), None
            except Exception as exc:
                return "", f"Failed reading transcript file: {exc}"

        stdout = (result.stdout or "").strip()
        return stdout, None

    def _transcribe_audio_ollama(self, audio_path: Path) -> tuple[str, str | None]:
        if not audio_path.exists() or audio_path.stat().st_size == 0:
            return "", "No audio captured"

        # Gemma audio prompts cap at 30s; past that the model degenerates into
        # repetition loops. Live recording already chunks, but retranscribe
        # sends whole archived files, so split long audio here.
        duration = audio_duration_seconds(audio_path)
        if duration > GEMMA4_MAX_AUDIO_SECONDS:
            return self._transcribe_audio_ollama_segmented(audio_path, duration)

        # Gemma's audio encoder is much weaker than Whisper on quiet input
        # (a -36 dB RMS take that Whisper nails comes back as "unintelligible"),
        # so peak-normalize a temp copy before sending.
        send_path = audio_path
        norm_path = STATE_DIR / f"ollama_norm_{audio_path.stem}.wav"
        if bool(self.config.get("ollama_normalize_audio", True)):
            try:
                result = subprocess.run(
                    ["sox", str(audio_path), str(norm_path), "norm", "-3"],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
                if result.returncode == 0 and norm_path.exists() and norm_path.stat().st_size > 0:
                    send_path = norm_path
            except Exception:
                pass

        try:
            audio_b64 = base64.b64encode(send_path.read_bytes()).decode("ascii")
        except Exception as exc:
            return "", f"Failed reading audio for Ollama: {exc}"
        finally:
            # The base64 copy is in memory; the temp file is no longer needed.
            norm_path.unlink(missing_ok=True)

        prompt = str(self.config.get("ollama_transcribe_prompt", DEFAULT_CONFIG["ollama_transcribe_prompt"]))
        payload = {
            "model": self.config.ollama_model(),
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "input_audio",
                            "input_audio": {"data": audio_b64, "format": "wav"},
                        },
                    ],
                }
            ],
            "stream": False,
            "temperature": 0,
            # Dampens the repetition loops gemma4 falls into on hard audio.
            "frequency_penalty": float(self.config.get("ollama_frequency_penalty", 0.5)),
            "max_tokens": int(self.config.get("ollama_max_tokens", 512)),
        }
        if bool(self.config.get("ollama_disable_thinking", True)):
            # gemma4 thinks by default; the chain-of-thought eats the whole
            # token budget and "content" comes back empty. Verified fix.
            payload["reasoning_effort"] = "none"
        timeout_sec = int(self.config.get("ollama_timeout_seconds", 120))

        try:
            req = urllib.request.Request(
                f"{self.config.ollama_url()}/v1/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                raw = resp.read().decode("utf-8")
            data = json.loads(raw)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="ignore")
            except Exception:
                body = str(exc)
            return "", f"Ollama transcription failed: {body or exc}"
        except Exception as exc:
            return "", f"Ollama transcription failed: {exc}"

        try:
            choices = data.get("choices", [])
            message = choices[0].get("message", {}) if choices else {}
            content = message.get("content", "")
        except Exception:
            content = ""
        return self._clean_gemma_text(str(content)), None

    def _transcribe_audio_ollama_segmented(self, audio_path: Path, duration: float) -> tuple[str, str | None]:
        segment_seconds = min(self._chunk_seconds(), float(GEMMA4_MAX_AUDIO_SECONDS))
        segment_dir = STATE_DIR / "ollama_segments"
        segment_dir.mkdir(parents=True, exist_ok=True)
        parts: list[str] = []
        last_error: str | None = None
        offset = 0.0
        index = 0
        while offset < duration:
            segment_path = segment_dir / f"segment_{index:03d}.wav"
            try:
                result = subprocess.run(
                    ["sox", str(audio_path), str(segment_path), "trim", str(offset), str(segment_seconds)],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
                if result.returncode != 0:
                    return "", f"Failed splitting audio for Ollama: {(result.stderr or '').strip()}"
                text, err = self._transcribe_audio_ollama(segment_path)
            finally:
                segment_path.unlink(missing_ok=True)
            if err:
                last_error = err
            elif text.strip():
                parts.append(text.strip())
            offset += segment_seconds
            index += 1
        combined = " ".join(parts)
        if combined:
            return combined, None
        return "", last_error

    def _transcribe_audio_gemma4(self, audio_path: Path) -> tuple[str, str | None]:
        if not audio_path.exists() or audio_path.stat().st_size == 0:
            return "", "No audio captured"

        try:
            import transformers  # type: ignore
        except Exception:
            return (
                "",
                "Gemma 4 backend requires Python package 'transformers'. "
                "Install torch, accelerate, transformers, and an audio decoder such as soundfile.",
            )

        backend = str(self.config.get("gemma_backend", "pipeline")).strip().lower()
        try:
            if backend == "objects":
                return self._transcribe_audio_gemma4_objects(audio_path, transformers)
            return self._transcribe_audio_gemma4_pipeline(audio_path, transformers)
        except Exception as exc:
            return "", f"Gemma 4 transcription failed: {exc}"

    def _gemma_messages_for_audio(self, audio_path: Path) -> list[dict[str, Any]]:
        prompt = str(self.config.get("gemma_transcribe_prompt", DEFAULT_CONFIG["gemma_transcribe_prompt"]))
        # The current Transformers pipeline accepts local audio paths as strings.
        return [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "audio", "audio": str(audio_path.resolve())},
                ],
            }
        ]

    def _gemma_generation_kwargs(self, transformers_module: Any) -> dict[str, Any]:
        max_new_tokens = int(self.config.get("gemma_max_new_tokens", 192))
        model_ref = self.config.gemma_model_ref()
        try:
            generation_config = transformers_module.GenerationConfig.from_pretrained(model_ref)
            generation_config.max_new_tokens = max_new_tokens
            return {"generation_config": generation_config}
        except Exception:
            return {"max_new_tokens": max_new_tokens}

    def _transcribe_audio_gemma4_pipeline(
        self,
        audio_path: Path,
        transformers_module: Any,
    ) -> tuple[str, str | None]:
        model_ref = self.config.gemma_model_ref()
        if not model_ref:
            return "", "Gemma model is not configured"

        if self._gemma_pipe is None or self._gemma_loaded_ref != f"pipeline:{model_ref}":
            dtype_value = str(self.config.get("gemma_dtype", "auto")).strip() or "auto"
            device_map = str(self.config.get("gemma_device_map", "auto")).strip() or "auto"
            try:
                self._gemma_pipe = transformers_module.pipeline(
                    task="any-to-any",
                    model=model_ref,
                    device_map=device_map,
                    dtype=dtype_value,
                )
            except TypeError:
                self._gemma_pipe = transformers_module.pipeline(
                    task="any-to-any",
                    model=model_ref,
                    device_map=device_map,
                    torch_dtype=dtype_value,
                )
            self._gemma_model = None
            self._gemma_processor = None
            self._gemma_loaded_ref = f"pipeline:{model_ref}"

        messages = self._gemma_messages_for_audio(audio_path)
        gen_kwargs = self._gemma_generation_kwargs(transformers_module)
        try:
            result = self._gemma_pipe(text=messages, return_full_text=False, generate_kwargs=gen_kwargs)
        except TypeError:
            result = self._gemma_pipe(messages, return_full_text=False, generate_kwargs=gen_kwargs)
        return self._extract_gemma_text(result), None

    def _transcribe_audio_gemma4_objects(
        self,
        audio_path: Path,
        transformers_module: Any,
    ) -> tuple[str, str | None]:
        model_ref = self.config.gemma_model_ref()
        if not model_ref:
            return "", "Gemma model is not configured"

        if self._gemma_model is None or self._gemma_processor is None or self._gemma_loaded_ref != f"objects:{model_ref}":
            model_cls = getattr(transformers_module, "AutoModelForMultimodalLM", None)
            if model_cls is None:
                model_cls = getattr(transformers_module, "AutoModelForImageTextToText", None)
            if model_cls is None:
                return (
                    "",
                    "Installed transformers does not expose AutoModelForMultimodalLM "
                    "or AutoModelForImageTextToText; use gemma_backend='pipeline' or upgrade transformers.",
                )
            dtype_value = str(self.config.get("gemma_dtype", "auto")).strip() or "auto"
            device_map = str(self.config.get("gemma_device_map", "auto")).strip() or "auto"
            processor_cls = getattr(transformers_module, "AutoProcessor")
            try:
                self._gemma_model = model_cls.from_pretrained(model_ref, dtype=dtype_value, device_map=device_map)
            except TypeError:
                self._gemma_model = model_cls.from_pretrained(
                    model_ref,
                    torch_dtype=dtype_value,
                    device_map=device_map,
                )
            self._gemma_processor = processor_cls.from_pretrained(model_ref)
            self._gemma_pipe = None
            self._gemma_loaded_ref = f"objects:{model_ref}"

        messages = self._gemma_messages_for_audio(audio_path)
        inputs = self._gemma_processor.apply_chat_template(
            messages,
            add_generation_prompt=True,
            tokenize=True,
            return_dict=True,
            return_tensors="pt",
        )
        inputs = inputs.to(self._gemma_model.device, dtype=self._gemma_model.dtype)
        outputs = self._gemma_model.generate(
            **inputs,
            max_new_tokens=int(self.config.get("gemma_max_new_tokens", 192)),
        )
        decoded = self._gemma_processor.batch_decode(
            outputs,
            skip_special_tokens=False,
            clean_up_tokenization_spaces=False,
        )
        return self._extract_gemma_text(decoded), None

    def _extract_gemma_text(self, value: Any) -> str:
        if isinstance(value, list):
            if not value:
                return ""
            first = value[0]
            if isinstance(first, dict):
                generated = first.get("generated_text", first.get("text", ""))
                if isinstance(generated, list):
                    parts = []
                    for item in generated:
                        if isinstance(item, dict):
                            parts.append(str(item.get("text", "")))
                        else:
                            parts.append(str(item))
                    return self._clean_gemma_text(" ".join(parts))
                return self._clean_gemma_text(str(generated))
            return self._clean_gemma_text(str(first))
        return self._clean_gemma_text(str(value))

    def _clean_gemma_text(self, text: str) -> str:
        text = text.strip()
        if "<|turn>model" in text:
            text = text.rsplit("<|turn>model", 1)[-1]
        if "<turn|>" in text:
            text = text.split("<turn|>", 1)[0]
        text = re.sub(r"<\|[^>]+?\|>", "", text)
        text = text.replace("<bos>", "").replace("<eos>", "")
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _route_transcript(self, text: str) -> str:
        # If recording started from GUI button, prefer showing transcript in GUI only.
        if self._started_from_gui:
            if bool(self.config.get("copy_to_clipboard_when_started_from_gui", False)):
                if copy_to_clipboard(text):
                    return "clipboard"
            return "gui_only"

        # If initial capture missed the concrete accessible node, try to hydrate it now.
        if self._capture_focus and self._focus_target is None and self._focus_target_at_start is not None:
            candidate = capture_focus_target_for_hotkey(retries=6, delay_s=0.03)
            self._remember_focus_target(candidate)
            if same_focus_target(candidate, self._focus_target_at_start):
                self._focus_target = candidate
                if candidate is not None:
                    log_routing(
                        "route: hydrated focus target from current focus "
                        f"app={candidate.source_app!r} pid={candidate.source_pid}"
                    )
            if self._focus_target is None:
                resolved = resolve_focus_target_from_hint(self._focus_target_at_start)
                if resolved is not None:
                    self._focus_target = resolved
                    log_routing(
                        "route: hydrated focus target from saved hint "
                        f"app={resolved.source_app!r} pid={resolved.source_pid}"
                    )
                    self._remember_focus_target(resolved)

        if self._capture_focus and self._focus_target is None and self._focus_target_at_start is None:
            cached = self._recent_focus_target(max_age_seconds=8.0)
            if cached is not None:
                self._focus_target = cached
                self._focus_target_at_start = cached
                log_routing(
                    "route: recovered start target from focus tracker "
                    f"app={cached.source_app!r} pid={cached.source_pid}"
                )

        if (
            self._capture_focus
            and self._focus_target is None
            and self._focus_target_at_start is None
            and bool(self.config.get("ask_when_focus_uncertain", False))
        ):
            if copy_to_clipboard(text):
                self._last_route_note = (
                    "No reliable target window was captured; focus the target and use paste-last."
                )
                speak_prompt(
                    "I could not identify the target window. Focus the window and say voice typing paste last.",
                    bool(self.config.get("voice_prompts_enabled", False)),
                )
                log_routing("route: clipboard_only because focus target was uncertain")
                return "clipboard_only"

        if self._capture_focus and self._focus_target is not None:
            # Ignore accidental self-target capture from the GUI process.
            if self._focus_target.source_pid != os.getpid():
                log_routing(
                    "route: trying focused target "
                    f"app={self._focus_target.source_app!r} pid={self._focus_target.source_pid}"
                )
                # Activate the target window at the compositor level FIRST,
                # then use AT-SPI grabFocus for the specific element.
                activate_window_by_pid(self._focus_target.source_pid)
                time.sleep(0.08)
                focus_target_element(self._focus_target)
                time.sleep(0.05)
                if insert_text_into_target(self._focus_target, text):
                    log_routing("route: focused_target success")
                    return "focused_target"
                # Re-activate at compositor level before paste attempt.
                activate_window_by_pid(self._focus_target.source_pid)
                time.sleep(0.10)
                if focus_target_element(self._focus_target):
                    if paste_text_via_clipboard(
                        text,
                        restore_clipboard=bool(self.config.get("restore_clipboard_after_paste", True)),
                        target=self._focus_target,
                    ):
                        log_routing("route: focused_target_pasted success")
                        return "focused_target_pasted"
                log_routing("route: focused target delivery failed")

        # Strict focus lock: never inject into a different focused app when a lock target exists.
        lock_is_usable = has_focus_target_identity(self._focus_target_at_start)
        if self._capture_focus and lock_is_usable:
            current_focus = capture_focus_target_for_hotkey(retries=4, delay_s=0.03)
            if not same_focus_target(current_focus, self._focus_target_at_start):
                log_routing(
                    "route: focus changed, attempting restore to original target "
                    f"(start={focus_signature(self._focus_target_at_start)}, current={focus_signature(current_focus)})"
                )
                # Try to force-focus the original captured target and deliver there.
                locked_target = self._focus_target
                if locked_target is None and self._focus_target_at_start is not None:
                    locked_target = resolve_focus_target_from_hint(self._focus_target_at_start)
                    if locked_target is not None:
                        self._focus_target = locked_target
                        log_routing(
                            "route: restored locked target from saved hint "
                            f"app={locked_target.source_app!r} pid={locked_target.source_pid}"
                        )
                if (
                    locked_target is not None
                    and locked_target.source_pid != os.getpid()
                ):
                    # Activate at compositor level, then AT-SPI level.
                    activate_window_by_pid(locked_target.source_pid)
                    time.sleep(0.10)
                    focus_target_element(locked_target)
                    time.sleep(0.05)
                    if insert_text_into_target(locked_target, text):
                        self._last_route_note = "Focus changed, but text was delivered to the original target."
                        log_routing("route: focused_target success after forced refocus")
                        return "focused_target"
                    # Re-activate at compositor level before paste.
                    activate_window_by_pid(locked_target.source_pid)
                    time.sleep(0.10)
                    focus_target_element(locked_target)
                    if paste_text_via_clipboard(
                        text,
                        restore_clipboard=bool(self.config.get("restore_clipboard_after_paste", True)),
                        target=locked_target,
                    ):
                        self._last_route_note = "Focus changed, but pasted to the original target after refocus."
                        log_routing("route: focused_target_pasted success after forced refocus")
                        return "focused_target_pasted"
                    if bool(self.config.get("direct_type_fallback_for_hotkey", True)) and type_text_via_ydotool(text):
                        self._last_route_note = "Focus changed, but typed into the original target after refocus."
                        log_routing("route: ydotool_typed success after forced refocus")
                        return "ydotool_typed"

                if copy_to_clipboard(text):
                    self._last_route_note = (
                        "Focus changed during transcription and original target could not be restored."
                    )
                    return "clipboard_only"
                self._last_route_note = "Focus changed and clipboard copy failed."
                return "gui_only"
        elif self._capture_focus:
            log_routing("route: no usable start-focus lock available; using best-effort delivery")

        current_focus: FocusTarget | None = None
        if self._capture_focus:
            current_focus = capture_focus_target_for_hotkey(retries=2, delay_s=0.02)
            self._remember_focus_target(current_focus)
            if (
                current_focus is not None
                and current_focus.source_pid != os.getpid()
                and insert_text_into_target(current_focus, text)
            ):
                log_routing("route: focused_current_target success (best-effort)")
                return "focused_current_target"

        if bool(self.config.get("paste_fallback_for_hotkey", True)):
            if current_focus is None:
                current_focus = capture_focus_target_for_hotkey(retries=2, delay_s=0.02)
                self._remember_focus_target(current_focus)
            paste_target = current_focus or self._recent_focus_target(max_age_seconds=8.0)
            if current_focus is not None and current_focus.source_pid == os.getpid():
                # Never paste into our own window; keep clipboard fallback instead.
                if copy_to_clipboard(text):
                    self._last_route_note = "Focused app switched to Voice Typing; paste was skipped for safety."
                    log_routing("route: clipboard_only (focus switched to Voice Typing)")
                    return "clipboard_only"
                return "gui_only"

            # Prefer paste path first because it is typically instant.
            if paste_text_via_clipboard(
                text,
                restore_clipboard=bool(self.config.get("restore_clipboard_after_paste", True)),
                target=paste_target,
            ):
                log_routing("route: clipboard+pasted success")
                return "clipboard+pasted"

            if bool(self.config.get("direct_type_fallback_for_hotkey", True)):
                if type_text_via_ydotool(text):
                    log_routing("route: ydotool_typed success")
                    return "ydotool_typed"
                log_routing("route: ydotool_typed failed")

            self._last_route_note = "Could not send Ctrl+V via ydotool; text was copied only."
            log_routing("route: clipboard paste fallback failed")

        if copy_to_clipboard(text):
            if bool(self.config.get("auto_paste_current_focus", False)) and paste_current_focus_via_ydotool(
                target=capture_focus_target() or self._recent_focus_target(max_age_seconds=8.0)
            ):
                log_routing("route: clipboard+pasted success (auto_paste_current_focus)")
                return "clipboard+pasted"
            if bool(self.config.get("auto_paste_current_focus", False)):
                self._last_route_note = "Auto-paste requested but ydotool paste failed."
                return "clipboard_only"
            return "clipboard"

        self._last_route_note = "Clipboard write failed."
        log_routing("route: gui_only (clipboard unavailable)")
        return "gui_only"

    def _emit_state(self, recording: bool, reason: str) -> None:
        if self.on_state_change is not None:
            self.on_state_change(recording, reason)

    def _emit_transcript(self, text: str, route: str) -> None:
        if self.on_transcript is not None:
            self.on_transcript(text, route)


class IpcServer(threading.Thread):
    def __init__(
        self,
        controller: RecorderController,
        config: ConfigManager,
        on_show: Callable[[bool], None] | None = None,
    ):
        super().__init__(daemon=True)
        self.controller = controller
        self.config = config
        self.on_show = on_show
        self._stop_event = threading.Event()
        self._socket: socket.socket | None = None
        self._last_toggle_at = 0.0

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
            server.listen(10)
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
                        raw = conn.recv(4096)
                        req = json.loads(raw.decode("utf-8")) if raw else {}
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
        focus_hint_raw = req.get("focus_hint")
        focus_hint: dict[str, Any] | None = None
        if isinstance(focus_hint_raw, dict):
            parsed_hint = focus_target_from_hint(focus_hint_raw)
            if parsed_hint is not None and has_focus_target_identity(parsed_hint):
                focus_hint = focus_target_to_hint(parsed_hint)

        def ensure_focus_hint() -> dict[str, Any] | None:
            if focus_hint is not None:
                return focus_hint
            cached = self.controller.recent_focus_hint(max_age_seconds=10.0)
            if cached is not None:
                log_routing(
                    "ipc: using cached focus hint "
                    f"app={cached.get('source_app', '')!r} pid={cached.get('source_pid')}"
                )
            return cached

        if cmd == "toggle":
            now = time.time()
            if now - self._last_toggle_at < TOGGLE_DEBOUNCE_SECONDS:
                return {
                    "ok": True,
                    "message": "Ignored duplicate toggle",
                    "recording": self.controller.is_recording(),
                    "busy": self.controller.is_transcribing(),
                }
            self._last_toggle_at = now

            was_recording = self.controller.is_recording()
            effective_focus_hint = ensure_focus_hint() if not was_recording else None
            ok, message = self.controller.toggle_recording(
                capture_focus=True,
                started_from_gui=False,
                focus_hint=effective_focus_hint,
            )
            if not ok and message == "Transcribing previous recording":
                return {
                    "ok": True,
                    "message": "Transcribing previous recording",
                    "recording": self.controller.is_recording(),
                    "busy": True,
                }
            if (
                ok
                and not was_recording
                and self.on_show is not None
                and bool(self.config.get("show_panel_on_hotkey_start", True))
            ):
                if self.controller.has_recent_focus(max_age_seconds=4.0):
                    self.on_show(False)
                else:
                    log_routing("ui: skipped panel show on hotkey start (no reliable focus target)")
            if ok and not was_recording:
                # Re-assert previous focus after shortcut handling.
                self.controller.restore_original_focus_target(max_age_seconds=10.0)
            return {"ok": ok, "message": message, "recording": self.controller.is_recording()}

        if cmd == "start":
            effective_focus_hint = ensure_focus_hint()
            ok, message = self.controller.start_recording(
                capture_focus=True,
                started_from_gui=False,
                focus_hint=effective_focus_hint,
            )
            if (
                ok
                and self.on_show is not None
                and bool(self.config.get("show_panel_on_hotkey_start", True))
            ):
                if self.controller.has_recent_focus(max_age_seconds=4.0):
                    self.on_show(False)
                else:
                    log_routing("ui: skipped panel show on hotkey start (no reliable focus target)")
            if ok:
                # Re-assert previous focus after shortcut handling.
                self.controller.restore_original_focus_target(max_age_seconds=10.0)
            return {"ok": ok, "message": message, "recording": self.controller.is_recording()}

        if cmd == "stop":
            ok, message = self.controller.stop_recording_async()
            return {"ok": ok, "message": message, "recording": self.controller.is_recording()}

        if cmd == "status":
            return {
                "ok": True,
                "recording": self.controller.is_recording(),
                "busy": self.controller.is_transcribing(),
                "engine": self.config.transcription_engine(),
                "model": self.config.get("model", ""),
                "gemma_model": self.config.gemma_model_ref(),
                "ollama_model": self.config.ollama_model(),
                "audio_input_device": self.config.get("audio_input_device", "") or "default",
            }

        if cmd == "show":
            if self.on_show is not None:
                self.on_show(True)
            return {"ok": True, "message": "window shown"}

        if cmd == "peek":
            if self.on_show is not None:
                self.on_show(False)
            return {"ok": True, "message": "window shown without focus"}

        if cmd == "paste_last":
            ok, message = self.controller.paste_last_transcript()
            return {"ok": ok, "message": message}

        if cmd == "list_models":
            return {"ok": True, "models": self.config.list_models()}

        if cmd == "list_engines":
            return {"ok": True, "engines": list(SUPPORTED_TRANSCRIPTION_ENGINES)}

        if cmd == "set_model":
            model = str(req.get("model", "")).strip()
            if not model:
                return {"ok": False, "error": "model is required"}
            available = self.config.list_models()
            if model not in available:
                return {"ok": False, "error": f"model not found in models dir: {model}"}
            self.config.set("model", model)
            return {"ok": True, "message": f"model set to {model}"}

        if cmd == "set_audio_input":
            device = str(req.get("device", "")).strip()
            self.config.set("audio_input_device", "" if device in {"", "default", "-d"} else device)
            label = self.config.get("audio_input_device", "") or "default"
            return {"ok": True, "message": f"audio input set to {label}"}

        if cmd == "set_engine":
            engine = str(req.get("engine", "")).strip().lower()
            engine = ENGINE_ALIASES.get(engine, engine)
            if engine not in SUPPORTED_TRANSCRIPTION_ENGINES:
                return {"ok": False, "error": f"engine must be one of: {', '.join(SUPPORTED_TRANSCRIPTION_ENGINES)}"}
            self.config.set("transcription_engine", engine)
            return {"ok": True, "message": f"engine set to {engine}"}

        if cmd == "set_gemma_model":
            model = str(req.get("model", "")).strip()
            if not model:
                return {"ok": False, "error": "model is required"}
            if model.startswith("/") or model.startswith("~"):
                self.config.set("gemma_model_path", model)
            else:
                self.config.set("gemma_model", model)
                self.config.set("gemma_model_path", "")
            return {"ok": True, "message": f"Gemma model set to {model}"}

        if cmd == "set_ollama_model":
            model = str(req.get("model", "")).strip()
            if not model:
                return {"ok": False, "error": "model is required"}
            self.config.set("ollama_model", model)
            return {"ok": True, "message": f"Ollama model set to {model}"}

        return {"ok": False, "error": f"unknown command: {cmd}"}


class VoiceTypingGui:
    def __init__(
        self,
        toggle_on_launch: bool,
        startup_focus_target: FocusTarget | None = None,
        startup_focus_hint: dict[str, Any] | None = None,
    ) -> None:
        self.config = ConfigManager(CONFIG_PATH)
        self.events: "queue.Queue[tuple[str, Any, Any]]" = queue.Queue()
        self.startup_focus_target = startup_focus_target
        self.startup_focus_hint = startup_focus_hint

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("560x420")
        self.root.minsize(440, 320)
        self.root.configure(bg="#0f1115")
        self.root.withdraw()
        self.root.resizable(True, True)
        # Prevent the window manager from stealing compositor focus when this
        # window is shown.  "utility" type tells the WM this is a secondary
        # helper window; "passive" focus model prevents automatic focus grabs.
        try:
            self.root.attributes("-type", "utility")
        except Exception:
            pass
        self.root.wm_focusmodel("passive")
        self._configure_style()
        self._apply_window_presentation()

        self.status_var = tk.StringVar(value="Idle")
        self.elapsed_var = tk.StringVar(value="00:00")
        self.route_var = tk.StringVar(value="Target: none yet")
        self.engine_var = tk.StringVar(value=self.config.transcription_engine())
        self.model_var = tk.StringVar(value=str(self.config.get("model", "")))
        self.gemma_model_var = tk.StringVar(value=self.config.gemma_model_ref())
        self.ollama_model_var = tk.StringVar(value=self.config.ollama_model())

        self.controller = RecorderController(
            config=self.config,
            on_state_change=lambda recording, reason: self.events.put(("state", recording, reason)),
            on_transcript=lambda text, route: self.events.put(("transcript", text, route)),
            on_audio_quality=lambda verdict, message: self.events.put(("audio_quality", verdict, message)),
        )
        self.ipc = IpcServer(
            self.controller,
            self.config,
            on_show=lambda active: self.events.put(("show", None, active)),
        )

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.ipc.start()
        self._tick()

        if toggle_on_launch:
            # Starting from a keyboard shortcut should capture target focus.
            self.controller.start_recording_with_target(
                capture_focus=True,
                started_from_gui=False,
                focus_target=self.startup_focus_target,
                focus_target_at_start_hint=focus_target_from_hint(self.startup_focus_hint),
            )
            if bool(self.config.get("show_panel_on_hotkey_start", True)):
                self._show_window(active=False)
        else:
            self._show_window(active=True)

    def _apply_window_presentation(self) -> None:
        try:
            controller = getattr(self, "controller", None)
            is_recording = bool(controller is not None and controller.is_recording())
            keep_on_top = bool(self.config.get("window_always_on_top", True)) or is_recording
            self.root.attributes("-topmost", keep_on_top)
        except Exception:
            pass
        try:
            opacity = float(self.config.get("window_opacity", 0.95))
            opacity = max(0.35, min(1.0, opacity))
            self.root.attributes("-alpha", opacity)
        except Exception:
            pass

    def _configure_style(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".", font=("DejaVu Sans Mono", 10))
        style.configure("VT.Root.TFrame", background="#0f1115")
        style.configure("VT.Card.TFrame", background="#171a20")
        style.configure("VT.Status.TLabel", font=("DejaVu Sans Mono", 11, "bold"), foreground="#f1f5f9", background="#171a20")
        style.configure("VT.Meta.TLabel", foreground="#f59e0b", background="#0f1115")
        style.configure("VT.TButton", padding=(10, 4), background="#ea580c", foreground="#fff7ed", borderwidth=0)
        style.map(
            "VT.TButton",
            background=[("pressed", "#c2410c"), ("active", "#fb923c")],
            foreground=[("disabled", "#fed7aa"), ("!disabled", "#fff7ed")],
        )
        style.configure(
            "VT.Detail.TButton",
            padding=(8, 3),
            background="#2b313b",
            foreground="#e2e8f0",
            borderwidth=0,
        )
        style.map(
            "VT.Detail.TButton",
            background=[("pressed", "#1f242c"), ("active", "#3d4653")],
            foreground=[("disabled", "#94a3b8"), ("!disabled", "#e2e8f0")],
        )
        style.configure("VT.TCheckbutton", background="#171a20", foreground="#cbd5e1")

    def _build_ui(self) -> None:
        root_pad = ttk.Frame(self.root, padding=10, style="VT.Root.TFrame")
        root_pad.pack(fill="both", expand=True)

        # Plain tk widgets here so the whole bar can change color with state.
        self.status_bar = tk.Frame(root_pad, bg="#171a20", padx=10, pady=8)
        self.status_bar.pack(fill="x")

        self.dot = tk.Canvas(self.status_bar, width=18, height=18, highlightthickness=0, bd=0, bg="#171a20")
        self.dot.pack(side="left", padx=(0, 8))
        self.dot_indicator = self.dot.create_oval(2, 2, 16, 16, fill="#475569", outline="")

        self.status_label = tk.Label(
            self.status_bar,
            textvariable=self.status_var,
            font=("DejaVu Sans Mono", 13, "bold"),
            fg="#94a3b8",
            bg="#171a20",
        )
        self.status_label.pack(side="left")
        self.elapsed_label = tk.Label(
            self.status_bar,
            textvariable=self.elapsed_var,
            font=("DejaVu Sans Mono", 12),
            fg="#94a3b8",
            bg="#171a20",
        )
        self.elapsed_label.pack(side="left", padx=(10, 0))

        self.toggle_btn = ttk.Button(self.status_bar, text="Record", style="VT.TButton", command=self._toggle_from_gui)
        self.toggle_btn.pack(side="right")

        mode_row = ttk.Frame(root_pad, style="VT.Root.TFrame")
        mode_row.pack(fill="x", pady=(6, 0))
        ttk.Label(mode_row, text="Mode:", style="VT.Meta.TLabel").pack(side="left")
        self.stable_mode_btn = tk.Button(
            mode_row,
            text="Stable · Whisper",
            relief="flat",
            bd=0,
            padx=10,
            pady=3,
            font=("DejaVu Sans Mono", 10, "bold"),
            command=lambda: self._apply_profile("stable"),
        )
        self.stable_mode_btn.pack(side="left", padx=(8, 4))
        self.gemma_mode_btn = tk.Button(
            mode_row,
            text="Gemma Agent · Ollama",
            relief="flat",
            bd=0,
            padx=10,
            pady=3,
            font=("DejaVu Sans Mono", 10, "bold"),
            command=lambda: self._apply_profile("gemma-agent"),
        )
        self.gemma_mode_btn.pack(side="left", padx=(0, 4))
        self.ask_mode_btn = tk.Button(
            mode_row,
            text="Ask Gemma",
            relief="flat",
            bd=0,
            padx=10,
            pady=3,
            font=("DejaVu Sans Mono", 10, "bold"),
            command=lambda: self._apply_profile("ask-gemma"),
        )
        self.ask_mode_btn.pack(side="left", padx=(0, 4))
        self._update_mode_buttons()

        ttk.Label(root_pad, textvariable=self.route_var, style="VT.Meta.TLabel").pack(
            anchor="w", pady=(4, 0)
        )

        self.mic_quality_var = tk.StringVar(value="")
        self.mic_quality_label = tk.Label(
            root_pad,
            textvariable=self.mic_quality_var,
            font=("DejaVu Sans Mono", 9),
            fg="#64748b",
            bg="#0f1115",
            anchor="w",
        )
        self.mic_quality_label.pack(fill="x", pady=(2, 0))

        transcript_frame = ttk.Frame(root_pad, style="VT.Card.TFrame", padding=(8, 8))
        transcript_frame.pack(fill="both", expand=True, pady=(8, 6))
        ttk.Label(transcript_frame, text="Latest Transcript", style="VT.Meta.TLabel").pack(anchor="w")
        self.transcript_box = tk.Text(
            transcript_frame,
            wrap="word",
            height=2,
            bg="#0b0d10",
            fg="#f8fafc",
            insertbackground="#f8fafc",
            highlightthickness=1,
            highlightbackground="#334155",
            relief="solid",
            bd=1,
        )
        self.transcript_box.pack(fill="both", expand=True, pady=(4, 0))
        self.transcript_box.configure(state="disabled")

        options_frame = ttk.Frame(root_pad, style="VT.Card.TFrame", padding=(8, 8))
        options_frame.pack(fill="x")

        engine_row = ttk.Frame(options_frame, style="VT.Card.TFrame")
        engine_row.pack(fill="x", pady=(0, 4))
        ttk.Label(engine_row, text="Engine:", style="VT.Meta.TLabel").pack(side="left")
        self.engine_combo = ttk.Combobox(
            engine_row,
            textvariable=self.engine_var,
            state="readonly",
            values=list(SUPPORTED_TRANSCRIPTION_ENGINES),
            width=10,
        )
        self.engine_combo.pack(side="left", padx=(6, 8))
        self.engine_combo.bind("<<ComboboxSelected>>", self._on_engine_selected)

        model_row = ttk.Frame(options_frame, style="VT.Card.TFrame")
        model_row.pack(fill="x", pady=(4, 4))
        ttk.Label(model_row, text="Whisper:", style="VT.Meta.TLabel").pack(side="left")
        self.model_combo = ttk.Combobox(model_row, textvariable=self.model_var, state="readonly")
        self.model_combo.pack(side="left", fill="x", expand=True, padx=(6, 6))
        self.model_combo.bind("<<ComboboxSelected>>", self._on_model_selected)
        ttk.Button(model_row, text="Refresh", style="VT.Detail.TButton", command=self._refresh_models).pack(
            side="left"
        )

        gemma_row = ttk.Frame(options_frame, style="VT.Card.TFrame")
        gemma_row.pack(fill="x", pady=(0, 4))
        ttk.Label(gemma_row, text="Gemma:", style="VT.Meta.TLabel").pack(side="left")
        self.gemma_model_entry = ttk.Entry(gemma_row, textvariable=self.gemma_model_var)
        self.gemma_model_entry.pack(side="left", fill="x", expand=True, padx=(6, 6))
        ttk.Button(gemma_row, text="Apply", style="VT.Detail.TButton", command=self._on_gemma_model_applied).pack(
            side="left"
        )

        ollama_row = ttk.Frame(options_frame, style="VT.Card.TFrame")
        ollama_row.pack(fill="x", pady=(0, 4))
        ttk.Label(ollama_row, text="Ollama:", style="VT.Meta.TLabel").pack(side="left")
        self.ollama_combo = ttk.Combobox(ollama_row, textvariable=self.ollama_model_var)
        self.ollama_combo.pack(side="left", fill="x", expand=True, padx=(6, 6))
        self.ollama_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_ollama_model_applied())
        ttk.Button(
            ollama_row, text="Refresh", style="VT.Detail.TButton", command=self._refresh_ollama_models
        ).pack(side="left", padx=(0, 6))
        ttk.Button(ollama_row, text="Apply", style="VT.Detail.TButton", command=self._on_ollama_model_applied).pack(
            side="left"
        )

        self.auto_paste_var = tk.BooleanVar(value=bool(self.config.get("auto_paste_current_focus", False)))
        self.gui_clipboard_var = tk.BooleanVar(
            value=bool(self.config.get("copy_to_clipboard_when_started_from_gui", False))
        )
        self.always_on_top_var = tk.BooleanVar(value=bool(self.config.get("window_always_on_top", True)))
        self.show_on_hotkey_var = tk.BooleanVar(value=bool(self.config.get("show_panel_on_hotkey_start", True)))
        self.use_gpu_var = tk.BooleanVar(value=not bool(self.config.get("no_gpu", True)))
        self.streaming_var = tk.BooleanVar(value=bool(self.config.get("streaming_transcription", False)))
        self.voice_commands_var = tk.BooleanVar(value=bool(self.config.get("voice_commands_enabled", True)))
        self.speak_answers_var = tk.BooleanVar(value=bool(self.config.get("answer_speak_aloud", True)))

        ttk.Checkbutton(
            options_frame,
            text="Auto-paste when fallback uses clipboard",
            variable=self.auto_paste_var,
            command=self._save_options,
            style="VT.TCheckbutton",
        ).pack(anchor="w")

        ttk.Checkbutton(
            options_frame,
            text="GUI-start recordings also copy to clipboard",
            variable=self.gui_clipboard_var,
            command=self._save_options,
            style="VT.TCheckbutton",
        ).pack(anchor="w", pady=(2, 0))

        ttk.Checkbutton(
            options_frame,
            text="Keep panel always on top",
            variable=self.always_on_top_var,
            command=self._save_options,
            style="VT.TCheckbutton",
        ).pack(anchor="w", pady=(2, 0))

        ttk.Checkbutton(
            options_frame,
            text="Show panel on hotkey start (no focus)",
            variable=self.show_on_hotkey_var,
            command=self._save_options,
            style="VT.TCheckbutton",
        ).pack(anchor="w", pady=(2, 0))

        ttk.Checkbutton(
            options_frame,
            text="Use GPU for transcription (if available)",
            variable=self.use_gpu_var,
            command=self._save_options,
            style="VT.TCheckbutton",
        ).pack(anchor="w", pady=(2, 0))

        ttk.Checkbutton(
            options_frame,
            text="Rolling chunks while recording",
            variable=self.streaming_var,
            command=self._save_options,
            style="VT.TCheckbutton",
        ).pack(anchor="w", pady=(2, 0))

        ttk.Checkbutton(
            options_frame,
            text="Voice commands",
            variable=self.voice_commands_var,
            command=self._save_options,
            style="VT.TCheckbutton",
        ).pack(anchor="w", pady=(2, 0))

        ttk.Checkbutton(
            options_frame,
            text="Speak answers aloud (Ask Gemma mode)",
            variable=self.speak_answers_var,
            command=self._save_options,
            style="VT.TCheckbutton",
        ).pack(anchor="w", pady=(2, 0))

        bottom = ttk.Frame(options_frame, style="VT.Card.TFrame")
        bottom.pack(fill="x")
        ttk.Button(bottom, text="Copy Last", style="VT.Detail.TButton", command=self._copy_last_transcript).pack(
            side="left"
        )
        ttk.Button(bottom, text="Stop", style="VT.Detail.TButton", command=self._stop_from_gui).pack(
            side="left",
            padx=(6, 0),
        )
        ttk.Button(
            bottom, text="Retry Last Audio", style="VT.Detail.TButton", command=self._retry_last_audio
        ).pack(side="left", padx=(6, 0))
        ttk.Button(bottom, text="Model Help", style="VT.Detail.TButton", command=self._show_model_help).pack(
            side="right"
        )

        self._refresh_models()
        self._refresh_ollama_models()

    def _refresh_ollama_models(self) -> None:
        def _fetch() -> None:
            names = list_ollama_models(self.config.ollama_url())
            if names:
                self.events.put(("ollama_models", names, None))

        threading.Thread(target=_fetch, daemon=True).start()

    def _apply_profile(self, profile: str) -> None:
        updates = PROFILE_CONFIGS.get(profile)
        if not updates:
            return
        if self.controller.is_recording() or self.controller.is_transcribing():
            notify("Voice typing", "Stop recording before switching mode")
            return
        for key, value in updates.items():
            self.config.set(key, value)
        self.config.set("active_profile", profile)
        self.engine_var.set(self.config.transcription_engine())
        self.streaming_var.set(bool(self.config.get("streaming_transcription", False)))
        self.voice_commands_var.set(bool(self.config.get("voice_commands_enabled", True)))
        self._update_mode_buttons()
        if profile in ("gemma-agent", "ask-gemma"):
            # Start loading the model now instead of on first use.
            self.controller.warm_up_transcriber_async()
        self.route_var.set(f"Mode switched to {profile}.")
        notify("Voice typing", f"Mode: {profile}")

    def _update_mode_buttons(self) -> None:
        engine = self.config.transcription_engine()
        answer_mode = bool(self.config.get("assistant_answer_mode", False))
        active = {"bg": "#ea580c", "fg": "#fff7ed", "activebackground": "#fb923c", "activeforeground": "#fff7ed"}
        inactive = {"bg": "#2b313b", "fg": "#94a3b8", "activebackground": "#3d4653", "activeforeground": "#e2e8f0"}
        self.stable_mode_btn.configure(**(active if engine == "whisper" and not answer_mode else inactive))
        self.gemma_mode_btn.configure(**(active if engine == "ollama" and not answer_mode else inactive))
        self.ask_mode_btn.configure(**(active if answer_mode else inactive))

    def _set_visual_state(self, state: str) -> None:
        palettes = {
            "idle": {"bar": "#171a20", "fg": "#94a3b8", "dot": "#475569", "title": APP_NAME},
            "recording": {"bar": "#7f1d1d", "fg": "#fee2e2", "dot": "#f87171", "title": f"● RECORDING — {APP_NAME}"},
            "transcribing": {"bar": "#78350f", "fg": "#ffedd5", "dot": "#fbbf24", "title": f"… Transcribing — {APP_NAME}"},
        }
        palette = palettes.get(state, palettes["idle"])
        self._visual_state = state
        self.status_bar.configure(bg=palette["bar"])
        self.dot.configure(bg=palette["bar"])
        self.dot.itemconfig(self.dot_indicator, fill=palette["dot"])
        self.status_label.configure(bg=palette["bar"], fg=palette["fg"])
        self.elapsed_label.configure(bg=palette["bar"], fg=palette["fg"])
        try:
            self.root.title(palette["title"])
        except Exception:
            pass

    def _refresh_models(self) -> None:
        models = self.config.list_models()
        self.model_combo["values"] = models

        current = str(self.config.get("model", "")).strip()
        if current in models:
            self.model_var.set(current)
        elif models:
            self.model_var.set(models[0])
            self.config.set("model", models[0])
        else:
            self.model_var.set("")

    def _on_model_selected(self, _event: Any) -> None:
        model = self.model_var.get().strip()
        if not model:
            return
        self.config.set("model", model)

    def _on_engine_selected(self, _event: Any) -> None:
        engine = self.engine_var.get().strip().lower()
        engine = ENGINE_ALIASES.get(engine, engine)
        if engine not in SUPPORTED_TRANSCRIPTION_ENGINES:
            return
        self.config.set("transcription_engine", engine)
        self._update_mode_buttons()
        if engine == "ollama":
            self.controller.warm_up_transcriber_async()

    def _on_gemma_model_applied(self) -> None:
        model_ref = self.gemma_model_var.get().strip()
        if not model_ref:
            return
        if model_ref.startswith("/") or model_ref.startswith("~"):
            self.config.set("gemma_model_path", model_ref)
        else:
            self.config.set("gemma_model", model_ref)
            self.config.set("gemma_model_path", "")
        notify("Voice typing", f"Gemma model set to {model_ref}")

    def _on_ollama_model_applied(self) -> None:
        model_ref = self.ollama_model_var.get().strip()
        if not model_ref:
            return
        self.config.set("ollama_model", model_ref)
        notify("Voice typing", f"Ollama model set to {model_ref}")

    def _save_options(self) -> None:
        self.config.set("auto_paste_current_focus", bool(self.auto_paste_var.get()))
        self.config.set(
            "copy_to_clipboard_when_started_from_gui",
            bool(self.gui_clipboard_var.get()),
        )
        self.config.set("window_always_on_top", bool(self.always_on_top_var.get()))
        self.config.set("show_panel_on_hotkey_start", bool(self.show_on_hotkey_var.get()))
        self.config.set("no_gpu", not bool(self.use_gpu_var.get()))
        self.config.set("streaming_transcription", bool(self.streaming_var.get()))
        self.config.set("voice_commands_enabled", bool(self.voice_commands_var.get()))
        self.config.set("answer_speak_aloud", bool(self.speak_answers_var.get()))
        self._apply_window_presentation()

    def _toggle_from_gui(self) -> None:
        ok, message = self.controller.toggle_recording(capture_focus=False, started_from_gui=True)
        if not ok:
            self.status_var.set(message)
            notify("Voice typing", message, urgency="critical")

    def _stop_from_gui(self) -> None:
        self.controller.speaker.stop()
        ok, message = self.controller.stop_recording_async(reason="manual_stop")
        if not ok:
            self.status_var.set(message)

    def _retry_last_audio(self) -> None:
        ok, message = self.controller.retranscribe_path_async()
        self.route_var.set(message)
        if not ok:
            notify("Voice typing", message, urgency="critical")

    def _copy_last_transcript(self) -> None:
        self.transcript_box.configure(state="normal")
        text = self.transcript_box.get("1.0", "end").strip()
        self.transcript_box.configure(state="disabled")
        if not text:
            return
        if copy_to_clipboard(text):
            notify("Voice typing", "Last transcript copied to clipboard")

    def _show_model_help(self) -> None:
        models_dir = self.config.models_dir()
        whisper_dir = self.config.whisper_cli_path().parent.parent.parent
        msg = (
            "Whisper models:\n\n"
            f"cd {whisper_dir}\n"
            "./models/download-ggml-model.sh small.en\n"
            "./models/download-ggml-model.sh base.en\n\n"
            f"Then click Refresh.\nCurrent models directory: {models_dir}\n\n"
            "Gemma 4 through Transformers is optional. Set Engine = gemma4-transformers and use either "
            "google/gemma-4-E4B-it or a local model directory. Gemma audio clips are "
            "processed as rolling chunks of 30 seconds or less.\n\n"
            "For the existing local Gemma model, set Engine = ollama and Ollama = gemma4:latest."
        )
        messagebox.showinfo("Model Help", msg)

    def _tick(self) -> None:
        self._drain_events()
        self._update_elapsed()
        self.root.after(200, self._tick)

    def _drain_events(self) -> None:
        while True:
            try:
                event, data, meta = self.events.get_nowait()
            except queue.Empty:
                return

            if event == "state":
                recording = bool(data)
                reason = str(meta)
                if recording:
                    self.status_var.set("● RECORDING")
                    self._set_visual_state("recording")
                    self.toggle_btn.configure(text="Stop")
                    if reason == "warning":
                        self.status_var.set("● RECORDING (near limit)")
                else:
                    self.status_var.set("Idle")
                    self._set_visual_state("idle")
                    self.toggle_btn.configure(text="Record")
                    if reason == "limit_or_error":
                        self.status_var.set("Stopped (limit reached)")
                self._apply_window_presentation()

            if event == "audio_quality":
                verdict = str(data)
                message = str(meta)
                colors = {
                    "good": "#4ade80",
                    "quiet": "#facc15",
                    "very_quiet": "#fb923c",
                    "clipping": "#fb923c",
                    "silent": "#f87171",
                }
                self.mic_quality_var.set(message)
                self.mic_quality_label.configure(fg=colors.get(verdict, "#64748b"))

            if event == "ollama_models":
                names = list(data) if isinstance(data, (list, tuple)) else []
                if names:
                    self.ollama_combo["values"] = names
                    current = self.config.ollama_model()
                    if current in names:
                        self.ollama_model_var.set(current)

            if event == "transcript":
                text = str(data)
                route = str(meta)
                if text:
                    self.transcript_box.configure(state="normal")
                    self.transcript_box.delete("1.0", "end")
                    self.transcript_box.insert("1.0", text)
                    self.transcript_box.configure(state="disabled")
                route_label = {
                    "recognized": "Ready: transcript recognized.",
                    "focused_target": "Ready: inserted into original focused field.",
                    "focused_current_target": "Ready: inserted into current focused field.",
                    "focused_target_pasted": "Ready: pasted into original focused field.",
                    "ydotool_typed": "Ready: typed into current focus (virtual keyboard).",
                    "clipboard": "Ready: copied to clipboard.",
                    "clipboard+pasted": "Ready: copied and pasted into current focus.",
                    "clipboard_only": "Ready: copied to clipboard. Auto-paste failed.",
                    "gui_only": "Ready: stored in GUI transcript box.",
                    "awaiting_target": "Waiting: choose a target window.",
                    "empty": "Ready: no text detected.",
                }.get(route, "Transcript processed.")
                self.status_var.set(route_label)
                self.route_var.set(route_label)
                # Keep focus on the original target even while GUI stays visible.
                self._schedule_post_transcript_focus_restore()

            if event == "show":
                self._show_window(active=bool(meta) if meta is not None else True)

    def _update_elapsed(self) -> None:
        recording = self.controller.is_recording()
        transcribing = self.controller.is_transcribing()
        current = getattr(self, "_visual_state", "idle")

        # The transcribing phase has no controller event of its own, so the
        # 200ms tick keeps the bar in sync with it.
        if not recording and transcribing and current != "transcribing":
            self.status_var.set("Transcribing…")
            self._set_visual_state("transcribing")
        elif not recording and not transcribing and current == "transcribing":
            self.status_var.set("Idle")
            self._set_visual_state("idle")

        if current == "recording":
            # Blink the dot roughly every 600ms so recording is unmissable.
            self._blink_tick = getattr(self, "_blink_tick", 0) + 1
            if self._blink_tick % 3 == 0:
                fill = self.dot.itemcget(self.dot_indicator, "fill")
                self.dot.itemconfig(
                    self.dot_indicator,
                    fill="#7f1d1d" if fill == "#f87171" else "#f87171",
                )

        if not recording:
            self.elapsed_var.set("00:00")
            return

        elapsed = max(0, int(time.time() - self.controller.started_at()))
        mins, secs = divmod(elapsed, 60)
        self.elapsed_var.set(f"{mins:02d}:{secs:02d}")

    def _show_window(self, active: bool = True) -> None:
        try:
            # Hotkey path asks for passive show. If the panel is already visible,
            # do not re-show/deiconify it (that can steal focus in some WMs).
            if not active:
                try:
                    if self.root.state() == "normal" and self.root.winfo_viewable():
                        self._apply_window_presentation()
                        self._schedule_passive_show_focus_restore()
                        return
                except Exception:
                    pass
            self.root.deiconify()
            self._apply_window_presentation()
            if active:
                self.root.lift()
            else:
                # Immediately try to push compositor focus back to the original
                # app so our utility window doesn't steal keyboard input.
                self._restore_compositor_focus_immediately()
                self._schedule_passive_show_focus_restore()
        except Exception:
            pass

    def _restore_compositor_focus_immediately(self) -> None:
        """Push compositor focus back to the original target right after GUI show."""
        target = self.controller._focus_target or self.controller._focus_target_at_start
        if target is None:
            target = self.controller._recent_focus_target(max_age_seconds=5.0)
        if target is not None and target.source_pid is not None and target.source_pid != os.getpid():
            activate_window_by_pid(target.source_pid)

    def _schedule_passive_show_focus_restore(self) -> None:
        # Retry a few times because compositor timing can vary.
        for delay_ms in (80, 220, 500, 900):
            self.root.after(delay_ms, self._restore_external_focus_after_hotkey_show)

    def _schedule_post_transcript_focus_restore(self) -> None:
        # After transcript routing, reaffirm original target focus at multiple
        # intervals to handle varying compositor timing.
        for delay_ms in (40, 150, 350, 600, 1000):
            self.root.after(delay_ms, self._restore_original_focus_after_transcript)

    def _restore_external_focus_after_hotkey_show(self) -> None:
        try:
            self.controller.restore_last_external_focus(max_age_seconds=10.0)
        except Exception:
            pass

    def _restore_original_focus_after_transcript(self) -> None:
        try:
            self.controller.restore_original_focus_target(max_age_seconds=12.0)
        except Exception:
            pass

    def _on_close(self) -> None:
        if self.controller.is_recording():
            self.controller.stop_recording(reason="gui_closed")
        self.ipc.stop()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def send_ipc_raw(
    request: dict[str, Any],
    timeout: float = 1.0,
) -> tuple[dict[str, Any] | None, Exception | None]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(SOCKET_PATH)
        client.sendall(json.dumps(request).encode("utf-8"))
        resp = client.recv(65536)
        if not resp:
            return None, RuntimeError("empty ipc response")
        return json.loads(resp.decode("utf-8")), None
    except Exception as exc:
        return None, exc
    finally:
        try:
            client.close()
        except Exception:
            pass


def send_ipc(request: dict[str, Any], timeout: float = 1.0) -> dict[str, Any] | None:
    response, _ = send_ipc_raw(request=request, timeout=timeout)
    return response


def send_ipc_with_retries(
    request: dict[str, Any],
    attempts: int = 3,
    timeout: float = 1.2,
    delay_seconds: float = 0.2,
) -> dict[str, Any] | None:
    for idx in range(max(1, attempts)):
        resp = send_ipc(request, timeout=timeout)
        if resp is not None:
            return resp
        if idx < attempts - 1:
            time.sleep(delay_seconds)
    return None


def _acquire_spawn_lock(timeout_seconds: float = 1.5) -> Any | None:
    Path(SPAWN_LOCK_PATH).parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(SPAWN_LOCK_PATH, "w", encoding="utf-8")
    deadline = time.time() + timeout_seconds
    while True:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lock_file
        except BlockingIOError:
            if time.time() >= deadline:
                lock_file.close()
                return None
            time.sleep(0.05)
        except Exception:
            lock_file.close()
            return None


def _release_spawn_lock(lock_file: Any | None) -> None:
    if lock_file is None:
        return
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        lock_file.close()
    except Exception:
        pass


def _debounced_toggle_allowed(window_seconds: float = TOGGLE_DEBOUNCE_SECONDS) -> bool:
    lock_file = _acquire_spawn_lock(timeout_seconds=0.4)
    if lock_file is None:
        return False
    try:
        now = time.time()
        previous = 0.0
        try:
            if os.path.exists(TOGGLE_DEBOUNCE_PATH):
                raw = Path(TOGGLE_DEBOUNCE_PATH).read_text(encoding="utf-8").strip()
                if raw:
                    previous = float(raw)
        except Exception:
            previous = 0.0
        if now - previous < window_seconds:
            return False
        try:
            Path(TOGGLE_DEBOUNCE_PATH).write_text(f"{now:.6f}\n", encoding="utf-8")
        except Exception:
            pass
        return True
    finally:
        _release_spawn_lock(lock_file)


def spawn_gui_with_optional_toggle(toggle_on_launch: bool, focus_hint: dict[str, Any] | None = None) -> None:
    cmd = [sys.executable, str(Path(__file__).resolve()), "gui"]
    if toggle_on_launch:
        cmd.append("--toggle-on-launch")
    env = os.environ.copy()
    if focus_hint:
        try:
            env["VOICE_TYPING_FOCUS_HINT"] = json.dumps(focus_hint)
        except Exception:
            pass
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )


def cmd_toggle() -> int:
    if not _debounced_toggle_allowed(window_seconds=TOGGLE_DEBOUNCE_SECONDS):
        print("Ignored duplicate toggle")
        return 0

    # Capture focus target NOW (before any IPC delay) so the GUI server
    # gets the best possible hint about which window/field was active
    # when the hotkey was pressed.
    focus_hint: dict[str, Any] | None = None
    try:
        target = capture_focus_target_for_hotkey(retries=4, delay_s=0.02)
        if target is not None:
            focus_hint = focus_target_to_hint(target)
    except Exception:
        pass

    ipc_req: dict[str, Any] = {"cmd": "toggle"}
    if focus_hint is not None:
        ipc_req["focus_hint"] = focus_hint

    resp = send_ipc_with_retries(ipc_req, attempts=1, timeout=0.45)
    if resp is not None:
        print(resp.get("message", "ok"))
        return 0 if resp.get("ok", False) else 1

    # If socket exists but we can't talk to it, avoid spawning duplicate windows.
    if os.path.exists(SOCKET_PATH):
        _, err = send_ipc_raw({"cmd": "status"}, timeout=1.0)
        if isinstance(err, socket.timeout):
            print("Voice typing controller is busy, try again")
            return 1
        if isinstance(err, ConnectionRefusedError):
            try:
                os.unlink(SOCKET_PATH)
            except Exception:
                pass

    lock_file = _acquire_spawn_lock(timeout_seconds=1.5)
    if lock_file is None:
        print("Voice typing is already starting, try again")
        return 1
    try:
        # Another invocation may have already started the server while we waited for lock.
        resp_after_lock = send_ipc_with_retries({"cmd": "toggle"}, attempts=2, timeout=0.7)
        if resp_after_lock is not None:
            print(resp_after_lock.get("message", "ok"))
            return 0 if resp_after_lock.get("ok", False) else 1

        # GUI server isn't up yet. Start it and immediately begin recording.
        spawn_gui_with_optional_toggle(toggle_on_launch=True, focus_hint=None)
        print("Started GUI and began recording")
        return 0
    finally:
        _release_spawn_lock(lock_file)


def cmd_status() -> int:
    resp = send_ipc_with_retries({"cmd": "status"}, attempts=2, timeout=1.2)
    if resp is None:
        print("GUI server is not running")
        return 1
    print(json.dumps(resp, indent=2))
    return 0


def cmd_start() -> int:
    resp = send_ipc_with_retries({"cmd": "start"}, attempts=1, timeout=0.45)
    if resp is None:
        lock_file = _acquire_spawn_lock(timeout_seconds=1.5)
        if lock_file is None:
            print("Voice typing is already starting, try again")
            return 1
        try:
            resp_after_lock = send_ipc_with_retries({"cmd": "start"}, attempts=2, timeout=0.7)
            if resp_after_lock is None:
                spawn_gui_with_optional_toggle(toggle_on_launch=True, focus_hint=None)
                print("Started GUI and began recording")
                return 0
            print(resp_after_lock.get("message", "ok"))
            return 0 if resp_after_lock.get("ok", False) else 1
        finally:
            _release_spawn_lock(lock_file)
    print(resp.get("message", "ok"))
    return 0 if resp.get("ok", False) else 1


def cmd_stop() -> int:
    resp = send_ipc_with_retries({"cmd": "stop"}, attempts=3, timeout=2.0)
    if resp is None:
        print("No running GUI server")
        return 1
    print(resp.get("message", "ok"))
    return 0 if resp.get("ok", False) else 1


def cmd_paste_last() -> int:
    """Type the last transcript into the currently focused field.

    Uses ydotool (kernel-level typing) so the clipboard is NOT touched.
    Bind this to a hotkey (e.g. Ctrl+Shift+Y) for quick re-paste.
    """
    resp = send_ipc_with_retries({"cmd": "paste_last"}, attempts=2, timeout=2.0)
    if resp is None:
        print("No running GUI server")
        return 1
    print(resp.get("message", "ok"))
    return 0 if resp.get("ok", False) else 1


def cmd_set_model(model: str) -> int:
    resp = send_ipc_with_retries({"cmd": "set_model", "model": model}, attempts=2, timeout=1.2)
    if resp is None:
        # Offline fallback updates config directly.
        cfg = ConfigManager(CONFIG_PATH)
        available = cfg.list_models()
        if model not in available:
            print(f"Model not found in {cfg.models_dir()}: {model}")
            return 1
        cfg.set("model", model)
        print(f"Model set to {model}")
        return 0

    if resp.get("ok"):
        print(resp.get("message", "ok"))
        return 0
    print(resp.get("error", "failed"))
    return 1


def cmd_list_models() -> int:
    resp = send_ipc_with_retries({"cmd": "list_models"}, attempts=2, timeout=1.2)
    if resp is not None and resp.get("ok"):
        models = resp.get("models", [])
    else:
        cfg = ConfigManager(CONFIG_PATH)
        models = cfg.list_models()

    if not models:
        print("No models found")
        return 1

    for model in models:
        print(model)
    return 0


def cmd_list_engines() -> int:
    for engine in SUPPORTED_TRANSCRIPTION_ENGINES:
        print(engine)
    return 0


def _command_lines(cmd: list[str], timeout: float = 2.0) -> list[str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except Exception:
        return []
    if result.returncode != 0:
        return []
    return (result.stdout or "").splitlines()


def cmd_list_audio_inputs() -> int:
    print("default")
    seen = {"default"}
    for line in _command_lines(["pactl", "list", "short", "sources"]):
        parts = line.split()
        if len(parts) >= 2 and ".monitor" not in parts[1]:
            name = f"pulse:{parts[1]}"
            if name not in seen:
                print(name)
                seen.add(name)
    for line in _command_lines(["arecord", "-L"]):
        if line and not line.startswith((" ", "\t")) and line not in {"null", "default"}:
            name = f"alsa:{line}"
            if name not in seen:
                print(name)
                seen.add(name)
    return 0


def cmd_set_audio_input(device: str) -> int:
    device = device.strip()
    value = "" if device in {"", "default", "-d"} else device
    resp = send_ipc_with_retries({"cmd": "set_audio_input", "device": value}, attempts=2, timeout=1.2)
    if resp is None:
        cfg = ConfigManager(CONFIG_PATH)
        cfg.set("audio_input_device", value)
        print(f"audio input set to {value or 'default'}")
        return 0
    if resp.get("ok"):
        print(resp.get("message", "ok"))
        return 0
    print(resp.get("error", "failed"))
    return 1


def cmd_list_windows() -> int:
    windows = list_open_windows(limit=12)
    if not windows:
        print("No windows found")
        return 1
    for item in windows:
        title = str(item.get("title", "")).strip()
        wm_class = str(item.get("wm_class", "")).strip()
        pid = item.get("pid")
        label = title or wm_class
        if wm_class and wm_class.lower() not in label.lower():
            label = f"{wm_class}: {label}"
        print(f"{pid}: {label}")
    return 0


def cmd_set_engine(engine: str) -> int:
    engine = engine.strip().lower()
    engine = ENGINE_ALIASES.get(engine, engine)
    if engine not in SUPPORTED_TRANSCRIPTION_ENGINES:
        print(f"Engine must be one of: {', '.join(SUPPORTED_TRANSCRIPTION_ENGINES)}")
        return 1

    resp = send_ipc_with_retries({"cmd": "set_engine", "engine": engine}, attempts=2, timeout=1.2)
    if resp is None:
        cfg = ConfigManager(CONFIG_PATH)
        cfg.set("transcription_engine", engine)
        print(f"engine set to {engine}")
        return 0
    if resp.get("ok"):
        print(resp.get("message", "ok"))
        return 0
    print(resp.get("error", "failed"))
    return 1


def cmd_set_gemma_model(model: str) -> int:
    model = model.strip()
    if not model:
        print("model is required")
        return 1

    resp = send_ipc_with_retries({"cmd": "set_gemma_model", "model": model}, attempts=2, timeout=1.2)
    if resp is None:
        cfg = ConfigManager(CONFIG_PATH)
        if model.startswith("/") or model.startswith("~"):
            cfg.set("gemma_model_path", model)
        else:
            cfg.set("gemma_model", model)
            cfg.set("gemma_model_path", "")
        print(f"Gemma model set to {model}")
        return 0
    if resp.get("ok"):
        print(resp.get("message", "ok"))
        return 0
    print(resp.get("error", "failed"))
    return 1


def cmd_set_ollama_model(model: str) -> int:
    model = model.strip()
    if not model:
        print("model is required")
        return 1

    resp = send_ipc_with_retries({"cmd": "set_ollama_model", "model": model}, attempts=2, timeout=1.2)
    if resp is None:
        cfg = ConfigManager(CONFIG_PATH)
        cfg.set("ollama_model", model)
        print(f"Ollama model set to {model}")
        return 0
    if resp.get("ok"):
        print(resp.get("message", "ok"))
        return 0
    print(resp.get("error", "failed"))
    return 1


def cmd_set_profile(profile: str) -> int:
    profile = profile.strip().lower()
    updates = PROFILE_CONFIGS.get(profile)
    if updates is None:
        print(f"Profile must be one of: {', '.join(sorted(PROFILE_CONFIGS))}")
        return 1
    cfg = ConfigManager(CONFIG_PATH)
    for key, value in updates.items():
        cfg.data[key] = value
    cfg.data["active_profile"] = profile
    cfg.save()
    print(f"profile set to {profile}")
    if send_ipc_with_retries({"cmd": "status"}, attempts=1, timeout=0.25) is not None:
        print("Restart the Voice Typing GUI for profile changes to fully apply.")
    return 0


def cmd_ask(question: str, speak: bool) -> int:
    cfg = ConfigManager(CONFIG_PATH)
    controller = RecorderController(config=cfg)
    model_name = cfg.ollama_model()
    if model_name not in {str(item.get("model", "")) for item in _ollama_loaded_models(cfg.ollama_url())}:
        print(f"(loading {model_name} — a cold start can take a minute...)", file=sys.stderr)
    if speak and controller.speaker.available():
        spoken: list[str] = []

        def _on_sentence(sentence: str) -> None:
            spoken.append(sentence)
            controller.speaker.speak(sentence)
            print(sentence, flush=True)

        answer, err = controller.ask_ollama_stream(question, _on_sentence)
        if err:
            print(err)
            return 1
        # Wait for playback of queued sentences to finish.
        while controller.speaker.is_busy():
            time.sleep(0.3)
        return 0
    answer, err = controller.ask_ollama(question)
    if err:
        print(err)
        return 1
    print(answer)
    return 0


def cmd_list_recordings() -> int:
    cfg = ConfigManager(CONFIG_PATH)
    controller = RecorderController(config=cfg)
    recordings_dir = controller.recordings_dir()
    wavs = sorted(recordings_dir.glob("*.wav")) if recordings_dir.exists() else []
    if not wavs:
        print(f"No saved recordings in {recordings_dir}")
        return 1
    for wav in wavs:
        duration = audio_duration_seconds(wav)
        mins, secs = divmod(int(duration), 60)
        print(f"{wav.name}  {mins:02d}:{secs:02d}  {wav.stat().st_size // 1024} KB")
    return 0


def cmd_retranscribe(target: str) -> int:
    cfg = ConfigManager(CONFIG_PATH)
    controller = RecorderController(config=cfg)
    if target in ("", "last"):
        audio_path = controller.last_recording_path()
        if audio_path is None:
            print(f"No saved recordings in {controller.recordings_dir()}")
            return 1
    else:
        audio_path = Path(os.path.expanduser(target))
        if not audio_path.is_absolute():
            candidate = controller.recordings_dir() / target
            if candidate.exists():
                audio_path = candidate
        if not audio_path.exists():
            print(f"Audio file not found: {audio_path}")
            return 1

    duration = audio_duration_seconds(audio_path)
    verdict, detail = analyze_audio_quality(audio_path)
    if verdict:
        print(f"audio quality: {AUDIO_QUALITY_LABELS.get(verdict, verdict)} ({detail})", file=sys.stderr)
    print(f"Transcribing {audio_path} ({duration:.0f}s of audio) with engine '{cfg.transcription_engine()}'...")
    text, err = controller._transcribe_audio_path(audio_path)
    if err:
        print(f"error: {err}")
        return 1
    transcript = text.strip()
    if not transcript:
        print("Transcript is empty")
        return 1
    print(transcript)
    if copy_to_clipboard(transcript):
        print("(copied to clipboard)", file=sys.stderr)
    return 0


def cmd_doctor() -> int:
    cfg = ConfigManager(CONFIG_PATH)
    engine = cfg.transcription_engine()
    checks = {
        "engine": engine,
        "audio_input_device": cfg.get("audio_input_device", "") or "default",
        "sox": shutil.which("sox") is not None,
        "wl-copy": shutil.which("wl-copy") is not None,
        "ydotool": shutil.which("ydotool") is not None,
        "whisper_cli": cfg.whisper_cli_path().exists(),
        "whisper_model": cfg.model_path().exists(),
        "gemma_model": cfg.gemma_model_ref(),
        "ollama_cli": shutil.which("ollama") is not None,
        "ollama_model": cfg.ollama_model(),
        "ollama_url": cfg.ollama_url(),
        "python_torch": importlib.util.find_spec("torch") is not None,
        "python_transformers": importlib.util.find_spec("transformers") is not None,
        "python_accelerate": importlib.util.find_spec("accelerate") is not None,
        "python_soundfile": importlib.util.find_spec("soundfile") is not None,
    }
    print(json.dumps(checks, indent=2))
    required_by_engine = {
        "whisper": ("sox", "whisper_cli", "whisper_model"),
        "gemma4-transformers": (
            "sox",
            "python_torch",
            "python_transformers",
            "python_accelerate",
            "python_soundfile",
        ),
        "ollama": ("sox", "ollama_cli"),
    }.get(engine, ("sox",))
    missing = [name for name in required_by_engine if checks.get(name) is False]
    return 1 if missing else 0


def reuse_existing_gui_instance(toggle_on_launch: bool) -> bool:
    status = send_ipc_with_retries({"cmd": "status"}, attempts=2, timeout=1.0, delay_seconds=0.15)
    if status is None:
        return False
    if toggle_on_launch:
        send_ipc_with_retries({"cmd": "toggle"}, attempts=2, timeout=1.6, delay_seconds=0.2)
    else:
        send_ipc({"cmd": "show"}, timeout=0.9)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline voice typing controller")
    sub = parser.add_subparsers(dest="command", required=True)

    gui_parser = sub.add_parser("gui", help="Run small recording GUI")
    gui_parser.add_argument("--toggle-on-launch", action="store_true")

    sub.add_parser("toggle", help="Toggle recording via IPC (for hotkey)")
    sub.add_parser("start", help="Start recording")
    sub.add_parser("stop", help="Stop recording and transcribe")
    sub.add_parser("paste-last", help="Type last transcript into current focus (no clipboard)")
    sub.add_parser("status", help="Show controller status")
    sub.add_parser("list-models", help="List available whisper models")
    sub.add_parser("list-engines", help="List transcription engines")
    sub.add_parser("list-audio-inputs", help="List microphone/input devices")
    sub.add_parser("list-windows", help="List open GNOME windows")
    sub.add_parser("doctor", help="Check local runtime dependencies")

    set_model_parser = sub.add_parser("set-model", help="Set active whisper model")
    set_model_parser.add_argument("model", help="Model file name (example: ggml-small.en.bin)")

    set_audio_parser = sub.add_parser("set-audio-input", help="Set microphone/input device")
    set_audio_parser.add_argument("device", help="default, pulse:<source>, or alsa:<device>")

    set_engine_parser = sub.add_parser("set-engine", help="Set transcription engine")
    set_engine_parser.add_argument("engine", help="whisper, ollama, or gemma4-transformers")

    set_gemma_parser = sub.add_parser("set-gemma-model", help="Set Gemma model id or local directory")
    set_gemma_parser.add_argument("model", help="example: google/gemma-4-E4B-it or /path/to/model")

    set_ollama_parser = sub.add_parser("set-ollama-model", help="Set Ollama model name")
    set_ollama_parser.add_argument("model", help="example: gemma4:latest")

    set_profile_parser = sub.add_parser("set-profile", help="Set a tested config profile")
    set_profile_parser.add_argument("profile", help="stable, gemma-agent, or antonio")

    sub.add_parser("list-recordings", help="List archived recordings")

    ask_parser = sub.add_parser("ask", help="Ask the local Gemma (Ollama) a question")
    ask_parser.add_argument("--speak", action="store_true", help="Speak the answer aloud with piper")
    ask_parser.add_argument("question", nargs="+", help="The question text")

    retranscribe_parser = sub.add_parser(
        "retranscribe", help="Transcribe an archived recording again (default: newest)"
    )
    retranscribe_parser.add_argument(
        "target", nargs="?", default="last", help="'last', a file name from list-recordings, or a path"
    )

    args = parser.parse_args()

    if args.command == "gui":
        if reuse_existing_gui_instance(toggle_on_launch=bool(args.toggle_on_launch)):
            return 0
        startup_focus_target = None
        startup_focus_hint: dict[str, Any] | None = None
        focus_hint_env = os.environ.get("VOICE_TYPING_FOCUS_HINT", "").strip()
        if focus_hint_env:
            try:
                parsed = json.loads(focus_hint_env)
                if isinstance(parsed, dict):
                    startup_focus_hint = parsed
            except Exception:
                startup_focus_hint = None
        app = VoiceTypingGui(
            toggle_on_launch=bool(args.toggle_on_launch),
            startup_focus_target=startup_focus_target,
            startup_focus_hint=startup_focus_hint,
        )
        app.run()
        return 0
    if args.command == "toggle":
        return cmd_toggle()
    if args.command == "start":
        return cmd_start()
    if args.command == "stop":
        return cmd_stop()
    if args.command == "paste-last":
        return cmd_paste_last()
    if args.command == "status":
        return cmd_status()
    if args.command == "list-models":
        return cmd_list_models()
    if args.command == "list-engines":
        return cmd_list_engines()
    if args.command == "list-audio-inputs":
        return cmd_list_audio_inputs()
    if args.command == "list-windows":
        return cmd_list_windows()
    if args.command == "set-model":
        return cmd_set_model(args.model)
    if args.command == "set-audio-input":
        return cmd_set_audio_input(args.device)
    if args.command == "set-engine":
        return cmd_set_engine(args.engine)
    if args.command == "set-gemma-model":
        return cmd_set_gemma_model(args.model)
    if args.command == "set-ollama-model":
        return cmd_set_ollama_model(args.model)
    if args.command == "set-profile":
        return cmd_set_profile(args.profile)
    if args.command == "list-recordings":
        return cmd_list_recordings()
    if args.command == "ask":
        return cmd_ask(" ".join(args.question), speak=bool(args.speak))
    if args.command == "retranscribe":
        return cmd_retranscribe(args.target)
    if args.command == "doctor":
        return cmd_doctor()

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
