"""Transcribing a recording in progress: pieces, line order across tracks, the whole follow."""

from __future__ import annotations

import asyncio
import os
import time

import numpy as np
import pytest

from paraspeakrs.live import listen
from paraspeakrs.live.caf import CafTail, write_header
from paraspeakrs.live.listen import (
    Channel,
    Chunker,
    Piece,
    RecordingSource,
    find_recording,
    is_silent,
    piece_lines,
    to_asr_rate,
    tracks,
)
from paraspeakrs.live.transcript import Line
from paraspeakrs.models import AsrSegment, Word

RATE = 16_000


def _signal(seconds: float, bursts: list[float], length: float = 0.5) -> np.ndarray:
    """Silence with a loud tone of ``length`` seconds starting at each burst time."""
    out = np.zeros(int(seconds * RATE), dtype=np.float32)
    t = np.arange(int(length * RATE)) / RATE
    for start in bursts:
        a = int(start * RATE)
        out[a : a + len(t)] = 0.5 * np.sin(2 * np.pi * 440 * t)[: len(out) - a]
    return out


class ToneAsr:
    """Hears one word per tone burst, timed where the burst is."""

    def __init__(self) -> None:
        self.loaded = False
        self.pieces: list[float] = []

    def load(self) -> None:
        self.loaded = True

    def transcribe_samples(self, samples: np.ndarray, sample_rate: int) -> AsrSegment:
        self.pieces.append(len(samples) / sample_rate)
        frame = sample_rate // 100
        frames = len(samples) // frame
        rms = np.sqrt(np.mean(samples[: frames * frame].reshape(frames, frame) ** 2, axis=1))
        loud = (rms > 0.05).astype(int)
        edges = np.flatnonzero(np.diff(np.concatenate(([0], loud, [0]))))
        words = [
            Word(word=f"tone{i}.", start=a * frame / sample_rate, end=b * frame / sample_rate)
            for i, (a, b) in enumerate(zip(edges[::2], edges[1::2]))
        ]
        return AsrSegment(text=" ".join(w.word for w in words), start=0, end=len(samples) / sample_rate, words=words)


def test_a_piece_ends_at_the_first_pause_after_the_minimum():
    chunker = Chunker(RATE, min_seconds=2, max_seconds=10, pause_seconds=0.5)
    pieces = chunker.push(_signal(6, [0.0, 1.0, 1.6, 4.0], length=0.4))
    # Pauses: 0.4-1.0 (too early), 1.4-1.6 (too short), 2.0-4.0 (cut in the middle of its
    # first 0.5 s), then 4.4-6.0 for the second piece.
    assert [round(p.start, 2) for p in pieces] == [0.0, 2.24]
    assert 4.4 < pieces[1].end < 5.0
    assert chunker.buffered_seconds == pytest.approx(6 - pieces[1].end)


def test_speech_without_a_pause_is_cut_at_the_maximum_at_its_quietest():
    chunker = Chunker(RATE, min_seconds=2, max_seconds=4, pause_seconds=0.5)
    t = np.arange(5 * RATE) / RATE
    level = np.where((t > 3.0) & (t < 3.3), 0.01, 0.5)  # a short dip, never a full pause
    pieces = chunker.push((level * np.sin(2 * np.pi * 300 * t)).astype(np.float32))
    assert len(pieces) == 1
    assert 3.0 <= pieces[0].end <= 3.3
    rest = chunker.flush()
    assert rest.start == pytest.approx(pieces[0].end)
    assert chunker.flush() is None


def test_silence_is_not_transcribed():
    assert is_silent(np.zeros(100, dtype=np.float32))
    assert is_silent(np.full(100, 0.001, dtype=np.float32))
    assert not is_silent(_signal(1, [0.2]))


def test_piece_lines_are_on_the_recording_clock_and_split_into_sentences():
    piece = Piece(start=60.0, samples=_signal(4, [0.5, 2.0]), rate=RATE)
    lines = piece_lines(ToneAsr(), piece, "Me")
    assert [(l.speaker, l.text, round(l.start, 2)) for l in lines] == [("Me", "tone0.", 60.5), ("Me", "tone1.", 62.0)]


def _channel(tmp_path, name, speaker, covered, lines=(), last_growth=0.0):
    channel = Channel(speaker, CafTail(tmp_path / name), last_growth=last_growth)
    channel.covered = covered
    channel.lines = list(lines)
    return channel


def test_a_line_waits_until_both_tracks_are_past_it(tmp_path):
    me = _channel(tmp_path, "a", "Me", 20.0, [Line(3, 4, "Me", "a"), Line(18, 19, "Me", "c")])
    others = _channel(tmp_path, "b", "Others", 10.0, [Line(8, 9, "Others", "b")])

    released = RecordingSource._release([me, others])

    assert [l.text for l in released] == ["a", "b"]
    assert [l.text for l in me.lines] == ["c"]


def test_an_idle_track_is_silent_up_to_just_behind_the_growing_one(tmp_path):
    me = _channel(tmp_path, "a", "Me", 0.0, last_growth=100.0)
    me.tail.frames_read, me.tail.sample_rate = 40 * RATE, RATE
    others = _channel(tmp_path, "b", "Others", 5.0, last_growth=90.0)

    RecordingSource._follow_idle([me, others], now=100.0)

    assert others.covered == 40 - listen.IDLE_SLACK_SECONDS
    assert me.covered == 0.0


