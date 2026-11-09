"""The JEV decision client, the chat client, and `paraspeakrs live check`."""

from __future__ import annotations

import argparse

import httpx
import pytest

from live_fakes import FakeChat, FakeDecisions
from paraspeakrs.live import cli as live_cli
from paraspeakrs.live.chat import ChatError, extract_json, strip_thinking
from paraspeakrs.live.decisions import (
    ChoiceAnswer,
    DecisionClient,
    DecisionError,
    NoulAnswer,
    ScoreAnswer,
    choice,
    noul,
    score,
)


async def test_decide_sends_the_jev_request_shape_and_reads_typed_answers():
    fake = FakeDecisions(lambda key, q, state: 0.83)
    client = fake.client()

    answers = await client.decide(
        {"note": "launch moved"},
        {
            "moved": noul("Did the launch move?", true="a new date is named", false="no date changes"),
            "team": choice("Which team?", {"billing": "payments", "tech": None}),
            "mood": score("How tense is it?", ["calm", "tense", "heated"]),
        },
    )

    sent = fake.requests[0]
    assert sent["model"] == "typesafe/jev-1.14"
    assert sent["state"] == {"note": "launch moved"}
    assert sent["questions"]["moved"] == {
        "type": "noul",
        "instructions": "Did the launch move?",
        "criteria": {"true": "a new date is named", "false": "no date changes"},
    }
    assert sent["questions"]["mood"]["criteria"] == ["calm", "tense", "heated"]
    assert answers["moved"] == NoulAnswer(p_yes=0.83)
    assert isinstance(answers["team"], ChoiceAnswer) and answers["team"].choice == "billing"
    assert isinstance(answers["mood"], ScoreAnswer) and answers["mood"].score == pytest.approx(1.2)


async def test_noul_without_criteria_leaves_them_out():
    assert noul("Is it raining?") == {"type": "noul", "instructions": "Is it raining?"}


def test_score_needs_two_to_ten_levels():
    with pytest.raises(ValueError):
        score("x", ["only one"])


async def test_an_error_status_names_the_url_and_status():
    client = FakeDecisions(status=503).client()
    with pytest.raises(DecisionError, match=r"jev\.test/v1/decisions answered 503: model not loaded"):
        await client.decide("s", {"q": noul("?")})


async def test_a_missing_answer_is_an_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": "x", "answers": {}})

    client = DecisionClient("http://jev.test/v1/decisions", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(DecisionError, match="did not answer question 'q'"):
        await client.decide("s", {"q": noul("?")})


async def test_an_unreachable_server_says_where_it_looked():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = DecisionClient("http://localhost:8080/v1/decisions", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(DecisionError, match="cannot reach the decision model at http://localhost:8080/v1/decisions"):
        await client.decide("s", {"q": noul("?")})


async def test_chat_uses_the_first_listed_model_and_drops_thinking():
    fake = FakeChat(lambda messages: "<think>let me see</think>\nHello there", models=("qwen-local", "other"))
    chat = fake.client()

    assert await chat.complete([{"role": "user", "content": "hi"}]) == "Hello there"
    assert fake.requests[0]["model"] == "qwen-local"
    assert fake.requests[0]["stream"] is False


async def test_chat_asks_once_more_when_the_reply_is_not_json():
    replies = iter(["Sure! Here it is", '```json\n{"type": "question"}\n```'])
    fake = FakeChat(lambda messages: next(replies))

    assert await fake.client("m").complete_json([{"role": "user", "content": "json please"}]) == {"type": "question"}
    assert fake.requests[1]["messages"][-1]["content"].startswith("That was not valid JSON")


async def test_chat_gives_up_after_the_second_non_json_reply():
    with pytest.raises(ChatError, match="did not reply with JSON"):
        await FakeChat(lambda messages: "no").client("m").complete_json([{"role": "user", "content": "x"}])


def test_strip_thinking_handles_a_template_that_opened_the_tag_itself():
    assert strip_thinking("reasoning here</think>\nanswer") == "\nanswer"


def test_extract_json_finds_the_object_inside_prose():
    assert extract_json('Here you go: {"a": {"b": 1}} hope that helps') == {"a": {"b": 1}}
    with pytest.raises(ValueError):
        extract_json("[1, 2]")


def _check_args(**overrides) -> argparse.Namespace:
    values = dict(
        decisions_url="http://jev.test/v1/decisions",
        decisions_model="typesafe/jev-1.14",
        chat_url="http://chat.test/v1",
        chat_model=None,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


async def test_check_reports_both_models(monkeypatch, capsys):
    decisions = FakeDecisions(lambda key, q, state: 0.97)
    chat = FakeChat(lambda messages: "OK")
    monkeypatch.setattr(live_cli, "_clients", lambda args: (decisions.client(args.decisions_url), chat.client()))

    assert await live_cli.check(_check_args()) is True

    out = capsys.readouterr().out
    assert "chat       ok" in out and "model local-chat" in out
    assert "decisions  ok" in out and "p(yes)=0.97" in out


async def test_check_finds_the_decisions_api_on_another_path(monkeypatch, capsys):
    decisions = FakeDecisions(lambda key, q, state: 0.9, path="/api/alpha/decisions")
    monkeypatch.setattr(
        live_cli, "_clients", lambda args: (decisions.client(args.decisions_url), FakeChat().client())
    )

    assert await live_cli.check(_check_args()) is False

    out = capsys.readouterr().out
    assert "decisions  FAIL" in out
    assert "pass --decisions-url http://jev.test/api/alpha/decisions" in out
