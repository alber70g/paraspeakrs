import json
from pathlib import Path

import pytest

from paraspeakrs.cache import LEGACY_NAMESPACE, SpeakerCache
from paraspeakrs.labeling import LabelingWorkspace
from paraspeakrs.mcp_store import ArtifactStore
from paraspeakrs.models import AsrSegment, DiarizationSegment, Word
from paraspeakrs.pipeline import TranscriptionPipeline


class FakeAudio:
    """Writes real (tiny) files, so tests can see what a job leaves on disk."""

    @staticmethod
    def _write(output_path, payload=b"audio"):
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def materialize(self, work_dir, *, file_path=None, file_url=None, file_base64=None):
        return self._write(Path(work_dir) / "raw.wav")

    def normalize_mono_16khz(self, input_path, output_path):
        return self._write(output_path)

    def normalize_16khz_preserving_channels(self, input_path, output_path):
        return self._write(output_path)

    def duration_seconds(self, input_path):
        return 5.0

    def channel_count(self, input_path):
        return 1

    def export_chunk(self, input_path, output_path, start, end):
        return self._write(output_path)

    def export_chunk_preserving_channels(self, input_path, output_path, start, end):
        return self._write(output_path)


class FakeDiarizer:
    name = "fake"

    def diarize(self, audio_path):
        return [DiarizationSegment(speaker="SPEAKER_00", start=0.0, end=5.0)]


class FakeAsr:
    def transcribe(self, audio_path):
        return AsrSegment(text="hello there", start=0.0, end=5.0)


class FakeEmbeddings:
    def extract(self, audio_path, diarization):
        return {"SPEAKER_00": [1.0, 0.0, 0.0]}


def _workspace(tmp_path: Path) -> LabelingWorkspace:
    pipeline = TranscriptionPipeline(
        diarizer=FakeDiarizer(),
        asr=FakeAsr(),
        audio=FakeAudio(),
        speaker_cache=SpeakerCache(tmp_path / "speaker-cache.json"),
        embedding_extractor=FakeEmbeddings(),
    )
    return LabelingWorkspace(pipeline, ArtifactStore(tmp_path / "jobs"))


def _body(transcript: str) -> str:
    """The spoken lines of a transcript, without its frontmatter header."""
    if transcript.startswith("---\n"):
        return transcript.split("\n---\n\n", 1)[1]
    return transcript


def _audio_file(tmp_path: Path) -> Path:
    path = tmp_path / "meeting.m4a"
    path.write_bytes(b"")
    return path


def test_transcribe_returns_summary_and_persists_artifacts(tmp_path):
    workspace = _workspace(tmp_path)
    source = _audio_file(tmp_path)

    summary = workspace.transcribe(source)

    assert summary.source_path == source
    assert summary.num_speakers == 1
    assert [row.speaker for row in summary.speakers] == ["SPEAKER_00"]
    assert summary.speakers[0].assigned_label is None
    assert (tmp_path / "jobs" / summary.job_id / "artifacts.json").is_file()


def test_label_speaker_updates_rows_and_speaker_cache(tmp_path):
    workspace = _workspace(tmp_path)
    summary = workspace.transcribe(_audio_file(tmp_path))

    rows = workspace.label_speaker(summary.job_id, "SPEAKER_00", "Alice")

    assert rows[0].assigned_label == "Alice"
    cache = json.loads((tmp_path / "speaker-cache.json").read_text())
    speakers = cache["speakers_by_source"][LEGACY_NAMESPACE]
    assert "Alice" in speakers
    assert len(speakers["Alice"]["embedding"]) == 3


