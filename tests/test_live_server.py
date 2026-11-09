"""The web API end to end, against fake models, and the CLI wiring."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from live_fakes import FakeChat, FakeDecisions, last_user, meeting_chat
from paraspeakrs import cli
from paraspeakrs.live.server import LiveSession, ReplaySource, create_app
from paraspeakrs.live.transcript import Line

LINES = [
    Line(0, 20, "Anna", "We launch on March 3rd."),
    Line(20, 40, "Ben", "I can live with that."),
    Line(40, 60, "Anna", "Next: the budget."),
]
BRIEF = {"title": "Launch sync", "personas": [{"name": "Owner hawk", "comment_when": "a date has no owner"}]}


def _chat(messages):
    if messages[0]["role"] == "system" and "interviewing the user" in messages[0]["content"]:
        return json.dumps({"type": "question", "question": "Who owns the launch?", "options": ["Anna", "Ben"], "why": "w"})
    return meeting_chat(messages)


@pytest.fixture
def session(tmp_path):
    decisions = FakeDecisions(lambda key, q, state: 0.9 if "March" in state["new_lines"] else 0.1)
    return LiveSession(
        ReplaySource("launch.txt", LINES, speed=0),
        decisions.client(),
        FakeChat(_chat).client("m"),
        session_dir=tmp_path / "session",
        briefs_dir=tmp_path / "briefs",
        step=20,
    )


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.02)


def test_the_page_and_state_are_served(session):
    with TestClient(create_app(session)) as client:
        assert "Prepare the meeting" in client.get("/").text
        state = client.get("/api/state").json()
    assert state["state"] == "waiting"
    assert state["source"] == {"kind": "replay", "name": "launch.txt", "lines": 3, "seconds": 60, "speed": 0}
    assert state["heard"] == 0
    assert state["models"]["decisions"]["url"] == "http://jev.test/v1/decisions"


def test_intake_returns_the_next_question(session):
    with TestClient(create_app(session)) as client:
        reply = client.post("/api/intake", json={"history": [{"role": "user", "content": "The meeting to prepare: launch"}]})
    assert reply.status_code == 200
    body = reply.json()
    assert body["question"]["question"] == "Who owns the launch?"
    assert len(body["history"]) == 2 and body["brief"] is None


def test_asking_before_the_start_is_refused(session):
    with TestClient(create_app(session)) as client:
        assert client.post("/api/ask", json={"question": "anything?"}).status_code == 409


def test_a_bad_brief_is_refused(session):
    with TestClient(create_app(session)) as client:
        assert client.post("/api/start", json={"brief": {"personas": [{"name": "no criteria"}]}}).status_code == 422


def test_a_meeting_runs_to_the_end_and_can_be_asked_about(session, tmp_path):
    with TestClient(create_app(session)) as client:
        assert client.post("/api/start", json={"brief": BRIEF}).status_code == 200
        _wait_for(lambda: session.state == "finished")
        assert client.post("/api/start", json={"brief": BRIEF}).status_code == 409
        answer = client.post("/api/ask", json={"question": "When do we launch?"}).json()["answer"]
        saved = client.get("/api/briefs").json()

    assert answer == "The launch moved to March."
    types = [e["type"] for e in session.events]
    assert types[0] == "status" and types[-1] == "answer"
    assert types.count("lines") == 3 and "comment" in types and "section" in types
    assert [e["seq"] for e in session.events] == list(range(len(session.events)))
    logged = [json.loads(l) for l in (tmp_path / "session" / "events.jsonl").read_text().splitlines()]
    assert [e["type"] for e in logged] == types
    assert json.loads((tmp_path / "session" / "brief.json").read_text())["title"] == "Launch sync"
    assert [b["name"] for b in saved] == ["launch-sync"]


async def test_the_event_stream_replays_the_past_then_follows(session):
    session.emit({"type": "status", "state": "waiting"})
    stream = session.stream()
    first = await anext(stream)
    session.emit({"type": "error", "where": "x", "message": "y"})
    second = await anext(stream)
    await stream.aclose()
    assert json.loads(first.removeprefix("data: "))["seq"] == 0
    assert json.loads(second.removeprefix("data: "))["type"] == "error"
    assert not session._subscribers


def test_cli_live_dispatches_with_the_workspace(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr("paraspeakrs.live.cli.run", lambda args, env: seen.append((args, env.workspace_dir)))
    monkeypatch.setenv("PARAKEET_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("PARASPEAKRS_CHAT_URL", "http://mac.local:8000/v1")
    monkeypatch.setattr(cli.sys, "argv", ["paraspeakrs", "live", "replay", "m.txt", "--speed", "0"])

    cli._main()

    args, workspace = seen[0]
    assert (args.live_command, args.speed, args.chat_url) == ("replay", 0.0, "http://mac.local:8000/v1")
    assert args.decisions_url == "http://localhost:8001/v1/decisions"
    assert workspace == tmp_path.resolve()


class FakeRecording:
    """A live source: two lines before anyone presses Start, the rest after."""

    live = True
    speed = 1.0

    def __init__(self) -> None:
        import threading

        self.go_on = threading.Event()

    def describe(self) -> dict:
        return {"kind": "recording", "name": "2026-11-09_1430", "speakers": ["Me", "Others"], "state": "recording"}

    async def produce(self, status):
        import asyncio

        status({"state": "recording", "detail": "2026-11-09_1430"})
        yield LINES[:2]
        while not self.go_on.is_set():
            await asyncio.sleep(0.01)
        status({"error": "transcribing Others at 00:00:40 failed: boom"})
        yield LINES[2:]
        status({"state": "ended", "detail": "2026-11-09_1430"})


def test_a_live_source_is_heard_before_the_start_and_analyzed_after(tmp_path):
    recording = FakeRecording()
    decisions = FakeDecisions(lambda key, q, state: 0.9 if "March" in state["new_lines"] else 0.1)
    session = LiveSession(
        recording,
        decisions.client(),
        FakeChat(_chat).client("m"),
        session_dir=tmp_path / "session",
        briefs_dir=tmp_path / "briefs",
        step=30,
    )
    with TestClient(create_app(session)) as client:
        _wait_for(lambda: len(session.heard) == 2)
        assert session.state == "waiting" and not decisions.requests
        assert client.get("/api/state").json()["heard"] == 2

        client.post("/api/start", json={"brief": BRIEF})
        recording.go_on.set()
        _wait_for(lambda: session.state == "finished")

    types = [e["type"] for e in session.events]
    assert types[:2] == ["source", "lines"]
    assert types.index("status") > types.index("lines")  # heard before the meeting was started
    errors = [e for e in session.events if e["type"] == "error"]
    assert [(e["where"], e["message"]) for e in errors] == [("transcription", "transcribing Others at 00:00:40 failed: boom")]
    analyzed = [line for r in decisions.requests if "comment__owner-hawk" in r["questions"] for line in r["state"]["new_lines"].splitlines()]
    assert len(analyzed) == 3
    assert [e["state"] for e in session.events if e["type"] == "source"] == ["recording", "ended"]


def test_step_batcher_waits_for_a_full_step_or_for_staleness():
    from paraspeakrs.live.server import StepBatcher

    batcher = StepBatcher(30)
    assert batcher.push([Line(0, 10, "A", "a")], now=0.0) is None
    assert batcher.stale(now=29.0, after=30) is None
    assert [l.text for l in batcher.stale(now=30.0, after=30)] == ["a"]
    assert batcher.push([Line(10, 20, "A", "b")], now=31.0) is None
    assert [l.text for l in batcher.push([Line(20, 40, "A", "c")], now=32.0)] == ["b", "c"]


def test_a_backlog_is_analyzed_in_pieces_of_at_most_two_minutes():
    from paraspeakrs.live.server import regroup

    batches = [[Line(i * 30, i * 30 + 30, "A", str(i))] for i in range(10)]
    chunks = regroup(batches, 120)
    assert [[l.text for l in c] for c in chunks] == [["0", "1", "2", "3"], ["4", "5", "6", "7"], ["8", "9"]]
    assert regroup(batches[:1], 120) == batches[:1]


class SpeakingRecording(FakeRecording):
    """A live source that tells voices apart: one voice, heard before it has a name."""

    def __init__(self, tmp_path) -> None:
        from paraspeakrs.cache import SpeakerCache
        from paraspeakrs.live.speakers import SpeakerBook
        from paraspeakrs.live.windows import LiveSpeakers

        super().__init__()
        book = SpeakerBook(SpeakerCache(tmp_path / "speaker-cache.json"), session_id="s")
        self.speakers = LiveSpeakers(book, lambda track, audio: ([], {}))

    async def produce(self, status):
        import asyncio

        book = self.speakers.book
        voice = book.place("mic", "Me", {"SPEAKER_00": [1.0, 0.0]}, {"SPEAKER_00": 9}, {"SPEAKER_00": 9})["SPEAKER_00"]
        yield book.relabel([Line(0, 20, voice, "We launch on March 3rd.", speaker_id=voice)])
        while not self.go_on.is_set():
            await asyncio.sleep(0.01)
        yield book.relabel([Line(20, 40, voice, "Next: the budget.", speaker_id=voice)])


def test_a_speaker_named_in_the_page_is_read_by_that_name_from_then_on(tmp_path):
    recording = SpeakingRecording(tmp_path)
    decisions = FakeDecisions(lambda key, q, state: 0.1)
    session = LiveSession(
        recording, decisions.client(), FakeChat(_chat).client("m"),
        session_dir=tmp_path / "session", briefs_dir=tmp_path / "briefs", step=30,
    )
    with TestClient(create_app(session)) as client:
        _wait_for(lambda: len(session.heard) == 1)
        [voice] = client.get("/api/state").json()["speakers"]
        assert (voice["id"], voice["display"]) == ("S1", "Me 1")
        assert session.events[1]["lines"][0] == {"start": 0, "end": 20, "speaker": "Me 1", "text": "We launch on March 3rd.", "speaker_id": "S1"}

        named = client.post("/api/speakers/S1", json={"name": "Anna"}).json()["speakers"]
        assert client.post("/api/speakers/S9", json={"name": "Ben"}).status_code == 404
        client.post("/api/start", json={"brief": BRIEF})
        recording.go_on.set()
        _wait_for(lambda: session.state == "finished")

    assert named[0]["display"] == "Anna"
    assert [e["speakers"][0]["display"] for e in session.events if e["type"] == "speakers"][-1] == "Anna"
    # The line heard as "Me 1" reaches the models as Anna, like the one after the naming.
    analyzed = {line for r in decisions.requests for line in r["state"].get("new_lines", "").splitlines()}
    assert {line.split("] ")[1].split(":")[0] for line in analyzed} == {"Anna"}


def test_naming_is_refused_when_the_source_cannot_tell_voices_apart(session):
    with TestClient(create_app(session)) as client:
        assert client.get("/api/state").json()["speakers"] is None
        assert client.post("/api/speakers/S1", json={"name": "Anna"}).status_code == 409


def test_speaking_now_is_marked_at_the_recording_clock(tmp_path):
    recording = SpeakingRecording(tmp_path)
    recording.now = lambda: 754.25
    session = LiveSession(
        recording, FakeDecisions(lambda key, q, state: 0.1).client(), FakeChat(_chat).client("m"),
        session_dir=tmp_path / "session", briefs_dir=tmp_path / "briefs", step=30,
    )
    with TestClient(create_app(session)) as client:
        marked = client.post("/api/speaking-now", json={"name": "Anna"}).json()
        assert client.post("/api/speaking-now", json={"name": " "}).status_code == 422

    assert marked == {"type": "mark", "at": 754.25, "name": "Anna", "state": "waiting", "speaker": None}
    assert [m.at for m in recording.speakers.marks] == [754.25]
    assert marked in [{k: v for k, v in e.items() if k not in ("seq", "time")} for e in session.events]


def test_speaking_now_is_refused_without_speakers(session):
    with TestClient(create_app(session)) as client:
        assert client.post("/api/speaking-now", json={"name": "Anna"}).status_code == 409


SCOPE = {"name": "Scope guard", "comment_when": "work is added"}


def test_the_persona_library_lists_saves_and_deletes(session, tmp_path):
    with TestClient(create_app(session)) as client:
        assert [e["persona"]["key"] for e in client.get("/api/personas/library").json()] == ["facilitator"]
        saved = client.post("/api/personas/library", json={"persona": SCOPE}).json()["persona"]
        # Kept next to the reusable briefs, in the workspace.
        assert (tmp_path / "personas" / "scope-guard.json").exists()
        listed = client.get("/api/personas/library").json()
        bad = client.post("/api/personas/library", json={"persona": {"name": "no criteria"}})
        gone = client.delete("/api/personas/library/scope-guard")
        missing = client.delete("/api/personas/library/scope-guard")

    assert saved["key"] == "scope-guard"
    assert [(e["persona"]["key"], e["builtin"]) for e in listed] == [("facilitator", True), ("scope-guard", False)]
    assert (bad.status_code, gone.status_code, missing.status_code) == (422, 200, 404)


def test_meeting_personas_need_a_started_meeting_and_a_known_key(session):
    with TestClient(create_app(session)) as client:
        early = client.post("/api/personas", json={"persona": SCOPE})
        client.post("/api/start", json={"brief": BRIEF})
        bad = client.post("/api/personas", json={"persona": {"name": "x"}})
        unknown_pause = client.post("/api/personas/nobody/pause", json={"paused": True})
        unknown_remove = client.delete("/api/personas/nobody")
    assert (early.status_code, bad.status_code) == (409, 422)
    assert (unknown_pause.status_code, unknown_remove.status_code) == (404, 404)


def test_a_persona_added_during_the_meeting_reruns_and_is_saved_in_the_brief(session, tmp_path):
    with TestClient(create_app(session)) as client:
        client.post("/api/start", json={"brief": BRIEF})
        _wait_for(lambda: session.state == "finished")
        added = client.post("/api/personas", json={"persona": SCOPE, "rerun": True}).json()["persona"]
        _wait_for(lambda: any(e["type"] == "rerun" and e["state"] == "done" for e in session.events))
        reusable = json.loads((tmp_path / "briefs" / "launch-sync.json").read_text())
        assert client.post(f"/api/personas/{added['key']}/pause", json={"paused": True}).status_code == 200
        assert client.delete(f"/api/personas/{added['key']}").status_code == 200

    assert [p["key"] for p in reusable["personas"]] == ["owner-hawk", "scope-guard"]
    reruns = [e for e in session.events if e["type"] == "comment" and e.get("rerun")]
    assert reruns and {c["persona"] for c in reruns} == {"scope-guard"}
    after = json.loads((tmp_path / "session" / "brief.json").read_text())
    assert [p["key"] for p in after["personas"]] == ["owner-hawk"]
    assert [e["paused"] for e in session.events if e["type"] == "personas"] == [[], ["scope-guard"], []]


def test_personas_are_proposed_from_what_was_heard_before_the_start(session):
    proposal = json.dumps({"type": "personas", "personas": [SCOPE]})
    fake = FakeChat(lambda m: proposal if "propose new personas" in m[0]["content"] else "ok")
    session.chat = fake.client("m")
    session.heard = [Line(0, 5, "Anna", "We also build the app.")]
    with TestClient(create_app(session)) as client:
        reply = client.post("/api/personas/suggest", json={"history": [{"role": "user", "content": "more"}]}).json()
        empty = client.post("/api/personas/suggest", json={"history": []})
    assert [p["name"] for p in reply["personas"]] == ["Scope guard"]
    assert reply["question"] is None and reply["history"][-1]["role"] == "assistant"
    assert empty.status_code == 422
    assert "Anna: We also build the app." in fake.requests[0]["messages"][0]["content"]
