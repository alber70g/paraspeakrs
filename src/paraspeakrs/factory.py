from __future__ import annotations

import os
import sys

from .asr import MockAsr, OpenAICompatibleParakeetAsr, SherpaParakeetAsr
from .audio import AudioPreparer
from .cache import SpeakerCache, embedding_namespace
from .config import Settings, sherpa_model_name
from .diarization import SenkoDiarizer, SpeakrsDiarizer
from .embeddings import CentroidEmbeddingExtractor
from .model_fetch import ensure_sherpa_model, find_sherpa_model, model_is_present, present_precision
from .pipeline import TranscriptionPipeline

# Senko's Numba/OpenMP stack segfaults on macOS without these. setdefault so an
# explicit override from the caller still wins. Must be set before senko (and
# thus Numba) is imported — SenkoDiarizer imports it lazily, so setting them here
# is early enough. Left untouched off macOS, where the single-thread cap would
# only slow diarization.
if sys.platform == "darwin":
    for _key, _value in (
        ("KMP_DUPLICATE_LIB_OK", "TRUE"),
        ("NUMBA_THREADING_LAYER", "workqueue"),
        ("NUMBA_NUM_THREADS", "1"),
    ):
        os.environ.setdefault(_key, _value)


def _use_downloaded_model(settings: Settings) -> Settings:
    """Point the settings at a model that is already on disk, if there is one.

    An explicit precision only accepts that precision -- the user asked for it, so
    finding a different one is a reason to download, not to substitute. Otherwise the
    precision this workspace chose on first run goes first, then best-to-worst.
    """
    from dataclasses import replace

    from . import precision as precision_mod

    assert settings.sherpa_model_dir is not None
    if settings.precision_is_explicit:
        order: tuple[str, ...] = (settings.sherpa_precision,)
    else:
        remembered = precision_mod.saved_choice(settings.workspace_dir)
        order = tuple(dict.fromkeys((remembered, *precision_mod.ORDER))) if remembered else precision_mod.ORDER
    found = find_sherpa_model(settings.sherpa_model_dir, settings.workspace_dir, order)
    if found is None or found == settings.sherpa_model_dir:
        return settings
    return replace(
        settings,
        sherpa_model_dir=found,
        sherpa_precision=present_precision(found) or settings.sherpa_precision,  # type: ignore[arg-type]
    )


def describe_asr(asr: object) -> str:
    """One line naming the ASR model a pipeline will actually use."""
    if isinstance(asr, SherpaParakeetAsr):
        precision = present_precision(asr.model_dir) or "unknown precision"
        return f"Parakeet {precision.upper()} · {asr.model_dir}"
    if isinstance(asr, OpenAICompatibleParakeetAsr):
        return f"{asr.model} via {asr.base_url}"
    return "mock ASR"


def _settle_precision(settings: Settings) -> Settings:
    """Ask which ASR model to fetch, once, before the first download.

    Only reached when the model is actually missing: an existing install must never
    be interrupted by a question, and re-asking after the answer is on disk would be
    worse than not asking at all.
    """
    from dataclasses import replace

    from . import precision as precision_mod

    assert settings.sherpa_model_dir is not None
    if model_is_present(settings.sherpa_model_dir):
        return settings

    chosen = precision_mod.resolve(
        workspace_dir=settings.workspace_dir,
        model_root=settings.sherpa_model_dir.parent,
        requested=settings.sherpa_precision,
        explicit=settings.precision_is_explicit,
    )
    if chosen == settings.sherpa_precision:
        return settings
    return replace(
        settings,
        sherpa_precision=chosen,
        sherpa_model_dir=settings.sherpa_model_dir.parent / sherpa_model_name(chosen),
    )


def build_pipeline(settings: Settings) -> TranscriptionPipeline:
    # Fetch before validating: validate_runtime() rejects a missing model dir, and
    # for an installed tool "missing" is simply the state of a first run.
    if settings.asr_backend == "sherpa" and settings.sherpa_model_dir is not None:
        settings = _use_downloaded_model(settings)
        settings = _settle_precision(settings)
        ensure_sherpa_model(settings.sherpa_model_dir, settings.sherpa_precision)
    settings.validate_runtime()
    if settings.asr_backend == "sherpa":
        assert settings.sherpa_model_dir is not None
        asr = SherpaParakeetAsr(settings.sherpa_model_dir, settings.device, settings.sherpa_num_threads)
    elif settings.asr_backend == "openai":
        assert settings.openai_base_url is not None
        asr = OpenAICompatibleParakeetAsr(
            settings.openai_base_url,
            settings.openai_api_key,
            settings.openai_model,
        )
    else:
        asr = MockAsr()
    # stderr: over stdio MCP, stdout is the protocol channel.
    print(f"ASR model: {describe_asr(asr)}", file=sys.stderr)

    audio = AudioPreparer()
    if settings.diar_backend == "speakrs":
        diarizer = SpeakrsDiarizer(
            audio=audio,
            binary=settings.speakrs_bin,
            models_dir=settings.speakrs_models_dir,
            mode=settings.speakrs_mode,
        )
    else:
        diarizer = SenkoDiarizer(audio=audio, device=settings.senko_device, warmup=settings.senko_warmup)

    return TranscriptionPipeline(
        diarizer=diarizer,
        asr=asr,
        audio=audio,
        speaker_cache=SpeakerCache(
            settings.workspace_dir / "speaker-cache.json",
            namespace=embedding_namespace(settings.diar_backend),
        ),
        embedding_extractor=CentroidEmbeddingExtractor(diarizer),
        chunk_target_seconds=settings.chunk_target_seconds,
        chunk_overlap_seconds=settings.chunk_overlap_seconds,
    )
