#!/usr/bin/env python3
"""Voice Studio: one window for dictation (voice_typing) and reading aloud
(voice_reading).

Both controllers and both IPC sockets live in this process, so the hotkey
scripts (voice-toggle, voice-read, voice-translate, voice-paste) keep
talking to /tmp/voice_typing.sock and /tmp/voice_reading.sock unchanged.
"""

from __future__ import annotations

import argparse
import array
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import messagebox, ttk

import voice_reading as vr
import voice_typing as vt
from studio_widgets import (
    ACCENT,
    ACCENT_SOFT,
    BG,
    BORDER,
    EDITOR_BG,
    MUTED,
    PASSAGE_BG,
    READ,
    READ_SOFT,
    REC,
    REC_HOVER,
    REC_SOFT,
    SELECTION,
    SUBTLE,
    SURFACE,
    SURFACE_2,
    SURFACE_3,
    TEXT,
    WARN,
    WARN_SOFT,
    WORD_BG,
    CircleButton,
    FlatButton,
    Fonts,
    ScrollFrame,
    Segmented,
    Switch,
    apply_theme,
    card,
    draw_icon,
    form_row,
    label,
)

APP_TITLE = "Voice Studio"
TABS = [("dictation", "Dictation"), ("reader", "Reader"), ("settings", "Settings")]
LANGUAGE_TARGETS = ["English", "Hebrew", "French", "Italian"]
TICK_MS = 40

ROUTE_LABELS = {
    "recognized": "Transcript recognized.",
    "focused_target": "Inserted into the original text field.",
    "focused_current_target": "Inserted into the focused field.",
    "focused_target_pasted": "Pasted into the original text box.",
    "ydotool_typed": "Typed into the focused app.",
    "clipboard": "Copied to the clipboard.",
    "clipboard+pasted": "Pasted into the focused app.",
    "clipboard_only": "Copied to the clipboard.",
    "gui_only": "Kept here in Voice Studio.",
    "awaiting_target": "Waiting: say which window to write in.",
    "empty": "No speech detected.",
}
ROUTE_BADGES = {
    "focused_target": ("ok", "Inserted"),
    "focused_current_target": ("ok", "Inserted"),
    "focused_target_pasted": ("ok", "Pasted"),
    "ydotool_typed": ("ok", "Typed"),
    "clipboard+pasted": ("ok", "Pasted"),
    "clipboard": ("copied", "Copied to clipboard"),
    "clipboard_only": ("copied", "Copied to clipboard"),
    "gui_only": ("copied", "In Voice Studio"),
    "awaiting_target": ("copied", "Say where to write"),
    "empty": ("error", "No speech detected"),
}


def read_custom_hotkeys() -> dict[str, str]:
    """GNOME custom shortcuts by script name, e.g. {"voice-toggle": "Super+T"}."""
    schema = "org.gnome.settings-daemon.plugins.media-keys"
    hotkeys: dict[str, str] = {}
    if shutil.which("gsettings") is None:
        return hotkeys

    def _get(*args: str) -> str:
        try:
            result = subprocess.run(["gsettings", "get", *args], capture_output=True, text=True, timeout=1.5, check=False)
            return (result.stdout or "").strip()
        except Exception:
            return ""

    for path in re.findall(r"'([^']+)'", _get(schema, "custom-keybindings")):
        binding = _get(f"{schema}.custom-keybinding:{path}", "binding").strip("'")
        command = _get(f"{schema}.custom-keybinding:{path}", "command").strip("'")
        if not binding or not command:
            continue
        name = Path(command.split()[0]).name
        mods = re.findall(r"<(\w+)>", binding)
        key = re.sub(r"<\w+>", "", binding)
        pretty = "+".join([m.replace("Primary", "Ctrl").capitalize() for m in mods] + [key.upper() if len(key) == 1 else key])
        hotkeys[name] = pretty
    return hotkeys


def primary_monitor_geometry(root: tk.Misc) -> tuple[int, int, int, int]:
    """(x, y, width, height) of the primary monitor in X coordinates."""
    try:
        result = subprocess.run(["xrandr", "--listactivemonitors"], capture_output=True, text=True, timeout=1.0, check=False)
        monitors = []
        for line in (result.stdout or "").splitlines():
            match = re.search(r"(\+\*?)\S*\s+(\d+)/\d+x(\d+)/\d+\+(-?\d+)\+(-?\d+)", line)
            if match:
                primary = "*" in match.group(1)
                monitors.append((primary, int(match.group(4)), int(match.group(5)), int(match.group(2)), int(match.group(3))))
        if monitors:
            chosen = next((m for m in monitors if m[0]), monitors[0])
            return chosen[1], chosen[2], chosen[3], chosen[4]
    except Exception:
        pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


