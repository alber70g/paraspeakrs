"""The agent flow: a directory of samples the user names by renaming files.

The filenames are the whole user interface, so every rename must mean exactly
one thing, a mistake must stop apply rather than silently drop a speaker, and
apply must be safe to run again after the user changes their mind.
"""

import json
from pathlib import Path

import pytest

from paraspeakrs import agent_dir
from paraspeakrs.cache import SpeakerCache
from paraspeakrs.labeling import LabelingWorkspace
from paraspeakrs.mcp_store import ArtifactStore
from paraspeakrs.models import AsrSegment, DiarizationSegment, Word
from paraspeakrs.pipeline import TranscriptionPipeline
from test_labeling import FakeAudio
from test_montage import WavAudio

EMBEDDINGS = {"SPEAKER_00": [1.0, 0.0, 0.0], "SPEAKER_01": [0.0, 1.0, 0.0], "SPEAKER_02": [0.0, 0.0, 1.0]}


class Audio(FakeAudio, WavAudio):
    """Montages are real wavs; everything else only needs to exist."""

    export_chunk = WavAudio.export_chunk


class ThreeSpeakers:
    name = "fake"

    def diarize(self, audio_path):
        return [
            DiarizationSegment(speaker="SPEAKER_00", start=0.0, end=4.0),
            DiarizationSegment(speaker="SPEAKER_01", start=4.0, end=8.0),
            DiarizationSegment(speaker="SPEAKER_02", start=8.0, end=12.0),
        ]


class Asr:
    def transcribe(self, audio_path):
        said = [("Welcome", 0.5), ("everyone.", 1.0), ("Thanks.", 5.0), ("Hello.", 9.0)]
        words = [Word(word=w, start=t, end=t + 0.4) for w, t in said]
        return AsrSegment(text=" ".join(w for w, _ in said), start=0.0, end=12.0, words=words)


class FailingAsr:
    """`agent diarize` must never reach the ASR engine."""

    def transcribe(self, audio_path):
        raise AssertionError("ASR must not run for `agent diarize`")


class Embeddings:
    def extract(self, audio_path, diarization):
        return EMBEDDINGS


def _workspace(tmp_path: Path, asr=None) -> LabelingWorkspace:
    cache = SpeakerCache(tmp_path / "speaker-cache.json")
    # A voice the library already knows, so SPEAKER_01 arrives with a suggestion.
    cache.fold("Alice", EMBEDDINGS["SPEAKER_01"], job_id="earlier", speaker_id="SPEAKER_03")
    pipeline = TranscriptionPipeline(
        diarizer=ThreeSpeakers(),
        asr=asr or Asr(),
        audio=Audio(),
        speaker_cache=cache,
        embedding_extractor=Embeddings(),
    )
    return LabelingWorkspace(pipeline, ArtifactStore(tmp_path / "jobs"))


@pytest.fixture
def prepared(tmp_path):
    workspace = _workspace(tmp_path)
    source = tmp_path / "meeting.m4a"
    source.write_bytes(b"")
    out = tmp_path / "out"
    result = agent_dir.prepare(workspace, source, out)
    return workspace, out, result


@pytest.fixture
def diarized(tmp_path):
    # A FailingAsr workspace: `agent diarize` reaching it would fail the test.
    workspace = _workspace(tmp_path, asr=FailingAsr())
    source = tmp_path / "meeting.m4a"
    source.write_bytes(b"")
    out = tmp_path / "out"
    result = agent_dir.diarize(workspace, source, out)
    return workspace, out, result


def _rename(out: Path, old: str, new: str) -> None:
    (out / old).rename(out / new)


def _transcript_names(out: Path) -> set[str]:
    body = (out / "transcript.txt").read_text().split("\n---\n\n")[-1]
    return {line.split("] ", 1)[1].split(":", 1)[0] for line in body.splitlines() if line}


def _voices(workspace) -> dict[str, list[str | None]]:
    cache = workspace.pipeline.speaker_cache
    return {name: sorted(m.speaker_id for m in cache.members(name) if m.job_id != "earlier") for name in cache.names()}


def test_prepare_writes_one_sample_per_speaker_and_suggests_known_voices(prepared):
    _, out, result = prepared

    assert sorted(p.name for p in out.iterdir()) == [
        "NEXT_STEPS.md",
        "SPEAKER_00__NAME-ME.wav",
        "SPEAKER_01__Alice_1.00.wav",
        "SPEAKER_02__NAME-ME.wav",
        "manifest.json",
        "transcript.json",
        "transcript.txt",
    ]
    # The agent gets text to reason from, not just audio it cannot hear.
    assert result["speakers"][0]["sample_lines"] == ["[00:00:00] Welcome everyone."]
    assert "agent apply" in result["next"]


