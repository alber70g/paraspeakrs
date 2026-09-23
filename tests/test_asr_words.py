from paraspeakrs.asr import _extract_words


class FakeResult:
    """Shape sherpa-onnx returns for a NeMo transducer: subword tokens, empty words."""

    def __init__(self, tokens, timestamps, durations, words=None):
        self.tokens = tokens
        self.timestamps = timestamps
        self.durations = durations
        self.words = words if words is not None else []


def test_subword_tokens_are_joined_into_words():
    result = FakeResult(
        tokens=[" Ik", " had", " die", " I", "B", " g", "eda", "an"],
        timestamps=[1.52, 1.76, 2.00, 2.32, 2.48, 3.20, 3.28, 3.36],
        durations=[0.24, 0.24, 0.32, 0.16, 0.24, 0.08, 0.08, 0.16],
    )

    words = _extract_words(result)

    assert [word.word for word in words] == ["Ik", "had", "die", "IB", "gedaan"]
    # a word spans from its first token's start to its last token's end
    assert words[3].start == 2.32
    assert words[3].end == 2.48 + 0.24


def test_words_field_is_ignored_when_empty():
    """Regression: the old code keyed off result.words and returned nothing."""
    result = FakeResult(tokens=[" hi"], timestamps=[0.5], durations=[0.2], words=[])

    assert [word.word for word in _extract_words(result)] == ["hi"]


def test_missing_durations_gives_zero_length_words():
    result = FakeResult(tokens=[" a", " b"], timestamps=[0.0, 1.0], durations=None)

    words = _extract_words(result)

    assert [(word.start, word.end) for word in words] == [(0.0, 0.0), (1.0, 1.0)]


def test_no_tokens_returns_no_words():
    assert _extract_words(FakeResult(tokens=[], timestamps=[], durations=[])) == []
