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
        ReplaySource("launch.txt", LINES, speed=0, step=20),
        decisions.client(),
        FakeChat(_chat).client("m"),
        session_dir=tmp_path / "session",
        briefs_dir=tmp_path / "briefs",
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
    assert state["source"] == {"name": "launch.txt", "lines": 3, "seconds": 60, "speed": 0}
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
    monkeypatch.setattr("paraspeakrs.live.cli.run", lambda args, workspace: seen.append((args, workspace)))
    monkeypatch.setenv("PARAKEET_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("PARASPEAKRS_CHAT_URL", "http://mac.local:8000/v1")
    monkeypatch.setattr(cli.sys, "argv", ["paraspeakrs", "live", "replay", "m.txt", "--speed", "0"])

    cli._main()

    args, workspace = seen[0]
    assert (args.live_command, args.speed, args.chat_url) == ("replay", 0.0, "http://mac.local:8000/v1")
    assert args.decisions_url == "http://localhost:8080/v1/decisions"
    assert workspace == tmp_path.resolve()
