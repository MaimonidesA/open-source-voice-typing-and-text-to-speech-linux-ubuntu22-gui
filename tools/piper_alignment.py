"""Word timings for Piper voices, without extra dependencies.

Piper (VITS) voices decide how long every phoneme lasts, then generate audio
from those durations, so the durations are exact timings of the speech. The
published ONNX files just do not output them. with_duration_output() adds
that output by editing the model's protobuf directly (no onnx package), and
word_times_for() turns the per-phoneme timeline into per-word timings.
"""

from __future__ import annotations

import difflib
import re
from typing import Callable

# Stress/length marks, word gaps and punctuation: differ between a word said
# alone and in a sentence, and carry no timing of their own.
_ALIGN_SKIP = set("ˈˌːˑ \t\n,.!?;:—–-'\"()[]{}…¡¿«»")


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if byte < 0x80:
            return result, pos
        shift += 7


def _encode_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _proto_fields(buf: bytes, start: int, end: int):
    """(field_number, wire_type, value_start, value_end, field_start) of a
    protobuf message stored in buf[start:end]."""
    pos = start
    while pos < end:
        field_start = pos
        key, pos = _read_varint(buf, pos)
        number, wire = key >> 3, key & 7
        if wire == 0:
            _, following = _read_varint(buf, pos)
        elif wire == 1:
            following = pos + 8
        elif wire == 2:
            length, pos = _read_varint(buf, pos)
            following = pos + length
        elif wire == 5:
            following = pos + 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        yield number, wire, pos, following, field_start
        pos = following


def with_duration_output(model: bytes) -> bytes | None:
    """Add the phoneme-duration tensor (the graph's only Ceil output) as a
    model output, like piper's patch_voice_with_alignment but without the
    onnx package: ModelProto.graph (7) gets one more GraphProto.output (12).
    Returns None when the graph does not look like a Piper voice."""
    graph = None
    for number, wire, start, end, field_start in _proto_fields(model, 0, len(model)):
        if number == 7 and wire == 2:
            graph = (field_start, start, end)
    if graph is None:
        return None
    field_start, graph_start, graph_end = graph
    ceil_outputs: set[bytes] = set()
    existing: set[bytes] = set()
    for number, wire, start, end, _ in _proto_fields(model, graph_start, graph_end):
        if number == 1 and wire == 2:  # NodeProto: output (2), op_type (4)
            op_type, outputs = b"", []
            for n2, w2, s2, e2, _ in _proto_fields(model, start, end):
                if n2 == 4 and w2 == 2:
                    op_type = model[s2:e2]
                elif n2 == 2 and w2 == 2:
                    outputs.append(model[s2:e2])
            if op_type == b"Ceil":
                ceil_outputs.update(outputs)
        elif number == 12 and wire == 2:  # ValueInfoProto: name (1)
            for n2, w2, s2, e2, _ in _proto_fields(model, start, end):
                if n2 == 1 and w2 == 2:
                    existing.add(model[s2:e2])
    if len(ceil_outputs) != 1:
        return None
    name = next(iter(ceil_outputs))
    if name in existing:
        return model
    value_info = b"\x0a" + _encode_varint(len(name)) + name
    graph_bytes = model[graph_start:graph_end] + b"\x62" + _encode_varint(len(value_info)) + value_info
    return model[:field_start] + b"\x3a" + _encode_varint(len(graph_bytes)) + graph_bytes + model[graph_end:]


def word_times_for(
    phonemize: Callable[[str], list[list[str]]],
    text: str,
    timeline: list[tuple[str, float, float]],
) -> list[list[float]] | None:
    """Start/end seconds of every whitespace-separated word of text.

    timeline is the spoken phoneme sequence with times. Each word is
    phonemized on its own and matched against it, which survives words the
    phonemizer expands ("2026", "3:45") or runs together. Words with no
    sound of their own ("—") take the time of their neighbours.
    """
    words = [m.group() for m in re.finditer(r"\S+", text)]
    spoken = [(ph, start, end) for ph, start, end in timeline if ph not in _ALIGN_SKIP]
    if not words or not spoken:
        return None
    word_phonemes: list[str] = []
    owners: list[int] = []
    for index, word in enumerate(words):
        for sentence in phonemize(word):
            for phoneme in sentence:
                if phoneme not in _ALIGN_SKIP:
                    word_phonemes.append(phoneme)
                    owners.append(index)
    times: list[list[float | None]] = [[None, None] for _ in words]
    matcher = difflib.SequenceMatcher(None, [ph for ph, _, _ in spoken], word_phonemes, autojunk=False)
    for a, b, size in matcher.get_matching_blocks():
        for k in range(size):
            _, start, end = spoken[a + k]
            slot = times[owners[b + k]]
            slot[0] = start if slot[0] is None else min(slot[0], start)
            slot[1] = end if slot[1] is None else max(slot[1], end)
    result: list[list[float]] = []
    previous_end = 0.0
    for index, (start, end) in enumerate(times):
        if start is None:
            following = next((t[0] for t in times[index + 1 :] if t[0] is not None), None)
            start = previous_end if following is None else min(previous_end, following)
            end = start
        start = max(start, result[-1][0] if result else 0.0)
        end = max(end if end is not None else start, start)
        result.append([round(start, 3), round(end, 3)])
        previous_end = end
    return result
