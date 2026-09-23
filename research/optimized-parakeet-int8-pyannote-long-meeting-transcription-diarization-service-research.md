# Optimized Parakeet INT8 + pyannote Long-Meeting Transcription/Diarization Service Research

Date: 2026-05-14

## Goal

Design a service for 60-120 minute meeting audio that:

- transcribes with Parakeet TDT v3 rather than Whisper;
- runs on either CUDA with about 8 GB VRAM or CPU;
- uses INT8 quantization where practical;
- diarizes with pyannote;
- caches speaker embeddings once users map `SPEAKER_NN` labels to real names;
- uses the first 5 minutes of stereo audio to decide whether one or both channels can avoid full diarization.

The local workspace is greenfield: it currently contains only `packages/`, no git metadata, and no implementation files. `code-review-graph update` could not run because the directory is not a git repository.

## Source Article Summary

Source: https://medium.com/@rafaelgalle1/building-a-custom-scalable-audio-transcription-pipeline-whisper-pyannote-ffmpeg-d0f03f884330

The article builds a Whisper + pyannote + FFmpeg pipeline around:

- accepting file path, URL, or base64 audio;
- normalizing audio to PCM WAV at 16 kHz;
- splitting stereo when each channel corresponds to one participant;
- preprocessing levels from raw audio through sanitization, filters, noise reduction, and RMS normalization;
- Faster-Whisper transcription with word timestamps;
- pyannote diarization for mono/mixed audio;
- timeline merging into JSON with speaker labels, timestamps, and language metadata.

Transferable design ideas:

- keep FFmpeg as the deterministic ingest/normalization layer;
- use channel split as a fast path, but do not assume stereo means one speaker per channel;
- keep diarization and transcription outputs as independent timelines and merge late;
- expose preprocessing as a bounded option because overprocessing can harm diarization.

Changes needed for this project:

- replace Whisper with Parakeet TDT v3;
- account for Parakeet's mono 16 kHz input expectations;
- provide chunked/long-form inference for 60-120 minute files;
- support CPU and CUDA targets;
- add speaker identity enrollment/cache logic.

## ASR Model Findings

### Recommended base model

Use `nvidia/parakeet-tdt-0.6b-v3` as the canonical model.

Sources:

- https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3
- https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3/commit/bb0964b98adc4b962566f3df9f5f1844011871a3

Relevant properties:

- FastConformer-TDT, about 600M parameters.
- Supports 25 European languages.
- Accepts mono 16 kHz `.wav`/`.flac`.
- Produces punctuation/capitalization and timestamp-capable outputs through NeMo.
- Model card suggests local attention for long audio:
  `change_attention_model(self_attention_model="rel_pos_local_attn", att_context_size=[256, 256])`.
- Streaming/chunked inference reference uses:
  `chunk_secs=2`, `left_context_secs=10.0`, `right_context_secs=2.0`, `batch_size=32`.

### INT8 options

There is no obvious official NVIDIA NeMo INT8 checkpoint for `parakeet-tdt-0.6b-v3`.

Candidate INT8 paths:

1. Community ONNX INT8, e.g. `nasedkinpv/parakeet-tdt-0.6b-v3-onnx-int8`
   - Source: https://huggingface.co/nasedkinpv/parakeet-tdt-0.6b-v3-onnx-int8
   - Weight-only dynamic quantization for MatMul/Gemm, with Conv ops left FP32.
   - Useful for CPU ONNX Runtime and possibly CUDA execution providers, but decoding/timestamps need careful validation.

2. Community CPU FastAPI implementations around ONNX INT8.
   - Source: https://github.com/groxaxo/parakeet-tdt-0.6b-v3-fastapi-openai
   - Claims CPU INT8 throughput around 17-30x real time on tested desktop CPUs.
   - Useful as a pattern, not something to copy blindly.

3. Build our own ONNX INT8 export/quantization.
   - More control over runtime and packaging.
   - Higher risk because TDT decoding and timestamp preservation are non-trivial.

CUDA with 8 GB VRAM can likely use the official NeMo model in BF16/FP16 and still fit, while CPU should prefer ONNX INT8. If the requirement is strict "INT8 everywhere", the CUDA path needs a deliberate ONNX/TensorRT or torch quantization validation pass.

## Diarization Model Findings

### Recommended open-source model

Prefer `pyannote/speaker-diarization-community-1` for new work when license/access terms are acceptable.

Sources:

- https://github.com/pyannote/pyannote-audio
- https://huggingface.co/pyannote/speaker-diarization-community-1

Reasons:

- It is newer than legacy `speaker-diarization-3.1`.
- The pyannote repo reports better DER than legacy 3.1 on several benchmarks.
- It exposes `exclusive_speaker_diarization`, which simplifies alignment with ASR timestamps.
- It runs locally via `pyannote.audio`.

Fallback:

- `pyannote/speaker-diarization-3.1`
- Source: https://huggingface.co/pyannote/speaker-diarization-3.1
- Stable pure-PyTorch legacy pipeline, accepts mono 16 kHz, supports `num_speakers`, `min_speakers`, and `max_speakers`.

