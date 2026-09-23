from pathlib import Path

from paraspeakrs.chunking import build_chunks
from paraspeakrs.models import AsrSegment, DiarizationSegment, Word
from paraspeakrs.pipeline import TranscriptionPipeline

DURATION = 200.0


class RecordingAudio:
    """FakeAudio that remembers the span of the chunk it was last asked to export."""

    def __init__(self) -> None:
        self.last_chunk = (0.0, 0.0)

    def materialize(self, work_dir, *, file_path=None, file_url=None, file_base64=None):
        return Path("raw.wav")

    def normalize_mono_16khz(self, input_path, output_path):
        return Path("normalized.wav")

    def normalize_16khz_preserving_channels(self, input_path, output_path):
        return Path("normalized-diar.wav")

    def duration_seconds(self, input_path):
        return DURATION

    def export_chunk(self, input_path, output_path, start, end):
        self.last_chunk = (start, end)
        return Path(f"chunk-{start}-{end}.wav")

    def export_chunk_preserving_channels(self, input_path, output_path, start, end):
        return Path("chunk-diar.wav")


class FakeDiarizer:
    name = "fake"

    def diarize(self, audio_path):
        return [
            DiarizationSegment(speaker="SPEAKER_00", start=float(s), end=float(s + 50))
            for s in (0, 50, 100, 150)
        ]


class SecondsAsr:
    """One word per second of the chunk, named for its absolute position.

    Word ``w48`` is the audio at t=48 s no matter which chunk transcribed it, so
    a word appearing twice in the output means the overlap was not deduplicated.
    """

    def __init__(self, audio: RecordingAudio) -> None:
        self.audio = audio

    def transcribe(self, audio_path):
        start, end = self.audio.last_chunk
        words = [
            Word(word=f"w{int(start + offset)}", start=float(offset), end=float(offset) + 0.5)
            for offset in range(int(end - start))
        ]
        return AsrSegment(
            text=" ".join(word.word for word in words),
            start=0.0,
            end=end - start,
            words=words,
        )


class DisagreeingAsr(SecondsAsr):
    """Like SecondsAsr, but garbles the first 2 s of every chunk after the first.

    Real behaviour: a chunk's opening seconds start mid-utterance with no
    preceding context, so the later chunk's take on an overlap is the worse one.
    """

    def transcribe(self, audio_path):
        segment = super().transcribe(audio_path)
        start, _ = self.audio.last_chunk
        if start == 0.0:
            return segment
        words = [
            word.model_copy(update={"word": f"garbled{word.word[1:]}"}) if word.start < 2.0 else word
            for word in segment.words
        ]
        return segment.model_copy(
            update={"words": words, "text": " ".join(word.word for word in words)}
        )


class FakeCache:
    def resolve_embeddings(self, embeddings):
        return {}

    def resolve(self, segments):
        return {}


def _pipeline(asr_class=SecondsAsr) -> TranscriptionPipeline:
    audio = RecordingAudio()
    return TranscriptionPipeline(
        diarizer=FakeDiarizer(),
        asr=asr_class(audio),
        audio=audio,
        speaker_cache=FakeCache(),
    )


def test_chunks_actually_overlap():
    """Guard the premise: without dedupe these spans double-transcribe audio."""
    chunks = build_chunks(FakeDiarizer().diarize(None), DURATION, 90.0, 2.0)

    assert len(chunks) > 1
    assert any(nxt.start < cur.end for cur, nxt in zip(chunks, chunks[1:]))


def test_overlapping_chunk_words_are_not_duplicated(tmp_path):
    result = _pipeline().run(work_dir=tmp_path, file_path=Path("input.wav"))

    spoken = [word.word for segment in result.segments for word in segment.words]

    assert len(spoken) == len(set(spoken)), "overlap region transcribed twice"
    assert spoken == sorted(spoken, key=lambda name: int(name[1:])), "words out of order"


def test_no_audio_is_dropped_at_chunk_boundaries(tmp_path):
    result = _pipeline().run(work_dir=tmp_path, file_path=Path("input.wav"))

    spoken = {word.word for segment in result.segments for word in segment.words}

    assert spoken == {f"w{second}" for second in range(int(DURATION))}


def test_overlap_keeps_the_earlier_chunks_rendering(tmp_path):
    """The two chunks disagree about the overlap; the earlier one wins.

    Deduplicating in the other direction drops the earlier chunk's words and
    trusts the later chunk to re-cover that audio -- which it does not do
    faithfully, so real words go missing.
    """
    result = _pipeline(DisagreeingAsr).run(work_dir=tmp_path, file_path=Path("input.wav"))

    spoken = [word.word for segment in result.segments for word in segment.words]

    assert not [word for word in spoken if word.startswith("garbled")]
    assert set(spoken) == {f"w{second}" for second in range(int(DURATION))}
