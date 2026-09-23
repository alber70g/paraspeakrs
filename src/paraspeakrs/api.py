from __future__ import annotations

import logging
from pathlib import Path
import warnings
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse

from .config import Settings
from .factory import build_pipeline
from .models import JobRecord, JobStatus
from .storage import JobStore
from .transcript_txt import render_txt
from .worker import JobRequest, Worker

LOGGER = logging.getLogger("uvicorn.error")


def _log_warning(message, category, filename, lineno, file=None, line=None) -> None:
    LOGGER.warning("%s:%s: %s: %s", filename, lineno, category.__name__, message)


warnings.showwarning = _log_warning


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="Parakeet INT8 pyannote Service")
    store = JobStore(settings.workspace_dir / "jobs")
    pipeline = build_pipeline(settings)
    worker = Worker(pipeline, store, settings.workspace_dir)
    worker.start()

    @app.post("/jobs", response_model=JobRecord)
    async def create_job(file: UploadFile = File(...)) -> JobRecord:
        job_id = str(uuid4())
        upload_dir = settings.workspace_dir / "uploads" / job_id
        upload_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(file.filename or "audio").suffix
        input_path = upload_dir / f"input{suffix}"
        input_path.write_bytes(await file.read())
        record = store.create(job_id, input_path)
        LOGGER.info("accepted upload job %s filename=%s path=%s", job_id, file.filename, input_path)
        worker.enqueue(JobRequest(job_id=job_id, file_path=input_path))
        return record

    @app.get("/jobs/{job_id}", response_model=JobRecord)
    def get_job(job_id: str) -> JobRecord:
        try:
            return store.get(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc

    @app.get("/jobs/{job_id}/txt", response_class=PlainTextResponse)
    def get_job_txt(job_id: str) -> PlainTextResponse:
        try:
            record = store.get(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc
        if record.status in (JobStatus.queued, JobStatus.running):
            body = f"# job {job_id} {record.status} ({record.progress_step or '-'})\n"
            return PlainTextResponse(body, status_code=202)
        if record.status == JobStatus.failed or record.result is None:
            raise HTTPException(status_code=409, detail=record.error or "job failed")
        return PlainTextResponse(render_txt(record.result))

    return app
