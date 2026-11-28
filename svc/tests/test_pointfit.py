# SPDX-License-Identifier: GPL-3.0-or-later
import math

import numpy as np
import pytest

from maxmount_svc import pointfit
from maxmount_svc.pointing import PointingModel

DEG = math.pi / 180
ARCSEC = DEG / 3600
TRUE = {"IA": 420.0, "IE": -300.0, "CA": 150.0, "NPAE": 40.0, "AN": 90.0, "AW": -60.0, "TF": 30.0}


def points(n=40, flipped=0, seed=2, noise=3.0, model=TRUE):
    rng = np.random.default_rng(seed)
    az = rng.uniform(0, 2 * math.pi, n)
    el = rng.uniform(15 * DEG, 80 * DEG, n)
    a_i, e_i = az.copy(), el.copy()
    k = rng.choice(n, flipped, replace=False) if flipped else []
    a_i[k], e_i[k] = az[k] + math.pi, math.pi - el[k]
    m = PointingModel(model)
    a_c, e_c = m.apply(a_i, e_i)
    a_c += rng.normal(0, noise * ARCSEC, n) / np.abs(np.cos(e_i))
    e_c += rng.normal(0, noise * ARCSEC, n)
    return a_i, e_i, a_c - a_i, e_c - e_i


def test_fit_recovers_a_known_model_with_an_outlier():
    a, e, da, de = points()
    de[5] += 600 * ARCSEC  # a bad solve
    f = pointfit.fit(a, e, da, de)
    assert not f.used[5] and f.used.sum() == len(a) - 1
    for t, v in TRUE.items():
        assert f.terms[t] == pytest.approx(v, abs=max(4 * f.sigma.get(t, 10), 10)), (t, f.terms, f.sigma)
    assert f.rms_after < 6 and f.rms_before > 300
    assert "BE" in f.dropped  # no flipped points: not constrained


def test_flipped_points_constrain_be_and_residual_signs():
    model = dict(TRUE, BE=-80.0)
    a, e, da, de = points(n=50, flipped=15, model=model)
    f = pointfit.fit(a, e, da, de)
    assert f.terms["BE"] == pytest.approx(-80, abs=10)
    # The on-sky error of an IE-only error: the boresight is IE low on the normal side
    # (commanded = ideal + IE) and IE high on the flipped side; both mean the true point is
    # below/above where believed by the same sky amount.
    x, y = pointfit.sky_error(np.array([0.5, math.pi - 0.5]), np.zeros(2), np.full(2, 10 * ARCSEC))
    assert y[0] == pytest.approx(-10) and y[1] == pytest.approx(10) and np.allclose(x, 0)


def test_site_mode_keeps_structural_terms():
    a, e, da, de = points(model=dict(TRUE, IA=100.0, AN=-30.0))
    cur = PointingModel({k: v for k, v in TRUE.items() if k not in pointfit.SITE_TERMS})
    f = pointfit.fit(a, e, da, de, mode="site", current=cur)
    assert set(f.fitted) <= set(pointfit.SITE_TERMS)
    assert f.terms["CA"] == TRUE["CA"] and f.terms["IA"] == pytest.approx(100, abs=10)
    assert f.terms["AN"] == pytest.approx(-30, abs=10)


def test_inverse_and_model_files(tmp_path, monkeypatch):
    m = PointingModel(TRUE)
    a, e = np.array([0.3, 4.0]), np.array([0.4, 1.2])
    ai, ei = pointfit.ideal_from_commanded(m, *m.apply(a, e))
    assert np.allclose(ai, a, atol=1e-10) and np.allclose(ei, e, atol=1e-10)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    class S:
        name, pointing_file = "t", None

    a, e, da, de = points()
    f = pointfit.fit(a, e, da, de)
    doc = pointfit.save_model(S, f, len(a))
    assert pointfit.model_path(S).exists() and pointfit.load_model(S).coefficients["IA"] == pytest.approx(TRUE["IA"], abs=10)
    assert pointfit.history(S)[0]["version"] == doc["version"]
