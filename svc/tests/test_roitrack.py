# SPDX-License-Identifier: GPL-3.0-or-later
"""ROI tracking: target detection (accuracy, gating, extended vs point sources, nothing found)
and the controller steering a ROI after a moving target with pipeline latency."""
import numpy as np

from maxmount_svc.roitrack import RoiTracker, measure


def blob(h, w, x, y, peak, sx=2.0, sy=2.0):
    yy, xx = np.mgrid[0:h, 0:w]
    return peak * np.exp(-((xx - x) ** 2 / (2 * sx**2) + (yy - y) ** 2 / (2 * sy**2)))


def noisy(img, rng, bg=20.0, sigma=3.0):
    return np.clip(img + bg + rng.normal(0, sigma, img.shape), 0, 255).astype(np.uint8)


def test_measure_accuracy_and_nothing_found():
    rng = np.random.default_rng(1)
    for x, y in ((100.3, 80.7), (37.0, 200.2), (250.5, 30.1)):
        f = noisy(blob(240, 320, x, y, 150, 3, 2), rng)
        d = measure(f)
        assert d is not None and abs(d.x - x) < 0.5 and abs(d.y - y) < 0.5 and d.snr > 20
    assert measure(noisy(np.zeros((240, 320)), rng)) is None
    assert measure(np.zeros((4, 4), np.uint8)) is None  # too small


def test_extended_target_beats_a_star_and_gating_holds_the_lock():
    rng = np.random.default_rng(2)
    sat = blob(240, 320, 200, 120, 90, 12, 4)    # extended, fainter peak
    star = blob(240, 320, 60, 60, 200, 1.2, 1.2)  # point, brighter peak
    f = noisy(sat + star, rng)
    d = measure(f)
    assert abs(d.x - 200) < 1 and abs(d.y - 120) < 1
    # A much brighter object outside the gate does not steal a lock.
    f = noisy(blob(240, 320, 200, 120, 60, 3, 3) + blob(240, 320, 60, 60, 250, 6, 6), rng)
    assert abs(measure(f).x - 60) < 1
    d = measure(f, near=(195, 118), gate=30)
    assert abs(d.x - 200) < 1 and abs(d.y - 120) < 1
    # RGB frames work too.
    rgb = np.repeat(noisy(sat, rng)[..., None], 3, -1)
    assert abs(measure(rgb).x - 200) < 1


def test_controller_follows_a_moving_target_with_latency():
    """A 128 x 128 ROI on a 1000 x 800 sensor; the target moves at 120 px/s; ROI moves take
    effect 2 frames later. The ROI stays on the target."""
    rng = np.random.default_rng(3)
    tr = RoiTracker(latency_frames=2)
    tr.start()
    w = h = 128
    center = np.array([500.0, 400.0])
    hist = [(0, 436, 336)]  # (first frame, x, y)
    errs = []
    dt = 1 / 60
    for k in range(1, 300):
        t = k * dt
        tx, ty = 500 + 120 * t, 400 + 60 * np.sin(t)
        ox, oy = next((x, y) for first, x, y in reversed(hist) if k >= first)
        f = noisy(blob(h, w, tx - ox, ty - oy, 120, 4, 3), rng)
        new = tr.update(f, (ox, oy), 1, int(t * 1e9), tuple(center), dt)
        if new is not None:
            center = np.array(new)
            hist.append((k + 1 + 2, int(round((new[0] - 63.5) / 2)) * 2, int(round((new[1] - 63.5) / 2)) * 2))
        if k > 60:
            errs.append(np.hypot(tx - (ox + 63.5), ty - (oy + 63.5)))
    assert tr.state == "locked" and tr.moves > 20
    assert max(errs) < 12 and np.median(errs) < 5
    assert abs(tr.vel[0] - 120) < 15  # the filter learned the speed
    # The target vanishes: lost after lost_after frames, and no ROI moves meanwhile.
    for k in range(300, 300 + tr.lost_after):
        assert tr.update(noisy(np.zeros((h, w)), rng), hist[-1][1:], 1, int(k * dt * 1e9), tuple(center), dt) is None
    assert tr.state == "lost"
    st = tr.status(hist[-1][1:], (w, h), 1)
    assert st["track_state"] == "lost" and st["roi_tracking"]


def test_pick_acquires_the_chosen_object():
    rng = np.random.default_rng(4)
    f = noisy(blob(240, 320, 250, 60, 200, 4, 4) + blob(240, 320, 50, 180, 80, 4, 4), rng)
    tr = RoiTracker()
    tr.start(pick=(1000 + 55, 500 + 175))  # sensor px; the ROI origin is (1000, 500)
    tr.update(f, (1000, 500), 1, 1, (1160, 620), 0.02)
    assert tr.state == "locked" and abs(tr.last.x - 50) < 1 and abs(tr.last.y - 180) < 1
