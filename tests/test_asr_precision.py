"""Which weights the ASR engine opens.

A model directory can hold FP32 or INT8 weights under different filenames, and the
two transcribe very differently: on a 42-minute Dutch meeting INT8 emitted 3546
words against FP32's 4715, losing content in every chunk. Loading the wrong one is
therefore silent quality loss, not a crash -- which is exactly why it needs a test.
"""

from __future__ import annotations

import pytest

from paraspeakrs.asr import SherpaParakeetAsr, _weight_suffix

FP32 = ("encoder.onnx", "decoder.onnx", "joiner.onnx")
INT8 = ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx")


def _model(tmp_path, names):
    for name in (*names, "tokens.txt"):
        (tmp_path / name).write_bytes(b"stub")
    return tmp_path


def test_fp32_weights_are_used_when_present(tmp_path):
    assert _weight_suffix(_model(tmp_path, FP32)) == ".onnx"


def test_int8_weights_are_used_when_that_is_all_there_is(tmp_path):
    assert _weight_suffix(_model(tmp_path, INT8)) == ".int8.onnx"


def test_fp32_wins_when_a_directory_holds_both(tmp_path):
    """A dir with both is ambiguous, and the better transcriber has to win the tie."""
    assert _weight_suffix(_model(tmp_path, FP32 + INT8)) == ".onnx"


def test_a_missing_model_names_the_file_it_wanted(tmp_path):
    """The error has to name a path, so a sideloaded copy can be put in the right place."""
    (tmp_path / "tokens.txt").write_bytes(b"stub")

    with pytest.raises(FileNotFoundError, match="encoder.int8.onnx"):
        SherpaParakeetAsr(tmp_path, "cpu")._load_recognizer(object())
