"""The analysis: sections by topic, comments per persona, answers about the meeting.

Each batch of new lines costs at most two decision calls, which JEV answers in well
under a second: one asks whether the topic changed and on which line, the other asks
every active persona at once whether the new lines give it something to say, and
whether they take a decision or agree on a to-do. Only a yes costs a chat call.

Personas can be added, paused and removed while the meeting runs. One added with a
rerun also goes over what was said before it joined, a ``rerun_step`` of meeting at
a time, and sees only what had been said up to each step.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .brief import Brief, Persona, persona_key
from .chat import ChatClient, ChatError
from .decisions import Answer, ChoiceAnswer, DecisionClient, DecisionError, NoulAnswer, choice, noul
from .intake import ask_json
from .items import KINDS, Item, ItemList, parse_updates
from .transcript import Line, render
from ..transcript_txt import format_time

Emit = Callable[[dict], None]

TOPIC_TRUE = "The new lines start a different subject, agenda item, problem or decision than the current section."
TOPIC_FALSE = "The new lines continue, deepen or briefly digress from the subject of the current section."
SKIP_DEFAULT = "Nothing in the new lines meets the comment criteria, or it was already commented on."
# Asked with the comment questions of every live batch, never in a rerun.
OUTCOME_QUESTIONS = {
    "outcome__decision": noul(
        "Do `new_lines` take a decision? Read `section_so_far` for context.",
        true=(
            "In `new_lines` the participants settle something: they choose between options, agree on a plan, "
            "date, number or approach, or confirm or change a decision taken earlier."
        ),
        false=(
            "`new_lines` only propose, discuss, ask or weigh options, or repeat a decision already taken "
            "in `section_so_far` without changing it."
        ),
    ),
    "outcome__todo": noul(
        "Do `new_lines` agree on a task someone will do? Read `section_so_far` for context.",
        true=(
            "In `new_lines` someone takes on or is given a concrete task to do after this moment, "
            "or the owner or the date of a task agreed earlier is named or changed."
        ),
        false=(
            "`new_lines` mention work only in general, describe work already done, "
            "or wish for something without anyone agreeing to do it."
        ),
    ),
}
ITEMS_SYSTEM = """\
You keep the list of decisions and to-dos of a meeting that is still going on.
A decision is something the participants settled: a choice, a plan, a date, a number.
A to-do is a task someone agreed to do. Its owner is the person who will do it, named as in the transcript; \
its due date is the date or moment as it was said, such as "Friday" or "before the release". \
Leave owner or due empty when they were not said.

Read the new lines and reply with exactly one JSON object, the items they add or change:
{{"items": [{{"id": "D1", "kind": "decision", "text": "...", "owner": "", "due": ""}}]}}
- kind is "decision" or "todo".
- To change an item on the current list, for example a new owner or a moved date, send it in full with its id.
- For a new item, leave out the id. Do not add an item that is already on the list.
- Do not send items that did not change, and never add again an item the user removed.
- Write each text as one short sentence in {language}.
- Reply {{"items": []}} when the new lines add or change nothing.

