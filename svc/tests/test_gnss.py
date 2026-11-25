# SPDX-License-Identifier: GPL-3.0-or-later
import json
import socket
import threading

import pytest

from maxmount_svc import gnss


def fake_gpsd(messages):
    """A one-shot gpsd: accepts a client, waits for ?WATCH, sends the messages, closes."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def serve():
        conn, _ = srv.accept()
        with conn:
            conn.recv(1024)
            for m in messages:
                conn.sendall((json.dumps(m) + "\n").encode())
        srv.close()

    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()[1]


@pytest.fixture(autouse=True)
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))


def tpv(mode, lat=34.2, lon=-118.1, hae=1700.0, **kw):
    return {"class": "TPV", "mode": mode, "lat": lat, "lon": lon, "altHAE": hae, "eph": 3.0, **kw}


def test_averages_3d_fixes_and_ignores_2d():
    port = fake_gpsd(
        [{"class": "VERSION"}, tpv(2, lat=0.0), tpv(3, lat=34.0, hae=1700.0), tpv(3, lat=34.2, hae=1702.0)]
    )
    fix = gnss.read_fix("127.0.0.1", port, n=2, timeout=5)
    assert fix.n_fixes == 2
    assert abs(fix.latitude_deg - 34.1) < 1e-12 and abs(fix.height_m - 1701.0) < 1e-12


def test_uses_ellipsoidal_height_from_msl_plus_geoid_separation():
    m = tpv(3)
    del m["altHAE"]
    m.update(alt=1735.0, geoidSep=-35.0)
    fix = gnss.read_fix("127.0.0.1", fake_gpsd([m]), n=1, timeout=5)
    assert fix.height_m == 1700.0


def test_falls_back_to_the_cache_without_a_fix():
    first, how = gnss.site_fix("127.0.0.1", fake_gpsd([tpv(3)] * 10), timeout=5)
    assert how.startswith("gpsd")
    again, how = gnss.site_fix("127.0.0.1", fake_gpsd([tpv(1)] * 3), timeout=1)
    assert how.startswith("cached") and again.latitude_deg == first.latitude_deg


def test_no_fix_and_no_cache_is_an_error():
    with pytest.raises(gnss.GnssError, match="see the sky"):
        gnss.site_fix("127.0.0.1", fake_gpsd([tpv(1)]), timeout=1)
