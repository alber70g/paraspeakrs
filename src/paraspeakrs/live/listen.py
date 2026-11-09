"""Transcribing a recording while the recorder is still making it.

The recorder writes two tracks as it records: ``<name>.mic.caf`` (you) and
``<name>.system.caf`` (everyone else on the call), both locked to the same clock.
Each track is read as it grows, cut into pieces of 6 to 20 seconds at a pause, and
each piece is transcribed by a Parakeet model that stays loaded. Who said it comes
from diarizing the piece with the minute before it, matched to the meeting's
speakers by voice print (``windows.py``, ``speakers.py``); without that, the speaker
is the track. Diarization of the whole recording still runs after the stop.

Lines are handed out in time order across both tracks. A line is final once both
tracks are transcribed past its start, so a track that is behind holds the other
back. The system track does not grow at all while nothing plays (the audio tap
delivers nothing then), so a track that has not grown for a moment counts as silent
up to where the other one is.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import numpy as np

from ..merge import merge_asr_with_diarization
from ..models import (
    AsrSegment,
    DiarizationSegment,
    TranscriptionResult,
    TranscriptSegment,
)
from ..transcript_txt import format_time, utterances
from .caf import CafTail
from .resume import RecordingState
from .transcript import Line
from .windows import WINDOW_SECONDS

if TYPE_CHECKING:
    from .windows import LiveSpeakers

DEFAULT_RECORDINGS_DIR = Path.home() / "Documents" / "paraspeakrs-recordings"
POLL_SECONDS = 0.5
# The recorder appends every 100 ms; a track that has not grown for this long is silent.
IDLE_SECONDS = 1.5
# How far behind the growing track an idle one is assumed silent, for the recorder's
# 50 ms alignment tolerance and its 100 ms write interval, with room to spare.
IDLE_SLACK_SECONDS = 1.0
# Both tracks idle this long and no .m4a: the recorder stopped without merging (a crash).
END_IDLE_SECONDS = 30.0
# A .mic.caf modified this recently is a recording in progress.
FRESH_SECONDS = 15.0
# A track that is behind (a restart in a running meeting, or the rest after the stop)
# is read this much at a time, and its lines are handed out after every round.
MAX_READ_SECONDS = 30.0
# Quieter than this, a piece is not worth a transcription.
SILENT_PEAK = 10 ** (-50 / 20)

Status = Callable[[dict], None]


class SampleAsr(Protocol):
    def load(self) -> object: ...

    def transcribe_samples(self, samples: np.ndarray, sample_rate: int) -> AsrSegment: ...


@dataclass
class Piece:
    start: float
    samples: np.ndarray
    rate: float

    @property
    def end(self) -> float:
        return self.start + len(self.samples) / self.rate


class Chunker:
    """Cuts a growing track into pieces at pauses.

    A piece ends at the first pause of ``pause_seconds`` after ``min_seconds``;
    without one, at the quietest 0.2 s between ``min_seconds`` and ``max_seconds``,
    as `paraspeakrs dictate` cuts. Short pieces keep the delay down; whole phrases
    keep words from being cut in half.
    """

    def __init__(
        self,
        rate: float,
        *,
        start: float = 0.0,
        min_seconds: float = 6.0,
        max_seconds: float = 20.0,
        pause_seconds: float = 0.5,
    ):
        self.rate = rate
        self.min_samples = int(min_seconds * rate)
        self.max_samples = int(max_seconds * rate)
        self.pause_seconds = pause_seconds
        self.frame = max(1, int(rate * 0.02))
        self._buffer = np.zeros(0, dtype=np.float32)
        self._start = start

    @property
    def buffered_seconds(self) -> float:
        return len(self._buffer) / self.rate

    def push(self, samples: np.ndarray) -> list[Piece]:
        self._buffer = np.concatenate((self._buffer, samples))
        pieces = []
        while (cut := self._cut()) is not None:
            pieces.append(self._take(cut))
        return pieces

    def flush(self) -> Piece | None:
        """Whatever is buffered, as one piece; None when nothing is."""
        return self._take(len(self._buffer)) if len(self._buffer) else None

    def _take(self, cut: int) -> Piece:
        piece = Piece(self._start, self._buffer[:cut].copy(), self.rate)
        self._buffer = self._buffer[cut:]
        self._start += cut / self.rate
        return piece

    def _cut(self) -> int | None:
        buffer = self._buffer
        if len(buffer) < self.min_samples:
            return None
        frames = len(buffer) // self.frame
        rms = np.sqrt(np.mean(buffer[: frames * self.frame].reshape(frames, self.frame) ** 2, axis=1))
        quiet = rms < max(SILENT_PEAK * 0.5, float(np.percentile(rms, 95)) * 0.1)
        need = max(1, round(self.pause_seconds / 0.02))
        run = 0
        for i in range(frames):
            run = run + 1 if quiet[i] else 0
            if run >= need:
                middle = (i - need // 2) * self.frame
                if middle >= self.min_samples:
                    return middle
        if len(buffer) >= self.max_samples:
            return self.min_samples + _quietest(buffer[self.min_samples : self.max_samples], self.rate)
        return None


def _quietest(samples: np.ndarray, rate: float) -> int:
    window = max(1, int(0.2 * rate))
    hop = max(1, int(rate) // 100)
    energy = np.concatenate(([0.0], np.cumsum(samples.astype(np.float64) ** 2)))
    per_window = (energy[window:] - energy[:-window])[::hop]
    return int(np.argmin(per_window)) * hop + window // 2


def is_silent(samples: np.ndarray) -> bool:
    return not len(samples) or float(np.max(np.abs(samples))) < SILENT_PEAK


ASR_RATE = 16_000


def to_asr_rate(samples: np.ndarray, rate: float) -> tuple[np.ndarray, int]:
    """The recorder's 48 kHz down to Parakeet's 16 kHz: low-pass, then every third sample.

    sherpa-onnx would resample too, but it logs "Creating a resampler" to stderr for
    every piece. Rates that are no whole multiple of 16 kHz are still left to it.
    """
    rate = int(round(rate))
    if rate == ASR_RATE or rate % ASR_RATE:
        return samples, rate
    factor = rate // ASR_RATE
    taps = np.arange(-16 * factor, 16 * factor + 1)
    cutoff = 0.45 / factor  # cycles per sample, a little under the new Nyquist
    kernel = 2 * cutoff * np.sinc(2 * cutoff * taps) * np.hamming(len(taps))
    kernel /= kernel.sum()
    return np.convolve(samples, kernel, mode="same")[::factor].astype(np.float32), ASR_RATE


def piece_lines(asr: SampleAsr, piece: Piece, speaker: str) -> list[Line]:
    """A piece transcribed and split into lines on the recording's clock."""
    return segment_lines(asr.transcribe_samples(*to_asr_rate(piece.samples, piece.rate)), piece, speaker)


