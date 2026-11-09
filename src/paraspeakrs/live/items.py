"""The decisions and to-dos of a meeting, as the chat model finds them and the user corrects them.

The model adds and updates items; an edit or a delete by the user wins over later
updates from the model. A deleted item stays in the list, so the model can be told
it was removed and should not be added again.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from ..transcript_txt import format_time

KINDS = ("decision", "todo")
_PREFIX = {"decision": "D", "todo": "T"}
_HEADINGS = {"decision": "Decisions", "todo": "To-dos"}


@dataclass
class Item:
    id: str
    kind: str
    text: str
    owner: str = ""
    # As it was said ("Friday", "3 March"): dates are not parsed.
    due: str = ""
    # Meeting seconds of the batch it comes from.
    at: float = 0.0
    section: int = 0
    edited: bool = False
    deleted: bool = False

    def to_json(self) -> dict:
        out = asdict(self)
        del out["deleted"]  # the page only ever gets visible items
        return out


def parse_updates(reply: dict) -> list[dict]:
    """The model's ``{"items": [...]}``; a ValueError makes ``ask_json`` send it back once."""
    updates = reply.get("items")
    if not isinstance(updates, list):
        raise ValueError("`items` must be a list")  # noqa: TRY004 - ask_json resends on ValueError
    out = []
    for raw in updates:
        if not isinstance(raw, dict):
            raise ValueError("each item must be an object")  # noqa: TRY004 - as above
        update = {k: str(raw.get(k) or "").strip() for k in ("id", "kind", "text", "owner", "due")}
        if update["kind"] not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}, not {update['kind']!r}")
        if not update["text"]:
            raise ValueError("each item needs a text")
        out.append(update)
    return out


class ItemList:
    def __init__(self) -> None:
        self.items: list[Item] = []

    def apply(self, updates: list[dict], at: float, section: int) -> bool:
        """The model's updates: with the id of an item a change to it, otherwise a new item.

        Returns whether anything changed. An empty owner or due in an update keeps the
        one the item has: the model leaving a field out is not the meeting clearing it.
        """
        changed = False
        for update in updates:
            item = self._find(update.get("id", ""))
            if item is None:
                kind = update["kind"]
                number = sum(1 for i in self.items if i.kind == kind) + 1
                self.items.append(
                    Item(f"{_PREFIX[kind]}{number}", kind, update["text"], update["owner"], update["due"], at, section)
                )
                changed = True
            elif not (item.edited or item.deleted):
                for field in ("text", "owner", "due"):
                    if update[field] and update[field] != getattr(item, field):
                        setattr(item, field, update[field])
                        changed = True
        return changed

    def edit(self, item_id: str, *, text: str | None = None, owner: str | None = None, due: str | None = None) -> Item:
        """The user's correction; from now on the model leaves the item alone."""
        item = self._visible(item_id)
        if text is not None and not text.strip():
            raise ValueError("an item needs a text")
        for field, value in (("text", text), ("owner", owner), ("due", due)):
            if value is not None:
                setattr(item, field, value.strip())
        item.edited = True
        return item

    def delete(self, item_id: str) -> None:
        self._visible(item_id).deleted = True

    def visible(self) -> list[Item]:
        return sorted((i for i in self.items if not i.deleted), key=lambda i: i.at)

    def markdown(self, title: str) -> str:
        out = [f"# {title}: decisions and to-dos"]
        for kind in KINDS:
            lines = [_markdown_line(i) for i in self.visible() if i.kind == kind]
            out += ["", f"## {_HEADINGS[kind]}", "", *(lines or ["None."])]
        return "\n".join(out) + "\n"

    def _find(self, item_id: str) -> Item | None:
        return next((i for i in self.items if i.id == item_id), None) if item_id else None

    def _visible(self, item_id: str) -> Item:
        item = self._find(item_id)
        if item is None or item.deleted:
            raise KeyError(item_id)
        return item


def _markdown_line(item: Item) -> str:
    details = [f"{label}: {value}" for label, value in (("owner", item.owner), ("due", item.due)) if value]
    line = ("- [ ] " if item.kind == "todo" else "- ") + item.text
    if details:
        line += f" ({', '.join(details)})"
    return f"{line} [{format_time(item.at)}]"
