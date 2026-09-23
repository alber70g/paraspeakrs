# Setting this up on another Mac

> **Try `uv tool install paraspeakrs` first.** It installs the diarizer binary and
> downloads both model sets on its own, so none of the steps below are needed. What
> follows is the fallback for a machine that cannot reach PyPI.

Apple Silicon, macOS 14+. Everything below runs as your normal user; `sudo` is only
needed if you choose to install Homebrew.

## 0. Clear the quarantine flag (only for some transfers)

Quarantine is set by the application that downloaded the file, not by the build.
A zip that arrived via **Safari, Chrome, AirDrop, Mail or Slack** is quarantined
and macOS will refuse to run the bundled binary:

```sh
xattr -dr com.apple.quarantine .
```

Pulling with `gh release download`, `curl`, `scp` or `git clone` sets no
quarantine at all, so this step is unnecessary there.

If Gatekeeper still blocks `bin/speakrs-diar`, the machine's MDM policy requires
notarized binaries. In that case rebuild from source instead — see step 5.

## 1. Prerequisites

```sh
# uv installs and manages its own Python, so no admin rights needed.
curl -LsSf https://astral.sh/uv/install.sh | sh

# ffmpeg + ffprobe are required for every audio path.
brew install ffmpeg
# No Homebrew? Download static builds from https://evermeet.cx/ffmpeg/
# and put ffmpeg and ffprobe somewhere on your PATH.
```

## 2. Install the Python side

```sh
uv sync --python 3.11 --group dev
```

This is the trimmed install (~75 MB). It deliberately does **not** pull torch or
senko. Add `--extra senko` only if you want the old diarization backend (~1.2 GB,
and it is the one with the known speaker-collapse bug), or `--extra mcp` / `--extra tui`.

## 3. Get the ASR model

The Parakeet sherpa-onnx INT8 model is not in this zip (~639 MB). Run
`paraspeakrs fetch-models`, or download
`sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8` and unpack it so that this path exists:

```
models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8/
  encoder.int8.onnx  decoder.int8.onnx  joiner.int8.onnx  tokens.txt
```

Or point `PARAKEET_SHERPA_MODEL_DIR` / `--sherpa-model-dir` at wherever you put it.

## 4. Point the service at the bundled diarizer

The binary in `bin/` is self-contained — it carries its own OpenBLAS and needs no
Homebrew. Its diarization models (~315 MB) download from the public
`avencera/speakrs-models` repo on first run; no Hugging Face token is required.

```sh
export SPEAKRS_BIN="$PWD/bin/speakrs-diar"
uv run paraspeakrs run --txt /path/to/audio.wav
```

Use an absolute path for `SPEAKRS_BIN`: the default is relative to the working
directory, which bites when an MCP client launches the server from elsewhere.

For the terminal UI, which reads env vars only and takes no flags:

```sh
export PARAKEET_RECORDINGS_DIR="$HOME/Library/Application Support/MacParakeet/meeting-recordings"
uv run --extra tui paraspeakrs tui
```

## 5. Rebuilding the binary instead (optional)

If Gatekeeper blocks the bundled one, or you are not on Apple Silicon:

```sh
brew install openblas
cd packages/speakrs-diar
PKG_CONFIG_PATH="/opt/homebrew/opt/openblas/lib/pkgconfig" \
  cargo build --release --features coreml     # NVIDIA: --features cuda
```

The crate defaults to the `blas-system` feature, which links Homebrew's OpenBLAS.

## 6. Verify

```sh
uv run --group dev pytest                    # 60 tests
ffmpeg -f lavfi -i "sine=frequency=440:duration=5" -ar 16000 -ac 1 /tmp/tone.wav
bin/speakrs-diar --mode coreml /tmp/tone.wav  # should emit JSON, not crash
```

## What is not in this zip

`var/` (previous jobs — audio, transcripts, speaker embeddings), `test-audio/`,
`models/`, and `checkpoints/` are all excluded. The speaker cache starts empty, so
speaker names must be learned again on this machine.

Jobs and the speaker cache are written to
`~/.local/share/fast-speaker-aware-meeting-transcriber`, not into the checkout, so
they survive replacing this directory with a newer build. Override with
`PARAKEET_WORKSPACE_DIR`.
