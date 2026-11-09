# Live meeting: the persona package

*Status: built 2026-11-09 on branch `claude/wizardly-faraday-firlh4`. Steps 1-4 have tests
(`test_live_personas.py`, the persona tests in `test_live_analyzer.py` and `test_live_server.py`).
Decisions taken while building: below, accepted by the user 2026-11-09.*

Goal: help steer the meeting while it runs. Personas become reusable entities, can be added,
paused and removed during a meeting, can rerun over what was already said, and a
"Facilitator" preset suggests questions to ask. Decisions behind this are in
`FUTURE-FEATURES.md` ("Persona package: decisions").

## What exists now

- `live/brief.py`: `Persona` (key, name, viewpoint, comment_when, skip_when, style) lives inside
  `Brief.personas`; `Brief._unique_persona_keys` slugs and dedups keys.
- `live/intake.py`: the questionnaire. `next_turn(chat, history)` returns a question
  (`IntakeQuestion`: question, options, why) or a `Brief`; one correction retry on a bad shape.
- `live/analyzer.py`: `Analyzer.feed(lines)` → sections (`Section`: index, lines, title, closed,
  comments per persona key) and, per batch, one decision call asking every persona at once
  (`_consider_comments`), then `_comment(persona, section, lines, p)` per yes. Emits `decision`
  and `comment` events (`comment`: section, persona, name, p, at, text).
- `live/server.py`: `LiveSession` (brief, analyzer, heard lines, events, SSE stream);
  `/api/intake`, `/api/briefs`, `/api/start`, `/api/ask`, `/api/state`, `/api/events`, plus
  the speaker endpoints. `session.start(brief)` saves the brief to the session folder and to
  `<workspace>/live/briefs/<slug>.json`.
- `live/page.html`: brief editor (JSON textarea + rendered view), persona panel while running
  (`#personabox`), timeline with `comment` nodes (`place(node, t, kind)`), personas coloured
  `--p1..--p6` by index.

## Plan

1. **Persona library** — new `live/personas.py`.
   - `PersonaLibrary(directory)`: `list()`, `save(persona)`, `delete(key)`; one JSON file per
     persona in `<workspace>/live/personas/<key>.json`.
   - Built-in presets (`PRESETS`), listed with `"builtin": true`; a saved persona with the same key
     replaces the preset. Preset "Facilitator": comments are questions the user could ask now
     to steer (unclear owner, open decision, drifting off the agenda, someone not heard).
   - Verify: unit tests for save/list/delete, a preset overridden by a saved one.
2. **Proposal questionnaire** — in `live/personas.py`.
   - `next_persona_turn(chat, history, *, brief, transcript)`: system prompt like the intake's,
     but it reads the meeting so far (brief context + the last ~6000 chars of transcript) and
     proposes personas fitted to it. At most 3 questions, then
     `{"type": "personas", "personas": [1-3 Persona]}`.
   - Share the JSON + one-retry logic with `intake.next_turn` (extract a helper); do not copy it.
   - Verify: tests with `FakeChat` for a question turn, a personas turn, one bad-shape retry.
3. **Analyzer** — `live/analyzer.py`.
   - `paused: set[str]`; `_consider_comments` asks only active personas.
   - `add_persona(persona, *, rerun)`: unique key against the brief, append to
     `brief.personas`, emit `personas`. With `rerun`, a background task goes over the sections
     as they were at that moment, in batches of `rerun_step` (30 s) of meeting, asking only the
     new persona; `section_so_far` is the section up to that batch, never later lines.
     Comments get `"rerun": true` and land at their batch's `at`. Progress events
     `{"type": "rerun", "persona", "done", "of", "state": "running"|"done"}`.
   - `pause(key, paused)`, `remove(key)` (earlier comments stay), each emits `personas`
     (`{"type": "personas", "personas": [...], "paused": [...]}`).
   - `_comment` takes the context lines explicitly, so a rerun cannot see the future.
   - Verify: tests that a rerun comment never cites lines after its batch, a paused persona is
     not in any decision request, remove keeps its comments, `finish` waits for a rerun.
4. **Server** — `live/server.py`.
   - `GET /api/personas/library`, `POST /api/personas/library {persona}`,
     `DELETE /api/personas/library/{key}`.
   - `POST /api/personas/suggest {history}` → `{history, question | personas}` (works before
     and during the meeting; uses `session.heard` and the brief).
   - `POST /api/personas {persona, rerun}` (409 before Start), `POST /api/personas/{key}/pause
     {paused}`, `DELETE /api/personas/{key}`.
   - Every change saves the brief to the session folder and `live/briefs/<slug>.json`.
   - Verify: API tests with fake models, including 404/409/422 cases.
5. **Page** — `live/page.html`. Can go to a Sonnet subagent with steps 3-4 as the contract.
   - Running persona panel: per persona Pause/Resume, Remove, Save to library; rerun progress.
   - "Add persona": the library list (Add from now / Add and rerun), and "Suggest from the
     meeting" → questionnaire UI (question, options, own answer) → proposals, each with
     Add from now / Add and rerun / Save to library.
   - Brief editor: "Add from library" inserts into the brief before Start.
   - Comments sidebar: every comment (persona colour, time, first words, "added later" for
     reruns); a click scrolls the timeline to it and highlights it. Timeline comment nodes get
     ids.
   - Verify: browser check with `live replay --speed 0` against fake or real models
     (chrome-devtools MCP), plus `bun build --no-bundle` on the extracted script.
6. **Docs**: README live section, CHANGELOG, DECISIONS.md entry, memory.

## Decisions taken while building (accepted 2026-11-09)

- **What a rerun sees.** ← chosen: at each 30 s step, the section up to and with
  that step, and only the comments the persona made about earlier moments. The
  alternative, the whole section as it is now, is simpler but lets a comment about
  minute 3 cite minute 20.
- **Pause during a rerun.** ← chosen: pausing stops the rerun; resuming continues
  from now only. Rejected: a rerun that waits while paused, because the end of the
  meeting waits for reruns and would then wait for the user; and a rerun that
  ignores the pause, because a paused persona would still cost decision calls.
- **Keys.** ← chosen: a persona added during the meeting never gets the key of one
  removed earlier (`scope-guard-2`), so the removed one's comments stay its own and
  keep their colour. Colours on the page go by key, not position.

## Open points (decide while building, or ask)

- Persona colours are by index; removing one must not recolour the others (key → colour map).
- Rerun cost: one decision call per 30 s batch of the meeting so far, plus a chat call per yes.

## Out of scope here

Decisions and to-dos, catch me up, summary and mail, final pass, search: see `FUTURE-FEATURES.md`
"Order". Restoring analysis state on restart belongs to "continue meetings".