class AudioLevelProbe:
    """Loudness of the last ~0.1s that sox wrote, for the live mic meters."""

    def __init__(self) -> None:
        self._chunk_path: Path | None = None
        self._chunk_scan_at = 0.0

    def level(self, chunked: bool) -> float:
        path = vt.AUDIO_PATH
        if chunked:
            now = time.monotonic()
            if now - self._chunk_scan_at > 0.5:
                self._chunk_scan_at = now
                try:
                    chunks = sorted(vt.CHUNK_DIR.glob("chunk_*.wav"), key=lambda p: p.stat().st_mtime)
                    self._chunk_path = chunks[-1] if chunks else None
                except Exception:
                    self._chunk_path = None
            if self._chunk_path is None:
                return 0.0
            path = self._chunk_path
        try:
            size = path.stat().st_size
            if size < 44 + 3200:
                return 0.0
            with path.open("rb") as handle:
                handle.seek(max(44, (size - 3200) // 2 * 2))
                data = handle.read(3200)
        except Exception:
            return 0.0
        samples = array.array("h")
        samples.frombytes(data[: len(data) // 2 * 2])
        if sys.byteorder != "little":
            samples.byteswap()
        if not samples:
            return 0.0
        rms = math.sqrt(sum(s * s for s in samples) / len(samples))
        db = 20.0 * math.log10(rms / 32768.0 + 1e-9)
        return max(0.0, min(1.0, (db + 55.0) / 45.0))


class RecordingBadge:
    """Floating "recording" pill on the primary monitor.

    Override-redirect, so the window manager never gives it focus: the text
    box the user is dictating into keeps the keyboard. A click stops the
    recording, like pressing the hotkey again.
    """

    WIDTH = 268
    HEIGHT = 58

    def __init__(self, app: "StudioApp") -> None:
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        try:
            self.win.attributes("-topmost", True)
            self.win.attributes("-alpha", 0.97)
        except tk.TclError:
            pass
        self.canvas = tk.Canvas(
            self.win,
            width=self.WIDTH,
            height=self.HEIGHT,
            bg=SURFACE,
            highlightthickness=1,
            highlightbackground=SURFACE_3,
            bd=0,
            cursor="hand2",
        )
        self.canvas.pack()
        self.canvas.bind("<ButtonRelease-1>", self._on_click)
        self.state = "hidden"
        self.started_at = 0.0
        self.message = ""
        self.detail = ""
        self.kind = ""
        self.hide_at = 0.0
        self.levels: list[float] = [0.0] * 14
        self._shown = False

    def _enabled(self) -> bool:
        return bool(self.app.typing_config.get("indicator_enabled", True))

    def _place(self) -> None:
        x, y, width, height = primary_monitor_geometry(self.app.root)
        position = str(self.app.typing_config.get("indicator_position", "bottom"))
        px = x + (width - self.WIDTH) // 2
        py = y + 64 if position == "top" else y + height - self.HEIGHT - 72
        self.win.geometry(f"{self.WIDTH}x{self.HEIGHT}+{px}+{py}")

    def _show(self) -> None:
        if not self._enabled():
            self.hide()
            return
        if not self._shown:
            self._place()
            self.win.deiconify()
            self.win.lift()
            self._shown = True

    def show_recording(self, started_at: float) -> None:
        self.state = "recording"
        self.started_at = started_at
        self.levels = [0.0] * len(self.levels)
        self._show()

    def show_transcribing(self) -> None:
        if self.state == "hidden" and not self._enabled():
            return
        self.state = "transcribing"
        self._show()

    def show_result(self, kind: str, message: str, detail: str = "", seconds: float = 2.2) -> None:
        if not bool(self.app.typing_config.get("indicator_show_result", True)) and kind != "error":
            self.hide()
            return
        self.state = "result"
        self.kind = kind
        self.message = message
        self.detail = detail
        self.hide_at = time.monotonic() + seconds
        self._show()

    def hide(self) -> None:
        self.state = "hidden"
        if self._shown:
            self.win.withdraw()
            self._shown = False

    def _on_click(self, _event: Any) -> None:
        if self.state == "recording":
            self.app.dictation.stop_recording()
        elif self.state == "result":
            self.hide()

    def tick(self, level: float, phase: float) -> None:
        if self.state == "hidden":
            return
        if self.state == "result" and time.monotonic() >= self.hide_at:
            self.hide()
            return
        if self.state == "recording":
            self.levels = self.levels[1:] + [level]
        self._draw(level, phase)

    def _draw(self, level: float, phase: float) -> None:
        c = self.canvas
        fonts = self.app.fonts
        c.delete("all")
        cx, cy, r = 29, self.HEIGHT / 2, 17
        title, subtitle = "", ""
        if self.state == "recording":
            ring = r + 3 + level * 9
            c.create_oval(cx - ring, cy - ring, cx + ring, cy + ring, fill=REC_SOFT, outline="")
            c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=REC, outline="")
            draw_icon(c, "mic", cx, cy, r * 2, "#ffffff")
            elapsed = max(0, int(time.time() - self.started_at))
            title = "Recording"
            subtitle = f"{elapsed // 60:02d}:{elapsed % 60:02d} · click to stop"
            base_x = self.WIDTH - 16 - (len(self.levels) - 1) * 6
            for index, value in enumerate(self.levels):
                bar = 3 + value * 26
                x = base_x + index * 6
                c.create_line(x, cy - bar / 2, x, cy + bar / 2, fill=REC if value > 0.08 else SURFACE_3, width=3, capstyle="round")
        elif self.state == "transcribing":
            c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=WARN_SOFT, outline="")
            draw_icon(c, "spinner", cx, cy, r * 2, WARN, phase)
            title = "Transcribing…"
            subtitle = "Pastes where you started"
        else:
            fill, icon_color, icon = {
                "ok": (READ_SOFT, READ, "check"),
                "copied": (ACCENT_SOFT, ACCENT, "clipboard"),
                "error": (REC_SOFT, REC, "cross"),
            }.get(self.kind, (SURFACE_3, TEXT, "check"))
            c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=fill, outline="")
            draw_icon(c, icon, cx, cy, r * 2, icon_color)
            title = self.message
            subtitle = self.detail
        text_x = cx + r + 14
        if subtitle:
            c.create_text(text_x, cy - 9, text=title, anchor="w", fill=TEXT, font=fonts.heading)
            c.create_text(text_x, cy + 11, text=subtitle, anchor="w", fill=MUTED, font=fonts.small)
        else:
            c.create_text(text_x, cy, text=title, anchor="w", fill=TEXT, font=fonts.heading)


class DictationPanel:
    def __init__(self, parent: tk.Misc, app: "StudioApp") -> None:
        self.app = app
        self.recorder: vt.RecorderController = app.recorder
        self.config: vt.ConfigManager = app.typing_config
        fonts = app.fonts
        self.frame = tk.Frame(parent, bg=BG)
        self._visual = "idle"
        self._last_result_at = 0.0
        self._expect_result = False
        self._idle_since: float | None = None

        hero = card(self.frame, padx=22, pady=18)
        hero.pack(fill="x")
        self.mic = CircleButton(
            hero,
            86,
            "mic",
            command=self._toggle_from_gui,
            fill=SURFACE_3,
            hover="#353b4c",
            ring_space=14,
            tooltip="Record here in Voice Studio (the hotkey records into the app you are typing in)",
        )
        self.mic.grid(row=0, column=0, rowspan=3, padx=(0, 18))
        self.status_label = label(hero, "Ready to dictate", font=fonts.status, anchor="w")
        self.status_label.grid(row=0, column=1, sticky="sw")
        self.hint_label = label(hero, "", font=fonts.body, fg=MUTED, anchor="w", justify="left")
        self.hint_label.grid(row=1, column=1, sticky="w", pady=(2, 0))
        self.meta_label = label(hero, "", font=fonts.small, fg=SUBTLE, anchor="w")
        self.meta_label.grid(row=2, column=1, sticky="nw", pady=(4, 0))
        hero.columnconfigure(1, weight=1)
        actions = tk.Frame(hero, bg=SURFACE)
        actions.grid(row=0, column=2, rowspan=3, sticky="e")
        self.translate_take_btn = FlatButton(
            actions,
            "Translate a take",
            self._translate_take,
            tooltip="Record once; the Whisper transcript is translated and delivered like a normal take",
        )
        self.translate_take_btn.pack(fill="x")
        self.stop_btn = FlatButton(actions, "Stop", self.stop_recording, variant="ghost")
        self.stop_btn.pack(fill="x", pady=(6, 0))

        mode_row = tk.Frame(self.frame, bg=BG)
        mode_row.pack(fill="x", pady=(14, 0))
        label(mode_row, "Mode", font=fonts.body_bold, fg=MUTED).pack(side="left", padx=(2, 10))
        self.mode = Segmented(
            mode_row,
            [("stable", "Whisper"), ("gemma-agent", "Gemma agent"), ("ask-gemma", "Ask Gemma")],
            command=self._apply_profile,
            font=fonts.body,
        )
        self.mode.pack(side="left")

        translate_card = card(self.frame, padx=16, pady=10)
        translate_card.pack(fill="x", pady=(12, 0))
        self.translation_var = tk.BooleanVar(value=bool(self.config.get("dictation_translation_enabled", False)))
        self.translation_target_var = tk.StringVar(
            value=str(self.config.get("dictation_translation_target_language", "English"))
        )
        self.translation_speak_var = tk.BooleanVar(
            value=bool(self.config.get("dictation_translation_speak_aloud", False))
        )
        Switch(translate_card, "Translate what I dictate into", self.translation_var, self._save_translation, font=fonts.body).pack(
            side="left"
        )
        target = ttk.Combobox(
            translate_card,
            textvariable=self.translation_target_var,
            values=LANGUAGE_TARGETS,
            state="readonly",
            width=9,
        )
        target.pack(side="left", padx=(10, 0))
        target.bind("<<ComboboxSelected>>", lambda _e: self._save_translation())
        Switch(translate_card, "Speak it", self.translation_speak_var, self._save_translation, font=fonts.body).pack(
            side="right"
        )

        transcript_card = card(self.frame, padx=0, pady=0)
        transcript_card.pack(fill="both", expand=True, pady=(12, 0))
        header = tk.Frame(transcript_card, bg=SURFACE, padx=16, pady=10)
        header.pack(fill="x")
        label(header, "Latest transcript", font=fonts.heading).pack(side="left")
        FlatButton(header, "Retry last audio", self._retry_last_audio, variant="ghost", font=fonts.small, padx=8, pady=3,
                   tooltip="Transcribe the newest saved recording again").pack(side="right")
        FlatButton(header, "Copy", self._copy_last_transcript, variant="ghost", font=fonts.small, padx=8, pady=3).pack(
            side="right", padx=(0, 4)
        )
        self.transcript = tk.Text(
            transcript_card,
            wrap="word",
            height=6,
            bg=EDITOR_BG,
            fg=TEXT,
            font=fonts.transcript,
            relief="flat",
            bd=0,
            padx=16,
            pady=12,
            highlightthickness=0,
            selectbackground=SELECTION,
            insertbackground=TEXT,
            spacing2=3,
        )
        self.transcript.pack(fill="both", expand=True)
        self.transcript.configure(state="disabled")
        footer = tk.Frame(transcript_card, bg=SURFACE, padx=16, pady=8)
        footer.pack(fill="x")
        self.route_dot = tk.Canvas(footer, width=10, height=10, bg=SURFACE, highlightthickness=0)
        self.route_dot.pack(side="left", padx=(0, 8))
        self.route_label = label(footer, "Nothing dictated yet.", font=fonts.small, fg=MUTED, anchor="w", justify="left")
        self.route_label.pack(side="left", fill="x", expand=True)
        self.route_label.bind("<Configure>", lambda e: self.route_label.configure(wraplength=max(200, e.width)))
        self._paint_route_dot(SUBTLE)

        self.update_hotkey_hint()
        self.update_mode()

    # -- actions -----------------------------------------------------------------

    def _toggle_from_gui(self) -> None:
        if self.recorder.is_recording():
            self.stop_recording()
            return
        ok, message = self.recorder.toggle_recording(capture_focus=False, started_from_gui=True)
        if not ok:
            self._set_route(message, REC)
            vt.notify("Voice typing", message, urgency="critical")

    def stop_recording(self) -> None:
        self.recorder.speaker.stop()
        ok, message = self.recorder.stop_recording_async(reason="manual_stop")
        if not ok and message != "Not recording":
            self._set_route(message, REC)

    def _translate_take(self) -> None:
        if self.recorder.is_recording():
            self.stop_recording()
            return
        self._save_translation()
        ok, message = self.recorder.start_translation_recording(capture_focus=True, started_from_gui=False)
        if ok:
            self.recorder.restore_original_focus_target(max_age_seconds=10.0)
        else:
            vt.notify("Voice typing", message, urgency="critical")
        self._set_route(message, ACCENT if ok else REC)

    def _retry_last_audio(self) -> None:
        ok, message = self.recorder.retranscribe_path_async()
        self._set_route(message, ACCENT if ok else REC)
        if not ok:
            vt.notify("Voice typing", message, urgency="critical")

    def _copy_last_transcript(self) -> None:
        text = self.transcript.get("1.0", "end").strip()
        if text and vt.copy_to_clipboard(text):
            self._set_route("Transcript copied to the clipboard.", ACCENT)

    def _save_translation(self) -> None:
        self.config.set("dictation_translation_enabled", bool(self.translation_var.get()))
        self.config.set("dictation_translation_target_language", self.translation_target_var.get().strip() or "English")
        self.config.set("dictation_translation_speak_aloud", bool(self.translation_speak_var.get()))

    def _apply_profile(self, profile: str) -> None:
        updates = vt.PROFILE_CONFIGS.get(profile)
        if not updates:
            return
        if self.recorder.is_recording() or self.recorder.is_transcribing():
            vt.notify("Voice typing", "Stop recording before switching mode")
            self.update_mode()
            return
        for key, value in updates.items():
            self.config.set(key, value)
        self.config.set("active_profile", profile)
        if profile in ("gemma-agent", "ask-gemma"):
            # Start loading the model now instead of on first use.
            self.recorder.warm_up_transcriber_async()
        self.update_mode()
        self.app.settings.reload_dictation()
        self._set_route(f"Mode switched to {dict([('stable', 'Whisper'), ('gemma-agent', 'Gemma agent'), ('ask-gemma', 'Ask Gemma')])[profile]}.", ACCENT)

    def update_mode(self) -> None:
        engine = self.config.transcription_engine()
        if bool(self.config.get("assistant_answer_mode", False)):
            self.mode.set("ask-gemma")
        elif engine == "whisper":
            self.mode.set("stable")
        elif engine == "ollama":
            self.mode.set("gemma-agent")
        else:
            self.mode.set(None)

    def update_hotkey_hint(self) -> None:
        hotkey = self.app.hotkeys.get("voice-toggle", "")
        self._idle_hint = (
            f"Press {hotkey} in any text box to dictate there. Or click the mic to record here."
            if hotkey
            else "Use the voice-toggle hotkey in any text box, or click the mic to record here."
        )
        if self._visual == "idle":
            self.hint_label.configure(text=self._idle_hint)

    # -- events ------------------------------------------------------------------

    def on_state(self, recording: bool, reason: str) -> None:
        if recording:
            self._expect_result = True
            self.app.badge.show_recording(self.recorder.started_at() or time.time())
        elif reason in {"limit_or_error", "recording_error"}:
            self.app.badge.show_result("error", "Recording stopped", "Check the microphone", seconds=4.0)
            self._set_route("Recording stopped unexpectedly. Check the microphone.", REC)
        elif reason == "limit_reached":
            self._set_route("Stopped at the recording limit.", WARN)

    def on_transcript(self, text: str, route: str) -> None:
        if text:
            self.transcript.configure(state="normal")
            self.transcript.delete("1.0", "end")
            self.transcript.insert("1.0", text)
            self.transcript.configure(state="disabled")
        if route == "recognized":
            return
        note = str(getattr(self.recorder, "_last_route_note", "") or "")
        message = ROUTE_LABELS.get(route, "Transcript processed.")
        if note:
            message = f"{message} {note}"
        color = {
            "ok": READ,
            "copied": ACCENT,
            "error": SUBTLE,
        }.get(ROUTE_BADGES.get(route, ("copied", ""))[0], ACCENT)
        self._set_route(message, color)
        if self.recorder.is_recording():
            return
        kind, badge_text = ROUTE_BADGES.get(route, ("copied", "Done"))
        detail = ""
        if route in {"clipboard_only", "clipboard"} and note:
            detail = "Focus moved away" if "Focus moved" in note else "Press Ctrl+V to paste"
        elif route == "clipboard_only":
            detail = "Press Ctrl+V to paste"
        self._expect_result = False
        self._last_result_at = time.monotonic()
        self.app.badge.show_result(kind, badge_text, detail)

    def on_quality(self, verdict: str, message: str) -> None:
        self._quality = (verdict, message)

    def _set_route(self, message: str, color: str) -> None:
        self.route_label.configure(text=message)
        self._paint_route_dot(color)

    def _paint_route_dot(self, color: str) -> None:
        self.route_dot.delete("all")
        self.route_dot.create_oval(1, 1, 9, 9, fill=color, outline="")

    def tick(self, level: float, phase: float) -> None:
        recording = self.recorder.is_recording()
        transcribing = self.recorder.is_transcribing()
        quality = getattr(self, "_quality", ("", ""))
        if recording:
            if self._visual != "recording":
                self._visual = "recording"
                self.mic.set_look(icon="stop", fill=REC, fg="#ffffff", hover=REC_HOVER, ring=REC_SOFT)
                self.status_label.configure(text="Listening…", fg=TEXT)
                started_from_gui = bool(getattr(self.recorder, "_started_from_gui", False))
                hotkey = self.app.hotkeys.get("voice-toggle", "the hotkey")
                self.hint_label.configure(
                    text="Click the mic to stop." if started_from_gui else f"Press {hotkey} again to stop and paste where you started."
                )
                self.translate_take_btn.set_enabled(False)
            elapsed = max(0, int(time.time() - self.recorder.started_at()))
            meta = f"{elapsed // 60:02d}:{elapsed % 60:02d}"
            if quality[1]:
                meta += f" · {quality[1]}"
            self.meta_label.configure(text=meta, fg=MUTED)
            self.mic.set_level(level)
        elif transcribing:
            if self._visual != "transcribing":
                self._visual = "transcribing"
                self.mic.set_look(icon="spinner", fill=WARN_SOFT, fg=WARN, hover=WARN_SOFT, ring=None)
                self.status_label.configure(text="Transcribing…")
                self.hint_label.configure(text="The text goes where you started, or to the clipboard.")
                self.app.badge.show_transcribing()
            self.mic.set_level(0, phase)
        else:
            if self._visual != "idle":
                self._visual = "idle"
                self._idle_since = time.monotonic()
                self.mic.set_look(icon="mic", fill=SURFACE_3, fg=TEXT, hover="#353b4c", ring=None)
                self.status_label.configure(text="Ready to dictate")
                self.hint_label.configure(text=self._idle_hint)
                self.translate_take_btn.set_enabled(True)
                if quality[1]:
                    self.meta_label.configure(text=f"Last take · {quality[1]}", fg=SUBTLE)
            if self.app.badge.state in {"transcribing", "recording"} and self._idle_since is not None:
                # The routed result normally arrives right away; without one the
                # take failed (the notification says why).
                if time.monotonic() - self._idle_since > 0.8:
                    self._idle_since = None
                    if self._expect_result:
                        self._expect_result = False
                        self.app.badge.show_result("error", "No transcript", "See the notification", seconds=3.0)
                    else:
                        self.app.badge.hide()


class ReaderPanel:
    """Editable document read aloud passage by passage.

    Text is handed to the reader a few passages ahead of playback; marks in
    the Text widget track each queued passage, so text pasted below keeps
    playing and edits to not-yet-spoken passages are re-read as edited.
    """

    def __init__(self, parent: tk.Misc, app: "StudioApp") -> None:
        self.app = app
        self.reader: vr.ReaderController = app.reader
        self.config: vr.ConfigManager = app.reading_config
        fonts = app.fonts
        self.frame = tk.Frame(parent, bg=BG)
        self._sid: int | None = None
        self._doc: tk.Text | None = None
        self._passages: dict[int, dict[str, Any]] = {}
        self._order: list[int] = []
        self._playing: int | None = None
        self._next_pid = 1
        self._first_passage = True
        self._limit_mark: str | None = None
        self._language: str | None = None
        self._last_edit_at = 0.0
        self._last_view_at = 0.0
        self._cursor_moved = False
        self._loading = False
        self._piper_model_map: dict[str, str] = {}

        toolbar = tk.Frame(self.frame, bg=BG)
        toolbar.pack(fill="x")
        self.play_btn = CircleButton(toolbar, 46, "play", command=self.play_pause, fill=ACCENT, fg="#ffffff",
                                     hover="#959bff", ring_space=2, tooltip="Read / pause (Ctrl+Enter reads from the cursor)")
        self.play_btn.pack(side="left")
        self.stop_btn = CircleButton(toolbar, 38, "stop", command=self.stop, fill=SURFACE_2, fg=MUTED,
                                     hover=SURFACE_3, ring_space=2, tooltip="Stop")
        self.stop_btn.pack(side="left", padx=(6, 12))
        status_box = tk.Frame(toolbar, bg=BG)
        status_box.pack(side="left", fill="x", expand=True)
        self.status_label = label(status_box, "Ready", font=fonts.heading, anchor="w")
        self.status_label.pack(anchor="w")
        self.source_label = label(status_box, "", font=fonts.small, fg=SUBTLE, anchor="w")
        self.source_label.pack(anchor="w")
        FlatButton(toolbar, "Translate", self.toggle_translation, tooltip="Translate the selection, or all the text, with local Gemma").pack(
            side="right"
        )
        self.read_selection_btn = FlatButton(
            toolbar,
            "Read selection",
            self.read_system_selection,
            variant="primary",
            tooltip="Read the text selected in another app (same as the read hotkey); it replaces the text here",
        )
        self.read_selection_btn.pack(side="right", padx=(0, 8))

        quick = tk.Frame(self.frame, bg=BG)
        quick.pack(fill="x", pady=(12, 0))
        label(quick, "Voice", font=fonts.small, fg=MUTED).pack(side="left")
        self.voice_var = tk.StringVar()
        self.voice_combo = ttk.Combobox(quick, textvariable=self.voice_var, width=30)
        self.voice_combo.pack(side="left", padx=(8, 14))
        self.voice_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_voice_selected())
        label(quick, "Language", font=fonts.small, fg=MUTED).pack(side="left")
        configured_language = str(self.config.get("language", "en")).strip().lower()
        self.language_var = tk.StringVar(value=configured_language if configured_language in {"en", "fr", "it", "he"} else "en")
        language_combo = ttk.Combobox(quick, textvariable=self.language_var, values=["en", "he", "fr", "it"], state="readonly", width=4)
        language_combo.pack(side="left", padx=(8, 14))
        language_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_language_changed())
        label(quick, "Speed", font=fonts.small, fg=MUTED).pack(side="left")
        self.rate_var = tk.IntVar(value=int(self.config.get("rate", 0)))
        self.rate_value = label(quick, "", font=fonts.small, fg=MUTED, width=4)
        self.rate_value.pack(side="right")
        rate = ttk.Scale(quick, from_=-100, to=100, variable=self.rate_var, orient="horizontal", command=self._on_rate_moved)
        rate.pack(side="left", fill="x", expand=True, padx=(8, 6))
        rate.bind("<ButtonRelease-1>", lambda _e: self._on_rate_released())
        self._on_rate_moved()

        editor_card = card(self.frame, padx=0, pady=0)
        editor_card.pack(fill="both", expand=True, pady=(12, 0))
        editor_row = tk.Frame(editor_card, bg=EDITOR_BG)
        editor_row.pack(fill="both", expand=True)
        self.editor = self._make_document(editor_row, fonts)
        scroll = ttk.Scrollbar(editor_row, orient="vertical", command=self.editor.yview, style="Slim.Vertical.TScrollbar")
        self.editor.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.editor.pack(side="left", fill="both", expand=True)
        self.placeholder = tk.Label(
            editor_row,
            text="",
            font=fonts.body,
            fg=SUBTLE,
            bg=EDITOR_BG,
            justify="center",
        )
        self.placeholder.bind("<Button-1>", lambda _e: self.editor.focus_set())
        editor_footer = tk.Frame(editor_card, bg=SURFACE, padx=14, pady=7)
        editor_footer.pack(fill="x")
        self.count_label = label(editor_footer, "", font=fonts.small, fg=SUBTLE)
        self.count_label.pack(side="left")
        FlatButton(editor_footer, "Clear", self.clear, variant="ghost", font=fonts.small, padx=8, pady=2).pack(side="right")
        FlatButton(editor_footer, "Copy all", self._copy_all, variant="ghost", font=fonts.small, padx=8, pady=2).pack(
            side="right", padx=(0, 4)
        )
        label(editor_footer, "Paste more text below while it reads; it keeps going.", font=fonts.small, fg=SUBTLE).pack(
            side="right", padx=(0, 12)
        )

        self.translation_card = card(self.frame, padx=0, pady=0)
        header = tk.Frame(self.translation_card, bg=SURFACE, padx=14, pady=8)
        header.pack(fill="x")
        label(header, "Translation", font=fonts.heading).pack(side="left")
        self.translation_target_var = tk.StringVar(value=str(self.config.get("translation_target_language", "English")))
        target = ttk.Combobox(header, textvariable=self.translation_target_var, values=LANGUAGE_TARGETS, state="readonly", width=9)
        target.pack(side="left", padx=(12, 8))
        target.bind("<<ComboboxSelected>>", lambda _e: self._save_translation_options())
        FlatButton(header, "Translate", self.translate, variant="primary", font=fonts.small, padx=10, pady=3).pack(side="left")
        FlatButton(header, "Cancel", self._cancel_translation, variant="ghost", font=fonts.small, padx=8, pady=3).pack(
            side="left", padx=(4, 0)
        )
        FlatButton(header, "Hide", self.toggle_translation, variant="ghost", font=fonts.small, padx=8, pady=3).pack(side="right")
        FlatButton(header, "Copy", self._copy_translation, variant="ghost", font=fonts.small, padx=8, pady=3).pack(
            side="right", padx=(0, 4)
        )
        FlatButton(header, "Read it", self.read_translation, variant="ghost", font=fonts.small, padx=8, pady=3).pack(
            side="right", padx=(0, 4)
        )
        self.translation_speak_var = tk.BooleanVar(value=bool(self.config.get("translation_speak_aloud", False)))
        Switch(header, "Speak", self.translation_speak_var, self._save_translation_options, font=fonts.small).pack(
            side="right", padx=(0, 10)
        )
        self.translation_box = self._make_document(self.translation_card, fonts, height=5)
        self.translation_box.pack(fill="both", expand=True)
        self.translation_status = label(self.translation_card, "Translation uses local Gemma through Ollama.", font=fonts.small, fg=SUBTLE, anchor="w")
        self.translation_status.configure(bg=SURFACE, padx=14, pady=6)
        self.translation_status.pack(fill="x")
        self._translation_visible = False

        self.refresh_voices()
        self._update_placeholder()
        self._update_counts()

    def _make_document(self, parent: tk.Misc, fonts: Fonts, height: int = 10) -> tk.Text:
        doc = tk.Text(
            parent,
            wrap="word",
            undo=True,
            autoseparators=True,
            maxundo=-1,
            height=height,
            bg=EDITOR_BG,
            fg=TEXT,
            font=fonts.editor,
            relief="flat",
            bd=0,
            padx=18,
            pady=14,
            highlightthickness=0,
            selectbackground=SELECTION,
            insertbackground=TEXT,
            insertwidth=2,
            spacing1=2,
            spacing2=4,
            spacing3=6,
        )
        doc.tag_configure("vr_passage", background=PASSAGE_BG)
        doc.tag_configure("vr_sentence", background="#223355")
        doc.tag_configure("vr_word", background=WORD_BG, foreground="#ffffff")
        doc.tag_raise("sel")
        doc.bind("<<Modified>>", lambda _e, d=doc: self._on_modified(d))
        doc.bind("<Control-Return>", lambda _e, d=doc: self._read_from_cursor(d) or "break")
        doc.bind("<Escape>", lambda _e: self.stop())
        doc.bind("<ButtonRelease-1>", lambda _e: self._note_cursor_move())
        doc.bind("<Button-3>", lambda e, d=doc: self._context_menu(e, d))
        for sequence in ("<Button-4>", "<Button-5>", "<MouseWheel>"):
            doc.bind(sequence, lambda _e: self._note_view_activity(), add="+")
        for key in ("Up", "Down", "Left", "Right", "Home", "End", "Prior", "Next"):
            doc.bind(f"<KeyRelease-{key}>", lambda _e: self._note_cursor_move(), add="+")
        return doc

    # -- voice / options -----------------------------------------------------------

    def refresh_voices(self) -> None:
        engine = str(self.config.get("engine", "spd-say")).strip()
        if engine == "piper":
            self._piper_model_map = {}
            labels: list[str] = []
            for model_path in vr.list_piper_model_paths():
                text = vr.format_piper_model_label(model_path)
                unique, suffix = text, 2
                while unique in self._piper_model_map:
                    unique = f"{text} #{suffix}"
                    suffix += 1
                self._piper_model_map[unique] = model_path
                labels.append(unique)
            self.voice_combo.configure(state="readonly", values=labels)
            saved = str(self.config.get("piper_model", "")).strip()
            selected = next((k for k, v in self._piper_model_map.items() if v == saved), "")
            if not selected:
                preferred = vr.choose_piper_model_for_language(list(self._piper_model_map.values()), self.language_var.get())
                selected = next((k for k, v in self._piper_model_map.items() if v == preferred), labels[0] if labels else "")
            self.voice_var.set(selected)
        elif engine == "spd-say":
            module = str(self.config.get("output_module", "")).strip()
            voices = vr.list_rhvoice_voices() if module.lower() == "rhvoice" else []
            if not voices:
                voices = vr.list_spd_synthesis_voices(module)
            current = str(self.config.get("voice", "")).strip()
            values = [v for v in voices if v]
            self.voice_combo.configure(state="normal", values=values)
            self.voice_var.set(current if current else (values[0] if values else ""))
        else:
            self.voice_combo.configure(state="normal", values=[])
            self.voice_var.set(str(self.config.get("voice", "")).strip())

    def _on_voice_selected(self) -> None:
        engine = str(self.config.get("engine", "spd-say")).strip()
        if engine == "piper":
            self.config.set("piper_model", self._piper_model_map.get(self.voice_var.get().strip(), ""))
        else:
            self.config.set("voice", self.voice_var.get().strip())
        self._restart_current_passage()

    def _on_language_changed(self) -> None:
        self.config.set("language", self.language_var.get().strip())
        if str(self.config.get("engine", "")).strip() == "piper":
            selected = vr.choose_piper_model_for_language(vr.list_piper_model_paths(), self.language_var.get().strip())
            if selected:
                self.config.set("piper_model", selected)
        self.refresh_voices()

    def _on_rate_moved(self, _value: Any = None) -> None:
        value = int(float(self.rate_var.get()))
        self.rate_value.configure(text=f"{value:+d}" if value else "0")

    def _on_rate_released(self) -> None:
        value = int(float(self.rate_var.get()))
        self.rate_var.set(value)
        if value != int(self.config.get("rate", 0)):
            self.config.set("rate", value)
            self._restart_current_passage()

    def _restart_current_passage(self) -> None:
        # Voice and speed are fixed per session; pick them up from the passage
        # being read so the change is heard right away.
        if not self.reader.is_speaking() or self._sid is None or self._doc is None:
            return
        pid = self._playing
        info = self._passages.get(pid) if pid is not None else None
        start = self._doc.index(info["start"]) if info else None
        doc, language, limit = self._doc, self._language, self._limit_mark
        limit_index = doc.index(limit) if limit else None
        if start is not None:
            self.start_reading(doc, start, limit_index, language)

    def _save_translation_options(self) -> None:
        self.config.set("translation_target_language", self.translation_target_var.get().strip() or "English")
        self.config.set("translation_speak_aloud", bool(self.translation_speak_var.get()))

    # -- reading -----------------------------------------------------------------

    def play_pause(self) -> None:
        if self.reader.is_speaking():
            self.reader.pause()
            return
        if self.reader.is_paused() and self._sid is not None and self.reader.active_session_id() == self._sid:
            self.reader.resume()
            return
        self._read_from_cursor(self.editor)

    def stop(self) -> None:
        self.reader.stop()

    def read_system_selection(self) -> None:
        ok, message = self.reader.read_selection_async()
        if not ok:
            self.status_label.configure(text=message)
            vr.notify("Voice Reading", message, urgency="critical")

    def read_translation(self) -> None:
        if self.translation_box.get("1.0", "end").strip():
            self.start_reading(self.translation_box, "1.0", None, self.translation_target_var.get().strip() or None)

    def _read_from_cursor(self, doc: tk.Text) -> None:
        end = doc.index("end-1c")
        if not doc.get("1.0", end).strip():
            self.status_label.configure(text="Nothing to read — paste or type some text")
            return
        limit = None
        if doc.tag_ranges("sel"):
            start, limit = doc.index("sel.first"), doc.index("sel.last")
        elif self._cursor_moved and doc.compare("insert", "<", end) and doc is self._doc_for_cursor():
            start = self._sentence_start(doc, doc.index("insert"))
        elif doc is self._doc and "vr_resume" in doc.mark_names() and doc.get("vr_resume", end).strip():
            start = doc.index("vr_resume")
        else:
            start = "1.0"
        language = self._language if doc is self._doc else None
        self.start_reading(doc, start, limit, language)

    def _doc_for_cursor(self) -> tk.Text:
        return self.editor

    def _sentence_start(self, doc: tk.Text, index: str) -> str:
        before = doc.get("1.0", index)
        last = None
        for match in re.finditer(r"[.!?…]+[\"”’')\]]*\s+|\n", before):
            last = match
        return doc.index(f"1.0 + {last.end()} chars") if last else "1.0"

    def start_reading(
        self,
        doc: tk.Text,
        start: str,
        limit: str | None,
        language: str | None,
        session_id: int | None = None,
    ) -> None:
        if session_id is None:
            session_id = self.reader.begin_document_session(language=language)
        self._clear_passages()
        self._sid = session_id
        self._doc = doc
        self._language = language
        self._first_passage = True
        self._cursor_moved = False
        doc.mark_set("vr_cursor", start)
        doc.mark_gravity("vr_cursor", "left")
        doc.mark_set("vr_resume", start)
        doc.mark_gravity("vr_resume", "left")
        self._limit_mark = None
        if limit is not None:
            doc.mark_set("vr_limit", limit)
            doc.mark_gravity("vr_limit", "left")
            self._limit_mark = "vr_limit"
        self._last_view_at = 0.0
        doc.see(start)
        self.feed()

    def _clear_passages(self) -> None:
        for doc in (self.editor, self.translation_box):
            doc.tag_remove("vr_passage", "1.0", "end")
            doc.tag_remove("vr_word", "1.0", "end")
            doc.tag_remove("vr_sentence", "1.0", "end")
        if self._doc is not None:
            for info in self._passages.values():
                for mark in (info["start"], info["end"]):
                    try:
                        self._doc.mark_unset(mark)
                    except tk.TclError:
                        pass
        self._passages.clear()
        self._order.clear()
        self._playing = None

    def feed(self) -> None:
        sid, doc = self._sid, self._doc
        if sid is None or doc is None or self.reader.active_session_id() != sid:
            return
        while self.reader.session_wants_more(sid):
            taken = self._take_passage(doc)
            if taken == "defer":
                self.reader.set_session_exhausted(sid, False)
                return
            if taken is None:
                self.reader.set_session_exhausted(sid, True)
                return
            start, end, raw = taken
            pid = self._next_pid
            self._next_pid += 1
            start_mark, end_mark = f"vrp{pid}s", f"vrp{pid}e"
            doc.mark_set(start_mark, start)
            doc.mark_gravity(start_mark, "right")
            doc.mark_set(end_mark, end)
            doc.mark_gravity(end_mark, "left")
            self._passages[pid] = {"start": start_mark, "end": end_mark, "raw": raw}
            self._order.append(pid)
            text = raw.strip()
            mode = vr.resolve_preprocess_mode(self.config)
            processed, _changed, _mode = vr.preprocess_text_for_mode(text, mode)
            self.reader.enqueue_passage(sid, pid, text, processed or text)

    def _take_passage(self, doc: tk.Text) -> tuple[str, str, str] | str | None:
        cursor = doc.index("vr_cursor")
        limit = doc.index(self._limit_mark) if self._limit_mark else doc.index("end-1c")
        if doc.compare(cursor, ">=", limit):
            return None
        # Enough text for one passage; long documents are not copied each tick.
        window_end = doc.index(f"{cursor} + 2000 chars")
        text = doc.get(cursor, window_end if doc.compare(window_end, "<", limit) else limit)
        stripped = text.lstrip()
        if not stripped.strip():
            return None
        lead = len(text) - len(stripped)
        if self._first_passage:
            cut, complete = vr.next_passage_cut(stripped, vr.READER_FIRST_PASSAGE_CHARS, 1)
        else:
            cut, complete = vr.next_passage_cut(stripped, vr.READER_PASSAGE_CHARS, vr.READER_PASSAGE_MIN_CHARS)
        if cut <= 0:
            return None
        if not complete and self._limit_mark is None and time.monotonic() - self._last_edit_at < 1.2:
            return "defer"  # the last sentence is probably still being typed
        start = doc.index(f"{cursor} + {lead} chars")
        end = doc.index(f"{start} + {cut} chars")
        doc.mark_set("vr_cursor", end)
        self._first_passage = False
        return start, end, stripped[:cut]

    def _on_modified(self, doc: tk.Text) -> None:
        if not doc.edit_modified():
            return
        doc.edit_modified(False)
        if doc is self.editor:
            self._update_placeholder()
            self._update_counts()
        if self._loading:
            return
        self._last_edit_at = time.monotonic()
        self._last_view_at = time.monotonic()
        if doc is self._doc and self._sid is not None:
            doc.after_idle(self._check_edits)

    def _check_edits(self) -> None:
        doc, sid = self._doc, self._sid
        if doc is None or sid is None or self.reader.active_session_id() != sid:
            return
        reached_playing = self._playing is None
        for pid in list(self._order):
            if pid == self._playing:
                reached_playing = True
                continue
            if not reached_playing:
                continue
            info = self._passages[pid]
            if doc.get(info["start"], info["end"]) == info["raw"]:
                continue
            dropped = self.reader.truncate_session(sid, pid)
            if dropped:
                doc.mark_set("vr_cursor", self._passages[dropped[0]]["start"])
                for dropped_id in dropped:
                    dropped_info = self._passages.pop(dropped_id, None)
                    if dropped_id in self._order:
                        self._order.remove(dropped_id)
                    if dropped_info:
                        for mark in (dropped_info["start"], dropped_info["end"]):
                            doc.mark_unset(mark)
                self._first_passage = False
            break
        self.feed()

    def on_passage(self, pid: int, word_index: int, word_total: int) -> None:
        doc = self._doc
        info = self._passages.get(pid)
        if doc is None or info is None:
            return
        if pid != self._playing:
            self._playing = pid
            doc.mark_set("vr_resume", info["start"])
            self._prune_played(pid)
            self.status_label.configure(text="Reading")
        start, end = info["start"], info["end"]
        doc.tag_remove("vr_passage", "1.0", "end")
        doc.tag_remove("vr_word", "1.0", "end")
        doc.tag_remove("vr_sentence", "1.0", "end")
        if bool(self.config.get("highlight_passage_enabled", True)):
            doc.tag_add("vr_passage", start, end)
        raw = info["raw"]
        words = list(re.finditer(r"\S+", raw))
        if words:
            index = max(0, min(word_index, len(words) - 1))
            word = words[index]
            if bool(self.config.get("highlight_sentences_enabled", False)):
                for sentence in re.finditer(r"[^.!?…]+[.!?…]*", raw):
                    if sentence.start() <= word.start() < sentence.end():
                        doc.tag_add("vr_sentence", f"{start} + {sentence.start()} chars", f"{start} + {sentence.end()} chars")
                        break
            if bool(self.config.get("highlight_words_enabled", False)):
                doc.tag_add("vr_word", f"{start} + {word.start()} chars", f"{start} + {word.end()} chars")
        if time.monotonic() - self._last_view_at > 4.0:
            doc.see(end)
            doc.see(start)

    def _prune_played(self, playing: int) -> None:
        doc = self._doc
        if doc is None or playing not in self._order:
            return
        position = self._order.index(playing)
        for pid in self._order[: max(0, position - 1)]:
            info = self._passages.pop(pid, None)
            if info:
                for mark in (info["start"], info["end"]):
                    try:
                        doc.mark_unset(mark)
                    except tk.TclError:
                        pass
        self._order = self._order[max(0, position - 1):]

    def on_state(self, speaking: bool, reason: str) -> None:
        if speaking:
            self.play_btn.set_look(icon="pause")
            self.status_label.configure(text="Reading")
            return
        if reason == "paused":
            self.play_btn.set_look(icon="play")
            self.status_label.configure(text="Paused")
            return
        self.play_btn.set_look(icon="play")
        self.status_label.configure(
            text={"done": "Finished", "stopped": "Stopped", "error": "Could not read — see the notification"}.get(reason, "Ready")
        )
        doc = self._doc
        if doc is not None:
            for tag in ("vr_passage", "vr_word", "vr_sentence"):
                doc.tag_remove(tag, "1.0", "end")
            if reason == "done":
                doc.mark_set("vr_resume", doc.index("vr_cursor") if "vr_cursor" in doc.mark_names() else "end-1c")
        self._playing = None

    def on_document(self, text: str, source: str, language: str | None, session_id: int) -> None:
        if self.reader.active_session_id() != session_id:
            return
        if source == "translation" and text.strip() == self.translation_box.get("1.0", "end").strip():
            self.start_reading(self.translation_box, "1.0", None, language, session_id=session_id)
            return
        self._loading = True
        try:
            self.editor.delete("1.0", "end")
            self.editor.insert("1.0", text)
            self.editor.edit_reset()
            self.editor.mark_set("insert", "1.0")
        finally:
            self._loading = False
        self._update_placeholder()
        self._update_counts()
        self.source_label.configure(text=f"From {source}" if source else "")
        self.start_reading(self.editor, "1.0", None, language, session_id=session_id)

    def clear(self) -> None:
        if self._doc is self.editor:
            self.reader.stop()
        self.editor.delete("1.0", "end")
        self.source_label.configure(text="")

    def _copy_all(self) -> None:
        text = self.editor.get("1.0", "end").strip()
        if text:
            vr.write_clipboard(text)
            self.status_label.configure(text="Copied all text")

    def _note_cursor_move(self) -> None:
        self._cursor_moved = True
        self._last_view_at = time.monotonic()

    def _note_view_activity(self) -> None:
        self._last_view_at = time.monotonic()

    def _context_menu(self, event: Any, doc: tk.Text) -> str:
        index = doc.index(f"@{event.x},{event.y}")
        menu = tk.Menu(doc, tearoff=False)
        menu.add_command(label="Read from here", command=lambda: self.start_reading(doc, self._sentence_start(doc, index), None, self._language if doc is self._doc else None))
        if doc.tag_ranges("sel"):
            menu.add_command(label="Read selection", command=lambda: self.start_reading(doc, doc.index("sel.first"), doc.index("sel.last"), None))
        menu.add_separator()
        menu.add_command(label="Cut", command=lambda: doc.event_generate("<<Cut>>"))
        menu.add_command(label="Copy", command=lambda: doc.event_generate("<<Copy>>"))
        menu.add_command(label="Paste", command=lambda: doc.event_generate("<<Paste>>"))
        menu.add_command(label="Select all", command=lambda: doc.tag_add("sel", "1.0", "end-1c"))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _update_placeholder(self) -> None:
        if self.editor.get("1.0", "end-1c"):
            self.placeholder.place_forget()
            return
        hotkey = self.app.hotkeys.get("voice-read", "the read hotkey")
        self.placeholder.configure(
            text=(
                f"Select text anywhere and press {hotkey}, or paste text here and press ▶.\n\n"
                "Paste more below while it reads and it keeps going.\n"
                "Ctrl+Enter reads from the cursor · right-click for more"
            )
        )
        self.placeholder.place(relx=0.5, rely=0.42, anchor="center")

    def _update_counts(self) -> None:
        text = self.editor.get("1.0", "end-1c")
        words = len(re.findall(r"\S+", text))
        minutes = words / 160.0
        self.count_label.configure(
            text=f"{words} words · about {max(1, round(minutes))} min" if words else "Empty"
        )

    # -- translation -----------------------------------------------------------------

    def toggle_translation(self) -> None:
        if self._translation_visible:
            self.translation_card.pack_forget()
            self._translation_visible = False
        else:
            self._show_translation()

    def _show_translation(self) -> None:
        if not self._translation_visible:
            self.translation_card.pack(fill="both", expand=False, pady=(12, 0))
            self._translation_visible = True

    def translate(self) -> None:
        self._show_translation()
        self._save_translation_options()
        target = self.translation_target_var.get().strip() or "English"
        if self.editor.tag_ranges("sel"):
            text = self.editor.get("sel.first", "sel.last")
        else:
            text = self.editor.get("1.0", "end").strip()
        if text:
            ok, message = self.reader.translate_text_async(text, target, speak_aloud=bool(self.translation_speak_var.get()))
        else:
            ok, message = self.reader.translate_selection_async(target, speak_aloud=bool(self.translation_speak_var.get()))
        self.translation_status.configure(text=message, fg=MUTED if ok else REC)

    def _cancel_translation(self) -> None:
        ok, message = self.reader.cancel_translation()
        self.translation_status.configure(text=message)

    def _copy_translation(self) -> None:
        text = self.translation_box.get("1.0", "end").strip()
        if text and vr.write_clipboard(text):
            self.translation_status.configure(text="Translation copied to the clipboard")

    def on_translation(self, text: str, target: str, meta: str) -> None:
        self._show_translation()
        if text:
            if self._doc is self.translation_box:
                self.reader.stop()
            self.translation_box.delete("1.0", "end")
            self.translation_box.insert("1.0", text)
            self.translation_box.edit_reset()
            self.translation_status.configure(text=f"Translated to {target} ({meta})", fg=MUTED)
        else:
            self.translation_status.configure(text=str(meta), fg=REC if "cancel" not in str(meta).lower() else MUTED)

    def tick(self) -> None:
        if self._sid is not None and self.reader.active_session_id() == self._sid:
            self.feed()


