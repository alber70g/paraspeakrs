"""Fetching the Parakeet ASR model so an installed tool can run from anywhere.

The model is ~490 MB compressed and cannot ship in a wheel, so it is downloaded on
first use into the workspace. The source is a plain GitHub release asset rather
than the Hugging Face mirror: it needs no account, no token, and no extra
dependency -- httpx is already a core dependency and the rest is stdlib.
"""

from __future__ import annotations

import os
import shutil
import sys
import tarfile
from pathlib import Path

import httpx

from .config import SHERPA_FP16_MODEL_NAME, SHERPA_INT8_MODEL_NAME, SHERPA_MODEL_NAME, sherpa_model_name

DEFAULT_MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
    f"{SHERPA_INT8_MODEL_NAME}.tar.bz2"
)

# The FP32 weights have no GitHub release tarball -- sherpa-onnx publishes only the INT8
# archive for v3 -- so they come from the upstream author's Hugging Face repo as four
# plain files. No account, no token, no hub client: the same httpx streaming used for the
# archive. encoder.weights is ONNX external data, useless without encoder.onnx beside it.
FP32_REPO = "csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3"
FP32_FILES = ("encoder.onnx", "encoder.weights", "decoder.onnx", "joiner.onnx", "tokens.txt")

# The sidecar pulls the diarization models with hf-hub, which caches under
# HF_HOME (default ~/.cache/huggingface) in a directory named after the repo.
SPEAKRS_HF_REPO = "avencera/speakrs-models"

# What SherpaAsr actually opens. A directory missing any one of these is not a
# usable model, however it got there.
REQUIRED_INT8_FILES = ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt")
REQUIRED_FP16_FILES = (
    "encoder.fp16.onnx",
    "encoder.fp16.weights",
    "decoder.fp16.onnx",
    "joiner.fp16.onnx",
    "tokens.txt",
)
REQUIRED_FP32_FILES = ("encoder.onnx", "encoder.weights", "decoder.onnx", "joiner.onnx", "tokens.txt")
# Back-compat alias: the INT8 set is what the offline help text still enumerates.
REQUIRED_FILES = REQUIRED_INT8_FILES

OFFLINE_HELP = f"""
Could not download the Parakeet ASR model. Three ways around it:

  1. Mirror or proxy -- point PARAKEET_MODEL_URL at a reachable copy of
     {SHERPA_INT8_MODEL_NAME}.tar.bz2, and set PARAKEET_SHERPA_PRECISION=int8

  2. Sideload -- on any machine with access, download
        {DEFAULT_MODEL_URL}
     unpack it, and point PARAKEET_SHERPA_MODEL_DIR (or --sherpa-model-dir) at the
     resulting directory. It must contain:
        {", ".join(REQUIRED_FILES)}

  3. Smaller download -- PARAKEET_SHERPA_PRECISION=int8 fetches a 490 MB archive
     instead of the 2.4 GB FP32 weights, at a real cost in accuracy on non-English
     speech (it drops whole clauses).

  4. Skip the local model entirely -- an OpenAI-compatible ASR endpoint needs no
     download at all:
        PARAKEET_ASR_BACKEND=openai PARAKEET_OPENAI_BASE_URL=http://your-host/v1
""".strip()


FP16_HELP = """
FP16 weights are built locally from the FP32 ones, which needs the conversion extra:

    uv tool install paraspeakrs --with 'paraspeakrs[fp16]'
    # or, in a plain environment:  pip install 'paraspeakrs[fp16]'

Or pick a precision that needs no conversion:

    paraspeakrs fetch-models --sherpa-precision fp32   # 2.4 GB, the most accurate
    paraspeakrs fetch-models --sherpa-precision int8   #  490 MB, drops speech
""".strip()


def model_url() -> str:
    return os.getenv("PARAKEET_MODEL_URL", DEFAULT_MODEL_URL)


def auto_download_enabled() -> bool:
    return os.getenv("PARAKEET_AUTO_DOWNLOAD", "1") not in ("0", "false", "False")


