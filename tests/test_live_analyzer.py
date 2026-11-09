"""Transcript replay, and the analysis: sections, comments, answers."""

from __future__ import annotations

import json

from live_fakes import FakeChat, FakeDecisions, last_user, meeting_chat
from paraspeakrs.live.analyzer import Analyzer, Thresholds
from paraspeakrs.live.brief import Brief, Persona
from paraspeakrs.live.transcript import Line, load_lines, replay

BRIEF = Brief.model_validate(
    {
        "title": "Launch sync",
        "personas": [
            {"name": "Owner hawk", "comment_when": "a date is agreed without an owner", "skip_when": "small talk"},
            {"name": "Budget keeper", "comment_when": "money is spent"},
        ],
    }
)


def test_txt_transcript_skips_frontmatter_and_guesses_line_ends(tmp_path):
    path = tmp_path / "meeting.txt"
    path.write_text(
        '---\ntitle: "weekly"\nnote: |\n  [00:00:01] Not: a line\n---\n\n'
        "[00:00:04] Albert: so where did we land\n"
        "[00:00:06] Michiel: on the migration: Q3\n"
        "not a transcript line\n"
        "[01:00:00] Albert: bye\n",
        encoding="utf-8",
    )
    lines = load_lines(path)
    assert [(l.start, l.speaker) for l in lines] == [(4.0, "Albert"), (6.0, "Michiel"), (3600.0, "Albert")]
    assert lines[0].end == 6.0  # cut at the next line
    assert lines[1].text == "on the migration: Q3"
    assert lines[2].end == 3601.0


def test_json_transcript_uses_the_same_lines_as_the_txt(tmp_path):
    path = tmp_path / "result.json"
    result = {
        "job_id": "j",
        "duration_seconds": 10,
        "num_speakers": 1,
        "segments": [{"speaker": "SPEAKER_00", "resolved_label": "Anna", "start": 0, "end": 4, "text": "Hello. Second sentence."}],
    }
    path.write_text(json.dumps({"result": result}), encoding="utf-8")
    assert [(l.speaker, l.text) for l in load_lines(path)] == [("Anna", "Hello."), ("Anna", "Second sentence.")]


def _lines(n: int, every: float = 10.0, speaker: str = "Anna") -> list[Line]:
    return [Line(i * every, i * every + every, speaker, f"point {i}") for i in range(n)]


async def test_replay_hands_out_every_line_in_order():
    lines = _lines(4)
    assert [line async for line in replay(lines, speed=0)] == lines


def _analyzer(decisions: FakeDecisions, chat: FakeChat, **thresholds) -> tuple[Analyzer, list[dict]]:
    events: list[dict] = []
    analyzer = Analyzer(
        BRIEF,
        decisions.client(),
        chat.client("m"),
        events.append,
        thresholds=Thresholds(**{"min_section_seconds": 30, **thresholds}),
    )
    return analyzer, events


def _of(events: list[dict], kind: str) -> list[dict]:
    return [e for e in events if e["type"] == kind]


async def test_a_topic_change_closes_the_section_and_titles_it():
    def rule(key, question, state):
        if key == "topic_changed":
            return 0.9 if "budget" in state["new_lines"] else 0.1
        return 0.0

    analyzer, events = _analyzer(FakeDecisions(rule), FakeChat(meeting_chat))
    await analyzer.feed([Line(0, 20, "Anna", "launch plan"), Line(20, 40, "Ben", "launch date")])
    await analyzer.feed([Line(40, 50, "Anna", "more on launch")])
    await analyzer.feed([Line(50, 60, "Ben", "now the budget")])
    await analyzer.finish()

    sections = _of(events, "section")
    assert [(s["section"], s["start"], s["reason"]) for s in sections] == [(0, 0, "start"), (1, 50, "topic")]
    assert [len(s.lines) for s in analyzer.sections] == [3, 1]
    assert {t["title"] for t in _of(events, "title")} == {"Budget review"}
    decisions = _of(events, "decision")
    assert decisions[0]["topic"] is None  # nothing to compare the first lines with
    assert decisions[1]["topic"] == 0.1 and decisions[2]["topic"] == 0.9


async def test_the_batch_is_cut_at_the_line_where_the_new_topic_starts():
    def rule(key, question, state):
        return 0.9 if key == "topic_changed" and "budget" in state["new_lines"] else 0.0

    def choose(key, question, state):
        return next(k for k, line in question["criteria"].items() if "budget" in line)

    decisions = FakeDecisions(rule, choose=choose)
    analyzer, events = _analyzer(decisions, FakeChat(meeting_chat))
    await analyzer.feed([Line(0, 40, "Anna", "launch date")])
    await analyzer.feed([Line(40, 50, "Ben", "date confirmed"), Line(50, 60, "Anna", "budget next"), Line(60, 70, "Ben", "18k")])

    assert [[l.text for l in s.lines] for s in analyzer.sections] == [
        ["launch date", "date confirmed"],
        ["budget next", "18k"],
    ]
    assert _of(events, "section")[1]["start"] == 50
    asked = decisions.requests[-3]["questions"]["first_new_line"]
    assert asked["type"] == "choice" and list(asked["criteria"]) == ["L0", "L1", "L2"]
    # The old section's last lines are still weighed for comments, then the new section's.
    assert [(e["section"], e["at"], e["topic"]) for e in _of(events, "decision")][1:] == [(0, 50, None), (1, 70, 0.9)]


