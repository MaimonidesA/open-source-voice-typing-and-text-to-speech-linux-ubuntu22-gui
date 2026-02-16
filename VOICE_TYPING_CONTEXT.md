# Project Context: Offline Voice Typing (Ubuntu 22.04, Wayland)

## Objective

Build a fully offline voice typing system on Ubuntu 22.04 (GNOME Wayland) using:

- whisper.cpp (speech-to-text)
- SoX (audio capture)
- ydotool + ydotoold (Wayland input injection via uinput)
- wl-clipboard (Wayland clipboard)
- Custom toggle script (`voice-toggle`)
- GNOME global keyboard shortcuts

The system must:
- Work in any focused text field (terminal, browser, editor)
- Be toggle-based (press once to record, press again to stop + type)
- Allow easy model switching
- Remain fully offline

---

## Current Architecture

### Audio Capture
- Tool: `sox`
- Format: 16kHz mono signed-integer PCM
- Safety cap: 60 seconds max recording

### Speech-to-Text
- Engine: `whisper.cpp`
- Binary: `~/whisper.cpp/build/bin/whisper-cli`
- Default flag set:
  - `-nt` (no timestamps)
  - `--no-gpu`
  - `timeout 25s` wrapper
- Models stored in:
  `~/whisper.cpp/models/`

### Installed Models
- base.en
- small.en
- (optional quantized variants)

Daily driver recommendation: `small.en`

---

### Wayland Input Injection

- ydotool built from source
- ydotoold daemon started manually:


sudo ydotoold --socket-path=/tmp/.ydotool_socket --socket-perm=0666 &

- Environment variable required:


export YDOTOOL_SOCKET=/tmp/.ydotool_socket


Typing works via:


ydotool type "<text>"


---

## Main Script

Path:


/usr/local/bin/voice-toggle


Behavior:
- First run: start recording
- Second run: stop recording, transcribe, type, copy to clipboard

Features:
- Locking via /tmp/voice_toggle.lock
- PID tracking via /tmp/voice_toggle.pid
- Logging via /tmp/voice_toggle_whisper.log
- Model switching via:


voice-toggle --model small.en

- Model listing:


voice-toggle --list-models


---

## GNOME Integration

Global keyboard shortcut configured via:


gnome-control-center keyboard


Primary binding:


/usr/local/bin/voice-toggle


Optional secondary binding:


/usr/local/bin/voice-toggle --model small.en


---

## Known Constraints

- Must run on GNOME Wayland
- Requires uinput kernel module
- ydotoold must be running
- Whisper invoked with `--no-gpu` to avoid backend instability
- 60s max recording cap
- Timeout enforced for Whisper (25s)

---

## Troubleshooting

Check daemon:


pgrep -a ydotoold


Check socket:


ls -l /tmp/.ydotool_socket


Kill stuck processes:


pkill -f whisper-cli
pkill -INT sox


Check Whisper logs:


tail -n 50 /tmp/voice_toggle_whisper.log


---

## Future Improvements

Possible enhancements:
- systemd user service for ydotoold
- Add model auto-benchmarking
- Add VAD instead of toggle mode
- Add model auto-selection based on CPU load
- Add confidence threshold filtering
- Switch to faster-whisper backend if needed
- Add language auto-detect mode
 the transcript in one place and then change it to another and I 