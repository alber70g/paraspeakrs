"""Downloading the ASR model on first run.

The interesting cases are all failure cases: a half-finished download must never
be mistaken for a working model, because the next run would then skip the fetch
and fail deep inside sherpa instead.
"""

from __future__ import annotations

import bz2
import io
import tarfile
from pathlib import Path

import pytest

from paraspeakrs import model_fetch
from paraspeakrs.model_fetch import (
    REQUIRED_FP32_FILES,
    REQUIRED_FILES,
    ensure_sherpa_model,
    model_is_present,
)


def _archive(names: tuple[str, ...], top: str = model_fetch.SHERPA_INT8_MODEL_NAME) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name in names:
            payload = b"stub"
            info = tarfile.TarInfo(f"{top}/{name}" if top else name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return bz2.compress(raw.getvalue())


@pytest.fixture
def served(monkeypatch):
    """Replace the network fetch with a local write, leaving the rest of the path real."""

    def serve(payload: bytes):
        def fake_stream(url: str, destination: Path) -> None:
            destination.write_bytes(payload)

        monkeypatch.setattr(model_fetch, "_stream_to_file", fake_stream)

    return serve


def test_fetches_when_absent(tmp_path, served):
    served(_archive(REQUIRED_FILES))
    target = tmp_path / "models" / model_fetch.SHERPA_INT8_MODEL_NAME

    ensure_sherpa_model(target)

    assert model_is_present(target)


def test_does_not_refetch_when_present(tmp_path, monkeypatch):
    target = tmp_path / "models" / model_fetch.SHERPA_INT8_MODEL_NAME
    target.mkdir(parents=True)
    for name in REQUIRED_FILES:
        (target / name).write_text("already here")

    def explode(*args, **kwargs):
        raise AssertionError("re-downloaded a model that was already present")

    monkeypatch.setattr(model_fetch, "_stream_to_file", explode)

    ensure_sherpa_model(target)


def test_incomplete_archive_leaves_no_model_behind(tmp_path, served):
    """A model dir missing one file is worse than no model dir at all.

    model_is_present() would accept whatever landed on a later run, so the fetch
    has to refuse to publish a partial extraction.
    """
    served(_archive(REQUIRED_FILES[:2]))
    target = tmp_path / "models" / model_fetch.SHERPA_INT8_MODEL_NAME

    with pytest.raises(RuntimeError, match="missing"):
        ensure_sherpa_model(target)

    assert not target.exists()


def test_failure_leaves_no_staging_directories(tmp_path, served):
    served(_archive(REQUIRED_FILES[:1]))
    target = tmp_path / "models" / model_fetch.SHERPA_INT8_MODEL_NAME

    with pytest.raises(RuntimeError):
        ensure_sherpa_model(target)

    assert list(target.parent.iterdir()) == []


def test_failure_explains_how_to_get_the_model_without_the_network(tmp_path, served):
    served(_archive(()))
    target = tmp_path / "models" / model_fetch.SHERPA_INT8_MODEL_NAME

    with pytest.raises(RuntimeError) as excinfo:
        ensure_sherpa_model(target)

    message = str(excinfo.value)
    assert "PARAKEET_MODEL_URL" in message
    assert "PARAKEET_SHERPA_MODEL_DIR" in message
    assert "PARAKEET_ASR_BACKEND=openai" in message


def test_accepts_an_archive_without_the_wrapping_directory(tmp_path, served):
    """A mirror may repack the tarball flat; that is still a usable model."""
    served(_archive(REQUIRED_FILES, top=""))
    target = tmp_path / "models" / model_fetch.SHERPA_INT8_MODEL_NAME

    ensure_sherpa_model(target)

    assert model_is_present(target)


def test_refuses_members_that_escape_the_target(tmp_path, served):
    served(_archive(("../../escaped.onnx",), top=""))
    target = tmp_path / "models" / model_fetch.SHERPA_INT8_MODEL_NAME

    with pytest.raises(RuntimeError, match="escapes"):
        ensure_sherpa_model(target)


def test_auto_download_can_be_turned_off(tmp_path, monkeypatch):
    monkeypatch.setenv("PARAKEET_AUTO_DOWNLOAD", "0")
    monkeypatch.setattr(model_fetch, "_stream_to_file", lambda *a, **k: pytest.fail("downloaded anyway"))
    target = tmp_path / "models" / model_fetch.SHERPA_INT8_MODEL_NAME

    with pytest.raises(RuntimeError, match="PARAKEET_AUTO_DOWNLOAD"):
        ensure_sherpa_model(target)


def test_download_names_the_destination_directory(tmp_path, served, capsys):
    """The path is the whole point: it is where a sideloaded copy has to go."""
    served(_archive(REQUIRED_FILES))
    target = tmp_path / "models" / model_fetch.SHERPA_INT8_MODEL_NAME

    ensure_sherpa_model(target)

    err = capsys.readouterr().err
    assert str(target) in err
    assert "PARAKEET_SHERPA_MODEL_DIR" in err


def test_speakrs_cache_follows_hf_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))

    assert model_fetch.speakrs_models_cache_dir() == (
        tmp_path / "hf" / "hub" / "models--avencera--speakrs-models"
    )


