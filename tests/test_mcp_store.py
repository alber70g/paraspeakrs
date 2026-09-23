import logging

from paraspeakrs.mcp_store import ArtifactStore
from paraspeakrs.models import PipelineArtifacts, TranscriptionResult


def _artifacts(job_id: str) -> PipelineArtifacts:
    result = TranscriptionResult(
        job_id=job_id,
        duration_seconds=1.0,
        num_speakers=0,
        segments=[],
    )
    return PipelineArtifacts(
        result=result,
        diarization=[],
        normalized_audio_path="audio.wav",
    )


def test_list_jobs_skips_corrupt_artifacts_and_warns(tmp_path, caplog):
    store = ArtifactStore(tmp_path)
    store.work_dir("good-job")
    store.save("good-job", _artifacts("good-job"))

    corrupt_dir = store.work_dir("corrupt-job")
    (corrupt_dir / "artifacts.json").write_text("{not valid json", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="uvicorn.error"):
        jobs = store.list_jobs()

    # The corrupt job must not silently vanish: exactly the readable job comes
    # back, and a WARNING naming the corrupt job id is left behind so the gap
    # is diagnosable instead of looking like a job that never ran.
    job_ids = [job_id for job_id, _ in jobs]
    assert job_ids == ["good-job"]

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "corrupt-job" in warnings[0].message


def test_a_job_with_no_note_reads_as_empty_rather_than_missing(tmp_path):
    """Callers render the note straight into a text field; None there would put
    the string "None" in front of the user."""
    store = ArtifactStore(tmp_path / "jobs")
    assert store.note("never-written") == ""


def test_a_note_survives_pruning_the_job_audio(tmp_path):
    """Pruning throws away everything rebuildable from the source recording. A
    note is the one thing in a job dir that cannot be rebuilt from anything."""
    store = ArtifactStore(tmp_path / "jobs")
    work = store.work_dir("job-1")
    (work / "input.wav").write_bytes(b"x" * 64)
    store.set_note("job-1", "sprint planning")

    freed = store.prune_audio("job-1")

    assert freed == 64
    assert store.note("job-1") == "sprint planning"


def test_clearing_a_note_removes_the_file_rather_than_storing_blank(tmp_path):
    store = ArtifactStore(tmp_path / "jobs")
    store.set_note("job-1", "something")
    store.set_note("job-1", "   ")
    assert store.note("job-1") == ""
    assert not (store.root / "job-1" / "note.json").exists()


def test_an_unreadable_note_costs_the_note_not_the_job(tmp_path):
    """artifacts.json is the expensive, irreplaceable half of a job; a corrupt
    note must never be a reason the job cannot be opened."""
    store = ArtifactStore(tmp_path / "jobs")
    store.work_dir("job-1")
    (store.root / "job-1" / "note.json").write_text("{not json", encoding="utf-8")
    assert store.note("job-1") == ""
