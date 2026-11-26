# SPDX-License-Identifier: GPL-3.0-or-later
"""SER video files: the header layout byte by byte, frames and timestamps, and recovering a
file whose writer never closed."""
import struct

import numpy as np
import pytest

from maxmount_svc import ser


def test_header_layout_frames_and_trailer(tmp_path):
    p = tmp_path / "a.ser"
    t0 = 1_764_000_000_123_456_700  # 2025-11-24, a multiple of 100 ns
    w = ser.SerWriter(p, 8, 6, "BAYER_RGGB", 16, instrument="ZWO ASI224MC", utc_offset_s=-5 * 3600)
    frames = [np.full((6, 8), 1000 * i + 7, np.uint16) for i in range(3)]
    for i, f in enumerate(frames):
        w.write(f, t0 + i * 10_000_000)
    w.close()
    b = p.read_bytes()
    assert len(b) == 178 + 3 * 8 * 6 * 2 + 3 * 8
    assert b[:14] == b"LUCAM-RECORDER"
    lu, color, le, width, height, depth, n = struct.unpack_from("<7i", b, 14)
    assert (lu, color, le, width, height, depth, n) == (0, 8, 0, 8, 6, 16, 3)
    assert b[42:82].rstrip(b"\0") == b"" and b[82:122].rstrip(b"\0") == b"ZWO ASI224MC"
    local, utc = struct.unpack_from("<qq", b, 162)
    assert utc == t0 // 100 + 621_355_968_000_000_000
    assert local == utc - 5 * 3600 * 10_000_000
    # Frame data is little-endian, right after the header.
    assert struct.unpack_from("<H", b, 178)[0] == 7 and struct.unpack_from("<H", b, 178 + 96)[0] == 1007
    r = ser.read_ser(p)
    assert r["count"] == 3 and r["color"] == "BAYER_RGGB" and r["instrument"] == "ZWO ASI224MC"
    assert np.array_equal(r["frames"][2], frames[2]) and r["start_ns"] == t0
    assert r["times_ns"] == [t0, t0 + 10_000_000, t0 + 20_000_000]


def test_rgb_and_mono_8bit(tmp_path):
    rgb = np.arange(4 * 8 * 3, dtype=np.uint8).reshape(4, 8, 3)
    w = ser.SerWriter(tmp_path / "c.ser", 8, 4, "RGB", 8)
    w.write(rgb[:, ::-1][:, ::-1], 1)  # a non-contiguous view is fine
    w.close()
    r = ser.read_ser(tmp_path / "c.ser")
    assert r["color"] == "RGB" and r["frames"].shape == (1, 4, 8, 3) and np.array_equal(r["frames"][0], rgb)
    w = ser.SerWriter(tmp_path / "m.ser", 8, 4, "MONO", 8)
    with pytest.raises(ValueError):
        w.write(np.zeros((4, 6), np.uint8), 1)  # wrong size
    w.close()
    assert ser.read_ser(tmp_path / "m.ser")["count"] == 0


def test_unclosed_file_is_recoverable(tmp_path):
    p = tmp_path / "crash.ser"
    w = ser.SerWriter(p, 8, 2, "MONO", 8)
    for i in range(5):
        w.write(np.full((2, 8), i, np.uint8), 100 + i)
    w._f.flush()  # the process dies here: header frame count 0, no trailer
    r = ser.read_ser(p)
    assert r["count"] == 5 and r["frames"][4][0, 0] == 4 and r["times_ns"] is None
    w.close()
