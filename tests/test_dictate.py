"""`paraspeakrs dictate`: what the recorder's dictation shortcut relies on."""

from __future__ import annotations

import io
import json
import wave
from pathlib import Path

import numpy as np
import pytest

from paraspeakrs import cli, dictate, factory, model_fetch
from paraspeakrs.config import Settings

RATE = 16_000


class FakeAsr:
    """Returns one word per piece, so a test can count the pieces it was given."""

    def __init__(self) -> None:
        self.loaded = False
        self.pieces: list[int] = []

    def load(self) -> None:
        self.loaded = True

    def transcribe_text(self, samples, sample_rate: int) -> str:
        self.pieces.append(len(samples))
        return "hello" if len(self.pieces) == 1 else "again"


def _write_wav(path: Path, samples: np.ndarray, rate: int = RATE) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())
    return path


def _loud(seconds: float, rate: int = RATE) -> np.ndarray:
    return np.random.default_rng(0).uniform(-0.5, 0.5, int(seconds * rate))


def test_one_shot_prints_only_the_text_because_the_app_types_stdout(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(dictate, "build_asr", lambda settings: FakeAsr())
    wav = _write_wav(tmp_path / "d.wav", _loud(2))
    monkeypatch.setattr(cli.sys, "argv", ["paraspeakrs", "dictate", str(wav)])

    cli._main()

    assert capsys.readouterr().out == "hello\n"


def test_dictate_builds_no_diarization_pipeline(monkeypatch, tmp_path):
    """A pipeline build would start speakrs and could download models."""
    monkeypatch.setattr(dictate, "build_asr", lambda settings: FakeAsr())
    monkeypatch.setattr(cli, "build_pipeline", lambda *a, **k: pytest.fail("pipeline built"))
    wav = _write_wav(tmp_path / "d.wav", _loud(1))
    monkeypatch.setattr(cli.sys, "argv", ["paraspeakrs", "dictate", str(wav)])

    cli._main()


def test_serve_loads_first_then_answers_one_line_per_path_and_ends_on_eof(tmp_path):
    """The app waits for `ready`, sends one path, and closes stdin to end the process."""
    asr = FakeAsr()
    first = _write_wav(tmp_path / "a.wav", _loud(1))
    second = _write_wav(tmp_path / "b.wav", _loud(1))
    out = io.StringIO()

    dictate.serve(asr, io.StringIO(f"{first}\n{second}\n"), out)  # returns: stdin is at EOF

    assert asr.loaded
    assert [json.loads(line) for line in out.getvalue().splitlines()] == [
        {"ready": True},
        {"text": "hello"},
        {"text": "again"},
    ]


def test_a_bad_path_gives_an_error_line_and_the_server_keeps_answering(tmp_path):
    good = _write_wav(tmp_path / "a.wav", _loud(1))
    out = io.StringIO()

    dictate.serve(FakeAsr(), io.StringIO(f"{tmp_path / 'missing.wav'}\n{good}\n"), out)

    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert "missing.wav" in replies[1]["error"]
    assert replies[2] == {"text": "hello"}


def test_a_missing_model_says_run_fetch_models_and_never_downloads(monkeypatch, tmp_path):
    """A 2.4 GB download must not start from a keyboard shortcut."""
    for name in ("download_sherpa_model", "download_sherpa_fp32_model", "ensure_sherpa_model"):
        monkeypatch.setattr(model_fetch, name, lambda *a, **k: pytest.fail("download started"))
    monkeypatch.setattr(factory, "ensure_sherpa_model", lambda *a, **k: pytest.fail("download started"))
    settings = Settings(sherpa_model_dir=tmp_path / "models" / "none", workspace_dir=tmp_path)

    with pytest.raises(RuntimeError, match="paraspeakrs fetch-models"):
        dictate.build_asr(settings)


def test_serve_reports_a_missing_model_on_stdout_so_the_app_can_show_it(monkeypatch, capsys):
    def missing(settings):
        raise RuntimeError("run `paraspeakrs fetch-models` first")

    monkeypatch.setattr(dictate, "build_asr", missing)

    with pytest.raises(RuntimeError):
        dictate.main(None, serve_mode=True)

    assert json.loads(capsys.readouterr().out) == {"error": "run `paraspeakrs fetch-models` first"}


def test_long_audio_is_cut_in_the_quiet_window_not_in_speech():
    """Cutting inside a word loses it; the silences at 25 s and 52 s are where to cut."""
    samples = _loud(70)
    for at in (25.0, 52.0):
        samples[int(at * RATE) : int((at + 0.3) * RATE)] = 0.0

    bounds = dictate.piece_bounds(samples, RATE)

    cuts = [end for _, end in bounds[:-1]]
    assert len(cuts) == 2
    for cut, silence in zip(cuts, (25.0, 52.0)):
        assert silence * RATE <= cut <= (silence + 0.3) * RATE
    assert np.abs(samples[cuts]).max() == 0.0


def test_no_piece_exceeds_30_seconds_even_without_a_pause():
    bounds = dictate.piece_bounds(_loud(95), RATE)

    assert bounds[0][0] == 0 and bounds[-1][1] == 95 * RATE
    assert all(b == c for (_, b), (c, _) in zip(bounds, bounds[1:]))
    assert all(end - start <= 30 * RATE for start, end in bounds)


def test_a_short_dictation_is_one_piece(tmp_path):
    asr = FakeAsr()

    text = dictate.transcribe_file(asr, _write_wav(tmp_path / "d.wav", _loud(12)))

    assert text == "hello" and asr.pieces == [12 * RATE]


def test_an_empty_recording_gives_empty_text_without_decoding(tmp_path):
    """onnxruntime raises on zero frames; a quick tap of the shortcut records about that."""
    asr = FakeAsr()

    assert dictate.transcribe_file(asr, _write_wav(tmp_path / "d.wav", _loud(0.01))) == ""
    assert asr.pieces == []


def test_piece_texts_are_joined_with_one_space(tmp_path):
    asr = FakeAsr()

    text = dictate.transcribe_file(asr, _write_wav(tmp_path / "d.wav", _loud(45)))

    assert text == "hello again"
    assert len(asr.pieces) == 2


def test_dictate_needs_a_file_or_serve(monkeypatch):
    monkeypatch.setattr(dictate, "build_asr", lambda settings: pytest.fail("model loaded"))

    with pytest.raises(ValueError, match="--serve"):
        dictate.main(None, serve_mode=False)
