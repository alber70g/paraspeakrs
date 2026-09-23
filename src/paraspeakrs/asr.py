from __future__ import annotations

import wave
from pathlib import Path
from typing import Protocol

from .models import AsrSegment, Word


class AsrEngine(Protocol):
    def transcribe(self, audio_path: Path) -> AsrSegment:
        ...


class SherpaParakeetAsr:
    def __init__(self, model_dir: Path, device: str, num_threads: int = 4) -> None:
        self.model_dir = model_dir
        self.device = device
        self.num_threads = num_threads
        self._recognizer = None

    def transcribe(self, audio_path: Path) -> AsrSegment:
        sherpa_onnx = _import_sherpa_onnx()
        recognizer = self._load_recognizer(sherpa_onnx)
        stream = recognizer.create_stream()
        samples, sample_rate = _read_mono_wave(audio_path)
        stream.accept_waveform(sample_rate, samples)
        recognizer.decode_stream(stream)
        result = stream.result
        text = getattr(result, "text", str(result)).strip()
        words = _extract_words(result)
        end = float(len(samples)) / float(sample_rate)
        return AsrSegment(text=text, start=0.0, end=end, words=words)

    def _load_recognizer(self, sherpa_onnx):
        if self._recognizer is not None:
            return self._recognizer

        suffix = _weight_suffix(self.model_dir)
        encoder = _require_file(self.model_dir / f"encoder{suffix}")
        decoder = _require_file(self.model_dir / f"decoder{suffix}")
        joiner = _require_file(self.model_dir / f"joiner{suffix}")
        tokens = _require_file(self.model_dir / "tokens.txt")
        kwargs = {
            "encoder": str(encoder),
            "decoder": str(decoder),
            "joiner": str(joiner),
            "tokens": str(tokens),
            "num_threads": self.num_threads,
            "decoding_method": "greedy_search",
            "model_type": "nemo_transducer",
        }
        try:
            self._recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
                **kwargs,
                provider=self.device,
            )
        except TypeError:
            self._recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(**kwargs)
        return self._recognizer


class OpenAICompatibleParakeetAsr:
    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    def transcribe(self, audio_path: Path) -> AsrSegment:
        import httpx

        with audio_path.open("rb") as handle:
            response = httpx.post(
                f"{self.base_url}/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                data={"model": self.model, "response_format": "json"},
                files={"file": (audio_path.name, handle, "audio/wav")},
                timeout=None,
            )
        response.raise_for_status()
        payload = response.json()
        text = payload.get("text", "")
        return AsrSegment(text=text, start=0.0, end=0.0, words=[])


class MockAsr:
    def __init__(self, text: str = "mock transcript") -> None:
        self.text = text

    def transcribe(self, audio_path: Path) -> AsrSegment:
        return AsrSegment(text=self.text, start=0.0, end=1.0, words=[])


def _import_sherpa_onnx():
    try:
        import sherpa_onnx
    except ImportError as exc:
        raise RuntimeError("install sherpa-onnx or choose another ASR backend") from exc
    return sherpa_onnx


def _weight_suffix(model_dir: Path) -> str:
    """Pick FP32 or INT8 weights by what the directory actually holds.

    Detected rather than configured so that a sideloaded model directory works
    whichever variant it happens to contain -- the precision setting chooses what
    gets *downloaded*, and a directory the user pointed us at is not ours to
    second-guess. FP32 wins a tie: it is the better transcriber (see the note on
    SHERPA_MODEL_NAME in config.py).
    """
    for suffix in (".onnx", ".fp16.onnx", ".int8.onnx"):
        if (model_dir / f"encoder{suffix}").is_file():
            return suffix
    return ".int8.onnx"


def _require_file(path: Path) -> Path:
    if not path.exists():
        raise FileNotFoundError(f"required Parakeet model file missing: {path}")
    return path


def _read_mono_wave(audio_path: Path):
    import numpy as np

    with wave.open(str(audio_path), "rb") as handle:
        sample_rate = handle.getframerate()
        channels = handle.getnchannels()
        sample_width = handle.getsampwidth()
        frames = handle.readframes(handle.getnframes())

    if sample_width != 2:
        raise ValueError("expected 16-bit PCM WAV")
    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples, sample_rate


def _extract_words(result) -> list[Word]:
    """Rebuild words from sherpa-onnx's timestamped subword tokens.

    sherpa-onnx leaves ``result.words`` empty for NeMo transducer models — it
    only fills that field for models whose word boundaries it can infer itself.
    What it does return is ``tokens`` / ``timestamps`` / ``durations``, aligned
    1:1, in SentencePiece form: a token that opens a new word carries a leading
    space, continuations do not (``" I"`` + ``"B"`` -> ``"IB"``).
    """
    tokens = getattr(result, "tokens", None)
    starts = getattr(result, "timestamps", None)
    if not tokens or not starts:
        return []
    durations = getattr(result, "durations", None)
    words: list[Word] = []
    for index, token in enumerate(tokens):
        if index >= len(starts):
            break
        start = float(starts[index])
        end = start + (float(durations[index]) if durations and index < len(durations) else 0.0)
        if words and not token.startswith(" "):
            previous = words[-1]
            words[-1] = previous.model_copy(update={"word": previous.word + token, "end": end})
        else:
            words.append(Word(word=token.strip(), start=start, end=end))
    return words
