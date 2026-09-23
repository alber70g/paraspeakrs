from pathlib import Path

from paraspeakrs.models import DiarizationSegment
from paraspeakrs.snippets import export_label_snippets


class FakeAudio:
    def __init__(self):
        self.calls = []

    def export_chunk(self, input_path, output_path, start, end):
        self.calls.append((input_path, output_path, start, end))
        output_path.write_bytes(b"")
        return output_path


def test_export_label_snippets_uses_longest_segments(tmp_path):
    audio = FakeAudio()
    snippets = export_label_snippets(
        audio,
        Path("normalized.wav"),
        tmp_path,
        [
            DiarizationSegment(speaker="SPEAKER_00", start=0, end=1),
            DiarizationSegment(speaker="SPEAKER_00", start=10, end=30),
            DiarizationSegment(speaker="SPEAKER_00", start=40, end=46),
        ],
        max_per_speaker=2,
        max_seconds=12,
        min_seconds=2,
    )

    assert snippets == {"SPEAKER_00": [tmp_path / "SPEAKER_00-01.wav", tmp_path / "SPEAKER_00-02.wav"]}
    assert audio.calls[0][2:] == (10, 22)


def test_export_label_snippets_skips_export_when_output_already_exists(tmp_path):
    audio = FakeAudio()
    segments = [DiarizationSegment(speaker="SPEAKER_00", start=10, end=30)]

    first = export_label_snippets(audio, Path("normalized.wav"), tmp_path, segments, max_per_speaker=1)
    assert len(audio.calls) == 1

    second = export_label_snippets(audio, Path("normalized.wav"), tmp_path, segments, max_per_speaker=1)

    assert len(audio.calls) == 1
    assert second == first
