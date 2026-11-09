"""The questionnaire that sets up a meeting: a chat model interviews the user.

The model leads. Each turn it either asks one multiple-choice question (the user may
also answer in their own words) or, once it knows enough, writes the brief. The
history lives with the caller, so a web page can hold it and post it back each turn.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from pydantic import ValidationError

from .brief import Brief
from .chat import ChatClient, ChatError

MAX_QUESTIONS = 10

SYSTEM_PROMPT = """\
You prepare a live meeting assistant for one specific meeting by interviewing the user before it starts.

Two models will work from what you find out while the meeting runs:
- A decision model reads the transcript as it comes in. For each persona it answers one yes/no question with a probability: "Do the newest lines give this persona something to comment on?" It judges only against the criteria you write, so write criteria that can be checked by reading a transcript.
- A chat model writes the comments in each persona's voice and answers the user's questions about the meeting. It knows only the background you write down.

Your job is to find the right angles of approach, not to fill in a form. Find out:
1. What the meeting is for, and what a good outcome looks like for the user.
2. Who takes part, their roles, and what each of them wants from it.
3. From which viewpoints the meeting should be watched: the personas. Propose viewpoints the user may not have thought of, based on what you learn, such as a risk they are exposed to or a stakeholder who is not in the room.
4. For each persona: what in the conversation deserves a comment, and what does not, so the comments stay few and useful.
5. What counts as a new section or topic in this meeting.
6. Background the chat model needs: jargon, earlier decisions, numbers, constraints.
7. The language the meeting is held in.

How to ask:
- One question per turn, multiple choice: 2 to 4 concrete options that are real alternatives, built on the earlier answers. The user can always answer in their own words instead.
- Ask only what changes the criteria or the background. Infer what you can and do not ask again for what you were told.
- Ask in the language the user writes in.
- Stop when you know enough, and at the latest after {max_questions} questions, and write the brief.

Reply with exactly one JSON object and nothing else, in one of two shapes.

A question:
{{"type": "question", "question": "...", "options": ["...", "..."], "why": "one short line on what the answer changes"}}

The finished brief:
{{"type": "brief", "brief": {{
  "title": "...", "language": "...", "purpose": "...", "context": "...", "section_rule": "...",
  "agenda": ["..."],
  "participants": [{{"name": "...", "role": "...", "goals": "..."}}],
  "personas": [{{"name": "...", "viewpoint": "...", "comment_when": "...", "skip_when": "...", "style": "..."}}]
}}}}

Write comment_when and skip_when as things a reader of the transcript can check, for example \
"a deadline or budget is agreed without naming who owns it", not "something important happens". \
Give 1 to 4 personas. style says how that persona's comments sound, for example "one pointed question, at most two sentences".
"""

FINISH_NOW = "Write the brief now with what you know."


@dataclass
class IntakeQuestion:
    question: str
    options: list[str] = field(default_factory=list)
    why: str = ""


@dataclass
class IntakeTurn:
    """What the model did this turn, and the history to send back next turn."""

    history: list[dict[str, str]]
    question: IntakeQuestion | None = None
    brief: Brief | None = None


async def next_turn(chat: ChatClient, history: list[dict[str, str]], *, max_questions: int = MAX_QUESTIONS) -> IntakeTurn:
    """Ask the model for its next question, or for the brief.

    ``history`` holds user and assistant messages only; the first user message
    describes the meeting in the user's own words.
    """
    if not history or history[-1]["role"] != "user":
        raise ValueError("the history must end with the user's message")
    asked = sum(1 for m in history if m["role"] == "assistant")
    messages = [{"role": "system", "content": SYSTEM_PROMPT.format(max_questions=max_questions)}, *history]
    if asked >= max_questions and history[-1]["content"] != FINISH_NOW:
        messages.append({"role": "user", "content": FINISH_NOW})

    reply = await chat.complete_json(messages, temperature=0.4)
    try:
        return _turn(history, reply)
    except (ValidationError, ValueError) as exc:
        # One correction round: local models get the shape right more often than not.
        retry = [
            *messages,
            {"role": "assistant", "content": _dump(reply)},
            {"role": "user", "content": f"That reply does not fit the required shape ({exc}). Send it again, fixed."},
        ]
        reply = await chat.complete_json(retry, temperature=0.2)
        try:
            return _turn(history, reply)
        except (ValidationError, ValueError) as exc2:
            raise ChatError(f"the questionnaire model sent an unusable reply: {exc2}") from exc2


def _turn(history: list[dict[str, str]], reply: dict) -> IntakeTurn:
    kind = reply.get("type")
    new_history = [*history, {"role": "assistant", "content": _dump(reply)}]
    if kind == "brief" or (kind is None and "brief" in reply):
        return IntakeTurn(history=new_history, brief=Brief.model_validate(reply["brief"]))
    if kind == "question" or (kind is None and "question" in reply):
        question = str(reply.get("question", "")).strip()
        if not question:
            raise ValueError("the question is empty")
        options = [str(o).strip() for o in reply.get("options") or [] if str(o).strip()][:4]
        return IntakeTurn(
            history=new_history,
            question=IntakeQuestion(question=question, options=options, why=str(reply.get("why", "")).strip()),
        )
    raise ValueError("type must be 'question' or 'brief'")


def _dump(reply: dict) -> str:
    return json.dumps(reply, ensure_ascii=False)
