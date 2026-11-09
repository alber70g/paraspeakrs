# Decisions

Autonomous decisions taken without waiting for review. Newest first.

Each entry carries its review status. **Accepted** means the user reviewed it
after the fact and let it stand; an entry stays **Pending** until they do.

## 2026-11-09 — Live speakers: diarize each piece with the minute before it, with the speakrs sidecar

*Status: pending.*

`live follow` named lines by track. The user asked for speakers told apart live,
named in the page, and matched to known voices with a percentage. speakrs has no
streaming mode, and found no speakers at all in a lone 10 s piece.

- **Rolling 60 s window per track through the existing speakrs-diar sidecar, matched
  to meeting speakers by voice print.** ← chosen. Measured on CoreML: 1.3-1.6 s per
  window; window prints matched the whole-recording speaker at 0.84-0.97. Same model
  as the voice cache (`speakrs:256`), so the 11 named voices already there are
  matchable. No Rust or wheel change. CPU mode is too slow (26 s for a 30 s window).
- **New embed-only mode in the sidecar** (speakrs exposes `EmbeddingModel::embed`).
  Same embeddings, one load per meeting; but a Rust change and a wheel rebuild, and
  no segmentation, so a piece with two voices stays one speaker.
- **sherpa-onnx speaker embeddings** (already a dependency). Different features and
  model, so the prints do not match the existing voice cache.
- **diart** (online pyannote). Needs torch and a Hugging Face token; also a
  different embedding space.

Choices the user made: suggestions need a click (as in the labeling TUI); both tracks
are diarized (the mic picks up the call and people in the room); naming folds the
voice into the cache at once; lines wait about 1.5 s for their speaker. Thresholds:
0.6 for "same voice in this meeting" (different people in the cache scored at most
0.61, median 0.27), the cache's 0.75 for suggestions.

Tuned after the first real meeting (41 min, in-room, 9 speakers in the full
diarization), which the live run split into 24. The windows were replayed offline
against the full diarization: a new speaker now needs 10 s of speech in its window
(was 2 s): 12 voices instead of 23 at the same purity (0.84 against 0.85; about 0.85
is the ceiling, where 60 s windows disagree with the whole-file diarization). A
same-voice threshold of 0.5-0.65 and merging voices whose prints converge (0.7,
0.75) were tried: merging lowered purity to 0.5-0.74, so it was left out. One meeting
is one data point.

Restarts: progress is kept per recording (`live/recordings/<name>/`), and the cache
knows a live voice as `live:<recording>`, so a restart refolds the same member
instead of adding the voice twice. Analysis state is not restored.

## 2026-11-09 — Live audio: read the recorder's growing CAF tracks, do not change the recorder

*Status: pending.*

The plan recommended that the recorder write extra WAV pieces for live transcription,
and rated reading the growing `.caf` files as fragile. Looking closer: CAF allows a
data chunk of size -1 ("to the end of the file") exactly so a file can be read while
it is written, and the recorder's README already relies on the tracks being usable
after a crash. This session has no macOS and no Swift compiler.

- **Recorder writes WAV pieces, as planned.** Two copies of the audio on disk; a Swift
  change that could not be built or tested here, and a recorder reinstall for every user.
- **Read the CAF tracks while they grow.** ← chosen. No recorder change; tested with
  the real Parakeet model on tracks written the way the recorder writes them. The
  reader takes whole frames up to the end of the file and re-reads the declared size
  on every call, so it works whether the writer leaves the size at -1, 0 or updates it.
  **Unverified:** that macOS writes the audio to disk while recording, rather than at
  close, next to the real recorder app.

## 2026-11-09 — Live analysis: where the decision model is assumed to listen

*Status: pending.*

The user gave `localhost:8080` for their JEV-like model but no path or model name,
and this cloud session cannot reach their Mac to look. Hosted JEV answers at
`/v1/decisions` (AI/ML API) and `/api/alpha/decisions` (OpenRouter).

- **Ask before building.** Blocks the spike on a value that only matters at run time.
- **Default to `/v1/decisions` and `typesafe/jev-1.14`, and make `paraspeakrs live
  check` try the other known paths on a 404.** ← chosen. One flag or environment
  variable fixes a wrong guess, and the check says which one.