def test_write_transcript_and_preview_use_resolved_names(tmp_path):
    workspace = _workspace(tmp_path)
    summary = workspace.transcribe(_audio_file(tmp_path))
    workspace.label_speaker(summary.job_id, "SPEAKER_00", "Alice")

    out_path, lines = workspace.write_transcript(summary.job_id, tmp_path / "t.txt")

    assert out_path.is_file()
    assert lines == 1
    text = out_path.read_text()
    assert text == workspace.transcript_text(summary.job_id)
    assert _body(text).startswith("[00:00:00] Alice: hello there")
    # The header says which meeting this was, so a transcript read weeks later
    # out of its folder still names the recording and who was in it.
    assert text.startswith('---\ntitle: "meeting"\ndate: "')
    assert 'speakers: ["Alice"]' in text


def test_list_jobs_reads_from_disk(tmp_path):
    workspace = _workspace(tmp_path)
    source = _audio_file(tmp_path)
    job_id = workspace.transcribe(source).job_id

    reopened = LabelingWorkspace(workspace.pipeline, ArtifactStore(tmp_path / "jobs"))
    jobs = reopened.list_jobs()

    assert [job.job_id for job in jobs] == [job_id]
    assert jobs[0].source_path == source


def test_mcp_transcribe_response_shape(tmp_path):
    pytest.importorskip("mcp")
    from paraspeakrs.mcp_server import _transcribe_response

    workspace = _workspace(tmp_path)
    summary = workspace.transcribe(_audio_file(tmp_path))

    response = _transcribe_response(summary)

    assert set(response) == {
        "job_id",
        "duration_seconds",
        "num_speakers",
        "unlabeled_speakers",
        "speakers",
    }
    assert response["unlabeled_speakers"] == ["SPEAKER_00"]
    assert set(response["speakers"][0]) == {
        "speaker",
        "assigned_label",
        "suggested_label",
        "suggestion_score",
        "talk_seconds",
        "excluded",
    }


class CountingDiarizer(FakeDiarizer):
    """Diarizer that records how many times it actually ran."""

    def __init__(self) -> None:
        self.calls = 0

    def diarize(self, audio_path):
        self.calls += 1
        return super().diarize(audio_path)


def _counting_workspace(tmp_path: Path, diarizer: CountingDiarizer) -> LabelingWorkspace:
    pipeline = TranscriptionPipeline(
        diarizer=diarizer,
        asr=FakeAsr(),
        audio=FakeAudio(),
        speaker_cache=SpeakerCache(tmp_path / "speaker-cache.json"),
        embedding_extractor=FakeEmbeddings(),
    )
    return LabelingWorkspace(pipeline, ArtifactStore(tmp_path / "jobs"))


def test_identical_audio_reuses_the_existing_job(tmp_path: Path) -> None:
    """Re-transcribing the same recording must not redo ASR and diarization.

    A meeting costs minutes to process and yields the same segments every time,
    so the expensive work is what must be skipped - not merely the job record.
    """
    audio = tmp_path / "meeting.m4a"
    audio.write_bytes(b"pretend audio")
    diarizer = CountingDiarizer()
    workspace = _counting_workspace(tmp_path, diarizer)

    first = workspace.transcribe(audio)
    second = workspace.transcribe(audio)

    assert diarizer.calls == 1, "the pipeline ran again for identical audio"
    assert second.job_id == first.job_id
    assert second.reused is True
    assert first.reused is False
    assert len(workspace.list_jobs()) == 1


def test_reuse_matches_on_content_not_filename(tmp_path: Path) -> None:
    """A recording that was renamed or moved is still the same recording."""
    original = tmp_path / "meeting.m4a"
    original.write_bytes(b"pretend audio")
    diarizer = CountingDiarizer()
    workspace = _counting_workspace(tmp_path, diarizer)
    workspace.transcribe(original)

    renamed = tmp_path / "meeting-copy.m4a"
    renamed.write_bytes(b"pretend audio")
    assert workspace.transcribe(renamed).reused is True

    changed = tmp_path / "other.m4a"
    changed.write_bytes(b"different audio")
    assert workspace.transcribe(changed).reused is False
    assert diarizer.calls == 2


