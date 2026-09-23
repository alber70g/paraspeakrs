# Parakeet INT8 + Senko Service Stacked Design

Date: 2026-05-14 (updated 2026-05-15: diarization stack switched from pyannote to Senko)

> **Update 2026-05-15.** The diarization engine is now Senko (CAM++ embeddings + spectral clustering), not pyannote. The high-level architecture, output contract, milestones, and risks below still apply; references to "pyannote" should be read as "the diarization engine" — Senko has replaced it. Two concrete behavioural changes:
> - Stereo handling for Milestone 1 is implemented as: split L/R → diarize each mono channel with Senko → merge with `L_`/`R_` speaker prefixes. The Milestone 2 "decision tree" below is therefore partially superseded (single-speaker-per-channel optimisation is no longer applied; both channels always run through diarization).
> - Speaker embeddings come from Senko's `speaker_centroids` (192-dim CAM++), not a separate pyannote embedding model.

## Purpose

Build a transcription and diarization service for long meetings, typically 60-90 minutes and up to 2 hours. The service runs with Parakeet TDT v3 INT8 for transcription and Senko for diarization. It supports CUDA with about 8 GB VRAM and CPU, uses an API plus worker architecture, and keeps a global speaker embedding cache so user-provided mappings from `SPEAKER_NN` to real labels can improve future jobs.

This design is intentionally stacked. Each milestone creates a working checkpoint before adding more branching or audio treatment.

## Non-Negotiable Decisions

- Service shape: API plus background worker.
- ASR model family: Parakeet TDT v3, not Whisper.
- Quantization: INT8 everywhere.
- Diarization: run diarization before transcription.
- Speaker cache: global embedding cache across all jobs.
- Output detail: word-level timestamps when possible; segment timestamps always.
- Completeness rule: transcription is leading. If diarization misses speech but ASR finds words, keep the words and mark speaker attribution as best-effort instead of dropping content.

## Runtime Candidates

### ASR

Canonical source model:

- `nvidia/parakeet-tdt-0.6b-v3`

Initial INT8 runtime candidates:

- ONNX INT8 community export such as `nasedkinpv/parakeet-tdt-0.6b-v3-onnx-int8`
- Custom export from the NVIDIA NeMo checkpoint if the community export fails timestamp, accuracy, CUDA, or packaging validation

Runtime target:

- CPU: ONNX Runtime CPU execution provider with INT8 model.
- CUDA: ONNX Runtime CUDA or TensorRT execution provider with INT8 model.

### Diarization

Selected engine (as of 2026-05-15):

