"""The analysis: sections by topic, comments per persona, answers about the meeting.

Each batch of new lines costs at most two decision calls, which JEV answers in well
under a second: one asks whether the topic changed and on which line, the other asks
every persona at once whether the new lines give it something to say. Only a yes
costs a chat call.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from .brief import Brief, Persona
from .chat import ChatClient, ChatError
from .decisions import Answer, ChoiceAnswer, DecisionClient, DecisionError, NoulAnswer, choice, noul
from .transcript import Line, render
from ..transcript_txt import format_time

Emit = Callable[[dict], None]

TOPIC_TRUE = "The new lines start a different subject, agenda item, problem or decision than the current section."
TOPIC_FALSE = "The new lines continue, deepen or briefly digress from the subject of the current section."
SKIP_DEFAULT = "Nothing in the new lines meets the comment criteria, or it was already commented on."


@dataclass
class Thresholds:
    """Where a probability becomes a yes. Re-pick these for every decision model."""

    topic: float = 0.6
    comment: float = 0.5
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
    comments: dict[str, list[str]] = field(default_factory=dict)

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
        """The section's transcript, cut from the front when it is too long."""
        text = render(self.lines)
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
    ) -> None:
        self.brief = brief
        self.decisions = decisions
        self.chat = chat
        self.emit = emit
        self.thresholds = thresholds or Thresholds()
        self.state_chars = state_chars
        self.ask_chars = ask_chars
        self.sections: list[Section] = []
        self.comments: list[dict] = []
        self._background: set[asyncio.Task] = set()
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
        comment_p = await self._consider_comments(section, lines)
        self.emit(
            {"type": "decision", "section": section.index, "at": lines[-1].end, "topic": topic_p, "comments": comment_p}
        )

    async def finish(self) -> None:
        """The meeting ended: close the last section and wait for pending titles."""
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

    async def _consider_comments(self, section: Section, lines: list[Line]) -> dict[str, float]:
        personas = self.brief.personas
        if not personas:
            return {}
        state: dict = {
            "meeting": self.brief.meeting_state(),
            "section_so_far": section.text(self.state_chars),
            "new_lines": render(lines),
        }
        already = {key: said for key, said in section.comments.items() if said}
        if already:
            state["already_commented"] = already
        questions = {f"comment__{p.key}": self._comment_question(p, p.key in already) for p in personas}
        answers = await self._decide(state, questions)
        if not answers:
            return {}
        probabilities = {p.key: _p(answers, f"comment__{p.key}") for p in personas}
        speaking = [p for p in personas if probabilities[p.key] >= self.thresholds.comment]
        await asyncio.gather(*(self._comment(p, section, lines, probabilities[p.key]) for p in speaking))
        return probabilities

    @staticmethod
    def _comment_question(persona: Persona, has_spoken: bool) -> dict:
        who = persona.name + (f" ({persona.viewpoint})" if persona.viewpoint else "")
        instructions = f"Do `new_lines` give the {who} something to comment on? Read `section_so_far` for context"
        if has_spoken:
            instructions += f", and do not count what `already_commented.{persona.key}` already covers"
        return noul(instructions + ".", true=persona.comment_when, false=persona.skip_when or SKIP_DEFAULT)

    async def _comment(self, persona: Persona, section: Section, lines: list[Line], p: float) -> None:
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
        user = f"The section so far:\n{section.text(self.state_chars)}\n\nThe new lines you react to:\n{render(lines)}"
        if section.comments.get(persona.key):
            user += "\n\nWhat you already said in this section:\n" + "\n".join(
                f"- {c}" for c in section.comments[persona.key]
            )
        user += "\n\nWrite your comment."
        text = await self._chat([{"role": "system", "content": system}, {"role": "user", "content": user}])
        if not text:
            return
        section.comments.setdefault(persona.key, []).append(text)
        comment = {
            "type": "comment",
            "section": section.index,
            "persona": persona.key,
            "name": persona.name,
            "p": p,
            "at": lines[-1].end,
            "text": text,
        }
        self.comments.append(comment)
        self.emit(comment)

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


def _p(answers: dict[str, Answer], key: str) -> float:
    answer = answers[key]
    return answer.p_yes if isinstance(answer, NoulAnswer) else 0.0
