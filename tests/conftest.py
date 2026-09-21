"""Tests run offline: no microphone, no speakers, no hotkeys. Playback is a
silent `sleep`, and anything touching the desktop is stubbed."""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for path in (REPO, REPO / "tools"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
