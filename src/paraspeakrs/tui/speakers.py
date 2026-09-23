from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static, TextArea

from ..labeling import JobSummary, SpeakerRow
from .modals import ChoiceModal, NameSuggester, TextPromptModal

SELECTED = "•"


class TranscriptScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", "Back")]

    def __init__(self, header: str, text: str) -> None:
        super().__init__()
        self._header = header
        self._text = text

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(self._header, id="status")
        yield TextArea(self._text, read_only=True, id="transcript")
        yield Footer()


class SpeakersChanged(Message):
    """A speaker in the shown job was named, cleared, merged, accepted or removed."""


class SpeakerHighlighted(Message):
    """The cursor in the speaker table landed on a speaker."""

    def __init__(self, speaker: str) -> None:
        super().__init__()
        self.speaker = speaker


class SpeakerPanel(Vertical):
    """Who is who in one job: your decisions in one column, the cache's guesses in another.

    The two are deliberately never blended. A name in ``Name`` was typed or
    accepted by a person and is the only thing that reaches a transcript; a name
    in ``Suggested`` is a cosine match waiting to be confirmed or ignored.

    This is a panel rather than a screen because naming speakers is what you do
    *about* the recording you have selected, not a place you travel to: shown
    beside the selection it stays answerable to a cursor moving through the
    list, and Tab is enough to reach it.

    Its bindings live here rather than on the host screen so that ``n`` means
    "name this speaker" only while this panel holds focus, leaving the same key
    free everywhere else on the screen.
    """

    BINDINGS = [
        ("p", "play", "Play"),
        ("s", "stop", "Stop"),
        ("n", "name", "Name"),
        ("A", "accept_all", "Accept all"),
        ("space", "select", "Select"),
        ("m", "merge", "Merge"),
        ("u", "unassign", "Clear name"),
        ("d", "exclude", "Remove"),
        ("t", "transcript", "Transcript"),
    ]

    def __init__(self, summary: JobSummary | None = None, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.summary = summary
        self._rows: dict[str, SpeakerRow] = {}
        self._selected: set[str] = set()

    def compose(self) -> ComposeResult:
        yield Static("", id="warning")
        yield DataTable(id="speakers", cursor_type="row")

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns(" ", "Speaker", "Name", "Suggested", "Conf", "Talk (s)")
        self.load(self.summary)

    # ----- what is being shown -------------------------------------------------

    def load(self, summary: JobSummary | None, *, placeholder: str = "no recording selected") -> None:
        """Point the panel at a job, or at nothing.

        Switching jobs drops the multi-select: a set of speaker IDs means
        something only inside the job it was picked in, and SPEAKER_01 exists in
        every one of them, so carrying it over would silently arm a merge on the
        wrong recording. Re-loading the *same* job keeps it, because that is what
        naming one of several selected speakers does.
        """
        same = summary is not None and self.summary is not None and summary.job_id == self.summary.job_id
        self.summary = summary
        if not same:
            self._selected = set()
            if self.is_mounted:
                self.query_one(DataTable).clear()
        if summary is None:
            self._rows = {}
            if self.is_mounted:
                self.query_one(DataTable).clear()
                self.query_one("#warning", Static).display = False
                self.border_title = placeholder
                self.border_subtitle = ""
            return
        self._show_backend_warning()
        self._refresh()

    def _show_backend_warning(self) -> None:
        """Say so when this job's voices can never match the cache.

        Embeddings are filed per diarization backend and are different widths,
        so a job diarized with one backend against a cache built by another
        produces an empty Suggested column forever. Left unexplained that reads
        as "I don't know any of these people" rather than "I cannot look".
        """
        warning = self.query_one("#warning", Static)
        mismatch = self.app.workspace.embedding_mismatch(self.summary.job_id)
        if mismatch is None:
            warning.display = False
            return
        width, namespace = mismatch
        warning.display = True
        warning.update(
            f"⚠ this job's voice prints are {width}-dim; your cache holds {namespace} — no suggestions possible"
        )

    def _refresh(self) -> None:
        if self.summary is None:
            return
        table = self.query_one(DataTable)
        # Rebuilding puts the cursor back on row 0; a toggle like d has to find
        # the same speaker under the cursor when it is pressed again.
        current = self._selected_speaker()
        table.clear()
        self._rows = {}
        for row in self.app.workspace.speaker_rows(self.summary.job_id):
            self._rows[row.speaker] = row
            style = "dim strike" if row.excluded else ""
            cells = (
                row.speaker,
                "removed" if row.excluded else row.assigned_label or "—",
                row.suggested_label or ("—" if row.suggestion_score <= 0 else "(no match)"),
                f"{row.suggestion_score:.2f}" if row.suggestion_score > 0 else "—",
                f"{row.talk_seconds:.1f}",
            )
            table.add_row(
                SELECTED if row.speaker in self._selected else " ",
                *(Text(cell, style=style) for cell in cells),
                key=row.speaker,
            )
        self._selected &= set(self._rows)
        if current in self._rows:
            table.move_cursor(row=table.get_row_index(current))
        source = self.summary.source_path.name if self.summary.source_path else self.summary.job_id
        self.border_title = (
            f"{source} · {self.summary.duration_seconds:.0f}s · {self.summary.num_speakers} speakers"
        )
        pending = sum(1 for r in self._rows.values() if r.unnamed and r.suggested_label)
        unnamed = sum(1 for r in self._rows.values() if r.unnamed)
        removed = sum(1 for r in self._rows.values() if r.excluded)
        self.border_subtitle = (
            f"{unnamed} unnamed · {pending} suggestion(s) pending"
            + (f" · {removed} removed" if removed else "")
            + (f" · {len(self._selected)} selected" if self._selected else "")
            + "   ↵ accept"
        )

    def focus_table(self) -> None:
        self.query_one(DataTable).focus()

    # ----- acting on a speaker -------------------------------------------------

    def _selected_speaker(self) -> str | None:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return row_key.value

    def action_select(self) -> None:
        speaker = self._selected_speaker()
        if speaker is None:
            return
        self._selected ^= {speaker}
        self._refresh()

    def action_stop(self) -> None:
        self.app.player.stop()

    def action_play(self) -> None:
        speaker = self._selected_speaker()
        if speaker is None or self.summary is None:
            return
        if not self.app.player.available:
            self.notify("no audio player found (afplay / ffplay)", severity="error")
            return
        self.notify(f"exporting sample for {speaker}…")
        self._export_and_play(speaker)

    @work(thread=True, exclusive=True, group="sample")
    def _export_and_play(self, speaker: str) -> None:
        job_id = self.summary.job_id
        try:
            path = self.app.workspace.speaker_sample(job_id, speaker)
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(self.notify, str(exc), severity="error")
            return
        self.app.call_from_thread(self.app.player.play, path)

    @on(DataTable.RowSelected, "#speakers")
    def _accept_row(self, event: DataTable.RowSelected) -> None:
        """Enter accepts the suggestion on the row it is on.

        A focused DataTable takes enter for itself, so this has to be the row
        event rather than a binding - the binding would never fire.
        """
        event.stop()
        self.action_accept()

    def action_accept(self) -> None:
        speaker = self._selected_speaker()
        if speaker is None:
            return
        row = self._rows[speaker]
        if row.suggested_label is None:
            self.notify(f"no suggestion for {speaker} — press n to name them", severity="warning")
            return
        self._apply(lambda: self.app.workspace.accept_suggestion(self.summary.job_id, speaker))

    def action_accept_all(self) -> None:
        if self.summary is None:
            return
        self._apply(lambda: self.app.workspace.accept_all_suggestions(self.summary.job_id))

    def action_exclude(self) -> None:
        """Take a speaker out of the transcript altogether, or put them back."""
        speaker = self._selected_speaker()
        if speaker is None:
            return
        self._apply(lambda: self.app.workspace.toggle_speaker_excluded(self.summary.job_id, speaker))

    @on(DataTable.RowHighlighted, "#speakers")
    def _highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key.value is not None:
            self.post_message(SpeakerHighlighted(event.row_key.value))

    def action_unassign(self) -> None:
        speaker = self._selected_speaker()
        if speaker is None or self._rows[speaker].assigned_label is None:
            return
        self._apply(lambda: self.app.workspace.unassign_speaker(self.summary.job_id, speaker))

    def action_name(self) -> None:
        speaker = self._selected_speaker()
        if speaker is None:
            return
        row = self._rows[speaker]
        self._prompt_name(
            f"Name for {speaker}",
            row.assigned_label or row.suggested_label or "",
            lambda name: self._label(speaker, name),
        )

    def action_merge(self) -> None:
        """Name several speakers at once as one person, folding them into one voice."""
        if len(self._selected) < 2:
            self.notify("select two or more speakers with space, then press m", severity="warning")
            return
        chosen = sorted(self._selected)
        suggested = next(
            (self._rows[s].assigned_label or self._rows[s].suggested_label for s in chosen if self._rows[s].assigned_label or self._rows[s].suggested_label),
            "",
        )
        body = "\n".join(
            f"{s}  {self._rows[s].talk_seconds:.0f}s  {self._rows[s].assigned_label or self._rows[s].suggested_label or '—'}"
            for s in chosen
        )
        self.notify(body, title=f"merging {len(chosen)} speakers")
        self._prompt_name("Merge these speakers as", suggested or "", self._merge_as)

    def _merge_as(self, name: str) -> None:
        chosen = sorted(self._selected)
        self._selected = set()
        self._apply(lambda: self.app.workspace.merge_speakers(self.summary.job_id, chosen, name))

    def _prompt_name(self, title: str, initial: str, then) -> None:
        suggester = NameSuggester(self.app.workspace.pipeline.speaker_cache.names())

        def done(name: str | None) -> None:
            if name:
                then(name)

        self.app.push_screen(TextPromptModal(title, initial, suggester), done)

    def _label(self, speaker: str, name: str) -> None:
        """Assign a name, asking first when it would fold a stranger into a known voice.

        Same name usually means same person, and folding is what makes the cache
        learn. But two people do share first names, and one averaged centroid
        then matches neither of them - so a distant match is a question, not an
        assumption.
        """
        job_id = self.summary.job_id
        score = self.app.workspace.fold_similarity(job_id, speaker, name)
        threshold = self.app.workspace.pipeline.speaker_cache.threshold
        if score is None or score >= threshold:
            self._apply(lambda: self.app.workspace.label_speaker(job_id, speaker, name))
            return

        def chose(choice: str | None) -> None:
            if choice == "fold":
                self._apply(lambda: self.app.workspace.label_speaker(job_id, speaker, name))
            elif choice == "separate":
                self._apply(lambda: self.app.workspace.label_speaker(job_id, speaker, name, teach=False))

        self.app.push_screen(
            ChoiceModal(
                f"Fold into the {name} you already know?",
                f"{speaker} sounds {score:.2f} like the stored {name}.\n"
                f"Folding teaches the voice; keeping separate names this job only.",
                [("fold", "Fold in"), ("separate", "Keep separate"), ("cancel", "Cancel")],
            ),
            chose,
        )

    def _apply(self, operation) -> None:
        try:
            operation()
        except Exception as exc:  # noqa: BLE001
            self.notify(str(exc), severity="error")
            return
        self._refresh()
        # The host screen shows how many speakers are still unnamed; naming one
        # here is exactly what changes that number.
        self.post_message(SpeakersChanged())

    # ----- the transcript ------------------------------------------------------

    def action_transcript(self) -> None:
        if self.summary is None:
            return
        if self.summary.source_path is not None:
            default = str(self.summary.source_path.with_suffix(".txt"))
        else:
            default = str(Path.cwd() / f"{self.summary.job_id}.txt")
        unnamed = [s for s, row in self._rows.items() if row.unnamed]
        if unnamed:
            self.notify(
                f"{len(unnamed)} speaker(s) still unnamed — they will appear as their IDs",
                severity="warning",
            )

        job_id = self.summary.job_id

        def done(path: str | None) -> None:
            if path:
                self._write_transcript(job_id, path)

        self.app.push_screen(TextPromptModal("Write transcript to", default), done)

    @work(thread=True, exclusive=True, group="transcript")
    def _write_transcript(self, job_id: str, path: str) -> None:
        try:
            out, lines = self.app.workspace.write_transcript(job_id, Path(path))
            text = out.read_text(encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(self.notify, str(exc), severity="error")
            return
        self.app.call_from_thread(
            self.app.push_screen,
            TranscriptScreen(f"saved {lines} lines → {out}", text),
        )
