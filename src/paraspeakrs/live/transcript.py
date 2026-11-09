"""Transcript lines, and a replay that hands them out as if the meeting were live."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..transcript_txt import format_time

_TXT_LINE = re.compile(r"^\[(\d+):(\d{2}):(\d{2})\]\s*([^:]+?):\s*(.*)$")
# Spoken Dutch and English both run at roughly 2.5 words a second.
_SECONDS_PER_WORD = 0.4


@dataclass(frozen=True)
class Line:
    start: float
    end: float
    speaker: str
    text: str

    def render(self) -> str:
        return f"[{format_time(self.start)}] {self.speaker}: {self.text}"

    def to_json(self) -> dict:
        return {"start": round(self.start, 2), "end": round(self.end, 2), "speaker": self.speaker, "text": self.text}


def render(lines: Sequence[Line]) -> str:
    return "\n".join(line.render() for line in lines)


def load_lines(path: Path) -> list[Line]:
    """Lines from a paraspeakrs transcript: the .txt it writes, or its JSON result."""
    text = path.read_text(encoding="utf-8")
    lines = _from_json(text) if path.suffix.lower() == ".json" else _from_txt(text)
    if not lines:
        raise ValueError(f"no transcript lines found in {path}")
    return lines


def _from_txt(text: str) -> list[Line]:
    body = text
    if body.startswith("---\n"):
        end = body.find("\n---", 4)
        if end != -1:
            body = body[end + 4 :]
    parsed: list[tuple[float, str, str]] = []
    for raw in body.splitlines():
        match = _TXT_LINE.match(raw.strip())
        if match:
            h, m, s, speaker, said = match.groups()
            parsed.append((int(h) * 3600 + int(m) * 60 + int(s), speaker.strip(), said.strip()))
    lines = []
    for i, (start, speaker, said) in enumerate(parsed):
        guess = start + max(1.0, len(said.split()) * _SECONDS_PER_WORD)
        end = min(guess, parsed[i + 1][0]) if i + 1 < len(parsed) and parsed[i + 1][0] > start else guess
        lines.append(Line(start=float(start), end=float(end), speaker=speaker, text=said))
    return [line for line in lines if line.text]


def _from_json(text: str) -> list[Line]:
    from ..models import TranscriptionResult
    from ..transcript_txt import utterances

    data = json.loads(text)
    # `paraspeakrs run` prints a TranscriptionResult; a stored job wraps it in "result".
    result = TranscriptionResult.model_validate(data.get("result", data))
    return [Line(u.start, u.end, u.name, u.text) for u in utterances(result)]


async def replay(lines: Sequence[Line], *, speed: float, step: float) -> AsyncIterator[list[Line]]:
    """The lines in batches of about ``step`` seconds of meeting, on a clock ``speed`` times real time.

    A batch is handed out when the meeting clock passes the end of its last line,
    as it would be when transcribed live. Time spent by the consumer counts against
    the clock, so slow analysis falls behind just as it would in a real meeting.
    ``speed`` 0 hands everything out without waiting.
    """
    if not lines:
        return
    origin = lines[0].start
    wall_start = time.monotonic()
    batch: list[Line] = []
    batch_start = origin
    for line in lines:
        batch.append(line)
        if line.end - batch_start >= step:
            await _wait_until(wall_start, line.end - origin, speed)
            yield batch
            batch, batch_start = [], line.end
    if batch:
        await _wait_until(wall_start, batch[-1].end - origin, speed)
        yield batch


async def _wait_until(wall_start: float, meeting_seconds: float, speed: float) -> None:
    if speed <= 0:
        await asyncio.sleep(0)
        return
    delay = wall_start + meeting_seconds / speed - time.monotonic()
    if delay > 0:
        await asyncio.sleep(delay)
