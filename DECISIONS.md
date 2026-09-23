# Decisions

Autonomous decisions taken without waiting for review. Newest first.

Each entry carries its review status. **Accepted** means the user reviewed it
after the fact and let it stand; an entry stays **Pending** until they do.

## 2026-09-22 — Carrying concurrent, unauthored changes into the release

*Status: accepted 2026-09-22.*

Between two `git status` calls in one session, the working tree gained changes I
did not author: notes rendered as YAML transcript frontmatter, `tui/jobs.py`
deleted, `UiState.mark` dropping DONE items, and two new tests for deleting a job
from the processed panel. The tree was **not green** — the delete action the new
tests describe was never implemented, and `home.py` still imported the deleted
`.jobs` module.

- **Revert the unauthored changes and release only my own work.** Clean
  provenance; throws away finished work (the frontmatter feature is complete and
  tested) and would fight whoever wrote it.
- **Stop and ask whose changes these are.** Safest; blocks a release the user
  explicitly asked for on a question whose answer does not change what the code
  has to do.
- **Finish the in-flight work to green, then release.** ← chosen. The new tests
  specify the missing behaviour exactly, so the intent is not guesswork, and the
  alternative leaves the repo in a state where `^j` crashes the TUI.

No peer session was active on this repo (`ListAgents`: all idle or offline), so
there was no live editor to collide with.

## 2026-09-22 — Stopping before the release tag

*Status: accepted 2026-09-22.*

`.github/workflows/publish.yml` fires on `v*` tags and publishes to PyPI via
trusted publishing. A PyPI version, once taken, cannot be reused.

- **Push the tag as asked.** Completes the request in one go; publishes a tree
  containing a feature whose author may not consider it finished, under a version
  number that can never be reclaimed.
- **Commit and push `master`, stop before the tag.** ← chosen. Pushing a branch
  is revertible and triggers nothing; the tag is one command the user runs when
  they confirm the concurrent work was meant to ship.

**Resolved the same day:** asked, and the user confirmed the concurrent work was
finished and should ship, by the PyPI tag route. Tag pushed.

## 2026-09-22 — Version 0.3.1, chosen by the user over my 0.4.0

*Status: accepted 2026-09-22.*

The release adds meeting notes, transcript frontmatter and a reworked
single-screen TUI, and removes the jobs screen. I proposed 0.4.0: no Python API
breaks, but `^j` stops working and transcripts gain a header, which is MINOR
rather than PATCH per SemVer. Asked, and the user chose **0.3.1**. Recorded
because the next person reading the version line will not guess that the jobs
screen disappeared in a patch release.

## 2026-09-22 — Implementing the delete action from its tests alone

*Status: accepted 2026-09-22.*

The concurrent changes left two passing-by-design tests for deleting a job from
the processed panel, and no implementation. The tests fixed the behaviour
exactly: `d` on a focused processed row, a confirm modal, then the job, its
note and its directory gone while the voices it taught stay.

- **Write it from the tests.** ← chosen. The tests are a complete spec, so this
  is reading a requirement, not inventing one.
- **Delete the tests as unfinished scaffolding.** Would have left `^j` crashing
  and the panel with no way to remove a job.

One thing the tests did not cover, added anyway: `NotePanel.forget()`. Clearing
the panel the ordinary way saves the outgoing note first, which for a
just-deleted job meant writing into a directory that no longer existed — an
error toast about a note the user never asked to keep.

## 2026-09-22 — Deleting the now-unreferenced `SpeakerScreen`

*Status: accepted 2026-09-22.*

My earlier refactor left `SpeakerScreen` as a thin host for `SpeakerPanel`, whose
only caller was the jobs screen. With that screen gone nothing constructs it.
Removed rather than kept "in case": it is code I introduced this session, so
removing it is cleaning up my own mess, not deleting someone else's work.
`TranscriptScreen` stays — the speaker panel still pushes it.
