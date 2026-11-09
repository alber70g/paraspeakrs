"""The brief, and the questionnaire that fills it in."""

from __future__ import annotations

import json

import pytest

from live_fakes import FakeChat
from paraspeakrs.live.brief import Brief
from paraspeakrs.live.chat import ChatError
from paraspeakrs.live.intake import FINISH_NOW, next_turn

BRIEF = {
    "title": "Pricing sync",
    "language": "Dutch",
    "purpose": "Agree on the Q1 price change",
    "context": "ARR is 2.1M. Last year's increase lost 4% of customers.",
    "section_rule": "each pricing tier",
    "agenda": ["Starter", "Pro"],
    "participants": [{"name": "Anna", "role": "CFO", "goals": "margin"}],
    "personas": [
        {"name": "Churn watcher", "viewpoint": "customer retention", "comment_when": "a price rises without a migration path"},
        {"name": "Churn watcher", "comment_when": "dup"},
    ],
}


def test_persona_keys_are_unique_slugs():
    brief = Brief.model_validate(BRIEF)
    assert [p.key for p in brief.personas] == ["churn-watcher", "churn-watcher-2"]


def test_meeting_state_leaves_out_what_only_the_chat_model_needs():
    state = Brief.model_validate(BRIEF).meeting_state()
    assert state["participants"] == [{"name": "Anna", "role": "CFO", "goals": "margin"}]
    assert "context" not in state and "personas" not in state


def test_context_text_carries_the_background():
    text = Brief.model_validate(BRIEF).context_text()
    assert "Anna (CFO): wants margin" in text
    assert "ARR is 2.1M" in text


def _reply(obj: dict) -> str:
    return json.dumps(obj)


async def test_a_question_turn_returns_the_question_and_the_history_to_send_back():
    fake = FakeChat(lambda m: _reply({"type": "question", "question": "Who decides?", "options": ["Anna", "Ben", ""], "why": "owner"}))
    history = [{"role": "user", "content": "The meeting to prepare: pricing sync"}]

    turn = await next_turn(fake.client("m"), history)

    assert turn.question.question == "Who decides?"
    assert turn.question.options == ["Anna", "Ben"]
    assert turn.brief is None
    assert turn.history[:1] == history and turn.history[1]["role"] == "assistant"
    system = fake.requests[0]["messages"][0]
    assert system["role"] == "system" and "comment_when" in system["content"]


async def test_a_brief_turn_returns_a_validated_brief():
    fake = FakeChat(lambda m: _reply({"type": "brief", "brief": BRIEF}))
    turn = await next_turn(fake.client("m"), [{"role": "user", "content": "go"}])
    assert turn.brief.title == "Pricing sync"
    assert turn.question is None


async def test_after_the_last_question_the_model_is_told_to_write_the_brief():
    fake = FakeChat(lambda m: _reply({"type": "brief", "brief": BRIEF}))
    history = [{"role": "user", "content": "go"}]
    for i in range(3):
        history += [{"role": "assistant", "content": "{}"}, {"role": "user", "content": f"answer {i}"}]

    await next_turn(fake.client("m"), history, max_questions=3)

    assert fake.requests[0]["messages"][-1]["content"] == FINISH_NOW


async def test_a_reply_of_the_wrong_shape_is_sent_back_once_to_be_fixed():
    replies = iter([_reply({"type": "brief", "brief": {"personas": [{"name": "x"}]}}), _reply({"type": "brief", "brief": BRIEF})])
    fake = FakeChat(lambda m: next(replies))

    turn = await next_turn(fake.client("m"), [{"role": "user", "content": "go"}])

    assert turn.brief.title == "Pricing sync"
    assert "does not fit the required shape" in fake.requests[1]["messages"][-1]["content"]


async def test_two_unusable_replies_raise():
    fake = FakeChat(lambda m: _reply({"type": "poem"}))
    with pytest.raises(ChatError, match="unusable reply"):
        await next_turn(fake.client("m"), [{"role": "user", "content": "go"}])


async def test_the_history_must_end_with_the_user():
    with pytest.raises(ValueError):
        await next_turn(FakeChat().client("m"), [])
