from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
import logging
from pathlib import Path

from .pipeline import TranscriptionPipeline
from .storage import JobStore

LOGGER = logging.getLogger("uvicorn.error")


@dataclass(frozen=True)
class JobRequest:
    job_id: str
    file_path: Path


class Worker:
    def __init__(self, pipeline: TranscriptionPipeline, store: JobStore, workspace: Path) -> None:
        self.pipeline = pipeline
        self.store = store
        self.workspace = workspace
        self._queue: queue.Queue[JobRequest] = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def enqueue(self, request: JobRequest) -> None:
        LOGGER.info("queued transcription job %s for %s", request.job_id, request.file_path)
        self._queue.put(request)

    def _run(self) -> None:
        while True:
            request = self._queue.get()
            try:
                LOGGER.info("starting transcription job %s", request.job_id)
                self.store.mark_running(request.job_id)
                work_dir = self.workspace / "work" / request.job_id
                progress = self._progress_callback(request.job_id)
                result = self.pipeline.run(
                    work_dir=work_dir,
                    file_path=request.file_path,
                    job_id=request.job_id,
                    progress=progress,
                )
                self.store.mark_completed(request.job_id, result)
                LOGGER.info("completed transcription job %s", request.job_id)
            except Exception as exc:  # noqa: BLE001 - worker must record failures
                self.store.mark_failed(request.job_id, str(exc))
                LOGGER.exception("failed transcription job %s", request.job_id)
            finally:
                self._queue.task_done()

    def _progress_callback(self, job_id: str):
        def update(step: str, detail: str | None, percent: int | None) -> None:
            self.store.mark_progress(job_id, step, detail, percent)
            percent_text = f" ({percent}%)" if percent is not None else ""
            detail_text = f": {detail}" if detail else ""
            LOGGER.info("job %s progress%s %s%s", job_id, percent_text, step, detail_text)

        return update
