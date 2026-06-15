# Remake Plan — Voice Typing + Voice Reading (June 2026)

## What the research found

### Why the app "stopped working properly"

1. **The Gemma/Ollama integration itself works.** Verified end-to-end on this
   machine: a 5-second WAV sent to `gemma4:latest` through
   `POST /v1/chat/completions` (the exact request `voice_typing.py` builds)
   returns a perfect transcript.
2. **The real breakage was config drift.** During the Gemma experiments the
   live config (`~/.config/voice-typing/config.json`) ended up in the
   "antonio" assistant state:
   - `assistant_wake_mode: true` → normal dictation is *ignored* until you say
     "hey antonio". This is the main reason it felt dead/broken.
   - `streaming_transcription: true` with `streaming_chunk_seconds: 5` →
     choppy 5-second chunks.
   - `voice_prompts_enabled: true`, `auto_paste_current_focus: true` → odd,
     surprising behavior.
3. **gemma4 runs 100% on CPU on this laptop.** The installed `gemma4:latest`
   is the **e4b** variant (8B params, Q4_K_M, ~10 GB loaded). The RTX 4070
   Laptop GPU has 8 GB VRAM, so Ollama falls back to CPU. Measured: ~5 s warm
   / ~19 s cold to transcribe a 5 s clip. Usable, but not instant.
4. **Recording state was easy to miss**: a 12 px dot + small text, and no
   visual state at all while transcribing.

### gemma4 sizes available in Ollama (`ollama pull gemma4:<tag>`)

| Tag | Fits 8 GB VRAM? | Notes |
|-----|-----------------|-------|
| `gemma4:e2b` | yes | fastest local option, best for this laptop's GPU |
| `gemma4:e4b` (= `latest`, installed) | no → CPU | what you have now |
| `gemma4:12b` / `12b-it-q4` / `12b-it-qat` | **no** → CPU, slower than e4b | only worth it on a machine with ≥ 12–16 GB VRAM |
| `gemma4:26b`, `gemma4:31b` | no | server/robot-class hardware only |

**Recommendation:** on this laptop do *not* move to 12b — it will be slower
than what you have. If you want faster Gemma here, try `ollama pull
gemma4:e2b` and select it in the GUI. Reserve 12b for the robot if that
machine has ≥ 12 GB of GPU memory; the code needs no changes, just a
different `ollama_model` value.

## Design decisions

- **Two named modes, one click each.** The robot/agent use-case and the
  daily-dictation use-case are different products sharing one pipeline:
  - `stable` — exactly the pre-Gemma behavior: whisper.cpp, single
    180 s recording, no wake word, no streaming. **Default.**
  - `gemma-agent` — Ollama + gemma4, rolling ≤ 28 s chunks (Gemma audio
    prompts are capped at 30 s), no wake word. This is the offline-robot
    pipeline: same model can later take the transcript as an agent prompt.
  - (`antonio` — the experimental wake-word mode — is kept but not default.)
  Switchable from the GUI (two buttons, applied live, no restart) or CLI
  (`./voice_typing.py set-profile stable|gemma-agent|antonio`).
