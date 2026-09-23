from __future__ import annotations

import logging
from pathlib import Path

from textual import events, on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, DirectoryTree, Footer, Header, ProgressBar, Static, Tree

from ..labeling import JobSummary
from .browse import BrowserTree, default_browse_root
from .lines import LinesPanel
from .modals import ChoiceModal, PathSuggester, TextPromptModal, confirm
from .note import NotePanel, NoteSaved
from .speakers import SpeakerHighlighted, SpeakerPanel, SpeakersChanged
from .state import DONE, FAILED, RUNNING, WAITING, QueueItem

STATUS_TEXT = {WAITING: "waiting", RUNNING: "running", DONE: "done", FAILED: "failed"}

#: Shown in place of a speaker table when the cursor is on something that has
#: not been through the pipeline. Saying which of the two it is matters: "not
#: transcribed yet" is an invitation to press enter, "a directory" is not.
NO_JOB = "not transcribed yet · ↵ to queue"
NOT_A_RECORDING = "no recording selected"

#: The answers to "is a channel one person?", as the channels each one names.
SINGLE_SPEAKER_CHOICES = {"none": [], "L": ["L"], "R": ["R"], "LR": ["L", "R"]}

LOGGER = logging.getLogger(__name__)


def _nav(key: str, action: str, description: str) -> list[Binding]:
    """Bind an action to both ctrl+key and the bare letter.

    The browse tree turns every printable key into filter input, so a plain
    letter cannot reach the screen while the tree has focus - but it is still
    the natural key everywhere else. Binding both means one footer entry that
    always works and a shortcut that works wherever the tree is not listening.
    """
    return [
        Binding(f"ctrl+{key}", action, description),
        Binding(key, action, description, show=False),
    ]


