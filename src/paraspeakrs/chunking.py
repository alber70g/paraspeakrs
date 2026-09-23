from __future__ import annotations

from dataclasses import dataclass

from .models import DiarizationSegment


@dataclass(frozen=True)
class Chunk:
    start: float
    end: float


def build_chunks(
    diarization: list[DiarizationSegment],
    duration_seconds: float,
    target_seconds: float,
    overlap_seconds: float,
) -> list[Chunk]:
    if duration_seconds <= 0:
        return []
    if not diarization:
        return _fixed_chunks(duration_seconds, target_seconds, overlap_seconds)

    chunks: list[Chunk] = []
    start = max(0.0, diarization[0].start)
    end = start
    for segment in diarization:
        would_exceed = segment.end - start > target_seconds and end > start
        if would_exceed:
            chunks.append(_bounded_chunk(start, end, duration_seconds))
            start = max(0.0, end - overlap_seconds)
        end = max(end, segment.end)

    if end > start:
        chunks.append(_bounded_chunk(start, end, duration_seconds))
    if chunks and chunks[0].start > 0:
        chunks.insert(0, Chunk(0.0, min(chunks[0].start, duration_seconds)))
    if chunks and chunks[-1].end < duration_seconds:
        chunks.append(Chunk(max(0.0, chunks[-1].end - overlap_seconds), duration_seconds))
    return chunks


def _fixed_chunks(duration_seconds: float, target_seconds: float, overlap_seconds: float) -> list[Chunk]:
    chunks: list[Chunk] = []
    start = 0.0
    step = max(1.0, target_seconds - overlap_seconds)
    while start < duration_seconds:
        end = min(duration_seconds, start + target_seconds)
        chunks.append(Chunk(start, end))
        if end == duration_seconds:
            break
        start += step
    return chunks


def _bounded_chunk(start: float, end: float, duration_seconds: float) -> Chunk:
    return Chunk(max(0.0, start), min(duration_seconds, end))
