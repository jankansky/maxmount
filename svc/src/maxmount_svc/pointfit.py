# SPDX-License-Identifier: GPL-3.0-or-later
"""Pointing model fitting (docs/design.md, "Automated pointing model").

A pointing point: the encoder (commanded) axis angles (A_c, E_c) when the widefield camera
took a frame, and where the frame really pointed, from a plate solve: ICRS RA/Dec at the
mid-exposure time. reduce() turns the solve into the ideal mechanical angles (A_i, E_i) of
that direction (ERFA: apparent place, Earth rotation, refraction; on the same side, normal or
flipped, as the encoders). PointingModel (pointing.py) says commanded = ideal + corrections,
so each point measures the corrections dA = A_c - A_i, dE = E_c - E_i at (A_i, E_i).

fit(): weighted least squares of the term coefficients (the terms are linear in them), with
the azimuth residual weighted by cos E so both are on-sky arcseconds; iterative 3-sigma
outlier rejection; then terms that are not significant (|value| < 2 sigma) are dropped and
the fit is repeated. IA and IE are always fitted. A term the points cannot constrain (no
flipped points for BE, say) is left out. "site" mode fits IA, IE, AN, AW only and keeps the
other terms at their present values.

Model files are YAML ({terms: {IA: ...}, sigma, version, date, points, rms_arcsec, mode});
apply() keeps every version in a history directory next to the active file.
"""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import yaml

from . import astro, kinematics
from .pointing import TERMS, PointingModel

ARCSEC = math.pi / (180 * 3600)
SITE_TERMS = ("IA", "IE", "AN", "AW")
ALWAYS = ("IA", "IE")


# -- the model's inverse and reduction ----------------------------------------------------------
def ideal_from_commanded(model: PointingModel, a_c, e_c, iterations: int = 5):
    """The ideal mechanical angles that the model turns into the commanded ones (fixed-point
    iteration; corrections are arcminutes, so a few iterations reach micro-arcseconds)."""
    a_c, e_c = np.asarray(a_c, float), np.asarray(e_c, float)
    a, e = a_c.copy(), e_c.copy()
    for _ in range(iterations):
        da, de = model.corrections(a, e)
        a, e = a_c - da, e_c - de
    return a, e


def reduce(site, t_ns: int, ra: float, dec: float, a_enc: float, e_enc: float) -> tuple[float, float, float, float]:
    """A solved ICRS direction at t_ns -> (A_i, E_i) ideal mechanical angles on the encoders'
    side (A_i unwrapped next to a_enc), and the observed (az, el), rad."""
    az, el = astro.observed_from_icrs(np.int64(t_ns), site, ra, dec)
    az, el = float(az), float(el)
    a_i, e_i = kinematics.to_mech(az, el, flipped=e_enc > math.pi / 2)
    a_i = float(a_enc + (float(a_i) - a_enc + math.pi) % (2 * math.pi) - math.pi)
    return a_i, float(e_i), az, el


def sky_error(e_i, da, de):
    """Pointing error on the sky (arcsec: cross-elevation, elevation) for an unmodelled
    correction (da, de; rad, mechanical): where the boresight really points minus where it
    was believed to point. On the flipped side, +A is still +azimuth but cos E < 0, and +E
    moves the boresight down."""
    e_i = np.asarray(e_i, float)
    sign = np.where(e_i > math.pi / 2, -1.0, 1.0)
    return -sign * np.asarray(da) * np.cos(e_i) / ARCSEC, -sign * np.asarray(de) / ARCSEC


def _to_sky(r: np.ndarray, e_i: np.ndarray) -> np.ndarray:
    """Stacked residuals [dA cos E..., dE...] (arcsec) -> (N, 2) on-sky pointing errors."""
    n = len(e_i)
    sign = np.where(e_i > math.pi / 2, -1.0, 1.0)
    return -np.stack([r[:n], r[n:]], axis=1) * sign[:, None]


# -- fitting ----------------------------------------------------------------------------------
def basis(a, e, terms=TERMS) -> np.ndarray:
    """Design matrix (2N x K): rows dA cos E (arcsec on the sky) then dE, per arcsec of each term."""
    a, e = np.asarray(a, float), np.asarray(e, float)
    cols = []
    for t in terms:
        da, de = PointingModel({t: 1.0}).corrections(a, e)
        cols.append(np.concatenate([da * np.cos(e), de]) / ARCSEC)
    return np.stack(cols, axis=1) if cols else np.zeros((2 * len(a), 0))


@dataclass
class Fit:
    terms: dict                    # all terms: fitted and kept (arcsec)
    sigma: dict                    # fitted terms: 1-sigma (arcsec)
    fitted: list                   # names of the fitted terms
    dropped: list                  # considered but not significant / not constrained
    rms_before: float              # on-sky arcsec, the model in use during the run
    rms_after: float
    used: np.ndarray               # per point: kept (not an outlier)
    res_before: np.ndarray         # (N, 2) on-sky pointing error, cross-el and el arcsec (sky_error)
    res_after: np.ndarray
    mode: str = "full"
    notes: list = field(default_factory=list)


def _rms(r: np.ndarray, mask: np.ndarray) -> float:
    return float(np.sqrt((r[mask] ** 2).sum(axis=1).mean())) if mask.any() else math.nan


