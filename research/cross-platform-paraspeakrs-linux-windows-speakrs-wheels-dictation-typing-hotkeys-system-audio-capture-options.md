# Cross-platform paraspeakrs: Linux and Windows support per component

Date: 2026-11-04. Scope: what each paraspeakrs component needs to run on Linux (x86_64,
aarch64) and Windows (x86_64), and which open-source tools already solve each part.

Method: primary sources read with `gh api` (repo files, trees, releases), the crates.io and
PyPI JSON APIs, Exa fetch, and Tavily (rate-limited for part of the session; HTTP 429). "Not
verified" marks claims that were not read in a primary source or not reproduced. Nothing was
built or run on Linux or Windows. A planned timing of speakrs `cpu` mode on this Mac was not
run: installing the upstream `speakrs` package was blocked by the permission classifier.

Related local research (not repeated here):
`research/macos-dictation-text-insertion-paste-vs-typing-hotkey-and-warm-asr-in-open-source-apps.md`
(macOS insertion, permissions, hotkeys, warm ASR in Handy, VoiceInk, Epicenter and others).

## 0. Current state of the repo

| Component | Today | Platform-bound parts |
| --- | --- | --- |
| `paraspeakrs` (Python, pure wheel) | runs anywhere Python 3.11–3.12 runs | `install-recorder` is macOS-only; `config.py` names the binary `speakrs-diar` (no `.exe`) and checks `os.access(..., X_OK)`; `default_speakrs_mode()` returns `cpu` off macOS |
| ASR: `sherpa-onnx` + `sherpa-onnx-core` | PyPI dependency | none (see §2) |
| Diarizer `paraspeakrs-speakrs` wheel | macOS arm64 only; `speakrs` 0.5 with `coreml` + `blas-static` (OpenBLAS from source, needs gfortran) | `.github/workflows/publish.yml` builds on `macos-14` only; `scripts/check-speakrs-wheel.py` uses `otool` |
| Fallback diarizer `senko` | extra, pulls torch | not checked per platform |
| Recorder + dictation (`packages/recorder-macos`) | Swift, Core Audio process taps, Carbon hotkey, CGEvent typing | entirely macOS |

## 1. speakrs on Linux and Windows

### 1.1 The crate changed since 0.5

| Version | Date | Inference backends | Linear algebra |
| --- | --- | --- | --- |
| 0.5.0 (used now) | 2026-07-07 | ONNX Runtime (`ort`) for CPU/CUDA; native CoreML | features `default-linalg`, `openblas-static`, `openblas-system`, `intel-mkl` |
| 0.6.0 | 2026-10-13 | native Rust CPU (`cpu`), native CUDA (`cuda`), native CoreML (`coreml`), ONNX Runtime only for `migraphx` (AMD) | BLAS features removed: "PLDA setup now uses a small built-in Rust solver, so builds no longer link MKL or OpenBLAS" |
| 0.7.0 (latest) | 2026-10-26 | same as 0.6 | none; adds Python bindings on PyPI |

Sources: crates.io API `https://crates.io/api/v1/crates/speakrs` (feature lists per version);
`CHANGELOG.md` in github.com/avencera/speakrs (0.6.0 and 0.7.0 entries); `Cargo.toml` on
`master`.

Consequence: on 0.6+, the gfortran/OpenBLAS/`--auditwheel=repair` steps in `publish.yml` and
the `blas-static`/`blas-system` features in `packages/speakrs-diar/Cargo.toml` become
unnecessary. The OpenBLAS-on-Windows question disappears. MSRV rises to 1.96.

### 1.2 Backends per platform (speakrs 0.7 README, "Features and build notes")

| Platform | Feature | Runtime dependency |
| --- | --- | --- |
| macOS | `coreml` | system frameworks |
| any CPU | `cpu` | none ("Native Rust, no ONNX Runtime") |
| Linux + NVIDIA (Turing / sm75 or newer) | `cuda` | driver; cuDNN 9 and cuBLAS 12 loaded at run time, optional ("builds without a CUDA toolkit") |
| Linux + AMD | `migraphx` + `load-dynamic` | ONNX Runtime MIGraphX |
| Windows GPU | none | no DirectML; `cuda` is documented as "Linux-only" in `Cargo.toml` |

