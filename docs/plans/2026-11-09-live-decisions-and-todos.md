# Live meeting: decisions and to-dos

*Status: built 2026-11-09, item 3 in `FUTURE-FEATURES.md` "Order". Branch
`claude/wizardly-faraday-firlh4`.*

Goal: while the meeting runs, the page keeps a list of the decisions taken and the
to-dos agreed (with owner and date when they were said), which the user can correct,
and which is always available as Markdown.

## Chosen with the user (2026-11-09)

- **Detection: the decision model, then the chat model**, the pattern of the persona
  comments. Per batch the decision model says whether the new lines hold a decision or
  an agreed task; only on a yes does the chat model write or update the items. It sees
  the current list, so a new owner or a moved date updates the item instead of adding a
  second one.
- **The user can edit and delete items.** An edit wins over later updates from the model.
- **Export: a Markdown file and a Copy button.**

## Plan

1. **Items** — new `live/items.py`.
   - `Item`: `id` (`D1`, `T1`, ...), `kind` (`"decision"` | `"todo"`), `text`, `owner`
     (`""` when not said), `due` (as said: "Friday", "3 March"; `""` when not said), `at`
     (meeting seconds of the line it comes from), `section`, `edited` (bool), `deleted` (bool).
   - `ItemList`: `apply(updates, at, section)` for the model's updates (`{"id"?, "kind", "text",
     "owner", "due"}`; with an `id` of an existing item: an update; without: a new item; an
     update to an `edited` or `deleted` item is ignored), `edit(id, text?, owner?, due?)`
     (sets `edited`), `delete(id)` (sets `deleted`; the item stays in the list for the model as
     "removed by the user, do not add again"), `visible()`, `markdown(title)`. (The page builds the Copy text itself.)
   - Verify: unit tests for new/update/ignored-after-edit/delete, ids per kind, the Markdown.
2. **Analyzer** — `live/analyzer.py`.
   - The per-batch comment request gets two more yes/no questions: `outcome__decision`
     ("Do `new_lines` take a decision?") and `outcome__todo` ("Do `new_lines` agree on a task
     someone will do?"), with checkable true/false criteria. No extra decision call. When
     there are no personas the request is still sent for these two.
   - On a yes for either (threshold `Thresholds.item`, default 0.5, flag `--item-threshold`):
     one chat call with the brief context, the section so far, the new lines, the current
     items (visible ones with ids, and the deleted ones as "removed by the user") →
     `{"items": [...updates]}` through the shared `intake.ask_json` (one correction retry).
     Owner names come from the transcript as the page names the speakers (`_named` lines).
   - Emits `{"type": "items", "items": [visible items]}` after each change; the `decision`
     event gets `"items": {"decision": p, "todo": p}`.
   - Verify: tests with `FakeDecisions`/`FakeChat`: a yes adds an item, an update by id
     changes it, an edited item is not overwritten, a deleted one is sent as removed, a no
     costs no chat call, the questions are in the same request as the personas.
3. **Server** — `live/server.py`.
   - `GET /api/items` → `{"items": [...]}`; `POST /api/items/{id} {text?, owner?, due?}` →
     `{"item": item}`; `DELETE /api/items/{id}`; 404 unknown id, 409 before Start.
   - `GET /api/items.md` → the Markdown (`text/markdown`, as a download named
     `<brief-slug>-decisions-and-todos.md`).
   - The Markdown is also written to `<session>/decisions-and-todos.md` after every change,
     so it is there whenever the meeting ends or the process stops.
   - Verify: API tests including 404/409.
4. **Page** — `live/page.html`. A sidebar box "Decisions and to-dos", shown once the
   meeting runs: two groups (Decisions, To-dos), each item with its text, owner, due date and
   time; clicking the time scrolls the timeline to that moment; **Edit** (inline: text,
   owner, due; Save / Cancel) and **Delete** (confirm). Buttons **Download .md** (the
   endpoint) and **Copy** (plain text to the clipboard). Built from the `items` events, so a
   reload restores it. The decision lines show `decision p · to-do p`.
   - Verify: browser check against fake models (`fake_live.py` in the scratchpad extended with
     outcome answers), `bun build --no-bundle` on the extracted script.
5. **Docs**: README live section, CHANGELOG, plan status.

## Out of scope

Adding items by hand, parsing dates, carrying items into the next meeting, restoring items
after a restart ("continue meetings"), the summary and follow-up mail (item 5, which will
read this list).
