"""Nemotron 3.5 Transcribe as an ASR backend, and its speaker tags as a diarizer.

The model is a speech LLM served over HTTP by either vLLM or NeMo-Speech.cpp
(`nemo-speech serve`); both take a chat-completions request with the audio inline
and answer with the transcript plus word timestamps. Only the request differs:
NeMo-Speech.cpp rejects fields it does not know (`max_tokens`, `chat_template_kwargs`),
vLLM needs them. The two response shapes are parsed by one function that accepts either.

Everything that can be a pure function is one, so the request building, parsing, tag
mapping and run building are tested without a server.
"""

from __future__ import annotations

import base64
import difflib
import re
import wave
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .config import NEMOTRON_DEFAULT_BASE_URL, NemotronServer
from .models import AsrSegment, DiarizationSegment, Word

UNKNOWN_SPEAKER = "UNKNOWN"

# NeMo-Speech.cpp names the model "salm"; vLLM serves it under its Hugging Face id.
DEFAULT_MODELS: dict[str, str] = {
    "nemo-speech": "salm",
    "vllm": "nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B",
}

# The model card's prompts, with the wording clause set to its clean variant (fillers and
# accidental repetitions omitted). The system prompt stays empty.
CLEAN_PROMPT = (
    "Audio transcription. Language: as spoken. "
    "Wording: omit fillers and accidental repetitions; keep the remaining spoken wording unchanged. "
    "Formatting: punctuation and capitalization. Output: transcript only."
)
# The card's speaker prompt names four tags; the model supports up to eight speakers.
MAX_SPEAKER_TAGS = 8
SPEAKER_PROMPT = (
    "Transcribe this audio with punctuation and capitalization. "
    "Omit fillers and accidental repetitions; keep the remaining spoken wording unchanged. "
    "Write numbers in their standard written form. "
    "Put speaker tags " + " ".join(f"<spk:{n}>" for n in range(MAX_SPEAKER_TAGS)) + " on the transcription."
)

# vLLM generates until the model stops; an hour of dense speech is about 20k tokens.
VLLM_MAX_TOKENS = 32768

# Words of the same speaker closer together than this form one diarization segment.
SPEAKER_RUN_GAP_SECONDS = 2.0
# Two transcriptions of the same word, from consecutive requests, start within this long
# of each other.
OVERLAP_MATCH_SECONDS = 1.0

_TAG_PATTERN = re.compile(r"<spk:\d+>|<\|[^|>]*\|>|<think>\s*</think>")


def normalize_base_url(base_url: str) -> str:
    """The server root. A trailing `/v1` is dropped; the endpoint path adds it back."""
    root = base_url.strip().rstrip("/")
    root = root.removesuffix("/v1")
    return root.rstrip("/")


def build_prompt(*, speakers: bool, context_terms: str | None = None) -> str:
    prompt = SPEAKER_PROMPT if speakers else CLEAN_PROMPT
    terms = (context_terms or "").strip()
    if terms:
        prompt = f"{prompt} Context terms: {terms}."
    return prompt


def build_request(server: NemotronServer, model: str, prompt: str, audio_wav: bytes) -> dict[str, Any]:
    """The JSON body for POST {base}/v1/chat/completions."""
    audio = base64.b64encode(audio_wav).decode("ascii")
    text = {"type": "text", "text": prompt}
    if server == "vllm":
        return {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        text,
                        {"type": "audio_url", "audio_url": {"url": f"data:audio/wav;base64,{audio}"}},
                    ],
                }
            ],
            "temperature": 0,
            "max_tokens": VLLM_MAX_TOKENS,
            "chat_template_kwargs": {"enable_thinking": False},
            "skip_special_tokens": False,
            "spaces_between_special_tokens": False,
            "mm_processor_kwargs": {"capture_ctc_timestamps": True},
        }
    # NeMo-Speech.cpp: one user message holding a text part and one input_audio part.
    # Word timestamps (which carry the speakers) are opt-in; unknown fields are a 400.
    return {
        "model": model,
        "timestamp_granularities": ["word"],
        "messages": [
            {
                "role": "user",
                "content": [
                    text,
                    {"type": "input_audio", "input_audio": {"data": audio, "format": "wav"}},
                ],
            }
        ],
    }


def strip_tags(text: str) -> str:
    """Drop speaker tags and special tokens from transcript text; collapse whitespace."""
    return " ".join(_TAG_PATTERN.sub(" ", text).split())