def test_force_re_transcribes_identical_audio(tmp_path: Path) -> None:
    audio = tmp_path / "meeting.m4a"
    audio.write_bytes(b"pretend audio")
    diarizer = CountingDiarizer()
    workspace = _counting_workspace(tmp_path, diarizer)

    first = workspace.transcribe(audio)
    second = workspace.transcribe(audio, force=True)

    assert diarizer.calls == 2
    assert second.job_id != first.job_id
    assert len(workspace.list_jobs()) == 2


def test_finished_job_keeps_only_the_transcript(tmp_path: Path) -> None:
    """Working audio is ~99% of a job and is rebuildable; the transcript is not.

    At several meetings a day the copies are what fill the disk, so a finished
    job must not hold on to them.
    """
    audio = tmp_path / "meeting.m4a"
    audio.write_bytes(b"pretend audio")
    workspace = _workspace(tmp_path)
    summary = workspace.transcribe(audio)

    job_dir = workspace.store.work_dir(summary.job_id)
    remaining = {child.name for child in job_dir.iterdir() if child.is_file()}
    assert remaining == {"artifacts.json"}
    # The original recording is untouched - only the copy inside the job went.
    assert audio.is_file()
    # And the transcript is still readable afterwards.
    assert "hello there" in workspace.transcript_text(summary.job_id)


def test_pruned_job_rebuilds_a_sample_without_keeping_the_audio(tmp_path: Path) -> None:
    audio = tmp_path / "meeting.m4a"
    audio.write_bytes(b"pretend audio")
    workspace = _workspace(tmp_path)
    summary = workspace.transcribe(audio)

    sample = workspace.speaker_sample(summary.job_id, "SPEAKER_00")
    assert sample.name.startswith("SPEAKER_00-")

    job_dir = workspace.store.work_dir(summary.job_id)
    assert not (job_dir / "playback.wav").exists(), "rebuilt audio was left behind"
    assert {child.name for child in job_dir.iterdir() if child.is_file()} == {"artifacts.json"}


def test_sample_fails_clearly_when_the_original_recording_is_gone(tmp_path: Path) -> None:
    audio = tmp_path / "meeting.m4a"
    audio.write_bytes(b"pretend audio")
    workspace = _workspace(tmp_path)
    summary = workspace.transcribe(audio)
    audio.unlink()

    with pytest.raises(ValueError, match="no longer at"):
        workspace.speaker_sample(summary.job_id, "SPEAKER_00")


class ThreeSpeakerDiarizer:
    name = "fake"

    def diarize(self, audio_path):
        return [
            DiarizationSegment(speaker="SPEAKER_00", start=0.0, end=2.0),
            DiarizationSegment(speaker="SPEAKER_01", start=2.0, end=3.5),
            DiarizationSegment(speaker="SPEAKER_02", start=3.5, end=5.0),
        ]


class ThreeSpeakerEmbeddings:
    """Raw (non-unit) embeddings, like a real extractor emits - norm ~4.5.

    SPEAKER_01 and SPEAKER_02 point in near-opposite directions (cosine -0.2), so
    merging them under one name lands the centroid roughly between them: cosine
    ~0.63 to either, below the cache's 0.75 threshold.
    """

    def extract(self, audio_path, diarization):
        return {
            "SPEAKER_00": [0.0, 0.0, 4.5],
            "SPEAKER_01": [4.5, 0.0, 0.0],
            "SPEAKER_02": [-0.9, 4.41, 0.0],
        }


def _three_speaker_workspace(tmp_path: Path, cache_name: str = "speaker-cache.json") -> LabelingWorkspace:
    pipeline = TranscriptionPipeline(
        diarizer=ThreeSpeakerDiarizer(),
        asr=FakeAsr(),
        audio=FakeAudio(),
        speaker_cache=SpeakerCache(tmp_path / cache_name),
        embedding_extractor=ThreeSpeakerEmbeddings(),
    )
    return LabelingWorkspace(pipeline, ArtifactStore(tmp_path / "jobs"))