def test_prepare_refuses_a_directory_with_something_in_it(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "notes.txt").write_text("mine")
    source = tmp_path / "meeting.m4a"
    source.write_bytes(b"")

    with pytest.raises(ValueError, match="not empty"):
        agent_dir.prepare(_workspace(tmp_path), source, out)


def test_every_rename_means_one_thing(prepared):
    workspace, out, _ = prepared
    _rename(out, "SPEAKER_00__NAME-ME.wav", "SPEAKER_00__Bob.wav")
    (out / "SPEAKER_02__NAME-ME.wav").unlink()

    result = agent_dir.apply(workspace, out)

    actions = {s["speaker"]: (s["action"], s["name"]) for s in result["speakers"]}
    assert actions == {
        "SPEAKER_00": ("named", "Bob"),
        "SPEAKER_01": ("suggestion", "Alice"),
        "SPEAKER_02": ("dropped", None),
    }
    # Dropped means gone from the transcript, not relabeled.
    assert _transcript_names(out) == {"Bob", "Alice"}
    # A typed name teaches the library; a guess left alone must not reinforce itself.
    assert _voices(workspace) == {"Alice": [], "Bob": ["SPEAKER_00"]}


def test_untouched_name_me_keeps_the_raw_id(prepared):
    workspace, out, _ = prepared

    agent_dir.apply(workspace, out)

    assert _transcript_names(out) == {"SPEAKER_00", "Alice", "SPEAKER_02"}


def test_same_name_twice_merges_the_speakers(prepared):
    workspace, out, _ = prepared
    _rename(out, "SPEAKER_00__NAME-ME.wav", "SPEAKER_00__Bob.wav")
    _rename(out, "SPEAKER_02__NAME-ME.wav", "SPEAKER_02__Bob.wav")

    result = agent_dir.apply(workspace, out)

    assert result["merged"] == {"Bob": ["SPEAKER_00", "SPEAKER_02"]}
    assert _voices(workspace)["Bob"] == ["SPEAKER_00", "SPEAKER_02"]


def test_reapply_follows_a_change_of_mind(prepared):
    """Rename, apply, rename again, restore a deleted file: the last state wins."""
    workspace, out, _ = prepared
    _rename(out, "SPEAKER_00__NAME-ME.wav", "SPEAKER_00__Bob.wav")
    (out / "SPEAKER_02__NAME-ME.wav").rename(out.parent / "kept.wav")
    agent_dir.apply(workspace, out)

    _rename(out, "SPEAKER_00__Bob.wav", "SPEAKER_00__Carol.wav")
    _rename(out, "SPEAKER_01__Alice_1.00.wav", "SPEAKER_01__Alice.wav")
    (out.parent / "kept.wav").rename(out / "SPEAKER_02__Dave.wav")
    agent_dir.apply(workspace, out)
    first = (out / "transcript.txt").read_text()
    agent_dir.apply(workspace, out)

    assert _transcript_names(out) == {"Carol", "Alice", "Dave"}
    # The wrong name is taken back out of the library, not left pulling on it.
    assert _voices(workspace) == {"Alice": ["SPEAKER_01"], "Carol": ["SPEAKER_00"], "Dave": ["SPEAKER_02"]}
    assert (out / "transcript.txt").read_text() == first


def test_dry_run_changes_nothing(prepared):
    workspace, out, _ = prepared
    before = (out / "transcript.txt").read_text()
    _rename(out, "SPEAKER_00__NAME-ME.wav", "SPEAKER_00__Bob.wav")

    result = agent_dir.apply(workspace, out, dry_run=True)

    assert result["speakers"][0] == {"speaker": "SPEAKER_00", "action": "named", "name": "Bob"}
    assert (out / "transcript.txt").read_text() == before
    assert "Bob" not in workspace.pipeline.speaker_cache.names()


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        # Dropping the ID would otherwise read as "speaker deleted" and lose their lines.
        ("SPEAKER_00__NAME-ME.wav", "Bob.wav", "does not start with a speaker ID"),
        ("SPEAKER_00__NAME-ME.wav", "SPEAKER_00__.wav", "empty"),
        ("SPEAKER_02__NAME-ME.wav", "SPEAKER_00__Bob.wav", "second file for SPEAKER_00"),
    ],
)
def test_a_bad_filename_stops_apply_before_any_change(prepared, old, new, message):
    workspace, out, _ = prepared
    before = (out / "transcript.txt").read_text()
    _rename(out, old, new)

    with pytest.raises(ValueError, match=message):
        agent_dir.apply(workspace, out)

    assert (out / "transcript.txt").read_text() == before


