"""The persona library, and the questionnaire that proposes personas from the meeting so far."""

from __future__ import annotations

import json

import pytest

from live_fakes import FakeChat
from paraspeakrs.live.brief import Brief, Persona
from paraspeakrs.live.chat import ChatError
from paraspeakrs.live.personas import FINISH_NOW, PersonaLibrary, next_persona_turn

PROPOSAL = {"name": "Scope guard", "viewpoint": "scope", "comment_when": "work is added without dropping other work"}


def _reply(obj: dict) -> str:
    return json.dumps(obj)


def _keys(library: PersonaLibrary) -> list[tuple[str, bool]]:
    return [(e["persona"]["key"], e["builtin"]) for e in library.list()]


def test_an_empty_library_offers_the_facilitator(tmp_path):
    library = PersonaLibrary(tmp_path / "personas")
    assert _keys(library) == [("facilitator", True)]
    facilitator = library.list()[0]["persona"]
    assert "question" in facilitator["style"]


def test_saved_personas_are_listed_and_deleted_by_key(tmp_path):
    library = PersonaLibrary(tmp_path / "personas")
    saved = library.save(Persona(name="Budget keeper", comment_when="money is committed"))
    assert saved.key == "budget-keeper"
    assert _keys(library) == [("facilitator", True), ("budget-keeper", False)]

    library.delete("budget-keeper")
    assert _keys(library) == [("facilitator", True)]
    with pytest.raises(KeyError):
        library.delete("budget-keeper")


def test_a_saved_persona_replaces_the_preset_until_it_is_deleted(tmp_path):
    """The user can tune the Facilitator; deleting the tuned one brings the shipped one back."""
    library = PersonaLibrary(tmp_path / "personas")
    library.save(Persona(key="facilitator", name="Facilitator", comment_when="mine"))
    assert _keys(library) == [("facilitator", False)]
    assert library.list()[0]["persona"]["comment_when"] == "mine"

    library.delete("facilitator")
    assert _keys(library) == [("facilitator", True)]


def test_a_preset_cannot_be_deleted_and_a_key_is_never_a_path(tmp_path):
    library = PersonaLibrary(tmp_path / "personas")
    (tmp_path / "outside.json").write_text("{}")
    with pytest.raises(KeyError):
        library.delete("facilitator")
    with pytest.raises(KeyError):
        library.delete("../outside")
    assert (tmp_path / "outside.json").exists()


async def test_proposals_read_the_brief_and_the_end_of_the_transcript():
    fake = FakeChat(lambda m: _reply({"type": "personas", "personas": [PROPOSAL, {**PROPOSAL, "viewpoint": "other"}]}))
    brief = Brief(title="Roadmap", personas=[Persona(name="Owner hawk", comment_when="no owner")])
    transcript = "[00:00:01] Anna: " + "x" * 7000 + "\n[00:12:00] Ben: we also take on the mobile app"

    turn = await next_persona_turn(fake.client("m"), [{"role": "user", "content": "more angles"}], brief=brief, transcript=transcript)

    # Two proposals with the same name still get keys that tell them apart.
    assert [p.key for p in turn.personas] == ["scope-guard", "scope-guard-2"]
    assert turn.question is None
    system = fake.requests[0]["messages"][0]["content"]
    assert "Meeting: Roadmap" in system
    assert "- Owner hawk: no owner" in system
    assert "we also take on the mobile app" in system
    assert "[00:00:01]" not in system  # the start of a long meeting is left out


async def test_a_question_turn_comes_back_with_the_history():
    fake = FakeChat(lambda m: _reply({"type": "question", "question": "For whom?", "options": ["Team", "Client"]}))
    turn = await next_persona_turn(fake.client("m"), [{"role": "user", "content": "go"}], brief=None, transcript="")
    assert turn.question.question == "For whom?"
    assert turn.personas == []
    assert turn.history[-1]["role"] == "assistant"
    assert "There is no brief for this meeting yet." in fake.requests[0]["messages"][0]["content"]


async def test_after_three_questions_the_model_is_told_to_propose():
    fake = FakeChat(lambda m: _reply({"type": "personas", "personas": [PROPOSAL]}))
    history = [{"role": "user", "content": "go"}]
    for i in range(3):
        history += [{"role": "assistant", "content": "{}"}, {"role": "user", "content": f"answer {i}"}]
    await next_persona_turn(fake.client("m"), history, brief=None, transcript="")
    assert fake.requests[0]["messages"][-1]["content"] == FINISH_NOW


async def test_a_bad_shape_is_sent_back_once_then_refused():
    replies = iter([_reply({"type": "personas", "personas": []}), _reply({"type": "personas", "personas": [PROPOSAL]})])
    fake = FakeChat(lambda m: next(replies))
    turn = await next_persona_turn(fake.client("m"), [{"role": "user", "content": "go"}], brief=None, transcript="")
    assert [p.name for p in turn.personas] == ["Scope guard"]
    assert "does not fit the required shape" in fake.requests[1]["messages"][-1]["content"]

    fake = FakeChat(lambda m: _reply({"type": "poem"}))
    with pytest.raises(ChatError, match="persona questionnaire"):
        await next_persona_turn(fake.client("m"), [{"role": "user", "content": "go"}], brief=None, transcript="")
