from __future__ import annotations

import base64
import logging
import shutil
import subprocess
import urllib.request
from pathlib import Path

LOGGER = logging.getLogger("uvicorn.error")


class AudioPreparer:
    def __init__(self, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> None:
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe

    def materialize(
        self,
        work_dir: Path,
        *,
        file_path: Path | None = None,
        file_url: str | None = None,
        file_base64: str | None = None,
    ) -> Path:
        provided = [file_path is not None, file_url is not None, file_base64 is not None]
        if sum(provided) != 1:
            raise ValueError("provide exactly one of file_path, file_url, or file_base64")

        work_dir.mkdir(parents=True, exist_ok=True)
        raw_path = work_dir / "input_audio"
        if file_path is not None:
            source = Path(file_path)
            raw_path = work_dir / source.name
            shutil.copyfile(source, raw_path)
        elif file_url is not None:
            with urllib.request.urlopen(file_url, timeout=60) as response:
                raw_path.write_bytes(response.read())
        elif file_base64 is not None:
            raw_path.write_bytes(base64.b64decode(file_base64))
        return raw_path

    def normalize_mono_16khz(self, input_path: Path, output_path: Path) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            self.ffmpeg,
            "-y",
            "-i",
            str(input_path),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ]
        _run_command(cmd, "normalize audio")
        return output_path

    def normalize_16khz_preserving_channels(self, input_path: Path, output_path: Path) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            self.ffmpeg,
            "-y",
            "-i",
            str(input_path),
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ]
        _run_command(cmd, "normalize audio (preserve channels)")
        return output_path

    def channel_count(self, input_path: Path) -> int:
        cmd = [
            self.ffprobe,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=channels",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(input_path),
        ]
        output = _run_command(cmd, "probe audio channels").stdout.strip()
        return int(output)

    def split_stereo_to_mono(self, input_path: Path, left_path: Path, right_path: Path) -> tuple[Path, Path]:
        left_path.parent.mkdir(parents=True, exist_ok=True)
        right_path.parent.mkdir(parents=True, exist_ok=True)
        for output_path, pan_expr in ((left_path, "c0=c0"), (right_path, "c0=c1")):
            cmd = [
                self.ffmpeg,
                "-y",
                "-i",
                str(input_path),
                "-af",
                f"pan=mono|{pan_expr}",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(output_path),
            ]
            _run_command(cmd, "split stereo to mono")
        return left_path, right_path

    def export_chunk(self, input_path: Path, output_path: Path, start: float, end: float) -> Path:
        duration = max(0.0, end - start)
        cmd = [
            self.ffmpeg,
            "-y",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(input_path),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ]
        _run_command(cmd, "export audio chunk")
        return output_path

    def export_chunk_preserving_channels(self, input_path: Path, output_path: Path, start: float, end: float) -> Path:
        duration = max(0.0, end - start)
        cmd = [
            self.ffmpeg,
            "-y",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(input_path),
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ]
        _run_command(cmd, "export audio chunk (preserve channels)")
        return output_path

    def duration_seconds(self, input_path: Path) -> float:
        cmd = [
            self.ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(input_path),
        ]
        output = _run_command(cmd, "probe audio duration").stdout.strip()
        return float(output)


def _run_command(cmd: list[str], action: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except subprocess.CalledProcessError as exc:
        LOGGER.error(
            "%s failed with exit code %s\ncommand: %s\nstdout:\n%s\nstderr:\n%s",
            action,
            exc.returncode,
            " ".join(cmd),
            exc.stdout or "",
            exc.stderr or "",
        )
        raise
