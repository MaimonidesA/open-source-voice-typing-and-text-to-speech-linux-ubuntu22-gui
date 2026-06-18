# Offline Voice Typing + Voice Reading (Ubuntu 22.04, Wayland)

Two cooperating apps, fully offline:

- **Voice Typing** (`voice_typing.py`) — push-to-talk dictation: `Win+H` toggles recording, the transcript is inserted back into the field you were typing in (AT-SPI, with clipboard/paste fallback). Also hosts the **Ask Gemma** voice-assistant mode.
- **Voice Reading** (`voice_reading.py`) — `Super+R` reads the currently selected text aloud (Piper neural voices or speech-dispatcher).

Design notes and the full history of decisions live in [REMAKE_PLAN.md](REMAKE_PLAN.md). (`VOICE_TYPING_CONTEXT.md` and `VOICE_TYPING_REQUIREMENTS_STATUS.md` are historical snapshots.)

## Repo layout

| Path | What it is |
|------|------------|
| `voice_typing.py` | Dictation app: GUI + IPC + recording + transcription + ask mode |
| `voice_reading.py` | Read-aloud app: GUI + IPC + TTS + voice downloader |
| `voice-toggle`, `voice-paste`, `voice-read` | Hotkey wrapper scripts (bind these in GNOME) |
| `config.example.json` | Reference for every voice-typing config key |
| `systemd/` | User services for autostart (see below) |
| `piper_runtime/piper/` | **Bundled** Piper TTS binary + libs — no install needed |
| `piper_voices/` | Piper voice models (`.onnx`) + the voice index used by the downloader |
| `.voice-typing-config/` | Fallback config dir (only used if `~/.config` is not writable) |

## Installation

### 1. System packages (apt)

```bash
sudo apt update
sudo apt install -y sox wl-clipboard ydotool libnotify-bin python3-tk python3-pyatspi speech-dispatcher curl
```

What each is for: `sox` (mic capture + audio analysis), `wl-clipboard` (Wayland clipboard), `ydotool` (key injection), `libnotify-bin` (desktop notifications), `python3-tk` (GUI), `python3-pyatspi` (insert-at-focus via accessibility), `speech-dispatcher` (fallback TTS engine for the reader).

No pip packages are required — both apps are Python stdlib + the apt packages above.

### 2. ydotoold daemon (required for auto-typing/pasting)

`ydotool` needs its daemon running with access to `/dev/uinput`. On this machine it is built from source and installed at `/usr/local/bin/ydotoold` (the Ubuntu 22.04 apt package does not ship a daemon). From scratch:

```bash
sudo apt install -y cmake scdoc build-essential
git clone https://github.com/ReimuNotMoe/ydotool ~/src/ydotool
cd ~/src/ydotool && mkdir build && cd build && cmake .. && make && sudo make install
```

On this machine it runs as a **user** service (`/usr/lib/systemd/user/ydotoold.service`, installed by `make install`), with the socket at `$XDG_RUNTIME_DIR/.ydotool_socket` — the app auto-discovers it there or at `/tmp/.ydotool_socket`. Enable and verify with:

```bash
systemctl --user enable --now ydotoold.service
ls "$XDG_RUNTIME_DIR/.ydotool_socket"
```

Your user also needs write access to `/dev/uinput` (udev rule or membership in the group that owns it) for the daemon to inject keys.

### 3. whisper.cpp (default speech-to-text)

Expected at `~/whisper.cpp`, models in `~/whisper.cpp/models` (paths configurable via `whisper_cli_path` / `models_dir`):

```bash
git clone https://github.com/ggml-org/whisper.cpp ~/whisper.cpp
cd ~/whisper.cpp
./models/download-ggml-model.sh small.en      # the model in use: ggml-small.en.bin
./models/download-ggml-model.sh base.en       # optional, faster/lighter
```

**CUDA build (used on this machine, `whisper_cli_path: ~/whisper.cpp/build-cuda/bin/whisper-cli`)** — runs on the RTX GPU and falls back to CPU automatically if CUDA is unavailable. Existing configs that still point at the old default `~/whisper.cpp/build/bin/whisper-cli` are promoted to this CUDA binary on startup when it exists. Requires the CUDA toolkit (installed here at `/usr/local/cuda`); cap build parallelism — an unrestricted `-j` CUDA compile can OOM the machine:

