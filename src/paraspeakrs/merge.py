from __future__ import annotations

from collections import Counter

from .models import AsrSegment, DiarizationSegment, TranscriptSegment, Word

# Words separated by less than this much silence are treated as one utterance and
# share a speaker. Diarization boundaries sit at subsegment midpoints, not at word
# boundaries, so assigning per word lets an imprecise edge flip one or two words
# mid-sentence; a real turn change is preceded by an audible pause.
TURN_PAUSE_SECONDS = 0.15


def merge_asr_with_diarization(
    asr_segments: list[AsrSegment],
    diarization: list[DiarizationSegment],
) -> tuple[list[TranscriptSegment], list[str]]:
    warnings: list[str] = []
    merged: list[TranscriptSegment] = []
    for asr in asr_segments:
        if asr.words:
            merged.extend(_merge_words(asr.words, diarization))
        elif not asr.text.strip():
            # A silent chunk -- a lead-in before anyone speaks, say. It has no
            # text to attribute, and warning here would report a word-timestamp
            # failure that did not happen.
            continue
        else:
            speaker, confidence = dominant_speaker(asr.start, asr.end, diarization)
            merged.append(
                TranscriptSegment(
                    speaker=speaker,
                    speaker_confidence=confidence,
                    start=asr.start,
                    end=asr.end,
                    text=asr.text,
                    words=[],
                )
            )
            warnings.append("ASR word timestamps unavailable; assigned chunk text to dominant speaker")
    return merged, sorted(set(warnings))


def dominant_speaker(
    start: float,
    end: float,
    diarization: list[DiarizationSegment],
) -> tuple[str, float]:
    overlaps = _speaker_overlaps(start, end, diarization)
    if not overlaps:
        return "UNKNOWN", 0.0
    speaker, overlap = overlaps.most_common(1)[0]
    duration = max(0.001, end - start)
    return speaker, min(1.0, float(overlap) / duration)


def _speaker_overlaps(
    start: float,
    end: float,
    diarization: list[DiarizationSegment],
) -> Counter[str]:
    overlaps: Counter[str] = Counter()
    for segment in diarization:
        overlap = max(0.0, min(end, segment.end) - max(start, segment.start))
        if overlap > 0:
            overlaps[segment.speaker] += overlap
    return overlaps


def _utterances(words: list[Word]) -> list[list[Word]]:
    """Split a word stream wherever the silence between two words is audible."""
    groups: list[list[Word]] = []
    for word in words:
        if groups and word.start - groups[-1][-1].end < TURN_PAUSE_SECONDS:
            groups[-1].append(word)
        else:
            groups.append([word])
    return groups


def _merge_words(
    stream: list[Word],
    diarization: list[DiarizationSegment],
) -> list[TranscriptSegment]:
    words: list[Word] = []
    for utterance in _utterances(stream):
        overlaps: Counter[str] = Counter()
        for word in utterance:
            overlaps.update(_speaker_overlaps(word.start, word.end, diarization))
        speaker = overlaps.most_common(1)[0][0] if overlaps else "UNKNOWN"
        words.extend(word.model_copy(update={"speaker": speaker}) for word in utterance)

    segments: list[TranscriptSegment] = []
    current: list[Word] = []
    for word in words:
        if current and current[-1].speaker != word.speaker:
            segments.append(_words_to_segment(current))
            current = []
        current.append(word)
    if current:
        segments.append(_words_to_segment(current))
    return segments


def _words_to_segment(words: list[Word]) -> TranscriptSegment:
    text = " ".join(word.word for word in words).strip()
    speaker = words[0].speaker if words else "UNKNOWN"
    return TranscriptSegment(
        speaker=speaker,
        speaker_confidence=1.0 if speaker != "UNKNOWN" else 0.0,
        start=words[0].start,
        end=words[-1].end,
        text=text,
        words=words,
    )
