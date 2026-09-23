# Changelog

All notable changes to `paraspeakrs` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [0.5.0] — 2026-09-23

### Added

- **`paraspeakrs agent`: speaker naming for coding agents, through files, with no MCP
  server.** `agent transcribe <audio> -o <dir>` writes the transcript and one sample per
  speaker, `<ID>__NAME-ME.wav`. Each sample stitches several takes of that speaker, at least
  8 s where they said that much, separated by a beep. A voice the library recognizes arrives as `<ID>__Alice_0.82.wav`.
  The user renames the samples to name the speakers: the same name twice merges them, and
  deleting a sample drops that speaker's lines. `agent apply <dir>` writes the names into
  the transcript and can be re-run after further renames. `--dry-run` shows what would
  change first. `agent help` prints the guide for agents. Both commands print JSON.

### Changed

- **The data directory is now `~/.local/share/paraspeakrs`** (or `$XDG_DATA_HOME/paraspeakrs`).
  It used to be named after the project's old name, `fast-speaker-aware-meeting-transcriber`.
  The first start of this version moves the old directory to the new name, with every job,
  named voice, note, the queue and the downloaded ASR model, and says so on stderr. If both
  directories exist nothing is merged: the new one is used and the old one is left
  untouched. If the move fails, the old directory stays in use rather than opening an empty
  workspace. `PARAKEET_WORKSPACE_DIR` is still honoured and is never moved.

## [0.4.1] — 2026-09-23

### Fixed

- **Retrying a failed recording no longer breaks the TUI.** Queuing a file that had failed
  added a second queue entry for the same path, which crashed the queue table with
  `DuplicateKey` right away and again on every later start. A retry now reuses the failed
  entry, and a `ui-state.json` that already holds duplicates is repaired when it loads.

### Added

- **Debug information for failed runs.** The TUI writes to `<workspace>/logs/tui.log`,
  including the full traceback of every failed transcription. Pressing enter on a failed queue
  row shows the error (the row itself cuts it off at 40 characters) and the log's path.
  `speakrs-diar` now runs with `RUST_BACKTRACE=1`, so a panic (exit code 101) records
  where it happened.

## [0.4.0] — 2026-09-23

### Added

- **Remove what was not part of the meeting.** A recording picks up whatever the mic heard —
  the kids talking through a call while the call mic was muted and the recorder was not.
  In the TUI, `d` on a speaker removes every line of theirs; `d` on a line in the new
  transcript pane removes just that one. Nothing is deleted: a removal is a mark on the
  job, shown struck through, and `d` again undoes it. Only the written transcript (TUI and
  MCP `get_transcript`) leaves it out. A removed speaker no longer counts as unnamed and is
  skipped by accept-all.

- **A transcript pane in the TUI's detail view**, under the speaker table. It shows the
  whole transcript with the selected speaker's lines bright and the rest dimmed, and jumps
  to that speaker as the table cursor moves. `j`/`k` move a line, `h`/`l` jump to the
  selected speaker's previous/next line. `Tab` now walks six panels.

- **Single-speaker stereo channels.** `Enter` on a stereo recording asks whether either
  channel is one person on their own mic; every cluster diarized on that channel is folded
  into one speaker, with a talk-time-weighted voice print, so a cough or a lowered voice
  does not come back as a second person to name. The answer survives a restart with the
  queue.

### Changed

- **Transcripts always open with YAML frontmatter**: `title`, `date` (the day the recording
  was made), the named `speakers`, and `note` when there is one. A job with no note used to
  get no header at all — anything that parses transcripts should skip the `---` fence.

- **The note panel is a small strip** headed by that same title, date and speakers line. It
  follows the names as you assign them and is never written into the note itself.

- The MCP `transcribe` roster has an `excluded` field per speaker.

### Fixed

- The speaker table's cursor jumped back to the first row after naming, accepting or
  removing a speaker.

## [0.3.2] — 2026-09-23

### Fixed