```bash
cmake -B build-cuda -DGGML_CUDA=1 -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc -DCMAKE_CUDA_ARCHITECTURES=89
nice -n 10 cmake --build build-cuda -j4 --config Release
```

CPU-only build (fallback/reference): `cmake -B build && cmake --build build -j4 --config Release`. Note: the app's "Use GPU" checkbox (`no_gpu` config) only has an effect with a CUDA build — a CPU-only `whisper-cli` ignores it.

### 4. Ollama + gemma4 (for the Gemma agent / Ask Gemma modes)

```bash
curl -fsSL https://ollama.com/install.sh | sh   # install or upgrade; safe to re-run
ollama pull gemma4                              # = gemma4:e4b, the model in use
```

Notes:
- Upgrading Ollama never requires changes to this app (it uses the stable HTTP API on `127.0.0.1:11434`), and downloaded models survive upgrades.
- `ollama pull gemma4:12b` requires a newer Ollama than 0.21 (you get HTTP 412 otherwise) — upgrade first with the same install script.
- VRAM guide for model choice: `e2b` fits an 8 GB GPU; `e4b` (~10 GB loaded) and `12b` fall back to CPU on this laptop — they work, just slower.

### 5. Piper TTS (bundled — nothing to install)

The Piper binary ships in this repo (`piper_runtime/piper/piper`) together with two British voices. More voices are downloaded on demand into `piper_voices/` (see "Reader voices" below). This bundled Piper runtime uses CPU ONNX Runtime; GPU TTS would require installing or building a CUDA-enabled Piper/ONNX Runtime runtime and putting that `piper` binary on `PATH`.
Verify the active runtime with `ldd "$(command -v piper || echo ./piper_runtime/piper/piper)" | grep -Ei 'cuda|onnxruntime'`; the bundled runtime has ONNX Runtime only, not CUDA providers.

**Experimental GPU Piper runtime** — isolated from the bundled CPU runtime and from whisper.cpp:

```bash
./scripts/setup_piper_gpu_runtime.sh
```

This creates `~/piper-gpu/bin/piper`, a small Piper-compatible wrapper that uses `piper-tts` with `onnxruntime-gpu`. In the Voice Reading Settings panel, enable **Use GPU Piper runtime** and set the path to `~/piper-gpu/bin/piper`. Uncheck the box to roll back instantly to the bundled CPU Piper. Verify with `./voice_reading.py status` and `nvidia-smi` during a long read.

### 6. Optional: gemma4 through HuggingFace transformers

Only for the alternative `gemma4-transformers` engine (not recommended on this machine — see REMAKE_PLAN.md):

```bash
python3 -m pip install torch accelerate transformers soundfile
```

### 7. First run + health check

```bash
cd /home/yaronbayrobee/Voice_typing
chmod +x voice_typing.py voice-toggle voice_reading.py voice-read
./voice_typing.py doctor     # checks sox, wl-copy, ydotool, whisper-cli, model, ollama
./voice_typing.py gui
```

### 8. Autostart (systemd user services)

`voice-typing-gui.service` is installed and enabled on this machine; the reader unit is optional:

```bash
cp systemd/voice-typing-gui.service systemd/voice-reading-gui.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now voice-typing-gui.service
systemctl --user enable --now voice-reading-gui.service   # optional
```

After editing the code, apply with: `systemctl --user restart voice-typing-gui.service`.

### 9. GNOME hotkeys

Settings → Keyboard → Custom Shortcuts:

| Shortcut | Command |
|----------|---------|
| `Win+H` | `/home/yaronbayrobee/Voice_typing/voice-toggle` |
| `Super+R` | `/home/yaronbayrobee/Voice_typing/voice-read` |

If the GUI is already running the hotkey toggles it; if not, the first press launches it and starts recording/reading.

## File and data paths

| Path | Purpose |
|------|---------|
| `~/.config/voice-typing/config.json` | Live voice-typing config (created on first run) |
| `~/.config/voice-typing/reading_config.json` | Live voice-reading config |
| `~/.local/share/voice-typing/recordings/` | Recording archive (newest 30 kept) |
| `/tmp/voice_typing/` | Runtime state: current WAV, chunks, `routing.log` |
| `/tmp/voice_typing.sock`, `/tmp/voice_reading.sock` | IPC sockets (hotkey ↔ GUI) |

## Operation modes (one click / one command)

