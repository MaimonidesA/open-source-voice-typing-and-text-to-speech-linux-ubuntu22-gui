import piper_alignment as pa


def _field(number: int, payload: bytes) -> bytes:
    return pa._encode_varint((number << 3) | 2) + pa._encode_varint(len(payload)) + payload


def _model(ceil_output: bytes = b"/Ceil_output_0", outputs: tuple[bytes, ...] = (b"output",)) -> bytes:
    """A minimal ModelProto: ir_version, and a graph with a Ceil node."""
    node = _field(1, b"x") + _field(2, ceil_output) + _field(4, b"Ceil")
    other = _field(1, b"y") + _field(2, b"z") + _field(4, b"Exp")
    graph = _field(1, other) + _field(1, node) + _field(2, b"g") + b"".join(_field(12, _field(1, o)) for o in outputs)
    return b"\x08\x07" + _field(7, graph) + _field(14, b"meta")


def _graph_outputs(model: bytes) -> list[bytes]:
    names = []
    for number, _wire, start, end, _ in pa._proto_fields(model, 0, len(model)):
        if number == 7:
            for n2, _w2, s2, e2, _ in pa._proto_fields(model, start, end):
                if n2 == 12:
                    for n3, _w3, s3, e3, _ in pa._proto_fields(model, s2, e2):
                        if n3 == 1:
                            names.append(model[s3:e3])
    return names


def test_varint_round_trip():
    for value in (0, 1, 127, 128, 300, 2**31, 114219352):
        encoded = pa._encode_varint(value)
        assert pa._read_varint(encoded, 0) == (value, len(encoded))


def test_adds_duration_output_and_keeps_the_rest():
    model = _model()
    patched = pa.with_duration_output(model)
    assert _graph_outputs(patched) == [b"output", b"/Ceil_output_0"]
    assert patched.startswith(b"\x08\x07") and patched.endswith(_field(14, b"meta"))


def test_already_patched_model_is_unchanged():
    model = _model(outputs=(b"output", b"/Ceil_output_0"))
    assert pa.with_duration_output(model) == model


def test_non_piper_graph_is_left_alone():
    graph = _field(1, _field(2, b"z") + _field(4, b"Exp"))
    assert pa.with_duration_output(_field(7, graph)) is None


def test_word_times_follow_the_spoken_timeline():
    # Phonemes are letters here; "2" is expanded to "two" when spoken.
    spoken = "hi two you"
    timeline, t = [], 0.0
    for ch in spoken:
        timeline.append((ch, t, t + 0.1))
        t += 0.1

    def phonemize(word):
        return [list({"2": "two"}.get(word, word).lower())]

    times = pa.word_times_for(phonemize, "Hi 2 — you", timeline)
    assert [round(s, 1) for s, _ in times] == [0.0, 0.3, 0.6, 0.7]
    assert times[2][0] == times[2][1]  # "—" has no sound of its own
    assert all(times[i][0] <= times[i + 1][0] for i in range(len(times) - 1))