def parse_response(payload: dict[str, Any], *, speakers: bool) -> tuple[str, list[Word]]:
    """Transcript text and timed words from either server's response.

    vLLM puts the words under `ctc_timestamps.words`, NeMo-Speech.cpp under
    `transcription.words`; whichever is present is used. Words are empty when the server
    could not align them (the transcript is still returned), so the caller falls back to
    text. Word speakers are kept as `spk:N` only when `speakers` is set.
    """
    text = ""
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            text = content
    transcription = payload.get("transcription")
    if not text.strip() and isinstance(transcription, dict) and isinstance(transcription.get("text"), str):
        text = transcription["text"]
    text = strip_tags(text)
    return text, with_transcript_spelling(_parse_words(_raw_words(payload), speakers=speakers), text)


def with_transcript_spelling(words: list[Word], text: str) -> list[Word]:
    """Give aligned words the transcript's punctuation and casing.

    The aligner may return bare words ("hello" for "Hello,"), and transcript lines are
    built by joining words, so without this the output loses its punctuation. Words that
    line up with a transcript token (same letters and digits) take that token's spelling;
    the rest stay as the aligner wrote them.
    """
    tokens = text.split()
    if not words or not tokens:
        return words
    matcher = difflib.SequenceMatcher(
        a=[_normalized(word.word) for word in words], b=[_normalized(token) for token in tokens], autojunk=False
    )
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            words[block.a + offset].word = tokens[block.b + offset]
    return words


def _raw_words(payload: dict[str, Any]) -> list[Any]:
    for key, field in (("transcription", "words"), ("ctc_timestamps", "words")):
        section = payload.get(key)
        words = section.get(field) if isinstance(section, dict) else None
        if isinstance(words, list) and words:
            return words
    return []


def _parse_words(raw_words: list[Any], *, speakers: bool) -> list[Word]:
    words: list[Word] = []
    for raw in raw_words:
        if not isinstance(raw, dict):
            continue
        text = strip_tags(str(raw.get("word") or ""))
        start, end = raw.get("start"), raw.get("end")
        if not text or not _is_number(start) or not _is_number(end):
            continue
        speaker = speaker_tag(raw.get("speaker")) if speakers else UNKNOWN_SPEAKER
        words.append(Word(word=text, start=float(start), end=max(float(start), float(end)), speaker=speaker))
    words.sort(key=lambda word: word.start)
    return words


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def speaker_tag(raw: object) -> str:
    """Canonical `spk:N` for a server's speaker value (`"spk:0"`, `0`, `"speaker_1"`)."""
    if isinstance(raw, bool):
        return UNKNOWN_SPEAKER
    if isinstance(raw, int):
        return f"spk:{raw}" if raw >= 0 else UNKNOWN_SPEAKER
    if isinstance(raw, str):
        match = re.search(r"(\d+)\s*$", raw)
        if match:
            return f"spk:{int(match.group(1))}"
    return UNKNOWN_SPEAKER


class NemotronTranscribeAsr:
    """`AsrEngine` over a local Nemotron server.

    With `speakers=True` the prompt asks for speaker tags and the returned words carry
    them as `spk:N` - per request, not yet comparable between requests (see
    :class:`SpeakerTagMapper`).
    """

    def __init__(
        self,
        base_url: str = NEMOTRON_DEFAULT_BASE_URL,
        *,
        server: NemotronServer = "nemo-speech",
        model: str | None = None,
        speakers: bool = False,
        context_terms: str | None = None,
        timeout: float | None = 3600.0,
        transport: Any = None,
    ) -> None:
        self.server = server
        self.base_url = normalize_base_url(base_url)
        self.model = model or DEFAULT_MODELS[server]
        self.speakers = speakers
        self.context_terms = context_terms
        self.timeout = timeout
        # httpx transport, for tests; None means a real connection.
        self.transport = transport

    @property
    def url(self) -> str:
        return f"{self.base_url}/v1/chat/completions"

    def transcribe(self, audio_path: Path) -> AsrSegment:
        import httpx

        body = build_request(
            self.server,
            self.model,
            build_prompt(speakers=self.speakers, context_terms=self.context_terms),
            Path(audio_path).read_bytes(),
        )
        # Decoding half an hour of audio takes minutes; only connecting is kept short so a
        # server that is not running fails at once.
        timeout = httpx.Timeout(self.timeout, connect=10.0)
        try:
            with httpx.Client(timeout=timeout, transport=self.transport) as client:
                response = client.post(self.url, json=body)
        except httpx.ConnectError as exc:
            raise RuntimeError(f"cannot reach the Nemotron server at {self.base_url}: {exc}") from exc
        if response.status_code >= 400:
            raise RuntimeError(f"Nemotron server returned {response.status_code}: {response.text[:500]}")
        text, words = parse_response(response.json(), speakers=self.speakers)
        end = _wav_seconds(audio_path)
        if end <= 0.0 and words:
            end = words[-1].end
        return AsrSegment(text=text, start=0.0, end=end, words=words)


