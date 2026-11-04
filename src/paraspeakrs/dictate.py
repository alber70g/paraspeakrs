"""`paraspeakrs dictate`: transcribe a short WAV for the recorder's dictation shortcut.

No job store, no diarization, no ffmpeg: the recorder writes a 16-bit PCM WAV and
types whatever comes back into the frontmost app, so stdout carries the text and
nothing else. Never downloads the model: a 2.4 GB download must not start from a
keyboard shortcut.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TextIO

from .asr import SherpaParakeetAsr, _read_mono_wave
from .config import Settings

# One pass over minutes of audio loses words (memory note "ASR chunk size: bigger
# loses words"), so longer dictations are cut into pieces of at most 30 s, each at
# the quietest 0.2 s between 20 and 30 s, where a word is least likely to be cut.
MAX_PIECE_SECONDS = 30.0
MIN_PIECE_SECONDS = 20.0
QUIET_WINDOW_SECONDS = 0.2
# Under about 5 ms onnxruntime fails on an empty feature matrix; under 0.1 s there
# is no word to recognise anyway. A quick tap of the shortcut records this little.
MIN_DECODE_SECONDS = 0.1


def build_asr(settings: Settings) -> SherpaParakeetAsr:
    """The ASR for an on-disk model, or a RuntimeError that says how to get one."""
    from .factory import _use_downloaded_model
    from .model_fetch import model_is_present

    assert settings.sherpa_model_dir is not None
    settings = _use_downloaded_model(settings)
    assert settings.sherpa_model_dir is not None
    if not model_is_present(settings.sherpa_model_dir):
        raise RuntimeError(
            f"ASR model missing at {settings.sherpa_model_dir}: run `paraspeakrs fetch-models` first"
        )
    return SherpaParakeetAsr(settings.sherpa_model_dir, settings.device, settings.sherpa_num_threads)


def piece_bounds(samples, sample_rate: int) -> list[tuple[int, int]]:
    """Sample ranges of at most MAX_PIECE_SECONDS, cut at the quietest window."""
    import numpy as np

    total = len(samples)
    longest = int(MAX_PIECE_SECONDS * sample_rate)
    shortest = int(MIN_PIECE_SECONDS * sample_rate)
    window = max(1, int(QUIET_WINDOW_SECONDS * sample_rate))
    hop = max(1, sample_rate // 100)
    bounds: list[tuple[int, int]] = []
    start = 0
    while total - start > longest:
        searched = np.asarray(samples[start + shortest : start + longest], dtype=np.float64)
        energy = np.concatenate(([0.0], np.cumsum(searched * searched)))
        per_window = (energy[window:] - energy[:-window])[::hop]
        cut = start + shortest + int(np.argmin(per_window)) * hop + window // 2
        bounds.append((start, cut))
        start = cut
    bounds.append((start, total))
    return bounds


def transcribe_file(asr: SherpaParakeetAsr, path: Path) -> str:
    samples, sample_rate = _read_mono_wave(path)
    shortest = MIN_DECODE_SECONDS * sample_rate
    texts = (
        asr.transcribe_text(samples[a:b], sample_rate)
        for a, b in piece_bounds(samples, sample_rate)
        if b - a >= shortest
    )
    return " ".join(text for text in texts if text)


def serve(asr: SherpaParakeetAsr, stdin: TextIO, stdout: TextIO) -> None:
    """Load the model, then answer one JSON line per WAV path until stdin closes.

    The recorder starts this on key-down so the model loads while the user speaks,
    and closes stdin after its one request, so the process never outlives it.
    """
    asr.load()
    _reply(stdout, {"ready": True})
    for line in iter(stdin.readline, ""):
        path = line.rstrip("\r\n")
        if not path:
            continue
        try:
            _reply(stdout, {"text": transcribe_file(asr, Path(path))})
        # Any failure belongs to this one request; the next one may well succeed.
        except Exception as exc:  # noqa: BLE001
            _reply(stdout, {"error": f"{path}: {exc}"})


def main(audio_path: Path | None, *, serve_mode: bool) -> None:
    if serve_mode == (audio_path is not None):
        raise ValueError("give either a WAV file or --serve")
    try:
        asr = build_asr(Settings.from_env())
    except RuntimeError as exc:
        if serve_mode:
            _reply(sys.stdout, {"error": str(exc)})
        raise
    if serve_mode:
        serve(asr, sys.stdin, sys.stdout)
    else:
        assert audio_path is not None
        print(transcribe_file(asr, audio_path))


def _reply(stdout: TextIO, message: dict) -> None:
    stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    stdout.flush()
