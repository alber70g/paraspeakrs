from __future__ import annotations

from textual import events, on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Static, TextArea

from ..labeling import JobSummary

PLACEHOLDER = "What was this meeting about?"


class NoteSaved(Message):
    """The note on ``job_id`` was written to disk."""

    def __init__(self, job_id: str, note: str) -> None:
        super().__init__()
        self.job_id = job_id
        self.note = note


class NotePanel(Vertical):
    """What the meeting was about, in your own words, kept with the job.

    Diarization answers who spoke and the transcript answers what was said;
    neither answers why the recording mattered, and three weeks later that is
    the only thing you need to pick the right one out of a list. So the note is
    editable right where the recording is selected, not behind a prompt.

    It saves when focus leaves rather than on a key you have to remember: a note
    lost to tabbing away is worse than a note saved once too often.
    """

    def __init__(self, summary: JobSummary | None = None, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.summary = summary
        self._loaded = ""

    def compose(self) -> ComposeResult:
        yield Static("", id="note-header")
        yield TextArea("", id="note", soft_wrap=True, placeholder=PLACEHOLDER)

    def on_mount(self) -> None:
        self.load(self.summary)

    def load(self, summary: JobSummary | None) -> None:
        """Show this job's note, saving whatever was being typed about the last one.

        Moving the cursor down a list is not a decision to discard an edit, and
        the selection can change without focus ever leaving this panel - so the
        outgoing note is written before the incoming one replaces it.

        Re-loading the same job leaves an unsaved edit alone. Every refresh of
        the job list arrives here, and overwriting the buffer from disk on each
        one would delete the sentence being typed.
        """
        area = self.query_one("#note", TextArea)
        # Which meeting this was is known without anyone typing it; the header
        # says so and follows the names, while the text below stays yours.
        self.query_one("#note-header", Static).update(summary.header if summary else "")
        same = summary is not None and self.summary is not None and summary.job_id == self.summary.job_id
        if same:
            self.summary = summary
            if area.text.strip() != self._loaded.strip():
                return
            self._loaded = summary.note
            area.text = summary.note
            return

        self.save()
        self.summary = summary
        if summary is None:
            self._loaded = ""
            area.text = ""
            area.read_only = True
            self.border_title = "note"
            self.border_subtitle = "select a processed recording"
            return
        area.read_only = False
        self._loaded = summary.note
        area.text = summary.note
        self.border_title = "note"
        self.border_subtitle = ""

    def forget(self) -> None:
        """Drop the buffer without saving, for a job that no longer exists.

        Clearing the panel the ordinary way saves the outgoing note first, which
        for a just-deleted job means writing into a directory that was removed -
        an error toast about a note the user never asked to keep.
        """
        self.summary = None
        self._loaded = ""
        if self.is_mounted:
            self.query_one("#note", TextArea).text = ""

    def save(self) -> None:
        """Write the note if it was changed. Unchanged notes are never rewritten."""
        if self.summary is None or not self.is_mounted:
            return
        text = self.query_one("#note", TextArea).text
        if text.strip() == self._loaded.strip():
            return
        try:
            stored = self.app.workspace.set_job_note(self.summary.job_id, text)
        except Exception as exc:  # noqa: BLE001
            self.notify(str(exc), severity="error")
            return
        self._loaded = stored
        self.summary.note = stored
        self.border_subtitle = "saved"
        self.post_message(NoteSaved(self.summary.job_id, stored))

    @on(events.DescendantBlur)
    def _save_on_blur(self) -> None:
        self.save()

    @on(TextArea.Changed, "#note")
    def _mark_dirty(self) -> None:
        if self.summary is None:
            return
        dirty = self.query_one("#note", TextArea).text.strip() != self._loaded.strip()
        self.border_subtitle = "unsaved · tab to save" if dirty else ""