class AsrSpeakerDiarizer:
    """Stands in for a diarizer when speakers come from the transcription.

    There is no separate diarization pass in that mode, so the stand-in only exists to
    fail clearly where a pass is asked for anyway (`label-dir`).
    """

    name = "Nemotron speaker tags"

    def diarize(self, audio_path: Path):
        raise RuntimeError(
            "--diar-backend nemotron reads speakers from the transcription and has no separate "
            "diarization pass; use speakrs or senko for this command"
        )


def _wav_seconds(audio_path: Path) -> float:
    try:
        with wave.open(str(audio_path), "rb") as handle:
            return handle.getnframes() / float(handle.getframerate())
    except (wave.Error, EOFError, ZeroDivisionError, OSError):
        return 0.0


def _normalized(word: str) -> str:
    return re.sub(r"\W+", "", word.lower())


def speaker_name(index: int) -> str:
    return f"SPEAKER_{index:02d}"


class SpeakerTagMapper:
    """Carries speaker identities from one request to the next.

    Tags (`spk:0`, `spk:1`, ...) are only meaningful within one request. Consecutive
    requests overlap, so the same words are transcribed twice, once under each request's
    tags. Each tag of the new request votes for the earlier speaker that said the same
    word at the same moment; the best-supported pairing wins, each earlier speaker is
    taken at most once, and a tag nobody voted for becomes a new speaker.

    A speaker who is silent throughout the overlap cannot be matched and shows up as a
    new speaker when they next talk; this is the cost of not using voice prints.
    """

    def __init__(self, tolerance_seconds: float = OVERLAP_MATCH_SECONDS) -> None:
        self.tolerance_seconds = tolerance_seconds
        self._previous: list[Word] = []
        self._speakers = 0

    def map_chunk(self, words: list[Word]) -> list[Word]:
        """`words` in whole-recording time with raw tags -> the same words with `SPEAKER_NN`."""
        votes = self._votes(words)
        assignment: dict[str, str] = {}
        taken: set[str] = set()
        ranked = sorted(
            ((count, tag, earlier) for tag, counts in votes.items() for earlier, count in counts.items()),
            key=lambda entry: (-entry[0], entry[1], entry[2]),
        )
        for _, tag, earlier in ranked:
            if tag not in assignment and earlier not in taken:
                assignment[tag] = earlier
                taken.add(earlier)
        for word in words:
            if word.speaker != UNKNOWN_SPEAKER and word.speaker not in assignment:
                assignment[word.speaker] = speaker_name(self._speakers)
                self._speakers += 1
        mapped = [word.model_copy(update={"speaker": assignment.get(word.speaker, UNKNOWN_SPEAKER)}) for word in words]
        if mapped:
            self._previous = mapped
        return mapped

    def _votes(self, words: list[Word]) -> dict[str, Counter[str]]:
        earlier_by_text: dict[str, list[Word]] = defaultdict(list)
        for word in self._previous:
            if word.speaker != UNKNOWN_SPEAKER:
                earlier_by_text[_normalized(word.word)].append(word)
        votes: dict[str, Counter[str]] = defaultdict(Counter)
        for word in words:
            if word.speaker == UNKNOWN_SPEAKER:
                continue
            candidates = [
                earlier
                for earlier in earlier_by_text.get(_normalized(word.word), ())
                if abs(earlier.start - word.start) <= self.tolerance_seconds
            ]
            if candidates:
                nearest = min(candidates, key=lambda earlier: abs(earlier.start - word.start))
                votes[word.speaker][nearest.speaker] += 1
        return votes


def diarization_from_words(segments: list[AsrSegment], max_gap_seconds: float = SPEAKER_RUN_GAP_SECONDS) -> list[DiarizationSegment]:
    """Runs of same-speaker words as diarization segments.

    A run ends when the speaker changes, a word has no speaker, or the speaker pauses for
    more than `max_gap_seconds`.
    """
    runs: list[DiarizationSegment] = []
    current: DiarizationSegment | None = None
    for segment in segments:
        for word in segment.words:
            if word.speaker == UNKNOWN_SPEAKER:
                if current is not None:
                    runs.append(current)
                    current = None
                continue
            if current is not None and current.speaker == word.speaker and word.start - current.end <= max_gap_seconds:
                current = current.model_copy(update={"end": max(current.end, word.end)})
                continue
            if current is not None:
                runs.append(current)
            current = DiarizationSegment(speaker=word.speaker, start=word.start, end=word.end)
    if current is not None:
        runs.append(current)
    return runs