def test_find_recording_takes_the_newest_recording_in_progress(tmp_path):
    old, new = tracks(tmp_path / "2026-11-09_0900")[0], tracks(tmp_path / "2026-11-09_1430")[0]
    old.write_bytes(b"")
    new.write_bytes(b"")
    now = time.time()
    os.utime(old, (now - 60, now - 60))
    assert find_recording(tmp_path, now=now) == tmp_path / "2026-11-09_1430"
    os.utime(new, (now - 60, now - 60))
    assert find_recording(tmp_path, now=now) is None  # nothing is being recorded
    assert find_recording(old) == tmp_path / "2026-11-09_0900"
    assert find_recording(tmp_path / "2026-11-09_1430") == tmp_path / "2026-11-09_1430"
    assert find_recording(tmp_path / "2026-11-10_1000") is None


def _append(fh, samples: np.ndarray) -> None:
    fh.write((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
    fh.flush()


async def test_follows_a_recording_from_its_start_to_the_merge(tmp_path, monkeypatch):
    monkeypatch.setattr(listen, "IDLE_SECONDS", 0.1)
    asr = ToneAsr()
    source = RecordingSource(
        tmp_path,
        asr,
        chunk_options={"min_seconds": 1, "max_seconds": 4, "pause_seconds": 0.3},
        poll_seconds=0.01,
    )
    statuses: list[dict] = []
    got: list[Line] = []

    async def follow():
        async for lines in source.produce(statuses.append):
            got.extend(lines)

    task = asyncio.create_task(follow())
    await asyncio.sleep(0.05)
    assert asr.loaded and statuses[-1]["state"] == "waiting"

    base = tmp_path / "2026-11-09_1430"
    mic_path, system_path = tracks(base)
    mic_audio = _signal(12, [1.0, 9.0])
    system_audio = _signal(6, [5.0])  # then nothing plays: the tap stops delivering
    with mic_path.open("wb") as mic, system_path.open("wb") as system:
        write_header(mic, sample_rate=RATE)
        write_header(system, sample_rate=RATE)
        for i in range(0, len(mic_audio), RATE // 2):
            _append(mic, mic_audio[i : i + RATE // 2])
            if i < len(system_audio):
                _append(system, system_audio[i : i + RATE // 2])
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.3)
        # Live: every line is out before the recording stops. The system track went
        # quiet after 6 s and does not hold back the microphone's line at 9 s.
        assert [(l.speaker, l.start) for l in got] == [("Me", 1.0), ("Others", 5.0), ("Me", 9.0)]
    mic_path.unlink()
    system_path.unlink()
    (tmp_path / "2026-11-09_1430.m4a").write_bytes(b"")
    await asyncio.wait_for(task, 5)

    assert [(l.speaker, l.text, l.start) for l in got] == [
        ("Me", "tone0.", 1.0),
        ("Others", "tone0.", 5.0),
        ("Me", "tone0.", 9.0),
    ]
    assert [s["state"] for s in statuses] == ["loading", "waiting", "recording", "ended"]
    assert source.describe()["name"] == "2026-11-09_1430"
    assert max(asr.pieces) <= 4.0 + 1e-6


async def test_a_failing_piece_is_reported_and_the_follow_goes_on(tmp_path, monkeypatch):
    monkeypatch.setattr(listen, "IDLE_SECONDS", 0.05)

    class Broken(ToneAsr):
        def transcribe_samples(self, samples, sample_rate):
            raise RuntimeError("onnx said no")

    base = tmp_path / "rec"
    mic_path, system_path = tracks(base)
    with mic_path.open("wb") as fh:
        write_header(fh, sample_rate=RATE)
        _append(fh, _signal(2, [0.5]))
    statuses: list[dict] = []
    source = RecordingSource(mic_path, Broken(), poll_seconds=0.01, end_idle_seconds=0.2)

    got = [lines async for lines in source.produce(statuses.append)]

    assert got == []
    errors = [s["error"] for s in statuses if "error" in s]
    assert errors == ["transcribing Me at 00:00:00 failed: onnx said no"]
    assert statuses[-1]["state"] == "ended"


def test_48k_tracks_reach_the_model_at_16k_without_aliasing():
    t = np.arange(48_000) / 48_000
    speech_band = np.sin(2 * np.pi * 1000 * t).astype(np.float32)
    above_nyquist = np.sin(2 * np.pi * 11_000 * t).astype(np.float32)

    low, rate = to_asr_rate(speech_band, 48_000)
    high, _ = to_asr_rate(above_nyquist, 48_000)

    assert rate == 16_000 and len(low) == 16_000
    middle = slice(1000, 15_000)
    assert np.max(np.abs(low[middle])) == pytest.approx(1.0, abs=0.02)
    assert np.max(np.abs(high[middle])) < 0.05
    # Same clock: the waveform is not shifted by the filter.
    np.testing.assert_allclose(low[middle], speech_band[::3][middle], atol=0.02)
    same, rate = to_asr_rate(speech_band[:100], 44_100)
    assert rate == 44_100 and same is not None
