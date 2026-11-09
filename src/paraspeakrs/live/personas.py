"""Personas kept between meetings, and a questionnaire that proposes new ones mid-meeting.

The library is one JSON file per persona in ``<workspace>/live/personas/``, plus the
built-in presets. A saved persona with a preset's key replaces that preset; deleting
it brings the preset back.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .brief import Brief, Persona, persona_key, slug
from .chat import ChatClient
from .intake import IntakeQuestion, ask_json, dump, parse_question

PRESETS = [
    Persona(
        key="facilitator",
        name="Facilitator",
        viewpoint="helps the user steer the meeting to clear outcomes",
        comment_when=(
            "An action or decision is agreed without a named owner or date; a question is left open and the talk "
            "moves on; the talk drifts away from the agenda or the purpose of the meeting; a participant whose area "
            "is being discussed has not said anything about it."
        ),
        skip_when="Owners and dates are named, open questions get answers, and the talk stays on the agenda.",
        style="One question the user can ask out loud now to steer the meeting, in one sentence. No explanation.",
    ),
]

MAX_QUESTIONS = 3
TRANSCRIPT_CHARS = 6000

SYSTEM_PROMPT = """\
You propose new personas for a live meeting assistant. The user asks for them before or during the meeting.

A persona is a viewpoint the meeting is watched from. While the meeting runs, a decision model reads each new \
part of the transcript and answers per persona one yes/no question with a probability: "Do the newest lines give \
this persona something to comment on?" It judges only against the persona's comment_when and skip_when, so write \
them as things a reader of the transcript can check, for example "a deadline or budget is agreed without naming \
who owns it", not "something important happens". A chat model then writes the comment in the persona's style.

Propose personas that fit this meeting and what was said so far, and that add a viewpoint the current personas \
do not cover. Follow what the user asks for in their messages.

You may ask the user up to {max_questions} questions first, one per turn, but only when the answer changes which \
personas you propose; often none is needed. A question is multiple choice: 2 to 4 concrete options. Ask in the \
language the user writes in.

Reply with exactly one JSON object and nothing else, in one of two shapes.

A question:
{{"type": "question", "question": "...", "options": ["...", "..."], "why": "one short line on what the answer changes"}}

The proposals, 1 to 3 personas:
{{"type": "personas", "personas": [{{"name": "...", "viewpoint": "...", "comment_when": "...", "skip_when": "...", "style": "..."}}]}}

style says how the persona's comments sound, for example "one pointed question, at most two sentences".

{meeting}"""

FINISH_NOW = "Propose the personas now with what you know."


class PersonaLibrary:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def list(self) -> list[dict]:
        """Every persona in the library: ``{"persona": ..., "builtin": bool}``, presets first."""
        saved = {p.key: p for p in self._saved()}
        out = []
        for preset in PRESETS:
            own = saved.pop(preset.key, None)
            out.append({"persona": (own or preset).model_dump(), "builtin": own is None})
        out += [{"persona": p.model_dump(), "builtin": False} for p in saved.values()]
        return out

    def save(self, persona: Persona) -> Persona:
        """Saves under the persona's key (or its name as a slug), replacing a persona with that key."""
        persona = persona.model_copy(update={"key": persona_key(persona, set())})
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{persona.key}.json"
        path.write_text(json.dumps(persona.model_dump(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return persona

    def delete(self, key: str) -> None:
        """Deletes a saved persona; KeyError when there is none with that key (presets cannot be deleted)."""
        path = self.directory / f"{key}.json"
        if key != slug(key) or not path.exists():  # a slug: never a path out of the directory
            raise KeyError(key)
        path.unlink()

    def _saved(self) -> list[Persona]:
        if not self.directory.exists():
            return []
        out = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                out.append(Persona.model_validate_json(path.read_text(encoding="utf-8")).model_copy(update={"key": path.stem}))
            except ValueError:
                continue  # a file edited by hand into something else: left alone, not listed
        return out


@dataclass
class PersonaTurn:
    """What the model did this turn, and the history to send back next turn."""

    history: list[dict[str, str]]
    question: IntakeQuestion | None = None
    personas: list[Persona] = field(default_factory=list)


async def next_persona_turn(
    chat: ChatClient,
    history: list[dict[str, str]],
    *,
    brief: Brief | None,
    transcript: str,
    max_questions: int = MAX_QUESTIONS,
) -> PersonaTurn:
    """Ask the model for a question, or for 1 to 3 proposed personas.

    ``history`` holds user and assistant messages only; the first user message says
    what the user wants. ``transcript`` is the meeting so far; only its end is sent.
    """
    if not history or history[-1]["role"] != "user":
        raise ValueError("the history must end with the user's message")
    system = SYSTEM_PROMPT.format(max_questions=max_questions, meeting=_meeting(brief, transcript))
    messages = [{"role": "system", "content": system}, *history]
    if sum(1 for m in history if m["role"] == "assistant") >= max_questions and history[-1]["content"] != FINISH_NOW:
        messages.append({"role": "user", "content": FINISH_NOW})
    return await ask_json(chat, messages, lambda reply: _turn(history, reply), what="persona questionnaire")


def _meeting(brief: Brief | None, transcript: str) -> str:
    parts = [f"About this meeting:\n{brief.context_text()}" if brief else "There is no brief for this meeting yet."]
    if brief and brief.personas:
        parts.append("Its personas now:\n" + "\n".join(f"- {p.name}: {p.viewpoint or p.comment_when}" for p in brief.personas))
    if transcript.strip():
        text = transcript if len(transcript) <= TRANSCRIPT_CHARS else "…" + transcript[-TRANSCRIPT_CHARS:]
        parts.append(f"The transcript so far, most recent last:\n{text}")
    else:
        parts.append("Nothing has been said yet.")
    return "\n\n".join(parts)


def _turn(history: list[dict[str, str]], reply: dict) -> PersonaTurn:
    new_history = [*history, {"role": "assistant", "content": dump(reply)}]
    if reply.get("type") == "personas" or (reply.get("type") is None and "personas" in reply):
        personas = [Persona.model_validate(p) for p in reply.get("personas") or []][:3]
        if not personas:
            raise ValueError("propose at least one persona")
        # Keys only to tell the proposals apart; adding one to a meeting picks its key there.
        taken: set[str] = set()
        for persona in personas:
            persona.key = persona_key(persona, taken)
            taken.add(persona.key)
        return PersonaTurn(history=new_history, personas=personas)
    if question := parse_question(reply):
        return PersonaTurn(history=new_history, question=question)
    raise ValueError("type must be 'question' or 'personas'")