### Speaker embeddings

Use `pyannote/embedding` or the embedding model bundled by the selected diarization pipeline for identity caching.

Sources:

- https://huggingface.co/pyannote/embedding
- https://github.com/pyannote/pyannote-audio/blob/main/src/pyannote/audio/pipelines/speaker_verification.py

Relevant API pattern:

- `Inference(model, window="whole")` extracts one embedding for known single-speaker audio.
- `Inference.crop(audio, Segment(start, end))` extracts an embedding for a diarized segment.
- Cosine distance can compare embeddings.

Design implication:

- Pyannote diarization itself clusters unknown speakers. Identity assignment should be a post-processing step:
  1. diarize audio;
  2. collect high-confidence, non-overlap segments per `SPEAKER_NN`;
  3. compute an aggregate embedding per diarized speaker;
  4. compare against cached labeled embeddings;
  5. assign real labels when similarity clears a threshold;
  6. if the user later maps `SPEAKER_01` to `Alice`, store/update Alice's embedding profile.

## Draft Stereo Decision Tree

Run this probe on the first 5 minutes after FFmpeg normalization. For files shorter than 5 minutes, use the full file.

1. Detect channel count.
   - If mono: run normal full-file diarization on the mono stream.
   - If stereo or more: split channels to mono streams. For more than two channels, apply the same decision per channel but return an "unsupported multichannel policy" warning unless explicitly configured.

2. For each stereo channel, run VAD and short-window diarization on the first 5 minutes.
   - Measure speech duration.
   - Estimate speaker count.
   - Track overlap/crosstalk by checking whether both channels contain speech at the same time and whether one channel has low-energy leakage of the other.

3. Classify each channel.
   - `silent_or_leakage`: too little speech or mostly low-energy bleed.
   - `single_speaker`: one dominant speaker with stable embedding clusters.
   - `multi_speaker`: more than one speaker cluster or unstable speaker count.

4. Decide full processing mode.
   - Both channels `single_speaker`: skip full diarization; transcribe both full channels separately; assign deterministic channel labels.
   - One channel `single_speaker`, other `silent_or_leakage`: transcribe active channel; optionally mark silent channel as empty.
   - One channel `single_speaker`, other `multi_speaker`: transcribe both channels; use deterministic label for single-speaker channel; run full diarization only on the multi-speaker channel.
   - Both channels `multi_speaker`: either downmix and diarize full audio, or diarize each channel independently then merge. Recommended default: diarize each channel independently to preserve separation and reduce speaker confusion.
   - Ambiguous probe: fall back to full diarization on each channel independently, then merge.

5. Reconcile labels.
   - Channel-derived labels can be stable aliases like `CHANNEL_0_SPEAKER_00`.
   - Pyannote-derived labels stay as `SPEAKER_NN` until identity cache matching or user mapping resolves them.

## Chunking Strategy for 60-120 Minute Meetings

Recommended default:

- Normalize once to 16 kHz mono channel files.
- Run diarization per selected full channel/audio path first, because speaker turns guide transcription segmentation and identity caching.
- For ASR, chunk by diarization-aware windows, not arbitrary fixed windows only:
  - target chunks: 30-120 seconds;
  - split on long silence where possible;
  - keep small context overlap, e.g. 2-5 seconds;
  - merge duplicate words/timestamps in overlap.
- For Parakeet NeMo long-form GPU path, validate local attention mode and/or the official streaming script settings.
- For ONNX INT8 CPU path, use fixed-size feature chunks plus decoder state/context if the chosen implementation supports it; otherwise use diarization/silence-bounded chunks.

Why not transcribe each tiny diarization turn independently:

- It improves speaker attribution but can lose ASR context, punctuation quality, and proper nouns.
- Better: transcribe medium windows and assign speaker labels at word/segment level afterward.

## Service Architecture Candidates

### Option A: Python FastAPI service with local worker

- FastAPI for upload/job/status/result.
- Local disk workspace per job.
- In-process model manager loads pyannote and ASR once.
- SQLite for job state and embedding cache.
- Best for initial local service and single-machine deployment.

### Option B: FastAPI API + separate worker queue

- API accepts jobs and returns IDs.
- Worker process handles GPU/CPU-heavy inference.
- Redis/RQ, Dramatiq, or Celery for queueing.
- SQLite or Postgres for job state and embedding cache.
- Better for long 2-hour meetings because HTTP request lifetimes do not matter.

### Option C: Reuse Replicate/Cog style

- Pattern borrowed from the source project.
- Good for model packaging and managed GPU scale.
- Less aligned with a persistent service that needs embedding cache and local CPU fallback.

Recommended: Option B if this is meant to be a durable service, Option A if the first milestone should stay small.

## Open Decisions

1. Whether this repository should start as a Python package/service from scratch.
2. Whether CPU must be first-class production support or just a fallback.
3. Whether "INT8" is strict for both CUDA and CPU, or acceptable as CPU INT8 plus CUDA FP16/BF16.
4. Whether identity cache should be per tenant/workspace, per recurring meeting, or global.
5. Whether outputs need word-level timestamps, segment-level timestamps, or both.

