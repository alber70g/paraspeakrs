"""The web page and its API: prepare a meeting, watch it, ask about it.

One process serves one meeting. Everything that happens is an event: the page gets
them over server-sent events (all past ones first, so a reload loses nothing), and
each is appended to ``events.jsonl`` in the session directory.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, ValidationError

from .analyzer import Analyzer, Thresholds
from .brief import Brief, load_brief, save_brief, slug
from .chat import ChatClient, ChatError
from .decisions import DecisionClient
from .intake import next_turn
from .transcript import Line, replay

KEEPALIVE_SECONDS = 15.0


class SessionStateError(RuntimeError):
    """Asked for something the meeting is not ready for, or past."""


@dataclass
class ReplaySource:
    """A finished transcript played back as if live."""

    name: str
    lines: Sequence[Line]
    speed: float
    step: float


class LiveSession:
    def __init__(
        self,
        source: ReplaySource,
        decisions: DecisionClient,
        chat: ChatClient,
        *,
        session_dir: Path,
        briefs_dir: Path,
        thresholds: Thresholds | None = None,
        brief: Brief | None = None,
    ) -> None:
        self.source = source
        self.decisions = decisions
        self.chat = chat
        self.session_dir = session_dir
        self.briefs_dir = briefs_dir
        self.thresholds = thresholds or Thresholds()
        self.brief = brief
        self.analyzer: Analyzer | None = None
        self.heard: list[Line] = []
        self.events: list[dict] = []
        self.state = "waiting"
        self._subscribers: set[asyncio.Queue] = set()
        self._task: asyncio.Task | None = None

    def emit(self, event: dict) -> None:
        event = {"seq": len(self.events), "time": time.time(), **event}
        self.events.append(event)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        with (self.session_dir / "events.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        for queue in self._subscribers:
            queue.put_nowait(event)

    def start(self, brief: Brief) -> None:
        if self.state != "waiting":
            raise SessionStateError("this meeting has already started")
        self.brief = brief
        save_brief(brief, self.session_dir / "brief.json")
        save_brief(brief, self.briefs_dir / f"{slug(brief.title) or 'meeting'}.json")
        self.analyzer = Analyzer(brief, self.decisions, self.chat, self.emit, thresholds=self.thresholds)
        self.state = "running"
        self.emit({"type": "status", "state": "running", "brief": brief.model_dump()})
        self._task = asyncio.create_task(self._run())

    async def ask(self, question: str) -> str:
        if self.analyzer is None:
            raise SessionStateError("start the meeting first")
        return await self.analyzer.ask(question, self.heard)

    async def _run(self) -> None:
        """Lines are shown the moment they arrive; analysis takes whatever has piled up.

        When the models are slower than the meeting, the next analysis step gets
        several batches at once instead of falling further and further behind.
        """
        assert self.analyzer is not None
        queue: asyncio.Queue[list[Line] | None] = asyncio.Queue()

        async def produce() -> None:
            try:
                async for batch in replay(self.source.lines, speed=self.source.speed, step=self.source.step):
                    self.heard.extend(batch)
                    self.emit({"type": "lines", "lines": [line.to_json() for line in batch]})
                    queue.put_nowait(batch)
            finally:
                queue.put_nowait(None)

        producer = asyncio.create_task(produce())
        try:
            done = False
            while not done:
                batch = await queue.get()
                if batch is None:
                    break
                while not queue.empty():
                    more = queue.get_nowait()
                    if more is None:
                        done = True
                        break
                    batch = batch + more
                await self.analyzer.feed(batch)
            await self.analyzer.finish()
            self.state = "finished"
            self.emit({"type": "status", "state": "finished"})
        except Exception as exc:  # noqa: BLE001 - shown on the page instead of dying silently
            producer.cancel()
            self.state = "failed"
            self.emit({"type": "error", "where": "session", "message": f"{type(exc).__name__}: {exc}"})
            raise
        finally:
            await asyncio.gather(producer, return_exceptions=True)

    async def stream(self) -> AsyncIterator[str]:
        """Every event so far, then new ones as they happen."""
        queue: asyncio.Queue = asyncio.Queue()
        # No await between the snapshot and subscribing, so no event falls between them.
        backlog = list(self.events)
        self._subscribers.add(queue)
        try:
            for event in backlog:
                yield _sse(event)
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), KEEPALIVE_SECONDS)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield _sse(event)
        finally:
            self._subscribers.discard(queue)


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


class IntakeRequest(BaseModel):
    history: list[dict[str, str]]


class StartRequest(BaseModel):
    brief: dict


class AskRequest(BaseModel):
    question: str


def create_app(session: LiveSession) -> FastAPI:
    app = FastAPI(title="paraspeakrs live")
    page = files("paraspeakrs.live").joinpath("page.html").read_text(encoding="utf-8")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return page

    @app.get("/api/state")
    def state() -> dict:
        return {
            "state": session.state,
            "brief": session.brief.model_dump() if session.brief else None,
            "source": {
                "name": session.source.name,
                "lines": len(session.source.lines),
                # Where the meeting clock ends, the same clock the lines carry.
                "seconds": session.source.lines[-1].end if session.source.lines else 0,
                "speed": session.source.speed,
            },
            "models": {
                "decisions": {"url": session.decisions.url, "model": session.decisions.model},
                "chat": {"url": session.chat.base_url, "model": session.chat.model},
            },
            "thresholds": session.thresholds.__dict__,
        }

    @app.post("/api/intake")
    async def intake(request: IntakeRequest) -> dict:
        try:
            turn = await next_turn(session.chat, request.history)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ChatError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {
            "history": turn.history,
            "question": turn.question.__dict__ if turn.question else None,
            "brief": turn.brief.model_dump() if turn.brief else None,
        }

    @app.get("/api/briefs")
    def briefs() -> list[dict]:
        if not session.briefs_dir.exists():
            return []
        out = []
        for path in sorted(session.briefs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                out.append({"name": path.stem, "brief": load_brief(path).model_dump()})
            except (ValidationError, ValueError):
                continue
        return out

    @app.post("/api/start")
    async def start(request: StartRequest) -> dict:
        try:
            brief = Brief.model_validate(request.brief)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            # In the event loop, not a worker thread: start() schedules the replay task there.
            session.start(brief)
        except SessionStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"state": session.state, "brief": brief.model_dump()}

    @app.post("/api/ask")
    async def ask(request: AskRequest) -> dict:
        if not request.question.strip():
            raise HTTPException(status_code=422, detail="ask a question")
        try:
            return {"answer": await session.ask(request.question.strip())}
        except SessionStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ChatError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/events")
    async def events() -> StreamingResponse:
        return StreamingResponse(
            session.stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