def _label_of(rows, speaker: str) -> str | None:
    return next(row.assigned_label for row in rows if row.speaker == speaker)


def _row_of(rows, speaker: str):
    return next(row for row in rows if row.speaker == speaker)


def test_naming_two_speakers_the_same_keeps_both_names(tmp_path: Path) -> None:
    """Two people in one meeting may share a first name, and one person can also be
    diarized as two speakers (left and right channel). Naming the second one must
    not wipe the name off the first: the user typed it, and watching it vanish from
    the table makes the labeling step untrustworthy.
    """
    workspace = _three_speaker_workspace(tmp_path)
    summary = workspace.transcribe(_audio_file(tmp_path))

    workspace.label_speaker(summary.job_id, "SPEAKER_00", "Albert")
    workspace.label_speaker(summary.job_id, "SPEAKER_01", "Michiel")
    rows = workspace.label_speaker(summary.job_id, "SPEAKER_02", "Michiel")

    artifacts = workspace.store.load(summary.job_id)
    guessed = workspace.pipeline.speaker_cache.resolve_embeddings(artifacts.speaker_embeddings)
    assert guessed["SPEAKER_01"] is None, "the reported regression no longer reproduces"

    assert _label_of(rows, "SPEAKER_01") == "Michiel"
    assert _label_of(rows, "SPEAKER_02") == "Michiel"
    assert _label_of(rows, "SPEAKER_00") == "Albert"


def test_assigned_name_survives_reopening_the_job(tmp_path: Path) -> None:
    """Labeling happens in one sitting, transcript export often in another. A name
    only held in memory would be gone by then, so it has to be on disk with the job -
    and it belongs to the job, not to the voice cache, which is a rebuildable pile of
    guesses the user may well have cleared in between.
    """
    workspace = _three_speaker_workspace(tmp_path)
    summary = workspace.transcribe(_audio_file(tmp_path))
    workspace.label_speaker(summary.job_id, "SPEAKER_00", "Michiel")

    reopened = _three_speaker_workspace(tmp_path, cache_name="emptied-cache.json")
    rows = reopened.speaker_rows(summary.job_id)

    assert _label_of(rows, "SPEAKER_00") == "Michiel"
    assert _body(reopened.transcript_text(summary.job_id)).startswith("[00:00:00] Michiel:")


def test_assigned_name_outranks_the_embedding_cache_guess(tmp_path: Path) -> None:
    """The cache recognizes voices across jobs, but it only ever guesses.

    A guess stays in the suggestion column and never reaches the transcript on
    its own; once the user says who this is, their answer is what is shown and
    what is written out.
    """
    workspace = _three_speaker_workspace(tmp_path)
    # A voice the cache already knows by another name.
    workspace.pipeline.speaker_cache.record_embeddings({"SPEAKER_00": "Bianca"}, {"SPEAKER_00": [0.0, 0.0, 4.5]})
    summary = workspace.transcribe(_audio_file(tmp_path))
    guessed = _row_of(workspace.speaker_rows(summary.job_id), "SPEAKER_00")
    assert (guessed.assigned_label, guessed.suggested_label) == (None, "Bianca")

    rows = workspace.label_speaker(summary.job_id, "SPEAKER_00", "Michiel")

    assert _label_of(rows, "SPEAKER_00") == "Michiel"
    assert _body(workspace.transcript_text(summary.job_id)).startswith("[00:00:00] Michiel:")


class NoCentroidEmbeddings:
    """A diarizer pass that produced segments but no centroids.

    Real backends do this: the speakrs sidecar omits ``centroids`` when it cannot
    compute them, and ``NullSpeakerEmbeddingExtractor`` returns nothing at all.
    """

    def extract(self, audio_path, diarization):
        return {}


