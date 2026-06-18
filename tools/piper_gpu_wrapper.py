#!/usr/bin/env python3
"""Small Piper-compatible CLI that forces ONNX Runtime CUDA.

This intentionally implements only the flags used by voice_reading.py.
It is launched from an isolated virtualenv created by
scripts/setup_piper_gpu_runtime.sh.
"""

from __future__ import annotations

import argparse
import sys
import time
import wave
from pathlib import Path

from piper import PiperVoice, SynthesisConfig


def main() -> int:
    parser = argparse.ArgumentParser(description="Piper CUDA wrapper")
    parser.add_argument("-m", "--model", required=True)
    parser.add_argument("-c", "--config")
    parser.add_argument("-f", "--output_file", "--output-file", required=True)
    parser.add_argument("--length_scale", "--length-scale", type=float)
    parser.add_argument("--noise_scale", "--noise-scale", type=float)
    parser.add_argument("--noise_w", "--noise-w", "--noise_w_scale", "--noise-w-scale", type=float)
    parser.add_argument("--sentence_silence", "--sentence-silence", type=float, default=0.2)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("-q", "--quiet", action="store_true")
    args = parser.parse_args()

    text = sys.stdin.read().strip()
    if not text:
        return 0

    started = time.monotonic()
    voice = PiperVoice.load(args.model, config_path=args.config, use_cuda=True)
    synth = SynthesisConfig(
        length_scale=args.length_scale,
        noise_scale=args.noise_scale,
        noise_w_scale=args.noise_w,
    )

    out_path = Path(args.output_file)
    silence = b""
    wrote_any = False
    infer_total = 0.0
    audio_total = 0.0

    with wave.open(str(out_path), "wb") as wav_file:
        audio_iter = iter(voice.synthesize(text, syn_config=synth))
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
                silence = bytes(int(chunk.sample_rate * args.sentence_silence * chunk.sample_width))
            elif silence:
                wav_file.writeframes(silence)

            audio = chunk.audio_int16_bytes
            audio_sec = len(audio) / float(chunk.sample_rate * chunk.sample_width * chunk.sample_channels)
            infer_total += infer_sec
            audio_total += audio_sec
            wav_file.writeframes(audio)
            wrote_any = True
            index += 1

            if args.debug and not args.quiet:
                print(
                    f"Synthesized {audio_sec} second(s) of audio in {infer_sec} second(s)",
                    file=sys.stderr,
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
