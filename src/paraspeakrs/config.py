from __future__ import annotations

import os
import shutil
import sys
import sysconfig
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Device = Literal["cpu", "cuda", "coreml"]
SenkoDevice = Literal["auto", "cpu", "cuda", "coreml"]
AsrBackend = Literal["sherpa", "openai", "mock"]
DiarBackend = Literal["senko", "speakrs"]
SpeakrsMode = Literal["cpu", "coreml", "coreml-fast", "cuda", "cuda-fast"]
SherpaPrecision = Literal["fp32", "fp16", "int8"]

# FP32 is the default despite being four times the download. On non-English speech the
# INT8 weights do not merely blur words, they drop whole clauses: measured over a 42-minute
# Dutch meeting, INT8 emitted 3546 words against FP32's 4715 (-25%), losing content in
# every single chunk. It is also *slower* -- ONNX Runtime has no fast INT8 transducer
# kernel on arm64 and dequantizes per operator, so FP32 decodes the same audio in 42 s
# against INT8's 65 s. INT8 remains selectable for tight disk or memory budgets.
SHERPA_MODEL_NAME = "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3"
SHERPA_INT8_MODEL_NAME = "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
SHERPA_FP16_MODEL_NAME = "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-fp16"
# Only reachable from a source checkout; an installed tool gets the binary from the
# paraspeakrs-speakrs wheel instead. Kept last in the search order so the dev workflow
# keeps working without an env var.
CHECKOUT_SPEAKRS_BIN = Path("packages/speakrs-diar/target/release/speakrs-diar")
SPEAKRS_BIN_NAME = "speakrs-diar"

APP_DIR_NAME = "paraspeakrs"
# The data directory's name up to 0.4.x, from before the project was renamed.
OLD_APP_DIR_NAME = "fast-speaker-aware-meeting-transcriber"
# Where the workspace used to live, relative to wherever you happened to launch from.
LEGACY_WORKSPACE_DIR = Path("var")


def default_workspace_dir() -> Path:
    """Machine-global home for jobs and the speaker cache.

    Under XDG_DATA_HOME rather than a cache directory on purpose: this holds
    hand-labeled speaker identities and the transcripts themselves, neither of
    which can be regenerated if something sweeps the cache.
    """
    return _data_root() / APP_DIR_NAME


def _data_root() -> Path:
    base = os.getenv("XDG_DATA_HOME")
    return Path(base).expanduser() if base else Path.home() / ".local" / "share"


def migrate_workspace_dir() -> Path:
    """Move a pre-0.5.0 data directory to its new name and return the one to use.

    It holds transcripts and hand-named speakers, which cannot be regenerated, so
    an upgrade must bring them along. When both directories exist nothing is
    merged; when the move fails the old directory stays in use rather than
    opening an empty workspace that would look like lost data. Messages go to
    stderr because stdout carries the MCP stdio protocol.
    """
    old = _data_root() / OLD_APP_DIR_NAME
    new = default_workspace_dir()
    if not old.is_dir():
        return new
    if new.exists():
        print(f"note: {old} is no longer used; the data in {new} is", file=sys.stderr)
        return new
    try:
        old.rename(new)
    except OSError as exc:
        # Another process starting at the same moment may have moved it first.
        if new.is_dir() and not old.exists():
            return new
        print(f"could not move {old} to {new} ({exc}); still using {old}", file=sys.stderr)
        return old
    print(f"moved {old} to {new}", file=sys.stderr)
    return new


def default_sherpa_model_dir(precision: SherpaPrecision = "fp32", workspace_dir: Path | None = None) -> Path:
    """Where the ASR model lives for an installed tool.

    Absolute, under the workspace, for the same reason the workspace itself is:
    a relative default silently resolves against whatever directory the process
    happened to start in, which breaks every launcher that is not a shell in the
    checkout (MCP clients, the TUI, a `uv tool` install).
    """
    return (workspace_dir or default_workspace_dir()) / "models" / sherpa_model_name(precision)


def sherpa_model_name(precision: SherpaPrecision = "fp32") -> str:
    return {
        "fp32": SHERPA_MODEL_NAME,
        "fp16": SHERPA_FP16_MODEL_NAME,
        "int8": SHERPA_INT8_MODEL_NAME,
    }[precision]


