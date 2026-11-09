"""Telling the meeting's voices apart while it runs, and naming them against the voice cache."""

from __future__ import annotations

import wave

import numpy as np
import pytest

from paraspeakrs.cache import SpeakerCache
from paraspeakrs.live import speakers
from paraspeakrs.live.listen import Piece, segment_lines
from paraspeakrs.live.speakers import SpeakerBook
from paraspeakrs.live.transcript import Line
from paraspeakrs.live.windows import LiveSpeakers, TrackAudio, WindowDiarizer
from paraspeakrs.models import AsrSegment, DiarizationSegment, Word

ANNA = [1.0, 0.0, 0.0, 0.0]
BEN = [0.0, 1.0, 0.0, 0.0]
CARL = [0.0, 0.0, 1.0, 0.0]


def _near(vector: list[float], wobble: float = 0.2) -> list[float]:
    return [x + wobble * (i == 3) for i, x in enumerate(vector)]


@pytest.fixture
def cache(tmp_path):
    cache = SpeakerCache(tmp_path / "speaker-cache.json", namespace="speakrs:256")
    cache.fold("Anna", ANNA, job_id="old-job", speaker_id="SPEAKER_00")
    return cache


@pytest.fixture
def book(cache, tmp_path):
    return SpeakerBook(cache, session_id="20261109-1430-live", path=tmp_path / "session" / "speakers.json")


def test_window_numbering_does_not_leak_into_the_meeting_speakers(book):
    # Every window numbers its speakers afresh; the meeting's ids must not jump with it.
    first = book.place("system", "Others", {"SPEAKER_00": ANNA, "SPEAKER_01": BEN}, {"SPEAKER_00": 20, "SPEAKER_01": 10}, {"SPEAKER_00": 5, "SPEAKER_01": 5})
    second = book.place("system", "Others", {"SPEAKER_00": _near(BEN), "SPEAKER_01": _near(ANNA)}, {"SPEAKER_00": 15, "SPEAKER_01": 15}, {"SPEAKER_00": 4, "SPEAKER_01": 4})

    assert second == {"SPEAKER_00": first["SPEAKER_01"], "SPEAKER_01": first["SPEAKER_00"]}
    assert [v["label"] for v in book.snapshot()] == ["Others 1", "Others 2"]


def test_a_new_voice_gets_a_new_speaker_but_a_few_seconds_do_not(book):
    book.place("system", "Others", {"SPEAKER_00": ANNA}, {"SPEAKER_00": 20}, {"SPEAKER_00": 5})
    cough = book.place("system", "Others", {"SPEAKER_00": ANNA, "SPEAKER_01": CARL}, {"SPEAKER_00": 20, "SPEAKER_01": 6}, {"SPEAKER_00": 5, "SPEAKER_01": 6})
    assert len(book.voices) == 1 and cough["SPEAKER_01"] == cough["SPEAKER_00"]

    carl = book.place("system", "Others", {"SPEAKER_00": ANNA, "SPEAKER_01": CARL}, {"SPEAKER_00": 20, "SPEAKER_01": 12}, {"SPEAKER_00": 2, "SPEAKER_01": 6})
    assert len(book.voices) == 2 and carl["SPEAKER_01"] != carl["SPEAKER_00"]


def test_tracks_keep_their_own_speakers(book):
    mic = book.place("mic", "Me", {"SPEAKER_00": ANNA}, {"SPEAKER_00": 9}, {"SPEAKER_00": 9})
    system = book.place("system", "Others", {"SPEAKER_00": ANNA}, {"SPEAKER_00": 9}, {"SPEAKER_00": 9})
    assert mic["SPEAKER_00"] != system["SPEAKER_00"]
    assert {v["display"] for v in book.snapshot()} == {"Me 1", "Others 1"}


