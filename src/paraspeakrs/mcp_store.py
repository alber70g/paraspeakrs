from __future__ import annotations

import hashlib
import json
import logging
import shutil
from collections import defaultdict
from pathlib import Path

from pydantic import ValidationError

from .models import PipelineArtifacts, TranscriptionResult

LOGGER = logging.getLogger("uvicorn.error")

# Files in a job dir that are not rebuildable from the source recording, and so
# must survive pruning.
_KEEP = {"artifacts.json", "note.json"}


class ArtifactStore:
    """Per-job persistence of pipeline artifacts for the MCP labeling flow.

    Each job gets its own directory under ``root`` holding the normalized audio
    (written there by the pipeline) and an ``artifacts.json`` snapshot of the
    diarization, per-speaker embeddings and transcription result so a later
    labeling or transcript call can reuse the exact same speaker IDs.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def work_dir(self, job_id: str) -> Path:
        path = self.root / job_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save(self, job_id: str, artifacts: PipelineArtifacts) -> None:
        path = self._path(job_id)
        temp_path = path.with_suffix(".json.tmp")
        temp_path.write_text(artifacts.model_dump_json(indent=2), encoding="utf-8")
        temp_path.replace(path)

    def load(self, job_id: str) -> PipelineArtifacts:
        path = self._path(job_id)
        if not path.exists():
            raise KeyError(job_id)
        return PipelineArtifacts.model_validate_json(path.read_text(encoding="utf-8"))

    def list_jobs(self) -> list[tuple[str, PipelineArtifacts]]:
        """(job_id, artifacts) for every job dir holding a valid artifacts.json.

        Newest first, by the artifacts file's modification time. A job whose
        artifacts.json is unreadable or fails schema validation is skipped
        with a WARNING logged, rather than silently dropped.
        """
        entries: list[tuple[float, str, PipelineArtifacts]] = []
        for child in self.root.iterdir():
            path = child / "artifacts.json"
            if not path.exists():
                continue
            try:
                artifacts = PipelineArtifacts.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValidationError) as exc:
                LOGGER.warning("skipping job %s: unreadable artifacts.json (%s)", child.name, exc)
                continue
            entries.append((path.stat().st_mtime, child.name, artifacts))
        entries.sort(key=lambda item: item[0], reverse=True)
        return [(job_id, artifacts) for _, job_id, artifacts in entries]

    def prune_audio(self, job_id: str) -> int:
        """Delete a finished job's working audio, returning the bytes freed.

        A job dir keeps a copy of the source recording, two normalized renders
        and a WAV per chunk - around 99% of its size, and all rebuildable from
        the original recording. Only ``artifacts.json``, the job's note and any
        exported speaker snippets are worth keeping, so those are what survive.
        """
        directory = self.root / job_id
        if not directory.is_dir():
            return 0
        freed = 0
        for child in directory.iterdir():
            if child.is_dir() or child.name in _KEEP:
                continue
            try:
                size = child.stat().st_size
                child.unlink()
            except OSError as exc:
                LOGGER.warning("could not prune %s: %s", child, exc)
                continue
            freed += size
        return freed

    def delete(self, job_id: str) -> bool:
        """Remove a job and everything it left on disk."""
        directory = self.root / job_id
        if not directory.is_dir():
            return False
        shutil.rmtree(directory)
        return True

    def note(self, job_id: str) -> str:
        """The free-text note kept beside this job, or "" when it has none.

        Notes live in their own ``note.json`` rather than in ``artifacts.json``:
        that file carries the diarization and one float vector per speaker, and
        rewriting all of it to record a typed sentence would put the expensive,
        irreplaceable part of a job at risk for the cheap part.
        """
        path = self._note_path(job_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ""
        except (OSError, ValueError) as exc:
            LOGGER.warning("ignoring unreadable note for job %s: %s", job_id, exc)
            return ""
        note = payload.get("note") if isinstance(payload, dict) else None
        return note if isinstance(note, str) else ""

    def set_note(self, job_id: str, note: str) -> None:
        """Write (or, given an empty note, remove) this job's note."""
        path = self._note_path(job_id)
        if not note.strip():
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(".json.tmp")
        temp_path.write_text(json.dumps({"note": note}, indent=2), encoding="utf-8")
        temp_path.replace(path)

    def _note_path(self, job_id: str) -> Path:
        return self.root / job_id / "note.json"

    def find_by_fingerprint(self, fingerprint: str) -> tuple[str, PipelineArtifacts] | None:
        """Newest job whose source audio hashed to ``fingerprint``, if any."""
        for job_id, artifacts in self.list_jobs():
            if artifacts.source_fingerprint == fingerprint:
                return job_id, artifacts
        return None

    def _path(self, job_id: str) -> Path:
        return self.root / job_id / "artifacts.json"


def fingerprint_file(path: Path, _chunk: int = 1 << 20) -> str:
    """SHA-256 of a file's contents.

    Hashing the bytes rather than trusting path or mtime means a recording that
    was moved, renamed or re-copied still matches its existing job. Reading a
    few hundred MB costs well under a second against the minutes of ASR it
    saves.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(_chunk):
            digest.update(block)
    return digest.hexdigest()


def apply_labels(result: TranscriptionResult, labels: dict[str, str | None]) -> TranscriptionResult:
    """Return a copy of ``result`` with resolved_label refreshed from ``labels``."""
    segments = []
    for segment in result.segments:
        words = [word.model_copy(update={"resolved_label": labels.get(word.speaker)}) for word in segment.words]
        segments.append(segment.model_copy(update={"resolved_label": labels.get(segment.speaker), "words": words}))
    return result.model_copy(update={"segments": segments})


def talk_seconds(artifacts: PipelineArtifacts) -> dict[str, float]:
    """Total diarized speaking time per speaker ID."""
    totals: dict[str, float] = defaultdict(float)
    for segment in artifacts.diarization:
        totals[segment.speaker] += max(0.0, segment.end - segment.start)
    return dict(totals)
