# Offline Voice Typing + Voice Reading (Wayland-safe workflow)

This setup replaces direct key-injection dictation with a controlled flow:

- `Win+H` toggles recording through an IPC controller.
- `Win+H` reuses a single GUI/controller instance (no duplicate windows).
- A small GUI always shows recording state (`Idle` vs `Recording...`).
- If started via keyboard shortcut, transcript is inserted back into the original focused field when possible (AT-SPI path).
- If direct insertion is unavailable, it falls back to clipboard + paste for hotkey flow.
- If started via GUI button, transcript stays in GUI by default (optional clipboard copy setting).

It also includes a companion **read-aloud** app:

- `Super+R` can read currently selected text.
- Small GUI provides speed/pitch/voice controls.
- Primary-selection first, copy-fallback second.
- Optional `piper` backend for natural neural voices (if installed + model files available).

## Files

- `voice_typing.py`: Main app (GUI + IPC + recording + transcription)
- `voice-toggle`: Shortcut wrapper command
- `voice_reading.py`: Text-to-speech app (GUI + IPC + selected-text read aloud)
- `voice-read`: Read-aloud hotkey wrapper command
- `config.example.json`: Example config fields

## Dependencies

Ubuntu 22.04:

```bash
sudo apt update
sudo apt install -y sox wl-clipboard ydotool libnotify-bin python3-tk python3-pyatspi speech-dispatcher
```

You also need:

- `whisper.cpp` with `whisper-cli` built
- models in `~/whisper.cpp/models` (for example: `ggml-small.en.bin`)

## First run

```bash
cd /home/yaronbayrobee/Voice_typing
chmod +x voice_typing.py voice-toggle voice_reading.py voice-read
./voice_typing.py gui
```

This creates config at:

- `~/.config/voice-typing/config.json`
- Fallback (if home config is not writable): `/home/yaronbayrobee/Voice_typing/.voice-typing-config/config.json`

## Bind `Win+H` in GNOME

Set custom shortcut command to:

```bash
/home/yaronbayrobee/Voice_typing/voice-toggle
```

Behavior:

- If GUI is already running: `Win+H` toggles start/stop.
- If GUI is not running: first `Win+H` launches GUI and starts recording.

## Bind `Super+R` for read aloud

Set another custom shortcut command to:

```bash
/home/yaronbayrobee/Voice_typing/voice-read
```

Behavior:

- If Voice Reading GUI is running: `Super+R` toggles read/stop.
- If not running: first `Super+R` launches it and starts read-aloud.

## Model switching

From GUI:

- Voice Typing: use model dropdown in `voice_typing.py`.
- Voice Reading: choose `Engine = piper` and click `Refresh Models` after adding `.onnx` voice models.

Voice Reading Piper model search paths:

- `~/.local/share/piper/voices`
- `~/.local/share/piper`
- `~/.cache/piper`
- `/usr/share/piper/voices`
- `/usr/share/piper`
- `/usr/share/piper-voices`
- `/opt/piper/voices`
- `/home/yaronbayrobee/Voice_typing/piper_voices`

From terminal:

```bash
./voice_typing.py list-models
./voice_typing.py set-model ggml-small.en.bin
```

## Recommended config tweaks

Edit your generated config file (`~/.config/voice-typing/config.json` or fallback path above):

- `recording_max_seconds`: max recording length.
- `recording_warn_before_seconds`: pre-limit warning.
- `auto_paste_current_focus`: paste after clipboard fallback (off by default for safety).
- `copy_to_clipboard_when_started_from_gui`: if true, GUI-start recordings also copy to clipboard.
- `paste_fallback_for_hotkey`: if true, hotkey flow pastes after clipboard fallback.
- `restore_clipboard_after_paste`: restore previous clipboard after auto-paste fallback.

## Notes on "type only where I started"

On Wayland, generic direct focus control is restricted. This implementation uses accessibility (AT-SPI) to target the original field when supported by the app. If unavailable, it safely uses clipboard fallback instead of uncontrolled key spam.