- **An already-downloaded ASR model is used instead of fetched again.** A model fetched at
  one precision was invisible to a run configured for another — so `paraspeakrs mcp`, the
  TUI or the CLI started with default settings would ask the first-run question or start a
  2.4 GB FP32 download beside a working INT8 or FP16 model. Before downloading, paraspeakrs
  now looks in the configured model directory, inside it (so `PARAKEET_SHERPA_MODEL_DIR` may
  name the `models/` folder), beside it, and in the workspace's `models/`. The workspace's
  first-run answer goes first, then FP32, FP16, INT8. An explicit precision is never
  substituted: finding INT8 when FP32 was asked for is a reason to download, not to swap.

- **The model directory follows `PARAKEET_WORKSPACE_DIR` / `--workspace-dir`.** Moving the
  workspace used to move the jobs but leave the model under `~/.local/share`.

### Changed

- **Every surface says which ASR model it loaded** — `ASR model: Parakeet FP32 · <path>` on
  stderr at startup (stdout stays clean for stdio MCP), and in the TUI's header, where the
  startup line would otherwise vanish behind the full-screen UI.

## [0.3.1] — 2026-09-22

### Added

- **Meeting notes.** Every job can carry a free-text note about what the recording
  was about — the one question neither the diarization nor the transcript answers,
  and the one that picks the right recording out of a list of thirty three weeks
  later. It is edited in a panel beside the selection and saves when focus leaves
  it (and on quit), so there is no key to remember. Notes are stored as `note.json`
  beside the job's `artifacts.json` rather than inside it: that file holds the
  diarization and a float vector per speaker, and rewriting all of it to record a
  typed sentence would risk the expensive half of a job for the cheap half. Pruning
  a job's working audio keeps the note.

- **A "processed" panel** on the home screen, listing everything already
  transcribed with its speaker and still-unnamed counts, a `✎` when it has a note,
  and `^d` to delete a job after asking. It replaces the jobs screen, which listed
  the same jobs one screen away from the speaker table you wanted them for.

- **The note reaches the written transcript**, as YAML frontmatter above the first
  line. It is always a block scalar, even for a one-line note: a note is prose, and
  prose containing a colon would otherwise make the header invalid YAML. A job
  without a note gets no fence at all, and the line count returned when a
  transcript is written counts spoken lines only — so it moves when something was
  said, not when somebody edits the note.

- **`get_note` / `set_note` MCP tools**, so an agent can read and write the same
  note the terminal UI shows. `set_note` replaces rather than appends.

### Changed

- **The TUI is now one screen instead of three.** The file browser, the processed
  recordings and the queue sit down the left; the speaker table and the note for
  whichever of them the cursor is on sit on the right. Moving down a list re-aims
  the detail rather than travelling to it, and `Tab` / `Shift+Tab` walk the five
  panels. The speaker table is no longer a screen you have to find your way back
  from.

- **`Enter` on an already-transcribed recording now opens that recording's
  speakers.** It used to answer "already transcribed — press ctrl+j to open it",
  which opened a second list you then had to find the same filename in again; the
  panel beside the tree is already showing that file, so `Enter` hands focus to it.

- **A finished recording now leaves the queue** instead of sitting in it as `done`.
  It has become a job and is listed as one; showing it in both places made one
  recording look like two. Failures stay in the queue, which is where the error is.

### Removed

- **The jobs screen (`^j`)** and its `SpeakerScreen` host. The processed panel
  lists the same jobs, beside the speaker table rather than away from it.

## [0.3.0] — 2026-09-19

### Added

- **First run now asks which ASR model to use**, with a comparison table and a
  recommendation drawn from the machine's free disk and RAM. The answer is remembered,
  so it is asked once. It is skipped entirely when a precision is already named
  (`--sherpa-precision`, `PARAKEET_SHERPA_PRECISION`) or when there is no terminal to
  ask — scripts, servers and CI never block on it, and `PARAKEET_NONINTERACTIVE=1`
  silences it explicitly.

- **FP16 weights** (`--sherpa-precision fp16`): 99.8% identical to FP32 — 8 differing
  words across a 42-minute recording — in half the disk space. Nobody publishes an FP16
  build of Parakeet v3 in sherpa's layout (the upstream repo is empty, and third-party
  ONNX exports fuse decoder and joiner into one graph `from_transducer()` cannot load),
  so `paraspeakrs` derives one: it fetches the FP32 weights, converts them, and deletes
  the original. That saves disk, not bandwidth, and needs the new `fp16` extra
  (`--with 'paraspeakrs[fp16]'`).

