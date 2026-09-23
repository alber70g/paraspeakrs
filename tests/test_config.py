from pathlib import Path

import pytest

from paraspeakrs.config import Settings


def test_sherpa_backend_requires_model_dir():
    settings = Settings(asr_backend="sherpa", sherpa_model_dir=None, diar_backend="senko")

    with pytest.raises(ValueError, match="sherpa_model_dir"):
        settings.validate_runtime()


def test_openai_backend_requires_base_url():
    settings = Settings(asr_backend="openai", openai_base_url=None, diar_backend="senko")

    with pytest.raises(ValueError, match="openai_base_url"):
        settings.validate_runtime()


def test_mock_backend_accepts_supported_devices():
    Settings(asr_backend="mock", device="cuda", diar_backend="senko").validate_runtime()
    Settings(asr_backend="mock", device="coreml", diar_backend="senko").validate_runtime()


def test_from_env_accepts_coreml_device(monkeypatch):
    monkeypatch.setenv("PARAKEET_DEVICE", "coreml")
    monkeypatch.setenv("PARAKEET_ASR_BACKEND", "mock")

    settings = Settings.from_env()

    assert settings.device == "coreml"


def test_speakrs_backend_requires_a_built_binary(tmp_path):
    """The sidecar is built by cargo, not installed by uv, so a missing binary is
    the expected first-run failure and must name the build command."""
    settings = Settings(asr_backend="mock", diar_backend="speakrs", speakrs_bin=tmp_path / "absent")

    with pytest.raises(ValueError, match="cargo build"):
        settings.validate_runtime()


def test_speakrs_backend_rejects_non_executable_binary(tmp_path):
    binary = tmp_path / "speakrs-diar"
    binary.write_text("")
    settings = Settings(asr_backend="mock", diar_backend="speakrs", speakrs_bin=binary)

    with pytest.raises(ValueError, match="not executable"):
        settings.validate_runtime()


def test_speakrs_backend_rejects_missing_models_dir(tmp_path):
    binary = tmp_path / "speakrs-diar"
    binary.write_text("")
    binary.chmod(0o755)
    settings = Settings(
        asr_backend="mock",
        diar_backend="speakrs",
        speakrs_bin=binary,
        speakrs_models_dir=tmp_path / "absent",
    )

    with pytest.raises(ValueError, match="models dir not found"):
        settings.validate_runtime()


def test_diar_backend_defaults_to_speakrs_and_honours_env(monkeypatch):
    monkeypatch.delenv("PARAKEET_DIAR_BACKEND", raising=False)
    assert Settings.from_env().diar_backend == "speakrs"

    monkeypatch.setenv("PARAKEET_DIAR_BACKEND", "senko")
    assert Settings.from_env().diar_backend == "senko"


def test_from_env_rejects_unknown_diar_backend(monkeypatch):
    monkeypatch.setenv("PARAKEET_DIAR_BACKEND", "pyannote")

    with pytest.raises(ValueError, match="PARAKEET_DIAR_BACKEND"):
        Settings.from_env()


def test_workspace_defaults_to_an_absolute_xdg_data_dir(monkeypatch, tmp_path) -> None:
    """The store must not move with the working directory.

    A relative default meant jobs and hand-labeled speakers silently vanished
    when the app was launched from somewhere else.
    """
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("PARAKEET_WORKSPACE_DIR", raising=False)

    workspace = Settings.from_env().workspace_dir
    assert workspace.is_absolute()
    assert workspace == tmp_path / "data" / "fast-speaker-aware-meeting-transcriber"


def test_explicit_workspace_env_wins_over_the_default(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("PARAKEET_WORKSPACE_DIR", str(tmp_path / "chosen"))
    assert Settings.from_env().workspace_dir == tmp_path / "chosen"


def test_relative_workspace_is_resolved_not_kept_relative(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert Settings(workspace_dir=Path("var")).workspace_dir == (tmp_path / "var").resolve()
