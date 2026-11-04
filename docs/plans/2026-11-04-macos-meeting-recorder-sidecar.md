# macOS meeting recorder sidecar

Date: 2026-11-04. Status: M0–M3 implemented in `packages/recorder-macos/`; see "Implementation
status" at the end.

## Goal

Record meetings without MacParakeet. A small Swift menu-bar app records the microphone and all system
audio into one stereo file. When you press stop, it hands the file to `paraspeakrs run`.

Out of scope for this plan: screen video, meeting notes, summaries, echo cancellation, live
transcription, signing and notarization for other users.

## Decisions taken

| Topic | Decision |
|---|---|
| Form | Swift menu-bar app (`MenuBarExtra`, `LSUIElement`), in `packages/recorder-macos/` |
| System audio | Core Audio process tap on all system output, excluding the recorder's own process |
| Microphone | Default input device, raw (headphones are the normal setup) |
| File | One stereo file: L = microphone, R = system audio (mono mixdown of the tap) |
| After stop | The app spawns `paraspeakrs run`; the job lands in the store the TUI reads (`cli.py:152`) |
| Build | Local build in this repo; no notarization |

### Permissions

The recorder needs two permissions, and neither one is Screen Recording.

- **Microphone**: `NSMicrophoneUsageDescription`.
- **System Audio Recording Only**: `NSAudioCaptureUsageDescription`. It appears under Privacy & Security →
  Screen & System Audio Recording, but it does not grant screen access. ScreenCaptureKit is the API that
  needs full Screen Recording, so this plan does not use it.

Sources: Apple, "Capturing system audio with Core Audio taps"; the RecordKit and SystemAudioKit permission
docs.

## Library choice

No candidate writes one stereo file with mic and system audio on a shared clock. Each records two
streams aligned by host time, and none corrects clock drift.

| Option | Why not adopted as-is |
|---|---|
| SystemAudioKit (MIT, SwiftPM) | One month old, no users. Writes two mono WAVs. Pads slow tracks but never trims fast ones. |
| AudioCap (BSD-2), audiotee (MIT) | No microphone capture. |
| MacParakeet (GPL-3.0) | Uses ScreenCaptureKit, so needs Screen Recording. The GPL licence blocks copying its code. |
| pasrom/meeting-transcriber `audiotap` (MIT) | Two tracks mixed afterwards. Uses an output-clocked aggregate, which breaks when AirPods switch to the call profile. |

**Chosen approach:** write directly against Core Audio, and borrow from SystemAudioKit (MIT):

- the tap description,
- the aggregate-device setup,
- the default-device listeners,
- the permission check.

The recording itself is one private aggregate device:

- the microphone is the main subdevice and drives the clock;
- the tap is in `TapList` with `kAudioSubTapDriftCompensationKey: true`;
- one IOProc receives the mic channels followed by the tap channels;
- the IOProc writes frames with `ExtAudioFileWriteAsync`, which is safe on the real-time thread.

This recipe comes from corti's `design/LESSONS.md` (MIT, Rust). One clock drives both channels, so the
two channels cannot drift apart. That claim is not tested in this plan; real meetings will show whether
it holds.

### File format

The final file is AAC in `.m4a` at 64 kbps per channel, about 58 MB per hour. That is the same
bitrate class as MacParakeet's files (`system-raw.m4a`: 13.6 MB for 27 minutes of mono).

The app gets there in two steps:

1. During recording, it writes uncompressed 16-bit PCM to a temporary `.caf` file, about 690 MB per
   hour at 48 kHz stereo. CAF stays readable after a crash. An `.m4a` written directly loses its index
   (the `moov` atom) when the app dies before closing it, and the whole recording is gone.
2. On stop, it converts the CAF to `.m4a` with `afconvert`, which ships with macOS, and deletes the CAF.
   If the app crashes, the CAF stays on disk and can be converted by hand.

Whether 64 kbps AAC costs ASR accuracy against PCM is unverified. paraspeakrs already transcribes
MacParakeet's AAC files without known problems.

## Changes to paraspeakrs

