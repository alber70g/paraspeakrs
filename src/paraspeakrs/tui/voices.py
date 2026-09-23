from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Footer, Header, Static, Tree

from ..labeling import Voice, VoiceMember
from .modals import NameSuggester, TextPromptModal, confirm


class VoicesScreen(Screen):
    """The voice cache, opened up: every name and the recordings it was learned from.

    A centroid on its own is unarguable - you cannot tell whether two names are
    the same person, or whether one of them quietly absorbed a stranger. Listing
    the members makes both checkable, and gives merge and unfold something
    concrete to act on.
    """

    BINDINGS = [
        ("p", "play", "Play"),
        ("s", "stop", "Stop"),
        ("m", "merge", "Merge into"),
        ("u", "unfold", "Unfold"),
        ("d", "delete", "Delete voice"),
        ("r", "refresh", "Refresh"),
        ("escape", "app.pop_screen", "Back"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Tree("voices", id="voices")
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        tree = self.query_one(Tree)
        tree.show_root = False
        tree.focus()
        self.action_refresh()

    def action_refresh(self) -> None:
        tree = self.query_one(Tree)
        tree.clear()
        voices = self.app.workspace.voices()
        for voice in voices:
            count = len(voice.members)
            node = tree.root.add(f"{voice.name}    {count} recording{'s' if count != 1 else ''}", data=voice)
            for member in voice.members:
                node.add_leaf(self._member_label(member), data=member)
        namespace = self.app.workspace.pipeline.speaker_cache.namespace
        self.query_one("#status", Static).update(
            f"{len(voices)} voice(s) · {namespace}" if voices else f"no voices learned yet · {namespace}"
        )

    def _member_label(self, member: VoiceMember) -> str:
        source = member.source or "recording gone"
        added = (member.added or "")[:10]
        if member.job_id is None:
            # Labeled outside a job (the CLI's label-dir), or learned before the
            # cache kept provenance: either way there is nothing to point at.
            return f"{member.speaker_id or 'unknown speaker'}    no recording kept"
        return f"{source}    {member.speaker_id}    {added}{'' if member.playable else '    (not playable)'}"

    def _selection(self) -> Voice | VoiceMember | None:
        node = self.query_one(Tree).cursor_node
        return node.data if node is not None else None

    def _voice_of(self, member: VoiceMember) -> str | None:
        node = self.query_one(Tree).cursor_node
        parent = node.parent if node is not None else None
        return parent.data.name if parent is not None and isinstance(parent.data, Voice) else None

    def action_stop(self) -> None:
        self.app.player.stop()

    def action_play(self) -> None:
        selection = self._selection()
        if not isinstance(selection, VoiceMember):
            self.notify("open a voice and put the cursor on one of its recordings", severity="warning")
            return
        if not selection.playable:
            self.notify("that recording is gone — nothing left to play", severity="warning")
            return
        if not self.app.player.available:
            self.notify("no audio player found (afplay / ffplay)", severity="error")
            return
        self.query_one("#status", Static).update(f"exporting {selection.speaker_id}…")
        self._export_and_play(selection.job_id, selection.speaker_id)

    @work(thread=True, exclusive=True, group="sample")
    def _export_and_play(self, job_id: str, speaker_id: str) -> None:
        try:
            path = self.app.workspace.speaker_sample(job_id, speaker_id)
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(self.notify, str(exc), severity="error")
            return
        self.app.call_from_thread(self.app.player.play, path)
        self.app.call_from_thread(self.query_one("#status", Static).update, f"playing {speaker_id}")

    def action_merge(self) -> None:
        selection = self._selection()
        if not isinstance(selection, Voice):
            self.notify("put the cursor on a voice to merge it into another", severity="warning")
            return
        others = [name for name in self.app.workspace.pipeline.speaker_cache.names() if name != selection.name]
        if not others:
            self.notify("there is no other voice to merge into", severity="warning")
            return

        def done(target: str | None) -> None:
            if not target or target == selection.name:
                return
            try:
                self.app.workspace.merge_voices(selection.name, target)
            except Exception as exc:  # noqa: BLE001
                self.notify(str(exc), severity="error")
                return
            self.notify(f"{selection.name} folded into {target}")
            self.action_refresh()

        self.app.push_screen(
            TextPromptModal(f"Merge {selection.name} into", "", NameSuggester(others)),
            done,
        )

    def action_unfold(self) -> None:
        selection = self._selection()
        if not isinstance(selection, VoiceMember):
            self.notify("put the cursor on one of a voice's recordings to unfold it", severity="warning")
            return
        if selection.job_id is None or selection.speaker_id is None:
            self.notify("this one was learned before recordings were kept — it cannot be unfolded", severity="warning")
            return
        name = self._voice_of(selection)
        if name is None:
            return
        try:
            self.app.workspace.unfold_voice(name, selection.job_id, selection.speaker_id)
        except Exception as exc:  # noqa: BLE001
            self.notify(str(exc), severity="error")
            return
        self.notify(f"{selection.speaker_id} is unnamed again")
        self.action_refresh()

    def action_delete(self) -> None:
        selection = self._selection()
        if not isinstance(selection, Voice):
            self.notify("put the cursor on a voice to delete it", severity="warning")
            return

        def chose(choice: str | None) -> None:
            if choice != "yes":
                return
            try:
                self.app.workspace.delete_voice(selection.name)
            except Exception as exc:  # noqa: BLE001
                self.notify(str(exc), severity="error")
                return
            self.action_refresh()

        self.app.push_screen(
            confirm(
                f"Forget the voice {selection.name}?",
                f"{len(selection.members)} recording(s) taught it. Names already assigned to jobs stay put.",
                "Forget",
            ),
            chose,
        )