def _no_centroid_workspace(tmp_path: Path) -> LabelingWorkspace:
    pipeline = TranscriptionPipeline(
        diarizer=ThreeSpeakerDiarizer(),
        asr=FakeAsr(),
        audio=FakeAudio(),
        speaker_cache=SpeakerCache(tmp_path / "speaker-cache.json"),
        embedding_extractor=NoCentroidEmbeddings(),
    )
    return LabelingWorkspace(pipeline, ArtifactStore(tmp_path / "jobs"))


def test_speakers_without_embeddings_are_still_nameable(tmp_path: Path) -> None:
    """Every speaker the table offers must accept a name.

    Naming is about this job, and the job knows its speakers from diarization -
    the embedding only teaches the cross-job voice cache. When the diarizer
    returns no centroids, the speakers are still listed, and refusing to name
    them leaves the user staring at rows that reject every name they type.
    """
    workspace = _no_centroid_workspace(tmp_path)
    summary = workspace.transcribe(_audio_file(tmp_path))
    assert [row.speaker for row in summary.speakers] == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"]

    rows = workspace.label_speaker(summary.job_id, "SPEAKER_00", "Michiel")

    assert _label_of(rows, "SPEAKER_00") == "Michiel"
    assert _label_of(workspace.speaker_rows(summary.job_id), "SPEAKER_00") == "Michiel"
    assert _body(workspace.transcript_text(summary.job_id)).startswith("[00:00:00] Michiel:")


def test_naming_a_speaker_the_job_does_not_have_says_which_ones_it_has(tmp_path: Path) -> None:
    """A typo'd speaker ID is a caller bug (the MCP tool takes one as a string),
    so the error has to name the IDs that would have worked."""
    workspace = _no_centroid_workspace(tmp_path)
    summary = workspace.transcribe(_audio_file(tmp_path))

    with pytest.raises(ValueError, match=r"SPEAKER_00, SPEAKER_01, SPEAKER_02"):
        workspace.label_speaker(summary.job_id, "L_SPEAKER_00", "Michiel")


def test_an_unaccepted_suggestion_never_reaches_the_transcript(tmp_path: Path) -> None:
    """A transcript states who said what, so a name in one has to be a decision.

    The cache matches on cosine similarity and is wrong often enough that
    printing its guess as fact quietly puts words in someone's mouth. Until the
    guess is accepted, the speaker keeps their raw diarization ID.
    """
    workspace = _three_speaker_workspace(tmp_path)
    workspace.pipeline.speaker_cache.record_embeddings({"SPEAKER_00": "Bianca"}, {"SPEAKER_00": [0.0, 0.0, 4.5]})
    summary = workspace.transcribe(_audio_file(tmp_path))

    assert _row_of(summary.speakers, "SPEAKER_00").suggested_label == "Bianca"
    assert _body(workspace.transcript_text(summary.job_id)).startswith("[00:00:00] SPEAKER_00:")


def test_accepting_a_suggestion_assigns_it(tmp_path: Path) -> None:
    workspace = _three_speaker_workspace(tmp_path)
    workspace.pipeline.speaker_cache.record_embeddings({"SPEAKER_00": "Bianca"}, {"SPEAKER_00": [0.0, 0.0, 4.5]})
    summary = workspace.transcribe(_audio_file(tmp_path))

    rows = workspace.accept_suggestion(summary.job_id, "SPEAKER_00")

    assert _label_of(rows, "SPEAKER_00") == "Bianca"
    assert _body(workspace.transcript_text(summary.job_id)).startswith("[00:00:00] Bianca:")


def test_accepting_a_speaker_with_no_suggestion_says_so(tmp_path: Path) -> None:
    """Accept is a single keystroke on whatever row the cursor is on; silently
    doing nothing on an unmatched speaker looks like the key is broken."""
    workspace = _three_speaker_workspace(tmp_path)
    summary = workspace.transcribe(_audio_file(tmp_path))

    with pytest.raises(ValueError, match="no suggestion"):
        workspace.accept_suggestion(summary.job_id, "SPEAKER_00")


