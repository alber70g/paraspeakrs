from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .models import DiarizationSegment


class SpeakerEmbeddingExtractor(Protocol):
    def extract(self, audio_path: Path, diarization: list[DiarizationSegment]) -> dict[str, list[float]]:
        ...


class CentroidSource(Protocol):
    """A diarizer that produced per-speaker centroids during its last pass."""

    @property
    def last_centroids(self) -> dict[str, list[float]]:
        ...


class NullSpeakerEmbeddingExtractor:
    def extract(self, audio_path: Path, diarization: list[DiarizationSegment]) -> dict[str, list[float]]:
        return {}


class CentroidEmbeddingExtractor:
    def __init__(self, diarizer: CentroidSource) -> None:
        self.diarizer = diarizer

    def extract(self, audio_path: Path, diarization: list[DiarizationSegment]) -> dict[str, list[float]]:
        return dict(self.diarizer.last_centroids)
