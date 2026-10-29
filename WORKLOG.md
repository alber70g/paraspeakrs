# Work log

What each working session did, what was checked, what was not and what is left. Newest first.

## 2026-10-29 — Nemotron 3.5 Transcribe backend

Plan: `docs/plans/2026-10-29-nemotron-3.5-transcribe-backend.md`. Research:
`research/nemotron-3.5-transcribe-30b-serving-memory-and-speaker-chunking.md`. Branch
`feat/nemotron-3.5-transcribe`.

### Done

- `src/paraspeakrs/nemotron.py`: `NemotronTranscribeAsr` (an `AsrEngine`), request building for
  NeMo-Speech.cpp and vLLM, one response parser for both shapes, tag stripping, the
  `SpeakerTagMapper` that carries speakers between overlapping requests,
  `diarization_from_words`, and a placeholder diarizer for speaker mode.
- `--asr-backend nemotron` with `--nemotron-server/-base-url/-model/-max-request-seconds/-context-terms`
  and the matching `PARAKEET_NEMOTRON_*` variables; the chunk target becomes the request length.
- `--diar-backend nemotron` (speaker mode): the pipeline skips the diarizer, uses fixed 60 s
  overlapping requests, maps tags by overlap voting, derives diarization from the words and
  keeps the words' speakers (`merge_asr_speaker_words`). `validate_runtime` needs no speakrs
  binary there and rejects it with any other ASR backend, without ASR, or with requests of
  120 s or less.
- Speaker cache gets an empty `nemotron:none` namespace so the no-voice-print path cannot crash.
- README section, CHANGELOG `[Unreleased]`, judgment calls in `DECISIONS.md`.
  `agent_guide.md` lists no backends, so it is unchanged.

### Verified

- `uv run pytest -q`: 317 passed (267 before; 50 new in `tests/test_nemotron.py`). Covers the
  request bodies of both servers, parsing of both documented response shapes, tag stripping,
  text-only fallback, overlap mapping, diarization from words, config/CLI validation, the
  factory, and a three-request pipeline run in speaker mode with a fake ASR whose tags flip
  between requests.
- The NeMo-Speech.cpp field names were taken from its `docs/api.md` (chat completions: `model`,
  `timestamp_granularities`, `input_audio` with `data`/`format`; response `transcription.words`
  with `word`, `start`, `end`, `speaker` as `spk:N`).

### Not verified

- Nothing ran against a real model or server (no GPU here). Both servers' behaviour is known
  from their documentation only.
- vLLM's `ctc_timestamps.words[].speaker` format is taken from the model card; the parser accepts
  an integer, `spk:N` or anything ending in digits.
- Whether the servers' `words` carry punctuation is unknown. Segment text is built from the
  words, so `with_transcript_spelling` copies the transcript's punctuation and casing onto words
  that line up with it (difflib match on letters and digits). Words that don't line up keep the
  aligner's spelling.
- Dutch speaker tagging quality; the 60 s overlap size; Metal on a Mac.
- NeMo-Speech.cpp's routing of `/v1/audio/transcriptions` to the speech LLM (unused here).

### Left

- One real run per server type, with speaker mode on a recording longer than one request.
- If silent-through-the-overlap speakers prove common, add voice prints for Nemotron's turns
  (the speakrs sidecar is macOS-only).
- Version bump and release are not done.