def test_accept_all_leaves_names_already_typed_alone(tmp_path: Path) -> None:
    """Accept-all is a bulk convenience, not an override. A name someone typed
    outranks every guess, including on a second press after more labeling."""
    workspace = _three_speaker_workspace(tmp_path)
    workspace.pipeline.speaker_cache.record_embeddings({"SPEAKER_00": "Bianca"}, {"SPEAKER_00": [0.0, 0.0, 4.5]})
    summary = workspace.transcribe(_audio_file(tmp_path))
    workspace.label_speaker(summary.job_id, "SPEAKER_00", "Michiel", teach=False)

    rows = workspace.accept_all_suggestions(summary.job_id)

    assert _label_of(rows, "SPEAKER_00") == "Michiel"


def test_merging_speakers_names_them_together_and_folds_one_voice(tmp_path: Path) -> None:
    """A stereo recording splits one person across channels, so the same voice
    arrives as two diarized speakers. Merging has to name both and leave the
    cache holding one voice learned from both, not two rivals."""
    workspace = _three_speaker_workspace(tmp_path)
    summary = workspace.transcribe(_audio_file(tmp_path))

    rows = workspace.merge_speakers(summary.job_id, ["SPEAKER_01", "SPEAKER_02"], "Michiel")

    assert _label_of(rows, "SPEAKER_01") == "Michiel"
    assert _label_of(rows, "SPEAKER_02") == "Michiel"
    assert workspace.pipeline.speaker_cache.names() == ["Michiel"]
    assert len(workspace.pipeline.speaker_cache.members("Michiel")) == 2


def test_unassigning_takes_the_name_out_of_the_voice_too(tmp_path: Path) -> None:
    """A name taken off a speaker must stop pulling on the voice it taught, or a
    misfiled label keeps skewing every later match from behind the scenes."""
    workspace = _three_speaker_workspace(tmp_path)
    summary = workspace.transcribe(_audio_file(tmp_path))
    workspace.label_speaker(summary.job_id, "SPEAKER_00", "Michiel")

    rows = workspace.unassign_speaker(summary.job_id, "SPEAKER_00")

    assert _label_of(rows, "SPEAKER_00") is None
    assert workspace.pipeline.speaker_cache.names() == []


def test_keeping_a_name_separate_labels_the_job_without_teaching_the_cache(tmp_path: Path) -> None:
    """Two different people can share a first name. Folding their voices into one
    centroid matches neither afterwards, so naming has to be possible without it."""
    workspace = _three_speaker_workspace(tmp_path)
    summary = workspace.transcribe(_audio_file(tmp_path))
    workspace.label_speaker(summary.job_id, "SPEAKER_00", "Michiel")

    workspace.label_speaker(summary.job_id, "SPEAKER_02", "Michiel", teach=False)

    assert len(workspace.pipeline.speaker_cache.members("Michiel")) == 1
    assert _label_of(workspace.speaker_rows(summary.job_id), "SPEAKER_02") == "Michiel"


def test_fold_similarity_warns_before_a_distant_voice_joins_a_known_name(tmp_path: Path) -> None:
    """Typing a name that already exists is usually the same person and sometimes
    a mistake. The score is what lets the UI ask instead of silently averaging a
    stranger into someone's voice."""
    workspace = _three_speaker_workspace(tmp_path)
    summary = workspace.transcribe(_audio_file(tmp_path))
    workspace.label_speaker(summary.job_id, "SPEAKER_01", "Michiel")

    close = workspace.fold_similarity(summary.job_id, "SPEAKER_01", "Michiel")
    distant = workspace.fold_similarity(summary.job_id, "SPEAKER_02", "Michiel")

    assert close == pytest.approx(1.0)
    assert distant is not None and distant < 0.0
    assert workspace.fold_similarity(summary.job_id, "SPEAKER_01", "Someone New") is None


