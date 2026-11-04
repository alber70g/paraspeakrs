# Recorder: dictation with Ctrl+\

Date: 2026-11-04. Status: released in 0.8.0. Verified by the user: hold, tap, tap again, Escape and ⌃Escape
cancel, sounds. Not verified: typing in each app of the D3 list (TextEdit, Safari, VS Code,
Slack, iTerm2), and the D4 latency for a 30 s dictation from the app.

## Goal

Press Ctrl+\ in any app, speak, and the transcript is typed into the focused text field. The
feature lives in the existing Swift menu-bar app (`packages/recorder-macos`). Transcription uses
the same Parakeet model as meetings, through `paraspeakrs`.

Out of scope: clipboard use, LLM cleanup or reformatting, a history of past dictations, live
(streaming) transcription, custom vocabulary, a configurable shortcut.

Research: `research/macos-dictation-text-insertion-paste-vs-typing-hotkey-and-warm-asr-in-open-source-apps.md`.

## Decisions taken

| Topic | Decision |
|---|---|
| Shortcut | Ctrl+\ via Carbon `RegisterEventHotKey`, pressed and released edges. No permission needed. Terminals no longer receive `^\` (SIGQUIT) while the app runs. |
| Mode | Hold or tap. Recording starts on key-down. Released after ≥ 0.35 s: stop (push-to-talk). Released earlier: keep recording until the next press (toggle). Escape cancels; it is registered only while dictating. |
| Insertion | Typed into the frontmost app with synthetic Unicode key events. The clipboard is never read or written. Needs the Accessibility permission. |
| ASR | On key-down the app starts `paraspeakrs dictate --serve`, which loads the model while the user speaks. On stop it receives the WAV path, returns the text, and exits. There is no setting to keep it loaded. |
| Feedback | Menu-bar icon shows idle / dictating / transcribing. System sounds on start, stop and failure. No overlay window. |

## Measurements behind the ASR choice

On this Mac (Apple Silicon, FP32 model in the page cache), a 4.8 s clip:

| Step | Time |
|---|---|
| CLI startup | 0.2 s |
| Model load (FP32, in the page cache) | 0.8 s |
| Decode of the 4.8 s clip | 0.1 s |
| Peak RSS of the process | 2.7 GB |

Starting the process on key-down hides startup and model load behind the speech: a dictation
lasts longer than 1 s. After release only the decode is left, about 0.1 s for a short clip. The
process exits after the transcript, so no RAM is held between dictations. If the user releases
before the model is loaded, the request waits for it.

If macOS evicts the 2.4 GB model from the page cache, the load also reads it from disk. That cost
is not measured.

FluidAudio (CoreML, Neural Engine) was not chosen: it adds a Swift dependency and a second model.
The user chose a `paraspeakrs` process instead.

## Design

### 1. Python: `paraspeakrs dictate`

New module `src/paraspeakrs/dictate.py`, new subcommand in `cli.py`.

- `paraspeakrs dictate FILE` prints the transcript of one 16-bit PCM WAV to stdout and exits.
  No job store, no diarization, no ffmpeg. The app uses it only as a fallback; it also serves
  testing from a terminal.
- `paraspeakrs dictate --serve` loads the model, prints `{"ready": true}`, then reads one WAV path
  per line from stdin. For each line it writes one JSON line to stdout: `{"text": "..."}` or
  `{"error": "..."}`. It exits when stdin closes, so it dies with the app.
- The model comes from `Settings.from_env()` and the same downloaded-model lookup as `run`. If the
  model is missing, it fails with "run `paraspeakrs fetch-models` first". It never downloads: a
  2.4 GB download must not start from a keyboard shortcut.
- Audio longer than 30 s is split into pieces of at most 30 s, each cut at the quietest 0.2 s
  window between 20 and 30 s. One pass over minutes of audio loses words and grows memory
  (memory note "ASR chunk size: bigger loses words"). The texts are joined with a space.
- A piece shorter than 0.1 s is not decoded and gives empty text: below about 5 ms onnxruntime
  fails on an empty feature matrix (measured), and a quick tap records about that much.
- `--serve` with a missing model prints `{"error": "..."}` before exiting with code 2, so the app
  can show the message instead of only seeing the process end.

Tests (`tests/test_dictate.py`):
- one-shot prints only the text on stdout, so the app can type it as-is;
- `--serve` answers one JSON line per request and exits on EOF;
- a bad path gives an `error` line and the server keeps running;
- a missing model fails with the `fetch-models` hint and does not download;
- the split never cuts at a loud sample when a quiet window exists, and no piece exceeds 30 s;
- `dictate` builds no diarization pipeline.

### 2. Swift: recording

- `TrackWriter` gets a file-type parameter (`kAudioFileWAVEType` for dictation, CAF stays the
  default for meetings). Dictation records the default input with the existing `MicSource` into a
  48 kHz mono 16-bit WAV in the app's temporary directory. sherpa-onnx resamples to 16 kHz.
- The file is deleted after the transcript is typed. On failure it stays, and the log says where.
- Dictation during a meeting recording is allowed. The dictated speech is also on the meeting's
  left channel.

### 3. Swift: shortcut

- `GlobalHotKey` handles `kEventHotKeyPressed` and `kEventHotKeyReleased`, and checks the
  `EventHotKeyID` of each event. Today each instance installs a handler that accepts every hot-key
  event and returns `noErr`, which ends the handler chain. With a second shortcut, one handler
  would receive both shortcuts. This follows from the Carbon handler chain; it is not tested.
- A repeated "pressed" without a "released" in between is a key repeat and is ignored.
- Escape and ⌃Escape are registered when dictation starts and unregistered when it stops, so they
  are not taken from other apps the rest of the time. ⌃Escape because Control is still down while
  Ctrl+\ is held for push-to-talk (found in testing: plain Escape did not cancel then). A cancel
  plays `Bottle`.

### 4. Swift: typing into the active window

New `TextTyper.swift`:

- Before typing, wait until no modifier key is physically held
  (`CGEventSource.flagsState(.hidSystemState)`: Shift, Control, Option, Command, Fn, as in
  Presspeech), for at most 2 s. With Control still down, the
  typed characters would arrive as Control shortcuts. On timeout, nothing is typed.
- Each key-down/key-up pair carries up to 20 UTF-16 units via `keyboardSetUnicodeString`. Chunks
  never split a character (grapheme cluster). Events have empty modifier flags, virtual key `0x31`,
  come from a `.privateState` event source, with 10 ms between chunks: VoiceInk's values
  (`DirectTextTyper.swift`). They are posted at `.cgSessionEventTap`, not at HID level as VoiceInk
  does: MacParakeet and FluidVoice report that posting at HID level left Command stuck down.
- The text is typed as Parakeet returns it, without a leading or trailing space.
- Without the Accessibility permission, macOS drops synthetic events without an error (Epicenter
  ADR-0040). The app checks `AXIsProcessTrusted()` first and does not type when it is false.

When the text cannot be typed (no permission, modifier held, nothing recognised), the panel shows
the last transcript with a Copy button, and a notification says why. Copying happens only when the
user clicks it.

### 5. Swift: transcription

New `DictationTranscriber.swift`, next to `Transcriber.swift`, with the same `PATH` and
environment (`PARAKEET_NONINTERACTIVE=1`):

- On key-down, spawn `paraspeakrs dictate --serve`. On stop,
  write the WAV path, read one JSON line, then close stdin so the process exits.
- Escape: close stdin without sending a path; the process exits.
- If the process has died before the path is sent, run the one-shot `paraspeakrs dictate FILE`
  for that dictation.
- stderr goes to `~/Library/Logs/Paraspeakrs Recorder/dictation.log`.

### 6. Swift: permissions and panel

- First dictation without Accessibility: call `AXIsProcessTrustedWithOptions` with the prompt.
  The panel shows "Accessibility is off" with a link to
  `x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility`, and checks
  again every second while the panel is open.
- Panel additions: the Ctrl+\ shortcut label (or "taken by another app"), and the last
  transcript when it could not be typed.
- Icon: `waveform.circle` idle, `mic.fill` while dictating, `ellipsis.circle` while transcribing.
  The red meeting icon wins when a meeting is recording.
- Sounds, chosen by the user after testing: start and stop are two hits cut from a notification
  sound the user supplied (`notification_message-notify-2-310755.mp3`): 670 Hz on start, 1250 Hz
  on stop, about 0.1 s each, in `Sources/ParaspeakrsRecorder/Sounds/`, copied into the bundle's
  Resources by `install-recorder`. Cancel: 0.18 s cut from `biww-quick-whoosh-transition-sound-561929.mp3`
  (also user-supplied), tail faded out over the last 80 ms. `Basso` on failure. A Sounds toggle in the panel mutes all four
  (`UserDefaults` key `dictationSounds`, default on).

## Milestones

| | Work | Check |
|---|---|---|
| D1 ✅ | `paraspeakrs dictate` one-shot and `--serve`, with tests | `uv run pytest`; time from writing a path to the JSON line, with the model loaded. Measured: `ready` 0.99 s after spawn; 5 s clip 0.11 s; 45 s clip (two pieces) 0.74 s; exit 0.08 s after stdin closes |
| D2 | `GlobalHotKey` with both edges and id dispatch; Ctrl+\ and ⌘⇧M side by side | Both shortcuts work in the installed app; hold and tap behave as specified |
| D3 | WAV recording for dictation; `TextTyper`; `DictationTranscriber` (start on key-down, exit after) | Dictate into TextEdit, Safari, VS Code, Slack, iTerm2; record what each app receives |
| D4 | Panel, icon, sounds, Accessibility flow | Latency from release to first typed character, for a 2 s and a 30 s dictation; no `paraspeakrs` process left after a dictation |
| D5 | README, CHANGELOG, plan status | Wheel build contains the new Swift files; `paraspeakrs install-recorder` from the built wheel |

## Open questions and risks

1. Does the Accessibility grant survive `paraspeakrs install-recorder` rebuilds with the Apple
   Development certificate? Not verified. If not, every reinstall needs a new grant.
2. Unicode typing is not verified in Electron apps, terminals, and web editors that handle key
   events themselves (Google Docs, Monaco). D3 tests these.
3. A Bluetooth microphone can take hundreds of milliseconds to start, so the first word may be
   cut. Not measured.
4. `kVK_ANSI_Backslash` is a key position. On non-US layouts that key may not be `\`. Not checked.
5. An app with an event tap takes a key before Carbon hot keys see it. On this Mac, MacParakeet's
   dictation shortcut is also Ctrl+\ (measured 2026-11-04: a synthetic Ctrl+\ reached neither the
   recorder nor a test process, while ⌃⌥⌘F12 reached the test process). `RegisterEventHotKey`
   still returns `noErr` in that case, and also when another process registered the same
   combination, so the panel's "taken by another app" warning does not detect either case.
