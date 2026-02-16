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
import fcntl
import json
import os
import queue
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
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
TRANSCRIPT_PREFIX = STATE_DIR / "transcript"
ROUTE_LOG_PATH = STATE_DIR / "routing.log"
SPAWN_LOCK_PATH = "/tmp/voice_typing_spawn.lock"
TOGGLE_DEBOUNCE_PATH = "/tmp/voice_typing_toggle.debounce"
TOGGLE_DEBOUNCE_SECONDS = 0.18
MIN_HOTKEY_RECORDING_SECONDS_BEFORE_STOP = 0.35


DEFAULT_CONFIG: dict[str, Any] = {
    "whisper_cli_path": "~/whisper.cpp/build/bin/whisper-cli",
    "models_dir": "~/whisper.cpp/models",
    "model": "",
    "recording_max_seconds": 180,
    "recording_warn_before_seconds": 15,
    "transcribe_timeout_seconds": 90,
    "no_gpu": True,
    "language": "en",
    "auto_paste_current_focus": False,
    "copy_to_clipboard_when_started_from_gui": False,
    "direct_type_fallback_for_hotkey": True,
    "paste_fallback_for_hotkey": True,
    "restore_clipboard_after_paste": False,
    "window_always_on_top": True,
    "show_panel_on_hotkey_start": True,
    "window_opacity": 0.95,
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


def log_routing(message: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with ROUTE_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"[{ts}] {message}\n")
    except Exception:
        pass


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


class RecorderController:
    def __init__(
        self,
        config: ConfigManager,
        on_state_change: Callable[[bool, str], None] | None = None,
        on_transcript: Callable[[str, str], None] | None = None,
    ) -> None:
        self.config = config
        self.on_state_change = on_state_change
        self.on_transcript = on_transcript

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
            if AUDIO_PATH.exists():
                AUDIO_PATH.unlink(missing_ok=True)

            max_seconds = int(self.config.get("recording_max_seconds", 180))
            sox_cmd = [
                "sox",
                "-q",
                "-d",
                "-r",
                "16000",
                "-c",
                "1",
                "-b",
                "16",
                str(AUDIO_PATH),
                "trim",
                "0",
                str(max_seconds),
            ]

            try:
                self._process = subprocess.Popen(
                    sox_cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            except FileNotFoundError:
                return False, "SoX is not installed or not in PATH"
            except Exception as exc:
                return False, f"Failed to start recording: {exc}"

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
        threading.Thread(target=self._watch_sox_process, daemon=True).start()
        return True, "Recording started"

    def stop_recording(self, reason: str = "manual_stop") -> tuple[bool, str]:
        process: subprocess.Popen[str] | None
        with self._lock:
            if not self._recording:
                return False, "Not recording"
            self._recording = False
            self._transcribing = True
            process = self._process
            self._process = None

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
        return self._finish_transcription(reason)

    def stop_recording_async(self, reason: str = "manual_stop") -> tuple[bool, str]:
        process: subprocess.Popen[str] | None
        with self._lock:
            if not self._recording:
                return False, "Not recording"
            self._recording = False
            self._transcribing = True
            process = self._process
            self._process = None

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
        max_seconds = int(self.config.get("recording_max_seconds", 180))

        if warn_before <= 0 or warn_before >= max_seconds:
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

    def _finish_transcription(self, reason: str) -> tuple[bool, str]:
        try:
            if not AUDIO_PATH.exists() or AUDIO_PATH.stat().st_size == 0:
                notify("Voice typing", "No audio captured")
                return False, "No audio captured"

            notify("Voice typing", "Transcribing...")
            text, err = self._transcribe_audio()
            if err:
                notify("Voice typing", err, urgency="critical")
                return False, err

            transcript = text.strip()
            if not transcript:
                notify("Voice typing", "Transcript is empty")
                self._emit_transcript("", "empty")
                return False, "Transcript is empty"

            # Store transcript so it can be pasted later via the paste-last hotkey.
            self._last_transcript = transcript

            # Surface transcript to UI immediately, even if insertion/clipboard routing fails.
            self._emit_transcript(transcript, "recognized")
            route = self._route_transcript(transcript)
            self._emit_transcript(transcript, route)

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

    def _transcribe_audio(self) -> tuple[str, str | None]:
        whisper_cli = self.config.whisper_cli_path()
        if not whisper_cli.exists():
            return "", f"whisper-cli not found: {whisper_cli}"

        model_path = self.config.model_path()
        if not model_path.exists():
            return "", f"Model not found: {model_path}"

        out_prefix = TRANSCRIPT_PREFIX
        txt_path = Path(str(out_prefix) + ".txt")
        txt_path.unlink(missing_ok=True)

        cmd = [
            str(whisper_cli),
            "-m",
            str(model_path),
            "-f",
            str(AUDIO_PATH),
            "-nt",
            "-of",
            str(out_prefix),
            "-l",
            str(self.config.get("language", "en")),
        ]
        if bool(self.config.get("no_gpu", True)):
            cmd.append("--no-gpu")

        timeout_sec = int(self.config.get("transcribe_timeout_seconds", 90))

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
                "model": self.config.get("model", ""),
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

        if cmd == "set_model":
            model = str(req.get("model", "")).strip()
            if not model:
                return {"ok": False, "error": "model is required"}
            available = self.config.list_models()
            if model not in available:
                return {"ok": False, "error": f"model not found in models dir: {model}"}
            self.config.set("model", model)
            return {"ok": True, "message": f"model set to {model}"}

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
        self.root.geometry("420x220")
        self.root.minsize(320, 150)
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
        self.model_var = tk.StringVar(value=str(self.config.get("model", "")))

        self.controller = RecorderController(
            config=self.config,
            on_state_change=lambda recording, reason: self.events.put(("state", recording, reason)),
            on_transcript=lambda text, route: self.events.put(("transcript", text, route)),
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

        top = ttk.Frame(root_pad, style="VT.Card.TFrame", padding=(8, 8))
        top.pack(fill="x")

        self.dot = tk.Canvas(top, width=12, height=12, highlightthickness=0, bd=0, bg="#171a20")
        self.dot.pack(side="left", padx=(0, 6))
        self.dot_indicator = self.dot.create_oval(1, 1, 11, 11, fill="#22c55e", outline="")

        ttk.Label(top, textvariable=self.status_var, style="VT.Status.TLabel").pack(side="left")
        ttk.Label(top, textvariable=self.elapsed_var, style="VT.Meta.TLabel").pack(side="left", padx=(8, 0))

        self.toggle_btn = ttk.Button(top, text="Record", style="VT.TButton", command=self._toggle_from_gui)
        self.toggle_btn.pack(side="right")

        ttk.Label(root_pad, textvariable=self.route_var, style="VT.Meta.TLabel").pack(
            anchor="w", pady=(4, 0)
        )

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

        model_row = ttk.Frame(options_frame, style="VT.Card.TFrame")
        model_row.pack(fill="x", pady=(8, 4))
        ttk.Label(model_row, text="Model:", style="VT.Meta.TLabel").pack(side="left")
        self.model_combo = ttk.Combobox(model_row, textvariable=self.model_var, state="readonly")
        self.model_combo.pack(side="left", fill="x", expand=True, padx=(6, 6))
        self.model_combo.bind("<<ComboboxSelected>>", self._on_model_selected)
        ttk.Button(model_row, text="Refresh", style="VT.Detail.TButton", command=self._refresh_models).pack(
            side="left"
        )

        self.auto_paste_var = tk.BooleanVar(value=bool(self.config.get("auto_paste_current_focus", False)))
        self.gui_clipboard_var = tk.BooleanVar(
            value=bool(self.config.get("copy_to_clipboard_when_started_from_gui", False))
        )
        self.always_on_top_var = tk.BooleanVar(value=bool(self.config.get("window_always_on_top", True)))
        self.show_on_hotkey_var = tk.BooleanVar(value=bool(self.config.get("show_panel_on_hotkey_start", True)))
        self.use_gpu_var = tk.BooleanVar(value=not bool(self.config.get("no_gpu", True)))

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

        bottom = ttk.Frame(options_frame, style="VT.Card.TFrame")
        bottom.pack(fill="x")
        ttk.Button(bottom, text="Copy Last", style="VT.Detail.TButton", command=self._copy_last_transcript).pack(
            side="left"
        )
        ttk.Button(bottom, text="Stop", style="VT.Detail.TButton", command=self._stop_from_gui).pack(
            side="left",
            padx=(6, 0),
        )
        ttk.Button(bottom, text="Model Help", style="VT.Detail.TButton", command=self._show_model_help).pack(
            side="right"
        )

        self._refresh_models()

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

    def _save_options(self) -> None:
        self.config.set("auto_paste_current_focus", bool(self.auto_paste_var.get()))
        self.config.set(
            "copy_to_clipboard_when_started_from_gui",
            bool(self.gui_clipboard_var.get()),
        )
        self.config.set("window_always_on_top", bool(self.always_on_top_var.get()))
        self.config.set("show_panel_on_hotkey_start", bool(self.show_on_hotkey_var.get()))
        self.config.set("no_gpu", not bool(self.use_gpu_var.get()))
        self._apply_window_presentation()

    def _toggle_from_gui(self) -> None:
        ok, message = self.controller.toggle_recording(capture_focus=False, started_from_gui=True)
        if not ok:
            self.status_var.set(message)
            notify("Voice typing", message, urgency="critical")

    def _stop_from_gui(self) -> None:
        ok, message = self.controller.stop_recording_async(reason="manual_stop")
        if not ok:
            self.status_var.set(message)

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
            "To download models, run one of these commands in a terminal:\n\n"
            f"cd {whisper_dir}\n"
            "./models/download-ggml-model.sh small.en\n"
            "./models/download-ggml-model.sh base.en\n\n"
            f"Then click Refresh.\nCurrent models directory: {models_dir}"
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
                    self.status_var.set("Recording...")
                    self.dot.itemconfig(self.dot_indicator, fill="#dc2626")
                    self.toggle_btn.configure(text="Stop")
                    if reason == "warning":
                        self.status_var.set("Recording... (near limit)")
                else:
                    self.status_var.set("Idle")
                    self.dot.itemconfig(self.dot_indicator, fill="#16a34a")
                    self.toggle_btn.configure(text="Record")
                    if reason == "limit_or_error":
                        self.status_var.set("Stopped (limit reached)")
                self._apply_window_presentation()

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
                    "empty": "Ready: no text detected.",
                }.get(route, "Transcript processed.")
                self.status_var.set(route_label)
                self.route_var.set(route_label)
                # Keep focus on the original target even while GUI stays visible.
                self._schedule_post_transcript_focus_restore()

            if event == "show":
                self._show_window(active=bool(meta) if meta is not None else True)

    def _update_elapsed(self) -> None:
        if not self.controller.is_recording():
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

    set_model_parser = sub.add_parser("set-model", help="Set active whisper model")
    set_model_parser.add_argument("model", help="Model file name (example: ggml-small.en.bin)")

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
    if args.command == "set-model":
        return cmd_set_model(args.model)

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