def test_voices_list_the_recordings_they_were_learned_from(tmp_path: Path) -> None:
    """Deciding whether two names are the same person means listening to them, so
    a stored voice has to remember which job and speaker it came from."""
    workspace = _three_speaker_workspace(tmp_path)
    summary = workspace.transcribe(_audio_file(tmp_path))
    workspace.label_speaker(summary.job_id, "SPEAKER_00", "Michiel")

    voices = workspace.voices()

    assert [voice.name for voice in voices] == ["Michiel"]
    member = voices[0].members[0]
    assert (member.job_id, member.speaker_id) == (summary.job_id, "SPEAKER_00")
    assert member.source == "meeting.m4a"
    assert member.playable is True


def test_merging_two_voices_renames_the_jobs_that_used_the_old_name(tmp_path: Path) -> None:
    """Merging "michiel" into "Michiel" that leaves old jobs saying "michiel"
    has not merged anything - the duplicate is still on every transcript."""
    workspace = _three_speaker_workspace(tmp_path)
    summary = workspace.transcribe(_audio_file(tmp_path))
    workspace.label_speaker(summary.job_id, "SPEAKER_00", "Albert")
    workspace.label_speaker(summary.job_id, "SPEAKER_01", "albert")

    workspace.merge_voices("albert", "Albert")

    rows = workspace.speaker_rows(summary.job_id)
    assert _label_of(rows, "SPEAKER_01") == "Albert"
    assert workspace.pipeline.speaker_cache.names() == ["Albert"]


def test_unfolding_a_voice_leaves_that_speaker_unnamed(tmp_path: Path) -> None:
    """Unfold is how a wrong merge is undone, so the speaker it pulls out has to
    go back to being a candidate - still listed, named by nobody, open to a
    fresh suggestion."""
    workspace = _three_speaker_workspace(tmp_path)
    summary = workspace.transcribe(_audio_file(tmp_path))
    workspace.merge_speakers(summary.job_id, ["SPEAKER_01", "SPEAKER_02"], "Michiel")

    workspace.unfold_voice("Michiel", summary.job_id, "SPEAKER_02")

    rows = workspace.speaker_rows(summary.job_id)
    assert _label_of(rows, "SPEAKER_01") == "Michiel"
    assert _label_of(rows, "SPEAKER_02") is None
    assert [m.speaker_id for m in workspace.pipeline.speaker_cache.members("Michiel")] == ["SPEAKER_01"]


def test_a_meeting_note_round_trips_through_the_workspace(tmp_path):
    """The note answers the one question the transcript cannot: why this
    recording mattered. It has to still be there next week."""
    workspace = _workspace(tmp_path)
    summary = workspace.transcribe(_audio_file(tmp_path))

    assert workspace.job_note(summary.job_id) == ""
    workspace.set_job_note(summary.job_id, "  sprint planning  ")

    assert workspace.job_note(summary.job_id) == "sprint planning"
    assert workspace.job_summary(summary.job_id).note == "sprint planning"
    assert workspace.list_jobs()[0].note == "sprint planning"


def test_a_note_cannot_be_written_against_a_job_that_does_not_exist(tmp_path):
    """Silently creating a note.json under an invented id would leave a note
    nothing ever shows, and a job dir list_jobs skips forever."""
    workspace = _workspace(tmp_path)
    with pytest.raises(ValueError):
        workspace.set_job_note("no-such-job", "hello")


class WordAsr:
    """ASR with word timings, so each diarized speaker gets their own lines."""

    def transcribe(self, audio_path):
        words = [
            Word(word="Morning.", start=0.2, end=0.8),
            Word(word="Mama!", start=2.2, end=2.6),
            Word(word="Juice.", start=2.9, end=3.3),
            Word(word="Sorry.", start=3.7, end=4.2),
        ]
        return AsrSegment(text=" ".join(w.word for w in words), start=0.0, end=5.0, words=words)


