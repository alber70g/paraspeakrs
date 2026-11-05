"""`paraspeakrs record`: record a meeting on Linux or Windows.

Runs the paraspeakrs-record sidecar, which writes the microphone and the system
audio as two mono WAVs on one clock, then merges them into the file the macOS
recorder makes: a stereo AAC `.m4a` with the microphone on the left and the
system audio on the right. Transcribing it with `--single-speaker-channel L`
keeps your own voice (one L_ speaker) apart from everyone else (R_ speakers).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

# Same folder as the macOS app, so recordings from both land in one place.
RECORDINGS_DIR = Path.home() / "Documents" / "paraspeakrs-recordings"
# Bump together with CONTRACT_VERSION in packages/speakrs-diar/src/bin/paraspeakrs-record.rs.
RECORDER_CONTRACT_VERSION = 1
MERGE_RATE = 48_000


@dataclass(frozen=True)
class Recording:
    path: Path
    seconds: float
    report: dict


def recording_base(directory: Path, now: datetime) -> Path:
    """`2026-11-04_1430`, then `-2`, `-3`, as the macOS app names them.

    A name counts as taken when any of its files exist, including the two tracks
    a crashed recording leaves behind.
    """
    stem = now.strftime("%Y-%m-%d_%H%M")
    candidate = directory / stem
    n = 2
    while any(directory.glob(f"{candidate.name}.*")):
        candidate = directory / f"{stem}-{n}"
        n += 1
    return candidate


def merge_command(ffmpeg: str, mic: Path, system: Path, out: Path, seconds: float) -> list[str]:
    """Mic left, system right, both resampled to one rate and padded to the same
    length, then AAC at 128 kbps like the macOS recorder."""
    graph = (
        f"[0:a]aresample={MERGE_RATE},apad[mic];"
        f"[1:a]aresample={MERGE_RATE},apad[system];"
        "[mic][system]join=inputs=2:channel_layout=stereo:map=0.0-FL|1.0-FR[out]"
    )
    return [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(mic), "-i", str(system),
        "-filter_complex", graph, "-map", "[out]",
        "-t", f"{seconds:.3f}",
        "-c:a", "aac", "-b:a", "128k",
        str(out),
    ]  # fmt: skip


def wait_for_enter(stop: threading.Event) -> None:
    """Set `stop` when Enter is pressed.

    End of input is not Enter: with stdin at /dev/null (a script, a scheduler)
    it arrives at once, and a `--seconds` recording would end before it began.
    """

    def read() -> None:
        try:
            line = sys.stdin.readline()
        except (OSError, ValueError):
            return
        if line:
            stop.set()

    threading.Thread(target=read, daemon=True).start()


def record(
    command: list[str],
    directory: Path,
    *,
    seconds: float | None = None,
    mic: str | None = None,
    system: str | None = None,
    ffmpeg: str | None = None,
    now: datetime | None = None,
    start_waiting: Callable[[threading.Event], None] = wait_for_enter,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> Recording:
    """Record until Enter, Ctrl+C or `seconds`, then merge into `<name>.m4a`.

    `command` is the recorder to run, normally [default_recorder_bin()].
    """
    ffmpeg = ffmpeg or shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not on PATH; it is needed to merge the two tracks into one file")
    directory.mkdir(parents=True, exist_ok=True)
    base = recording_base(directory, now or datetime.now())
    mic_wav = base.parent / f"{base.name}.mic.wav"
    system_wav = base.parent / f"{base.name}.system.wav"
    out = base.parent / f"{base.name}.m4a"

    cmd = [*command, "--mic-out", str(mic_wav), "--system-out", str(system_wav), "--stop-on-stdin"]
    if seconds is not None:
        cmd += ["--seconds", str(seconds)]
    if mic:
        cmd += ["--mic", mic]
    if system:
        cmd += ["--system", system]

    # stderr is left on the terminal: the recorder names its devices and warns
    # there while it runs.
    proc = popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8")
    hint = f"for {seconds:g} s, or until Enter" if seconds is not None else "until you press Enter"
    print(f"Recording {hint} (Ctrl+C also stops) -> {out}", file=sys.stderr)
    stop = threading.Event()
    start_waiting(stop)
    try:
        while proc.poll() is None and not stop.wait(0.1):
            pass
    except KeyboardInterrupt:
        # The recorder got the same Ctrl+C from the terminal and is stopping too.
        pass
    finally:
        stop.set()
        if proc.stdin is not None:
            try:
                proc.stdin.close()  # end of input: the recorder's other stop signal
            except OSError:
                pass
    stdout = _wait_ignoring_ctrl_c(proc)

    if proc.returncode != 0:
        raise RuntimeError(f"paraspeakrs-record failed with exit code {proc.returncode}")
    report = _parse_report(stdout)
    length = float(report["seconds"])

    merged = run(merge_command(ffmpeg, mic_wav, system_wav, out, length), capture_output=True, text=True)
    if merged.returncode != 0:
        raise RuntimeError(
            f"could not merge the tracks into {out}: {(merged.stderr or '').strip()}\n"
            f"The two tracks are kept: {mic_wav} and {system_wav}"
        )
    # Kept only until the merge has worked, as the macOS app keeps its CAFs.
    mic_wav.unlink(missing_ok=True)
    system_wav.unlink(missing_ok=True)
    _warn_about_silence(report)
    return Recording(path=out, seconds=length, report=report)


def _wait_ignoring_ctrl_c(proc: subprocess.Popen) -> str:
    """Wait for the recorder to finish its files; a second Ctrl+C must not orphan
    two half-written WAVs.

    Reads stdout directly rather than through communicate(), which flushes the
    stdin this module has already closed and raises on it.
    """
    chunks: list[str] = []
    while True:
        try:
            if proc.stdout is not None:
                chunks.append(proc.stdout.read())
            proc.wait()
            return "".join(chunks)
        except KeyboardInterrupt:
            print("still saving the recording...", file=sys.stderr)


def _parse_report(stdout: str) -> dict:
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("paraspeakrs-record printed no report")
    try:
        report = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"paraspeakrs-record printed an invalid report: {exc}") from exc
    if report.get("version") != RECORDER_CONTRACT_VERSION:
        raise RuntimeError(
            f"paraspeakrs-record report version {report.get('version')!r} is not the expected "
            f"{RECORDER_CONTRACT_VERSION}; reinstall paraspeakrs so both match"
        )
    return report


def _warn_about_silence(report: dict) -> None:
    if not report["system"].get("heard"):
        print(
            "warning: the system-audio track is silent. Nothing played, or the wrong output "
            "was recorded: see `paraspeakrs record --list-devices`.",
            file=sys.stderr,
        )
    if not report["mic"].get("heard"):
        print(
            "warning: the microphone track is silent. Check the input device and its mute switch.",
            file=sys.stderr,
        )
