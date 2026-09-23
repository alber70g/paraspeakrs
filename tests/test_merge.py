from paraspeakrs.merge import dominant_speaker, merge_asr_with_diarization
from paraspeakrs.models import AsrSegment, DiarizationSegment, Word


def test_dominant_speaker_returns_unknown_for_diarization_gap():
    speaker, confidence = dominant_speaker(
        10.0,
        11.0,
        [DiarizationSegment(speaker="SPEAKER_00", start=0.0, end=5.0)],
    )

    assert speaker == "UNKNOWN"
    assert confidence == 0.0


def test_merge_keeps_words_when_diarization_misses_part():
    asr = AsrSegment(
        text="hello gap",
        start=0.0,
        end=2.0,
        words=[
            Word(word="hello", start=0.1, end=0.5),
            Word(word="gap", start=1.5, end=1.9),
        ],
    )
    diarization = [DiarizationSegment(speaker="SPEAKER_00", start=0.0, end=1.0)]

    segments, warnings = merge_asr_with_diarization([asr], diarization)

    assert warnings == []
    assert [word.word for segment in segments for word in segment.words] == ["hello", "gap"]
    assert segments[0].speaker == "SPEAKER_00"
    assert segments[1].speaker == "UNKNOWN"


def test_merge_assigns_chunk_text_when_words_unavailable():
    asr = AsrSegment(text="chunk text", start=0.0, end=4.0)
    diarization = [DiarizationSegment(speaker="SPEAKER_01", start=1.0, end=4.0)]

    segments, warnings = merge_asr_with_diarization([asr], diarization)

    assert segments[0].text == "chunk text"
    assert segments[0].speaker == "SPEAKER_01"
    assert segments[0].speaker_confidence == 0.75
    assert warnings == ["ASR word timestamps unavailable; assigned chunk text to dominant speaker"]


def test_merge_keeps_fluent_speech_on_one_speaker():
    """A diarization edge inside continuous speech must not flip a word or two.

    Senko places boundaries at subsegment midpoints, so a short island of the
    other speaker routinely lands mid-sentence. Without a pause there was no turn
    change, so every word here belongs to the speaker holding the floor.
    """
    asr = AsrSegment(
        text="dat is wel ingewikkeld ja",
        start=0.0,
        end=2.0,
        words=[
            Word(word="dat", start=0.00, end=0.30),
            Word(word="is", start=0.30, end=0.60),
            Word(word="wel", start=0.60, end=0.90),
            Word(word="ingewikkeld", start=0.90, end=1.40),
            Word(word="ja", start=1.40, end=1.80),
        ],
    )
    diarization = [
        DiarizationSegment(speaker="SPEAKER_01", start=0.0, end=0.85),
        DiarizationSegment(speaker="SPEAKER_02", start=0.85, end=1.05),
        DiarizationSegment(speaker="SPEAKER_01", start=1.05, end=2.0),
    ]

    segments, _ = merge_asr_with_diarization([asr], diarization)

    assert [segment.speaker for segment in segments] == ["SPEAKER_01"]
    assert segments[0].text == "dat is wel ingewikkeld ja"


def test_merge_still_switches_speaker_after_a_pause():
    """The pause is what licenses a turn change, so a real one must survive."""
    asr = AsrSegment(
        text="precies zeker niet",
        start=0.0,
        end=2.0,
        words=[
            Word(word="precies", start=0.00, end=0.40),
            Word(word="zeker", start=0.90, end=1.20),
            Word(word="niet", start=1.20, end=1.50),
        ],
    )
    diarization = [
        DiarizationSegment(speaker="SPEAKER_01", start=0.0, end=0.70),
        DiarizationSegment(speaker="SPEAKER_02", start=0.70, end=2.0),
    ]

    segments, _ = merge_asr_with_diarization([asr], diarization)

    assert [segment.speaker for segment in segments] == ["SPEAKER_01", "SPEAKER_02"]
    assert [segment.text for segment in segments] == ["precies", "zeker niet"]


def test_merge_ignores_a_silent_chunk():
    """A chunk with no speech is not a word-timestamp failure and must not warn."""
    silent = AsrSegment(text="", start=0.0, end=16.0)
    spoken = AsrSegment(
        text="hallo",
        start=16.0,
        end=17.0,
        words=[Word(word="hallo", start=16.0, end=16.5)],
    )
    diarization = [DiarizationSegment(speaker="SPEAKER_01", start=16.0, end=17.0)]

    segments, warnings = merge_asr_with_diarization([silent, spoken], diarization)

    assert warnings == []
    assert [segment.text for segment in segments] == ["hallo"]
