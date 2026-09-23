from __future__ import annotations

import json
from pathlib import Path

from .models import JobRecord, JobStatus, TranscriptionResult


class JobStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, job_id: str, input_path: Path | None = None) -> JobRecord:
        record = JobRecord(
            job_id=job_id,
            status=JobStatus.queued,
            input_path=input_path,
            progress_step="queued",
            progress_detail="waiting for worker",
            progress_percent=0,
        )
        self.save(record)
        return record

    def mark_running(self, job_id: str) -> None:
        record = self.get(job_id)
        record.status = JobStatus.running
        record.progress_step = "starting"
        record.progress_detail = "worker picked up job"
        record.progress_percent = max(record.progress_percent, 1)
        self.save(record)

    def mark_progress(self, job_id: str, step: str, detail: str | None = None, percent: int | None = None) -> None:
        record = self.get(job_id)
        record.progress_step = step
        record.progress_detail = detail
        if percent is not None:
            record.progress_percent = max(0, min(100, percent))
        self.save(record)

    def mark_completed(self, job_id: str, result: TranscriptionResult) -> None:
        record = self.get(job_id)
        record.status = JobStatus.completed
        record.progress_step = "completed"
        record.progress_detail = "result ready"
        record.progress_percent = 100
        record.result = result
        self.save(record)

    def mark_failed(self, job_id: str, error: str) -> None:
        record = self.get(job_id)
        record.status = JobStatus.failed
        record.progress_step = "failed"
        record.progress_detail = error
        record.error = error
        self.save(record)

    def get(self, job_id: str) -> JobRecord:
        path = self._path(job_id)
        if not path.exists():
            raise KeyError(job_id)
        return JobRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, record: JobRecord) -> None:
        path = self._path(record.job_id)
        temp_path = path.with_suffix(".json.tmp")
        temp_path.write_text(
            json.dumps(record.model_dump(mode="json"), indent=2),
            encoding="utf-8",
        )
        temp_path.replace(path)

    def _path(self, job_id: str) -> Path:
        return self.root / f"{job_id}.json"
