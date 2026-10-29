# Nemotron 3.5 Transcribe (30B-A4B): serving, memory and speaker chunking

Research date: 2026-10-29 (model released 2026-10-28). Gathered by research subagents from the
model card, the HF blog, NeMo-Speech.cpp docs, the AtomicChat GGUF card and FluidAudio source.
Unverified items are marked.

## What the model is

- HF: `nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B` (BF16), `-FP8`, `-NVFP4`, `-DFlash2` (speculative draft).
  License OpenMDW-1.1.
- Speech LLM: ~600M FastConformer encoder + Sortformer speaker encoder + Nemotron 3 hybrid
  Mamba-2/Transformer MoE (34B total, ~4.3B active). CTC head aligns words after decoding.
- Card: "an offline model... not a real-time streaming ASR service". Max 1 h audio, 65,536-token context.
- Not the same model as `nvidia/nemotron-3.5-asr-streaming-0.6b` (the small live model already in
  speech-to-translation as `nemotron-mlx` / `nemo-speech-cpp`).
- Languages: 40 for ASR; translation to/from English for 31; **speaker attribution for 13 languages,
  Dutch not among them** (multispeaker "—" for nl). Dutch ASR WER 3.75 / 5.96 on the card's tables.
  nl→en BLEU 29.74, en→nl 25.47 (FLEURS).
- Speakers: up to 8 (`max_num_of_spks: 8`). Tags are inline `<spk:N>`; "labels distinguish speakers
  within a recording". Nothing says tags are stable across separate requests: treat as per-request.

## Serving options

| Route | Where | Endpoint | Notes |
|---|---|---|---|
| vLLM 0.32 + NeMo Speech plugin (`VLLM_PLUGINS=nemo_speechlm`) | BF16: 80 GB (A100/H100); FP8: L40S 48 GB+; NVFP4: Blackwell only (RTX 5090, PRO 6000) | `POST /v1/chat/completions` | Official. Word timestamps need `--middleware ...ctc_timestamp_middleware`, `VLLM_USE_V2_MODEL_RUNNER=0`, `mm_processor_kwargs={"capture_ctc_timestamps": true}` |
| NeMo-Speech.cpp `nemo-speech serve --asr-model <decoder>.gguf` | CUDA (24 GB with IQ4_NL), Metal (untested by GGUF author) | `POST /v1/chat/completions` (also `/v1/audio/transcriptions`, routing to SALM unconfirmed) | GGUF: `AtomicChat/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B-GGUF` (decoder + required `...-Encoder-F16.gguf`, 1.67 GB). Official `nvidia/...-GGUF` repo exists but was not readable |
| HF Transformers (from source) | big GPU | in-process | `AutoModelForMultimodalLM`, `processor.apply_transcription_request` |
| MLX | — | — | No MLX port found as of 2026-10-29 |

GGUF decoder sizes: Q8_0 35.0 GB, AD-Q5_0_XL 29.3 GB, AD-IQ4_NL_L 22.6 GB ("32 GB GPUs and Macs"),
AD-IQ4_NL 19.7 GB ("24 GB GPUs, CPU, Metal, Vulkan"), AD-Q2_0_L 15.6 GB ("24 GB Macs"),
AD-Q2_0_M 13.6 GB ("16 GB GPUs").

### Request shape

vLLM (from the card):

```python
client.chat.completions.create(model="nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B",
  messages=[{"role":"user","content":[
    {"type":"text","text":"Transcribe the following audio."},
    {"type":"audio_url","audio_url":{"url":f"data:audio/wav;base64,{audio}"}}]}],
  temperature=0,
  extra_body={"chat_template_kwargs":{"enable_thinking":False},"skip_special_tokens":False,
              "mm_processor_kwargs":{"capture_ctc_timestamps":True}})
```

Response gains a top-level `ctc_timestamps` object: `words` (`word`, `start`, `end`, `speaker`),
`diarization`, `speaker_tag_to_diarization_speaker`, `error`.

NeMo-Speech.cpp (`docs/api.md`): chat completions with `"model": "salm"`, `language`,
`timestamp_granularities: ["word"]`, a text part plus an `input_audio` part (base64 + `format`).
The response adds a `transcription` object with words (speaker as `"spk:0"`), cues and diarization.
Exact field names must be checked against `docs/api.md` / `docs/salm/timestamps-and-speakers.md`
during implementation.

Prompts from the card (system prompt empty):

- Verbatim: `Audio transcription. Language: as spoken. Wording: verbatim, including fillers and repetitions. Formatting: punctuation and capitalization. Output: transcript only.`
- Clean: wording clause becomes `omit fillers and accidental repetitions; keep the remaining spoken wording unchanged`.
- Context terms: `Transcribe with punctuation and capitalization. Context terms: <terms>.`
- Speakers: `Transcribe this audio with punctuation and capitalization. Include all spoken words and verbal fillers. Write numbers in their standard written form. Put speaker tags <spk:0> <spk:1> <spk:2> <spk:3> on the transcription.`
- Translate: `Please translate the given speech to <target language>.`

