# paraspeakrs-speakrs

The native sidecar binaries for [paraspeakrs][repo], packaged as platform wheels.

This distribution contains no Python code. It exists so that `uv tool install paraspeakrs`
works without a Rust toolchain or a source build. `paraspeakrs` runs each binary as a
subprocess:

| Binary | Does | Wheels |
|---|---|---|
| `speakrs-diar` | reads a WAV file and prints diarization segments and speaker embeddings as JSON | macOS arm64, Linux x86_64, Windows x64 |
| `paraspeakrs-record` | records the microphone and the system audio as two time-aligned tracks, for `paraspeakrs record` | Linux x86_64, Windows x64 |

`speakrs-diar` is built with speakrs' native CPU backend everywhere, plus CoreML on macOS
(macOS 15 or later) and CUDA on Linux. It links no BLAS, no ONNX Runtime and no OpenSSL. On
its first run it downloads the speakrs models for the chosen mode from the public
`avencera/speakrs-models` repository; no Hugging Face token is required. Set
`SPEAKRS_MODELS_DIR` to use a local copy instead.

`paraspeakrs-record` needs PulseAudio or PipeWire (with `pipewire-pulse`, the default on
current desktops) on Linux, and links the system's ALSA library (`libasound.so.2`).

Install `paraspeakrs` rather than this package directly.

[repo]: https://github.com/alber70g/paraspeakrs
