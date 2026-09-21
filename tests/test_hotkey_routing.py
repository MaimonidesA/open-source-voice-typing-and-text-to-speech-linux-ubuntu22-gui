"""Where a hotkey transcript goes. No keys are sent: paste/insert/copy are
recorded instead."""

import json

import pytest

import voice_typing as vt


def target(app: str, key: str, accessible=None, pid: int = 100) -> vt.FocusTarget:
    return vt.FocusTarget(accessible=accessible, source_app=app, source_pid=pid, window_key=key)


CODE = target("code", "100:/w/1")
CODE_AGAIN = target("code", "100:/w/1")
TERMINAL = target("gnome-terminal-server", "200:/w/2", pid=200)


class Desktop:
    """Stand-in for AT-SPI focus, the clipboard and ydotool."""

    def __init__(self, focus_sequence, paste_ok=True, insert_ok=True):
        self.focus_sequence = list(focus_sequence)
        self.paste_ok, self.insert_ok = paste_ok, insert_ok
        self.pastes, self.inserts, self.copies = [], [], []

    def capture(self):
        return self.focus_sequence.pop(0) if self.focus_sequence else None

    def paste(self, text, restore_clipboard, target=None, shortcut=None):
        self.pastes.append((text, target, shortcut))
        return self.paste_ok

    def insert(self, target, text):
        self.inserts.append((target, text))
        return self.insert_ok

    def copy(self, text):
        self.copies.append(text)
        return True


@pytest.fixture
def route(tmp_path, monkeypatch):
    def _route(focus_sequence, start=None, start_hint=None, config=None, **desktop_kwargs):
        desktop = Desktop(focus_sequence, **desktop_kwargs)
        monkeypatch.setattr(vt, "capture_focus_target", desktop.capture)
        monkeypatch.setattr(vt, "paste_text_via_clipboard", desktop.paste)
        monkeypatch.setattr(vt, "insert_text_into_target", desktop.insert)
        monkeypatch.setattr(vt, "copy_to_clipboard", desktop.copy)
        monkeypatch.setattr(vt, "type_text_via_ydotool", lambda text: False)
        monkeypatch.setattr(vt, "log_routing", lambda message: None)
        monkeypatch.setattr(vt, "speak_prompt", lambda *a, **k: None)
        monkeypatch.setattr(vt.time, "sleep", lambda seconds: None)
        monkeypatch.setattr(vt, "pyatspi", None)  # no focus tracker thread
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps(config or {}))
        controller = vt.RecorderController(vt.ConfigManager(cfg_path))
        controller._capture_focus = True
        controller._started_from_gui = False
        controller._focus_target = start
        controller._focus_target_at_start = start_hint
        return controller, controller._route_transcript("hello"), desktop

    return _route


def test_same_window_pastes(route):
    controller, result, desktop = route([CODE_AGAIN], start=CODE)
    assert result == "focused_target_pasted"
    assert desktop.pastes[0][1] is CODE_AGAIN
    assert controller._clipboard_text == "hello"


def test_moved_to_another_app_only_copies(route):
    controller, result, desktop = route([TERMINAL], start=CODE)
    assert result == "clipboard_only" and not desktop.pastes and desktop.copies == ["hello"]
    assert "code" in controller._last_route_note and "gnome-terminal-server" in controller._last_route_note


def test_moved_away_from_an_editable_field_inserts_there(route):
    _, result, desktop = route([TERMINAL], start=target("gedit", "300:/w/3", accessible=object(), pid=300))
    assert result == "focused_target" and desktop.inserts and not desktop.pastes


def test_transient_focus_miss_is_retried(route):
    _, result, _ = route([None, CODE_AGAIN], start=CODE)
    assert result == "focused_target_pasted"


def test_unknown_start_and_unknown_focus_pastes_best_effort(route):
    _, result, desktop = route([None, None, None])
    assert result == "clipboard+pasted" and desktop.pastes[0][1] is None


def test_unknown_start_but_known_focus_only_copies(route):
    _, result, desktop = route([CODE])
    assert result == "clipboard_only" and not desktop.pastes
    _, result, desktop = route([CODE], config={"auto_paste_current_focus": True})
    assert result == "clipboard+pasted"


def test_start_from_hint_and_per_app_shortcut(route):
    hint = vt.focus_target_from_hint(vt.focus_target_to_hint(CODE))
    assert hint.window_key == CODE.window_key
    _, result, desktop = route([CODE_AGAIN], start_hint=hint, config={"paste_shortcut_overrides": {"Code": "Ctrl+Shift+V"}})
    assert result == "focused_target_pasted" and desktop.pastes[0][2] == "ctrl+shift+v"


def test_paste_failure_falls_back_to_clipboard(route):
    _, result, desktop = route([CODE_AGAIN], start=CODE, paste_ok=False, insert_ok=False)
    assert result == "clipboard_only" and desktop.copies == ["hello"]


def test_paste_keys(monkeypatch):
    sent = []
    monkeypatch.setattr(vt, "_run_ydotool_command", lambda cmd, **kw: (sent.append(cmd) or (True, "")))
    monkeypatch.setattr(vt, "log_routing", lambda message: None)
    monkeypatch.setattr(vt.shutil, "which", lambda name: "/usr/bin/" + name)
    vt.paste_current_focus_via_ydotool(CODE)
    vt.paste_current_focus_via_ydotool(vt.FocusTarget(None, "gnome-terminal-server", 1, "terminal:x"))
    assert sent[0][4:] == ["29:1", "47:1", "47:0", "29:0"]
    assert sent[1][4:] == ["29:1", "42:1", "47:1", "47:0", "42:0", "29:0"]
