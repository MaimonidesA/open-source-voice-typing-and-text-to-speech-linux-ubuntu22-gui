# Voice Typing Requirements Status

Status snapshot based on our implementation and your latest tests/logs (as of 2026-02-12).

## Achieved Requirements

- Hotkey flow works in general:
  - Press once to start recording.
  - Press again to stop and transcribe.
- Startup readiness is in place:
  - Service-based startup/run path exists.
  - Hotkey can launch/use the GUI controller when needed.
- Single-instance behavior improved:
  - Duplicate GUI creation/flicker issues were reduced.
- GUI feedback is present:
  - Clear recording/transcribing/ready state messages.
  - Latest transcript is shown in GUI.
  - `Copy Last` works.
- GUI redesign requests were implemented:
  - Darker modern style.
  - Always-expanded layout (no Details toggle).
  - Transcript moved near top.
  - Model/options moved below transcript.
  - Smaller default window and smaller minimum size.
- Fallback safety works:
  - Transcript is preserved in GUI and/or clipboard when direct placement is not possible.

## Partially Achieved (Gap Between Requirement and Current Behavior)

- Focus retention while GUI remains visible:
  - Requirement: GUI can stay visible/topmost without stealing effective typing focus.
  - Current: improved with repeated focus-restore attempts, but still inconsistent in some runs.
- Direct paste behavior quality:
  - Requirement: fast “paste-like” delivery.
  - Current: often routes through clipboard and may paste successfully, but not always to intended location.
- Hotkey responsiveness:
  - Requirement: immediate, predictable start/stop every time.
  - Current: much better than before, but some sessions still show occasional timing/jitter issues.
- System-wide target robustness:
  - Requirement: consistent behavior across terminals, browser fields, editor/chat inputs.
  - Current: works in many cases, but not uniformly reliable across all target types and focus states.

## Not Achieved / Unmet Requirements

- Primary core requirement is still unmet:
  - It should always type/paste into the exact text box that was focused at the moment hotkey start was pressed.
  - After stop/transcription, focus should still be there so keyboard-only flow continues.
  - In practice, this still fails in some runs and falls back to clipboard-only behavior.
- Fully keyboard-only end-to-end flow is not yet guaranteed:
  - Requirement: no mouse re-focus needed after stop.
  - Current: sometimes user must click back with mouse to continue.

## Requirement Priority (from your intent)

1. **Highest priority (not solved yet):** deterministic delivery to the original focused text box from hotkey start, with focus preserved/restored for keyboard-only continuation.
2. **Second priority:** keep GUI visible for confidence/status without disrupting focus.
3. **Third priority:** keep current reliable fallbacks (clipboard + GUI transcript) as backup only.

