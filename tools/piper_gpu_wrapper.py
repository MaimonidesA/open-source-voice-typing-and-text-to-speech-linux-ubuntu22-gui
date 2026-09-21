#!/usr/bin/env python3
"""Small Piper-compatible CLI that forces ONNX Runtime CUDA.

This intentionally implements only the flags used by voice_reading.py.
It is launched from an isolated virtualenv created by
scripts/setup_piper_gpu_runtime.sh.

With --json-input it stays running like the C++ piper: each stdin line is
{"text": ..., "output_file": ...}; the path of every finished WAV is printed
on its own stdout line. Loading the CUDA model takes ~2.5s, so the reader
keeps one process warm instead of paying that per passage.

A request with "alignments": true is answered with a JSON line instead:
{"output_file": ..., "word_times": [[start, end], ...]}, one pair of seconds
per whitespace-separated word of the text. The times come from the model's
own phoneme durations, which Piper voices compute but do not export; the
model is patched in memory to expose them (see piper_alignment.py).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import wave
from pathlib import Path

import onnxruntime
from piper import PiperVoice, SynthesisConfig
from piper.config import PiperConfig
from piper.voice import ESPEAK_DATA_DIR

from piper_alignment import with_duration_output, word_times_for


def load_voice(model_path: str, config_path: str | None, alignments: bool) -> PiperVoice:
    if not alignments:
        return PiperVoice.load(model_path, config_path=config_path, use_cuda=True)
    patched = with_duration_output(Path(model_path).read_bytes())
    if patched is None:
        return PiperVoice.load(model_path, config_path=config_path, use_cuda=True)
    with open(config_path or f"{model_path}.json", "r", encoding="utf-8") as config_file:
        config = PiperConfig.from_dict(json.load(config_file))
    session = onnxruntime.InferenceSession(
        patched,
        sess_options=onnxruntime.SessionOptions(),
        providers=[("CUDAExecutionProvider", {"cudnn_conv_algo_search": "HEURISTIC"})],
    )
    return PiperVoice(config=config, session=session, espeak_data_dir=Path(ESPEAK_DATA_DIR), download_dir=Path.cwd())


def synthesize_to_wav(
    voice: PiperVoice,
    synth: SynthesisConfig,
    text: str,
    out_path: Path,
    sentence_silence: float,
    debug: bool,
    timeline: list[tuple[str, float, float]] | None = None,
) -> tuple[bool, float, float]:
    """Write text to out_path; returns (wrote_any, audio_sec, infer_sec).

    With a timeline list, the spoken phonemes are appended to it as
    (phoneme, start_sec, end_sec) when the voice reports alignments."""
    silence = b""
    wrote_any = False
    infer_total = 0.0
    audio_total = 0.0
    written_samples = 0

    with wave.open(str(out_path), "wb") as wav_file:
        audio_iter = iter(voice.synthesize(text, syn_config=synth, include_alignments=timeline is not None))
        index = 0
        while True:
            before = time.monotonic()
            try:
                chunk = next(audio_iter)
            except StopIteration:
                break
            infer_sec = max(0.0, time.monotonic() - before)
            if index == 0:
                wav_file.setframerate(chunk.sample_rate)
                wav_file.setsampwidth(chunk.sample_width)
                wav_file.setnchannels(chunk.sample_channels)
                silence = bytes(int(chunk.sample_rate * sentence_silence * chunk.sample_width))
            elif silence:
                wav_file.writeframes(silence)
                written_samples += len(silence) // chunk.sample_width

            if timeline is not None and chunk.phoneme_alignments:
                position = written_samples
                for alignment in chunk.phoneme_alignments:
                    if alignment.phoneme not in ("^", "$"):
                        timeline.append(
                            (
                                alignment.phoneme,
                                position / chunk.sample_rate,
                                (position + alignment.num_samples) / chunk.sample_rate,
                            )
                        )
                    position += alignment.num_samples

            audio = chunk.audio_int16_bytes
            written_samples += len(audio) // (chunk.sample_width * chunk.sample_channels)
            audio_sec = len(audio) / float(chunk.sample_rate * chunk.sample_width * chunk.sample_channels)
            infer_total += infer_sec
            audio_total += audio_sec
            wav_file.writeframes(audio)
            wrote_any = True
            index += 1

            if debug:
                print(
                    f"Synthesized {audio_sec} second(s) of audio in {infer_sec} second(s)",
                    file=sys.stderr,
                )
        if not wrote_any:
            # Text with nothing to pronounce (e.g. "---"): a valid, empty WAV
            # instead of wave's "# channels not specified" on close.
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(int(getattr(voice.config, "sample_rate", 22050) or 22050))
    return wrote_any, audio_total, infer_total


def serve_json_lines(voice: PiperVoice, synth: SynthesisConfig, args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir) if args.output_dir else Path.cwd()
    counter = 0
    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            request = json.loads(raw_line)
            text = str(request.get("text", "")).strip()
            output_file = str(request.get("output_file", "")).strip()
            want_alignments = bool(request.get("alignments", False))
        except Exception as exc:
            print(f"ERROR: invalid JSON line ({exc})", flush=True)
            continue
        counter += 1
        out_path = Path(output_file) if output_file else output_dir / f"{int(time.time() * 1000)}_{counter}.wav"
        if not text:
            print(f"ERROR: empty text for {out_path}", flush=True)
            continue
        timeline: list[tuple[str, float, float]] | None = [] if want_alignments else None
        try:
            synthesize_to_wav(
                voice, synth, text, out_path, args.sentence_silence, args.debug and not args.quiet, timeline
            )
        except Exception as exc:
            print(f"ERROR: {exc}", flush=True)
            continue
        if not want_alignments:
            print(str(out_path), flush=True)
            continue
        try:
            word_times = word_times_for(voice.phonemize, text, timeline or [])
        except Exception:
            word_times = None
        print(json.dumps({"output_file": str(out_path), "word_times": word_times}), flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Piper CUDA wrapper")
    parser.add_argument("-m", "--model", required=True)
    parser.add_argument("-c", "--config")
    parser.add_argument("-f", "--output_file", "--output-file")
    parser.add_argument("-d", "--output_dir", "--output-dir")
    parser.add_argument("--json-input", "--json_input", action="store_true")
    parser.add_argument("--length_scale", "--length-scale", type=float)
    parser.add_argument("--noise_scale", "--noise-scale", type=float)
    parser.add_argument("--noise_w", "--noise-w", "--noise_w_scale", "--noise-w-scale", type=float)
    parser.add_argument("--sentence_silence", "--sentence-silence", type=float, default=0.2)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("-q", "--quiet", action="store_true")
    args = parser.parse_args()

    if not args.json_input and not args.output_file:
        parser.error("--output_file is required unless --json-input is used")

    text = ""
    if not args.json_input:
        text = sys.stdin.read().strip()
        if not text:
            return 0

    started = time.monotonic()
    voice = load_voice(args.model, args.config, alignments=args.json_input)
    synth = SynthesisConfig(
        length_scale=args.length_scale,
        noise_scale=args.noise_scale,
        noise_w_scale=args.noise_w,
    )

    if args.json_input:
        return serve_json_lines(voice, synth, args)

    wrote_any, audio_total, infer_total = synthesize_to_wav(
        voice,
        synth,
        text,
        Path(args.output_file),
        args.sentence_silence,
        args.debug and not args.quiet,
    )

    if args.debug and not args.quiet and wrote_any:
        elapsed = max(0.001, time.monotonic() - started)
        print(
            f"Real-time factor: {elapsed / max(0.001, audio_total)} "
            f"(infer={infer_total} sec, audio={audio_total} sec)",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
