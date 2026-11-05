#!/usr/bin/env python3
"""Check an installed paraspeakrs-speakrs wheel end to end.

Run with the interpreter of an environment that has paraspeakrs and the wheel
installed. Fails unless:

- paraspeakrs finds the binary the wheel installed (not a checkout build),
- the binary lists the modes it was built with, and
- each requested mode downloads its models and diarizes a short file, printing
  version-1 JSON.

The audio is synthetic (noise-modulated tones), so the segments say nothing
about accuracy; this proves the binary runs on the platform.

Usage: smoke-test-speakrs.py <mode> [<mode> ...]
"""

from __future__ import annotations

import json
import math
import random
import struct
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

from paraspeakrs.config import CHECKOUT_SPEAKRS_BIN, default_speakrs_bin

SAMPLE_RATE = 16_000
SECONDS = 30


def write_test_audio(path: Path) -> None:
    """Two alternating "voices": bursts of noise-modulated tones at different pitches."""
    rng = random.Random(0)
    frames = bytearray()
    for n in range(SAMPLE_RATE * SECONDS):
        t = n / SAMPLE_RATE
        pitch = 140.0 if int(t / 3) % 2 == 0 else 230.0
        envelope = max(0.0, math.sin(2 * math.pi * 3.0 * t))
        value = envelope * (0.5 * math.sin(2 * math.pi * pitch * t) + 0.1 * rng.uniform(-1, 1))
        frames += struct.pack("<h", int(max(-1.0, min(1.0, value)) * 32767 * 0.6))
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(bytes(frames))


def main(modes: list[str]) -> int:
    if not modes:
        print(__doc__, file=sys.stderr)
        return 2

    binary = default_speakrs_bin()
    print(f"paraspeakrs uses {binary}")
    if binary == CHECKOUT_SPEAKRS_BIN or not binary.is_file():
        print("error: paraspeakrs did not find the binary the wheel installed", file=sys.stderr)
        return 1

    listing = subprocess.run([str(binary), "--mode", "none", "x.wav"], capture_output=True, text=True)
    print(listing.stderr.strip())
    missing = [mode for mode in modes if mode not in listing.stderr]
    if missing:
        print(f"error: binary was built without mode(s) {', '.join(missing)}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        audio = Path(tmp) / "smoke.wav"
        write_test_audio(audio)
        for mode in modes:
            started = time.monotonic()
            run = subprocess.run([str(binary), "--mode", mode, str(audio)], capture_output=True, text=True)
            elapsed = time.monotonic() - started
            if run.returncode != 0:
                print(f"error: --mode {mode} exited {run.returncode}:\n{run.stderr}", file=sys.stderr)
                return 1
            payload = json.loads(run.stdout)
            if payload.get("version") != 1:
                print(f"error: --mode {mode} returned contract version {payload.get('version')!r}", file=sys.stderr)
                return 1
            print(
                f"--mode {mode}: {len(payload['segments'])} segments, "
                f"{len(payload['centroids'])} speakers, {elapsed:.1f} s including model download"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
