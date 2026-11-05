import json
import math
import shutil
import struct
import subprocess
import sys
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from paraspeakrs import record as record_mod
from paraspeakrs.record import merge_command, record, recording_base

NOW = datetime(2026, 11, 4, 14, 30)

# Stands in for paraspeakrs-record: writes both tracks, waits for its stdin to
# close (the stop signal record() sends), then prints the report.
FAKE_RECORDER = r"""
import json, sys, wave
VALUED = {"--mic-out", "--system-out", "--version", "--seconds", "--mic", "--system"}
args, argv = {}, iter(sys.argv[1:])
for arg in argv:
    args[arg] = next(argv) if arg in VALUED else True
for key in ("--mic-out", "--system-out"):
    with wave.open(args[key], "wb") as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(16000)
        out.writeframes(b"\x00\x00" * 16000)
if "--fail" in sys.argv:
    sys.exit(3)
sys.stdin.read()
track = {"path": "", "device": "fake", "sample_rate": 16000, "channels": 1,
         "received_seconds": 1.0, "padded_seconds": 0.0, "dropped_seconds": 0.0,
         "heard": "--silent" not in sys.argv}
print(json.dumps({"version": int(args.get("--version", 1)), "seconds": 1.0,
                  "mic": track, "system": track}))
"""


def _fake(tmp_path: Path, *flags: str) -> list[str]:
    script = tmp_path / "fake_recorder.py"
    script.write_text(FAKE_RECORDER, encoding="utf-8")
    return [sys.executable, str(script), *flags]


def _merged_ok(cmd, **kwargs):
    Path(cmd[-1]).write_bytes(b"m4a")
    return subprocess.CompletedProcess(cmd, 0, "", "")


def _stop_at_once(stop):
    stop.set()


def test_names_follow_the_macos_app_and_skip_taken_ones(tmp_path):
    assert recording_base(tmp_path, NOW) == tmp_path / "2026-11-04_1430"
    (tmp_path / "2026-11-04_1430.m4a").write_bytes(b"")
    assert recording_base(tmp_path, NOW) == tmp_path / "2026-11-04_1430-2"
    # A crashed recording's tracks also hold on to the name.
    (tmp_path / "2026-11-04_1430-2.mic.wav").write_bytes(b"")
    assert recording_base(tmp_path, NOW) == tmp_path / "2026-11-04_1430-3"


def test_records_merges_and_removes_the_tracks(tmp_path):
    merges = []

    def run(cmd, **kwargs):
        merges.append(cmd)
        return _merged_ok(cmd)

    recording = record(
        _fake(tmp_path), tmp_path / "out", ffmpeg="ffmpeg", now=NOW, start_waiting=_stop_at_once, run=run
    )

    out = tmp_path / "out" / "2026-11-04_1430.m4a"
    assert recording.path == out and out.exists()
    assert recording.seconds == 1.0
    assert merges[0][merges[0].index("-t") + 1] == "1.000"
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["2026-11-04_1430.m4a"]


def test_options_reach_the_recorder(tmp_path):
    seen = []

    def popen(cmd, **kwargs):
        seen.append(cmd)
        return subprocess.Popen(cmd, **kwargs)

    record(
        _fake(tmp_path),
        tmp_path,
        seconds=90,
        mic="USB",
        system="HDMI",
        ffmpeg="ffmpeg",
        now=NOW,
        start_waiting=_stop_at_once,
        popen=popen,
        run=_merged_ok,
    )

    cmd = seen[0]
    assert cmd[cmd.index("--seconds") + 1] == "90"
    assert cmd[cmd.index("--mic") + 1] == "USB"
    assert cmd[cmd.index("--system") + 1] == "HDMI"
    assert "--stop-on-stdin" in cmd


def test_a_failed_merge_keeps_both_tracks(tmp_path):
    def run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "Invalid data")

    with pytest.raises(RuntimeError, match="kept"):
        record(_fake(tmp_path), tmp_path, ffmpeg="ffmpeg", now=NOW, start_waiting=_stop_at_once, run=run)
    assert (tmp_path / "2026-11-04_1430.mic.wav").exists()
    assert (tmp_path / "2026-11-04_1430.system.wav").exists()


def test_a_failing_recorder_is_reported(tmp_path):
    with pytest.raises(RuntimeError, match="exit code 3"):
        record(
            _fake(tmp_path, "--fail"), tmp_path, ffmpeg="ffmpeg", now=NOW, start_waiting=_stop_at_once, run=_merged_ok
        )


def test_a_recorder_from_another_release_is_refused(tmp_path):
    with pytest.raises(RuntimeError, match="version 2"):
        record(
            _fake(tmp_path, "--version", "2"),
            tmp_path,
            ffmpeg="ffmpeg",
            now=NOW,
            start_waiting=_stop_at_once,
            run=_merged_ok,
        )


def test_a_silent_track_is_pointed_out(tmp_path, capsys):
    record(
        _fake(tmp_path, "--silent"), tmp_path, ffmpeg="ffmpeg", now=NOW, start_waiting=_stop_at_once, run=_merged_ok
    )
    assert "system-audio track is silent" in capsys.readouterr().err


def test_end_of_input_is_not_enter(monkeypatch):
    """With stdin at /dev/null, a --seconds recording must not stop at once."""
    import io
    import threading

    stop = threading.Event()
    monkeypatch.setattr(record_mod.sys, "stdin", io.StringIO(""))
    record_mod.wait_for_enter(stop)
    assert not stop.wait(0.2)

    monkeypatch.setattr(record_mod.sys, "stdin", io.StringIO("\n"))
    record_mod.wait_for_enter(stop)
    assert stop.wait(2)


def _tone(path: Path, rate: int, seconds: float, hz: float) -> None:
    frames = b"".join(
        struct.pack("<h", int(12000 * math.sin(2 * math.pi * hz * n / rate))) for n in range(int(rate * seconds))
    )
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(frames)


def _loudest_hz(samples: np.ndarray, rate: int) -> float:
    return float(np.fft.rfftfreq(len(samples), 1 / rate)[np.argmax(np.abs(np.fft.rfft(samples)))])


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_the_merge_puts_the_mic_left_and_the_system_right(tmp_path):
    """Different rates and lengths, as two real devices give them."""
    mic, system, out = tmp_path / "mic.wav", tmp_path / "system.wav", tmp_path / "out.m4a"
    _tone(mic, 44_100, 2.0, 440)
    _tone(system, 48_000, 1.5, 880)

    subprocess.run(merge_command("ffmpeg", mic, system, out, 2.0), check=True)

    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(out), "-f", "s16le", "-ac", "2", "-ar", "48000", "-"],
        capture_output=True,
        check=True,
    ).stdout
    stereo = np.frombuffer(raw, dtype="<i2").reshape(-1, 2).astype(float)
    assert abs(len(stereo) / 48_000 - 2.0) < 0.05
    first_second = stereo[:48_000]
    assert abs(_loudest_hz(first_second[:, 0], 48_000) - 440) < 5
    assert abs(_loudest_hz(first_second[:, 1], 48_000) - 880) < 5
    # The shorter system track is padded with silence, not stretched.
    assert np.abs(stereo[-4_800:, 1]).max() < 50
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=channel_layout,codec_name", "-of", "json", str(out)],
        capture_output=True,
        text=True,
        check=True,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert stream == {"codec_name": "aac", "channel_layout": "stereo"}
