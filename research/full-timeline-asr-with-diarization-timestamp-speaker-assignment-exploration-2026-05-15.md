# Full-Timeline ASR with Diarization Timestamp Speaker Assignment Exploration

Date: 2026-05-15

## Question

Explore replacing diarization-derived ASR chunking with full-timeline ASR, chunking only when needed for memory/runtime limits, then assigning speaker labels from diarization timestamps. Do not build it yet.

## Current Service Shape

The current pipeline normalizes audio, runs full-file pyannote diarization, extracts speaker embeddings, then builds ASR chunks from the diarization timeline:

- `pipeline.py`: `run_with_artifacts` diarizes first, then calls `_transcribe_chunks`.
- `chunking.py`: `build_chunks` is driven by diarization segment starts/ends when diarization is present.
- `pipeline.py`: `_transcribe_chunks` exports each chunk to a WAV file and calls `asr.transcribe` once per chunk.
- `merge.py`: when ASR word timestamps exist, it assigns each word to a diarization speaker by timestamp overlap. When word timestamps are absent, it assigns each entire ASR chunk to the dominant speaker.

This means the service already has much of the timestamp-join machinery, but its ASR work is still shaped by diarization output.

## Comparison Implementation

The NVIDIA script at:

`speech-to-translation/packages/nvidia-4spk-stt/nvidia_4spk_stt/main.py`

uses a different shape:

- `run_asr` transcribes fixed-duration timeline chunks, defaulting to 600 seconds.
- `run_parallel` actually runs diarization first, frees the diarization model, then runs ASR.
- `assign_speakers` assigns speaker labels after ASR by matching ASR word timestamps to diarization segments.

This is conceptually:

`full-file diarization + timeline ASR chunks + timestamp speaker assignment`

The current service is closer to:

`full-file diarization + diarization-shaped ASR chunks + merge`

## External Pattern Check

This timestamp-join architecture is common:

- pyannoteAI documents merging diarization segments with timestamped ASR segments and recommends exclusive diarization for speaker-attributed transcripts:
  https://docs.pyannote.ai/tutorials/diarization-asr-merge
- pyannoteAI's STT orchestration exposes word-level transcription with speaker attribution, including Nvidia Parakeet as an STT option:
  https://docs.pyannote.ai/tutorials/speech-to-text-diarization
- WhisperX-style pipelines run ASR/alignment, run pyannote diarization, then assign speakers to words:
  https://deepwiki.com/m-bain/whisperX/3.4-speaker-diarization

## Key Feasibility Point

The local `sherpa_onnx` result object exposes `words`, `timestamps`, and `durations` attributes. The current `SherpaParakeetAsr._extract_words` already reads those fields.

However, prior project status notes say this Parakeet INT8 model path returned segment text but not usable word-level timestamps in a smoke run. Therefore the design is feasible only if a fresh validation proves at least one of these is true:

1. sherpa-onnx Parakeet INT8 returns usable word timestamps for the real audio/model;
2. sherpa-onnx returns segment timestamps good enough for segment-level speaker assignment;
3. another ASR backend is used for full-timeline ASR when word timestamps are required.

If ASR still returns no word timestamps, full-file ASR will degrade speaker attribution because the whole file or large 600-second chunks would be assigned to one dominant speaker.

## Proposed Design Direction

Add a new ASR chunking mode, not a replacement at first:

- `diarization`: current behavior, chunks based on diarization timeline.
- `timeline`: fixed timeline chunks independent of diarization.
- `full`: one ASR pass over the normalized WAV, with fallback to timeline chunks if memory or backend failure occurs.

The pipeline would become:

1. Normalize audio once.
2. Measure duration.
3. Run full-file diarization.
4. Extract embeddings.
5. Run ASR over the whole normalized timeline:
   - try one pass for short files;
   - otherwise use fixed chunks, e.g. 600 seconds;
   - on OOM or backend-specific failure, reduce chunk size and retry.
6. Offset chunk word timestamps back to absolute audio time.
7. Run speaker assignment from ASR timestamps against diarization.
8. Resolve speaker cache labels.

## Expected Performance Impact

This should reduce:

- number of exported ASR chunks;
- repeated `recognizer.create_stream` / `accept_waveform` / `decode_stream` overhead;
- diarization-boundary fragmentation;
- merge warnings caused by chunks that do not align well with words;
- file I/O from many tiny chunks.

It will not make pyannote diarization itself faster.

For the reported `main.py` 778s vs service 1885s gap, this design mostly targets the post-diarization ASR overhead, not the diarization wall time.

## Accuracy Risks

1. Word timestamps are mandatory for good speaker assignment.
2. Segment-level fallback is acceptable only if ASR segment timestamps are short and stable.
3. Long ASR chunks may accumulate timestamp drift; fixed 5-10 minute chunks are safer than one huge pass if drift appears.
4. Overlapping speech remains hard. The comparison script has lookahead and nearest-speaker fallback logic that is more nuanced than the current service's `dominant_speaker`.
5. If sherpa INT8 only returns full text without timestamps, keep diarization-shaped chunks or switch ASR backend for this mode.

## Implementation Sketch

No implementation done yet.

Likely small changes when ready:

- Add config/CLI field: `asr_chunking_mode: Literal["diarization", "timeline", "full"]`.
- Add config/CLI field: `asr_timeline_chunk_seconds`, default perhaps `600`.
- Add `build_timeline_chunks(duration, chunk_seconds, overlap_seconds=0)`.
- Split `_transcribe_chunks` into:
  - `_transcribe_diarization_chunks`;
  - `_transcribe_timeline_chunks`;
  - `_transcribe_full_or_timeline_fallback`.
- Keep `merge_asr_with_diarization` as the main speaker assignment path, but consider adopting the sweep-line/nearest/lookahead logic from the NVIDIA script if word counts become large.

## Validation Plan

Before building the production change:

1. Run a one-off script against the 14-minute file:
   - one full-file sherpa ASR pass;
   - one 600-second timeline chunk pass;
   - current diarization-shaped chunk pass.
2. For each mode record:
   - wall time;
   - peak RSS;
   - number of ASR calls;
   - number of exported chunk files;
   - whether words/timestamps/durations are populated;
   - ASR text length and sample output;
   - merge warnings.
3. Only implement the new mode if timestamp coverage is good enough.

## Decision

Recommended next step: benchmark timestamp availability and ASR wall time before production implementation.

The direction is sound if timestamps are available. If timestamps are not available from the current sherpa Parakeet INT8 backend, full-file ASR should not become the default because speaker attribution would become worse.