class SettingsPanel:
    def __init__(self, parent: tk.Misc, app: "StudioApp") -> None:
        self.app = app
        self.tc: vt.ConfigManager = app.typing_config
        self.rc: vr.ConfigManager = app.reading_config
        self.fonts = fonts = app.fonts
        self.frame = tk.Frame(parent, bg=BG)
        scroll = ScrollFrame(self.frame)
        scroll.pack(fill="both", expand=True)
        body = scroll.body
        self._body = body

        # Dictation ------------------------------------------------------------------
        section = self._section(body, "Dictation", "How speech becomes text.")
        self.engine_var = tk.StringVar(value=self.tc.transcription_engine())
        engine = ttk.Combobox(section, textvariable=self.engine_var, values=list(vt.SUPPORTED_TRANSCRIPTION_ENGINES), state="readonly")
        engine.bind("<<ComboboxSelected>>", lambda _e: self._on_engine())
        form_row(section, 0, "Engine", engine, fonts)
        model_row = tk.Frame(section, bg=SURFACE)
        self.model_var = tk.StringVar(value=str(self.tc.get("model", "")))
        self.model_combo = ttk.Combobox(model_row, textvariable=self.model_var, state="readonly")
        self.model_combo.pack(side="left", fill="x", expand=True)
        self.model_combo.bind("<<ComboboxSelected>>", lambda _e: self.tc.set("model", self.model_var.get().strip()))
        FlatButton(model_row, "Refresh", self._refresh_whisper_models, font=fonts.small, padx=10, pady=4).pack(side="left", padx=(8, 0))
        FlatButton(model_row, "Help", self._whisper_help, variant="ghost", font=fonts.small, padx=8, pady=4).pack(side="left", padx=(4, 0))
        form_row(section, 1, "Whisper model", model_row, fonts)
        self.speech_language_var = tk.StringVar(value=str(self.tc.get("language", "auto")))
        speech_language = ttk.Combobox(section, textvariable=self.speech_language_var, values=["auto", "en", "he", "fr", "it"], width=10)
        speech_language.bind("<<ComboboxSelected>>", lambda _e: self.tc.set("language", self.speech_language_var.get().strip() or "auto"))
        form_row(section, 2, "Speech language", speech_language, fonts, "auto keeps the spoken language and script; it needs a multilingual Whisper model.")
        gemma_row = tk.Frame(section, bg=SURFACE)
        self.gemma_var = tk.StringVar(value=self.tc.gemma_model_ref())
        ttk.Entry(gemma_row, textvariable=self.gemma_var).pack(side="left", fill="x", expand=True)
        FlatButton(gemma_row, "Apply", self._apply_gemma, font=fonts.small, padx=10, pady=4).pack(side="left", padx=(8, 0))
        form_row(section, 3, "Gemma (Transformers)", gemma_row, fonts)
        switches = tk.Frame(section, bg=SURFACE)
        switches.grid(row=8, column=0, columnspan=2, sticky="we", pady=(12, 0))
        self.dictation_switches: dict[str, tk.BooleanVar] = {}
        for key, title, hint in (
            ("always_copy_hotkey_to_clipboard", "Also keep every hotkey transcript on the clipboard", "A safety net when a paste does not land."),
            ("auto_paste_current_focus", "Paste even when no text box was focused at the start", "Off: such takes only go to the clipboard."),
            ("copy_to_clipboard_when_started_from_gui", "Copy takes recorded with the mic button here", ""),
            ("streaming_transcription", "Transcribe in rolling chunks while recording", ""),
            ("voice_commands_enabled", "Voice commands", "Say \"computer, stop listening\", \"computer, paste last\", …"),
            ("answer_speak_aloud", "Speak Ask Gemma answers aloud", ""),
        ):
            var = tk.BooleanVar(value=bool(self.tc.get(key, False)))
            self.dictation_switches[key] = var
            Switch(switches, title, var, lambda k=key, v=var: self.tc.set(k, bool(v.get())), font=fonts.body, hint=hint, hint_font=fonts.small).pack(
                anchor="w", pady=(0, 8)
            )

        # Reader ----------------------------------------------------------------------
        section = self._section(body, "Reader", "The voice that reads text aloud. Voice, language and speed are on the Reader tab.")
        engines = vr.detect_engines(self.rc) or ["spd-say", "espeak-ng", "espeak"]
        self.reader_engine_var = tk.StringVar(value=str(self.rc.get("engine", engines[0])))
        reader_engine = ttk.Combobox(section, textvariable=self.reader_engine_var, values=engines, state="readonly")
        reader_engine.bind("<<ComboboxSelected>>", lambda _e: self._on_reader_engine())
        form_row(section, 0, "Engine", reader_engine, fonts, "piper sounds best; speech-dispatcher and espeak are fallbacks.")
        self.module_var = tk.StringVar(value=str(self.rc.get("output_module", "")))
        self.module_combo = ttk.Combobox(section, textvariable=self.module_var, state="readonly")
        self.module_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_module())
        form_row(section, 1, "Speech-dispatcher module", self.module_combo, fonts)
        self.voice_type_var = tk.StringVar(value=str(self.rc.get("voice_type", "male1")))
        voice_type = ttk.Combobox(section, textvariable=self.voice_type_var, values=vr.VOICE_TYPES, state="readonly")
        voice_type.bind("<<ComboboxSelected>>", lambda _e: self.rc.set("voice_type", self.voice_type_var.get()))
        form_row(section, 2, "Voice type", voice_type, fonts, "speech-dispatcher only.")
        self.mode_var = tk.StringVar(value=vr.PREPROCESS_MODE_LABELS.get(vr.resolve_preprocess_mode(self.rc), vr.PREPROCESS_MODE_LABELS[vr.PREPROCESS_MODE_OFF]))
        mode = ttk.Combobox(section, textvariable=self.mode_var, values=list(vr.PREPROCESS_MODE_LABELS.values()), state="readonly")
        mode.bind("<<ComboboxSelected>>", lambda _e: self._on_reading_mode())
        form_row(section, 3, "Reading mode", mode, fonts, "How code, math and markdown are spoken.")
        self.pitch_var = tk.IntVar(value=int(self.rc.get("pitch", 0)))
        pitch = ttk.Scale(section, from_=-100, to=100, variable=self.pitch_var, orient="horizontal")
        pitch.bind("<ButtonRelease-1>", lambda _e: self.rc.set("pitch", int(float(self.pitch_var.get()))))
        form_row(section, 4, "Pitch", pitch, fonts, "speech-dispatcher and espeak.")
        switches = tk.Frame(section, bg=SURFACE)
        switches.grid(row=10, column=0, columnspan=2, sticky="we", pady=(12, 0))
        for key, title, default, hint in (
            ("highlight_passage_enabled", "Highlight the passage being read", True, ""),
            ("highlight_sentences_enabled", "Highlight the sentence being read", False, ""),
            ("highlight_words_enabled", "Highlight the word being read", False,
             "Exact with the GPU Piper voice (timed from the voice itself); estimated with other engines."),
            ("prefer_primary_selection", "Read the mouse selection (primary) first", True, ""),
            ("capture_selected_text_via_copy", "Fall back to copying the selection with Ctrl+C", True, ""),
        ):
            var = tk.BooleanVar(value=bool(self.rc.get(key, default)))
            Switch(switches, title, var, lambda k=key, v=var: self.rc.set(k, bool(v.get())), font=fonts.body, hint=hint, hint_font=fonts.small).pack(
                anchor="w", pady=(0, 8)
            )
        buttons = tk.Frame(section, bg=SURFACE)
        buttons.grid(row=11, column=0, columnspan=2, sticky="w", pady=(4, 0))
        FlatButton(buttons, "Get more voices…", self._open_voice_downloader, font=fonts.small, padx=10, pady=4).pack(side="left")
        FlatButton(buttons, "Refresh voices", self._refresh_reader_voices, variant="ghost", font=fonts.small, padx=10, pady=4).pack(side="left", padx=(6, 0))

        # Local AI ------------------------------------------------------------------------
        section = self._section(body, "Local AI (Ollama)", "Gemma through Ollama: Gemma dictation modes, Ask Gemma and translations.")
        self.dictation_ai_var = tk.StringVar(value=self.tc.ollama_model())
        self.dictation_ai_combo = ttk.Combobox(section, textvariable=self.dictation_ai_var)
        self.dictation_ai_combo.bind("<<ComboboxSelected>>", lambda _e: self._save_ai())
        self.dictation_ai_combo.bind("<FocusOut>", lambda _e: self._save_ai())
        form_row(section, 0, "Dictation model", self.dictation_ai_combo, fonts, "Gemma agent needs an audio-capable model (gemma4 e2b/e4b).")
        self.reader_ai_var = tk.StringVar(value=str(self.rc.get("ollama_model", "gemma4:latest")))
        self.reader_ai_combo = ttk.Combobox(section, textvariable=self.reader_ai_var)
        self.reader_ai_combo.bind("<<ComboboxSelected>>", lambda _e: self._save_ai())
        self.reader_ai_combo.bind("<FocusOut>", lambda _e: self._save_ai())
        form_row(section, 1, "Translation model (Reader)", self.reader_ai_combo, fonts)
        context = str(self.tc.get("ollama_context_tokens", 2048))
        self.context_var = tk.StringVar(value=context if context in {"1024", "2048", "4096", "8192", "16384"} else "2048")
        context_combo = ttk.Combobox(section, textvariable=self.context_var, values=["1024", "2048", "4096", "8192", "16384"], state="readonly", width=8)
        context_combo.bind("<<ComboboxSelected>>", lambda _e: self._save_ai())
        form_row(section, 2, "Context tokens", context_combo, fonts, "Used by both. Lower uses less VRAM; 2048 suits an 8 GB GPU.")
        self.ollama_url_var = tk.StringVar(value=self.tc.ollama_url())
        url = ttk.Entry(section, textvariable=self.ollama_url_var)
        url.bind("<FocusOut>", lambda _e: self._save_ai())
        url.bind("<Return>", lambda _e: self._save_ai())
        form_row(section, 3, "Ollama address", url, fonts, "Used by both.")
        FlatButton(section, "Refresh model list", self.refresh_ollama_models, variant="ghost", font=fonts.small, padx=10, pady=4).grid(
            row=9, column=1, sticky="w", pady=(10, 0)
        )

        # Audio ------------------------------------------------------------------------
        section = self._section(body, "Audio devices")
        self.mic_var = tk.StringVar()
        self._mic_map: dict[str, str] = {}
        self.mic_combo = ttk.Combobox(section, textvariable=self.mic_var, state="readonly", postcommand=self._refresh_mics)
        self.mic_combo.bind("<<ComboboxSelected>>", lambda _e: self.tc.set("audio_input_device", self._mic_map.get(self.mic_var.get(), "")))
        form_row(section, 0, "Microphone", self.mic_combo, fonts, "For dictation.")
        self.speaker_var = tk.StringVar()
        self._speaker_map: dict[str, str] = {}
        self.speaker_combo = ttk.Combobox(section, textvariable=self.speaker_var, state="readonly", postcommand=self._refresh_speakers)
        self.speaker_combo.bind("<<ComboboxSelected>>", lambda _e: self.rc.set("audio_output_device", self._speaker_map.get(self.speaker_var.get(), "")))
        form_row(section, 1, "Speaker", self.speaker_combo, fonts, "For reading aloud.")
        self._refresh_mics()
        self._refresh_speakers()

        # Performance ----------------------------------------------------------------------
        section = self._section(body, "Performance")
        self.whisper_gpu_var = tk.BooleanVar(value=not bool(self.tc.get("no_gpu", True)))
        Switch(section, "Use the GPU for Whisper", self.whisper_gpu_var, lambda: self.tc.set("no_gpu", not bool(self.whisper_gpu_var.get())),
               font=fonts.body).grid(row=0, column=0, columnspan=2, sticky="w")
        self.piper_gpu_var = tk.BooleanVar(value=bool(self.rc.get("piper_gpu_enabled", False)))
        Switch(section, "Use the GPU for Piper voices", self.piper_gpu_var, lambda: self.rc.set("piper_gpu_enabled", bool(self.piper_gpu_var.get())),
               font=fonts.body, hint="Needs the CUDA runtime from scripts/setup_piper_gpu_runtime.sh.", hint_font=fonts.small).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(10, 0)
        )
        self.piper_path_var = tk.StringVar(value=str(self.rc.get("piper_gpu_binary_path", "")))
        piper_path = ttk.Entry(section, textvariable=self.piper_path_var)
        piper_path.bind("<FocusOut>", lambda _e: self.rc.set("piper_gpu_binary_path", self.piper_path_var.get().strip()))
        form_row(section, 2, "GPU Piper command", piper_path, fonts)
        keep_choices = {"Release right away": 0, "1 minute": 60, "5 minutes": 300, "15 minutes": 900, "1 hour": 3600}
        current = int(float(self.rc.get("piper_keep_warm_seconds", vr.PIPER_KEEP_WARM_SECONDS_DEFAULT)))
        self.keep_var = tk.StringVar(value=next((k for k, v in keep_choices.items() if v == current), "5 minutes"))
        keep = ttk.Combobox(section, textvariable=self.keep_var, values=list(keep_choices), state="readonly", width=16)
        keep.bind("<<ComboboxSelected>>", lambda _e: self.rc.set("piper_keep_warm_seconds", keep_choices[self.keep_var.get()]))
        form_row(section, 3, "Keep the voice loaded", keep, fonts, "A loaded voice starts reading instantly but holds GPU memory.")

        # Window & indicator ----------------------------------------------------------------
        section = self._section(body, "Window and recording badge")
        rows = [
            (self.tc, "indicator_enabled", True, "Show a recording badge on screen", "A small floating mic while recording; click it to stop. It never takes keyboard focus."),
            (self.tc, "indicator_show_result", True, "Show where the text went", "The badge briefly says Pasted or Copied after each take."),
            (self.tc, "show_panel_on_hotkey_start", True, "Open this window when dictation starts", ""),
            (self.rc, "show_panel_on_hotkey", True, "Open this window when the read hotkey is used", ""),
            (self.tc, "window_always_on_top", False, "Keep this window above others", ""),
        ]
        for index, (cfg, key, default, title, hint) in enumerate(rows):
            var = tk.BooleanVar(value=bool(cfg.get(key, default)))
            Switch(section, title, var, lambda c=cfg, k=key, v=var: self._set_window_option(c, k, bool(v.get())),
                   font=fonts.body, hint=hint, hint_font=fonts.small).grid(row=index, column=0, columnspan=2, sticky="w", pady=(0, 8))
        self.position_segment = Segmented(
            section,
            [("bottom", "Bottom"), ("top", "Top")],
            command=lambda key: self.tc.set("indicator_position", key),
            value=str(self.tc.get("indicator_position", "bottom")),
            font=fonts.small,
            padx=10,
            pady=3,
        )
        form_row(section, 5, "Badge position", self.position_segment, fonts)
        self.position_segment.grid_configure(sticky="w")
        self.opacity_var = tk.DoubleVar(value=float(self.tc.get("window_opacity", 0.95)))
        opacity = ttk.Scale(section, from_=0.6, to=1.0, variable=self.opacity_var, orient="horizontal")
        opacity.bind("<ButtonRelease-1>", lambda _e: self._set_window_option(self.tc, "window_opacity", round(float(self.opacity_var.get()), 2)))
        form_row(section, 6, "Window opacity", opacity, fonts)

        # Hotkeys & files ----------------------------------------------------------------
        section = self._section(body, "Hotkeys", "Set in GNOME Settings → Keyboard → Custom Shortcuts.")
        self.hotkey_labels: dict[str, tk.Label] = {}
        for index, (script, title) in enumerate(
            (
                ("voice-toggle", "Start / stop dictation"),
                ("voice-read", "Read the selected text / stop"),
                ("voice-translate", "Translate the selected text"),
                ("voice-paste", "Type the last transcript again"),
            )
        ):
            value = tk.Label(section, text="—", font=fonts.body_bold, fg=TEXT, bg=SURFACE, anchor="w")
            form_row(section, index, title, value, fonts)
            self.hotkey_labels[script] = value

        section = self._section(body, "About")
        info = (
            f"Dictation settings: {vt.CONFIG_PATH}\n"
            f"Reader settings: {vr.CONFIG_PATH}\n"
            f"Logs: {vt.ROUTE_LOG_PATH} · {vr.LOG_PATH}"
        )
        tk.Label(section, text=info, font=fonts.small, fg=SUBTLE, bg=SURFACE, justify="left", anchor="w").grid(row=0, column=0, columnspan=2, sticky="w")
        buttons = tk.Frame(section, bg=SURFACE)
        buttons.grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 0))
        FlatButton(buttons, "Open routing log", lambda: self._open_path(vt.ROUTE_LOG_PATH), variant="ghost", font=fonts.small, padx=10, pady=4).pack(side="left")
        FlatButton(buttons, "Quit Voice Studio", app.quit, variant="danger", font=fonts.small, padx=10, pady=4,
                   tooltip="Hotkeys start it again when needed").pack(side="left", padx=(8, 0))
        tk.Frame(body, bg=BG, height=12).pack()

        self._refresh_whisper_models()
        self._refresh_modules()
        self.refresh_ollama_models()

    def _section(self, body: tk.Misc, title: str, subtitle: str = "") -> tk.Frame:
        outer = tk.Frame(body, bg=BG)
        outer.pack(fill="x", padx=(0, 6), pady=(0, 14))
        label(outer, title, font=self.fonts.title).pack(anchor="w", padx=2)
        if subtitle:
            label(outer, subtitle, font=self.fonts.small, fg=SUBTLE).pack(anchor="w", padx=2, pady=(2, 0))
        inner = card(outer, padx=18, pady=14)
        inner.pack(fill="x", pady=(8, 0))
        return inner

    def update_hotkeys(self) -> None:
        for script, widget in self.hotkey_labels.items():
            widget.configure(text=self.app.hotkeys.get(script, "not set"))

    def reload_dictation(self) -> None:
        self.engine_var.set(self.tc.transcription_engine())
        for key, var in self.dictation_switches.items():
            var.set(bool(self.tc.get(key, False)))

    def _set_window_option(self, cfg: Any, key: str, value: Any) -> None:
        cfg.set(key, value)
        self.app.apply_presentation()
        if key == "indicator_enabled" and not value:
            self.app.badge.hide()

    def _on_engine(self) -> None:
        engine = vt.ENGINE_ALIASES.get(self.engine_var.get().strip().lower(), self.engine_var.get().strip().lower())
        if engine not in vt.SUPPORTED_TRANSCRIPTION_ENGINES:
            return
        self.tc.set("transcription_engine", engine)
        self.app.dictation.update_mode()
        if engine == "ollama":
            self.app.recorder.warm_up_transcriber_async()

    def _refresh_whisper_models(self) -> None:
        models = self.tc.list_models()
        self.model_combo.configure(values=models)
        current = str(self.tc.get("model", "")).strip()
        if current in models:
            self.model_var.set(current)
        elif models:
            self.model_var.set(models[0])
            self.tc.set("model", models[0])

    def _whisper_help(self) -> None:
        whisper_dir = self.tc.whisper_cli_path().parent.parent.parent
        messagebox.showinfo(
            "Whisper models",
            f"Download models with:\n\ncd {whisper_dir}\n./models/download-ggml-model.sh small\n\n"
            f"Models folder: {self.tc.models_dir()}\nThen press Refresh.\n\n"
            "Gemma agent mode uses Ollama (Local AI section) instead of Whisper.",
        )

    def _apply_gemma(self) -> None:
        ref = self.gemma_var.get().strip()
        if not ref:
            return
        if ref.startswith(("/", "~")):
            self.tc.set("gemma_model_path", ref)
        else:
            self.tc.set("gemma_model", ref)
            self.tc.set("gemma_model_path", "")

    def _on_reader_engine(self) -> None:
        self.rc.set("engine", self.reader_engine_var.get().strip())
        self._refresh_modules()
        self.app.reader_panel.refresh_voices()

    def _refresh_modules(self) -> None:
        if self.reader_engine_var.get().strip() != "spd-say":
            self.module_combo.configure(state="disabled", values=[])
            return
        modules = vr.list_spd_output_modules() or [""]
        self.module_combo.configure(state="readonly", values=modules)
        current = str(self.rc.get("output_module", "")).strip()
        self.module_var.set(current if current in modules else ("rhvoice" if "rhvoice" in modules else modules[0]))

    def _on_module(self) -> None:
        self.rc.set("output_module", self.module_var.get().strip())
        self.app.reader_panel.refresh_voices()

    def _on_reading_mode(self) -> None:
        key = vr.PREPROCESS_MODE_LABEL_TO_KEY.get(self.mode_var.get().strip(), vr.PREPROCESS_MODE_OFF)
        self.rc.set("reading_preprocess_mode", key)
        self.rc.set("code_math_reading_mode", key == vr.PREPROCESS_MODE_CODE_MATH)

    def _refresh_reader_voices(self) -> None:
        self._refresh_modules()
        self.app.reader_panel.refresh_voices()

    def refresh_ollama_models(self) -> None:
        url = self.ollama_url_var.get().strip() or self.tc.ollama_url()

        def _fetch() -> None:
            names = vt.list_ollama_models(url)
            self.app.events.put(("ollama_models", names, None))

        threading.Thread(target=_fetch, daemon=True).start()

    def on_ollama_models(self, names: list[str]) -> None:
        for combo, var in ((self.dictation_ai_combo, self.dictation_ai_var), (self.reader_ai_combo, self.reader_ai_var)):
            values = list(dict.fromkeys(([var.get()] if var.get() else []) + list(names)))
            combo.configure(values=values)

    def _save_ai(self) -> None:
        dictation_model = self.dictation_ai_var.get().strip()
        reader_model = self.reader_ai_var.get().strip()
        if dictation_model:
            self.tc.set("ollama_model", dictation_model)
        if reader_model:
            self.rc.set("ollama_model", reader_model)
        try:
            tokens = max(512, min(262144, int(self.context_var.get())))
        except ValueError:
            tokens = 2048
        url = self.ollama_url_var.get().strip()
        for cfg in (self.tc, self.rc):
            cfg.set("ollama_context_tokens", tokens)
            if url:
                cfg.set("ollama_url", url)

    def _refresh_mics(self) -> None:
        saved = str(self.tc.get("audio_input_device", "")).strip()
        self._mic_map = {"System default": ""}
        name = ""
        for line in vt._command_lines(["pactl", "list", "sources"]):
            line = line.strip()
            if line.startswith("Name:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("Description:") and name:
                if not name.endswith(".monitor"):
                    description = line.split(":", 1)[1].strip()
                    label_text, suffix = description, 2
                    while label_text in self._mic_map:
                        label_text = f"{description} #{suffix}"
                        suffix += 1
                    self._mic_map[label_text] = f"pulse:{name}"
                name = ""
        if saved and saved not in self._mic_map.values():
            self._mic_map[f"{saved} (unavailable)"] = saved
        self.mic_combo.configure(values=list(self._mic_map))
        self.mic_var.set(next((k for k, v in self._mic_map.items() if v == saved), "System default"))

    def _refresh_speakers(self) -> None:
        saved = str(self.rc.get("audio_output_device", "")).strip()
        self._speaker_map = {}
        for name, description in vr.list_output_devices():
            label_text, suffix = description, 2
            while label_text in self._speaker_map:
                label_text = f"{description} #{suffix}"
                suffix += 1
            self._speaker_map[label_text] = name
        if saved and saved not in self._speaker_map.values():
            self._speaker_map[f"{saved} (unavailable)"] = saved
        self.speaker_combo.configure(values=list(self._speaker_map))
        self.speaker_var.set(next((k for k, v in self._speaker_map.items() if v == saved), "System default"))

    def _open_path(self, path: Path) -> None:
        if shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

    def _open_voice_downloader(self) -> None:
        VoiceDownloadDialog(self.app)


class VoiceDownloadDialog:
    def __init__(self, app: "StudioApp") -> None:
        self.app = app
        catalog = vr.list_voice_catalog()
        if not catalog:
            messagebox.showerror(APP_TITLE, "Bundled voice index not found (piper_voices/index/voices.json).")
            return
        self.catalog = catalog
        self.events: "queue.Queue[tuple[str, str]]" = queue.Queue()
        fonts = app.fonts
        win = tk.Toplevel(app.root)
        win.title("Piper voices")
        win.configure(bg=BG, padx=16, pady=14)
        win.transient(app.root)
        self.win = win
        label(win, "Download Piper voices", font=fonts.title).pack(anchor="w")
        label(win, "Voices download from Hugging Face into piper_voices/. The Hebrew Phonikud voice also downloads its phonemizer.",
              font=fonts.small, fg=SUBTLE).pack(anchor="w", pady=(2, 10))
        frame = card(win, padx=0, pady=0)
        frame.pack(fill="both", expand=True)
        self.listbox = tk.Listbox(
            frame,
            height=min(18, len(catalog)),
            width=100,
            bg=EDITOR_BG,
            fg=TEXT,
            selectbackground=ACCENT_SOFT,
            selectforeground=TEXT,
            font=fonts.mono,
            activestyle="none",
            highlightthickness=0,
            bd=0,
        )
        for item in catalog:
            mark = "  ✓ installed" if item["installed"] else ""
            if item["is_default"]:
                mark += "  ★ recommended"
            language = f"{item['language_name']} [{item['language']}]"
            self.listbox.insert("end", f"  {item['key']:<34} {language:<28} {item['quality']:<7} {item['size_mb']:>4} MB{mark}")
        self.listbox.pack(fill="both", expand=True, padx=2, pady=2)
        self.progress = label(win, "", font=fonts.small, fg=MUTED)
        self.progress.pack(anchor="w", pady=(8, 6))
        row = tk.Frame(win, bg=BG)
        row.pack(fill="x")
        self.download_btn = FlatButton(row, "Download selected", self._download, variant="primary")
        self.download_btn.pack(side="left")
        FlatButton(row, "Close", win.destroy, variant="ghost").pack(side="right")

    def _download(self) -> None:
        selection = self.listbox.curselection()
        if not selection:
            self.progress.configure(text="Select a voice first.")
            return
        entry = self.catalog[selection[0]]
        if entry["installed"]:
            self.progress.configure(text=f"{entry['key']} is already installed.")
            return
        self.download_btn.set_enabled(False)
        self.progress.configure(text=f"Downloading {entry['key']}…")

        def _work() -> None:
            ok, message = vr.download_piper_voice(entry["key"], progress=lambda msg: self.events.put(("progress", msg)))
            self.events.put(("done" if ok else "error", message))

        threading.Thread(target=_work, daemon=True).start()
        self._poll()

    def _poll(self) -> None:
        try:
            while True:
                kind, message = self.events.get_nowait()
                self.progress.configure(text=message)
                if kind in {"done", "error"}:
                    self.download_btn.set_enabled(True)
                    if kind == "done":
                        self.app.reader_panel.refresh_voices()
                    return
        except queue.Empty:
            pass
        if self.win.winfo_exists():
            self.win.after(200, self._poll)


class StudioApp:
    def __init__(
        self,
        dictate_on_launch: bool = False,
        read_on_launch: bool = False,
        translate_on_launch: bool = False,
        focus_hint: dict[str, Any] | None = None,
        start_tab: str | None = None,
    ) -> None:
        self.root = tk.Tk(className="VoiceStudio")
        self.root.withdraw()
        self.root.title(APP_TITLE)
        self.typing_config = vt.ConfigManager(vt.CONFIG_PATH)
        self.reading_config = vr.ConfigManager(vr.CONFIG_PATH)
        geometry = str(self.typing_config.get("studio_geometry", "")).strip()
        self.root.geometry(geometry if re.fullmatch(r"\d+x\d+[+-]\d+[+-]\d+", geometry) else "880x680")
        self.root.minsize(620, 480)
        if not bool(self.typing_config.get("studio_normal_window", False)):
            # A utility window is not focused when it appears, so showing it on
            # a hotkey never takes the keyboard from the text box being dictated into.
            try:
                self.root.attributes("-type", "utility")
            except tk.TclError:
                pass
        self.root.wm_focusmodel("passive")
        self.fonts = Fonts(self.root)
        apply_theme(self.root, self.fonts)
        self.events: "queue.Queue[tuple[str, Any, Any]]" = queue.Queue()
        self.hotkeys: dict[str, str] = {}
        self.level_probe = AudioLevelProbe()
        self._phase = 0.0
        self._level = 0.0
        self._level_at = 0.0
        self._title = ""

        self.recorder = vt.RecorderController(
            config=self.typing_config,
            on_state_change=lambda recording, reason: self.events.put(("typing_state", recording, reason)),
            on_transcript=lambda text, route: self.events.put(("typing_transcript", text, route)),
            on_audio_quality=lambda verdict, message: self.events.put(("typing_quality", verdict, message)),
        )
        self.reader = vr.ReaderController(
            config=self.reading_config,
            on_state_change=lambda speaking, reason: self.events.put(("reader_state", speaking, reason)),
            on_translation=lambda text, target, meta: self.events.put(("reader_translation", text, (target, meta))),
            on_passage=lambda pid, word, total: self.events.put(("reader_passage", pid, (word, total))),
            on_need_text=lambda: self.events.put(("reader_need", None, None)),
        )
        self.reader.document_host = lambda text, source, language, sid: self.events.put(
            ("reader_document", (text, source, language), sid)
        )
        self.typing_ipc = vt.IpcServer(
            self.recorder,
            self.typing_config,
            on_show=lambda active: self.events.put(("typing_show", None, active)),
        )
        self.reading_ipc = vr.IpcServer(
            controller=self.reader,
            config=self.reading_config,
            on_show=lambda active: self.events.put(("reader_show", None, active)),
        )

        self._build()
        self.badge = RecordingBadge(self)
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.apply_presentation()
        self.typing_ipc.start()
        self.reading_ipc.start()
        threading.Thread(target=self._load_hotkeys, daemon=True).start()
        self._tick()

        tab = start_tab or str(self.typing_config.get("studio_last_tab", "dictation"))
        if dictate_on_launch:
            self.recorder.start_recording_with_target(
                capture_focus=True,
                started_from_gui=False,
                focus_target=None,
                focus_target_at_start_hint=vt.focus_target_from_hint(focus_hint),
            )
            if bool(self.typing_config.get("show_panel_on_hotkey_start", True)):
                self.show(active=False, tab="dictation")
        elif read_on_launch:
            self.select_tab("reader")
            self.reader.read_selection_async()
            if bool(self.reading_config.get("show_panel_on_hotkey", True)):
                self.show(active=False, tab="reader")
        elif translate_on_launch:
            target = str(self.reading_config.get("translation_target_language", "English")).strip() or "English"
            self.reader.translate_selection_async(target, speak_aloud=False)
            self.show(active=False, tab="reader")
        else:
            self.show(active=True, tab=tab if tab in dict(TABS) else "dictation")

    # -- layout ---------------------------------------------------------------------

    def _build(self) -> None:
        fonts = self.fonts
        header = tk.Frame(self.root, bg=BG, padx=20, pady=14)
        header.pack(fill="x")
        logo = tk.Canvas(header, width=30, height=30, bg=BG, highlightthickness=0)
        logo.pack(side="left")
        logo.create_oval(1, 1, 29, 29, fill=ACCENT_SOFT, outline="")
        for x, height in ((9, 8), (13, 14), (17, 18), (21, 10)):
            logo.create_line(x, 15 - height / 2, x, 15 + height / 2, fill=ACCENT, width=2.4, capstyle="round")
        label(header, APP_TITLE, font=fonts.title).pack(side="left", padx=(10, 22))
        self.tabs = Segmented(header, TABS, command=self.select_tab, font=fonts.body_bold, padx=16, pady=6)
        self.tabs.pack(side="left")
        self.chip = tk.Label(header, text="", font=fonts.small_bold, padx=12, pady=5, bd=0)
        self.chip.pack(side="right")
        tk.Frame(self.root, bg=BORDER, height=1).pack(fill="x")

        self.pages = tk.Frame(self.root, bg=BG, padx=20, pady=16)
        self.pages.pack(fill="both", expand=True)
        self.dictation = DictationPanel(self.pages, self)
        self.reader_panel = ReaderPanel(self.pages, self)
        self.settings = SettingsPanel(self.pages, self)
        self._pages = {"dictation": self.dictation.frame, "reader": self.reader_panel.frame, "settings": self.settings.frame}
        self._current_tab = ""
        last = str(self.typing_config.get("studio_last_tab", "dictation"))
        self.select_tab(last if last in self._pages else "dictation", remember=False)

    def select_tab(self, tab: str, remember: bool = True) -> None:
        if tab not in self._pages or tab == self._current_tab:
            self.tabs.set(tab if tab in self._pages else self._current_tab)
            return
        for frame in self._pages.values():
            frame.pack_forget()
        self._pages[tab].pack(fill="both", expand=True)
        self._current_tab = tab
        self.tabs.set(tab)
        if remember and tab in {"dictation", "reader"}:
            self.typing_config.set("studio_last_tab", tab)

    def apply_presentation(self) -> None:
        try:
            self.root.attributes("-topmost", bool(self.typing_config.get("window_always_on_top", False)))
        except tk.TclError:
            pass
        try:
            opacity = max(0.35, min(1.0, float(self.typing_config.get("window_opacity", 0.95))))
            self.root.attributes("-alpha", opacity)
        except (tk.TclError, ValueError):
            pass

    # -- window ------------------------------------------------------------------------

    def _visible(self) -> bool:
        try:
            return self.root.state() == "normal" and bool(self.root.winfo_viewable())
        except tk.TclError:
            return False

    def show(self, active: bool, tab: str | None = None) -> None:
        was_visible = self._visible()
        if tab and (active or not was_visible or tab == "reader"):
            self.select_tab(tab)
        if was_visible and not active:
            # Re-showing a visible window can steal focus in some window managers.
            self.apply_presentation()
            if tab == "dictation":
                self._schedule_focus_restore()
            return
        self.root.deiconify()
        self.apply_presentation()
        if active:
            self.root.lift()
        elif tab == "dictation":
            target = self.recorder._focus_target or self.recorder._focus_target_at_start
            if target is not None and target.source_pid not in (None, os.getpid()):
                vt.activate_window_by_pid(target.source_pid)
            self._schedule_focus_restore()

    def _schedule_focus_restore(self) -> None:
        for delay in (80, 220, 500, 900):
            self.root.after(delay, lambda: self.recorder.restore_last_external_focus(max_age_seconds=1.0))

    def hide(self) -> None:
        self._save_geometry()
        self.root.withdraw()

    def _save_geometry(self) -> None:
        try:
            if self._visible():
                self.typing_config.set("studio_geometry", self.root.geometry())
        except Exception:
            pass

    def quit(self) -> None:
        self._save_geometry()
        if self.recorder.is_recording():
            self.recorder.stop_recording(reason="gui_closed")
        self.reader.cancel_translation()
        self.reader.shutdown()
        self.typing_ipc.stop()
        self.reading_ipc.stop()
        self.root.destroy()

    def _load_hotkeys(self) -> None:
        self.events.put(("hotkeys", read_custom_hotkeys(), None))

    # -- event pump -------------------------------------------------------------------------

    def _tick(self) -> None:
        try:
            self._tick_once()
        except Exception as exc:
            if not getattr(self, "_tick_error_logged", False):
                self._tick_error_logged = True
                vr.log_line(f"studio: tick failed: {exc!r}")
        finally:
            self.root.after(TICK_MS, self._tick)

    def _tick_once(self) -> None:
        self._drain_events()
        self._phase = (self._phase + 0.06) % 1.0
        now = time.monotonic()
        if self.recorder.is_recording():
            if now - self._level_at > 0.07:
                self._level_at = now
                chunked = getattr(self.recorder, "_recording_mode", "single") == "chunked"
                target = self.level_probe.level(chunked)
                self._level = target if target > self._level else self._level * 0.7 + target * 0.3
        else:
            self._level = 0.0
        self.dictation.tick(self._level, self._phase)
        self.badge.tick(self._level, self._phase)
        self.reader_panel.tick()
        self._update_chip()

    def _drain_events(self) -> None:
        for _ in range(200):
            try:
                event, data, meta = self.events.get_nowait()
            except queue.Empty:
                return
            try:
                self._dispatch(event, data, meta)
            except Exception as exc:  # keep the pump alive
                vr.log_line(f"studio: event {event} failed: {exc!r}")

    def _dispatch(self, event: str, data: Any, meta: Any) -> None:
        if event == "typing_state":
            self.dictation.on_state(bool(data), str(meta))
            self.apply_presentation()
        elif event == "typing_transcript":
            self.dictation.on_transcript(str(data), str(meta))
        elif event == "typing_quality":
            self.dictation.on_quality(str(data), str(meta))
        elif event == "typing_show":
            self.show(active=bool(meta), tab="dictation")
        elif event == "reader_state":
            self.reader_panel.on_state(bool(data), str(meta))
        elif event == "reader_document":
            text, source, language = data
            self.reader_panel.on_document(text, source, language, int(meta))
        elif event == "reader_passage":
            word, total = meta
            self.reader_panel.on_passage(int(data), int(word), int(total))
        elif event == "reader_need":
            self.reader_panel.feed()
        elif event == "reader_translation":
            target, info = meta
            self.reader_panel.on_translation(str(data), str(target), str(info))
        elif event == "reader_show":
            self.show(active=bool(meta), tab="reader")
        elif event == "ollama_models":
            self.settings.on_ollama_models(list(data or []))
        elif event == "hotkeys":
            self.hotkeys = dict(data or {})
            self.dictation.update_hotkey_hint()
            self.reader_panel._update_placeholder()
            self.settings.update_hotkeys()

    def _update_chip(self) -> None:
        if self.recorder.is_recording():
            elapsed = max(0, int(time.time() - self.recorder.started_at()))
            text, fg, bg = f"●  Recording {elapsed // 60:02d}:{elapsed % 60:02d}", "#ffd9d8", REC_SOFT
            title = f"● Recording — {APP_TITLE}"
        elif self.recorder.is_transcribing():
            text, fg, bg = "Transcribing…", WARN, WARN_SOFT
            title = f"Transcribing — {APP_TITLE}"
        elif self.reader.is_speaking():
            text, fg, bg = "▶  Reading", READ, READ_SOFT
            title = f"▶ Reading — {APP_TITLE}"
        elif self.reader.is_paused():
            text, fg, bg = "Paused", MUTED, SURFACE_2
            title = f"Paused — {APP_TITLE}"
        else:
            text, fg, bg = "Idle", SUBTLE, SURFACE_2
            title = APP_TITLE
        if self.chip.cget("text") != text:
            self.chip.configure(text=text, fg=fg, bg=bg)
        if title != self._title:
            self._title = title
            self.root.title(title)

    def run(self) -> None:
        self.root.mainloop()


def _server_alive(send: Callable[..., Any]) -> bool:
    try:
        return send({"cmd": "status"}, timeout=0.6) is not None
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Voice Studio: dictation and read-aloud in one window")
    parser.add_argument("--dictate-on-launch", action="store_true", help="start a dictation take (hotkey launch)")
    parser.add_argument("--read-on-launch", action="store_true", help="read the selected text (hotkey launch)")
    parser.add_argument("--translate-on-launch", action="store_true", help="translate the selected text (hotkey launch)")
    parser.add_argument("--tab", choices=[key for key, _ in TABS])
    parser.add_argument("command", nargs="?", default="gui", choices=["gui", "show"])
    args = parser.parse_args(argv)

    typing_alive = _server_alive(vt.send_ipc)
    reading_alive = _server_alive(vr.send_ipc)
    if typing_alive:
        # Already running: hand the request to it.
        if args.dictate_on_launch:
            vt.send_ipc_with_retries({"cmd": "toggle"}, attempts=2, timeout=1.0)
        elif args.read_on_launch and reading_alive:
            vr.send_ipc_with_retries({"cmd": "toggle"}, attempts=2, timeout=1.0)
        elif args.translate_on_launch and reading_alive:
            vr.send_ipc_with_retries({"cmd": "translate"}, attempts=2, timeout=1.0)
        elif args.tab == "reader" and reading_alive:
            vr.send_ipc_with_retries({"cmd": "show"}, attempts=1, timeout=1.0)
        else:
            vt.send_ipc_with_retries({"cmd": "show"}, attempts=1, timeout=1.0)
        return 0
    if args.command == "show" and reading_alive:
        vr.send_ipc_with_retries({"cmd": "show"}, attempts=1, timeout=1.0)
        return 0

    focus_hint: dict[str, Any] | None = None
    raw_hint = os.environ.get("VOICE_TYPING_FOCUS_HINT", "").strip()
    if raw_hint:
        try:
            parsed = json.loads(raw_hint)
            focus_hint = parsed if isinstance(parsed, dict) else None
        except Exception:
            focus_hint = None

    app = StudioApp(
        dictate_on_launch=args.dictate_on_launch,
        read_on_launch=args.read_on_launch,
        translate_on_launch=args.translate_on_launch,
        focus_hint=focus_hint,
        start_tab=args.tab,
    )
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
