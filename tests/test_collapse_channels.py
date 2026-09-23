from paraspeakrs.diarization import collapse_channels
from paraspeakrs.models import DiarizationSegment


def _seg(speaker, start, end):
    return DiarizationSegment(speaker=speaker, start=start, end=end)


SEGMENTS = [
    _seg("L_SPEAKER_00", 0.0, 9.0),
    _seg("R_SPEAKER_00", 1.0, 3.0),
    _seg("L_SPEAKER_01", 9.0, 10.0),
    _seg("R_SPEAKER_01", 3.0, 5.0),
]
EMBEDDINGS = {
    "L_SPEAKER_00": [1.0, 0.0],
    "L_SPEAKER_01": [0.0, 1.0],
    "R_SPEAKER_00": [5.0, 5.0],
    "R_SPEAKER_01": [6.0, 6.0],
}


def test_a_single_speaker_channel_ends_up_as_one_speaker():
    """The whole point: a person alone on their mic must not come back as two
    speakers the user then has to name twice."""
    segments, embeddings = collapse_channels(SEGMENTS, EMBEDDINGS, {"L"})
    assert {s.speaker for s in segments if s.speaker.startswith("L_")} == {"L_SPEAKER_00"}
    assert set(embeddings) == {"L_SPEAKER_00", "R_SPEAKER_00", "R_SPEAKER_01"}


def test_the_other_channel_is_still_diarized_as_found():
    """Only the channel the user vouched for is overridden; the far end of a
    call can still be several people."""
    segments, embeddings = collapse_channels(SEGMENTS, EMBEDDINGS, {"L"})
    assert [s.speaker for s in segments if s.speaker.startswith("R_")] == ["R_SPEAKER_00", "R_SPEAKER_01"]
    assert embeddings["R_SPEAKER_01"] == [6.0, 6.0]


def test_speech_timing_is_untouched():
    """Diarization still decides where speech is; only who is replaced."""
    segments, _ = collapse_channels(SEGMENTS, EMBEDDINGS, {"L", "R"})
    assert [(s.start, s.end) for s in segments] == [(s.start, s.end) for s in SEGMENTS]


def test_the_merged_voice_print_leans_on_the_speaker_who_talked_most():
    """A one-second stray cluster must not drag the voice print halfway to
    itself, or the cache stops recognising this person next time."""
    _, embeddings = collapse_channels(SEGMENTS, EMBEDDINGS, {"L"})
    assert embeddings["L_SPEAKER_00"] == [0.9, 0.1]


def test_no_channels_changes_nothing():
    assert collapse_channels(SEGMENTS, EMBEDDINGS, ()) == (SEGMENTS, EMBEDDINGS)
