"""What `live follow` has done for one recording, so a restart continues where it stopped.

Kept in ``<workspace>/live/recordings/<recording>/``, apart from the per-run session
folder, because a restart is a new session of the same recording:

- ``lines.jsonl``: every line handed out so far, in order;
- ``progress.json``: per track, how far it is transcribed and the lines from there
  on that were not handed out yet (the other track was behind);
- ``speakers.json``: the meeting's voices and names (written by ``SpeakerBook``).

A track resumes at the end of its last transcribed piece, so no audio is
transcribed twice and none is skipped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .transcript import Line


@dataclass
class Progress:
    lines: list[Line] = field(default_factory=list)
    covered: dict[str, float] = field(default_factory=dict)
    pending: dict[str, list[Line]] = field(default_factory=dict)


class RecordingState:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    @property
    def speakers_path(self) -> Path:
        return self.directory / "speakers.json"

    def load(self) -> Progress | None:
        """What was done before, or None for a recording not seen yet."""
        progress_path = self.directory / "progress.json"
        if not progress_path.exists():
            return None
        data = json.loads(progress_path.read_text(encoding="utf-8"))
        lines_path = self.directory / "lines.jsonl"
        lines = (
            [Line.from_json(json.loads(raw)) for raw in lines_path.read_text(encoding="utf-8").splitlines() if raw]
            if lines_path.exists()
            else []
        )
        return Progress(
            lines=lines,
            covered={track: float(t["covered"]) for track, t in data["tracks"].items()},
            pending={track: [Line.from_json(x) for x in t["pending"]] for track, t in data["tracks"].items()},
        )

    def save(self, released: list[Line], tracks: dict[str, tuple[float, list[Line]]]) -> None:
        """Append the lines just handed out and record each track's (covered, pending)."""
        self.directory.mkdir(parents=True, exist_ok=True)
        if released:
            with (self.directory / "lines.jsonl").open("a", encoding="utf-8") as fh:
                for line in released:
                    fh.write(json.dumps(line.to_json(), ensure_ascii=False) + "\n")
        data = {
            "tracks": {
                track: {"covered": covered, "pending": [line.to_json() for line in pending]}
                for track, (covered, pending) in tracks.items()
            }
        }
        temp = self.directory / "progress.json.tmp"
        temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        temp.replace(self.directory / "progress.json")
