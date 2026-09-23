from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from ..labeling import JobSummary, TranscriptLine
from ..transcript_txt import format_time


class LinesPanel(Vertical):
    """The transcript, read through the eyes of the speaker selected above it.

    Every line is shown, because a stray line is only recognisable as stray in
    context - but the selected speaker's lines are the bright ones, and h / l
    jump between them. d removes the line under the cursor from the transcript,
    and d again puts it back: removal is a mark on the job, never a deletion.

    Removing a whole speaker is the speaker table's business; their lines show
    struck through here so you can see what went, and cannot be restored one
    by one while the speaker as a whole is out.
    """

    BINDINGS = [
        ("j", "down", "Down"),
        ("k", "up", "Up"),
        ("h", "previous_of_speaker", "Prev of speaker"),
        ("l", "next_of_speaker", "Next of speaker"),
        ("d", "toggle_line", "Remove line"),
    ]

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.summary: JobSummary | None = None
        self.speaker: str | None = None
        self._lines: list[TranscriptLine] = []

    def compose(self) -> ComposeResult:
        yield OptionList(id="lines")

    def on_mount(self) -> None:
        self.border_title = "transcript"

    # ----- what is being shown -------------------------------------------------

    def load(self, summary: JobSummary | None) -> None:
        """Show this job's lines. Re-loading the same job keeps the cursor where it was."""
        same = summary is not None and self.summary is not None and summary.job_id == self.summary.job_id
        self.summary = summary
        if not same:
            self.speaker = None
        if summary is None:
            self._lines = []
            self._show_lines(keep=False)
            self.border_subtitle = ""
            return
        try:
            self._lines = self.app.workspace.transcript_lines(summary.job_id)
        except Exception as exc:  # noqa: BLE001
            self.notify(str(exc), severity="error")
            self._lines = []
        self._show_lines(keep=same)

    def follow(self, speaker: str | None) -> None:
        """Brighten this speaker's lines and put the cursor on their first one."""
        if speaker == self.speaker:
            return
        self.speaker = speaker
        self._show_lines(keep=False)
        first = next((i for i, line in enumerate(self._lines) if line.speaker == speaker), None)
        if first is not None:
            self._move_to(first)

    def _show_lines(self, *, keep: bool) -> None:
        options = self.query_one(OptionList)
        highlighted = options.highlighted if keep else None
        options.clear_options()
        options.add_options([Option(self._prompt(line)) for line in self._lines])
        if highlighted is not None and self._lines:
            self._move_to(min(highlighted, len(self._lines) - 1))
        if self.summary is not None:
            removed = sum(1 for line in self._lines if line.excluded)
            self.border_subtitle = f"{len(self._lines)} lines" + (f" · {removed} removed" if removed else "")

    def _prompt(self, line: TranscriptLine) -> Text:
        mine = line.speaker == self.speaker
        style = "" if mine else "dim"
        if line.excluded:
            style = "dim strike"
        text = Text(f"[{format_time(line.start)}] ", style="dim")
        text.append(f"{line.name}: ", style=f"{style} bold" if mine else style)
        text.append(line.text, style=style)
        if line.excluded:
            text = Text.assemble(("✕ ", "red"), text)
        return text

    def _move_to(self, index: int) -> None:
        options = self.query_one(OptionList)
        options.highlighted = index
        options.scroll_to_highlight()

    # ----- moving --------------------------------------------------------------

    def action_down(self) -> None:
        self.query_one(OptionList).action_cursor_down()

    def action_up(self) -> None:
        self.query_one(OptionList).action_cursor_up()

    def action_next_of_speaker(self) -> None:
        self._jump(+1)

    def action_previous_of_speaker(self) -> None:
        self._jump(-1)

    def _jump(self, step: int) -> None:
        current = self.query_one(OptionList).highlighted
        start = -1 if current is None and step > 0 else (len(self._lines) if current is None else current)
        index = start + step
        while 0 <= index < len(self._lines):
            if self._lines[index].speaker == self.speaker:
                self._move_to(index)
                return
            index += step

    # ----- removing ------------------------------------------------------------

    def action_toggle_line(self) -> None:
        index = self.query_one(OptionList).highlighted
        if self.summary is None or index is None or index >= len(self._lines):
            return
        line = self._lines[index]
        if any(row.speaker == line.speaker and row.excluded for row in self.summary.speakers):
            self.notify(f"{line.speaker} is removed as a whole — press d on them in the speaker table")
            return
        try:
            self.app.workspace.toggle_line_excluded(self.summary.job_id, line.speaker, line.start)
        except Exception as exc:  # noqa: BLE001
            self.notify(str(exc), severity="error")
            return
        self.load(self.summary)

