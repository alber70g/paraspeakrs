import json
import subprocess
import wave
from pathlib import Path

import pytest

from paraspeakrs.audio import AudioPreparer
from paraspeakrs.diarization import (
    SPEAKRS_CONTRACT_VERSION,
    SpeakrsDiarizer,
)


class FakeAudio:
    """Only the AudioPreparer methods the speakrs backend touches."""

    def __init__(self, channels: int = 1, silent: frozenset[str] = frozenset()) -> None:
        self.channels = channels
        self.splits: list[Path] = []
        # File names whose track has no sound at all.
        self.silent = silent

    def channel_count(self, audio_path: Path) -> int:
        return self.channels

    def is_silent(self, audio_path: Path) -> bool:
        return audio_path.name in self.silent

    def split_stereo_to_mono(self, input_path: Path, left_path: Path, right_path: Path):
        self.splits.append(input_path)
        return left_path, right_path


def _payload(**overrides):
    payload = {
        "version": SPEAKRS_CONTRACT_VERSION,
        "file_id": "audio",
        "embedding_dim": 3,
        "segments": [
            {"speaker": "SPEAKER_00", "start": 0.0, "end": 2.0},
            {"speaker": "SPEAKER_01", "start": 2.0, "end": 4.0},
        ],
        "centroids": {"SPEAKER_00": [1.0, 0.0, 0.0], "SPEAKER_01": [0.0, 1.0, 0.0]},
    }
    payload.update(overrides)
    return payload


def _runner(payload=None, returncode=0, stdout=None, stderr=""):
    """Stand-in for subprocess.run that records the command it was handed."""
    calls: list[list[str]] = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        text = stdout if stdout is not None else json.dumps(payload if payload is not None else _payload())
        return subprocess.CompletedProcess(cmd, returncode, stdout=text, stderr=stderr)

    run.calls = calls
    return run


def test_parses_segments_and_centroids(tmp_path):
    diarizer = SpeakrsDiarizer(FakeAudio(), binary=Path("speakrs-diar"), runner=_runner())

    segments = diarizer.diarize(tmp_path / "audio.wav")

    assert [(s.speaker, s.start, s.end) for s in segments] == [
        ("SPEAKER_00", 0.0, 2.0),
        ("SPEAKER_01", 2.0, 4.0),
    ]
    assert diarizer.last_centroids == {
        "SPEAKER_00": [1.0, 0.0, 0.0],
        "SPEAKER_01": [0.0, 1.0, 0.0],
    }


def test_passes_mode_and_models_dir_to_the_binary(tmp_path):
    runner = _runner()
    diarizer = SpeakrsDiarizer(
        FakeAudio(),
        binary=Path("/opt/speakrs-diar"),
        models_dir=Path("/models"),
        mode="coreml",
        runner=runner,
    )

    diarizer.diarize(tmp_path / "audio.wav")

    assert runner.calls[0] == [
        "/opt/speakrs-diar",
        "--mode",
        "coreml",
        "--models-dir",
        "/models",
        str(tmp_path / "audio.wav"),
    ]


def test_omits_models_dir_when_unset(tmp_path):
    runner = _runner()
    diarizer = SpeakrsDiarizer(FakeAudio(), binary=Path("speakrs-diar"), runner=runner)

    diarizer.diarize(tmp_path / "audio.wav")

    assert "--models-dir" not in runner.calls[0]


def test_nonzero_exit_reports_stderr(tmp_path):
    runner = _runner(returncode=1, stdout="", stderr="cannot read audio.wav")
    diarizer = SpeakrsDiarizer(FakeAudio(), binary=Path("speakrs-diar"), runner=runner)

    with pytest.raises(RuntimeError, match="cannot read audio.wav"):
        diarizer.diarize(tmp_path / "audio.wav")


def test_the_sidecar_runs_with_rust_backtraces_on(tmp_path):
    """Exit code 101 is a Rust panic inside speakrs or onnxruntime. Its message
    alone rarely says where, and the failure happens on a machine we cannot
    reach, so the backtrace has to be in the stderr we keep."""
    seen = {}
    base = _runner()

    def runner(cmd, **kwargs):
        seen.update(kwargs.get("env") or {})
        return base(cmd, **kwargs)

    SpeakrsDiarizer(FakeAudio(), binary=Path("speakrs-diar"), runner=runner).diarize(tmp_path / "audio.wav")

    assert seen.get("RUST_BACKTRACE") == "1"


