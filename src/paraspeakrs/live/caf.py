"""Reading a CAF file while the recorder is still writing it.

The recorder writes each track as uncompressed 16-bit PCM in a CAF file and appends
to it every 100 ms. CAF puts the audio in a final ``data`` chunk whose size may be
-1 ("up to the end of the file") while it is being written; it is filled in when the
file is closed. So the reader takes whatever whole frames lie between the start of
the audio and the end of the file, and reads the chunk size again on every call in
case the writer has set it.

The file handle stays open: the recorder deletes the CAF once it has merged the
recording into an .m4a, and an open handle can still read the rest after that.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

import numpy as np

_FLOAT = 1 << 0
_LITTLE_ENDIAN = 1 << 1


class CafError(ValueError):
    pass


class CafTail:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.sample_rate: float = 0.0
        self.channels = 0
        self.frames_read = 0
        self._fh = None
        self._dtype: np.dtype | None = None
        self._scale = 1.0
        self._bytes_per_frame = 0
        self._size_offset = 0
        self._audio_start = 0
        self._pos = 0

    @property
    def seconds_read(self) -> float:
        return self.frames_read / self.sample_rate if self.sample_rate else 0.0

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def read(self) -> np.ndarray:
        """The mono float32 samples written since the last call; empty when there are none yet."""
        if self._fh is None:
            try:
                self._fh = self.path.open("rb")
            except FileNotFoundError:
                return _empty()
        if self._dtype is None and not self._parse_header():
            return _empty()
        end = os.fstat(self._fh.fileno()).st_size
        declared = self._declared_size()
        if declared > 0:
            end = min(end, self._audio_start + declared)
        available = end - self._pos
        available -= available % self._bytes_per_frame
        if available <= 0:
            return _empty()
        self._fh.seek(self._pos)
        raw = self._fh.read(available)
        raw = raw[: len(raw) - len(raw) % self._bytes_per_frame]
        self._pos += len(raw)
        samples = np.frombuffer(raw, dtype=self._dtype).astype(np.float32) * self._scale
        if self.channels > 1:
            samples = samples.reshape(-1, self.channels).mean(axis=1)
        self.frames_read += len(samples)
        return samples

    def _declared_size(self) -> int:
        """The data chunk's size without its 4-byte edit count; 0 when not known yet."""
        self._fh.seek(self._size_offset)
        raw = self._fh.read(8)
        if len(raw) < 8:
            return 0
        (size,) = struct.unpack(">q", raw)
        return size - 4 if size > 4 else 0

    def _parse_header(self) -> bool:
        """False while the file is too short to hold the header; CafError if it is not usable."""
        self._fh.seek(0)
        head = self._fh.read(8)
        if len(head) < 8:
            return False
        if head[:4] != b"caff":
            raise CafError(f"{self.path} is not a CAF file")
        offset = 8
        description = None
        while True:
            self._fh.seek(offset)
            chunk = self._fh.read(12)
            if len(chunk) < 12:
                return False
            kind, size = chunk[:4], struct.unpack(">q", chunk[4:])[0]
            if kind == b"desc":
                body = self._fh.read(32)
                if len(body) < 32:
                    return False
                description = struct.unpack(">d4sIIIII", body)
            elif kind == b"data":
                if description is None:
                    raise CafError(f"{self.path}: audio before its description")
                self._size_offset = offset + 4
                self._audio_start = offset + 12 + 4
                self._pos = self._audio_start
                self._set_format(description)
                return True
            if size < 0:
                raise CafError(f"{self.path}: chunk {kind!r} of unknown size before the audio")
            offset += 12 + size

    def _set_format(self, description: tuple) -> None:
        rate, format_id, flags, bytes_per_packet, frames_per_packet, channels, bits = description
        if format_id != b"lpcm":
            raise CafError(f"{self.path}: {format_id.decode(errors='replace')} audio, expected uncompressed PCM")
        order = "<" if flags & _LITTLE_ENDIAN else ">"
        if flags & _FLOAT:
            kinds = {32: "f4", 64: "f8"}
            scale = 1.0
        else:
            kinds = {16: "i2", 32: "i4"}
            scale = 1.0 / (1 << (bits - 1)) if bits in kinds else 1.0
        if bits not in kinds or frames_per_packet != 1 or channels < 1:
            raise CafError(f"{self.path}: unsupported PCM layout ({bits} bits, {channels} channels)")
        self.sample_rate = rate
        self.channels = channels
        self._dtype = np.dtype(order + kinds[bits])
        self._scale = scale
        self._bytes_per_frame = bytes_per_packet or (bits // 8) * channels


def _empty() -> np.ndarray:
    return np.zeros(0, dtype=np.float32)


def write_header(fh, *, sample_rate: float = 48_000.0, data_size: int = -1, little_endian: bool = True) -> None:
    """A CAF header for mono 16-bit PCM, as the recorder writes it. For tests and fakes."""
    flags = _LITTLE_ENDIAN if little_endian else 0
    fh.write(b"caff" + struct.pack(">HH", 1, 0))
    fh.write(b"desc" + struct.pack(">q", 32) + struct.pack(">d4sIIIII", sample_rate, b"lpcm", flags, 2, 1, 1, 16))
    fh.write(b"data" + struct.pack(">q", data_size if data_size < 0 else data_size + 4) + struct.pack(">I", 0))