### Changed

- **The ASR model now defaults to FP32 weights instead of INT8.** On non-English
  speech the INT8 build does not merely blur the occasional word — it drops whole
  clauses, and fills the gap with fluent-sounding nonsense that is easy to mistake
  for a correct transcript. Measured over a 42-minute Dutch meeting with identical
  chunking and greedy decoding, INT8 emitted 3546 words against FP32's 4715
  (**-25%**), losing content in *every single chunk*: `karmechanic AI` for *Car
  mechanic AI*, `factor databases` for *vector databases*, `En daar is in de handen
  maak een foto van voor de pasmachine` for *Duizenden handen die maak je een foto
  van bijvoorbeeld de wasmachine*.

  FP32 is also **faster** — 42 s against 65 s for that same recording. ONNX Runtime
  has no fast INT8 transducer kernel on arm64, so it dequantizes per operator and
  pays more than the smaller weights save. The costs of the new default are download
  size (2.4 GB, up from 490 MB) and memory, nothing else.

  INT8 stays available for tight disk or memory budgets via
  `--sherpa-precision int8` or `PARAKEET_SHERPA_PRECISION=int8`, and FP16 sits between
  them. **Lower precision does not save memory**, which is the counterintuitive part:
  ONNX Runtime has no fast low-precision transducer kernels on CPU and casts the weights
  back up at load time, so peak RSS is 3.15 GB for FP32, 3.29 GB for INT8 and 3.95 GB for
  FP16. FP32 is the lightest *and* the fastest; precision trades disk alone. Existing installs
  keep working: which weights get loaded is detected from the files present in the
  model directory, so a sideloaded copy of either build is used as-is. A machine
  that already has the INT8 model will download the FP32 one on its next run unless
  it opts out.

### Added

- `--sherpa-precision` / `PARAKEET_SHERPA_PRECISION` (`fp32` | `int8`) selects which
  weights to download.
- `PARAKEET_NONINTERACTIVE=1` suppresses the first-run question.
- `PARAKEET_FP32_BASE_URL` points the FP32 fetch at a mirror. sherpa-onnx publishes
  no FP32 tarball for v3, so those weights are pulled file by file from the upstream
  author's Hugging Face repo rather than as a single archive — which is why the
  existing `PARAKEET_MODEL_URL` (an archive URL) applies only to INT8.

## [0.2.3] — 2026-09-18

### Changed

- **Model downloads now name the directory they are writing into.** The ASR fetch
  printed only the URL, and the speakrs sidecar downloaded its diarization models
  with no output at all, so there was no way to learn where a local copy belongs
  short of reading the source. Both now print the destination before the download
  starts, along with the flag that points them elsewhere. The diarization notice
  appears only when the Hugging Face cache is still empty, so a normal run stays
  quiet.

## [0.2.2] — 2026-09-18

### Fixed

- **The MCP server could not start at all on a fresh install.** The `mcp` extra
  allowed any `mcp>=1.2.0`, which now resolves to 2.x, where `FastMCP` was renamed
  to `MCPServer`. `paraspeakrs mcp` then died on import with
  `ModuleNotFoundError: No module named 'mcp.server.fastmcp'`. The extra is capped
  at `<2` until the server is migrated to the 2.x API.

  This affected 0.2.0 and 0.2.1: the checkout's lockfile held mcp 1.x, so the
  suite passed while every fresh install was broken. A test now imports
  `paraspeakrs.mcp_server` so the next such rename fails in CI rather than for a
  user -- nothing else imports that module, which is why it went unnoticed.

## [0.2.1] — 2026-09-18

Fixes two bugs in 0.2.0 that its release testing did not reach, both found by
exercising the surfaces end to end rather than only the ones the CLI made easy.

### Fixed

- **`paraspeakrs mcp` rejected every flag it was supposed to forward.** Running
  `paraspeakrs mcp --transport stdio` failed with `unrecognized arguments:
  --transport`, which broke every MCP client configuration. The flags were routed
  through an `argparse.REMAINDER` positional, and REMAINDER does not capture a
  *leading* option -- argparse tried to match `--transport` against the top-level
  parser first. `mcp` now hands its arguments to the server before argparse sees
  them, so the server stays the one place those flags are defined.