def test_a_name_that_only_looks_like_a_suggestion_is_a_name(prepared):
    workspace, out, _ = prepared
    _rename(out, "SPEAKER_00__NAME-ME.wav", "SPEAKER_00__Room_2.wav")

    result = agent_dir.apply(workspace, out)

    assert result["speakers"][0] == {"speaker": "SPEAKER_00", "action": "named", "name": "Room_2"}


def test_manifest_points_at_the_shared_job(prepared):
    """The TUI and MCP server must see the names the agent flow applied."""
    workspace, out, result = prepared
    _rename(out, "SPEAKER_00__NAME-ME.wav", "SPEAKER_00__Bob.wav")
    agent_dir.apply(workspace, out)

    job_id = json.loads((out / "manifest.json").read_text())["job_id"]
    assert job_id == result["job_id"]
    assert workspace.speaker_rows(job_id)[0].assigned_label == "Bob"


def _transcript_json(out: Path) -> dict:
    return json.loads((out / "transcript.json").read_text())


def test_transcribe_writes_a_transcript_json_matching_the_schema(prepared):
    """MacParakeet reads this file for word-level timings and speakers, so its
    shape has to be exact, not just "parseable"."""
    workspace, out, result = prepared
    data = _transcript_json(out)

    assert data["version"] == 1
    assert data["job_id"] == result["job_id"]
    assert data["source"] == str(out.parent / "meeting.m4a")
    assert data["language"] is None
    assert data["duration_seconds"] == 5.0

    assert data["speakers"] == [
        {
            "id": "SPEAKER_00",
            "label": None,
            "label_source": None,
            "suggested_label": None,
            "suggestion_score": None,
            "sample_file": "SPEAKER_00__NAME-ME.wav",
            "talk_seconds": 4.0,
            "excluded": False,
        },
        {
            "id": "SPEAKER_01",
            "label": "Alice",
            "label_source": "suggestion",
            "suggested_label": "Alice",
            "suggestion_score": 1.0,
            "sample_file": "SPEAKER_01__Alice_1.00.wav",
            "talk_seconds": 4.0,
            "excluded": False,
        },
        {
            "id": "SPEAKER_02",
            "label": None,
            "label_source": None,
            "suggested_label": None,
            "suggestion_score": None,
            "sample_file": "SPEAKER_02__NAME-ME.wav",
            "talk_seconds": 4.0,
            "excluded": False,
        },
    ]
    # Unnamed/suggested speakers keep their raw ID as the segment speaker - the
    # same ID the user renames files by, not a name nobody confirmed yet.
    assert [s["speaker"] for s in data["segments"]] == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"]
    assert data["segments"][0] == {
        "speaker": "SPEAKER_00",
        "start": 0.5,
        "end": 1.4,
        "text": "Welcome everyone.",
        "words": [
            {"word": "Welcome", "start": 0.5, "end": 0.9},
            {"word": "everyone.", "start": 1.0, "end": 1.4},
        ],
    }


def test_apply_keeps_transcript_json_in_sync_with_the_directory(prepared):
    """apply restates the whole directory every run; transcript.json must follow
    the same renames and deletions as transcript.txt, not a stale snapshot from
    `transcribe` time."""
    workspace, out, _ = prepared
    _rename(out, "SPEAKER_00__NAME-ME.wav", "SPEAKER_00__Bob.wav")
    (out / "SPEAKER_02__NAME-ME.wav").unlink()

    agent_dir.apply(workspace, out)
    data = _transcript_json(out)

    by_id = {s["id"]: s for s in data["speakers"]}
    assert by_id["SPEAKER_00"]["label"] == "Bob"
    assert by_id["SPEAKER_00"]["label_source"] == "named"
    assert by_id["SPEAKER_00"]["sample_file"] == "SPEAKER_00__Bob.wav"
    assert by_id["SPEAKER_00"]["excluded"] is False

    # Deleting the sample drops the speaker from the transcript, not just the file.
    assert by_id["SPEAKER_02"]["excluded"] is True
    assert by_id["SPEAKER_02"]["sample_file"] is None
    assert by_id["SPEAKER_02"]["label"] is None

    speakers_in_segments = {s["speaker"] for s in data["segments"]}
    assert speakers_in_segments == {"SPEAKER_00", "SPEAKER_01"}


def test_dry_run_does_not_touch_transcript_json(prepared):
    workspace, out, _ = prepared
    before = (out / "transcript.json").read_text()
    _rename(out, "SPEAKER_00__NAME-ME.wav", "SPEAKER_00__Bob.wav")

    agent_dir.apply(workspace, out, dry_run=True)

    # A dry run reports what would change; it must not rewrite any output file.
    assert (out / "transcript.json").read_text() == before


