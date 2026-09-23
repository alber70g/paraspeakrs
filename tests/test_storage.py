from paraspeakrs.models import JobStatus
from paraspeakrs.storage import JobStore


def test_job_store_records_progress(tmp_path):
    store = JobStore(tmp_path)
    record = store.create("job-1")

    assert record.status == JobStatus.queued
    assert record.progress_step == "queued"
    assert record.progress_percent == 0

    store.mark_running("job-1")
    store.mark_progress("job-1", "diarization", "running pyannote speaker diarization", 25)

    updated = store.get("job-1")
    assert updated.status == JobStatus.running
    assert updated.progress_step == "diarization"
    assert updated.progress_detail == "running pyannote speaker diarization"
    assert updated.progress_percent == 25
