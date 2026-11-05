#!/usr/bin/env python3
"""Check an installed paraspeakrs-record (Linux and Windows wheels).

Run with the interpreter of an environment that has paraspeakrs and the wheel
installed. Fails unless paraspeakrs finds the binary the wheel installed and it
starts. With --record it also records a few seconds through `paraspeakrs
record` and checks the result is the stereo AAC the macOS recorder makes, which
needs audio devices: CI provides them on Linux with PulseAudio null sinks.

Usage: smoke-test-recorder.py [--record]
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from paraspeakrs.config import CHECKOUT_RECORDER_BIN, default_recorder_bin


def main(argv: list[str]) -> int:
    binary = default_recorder_bin()
    print(f"paraspeakrs uses {binary}")
    if binary == CHECKOUT_RECORDER_BIN or not binary.is_file():
        print("error: paraspeakrs did not find the recorder the wheel installed", file=sys.stderr)
        return 1
    help_run = subprocess.run([str(binary), "--help"], capture_output=True, text=True)
    if help_run.returncode != 0 or "--mic-out" not in help_run.stdout:
        print(f"error: {binary} --help failed:\n{help_run.stderr}", file=sys.stderr)
        return 1
    if "--record" not in argv:
        return 0

    print(subprocess.run([str(binary), "--list-devices"], capture_output=True, text=True).stdout)
    with tempfile.TemporaryDirectory() as tmp:
        run = subprocess.run(
            [sys.executable, "-c", "from paraspeakrs.cli import main; main()", "record", "--seconds", "3", "--no-transcribe", "--out-dir", tmp],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )
        print(run.stderr)
        if run.returncode != 0:
            print(f"error: paraspeakrs record exited {run.returncode}", file=sys.stderr)
            return 1
        recordings = list(Path(tmp).glob("*.m4a"))
        leftovers = list(Path(tmp).glob("*.wav"))
        if len(recordings) != 1 or leftovers:
            print(f"error: expected one .m4a and no tracks, got {sorted(Path(tmp).iterdir())}", file=sys.stderr)
            return 1
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,channels:format=duration",
             "-of", "json", str(recordings[0])],
            capture_output=True, text=True, check=True,
        )  # fmt: skip
        info = json.loads(probe.stdout)
        stream, seconds = info["streams"][0], float(info["format"]["duration"])
        print(f"{recordings[0].name}: {stream}, {seconds:.2f} s")
        # 0.25 s: AAC framing adds a few ms; seconds spent opening devices must not count.
        if stream != {"codec_name": "aac", "channels": 2} or abs(seconds - 3) > 0.25:
            print("error: not a 3 s stereo AAC recording", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
