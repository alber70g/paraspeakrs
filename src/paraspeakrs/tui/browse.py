from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path

from rich.style import Style
from rich.text import Text
from textual import events
from textual.fuzzy import Matcher
from textual.widgets import DirectoryTree
from textual.widgets._tree import TreeNode


def fuzzy_matches(paths: Iterable[Path], query: str) -> list[Path]:
    """The entries whose name fuzzy-matches `query`, in the order given."""
    matcher = Matcher(query)
    return [path for path in paths if matcher.match(path.name)]


def best_match(names: Sequence[str], query: str) -> int | None:
    """Index of the strongest fuzzy match, or None when nothing matches.

    The tree keeps its own directories-then-alphabetical order, so the entry
    the query describes best is rarely the top one - the cursor has to find it.
    """
    matcher = Matcher(query)
    scores = [matcher.match(name) for name in names]
    best = max(scores, default=0.0)
    return scores.index(best) if best else None


#: What counts as a recording when summarising a directory. The tree still lists
#: every file; this only decides what the "2/5" beside a directory is out of.
AUDIO_SUFFIXES = frozenset(
    {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".aiff", ".aif", ".caf", ".webm", ".mp4"}
)


def created_at(path: Path) -> float:
    """When the file was created, falling back to mtime where the OS has no birth time."""
    try:
        stat = path.stat()
    except OSError:
        return 0.0
    return getattr(stat, "st_birthtime", stat.st_mtime)


def audio_progress(directory: Path, done: Iterable[Path]) -> tuple[int, int]:
    """(transcribed, total) recordings directly inside `directory`.

    Only the one level: walking everything below a directory as large as ~
    would stall the tree on every redraw, and the counts are about what you
    would find by opening it.
    """
    try:
        audio = [entry for entry in directory.iterdir() if entry.suffix.lower() in AUDIO_SUFFIXES and entry.is_file()]
    except OSError:
        return 0, 0
    finished = set(done)
    return sum(1 for entry in audio if entry.resolve() in finished), len(audio)


def default_browse_root() -> Path:
    """Where the file tree starts when nothing was remembered from last time.

    The audio being transcribed usually lives nowhere near the checkout - a
    recorder writes to its own Application Support directory - so the root is
    configurable rather than pinned to the working directory.
    """
    configured = os.getenv("PARAKEET_RECORDINGS_DIR")
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_dir():
            return candidate
    return Path.cwd()