## 2026-11-09 — Live analysis: section boundaries on the line, not the batch

*Status: pending.*

The analysis works on batches of about 30 s. In the browser run the last two lines
about the launch date landed in the budget section, because a new section started at
the first line of the batch in which the topic changed.

- **Smaller batches.** More decision calls, and still up to one batch off.
- **Ask where the new topic starts, in the same request.** ← chosen. A JEV `choice`
  question over the batch's lines rides along with the yes/no topic question, so it
  costs no extra call. The batch is cut at the chosen line.

## 2026-11-04 — Recorder distribution: build on the user's Mac via `paraspeakrs install-recorder`

*Status: pending.*

The user wants others to use the recorder and asked for a command that builds and
installs it. Without a paid Developer ID, a downloaded prebuilt app is blocked by
Gatekeeper; an app built on the machine that runs it is not quarantined.

- **Fetch the sources from GitHub at the matching tag.** Keeps the wheel small;
  needs git and network at install time, and a tag that exists.
- **Ship the Swift sources inside the wheel** (`paraspeakrs/recorder_macos/`, about
  60 KB). ← chosen. Offline, always the version that matches the installed
  paraspeakrs, and nothing extra to publish.
- **Keep the shell script and document cloning the repo.** No Python change; every
  user needs a checkout, which `uv tool install` users do not have.

Named `install-recorder`, not `build-sidecar` as suggested: "sidecar" already means
the speakrs diarizer binary throughout this repository. `scripts/build-recorder-app.sh`
is removed; the command is its one implementation.

## 2026-11-04 — Recorder app: ad-hoc signature with a fixed designated requirement

*Status: pending.*

`security find-identity -p codesigning` lists no identities on this Mac. macOS
ties microphone and system-audio grants to the app's code identity, and a plain
ad-hoc signature's identity is its cdhash, which changes on every build.

- **Create a self-signed code-signing certificate in the login keychain.** Stable
  identity, grants survive rebuilds; modifies the keychain, which is security
  configuration I do not change without asking.
- **Plain ad-hoc signing.** Zero setup; almost certainly re-prompts for both
  permissions after every rebuild.
- **Ad-hoc signing with `designated => identifier "io.github.alber70g.paraspeakrs-recorder"`.**
  ← chosen. No keychain change, and the designated requirement no longer
  depends on the cdhash. Whether TCC honours it across rebuilds is
  **unverified**. `CODESIGN_IDENTITY=... scripts/build-recorder-app.sh` switches
  to a real certificate if it does not. (Since replaced by `paraspeakrs
  install-recorder`, which picks a certificate from the keychain on its own.)

## 2026-11-04 — Recorder: two host-time-locked tracks instead of one aggregate device

*Status: accepted 2026-11-04.*

The plan (`docs/plans/2026-11-04-macos-meeting-recorder-sidecar.md`) put the mic and
the system-audio tap in one aggregate device, mic as clock. Measured in the M0 spike:
that device delivers **no buffers at all, mic included**, while nothing plays on the
system output. Frames stayed at 0 until `afplay` started, then ran normally. A meeting
with a silent stretch would lose your own voice too. Whether this only happens because
the tap had no system-audio permission (TCC refused it for iTerm2) is unverified.

- **Keep the single aggregate and investigate after the permission is granted.** One
  clock, no drift by construction; blocks M0 on a GUI permission click, and if the
  stall is not permission-related the design is dead anyway.
- **Mic IOProc directly on the mic device, tap in a tap-only aggregate, two mono
  tracks locked to host time, merged on stop.** ← chosen. This was the plan's
  documented fallback (SystemAudioKit's design). Each track pads silence when it falls
  more than 50 ms behind host time and drops frames when it runs 50 ms ahead, so
  drift is corrected in both directions and a stalled tap costs only the right
  channel.
- **Mic through AVAudioEngine instead of a HAL IOProc.** Less code for the mic side;
  a second audio stack next to the HAL code for the tap, and its own device-change
  behaviour to learn.

Consequence: the stereo `.m4a` is written by a Swift merge step (ExtAudioFile, AAC
128 kbps) rather than `afconvert`. A crash leaves two mono `.caf` files, not one.

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
