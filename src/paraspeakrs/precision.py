"""Choosing ASR model precision, and asking the user about it on first run.

The numbers below are measured, not estimated: one 42-minute Dutch meeting, identical
chunking, greedy decoding, macOS arm64, sherpa-onnx 1.13.8. They matter because the
intuitive model of this choice is wrong in two ways.

First, INT8 does not merely blur words on non-English speech -- it drops whole clauses,
losing a quarter of the transcript while still reading like fluent Dutch, so the damage
is easy to miss.

Second, lower precision does not save memory. Peak RSS is 3.15 GB for FP32, 3.29 GB for
INT8 and 3.95 GB for FP16: ONNX Runtime has no fast low-precision transducer kernels on
CPU, so it casts the weights up at load time and holds both copies. FP32 is the *lightest*
and the *fastest* of the three. What precision actually trades is disk.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import SherpaPrecision


@dataclass(frozen=True)
class PrecisionSpec:
    name: SherpaPrecision
    disk_gb: float
    peak_ram_gb: float
    words: int
    decode_seconds: int
    summary: str
    pro: str
    con: str


# words / decode_seconds are for the same 42-minute recording; 4715 is the reference.
SPECS: dict[str, PrecisionSpec] = {
    "fp32": PrecisionSpec(
        name="fp32",
        disk_gb=2.4,
        peak_ram_gb=3.15,
        words=4715,
        decode_seconds=42,
        summary="full precision - the most accurate, the fastest, the lightest on RAM",
        pro="best transcription; fastest decoding; lowest peak memory",
        con="2.4 GB download",
    ),
    "fp16": PrecisionSpec(
        name="fp16",
        disk_gb=1.2,
        peak_ram_gb=3.95,
        words=4712,
        decode_seconds=46,
        summary="half precision - same transcript as FP32, half the disk",
        pro="99.8% identical to FP32; half the disk footprint",
        con="still downloads FP32 first and converts locally (needs the 'fp16' extra); "
            "slightly slower and uses the most RAM",
    ),
    "int8": PrecisionSpec(
        name="int8",
        disk_gb=0.6,
        peak_ram_gb=3.29,
        words=3546,
        decode_seconds=65,
        summary="quantized - smallest download, but it drops speech",
        pro="490 MB download, by far the smallest",
        con="loses ~25% of the words on non-English speech, and is the slowest",
    ),
}

ORDER: tuple[SherpaPrecision, ...] = ("fp32", "fp16", "int8")


def total_ram_gb() -> float | None:
    """Physical RAM in GB, or None where the platform will not say.

    sysconf rather than psutil: this runs during first-run setup, and a dependency
    that might not be installed is no use at the moment we need to give advice.
    """
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024**3
    except (ValueError, OSError, AttributeError):
        return None


def free_disk_gb(path: Path) -> float | None:
    """Free space on the filesystem that will hold the model."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free / 1024**3
    except OSError:
        return None


def recommend(ram_gb: float | None, disk_gb: float | None) -> tuple[SherpaPrecision, str]:
    """Pick a precision and say why, in one sentence the user can act on.

    Driven by free disk, because that is the only resource the choice actually trades.
    RAM enters only as a warning: every precision peaks around 3-4 GB, so a machine
    that cannot spare that will struggle whichever one is picked.
    """
    headroom = "" if ram_gb is None or ram_gb >= 8 else (
        f" Heads up: {ram_gb:.0f} GB of RAM is tight for this — every option peaks "
        "around 3–4 GB, so close what you can before a long recording."
    )

    if disk_gb is None:
        return "fp32", "FP32 — the best transcription, and the fastest." + headroom
    if disk_gb < 2:
        return "int8", (
            f"INT8 — only {disk_gb:.1f} GB free, which is all that fits. Expect it to "
            "miss words; free up 3 GB and re-run `paraspeakrs fetch-models` for FP32."
        ) + headroom
    if disk_gb < 8:
        return "fp16", (
            f"FP16 — {disk_gb:.1f} GB free is enough for FP32's download but leaves "
            "little behind, and FP16 keeps the same transcript in half the space."
        ) + headroom
    return "fp32", (
        f"FP32 — {disk_gb:.0f} GB free is plenty, and FP32 is the most accurate, "
        "the fastest and the lightest on RAM."
    ) + headroom