### 1.3 Evidence per target

| Target | Status | Evidence |
| --- | --- | --- |
| Linux x86_64 | supported, shipped | upstream `package.yml` builds `x86_64-unknown-linux-gnu` with `manylinux: "2_28"` via `PyO3/maturin-action@v1.51.0`; PyPI has `speakrs-0.7.0-cp311-abi3-manylinux_2_28_x86_64.whl` |
| Linux aarch64 | compiles in CI, no wheel | `ci.yml` runs clippy on `ubuntu-24.04-arm` with `cpu cuda migraphx load-dynamic _metrics`; PyPI has no aarch64 wheel |
| Windows x86_64 | not verified | no Windows job in `ci.yml` or `package.yml`; pyproject classifiers list only macOS and Linux; no Windows issue in the tracker (`gh issue list --search windows`: none); one `#[cfg(target_os = "windows")]` exists in `src/inference/ort_runtime.rs` (ORT path only). The `cpu` dependency set (rustfft, safetensors, ndarray, rayon, libloading, hf-fetch → ureq 3 with rustls) contains no Unix-only crate that I found, so a Windows `cpu` build is plausible but not built here |
| Windows CUDA | not available | `cuda` is Linux-only |

CPU speed: speakrs publishes no `cpu`-mode benchmark (README and `benchmarks/*.txt` list only
CoreML and CUDA rows). Diarization speed for Linux/Windows users without NVIDIA is therefore
unknown. Measure before promising it.

### 1.4 Two packaging routes