def default_speakrs_bin() -> Path:
    """Locate the speakrs-diar sidecar without assuming a working directory.

    The paraspeakrs-speakrs wheel installs the binary next to the interpreter, so
    that is the first place to look for an installed tool; `which` covers a
    hand-placed copy; the checkout path keeps `cargo build --release` usable during
    development. Returns the checkout path when nothing exists so the error message
    in validate_runtime() still has something concrete to name.
    """
    # sysconfig, not Path(sys.executable).parent: in a venv the interpreter is a
    # symlink into the real Python installation, and resolving it lands outside the
    # venv, where the wheel's binary is not.
    for scripts in (sysconfig.get_path("scripts"), str(Path(sys.executable).parent)):
        bundled = Path(scripts) / SPEAKRS_BIN_NAME
        if bundled.is_file():
            return bundled
    found = shutil.which(SPEAKRS_BIN_NAME)
    if found:
        return Path(found)
    return CHECKOUT_SPEAKRS_BIN


def legacy_jobs_dir() -> Path | None:
    """A pre-XDG ``./var/mcp-jobs`` holding jobs, if one is sitting in the cwd.

    Moving the default would otherwise make an existing history silently
    invisible - the very failure the absolute-path fix was about.
    """
    candidate = LEGACY_WORKSPACE_DIR / "mcp-jobs"
    try:
        if candidate.is_dir() and any(candidate.iterdir()):
            return candidate.resolve()
    except OSError:
        return None
    return None


