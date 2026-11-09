"""The voices of the meeting in progress, kept apart by their voice prints.

Each transcribed piece is diarized together with the minute before it (see
``windows.py``). Diarization numbers its speakers afresh every time, so its
SPEAKER_00 is matched to the meeting's own speakers by voice print: the closest
one on the same track at a cosine of at least ``SAME_VOICE``, else a new one.

Every meeting speaker is also compared with the named voices in the shared voice
cache (``speaker-cache.json``, the one the labeling TUI teaches). The best match is
shown with its score; a match at or above the cache threshold is offered as a
suggestion, and only a person accepting it or typing a name names the speaker.
Naming folds the voice into the cache, so the next meeting knows it.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from ..cache import SpeakerCache
from .transcript import Line

# Measured on a recorded call with the speakrs model: one speaker's 60 s windows
# against the whole recording scored 0.84-0.97; different people in the voice
# cache scored 0.27 (median) and at most 0.61.
SAME_VOICE = 0.6
# A window speaker with less speech than this in the window that matches nobody
# joins the nearest voice instead of starting a new one: a short window gives a
# noisy print. Replayed on a 41-minute in-room meeting (9 speakers in the full
# diarization): 2 s made 23 voices, 10 s made 12, at the same purity (0.84-0.85).
# The cost: a new person is first heard as the nearest voice, for up to 10 s.
MIN_NEW_SECONDS = 10.0
# How often a named voice's growing print is written back to the cache.
REFOLD_SECONDS = 60.0


@dataclass
class Voice:
    id: str
    track: str
    label: str
    # Sum of unit-length window prints, each weighted by the new seconds it covered.
    total: list[float]
    seconds: float = 0.0
    name: str | None = None
    match: str | None = None
    score: float = -1.0

    @property
    def display(self) -> str:
        return self.name or self.label

    @property
    def print(self) -> list[float]:
        return _unit(self.total)


class SpeakerBook:
    def __init__(
        self,
        cache: SpeakerCache,
        *,
        session_id: str,
        path: Path | None = None,
        same_voice: float = SAME_VOICE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cache = cache
        self.job_id = f"live:{session_id}"
        self.path = path
        self.same_voice = same_voice
        self.clock = clock
        self.voices: dict[str, Voice] = {}
        self.on_change: Callable[[], None] = lambda: None
        self._folded: dict[str, tuple[str, float]] = {}

    def attach(self, path: Path, recording: str) -> None:
        """Keep the book with a recording, picking up the voices a previous run found in it.

        The cache knows a voice by (job id, speaker id); with the recording as the job
        a restart refolds the same members instead of adding the voice a second time.
        """
        self.path = path
        self.job_id = f"live:{recording}"
        if path.exists():
            for entry in json.loads(path.read_text(encoding="utf-8")):
                weight = max(float(entry["seconds"]), 1e-3)
                voice = Voice(
                    id=entry["id"],
                    track=entry["track"],
                    label=entry["label"],
                    total=[weight * x for x in entry["print"]],
                    seconds=float(entry["seconds"]),
                    name=entry.get("name"),
                )
                self._match(voice)
                self.voices[voice.id] = voice
                if voice.name:
                    self._folded[voice.id] = (voice.name, self.clock())
        self._changed()

    def place(
        self,
        track: str,
        prefix: str,
        prints: dict[str, list[float]],
        talk: dict[str, float],
        fresh: dict[str, float],
    ) -> dict[str, str]:
        """Window speaker -> meeting speaker id.

        ``talk`` is each window speaker's seconds in the whole window, ``fresh`` in
        the part not diarized before; only fresh seconds add to a voice's print, or
        the minute that every window repeats would count many times over.
        """
        placed: dict[str, str] = {}
        claimed: set[str] = set()
        for local in sorted(prints, key=lambda s: -talk.get(s, 0.0)):
            vector = _unit(prints[local])
            if not any(vector):
                continue
            ranked = sorted(
                ((_cosine(vector, v.print), v) for v in self.voices.values() if v.track == track),
                key=lambda pair: -pair[0],
            )
            free = [(score, v) for score, v in ranked if v.id not in claimed]
            if free and free[0][0] >= self.same_voice:
                voice = free[0][1]
            elif talk.get(local, 0.0) < MIN_NEW_SECONDS and ranked:
                voice = (free or ranked)[0][1]
            else:
                voice = self._new(track, prefix)
            claimed.add(voice.id)
            placed[local] = voice.id
            weight = fresh.get(local, 0.0)
            if weight > 0 or not voice.seconds:
                weight = max(weight, 1e-3)
                voice.total = [t + weight * x for t, x in zip(voice.total, vector)] if voice.total else [weight * x for x in vector]
                voice.seconds += fresh.get(local, 0.0)
                self._match(voice)
                self._refold(voice)
        self._changed()
        return placed

    def name(self, voice_id: str, name: str | None) -> None:
        """Name a voice (None or blank: unname it), and teach or untie the cache."""
        voice = self.voices.get(voice_id)
        if voice is None:
            raise KeyError(voice_id)
        name = (name or "").strip() or None
        if voice.name and voice.name != name:
            self.cache.unfold(voice.name, self.job_id, voice.id)
            self._folded.pop(voice.id, None)
        voice.name = name
        self._refold(voice, force=True)
        self._match(voice)
        self._changed()

    def finish(self) -> None:
        """The recording stopped: every named voice goes to the cache as it ended up."""
        for voice in self.voices.values():
            self._refold(voice, force=True)

    def display(self, voice_id: str | None, fallback: str) -> str:
        voice = self.voices.get(voice_id or "")
        return voice.display if voice else fallback

    def relabel(self, lines: Sequence[Line]) -> list[Line]:
        """Lines with the names their speakers have now."""
        return [
            replace(line, speaker=self.voices[line.speaker_id].display)
            if line.speaker_id in self.voices and self.voices[line.speaker_id].display != line.speaker
            else line
            for line in lines
        ]

    def snapshot(self) -> list[dict]:
        out = []
        for voice in self.voices.values():
            suggested = voice.match if voice.score >= self.cache.threshold and voice.match != voice.name else None
            out.append(
                {
                    "id": voice.id,
                    "track": voice.track,
                    "label": voice.label,
                    "name": voice.name,
                    "display": voice.display,
                    "seconds": round(voice.seconds, 1),
                    "match": {"name": voice.match, "score": round(max(voice.score, 0.0), 3)} if voice.match else None,
                    "suggested": suggested,
                }
            )
        return out

    def _new(self, track: str, prefix: str) -> Voice:
        number = sum(1 for v in self.voices.values() if v.track == track) + 1
        voice = Voice(id=f"S{len(self.voices) + 1}", track=track, label=f"{prefix} {number}", total=[])
        self.voices[voice.id] = voice
        return voice

    def _match(self, voice: Voice) -> None:
        if not voice.total:
            return
        try:
            voice.match, voice.score = self.cache.suggest_embeddings({voice.id: voice.print})[voice.id]
        except ValueError:  # a cache from another model: nothing comparable to offer
            voice.match, voice.score = None, -1.0

    def _refold(self, voice: Voice, *, force: bool = False) -> None:
        if not voice.name or not voice.total:
            return
        last = self._folded.get(voice.id)
        now = self.clock()
        if not force and last and last[0] == voice.name and now - last[1] < REFOLD_SECONDS:
            return
        self.cache.fold(voice.name, voice.print, job_id=self.job_id, speaker_id=voice.id)
        self._folded[voice.id] = (voice.name, now)

    def _changed(self) -> None:
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = [{**entry, "print": self.voices[entry["id"]].print} for entry in self.snapshot()]
            self.path.write_text(json.dumps(data), encoding="utf-8")
        self.on_change()


def _unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector] if norm else list(vector)


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return -1.0
    return sum(a * b for a, b in zip(left, right))  # both unit length
