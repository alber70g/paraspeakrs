from __future__ import annotations

import wave
from collections import defaultdict
from pathlib import Path

import numpy as np

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


MONTAGE_RATE = 16000


def montage_segments(
    segments: list[DiarizationSegment],
    count: int = 3,
    max_seconds: float = 10.0,
    min_seconds: float = 1.5,
    target_seconds: float = 8.0,
) -> list[tuple[float, float]]:
    """The spans a speaker's montage is cut from: the longest few, in time order.

    Up to ``count`` takes of at least ``min_seconds``; while those add up to less
    than ``target_seconds``, the next-longest takes are added whatever their
    length. Someone who spoke in short bursts is otherwise left with a single
    second of audio, and every diarized speaker gets a sample however little
    they said.
    """
    chosen: list[tuple[float, float]] = []
    total = 0.0
    for seg in sorted(segments, key=lambda seg: seg.end - seg.start, reverse=True):
        long_enough = seg.end - seg.start >= min_seconds
        if total >= target_seconds and (len(chosen) >= count or not long_enough):
            break
        end = min(seg.end, seg.start + max_seconds)
        chosen.append((seg.start, end))
        total += end - seg.start
    return sorted(chosen)


def write_montage(
    audio: AudioPreparer,
    audio_path: Path,
    output_path: Path,
    spans: list[tuple[float, float]],
) -> Path:
    """Stitch ``spans`` of ``audio_path`` into one wav, a short beep between each.

    The beep says "next take" - without it two takes of one speaker run together
    and sound like a single odd sentence.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    parts: list[bytes] = []
    for index, (start, end) in enumerate(spans):
        scratch = output_path.with_name(f".{output_path.stem}-part{index}.wav")
        try:
            audio.export_chunk(audio_path, scratch, start, end)
            with wave.open(str(scratch), "rb") as clip:
                parts.append(clip.readframes(clip.getnframes()))
        finally:
            scratch.unlink(missing_ok=True)
    with wave.open(str(output_path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(MONTAGE_RATE)
        out.writeframes(_separator().join(parts))
    return output_path


def _separator() -> bytes:
    """0.3 s silence, a 0.2 s quiet 880 Hz tone, 0.3 s silence - as 16-bit mono."""
    silence = np.zeros(int(0.3 * MONTAGE_RATE), dtype=np.int16)
    t = np.arange(int(0.2 * MONTAGE_RATE)) / MONTAGE_RATE
    tone = (0.2 * 32767 * np.sin(2 * np.pi * 880 * t)).astype(np.int16)
    return np.concatenate([silence, tone, silence]).tobytes()


def _segments_by_speaker(diarization: list[DiarizationSegment]) -> dict[str, list[DiarizationSegment]]:
    grouped: dict[str, list[DiarizationSegment]] = defaultdict(list)
    for segment in diarization:
        grouped[segment.speaker].append(segment)
    return grouped
