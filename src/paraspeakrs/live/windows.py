"""Who spoke in a piece: diarize it together with the minute before it.

speakrs diarizes a file, not a stream, and finds nobody in a lone 10 s piece. So
each track keeps its last ``WINDOW_SECONDS`` of 16 kHz audio, and every piece is
diarized as the end of that window by the speakrs-diar sidecar. On CoreML a 60 s
window takes about 1.5 s, while the piece is being transcribed.
"""

from __future__ import annotations

import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np

from ..models import DiarizationSegment
from .speakers import SpeakerBook

WINDOW_SECONDS = 60.0


class FileDiarizer(Protocol):
    def diarize(self, audio_path: Path) -> list[DiarizationSegment]: ...

    @property
    def last_centroids(self) -> dict[str, list[float]]: ...


class TrackAudio:
    """The last ``seconds`` of one track, on the recording's clock."""

    def __init__(self, rate: int, seconds: float = WINDOW_SECONDS) -> None:
        self.rate = rate
        self.keep = int(seconds * rate)
        self.start = 0.0
        self.samples = np.zeros(0, dtype=np.float32)

    @property
    def end(self) -> float:
        return self.start + len(self.samples) / self.rate

    def copy(self) -> TrackAudio:
        twin = TrackAudio(self.rate)
        twin.keep, twin.start, twin.samples = self.keep, self.start, self.samples
        return twin

    def add(self, start: float, samples: np.ndarray) -> None:
        # Pieces follow each other without a gap; anything else starts afresh.
        if len(self.samples) and abs(start - self.end) > 1 / self.rate:
            self.samples = np.zeros(0, dtype=np.float32)
        if not len(self.samples):
            self.start = start
        self.samples = np.concatenate((self.samples, samples.astype(np.float32)))
        if len(self.samples) > self.keep:
            cut = len(self.samples) - self.keep
            self.samples = self.samples[cut:]
            self.start += cut / self.rate


class WindowDiarizer:
    """Diarizes a track's window through a file diarizer, on the recording's clock."""

    def __init__(self, diarizer: FileDiarizer, work_dir: Path) -> None:
        self.diarizer = diarizer
        self.work_dir = work_dir

    def __call__(self, track: str, audio: TrackAudio) -> tuple[list[DiarizationSegment], dict[str, list[float]]]:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        path = self.work_dir / f"{track}-window.wav"
        pcm = (np.clip(audio.samples, -1, 1) * 32767).astype("<i2")
        with wave.open(str(path), "wb") as fh:
            fh.setnchannels(1)
            fh.setsampwidth(2)
            fh.setframerate(audio.rate)
            fh.writeframes(pcm.tobytes())
        try:
            segments = self.diarizer.diarize(path)
        except RuntimeError:
            # The sidecar panics on a window with too little speech in it, where
            # the answer is nobody: the piece keeps its track's speaker.
            return [], {}
        shifted = [
            DiarizationSegment(speaker=s.speaker, start=s.start + audio.start, end=s.end + audio.start) for s in segments
        ]
        return shifted, dict(self.diarizer.last_centroids)


# A mark names whoever spoke in the MARK_BEFORE s before the click to MARK_AFTER s
# after it: a click comes during or just after what someone says.
MARK_BEFORE = 4.0
MARK_AFTER = 0.5
# Settled once a track is diarized this far past the click, so both tracks had their say.
MARK_SETTLE = 5.0
# With nobody heard around it by then, the mark is dropped.
MARK_GIVE_UP = 15.0


@dataclass
class Mark:
    at: float
    name: str
    # Meeting speaker -> seconds heard around the mark, the most any window saw.
    heard: dict[str, float] = field(default_factory=dict)

    def event(self, state: str, speaker: str | None = None) -> dict:
        return {"type": "mark", "at": round(self.at, 2), "name": self.name, "state": state, "speaker": speaker}


Diarize = Callable[[str, TrackAudio], "tuple[list[DiarizationSegment], dict[str, list[float]]]"]


class LiveSpeakers:
    """Keeps each track's window and turns a window's speakers into the meeting's."""

    def __init__(self, book: SpeakerBook, diarize: Diarize) -> None:
        self.book = book
        self.diarize = diarize
        self.audio: dict[str, TrackAudio] = {}
        # The meeting speaker who spoke most in a track's latest piece: the speaker
        # for words the diarization did not cover.
        self.last: dict[str, str] = {}
        self.marks: list[Mark] = []
        self.on_mark: Callable[[dict], None] = lambda event: None

    def mark(self, at: float, name: str) -> dict:
        """Name whoever is speaking at ``at``, once that moment has been diarized."""
        mark = Mark(at=at, name=name.strip())
        self.marks.append(mark)
        return mark.event("waiting")

    def hear(self, track: str, start: float, samples: np.ndarray, rate: int) -> None:
        self.audio.setdefault(track, TrackAudio(rate)).add(start, samples)

    def window(self, track: str) -> tuple[list[DiarizationSegment], dict[str, list[float]]]:
        """Runs in a worker thread, on a copy, while the next piece is heard."""
        return self.diarize(track, self.audio[track].copy())

    def place(
        self,
        track: str,
        prefix: str,
        start: float,
        end: float,
        segments: list[DiarizationSegment],
        prints: dict[str, list[float]],
    ) -> list[DiarizationSegment]:
        """The window's segments under meeting speaker ids."""
        talk: dict[str, float] = {}
        fresh: dict[str, float] = {}
        for s in segments:
            talk[s.speaker] = talk.get(s.speaker, 0.0) + s.end - s.start
            fresh[s.speaker] = fresh.get(s.speaker, 0.0) + max(0.0, min(end, s.end) - max(start, s.start))
        ids = self.book.place(track, prefix, {k: v for k, v in prints.items() if k in talk}, talk, fresh)
        if fresh and (top := max(fresh, key=fresh.get)) in ids and fresh[top] > 0:
            self.last[track] = ids[top]
        placed = [s.model_copy(update={"speaker": ids[s.speaker]}) for s in segments if s.speaker in ids]
        self._settle_marks(placed, end)
        return placed

    def _settle_marks(self, segments: list[DiarizationSegment], reached: float) -> None:
        """Credit each waiting mark with who spoke around it; name them once settled."""
        waiting = []
        for mark in self.marks:
            for s in segments:
                overlap = max(0.0, min(mark.at + MARK_AFTER, s.end) - max(mark.at - MARK_BEFORE, s.start))
                mark.heard[s.speaker] = max(mark.heard.get(s.speaker, 0.0), overlap)
            if reached < mark.at + MARK_SETTLE:
                waiting.append(mark)
            elif mark.heard and max(mark.heard.values()) > 0:
                voice = max(mark.heard, key=mark.heard.get)
                self.book.name(voice, mark.name)
                self.on_mark(mark.event("named", voice))
            elif reached >= mark.at + MARK_GIVE_UP:
                self.on_mark(mark.event("nobody"))
            else:
                waiting.append(mark)
        self.marks = waiting
