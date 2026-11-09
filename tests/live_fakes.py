"""Stand-ins for a JEV decision server and an OpenAI-compatible chat server.

Both are httpx transports, so the real clients run unchanged against them, and both
record what they were sent.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx

from paraspeakrs.live.chat import ChatClient
from paraspeakrs.live.decisions import DecisionClient

# (question key, question, state) -> p(yes) for a noul, or the chosen option for a choice
Rule = Callable[[str, dict, object], float]
Choose = Callable[[str, dict, object], str]


class FakeDecisions:
    def __init__(
        self, rule: Rule | None = None, *, choose: Choose | None = None, path: str = "/v1/decisions", status: int = 200
    ) -> None:
        self.rule = rule or (lambda key, question, state: 0.0)
        self.choose = choose or (lambda key, question, state: next(iter(question["criteria"])))
        self.path = path
        self.status = status
        self.requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != self.path:
            return httpx.Response(404, json={"error": "not found"})
        if self.status != 200:
            return httpx.Response(self.status, text="model not loaded")
        body = json.loads(request.content)
        self.requests.append(body)
        answers = {}
        for key, question in body["questions"].items():
            if question["type"] == "noul":
                answers[key] = {"type": "noul", "noul": self.rule(key, question, body["state"])}
            elif question["type"] == "choice":
                picked = self.choose(key, question, body["state"])
                rest = 0.1 / max(1, len(question["criteria"]) - 1)
                answers[key] = {
                    "type": "choice",
                    "choice": picked,
                    "confidence": 0.9,
                    "probabilities": {o: (0.9 if o == picked else rest) for o in question["criteria"]},
                }
            elif question["type"] == "score":
                answers[key] = {
                    "type": "score",
                    "score": 1.2,
                    "confidence": 0.7,
                    "legend": {str(i): c for i, c in enumerate(question["criteria"])},
                    "probabilities": {str(i): 1 / len(question["criteria"]) for i in range(len(question["criteria"]))},
                }
        return httpx.Response(
            200, json={"model": "jev-fake", "answers": answers, "usage": {"input_tokens": 1, "output_tokens": 1}}
        )

    def client(self, url: str = "http://jev.test/v1/decisions") -> DecisionClient:
        return DecisionClient(url, "typesafe/jev-1.14", client=httpx.AsyncClient(transport=httpx.MockTransport(self.handler)))


class FakeChat:
    def __init__(self, reply: Callable[[list[dict]], str] | None = None, *, models: tuple[str, ...] = ("local-chat",)) -> None:
        self.reply = reply or (lambda messages: "ok")
        self.models = models
        self.requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": [{"id": m} for m in self.models]})
        body = json.loads(request.content)
        self.requests.append(body)
        content = self.reply(body["messages"])
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}]})

    def client(self, model: str | None = None) -> ChatClient:
        return ChatClient("http://chat.test/v1", model, client=httpx.AsyncClient(transport=httpx.MockTransport(self.handler)))


def last_user(messages: list[dict]) -> str:
    return next(m["content"] for m in reversed(messages) if m["role"] == "user")


def meeting_chat(messages: list[dict]) -> str:
    """Replies the way a cooperative model would to each prompt the analysis sends."""
    prompt = last_user(messages)
    if prompt.startswith("Give this part of a meeting a title"):
        return "<think>short</think>Budget review"
    if prompt.startswith("Transcript so far"):
        return "The launch moved to March."
    return "Who owns the March date?"
