# Building a Custom, Scalable Audio Transcription Diarization Pipeline (Whisper + Pyannote + FFmpeg)

Source URL: https://medium.com/@rafaelgalle1/building-a-custom-scalable-audio-transcription-pipeline-whisper-pyannote-ffmpeg-d0f03f884330

Author: Rafael Galle

Published: 2025-09-10

Stored from user-provided article text on 2026-05-14.

## Article Text

In this article I'll walk you through the design of a custom transcription pipeline that I built to solve three recurring problems in large-scale call transcription:

- **Slow transcription latency** (hours or even days)
- **Excessive costs** with third-party providers
- **Diarization errors** in noisy environments or with similar voices

We were relying on a transcription provider that was **extremely expensive, didn't scale well, and often failed under peak loads**. On busy days, transcriptions could take many hours, sometimes even rolling over to the next day, and the overall quality wasn't great either.

That's when we started looking for alternatives.

With this custom solution, we managed to **reduce transcription costs by 90%**. Running on **Replicate**, we only pay for the actual execution time of the GPU instances. The pipeline can also be self-hosted, but Replicate has proven to be a very effective and convenient option for our workloads.

After testing several market solutions (AssemblyAI, ElevenLabs, others), I decided to build my own modular pipeline, using **FFmpeg, Whisper, and Pyannote,** optimized for **speed, cost, and accuracy**.

## 1. Multi-source ingestion: why 3 different input types?

The pipeline accepts:

- **file_path** (ideal for local use as development, or CLI usage)
- **file_url** (integration with external systems, like cloud storage or CRMs)
- **file_string (base64)** (useful where you don't want to persist audio to disk)

This flexibility makes the system portable and easy to integrate across environments (on-prem, cloud, APIs).

## 2. Conversion to PCM 16 kHz WAV: aligning with Whisper's training

Before transcription, all inputs are converted via **FFmpeg** into **PCM WAV at 16 kHz**.

Why? OpenAI published that Whisper models were trained on audio resampled to this format. By normalizing all inputs to this format, we:

- **Increase transcription accuracy**, since we match the model's training distribution
- **Simplify preprocessing** for later stages

## 3. Stereo vs Mono: why split channels?

- **Stereo audio (agent/customer calls)**: each channel represents one speaker. Splitting them guarantees **100% speaker attribution accuracy**, since diarization is no longer probabilistic.
- **Mono audio**: requires diarization models (like Pyannote), which infer speaker turns based on acoustic similarity.

Whenever stereo is available, we bypass Pyannote and gain both **speed (30-50% faster)** and **cost reduction**.

## 4. Preprocessing levels (0-4): controlling audio treatment

Each channel (or mono file) passes through a configurable preprocessing pipeline:

- **Level 0** -> Raw audio
- **Level 1** -> Sanitization (mono, 16 kHz, PCM)
- **Level 2** -> + High-pass & Low-pass filters (remove rumble, hiss)
- **Level 3** -> + Noise reduction (spectral gating, configurable strength)
- **Level 4** -> + RMS normalization (target dBFS)

## Why preprocessing matters

- **Filters** improve tokenization and timestamp alignment
- **Noise reduction** reduces false pauses and improves diarization stability
- **Normalization** equalizes loudness across files, reducing transcription drift

Note: overprocessing clean audio can hurt transcription quality. Always calibrate based on source.

## 5. Whisper transcription

Transcription is handled by **Faster-Whisper (large-v3-turbo)** with several tuning options to improve accuracy, especially on noisy or low-quality audio:

- **Prompt injection** -> inject domain-specific acronyms, names, or jargon to stabilize logits and guide decoding. This is especially important in **low-quality audio**, where the model might be unsure between multiple hypotheses: by seeing expected words in the prompt, it biases decoding toward valid domain terms instead of random guesses.
- **Language detection** -> automatic or forced, depending on the input context.
- **Word-level timestamps** -> critical for downstream analytics (pauses, interruptions, WPM).
- **Beam search (`beam_size`)** -> evaluates multiple hypotheses in parallel (e.g., 5-10 beams). Compared to greedy decoding (`beam_size=1`), which just picks the top token at each step, beam search explores alternatives and backtracks when the obvious path leads to nonsense. This significantly improves recovery of words in degraded or overlapping speech.

Together, these parameters let Whisper not only transcribe clean speech efficiently, but also fill the gaps in poor recordings by leveraging context, domain knowledge, and probabilistic decoding.

## 6. Diarization: Pyannote vs channel split

**Pyannote** is a deep-learning framework for speaker diarization. It combines multiple specialized models in a pipeline:

1. **Voice Activity Detection (VAD)** -> detects which parts of the audio contain speech vs. silence, noise, or music. This avoids wasting resources on non-speech regions.
2. **Speaker Embedding Extraction** -> for each speech segment, Pyannote generates a **vector embedding** that captures acoustic and timbral characteristics of the voice.
3. **Clustering** -> embeddings are grouped using clustering algorithms (e.g., Agglomerative or Spectral Clustering). Each cluster corresponds to a unique speaker.
4. **Attribution** -> each speech segment is labeled (`SPEAKER_0`, `SPEAKER_1`, ...) based on its cluster assignment.

In short: Pyannote doesn't know who the speakers are, but ensures that similar voices are consistently grouped under the same label.

## Pyannote output

The output is a sequence of labeled speech segments with timestamps:

```json
[
  {"speaker": "SPEAKER_0", "start": 0.2, "end": 3.4},
  {"speaker": "SPEAKER_1", "start": 3.5, "end": 6.8},
  {"speaker": "SPEAKER_0", "start": 7.0, "end": 12.1}
]
```

This can then be aligned with Whisper's word-level timestamps to build a **speaker-labeled transcript**.

## 7. Merging timelines & cleanup

Once transcripts are generated:

- **Stereo branch**: two channel transcripts are merged, preserving chronological word order.
- **Mono branch**: Pyannote maps speaker segments to words.

Both paths lead to a **final timeline** containing:

- Speaker attribution (SPEAKER_00 / SPEAKER_01 or diarized IDs)
- Word-level timestamps
- Segment durations

Finally, the pipeline outputs a clean **JSON schema**:

```json
{
  "segments": [...],
  "language": "en",
  "num_speakers": 2
}
```

This format is analytics-friendly and extensible for downstream tasks.

## Main results obtained with this customized pipeline

- **Channel-based diarization** -> perfect agent/customer attribution
- **Preprocessing** -> higher accuracy, fewer false pauses
- **Skipping Pyannote in stereo** -> 30-50% faster, cheaper
- **Modular architecture** -> easy to add layers:
  - Sentiment analysis
  - Summarization (per call / per segment)
  - Compliance checks (PII masking, sensitive terms)

## Scaling with Replicate

Deploying on **Replicate** unlocked massive scalability:

- Thousands of audios can be transcribed **in seconds**
- Instances are managed automatically
- Once the model is warm, executions are extremely fast
- A small cold-start delay happens when an instance needs to spin up

This makes the system production-ready without needing to manage GPUs directly.

## Comparison with other options

The author did not do a detailed comparison with other providers, but referenced a comparison article and included the new option (Whisper + Pyannote + Replicate).

The article concludes that combining open-source models with careful engineering choices produced:

- Faster transcription
- Lower costs
- More accurate diarization in real-world call center scenarios

The pipeline is described as modular, extensible, and ready for future layers of analytics like sentiment, intent classification, and compliance.

Project links:

- https://replicate.com/rafaelgalle/whisper-diarization-advanced
- https://github.com/rafaelgalle/whisper-diarization-advanced