About this meeting:
{context}"""


@dataclass
class Thresholds:
    """Where a probability becomes a yes. Re-pick these for every decision model."""

    topic: float = 0.6
    comment: float = 0.5
    # p(decision) or p(to-do) that has the chat model update the items.
    item: float = 0.5
    # A section shorter than this is never closed by a topic change: one stray
    # sentence should not become a section of its own.
    min_section_seconds: float = 60.0
    # Long sections are closed regardless, so the state sent stays small.
    max_section_seconds: float = 900.0


@dataclass
class Section:
    index: int
    lines: list[Line]
    title: str = ""
    closed: bool = False

    @property
    def start(self) -> float:
        return self.lines[0].start

    @property
    def end(self) -> float:
        return self.lines[-1].end

    @property
    def duration(self) -> float:
        return self.end - self.start

    def text(self, max_chars: int) -> str:
        return _text(self.lines, max_chars)


def _text(lines: Sequence[Line], max_chars: int) -> str:
    """The transcript of ``lines``, cut from the front when it is too long."""
    text = render(lines)
    return text if len(text) <= max_chars else "…" + text[-max_chars:]


class Analyzer:
    def __init__(
        self,
        brief: Brief,
        decisions: DecisionClient,
        chat: ChatClient,
        emit: Emit,
        *,
        thresholds: Thresholds | None = None,
        state_chars: int = 6000,
        ask_chars: int = 40000,
        rerun_step: float = 30.0,
    ) -> None:
        self.brief = brief
        self.decisions = decisions
        self.chat = chat
        self.emit = emit
        self.thresholds = thresholds or Thresholds()
        self.state_chars = state_chars
        self.ask_chars = ask_chars
        self.rerun_step = rerun_step
        self.sections: list[Section] = []
        self.comments: list[dict] = []
        self.items = ItemList()
        self.paused: set[str] = set()
        self._background: set[asyncio.Task] = set()
        # Per persona: its rerun, and the progress last reported.
        self._reruns: dict[str, tuple[asyncio.Task, dict]] = {}
        self._last_error: dict[str, str] = {}

    @property
    def current(self) -> Section | None:
        if self.sections and not self.sections[-1].closed:
            return self.sections[-1]
        return None

    async def feed(self, lines: Sequence[Line]) -> None:
        """Analyze lines that just arrived, in meeting order."""
        lines = list(lines)
        if not lines:
            return
        topic_p: float | None = None
        section = self.current
        if section is None:
            section = self._open(lines, "start")
        elif section.duration >= self.thresholds.max_section_seconds:
            self._close(section)
            section = self._open(lines, "length")
        else:
            shift = None
            if section.duration >= self.thresholds.min_section_seconds:
                shift = await self._topic_shift(section, lines)
            if shift is not None:
                topic_p = shift[0]
            if shift is not None and shift[0] >= self.thresholds.topic:
                # The batch is cut where the new topic starts, not at its first line.
                head, lines = lines[: shift[1]], lines[shift[1] :]
                if head:
                    section.lines.extend(head)
                    await self._assess(section, head, None)
                self._close(section)
                section = self._open(lines, "topic")
            else:
                section.lines.extend(lines)
        await self._assess(section, lines, topic_p)

    async def _assess(self, section: Section, lines: list[Line], topic_p: float | None) -> None:
        comment_p, item_p = await self._consider_comments(section, lines)
        event = {
            "type": "decision", "section": section.index, "at": lines[-1].end, "topic": topic_p, "comments": comment_p
        }
        if item_p is not None:
            event["items"] = item_p
        self.emit(event)

    async def finish(self) -> None:
        """The meeting ended: close the last section and wait for pending titles and reruns."""
        if self.current is not None:
            self._close(self.current)
        while self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)

    async def ask(self, question: str, lines: Sequence[Line]) -> str:
        """Answer a question from everything said so far, including lines not yet analyzed."""
        system = (
            "You answer questions about a meeting that is still going on, from its transcript so far. "
            "If the transcript does not hold the answer, say so. Be brief. "
            "Answer in the language the question is asked in.\n\n"
            f"About this meeting:\n{self.brief.context_text()}"
        )
        user = f"Transcript so far, most recent last:\n{self._ask_transcript(lines)}\n\nQuestion: {question}"
        answer = await self.chat.complete([{"role": "system", "content": system}, {"role": "user", "content": user}])
        self.emit({"type": "answer", "question": question, "answer": answer, "at": lines[-1].end if lines else 0})
        return answer

    # -- personas -----------------------------------------------------------

    def add_persona(self, persona: Persona, *, rerun: bool) -> Persona:
        """Add a persona from now on; with ``rerun``, it also goes over the meeting so far."""
        # Never the key of a persona that was removed: its comments stay hers.
        taken = {p.key for p in self.brief.personas} | {c["persona"] for c in self.comments}
        persona = persona.model_copy(update={"key": persona_key(persona, taken)})
        self.brief.personas.append(persona)
        self._personas_changed()
        if rerun and self.sections:
            # The sections as they are now; lines heard from here on reach it through feed.
            steps = [
                (section, section.lines[:end], section.lines[start:end])
                for section in self.sections
                for start, end in _steps(section.lines, self.rerun_step)
            ]
            progress = {"type": "rerun", "persona": persona.key, "of": len(steps), "done": 0, "state": "running"}
            self.emit(dict(progress))
            task = asyncio.create_task(self._rerun(persona, steps, progress))
            self._reruns[persona.key] = (task, progress)
            self._background.add(task)
            task.add_done_callback(self._background.discard)
        return persona

    def pause(self, key: str, paused: bool) -> None:
        """A paused persona is not asked; pausing also stops its rerun, and resuming goes on from now."""
        self.brief.persona(key)  # KeyError for an unknown key
        if paused:
            self.paused.add(key)
            self._stop_rerun(key)
        else:
            self.paused.discard(key)
        self._personas_changed()

    def remove(self, key: str) -> None:
        """The persona leaves the meeting; what it said stays."""
        self.brief.personas.remove(self.brief.persona(key))
        self.paused.discard(key)
        self._stop_rerun(key)
        self._personas_changed()

    def _stop_rerun(self, key: str) -> None:
        task, progress = self._reruns.pop(key, (None, None))
        if task is not None and not task.done():
            task.cancel()
            progress["state"] = "stopped"
            self.emit(dict(progress))

    def _personas_changed(self) -> None:
        self.emit(
            {
                "type": "personas",
                "personas": [p.model_dump() for p in self.brief.personas],
                "paused": sorted(self.paused),
            }
        )

    async def _rerun(self, persona: Persona, steps: list[tuple[Section, list[Line], list[Line]]], progress: dict) -> None:
        for section, so_far, lines in steps:
            await self._ask_personas([persona], section, so_far, lines, rerun=True)
            progress["done"] += 1
            if progress["done"] == progress["of"]:
                progress["state"] = "done"
            self.emit(dict(progress))

    # -- sections -----------------------------------------------------------

    def _open(self, lines: list[Line], reason: str) -> Section:
        section = Section(index=len(self.sections), lines=list(lines))
        self.sections.append(section)
        self.emit({"type": "section", "section": section.index, "start": section.start, "reason": reason})
        return section

    def _close(self, section: Section) -> None:
        section.closed = True
        self.emit({"type": "section_closed", "section": section.index, "end": section.end})
        task = asyncio.create_task(self._title(section))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _topic_shift(self, section: Section, lines: list[Line]) -> tuple[float, int] | None:
        """p(the topic changed), and the index of the first line on the new topic.

        Both are asked in one request: a yes/no question, and a choice between the
        new lines that only matters when the answer is yes.
        """
        state = {
            "meeting": self.brief.meeting_state(),
            "current_section": section.text(self.state_chars),
            "new_lines": render(lines),
        }
        true = TOPIC_TRUE
        if self.brief.section_rule:
            true += f" In this meeting a new section starts when: {self.brief.section_rule}"
        questions = {
            "topic_changed": noul(
                "Do `new_lines` move the meeting on to a different topic than `current_section`?",
                true=true,
                false=TOPIC_FALSE,
            )
        }
        if len(lines) > 1:
            questions["first_new_line"] = choice(
                "Suppose `new_lines` move on to a different topic than `current_section`: "
                "which line is the first one on the new topic?",
                {f"L{i}": line.render() for i, line in enumerate(lines)},
            )
        answers = await self._decide(state, questions)
        if not answers:
            return None
        first = 0
        picked = answers.get("first_new_line")
        if isinstance(picked, ChoiceAnswer) and picked.choice.removeprefix("L").isdigit():
            first = min(int(picked.choice.removeprefix("L")), len(lines) - 1)
        return _p(answers, "topic_changed"), first

    async def _title(self, section: Section) -> None:
        language = self.brief.language or "the language of the transcript"
        prompt = (
            f"Give this part of a meeting a title of 2 to 6 words, in {language}. "
            f"Reply with the title only.\n\n{section.text(self.state_chars)}"
        )
        reply = await self._chat([{"role": "user", "content": prompt}], max_tokens=1024)
        if reply:
            section.title = reply.splitlines()[0].strip().strip("\"'*#").strip()
            self.emit({"type": "title", "section": section.index, "title": section.title})

    # -- comments -----------------------------------------------------------

    async def _consider_comments(
        self, section: Section, lines: list[Line]
    ) -> tuple[dict[str, float], dict[str, float] | None]:
        active = [p for p in self.brief.personas if p.key not in self.paused]
        return await self._ask_personas(active, section, section.lines, lines, outcomes=True)

    async def _ask_personas(
        self,
        personas: list[Persona],
        section: Section,
        so_far: list[Line],
        lines: list[Line],
        *,
        rerun: bool = False,
        outcomes: bool = False,
    ) -> tuple[dict[str, float], dict[str, float] | None]:
        """One decision call for these personas about ``lines``; ``so_far`` is the section up to and with them.

        With ``outcomes`` the same call also asks whether the lines take a decision or
        agree on a to-do: p per kind, or None when that was not asked or not answered.
        """
        if not personas and not outcomes:
            return {}, None
        state: dict = {
            "meeting": self.brief.meeting_state(),
            "section_so_far": _text(so_far, self.state_chars),
            "new_lines": render(lines),
        }
        until = lines[-1].end
        already = {p.key: said for p in personas if (said := self._said(section, p.key, until))}
        if already:
            state["already_commented"] = already
        questions = {f"comment__{p.key}": self._comment_question(p, p.key in already) for p in personas}
        if outcomes:
            questions.update(OUTCOME_QUESTIONS)
        answers = await self._decide(state, questions)
        if not answers:
            return {}, None
        probabilities = {p.key: _p(answers, f"comment__{p.key}") for p in personas}
        speaking = [p for p in personas if probabilities[p.key] >= self.thresholds.comment]
        item_p = {kind: _p(answers, f"outcome__{kind}") for kind in KINDS} if outcomes else None
        update_items = item_p is not None and max(item_p.values()) >= self.thresholds.item
        await asyncio.gather(
            *(self._comment(p, section, so_far, lines, probabilities[p.key], rerun=rerun) for p in speaking),
            *([self._update_items(section, so_far, lines)] if update_items else []),
        )
        return probabilities, item_p

    def _said(self, section: Section, key: str, until: float) -> list[str]:
        """What the persona said in the section about lines up to ``until``: a rerun must not see later comments."""
        said = [c for c in self.comments if c["section"] == section.index and c["persona"] == key and c["at"] <= until]
        return [c["text"] for c in sorted(said, key=lambda c: c["at"])]

    @staticmethod
    def _comment_question(persona: Persona, has_spoken: bool) -> dict:
        who = persona.name + (f" ({persona.viewpoint})" if persona.viewpoint else "")
        instructions = f"Do `new_lines` give the {who} something to comment on? Read `section_so_far` for context"
        if has_spoken:
            instructions += f", and do not count what `already_commented.{persona.key}` already covers"
        return noul(instructions + ".", true=persona.comment_when, false=persona.skip_when or SKIP_DEFAULT)

    async def _comment(
        self, persona: Persona, section: Section, so_far: list[Line], lines: list[Line], p: float, *, rerun: bool
    ) -> None:
        language = self.brief.language or "the language of the meeting"
        style = persona.style or (
            "One or two short sentences: a pointed question, a risk or a suggestion. "
            "No greeting and no summary of what was said."
        )
        system = (
            f"You are the {persona.name}, following a live meeting.\n"
            + (f"Your angle: {persona.viewpoint}\n" if persona.viewpoint else "")
            + f"You speak up when: {persona.comment_when}\n"
            + f"{style}\nWrite in {language}.\n\n"
            + f"About this meeting:\n{self.brief.context_text()}"
        )
        user = f"The section so far:\n{_text(so_far, self.state_chars)}\n\nThe new lines you react to:\n{render(lines)}"
        if said := self._said(section, persona.key, lines[-1].end):
            user += "\n\nWhat you already said in this section:\n" + "\n".join(f"- {c}" for c in said)
        user += "\n\nWrite your comment."
        text = await self._chat([{"role": "system", "content": system}, {"role": "user", "content": user}])
        if not text:
            return
        comment = {
            "type": "comment",
            "section": section.index,
            "persona": persona.key,
            "name": persona.name,
            "p": p,
            "at": lines[-1].end,
            "text": text,
        }
        if rerun:
            comment["rerun"] = True  # written later, about this moment
        self.comments.append(comment)
        self.emit(comment)

    # -- decisions and to-dos ---------------------------------------------

    async def _update_items(self, section: Section, so_far: list[Line], lines: list[Line]) -> None:
        """One chat call that adds or updates items from ``lines``, knowing the current list."""
        system = ITEMS_SYSTEM.format(
            language=self.brief.language or "the language of the meeting", context=self.brief.context_text()
        )
        current = "\n".join(
            json.dumps({"id": i.id, "kind": i.kind, "text": i.text, "owner": i.owner, "due": i.due}, ensure_ascii=False)
            for i in self.items.visible()
        )
        removed = [f"- {i.id} ({i.kind}): {i.text}" for i in self.items.items if i.deleted]
        user = (
            f"The section so far:\n{_text(so_far, self.state_chars)}\n\n"
            f"The new lines:\n{render(lines)}\n\n"
            f"Current items:\n{current or 'none'}"
        )
        if removed:
            user += "\n\nRemoved by the user, do not add again:\n" + "\n".join(removed)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        try:
            updates = await ask_json(self.chat, messages, parse_updates, what="decisions and to-dos")
        except ChatError as exc:
            self._error("chat", str(exc))
            return
        self._last_error.pop("chat", None)
        if self.items.apply(updates, at=lines[-1].end, section=section.index):
            self._items_changed()

    def edit_item(self, item_id: str, **fields: str | None) -> Item:
        """The user's correction (text, owner, due); KeyError for an unknown id."""
        item = self.items.edit(item_id, **fields)
        self._items_changed()
        return item

    def delete_item(self, item_id: str) -> None:
        self.items.delete(item_id)
        self._items_changed()

    def _items_changed(self) -> None:
        self.emit({"type": "items", "items": [i.to_json() for i in self.items.visible()]})

    # -- questions --------------------------------------------------------

    def _ask_transcript(self, lines: Sequence[Line]) -> str:
        """Section headers, lines and comments in time order; the oldest lines go first when it is too long."""
        starts = {s.start: s for s in self.sections}
        comments_after: dict[float, list[dict]] = {}
        for c in self.comments:
            comments_after.setdefault(c["at"], []).append(c)
        blocks: list[str] = []
        for line in lines:
            if line.start in starts:
                s = starts[line.start]
                blocks.append(f"\n## {s.title or f'Section {s.index + 1}'} (from {format_time(s.start)})")
            blocks.append(line.render())
            for c in comments_after.get(line.end, []):
                blocks.append(f"  > {c['name']} commented: {c['text']}")
        out: list[str] = []
        used = 0
        for block in reversed(blocks):
            used += len(block) + 1
            if used > self.ask_chars:
                out.append("[earlier part of the meeting left out]")
                break
            out.append(block)
        return "\n".join(reversed(out)).strip()

    # -- model calls ------------------------------------------------------

    async def _decide(self, state: dict, questions: dict) -> dict[str, Answer] | None:
        try:
            answers = await self.decisions.decide(state, questions)
        except DecisionError as exc:
            self._error("decisions", str(exc))
            return None
        self._last_error.pop("decisions", None)
        return answers

    async def _chat(self, messages: list[dict], **kwargs) -> str | None:
        try:
            reply = await self.chat.complete(messages, **kwargs)
        except ChatError as exc:
            self._error("chat", str(exc))
            return None
        self._last_error.pop("chat", None)
        return reply

    def _error(self, where: str, message: str) -> None:
        """Report a failing model once, not on every batch, until it recovers."""
        if self._last_error.get(where) != message:
            self._last_error[where] = message
            self.emit({"type": "error", "where": where, "message": message})


def _steps(lines: Sequence[Line], step: float) -> list[tuple[int, int]]:
    """``lines`` cut into runs of about ``step`` seconds, as (start, end) indexes."""
    out: list[tuple[int, int]] = []
    start = 0
    for i, line in enumerate(lines):
        if line.end - lines[start].start >= step:
            out.append((start, i + 1))
            start = i + 1
    if start < len(lines):
        out.append((start, len(lines)))
    return out


def _p(answers: dict[str, Answer], key: str) -> float:
    answer = answers[key]
    return answer.p_yes if isinstance(answer, NoulAnswer) else 0.0