class BrowserTree(DirectoryTree):
    """A DirectoryTree you can walk out of, not just down into.

    A plain DirectoryTree is trapped under its starting root, which is useless
    here because recordings live somewhere else entirely. Backspace re-roots to
    the parent, Enter re-roots into the highlighted directory, and Right expands
    a directory in place so it can be peeked at without moving the root.
    Textual's Tree binds neither backspace nor right on its own.

    Every entry is prefixed with its creation date, and ctrl+s flips the order
    between name and newest-first - recorders name files by device as often as
    by date, so the name alone rarely says which one was this morning's.

    Typing filters: printable keys build a fuzzy query that hides the
    non-matching entries of the rooted directory and parks the cursor on the
    strongest match, so a recording is a few letters away instead of a long
    scroll. Only that one level is filtered - an expanded subdirectory keeps
    all of its contents, because the query describes what you are looking for
    here, not everywhere below. Backspace edits the query, escape drops it.
    """

    BINDINGS = [
        ("backspace", "go_up", "Parent dir"),
        ("right", "expand_node", "Open"),
        ("left", "collapse_node", "Close"),
        ("ctrl+s", "toggle_sort", "Sort"),
    ]

    def __init__(self, path: str | Path) -> None:
        super().__init__(path)
        # Enter must re-root rather than expand, so the two actions stay distinct.
        self.auto_expand = False
        self.search = ""
        self.marks: dict[Path, str] = {}
        self.sort_by = "name"
        self._progress: dict[Path, tuple[int, int]] = {}
        self.border_title = self._title()

    def _title(self) -> str:
        prefix = f"filter: {self.search}" if self.search else "type to filter"
        return f"{prefix} · newest first" if self.sort_by == "created" else prefix

    def set_marks(self, marks: dict[Path, str]) -> None:
        """Replace the marks; the directory counts derived from them go stale with them."""
        self.marks = marks
        self._progress.clear()
        self.refresh()

    def render_label(self, node: TreeNode, base_style: Style, style: Style) -> Text:
        """Tag entries the caller has something to say about (already transcribed, queued).

        Without it the tree gives no hint which of thirty similarly named
        recordings have been through the pipeline, and the only way to find out
        is to transcribe them again.
        """
        name = super().render_label(node, base_style, style)
        path = getattr(node.data, "path", None)
        if path is None:
            return name
        label = Text(datetime.fromtimestamp(created_at(path)).strftime("%Y-%m-%d %H:%M  "), style="dim")
        label.append_text(name)
        mark = self.marks.get(path) or self._directory_mark(path)
        if mark:
            label.append(f"  {mark}", base_style)
        return label

    def _directory_mark(self, path: Path) -> str | None:
        """"3/5" for a directory with recordings in it, "5/5 ✓" once all are done.

        A ✓ on each recording only helps once you have opened the directory;
        this says which directories are worth opening at all.
        """
        if not self._safe_is_dir(path):
            return None
        if path not in self._progress:
            done = [p for p, mark in self.marks.items() if mark == "✓"]
            self._progress[path] = audio_progress(path, done)
        finished, total = self._progress[path]
        if not total:
            return None
        return f"{finished}/{total} ✓" if finished == total else f"{finished}/{total}"

    async def on_key(self, event: events.Key) -> None:
        if event.key == "backspace" and self.search:
            query = self.search[:-1]
        elif event.key == "escape" and self.search:
            query = ""
        elif event.is_printable and event.character is not None:
            # Space still toggles a node while no query is being typed; once one
            # is, it is a character like any other - filenames are full of them.
            if event.character == " " and not self.search:
                return
            query = self.search + event.character
        else:
            return
        event.stop()
        event.prevent_default()
        await self._search(query)

    async def _search(self, query: str) -> None:
        self.search = query
        self.border_title = self._title()
        await self.reload_node(self.root)
        children = self.root.children
        if not query or not children:
            return
        index = best_match([str(child.label) for child in children], query)
        if index is not None:
            self.move_cursor(children[index])

    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        entries = list(paths)
        if not self.search or not entries:
            return entries
        # Textual asks per directory, so the parent tells us whether this is the
        # level the tree is rooted at - the only one the query applies to.
        root = Path(self.path).expanduser().resolve()
        if entries[0].parent != root:
            return entries
        return fuzzy_matches(entries, self.search)

    def _populate_node(self, node: TreeNode, content: Iterable[Path]) -> None:
        # Textual sorts by name after filter_paths, so reordering has to happen here.
        if self.sort_by == "created":
            content = sorted(content, key=lambda path: (not self._safe_is_dir(path), -created_at(path)))
        super()._populate_node(node, content)

    async def action_toggle_sort(self) -> None:
        self.sort_by = "created" if self.sort_by == "name" else "name"
        self.border_title = self._title()
        await self.reload()

    async def watch_path(self) -> None:
        """A new root is a fresh start; the old query says nothing about it."""
        self.search = ""
        self.border_title = self._title()
        self._progress.clear()
        await super().watch_path()

    def action_go_up(self) -> None:
        current = Path(self.path).expanduser()
        if current.parent != current:
            self.path = current.parent

    def action_expand_node(self) -> None:
        node = self.cursor_node
        if node is not None and node.allow_expand:
            node.expand()

    def action_collapse_node(self) -> None:
        node = self.cursor_node
        if node is not None and node.is_expanded:
            node.collapse()
