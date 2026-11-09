"""A client for an OpenAI-compatible chat endpoint (mlx_lm, vLLM, LM Studio, Ollama).

Local reasoning models often put their thinking in the reply as ``<think>...</think>``;
it is stripped, so callers only ever see the answer.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

DEFAULT_CHAT_URL = "http://localhost:8000/v1"
# Generous, because a reasoning model spends part of it thinking before it answers.
MAX_TOKENS = 2048

_THINK = re.compile(r"<think>.*?</think>", re.S)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)


class ChatError(RuntimeError):
    pass


class ChatClient:
    def __init__(
        self,
        base_url: str = DEFAULT_CHAT_URL,
        model: str | None = None,
        *,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 180.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def model_id(self) -> str:
        """The configured model, or the first one the server lists."""
        if self.model:
            return self.model
        data = await self._request("GET", "/models")
        models = data.get("data") or data.get("models") or []
        ids = [m.get("id") for m in models if isinstance(m, dict) and m.get("id")]
        if not ids:
            raise ChatError(f"{self.base_url}/models lists no models; pass --chat-model")
        self.model = ids[0]
        return self.model

    async def complete(
        self, messages: list[dict[str, str]], *, temperature: float = 0.3, max_tokens: int = MAX_TOKENS
    ) -> str:
        body = {
            "model": await self.model_id(),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        data = await self._request("POST", "/chat/completions", json=body)
        try:
            content = data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ChatError(f"unexpected reply from {self.base_url}: {str(data)[:300]}") from exc
        return strip_thinking(content).strip()

    async def complete_json(self, messages: list[dict[str, str]], **kwargs: Any) -> dict[str, Any]:
        """A reply parsed as one JSON object, asking once more if the first is not one."""
        reply = await self.complete(messages, **kwargs)
        try:
            return extract_json(reply)
        except ValueError:
            retry = [
                *messages,
                {"role": "assistant", "content": reply},
                {"role": "user", "content": "That was not valid JSON. Reply with only the JSON object."},
            ]
            reply = await self.complete(retry, **kwargs)
            try:
                return extract_json(reply)
            except ValueError as exc:
                raise ChatError(f"the chat model did not reply with JSON: {reply[:300]}") from exc

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        url = self.base_url + path
        try:
            response = await self._client.request(method, url, headers=self._headers, **kwargs)
        except httpx.HTTPError as exc:
            raise ChatError(f"cannot reach the chat model at {url}: {exc}") from exc
        if response.status_code >= 400:
            raise ChatError(f"{url} answered {response.status_code}: {response.text[:300]}")
        try:
            return response.json()
        except ValueError as exc:
            raise ChatError(f"{url} did not answer JSON: {response.text[:300]}") from exc


def strip_thinking(text: str) -> str:
    text = _THINK.sub("", text)
    # Some chat templates open <think> in the prompt, so only the close tag arrives.
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    return text


def extract_json(text: str) -> dict[str, Any]:
    """The first JSON object in a reply, fenced or surrounded by prose."""
    text = _FENCE.sub("", strip_thinking(text)).strip()
    decoder = json.JSONDecoder()
    for start in (i for i, ch in enumerate(text) if ch == "{"):
        try:
            value, _ = decoder.raw_decode(text, start)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("no JSON object in reply")
