from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class SamplePlayer:
    """Fire-and-forget playback of a short wav via afplay / ffplay."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._cmd = _player_command()

    @property
    def available(self) -> bool:
        return self._cmd is not None

    def play(self, path: Path) -> None:
        if self._cmd is None:
            raise RuntimeError("no audio player found (install ffmpeg for ffplay, or run on macOS)")
        self.stop()
        self._proc = subprocess.Popen(
            [*self._cmd, str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
        self._proc = None

    def is_playing(self) -> bool:
        return self._proc is not None and self._proc.poll() is None


def _player_command() -> list[str] | None:
    if shutil.which("afplay"):
        return ["afplay"]
    if shutil.which("ffplay"):
        return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]
    return None
