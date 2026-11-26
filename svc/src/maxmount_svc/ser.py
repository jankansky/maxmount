# SPDX-License-Identifier: GPL-3.0-or-later
"""SER video files (the planetary / lucky-imaging format read by AutoStakkert!, PIPP, Siril,
RegiStax and SER Player).

Layout (SER v3): a 178-byte little-endian header, the frames back to back (rows top-down,
RGB interleaved for colour), then one int64 UTC timestamp per frame.
Times are "ticks": 100 ns units since 0001-01-01 00:00 (.NET DateTime).

The header's LittleEndian field: the specification says 1 means little-endian 16-bit data,
but the programs that write and read SER (FireCapture, SharpCap, AutoStakkert!, PIPP, Siril)
treat 0 as little-endian. We follow the programs: little-endian data with LittleEndian = 0.
"""
from __future__ import annotations

import struct
from array import array
from pathlib import Path

import numpy as np

HEADER_SIZE = 178
TICKS_UNIX_EPOCH = 621_355_968_000_000_000  # ticks from 0001-01-01 to 1970-01-01
COLOR_IDS = {"MONO": 0, "BAYER_RGGB": 8, "BAYER_GRBG": 9, "BAYER_GBRG": 10, "BAYER_BGGR": 11, "RGB": 100, "BGR": 101}
_HDR = struct.Struct("<14s7i40s40s40sqq")
assert _HDR.size == HEADER_SIZE


def ticks(unix_ns: int) -> int:
    return unix_ns // 100 + TICKS_UNIX_EPOCH


def unix_ns(t: int) -> int:
    return (t - TICKS_UNIX_EPOCH) * 100


class SerWriter:
    """Writes frames of one size and type to a SER file. The frame count in the header and the
    timestamp trailer are written by close(); a file whose writer never closed still holds
    its frames (readers that trust the file size, like read_ser here, can recover it)."""

    def __init__(self, path: str | Path, width: int, height: int, color: str, bit_depth: int,
                 observer: str = "", instrument: str = "", telescope: str = "", utc_offset_s: int = 0):
        if color not in COLOR_IDS:
            raise ValueError(f"color must be one of {', '.join(COLOR_IDS)}")
        self.path = Path(path)
        self.width, self.height, self.color, self.bit_depth = width, height, color, bit_depth
        self.planes = 3 if color in ("RGB", "BGR") else 1
        self.dtype = np.dtype("<u2") if bit_depth > 8 else np.dtype("u1")
        self.frame_bytes = width * height * self.planes * self.dtype.itemsize
        self._meta = (observer, instrument, telescope)
        self._utc_offset_ns = utc_offset_s * 1_000_000_000
        self.frames = 0
        self._times = array("q")
        self._first_ns = 0
        self._f = open(self.path, "wb")
        self._f.write(self._header())

    def _header(self) -> bytes:
        def s(x: str) -> bytes:
            return x.encode("ascii", "replace")[:40]
        t = self._first_ns
        return _HDR.pack(b"LUCAM-RECORDER", 0, COLOR_IDS[self.color], 0, self.width, self.height, self.bit_depth,
                         self.frames, *(s(m) for m in self._meta),
                         ticks(t + self._utc_offset_ns) if t else 0, ticks(t) if t else 0)

    def write(self, frame: np.ndarray, t_ns: int) -> None:
        """One frame (H x W, or H x W x 3 for RGB/BGR) and its UTC time in ns since 1970."""
        if frame.shape[:2] != (self.height, self.width) or frame.dtype.itemsize != self.dtype.itemsize:
            raise ValueError(f"frame {frame.shape} {frame.dtype} does not match {self.width}x{self.height} {self.dtype}")
        if self.dtype.itemsize == 2 and frame.dtype.byteorder == ">":
            frame = frame.astype(self.dtype)
        self._f.write(memoryview(np.ascontiguousarray(frame)).cast("B"))
        if not self.frames:
            self._first_ns = t_ns
        self._times.append(ticks(t_ns))
        self.frames += 1

    @property
    def bytes_written(self) -> int:
        return HEADER_SIZE + self.frames * self.frame_bytes

    def close(self) -> None:
        if self._f.closed:
            return
        self._f.write(self._times.tobytes())  # array('q') is native = little-endian on the Pi and Macs
        self._f.seek(0)
        self._f.write(self._header())
        self._f.close()


def read_ser(path: str | Path) -> dict:
    """The header, the frames (a memory map, N x H x W [x 3]) and the timestamps (unix ns, or
    None when the file has no trailer)."""
    path = Path(path)
    with open(path, "rb") as fh:
        raw = fh.read(HEADER_SIZE)
    if len(raw) < HEADER_SIZE:
        raise ValueError(f"{path}: too short for a SER file")
    f = _HDR.unpack(raw)
    if f[0] != b"LUCAM-RECORDER":
        raise ValueError(f"{path}: not a SER file")
    color = {v: k for k, v in COLOR_IDS.items()}.get(f[2], str(f[2]))
    w, h, depth, n = f[4], f[5], f[6], f[7]
    planes = 3 if color in ("RGB", "BGR") else 1
    dt = np.dtype("<u2") if depth > 8 else np.dtype("u1")
    frame_bytes = w * h * planes * dt.itemsize
    size = path.stat().st_size
    if n == 0:  # never closed: count the frames the file holds
        n = (size - HEADER_SIZE) // frame_bytes
    shape = (n, h, w, 3) if planes == 3 else (n, h, w)
    frames = np.memmap(path, dtype=dt, mode="r", offset=HEADER_SIZE, shape=shape) if n else np.empty(shape, dt)
    trailer = HEADER_SIZE + n * frame_bytes
    times = None
    if size >= trailer + 8 * n and n:
        times = [unix_ns(t) for t in np.fromfile(path, dtype="<i8", count=n, offset=trailer)]
    return {"color": color, "width": w, "height": h, "bit_depth": depth, "frames": frames, "count": n,
            "observer": f[8].rstrip(b"\0 ").decode("ascii", "replace"),
            "instrument": f[9].rstrip(b"\0 ").decode("ascii", "replace"),
            "telescope": f[10].rstrip(b"\0 ").decode("ascii", "replace"),
            "start_ns": unix_ns(f[12]) if f[12] else 0, "times_ns": times}
