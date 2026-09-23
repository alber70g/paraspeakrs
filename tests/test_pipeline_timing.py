from pathlib import Path
import time

from paraspeakrs.models import AsrSegment, DiarizationSegment
from paraspeakrs.pipeline import TranscriptionPipeline, _run_with_heartbeat


class FakeAudio:
    def materialize(self, work_dir, *, file_path=None, file_url=None, file_base64=None):
        return Path("raw.wav")

    def normalize_mono_16khz(self, input_path, output_path):
        return Path("normalized.wav")

    def normalize_16khz_preserving_channels(self, input_path, output_path):
        return Path("normalized-diar.wav")

    def duration_seconds(self, input_path):
        return 5.0

    def export_chunk(self, input_path, output_path, start, end):
        return Path("chunk.wav")

    def export_chunk_preserving_channels(self, input_path, output_path, start, end):
        return Path("chunk-diar.wav")


class FakeDiarizer:
    name = "fake"

    def diarize(self, audio_path):
        return [DiarizationSegment(speaker="SPEAKER_00", start=0.0, end=5.0)]


class ZeroDurationAsr:
    def transcribe(self, audio_path):
        return AsrSegment(text="external text", start=0.0, end=0.0)


class FakeCache:
    def resolve_embeddings(self, embeddings):
        return {}

    def resolve(self, segments):
        return {}


def test_pipeline_uses_chunk_end_when_asr_has_no_duration(tmp_path):
    pipeline = TranscriptionPipeline(
        diarizer=FakeDiarizer(),
        asr=ZeroDurationAsr(),
        audio=FakeAudio(),
        speaker_cache=FakeCache(),
    )

    result = pipeline.run(work_dir=tmp_path, file_path=Path("input.wav"))

    assert result.segments[0].start == 0.0
    assert result.segments[0].end == 5.0


def test_pipeline_reports_progress_steps(tmp_path):
    progress = []
    pipeline = TranscriptionPipeline(
        diarizer=FakeDiarizer(),
        asr=ZeroDurationAsr(),
        audio=FakeAudio(),
        speaker_cache=FakeCache(),
    )

    pipeline.run(
        work_dir=tmp_path,
        file_path=Path("input.wav"),
        progress=lambda step, detail, percent: progress.append((step, detail, percent)),
    )

    steps = [step for step, _, _ in progress]
    assert steps == [
        "materialize",
        "normalize",
        "duration",
        "diarization",
        "diarization",
        "embeddings",
        "transcription",
        "merge",
        "speaker-cache",
    ]
    # Progress must name the backend that actually ran, not a hardcoded one, so
    # that the UI cannot claim senko while speakrs is doing the work.
    assert progress[3][1] == f"running {FakeDiarizer.name} speaker diarization"
    assert progress[4][1] == f"completed {FakeDiarizer.name} diarization: 1 segments"
    assert progress[6][1] == "transcribing chunk 1 of 1"
    assert progress[-1][2] == 95


def test_diarization_heartbeat_reports_elapsed_progress():
    progress = []

    result = _run_with_heartbeat(
        lambda step, detail, percent: progress.append((step, detail, percent)),
        step="diarization",
        detail="running senko speaker diarization",
        start_percent=25,
        max_percent=44,
        interval_seconds=0.01,
        work=lambda: time.sleep(0.03) or "done",
    )

    assert result == "done"
    assert progress[0] == ("diarization", "running senko speaker diarization", 25)
    assert any("elapsed" in (detail or "") for _, detail, _ in progress[1:])


def test_prepare_labeling_uses_probe_audio_for_long_files(tmp_path):
    audio = FakeAudio()
    pipeline = TranscriptionPipeline(
        diarizer=FakeDiarizer(),
        asr=ZeroDurationAsr(),
        audio=audio,
        speaker_cache=FakeCache(),
    )

    artifacts = pipeline.prepare_labeling(work_dir=tmp_path, file_path=Path("input.wav"), probe_seconds=2.0)

    assert artifacts.normalized_audio_path == Path("chunk.wav")
