"""Using an ASR model that is already on disk instead of fetching another.

The failure this guards: a model fetched at one precision (or into a custom workspace)
was invisible to a server started with the defaults, which then tried to download
2.4 GB of FP32 weights -- or stalled on the first-run question -- next to a working model.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from paraspeakrs import factory, model_fetch
from paraspeakrs.config import Settings, sherpa_model_name
from paraspeakrs.model_fetch import REQUIRED_FP32_FILES, REQUIRED_INT8_FILES, find_sherpa_model


def _model(root: Path, precision: str, files: tuple[str, ...]) -> Path:
    directory = root / sherpa_model_name(precision)  # type: ignore[arg-type]
    directory.mkdir(parents=True)
    for name in files:
        (directory / name).write_bytes(b"stub")
    return directory


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(model_fetch, "_stream_to_file", lambda *a: pytest.fail("must not download"))
    monkeypatch.setenv("PARAKEET_NONINTERACTIVE", "1")


def test_an_int8_model_is_found_when_the_default_asks_for_fp32(tmp_path):
    int8 = _model(tmp_path / "models", "int8", REQUIRED_INT8_FILES)
    settings = Settings(workspace_dir=tmp_path, sherpa_model_dir=tmp_path / "models" / sherpa_model_name("fp32"))

    resolved = factory._use_downloaded_model(settings)

    assert resolved.sherpa_model_dir == int8
    assert resolved.sherpa_precision == "int8"


def test_the_workspace_env_var_carries_the_models_directory(tmp_path, monkeypatch):
    """PARAKEET_WORKSPACE_DIR used to move jobs but leave the model under ~/.local/share."""
    monkeypatch.setenv("PARAKEET_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.delenv("PARAKEET_SHERPA_MODEL_DIR", raising=False)
    monkeypatch.delenv("PARAKEET_SHERPA_PRECISION", raising=False)

    assert Settings.from_env().sherpa_model_dir == tmp_path / "models" / sherpa_model_name("fp32")


def test_a_model_dir_pointing_at_the_models_folder_finds_the_model_inside(tmp_path):
    fp32 = _model(tmp_path / "models", "fp32", REQUIRED_FP32_FILES)
    assert find_sherpa_model(tmp_path / "models", tmp_path / "elsewhere") == fp32


def test_fp32_wins_when_several_are_downloaded(tmp_path):
    _model(tmp_path / "models", "int8", REQUIRED_INT8_FILES)
    fp32 = _model(tmp_path / "models", "fp32", REQUIRED_FP32_FILES)
    assert find_sherpa_model(tmp_path / "missing", tmp_path) == fp32


def test_the_workspaces_remembered_choice_beats_the_default_order(tmp_path):
    int8 = _model(tmp_path / "models", "int8", REQUIRED_INT8_FILES)
    _model(tmp_path / "models", "fp32", REQUIRED_FP32_FILES)
    (tmp_path / "asr-precision").write_text("int8\n")
    settings = Settings(workspace_dir=tmp_path, sherpa_model_dir=tmp_path / "nope")

    assert factory._use_downloaded_model(settings).sherpa_model_dir == int8


def test_an_explicit_precision_is_not_substituted(tmp_path):
    """Asking for FP32 and silently getting INT8 would lose a quarter of the transcript."""
    _model(tmp_path / "models", "int8", REQUIRED_INT8_FILES)
    wanted = tmp_path / "models" / sherpa_model_name("fp32")
    settings = Settings(
        workspace_dir=tmp_path, sherpa_model_dir=wanted, sherpa_precision="fp32", precision_is_explicit=True
    )

    assert factory._use_downloaded_model(settings).sherpa_model_dir == wanted


def test_the_chosen_model_is_announced(tmp_path, monkeypatch, capsys):
    int8 = _model(tmp_path / "models", "int8", REQUIRED_INT8_FILES)
    monkeypatch.setattr(factory, "SpeakrsDiarizer", lambda **kw: object())
    monkeypatch.setattr(Settings, "validate_runtime", lambda self: None)
    settings = Settings(workspace_dir=tmp_path, sherpa_model_dir=tmp_path / "models" / sherpa_model_name("fp32"))

    pipeline = factory.build_pipeline(settings)

    assert pipeline.asr.model_dir == int8
    assert f"ASR model: Parakeet INT8 · {int8}" in capsys.readouterr().err