def _meeting_workspace(tmp_path: Path) -> LabelingWorkspace:
    pipeline = TranscriptionPipeline(
        diarizer=ThreeSpeakerDiarizer(),
        asr=WordAsr(),
        audio=FakeAudio(),
        speaker_cache=SpeakerCache(tmp_path / "speaker-cache.json"),
        embedding_extractor=ThreeSpeakerEmbeddings(),
    )
    return LabelingWorkspace(pipeline, ArtifactStore(tmp_path / "jobs"))


def test_removing_a_speaker_keeps_them_out_of_the_transcript_until_restored(tmp_path):
    """A child talking through a call gets removed; a mis-press must be undoable,
    and the removal has to survive reopening the job."""
    workspace = _meeting_workspace(tmp_path)
    job_id = workspace.transcribe(_audio_file(tmp_path)).job_id

    workspace.toggle_speaker_excluded(job_id, "SPEAKER_01")
    reopened = LabelingWorkspace(workspace.pipeline, ArtifactStore(tmp_path / "jobs"))

    body = _body(reopened.transcript_text(job_id))
    assert "Mama!" not in body and "Juice." not in body
    assert "Morning." in body and "Sorry." in body
    assert [line.excluded for line in reopened.transcript_lines(job_id) if line.speaker == "SPEAKER_01"] == [True, True]

    reopened.toggle_speaker_excluded(job_id, "SPEAKER_01")
    assert "Mama!" in reopened.transcript_text(job_id)


def test_removing_one_line_leaves_the_rest_of_that_speaker(tmp_path):
    """Diarization lumps a stray remark in with someone real; only that line goes."""
    workspace = _meeting_workspace(tmp_path)
    job_id = workspace.transcribe(_audio_file(tmp_path)).job_id
    mama = next(line for line in workspace.transcript_lines(job_id) if line.text == "Mama!")

    assert workspace.toggle_line_excluded(job_id, mama.speaker, mama.start) is True
    body = _body(workspace.transcript_text(job_id))
    assert "Mama!" not in body and "Juice." in body

    assert workspace.toggle_line_excluded(job_id, mama.speaker, mama.start) is False
    assert "Mama!" in workspace.transcript_text(job_id)


def test_a_removed_speaker_is_not_waiting_for_a_name(tmp_path):
    """Nobody names the kids; counting them as unnamed would nag forever, and
    accept-all must not teach the voice cache a voice that was thrown away."""
    workspace = _meeting_workspace(tmp_path)
    earlier = tmp_path / "earlier.m4a"
    earlier.write_bytes(b"yesterday")
    workspace.label_speaker(workspace.transcribe(earlier).job_id, "SPEAKER_01", "Kid")
    job_id = workspace.transcribe(_audio_file(tmp_path)).job_id
    assert _row(workspace.speaker_rows(job_id), "SPEAKER_01").suggested_label == "Kid"

    rows = workspace.toggle_speaker_excluded(job_id, "SPEAKER_01")

    assert [r.speaker for r in rows if r.unnamed] == ["SPEAKER_00", "SPEAKER_02"]
    workspace.accept_all_suggestions(job_id)
    assert _label_of(workspace.speaker_rows(job_id), "SPEAKER_01") is None


def _row(rows, speaker: str):
    return next(row for row in rows if row.speaker == speaker)


def test_the_header_names_only_speakers_who_stayed_in(tmp_path):
    workspace = _meeting_workspace(tmp_path)
    job_id = workspace.transcribe(_audio_file(tmp_path)).job_id
    workspace.label_speaker(job_id, "SPEAKER_00", "Albert")
    workspace.label_speaker(job_id, "SPEAKER_01", "Kid")
    workspace.toggle_speaker_excluded(job_id, "SPEAKER_01")

    summary = workspace.job_summary(job_id)

    assert summary.named_speakers == ["Albert"]
    assert summary.header == f"meeting · {summary.recorded_on} · Albert"
    assert 'speakers: ["Albert"]' in workspace.transcript_text(job_id)
