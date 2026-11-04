# Paraspeakrs Recorder (macOS)

A menu-bar app that records a meeting for paraspeakrs: your microphone on the left
channel, all system audio on the right. On stop it saves a stereo `.m4a` and runs
`paraspeakrs run --single-speaker-channel L` on it, so the transcript keeps your
voice (one `L_` speaker) apart from everyone else (`R_` speakers).

Requires macOS 15 or later on Apple Silicon, and paraspeakrs with
`--single-speaker-channel` (added after 0.6.0).

## Build and run

```sh
paraspeakrs install-recorder          # builds, installs in ~/Applications, launches
uv run paraspeakrs install-recorder   # the same from a checkout of this repository
```

The command builds the Swift sources that ship inside the paraspeakrs package,
in `~/Library/Caches/paraspeakrs/recorder-build`, so a reinstall is incremental.
It signs the app with a Developer ID or Apple Development certificate if the
keychain has one, otherwise ad hoc. It also stores the path of the `paraspeakrs`
that ran it, which the app calls after each recording. Options: `--dest DIR`,
`--sign IDENTITY`, `--no-open`.

If signing fails with `errSecInternalComponent`, the keychain is missing Apple's
intermediate certificate:
<https://www.apple.com/certificateauthority/AppleWWDRCAG3.cer>.

## Shortcut

⌘⇧M starts and stops a recording from any app. It is the same combination as
mute/unmute in Microsoft Teams for Mac, and the recorder takes it first: while the
recorder runs, ⌘⇧M no longer mutes you in Teams. If another app already holds
⌘⇧M, the panel says so and only the button works.

## Permissions

The first recording asks for two permissions. Neither is Screen Recording.

| Prompt | Where to change it later |
|---|---|
| Microphone | Privacy & Security → Microphone |
| System Audio Recording Only | Privacy & Security → Screen & System Audio Recording |

Without the system-audio permission the recording still works, but the right
channel is silent. The app warns after 30 s without system audio. It shows the
same warning when nothing is playing, because macOS reports both cases the same way.

## Files

Recordings go to `~/Documents/paraspeakrs-recordings/`:

| File | Content |
|---|---|
| `2026-11-04_1430.m4a` | Stereo AAC, 128 kbps, about 46–58 MB per hour |
| `2026-11-04_1430.log` | Output of `paraspeakrs run` |

The transcript itself is a paraspeakrs job: open it with `paraspeakrs tui`.

While recording, the app writes two uncompressed tracks, `<name>.mic.caf` and
`<name>.system.caf`, and deletes them once the `.m4a` is written. If the app
crashes or the merge fails, they stay on disk and can be merged by hand:

```sh
swift run --package-path packages/recorder-macos recorder-spike \
  --merge NAME.mic.caf NAME.system.caf NAME.m4a
```

## Settings

The panel has a field for the `paraspeakrs` executable, default
`~/.local/bin/paraspeakrs` (where `uv tool install` puts it). The app adds
`~/.local/bin`, `/opt/homebrew/bin` and `/usr/local/bin` to `PATH` for the run,
because paraspeakrs needs `ffmpeg` and an app started from Finder does not get the
shell's `PATH`.

## How it records

- **Microphone:** an IOProc directly on the default input device.
- **System audio:** a Core Audio process tap on all output except this app, inside
  a private aggregate device that holds only the tap.
- **Alignment:** each track is locked to host time. It writes silence when it falls
  more than 50 ms behind and drops frames when it runs 50 ms ahead.
- **Device changes:** when the default input or output changes, for example when
  AirPods connect, both devices are rebuilt. The gap becomes silence; a test
  rebuild left a 0.25 s gap.

Why two devices rather than one aggregate: see `DECISIONS.md`, 2026-11-04.

`recorder-spike` records from the command line, for testing:

```sh
swift run --package-path packages/recorder-macos recorder-spike --seconds 10 /tmp/test
```

Run from a terminal, macOS attributes the system-audio request to the terminal
app, so the right channel is silent unless that app has the permission.