- [Senko](https://pypi.org/project/senko/) — VAD + CAM++ speaker embeddings + spectral clustering, single library call.

Previously evaluated:

- `pyannote/speaker-diarization-community-1` (replaced — Senko is faster on CPU/CoreML and returns ready-to-use speaker centroids in one call).

Embedding extraction:

- Reuse Senko's `speaker_centroids` (192-dim CAM++) returned directly from `Diarizer.diarize(...)`. No separate embedding model is loaded.

## Architecture

The service has four main surfaces:

1. API process
   - Accepts upload, URL, or base64 input.
   - Creates a job record.
   - Returns job status and final result.
   - Accepts user speaker-label feedback, e.g. `SPEAKER_01 = Alice`.

2. Worker process
   - Downloads or materializes the input.
   - Runs FFmpeg normalization and optional preprocessing.
   - Runs diarization.
   - Runs Parakeet INT8 transcription.
   - Merges timelines.
   - Updates the global speaker embedding cache.

3. Storage
   - Job state and metadata.
   - Intermediate artifacts with retention policy.
   - Final JSON transcript.
   - Global speaker embeddings and label history.

4. Model manager
   - Loads ASR and diarization models once per worker.
   - Selects CPU or CUDA runtime.
   - Enforces INT8 ASR runtime.

## Output Contract

Each completed job returns:

```json
{
  "job_id": "string",
  "language": "en",
  "duration_seconds": 0.0,
  "num_speakers": 0,
  "segments": [
    {
      "speaker": "SPEAKER_00",
      "resolved_label": "Alice",
      "speaker_confidence": 0.0,
      "start": 0.0,
      "end": 0.0,
      "text": "string",
      "words": [
        {
          "word": "string",
          "start": 0.0,
          "end": 0.0,
          "speaker": "SPEAKER_00",
          "resolved_label": "Alice"
        }
      ]
    }
  ],
  "warnings": []
}
```

If word-level timestamps are unavailable from the selected ASR runtime, the service still returns segment timestamps and adds a warning. The implementation should keep word-level timestamps as a validation target, not silently degrade.

### Secondary Representation: Utterance Text

In addition to the JSON contract above, the service exposes `GET /jobs/{job_id}/txt`, which returns the same completed job as a `text/plain` utterance transcript:

```text
[HH:MM:SS] NAME: utterance text
```

`NAME` is the resolved speaker label when known, otherwise the raw `SPEAKER_xx` (or `L_*` / `R_*` for stereo). Utterances are produced by splitting each speaker's run on sentence enders (`.`, `?`, `!`) and on relative silence — a gap that exceeds the 90th percentile of recent same-speaker gaps, with a 0.6 s absolute floor. The silence window resets on speaker change. When the ASR runtime returns no word timestamps, sub-utterance timestamps are pro-rated across the segment span by character count.

Response codes: `200` when completed, `202` while queued or running, `409` when failed, `404` for an unknown job ID.

## Milestone 1: Minimal Pipeline

Goal: produce a complete speaker-labeled transcript for mono or already-normalized audio without stereo branching or the full preprocessing stack.

### Flow

1. API creates a transcription job.
2. Worker materializes input audio.
3. Worker normalizes to mono 16 kHz PCM WAV.
4. Worker runs Senko diarization on the channel-preserving normalized file (mono → one pass; stereo → L/R split, diarize each, merge with `L_`/`R_` prefixes).
5. Worker creates ASR chunks from diarization-aware windows.
6. Worker transcribes chunks with Parakeet TDT v3 INT8.
7. Worker merges ASR words/segments with diarization output.
8. Worker writes final JSON.
9. API serves job result.

### Chunking

Use medium chunks rather than tiny diarization turns:

- target chunk length: 30-120 seconds;
- prefer silence or diarization boundaries;
- allow small overlap, around 2-5 seconds;
- remove duplicate overlap text during merge.

This preserves ASR context while still using diarization to guide timestamps.

### Speaker Cache

At this milestone, cache writes can be simple:

1. For each diarized speaker, collect non-overlapping speech regions.
2. Extract embeddings from representative regions.
3. Average them into a speaker centroid for the job.
4. If the user later maps `SPEAKER_NN` to a real label, store that centroid globally.

Cache reads:

1. Compare job speaker centroids to global centroids.
2. Resolve labels only when cosine similarity clears a conservative threshold.
3. Otherwise keep the `SPEAKER_NN` label.

### Checkpoint

Milestone 1 is complete when:

- a 10-15 minute mono fixture can be processed through API plus worker;
- ASR runs through an INT8 runtime on CPU;
- diarization precedes transcription;
- final JSON includes speaker segments and words where available;
- diarization gaps do not cause ASR text to be dropped;
- user speaker-label feedback updates the global cache.

## Milestone 2: Stereo Decision Tree

Goal: add channel-aware routing so the service avoids full diarization when stereo channels clearly map to speakers, but falls back conservatively when channel assumptions are unsafe.

### First 5 Minute Probe

For stereo files, split channels and analyze the first 5 minutes. If the file is shorter than 5 minutes, use the full duration.

For each channel:

- measure speech duration;
- run VAD;
- estimate speaker count with short-window diarization;
- inspect crosstalk/leakage by comparing simultaneous speech and relative channel energy;
- classify the channel as `silent_or_leakage`, `single_speaker`, `multi_speaker`, or `ambiguous`.

### Decision Tree

1. If input is mono, use the Milestone 1 path.
2. If stereo, split into channel 0 and channel 1.
3. If both channels are `single_speaker`, preprocess each channel into the canonical mono format, transcribe each full channel, skip full diarization, and assign deterministic channel speaker labels.
4. If one channel is `single_speaker` and the other is `silent_or_leakage`, transcribe the active channel and return an empty or low-confidence result for the silent channel.
5. If one channel is `single_speaker` and the other is `multi_speaker`, transcribe both channels, assign a deterministic speaker label to the single-speaker channel, and run full diarization only on the multi-speaker channel.
6. If both channels are `multi_speaker`, preprocess each channel independently, diarize each channel independently, transcribe each channel, and merge timelines.
7. If either channel classification is `ambiguous`, downmix and preprocess to canonical mono, then run full diarization and transcription on the mono file.

The ambiguous case must not preserve uncertain channel assumptions. It should use the same mono 16 kHz path expected by Senko and Parakeet.

> **Note (2026-05-15).** Milestone 1 already ships a simpler stereo path: split L/R, diarize each channel with Senko, merge results with `L_`/`R_` speaker prefixes. The decision-tree optimisations below (skipping diarization on a confidently-single-speaker channel) are deferred and remain a future option if probe-based classification proves worth the added complexity.

### Merge Rules

- Preserve chronological order across channels.
- Keep original channel metadata in every segment.
- Use deterministic channel labels only when the probe is confident.
- If diarization and ASR disagree, keep ASR text and mark speaker attribution as lower confidence.

### Checkpoint

Milestone 2 is complete when:

- stereo with one speaker per channel skips full diarization;
- stereo with one mixed channel diarizes only that channel;
- ambiguous stereo downmixes to mono and uses full diarization;
- merged output preserves channel metadata;
- no branch drops transcribed words because diarization was incomplete.

## Milestone 3: Article-Inspired Preprocessing Stack

Goal: add configurable audio treatment from the source article while keeping a safe default for speaker identity and ASR quality.

### Preprocessing Levels

Level 0: raw audio

- Only use for debugging or controlled fixtures.

Level 1: sanitization

- Convert to PCM WAV.
- Resample to 16 kHz.
- Convert to mono for the Parakeet ASR path; keep channels for the Senko diarization path so the stereo split can run.

Level 2: filtering

- Apply high-pass filter to reduce rumble.
- Apply low-pass filter to reduce hiss and high-frequency noise.

Level 3: noise reduction

- Apply conservative spectral noise reduction.
- Avoid aggressive settings before diarization because they can damage speaker characteristics.

Level 4: RMS normalization

- Normalize loudness to a target dBFS.
- Use after filtering and optional noise reduction.

### Default Policy

Default preprocessing should be Level 4 with conservative noise reduction for general meeting audio, with a service option to use Level 2 when speaker identity quality is more important than noise cleanup.

For diarization-sensitive paths:

- run diarization on Level 2 or conservative Level 4 audio;
- avoid aggressive noise reduction before embedding extraction;
- if both clean and enhanced variants are produced, use cleaner audio for ASR and less-altered audio for speaker embeddings.

For ambiguous stereo:

- downmix first;
- apply canonical preprocessing;
- run full diarization and transcription on that canonical mono file.

### Checkpoint

Milestone 3 is complete when:

- all preprocessing levels are available through job options;
- the default preprocessing path is documented in job metadata;
- diarization can run on a less-destructive variant when needed;
- ASR can run on the enhanced variant;
- output includes preprocessing metadata and warnings;
- regression fixtures show preprocessing does not remove speech or collapse speakers unexpectedly.

## Validation Plan

Use a small fixture set before optimizing:

- mono two-speaker meeting sample;
- stereo one-speaker-per-channel sample;
- stereo sample with one mixed channel;
- ambiguous stereo sample with crosstalk;
- long sample at least 60 minutes;
- noisy sample requiring filtering/normalization;
- sample with known recurring speaker for cache validation.

Required checks:

- CPU INT8 ASR completes.
- CUDA INT8 ASR completes within 8 GB VRAM.
- Diarization runs before transcription.
- Word timestamps are present when runtime supports them.
- If word timestamps are unavailable, warning is explicit.
- No ASR words are dropped due to missing diarization.
- Global speaker cache resolves known speakers only above threshold.
- False speaker cache matches are logged and easy to correct.

## Main Risks

1. Parakeet TDT v3 INT8 timestamp support may be weaker in community ONNX exports than in official NeMo.
   - Mitigation: keep ASR runtime behind an interface and validate timestamps before locking the model artifact.

2. CUDA INT8 may require TensorRT/export work beyond a CPU ONNX model.
   - Mitigation: make CUDA INT8 a milestone gate, not an assumption.

3. Global speaker cache can create false identity matches.
   - Mitigation: conservative similarity threshold, store confidence, allow user correction, and keep original `SPEAKER_NN`.

4. Noise reduction can harm diarization.
   - Mitigation: separate diarization/embedding audio from ASR-enhanced audio when needed.

5. Stereo channel assumptions can be wrong.
   - Mitigation: first 5 minute probe and ambiguous fallback to canonical mono full diarization.

## References

- Source article: `research/source-medium-rafael-galle-custom-scalable-audio-transcription-diarization-pipeline-whisper-pyannote-ffmpeg.md`
- Research synthesis: `research/optimized-parakeet-int8-pyannote-long-meeting-transcription-diarization-service-research.md`
- NVIDIA Parakeet TDT v3: https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3
- Senko (current diarization engine): https://pypi.org/project/senko/
- pyannote community diarization (historical reference): https://huggingface.co/pyannote/speaker-diarization-community-1