def _diarization_json(out: Path) -> dict:
    return json.loads((out / "diarization.json").read_text())


def test_diarize_writes_samples_and_diarization_json_with_no_asr(diarized):
    _, out, result = diarized

    assert sorted(p.name for p in out.iterdir()) == [
        "NEXT_STEPS.md",
        "SPEAKER_00__NAME-ME.wav",
        "SPEAKER_01__Alice_1.00.wav",
        "SPEAKER_02__NAME-ME.wav",
        "diarization.json",
        "manifest.json",
    ]
    assert result["diarization"] == str(out / "diarization.json")
    assert "transcript" not in result

    assert json.loads((out / "manifest.json").read_text())["kind"] == "diarization"

    data = _diarization_json(out)
    assert data["version"] == 1
    assert data["job_id"] == result["job_id"]
    assert data["source"] == str(out.parent / "meeting.m4a")
    assert data["duration_seconds"] == 5.0
    # ThreeSpeakers diarizes 00/01/02 back to back - already in order.
    assert data["turns"] == [
        {"speaker": "SPEAKER_00", "start": 0.0, "end": 4.0},
        {"speaker": "SPEAKER_01", "start": 4.0, "end": 8.0},
        {"speaker": "SPEAKER_02", "start": 8.0, "end": 12.0},
    ]
    by_id = {s["id"]: s for s in data["speakers"]}
    assert by_id["SPEAKER_01"]["label"] == "Alice"
    assert by_id["SPEAKER_01"]["label_source"] == "suggestion"
    assert by_id["SPEAKER_01"]["sample_file"] == "SPEAKER_01__Alice_1.00.wav"


def test_diarize_apply_names_a_speaker_and_drops_an_excluded_ones_turns(diarized):
    workspace, out, _ = diarized
    _rename(out, "SPEAKER_00__NAME-ME.wav", "SPEAKER_00__Bob.wav")
    (out / "SPEAKER_02__NAME-ME.wav").unlink()

    result = agent_dir.apply(workspace, out)

    assert result["diarization"] == str(out / "diarization.json")
    assert "transcript" not in result
    assert "lines" not in result
    # apply must not have written a transcript for a diarize-only directory.
    assert not (out / "transcript.txt").exists()
    assert not (out / "transcript.json").exists()

    data = _diarization_json(out)
    by_id = {s["id"]: s for s in data["speakers"]}
    assert by_id["SPEAKER_00"]["label"] == "Bob"
    assert by_id["SPEAKER_00"]["label_source"] == "named"
    assert by_id["SPEAKER_00"]["sample_file"] == "SPEAKER_00__Bob.wav"
    assert by_id["SPEAKER_02"]["excluded"] is True
    assert by_id["SPEAKER_02"]["sample_file"] is None

    # The dropped speaker's turns are gone, not merely unlabeled.
    assert {t["speaker"] for t in data["turns"]} == {"SPEAKER_00", "SPEAKER_01"}


def test_unattributed_segments_have_a_null_speaker(tmp_path):
    """A word diarization never covered (no speaker overlaps it at all) must
    not surface as a fake speaker ID in the JSON - MacParakeet would otherwise
    try to look up a speaker that was never offered for naming."""

    class AsrWithUnattributedTail:
        def transcribe(self, audio_path):
            said = [("Welcome", 0.5), ("everyone.", 1.0), ("Thanks.", 5.0), ("Hello.", 9.0), ("Echo?", 20.0)]
            words = [Word(word=w, start=t, end=t + 0.4) for w, t in said]
            return AsrSegment(text=" ".join(w for w, _ in said), start=0.0, end=20.4, words=words)

    cache = SpeakerCache(tmp_path / "speaker-cache.json")
    pipeline = TranscriptionPipeline(
        diarizer=ThreeSpeakers(),
        asr=AsrWithUnattributedTail(),
        audio=Audio(),
        speaker_cache=cache,
        embedding_extractor=Embeddings(),
    )
    workspace = LabelingWorkspace(pipeline, ArtifactStore(tmp_path / "jobs"))
    source = tmp_path / "meeting.m4a"
    source.write_bytes(b"")
    out = tmp_path / "out"
    agent_dir.prepare(workspace, source, out)

    data = _transcript_json(out)

    unattributed = [s for s in data["segments"] if s["speaker"] is None]
    assert [s["text"] for s in unattributed] == ["Echo?"]
    # Diarization only ever produced SPEAKER_00/01/02; the pseudo-speaker must
    # never be listed as though a person could rename or exclude it.
    assert "UNKNOWN" not in {s["id"] for s in data["speakers"]}
