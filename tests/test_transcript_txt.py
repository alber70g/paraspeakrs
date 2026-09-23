import re

from paraspeakrs.models import (
    TranscriptionResult,
    TranscriptSegment,
    Word,
)
from paraspeakrs.transcript_txt import render_frontmatter, render_txt, utterances

LINE_RE = re.compile(r"^\[\d\d:\d\d:\d\d\] [^:]+: .+$")


def _result(segments):
    return TranscriptionResult(
        job_id="t",
        duration_seconds=segments[-1].end if segments else 0.0,
        num_speakers=len({s.speaker for s in segments}),
        segments=segments,
    )


def test_empty_result_returns_empty_string():
    assert render_txt(_result([])) == ""


def test_no_words_splits_on_period_and_prorates():
    segment = TranscriptSegment(
        speaker="SPEAKER_01",
        start=10.0,
        end=20.0,
        text="One. Two. Three.",
    )
    out = render_txt(_result([segment]))
    lines = out.strip().splitlines()

    assert len(lines) == 3
    for line in lines:
        assert LINE_RE.match(line), line
    assert lines[0].startswith("[00:00:10]")
    assert lines[0].endswith(": One.")
    assert "Two." in lines[1]
    assert "Three." in lines[2]


def test_resolved_label_overrides_speaker_id():
    segment = TranscriptSegment(
        speaker="SPEAKER_01",
        resolved_label="Albert",
        start=0.0,
        end=4.0,
        text="Hi.",
    )
    out = render_txt(_result([segment])).strip()
    assert "Albert:" in out
    assert "SPEAKER_01" not in out


def test_words_split_on_sentence_end():
    words = [
        Word(word="Hello.", start=0.0, end=0.5, speaker="SPEAKER_01"),
        Word(word="World", start=0.6, end=1.0, speaker="SPEAKER_01"),
        Word(word="now.", start=1.0, end=1.4, speaker="SPEAKER_01"),
    ]
    segment = TranscriptSegment(
        speaker="SPEAKER_01",
        start=0.0,
        end=1.4,
        text="Hello. World now.",
        words=words,
    )
    out = render_txt(_result([segment])).strip().splitlines()
    assert len(out) == 2
    assert out[0].endswith(": Hello.")
    assert out[1].endswith(": World now.")


def test_words_split_on_silence():
    base = [
        Word(word=f"w{i}", start=i * 0.4, end=i * 0.4 + 0.3, speaker="SPEAKER_01")
        for i in range(8)
    ]
    big_gap = Word(word="later", start=10.0, end=10.3, speaker="SPEAKER_01")
    follow = Word(word="more", start=10.4, end=10.7, speaker="SPEAKER_01")
    words = base + [big_gap, follow]
    segment = TranscriptSegment(
        speaker="SPEAKER_01",
        start=0.0,
        end=10.7,
        text="ignored",
        words=words,
    )

    out = render_txt(_result([segment])).strip().splitlines()
    assert len(out) == 2
    assert "later more" in out[1]


def test_speaker_change_resets_silence_history():
    a_words = [
        Word(word=f"a{i}", start=i * 0.3, end=i * 0.3 + 0.25, speaker="SPEAKER_01")
        for i in range(8)
    ]
    seg_a = TranscriptSegment(
        speaker="SPEAKER_01",
        start=0.0,
        end=a_words[-1].end,
        text="ignored",
        words=a_words,
    )
    b_words = [
        Word(word="x", start=a_words[-1].end + 0.7, end=a_words[-1].end + 0.95, speaker="SPEAKER_02"),
        Word(word="y", start=a_words[-1].end + 1.0, end=a_words[-1].end + 1.3, speaker="SPEAKER_02"),
    ]
    seg_b = TranscriptSegment(
        speaker="SPEAKER_02",
        start=b_words[0].start,
        end=b_words[-1].end,
        text="ignored",
        words=b_words,
    )

    out = render_txt(_result([seg_a, seg_b])).strip().splitlines()
    speaker_b_lines = [line for line in out if "SPEAKER_02" in line]
    assert len(speaker_b_lines) == 1
    assert "x y" in speaker_b_lines[0]


def test_timestamp_zero_padding():
    segment = TranscriptSegment(
        speaker="A",
        start=3725.0,
        end=3726.0,
        text="Tick.",
    )
    out = render_txt(_result([segment])).strip()
    assert out.startswith("[01:02:05]")


def _meeting():
    """Two speakers, several lines each - the shape removal works on."""
    def seg(speaker, words):
        return TranscriptSegment(
            speaker=speaker,
            start=words[0].start,
            end=words[-1].end,
            text=" ".join(w.word for w in words),
            words=words,
        )

    return _result([
        seg("ME", [Word(word="Morning.", start=0.0, end=0.4), Word(word="Agenda.", start=0.6, end=1.0)]),
        seg("KID", [Word(word="Mama!", start=1.5, end=1.9), Word(word="Juice.", start=2.1, end=2.5)]),
        seg("ME", [Word(word="Sorry.", start=3.0, end=3.4)]),
    ])


def test_removing_a_speaker_drops_every_line_of_theirs_and_nothing_else():
    """A child talking through a call is not part of the meeting; everyone
    else's words have to survive untouched."""
    full = render_txt(_meeting()).splitlines()
    out = render_txt(_meeting(), excluded_speakers={"KID"}).splitlines()

    assert out == [line for line in full if "KID" not in line]
    assert len(out) == 3


def test_removing_one_line_leaves_the_other_lines_exactly_as_they_were():
    """Lines are found again by (speaker, start). If removing one moved the
    boundaries of the rest, every other removal would silently miss."""
    lines = utterances(_meeting())
    juice = next(u for u in lines if u.text == "Juice.")

    out = render_txt(_meeting(), excluded_lines={juice.key}).splitlines()

    assert out == [line for line in render_txt(_meeting()).splitlines() if "Juice." not in line]


def test_a_line_key_matches_despite_float_noise_from_storage():
    """Keys round-trip through JSON; a start of 2.1000000001 is still line 2.1."""
    out = render_txt(_meeting(), excluded_lines={("KID", 2.1000000001)})
    assert "Juice." not in out
    assert "Mama!" in out


def test_frontmatter_names_the_meeting_even_without_a_note():
    out = render_frontmatter("", title="standup: day 2", date="2026-09-23", speakers=["Albert", "Jan"])
    assert out == '---\ntitle: "standup: day 2"\ndate: "2026-09-23"\nspeakers: ["Albert", "Jan"]\n---\n\n'


def test_frontmatter_with_nothing_known_is_empty():
    assert render_frontmatter("") == ""
