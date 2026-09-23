from __future__ import annotations

import os
from collections.abc import Sequence

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.suggester import Suggester
from textual.widgets import Button, Input, Static


class PathSuggester(Suggester):
    """Inline filesystem completion for a path Input. Accept with Right arrow."""

    def __init__(self) -> None:
        super().__init__(use_cache=False, case_sensitive=True)

    async def get_suggestion(self, value: str) -> str | None:
        expanded = os.path.expanduser(value)
        directory, sep, prefix = expanded.rpartition("/")
        directory = directory + sep if sep else "."
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            return None
        for name in names:
            if not name.startswith(prefix):
                continue
            suffix = name[len(prefix):]
            if os.path.isdir(os.path.join(directory, name)):
                suffix += "/"
            return value + suffix
        return None


class NameSuggester(Suggester):
    """Completion over the names already in the voice cache.

    Retyping a known name by hand is how "Michiel" and "michiel" end up as two
    voices competing for the same speakers; completing from what is stored keeps
    the duplicate from being created in the first place.
    """

    def __init__(self, names: Sequence[str]) -> None:
        super().__init__(use_cache=False, case_sensitive=False)
        self._names = list(names)

    async def get_suggestion(self, value: str) -> str | None:
        lowered = value.lower()
        return next((name for name in self._names if name.lower().startswith(lowered)), None)


class TextPromptModal(ModalScreen[str | None]):
    """One-line text prompt. Dismisses with the entered string, or None on cancel."""

    BINDINGS = [("escape", "dismiss", "Cancel")]

    def __init__(self, title: str, initial: str = "", suggester: Suggester | None = None) -> None:
        super().__init__()
        self._title = title
        self._initial = initial
        self._suggester = suggester

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._title)
            yield Input(value=self._initial, id="value", suggester=self._suggester)
            yield Button("OK", variant="primary", id="ok")

    def on_mount(self) -> None:
        self.query_one("#value", Input).focus()

    @on(Input.Submitted, "#value")
    def _submit(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    @on(Button.Pressed, "#ok")
    def _ok(self) -> None:
        self.dismiss(self.query_one("#value", Input).value.strip() or None)

    def action_dismiss(self) -> None:
        self.dismiss(None)


class ChoiceModal(ModalScreen[str | None]):
    """A question with named buttons. Dismisses with the chosen id, or None."""

    BINDINGS = [("escape", "dismiss", "Cancel")]

    def __init__(self, title: str, body: str, choices: Sequence[tuple[str, str]]) -> None:
        super().__init__()
        self._title = title
        self._body = body
        self._choices = list(choices)

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._title, classes="modal-title")
            yield Static(self._body)
            with Horizontal(classes="modal-buttons"):
                for index, (choice_id, label) in enumerate(self._choices):
                    yield Button(label, id=f"choice-{choice_id}", variant="primary" if index == 0 else "default")

    def on_mount(self) -> None:
        self.query(Button).first().focus()

    @on(Button.Pressed)
    def _chose(self, event: Button.Pressed) -> None:
        self.dismiss(str(event.button.id).removeprefix("choice-"))

    def action_dismiss(self) -> None:
        self.dismiss(None)


def confirm(title: str, body: str, verb: str = "Yes") -> ChoiceModal:
    return ChoiceModal(title, body, [("yes", verb), ("no", "Cancel")])