async def test_a_short_section_is_not_split_by_a_topic_change():
    fake = FakeDecisions(lambda key, q, state: 0.99 if key == "topic_changed" else 0.0)
    analyzer, events = _analyzer(fake, FakeChat(meeting_chat), min_section_seconds=60)
    await analyzer.feed([Line(0, 10, "Anna", "a")])
    await analyzer.feed([Line(10, 20, "Anna", "b")])
    assert len(_of(events, "section")) == 1
    assert not any("topic_changed" in r["questions"] for r in fake.requests)


async def test_a_long_section_is_split_without_asking():
    fake = FakeDecisions()
    analyzer, events = _analyzer(fake, FakeChat(meeting_chat), max_section_seconds=60)
    await analyzer.feed([Line(0, 70, "Anna", "a very long monologue")])
    await analyzer.feed([Line(70, 80, "Anna", "still going")])
    assert [s["reason"] for s in _of(events, "section")] == ["start", "length"]


async def test_only_personas_above_the_threshold_comment_and_they_see_what_they_said():
    def rule(key, question, state):
        if key == "comment__owner-hawk":
            return 0.8
        if key == "comment__budget-keeper":
            return 0.3
        return 0.0

    decisions = FakeDecisions(rule)
    chat = FakeChat(meeting_chat)
    analyzer, events = _analyzer(decisions, chat)
    await analyzer.feed([Line(0, 10, "Anna", "launch on March 3rd")])
    await analyzer.feed([Line(10, 20, "Ben", "fine by me")])

    comments = _of(events, "comment")
    assert [(c["persona"], c["text"], c["p"]) for c in comments] == [("owner-hawk", "Who owns the March date?", 0.8)] * 2
    first, second = (r for r in decisions.requests if "comment__owner-hawk" in r["questions"])
    q = first["questions"]["comment__owner-hawk"]
    assert q["criteria"] == {"true": "a date is agreed without an owner", "false": "small talk"}
    assert "already_commented" not in first["state"]
    assert second["state"]["already_commented"] == {"owner-hawk": ["Who owns the March date?"]}
    assert "already_commented.owner-hawk" in second["questions"]["comment__owner-hawk"]["instructions"]
    comment_prompt = chat.requests[1]["messages"]
    assert comment_prompt[0]["content"].startswith("You are the Owner hawk")
    assert "What you already said in this section:\n- Who owns the March date?" in last_user(comment_prompt)
    assert _of(events, "decision")[0]["comments"] == {"owner-hawk": 0.8, "budget-keeper": 0.3}


async def test_a_failing_decision_model_is_reported_once_and_the_lines_still_count():
    analyzer, events = _analyzer(FakeDecisions(status=503), FakeChat(meeting_chat))
    for line in _lines(5):
        await analyzer.feed([line])
    errors = _of(events, "error")
    assert len(errors) == 1 and errors[0]["where"] == "decisions"
    assert len(analyzer.sections[0].lines) == 5


async def test_ask_sees_sections_comments_and_lines_not_yet_analyzed():
    chat = FakeChat(meeting_chat)
    analyzer, events = _analyzer(FakeDecisions(lambda key, q, s: 0.9 if key == "comment__owner-hawk" else 0.0), chat)
    heard = [Line(0, 10, "Anna", "launch on March 3rd"), Line(10, 20, "Ben", "not analyzed yet")]
    await analyzer.feed(heard[:1])

    answer = await analyzer.ask("When is the launch?", heard)

    assert answer == "The launch moved to March."
    prompt = last_user(chat.requests[-1]["messages"])
    assert "## Section 1 (from 00:00:00)" in prompt
    assert "> Owner hawk commented: Who owns the March date?" in prompt
    assert "Ben: not analyzed yet" in prompt
    assert prompt.endswith("Question: When is the launch?")
    assert _of(events, "answer")[0]["at"] == 20


async def test_ask_drops_the_oldest_lines_when_the_meeting_is_long():
    chat = FakeChat(meeting_chat)
    analyzer, _ = _analyzer(FakeDecisions(), chat)
    analyzer.ask_chars = 200
    heard = _lines(50)
    await analyzer.ask("What now?", heard)
    prompt = last_user(chat.requests[-1]["messages"])
    assert "[earlier part of the meeting left out]" in prompt
    assert "point 49" in prompt and "point 0\n" not in prompt