def fit(a_i, e_i, da, de, mode: str = "full", current: PointingModel | None = None, weights=None,
        reject_sigma: float = 3.0, min_sigma_arcsec: float = 2.0, significance: float = 2.0) -> Fit:
    """Fit the pointing terms to measured corrections (all rad) at ideal angles (a_i, e_i)."""
    a_i, e_i, da, de = (np.asarray(v, float) for v in (a_i, e_i, da, de))
    n = len(a_i)
    current = current or PointingModel()
    cur = {t: float(current.coefficients.get(t, 0.0)) for t in TERMS}
    obs = np.concatenate([da * np.cos(e_i), de]) / ARCSEC
    w = np.ones(n) if weights is None else np.asarray(weights, float)
    candidates = list(SITE_TERMS if mode == "site" else TERMS)
    kept = {t: cur[t] for t in TERMS if t not in candidates}  # site mode: the structural terms stay
    b_all = basis(a_i, e_i, TERMS)
    fixed = b_all[:, [TERMS.index(t) for t in kept]] @ np.array(list(kept.values())) if kept else np.zeros(2 * n)
    y = obs - fixed
    pre = obs - b_all @ np.array([cur[t] for t in TERMS])
    res_before = _to_sky(pre, e_i)
    notes = []
    # Terms the points cannot constrain (an all-zero column, or one that duplicates another).
    b_c = basis(a_i, e_i, candidates)
    usable = [t for k, t in enumerate(candidates) if np.linalg.norm(b_c[:, k]) > 1e-6 * math.sqrt(2 * n)]
    for t in set(candidates) - set(usable):
        notes.append(f"{t}: not constrained by these points")
    terms = list(usable)
    used = np.ones(n, bool)

    def solve(terms, used):
        b = basis(a_i, e_i, terms)
        m = np.concatenate([used, used])
        ww = np.concatenate([w, w])[m]
        bw, yw = b[m] * ww[:, None], y[m] * ww
        coef, *_ = np.linalg.lstsq(bw, yw, rcond=None)
        r = y - b @ coef
        dof = max(int(m.sum()) - len(terms), 1)
        s2 = float((r[m] ** 2 * ww ** 2).sum() / dof)
        try:
            cov = np.linalg.inv(bw.T @ bw) * s2
            sig = np.sqrt(np.clip(np.diag(cov), 0, None))
        except np.linalg.LinAlgError:
            sig = np.full(len(terms), np.inf)
        return coef, sig, r

    if 2 * n < len(terms) + 2:
        raise ValueError(f"{n} points are too few for {len(terms)} terms")
    def reject(terms, used):
        """Iterate 3-sigma outlier rejection (sigma from the median residual) to convergence."""
        for _ in range(6):
            _coef, _sig, r = solve(terms, used)
            rr = np.hypot(r[:n], r[n:])
            scale = max(min_sigma_arcsec, 1.4826 * float(np.median(rr[used])) * 1.2)
            new = rr < reject_sigma * scale
            if new.sum() < max(len(terms) // 2 + 2, 3) or (new == used).all():
                return used
            used = new
        return used

    used = reject(terms, used)
    while True:  # drop the least significant term until all are significant (IA, IE stay)
        coef, sig, _r = solve(terms, used)
        weak = [(abs(c) / max(s, 1e-12), t) for t, c, s in zip(terms, coef, sig) if t not in ALWAYS and abs(c) < significance * s]
        if not weak:
            break
        worst = min(weak)[1]
        terms = [t for t in terms if t != worst]
        notes.append(f"{worst}: not significant, dropped")
        used = reject(terms, used)
    coef, sig, r = solve(terms, used)
    out = {t: 0.0 for t in TERMS}
    out.update(kept)
    out.update({t: float(c) for t, c in zip(terms, coef)})
    res_after = _to_sky(r, e_i)
    if (~used).any():
        notes.append(f"{int((~used).sum())} point(s) rejected as outliers")
    return Fit(terms=out, sigma={t: float(s) for t, s in zip(terms, sig)}, fitted=terms,
               dropped=[t for t in candidates if t not in terms], rms_before=_rms(res_before, used),
               rms_after=_rms(res_after, used), used=used, res_before=res_before, res_after=res_after, mode=mode, notes=notes)


# -- model files ------------------------------------------------------------------------------
def state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "maxmount"


def model_path(settings) -> Path:
    """The active pointing model file: the system file's `pointing:` file, or one in the state
    directory when the system file names none."""
    return Path(settings.pointing_file) if settings.pointing_file else state_dir() / f"{settings.name}-pointing.yaml"


def history_dir(settings) -> Path:
    return state_dir() / f"{settings.name}-pointing-history"


def load_model(settings) -> PointingModel:
    p = model_path(settings)
    return PointingModel.load(p) if p.exists() else PointingModel()


def read_info(path: Path) -> dict:
    try:
        doc = yaml.safe_load(Path(path).read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return doc if isinstance(doc, dict) else {}


def save_model(settings, f: Fit, n_points: int, extra: dict | None = None) -> dict:
    """Write the fit as the active model (atomically) and as a version in the history."""
    version = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    doc = {"version": version, "date": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()), "system": settings.name,
           "mode": f.mode, "points": int(n_points), "used": int(f.used.sum()),
           "rms_arcsec": round(f.rms_after, 2), "rms_before_arcsec": round(f.rms_before, 2),
           "terms": {t: round(float(v), 2) for t, v in f.terms.items() if v != 0.0},
           "sigma": {t: round(float(v), 2) for t, v in f.sigma.items()}, **(extra or {})}
    text = "# maxmount pointing model (pointfit.py); arcseconds, terms as in pointing.py\n" + yaml.safe_dump(doc, sort_keys=False)
    hist = history_dir(settings)
    hist.mkdir(parents=True, exist_ok=True)
    (hist / f"{version}.yaml").write_text(text)
    p = model_path(settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(text)
    tmp.replace(p)
    return doc


def history(settings, limit: int = 20) -> list[dict]:
    hist = history_dir(settings)
    out = [read_info(p) for p in sorted(hist.glob("*.yaml"), reverse=True)[:limit]] if hist.exists() else []
    return [d for d in out if d]