- **Whisper stays the default STT.** It is faster and lighter than Gemma on
  this hardware. Gemma earns its place as the *agent brain* (and as STT where
  whisper.cpp isn't available, e.g. a stripped robot image).
- **Warm-up ping for Ollama.** When a recording starts in gemma-agent mode the
  app asks Ollama to load the model (`keep_alive`), so transcription of the
  first chunk doesn't pay the ~14 s cold-load penalty.
- **Unmistakable recording state.** The whole status bar changes color
  (red = recording with blinking dot, amber = transcribing, dark = idle) and
  the window title carries the state too (visible from the taskbar).
- **Reader voices on demand.** A built-in downloader fetches additional
  British (`en_GB`) Piper voices from `rhasspy/piper-voices` on Hugging Face
  into `piper_voices/` — GUI dialog or
  `./voice_reading.py download-voice <name>`. Current voice is
  `en_GB-cori-high`; good alternatives to audition: `jenny_dioco`,
  `alan`, `northern_english_male`, `semaine`, `vctk`.
- **No changes to the routing/AT-SPI focus logic** — that predates the Gemma
  work and was explicitly out of scope.

## Never lose long recordings (added during this session)

A second root cause found while working: `transcribe_timeout_seconds: 90`
silently killed transcription of anything much longer than ~3 minutes of
audio — exactly the "I spoke for 30 minutes and lost it" failure. Fixes:

- **Archive before transcribing.** Every take (single or chunked) is copied to
  `~/.local/share/voice-typing/recordings/` (newest 30 kept,
  `recordings_keep`) *before* the transcriber runs.
- **Timeout scales with audio length** (3× duration + 60 s, minimum the
  configured value).
- **Retry without re-speaking:** `./voice_typing.py retranscribe [last|file]`
  or the GUI button *Retry Last Audio* — result goes to the transcript box +
  clipboard (never auto-typed, since the original focus is gone).
- **Recording limit raised** to 30 min (`recording_max_seconds: 1800`) with a
  30 s warning, since long dictation is normal use.
- Whole-recording transcription (stable mode) is kept as the default because
  Whisper does better with full context than with 5 s fragments.

## Microphone-quality feedback (added during this session)

`sox <wav> -n stats` (C, runs in milliseconds, no extra Python DSP or model
load) is run on every finished recording and on every rolling chunk:

- Verdicts: **Mic OK / quiet / very quiet / clipping / no signal**, derived
  from RMS and peak dB.
- Shown live in a small colored line under the status bar, appended to
  failure notifications, logged to `/tmp/voice_typing/routing.log`, and
  printed by `retranscribe`.

## Why the Gemma agent sometimes "didn't respond" (fixed)

Two compounding causes, both reproduced against archived recordings and fixed
in `_transcribe_audio_ollama`:

1. **gemma4 thinks by default.** The chain-of-thought went into a `reasoning`
   field, consumed the entire `max_tokens` budget (`finish_reason: "length"`),
   and `content` came back empty — the app showed nothing. Fix:
   `reasoning_effort: "none"` in the request (config
   `ollama_disable_thinking`, default on) plus `ollama_max_tokens: 512`.
   Measured: 44 s → 5 s, empty → real transcript.
2. **Gemma's audio encoder is weak on quiet input.** A −36 dB RMS recording
   that Whisper transcribes perfectly was dismissed by Gemma as
   "unintelligible". Fix: each clip is peak-normalized (`sox … norm -3`,
   milliseconds) before upload (config `ollama_normalize_audio`, default on).

3. **Long files degenerate.** Gemma audio prompts cap at 30 s; an 80 s
   archived recording sent whole produced an endless repetition loop. Live
   recording already chunks at 28 s, but *retranscribe* sends whole files, so
   the Ollama engine now auto-splits anything over 30 s (`sox trim`) and
   joins the partial transcripts. A `frequency_penalty: 0.5` further damps
   repetition loops on hard audio.

### Head-to-head on a real 80 s recording (same archived file)

| Engine | Time | Result |
|--------|------|--------|
| whisper (`ggml-small.en`) | 15 s | Near-perfect transcript of accented English |
| gemma4 e4b via Ollama (all fixes) | ~40 s | Responds reliably now, but misses/garbles phrases |

Conclusion: even fully fixed, Gemma's transcription accuracy is below
whisper.cpp for accented speech — which is why `stable` (Whisper) stays the
dictation default and Gemma is positioned as the offline *agent* pipeline
(and as STT only where whisper.cpp cannot run).

### Direct model access instead of Ollama?

Considered and not recommended. Ollama already runs the model locally via
llama.cpp with quantization, memory management, and keep-alive; bypassing it
(the `gemma4-transformers` engine, kept as an option) needs torch +
transformers, a separate ~16 GB checkpoint download, and is slower on this
8 GB-VRAM machine. The "doesn't respond" problem was never Ollama overhead —
it was the request shape, fixed above. For the robot, Ollama remains the
cleanest fully-offline runtime.

## One-click restore

`./voice_typing.py set-profile stable` (also a GUI button). This was applied
to the live config as part of this remake, so the next launch behaves like
the pre-Gemma setup.

## Ask-Gemma mode (added during this session)

The head-to-head results led to a role split the user proposed: **Whisper
transcribes, Gemma reasons.** The `ask-gemma` profile (third GUI mode button):

1. Whisper transcribes the spoken question (the accurate STT).
2. The transcript goes to gemma4 via Ollama as a *text* question
   (`ask_ollama` / streaming variant; thinking disabled, plain-text system
   prompt).
3. The answer is routed exactly like a transcript (typed/pasted/clipboard at
   the point of focus) and shown as `Q: … / A: …` in the panel.
4. **Spoken answers**: tokens stream from Ollama; complete sentences are cut
   off as they form and queued into a background piper TTS worker
   (`SentenceSpeaker`, uses the reader's configured voice). Speech starts
   after the first sentence, while the model is still generating; nothing
   blocks the GUI. Starting a new recording or pressing Stop silences it.
   Toggle: "Speak answers aloud" checkbox / `answer_speak_aloud`.
5. CLI: `./voice_typing.py ask [--speak] "question"`.

Measured end-to-end (spoken question → spoken answer begins): ~6 s warm.

## Known limitations / later ideas

- The ask flow is single-turn (no conversation memory yet); a follow-up could
  keep a short rolling chat history per session.
- Gemma agent mode currently only transcribes; wiring transcripts into an
  Ollama *tool-calling* agent loop (for the robot) is the natural next step —
  gemma4 reports `tools` + `thinking` capabilities, so the plumbing exists.
- Wake-word mode (`antonio`) still uses Whisper for detection; it remains
  experimental.
- The reader's `openai_api_key` config field contains accidentally dictated
  text (harmless while "Reading Mode" preprocessing is off).