def test_a_stored_voice_is_suggested_with_its_score_and_never_applied_by_itself(book):
    ids = book.place("system", "Others", {"SPEAKER_00": _near(ANNA), "SPEAKER_01": BEN}, {"SPEAKER_00": 12, "SPEAKER_01": 12}, {"SPEAKER_00": 9, "SPEAKER_01": 9})
    anna, ben = (next(v for v in book.snapshot() if v["id"] == ids[k]) for k in ("SPEAKER_00", "SPEAKER_01"))

    assert anna["suggested"] == "Anna" and anna["match"]["score"] == pytest.approx(0.981, abs=1e-3)
    # A suggestion is not a name: the transcript keeps the label until someone accepts it.
    assert anna["name"] is None and anna["display"] == "Others 1"
    # The nearest stored voice is still shown, with its weak score, but not offered.
    assert ben["match"] == {"name": "Anna", "score": 0.0} and ben["suggested"] is None


def test_naming_teaches_the_cache_and_renaming_moves_the_voice(book, cache):
    ids = book.place("system", "Others", {"SPEAKER_00": BEN}, {"SPEAKER_00": 9}, {"SPEAKER_00": 9})
    voice = ids["SPEAKER_00"]

    book.name(voice, "Ben")
    assert [m.ref for m in cache.members("Ben")] == [("live:20261109-1430-live", voice)]
    # The next meeting recognizes Ben from the cache alone.
    assert cache.suggest_embeddings({"x": BEN})["x"] == ("Ben", pytest.approx(1.0))

    book.name(voice, "Benjamin")
    assert cache.members("Ben") == [] and len(cache.members("Benjamin")) == 1

    book.name(voice, "  ")
    assert cache.members("Benjamin") == [] and book.snapshot()[0]["display"] == "Others 1"


def test_lines_take_the_name_given_after_they_were_heard(book):
    ids = book.place("mic", "Me", {"SPEAKER_00": BEN}, {"SPEAKER_00": 9}, {"SPEAKER_00": 9})
    lines = [Line(0, 2, "Me 1", "Hello.", speaker_id=ids["SPEAKER_00"]), Line(2, 3, "Others", "Hi.")]
    book.name(ids["SPEAKER_00"], "Ben")
    assert [l.speaker for l in book.relabel(lines)] == ["Ben", "Others"]


def test_the_book_is_kept_with_the_session_and_announces_changes(book, tmp_path):
    seen = []
    book.on_change = lambda: seen.append(len(book.voices))
    book.place("mic", "Me", {"SPEAKER_00": BEN}, {"SPEAKER_00": 9}, {"SPEAKER_00": 9})
    stored = (tmp_path / "session" / "speakers.json").read_text()
    assert seen == [1] and '"print": [0.0, 1.0, 0.0, 0.0]' in stored


def test_a_piece_is_split_where_the_speaker_changes():
    words = [Word(word="Hello.", start=0.2, end=0.6), Word(word="Hi", start=2.0, end=2.3), Word(word="there.", start=2.35, end=2.7), Word(word="Bye.", start=5.0, end=5.4)]
    segment = AsrSegment(text="Hello. Hi there. Bye.", start=0, end=6, words=words)
    piece = Piece(start=100.0, samples=np.zeros(6 * 16000, dtype=np.float32), rate=16000)
    diarization = [DiarizationSegment(speaker="S1", start=100.0, end=101.0), DiarizationSegment(speaker="S2", start=101.8, end=103.0)]

    lines = segment_lines(segment, piece, "Others", diarization, fallback="S2")

    # The last word is outside every segment: it goes to the track's latest speaker.
    assert [(l.speaker_id, l.text, l.start) for l in lines] == [("S1", "Hello.", 100.2), ("S2", "Hi there.", 102.0), ("S2", "Bye.", 105.0)]


def test_without_diarization_a_line_keeps_its_track():
    segment = AsrSegment(text="Hello.", start=0, end=1, words=[Word(word="Hello.", start=0.1, end=0.5)])
    piece = Piece(start=0.0, samples=np.zeros(16000, dtype=np.float32), rate=16000)
    [line] = segment_lines(segment, piece, "Others")
    assert (line.speaker, line.speaker_id) == ("Others", None)


