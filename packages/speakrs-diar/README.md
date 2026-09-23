# paraspeakrs-speakrs

The `speakrs-diar` sidecar binary for [paraspeakrs][repo], packaged as a platform wheel.

This distribution contains no Python code. It exists so that `uv tool install paraspeakrs`
can provide a working default diarization backend without a Rust toolchain, Homebrew, or a
source build. The binary reads a WAV file and emits diarization segments as JSON; it is
invoked by `paraspeakrs` as a subprocess.

Built for macOS arm64 with `--features coreml,blas-static`, so it carries its own OpenBLAS
and depends only on macOS system frameworks.

On its first run it downloads the speakrs diarization models (~315 MB) from the public
`avencera/speakrs-models` repository. No Hugging Face token is required. Set
`SPEAKRS_MODELS_DIR` to use a local copy instead.

Install `paraspeakrs` rather than this package directly.

[repo]: https://github.com/alber70g/paraspeakrs
