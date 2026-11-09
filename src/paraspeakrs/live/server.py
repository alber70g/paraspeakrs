"""The web page and its API: prepare a meeting, watch it, ask about it.

One process serves one meeting. Its lines come from a source: a finished transcript
played back, or a recording transcribed while it is being made. Everything that
happens is an event: the page gets them over server-sent events (all past ones
first, so a reload loses nothing), and each is appended to ``events.jsonl`` in the
session directory.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Protocol

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, ValidationError

from .analyzer import Analyzer, Thresholds
from .brief import Brief, Persona, load_brief, save_brief, slug
from .chat import ChatClient, ChatError
from .decisions import DecisionClient
from .intake import next_turn
from .personas import PersonaLibrary, PersonaTurn, next_persona_turn
from .transcript import Line, render, replay

KEEPALIVE_SECONDS = 15.0
# Analysis that fell behind takes what piled up in pieces of at most this much meeting,
# so sections still split where the topic changes.
CATCHUP_SECONDS = 120.0


class SessionStateError(RuntimeError):
    """Asked for something the meeting is not ready for, or past."""


class Source(Protocol):
    """Where the lines come from."""

    # True: lines are taken, and shown, from the moment the page is up, because the
    # meeting happens whether or not the analysis has started. False: from the start.
    live: bool
    # Meeting seconds per wall-clock second.
    speed: float

    def describe(self) -> dict: ...

    def produce(self, status: Callable[[dict], None]) -> AsyncIterator[list[Line]]: ...


@dataclass
class ReplaySource:
    """A finished transcript played back as if live."""

    name: str
    lines: Sequence[Line]
    speed: float
    live: bool = False

    def describe(self) -> dict:
        return {
            "kind": "replay",
            "name": self.name,
            "lines": len(self.lines),
            # Where the meeting clock ends, the same clock the lines carry.
            "seconds": self.lines[-1].end if self.lines else 0,
            "speed": self.speed,
        }

    async def produce(self, status: Callable[[dict], None]) -> AsyncIterator[list[Line]]:
        async for line in replay(self.lines, speed=self.speed):
            yield [line]


class StepBatcher:
    """Groups lines into analysis batches of about ``step`` seconds of meeting."""

    def __init__(self, step: float) -> None:
        self.step = step
        self._pending: list[Line] = []
        self._since = 0.0

    def push(self, lines: Sequence[Line], now: float) -> list[Line] | None:
        if not self._pending:
            self._since = now
        self._pending += lines
        if self._pending[-1].end - self._pending[0].start >= self.step:
            return self.flush()
        return None

    def stale(self, now: float, after: float) -> list[Line] | None:
        """The pending lines, once the first has waited ``after`` seconds: a quiet meeting still gets analyzed."""
        return self.flush() if self._pending and now - self._since >= after else None

    def flush(self) -> list[Line]:
        batch, self._pending = self._pending, []
        return batch


def regroup(batches: list[list[Line]], longest: float) -> list[list[Line]]:
    if len(batches) == 1:
        return batches
    out: list[list[Line]] = []
    current: list[Line] = []
    for line in (line for batch in batches for line in batch):
        if current and line.end - current[0].start > longest:
            out.append(current)
            current = []
        current.append(line)
    if current:
        out.append(current)
    return out


class LiveSession:
    def __init__(
        self,
        source: Source,
        decisions: DecisionClient,
        chat: ChatClient,
        *,
        session_dir: Path,
        briefs_dir: Path,
        thresholds: Thresholds | None = None,
        brief: Brief | None = None,
        step: float = 30.0,
        personas_dir: Path | None = None,
    ) -> None:
        self.source = source
        self.decisions = decisions
        self.chat = chat
        self.session_dir = session_dir
        self.briefs_dir = briefs_dir
        # <workspace>/live/personas, next to the briefs.
        self.library = PersonaLibrary(personas_dir or briefs_dir.parent / "personas")
        self.thresholds = thresholds or Thresholds()
        self.brief = brief
        self.step = step
        self.analyzer: Analyzer | None = None
        self.heard: list[Line] = []
        self.events: list[dict] = []
        self.state = "waiting"
        self._subscribers: set[asyncio.Queue] = set()
        self._batches: asyncio.Queue[list[Line] | None] = asyncio.Queue()
        self._batcher = StepBatcher(step)
        self._pump: asyncio.Task | None = None
        self._task: asyncio.Task | None = None
        # The meeting's voices, when the source tells speakers apart (``speakers.py``).
        self.speakers = getattr(source, "speakers", None)
        self.book = getattr(self.speakers, "book", None)
        if self.book is not None:
            self.book.on_change = lambda: self.emit({"type": "speakers", "speakers": self.book.snapshot()})
            self.speakers.on_mark = self.emit

    def emit(self, event: dict) -> None:
        event = {"seq": len(self.events), "time": time.time(), **event}
        self.events.append(event)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        with (self.session_dir / "events.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        for queue in self._subscribers:
            queue.put_nowait(event)

    def begin(self) -> None:
        """The page is up: a live source starts listening now."""
        if self.source.live:
            self._start_pump()

    async def close(self) -> None:
        tasks = [t for t in (self._pump, self._task) if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def start(self, brief: Brief) -> None:
        if self.state != "waiting":
            raise SessionStateError("this meeting has already started")
        self.brief = brief
        self._save_brief()
        self.analyzer = Analyzer(brief, self.decisions, self.chat, self.emit, thresholds=self.thresholds)
        self.state = "running"
        self.emit({"type": "status", "state": "running", "brief": brief.model_dump()})
        self._start_pump()
        self._task = asyncio.create_task(self._consume())

    def _save_brief(self) -> None:
        """In the session, and as the reusable brief for the next meeting with this title."""
        assert self.brief is not None
        save_brief(self.brief, self.session_dir / "brief.json")
        save_brief(self.brief, self.briefs_dir / f"{slug(self.brief.title) or 'meeting'}.json")

    async def suggest_personas(self, history: list[dict[str, str]]) -> PersonaTurn:
        """The persona questionnaire, before or during the meeting, reading what was said so far."""
        return await next_persona_turn(self.chat, history, brief=self.brief, transcript=render(self._named(self.heard)))

    def add_persona(self, persona: Persona, *, rerun: bool) -> Persona:
        added = self._running().add_persona(persona, rerun=rerun)
        self._save_brief()
        return added

    def pause_persona(self, key: str, paused: bool) -> None:
        self._running().pause(key, paused)

    def remove_persona(self, key: str) -> None:
        self._running().remove(key)
        self._save_brief()

    def _running(self) -> Analyzer:
        if self.analyzer is None:
            raise SessionStateError("start the meeting first")
        return self.analyzer

    async def ask(self, question: str) -> str:
        if self.analyzer is None:
            raise SessionStateError("start the meeting first")
        return await self.analyzer.ask(question, self._named(self.heard))

    def name_speaker(self, voice_id: str, name: str | None) -> list[dict]:
        if self.book is None:
            raise SessionStateError("this meeting does not tell speakers apart")
        self.book.name(voice_id, name)
        return self.book.snapshot()

    def mark_speaking(self, name: str) -> dict:
        """Name whoever is speaking right now, once that moment has been diarized."""
        if self.speakers is None:
            raise SessionStateError("this meeting does not tell speakers apart")
        at = self.source.now() if hasattr(self.source, "now") else None
        if at is None:
            raise SessionStateError("no recording yet")
        event = self.speakers.mark(at, name)
        self.emit(event)
        return event

    def _named(self, lines: Sequence[Line]) -> list[Line]:
        """Lines under the names their speakers have now, for the models to read."""
        return self.book.relabel(lines) if self.book is not None else list(lines)

    def _start_pump(self) -> None:
        if self._pump is None:
            self._pump = asyncio.create_task(self._take_lines())

    def _on_source(self, info: dict) -> None:
        if "error" in info:
            self.emit({"type": "error", "where": "transcription", "message": info["error"]})
        else:
            self.emit({"type": "source", **info})

    async def _take_lines(self) -> None:
        """Lines are shown the moment they arrive and queued for analysis in batches."""
        try:
            async for lines in self.source.produce(self._on_source):
                if not lines:
                    continue
                self.heard.extend(lines)
                self.emit({"type": "lines", "lines": [line.to_json() for line in lines]})
                if batch := self._batcher.push(lines, time.monotonic()):
                    self._batches.put_nowait(batch)
        except Exception as exc:  # noqa: BLE001 - shown on the page; what was heard stays
            self.emit({"type": "error", "where": "transcription", "message": f"{type(exc).__name__}: {exc}"})
        finally:
            if rest := self._batcher.flush():
                self._batches.put_nowait(rest)
            self._batches.put_nowait(None)

    async def _consume(self) -> None:
        """Analyze batches as they come; when the models fall behind, take what piled up at once."""
        assert self.analyzer is not None
        stale_after = self.step / self.source.speed if self.source.speed > 0 else self.step
        getter: asyncio.Task | None = None
        try:
            done = False
            while not done:
                # A get that is kept across timeouts, rather than wait_for around it,
                # which on Python 3.11 can drop an item when the timeout fires.
                getter = getter or asyncio.ensure_future(self._batches.get())
                finished, _ = await asyncio.wait({getter}, timeout=stale_after)
                if getter in finished:
                    batch, getter = getter.result(), None
                else:
                    batch = self._batcher.stale(time.monotonic(), stale_after)
                    if not batch:
                        continue
                if batch is None:
                    break
                batches = [batch]
                while not self._batches.empty():
                    more = self._batches.get_nowait()
                    if more is None:
                        done = True
                        break
                    batches.append(more)
                for chunk in regroup(batches, CATCHUP_SECONDS):
                    await self.analyzer.feed(self._named(chunk))
            await self.analyzer.finish()
            self.state = "finished"
            self.emit({"type": "status", "state": "finished"})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - shown on the page instead of dying silently
            self.state = "failed"
            self.emit({"type": "error", "where": "session", "message": f"{type(exc).__name__}: {exc}"})
            raise
        finally:
            if getter is not None:
                getter.cancel()

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


def _persona(data: dict) -> Persona:
    try:
        return Persona.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


class IntakeRequest(BaseModel):
    history: list[dict[str, str]]


class StartRequest(BaseModel):
    brief: dict


class AskRequest(BaseModel):
    question: str


class NameRequest(BaseModel):
    name: str | None = None


class PersonaRequest(BaseModel):
    persona: dict
    rerun: bool = False


class PauseRequest(BaseModel):
    paused: bool


def create_app(session: LiveSession) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        session.begin()
        yield
        await session.close()

    app = FastAPI(title="paraspeakrs live", lifespan=lifespan)
    page = files("paraspeakrs.live").joinpath("page.html").read_text(encoding="utf-8")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return page

    @app.get("/api/state")
    def state() -> dict:
        return {
            "state": session.state,
            "brief": session.brief.model_dump() if session.brief else None,
            "source": session.source.describe(),
            "heard": len(session.heard),
            "models": {
                "decisions": {"url": session.decisions.url, "model": session.decisions.model},
                "chat": {"url": session.chat.base_url, "model": session.chat.model},
            },
            "thresholds": session.thresholds.__dict__,
            "speakers": session.book.snapshot() if session.book is not None else None,
            # Names in the voice cache, offered while typing a name.
            "voices": session.book.cache.names() if session.book is not None else [],
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
            # In the event loop, not a worker thread: start() schedules its tasks there.
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

    @app.post("/api/speaking-now")
    async def speaking_now(request: NameRequest) -> dict:
        if not (request.name or "").strip():
            raise HTTPException(status_code=422, detail="give a name")
        try:
            return session.mark_speaking(request.name)
        except SessionStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/speakers/{voice_id}")
    async def name_speaker(voice_id: str, request: NameRequest) -> dict:
        try:
            return {"speakers": session.name_speaker(voice_id, request.name)}
        except SessionStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no speaker {voice_id!r}") from exc

    @app.get("/api/personas/library")
    def persona_library() -> list[dict]:
        return session.library.list()

    @app.post("/api/personas/library")
    def save_to_library(request: PersonaRequest) -> dict:
        return {"persona": session.library.save(_persona(request.persona)).model_dump()}

    @app.delete("/api/personas/library/{key}")
    def delete_from_library(key: str) -> dict:
        try:
            session.library.delete(key)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no saved persona {key!r}") from exc
        return {"personas": session.library.list()}

    @app.post("/api/personas/suggest")
    async def suggest_personas(request: IntakeRequest) -> dict:
        try:
            turn = await session.suggest_personas(request.history)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ChatError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {
            "history": turn.history,
            "question": turn.question.__dict__ if turn.question else None,
            "personas": [p.model_dump() for p in turn.personas],
        }

    @app.post("/api/personas")
    async def add_persona(request: PersonaRequest) -> dict:
        persona = _persona(request.persona)
        try:
            # In the event loop: a rerun is a task there.
            added = session.add_persona(persona, rerun=request.rerun)
        except SessionStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"persona": added.model_dump()}

    @app.post("/api/personas/{key}/pause")
    async def pause_persona(key: str, request: PauseRequest) -> dict:
        try:
            session.pause_persona(key, request.paused)
        except SessionStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no persona {key!r} in this meeting") from exc
        return {"paused": request.paused}

    @app.delete("/api/personas/{key}")
    async def remove_persona(key: str) -> dict:
        try:
            session.remove_persona(key)
        except SessionStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no persona {key!r} in this meeting") from exc
        return {"removed": key}

    @app.get("/api/events")
    async def events() -> StreamingResponse:
        return StreamingResponse(
            session.stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