1. **Keep the sidecar, upgrade it.** Bump `packages/speakrs-diar` to `speakrs = "0.7"`, map
   features per target (macOS: `coreml`, Linux x86_64: `cpu` + `cuda`, Linux aarch64 and
   Windows: `cpu`), and build a maturin `bindings = "bin"` matrix. maturin packages a `bin`
   target into the wheel as a script on `PATH` (maturin.rs/bindings.html). `PyO3/maturin-action`
   is what upstream uses for manylinux. Work items:
   - API migration 0.5 → 0.7 (0.6 has breaking changes: `QueueSender::push` removed,
     `ort::Error` replaced by `InferenceError`, `ModelBundle::from_dir` returns `Result`, and
     more). Size of the migration for `src/main.rs` not checked.
   - CPU and CUDA modes load `.safetensors` weights, not ONNX. Offline `--models-dir` bundles and
     `SPEAKRS_MODELS_DIR` need the new file set (`speakrs::models::required_model_files`).
   - Diarization output may change (0.6 changelog: "Match CoreML diarization accuracy with CUDA
     by fixing chunk filterbank stitching and using a 1 second segmentation step"). Re-run any
     golden-output tests.
   - Python: look for `speakrs-diar.exe` on Windows; `os.access(X_OK)` is meaningless there;
     replace the `otool` wheel check with a per-OS check (`auditwheel show`, `delvewheel` or
     `dumpbin /dependents`).
2. **Use upstream `pip install speakrs` in-process.** `speakrs.Pipeline.from_pretrained("cpu")`
   and `.diarize_file()` (README "Python"). abi3 wheels for Python ≥ 3.11: macOS arm64
   (CPU+CoreML) and Linux x86_64 (CPU+CUDA) only. No Windows, no Linux aarch64. Removes this
   repo's Rust build entirely, but leaves Windows uncovered. Whether its Python API exposes the
   embeddings `speakrs-diar` currently emits (`embedding_dim` in the JSON contract) is not
   checked (see `crates/speakrs-python/API.md`).

## 2. sherpa-onnx Python wheels

PyPI `sherpa-onnx` 2.0.1 (`https://pypi.org/pypi/sherpa-onnx/json`): wheels for cp38–cp315 on
`manylinux2014_x86_64`, `manylinux2014_aarch64`, `manylinux_2_35_armv7l`, `win_amd64`, `win32`,
`win_arm64` (cp311+), macOS x86_64/arm64/universal2, and Android.
`sherpa-onnx-core` 2.0.1 (carries `libonnxruntime`): `py3-none` wheels for the same Linux,
Windows and macOS targets. ASR needs no work per platform. Not checked: GPU (CUDA/DirectML)
builds of sherpa-onnx; the PyPI wheels are assumed CPU-only.

## 3. Cross-platform dictation apps

| App | Platforms (release assets / docs) | Parakeet TDT v3 | Stack | Licence | Stars, last push, release |
| --- | --- | --- | --- | --- | --- |
| Handy (cjpais/Handy) | macOS arm64/x64, Windows x64/arm64 (.exe/.msi), Linux x86_64/aarch64 (.deb/.rpm/AppImage) | yes: catalog id `parakeet-tdt-0.6b-v3` (GGUF, `handy-computer/parakeet-tdt-0.6b-v3-gguf`), via `transcribe-cpp` 0.4 | Tauri 2 / Rust | MIT | 37,473; 2026-11-02; v0.10.0 (2026-11-02) |
| OpenWhispr (OpenWhispr/openwhispr) | macOS, Windows 10+ x64, Linux x86_64 only ("there's no ARM build") | yes, via sherpa-onnx (README credits) | Electron 41 + C/Swift helpers | MIT | 10,686; 2026-11-04; v1.11.0 (2026-10-29) |
| VoiceTypr (moinulmoin/voicetypr) | macOS 14+, Windows 10/11; no Linux | macOS only ("Apple Silicon-optimized Parakeet models on macOS") | Tauri | AGPL-3.0 | 740; 2026-11-02; v2.0.11 |
| Whispering (EpicenterHQ/epicenter, `apps/whispering`) | README: "Windows and Linux are untested" | not checked | Tauri / Bun | AGPL-3.0 (per earlier research) | monorepo 4,839; last release v7.11.0 (2025-12-27) |
| Murmure (Kieirra/murmure) | "cross platform" (description; platforms not checked) | not checked | not checked | AGPL-3.0 | 1,152; 2026-10-29 |

Sources: `gh repo view`, `gh release view v0.10.0 -R cjpais/Handy`, Handy
`src-tauri/src/catalog/catalog.json`, OpenWhispr README and docs.openwhispr.com/platform/linux,
VoiceTypr README.

### 3.1 Handy details relevant to paraspeakrs

- Text insertion (`src-tauri/src/clipboard.rs`): paste methods `CtrlV`, `Direct`, `None`,
  `ShiftInsert`, `CtrlShiftV`, `ExternalScript`. `Direct` on macOS/Windows is
  `enigo.text()` (`input.rs::paste_text_direct`); enigo's Windows backend uses `SendInput` with
  `KEYEVENTF_UNICODE` (`src/win/win_impl.rs`).
- Linux insertion order: Wayland → `kwtype` on KDE, else `wtype` (skipped on KDE and GNOME:
  "Mutter deliberately does not implement the virtual-keyboard-v1 protocol"), then `dotool`, then
  `ydotool`; X11 → `xdotool`, then `ydotool`; enigo as last resort. Clipboard write uses
  `wl-copy` on Wayland. README: Ubuntu 26.04 needs `ydotool` with a systemd daemon.
- Windows "reliable paste": delayed-render clipboard (`SetClipboardData(CF_UNICODETEXT, NULL)`,
  `WM_RENDERFORMAT` as read receipt) in `src-tauri/src/paste_tx/windows.rs`.
- Hotkeys: `handy-keys` 0.3.4 or `tauri-plugin-global-shortcut`; on Wayland the README tells
  users to bind `handy --toggle-transcription` or `pkill -USR2 -n handy` in the desktop/WM.
  That is toggle only; no push-to-talk on Wayland through this path.
- Extension points: CLI remote control (`--toggle-transcription`, `--cancel`), a
  `hooks/transcription` stdin/stdout text hook (macOS and Linux only), `ExternalScript` paste.
- No remote/pluggable ASR. PRs #804 "Remote Whisper (OpenAI-compatible) transcription" and #886
  "OpenAI-compatible local server transcription" were closed unmerged (2026-02).
  `CONTRIBUTING.md` declares a feature freeze ("a new feature that the community has not asked
  for ... will be rejected") and "Handy aims to be the most forkable speech-to-text app".
- Handy is dictation only: no meeting / system-audio recording.

### 3.2 OpenWhispr details

- Ships its own native helpers in `resources/`: `windows-fast-paste.c` (`SendInput` Ctrl+V,
  releases held modifiers first), `windows-key-listener.c` (hold-to-talk), 
  `windows-system-audio-helper.c` (process loopback, see §6), `linux-fast-paste.c` (XTest and
  `/dev/uinput`), `linux-key-listener.c`, `linux-system-audio-helper.c` (PipeWire),
  `src/helpers/gnomeGlobalShortcutsPortal.js` (listens to `Activated` and `Deactivated`).
- Linux docs: uinput helper needs the `input` group on Wayland; fallbacks `xdotool` (X11),
  `wtype` (wlroots), `ydotool` + `ydotoold` (GNOME/KDE); else "Clipboard Mode on Wayland".
  Hold-to-talk works natively on GNOME 48+, KDE and Hyprland; elsewhere needs the `input` group.
- Windows docs: no permissions needed; Ctrl+V for apps, Ctrl+Shift+V for detected terminals.
- Has meeting transcription with live diarization. Business model: free local use, paid cloud
  tiers, optional account (docs FAQ). Whether meetings store mic and system audio as separate
  tracks: not verified (a `native/meeting-aec-helper` built on WebRTC AEC suggests the two are
  processed separately).

## 4. Text insertion per OS

| OS / session | Mechanism | Limits | Tools / libraries |
| --- | --- | --- | --- |
| Windows | `SendInput` with `KEYEVENTF_UNICODE` (typing) or Ctrl+V | UIPI: "Applications are permitted to inject input only into applications that are at an equal or lesser integrity level"; failure is silent ("neither GetLastError nor the return value will indicate"). Held keys interfere; docs advise `GetAsyncKeyState` checks | enigo (`text()`), OpenWhispr `windows-fast-paste.c`, pynput |
| Linux X11 | XTest (`xdotool type` / `key`) | none beyond X11 | enigo default `x11rb` (or `xdo`), xdotool, pynput |
| Wayland, wlroots (Sway, Hyprland) | `virtual-keyboard-unstable-v1` | not on GNOME or KDE | `wtype`, enigo `wayland` feature (experimental) |
| Wayland, GNOME | libei via `org.freedesktop.portal.RemoteDesktop.ConnectToEIS` (GNOME 46+ per wdotool-core source comment, not verified in GNOME notes); or uinput | Mutter will not implement virtual-keyboard ("Upstream said it's unlikely ... recommended using libei", wtype #22, mutter #1974). RemoteDesktop shows a consent dialog; whether a restore token avoids it on every start: not verified | enigo `libei` feature (experimental; tested on GNOME Wayland per README), `ydotool`/`dotool` (uinput, needs `input` group or udev rule) |
| Wayland, KDE | libei via RemoteDesktop portal (Plasma 6.1+; xdg-desktop-portal-kde !279, KWin libeis plugin); KDE fake-input protocol | wtype unsupported | `kwtype`, enigo `libei`, ydotool |

enigo (enigo-rs/enigo, MIT, 1,804 stars, pushed 2026-11-02): README lists Windows text,
X11 text, and "Linux (Wayland) text (Experimental)" and "Linux (libei) text (Experimental)",
behind features `wayland` and `libei` "because there are currently some bugs with it".

pynput (LGPL-3.0, 1.8.2, 2026-05): Linux uses X or uinput; uinput requires root; under Wayland
it only reaches Xwayland apps (pynput docs "Platform limitations"). Not suitable for Wayland.

## 5. Global hotkeys with press and release

| OS / session | Native API | Release event | Library notes |
| --- | --- | --- | --- |
| Windows | `RegisterHotKey` → `WM_HOTKEY` on press | no release message; libraries poll | `global-hotkey` 0.9.0 emits `Pressed` on `WM_HOTKEY`, then polls `GetAsyncKeyState` every 50 ms and emits `Released` (`src/platform_impl/windows/mod.rs`). Low-level hooks (handy-keys, OpenWhispr `windows-key-listener.c`) see real key-up |
| Linux X11 | `XGrabKey` | `KeyRelease` | `global-hotkey` X11 backend emits `Released` on `KeyRelease`; README: "Linux (X11 Only)" |
| Wayland, any | XDG `org.freedesktop.portal.GlobalShortcuts` (interface v2) | spec: "The Activated and Deactivated signals are emitted, respectively, whenever a shortcut is activated and deactivated" | must use D-Bus (e.g. `ashpd` in Rust; not checked) |
| Wayland, any (no portal) | evdev `/dev/input/event*` | yes | `handy-keys` 0.3.4 (MIT): "works identically on Wayland, X11, and the console"; needs read access to `/dev/input` (input group or udev `uaccess` rule); blocking needs `/dev/uinput` |

GlobalShortcuts portal backends:

| Desktop | Support | Source |
| --- | --- | --- |
| GNOME | 48+ (xdg-desktop-portal-gnome 48.rc "Add global shortcuts portal backend"; release notes: "fully supported by GNOME 48") | release.gnome.org/48/developers, x-d-p-gnome NEWS |
| KDE Plasma | yes (5.27+ per third-party wiki; version not verified in KDE sources) | ArchWiki portal table; agent-desktop wiki |
| Hyprland | yes (`hyprland.portal` lists `org.freedesktop.impl.portal.GlobalShortcuts`) | hyprwm/xdg-desktop-portal-hyprland |
| Sway / wlroots | no (xdg-desktop-portal-wlr #240 open; needs a Wayland protocol) | emersion/xdg-desktop-portal-wlr#240, swaywm/sway#8062 |
| COSMIC | conflicting: ArchWiki table says "No"; a third-party issue says "works" | not verified |

Release delivery in practice: OpenWhispr docs state GNOME 48+ "can report the key release;
older GNOME reports a shortcut as a single press", and that hold works on KDE and Hyprland.
That is vendor documentation, not tested here.

rdev (Narsil/rdev 0.5.3, last release 2023-06): stale; Handy depends on the
`rustdesk-org/rdev` fork instead. Its Wayland behaviour was not checked.

## 6. Meeting recording: mic and system audio per OS

### 6.1 OS mechanisms

| OS | System-audio capture | Separate mic track | Notes |
| --- | --- | --- | --- |
| Windows | WASAPI loopback on a render endpoint (`AUDCLNT_STREAMFLAGS_LOOPBACK`); or process loopback via `ActivateAudioInterfaceAsync` with `AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS` (include or exclude a process tree) | yes, separate capture client | Process loopback minimum: Microsoft Learn says "Windows 10 Build 20348"; OpenWhispr's helper comment says "Windows 10 2004+". Conflict not resolved |
| Linux | PipeWire stream with `stream.capture.sink = true` (records the default sink's monitor); PulseAudio `.monitor` sources | yes | OpenWhispr docs: "no permission dialog and no screen-share picker on Linux" |
| macOS | Core Audio process taps (current Swift app) | yes | unchanged |

### 6.2 Libraries

| Library | Windows loopback | Linux monitor | macOS | Status |
| --- | --- | --- | --- | --- |
| cpal (RustAudio) 0.18.2, 2026-08-16 | yes: input stream on a render device sets `AUDCLNT_STREAMFLAGS_LOOPBACK` (`src/host/wasapi/device.rs` line ~867) | yes: native PipeWire host (new in 0.18.0) sets `STREAM_CAPTURE_SINK` (`src/host/pipewire/device.rs` line 168); PulseAudio host also new | loopback on macOS > 14.6 (0.17.0 changelog) | Linux default host priority PipeWire > PulseAudio > ALSA |
| wasapi (HEnquist/wasapi-rs) 0.26.0, 2026-10-11 | "Loopback capture"; `PROCESS_LOOPBACK` appears in `src/api.rs` | — | — | Windows only |
| pipewire-rs | not checked | — | — | — |
| SoundCard (Python, bastibe) 0.4.6, 2026-04 | loopback via `include_loopback` (Media Foundation/WASAPI backend) | loopback via PulseAudio backend | `coreaudio.py` mentions `include_loopback`; support not verified | BSD-3-Clause |

Endpoint loopback (cpal) records everything the device plays, including the recorder's own
sounds. Process loopback in exclude mode avoids that; cpal does not expose it (not found in
the source search), wasapi-rs appears to.

Timestamps: the Swift recorder locks both tracks to host time. cpal's input callback carries a
`StreamInstant` capture timestamp per buffer (cpal API; not re-checked for 0.18). Whether that
is enough for sample-accurate alignment across a mic device and a loopback device: not verified.

### 6.3 Existing cross-platform meeting recorders

| Project | Platforms | Mic + system as separate tracks | Diarization | Licence |
| --- | --- | --- | --- | --- |
| Meetily (Zackriya-Solutions/meeting-minutes, 32,442 stars, v0.4.1 2026-09-12) | Windows and macOS packaged; Linux "build from source" | no: README "Capture microphone and system audio simultaneously with intelligent ducking and clipping prevention" (mixing). Non-macOS path in `audio/capture/system.rs` bails "System audio capture not yet implemented for this platform"; Windows devices go through cpal WASAPI output devices; Linux looks for ALSA devices named "monitor" | speaker identification is in paid "Meetily PRO" (README) | MIT (community edition) |
| OpenWhispr | macOS, Windows, Linux x86_64 | not verified (separate helpers for mic and system; AEC helper) | yes, local | MIT |

## 7. Options

Shared first step for every option: make the CLI install and run on Linux and Windows. That is
§1.4 route 1 plus small Python fixes. sherpa-onnx already covers all targets. Without this, no
front end on Linux/Windows can produce speaker-aware transcripts.

| | (a) Port each piece natively | (b) One cross-platform app (Tauri/Rust) replacing the Swift app | (c) Adopt existing apps; paraspeakrs stays CLI/back end |
| --- | --- | --- | --- |
| What it takes | Windows: C#/C++ or Rust tray app with WASAPI capture, low-level keyboard hook, `SendInput`. Linux: PipeWire capture, portal/evdev hotkey, libei/uinput/xdotool typing ladder. Keep Swift on macOS | Rust core: cpal 0.18 for mic + loopback (two WAVs), `global-hotkey` / `handy-keys` / portal for hotkeys, enigo (+ wtype/ydotool/kwtype ladder copied from Handy) for typing; spawn `paraspeakrs dictate --serve` and `paraspeakrs` for transcription. Optionally replace the Swift app on macOS too | Dictation: point Linux/Windows users to Handy (Parakeet TDT v3 locally, MIT). Meetings: a small recorder that writes two WAVs per OS, or OpenWhispr's MIT helpers (`windows-system-audio-helper.c`, `linux-system-audio-helper.c`) adapted as sidecars that paraspeakrs launches |
| Pros | Best native behaviour per OS | One codebase; Handy, OpenWhispr and Meetily prove the stack; reuses `dictate --serve` JSON protocol | Least code; Handy already solves the Linux insertion and Wayland hotkey cases; paraspeakrs keeps its scope (transcription, diarization, agent tooling) |
| Cons | Three UI codebases; highest maintenance | Linux Wayland insertion and hotkeys stay fragile (every project above documents workarounds); Tauri needs WebKitGTK on Linux (Handy README lists renderer and `gtk-layer-shell` issues); still a new app to maintain | Two ASR stacks for users (GGUF in Handy, sherpa-onnx in paraspeakrs); Handy will not accept a remote-ASR backend (PRs #804, #886 closed; feature freeze), so feeding Handy into paraspeakrs needs a fork; no meeting recorder exists that matches the two-track design |
| Maintenance cost (estimate, not measured) | high | medium–high | low for dictation; medium for the recorder sidecars |

Recommendation:

1. Do the shared CLI step first: speakrs 0.7 sidecar wheels for macOS arm64, Linux x86_64
   (`cpu` + `cuda`), Linux aarch64 and Windows x64 (`cpu`), built with `PyO3/maturin-action`.
   Gate: a Windows `cargo build --features cpu` in CI, and a measured `cpu`-mode RTFx on one
   x86_64 machine.
2. Dictation on Linux/Windows: option (c). Document Handy as the dictation tool there; keep the
   Swift dictation on macOS. Do not port `ctrl+\` dictation unless Handy's behaviour is
   insufficient for a named reason.
3. Meeting recording on Linux/Windows: a small Rust CLI recorder (`paraspeakrs record` sidecar)
   on cpal 0.18 that writes the mic and the loopback as two WAVs, as in option (b) but without
   UI. Borrow OpenWhispr's process-loopback (Windows) and PipeWire capture-sink (Linux) code
   patterns if cpal's endpoint loopback picks up unwanted audio.

Questions to answer before implementation:

1. Is Windows a required target for diarization, or are Linux x86_64 and macOS enough? (If
   enough, upstream `pip install speakrs` covers both without a Rust build here.)
2. Is CPU-only diarization acceptable on Linux/Windows without NVIDIA, before its speed is
   measured?
3. Is recommending Handy for dictation on Linux/Windows acceptable (yes/no)?
4. Must Linux/Windows meeting recordings keep two separate time-aligned tracks, as on macOS
   (yes/no)?
5. Is a tray UI needed on Linux/Windows, or is a CLI `record` command enough?

## Sources

- speakrs: github.com/avencera/speakrs (`README.md`, `CHANGELOG.md`, `Cargo.toml`,
  `.github/workflows/ci.yml`, `.github/workflows/package.yml`, `crates/speakrs-python/pyproject.toml`,
  `crates/speakrs-python/Cargo.toml`, `benchmarks/voxconverse-dev.txt`);
  crates.io/api/v1/crates/speakrs; pypi.org/pypi/speakrs/json; hf-fetch 0.1.2 dependencies.
- sherpa-onnx: pypi.org/pypi/sherpa-onnx/json, pypi.org/pypi/sherpa-onnx-core/json.
- maturin bin bindings: https://www.maturin.rs/bindings.html
- Handy: github.com/cjpais/Handy (`README.md`, `CONTRIBUTING.md`, `src-tauri/Cargo.toml`,
  `src-tauri/src/clipboard.rs`, `src-tauri/src/input.rs`, `src-tauri/src/settings.rs`,
  `src-tauri/src/paste_tx/windows.rs`, `src-tauri/src/shortcut/mod.rs`,
  `src-tauri/src/catalog/catalog.json`), release v0.10.0, PRs #804 and #886.
- handy-keys: github.com/handy-computer/handy-keys `README.md`.
- OpenWhispr: github.com/OpenWhispr/openwhispr (`README.md`, `resources/*.c`,
  `src/helpers/gnomeGlobalShortcutsPortal.js`); docs.openwhispr.com/platform/linux,
  /platform/windows, /faq.
- VoiceTypr README; Epicenter `apps/whispering/README.md`; Meetily `README.md`,
  `frontend/src-tauri/src/audio/capture/system.rs`, `audio/devices/platform/{windows,linux}.rs`.
- enigo: github.com/enigo-rs/enigo (`README.md`, `Cargo.toml`, `src/win/win_impl.rs`).
- global-hotkey: github.com/tauri-apps/global-hotkey (`README.md`, `src/lib.rs`,
  `src/platform_impl/windows/mod.rs`, `src/platform_impl/x11/mod.rs`).
- cpal: github.com/RustAudio/cpal (`README.md`, `CHANGELOG.md`, `src/host/wasapi/device.rs`,
  `src/host/pipewire/device.rs`). wasapi-rs `README.md`. SoundCard `README.rst`.
- XDG GlobalShortcuts: https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.GlobalShortcuts.html;
  https://release.gnome.org/48/developers/; xdg-desktop-portal-gnome NEWS;
  https://github.com/emersion/xdg-desktop-portal-wlr/issues/240;
  https://github.com/swaywm/sway/issues/8062; hyprwm/xdg-desktop-portal-hyprland `hyprland.portal`;
  https://wiki.archlinux.org/title/XDG_Desktop_Portal.
- libei / RemoteDesktop: https://invent.kde.org/plasma/xdg-desktop-portal-kde/-/merge_requests/279;
  KWin commit 499d006e; Phoronix "KDE Fixes For Qt 6.7" (Plasma 6.1 ConnectToEIS);
  who-t.blogspot.com 2022-12; docs.rs wdotool-core `src/recorder/portal.rs`.
- wtype on GNOME: https://github.com/atx/wtype/issues/22; https://gitlab.gnome.org/GNOME/mutter/-/issues/1974.
- Windows APIs: learn.microsoft.com `SendInput`, `RegisterHotKey`,
  `AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS`.
- pynput: https://pynput.readthedocs.io/en/latest/limitations.html