def test_speakrs_download_is_announced_once_the_first_time(tmp_path, monkeypatch, capsys):
    """Announce a download that is about to happen, and nothing when it is not.

    Sidecar runs are per channel, so a second invocation on cached models must
    not repeat the notice.
    """
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    cache = model_fetch.speakrs_models_cache_dir()

    model_fetch.announce_speakrs_models_dir()
    assert str(cache) in capsys.readouterr().err

    cache.mkdir(parents=True)
    model_fetch.announce_speakrs_models_dir()
    assert capsys.readouterr().err == ""


def test_fp32_is_what_a_bare_model_dir_fetches(tmp_path, served):
    """FP32 is the default because INT8 drops speech; the default must not drift back."""
    served(b"stub")
    target = tmp_path / "models" / model_fetch.SHERPA_MODEL_NAME

    ensure_sherpa_model(target)

    assert model_is_present(target)
    for name in REQUIRED_FP32_FILES:
        assert (target / name).is_file(), name


def test_int8_directory_still_fetches_the_archive(tmp_path, served):
    """A dir named -int8 gets INT8 weights -- precision is inferred, never assumed."""
    served(_archive(REQUIRED_FILES))
    target = tmp_path / "models" / model_fetch.SHERPA_INT8_MODEL_NAME

    ensure_sherpa_model(target)

    assert (target / "encoder.int8.onnx").is_file()
    assert not (target / "encoder.onnx").exists()


def test_explicit_precision_beats_the_directory_name(tmp_path, served):
    """The caller's setting wins: a workspace path must not dictate the weights."""
    served(b"stub")
    target = tmp_path / "models" / "whatever-i-named-it-int8"

    ensure_sherpa_model(target, "fp32")

    assert (target / "encoder.onnx").is_file()


def test_partial_fp32_download_is_not_mistaken_for_a_model(tmp_path, monkeypatch):
    """The whole point of staging: a cut-off download must not satisfy the next run."""
    target = tmp_path / "models" / model_fetch.SHERPA_MODEL_NAME
    written: list[Path] = []

    def fake_stream(url: str, destination: Path) -> None:
        if len(written) >= 2:
            raise RuntimeError("connection reset")
        destination.write_bytes(b"stub")
        written.append(destination)

    monkeypatch.setattr(model_fetch, "_stream_to_file", fake_stream)

    with pytest.raises(RuntimeError):
        ensure_sherpa_model(target)

    assert not model_is_present(target)
    assert not target.exists()
    assert list(target.parent.glob(".*incoming")) == []


def test_fp16_is_recognised_as_a_complete_model(tmp_path):
    """FP16 is built locally, so nothing else would notice if its file set were wrong."""
    target = tmp_path / "models" / model_fetch.SHERPA_FP16_MODEL_NAME
    target.mkdir(parents=True)
    for name in model_fetch.REQUIRED_FP16_FILES:
        (target / name).write_bytes(b"stub")

    assert model_is_present(target)


def test_an_fp16_directory_is_not_filled_with_fp32_weights(tmp_path, monkeypatch):
    """Precision is inferred from the directory name for every build, not just int8."""
    target = tmp_path / "models" / model_fetch.SHERPA_FP16_MODEL_NAME
    monkeypatch.setattr(model_fetch, "build_sherpa_fp16_model", lambda d: d)
    monkeypatch.setattr(
        model_fetch, "download_sherpa_fp32_model",
        lambda d: pytest.fail("an fp16 directory must not fetch fp32 directly"),
    )

    ensure_sherpa_model(target)


def test_fp16_without_the_extra_says_how_to_install_it(tmp_path, monkeypatch):
    """The failure is a missing optional dependency, so the message has to name it."""
    monkeypatch.setitem(__import__("sys").modules, "onnx", None)
    target = tmp_path / "models" / model_fetch.SHERPA_FP16_MODEL_NAME
    (tmp_path / "models" / model_fetch.SHERPA_MODEL_NAME).mkdir(parents=True)
    for name in model_fetch.REQUIRED_FP32_FILES:
        (tmp_path / "models" / model_fetch.SHERPA_MODEL_NAME / name).write_bytes(b"stub")

    with pytest.raises(RuntimeError, match=r"paraspeakrs\[fp16\]"):
        model_fetch.build_sherpa_fp16_model(target)
