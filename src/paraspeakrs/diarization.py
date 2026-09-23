from __future__ import annotations

import json
import subprocess
from pathlib import Path
from collections.abc import Collection
from typing import Callable, Protocol

from .audio import AudioPreparer
from .model_fetch import announce_speakrs_models_dir
from .models import DiarizationSegment

# Bump together with CONTRACT_VERSION in packages/speakrs-diar/src/main.rs.
SPEAKRS_CONTRACT_VERSION = 1

# A mono pass returns its segments plus the per-speaker centroids from the same
# pass; the channel wrapper is what prefixes both for stereo input.
MonoPass = Callable[[Path], "tuple[list[DiarizationSegment], dict[str, list[float]]]"]


class Diarizer(Protocol):
    # Reported in progress output so it is obvious which backend actually ran.
    name: str

    def diarize(self, audio_path: Path) -> list[DiarizationSegment]:
        ...


def diarize_channels(
    audio: AudioPreparer,
    audio_path: Path,
    run_mono: MonoPass,
) -> tuple[list[DiarizationSegment], dict[str, list[float]]]:
    """Route mono straight through; split stereo into two independent mono passes.

    Backend-agnostic: every diarizer we support wants mono input, so the channel
    split, the L_/R_ prefixing and the merge live here rather than in any one
    backend.
    """
    if audio.channel_count(audio_path) < 2:
        return run_mono(audio_path)

    work_dir = audio_path.parent
    left = work_dir / f"{audio_path.stem}-left.wav"
    right = work_dir / f"{audio_path.stem}-right.wav"
    audio.split_stereo_to_mono(audio_path, left, right)

    segments: list[DiarizationSegment] = []
    centroids: dict[str, list[float]] = {}
    for channel_path, prefix in ((left, "L"), (right, "R")):
        channel_segments, channel_centroids = run_mono(channel_path)
        segments.extend(
            DiarizationSegment(speaker=f"{prefix}_{segment.speaker}", start=segment.start, end=segment.end)
            for segment in channel_segments
        )
        centroids.update({f"{prefix}_{speaker}": vector for speaker, vector in channel_centroids.items()})
    segments.sort(key=lambda segment: segment.start)
    return segments, centroids


def collapse_channels(
    segments: list[DiarizationSegment],
    embeddings: dict[str, list[float]],
    channels: Collection[str],
) -> tuple[list[DiarizationSegment], dict[str, list[float]]]:
    """Fold every speaker found on each of `channels` ("L", "R") into one.

    When the caller knows a channel carries one person - their own headset mic,
    say - clustering it can only get it wrong: a cough or a change of tone
    becomes a second speaker to name. Diarization still runs, because it is what
    finds where the speech is; only its answer to "how many people" is replaced.
    The merged voice print is the speaking-time-weighted mean of the parts, so a
    two-second stray cluster does not pull it far from the real voice.
    """
    if not channels:
        return segments, embeddings
    renamed: dict[str, str] = {}
    for channel in channels:
        speakers = sorted({s.speaker for s in segments if s.speaker.startswith(f"{channel}_")})
        renamed.update({speaker: speakers[0] for speaker in speakers})

    merged: dict[str, list[float]] = {}
    for target in set(renamed.values()):
        parts = [(speaker, embeddings[speaker]) for speaker, into in renamed.items() if into == target and speaker in embeddings]
        if not parts:
            continue
        weights = [
            sum(s.end - s.start for s in segments if s.speaker == speaker) or 1.0 for speaker, _ in parts
        ]
        total = sum(weights)
        merged[target] = [
            sum(weight * vector[i] for weight, (_, vector) in zip(weights, parts)) / total
            for i in range(len(parts[0][1]))
        ]

    collapsed = [
        DiarizationSegment(speaker=renamed.get(s.speaker, s.speaker), start=s.start, end=s.end) for s in segments
    ]
    kept = {speaker: vector for speaker, vector in embeddings.items() if speaker not in renamed}
    return collapsed, {**kept, **merged}