def test_a_track_keeps_its_last_minute_on_the_recording_clock():
    audio = TrackAudio(rate=10, seconds=3)
    audio.add(0.0, np.ones(20))
    audio.add(2.0, np.full(20, 2.0))
    assert (audio.start, audio.end, len(audio.samples)) == (1.0, 4.0, 30)
    audio.add(9.0, np.ones(10))  # a gap: the window starts again
    assert (audio.start, audio.end) == (9.0, 10.0)


class FakeSidecar:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.last_centroids: dict[str, list[float]] = {}
        self.seconds = 0.0

    def diarize(self, path):
        with wave.open(str(path)) as fh:
            self.seconds = fh.getnframes() / fh.getframerate()
        if self.fail:
            raise RuntimeError("speakrs-diar failed with exit code 101: index out of bounds")
        self.last_centroids = {"SPEAKER_00": BEN}
        return [DiarizationSegment(speaker="SPEAKER_00", start=1.0, end=2.0)]


def test_the_window_is_diarized_on_the_recording_clock(tmp_path):
    audio = TrackAudio(rate=16000)
    audio.add(30.0, np.zeros(16000 * 5, dtype=np.float32))
    sidecar = FakeSidecar()
    segments, prints = WindowDiarizer(sidecar, tmp_path)("system", audio)
    assert sidecar.seconds == 5.0
    assert [(s.start, s.end) for s in segments] == [(31.0, 32.0)] and prints == {"SPEAKER_00": BEN}


def test_a_sidecar_crash_on_a_quiet_window_means_nobody(tmp_path):
    audio = TrackAudio(rate=16000)
    audio.add(0.0, np.zeros(1600, dtype=np.float32))
    assert WindowDiarizer(FakeSidecar(fail=True), tmp_path)("mic", audio) == ([], {})


def test_window_speakers_become_meeting_speakers_and_the_latest_is_remembered(book):
    live = LiveSpeakers(book, lambda track, audio: ([], {}))
    segments = [DiarizationSegment(speaker="SPEAKER_00", start=0, end=40), DiarizationSegment(speaker="SPEAKER_01", start=40, end=55)]
    placed = live.place("system", "Others", 50.0, 55.0, segments, {"SPEAKER_00": ANNA, "SPEAKER_01": BEN})
    ids = {s.speaker for s in placed}
    assert len(ids) == 2 and live.last["system"] == placed[1].speaker
    # Only the seconds of the new piece weigh into a voice: SPEAKER_00 spoke none of them.
    assert sorted(v.seconds for v in book.voices.values()) == [0.0, 5.0]


def _bursts(audio: TrackAudio) -> list[tuple[float, float]]:
    frame = audio.rate // 100
    frames = len(audio.samples) // frame
    rms = np.sqrt(np.mean(audio.samples[: frames * frame].reshape(frames, frame) ** 2, axis=1))
    edges = np.flatnonzero(np.diff(np.concatenate(([0], (rms > 0.05).astype(int), [0]))))
    return [(audio.start + a * frame / audio.rate, audio.start + b * frame / audio.rate) for a, b in zip(edges[::2], edges[1::2])]


