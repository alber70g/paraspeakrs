"""A client for the JEV Decisions API.

JEV (TypeSafe's "System One" decision model) takes a ``state`` and a map of typed
questions and answers each with probabilities, never with text::

    POST /v1/decisions
    {"model": "typesafe/jev-1.14",
     "state": {...},
     "questions": {"urgent": {"type": "noul", "instructions": "...",
                              "criteria": {"true": "...", "false": "..."}}}}
    -> {"model": "jev-1.14.0", "answers": {"urgent": {"type": "noul", "noul": 0.96}}, "usage": {...}}

The same shape is served by OpenRouter (``/api/alpha/decisions``), AI/ML API
(``/v1/decisions``) and local JEV-compatible servers, so the URL is the whole
configuration. Box questions (images) are left out: a meeting transcript has none.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_DECISIONS_URL = "http://localhost:8001/v1/decisions"
DEFAULT_DECISIONS_MODEL = "typesafe/jev-1.14"
# Paths other servers use for the same API, tried by `paraspeakrs live check`
# when the configured one answers 404.
KNOWN_PATHS = ("/v1/decisions", "/decisions", "/api/alpha/decisions", "/api/v1/decisions")


class DecisionError(RuntimeError):
    pass


@dataclass(frozen=True)
class NoulAnswer:
    """A yes/no answer: the probability of yes."""

    p_yes: float


@dataclass(frozen=True)
class ChoiceAnswer:
    choice: str
    confidence: float
    probabilities: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class ScoreAnswer:
    """The expected level on the scale, lowest level 0, possibly fractional."""

    score: float
    confidence: float
    probabilities: dict[str, float] = field(default_factory=dict)


Answer = NoulAnswer | ChoiceAnswer | ScoreAnswer


def noul(instructions: str, *, true: str | None = None, false: str | None = None) -> dict[str, Any]:
    question: dict[str, Any] = {"type": "noul", "instructions": instructions}
    criteria = {k: v for k, v in (("true", true), ("false", false)) if v}
    if criteria:
        question["criteria"] = criteria
    return question


def choice(instructions: str, criteria: dict[str, str | None]) -> dict[str, Any]:
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def score(instructions: str, levels: list[str]) -> dict[str, Any]:
    if not 2 <= len(levels) <= 10:
        raise ValueError("a score question takes 2 to 10 levels")
    return {"type": "score", "instructions": instructions, "criteria": levels}


class DecisionClient:
    def __init__(
        self,
        url: str = DEFAULT_DECISIONS_URL,
        model: str = DEFAULT_DECISIONS_MODEL,
        *,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.url = url
        self.model = model
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def decide(self, state: Any, questions: dict[str, dict[str, Any]]) -> dict[str, Answer]:
        payload = {"model": self.model, "state": state, "questions": questions}
        try:
            response = await self._client.post(self.url, json=payload, headers=self._headers)
        except httpx.HTTPError as exc:
            raise DecisionError(f"cannot reach the decision model at {self.url}: {exc}") from exc
        if response.status_code >= 400:
            raise DecisionError(f"{self.url} answered {response.status_code}: {response.text[:300]}")
        try:
            answers = response.json()["answers"]
        except (ValueError, KeyError, TypeError) as exc:
            raise DecisionError(f"{self.url} sent no `answers`: {response.text[:300]}") from exc
        out: dict[str, Answer] = {}
        for key, question in questions.items():
            if key not in answers:
                raise DecisionError(f"{self.url} did not answer question {key!r}")
            out[key] = _parse(question["type"], answers[key])
        return out


def _parse(kind: str, raw: dict[str, Any]) -> Answer:
    try:
        if kind == "noul":
            return NoulAnswer(p_yes=float(raw["noul"]))
        if kind == "choice":
            return ChoiceAnswer(
                choice=str(raw["choice"]),
                confidence=float(raw.get("confidence", 0.0)),
                probabilities={k: float(v) for k, v in raw.get("probabilities", {}).items()},
            )
        if kind == "score":
            return ScoreAnswer(
                score=float(raw["score"]),
                confidence=float(raw.get("confidence", 0.0)),
                probabilities={k: float(v) for k, v in raw.get("probabilities", {}).items()},
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise DecisionError(f"malformed {kind} answer: {raw!r}") from exc
    raise DecisionError(f"unsupported question type {kind!r}")
