# Live meeting analysis: personas, a decision model, and questions during the meeting

Date: 2026-11-09. Status: replay spike built (`paraspeakrs live`). Live audio not started.
Verified: unit tests against fake models, and a browser run of the whole flow (questionnaire,
brief, replay, comments, a question) against fake servers on ports 8080 and 8000.
Not verified: the real models on the MacBook Pro M5.

## Goal

While a meeting is still going on:

1. The transcript is cut into **sections by topic**.
2. For each **persona** (a viewpoint, such as "budget keeper"), a **decision model** decides
   whether the newest lines deserve a comment. What deserves one is different for every meeting
   and comes from the user.
3. A **chat model** writes those comments in the persona's voice and **answers questions** about
   the meeting so far.
4. Before the meeting, a **questionnaire led by the chat model** works out the personas, the
   participants and the comment criteria.

Everything runs on local models: a decision model that speaks the JEV Decisions API at
`localhost:8080`, and an OpenAI-compatible chat model at `localhost:8000/v1`.

Out of scope for the spike: live audio (see [Toward live audio](#toward-live-audio)), more than one
meeting per process, authentication, editing comments, exporting a report.

## Decisions taken

| Topic | Decision |
|---|---|
| Deliverable | This plan, plus a spike that replays a finished transcript as if live. Proves the analysis without touching the recorder. |
| Surface | A local web page served by `paraspeakrs live replay`, updated with server-sent events. |
| Section | A topic change, decided by the decision model on every batch of about 30 s of meeting. A section shorter than 60 s is never closed by a topic change; one longer than 15 min is closed regardless. |
| Persona | Both meanings. **Personas** are the viewpoints that comment; **participants** are the people in the meeting, with role and goals, so both models know who wants what. |
| Per-meeting input | A questionnaire led by the chat model, one multiple-choice question per turn with a free-text answer always possible. It ends in a **brief** (JSON) the user can edit before starting. |
| Decision model | Any server that speaks the JEV Decisions API. Default `http://localhost:8080/v1/decisions`, model `typesafe/jev-1.14`. |
| Chat model | Any OpenAI-compatible server. Default `http://localhost:8000/v1`, first model it lists. `<think>` blocks are stripped. |
| Storage | Each run writes `brief.json` and `events.jsonl` to `~/.local/share/paraspeakrs/live/sessions/<time>-<name>/`. Briefs are also kept in `live/briefs/` and offered again on the next run. |

## How it works

```
 transcript lines ──► batches of ~30 s ──► page (shown immediately)
                             │
                             ▼
                        Analyzer.feed
             ┌───────────────┴────────────────┐
   decision call 1                       decision call 2
   topic_changed (noul)                  comment__<persona> (noul), one per persona,
   first_new_line (choice)               all in one request
             │                                │
   close section, cut the batch      p ≥ threshold → chat: write the comment
   at the first new-topic line                 (personas in parallel)
   chat (background): title the
   closed section
```

Each batch costs at most two decision calls; JEV answers in well under a second. Only a "yes"
costs a chat call. When the models fall behind, the batches that piled up are analyzed together
instead of one by one, so the analysis catches up instead of drifting further behind.

Questions from the page go straight to the chat model with the brief, all lines heard so far
(including lines not analyzed yet), section titles and comments. When that is longer than
40,000 characters the oldest lines are left out.

## The brief

```json
{
  "title": "Launch planning sync",
  "language": "English",
  "purpose": "Fix the launch date and budget for the new onboarding flow.",
  "context": "Last launch slipped six weeks because no one owned QA sign-off. Cap: 40k euro.",
  "section_rule": "each agenda item: date, budget, hiring",
  "agenda": ["Launch date", "Budget", "Hiring"],
  "participants": [{"name": "Chloe", "role": "finance", "goals": "stay under the cap"}],
  "personas": [{
    "name": "Owner hawk",
    "viewpoint": "every commitment needs one named owner",
    "comment_when": "a date, deadline or deliverable is agreed without a named owner",
    "skip_when": "the owner is named in the same exchange, or it is only discussion",
    "style": "one pointed question"
  }]
}
```

Who reads what:

- **Decision model:** `title`, `purpose`, `agenda` and `participants` in every request's state;
  `comment_when` and `skip_when` as the true and false criteria of the persona's question;
  `section_rule` added to the topic question.
- **Chat model:** everything, `context` included, as background in the system prompt; the
  persona's `viewpoint`, `comment_when` and `style` when it writes that persona's comments.

## The questionnaire

`live/intake.py`. The system prompt tells the chat model what the two models will do with its
findings, so it can aim its questions at the criteria and the background. It must uncover the
purpose, the participants, the viewpoints (and suggest ones the user did not think of), per
persona what deserves a comment and what does not, what counts as a new section, the background,
and the language.

Each turn the model replies with one JSON object, either a question:

```json
{"type": "question", "question": "What went wrong last time that you want to catch early?",
 "options": ["Dates without owners", "Costs creeping up", "Depending on one contractor"],
 "why": "Turns into the comment criteria."}
```

or the brief: `{"type": "brief", "brief": {...}}`. After 10 questions it is told to write the
brief; the page's "Write the brief now" button does the same earlier. A reply that is not JSON, or
does not fit the shape, is sent back once to be fixed. The history lives in the page and is posted
back each turn, so the server keeps no questionnaire state.

The questionnaire asks for criteria that can be checked against a transcript ("a deadline is
agreed without an owner"), not vague ones ("something important happens"). Vague criteria are the
main cause of noisy comments; see [Risks](#risks-and-open-questions).

## Decision requests

Topic check, sent when the current section is at least 60 s long:

```json
{
  "model": "typesafe/jev-1.14",
  "state": {
    "meeting": {"title": "...", "purpose": "...", "agenda": ["..."], "participants": ["..."]},
    "current_section": "[00:00:02] Anna: ... (at most the last 6000 characters)",
    "new_lines": "[00:01:25] Anna: Fine. That covers the date I think.\n[00:01:35] Chloe: Moving on to the budget then."
  },
  "questions": {
    "topic_changed": {
      "type": "noul",
      "instructions": "Do `new_lines` move the meeting on to a different topic than `current_section`?",
      "criteria": {
        "true": "The new lines start a different subject, agenda item, problem or decision than the current section. In this meeting a new section starts when: each agenda item: date, budget, hiring",
        "false": "The new lines continue, deepen or briefly digress from the subject of the current section."
      }
    },
    "first_new_line": {
      "type": "choice",
      "instructions": "Suppose `new_lines` move on to a different topic than `current_section`: which line is the first one on the new topic?",
      "criteria": {"L0": "[00:01:25] Anna: Fine. ...", "L1": "[00:01:35] Chloe: Moving on ..."}
    }
  }
}
```

When `topic_changed` is at or above the threshold, the batch is cut at `first_new_line`: the lines
before it stay in the old section, the rest start the new one. Without the choice question,
sections would start at batch edges, up to 30 s off.

Comment check, sent on every batch, one question per persona in a single request:

```json
{
  "state": {
    "meeting": {"title": "..."},
    "section_so_far": "...",
    "new_lines": "...",
    "already_commented": {"owner-hawk": ["Who owns the QA sign-off for the March 3rd date?"]}
  },
  "questions": {
    "comment__owner-hawk": {
      "type": "noul",
      "instructions": "Do `new_lines` give the Owner hawk (every commitment needs one named owner) something to comment on? Read `section_so_far` for context, and do not count what `already_commented.owner-hawk` already covers.",
      "criteria": {"true": "<comment_when>", "false": "<skip_when>"}
    }
  }
}
```

`already_commented` holds what each persona said in the current section, so it does not repeat
itself; it starts empty in each new section. Comments are written before the next batch is
decided, so the next decision already sees them.

State stays small on purpose: JEV moves requests over about 32 KB of text to a slower, less
accurate long-context mode.

## Chat prompts

| Call | When | Prompt, in short |
|---|---|---|
| Comment | A persona's p ≥ threshold | System: "You are the {persona}…", angle, when it speaks up, style, language, brief background. User: section so far, the new lines, what it already said. |
| Title | A section closes (in the background) | "Give this part of a meeting a title of 2 to 6 words, in {language}." |
| Answer | The user asks | System: answer from the transcript so far, say so when it is not there, be brief. User: transcript with section headers and comments, then the question. |
| Questionnaire | Before the meeting | See above. |

## The web page

One page, two columns (one on a phone):

- **Left:** the transcript as it arrives, grouped under section headers ("2. Budget for the
  launch · 00:01:35 · topic changed"), with comments in persona colours under the line that
  triggered them, and a faint row with every probability per batch ("topic 0.88 · Owner hawk 0.12
  · Budget keeper 0.81"). The probabilities can be hidden; they are there to pick thresholds.
- **Right, before the meeting:** describe the meeting → questionnaire → brief (readable summary,
  editable as JSON) → Start meeting. A brief from an earlier run can be picked instead.
- **Right, during the meeting:** a question box with the answers below it, and the personas.

API: `GET /api/state`, `POST /api/intake`, `GET /api/briefs`, `POST /api/start`, `POST /api/ask`,
`GET /api/events` (server-sent events; every past event first, so a reload loses nothing).

## Trying it on the Mac

```sh
paraspeakrs live check                         # one call to each model, says what works
paraspeakrs live replay meeting.txt            # opens http://127.0.0.1:8765/
paraspeakrs live replay meeting.txt --speed 1 --brief ~/.local/share/paraspeakrs/live/briefs/launch-planning-sync.json
```

`live check` also tries `/decisions`, `/api/alpha/decisions` and `/api/v1/decisions` when the
configured URL answers 404, and prints the one that works. URLs and models can be set with
flags or with `PARASPEAKRS_DECISIONS_URL`, `PARASPEAKRS_DECISIONS_MODEL`, `PARASPEAKRS_CHAT_URL`
and `PARASPEAKRS_CHAT_MODEL`; keys only through `PARASPEAKRS_DECISIONS_API_KEY` and
`PARASPEAKRS_CHAT_API_KEY`, which also makes OpenRouter's hosted JEV usable.

A transcript is any `.txt` paraspeakrs writes (`[hh:mm:ss] Name: text`, frontmatter allowed) or
the JSON of `paraspeakrs run`.

## Toward live audio

The analyzer takes lines and does not care where they come from, so live audio only needs a
source that produces lines during the recording. Today the recorder writes two mono `.caf` tracks
and paraspeakrs transcribes and diarizes after the recording stops.

| Option | For | Against |
|---|---|---|
| **A. The recorder also writes 20–30 s WAV pieces per channel while recording; a warm paraspeakrs process transcribes each piece as it lands. Speaker = channel: left is you (microphone), right is everyone else (system audio).** ← recommended | Reuses what exists: the piece cutting at quiet points and the warm model of `paraspeakrs dictate --serve`, and the recorder's track writer. No new model. Full diarization still runs after the stop, as now. | Everyone on the call is one "speaker" until the meeting ends. |
| B. paraspeakrs reads the growing `.caf` files | No recorder change. | ExtAudioFile finalizes the file on close; reading mid-write is fragile. |
| C. A streaming ASR model | Lowest latency. | A different model from Parakeet TDT, of unknown quality on Dutch; more work. |
| D. Live diarization on rolling windows | Real speaker names during the meeting. | Labels jump between windows; needs matching against the voice cache. Worth it after A works. |

With A a line reaches the analyzer 20–30 s after it is spoken, plus one analysis step. That is
the same scale as the 30 s batches, so it fits the design.

The page would then be opened by the recorder (or `paraspeakrs live follow <recording dir>`)
instead of by `live replay`.

## Risks and open questions

- **Where the local JEV-like server listens.** The default `/v1/decisions` and the model name
  `typesafe/jev-1.14` are guesses; `live check` finds the path if it is one of the common ones.
- **Thresholds are per model.** TypeSafe and OpenRouter both say probabilities differ between
  decision models. Defaults: topic 0.6, comment 0.5. Pick new ones from the probability rows on
  the page after a replay or two (`--topic-threshold`, `--comment-threshold`).
- **Noisy personas.** Vague `comment_when` text makes a persona comment on everything.
  Mitigations: the questionnaire asks for checkable criteria, `skip_when` is the false criterion,
  and `already_commented` stops repeats within a section.
- **Slow chat model.** Comments are written before the next batch is analyzed, so a slow model
  makes the analysis lag; batches then merge. Titles are written in the background.
- **Language.** Prompts are in English; output follows `language` in the brief, or the language
  of the meeting when it is empty. Not yet tried on a Dutch meeting with the real models.
- **The questionnaire depends on the chat model following a JSON format.** One retry is built in;
  a weak local model may still fail, which the page shows as an error.
- **Privacy.** Nothing leaves the machine unless a URL points elsewhere. Briefs and events are
  stored under `~/.local/share/paraspeakrs/live/`.

## Next steps

1. Run `live check` and a replay of a real Dutch meeting on the MacBook; set thresholds.
2. Decide on option A for live audio, then add the WAV pieces to the recorder and a `live follow`
   source.
3. Depending on what the replays show: a `score` question for how urgent a comment is, or
   comments that can be dismissed from the page.