def model_is_present(model_dir: Path) -> bool:
    """True when ``model_dir`` holds a complete model of *either* precision."""
    return any(
        all((model_dir / name).is_file() for name in required)
        for required in (REQUIRED_FP32_FILES, REQUIRED_FP16_FILES, REQUIRED_INT8_FILES)
    )


def present_precision(model_dir: Path) -> str | None:
    """Which precision's complete weights ``model_dir`` holds; FP32 wins a tie."""
    for name, required in (
        ("fp32", REQUIRED_FP32_FILES),
        ("fp16", REQUIRED_FP16_FILES),
        ("int8", REQUIRED_INT8_FILES),
    ):
        if all((model_dir / f).is_file() for f in required):
            return name
    return None


def find_sherpa_model(
    model_dir: Path,
    workspace_dir: Path,
    precisions: tuple[str, ...] = ("fp32", "fp16", "int8"),
) -> Path | None:
    """A model already on disk, to use instead of downloading into ``model_dir``.

    Looks in ``model_dir`` itself, inside it (it may name the models directory rather
    than one model), beside it, and in the workspace's models directory -- in that
    order, trying ``precisions`` in order within each. Without this a server started
    with a different precision setting than the one that downloaded the model would
    ignore gigabytes sitting next to it and fetch another copy.
    """
    if model_is_present(model_dir):
        return model_dir
    roots = dict.fromkeys((model_dir, model_dir.parent, workspace_dir / "models"))
    for precision in precisions:
        for root in roots:
            candidate = root / sherpa_model_name(precision)  # type: ignore[arg-type]
            if present_precision(candidate) == precision:
                return candidate
    return None


def _precision_for(model_dir: Path, precision: str | None) -> str:
    """What to download when ``model_dir`` is empty.

    Falls back to the directory name so that a path the caller chose -- the default
    dirs are named after the model -- still fetches what it is named for, rather
    than silently filling an ``-int8`` directory with FP32 weights.
    """
    if precision is not None:
        return precision
    for suffix in ("-int8", "-fp16"):
        if model_dir.name.endswith(suffix):
            return suffix.lstrip("-")
    return "fp32"


def ensure_sherpa_model(model_dir: Path, precision: str | None = None) -> Path:
    """Return ``model_dir``, downloading the model into it if it is not there yet.

    A no-op when the model is already present, so this is safe to call on every
    pipeline build.
    """
    if model_is_present(model_dir):
        return model_dir
    if not auto_download_enabled():
        raise RuntimeError(
            f"ASR model missing at {model_dir} and PARAKEET_AUTO_DOWNLOAD is off.\n\n{OFFLINE_HELP}"
        )
    chosen = _precision_for(model_dir, precision)
    if chosen == "int8":
        download_sherpa_model(model_dir)
    elif chosen == "fp16":
        build_sherpa_fp16_model(model_dir)
    else:
        download_sherpa_fp32_model(model_dir)
    return model_dir


