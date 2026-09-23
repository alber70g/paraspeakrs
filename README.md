# paraspeakrs

Speaker-aware transcription for long meeting recordings: **Para**keet for ASR,
**speakrs** for diarization.

```sh
uv tool install paraspeakrs          # the tool, plus the diarizer binary on macOS arm64
brew install ffmpeg                  # ffmpeg and ffprobe must be on PATH

paraspeakrs run --txt meeting.m4a    # transcribe; models download themselves on first run
paraspeakrs tui                      # the terminal UI (add --with 'paraspeakrs[tui]')
paraspeakrs mcp                      # the MCP server (add --with 'paraspeakrs[mcp]')
```

To install with the optional interfaces in one go:

```sh
uv tool install paraspeakrs --with 'paraspeakrs[tui]' --with 'paraspeakrs[mcp]'
```

The first run asks which Parakeet ASR model you want (FP32, FP16 or INT8 — see
[Model precision](#model-precision)) and downloads it, along with the speakrs diarization
models (~315 MB) into `~/.local/share/fast-speaker-aware-meeting-transcriber`. Neither
needs an account or a token. `paraspeakrs fetch-models` pre-seeds the ASR model; see
[Running without model downloads](#running-without-model-downloads) if your network
blocks the download.

Jobs, transcripts and the speaker cache live in that same directory, not in the
working directory, so `paraspeakrs` behaves the same wherever it is launched from.

## What it does

- API plus background worker
- FFmpeg normalization to mono 16 kHz WAV for ASR; channel-preserving 16 kHz for diarization
- Speaker diarization before transcription (mono in → one pass; stereo in → split L/R, diarize each channel, merge with `L_`/`R_` speaker prefixes)
- Two diarization backends: **speakrs** (default) and Senko — see [Diarization backends](#diarization-backends)
- Per-speaker centroid embeddings come back from the diarizer directly — no separate embedding model
- Parakeet TDT v3 ASR, precision chosen on first run (see [Model precision](#model-precision)), on CPU, CUDA on NVIDIA GPUs, or CoreML on macOS
- ASR output is kept even when diarization has gaps

## Prerequisites

- **FFmpeg, with `ffprobe`, on `PATH`** — the only thing `uv tool install` cannot
  provide for you. Every audio path shells out to it. `brew install ffmpeg`, or static
  builds from <https://evermeet.cx/ffmpeg/>.
- Python 3.11 or 3.12, which `uv` will install and manage on its own.

The ASR model is downloaded on first use into
`~/.local/share/fast-speaker-aware-meeting-transcriber/models/`. It is a directory
containing `encoder.onnx`, `encoder.weights`, `decoder.onnx`, `joiner.onnx` and
`tokens.txt` (or the `.int8.onnx` equivalents for the INT8 build); point
`PARAKEET_SHERPA_MODEL_DIR` or `--sherpa-model-dir` at a copy you already have to skip
the download. Which variant is loaded is detected from the files present, so a
sideloaded directory of either precision just works.

### Model precision

On its **first run** `paraspeakrs` asks which model to use, with a recommendation based
on your free disk and RAM:

```
  Measured on one 42-minute Dutch meeting (4715 words is the reference):

              disk  peak RAM   words   decode   notes
  * fp32      2.4G     3.15G    4715      42s   full precision - the most accurate, the fastest, the lightest on RAM
    fp16      1.2G     3.95G    4712      46s   half precision - same transcript as FP32, half the disk
    int8      0.6G     3.29G    3546      65s   quantized - smallest download, but it drops speech
```

The answer is remembered, so it is asked once. Naming a precision up front skips the
question entirely, as does running without a terminal — scripts, servers and CI never
block on it:

```sh
paraspeakrs run --sherpa-precision fp16 meeting.m4a
export PARAKEET_SHERPA_PRECISION=int8     # or set it once
export PARAKEET_NONINTERACTIVE=1          # never ask, just use the default
```

**Two things about that table are worth knowing, because both are counterintuitive.**

*INT8 does not blur words — it deletes them.* On the recording above it lost roughly a
quarter of the transcript, missing content in **every single chunk**, and replaced it
with fluent-sounding nonsense: `karmechanic AI` for *Car mechanic AI*, `factor databases`
for *vector databases*, `En daar is in de handen maak een foto van voor de pasmachine`
for *Duizenden handen die maak je een foto van bijvoorbeeld de wasmachine*. Because the
output still reads like Dutch, the damage is easy to miss. Non-English speech is hit
hardest; English degrades far less.

*Lower precision does not save memory.* ONNX Runtime has no fast low-precision transducer
kernels on CPU, so it casts the weights back up at load time — FP32 is the **lightest**
of the three at 3.15 GB peak, and the fastest. Precision trades disk, nothing else. If
you are short on RAM rather than disk, none of these options helps: all three peak
between 3 and 4 GB.

So the short version: **pick FP32 unless disk is tight.** FP16 is 99.8% identical to it
(8 differing words across 42 minutes) in half the space. INT8 is a genuine compromise,
worth it only when 490 MB is all that fits.

FP16 has one wrinkle: nobody publishes an FP16 build of Parakeet v3 in sherpa's layout,
so `paraspeakrs` derives one by downloading the FP32 weights and converting them locally.
It saves disk, not bandwidth, and it needs the conversion extra:

```sh
uv tool install paraspeakrs --with 'paraspeakrs[fp16]'
```

Beam search (`modified_beam_search`) is *not* a substitute for precision: it recovers some
of what INT8 loses, but it cannot invent `vector` where the quantized encoder heard
`factor` — and on FP32 it actively *hurts*, pruning the discourse particles (`ja`, `nou`)
that spoken Dutch is full of.

### The diarizer binary

The default `speakrs` backend runs as a sidecar binary. The
`paraspeakrs-speakrs` wheel ships it prebuilt for **macOS arm64**, and
`uv tool install paraspeakrs` pulls it in automatically there — no Rust toolchain and no
Homebrew OpenBLAS.

On other platforms that wheel does not exist, so `paraspeakrs` installs without it and
you need one of:

- `--diar-backend senko` (install with `--with 'paraspeakrs[senko]'`; pulls torch, ~1.2 GB)
- your own build, with `SPEAKRS_BIN` pointing at it:
  ```sh
  brew install openblas
  cd packages/speakrs-diar
  PKG_CONFIG_PATH="/opt/homebrew/opt/openblas/lib/pkgconfig" \
    cargo build --release --features coreml     # NVIDIA: --features cuda
  ```

`paraspeakrs` finds the binary next to its own interpreter first, then on `PATH`, then in
a source checkout — so a development checkout keeps working with a plain
`cargo build --release` and no environment variable.

## Running without model downloads

Each download prints the directory it is writing into, so a sideloaded copy can simply
be placed there. The defaults are:

- ASR: `~/.local/share/fast-speaker-aware-meeting-transcriber/models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3`
  (with `-fp16` or `-int8` appended for those precisions)
  (under `$XDG_DATA_HOME` when that is set); `paraspeakrs fetch-models --help` prints the
  path this machine will use
- Diarization: `~/.cache/huggingface/hub/models--avencera--speakrs-models` (under `$HF_HOME`
  when that is set), the cache the speakrs sidecar shares with every other Hugging Face tool

Both downloads are plain HTTPS with no account and no token, but if your network blocks
them there are three ways through, in the order worth trying:

**1. Point at a mirror.** The two precisions come from different places, so they have
different overrides. FP32 is fetched file by file, so its override is a base URL:

```sh
# FP32 (default) -- must serve encoder.onnx, encoder.weights, decoder.onnx,
# joiner.onnx and tokens.txt directly underneath
export PARAKEET_FP32_BASE_URL="https://your-proxy.internal/parakeet-tdt-0.6b-v3"

# INT8 -- a single archive
export PARAKEET_SHERPA_PRECISION=int8
export PARAKEET_MODEL_URL="https://your-proxy.internal/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2"
```

**2. Sideload.** On any machine with access, fetch the model, copy the directory over,
and point at it. The precision is detected from the filenames, so either build works:

```sh
# FP32 (default): the five files from
# https://huggingface.co/csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3
export PARAKEET_SHERPA_MODEL_DIR="/path/to/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3"

# INT8: unpack
# https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2
export PARAKEET_SHERPA_MODEL_DIR="/path/to/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"

export PARAKEET_AUTO_DOWNLOAD=0   # optional: fail loudly instead of trying the network
```

The same applies to diarization: the speakrs sidecar fetches its models from the public
`avencera/speakrs-models` repository on first run, and `SPEAKRS_MODELS_DIR` points it at a
local copy instead.

**3. Skip the local model entirely.** The OpenAI-compatible ASR backend needs no download
at all — point it at any server exposing a compatible transcription endpoint:

```sh
export PARAKEET_ASR_BACKEND=openai
export PARAKEET_OPENAI_BASE_URL="http://your-host/v1"
```

Diarization still runs locally, so this only removes the ASR download.

Supported ASR devices:

- `cpu`: Linux, Windows, or macOS CPU inference
- `cuda`: NVIDIA GPU inference on Linux or Windows with a CUDA-enabled `sherpa-onnx` wheel
- `coreml`: macOS inference through ONNX Runtime's CoreML execution provider

Supported Senko devices (`--senko-device` / `SENKO_DEVICE`):

- `auto` (default), `cpu`, `cuda`, `coreml`

## Diarization backends

Select with `--diar-backend` / `PARAKEET_DIAR_BACKEND`.

### `speakrs` (default)

A Rust port of the pyannote `community-1` pipeline. It finds more speakers and far
finer turns than Senko: on a 3059 s four-person recording Senko reported 3 speakers
with a single 347 s unbroken block, while speakrs reported 4 speakers with a 43 s
longest block — and ran faster (6.1 s vs 8.3 s).

speakrs is a Rust library with no Python bindings, so it runs as a sidecar binary
that must be built once:

```sh
brew install openblas                       # macOS; Linux: your distro's openblas-dev
cd packages/speakrs-diar
PKG_CONFIG_PATH="/opt/homebrew/opt/openblas/lib/pkgconfig" \
  cargo build --release --features coreml   # NVIDIA: --features cuda; otherwise omit
```

Build with the feature matching the `--speakrs-mode` you intend to use; a binary
built without `coreml` rejects `--speakrs-mode coreml` and lists what it supports.

- `--speakrs-bin` / `SPEAKRS_BIN` — defaults to `packages/speakrs-diar/target/release/speakrs-diar`
- `--speakrs-mode` / `SPEAKRS_MODE` — `cpu`, `coreml` (default on macOS), `coreml-fast`, `cuda`, `cuda-fast`
- `--speakrs-models-dir` / `SPEAKRS_MODELS_DIR` — load models from a directory instead of
  downloading them from the public `avencera/speakrs-models` repo on first use (no HF token needed)

### `senko`

The previous default. No sidecar build, but it lives behind an extra because it pulls
torch (~2–3 GB); the core install needs no ML framework at all:

```sh
uv sync --extra senko
paraspeakrs run --diar-backend senko "$AUDIO_FILE"
```

Pick it if you would rather not build the sidecar, and note the accuracy caveat above.
The root-level preprocessing experiments (`transcribe_variants.py` and friends) have
their own extra, `--extra denoise`.

### Moving this to another Mac

`uv tool install paraspeakrs` is the supported route — it carries the diarizer binary and
fetches its own models, so there is nothing to copy by hand.

For a machine that cannot reach PyPI, `scripts/make-shipping-zip.sh` still builds a
self-contained zip of the source plus the binary; see
[scripts/SHIPPING.md](scripts/SHIPPING.md) and [docs/releases.md](docs/releases.md).

### Speaker cache and embedding dimensions

The two backends emit different embeddings (Senko 192-dim CAM++, speakrs 256-dim
WeSpeaker), which are not comparable. The speaker cache therefore files
speakers under the backend that produced them, and switching backends means
re-labeling each voice once. Labels recorded before this split are read as Senko's.

## Linux

Copy the whole block, then replace the variables at the top.

```sh
# Required inputs. Replace these with your local values.
export SHERPA_MODEL_DIR="/path/to/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
export AUDIO_FILE="/path/to/audio.wav"
export AUDIO_DIR="./test-audio"

# Choose the ASR device.
# Use cpu for normal CPU inference.
# Use cuda only with an NVIDIA GPU, CUDA-enabled PyTorch, and a CUDA-enabled sherpa-onnx wheel.
export PARAKEET_DEVICE="cpu"
export DIAR_BACKEND="speakrs"
export SENKO_DEVICE="auto"

# Install/sync the project into uv's managed .venv.
uv sync --python 3.11 --group dev

# Optional CUDA note:
# If you use PARAKEET_DEVICE=cuda, install CUDA-enabled PyTorch and sherpa-onnx
# into this uv environment before running the service.

# Run the API service in the background.
mkdir -p var
paraspeakrs serve \
  --host 127.0.0.1 \
  --port 8000 \
  --device "$PARAKEET_DEVICE" \
  --diar-backend "$DIAR_BACKEND" \
  --senko-device "$SENKO_DEVICE" \
  --asr-backend sherpa \
  --sherpa-model-dir "$SHERPA_MODEL_DIR" \
  > var/service.log 2>&1 &
SERVICE_PID=$!
sleep 3

# Submit one audio file to the service.
JOB_JSON="$(curl -sS -X POST http://127.0.0.1:8000/jobs -F "file=@${AUDIO_FILE}")"
echo "$JOB_JSON"
JOB_ID="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["job_id"])' <<< "$JOB_JSON")"

# Poll the job. Repeat this command until status is completed or failed.
# The response includes progress_step, progress_detail, and progress_percent.
curl -sS "http://127.0.0.1:8000/jobs/${JOB_ID}"

# Fetch the human-readable utterance transcript once the job is completed.
# 202 while still queued/running, 409 if the job failed.
curl -sS "http://127.0.0.1:8000/jobs/${JOB_ID}/txt"

# Stop the background service when you are done testing service mode.
kill "$SERVICE_PID"

# Run a stand-alone transcription/diarization test for one audio file.
paraspeakrs run \
  --device "$PARAKEET_DEVICE" \
  --diar-backend "$DIAR_BACKEND" \
  --senko-device "$SENKO_DEVICE" \
  --asr-backend sherpa \
  --sherpa-model-dir "$SHERPA_MODEL_DIR" \
  "$AUDIO_FILE"

# Run interactive label-snippet generation for a directory of audio files.
paraspeakrs label-dir "$AUDIO_DIR" \
  --device "$PARAKEET_DEVICE" \
  --diar-backend "$DIAR_BACKEND" \
  --senko-device "$SENKO_DEVICE" \
  --asr-backend sherpa \
  --sherpa-model-dir "$SHERPA_MODEL_DIR" \
  --label-probe-seconds 300

# Run the test suite.
uv run pytest
```

## macOS

Copy the whole block, then replace the variables at the top.

```sh
# Required inputs. Replace these with your local values.
export SHERPA_MODEL_DIR="/path/to/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
export AUDIO_FILE="/path/to/audio.wav"
export AUDIO_DIR="./test-audio"

# Choose the ASR device.
# Use coreml on Apple Silicon when sherpa-onnx includes ONNX Runtime CoreML.
# Use cpu when CoreML is unavailable or you want plain CPU inference.
export PARAKEET_DEVICE="coreml"
export DIAR_BACKEND="speakrs"
export SENKO_DEVICE="auto"

# Install/sync the project into uv's managed .venv.
uv sync --python 3.11 --group dev

# Run the API service in the background.
mkdir -p var
paraspeakrs serve \
  --host 127.0.0.1 \
  --port 8000 \
  --device "$PARAKEET_DEVICE" \
  --diar-backend "$DIAR_BACKEND" \
  --senko-device "$SENKO_DEVICE" \
  --asr-backend sherpa \
  --sherpa-model-dir "$SHERPA_MODEL_DIR" \
  > var/service.log 2>&1 &
SERVICE_PID=$!
sleep 3

# Submit one audio file to the service.
JOB_JSON="$(curl -sS -X POST http://127.0.0.1:8000/jobs -F "file=@${AUDIO_FILE}")"
echo "$JOB_JSON"
JOB_ID="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["job_id"])' <<< "$JOB_JSON")"

# Poll the job. Repeat this command until status is completed or failed.
# The response includes progress_step, progress_detail, and progress_percent.
curl -sS "http://127.0.0.1:8000/jobs/${JOB_ID}"

# Fetch the human-readable utterance transcript once the job is completed.
# 202 while still queued/running, 409 if the job failed.
curl -sS "http://127.0.0.1:8000/jobs/${JOB_ID}/txt"

# Stop the background service when you are done testing service mode.
kill "$SERVICE_PID"

# Run a stand-alone transcription/diarization test for one audio file.
paraspeakrs run \
  --device "$PARAKEET_DEVICE" \
  --diar-backend "$DIAR_BACKEND" \
  --senko-device "$SENKO_DEVICE" \
  --asr-backend sherpa \
  --sherpa-model-dir "$SHERPA_MODEL_DIR" \
  "$AUDIO_FILE"

# Run interactive label-snippet generation for a directory of audio files.
paraspeakrs label-dir "$AUDIO_DIR" \
  --device "$PARAKEET_DEVICE" \
  --diar-backend "$DIAR_BACKEND" \
  --senko-device "$SENKO_DEVICE" \
  --asr-backend sherpa \
  --sherpa-model-dir "$SHERPA_MODEL_DIR" \
  --label-probe-seconds 300

# Run the test suite.
uv run pytest
```

## Windows

Run from PowerShell. Copy the whole block, then replace the variables at the top.

```powershell
# Required inputs. Replace these with your local values.
$SherpaModelDir = "C:\path\to\sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
$AudioFile = "C:\path\to\audio.wav"
$AudioDir = ".\test-audio"

# Choose the ASR device.
# Use cpu for normal CPU inference.
# Use cuda only with an NVIDIA GPU, CUDA-enabled PyTorch, and a CUDA-enabled sherpa-onnx wheel.
$ParakeetDevice = "cpu"
$SenkoDevice = "auto"

# Install/sync the project into uv's managed .venv.
uv sync --python 3.11 --group dev

# Optional CUDA note:
# If you use $ParakeetDevice = "cuda", install CUDA-enabled PyTorch and sherpa-onnx
# into this uv environment before running the service.

# Run the API service in the background.
New-Item -ItemType Directory -Force -Path var | Out-Null
$Service = Start-Process -FilePath "uv" -ArgumentList @(
  "paraspeakrs", "serve",
  "--host", "127.0.0.1",
  "--port", "8000",
  "--device", $ParakeetDevice,
  "--senko-device", $SenkoDevice,
  "--asr-backend", "sherpa",
  "--sherpa-model-dir", $SherpaModelDir
) -PassThru -RedirectStandardOutput "var\service.log" -RedirectStandardError "var\service.err.log"
Start-Sleep -Seconds 3

# Submit one audio file to the service.
$Job = curl.exe -sS -X POST http://127.0.0.1:8000/jobs -F "file=@$AudioFile" | ConvertFrom-Json
$Job

# Poll the job. Repeat this command until status is completed or failed.
# The response includes progress_step, progress_detail, and progress_percent.
curl.exe -sS "http://127.0.0.1:8000/jobs/$($Job.job_id)"

# Fetch the human-readable utterance transcript once the job is completed.
# 202 while still queued/running, 409 if the job failed.
curl.exe -sS "http://127.0.0.1:8000/jobs/$($Job.job_id)/txt"

# Stop the background service when you are done testing service mode.
Stop-Process -Id $Service.Id

# Run a stand-alone transcription/diarization test for one audio file.
paraspeakrs run `
  --device $ParakeetDevice `
  --senko-device $SenkoDevice `
  --asr-backend sherpa `
  --sherpa-model-dir $SherpaModelDir `
  $AudioFile

# Run interactive label-snippet generation for a directory of audio files.
paraspeakrs label-dir $AudioDir `
  --device $ParakeetDevice `
  --senko-device $SenkoDevice `
  --asr-backend sherpa `
  --sherpa-model-dir $SherpaModelDir `
  --label-probe-seconds 300

# Run the test suite.
uv run pytest
```

## MCP server

The same pipeline is exposed as a local [MCP](https://modelcontextprotocol.io)
server (stdio) so an LLM agent can drive transcription, speaker labeling, and
transcript retrieval directly. It runs the pipeline in-process — no HTTP service
required — and reads audio from local file paths.

Install the extra and run it:

```sh
uv sync --python 3.11 --extra mcp
export PARAKEET_WORKSPACE_DIR="./var"   # job artifacts + speaker cache live here
# PARAKEET_SHERPA_MODEL_DIR defaults to models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3
# PARAKEET_SHERPA_PRECISION=fp32|fp16|int8 picks the weights without being asked

# stdio transport (default) — for a local client that launches the server itself.
paraspeakrs mcp

# streamable-http transport — for remote/network clients. Binds 127.0.0.1:8000 by default.
paraspeakrs mcp --transport streamable-http --host 0.0.0.0 --port 8000
```

### Transports

- `stdio` (default) — the client spawns `paraspeakrs mcp` and talks over stdin/stdout. Best for a local agent.
- `streamable-http` — the server listens on `--host`/`--port` (default `127.0.0.1:8000`); the endpoint is `http://<host>:<port>/mcp`. Use for remote clients or to share one running server.
- `sse` — legacy Server-Sent Events transport, also available via `--transport sse`.

Over HTTP the pipeline still runs in-process on the server host, and `audio_path` / `output_path` are resolved on the **server's** filesystem, not the client's.

Configuration is the same env vars as the service (`PARAKEET_DEVICE`,
`PARAKEET_DIAR_BACKEND`, `SENKO_DEVICE`, `SPEAKRS_BIN`, `SPEAKRS_MODE`,
`SPEAKRS_MODELS_DIR`, `PARAKEET_ASR_BACKEND`, `PARAKEET_SHERPA_MODEL_DIR`,
`PARAKEET_WORKSPACE_DIR`, …). Note that the default `speakrs` backend resolves
`SPEAKRS_BIN` relative to the server's working directory, so set it to an
absolute path when an MCP client launches the server. Example client config for the default **stdio**
transport (Claude Desktop / any MCP client launches the server itself):

```json
{
  "mcpServers": {
    "parakeet-diarize": {
      "command": "uv",
      "args": ["mcp"],
      "env": {
        "PARAKEET_SHERPA_MODEL_DIR": "/path/to/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8",
        "PARAKEET_DEVICE": "coreml",
        "PARAKEET_WORKSPACE_DIR": "/abs/path/to/var"
      }
    }
  }
}
```

For an already-running **streamable-http** server, point the client at the URL
instead of spawning a command:

```json
{
  "mcpServers": {
    "parakeet-diarize": {
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

### Tools

| Tool | Description |
| --- | --- |
| `transcribe(audio_path)` | Run diarization + ASR on a local file. Returns `job_id`, duration, `num_speakers`, and the speaker roster. Speakers with `assigned_label: null` are unnamed; a `suggested_label` is the voice cache's guess and only reaches the transcript once confirmed with `label_speaker`. **Does not return the transcript text.** |
| `list_speakers(job_id)` | The job's speakers with their assigned names, suggestions and talk time. |
| `get_speaker_audio(job_id, speaker_id)` | Export a short (up to 10 s) sample clip for one speaker and return its wav path. Play it for the user to identify the speaker. |
| `label_speaker(job_id, speaker_id, name)` | Assign a name to a speaker and store its embedding so future transcriptions recognize the same voice automatically. |
| `get_note(job_id)` | The free-text note recorded about this meeting — what it was about, in the user's own words. `""` when nothing has been written. |
| `set_note(job_id, note)` | Record what the meeting was about. Replaces rather than appends, so read the note first if you mean to add to one the user wrote; an empty note clears it. |
| `get_transcript(job_id, output_path)` | Render the transcript and **write it to `output_path`**, returning only the path and a count of spoken lines. It opens with YAML frontmatter (title, date, named speakers, and the note when there is one); speakers and lines removed in the TUI are left out. |

### Speaker cache

Naming a speaker persists their centroid embedding in
`$PARAKEET_WORKSPACE_DIR/speaker-cache.json` under that name. On every
`transcribe` call, new speakers are matched against that cache by cosine
similarity, so once you name someone they are *suggested* in later meetings
without relabeling — confirm the suggestion and they are named.

A name keeps the embeddings it was learned from rather than only their running
average, so the members of a voice can be listened to individually and pulled
back out one at a time. Each additional recording of a voice moves its centroid
by `1/(n+1)`, so an established name is not yanked onto whoever was added last.

### Workflow

- **No labeling needed:** `transcribe` → `list_speakers` → confirm each
  `suggested_label` with `label_speaker` → `get_transcript(output_path)`.
  A suggestion that is never confirmed leaves the raw `SPEAKER_xx` ID in the
  transcript: a name in a transcript is a claim about who said something, so it
  only goes in once a person has agreed to it.
- **Labeling needed:** `transcribe` → `list_speakers` shows some
  `assigned_label: null` → for each unknown speaker: `get_speaker_audio` →
  play the clip for the user → user identifies them → `label_speaker` →
  `get_transcript`.

### Retrieving the transcript

`get_transcript` **writes the transcript to the `output_path` file you provide
and returns only that path plus a line/byte count — it never returns the
transcript body.** This is deliberate: meeting transcripts are large and would
otherwise flood the model's context window. The file contents are the same
`[HH:MM:SS] NAME: text` format described under
[Output formats](#output-formats).

**Consumer guidance — pipe transcript retrieval to a file, not into context.**
Any client/agent calling `get_transcript` (and the wav path from
`get_speaker_audio`) must read or stream the file from disk, or hand the path to
the user, rather than loading the body back through the model. Do not echo the
transcript into the conversation.

Note that `output_path` (and `audio_path` on `transcribe`) is resolved on the
**server's** filesystem. With `stdio` transport the server is local, so the path
is on your own machine. With `streamable-http` to a remote server, the file
lands on the server host — co-locate the client and server, mount a shared
volume, or fetch the file out-of-band; the transcript body is never sent over
the MCP channel.

## Terminal UI

A Textual terminal UI drives the same in-process pipeline as the MCP server — no
browser, no HTTP service. Browse for recordings, queue them up, listen to each
detected speaker, name them, and write the transcript.

```sh
uv sync --python 3.11 --extra tui
export PARAKEET_WORKSPACE_DIR="./var"   # job artifacts + speaker cache live here
# PARAKEET_SHERPA_MODEL_DIR defaults to models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3
# PARAKEET_SHERPA_PRECISION=fp32|fp16|int8 picks the weights without being asked
# Where the file browser starts the very first time; after that it reopens
# wherever you left it.
export PARAKEET_RECORDINGS_DIR="$HOME/Library/Application Support/MacParakeet/meeting-recordings"

paraspeakrs tui
```

The TUI reads configuration from the environment only — it takes no command-line
flags, so `SPEAKRS_BIN` and friends must be exported rather than passed.

### One screen: lists on the left, the selected recording on the right

```
╭─ type to filter ────────────────────╮╭─ weekly-09-16.wav · 2:41 · 3 speakers ──────────────────╮
│📂 2026-09                           ││  Speaker       Name     Suggested   Conf  Talk (s)      │
│├── 📁 archive                       ││  L_SPEAKER_00  Albert   —           —     412.0         │
│├── 📄 standup-09-16.wav  ✓          ││  L_SPEAKER_01  —        Michiel     0.81  233.1         │
│├── 📄 weekly-09-16.wav   ✓          ││  R_SPEAKER_01  —        (no match)  0.41   35.4         │
│└── 📄 retro-09-17.m4a    …          │╰──── 2 unnamed · 1 suggestion(s) pending      ↵ accept ──╯
│                                     │╭─ transcript ────────────────────────────────────────────╮
╰─────────────────────────────────────╯│ [00:00:04] Albert: so where did we land on migration?   │
╭─ processed · 7 ─────────────────────╮│ [00:00:07] L_SPEAKER_01: we pushed it to Q3             │
│ Recording          Spk  Unnamed Note││ ✕ [00:00:09] R_SPEAKER_01: mum, can I have juice        │
│ weekly-09-16.wav   3    2       ✎   │╰───────────────────────────────── 212 lines · 1 removed ─╯
│ standup-09-16.wav  3    —           │╭─ note ──────────────────────────────────────────────────╮
╰─────────────────────────────────────╯│ weekly-09-16 · 2026-09-16 · Albert                      │
╭─ queue · 1 waiting ─────────────────╮│ Sprint planning. Postponed the migration until Q3.      │
│ retro-09-17.m4a    running          ││                                                         │
╰─────────────────────────────────────╯╰─────────────────────────────────────────────────────────╯
```

The three lists on the left are all ways of naming one recording — one you are
looking for, one already transcribed, one waiting its turn — and the two panels
on the right are always about whichever of them the cursor is on. Moving down a
list re-aims the detail; it never travels to another screen, so the speaker
table is never something you have to find your way back from.

`Tab` walks the six panels in reading order — browse, processed, queue,
speakers, transcript, note — and `Shift+Tab` walks back. The focused panel is the one with
the highlighted border.

`Enter` on a recording queues it, and items are transcribed one at a time in the
background while you keep browsing. For a stereo recording, `Enter` first asks whether
either channel is a single speaker — someone alone on their own mic. Every
speaker found on a channel you vouch for is merged into one, so a laugh or a
lowered voice does not come back as a second person to name; the other channel
is diarized as usual. Diarization still runs on that channel to find where the
speech is. `Esc` backs out without queueing. `Enter` on a recording that already has a
job hands focus to its speaker table instead — that file's, not a list you then
have to find it in again. `✓` marks a recording that already has a job, `…` one
that is queued. A directory shows how many of the recordings directly inside it
are done — `3/5`, or `5/5 ✓` once all are — and every entry is prefixed with its
creation date. The queue survives a restart, and a run interrupted by a crash
goes back to `waiting`.

Keys in the tree: `↑`/`↓` move, `→` open a directory in place, `←` close it,
`Enter` on a directory makes it the new root, `Backspace` go up to the parent,
`^s` toggle between sorting by name and newest first.
The directory you end up in is remembered, so the next launch starts there.

Typing any other character fuzzy-filters the directory the tree is rooted at —
`stwav` finds `standup-2026-03-04.wav` — and jumps the cursor to the best match.
The filter applies to that level only; open a subdirectory and you see all of it.
`Backspace` deletes a character while a filter is active (and only goes up to the
parent once it is empty), `Esc` clears it.

Because the tree turns every letter into filter input, the screen's own keys are
also bound to `ctrl`: `^v` voices, `^g` go to a path, `^x` drop a queued item,
`^d` delete a job, `^r` refresh. The bare letters work wherever the tree is not
listening — on the queue, for instance.

### Meeting notes

The note panel is what the recording was *about*, in your own words. Diarization
answers who spoke and the transcript answers what was said; neither answers why
the recording mattered, and three weeks later that is the only thing that picks
the right one out of a list of thirty. `Tab` to the note, type, `Tab` away — it
saves when focus leaves, and on quit, so there is no key to remember. A `✎` in
the processed list marks the recordings that have one.

Above what you typed, the panel shows which meeting this is — the recording's
name, the day it was recorded, and the speakers you have named — so you never
have to type it. That line follows the names as you assign them and is never
written into your note.

The note also rides along into the written transcript as YAML frontmatter (see
[Output formats](#output-formats)), because a transcript is read weeks later out
of context: who said what is in the body, and why the meeting happened is only
ever in the note.

Notes are stored as `note.json` beside the job's `artifacts.json`, not inside it:
that file holds the diarization and a float vector per speaker, and rewriting all
of it to record a typed sentence would put the expensive, irreplaceable half of a
job at risk for the cheap half. Clearing a note deletes the file. Pruning a job's
working audio keeps the note — it is the one thing in a job directory that cannot
be rebuilt from the original recording.

### Naming speakers

The speaker panel on the right of the home screen, and the whole of the screen
reached from the job list:

```
Speaker        Name        Suggested       Conf   Talk
L_SPEAKER_00   Albert      –               –      4:12
L_SPEAKER_01   –           Michiel         0.81   3:40
R_SPEAKER_01   –           (no match)      0.41   0:35
```

The two columns are never blended. **Name** is a decision — typed or accepted by
you — and is the only thing that ever reaches a transcript. **Suggested** is the
voice cache's nearest match, offered and never applied on its own; a speaker you
leave alone appears in the transcript as their raw `SPEAKER_xx` ID.

Keys: `p` play the selected speaker's sample, `s` stop, `n` name them, `Enter`
accept the suggestion on that row, `A` accept every suggestion, `space` select a
row, `m` merge the selected rows into one person, `u` clear an assigned name,
`d` remove the speaker from the transcript (again to put them back),
`t` write the transcript. These only apply while the speaker panel holds focus,
which leaves the same letters free on the rest of the screen; on the standalone
screen reached from the job list, `Esc` goes back. Playback uses `afplay` on
macOS, `ffplay` otherwise.

Merging is the ordinary case for stereo: one person picked up on both channels
arrives as `L_SPEAKER_00` and `R_SPEAKER_00`, and merging names both and teaches
the cache one voice learned from both.

Typing a name that already exists, for a voice that does not sound like it, asks
first — fold the two together, or name this job only and leave the stored voice
alone. Two different people really can share a first name, and one averaged
centroid then matches neither of them.

### Removing what was not part of the meeting

A recording picks up whatever the microphone heard, and that is not always the
meeting — the kids talking through a call while your call mic was muted, but the
recorder was not. Two keys take it out again:

- `d` on a speaker in the speaker panel removes every line of theirs. The row is
  struck through, reads `removed`, and no longer counts as unnamed.
- In the transcript panel below it, `d` removes just the line under the cursor
  — for the stray remark diarization filed under someone who was in the meeting.

The transcript panel shows the whole transcript with the speaker selected above
it bright and everyone else dimmed, because a stray line is only recognisable in
context. Moving through the speaker table moves it to that speaker's first line.
Keys: `↑`/`↓` or `j`/`k` move a line, `h`/`l` jump to the selected speaker's
previous/next line, `d` removes or restores a line.

Nothing is deleted. A removal is a mark on the job, shown struck through with a
`✕`, and `d` again undoes it; only the written transcript — from the TUI or from
the MCP server's `get_transcript` — leaves it out.

### Voices (`^v`)

```
▾ Michiel                                    2 recordings
    standup-09-16.wav   L_SPEAKER_01   2026-09-16
    weekly-09-16.wav    R_SPEAKER_00   2026-09-17
  Albert                                     5 recordings
```

The speaker cache, opened up. Each name lists the recordings it was learned from;
`p` plays one of them, `u` unfolds it — pulling that recording back out of the
voice and leaving its speaker unnamed again — `m` folds one name into another
(for the `Michiel`/`michiel` case), and `d` forgets a voice entirely.

Unfolding is exact because the cache stores its contributing members rather than
only a running average. Voices learned before this layout still match and can
still be folded into, but have no recording to play and cannot be unfolded.

### Processed recordings

The `processed` panel is every job on disk, shared with the MCP server
(`$PARAKEET_WORKSPACE_DIR/mcp-jobs/`) — with its speaker count, how many of them
are still unnamed, and a `✎` when it has a note. It is empty until something is
transcribed through the TUI or MCP server; `paraspeakrs run` prints its result and
keeps no artifacts, so it contributes no jobs.

`Enter` hands focus to that recording's speaker table. `^d` deletes a job, after
asking — the transcript, its names and its note go with it, and the voices it
taught are kept, because those are how every future recording gets recognized.

An item leaves the `queue` as soon as it finishes, rather than sitting there as
`done`: it has become a job and is listed as one, and showing it in both places
made one recording look like two. Failures stay in the queue, where the error is.

There is no separate jobs screen any more — it listed the same jobs this panel
does, one screen away from the speaker table you wanted them for.

## Jobs and reuse

Every transcription — CLI, TUI or MCP — is stored as a job under
`$PARAKEET_WORKSPACE_DIR/mcp-jobs/<job_id>/`, holding the diarization, the
per-speaker embeddings and the result. All three front ends read the same store,
so a job started on the CLI can be labeled in the TUI.

Jobs are keyed by a SHA-256 of the source audio, so transcribing the same
recording twice returns the existing job instead of re-running ASR and
diarization. Matching is on content, so a recording that was renamed, moved or
re-copied still matches. `--force` re-transcribes anyway.

```sh
paraspeakrs run meeting.m4a          # full run, stored as a job
paraspeakrs run meeting.m4a          # returns immediately, reuses the job
paraspeakrs run --txt meeting.m4a    # formatted transcript instead of JSON
paraspeakrs run --force meeting.m4a  # ignore the existing job
```

### What a job keeps

A finished job keeps its `artifacts.json` — transcript, diarization, speaker
embeddings — and nothing else. The copy of the source recording, both normalized
renders and the per-chunk WAVs are deleted once the job is saved: they are
roughly 99% of its size and all rebuildable from the original recording, which is
never touched.

Measured on a 2-minute recording: **15 MB → 72 KB** per job.

Speaker samples are cut on demand. If a job has been pruned, the audio is rebuilt
from `source_path`, the sample is cached under `snippets/`, and the rebuilt render
is deleted again. Move or delete the original recording and sample playback for
that job reports so plainly — the transcript itself is unaffected.

### Where the workspace lives

By default `$XDG_DATA_HOME/fast-speaker-aware-meeting-transcriber`, i.e.
`~/.local/share/fast-speaker-aware-meeting-transcriber`. It is machine-global, so
jobs and learned speakers are the same no matter which directory you launch from.
`PARAKEET_WORKSPACE_DIR` overrides it and is resolved to an absolute path.

It sits under the data directory rather than a cache directory deliberately:
`speaker-cache.json` holds the named voice prints, `ui-state.json` the browser's
last directory and the processing queue, and each job holds its
transcript, and neither can be regenerated if something sweeps the cache.

CLI flags default to the environment, so `PARAKEET_*` applies to
`paraspeakrs run` exactly as it does to `paraspeakrs tui` and `paraspeakrs mcp`, and an
explicit flag still wins.

Earlier versions used `./var` relative to the working directory. To carry that
history over:

```sh
DEST=~/.local/share/fast-speaker-aware-meeting-transcriber
mkdir -p "$DEST"
mv var/mcp-jobs "$DEST"/
mv var/speaker-cache.json "$DEST"/     # the irreplaceable part
```

The TUI says so explicitly when the new workspace is empty but a `./var` with
jobs is present.

## Output formats

The service exposes two representations of the same job:

- `GET /jobs/{job_id}` — full JSON with segments, words (when available), speakers, embeddings, and warnings.
- `GET /jobs/{job_id}/txt` — plain-text utterance transcript, one line per utterance:

  ```text
  [HH:MM:SS] NAME: utterance text
  ```

  A transcript written from the TUI or MCP server opens with YAML frontmatter
  saying which meeting it was — the recording's name, the day it was recorded, the
  speakers you named — and the note, when there is one:

  ```text
  ---
  title: "weekly-09-16"
  date: "2026-09-16"
  speakers: ["Albert", "Michiel"]
  note: |
    Sprint planning. Postponed the migration until Q3.
  ---

  [00:00:04] Albert: so where did we land on the migration
  ```

  The note is always a block scalar, even when it is one line — a note is prose,
  and prose that happens to contain a colon would otherwise make the header
  invalid YAML; the other fields are quoted for the same reason. Speakers and
  lines removed in the TUI are left out. The line count reported when a
  transcript is written counts spoken lines only, so it does not move when
  somebody edits the note.

  `NAME` is the name assigned to that speaker if there is one, otherwise the raw `SPEAKER_xx` / `L_SPEAKER_xx` / `R_SPEAKER_xx` ID. A voice-cache match on its own is a suggestion and does not put a name here. Lines are split on sentence enders (`.`, `?`, `!`) and on relative silence within a speaker's run (rolling 90th-percentile gap, with a 0.6 s floor; the silence history resets whenever the speaker changes). When the active ASR backend does not provide word-level timestamps, sentence-splits fall back to character-proportional timestamps within each segment.

  Response status:
  - `200 text/plain` once the job is `completed`.
  - `202` with a one-line comment while the job is `queued` or `running` (poll the same URL).
  - `409` when the job exists but `failed`.
  - `404` when the job ID is unknown.

## Stereo handling

If the input audio has two channels the pipeline splits it into two mono streams,
runs Senko on each independently, and merges the segments back into a single
result. Speaker IDs from the left channel are prefixed `L_` and from the right
channel `R_` so they never collide. Mono input takes a single Senko pass and
keeps the bare `SPEAKER_xx` IDs Senko produces.

## Senko threading on macOS

Senko's Numba/OpenMP stack segfaults on macOS unless these are set:

```sh
KMP_DUPLICATE_LIB_OK=TRUE NUMBA_THREADING_LAYER=workqueue NUMBA_NUM_THREADS=1
```

`build_pipeline` sets them (via `os.environ.setdefault`) on macOS, so the
service, CLI, MCP server and terminal UI need no extra setup. The standalone
`packages/senko-diarize-test/diarize.py` helper does not go through
`build_pipeline`, so run it with the variables set explicitly:

```sh
KMP_DUPLICATE_LIB_OK=TRUE NUMBA_THREADING_LAYER=workqueue NUMBA_NUM_THREADS=1 \
  uv run python ./packages/senko-diarize-test/diarize.py
```

## Current Scope

This is milestone 1. speakrs diarization (including stereo channel-split) and its
per-speaker centroid embeddings are wired in, with Senko kept as an optional
backend. Article-inspired multi-level preprocessing is intentionally left for
later milestones.

## Acknowledgements

This service is mostly glue. The parts that do the actual work belong to other
people:

**Diarization**

- [speakrs](https://github.com/avencera/speakrs) by Praveen Perera (Apache-2.0) —
  the default backend. A Rust implementation of the pyannote `community-1`
  pipeline: segmentation, powerset decode, overlap-add aggregation, binarization,
  embedding, PLDA and VBx clustering, with no Python in the library path.
- [pyannote.audio](https://github.com/pyannote/pyannote-audio) by Hervé Bredin and
  contributors — the pipeline speakrs ports, and the `segmentation-3.0` model it
  runs.
- [WeSpeaker](https://github.com/wenet-e2e/wespeaker) — the speaker embedding
  model behind speakrs' 256-dim centroids, which is what makes the cross-meeting
  speaker cache possible.
- [Senko](https://github.com/narcotic-sh/senko) by narcotic-sh — the previous
  default, still available via `--diar-backend senko`, using CAM++ embeddings from
  [3D-Speaker](https://github.com/modelscope/3D-Speaker).

**Speech recognition**

- [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) by the k2-fsa team
  (Apache-2.0) — the ASR runtime.
- [NVIDIA NeMo Parakeet TDT 0.6B v3](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3)
  (CC-BY-4.0) — the ASR model, covering 25 European languages including Dutch.

**Infrastructure**

- [ONNX Runtime](https://github.com/microsoft/onnxruntime) and the
  [`ort`](https://github.com/pykeio/ort) crate — inference for both stacks.
- [FFmpeg](https://ffmpeg.org/) — every audio normalization, channel split and
  chunk export.
- [OpenBLAS](https://github.com/OpenMathLib/OpenBLAS) — the linear algebra behind
  speakrs' PLDA and VBx clustering.
- [FastAPI](https://github.com/fastapi/fastapi), [Textual](https://github.com/Textualize/textual),
  and the [Model Context Protocol](https://github.com/modelcontextprotocol) SDK —
  the service, terminal UI and MCP surfaces.
- [uv](https://github.com/astral-sh/uv) — environment and dependency management.

Licences differ per project; check each one before redistributing. The Parakeet
model's CC-BY-4.0 in particular requires attribution in anything built on it.
