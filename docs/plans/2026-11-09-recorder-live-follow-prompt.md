# Recorder: offer `live follow` when a meeting recording starts

*Status: built and checked on the Mac 2026-11-09 (`LiveFollow.swift`, `LiveFollowPrompt.swift`):
pop-up under the icon, Open, Dismiss then "Open live meeting", a second recording replaces the
server, quit stops it, stopping the recording closes the pop-up. Comes before the persona package
(`docs/plans/2026-11-09-live-persona-package.md`).*

Found while building: with a page open, `live follow` ignored SIGTERM until the browser
let go of the event stream. `uvicorn.run(..., timeout_graceful_shutdown=2)` fixes that; the
recorder still sends SIGKILL after 5 s, since a transcription in progress can hold Python.
The recorder matches a running server on `source.watching` (the `.mic.caf` path, known at
once) as well as `source.name` (known once the ASR model has loaded).

## What the user asked

- Pressing the meeting-recording shortcut (default ⇧⌘M) starts the recording as now, and the
  recorder's own panel pops up asking whether to start `live follow`: **Open** or **Dismiss**.
- **Open** starts `paraspeakrs live follow` for this recording in the background (unless one is
  already running for it) and opens the page in the browser.
- After **Dismiss**, the menu-bar panel keeps an **Open live meeting** button while recording, and
  while the follow server for the last recording is still up.

## What exists

- `packages/recorder-macos/Sources/ParaspeakrsRecorder/`: `RecorderApp` (a SwiftUI `MenuBarExtra`
  with `.menuBarExtraStyle(.window)`), `RecorderPanel`, `MeetingSection` (record button, levels,
  errors), `RecorderModel` (shortcut via `KeyMonitor`; `start()` creates a `MeetingCapture` with
  base `~/Documents/paraspeakrs-recordings/<yyyy-MM-dd_HHmm>`, tracks `<base>.mic.caf` and
  `<base>.system.caf`; `stop()` merges to `<base>.m4a` and queues `paraspeakrs run`),
  `Transcriber` (runs the paraspeakrs executable with `environment()` that fixes PATH and sets
  `PARAKEET_NONINTERACTIVE=1`; executable from `UserDefaults` `paraspeakrsPath`), `Notifier`.
- The recorder has no SwiftPM dependencies, by decision (see the shortcuts decision in
  DECISIONS.md / memory): `install-recorder` builds the sources on the user's Mac.
- Python: `paraspeakrs live follow <base>.mic.caf --no-browser --port N` follows one recording;
  `/api/state` answers once the server is up and names the recording in `source.name`.

## Plan

1. **Live follow runner** — new `LiveFollow.swift` in the app target.
   - `start(base:)`: if `GET http://127.0.0.1:8765/api/state` answers with `source.name ==
     base.lastPathComponent`, reuse it. Otherwise start `paraspeakrs live follow <base>.mic.caf
     --no-browser --port 8765` with `Transcriber.environment()`, output to `<base>.live.log`.
     If another follow server holds the port (a different recording), stop ours first if it is
     ours; if it is not ours, report "port 8765 is in use" with the log.
   - `open()`: poll `/api/state` (up to ~20 s, the ASR model loads first), then
     `NSWorkspace.shared.open(http://127.0.0.1:8765/)`.
   - The process is kept after the recording stops (the page stays readable) and terminated
     when the next follow starts or the app quits.
   - Verify: a Swift test or `recorder-spike`-style check is not available for UI; verify by hand
     on the Mac (step 5), and keep the logic small enough to read.
2. **Prompt in the panel** — `RecorderModel` gets `@Published var offerLiveFollow: Bool`, set when
   a recording starts from the shortcut or the button. `MeetingSection` shows "Follow this meeting
   live?" with **Open** / **Dismiss** while it is set, and **Open live meeting** otherwise while
   recording or while the follow server runs.
3. **The pop-up** — a small floating `NSPanel` of our own just under the menu-bar icon, with the
   prompt and Open / Dismiss (decided 2026-11-09, see below). It does not take focus from the
   meeting app, closes on either button, and closes by itself when the recording stops.
4. **Python side**: none expected. Check that `live follow` on an explicit `.mic.caf` path works
   from the moment the file is created (it waits for the header), and that the recorder's PATH
   finds ffmpeg and the speakrs sidecar.
5. **Verify on the Mac**: `uv run paraspeakrs install-recorder`, press ⇧⌘M, check the panel opens
   with the prompt, Open starts the server and the page, Dismiss leaves the menu item, a second
   recording replaces the follow server, quitting the app stops it. README "Shortcuts"/meeting
   section and CHANGELOG updated.

## Decision: how the prompt pops up (own floating panel, chosen 2026-11-09)

SwiftUI's `MenuBarExtra` has no API to open its window from code.

| Option | For | Against |
|---|---|---|
| **Chosen:** own small floating `NSPanel` near the menu-bar icon with the prompt (Open / Dismiss) | No dependency, no private API, always works | Not the menu-bar panel itself, a second small window |
| `MenuBarExtraAccess` (SwiftPM package, adds `isPresented` to `MenuBarExtra`) | Opens the real panel | Breaks the no-dependencies rule for the recorder build; relies on AppKit internals |
| Find the status item's button among `NSApp.windows` and `performClick` | Opens the real panel, no dependency | Undocumented; may break with a macOS update |

## Risks

- `paraspeakrs run` (after the stop) and the follow server's catch-up run at the same time:
  RAM for two ASR models (FP32 Parakeet, ~3-4 GB each at peak).
- Port 8765 hard-coded: a `live follow` the user started in a terminal holds it.