def build_sherpa_fp16_model(model_dir: Path) -> Path:
    """Produce FP16 weights locally by converting the FP32 ones.

    Nobody publishes an FP16 build of Parakeet v3 in sherpa's layout -- the upstream
    repo for it is empty, and the third-party ONNX exports fuse decoder and joiner into
    one graph that sherpa's from_transducer() cannot load. So FP16 is *derived*: the
    FP32 weights are fetched, converted, and then deleted. The saving is disk, not
    bandwidth, and the user is told so before it starts.
    """
    source = model_dir.parent / SHERPA_MODEL_NAME
    print(f"Building FP16 weights in {model_dir}", file=sys.stderr)
    print(
        "  FP16 is converted from the FP32 weights, so this downloads 2.4 GB once and "
        "then keeps 1.2 GB.",
        file=sys.stderr,
    )
    if not model_is_present(source):
        download_sherpa_fp32_model(source)

    staging = model_dir.parent / f".{model_dir.name}.incoming"
    try:
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        shutil.copyfile(source / "tokens.txt", staging / "tokens.txt")
        for name in ("decoder", "joiner", "encoder"):
            print(f"  converting {name}", file=sys.stderr)
            _convert_to_fp16(source / f"{name}.onnx", staging / f"{name}.fp16.onnx", name == "encoder")
        missing = [n for n in REQUIRED_FP16_FILES if not (staging / n).is_file()]
        if missing:
            raise RuntimeError(f"conversion did not produce: {', '.join(missing)}")
        shutil.rmtree(model_dir, ignore_errors=True)
        staging.rename(model_dir)
    except Exception as exc:
        raise RuntimeError(f"{exc}\n\n{FP16_HELP}") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    # The FP32 copy was only ever an intermediate; keeping it would defeat the point.
    shutil.rmtree(source, ignore_errors=True)
    print(f"ASR model ready at {model_dir}", file=sys.stderr)
    return model_dir


def _convert_to_fp16(source: Path, destination: Path, external_data: bool) -> None:
    try:
        import onnx
        from onnxconverter_common import float16
    except ImportError as exc:
        raise RuntimeError(f"{exc}\n\n{FP16_HELP}") from exc

    model = onnx.load(str(source))

    # Everything the encoder derives from its `length` input is frame-count arithmetic,
    # not neural computation. Converting it produces a graph ONNX Runtime rejects -- the
    # converter rewrites the constants but not the Cast attributes beside them -- and
    # rounding a sequence length in fp16 would be wrong even if it loaded. Propagation
    # stops at anything outside SCALAR so the real Conv/MatMul work is still converted.
    SCALAR = {"Cast", "Add", "Sub", "Mul", "Div", "Floor", "Ceil", "Min", "Max", "Clip", "Round"}
    tainted = {"length"}
    block: set[str] = set()
    for node in model.graph.node:
        if node.op_type in SCALAR and any(i in tainted for i in node.input):
            block.add(node.name)
            tainted.update(node.output)
    # Constant nodes have no inputs, so the pass above can never reach them -- but a
    # blocked node adding a converted constant is exactly the type clash being avoided.
    blocked_inputs = {i for n in model.graph.node if n.name in block for i in n.input}
    for node in model.graph.node:
        if node.op_type == "Constant" and any(o in blocked_inputs for o in node.output):
            block.add(node.name)

    converted = float16.convert_float_to_float16(
        model, keep_io_types=True, disable_shape_infer=True, node_block_list=sorted(block)
    )
    # value_info still holds the pre-conversion float annotations; ORT trusts them over
    # the graph and refuses to load. Dropping them lets it re-infer.
    del converted.graph.value_info[:]
    onnx.save(
        converted,
        str(destination),
        save_as_external_data=external_data,
        location=f"{destination.stem}.weights" if external_data else None,
        all_tensors_to_one_file=True,
    )


def download_sherpa_fp32_model(model_dir: Path) -> Path:
    """Fetch the FP32 weights file by file into ``model_dir``.

    Staged and renamed like the archive path, for the same reason: a download cut
    off halfway must not leave behind a directory that passes model_is_present().
    """
    base = os.getenv("PARAKEET_FP32_BASE_URL", f"https://huggingface.co/{FP32_REPO}/resolve/main")
    parent = model_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = parent / f".{model_dir.name}.incoming"

    print(f"Downloading Parakeet ASR model (FP32, ~2.4 GB) into {model_dir}", file=sys.stderr)
    print(f"  from {base}", file=sys.stderr)
    print(
        "  (too big? PARAKEET_SHERPA_PRECISION=int8 fetches 490 MB instead, "
        "at a real cost in accuracy on non-English speech)",
        file=sys.stderr,
    )
    try:
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        for name in FP32_FILES:
            print(f"  {name}", file=sys.stderr)
            _stream_to_file(f"{base}/{name}", staging / name)
        missing = [n for n in REQUIRED_FP32_FILES if not (staging / n).is_file()]
        if missing:
            raise RuntimeError(f"download is missing: {', '.join(missing)}")
        shutil.rmtree(model_dir, ignore_errors=True)
        staging.rename(model_dir)
    except Exception as exc:
        raise RuntimeError(f"{exc}\n\n{OFFLINE_HELP}") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    print(f"ASR model ready at {model_dir}", file=sys.stderr)
    return model_dir