class SenkoDiarizer:
    name = "senko"

    def __init__(
        self,
        audio: AudioPreparer,
        device: str = "auto",
        warmup: bool = True,
    ) -> None:
        self.audio = audio
        self.device = device
        self.warmup = warmup
        self._diarizer = None
        self._last_centroids: dict[str, list[float]] = {}

    @property
    def last_centroids(self) -> dict[str, list[float]]:
        return self._last_centroids

    def diarize(self, audio_path: Path) -> list[DiarizationSegment]:
        segments, centroids = diarize_channels(self.audio, audio_path, self._run_mono)
        self._last_centroids = centroids
        return segments

    def _run_mono(self, audio_path: Path) -> tuple[list[DiarizationSegment], dict[str, list[float]]]:
        result = self._load().diarize(str(audio_path), generate_colors=False)
        if result is None:
            return [], {}
        segments = [
            DiarizationSegment(
                speaker=str(segment["speaker"]),
                start=float(segment["start"]),
                end=float(segment["end"]),
            )
            for segment in result["merged_segments"]
        ]
        centroids = {
            str(speaker): [float(value) for value in (vector.tolist() if hasattr(vector, "tolist") else vector)]
            for speaker, vector in result.get("speaker_centroids", {}).items()
        }
        return segments, centroids

    def _load(self):
        if self._diarizer is not None:
            return self._diarizer
        try:
            import senko
        except ModuleNotFoundError as exc:  # senko lives behind an extra; speakrs is the default
            raise RuntimeError(
                "the senko diarization backend is not installed - install it with "
                "`uv sync --extra senko` (it pulls torch, ~2-3 GB), or use the default "
                "--diar-backend speakrs"
            ) from exc

        self._diarizer = senko.Diarizer(device=self.device, warmup=self.warmup, quiet=True)
        return self._diarizer


class SpeakrsDiarizer:
    """Diarize by shelling out to the speakrs-diar sidecar (packages/speakrs-diar).

    speakrs is a Rust library with no Python bindings, so a subprocess is the only
    way in. Running it out-of-process also keeps an ONNX/CoreML crash from taking
    the worker down with it.
    """

    name = "speakrs"

    def __init__(
        self,
        audio: AudioPreparer,
        binary: Path,
        models_dir: Path | None = None,
        mode: str = "cpu",
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        self.audio = audio
        self.binary = binary
        self.models_dir = models_dir
        self.mode = mode
        self.runner = runner
        self._last_centroids: dict[str, list[float]] = {}

    @property
    def last_centroids(self) -> dict[str, list[float]]:
        return self._last_centroids

    def diarize(self, audio_path: Path) -> list[DiarizationSegment]:
        segments, centroids = diarize_channels(self.audio, audio_path, self._run_mono)
        self._last_centroids = centroids
        return segments

    def _run_mono(self, audio_path: Path) -> tuple[list[DiarizationSegment], dict[str, list[float]]]:
        payload = self._invoke(audio_path)
        segments = [
            DiarizationSegment(
                speaker=str(segment["speaker"]),
                start=float(segment["start"]),
                end=float(segment["end"]),
            )
            for segment in payload.get("segments", [])
        ]
        centroids = {
            str(speaker): [float(value) for value in vector]
            for speaker, vector in payload.get("centroids", {}).items()
        }
        return segments, centroids

    def _invoke(self, audio_path: Path) -> dict:
        cmd = [str(self.binary), "--mode", self.mode]
        if self.models_dir is not None:
            cmd += ["--models-dir", str(self.models_dir)]
        else:
            # The sidecar downloads silently; say where before it takes minutes.
            announce_speakrs_models_dir()
        cmd.append(str(audio_path))

        completed = self.runner(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if completed.returncode != 0:
            raise RuntimeError(
                f"speakrs-diar failed with exit code {completed.returncode}: {(completed.stderr or '').strip()}"
            )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"speakrs-diar returned invalid JSON: {exc}") from exc
        version = payload.get("version")
        if version != SPEAKRS_CONTRACT_VERSION:
            raise RuntimeError(
                f"speakrs-diar contract version {version!r} is not the expected "
                f"{SPEAKRS_CONTRACT_VERSION} - rebuild packages/speakrs-diar"
            )
        return payload
