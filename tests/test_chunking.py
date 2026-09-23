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
