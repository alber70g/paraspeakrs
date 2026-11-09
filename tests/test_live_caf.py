"""Reading the recorder's CAF tracks while they are still being written."""

from __future__ import annotations

import struct

import numpy as np
import pytest

from paraspeakrs.live.caf import CafError, CafTail, write_header


def _pcm(values, *, big_endian: bool = False) -> bytes:
    return np.asarray(values, dtype=">i2" if big_endian else "<i2").tobytes()


def test_reads_what_was_appended_since_the_last_call(tmp_path):
    path = tmp_path / "t.mic.caf"
    tail = CafTail(path)
    assert len(tail.read()) == 0  # not created yet

    with path.open("wb") as fh:
        fh.write(b"caff")
        fh.flush()
        assert len(tail.read()) == 0  # header still incomplete
        fh.seek(0)
        write_header(fh, sample_rate=48_000)
        fh.write(_pcm([0, 16384]))
        fh.flush()
        np.testing.assert_allclose(tail.read(), [0.0, 0.5])
        fh.write(_pcm([-32768]) + b"\x01")  # one whole frame and half of the next
        fh.flush()
        np.testing.assert_allclose(tail.read(), [-1.0])
        fh.write(b"\x00")
        fh.flush()
        assert len(tail.read()) == 1

    assert tail.sample_rate == 48_000
    assert tail.frames_read == 4
    assert tail.seconds_read == pytest.approx(4 / 48_000)


def test_a_closed_file_stops_at_the_declared_size(tmp_path):
    path = tmp_path / "t.caf"
    with path.open("wb") as fh:
        write_header(fh, data_size=4)
        fh.write(_pcm([100, 200]))
        fh.write(b"info" + struct.pack(">q", 4) + b"\x00\x00\x00\x00")
    samples = CafTail(path).read()
    assert len(samples) == 2


def test_a_size_of_zero_means_not_known_yet(tmp_path):
    path = tmp_path / "t.caf"
    with path.open("wb") as fh:
        write_header(fh, data_size=0)
        fh.write(_pcm([1, 2, 3]))
    assert len(CafTail(path).read()) == 3


def test_big_endian_and_chunks_before_the_audio(tmp_path):
    path = tmp_path / "t.caf"
    with path.open("wb") as fh:
        fh.write(b"caff" + struct.pack(">HH", 1, 0))
        fh.write(b"desc" + struct.pack(">q", 32) + struct.pack(">d4sIIIII", 16_000.0, b"lpcm", 0, 2, 1, 1, 16))
        fh.write(b"free" + struct.pack(">q", 6) + b"\x00" * 6)
        fh.write(b"data" + struct.pack(">q", -1) + struct.pack(">I", 0))
        fh.write(_pcm([16384], big_endian=True))
    tail = CafTail(path)
    np.testing.assert_allclose(tail.read(), [0.5])
    assert tail.sample_rate == 16_000


def test_float_stereo_is_mixed_to_mono(tmp_path):
    path = tmp_path / "t.caf"
    with path.open("wb") as fh:
        fh.write(b"caff" + struct.pack(">HH", 1, 0))
        fh.write(b"desc" + struct.pack(">q", 32) + struct.pack(">d4sIIIII", 48_000.0, b"lpcm", 3, 8, 1, 2, 32))
        fh.write(b"data" + struct.pack(">q", -1) + struct.pack(">I", 0))
        fh.write(np.asarray([0.2, 0.4, 1.0, 0.0], dtype="<f4").tobytes())
    np.testing.assert_allclose(CafTail(path).read(), [0.3, 0.5], rtol=1e-6)


def test_the_rest_is_still_read_after_the_recorder_deletes_the_file(tmp_path):
    path = tmp_path / "t.caf"
    with path.open("wb") as fh:
        write_header(fh)
        fh.write(_pcm([1]))
        fh.flush()
        tail = CafTail(path)
        assert len(tail.read()) == 1
        fh.write(_pcm([2, 3]))
    path.unlink()
    assert len(tail.read()) == 2
    tail.close()


def test_compressed_or_foreign_files_are_refused(tmp_path):
    path = tmp_path / "t.caf"
    path.write_bytes(b"RIFF" + b"\x00" * 40)
    with pytest.raises(CafError, match="not a CAF"):
        CafTail(path).read()
    with path.open("wb") as fh:
        fh.write(b"caff" + struct.pack(">HH", 1, 0))
        fh.write(b"desc" + struct.pack(">q", 32) + struct.pack(">d4sIIIII", 48_000.0, b"aac ", 0, 0, 1024, 1, 0))
        fh.write(b"data" + struct.pack(">q", -1) + struct.pack(">I", 0))
    with pytest.raises(CafError, match="expected uncompressed PCM"):
        CafTail(path).read()