def test_malformed_json_is_rejected(tmp_path):
    diarizer = SpeakrsDiarizer(
        FakeAudio(), binary=Path("speakrs-diar"), runner=_runner(stdout="not json")
    )

    with pytest.raises(RuntimeError, match="invalid JSON"):
        diarizer.diarize(tmp_path / "audio.wav")


def test_contract_version_mismatch_is_rejected(tmp_path):
    """A stale binary must fail loudly rather than be parsed against the wrong shape."""
    diarizer = SpeakrsDiarizer(
        FakeAudio(),
        binary=Path("speakrs-diar"),
        runner=_runner(payload=_payload(version=SPEAKRS_CONTRACT_VERSION + 1)),
    )

    with pytest.raises(RuntimeError, match="contract version"):
        diarizer.diarize(tmp_path / "audio.wav")


def test_stereo_input_is_split_and_speakers_prefixed(tmp_path):
    """Both channels are diarized independently, so speaker ids must not collide."""
    audio = FakeAudio(channels=2)
    diarizer = SpeakrsDiarizer(audio, binary=Path("speakrs-diar"), runner=_runner())

    segments = diarizer.diarize(tmp_path / "audio.wav")

    assert audio.splits == [tmp_path / "audio.wav"]
    assert [s.speaker for s in segments] == [
        "L_SPEAKER_00",
        "R_SPEAKER_00",
        "L_SPEAKER_01",
        "R_SPEAKER_01",
    ]
    assert [s.start for s in segments] == [0.0, 0.0, 2.0, 2.0]
    assert set(diarizer.last_centroids) == {
        "L_SPEAKER_00",
        "L_SPEAKER_01",
        "R_SPEAKER_00",
        "R_SPEAKER_01",
    }


def test_empty_result_clears_stale_centroids(tmp_path):
    """last_centroids is a side channel read after diarize(); it must not survive
    into a later run that found no speakers."""
    diarizer = SpeakrsDiarizer(FakeAudio(), binary=Path("speakrs-diar"), runner=_runner())
    diarizer.diarize(tmp_path / "audio.wav")

    diarizer.runner = _runner(payload=_payload(segments=[], centroids={}))
    assert diarizer.diarize(tmp_path / "audio.wav") == []
    assert diarizer.last_centroids == {}


def test_a_silent_track_has_no_speakers_and_skips_the_sidecar(tmp_path):
    """speakrs-diar panics on a track with no sound (a meeting where the other
    side never spoke); the answer is nobody, not a failed run."""
    runner = _runner()
    audio = FakeAudio(silent=frozenset({"audio.wav"}))
    diarizer = SpeakrsDiarizer(audio, binary=Path("speakrs-diar"), runner=runner)

    assert diarizer.diarize(tmp_path / "audio.wav") == []
    assert runner.calls == []
    assert diarizer.last_centroids == {}


def test_a_silent_channel_is_skipped_and_the_other_still_diarized(tmp_path):
    runner = _runner()
    audio = FakeAudio(channels=2, silent=frozenset({"audio-right.wav"}))
    diarizer = SpeakrsDiarizer(audio, binary=Path("speakrs-diar"), runner=runner)

    segments = diarizer.diarize(tmp_path / "audio.wav")

    assert len(runner.calls) == 1
    assert {s.speaker for s in segments} == {"L_SPEAKER_00", "L_SPEAKER_01"}


def _wav(path: Path, samples: list[int]) -> Path:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(b"".join(value.to_bytes(2, "little", signed=True) for value in samples))
    return path


def test_is_silent_reads_the_peak_of_a_real_wav(tmp_path):
    preparer = AudioPreparer()
    # Dither-level noise, like a muted capture.
    assert preparer.is_silent(_wav(tmp_path / "quiet.wav", [0, 3, -5, 2] * 40_000))
    # One audible sample anywhere is enough to diarize.
    assert not preparer.is_silent(_wav(tmp_path / "speech.wav", [0] * 100_000 + [-2000] + [0] * 10))
