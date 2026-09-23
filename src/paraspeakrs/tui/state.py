from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

LOGGER = logging.getLogger(__name__)

WAITING = "waiting"
RUNNING = "running"
DONE = "done"
FAILED = "failed"


@dataclass
class QueueItem:
    path: Path
    status: str = WAITING
    job_id: str | None = None
    error: str | None = None
    # Stereo channels ("L", "R") the user said carry one person each. Kept on the
    # item because the queue outlives a restart and the answer must too.
    single_speaker_channels: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "path": str(self.path),
            "status": self.status,
            "job_id": self.job_id,
            "error": self.error,
            "single_speaker_channels": self.single_speaker_channels,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> QueueItem:
        return cls(
            path=Path(payload["path"]),
            # A run that was in flight when the app died never finished, and
            # nothing will ever finish it; it has to go back in line.
            status=WAITING if payload.get("status") == RUNNING else payload.get("status", WAITING),
            job_id=payload.get("job_id"),
            error=payload.get("error"),
            single_speaker_channels=[c for c in payload.get("single_speaker_channels", []) if c in ("L", "R")],
        )


@dataclass
class UiState:
    """What the UI remembers between runs: where you were browsing, what is queued.

    Recordings live far from the working directory - a recorder writes into its
    own Application Support folder - so starting at the filesystem root every
    time means walking the same eight directories before any work can begin.
    """

    path: Path
    browse_dir: Path | None = None
    queue: list[QueueItem] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> UiState:
        """Read the stored state, falling back to empty on anything unreadable.

        This file is a convenience, never a source of truth: a corrupt or
        half-written one must cost the user their last directory, not their
        ability to start the app.
        """
        state = cls(path=path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return state
        except (OSError, ValueError) as exc:
            LOGGER.warning("ignoring unreadable UI state at %s: %s", path, exc)
            return state
        if not isinstance(payload, dict):
            return state

        browse_dir = payload.get("browse_dir")
        if isinstance(browse_dir, str):
            candidate = Path(browse_dir).expanduser()
            # An external drive that is no longer mounted must not strand the
            # browser on a path that cannot be listed.
            if candidate.is_dir():
                state.browse_dir = candidate

        for entry in payload.get("queue", []):
            if isinstance(entry, dict) and entry.get("path"):
                item = QueueItem.from_dict(entry)
                # A finished item has become a job and is listed as one; only
                # failures still need somewhere to be seen.
                if item.status == DONE:
                    continue
                # Older versions queued a failed file a second time on retry; the
                # table keys rows by path, so keep only the later entry.
                state.queue = [kept for kept in state.queue if kept.path != item.path]
                state.queue.append(item)
        return state

    def save(self) -> None:
        payload = {
            "browse_dir": str(self.browse_dir) if self.browse_dir else None,
            "queue": [item.as_dict() for item in self.queue],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".json.tmp")
            temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            temp.replace(self.path)
        except OSError as exc:
            LOGGER.warning("could not save UI state to %s: %s", self.path, exc)

    def remember_dir(self, directory: Path) -> None:
        directory = Path(directory)
        if self.browse_dir == directory:
            return
        self.browse_dir = directory
        self.save()

    def enqueue(self, path: Path, single_speaker_channels: list[str] | None = None) -> QueueItem | None:
        """Add a recording to the queue, or None when it is already in it.

        Queuing the same file twice would transcribe it twice for one job,
        because the fingerprint check only reuses jobs that already exist.
        """
        path = Path(path)
        existing = next((item for item in self.queue if item.path == path), None)
        if existing is not None and existing.status in (WAITING, RUNNING):
            return None
        if existing is not None:
            # A failed file is retried in place: a second entry for the same path
            # would give the queue table two rows with one key.
            existing.status, existing.error = WAITING, None
            existing.single_speaker_channels = list(single_speaker_channels or [])
            self.save()
            return existing
        item = QueueItem(path=path, single_speaker_channels=list(single_speaker_channels or []))
        self.queue.append(item)
        self.save()
        return item

    def drop(self, path: Path) -> bool:
        """Remove an item. A run already in flight is left alone - it owns a worker."""
        for index, item in enumerate(self.queue):
            if item.path == Path(path) and item.status != RUNNING:
                del self.queue[index]
                self.save()
                return True
        return False

    def next_waiting(self) -> QueueItem | None:
        return next((item for item in self.queue if item.status == WAITING), None)

    def mark(self, item: QueueItem, status: str, *, job_id: str | None = None, error: str | None = None) -> None:
        """Record what became of an item, dropping it once it is done.

        A finished item has become a job and is listed as one, so leaving it here
        puts the same recording in two lists at once. Only failures still need
        somewhere to be seen - this is the same rule :meth:`load` applies across
        a restart, applied as it happens instead.
        """
        item.status = status
        item.job_id = job_id
        item.error = error
        if status == DONE:
            self.queue = [entry for entry in self.queue if entry is not item]
        self.save()

    def queued_paths(self) -> set[Path]:
        return {item.path for item in self.queue}
