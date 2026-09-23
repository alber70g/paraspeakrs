from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from .audio import AudioPreparer
from .models import DiarizationSegment


def export_label_snippets(
    audio: AudioPreparer,
    audio_path: Path,
    output_dir: Path,
    diarization: list[DiarizationSegment],
    max_per_speaker: int = 3,
    max_seconds: float = 12.0,
    min_seconds: float = 2.0,
) -> dict[str, list[Path]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    snippets: dict[str, list[Path]] = {}
    for speaker, segments in _segments_by_speaker(diarization).items():
        candidates = sorted(segments, key=lambda item: item.end - item.start, reverse=True)
        snippets[speaker] = []
        for index, segment in enumerate(candidates[:max_per_speaker], start=1):
            duration = segment.end - segment.start
            if duration < min_seconds:
                continue
            end = min(segment.end, segment.start + max_seconds)
            path = output_dir / f"{speaker}-{index:02d}.wav"
            if not path.exists():
                audio.export_chunk(audio_path, path, segment.start, end)
            snippets[speaker].append(path)
    return snippets


def _segments_by_speaker(diarization: list[DiarizationSegment]) -> dict[str, list[DiarizationSegment]]:
    grouped: dict[str, list[DiarizationSegment]] = defaultdict(list)
    for segment in diarization:
        grouped[segment.speaker].append(segment)
    return grouped
