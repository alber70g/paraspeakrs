"""The brief: what the live analysis knows about one meeting.

The questionnaire in intake.py fills it in. Two models read it: the decision model
gets each persona's ``comment_when`` and ``skip_when`` as the true and false
criteria of a yes/no question, and the chat model gets the rest as background.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel, Field, model_validator


class Participant(BaseModel):
    """Someone in the meeting, so comments and answers know who wants what."""

    name: str
    role: str = ""
    goals: str = ""


class Persona(BaseModel):
    """A viewpoint the meeting is watched from, and when that viewpoint speaks up."""

    key: str = ""
    name: str
    viewpoint: str = ""
    comment_when: str
    skip_when: str = ""
    style: str = ""


class Brief(BaseModel):
    title: str = "Meeting"
    # Empty means: write in whatever language the meeting is held in.
    language: str = ""
    purpose: str = ""
    # Background only the chat model reads: jargon, earlier decisions, numbers.
    context: str = ""
    # What counts as a new section in this meeting, e.g. "each customer ticket".
    section_rule: str = ""
    agenda: list[str] = Field(default_factory=list)
    participants: list[Participant] = Field(default_factory=list)
    personas: list[Persona] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_persona_keys(self) -> Brief:
        """Every persona gets a key that is safe in a question name and unique."""
        seen: set[str] = set()
        for persona in self.personas:
            persona.key = persona_key(persona, seen)
            seen.add(persona.key)
        return self

    def persona(self, key: str) -> Persona:
        for persona in self.personas:
            if persona.key == key:
                return persona
        raise KeyError(key)

    def meeting_state(self) -> dict:
        """The part of the brief the decision model sees in every request's state.

        Short on purpose: JEV moves requests over about 32 KB of text to a slower,
        less accurate long-context mode, and the transcript needs that room.
        """
        state: dict = {"title": self.title}
        if self.purpose:
            state["purpose"] = self.purpose
        if self.agenda:
            state["agenda"] = self.agenda
        if self.participants:
            state["participants"] = [
                {k: v for k, v in p.model_dump().items() if v} for p in self.participants
            ]
        return state

    def context_text(self) -> str:
        """The brief as background for a chat model's system prompt."""
        parts = [f"Meeting: {self.title}"]
        if self.purpose:
            parts.append(f"Purpose: {self.purpose}")
        if self.agenda:
            parts.append("Agenda:\n" + "\n".join(f"- {item}" for item in self.agenda))
        if self.participants:
            people = []
            for p in self.participants:
                line = p.name + (f" ({p.role})" if p.role else "")
                if p.goals:
                    line += f": wants {p.goals}"
                people.append(f"- {line}")
            parts.append("Participants:\n" + "\n".join(people))
        if self.context:
            parts.append(f"Background:\n{self.context}")
        return "\n\n".join(parts)


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]


def persona_key(persona: Persona, taken: set[str]) -> str:
    """The persona's key or name as a slug, numbered when it is taken."""
    base = slug(persona.key or persona.name) or "persona"
    key, n = base, 2
    while key in taken:
        key, n = f"{base}-{n}", n + 1
    return key


def load_brief(path: Path) -> Brief:
    return Brief.model_validate_json(path.read_text(encoding="utf-8"))


def save_brief(brief: Brief, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(brief.model_dump(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
