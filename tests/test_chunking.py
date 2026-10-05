from paraspeakrs.chunking import build_chunks
from paraspeakrs.models import DiarizationSegment


def test_build_chunks_covers_diarization_gaps_before_and_after():
    chunks = build_chunks(
        [DiarizationSegment(speaker="SPEAKER_00", start=10.0, end=20.0)],
        duration_seconds=30.0,
        target_seconds=90.0,
        overlap_seconds=2.0,
    )

    assert chunks[0].start == 0.0
    assert chunks[0].end == 10.0
    assert chunks[-1].end == 30.0


def test_build_chunks_falls_back_to_fixed_chunks_without_diarization():
    chunks = build_chunks([], duration_seconds=200.0, target_seconds=90.0, overlap_seconds=2.0)

    assert chunks[0].start == 0.0
    assert chunks[0].end == 90.0
    assert chunks[1].start == 88.0


def _segment(start: float, end: float) -> DiarizationSegment:
    return DiarizationSegment(speaker="SPEAKER_00", start=start, end=end)


def _assert_capped_and_covering(chunks, duration, target):
    # ASR memory grows quadratically with chunk length (full self-attention): a
    # 400 s chunk peaked at 8.6 GB against 2.5 GB for 90 s, and longer windows
    # also drop whole phrases. The target is a ceiling, not a hint.
    assert max(chunk.end - chunk.start for chunk in chunks) <= target
    assert chunks[0].start == 0.0
    assert chunks[-1].end == duration
    assert all(nxt.start <= cur.end for cur, nxt in zip(chunks, chunks[1:])), "gap in coverage"


def test_build_chunks_caps_a_long_silent_tail():
    # A meeting whose recording ran on for minutes after everyone stopped talking.
    chunks = build_chunks([_segment(0.0, 60.0)], duration_seconds=460.0, target_seconds=90.0, overlap_seconds=2.0)

    _assert_capped_and_covering(chunks, 460.0, 90.0)


def test_build_chunks_caps_a_long_silent_lead_in():
    chunks = build_chunks([_segment(400.0, 460.0)], duration_seconds=460.0, target_seconds=90.0, overlap_seconds=2.0)

    _assert_capped_and_covering(chunks, 460.0, 90.0)


def test_build_chunks_caps_a_single_long_monologue():
    chunks = build_chunks([_segment(0.0, 400.0)], duration_seconds=400.0, target_seconds=90.0, overlap_seconds=2.0)

    _assert_capped_and_covering(chunks, 400.0, 90.0)


def test_build_chunks_caps_a_long_gap_between_turns():
    chunks = build_chunks(
        [_segment(0.0, 60.0), _segment(400.0, 460.0)],
        duration_seconds=460.0,
        target_seconds=90.0,
        overlap_seconds=2.0,
    )

    _assert_capped_and_covering(chunks, 460.0, 90.0)