## Memory per request (answers "does whole file need massive RAM?")

No. Per-request memory beyond the weights is under 1 GiB even for an hour.

- 12.5 audio tokens per second (80 ms each) plus ~5.5 output tokens/s (card: ~20k per dense hour).
- Only 6 of 52 LLM layers are attention (2 KV heads, head_dim 128) → ~6 KiB KV per token.
  Mamba state is constant (~48 MiB per slot).
- Encoder runs in 30 s chunks by design, so encoder activations do not grow with file length.

| Audio | Context tokens | KV cache | Encoder output held on GPU |
|---|---|---|---|
| 5 min | ~5.4k | ~33 MiB | ~40 MB |
| 15 min | ~16k | ~100 MiB | ~120 MB |
| 30 min | ~32.5k | ~200 MiB | ~240 MB |
| 60 min | ~65k | ~400 MiB | ~480 MB |

(Estimates from the config; NeMo-Speech.cpp docs confirm "about 0.5 GiB of KV cache" at the 73,728 max context.)

The limit is fixed overhead (CUDA context, compute buffers, slot count), not audio length:

- **24 GB NVIDIA, AD-IQ4_NL:** NVIDIA's validated 24 GB row is `max_sequences 4`,
  `n_ctx_per_sequence 8192`, `mtp_draft 0`, `n_batch 1024`, `encoder_max_batch 4`, longest request
  **29 min** (`max_context: 32768`). 60 min may fit with `max_sequences 1–2` and `max_context 73728`,
  but this is untested.
- **Mac 32 GB:** IQ4_NL with `max_sequences 1`, about 30 min safe (Metal GPU memory cap). 64 GB: Q8_0, full hour.
- CPU/Vulkan/Metal reserve prompt + full transcript budget up front; CUDA grows cells as it decodes.

## How FluidAudio handles long audio

FluidAudio (`FluidInference/FluidAudio`, cloned at e152879) does **not** size chunks by device memory.
`Documentation/ASR/LongTranscription.md`: chunk numbers "are not configurable at runtime… the same on every device".

- Parakeet ASR: fixed 15 s window, 2 s overlap, silence-aligned chunk starts, merge by token-sequence
  match in the overlap (then LCS, then midpoint split).
- Offline diarization (VBx): 10 s windows, 2 s step; 256-d WeSpeaker embeddings for every window, AHC
  (threshold 0.6) then VBx clustering over **all** embeddings → speaker identity is consistent for the
  whole file. Streaming `SpeakerManager`: cosine threshold 0.65 against a speaker database.
- It also has a "Nemotron 3.5 Transcribe Lite" path: the 600M encoder + CTC head only (no LLM),
  48 s windows, plus Nemotron-3-Diarization for 8 speakers. Fast (49.5 min in ~5.5 s on M5 Pro), much
  less accurate than the full model.

## Conclusions for our integration

1. Both repos talk to a local HTTP server (vLLM or NeMo-Speech.cpp); no Python library runs this
   model on a 24 GB GPU or Mac. One client supports both response shapes.
2. Request length is a setting, not a hardware probe: default **25 min** (under NVIDIA's validated
   29 min for 24 GB), raise to 60 min on 32 GB+ GPUs or 64 GB Macs.
3. Speaker tags are per request. Files longer than the max request length need cross-chunk speaker
   matching. Cheapest option: overlap consecutive requests (e.g. 60 s) and map tags by majority vote
   over words in the overlap (the same words get transcribed twice).
4. Dutch speaker tagging is not an advertised capability. Test it before relying on it.

## Sources

- https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B
- https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B-NVFP4
- https://huggingface.co/blog/nvidia/nemotron-transcribe-granary
- https://research.nvidia.com/labs/nemotron/NVIDIA-Nemotron-3.5-Transcribe/
- https://huggingface.co/AtomicChat/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B-GGUF
- https://github.com/NVIDIA/NeMo-Speech.cpp (README, docs/api.md, docs/server.md, docs/cli.md,
  docs/salm/configuration.md, docs/salm/timestamps-and-speakers.md)
- https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16/raw/main/config.json
- https://github.com/FluidInference/FluidAudio (`Documentation/ASR/LongTranscription.md`,
  `ChunkProcessor.swift`, `OfflineDiarizerTypes.swift`, `Documentation/ASR/NemotronTranscribeLite.md`)
- https://siliconangle.com/2026/10/28/nvidia-releases-all-in-one-adaptable-nemotron-3-5-audio-transcription-and-translation-model/
