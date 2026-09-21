"""ReaderController sessions: passages fed while earlier ones play."""

import json
import os
import shutil
import tempfile
import time
import wave
from pathlib import Path

import pytest

import voice_reading as vr

REPO = Path(__file__).resolve().parents[1]


def write_wav(path: str, seconds: float = 0.2) -> None:
    with wave.open(path, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(b"\x00\x00" * int(16000 * seconds))


def fake_synth(session, text):
    path = tempfile.mktemp(suffix=".wav")
    write_wav(path)
    time.sleep(0.03)
    return path, 0.2, "", None


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(vr, "notify", lambda *a, **k: None)
    monkeypatch.setattr(vr, "log_line", lambda message: None)


@pytest.fixture
def make(tmp_path, monkeypatch):
    def _make(config=None, play_seconds=0.25, synth=fake_synth):
        cfg = {"engine": "piper", "piper_model": "/fake/en_GB-x-high.onnx", "language": "en", "highlight_delay_seconds": 0}
        cfg.update(config or {})
        path = tmp_path / f"reading_{time.monotonic_ns()}.json"
        path.write_text(json.dumps(cfg))
        events = []
        controller = vr.ReaderController(
            vr.ConfigManager(path),
            on_state_change=lambda speaking, reason: events.append(("state", speaking, reason)),
            on_passage=lambda pid, word, total: events.append(("passage", pid, word, total)),
        )
        if synth is not None:
            original = controller._resolve_speech_settings

            def settings(language):
                resolved = original(language)
                resolved.update(engine="piper", model="/fake/model.onnx", binary="/fake/piper", device="")
                return resolved

            controller._resolve_speech_settings = settings
            controller._synthesize_passage = synth
        monkeypatch.setattr(vr, "list_audio_player_commands", lambda wav, device="": [["sleep", str(play_seconds)]])
        return controller, events

    return _make


def wait_for(events, reason, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if any(e[0] == "state" and e[2] == reason for e in events):
            return True
        time.sleep(0.02)
    return False


def played(events):
    ids = []
    for e in events:
        if e[0] == "passage" and e[2] == 0 and (not ids or ids[-1] != e[1]):
            ids.append(e[1])
    return ids


def test_passage_cut_prefers_sentence_and_paragraph_ends():
    assert vr.next_passage_cut("Hello world. This is two.", 220, 1) == (12, True)
    assert vr.next_passage_cut("Para one line.\n\nPara two.", 220, 160) == (15, True)
    assert vr.next_passage_cut("Still typing this", 220, 1) == (17, False)
    cut, complete = vr.next_passage_cut("no punctuation " * 10, 40, 1)
    assert cut <= 40 and complete


def test_reads_all_passages_in_order(make):
    controller, events = make()
    sid = controller.begin_document_session()
    for pid in (1, 2, 3):
        controller.enqueue_passage(sid, pid, f"Passage {pid}.")
    controller.set_session_exhausted(sid, True)
    assert wait_for(events, "done")
    assert played(events) == [1, 2, 3]
    assert not controller.is_speaking() and controller.active_session_id() is None


def test_text_pasted_while_reading_continues(make):
    controller, events = make(play_seconds=0.4)
    sid = controller.begin_document_session()
    controller.enqueue_passage(sid, 1, "First.")
    controller.set_session_exhausted(sid, True)
    time.sleep(0.25)
    assert controller.is_speaking()
    controller.enqueue_passage(sid, 2, "Pasted later.")
    controller.set_session_exhausted(sid, True)
    assert wait_for(events, "done")
    assert played(events) == [1, 2]


def test_pause_resume_and_stop(make):
    controller, events = make(play_seconds=0.6)
    sid = controller.begin_document_session()
    for pid in (1, 2):
        controller.enqueue_passage(sid, pid, f"P{pid}.")
    controller.set_session_exhausted(sid, True)
    time.sleep(0.3)
    assert controller.pause()[0] and controller.is_paused()
    time.sleep(0.7)
    assert played(events) == [1]
    assert controller.resume()[0]
    assert wait_for(events, "done") and played(events) == [1, 2]

    controller, events = make(play_seconds=1.0)
    sid = controller.begin_document_session()
    for pid in (1, 2, 3):
        controller.enqueue_passage(sid, pid, f"P{pid}.")
    time.sleep(0.3)
    assert controller.stop() == (True, "Stopped")
    time.sleep(0.4)
    assert played(events) == [1] and wait_for(events, "stopped", 1)


def test_edited_passages_are_dropped_but_not_the_playing_one(make):
    controller, _ = make(play_seconds=0.5)
    sid = controller.begin_document_session()
    for pid in (1, 2, 3, 4):
        controller.enqueue_passage(sid, pid, f"P{pid}.")
    time.sleep(0.2)
    assert controller.truncate_session(sid, 3) == [3, 4]
    assert 1 not in controller.truncate_session(sid, 1)
    controller.stop()


def test_lookahead_is_bounded(make):
    controller, _ = make(play_seconds=2.0)
    sid = controller.begin_document_session()
    count = 0
    while controller.session_wants_more(sid) and count < 50:
        count += 1
        controller.enqueue_passage(sid, count, "x" * 100)
    assert count == vr.READER_LOOKAHEAD_PASSAGES
    controller.stop()


def test_hotkey_reads_go_through_the_reader_window(make, monkeypatch):
    controller, _ = make()
    hosted = []
    controller.document_host = lambda text, source, language, sid: hosted.append((text, source, language, sid))
    monkeypatch.setattr(vr, "get_selected_text", lambda config: ("Selected words.", "primary"))
    assert controller.read_selection_async()[0]
    assert hosted[-1][:2] == ("Selected words.", "primary") and hosted[-1][3] == controller.active_session_id()
    assert controller.speak_text_async("שלום", language="Hebrew")[0]
    assert hosted[-1][1:3] == ("translation", "Hebrew")
    assert controller.toggle() == (True, "Stopped")


def test_without_a_window_the_whole_text_is_read(make, monkeypatch):
    controller, events = make()
    monkeypatch.setattr(vr, "get_selected_text", lambda config: ("One. Two. Three.", "primary"))
    assert controller.read_selection_async()[0]
    assert wait_for(events, "done")
    assert len(played(events)) >= 2


def test_failures(make):
    controller, events = make(synth=lambda session, text: ("", 0.0, "boom", None))
    sid = controller.begin_document_session()
    controller.enqueue_passage(sid, 1, "Bad.")
    controller.set_session_exhausted(sid, True)
    assert wait_for(events, "error")

    def flaky(session, text):
        return ("", 0.0, "boom", None) if "bad" in text else fake_synth(session, text)

    controller, events = make(synth=flaky)
    sid = controller.begin_document_session()
    for pid, text in ((1, "Good."), (2, "bad"), (3, "Good again.")):
        controller.enqueue_passage(sid, pid, text)
    controller.set_session_exhausted(sid, True)
    assert wait_for(events, "done") and 3 in played(events)


def test_highlight_follows_exact_word_times(make):
    exact = [[0.0, 0.1], [0.3, 0.4], [0.6, 0.7], [0.9, 1.0]]

    def timed(session, text):
        path = tempfile.mktemp(suffix=".wav")
        write_wav(path, 1.1)
        return path, 1.1, "", exact

    controller, events = make(play_seconds=1.2, synth=timed)
    stamps = []
    controller.on_passage = lambda pid, word, total: stamps.append((time.monotonic(), word))
    sid = controller.begin_document_session()
    controller.enqueue_passage(sid, 1, "one two three four")
    controller.set_session_exhausted(sid, True)
    assert wait_for(events, "done")
    start = stamps[0][0]
    assert [word for _, word in stamps] == [0, 1, 2, 3]
    assert all(abs((t - start) - exact[word][0]) < 0.06 for t, word in stamps)


def _voice_model() -> str:
    for path in sorted((REPO / "piper_voices").rglob("en_*.onnx")):
        return str(path)
    return ""


@pytest.mark.skipif(not _voice_model(), reason="no Piper voice in piper_voices/")
@pytest.mark.parametrize("gpu", [False, True])
def test_real_piper_synthesis(make, gpu):
    gpu_binary = str(Path.home() / "piper-gpu" / "bin" / "piper")
    if gpu and not os.path.exists(gpu_binary):
        pytest.skip("GPU Piper runtime not installed")
    if not gpu and not (REPO / "piper_runtime" / "piper" / "piper").exists() and not shutil.which("piper"):
        pytest.skip("CPU Piper not installed")
    controller, _ = make(
        {"piper_model": _voice_model(), "piper_gpu_enabled": gpu, "piper_gpu_binary_path": gpu_binary},
        synth=None,
    )
    session = vr.ReadingSession(session_id=1, settings=controller._resolve_speech_settings(None))
    try:
        wav, duration, error, times = controller._synthesize_passage(session, "Meet me at 3:45, please.")
        assert not error and duration > 0.5 and os.path.exists(wav)
        if gpu:
            assert times is not None and len(times) == 5
            assert all(times[i][0] <= times[i + 1][0] for i in range(4))
        os.unlink(wav)
        silent = controller._synthesize_passage(session, "---")
        assert not silent[2]
        if silent[0]:
            os.unlink(silent[0])
    finally:
        controller.shutdown()