# -- personas added, paused and removed during the meeting ---------------------

SCOPE = Persona(name="Scope guard", comment_when="work is added")


def _fresh(decisions: FakeDecisions, chat: FakeChat, **kwargs) -> tuple[Analyzer, list[dict]]:
    """An analyzer on its own copy of the brief: adding a persona changes the brief."""
    events: list[dict] = []
    analyzer = Analyzer(
        BRIEF.model_copy(deep=True),
        decisions.client(),
        chat.client("m"),
        events.append,
        thresholds=Thresholds(min_section_seconds=30),
        **kwargs,
    )
    return analyzer, events


def _reacting_chat(messages):
    """A comment that names the last line it reacts to, so a test can tell comments apart."""
    prompt = last_user(messages)
    if "The new lines you react to:\n" in prompt:
        reacted = prompt.split("The new lines you react to:\n")[1].split("\n\n")[0].splitlines()[-1]
        return f"re: {reacted}"
    return meeting_chat(messages)


def _asked(decisions: FakeDecisions, key: str) -> list[dict]:
    return [r for r in decisions.requests if f"comment__{key}" in r["questions"]]


async def test_a_rerun_sees_the_meeting_only_up_to_each_step():
    """A persona added late comments on the past as if it had been there: never with hindsight."""
    decisions = FakeDecisions(lambda key, q, state: 0.9 if key == "comment__scope-guard" else 0.0)
    analyzer, events = _fresh(decisions, FakeChat(_reacting_chat))
    for line in _lines(9):  # 0-90 s, one section
        await analyzer.feed([line])

    added = analyzer.add_persona(SCOPE, rerun=True)
    await analyzer.feed([Line(90, 100, "Ben", "and the mobile app")])  # heard after the add: live
    await analyzer.finish()

    assert added.key == "scope-guard"
    reruns = [c for c in _of(events, "comment") if c.get("rerun")]
    live = [c for c in _of(events, "comment") if c["persona"] == "scope-guard" and not c.get("rerun")]
    assert [c["at"] for c in reruns] == [30, 60, 90]
    assert [c["text"] for c in live] == ["re: [00:01:30] Ben: and the mobile app"]
    for request in _asked(decisions, "scope-guard"):
        so_far, new = request["state"]["section_so_far"], request["state"]["new_lines"]
        assert so_far.endswith(new.splitlines()[-1])  # nothing after the step it is about
        if "mobile app" not in new:  # a rerun step: the live comment came later
            assert live[0]["text"] not in json.dumps(request["state"].get("already_commented", {}))
    assert _of(events, "rerun")[-1] == {"type": "rerun", "persona": "scope-guard", "of": 3, "done": 3, "state": "done"}


async def test_a_paused_persona_is_not_asked_and_pausing_stops_its_rerun():
    decisions = FakeDecisions(lambda key, q, state: 0.9)
    analyzer, events = _fresh(decisions, FakeChat(meeting_chat))
    for line in _lines(6):
        await analyzer.feed([line])
    asked_before = len(decisions.requests)

    analyzer.add_persona(SCOPE, rerun=True)
    analyzer.pause("scope-guard", True)  # before the rerun got a turn
    await analyzer.feed([Line(60, 70, "Ben", "more")])
    await analyzer.finish()

    assert _asked(decisions, "scope-guard") == []
    assert len(decisions.requests) > asked_before  # the others still are
    assert _of(events, "rerun")[-1]["state"] == "stopped"
    assert _of(events, "personas")[-1]["paused"] == ["scope-guard"]

    analyzer.pause("scope-guard", False)
    await analyzer.feed([Line(70, 80, "Ben", "again")])
    assert len(_asked(decisions, "scope-guard")) == 1


async def test_a_removed_persona_keeps_its_comments_and_its_key():
    decisions = FakeDecisions(lambda key, q, state: 0.9 if key == "comment__scope-guard" else 0.0)
    analyzer, events = _fresh(decisions, FakeChat(meeting_chat))
    analyzer.add_persona(SCOPE, rerun=False)
    await analyzer.feed([Line(0, 10, "Anna", "add the app")])

    analyzer.remove("scope-guard")
    await analyzer.feed([Line(10, 20, "Anna", "and the web")])
    again = analyzer.add_persona(SCOPE, rerun=False)

    assert [c["persona"] for c in analyzer.comments] == ["scope-guard"]
    assert len(_asked(decisions, "scope-guard")) == 1
    # Added again, it is a new persona: the old comments stay with the old key.
    assert again.key == "scope-guard-2"
    assert [p["key"] for p in _of(events, "personas")[-1]["personas"]] == ["owner-hawk", "budget-keeper", "scope-guard-2"]