def download_sherpa_model(model_dir: Path) -> Path:
    """Download and unpack the model so that ``model_dir`` ends up complete.

    Unpacks into a staging directory and renames at the end: an interrupted
    download must never leave something behind that looks like a working model.
    """
    url = model_url()
    parent = model_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    archive = parent / f".{model_dir.name}.tar.bz2.part"
    staging = parent / f".{model_dir.name}.incoming"

    print(f"Downloading Parakeet ASR model into {model_dir}", file=sys.stderr)
    print(f"  from {url}", file=sys.stderr)
    print(
        "  (already have this model? put it at that path, or point "
        "--sherpa-model-dir / PARAKEET_SHERPA_MODEL_DIR at your copy)",
        file=sys.stderr,
    )
    try:
        _stream_to_file(url, archive)
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        _safe_extract(archive, staging)

        unpacked = staging / SHERPA_INT8_MODEL_NAME
        if not unpacked.is_dir():
            # A mirror may have repacked the archive without the wrapping directory.
            unpacked = staging
        if not model_is_present(unpacked):
            missing = [n for n in REQUIRED_INT8_FILES if not (unpacked / n).is_file()]
            raise RuntimeError(f"downloaded archive is missing: {', '.join(missing)}")

        shutil.rmtree(model_dir, ignore_errors=True)
        unpacked.rename(model_dir)
    except Exception as exc:
        raise RuntimeError(f"{exc}\n\n{OFFLINE_HELP}") from exc
    finally:
        archive.unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)

    print(f"ASR model ready at {model_dir}", file=sys.stderr)
    return model_dir


def _stream_to_file(url: str, destination: Path) -> None:
    with httpx.stream("GET", url, follow_redirects=True, timeout=60.0) as response:
        response.raise_for_status()
        total = int(response.headers.get("content-length", 0))
        done = 0
        step = 0
        with destination.open("wb") as handle:
            for chunk in response.iter_bytes(chunk_size=1 << 20):
                handle.write(chunk)
                done += len(chunk)
                if total and done * 20 // total > step:
                    step = done * 20 // total
                    print(f"  {done >> 20} / {total >> 20} MiB", file=sys.stderr)


def _safe_extract(archive: Path, destination: Path) -> None:
    """Extract without trusting member paths to stay inside the destination."""
    with tarfile.open(archive, "r:bz2") as tar:
        for member in tar.getmembers():
            target = (destination / member.name).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise RuntimeError(f"archive member escapes the target directory: {member.name}")
        tar.extractall(destination)


def speakrs_models_cache_dir() -> Path:
    """Where the speakrs sidecar caches the diarization models.

    Mirrors hf-hub's layout rather than asking the sidecar, which only learns the
    path after the download this is meant to announce.
    """
    home = os.getenv("HF_HOME")
    root = Path(home).expanduser() if home else Path.home() / ".cache" / "huggingface"
    return root / "hub" / f"models--{SPEAKRS_HF_REPO.replace('/', '--')}"


def announce_speakrs_models_dir() -> None:
    """Name the download location before the sidecar spends minutes on it.

    Silent once the cache exists, so this speaks only when a download is actually
    about to happen -- including for the second channel of the same run.
    """
    cache = speakrs_models_cache_dir()
    if cache.exists():
        return
    print(f"Downloading speakrs diarization models into {cache}", file=sys.stderr)
    print(
        "  (already have them? point --speakrs-models-dir / SPEAKRS_MODELS_DIR at your copy)",
        file=sys.stderr,
    )