def describe(model_root: Path) -> str:
    """The comparison table shown before the question."""
    ram = total_ram_gb()
    disk = free_disk_gb(model_root)
    choice, why = recommend(ram, disk)

    machine = []
    if ram is not None:
        machine.append(f"{ram:.0f} GB RAM")
    if disk is not None:
        machine.append(f"{disk:.0f} GB free disk")

    lines = [
        "",
        "Which Parakeet ASR model should paraspeakrs use?",
        "",
        "  Measured on one 42-minute Dutch meeting (4715 words is the reference):",
        "",
        f"    {'':<6} {'disk':>7} {'peak RAM':>9} {'words':>7} {'decode':>8}   notes",
    ]
    for name in ORDER:
        s = SPECS[name]
        mark = "*" if name == choice else " "
        lines.append(
            f"  {mark} {s.name:<6} {s.disk_gb:>6.1f}G {s.peak_ram_gb:>8.2f}G "
            f"{s.words:>7} {s.decode_seconds:>7}s   {s.summary}"
        )
    lines += [
        "",
        "  Lower precision does not save memory — ONNX Runtime casts the weights back up",
        "  on CPU, so FP32 is both the fastest and the lightest. It only saves disk.",
        "",
    ]
    if machine:
        lines.append(f"  This machine: {', '.join(machine)}")
    lines.append(f"  Recommended: {why}")
    lines.append("")
    return "\n".join(lines)


def can_prompt() -> bool:
    """Only ask a human who is actually watching a terminal."""
    if os.getenv("PARAKEET_NONINTERACTIVE"):
        return False
    try:
        return sys.stdin.isatty() and sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False


def ask(model_root: Path) -> SherpaPrecision:
    """Ask which precision to download. Only call when can_prompt() is true."""
    print(describe(model_root), file=sys.stderr)
    default, _ = recommend(total_ram_gb(), free_disk_gb(model_root))
    while True:
        try:
            raw = input(f"Choice [fp32/fp16/int8] (default {default}): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(f"\nUsing {default}.", file=sys.stderr)
            return default
        if not raw:
            return default
        if raw in SPECS:
            return raw  # type: ignore[return-value]
        print(f"  '{raw}' is not one of fp32, fp16, int8.", file=sys.stderr)


CHOICE_FILE = "asr-precision"


def saved_choice(workspace_dir: Path) -> SherpaPrecision | None:
    """The precision this workspace settled on, if it has been asked before."""
    try:
        raw = (workspace_dir / CHOICE_FILE).read_text().strip()
    except OSError:
        return None
    return raw if raw in SPECS else None  # type: ignore[return-value]


def remember(workspace_dir: Path, choice: SherpaPrecision) -> None:
    """Record the choice so first-run setup happens exactly once."""
    try:
        workspace_dir.mkdir(parents=True, exist_ok=True)
        (workspace_dir / CHOICE_FILE).write_text(f"{choice}\n")
    except OSError:
        # Not being able to remember is a nuisance, not a failure -- the model is
        # still downloaded and the run still works. It will just ask again.
        pass


def resolve(
    workspace_dir: Path,
    model_root: Path,
    requested: SherpaPrecision,
    explicit: bool,
) -> SherpaPrecision:
    """Settle on a precision, asking the user once if nothing else has decided it.

    The prompt is skipped whenever an answer already exists -- an explicit flag or
    environment variable, a previous answer, or a terminal nobody is watching -- so
    scripts, servers and CI never block on it.
    """
    if explicit:
        return requested
    remembered = saved_choice(workspace_dir)
    if remembered is not None:
        return remembered
    if not can_prompt():
        return requested
    choice = ask(model_root)
    remember(workspace_dir, choice)
    return choice