- **ASR could install in a state where it never worked.** `sherpa_onnx` imports a
  compiled extension that dlopens `libonnxruntime`, which ships in the separate
  `sherpa-onnx-core` distribution. 0.2.0 depended only on `sherpa-onnx>=1.13.2`,
  and the 1.13.2 *wheels* do not declare the core dependency, so a resolver
  landing on that version produced a package that imported and then failed on
  every transcription. The floor is now `>=1.13.8` and `sherpa-onnx-core` is
  declared explicitly.

  0.2.0's notes described removing the old `asr` extra as a dependency cleanup.
  That was wrong: the extra carried `sherpa-onnx-core` for exactly this reason,
  and dropping it removed a safeguard rather than a redundancy.

### Changed

- `uv sync` in a checkout installs the prebuilt `paraspeakrs-speakrs` wheel from
  PyPI instead of compiling the Rust sidecar locally. The `[tool.uv.sources]`
  entry that forced a local build existed only to make `uv lock` resolvable
  before the sidecar was first published.

## [0.2.0] — 2026-09-18

Installable as a tool: `uv tool install paraspeakrs`.

### Changed

- **Renamed** from `parakeet-int8-pyannote-service` to `paraspeakrs`, and the import
  package from `parakeet_int8_pyannote_service` to `paraspeakrs`. The name now covers both
  halves of the pipeline — Parakeet for ASR, speakrs for diarization — rather than naming
  pyannote, which the project stopped depending on when speakrs became the default backend.
- **One command instead of three.** `parakeet-service`, `parakeet-tui` and `parakeet-mcp`
  are replaced by `paraspeakrs` with `run`, `serve`, `label-dir`, `tui`, `mcp` and
  `fetch-models` subcommands. The `tui` and `mcp` imports stay lazy so the core install
  does not need their extras.
- **The ASR model and diarizer binary no longer default to paths relative to the working
  directory.** Both defaulted into the checkout (`models/…`, `packages/speakrs-diar/…`),
  so the tool only worked when launched from the repository root. The model now defaults
  under the workspace directory, and the binary is looked up next to the interpreter, then
  on `PATH`, then in a checkout — so a source checkout keeps working unchanged.
- `label-dir` now requires its directory argument. It used to default to `test-audio`,
  which only means something inside the repository.
- Environment variable names are unchanged; existing `PARAKEET_*` and `SPEAKRS_*` setups
  keep working, as does the workspace at
  `~/.local/share/fast-speaker-aware-meeting-transcriber`.

### Added

- **The ASR model downloads itself on first run** (~490 MB, from the sherpa-onnx GitHub
  release — no Hugging Face account or token). `paraspeakrs fetch-models` pre-seeds it;
  `PARAKEET_AUTO_DOWNLOAD=0` disables the implicit fetch. Downloads stage through a
  temporary directory and are renamed into place, so an interrupted fetch cannot leave
  behind something that looks like a working model.
- `PARAKEET_MODEL_URL` points the download at a mirror or internal proxy.
- Failures to obtain the model now explain the three ways around it — mirror, sideload,
  or the OpenAI-compatible backend, which needs no local model at all.
- `paraspeakrs-speakrs`, a companion wheel carrying the speakrs diarization binary for
  macOS arm64, so the default backend works without Rust, cargo or Homebrew. The binary
  is built with `--features coreml,blas-static` and its gfortran runtime is vendored into
  the wheel, so it depends only on macOS system frameworks.
- `paraspeakrs --version`.
- Packaging metadata for publication: license, authors, classifiers, project URLs, and a
  tag-triggered PyPI workflow using trusted publishing.

### Fixed

- `rich` is imported by the TUI but was never declared as a dependency; it arrived only as
  a transitive dependency of `textual`. It is now declared in the `tui` extra.
- `soundfile` was a required dependency but is imported nowhere in the package. Removed.
- `sherpa-onnx` was declared both as a core dependency and in an `asr` extra, with
  different version floors, while every documented command passed `--extra asr`. The extra
  is gone; the core dependency stands.
- `__version__` was hard-coded and had drifted behind the packaged version. It now reads
  the installed distribution metadata.