1. Add `--single-speaker-channel {L,R}`, repeatable, to `paraspeakrs run`. Pass it through to
   `LabelingWorkspace.transcribe` as `single_speaker_channels`. The pipeline already supports this
   (`collapse_channels`, `diarization.py:62`), but only the TUI exposes it (`tui/home.py:425`). With `L`
   set, your mic channel always yields one speaker.
   - Test: a stereo fixture whose left channel diarizes into two clusters comes back with one `L_`
     speaker when the flag is set and two without it.
2. Check that `paraspeakrs run` accepts a stereo `.m4a` from the recorder end to end. No code change is
   expected.

## Milestones

### M0: capture spike (Swift CLI, no UI)

Build `recorder-spike --seconds N out.caf` on the aggregate-device recipe.

Verify:
- `ffprobe` reports 2 channels.
- L contains only your voice; R contains only a YouTube clip playing at the same time.
- Record to the file who owns the clock when AirPods are the mic, and what sample rate the aggregate then
  runs at.

No long sync test. If real meetings show the channels drifting apart, fall back to SystemAudioKit's
two-stream design and interleave the streams at write time.

### M1: menu-bar app

- Menu items: Record / Stop, elapsed time, and a level meter for each channel.
- The menu-bar icon changes colour while recording.
- Permission states: not asked, allowed, denied. Each shows a button that opens the matching System
  Settings pane.
- Silence warning: when R peak stays at 0 for 30 s while recording, show "no system audio received". The
  tap delivers zeros without permission, and on macOS 26.5 it can also go silent mid-session (Apple forum
  thread 825780).
- Recordings are saved to `~/Documents/paraspeakrs-recordings/YYYY-MM-DD_HHMM.m4a`, converted from the
  temporary CAF on stop (see "File format").
- Packaging: `scripts/build-recorder-app.sh` wraps the SwiftPM executable in a `.app` with an
  `Info.plist`.
  - Sign with a stable local certificate, not ad-hoc. macOS ties permission grants to the code signature
    (unverified for ad-hoc builds). An ad-hoc build changes signature on every rebuild, so the
    permissions would reset each time.

Verify: record a 5-minute Teams or Meet call. Both channels are present and the file plays in QuickTime.

### M2: handoff to paraspeakrs

- On stop, the app runs `paraspeakrs run --single-speaker-channel L <file>`.
- The binary path is a setting with default `~/.local/bin/paraspeakrs`. GUI apps do not inherit the shell
  `PATH`.
- Jobs run one at a time, oldest first. Each transcription peaks at 3–4 GB RAM, so two in parallel would
  double that.
- stdout and stderr go to `<recording>.log`.
- A macOS notification reports success or failure. On failure it shows the last stderr line and opens
  the recordings directory in Finder. No retry list.
- Non-interactive runs do not ask the precision question; that is existing behaviour (README, "Model
  precision").

Verify: stop a recording. The job appears in `paraspeakrs tui`, and the mic speaker is a single `L_`
speaker.

### M3: device changes during a recording

- Rebuild the aggregate when the default input or output device changes, for example when AirPods
  connect.
- Fill the gap with silence based on host time, so the timeline stays continuous.
- Rebuild once when the tap goes silent mid-session (the macOS 26.5 bug above).

Verify: connect and disconnect AirPods during a recording. The recording continues, and the timeline has
a gap of under 1 s.

## Open questions

None.

## Implementation status (2026-11-04)

**Changed from the plan.** The single aggregate device (mic as clock, tap inside) delivered no
buffers at all, microphone included, while nothing played on the system output. The recorder
therefore uses the fallback: two devices, two mono tracks locked to host time, merged into the
stereo `.m4a` by Swift code on stop instead of `afconvert`. The reasoning is in `DECISIONS.md`.

| Item | State |
|---|---|
| `--single-speaker-channel` on `paraspeakrs run` | Done; test in `tests/test_cli_dispatch.py`; full suite passes |
| Capture core + `recorder-spike` | Done; mic track, timeline padding, rebuild gap (0.25 s) and merge verified |
| System-audio channel | **Not verified**: needs the permission granted to the app |
| Menu-bar app + `scripts/build-recorder-app.sh` | Builds and launches (72 MB RSS idle); recording from the UI not exercised |
| Hand-off to paraspeakrs | Verified with the repo checkout on a synthetic stereo file; installed 0.5.1 lacks the flag |
| AirPods / real device change | Not tested; rebuild path tested by forcing a rebuild |
