from __future__ import annotations

from pathlib import Path

from textual.app import App

from ..labeling import LabelingWorkspace
from .home import HomeScreen
from .note import NotePanel
from .player import SamplePlayer
from .state import UiState


class DiarizeApp(App):
    """Terminal UI over the diarize / label / transcribe pipeline."""

    TITLE = "parakeet · diarize"
    BINDINGS = [("q", "quit", "Quit")]
    CSS = """
    /* Masters left, detail right. The detail is wider because a speaker table
       is six columns and a note is prose, while every list on the left is a
       filename and a number. */
    #panes { height: 1fr; }
    #masters { width: 2fr; }
    #detail { width: 3fr; }

    /* One rule for every panel box, so the borders line up across the two
       columns instead of each panel inventing its own margins. */
    #masters > BrowserTree, #masters > Vertical, SpeakerPanel, LinesPanel, NotePanel {
        border: round $panel; margin: 0 1 1 0; padding: 0;
    }
    #masters > BrowserTree, #masters > Vertical { margin-left: 1; }
    #masters > BrowserTree { height: 1fr; min-height: 8; }
    #processed-pane, #queue-pane { height: auto; max-height: 40%; }
    /* The transcript gets the height: it is what you scroll through. The speaker
       table is a handful of rows, and the note a sentence or two. */
    #speaker-panel { height: auto; max-height: 35%; min-height: 8; }
    #lines-pane { height: 1fr; min-height: 6; }
    #note-pane { height: 6; }
    #panes > * { margin-top: 1; }

    /* A focused panel is the one the keys go to, so it has to be the one that
       looks live - five bordered boxes are otherwise indistinguishable. */
    #masters > BrowserTree:focus-within, #masters > Vertical:focus-within,
    SpeakerPanel:focus-within, LinesPanel:focus-within, NotePanel:focus-within { border: round $accent; }

    DataTable { height: auto; max-height: 100%; }
    #speakers { height: 1fr; }
    #note { height: 1fr; border: none; padding: 0 1; background: $surface; }
    #note-header { padding: 0 1; color: $text-muted; }
    #lines { height: 1fr; border: none; }
    #status { padding: 0 1; color: $text-muted; }
    #warning { padding: 0 1; color: $warning; }
    ProgressBar { padding: 0 1; }
    #transcript { height: 1fr; border: round $panel; margin: 1; }
    TextPromptModal, ChoiceModal { align: center middle; }
    TextPromptModal > Vertical, ChoiceModal > Vertical {
        width: 64; height: auto; padding: 1 2; border: thick $primary; background: $surface;
    }
    ChoiceModal.wide > Vertical { width: 78; }
    .modal-title { text-style: bold; padding-bottom: 1; }
    .modal-buttons { height: auto; padding-top: 1; align-horizontal: right; }
    .modal-buttons Button { margin-left: 1; }
    """

    def __init__(self, workspace: LabelingWorkspace, state_path: Path) -> None:
        super().__init__()
        self.workspace = workspace
        self.state = UiState.load(state_path)
        self.player = SamplePlayer()
        # Set by main() once logging goes to a file; named in failure messages.
        self.log_path: Path | None = None

    def on_mount(self) -> None:
        self.push_screen(HomeScreen())

    def on_unmount(self) -> None:
        self.player.stop()
        # Quitting straight out of the note field is not a decision to discard
        # what was typed in it; the blur that normally saves never happens.
        for panel in self.query(NotePanel):
            panel.save()
        self.state.save()