| Profile | What it does |
|---------|--------------|
| `stable` | The original behavior: whisper.cpp, single recording up to 30 min, no wake word. **Default.** |
| `ask-gemma` | Voice Q&A: Whisper transcribes your spoken question, gemma4 (Ollama) answers; the answer is typed/pasted where you were working **and spoken aloud** (Piper, sentence-by-sentence while still generating). |
| `gemma-agent` | Fully-offline gemma4 STT via Ollama in 28 s rolling chunks. For machines without whisper.cpp (robot). |
| `antonio` | Experimental wake-word assistant ("hey antonio"). |

Switch from the GUI ("Mode:" buttons) or terminal: `./voice_typing.py set-profile stable`.

Ask Gemma from the terminal too:

```bash
./voice_typing.py ask "why is the sky blue?"
./voice_typing.py ask --speak "why is the sky blue?"
```

## Never lose a recording

Every recording is archived to `~/.local/share/voice-typing/recordings/` **before** transcription starts. If transcription fails, times out, or comes back empty:

```bash
./voice_typing.py list-recordings
./voice_typing.py retranscribe              # newest recording, prints + copies to clipboard
./voice_typing.py retranscribe recording_20260612_110000.wav
```

or click **Retry Last Audio** in the GUI. Whisper transcription timeouts scale with audio length, so a 30-minute take is allowed to finish. A lightweight mic-quality check (`sox stats`, milliseconds of CPU) runs on every recording/chunk and shows "Mic OK / quiet / clipping / no signal" in the GUI and in failure notifications.

## Model switching

**Whisper models** — drop `ggml-*.bin` files into `~/whisper.cpp/models` (or run the download script in step 3), then the model dropdown in the GUI, or:

```bash
./voice_typing.py list-models
./voice_typing.py set-model ggml-small.en.bin
```

**Ollama / Gemma models** — pull with Ollama, then the Ollama dropdown (**Refresh** to re-scan), or:

```bash
ollama pull gemma4:12b
./voice_typing.py set-ollama-model gemma4:12b
```

The selected Ollama model is used for both gemma-agent transcription and ask-gemma answers.

**Reader voices (Piper, British)** — the **Get GB Voices** button in the reader GUI, or:

```bash
./voice_reading.py list-gb-voices
./voice_reading.py download-voice en_GB-jenny_dioco-medium
```

then **Refresh Models** and pick the voice in the Model dropdown. Voices land in `piper_voices/`; the reader also scans `~/.local/share/piper[/voices]`, `~/.cache/piper`, `/usr/share/piper[-voices]`, `/opt/piper/voices`.

**Spoken-answer voice (ask-gemma)** — follows the reader's chosen Piper voice automatically; override with the `answer_voice_model` config key (path to an `.onnx`).

## CLI reference (voice_typing.py)

```bash
./voice_typing.py gui | toggle | start | stop | status | paste-last
./voice_typing.py doctor                       # dependency health check
./voice_typing.py set-profile stable|ask-gemma|gemma-agent|antonio
./voice_typing.py list-audio-inputs | set-audio-input default|pulse:<source>|alsa:<device>
./voice_typing.py list-engines | set-engine whisper|ollama|gemma4-transformers
./voice_typing.py list-models | set-model <ggml-file>
./voice_typing.py set-ollama-model <name> | set-gemma-model <hf-id-or-path>
./voice_typing.py list-recordings | retranscribe [last|file|path]
./voice_typing.py ask [--speak] "question"
./voice_typing.py list-windows
```

## Key config settings

Full reference with defaults: [config.example.json](config.example.json). Highlights (`~/.config/voice-typing/config.json`):