@dataclass(frozen=True)
class Settings:
    device: Device = "cpu"
    workspace_dir: Path = field(default_factory=default_workspace_dir)
    diar_backend: DiarBackend = "speakrs"
    senko_device: SenkoDevice = "auto"
    senko_warmup: bool = True
    speakrs_bin: Path = field(default_factory=default_speakrs_bin)
    speakrs_models_dir: Path | None = None
    speakrs_mode: SpeakrsMode = "cpu"
    asr_backend: AsrBackend = "sherpa"
    sherpa_model_dir: Path | None = field(default_factory=default_sherpa_model_dir)
    sherpa_precision: SherpaPrecision = "fp32"
    # True when the user actually named a precision, which suppresses the first-run
    # question -- an answer already given must not be asked for again.
    precision_is_explicit: bool = False
    sherpa_num_threads: int = 4
    openai_base_url: str | None = None
    openai_api_key: str = "sk-no-key-required"
    openai_model: str = "parakeet-tdt-0.6b-v3"
    chunk_target_seconds: float = 90.0
    chunk_overlap_seconds: float = 2.0

    def __post_init__(self) -> None:
        # Anchor the workspace to an absolute path. Left relative, the job store
        # and speaker cache move with the working directory, so launching from
        # somewhere else silently presents an empty history.
        object.__setattr__(self, "workspace_dir", Path(self.workspace_dir).expanduser().resolve())

    @classmethod
    def from_env(cls) -> "Settings":
        device = _literal("PARAKEET_DEVICE", os.getenv("PARAKEET_DEVICE", "cpu"), ("cpu", "cuda", "coreml"))
        backend = _literal("PARAKEET_ASR_BACKEND", os.getenv("PARAKEET_ASR_BACKEND", "sherpa"), ("sherpa", "openai", "mock"))
        senko_device = _literal(
            "SENKO_DEVICE",
            os.getenv("SENKO_DEVICE", "auto"),
            ("auto", "cpu", "cuda", "coreml"),
        )
        diar_backend = _literal(
            "PARAKEET_DIAR_BACKEND",
            os.getenv("PARAKEET_DIAR_BACKEND", "speakrs"),
            ("senko", "speakrs"),
        )
        speakrs_mode = _literal(
            "SPEAKRS_MODE",
            os.getenv("SPEAKRS_MODE", default_speakrs_mode()),
            ("cpu", "coreml", "coreml-fast", "cuda", "cuda-fast"),
        )
        speakrs_models_dir = os.getenv("SPEAKRS_MODELS_DIR")
        speakrs_bin = os.getenv("SPEAKRS_BIN")
        model_dir = os.getenv("PARAKEET_SHERPA_MODEL_DIR")
        precision = _literal(
            "PARAKEET_SHERPA_PRECISION",
            os.getenv("PARAKEET_SHERPA_PRECISION", "fp32"),
            ("fp32", "fp16", "int8"),
        )
        workspace_dir = (
            Path(os.environ["PARAKEET_WORKSPACE_DIR"]).expanduser().resolve()
            if os.getenv("PARAKEET_WORKSPACE_DIR")
            else migrate_workspace_dir()
        )
        return cls(
            device=device,
            workspace_dir=workspace_dir,
            diar_backend=diar_backend,
            senko_device=senko_device,
            senko_warmup=os.getenv("SENKO_WARMUP", "1") not in ("0", "false", "False"),
            speakrs_bin=Path(speakrs_bin) if speakrs_bin else default_speakrs_bin(),
            speakrs_models_dir=Path(speakrs_models_dir) if speakrs_models_dir else None,
            speakrs_mode=speakrs_mode,
            asr_backend=backend,
            sherpa_model_dir=Path(model_dir).expanduser()
            if model_dir
            else default_sherpa_model_dir(precision, workspace_dir),
            sherpa_precision=precision,
            precision_is_explicit=bool(os.getenv("PARAKEET_SHERPA_PRECISION")),
            sherpa_num_threads=int(os.getenv("PARAKEET_SHERPA_THREADS", "4")),
            openai_base_url=os.getenv("PARAKEET_OPENAI_BASE_URL"),
            openai_api_key=os.getenv("PARAKEET_OPENAI_API_KEY", "sk-no-key-required"),
            openai_model=os.getenv("PARAKEET_OPENAI_MODEL", cls.openai_model),
        )

    def validate_runtime(self) -> None:
        if self.device not in ("cpu", "cuda", "coreml"):
            raise ValueError("device must be 'cpu', 'cuda', or 'coreml'")
        if self.asr_backend == "sherpa":
            if self.sherpa_model_dir is None:
                raise ValueError("sherpa_model_dir is required for sherpa ASR")
            if not self.sherpa_model_dir.is_dir():
                raise ValueError(
                    f"sherpa model dir not found: {self.sherpa_model_dir}\n"
                    "Fetch it with:  paraspeakrs fetch-models\n"
                    "Offline? See 'Running without model downloads' in the README "
                    "(PARAKEET_MODEL_URL, PARAKEET_SHERPA_MODEL_DIR, "
                    "or PARAKEET_ASR_BACKEND=openai)."
                )
        if self.asr_backend == "openai" and not self.openai_base_url:
            raise ValueError("openai_base_url is required for OpenAI-compatible ASR")
        if self.diar_backend == "speakrs":
            if not self.speakrs_bin.is_file():
                raise ValueError(
                    f"speakrs-diar binary not found: {self.speakrs_bin}\n"
                    "The paraspeakrs-speakrs wheel ships this binary, but only for "
                    "macOS arm64. Elsewhere, either:\n"
                    "  - use the other backend:  --diar-backend senko  "
                    "(needs the 'senko' extra)\n"
                    "  - build it from a checkout and point SPEAKRS_BIN at the result:\n"
                    "      cd packages/speakrs-diar && cargo build --release --features coreml"
                )
            if not os.access(self.speakrs_bin, os.X_OK):
                raise ValueError(f"speakrs-diar binary is not executable: {self.speakrs_bin}")
            if self.speakrs_models_dir is not None and not self.speakrs_models_dir.is_dir():
                raise ValueError(f"speakrs models dir not found: {self.speakrs_models_dir}")


def default_speakrs_mode() -> str:
    """CoreML is the fastest speakrs backend on Apple Silicon and the one the
    documented build enables; everywhere else fall back to portable CPU."""
    return "coreml" if sys.platform == "darwin" else "cpu"


def _literal(name: str, value: str, allowed: tuple[str, ...]):
    if value not in allowed:
        joined = ", ".join(allowed)
        raise ValueError(f"{name} must be one of: {joined}")
    return value
