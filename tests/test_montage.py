"""Speaker montages: one file per speaker that is enough to recognize them by.

A single clip is often a cough, a "yeah", or crosstalk. The agent flow gives the
user exactly one file per speaker, so that file has to carry several takes.
"""

import wave
from pathlib import Path

from paraspeakrs.models import DiarizationSegment
from paraspeakrs.snippets import MONTAGE_RATE, montage_segments, write_montage


def _seg(start: float, end: float) -> DiarizationSegment:
    return DiarizationSegment(speaker="SPEAKER_00", start=start, end=end)


def test_takes_the_three_longest_in_time_order():
    segments = [_seg(0, 3), _seg(10, 30), _seg(40, 42), _seg(50, 56), _seg(60, 64)]

    chosen = montage_segments(segments)

    # Longest three are 10-30, 50-56, 60-64; played in the order they were said,
    # and each capped so one monologue does not become the whole sample.
    assert chosen == [(10, 20), (50, 56), (60, 64)]


def test_three_long_takes_are_enough_without_topping_up():
    chosen = montage_segments([_seg(0, 9), _seg(20, 29), _seg(40, 49), _seg(60, 61)])

    assert chosen == [(0, 9), (20, 29), (40, 49)]


def test_tops_up_with_shorter_takes_until_eight_seconds():
    """One 1.7 s take of someone who spoke for 9 s is too little to recognize them by."""
    short = [_seg(t, t + 1.4) for t in (5, 10, 15, 20, 25)]
    segments = [_seg(0, 1.7), *short, _seg(35, 35.5), _seg(40, 40.3)]

    chosen = montage_segments(segments)

    total = sum(end - start for start, end in chosen)
    assert total >= 8.0
    # Longest first, and no more than it takes: the two shortest are not needed.
    assert (35, 35.5) not in chosen and (40, 40.3) not in chosen
    assert chosen == sorted(chosen)


def test_a_speaker_with_little_audio_gets_all_of_it():
    """A speaker who only ever said "yes" still needs a sample to be named from."""
    chosen = montage_segments([_seg(0, 0.4), _seg(5, 6.2), _seg(9, 9.3)])

    assert chosen == [(0, 0.4), (5, 6.2), (9, 9.3)]


class WavAudio:
    """Exports real silent wavs of the requested length, like ffmpeg would."""

    def export_chunk(self, input_path, output_path, start, end):
        frames = int(round((end - start) * MONTAGE_RATE))
        with wave.open(str(output_path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(MONTAGE_RATE)
            out.writeframes(b"\x00\x00" * frames)
        return output_path


def _seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes() / wav.getframerate()


def test_montage_is_the_clips_plus_separators(tmp_path):
    out = write_montage(WavAudio(), Path("source.wav"), tmp_path / "m.wav", [(0, 2), (10, 13)])

    single = write_montage(WavAudio(), Path("source.wav"), tmp_path / "s.wav", [(0, 2)])

    separator = _seconds(out) - 5.0
    assert separator > 0.2, "clips must be audibly separated"
    # One clip needs no separator at all.
    assert abs(_seconds(single) - 2.0) < 0.01
    # Scratch clips are not left next to the montage.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["m.wav", "s.wav"]