- `transcription_engine`: `whisper`, `ollama`, or `gemma4-transformers`.
- `recording_max_seconds` (1800) / `recording_warn_before_seconds` (30); `0` = unlimited (use with rolling chunks).
- `audio_input_device` — empty/default uses the desktop default mic; set with `./voice_typing.py list-audio-inputs` and `./voice_typing.py set-audio-input ...`.
- `save_recordings` (true) / `recordings_dir` / `recordings_keep` (30) — the archive.
- `streaming_transcription` + `streaming_chunk_seconds` — rolling chunks while talking (Gemma is clamped to ≤30 s).
- `ollama_model`, `ollama_url`, `ollama_keep_alive` (30m, keeps the model warm), `ollama_max_tokens`, `ollama_disable_thinking` (true — **important**: gemma4 otherwise burns its whole token budget on hidden reasoning and returns nothing), `ollama_normalize_audio` (true — Gemma needs louder input than Whisper), `ollama_frequency_penalty` (0.5 — damps repetition loops).
- `assistant_answer_mode` — the ask-gemma switch; `answer_speak_aloud` (true), `answer_voice_model`, `ollama_ask_system_prompt`, `ollama_answer_max_tokens` (600).
- `auto_paste_current_focus`, `paste_fallback_for_hotkey`, `restore_clipboard_after_paste`, `copy_to_clipboard_when_started_from_gui` — routing behavior.
- `voice_commands_enabled`, `assistant_wake_mode`, `assistant_wake_phrases` — wake-word/assistant extras.

## Troubleshooting

- `./voice_typing.py doctor` — checks every dependency for the active engine.
- **Nothing types/pastes** → is `ydotoold` running? (`systemctl --user status ydotoold`, socket at `$XDG_RUNTIME_DIR/.ydotool_socket`).
- **Gemma gives empty answers** → make sure `ollama_disable_thinking` is true (default).
- **`ollama pull` says HTTP 412** → your Ollama is too old for that model; re-run the install script to upgrade.
- **Gemma slow** → `ollama ps` shows `100% CPU` when the model doesn't fit VRAM; pick a smaller model (`gemma4:e2b`).
- **Gemma *extremely* slow (under 1 token/s) even though `ollama ps` says GPU** → check `journalctl -u ollama | grep -i cuda`. If you see `failed to initialize CUDA: unknown error` and `journalctl -k` shows NVRM assertion failures, the NVIDIA driver state is corrupted (happens after long uptimes); Ollama silently falls back to a broken Vulkan path. **Fix: reboot**, then `systemctl restart ollama`. Verify with `ollama ps` (model should load fast) and `journalctl -u ollama | grep "library=CUDA"`.
- **"Graphics apps (Gazebo/RViz) use the RTX fine but Whisper/Ollama don't"** → graphics (OpenGL/Vulkan) and compute (CUDA) are separate driver paths; CUDA can be broken while rendering still works. Test compute health with `python3 -c "import torch; print(torch.cuda.is_available())"`. On dual-GPU laptops the desktop runs on the integrated GPU by design — that's fine; CUDA apps address the RTX directly.
- **GPU sharing with robot sims** → everything fits 8 GB together (whisper small ≈ 0.5 GB transient, gemma4 e4b ≈ 2.6 GB, Gazebo+RViz ≈ 1.2 GB). To keep voice typing off the GPU during heavy sim work: untick "Use GPU" (whisper) and lower `ollama_keep_alive` so Gemma unloads when idle.
- **Blank transcript right after pressing the hotkey** → duplicate GNOME shortcut events are ignored: toggles inside 0.8 s are dropped, and hotkey-started recordings cannot be stopped for the first 1.5 s.
- **Blank audio / wrong microphone** → run `./voice_typing.py list-audio-inputs`, then choose a source such as `./voice_typing.py set-audio-input pulse:alsa_input.usb-MUSIC-BOOST_Trust_GXT_232_Microphone-00.mono-fallback`; if a pinned Pulse source disappears, recording is refused with a microphone error instead of being reported as a time limit. Truly silent captures are skipped before Whisper.
- **Transcript goes to the wrong app** → the hotkey flow only trusts the focus target captured at start. If no reliable starting text box is found, the transcript is copied to the clipboard instead of being pasted into whatever happens to be focused later.
- **Reader stop/start does not stop audio** → the reader now stops the current TTS/player process group before starting a new GUI or `read` request. `toggle` still means stop when already reading.
- **Reader highlight timing** → Piper debug output provides generated audio duration per sentence, so sentence highlighting follows actual sentence boundaries. Piper does not provide exact word timestamps in this setup; word highlighting is therefore off by default and remains an optional estimated mode in Settings.
- **Lost a transcription** → it isn't lost: `./voice_typing.py retranscribe`.
- Routing decisions are logged to `/tmp/voice_typing/routing.log`.

## Notes on "type only where I started"

On Wayland, generic direct focus control is restricted. This implementation uses accessibility (AT-SPI) to target the original field when supported by the app. If unavailable, it safely uses clipboard fallback instead of uncontrolled key spam.
