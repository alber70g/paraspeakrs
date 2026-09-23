from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field


class JobStatus(StrEnum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"


class Word(BaseModel):
    word: str
    start: float
    end: float
    speaker: str = "UNKNOWN"
    resolved_label: str | None = None


class TranscriptSegment(BaseModel):
    speaker: str
    resolved_label: str | None = None
    speaker_confidence: float = 0.0
    start: float
    end: float
    text: str
    words: list[Word] = Field(default_factory=list)


class DiarizationSegment(BaseModel):
    speaker: str
    start: float
    end: float


class AsrSegment(BaseModel):
    text: str
    start: float
    end: float
    words: list[Word] = Field(default_factory=list)


class TranscriptionResult(BaseModel):
    job_id: str
    language: str | None = None
    duration_seconds: float
    num_speakers: int
    segments: list[TranscriptSegment]
    warnings: list[str] = Field(default_factory=list)


class PipelineArtifacts(BaseModel):
    result: TranscriptionResult
    diarization: list[DiarizationSegment]
    speaker_embeddings: dict[str, list[float]] = Field(default_factory=dict)
    normalized_audio_path: Path
    source_path: Path | None = None
    # Content hash of the source audio, so an identical file can reuse this job
    # instead of re-running ASR and diarization. None on jobs written before this
    # field existed; those simply never match.
    source_fingerprint: str | None = None
    # Speaker ID -> name assigned by hand for this job. These outrank whatever the
    # embedding cache guesses, so a name typed here survives a later voice match
    # that would have resolved the speaker to someone else (or to nobody).
    # Empty on jobs written before this field existed.
    speaker_labels: dict[str, str] = Field(default_factory=dict)
    # What a person removed from the transcript: whole speaker IDs (someone who
    # was never part of the meeting) and single lines as (speaker, start) keys
    # from transcript_txt.line_key. Kept rather than deleted, so a removal can
    # be undone; only the rendered transcript leaves them out.
    excluded_speakers: list[str] = Field(default_factory=list)
    excluded_lines: list[tuple[str, float]] = Field(default_factory=list)


class LabelingArtifacts(BaseModel):
    duration_seconds: float
    diarization: list[DiarizationSegment]
    speaker_embeddings: dict[str, list[float]] = Field(default_factory=dict)
    resolved_labels: dict[str, str | None] = Field(default_factory=dict)
    snippet_paths: dict[str, list[Path]] = Field(default_factory=dict)
    normalized_audio_path: Path


class JobRecord(BaseModel):
    job_id: str
    status: JobStatus
    input_path: Path | None = None
    progress_step: str | None = None
    progress_detail: str | None = None
    progress_percent: int = 0
    result: TranscriptionResult | None = None
    error: str | None = None
