"""The defaults an installed tool depends on.

Every one of these guards a failure mode that only shows up once the package is
installed rather than run from a checkout, which is exactly where a normal test
run would not notice it.
"""

from __future__ import annotations

import sysconfig
from pathlib import Path

import pytest

from paraspeakrs import config
from paraspeakrs.config import Settings, default_sherpa_model_dir, default_speakrs_bin


def test_default_model_dir_is_absolute():
    """A relative default resolves against the working directory.

    That is what made the tool only work when launched from the repository root:
    an MCP client or a `uv tool` install starts somewhere else and silently looked
    for the model in the wrong place.
    """
    assert default_sherpa_model_dir().is_absolute()


def test_default_model_dir_lives_under_the_workspace():
    assert config.default_workspace_dir() in default_sherpa_model_dir().parents


def test_speakrs_bin_prefers_the_binary_next_to_the_interpreter(tmp_path, monkeypatch):
    """The companion wheel installs speakrs-diar into the venv's scripts dir.

    Resolving through sys.executable does not work here: in a venv the interpreter
    is a symlink into the real Python installation, so following it lands outside
    the venv where the binary is not.
    """
    scripts = tmp_path / "bin"
    scripts.mkdir()
    binary = scripts / config.SPEAKRS_BIN_NAME
    binary.touch()
    monkeypatch.setattr(sysconfig, "get_path", lambda name: str(scripts) if name == "scripts" else "")

    assert default_speakrs_bin() == binary


def test_speakrs_bin_falls_back_to_path(tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    binary = elsewhere / config.SPEAKRS_BIN_NAME
    binary.touch()
    # Both interpreter-relative candidates have to miss before PATH is consulted, and
    # the dev venv really does have the binary installed next to python.
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(sysconfig, "get_path", lambda name: str(empty))
    monkeypatch.setattr(config.sys, "executable", str(empty / "python"))
    monkeypatch.setattr(config.shutil, "which", lambda name: str(binary))

    assert default_speakrs_bin() == binary


def test_speakrs_error_does_not_tell_a_pypi_user_to_run_cargo():
    """The old message only made sense inside a checkout.

    Someone who installed from PyPI has no packages/speakrs-diar to cd into, so the
    message has to lead with the options that apply to them.
    """
    settings = Settings(diar_backend="speakrs", speakrs_bin=Path("/nonexistent/speakrs-diar"), asr_backend="mock")

    with pytest.raises(ValueError) as excinfo:
        settings.validate_runtime()

    message = str(excinfo.value)
    assert "--diar-backend senko" in message
    assert message.index("--diar-backend senko") < message.index("cargo build")


def test_missing_model_error_points_at_fetch_models():
    settings = Settings(asr_backend="sherpa", sherpa_model_dir=Path("/nonexistent/model"), diar_backend="senko")

    with pytest.raises(ValueError, match="paraspeakrs fetch-models"):
        settings.validate_runtime()