def segment_lines(
    segment: AsrSegment,
    piece: Piece,
    speaker: str,
    diarization: list[DiarizationSegment] | None = None,
    fallback: str | None = None,
) -> list[Line]:
    """A transcribed piece as lines; with ``diarization``, split where the speaker changes.

    Lines then carry the meeting speaker id in ``speaker_id``, and ``speaker`` until
    their names are filled in at release. Words no segment covers go to ``fallback``
    (the track's latest speaker), or to the track when there is none.
    """
    if not segment.text:
        return []
    words = [
        w.model_copy(update={"start": w.start + piece.start, "end": w.end + piece.start, "speaker": speaker})
        for w in segment.words
    ]
    whole = TranscriptSegment(speaker=speaker, start=piece.start, end=piece.end, text=segment.text, words=words)
    segments = [whole]
    if diarization:
        asr = AsrSegment(text=segment.text, start=piece.start, end=piece.end, words=words)
        segments = [
            s.model_copy(update={"speaker": fallback or speaker}) if s.speaker == "UNKNOWN" else s
            for s in merge_asr_with_diarization([asr], diarization)[0]
        ]
    elif fallback:
        segments = [whole.model_copy(update={"speaker": fallback})]
    result = TranscriptionResult(job_id="live", duration_seconds=piece.end, num_speakers=1, segments=segments)
    return [
        Line(u.start, u.end, u.name, u.text, speaker_id=None if u.speaker == speaker else u.speaker)
        for u in utterances(result)
    ]


@dataclass
class Channel:
    speaker: str
    tail: CafTail
    track: str = ""
    chunk_options: dict = field(default_factory=dict)
    chunker: Chunker | None = None
    # Transcribed up to here; every later line starts at or after it.
    covered: float = 0.0
    last_growth: float = 0.0
    lines: list[Line] = field(default_factory=list)
    # Where the first piece starts: 0, or where a previous run stopped.
    origin: float = 0.0
    # The last read was cut off at MAX_READ_SECONDS: there is more waiting.
    behind: bool = False
    drained: bool = False

    def poll(self, now: float) -> list[Piece]:
        samples = self._read()
        if len(samples):
            self.last_growth = now
            return self._push(samples)
        if self.idle(now) and self.chunker is not None:
            piece = self.chunker.flush()
            return [piece] if piece else []
        return []

    def drain_step(self) -> list[Piece]:
        """The next block of what is left once the recording has stopped; the buffer at the end."""
        samples = self._read()
        if len(samples):
            return self._push(samples)
        self.drained = True
        piece = self.chunker.flush() if self.chunker is not None else None
        return [piece] if piece else []

    def _read(self) -> np.ndarray:
        if not self.tail.sample_rate:
            self.tail.read(0)  # reads the header only, so the block size is known
        block = int(MAX_READ_SECONDS * self.tail.sample_rate) if self.tail.sample_rate else 0
        samples = self.tail.read(block) if block else self.tail.read(0)
        self.behind = bool(block) and len(samples) >= block
        return samples

    def _push(self, samples: np.ndarray) -> list[Piece]:
        if self.chunker is None:
            self.chunker = Chunker(self.tail.sample_rate, start=self.origin, **self.chunk_options)
        return self.chunker.push(samples)

    def idle(self, now: float) -> bool:
        return now - self.last_growth >= IDLE_SECONDS

    @property
    def buffered(self) -> bool:
        return self.chunker is not None and self.chunker.buffered_seconds > 0