class HomeScreen(Screen):
    """Everything at once: pick a recording on the left, work on it on the right.

    The three lists on the left are all ways of naming one recording - one you
    are looking for, one already transcribed, one waiting its turn - and the two
    panels on the right are always about whichever of them the cursor is on. So
    moving through a list re-aims the detail instead of travelling to it, and
    the speaker table is never a screen you have to find your way back from.

    Tab walks the six panels in reading order: browse, processed, queue,
    speakers, transcript, note.
    """

    BINDINGS = [
        Binding("tab", "app.focus_next", "Next panel"),
        Binding("shift+tab", "app.focus_previous", "Prev panel", show=False),
        *_nav("v", "voices", "Voices"),
        *_nav("g", "goto", "Go to path"),
        *_nav("x", "drop", "Drop"),
        *_nav("d", "delete", "Delete job"),
        *_nav("r", "refresh", "Refresh"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._draining = False
        self._jobs: dict[str, JobSummary] = {}
        self._jobs_by_path: dict[Path, JobSummary] = {}
        # Which of the three lists the detail panels are following. Focus is
        # not enough: tabbing into the speaker table must not re-aim the detail
        # at whatever some other list's cursor happens to sit on.
        self._master = "tree"

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="panes"):
            with Vertical(id="masters"):
                yield BrowserTree(self.app.state.browse_dir or default_browse_root())
                with Vertical(id="processed-pane"):
                    yield DataTable(id="processed", cursor_type="row")
                with Vertical(id="queue-pane"):
                    yield DataTable(id="queue", cursor_type="row")
            with Vertical(id="detail"):
                yield SpeakerPanel(id="speaker-panel")
                yield LinesPanel(id="lines-pane")
                yield NotePanel(id="note-pane")
        yield ProgressBar(total=100, show_eta=False)
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#queue", DataTable).add_columns("Recording", "Status")
        self.query_one("#processed", DataTable).add_columns("Recording", "Spk", "Unnamed", "Note")
        self.query_one("#queue-pane").border_title = "queue"
        self.query_one("#processed-pane").border_title = "processed"
        self.query_one(ProgressBar).display = False
        self.query_one(BrowserTree).focus()
        self.action_refresh()
        self._pump()

    def on_screen_resume(self) -> None:
        """Coming back from the job list or voices, names and jobs have moved on."""
        self.action_refresh()

    # ----- jobs, marks, status -------------------------------------------------

    def action_refresh(self) -> None:
        self._load_jobs()

    @work(thread=True, exclusive=True, group="jobs")
    def _load_jobs(self) -> None:
        try:
            jobs = self.app.workspace.list_jobs()
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(self.notify, str(exc), severity="error")
            return
        self.app.call_from_thread(self._apply_jobs, jobs)

    def _apply_jobs(self, jobs: list[JobSummary]) -> None:
        # The tree hands out resolved paths (/private/var/... on macOS) while a job
        # stores whatever path it was given, so both sides have to be resolved or
        # nothing ever matches and every recording looks untouched.
        self._jobs = {job.job_id: job for job in jobs}
        self._jobs_by_path = {job.source_path.resolve(): job for job in jobs if job.source_path is not None}
        self._refresh_marks()
        self._refresh_queue()
        self._refresh_processed()
        unnamed = sum(1 for job in jobs for row in job.speakers if row.unnamed)
        self.query_one("#status", Static).update(
            f"{len(jobs)} job(s) · {unnamed} speaker(s) still unnamed · {self.app.workspace.store.root}"
        )
        self._resync_detail()

    def _refresh_marks(self) -> None:
        tree = self.query_one(BrowserTree)
        marks = {path: "✓" for path in self._jobs_by_path}
        for item in self.app.state.queue:
            if item.status in (WAITING, RUNNING):
                marks[item.path] = "…"
        tree.set_marks(marks)

    # ----- the detail panels ---------------------------------------------------

    def _show(self, summary: JobSummary | None, placeholder: str = NOT_A_RECORDING) -> None:
        """Aim the detail panels at one job, or at nothing."""
        self.query_one(SpeakerPanel).load(summary, placeholder=placeholder)
        self.query_one(LinesPanel).load(summary)
        self.query_one(NotePanel).load(summary)

    def _job_for_path(self, path: Path | None) -> JobSummary | None:
        if path is None:
            return None
        return self._jobs_by_path.get(Path(path).resolve())

    def _resync_detail(self) -> None:
        """Re-aim the detail at the cursor of whichever list is in charge.

        Called after a refresh because the JobSummary the panels hold is a
        snapshot: a job that has just finished, or been named, is a different
        object with the same job_id, and the counts drawn from the old one are
        already wrong.
        """
        if self._master == "processed":
            self._show_processed_row()
        elif self._master == "queue":
            self._show_queue_row()
        else:
            self._show_tree_node()

    def _show_tree_node(self) -> None:
        tree = self.query_one(BrowserTree)
        node = tree.cursor_node
        path = getattr(node.data, "path", None) if node is not None else None
        if path is None or Path(path).is_dir():
            self._show(None, NOT_A_RECORDING)
            return
        job = self._job_for_path(Path(path))
        self._show(job, NO_JOB)

    def _show_processed_row(self) -> None:
        self._show(self._row_job("#processed", lambda key: self._jobs.get(key)))

    def _show_queue_row(self) -> None:
        """A queue row's detail is its job, once it has one; until then, its status.

        A waiting or failed item has no speakers to show, and "↵ to queue" would
        be the wrong thing to say about something already in the queue.
        """
        placeholder = NOT_A_RECORDING

        def job(key: str) -> JobSummary | None:
            nonlocal placeholder
            item = next((i for i in self.app.state.queue if str(i.path) == key), None)
            if item is None:
                return None
            placeholder = f"{item.path.name} · {self._status_text(item)}"
            return self._jobs.get(item.job_id or "") or self._job_for_path(item.path)

        summary = self._row_job("#queue", job)
        self._show(summary, placeholder)

    def _row_job(self, selector: str, lookup) -> JobSummary | None:
        table = self.query_one(selector, DataTable)
        if table.row_count == 0:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return lookup(row_key.value) if row_key.value is not None else None

    @on(events.DescendantFocus)
    def _master_took_over(self, event: events.DescendantFocus) -> None:
        """Tabbing into a list makes its cursor the one the detail follows.

        Without this, the detail keeps showing whatever the last list you moved
        through was pointing at, which reads as the panels having come unstuck
        from the selection.
        """
        widget = event.widget
        if isinstance(widget, BrowserTree):
            self._master = "tree"
        elif widget.id in ("processed", "queue"):
            self._master = widget.id
        else:
            # Focus moved into the detail; it keeps showing what it was showing.
            return
        self._resync_detail()

    @on(Tree.NodeHighlighted)
    def _tree_moved(self, event: Tree.NodeHighlighted) -> None:
        self._take_over(event.control, "tree")

    @on(DataTable.RowHighlighted, "#processed")
    def _processed_moved(self, event: DataTable.RowHighlighted) -> None:
        self._take_over(event.control, "processed")

    @on(DataTable.RowHighlighted, "#queue")
    def _queue_moved(self, event: DataTable.RowHighlighted) -> None:
        self._take_over(event.control, "queue")

    def _take_over(self, widget, master: str) -> None:
        """A cursor move re-aims the detail only if the user made it.

        Rebuilding either table posts a highlight for its first row, and a
        background refresh must not therefore yank the detail off whatever the
        list you are actually working in has selected.
        """
        if not widget.has_focus:
            return
        self._master = master
        self._resync_detail()

    @on(SpeakersChanged)
    def _speakers_changed(self) -> None:
        """A name was assigned in the panel; the counts on the left are now stale."""
        self.action_refresh()

    @on(SpeakerHighlighted)
    def _speaker_moved(self, event: SpeakerHighlighted) -> None:
        self.query_one(LinesPanel).follow(event.speaker)

    @on(NoteSaved)
    def _note_saved(self, event: NoteSaved) -> None:
        job = self._jobs.get(event.job_id)
        if job is not None:
            job.note = event.note
        self._refresh_processed()

    # ----- browsing ------------------------------------------------------------

    @on(DirectoryTree.DirectorySelected)
    def _descend(self, event: DirectoryTree.DirectorySelected) -> None:
        """Enter on a directory makes it the new root; Right expands it in place."""
        self.query_one(BrowserTree).path = event.path
        self.app.state.remember_dir(Path(event.path))

    @on(DirectoryTree.FileSelected)
    def _chose_recording(self, event: DirectoryTree.FileSelected) -> None:
        """Enter on a recording queues it, or opens the one already transcribed.

        Enter used to queue unconditionally and, for a recording already done,
        say so and point at the job list - which meant re-finding the same file
        by name in a second list. The panel beside the tree is already showing
        it, so enter can simply hand focus over.
        """
        path = Path(event.path).resolve()
        job = self._jobs_by_path.get(path)
        if job is not None:
            self._show(job)
            self.query_one(SpeakerPanel).focus_table()
            return
        self._ask_channels(path)

    def _ask_channels(self, path: Path) -> None:
        """Queue a recording, first asking whether a stereo channel is one person.

        Each channel of a stereo recording is diarized on its own, and a
        headset channel that only ever holds its wearer still gets clustered -
        so a laugh or a lowered voice becomes a second speaker to name by hand.
        Only the user knows how the recording was wired, so they are asked; a
        mono file has no channels to ask about and is queued straight away.
        """
        try:
            channels = self.app.workspace.pipeline.audio.channel_count(path)
        except Exception:  # noqa: BLE001 - the transcription reports it properly
            channels = 1
        if channels < 2:
            self._enqueue(path)
            return

        def chose(choice: str | None) -> None:
            if choice is not None:
                self._enqueue(path, SINGLE_SPEAKER_CHOICES[choice])

        question = ChoiceModal(
            f"Queue {path.name}",
            "This recording is stereo. Is either channel a single speaker - "
            "someone on their own mic? Then that channel is not searched for "
            "more voices.",
            [
                ("none", "No / not sure"),
                ("L", "Left is one"),
                ("R", "Right is one"),
                ("LR", "Both are"),
            ],
        )
        # Four buttons do not fit the width the two-button confirmations use.
        question.add_class("wide")
        self.app.push_screen(question, chose)

    def action_goto(self) -> None:
        def done(value: str | None) -> None:
            if not value:
                return
            candidate = Path(value).expanduser()
            target = candidate if candidate.is_dir() else candidate.parent
            if not target.is_dir():
                self.notify(f"no such directory: {target}", severity="error")
                return
            self.query_one(BrowserTree).path = target
            self.app.state.remember_dir(target)

        current = str(self.query_one(BrowserTree).path)
        self.app.push_screen(TextPromptModal("Go to directory", current, PathSuggester()), done)

    # ----- processed recordings ------------------------------------------------

    def _refresh_processed(self) -> None:
        table = self.query_one("#processed", DataTable)
        table.clear()
        for job in self._jobs.values():
            unnamed = sum(1 for row in job.speakers if row.unnamed)
            table.add_row(
                job.source_path.name if job.source_path else job.job_id,
                str(job.num_speakers),
                str(unnamed) if unnamed else "—",
                "✎" if job.note.strip() else " ",
                key=job.job_id,
            )
        self.query_one("#processed-pane").border_title = (
            f"processed · {len(self._jobs)}" if self._jobs else "processed"
        )

    def _selected_processed(self) -> JobSummary | None:
        """The job under the processed panel's cursor, but only while it has focus.

        Deleting is destructive and the key is a bare letter, so it must not fire
        for a row that merely happens to be where some other panel's cursor left
        the processed table.
        """
        table = self.query_one("#processed", DataTable)
        if table.row_count == 0 or not table.has_focus:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return self._jobs.get(row_key.value) if row_key.value is not None else None

    def action_delete(self) -> None:
        """Forget a job, after asking. The voices it taught are kept."""
        job = self._selected_processed()
        if job is None:
            return
        source = job.source_path.name if job.source_path else job.job_id

        def chose(choice: str | None) -> None:
            if choice != "yes":
                return
            try:
                self.app.workspace.delete_job(job.job_id)
            except Exception as exc:  # noqa: BLE001
                self.notify(str(exc), severity="error")
                return
            # The panels are holding a job that no longer has a directory; the
            # note must be dropped rather than saved back into one that is gone.
            self.query_one(NotePanel).forget()
            self.query_one(SpeakerPanel).load(None)
            self.query_one(LinesPanel).load(None)
            self.action_refresh()

        self.app.push_screen(
            confirm(
                f"Delete the job for {source}?",
                "The transcript, its names and its note go with it. "
                "The voices it taught are kept.",
                "Delete",
            ),
            chose,
        )

    @on(DataTable.RowSelected, "#processed")
    def _open_processed(self, event: DataTable.RowSelected) -> None:
        job = self._jobs.get(event.row_key.value)
        if job is None:
            return
        self._show(job)
        self.query_one(SpeakerPanel).focus_table()

    # ----- the queue -----------------------------------------------------------

    def _enqueue(self, path: Path, single_speaker_channels: list[str] | None = None) -> None:
        path = Path(path).resolve()
        if path in self._jobs_by_path:
            self.notify(f"{path.name} is already transcribed — its speakers are on the right")
            return
        if self.app.state.enqueue(path, single_speaker_channels) is None:
            self.notify(f"{path.name} is already queued")
            return
        self._refresh_queue()
        self._refresh_marks()
        self._pump()

    def action_drop(self) -> None:
        item = self._selected_item()
        if item is None:
            return
        if not self.app.state.drop(item.path):
            self.notify("that one is running — it will finish first", severity="warning")
            return
        self._refresh_queue()
        self._refresh_marks()

    def _selected_item(self) -> QueueItem | None:
        table = self.query_one("#queue", DataTable)
        if table.row_count == 0 or not table.has_focus:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return next((item for item in self.app.state.queue if str(item.path) == row_key.value), None)

    def _refresh_queue(self) -> None:
        table = self.query_one("#queue", DataTable)
        table.clear()
        for item in self.app.state.queue:
            table.add_row(item.path.name, self._status_text(item), key=str(item.path))
        waiting = sum(1 for item in self.app.state.queue if item.status == WAITING)
        self.query_one("#queue-pane").border_title = f"queue · {waiting} waiting" if waiting else "queue"

    def _status_text(self, item: QueueItem) -> str:
        if item.status == FAILED:
            return f"failed: {(item.error or '').splitlines()[0][:40]}"
        return STATUS_TEXT.get(item.status, item.status)

    @on(DataTable.RowSelected, "#queue")
    def _open_queued(self, event: DataTable.RowSelected) -> None:
        item = next((i for i in self.app.state.queue if str(i.path) == event.row_key.value), None)
        if item is None:
            return
        job = self._jobs.get(item.job_id or "") or self._job_for_path(item.path)
        if job is None and item.status == FAILED:
            # The row shows 40 characters; the part that says why is after them.
            where = f"\n\nfull log: {self.app.log_path}" if self.app.log_path else ""
            head = "\n".join((item.error or "").splitlines()[:4])
            self.notify(f"{head}{where}\n\n↵ on the file again to retry", severity="error", timeout=30)
            return
        if job is None:
            self.notify(f"{item.path.name} is still {self._status_text(item)}")
            return
        self._show(job)
        self.query_one(SpeakerPanel).focus_table()

    # ----- the worker ----------------------------------------------------------

    def _pump(self) -> None:
        """Start draining the queue unless a drain is already in flight.

        Textual's exclusive workers cancel the one already running, which here
        would abandon a transcription mid-file every time something was queued.
        """
        if self._draining or self.app.state.next_waiting() is None:
            return
        self._draining = True
        self.query_one(ProgressBar).display = True
        self._drain()

    @work(thread=True, group="queue")
    def _drain(self) -> None:
        state = self.app.state
        while True:
            item = state.next_waiting()
            if item is None:
                break
            state.mark(item, RUNNING)
            self.app.call_from_thread(self._refresh_queue)

            def progress(step: str, detail: str | None, percent: int | None, path=item.path) -> None:
                self.app.call_from_thread(self._progress, path, step, detail, percent)

            try:
                summary = self.app.workspace.transcribe(
                    item.path,
                    progress=progress,
                    single_speaker_channels=frozenset(item.single_speaker_channels),
                )
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("transcribing %s failed", item.path)
                state.mark(item, FAILED, error=str(exc))
            else:
                state.mark(item, DONE, job_id=summary.job_id)
            self.app.call_from_thread(self._finished_one)
        self.app.call_from_thread(self._drained)

    def _progress(self, path: Path, step: str, detail: str | None, percent: int | None) -> None:
        if percent is not None:
            self.query_one(ProgressBar).update(progress=percent)
        self.query_one("#status", Static).update(f"{path.name} · {step}: {detail or ''}")

    def _finished_one(self) -> None:
        self._refresh_queue()
        self.action_refresh()

    def _drained(self) -> None:
        self._draining = False
        self.query_one(ProgressBar).display = False
        self._refresh_queue()
        self._refresh_marks()

    # ----- navigation ----------------------------------------------------------

    def action_voices(self) -> None:
        from .voices import VoicesScreen

        self.app.push_screen(VoicesScreen())


__all__ = ["HomeScreen"]