async def test_a_followed_recording_names_two_voices_on_one_track(tmp_path, monkeypatch, book):
    from test_live_listen import ToneAsr, _append, _signal

    from paraspeakrs.live import listen
    from paraspeakrs.live.caf import write_header
    from paraspeakrs.live.listen import RecordingSource, tracks

    monkeypatch.setattr(listen, "IDLE_SECONDS", 0.05)
    # The tones are short; what is tested here is the wiring, not the new-voice minimum.
    monkeypatch.setattr(speakers, "MIN_NEW_SECONDS", 2.0)

    def diarize(track, audio):
        # Whoever speaks before 4 s sounds like Anna, after it like Ben; each window
        # numbers them from SPEAKER_00 again, as speakrs does.
        segments = [DiarizationSegment(speaker="early" if a < 4 else "late", start=a, end=b) for a, b in _bursts(audio)]
        local = {name: f"SPEAKER_{i:02}" for i, name in enumerate(dict.fromkeys(s.speaker for s in segments))}
        voice = {"early": ANNA, "late": BEN}
        return (
            [s.model_copy(update={"speaker": local[s.speaker]}) for s in segments],
            {local[name]: voice[name] for name in local},
        )

    base = tmp_path / "rec"
    mic_path, system_path = tracks(base)
    with mic_path.open("wb") as mic, system_path.open("wb") as system:
        write_header(mic, sample_rate=16000)
        write_header(system, sample_rate=16000)
        _append(mic, np.zeros(8 * 16000, dtype=np.float32))
        _append(system, _signal(8, [1.0, 5.0], length=2.5))
    source = RecordingSource(
        mic_path,
        ToneAsr(),
        chunk_options={"min_seconds": 1, "max_seconds": 4, "pause_seconds": 0.3},
        poll_seconds=0.01,
        end_idle_seconds=0.2,
        speakers=LiveSpeakers(book, diarize),
    )
    statuses: list[dict] = []

    got = [line for lines in [ls async for ls in source.produce(statuses.append)] for line in lines]

    assert not [s for s in statuses if "error" in s]
    assert [(l.speaker, round(l.start, 2)) for l in got] == [("Others 1", 1.0), ("Others 2", 5.0)]
    # The first voice is Anna's stored one: offered, not applied.
    assert [v["suggested"] for v in book.snapshot()] == ["Anna", None]


def test_a_restart_keeps_the_voices_and_does_not_teach_the_cache_twice(cache, tmp_path):
    path = tmp_path / "recordings" / "2026-11-09_1416" / "speakers.json"
    first = SpeakerBook(cache, session_id="run-1")
    first.attach(path, "2026-11-09_1416")
    voice = first.place("system", "Others", {"SPEAKER_00": BEN}, {"SPEAKER_00": 9}, {"SPEAKER_00": 9})["SPEAKER_00"]
    first.name(voice, "Ben")

    again = SpeakerBook(cache, session_id="run-2")
    again.attach(path, "2026-11-09_1416")
    later = again.place("system", "Others", {"SPEAKER_00": _near(BEN)}, {"SPEAKER_00": 9}, {"SPEAKER_00": 9})
    again.finish()

    assert later["SPEAKER_00"] == voice and again.voices[voice].display == "Ben"
    assert [m.ref for m in cache.members("Ben")] == [("live:2026-11-09_1416", voice)]


def _live(book):
    live = LiveSpeakers(book, lambda track, audio: ([], {}))
    events: list[dict] = []
    live.on_mark = events.append
    return live, events


def test_marking_who_speaks_now_names_that_voice_once_the_moment_is_diarized(book, monkeypatch):
    monkeypatch.setattr(speakers, "MIN_NEW_SECONDS", 2.0)
    live, events = _live(book)
    assert live.mark(30.0, " Anna ")["state"] == "waiting"
    talk = [DiarizationSegment(speaker="SPEAKER_00", start=20, end=27.5), DiarizationSegment(speaker="SPEAKER_01", start=27.5, end=31)]
    prints = {"SPEAKER_00": BEN, "SPEAKER_01": CARL}

    live.place("system", "Others", 25.0, 31.0, talk, prints)
    assert events == [] and all(v.name is None for v in book.voices.values())  # not settled: 31 < 30 + 5

    live.place("system", "Others", 31.0, 36.0, talk, prints)
    [event] = events
    named = book.voices[event["speaker"]]
    # SPEAKER_01 spoke 2.5 s of the 4.5 s around the click, SPEAKER_00 only 1.5 s.
    assert (event["state"], named.name, named.label) == ("named", "Anna", "Others 2")
    assert live.marks == []


def test_a_mark_with_nobody_speaking_is_dropped_and_said_so(book):
    live, events = _live(book)
    live.mark(10.0, "Anna")
    later = [DiarizationSegment(speaker="SPEAKER_00", start=20, end=26)]
    live.place("mic", "Me", 20.0, 26.0, later, {"SPEAKER_00": BEN})
    assert [(e["state"], e["name"]) for e in events] == [("nobody", "Anna")]
    assert all(v.name is None for v in book.voices.values())