def tracks(base: Path) -> tuple[Path, Path]:
    return base.parent / f"{base.name}.mic.caf", base.parent / f"{base.name}.system.caf"


def find_recording(target: Path, *, now: float | None = None) -> Path | None:
    """The base path (``dir/2026-11-09_1430``) of the recording to follow, if there is one yet.

    ``target`` is a recordings directory (the newest recording in progress there), a
    track file, or a base path.
    """
    for suffix in (".mic.caf", ".system.caf"):
        if target.name.endswith(suffix):
            return target.parent / target.name.removesuffix(suffix)
    if target.is_dir():
        now = time.time() if now is None else now
        fresh = [p for p in target.glob("*.mic.caf") if now - p.stat().st_mtime < FRESH_SECONDS]
        if not fresh:
            return None
        newest = max(fresh, key=lambda p: p.stat().st_mtime)
        return newest.parent / newest.name.removesuffix(".mic.caf")
    return target if tracks(target)[0].exists() else None


class RecordingSource:
    """Lines from the recording in progress, as they are transcribed."""

    live = True
    speed = 1.0

    def __init__(
        self,
        target: Path,
        asr: SampleAsr,
        *,
        me: str = "Me",
        others: str = "Others",
        chunk_options: dict | None = None,
        poll_seconds: float = POLL_SECONDS,
        end_idle_seconds: float = END_IDLE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        speakers: LiveSpeakers | None = None,
        state_root: Path | None = None,
    ) -> None:
        self.target = target
        # <workspace>/live/recordings: per recording, what a restart can continue from.
        self.state_root = state_root
        self.asr = asr
        self.speakers = speakers
        self.names = (me, others)
        self.chunk_options = chunk_options or {}
        self.poll_seconds = poll_seconds
        self.end_idle_seconds = end_idle_seconds
        self.clock = clock
        self.recording: str | None = None
        self.state = "starting"
        self.channels: list[Channel] = []

    def now(self) -> float | None:
        """Where the recording is at this moment (not how far it is transcribed), once there is one."""
        if not self.channels:
            return None
        return max(c.tail.seconds_written for c in self.channels)

    def describe(self) -> dict:
        return {
            "kind": "recording",
            "name": self.recording or "",
            "watching": str(self.target),
            "state": self.state,
            "speakers": list(self.names),
        }

    async def produce(self, status: Status) -> AsyncIterator[list[Line]]:
        loop = asyncio.get_running_loop()
        executor = ThreadPoolExecutor(1, thread_name_prefix="live-asr")
        self._diar_executor = ThreadPoolExecutor(1, thread_name_prefix="live-diar")
        channels: list[Channel] = []
        try:
            self._status(status, "loading", "loading the ASR model")
            await loop.run_in_executor(executor, self.asr.load)
            self._status(status, "waiting", f"waiting for a recording in {self.target}")
            while (base := find_recording(self.target)) is None:
                await asyncio.sleep(self.poll_seconds)
            self.recording = base.name
            now = self.clock()
            channels = [
                Channel(name, CafTail(path), track, self.chunk_options, last_growth=now)
                for track, name, path in zip(("mic", "system"), self.names, tracks(base))
            ]
            self.channels = channels
            state = RecordingState(self.state_root / base.name) if self.state_root else None
            if state is not None:
                if self.speakers is not None:
                    self.speakers.book.attach(state.speakers_path, base.name)
                if past := self._resume(state, channels):
                    self._status(status, "resumed", f"{base.name}: {len(past)} lines from the previous run")
                    yield self.speakers.book.relabel(past) if self.speakers else past
            self._status(status, "recording", base.name)

            while not self._ended(base, channels):
                now = self.clock()
                for channel in channels:
                    for piece in channel.poll(now):
                        await self._transcribe(loop, executor, channel, piece, status)
                self._follow_idle(channels, now)
                if ready := self._hand_out(channels, state):
                    yield ready
                if not any(c.behind for c in channels):
                    await asyncio.sleep(self.poll_seconds)

            # The rest, a block per track at a time, handed out as it is done.
            while not all(c.drained for c in channels):
                for channel in channels:
                    if channel.drained:
                        continue
                    for piece in channel.drain_step():
                        await self._transcribe(loop, executor, channel, piece, status)
                    if channel.drained:
                        channel.covered = math.inf
                if ready := self._hand_out(channels, state):
                    yield ready
            if self.speakers is not None:
                self.speakers.book.finish()
            self._status(status, "ended", base.name)
        finally:
            for channel in channels:
                channel.tail.close()
            executor.shutdown(wait=False)
            self._diar_executor.shutdown(wait=False)

    def _resume(self, state: RecordingState, channels: list[Channel]) -> list[Line]:
        """Pick up where a previous run of this recording stopped; the lines it handed out."""
        progress = state.load()
        if progress is None:
            return []
        for channel in channels:
            covered = progress.covered.get(channel.track, 0.0)
            channel.lines = progress.pending.get(channel.track, [])
            # The minute before goes to the speaker window only, so the first piece
            # is diarized with its context, as it would have been.
            start = max(0.0, covered - WINDOW_SECONDS)
            if not channel.tail.seek(start):
                continue
            before = channel.tail.read(round((covered - start) * channel.tail.sample_rate))
            if self.speakers is not None and len(before):
                self.speakers.hear(channel.track, start, *to_asr_rate(before, channel.tail.sample_rate))
            channel.covered = channel.origin = start + len(before) / channel.tail.sample_rate
        return progress.lines

    def _hand_out(self, channels: list[Channel], state: RecordingState | None) -> list[Line]:
        """The lines ready to go, and a note of how far each track got, for a restart."""
        ready = self._ready(channels)
        if state is not None:
            state.save(
                ready,
                {
                    c.track: (c.covered if math.isfinite(c.covered) else c.tail.seconds_read, c.lines)
                    for c in channels
                },
            )
        return ready

    def _status(self, status: Status, state: str, detail: str) -> None:
        self.state = state
        status({"state": state, "detail": detail})

    async def _transcribe(self, loop, executor, channel: Channel, piece: Piece, status: Status) -> None:
        samples, rate = to_asr_rate(piece.samples, piece.rate)
        speakers = self.speakers
        if speakers is not None:
            speakers.hear(channel.track, piece.start, samples, rate)
        if not is_silent(piece.samples):
            # Transcription and diarization of the same piece run side by side.
            who = loop.run_in_executor(self._diar_executor, speakers.window, channel.track) if speakers else None
            try:
                segment = await loop.run_in_executor(executor, self.asr.transcribe_samples, samples, rate)
            except Exception as exc:  # noqa: BLE001 - one bad piece must not end the meeting
                segment = None
                status({"error": f"transcribing {channel.speaker} at {format_time(piece.start)} failed: {exc}"})
            diarization = None
            if who is not None:
                try:
                    diarization = speakers.place(channel.track, channel.speaker, piece.start, piece.end, *await who)
                except Exception as exc:  # noqa: BLE001 - the line keeps its track's speaker
                    status({"error": f"telling speakers apart at {format_time(piece.start)} failed: {exc}"})
            if segment is not None:
                fallback = speakers.last.get(channel.track) if speakers else None
                channel.lines += segment_lines(segment, piece, channel.speaker, diarization, fallback)
        channel.covered = max(channel.covered, piece.end)

    @staticmethod
    def _follow_idle(channels: list[Channel], now: float) -> None:
        """An idle track with nothing buffered is silent up to just behind the growing one."""
        growing = [c.tail.seconds_read for c in channels if not c.idle(now)]
        if not growing:
            return
        silent_until = max(growing) - IDLE_SLACK_SECONDS
        for channel in channels:
            if channel.idle(now) and not channel.buffered:
                channel.covered = max(channel.covered, silent_until)

    def _ready(self, channels: list[Channel]) -> list[Line]:
        """Released lines, under the names their speakers have by now."""
        ready = self._release(channels)
        return self.speakers.book.relabel(ready) if self.speakers else ready

    @staticmethod
    def _release(channels: list[Channel]) -> list[Line]:
        watermark = min(c.covered for c in channels)
        ready: list[Line] = []
        for channel in channels:
            ready += [line for line in channel.lines if line.start < watermark]
            channel.lines = [line for line in channel.lines if line.start >= watermark]
        return sorted(ready, key=lambda line: (line.start, line.end))

    def _ended(self, base: Path, channels: list[Channel]) -> bool:
        """The recorder merged the tracks into an .m4a, deleted them, or stopped writing long ago."""
        if (base.parent / f"{base.name}.m4a").exists():
            return True
        if not any(path.exists() for path in tracks(base)):
            return True
        now = self.clock()
        return all(now - c.last_growth >= self.end_idle_seconds for c in channels)
