"""The decisions and to-dos list: what the model may change, and what the user's corrections protect."""

from __future__ import annotations

import pytest

from paraspeakrs.live.items import ItemList, parse_updates


def _update(kind: str, text: str, *, id: str = "", owner: str = "", due: str = "") -> dict:
    return {"id": id, "kind": kind, "text": text, "owner": owner, "due": due}


def test_new_items_are_numbered_per_kind_and_shown_in_meeting_order():
    items = ItemList()
    assert items.apply([_update("todo", "Send the invite", owner="Ben")], at=50, section=0)
    assert items.apply([_update("decision", "Launch on 3 March"), _update("todo", "Book the room")], at=20, section=1)
    assert [(i.id, i.at, i.section) for i in items.visible()] == [("D1", 20, 1), ("T2", 20, 1), ("T1", 50, 0)]


def test_an_update_by_id_changes_the_item_and_keeps_what_it_leaves_empty():
    items = ItemList()
    items.apply([_update("todo", "Send the invite", owner="Ben", due="Friday")], at=10, section=0)
    # The model moves the date but does not repeat the owner: the owner was not cleared in the meeting.
    assert items.apply([_update("todo", "Send the invite", id="T1", due="Monday")], at=90, section=2)
    (item,) = items.visible()
    assert (item.owner, item.due, item.at, item.section) == ("Ben", "Monday", 10, 0)
    assert not items.apply([_update("todo", "Send the invite", id="T1", due="Monday")], at=95, section=2)


def test_the_model_cannot_change_an_item_the_user_edited_or_deleted():
    items = ItemList()
    items.apply([_update("todo", "Send the invite", owner="Ben"), _update("decision", "Go")], at=10, section=0)
    items.edit("T1", owner="Anna")
    items.delete("D1")

    updates = [_update("todo", "Send it", id="T1", owner="Ben"), _update("decision", "Stop", id="D1")]
    changed = items.apply(updates, at=20, section=0)

    assert not changed
    assert [(i.id, i.text, i.owner, i.edited) for i in items.visible()] == [("T1", "Send the invite", "Anna", True)]
    # A deleted item keeps its id: a new decision does not take D1 again.
    items.apply([_update("decision", "Ship it")], at=30, section=0)
    assert [i.id for i in items.visible()] == ["T1", "D2"]


def test_an_unknown_or_deleted_id_cannot_be_edited_or_deleted_and_an_edit_needs_a_text():
    items = ItemList()
    items.apply([_update("decision", "Go")], at=10, section=0)
    items.delete("D1")
    for call in (lambda: items.edit("D1", text="x"), lambda: items.delete("D1"), lambda: items.edit("T9", text="x")):
        with pytest.raises(KeyError):
            call()
    items.apply([_update("todo", "Book the room")], at=10, section=0)
    with pytest.raises(ValueError):
        items.edit("T1", text="  ")


def test_the_markdown_lists_both_kinds_with_owner_due_and_time():
    items = ItemList()
    updates = [_update("decision", "Launch on 3 March"), _update("todo", "Send the invite", owner="Ben", due="Friday")]
    items.apply(updates, at=75, section=0)
    assert items.markdown("Launch sync") == (
        "# Launch sync: decisions and to-dos\n\n"
        "## Decisions\n\n- Launch on 3 March [00:01:15]\n\n"
        "## To-dos\n\n- [ ] Send the invite (owner: Ben, due: Friday) [00:01:15]\n"
    )
    assert "## Decisions\n\nNone." in ItemList().markdown("Empty")


def test_a_reply_of_the_wrong_shape_is_refused_so_it_can_be_sent_back():
    assert parse_updates({"items": [{"kind": "todo", "text": " Book it ", "owner": None}]}) == [
        _update("todo", "Book it")
    ]
    for reply in ({}, {"items": "none"}, {"items": [{"kind": "task", "text": "x"}]}, {"items": [{"kind": "todo"}]}):
        with pytest.raises(ValueError):
            parse_updates(reply)
