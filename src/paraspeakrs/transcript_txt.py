from __future__ import annotations

import json
import re
import time
from collections import deque
from collections.abc import Collection, Sequence
from dataclasses import dataclass

from .models import TranscriptionResult, TranscriptSegment, Word

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_SENTENCE_ENDERS = (".", "!", "?")


@dataclass
class Utterance:
    start: float
    end: float
    speaker: str
    resolved_label: str | None
    text: str

    @property
    def key(self) -> tuple[str, float]:
        """What identifies this line when it is removed from a transcript.

        Splitting is deterministic over a stored result, so the speaker and the
        start time find the same line again on every render.
        """
        return line_key(self.speaker, self.start)

    @property
    def name(self) -> str:
        return (self.resolved_label or self.speaker).strip() or self.speaker


def line_key(speaker: str, start: float) -> tuple[str, float]:
    return speaker, round(start, 3)


def utterances(
    result: TranscriptionResult,
    *,
    silence_p: float = 0.90,
    window: int = 20,
    min_silence: float = 0.6,
) -> list[Utterance]:
    """The transcript split into lines, empty ones dropped."""
    out: list[Utterance] = []
    prev_speaker: str | None = None
    gap_history: deque[float] = deque(maxlen=window)

    for segment in result.segments:
        if segment.speaker != prev_speaker:
            gap_history.clear()
        if segment.words:
            out.extend(_split_words(segment, gap_history, silence_p, min_silence))
        else:
            out.extend(_split_text(segment))
        prev_speaker = segment.speaker
    for utt in out:
        utt.text = utt.text.strip()
    return [utt for utt in out if utt.text]


def render_txt(
    result: TranscriptionResult,
    *,
    excluded_speakers: Collection[str] = (),
    excluded_lines: Collection[tuple[str, float]] = (),
) -> str:
    """One ``[hh:mm:ss] Name: text`` line per utterance, minus what was removed.

    Removal filters lines *after* splitting. Filtering segments first would
    change the silence history the splitter learns from, move the boundaries of
    the lines that remain, and orphan every other removed line's key.
    """
    speakers = set(excluded_speakers)
    lines_out = {line_key(s, t) for s, t in excluded_lines}
    lines = [
        format_line(utt)
        for utt in utterances(result)
        if utt.speaker not in speakers and utt.key not in lines_out
    ]
    return "\n".join(lines) + ("\n" if lines else "")


def format_line(utt: Utterance) -> str:
    return f"[{format_time(utt.start)}] {utt.name}: {utt.text}"


def render_frontmatter(
    note: str,
    *,
    title: str | None = None,
    date: str | None = None,
    speakers: Sequence[str] = (),
) -> str:
    """A YAML frontmatter block saying what the recording was, or "" when nothing is known.

    Title, date and speakers are quoted as JSON strings, which YAML reads as
    double-quoted scalars: a file name with a colon in it must not turn a
    transcript into invalid YAML.

    The note is always emitted as a block scalar, even when it is one line: a
    note is prose, and prose that happens to contain a colon or open with a
    quote would otherwise break the header the same way.
    """
    fields = []
    if title:
        fields.append(f"title: {json.dumps(title, ensure_ascii=False)}")
    if date:
        fields.append(f"date: {json.dumps(date)}")
    if speakers:
        fields.append(f"speakers: {json.dumps(list(speakers), ensure_ascii=False)}")
    note = note.strip()
    if note:
        body = "\n".join(f"  {line}".rstrip() for line in note.splitlines())
        fields.append(f"note: |\n{body}")
    if not fields:
        return ""
    return "---\n" + "\n".join(fields) + "\n---\n\n"


def format_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return time.strftime("%H:%M:%S", time.gmtime(seconds))


def _split_words(
    segment: TranscriptSegment,
    gap_history: deque[float],
    silence_p: float,
    min_silence: float,
) -> list[Utterance]:
    out: list[Utterance] = []
    phrase: list[Word] = []
    prev: Word | None = None
    min_samples = max(5, gap_history.maxlen // 4 if gap_history.maxlen else 5)

    for word in segment.words:
        split_here = False
        if prev is not None:
            gap = max(0.0, word.start - prev.end)
            sentence_end = prev.word.endswith(_SENTENCE_ENDERS)
            silence = False
            if len(gap_history) >= min_samples:
                threshold = max(min_silence, _percentile(sorted(gap_history), silence_p))
                silence = gap >= threshold
            split_here = sentence_end or silence
            gap_history.append(gap)

        if split_here and phrase:
            out.append(_phrase_to_utterance(phrase, segment))
            phrase = []
        phrase.append(word)
        prev = word

    if phrase:
        out.append(_phrase_to_utterance(phrase, segment))
    return out


def _phrase_to_utterance(words: list[Word], segment: TranscriptSegment) -> Utterance:
    text = " ".join(w.word for w in words)
    return Utterance(
        start=words[0].start,
        end=words[-1].end,
        speaker=segment.speaker,
        resolved_label=segment.resolved_label,
        text=text,
    )


def _split_text(segment: TranscriptSegment) -> list[Utterance]:
    text = segment.text.strip()
    if not text:
        return []
    fragments = [frag for frag in _SENTENCE_SPLIT.split(text) if frag.strip()]
    if not fragments:
        return []
    span = max(0.0, segment.end - segment.start)
    total_chars = sum(len(frag) for frag in fragments) or 1
    out: list[Utterance] = []
    cursor = 0
    for fragment in fragments:
        frag_chars = len(fragment)
        start = segment.start + span * (cursor / total_chars)
        cursor += frag_chars
        end = segment.start + span * (cursor / total_chars)
        out.append(
            Utterance(
                start=start,
                end=end,
                speaker=segment.speaker,
                resolved_label=segment.resolved_label,
                text=fragment,
            )
        )
    return out


def _percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    k = (len(sorted_values) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)
