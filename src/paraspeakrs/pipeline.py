from __future__ import annotations

from pathlib import Path
import threading
import time
from typing import Callable
from uuid import uuid4

from .asr import AsrEngine
from .audio import AudioPreparer
from .cache import SpeakerCache
from .chunking import build_chunks
from .diarization import Diarizer, collapse_channels
from .embeddings import NullSpeakerEmbeddingExtractor, SpeakerEmbeddingExtractor
from .merge import merge_asr_with_diarization
from .models import AsrSegment, LabelingArtifacts, PipelineArtifacts, TranscriptionResult
from .snippets import export_label_snippets

ProgressCallback = Callable[[str, str | None, int | None], None]


class TranscriptionPipeline:
    def __init__(
        self,
        diarizer: Diarizer,
        asr: AsrEngine,
        audio: AudioPreparer,
        speaker_cache: SpeakerCache,
        embedding_extractor: SpeakerEmbeddingExtractor | None = None,
        chunk_target_seconds: float = 90.0,
        chunk_overlap_seconds: float = 2.0,
    ) -> None:
        self.diarizer = diarizer
        self.asr = asr
        self.audio = audio
        self.speaker_cache = speaker_cache
        self.embedding_extractor = embedding_extractor or NullSpeakerEmbeddingExtractor()
        self.chunk_target_seconds = chunk_target_seconds
        self.chunk_overlap_seconds = chunk_overlap_seconds

    def run(
        self,
        *,
        work_dir: Path,
        file_path: Path | None = None,
        file_url: str | None = None,
        file_base64: str | None = None,
        job_id: str | None = None,
        progress: ProgressCallback | None = None,
    ) -> TranscriptionResult:
        return self.run_with_artifacts(
            work_dir=work_dir,
            file_path=file_path,
            file_url=file_url,
            file_base64=file_base64,
            job_id=job_id,
            progress=progress,
        ).result

    def run_with_artifacts(
        self,
        *,
        work_dir: Path,
        file_path: Path | None = None,
        file_url: str | None = None,
        file_base64: str | None = None,
        job_id: str | None = None,
        progress: ProgressCallback | None = None,
        single_speaker_channels: frozenset[str] = frozenset(),
    ) -> PipelineArtifacts:
        """`single_speaker_channels` names stereo channels ("L", "R") known to
        carry one person each; see :func:`collapse_channels`."""
        job_id = job_id or str(uuid4())
        _report(progress, "materialize", "copying uploaded audio into workspace", 5)
        raw = self.audio.materialize(
            work_dir,
            file_path=file_path,
            file_url=file_url,
            file_base64=file_base64,
        )
        _report(progress, "normalize", "converting audio to mono 16 kHz WAV", 10)
        normalized = self.audio.normalize_mono_16khz(raw, work_dir / "normalized.wav")
        diar_audio = self.audio.normalize_16khz_preserving_channels(raw, work_dir / "normalized-diar.wav")
        _report(progress, "duration", "probing normalized audio duration", 15)
        duration = self.audio.duration_seconds(normalized)
        diarization = _run_with_heartbeat(
            progress,
            step="diarization",
            detail=f"running {self.diarizer.name} speaker diarization",
            start_percent=25,
            max_percent=44,
            work=lambda: self.diarizer.diarize(diar_audio),
        )
        _report(
            progress,
            "diarization",
            f"completed {self.diarizer.name} diarization: {len(diarization)} segments",
            44,
        )
        _report(progress, "embeddings", "extracting speaker embeddings", 45)
        speaker_embeddings = self.embedding_extractor.extract(diar_audio, diarization)
        diarization, speaker_embeddings = collapse_channels(diarization, speaker_embeddings, single_speaker_channels)
        asr_segments = self._transcribe_chunks(work_dir, normalized, diarization, duration, progress)
        _report(progress, "merge", "merging ASR text with diarization", 90)
        merged, warnings = merge_asr_with_diarization(asr_segments, diarization)
        _report(progress, "speaker-cache", "resolving speaker labels from cache", 95)
        labels = self.speaker_cache.resolve_embeddings(speaker_embeddings)
        if not labels:
            labels = self.speaker_cache.resolve(diarization)
        for segment in merged:
            segment.resolved_label = labels.get(segment.speaker)
            for word in segment.words:
                word.resolved_label = labels.get(word.speaker)
        result = TranscriptionResult(
            job_id=job_id,
            duration_seconds=duration,
            num_speakers=len({segment.speaker for segment in diarization}),
            segments=merged,
            warnings=warnings,
        )
        return PipelineArtifacts(
            result=result,
            diarization=diarization,
            speaker_embeddings=speaker_embeddings,
            normalized_audio_path=normalized,
            source_path=file_path,
        )

    def prepare_labeling(
        self,
        *,
        work_dir: Path,
        file_path: Path | None = None,
        file_url: str | None = None,
        file_base64: str | None = None,
        probe_seconds: float | None = 300.0,
    ) -> LabelingArtifacts:
        raw = self.audio.materialize(
            work_dir,
            file_path=file_path,
            file_url=file_url,
            file_base64=file_base64,
        )
        normalized = self.audio.normalize_mono_16khz(raw, work_dir / "normalized.wav")
        diar_full = self.audio.normalize_16khz_preserving_channels(raw, work_dir / "normalized-diar.wav")
        duration = self.audio.duration_seconds(normalized)
        labeling_audio = normalized
        diar_audio = diar_full
        labeling_duration = duration
        if probe_seconds is not None and duration > probe_seconds:
            labeling_audio = self.audio.export_chunk(normalized, work_dir / "label-probe.wav", 0.0, probe_seconds)
            diar_audio = self.audio.export_chunk_preserving_channels(diar_full, work_dir / "label-probe-diar.wav", 0.0, probe_seconds)
            labeling_duration = probe_seconds
        diarization = self.diarizer.diarize(diar_audio)
        speaker_embeddings = self.embedding_extractor.extract(diar_audio, diarization)
        resolved_labels = self.speaker_cache.resolve_embeddings(speaker_embeddings)
        if not resolved_labels:
            resolved_labels = self.speaker_cache.resolve(diarization)
        snippet_paths = export_label_snippets(
            self.audio,
            normalized,
            work_dir / "label-snippets",
            diarization,
        )
        return LabelingArtifacts(
            duration_seconds=duration,
            diarization=diarization,
            speaker_embeddings=speaker_embeddings,
            resolved_labels=resolved_labels,
            snippet_paths=snippet_paths,
            normalized_audio_path=labeling_audio,
        )

    def _transcribe_chunks(
        self,
        work_dir: Path,
        audio_path: Path,
        diarization,
        duration: float,
        progress: ProgressCallback | None = None,
    ) -> list[AsrSegment]:
        chunks = build_chunks(
            diarization,
            duration,
            self.chunk_target_seconds,
            self.chunk_overlap_seconds,
        )
        segments: list[AsrSegment] = []
        total = len(chunks)
        transcribed_through = 0.0
        for index, chunk in enumerate(chunks):
            percent = 50 + int((index / max(1, total)) * 35)
            _report(progress, "transcription", f"transcribing chunk {index + 1} of {total}", percent)
            chunk_path = self.audio.export_chunk(audio_path, work_dir / f"chunk-{index:04d}.wav", chunk.start, chunk.end)
            result = self.asr.transcribe(chunk_path)
            result_end = result.end + chunk.start if result.end > result.start else chunk.end
            words = [
                word.model_copy(update={"start": word.start + chunk.start, "end": word.end + chunk.start})
                for word in result.words
            ]
            update = {
                "start": result.start + chunk.start,
                "end": min(chunk.end, result_end),
                "words": words,
            }
            # Chunks overlap, so this head repeats audio the previous chunk
            # already transcribed. Drop it here rather than there: the previous
            # chunk heard it with full preceding context, while a chunk's
            # opening seconds start mid-utterance and come out garbled. Keeping
            # the earlier rendering also cannot lose audio -- the previous chunk
            # covered that whole span. Needs word timestamps; without them the
            # duplicate text is kept as before.
            if words and index > 0:
                kept = [word for word in words if word.start >= transcribed_through]
                if len(kept) != len(words):
                    if not kept:
                        transcribed_through = max(transcribed_through, chunk.end)
                        continue
                    update["words"] = kept
                    update["text"] = " ".join(word.word for word in kept)
                    update["start"] = kept[0].start
            transcribed_through = max(transcribed_through, chunk.end)
            segments.append(result.model_copy(update=update))
        return segments


def _report(progress: ProgressCallback | None, step: str, detail: str | None, percent: int | None) -> None:
    if progress is not None:
        progress(step, detail, percent)


def _run_with_heartbeat(
    progress: ProgressCallback | None,
    *,
    step: str,
    detail: str,
    start_percent: int,
    max_percent: int,
    work,
    interval_seconds: float = 10.0,
):
    _report(progress, step, detail, start_percent)
    if progress is None:
        return work()

    done = threading.Event()

    def heartbeat() -> None:
        started = time.monotonic()
        while not done.wait(interval_seconds):
            elapsed = int(time.monotonic() - started)
            percent = min(max_percent, start_percent + max(1, elapsed // 15))
            _report(progress, step, f"{detail} (elapsed {elapsed}s)", percent)

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        return work()
    finally:
        done.set()
        thread.join(timeout=1.0)
