# SPDX-License-Identifier: GPL-3.0-or-later
"""Plate solving: where a camera frame points on the sky (ICRS J2000).

Built-in solver (numpy only), for a camera that roughly knows where it points (the mount
says so, to within a few degrees) and its pixel scale (to ~10 %):
  1. detect stars: background from block medians, noise from the MAD, local maxima above a
     threshold, intensity-weighted centroids;
  2. take the catalogue stars (skycat, mag <= 8) in a cone around the hint, gnomonic
     projection about the hint;
  3. match triangles of the brightest image and catalogue stars by their shape (side ratios,
     which ignore rotation, mirroring and scale) and their size (the scale is roughly known);
     every matching pair of triangles votes for its three star pairs; the best-supported
     triangle pairs give affine transforms, and the one that brings most image stars onto
     catalogue stars wins;
  4. refine: re-centre the projection on the solution, match every detected star to its
     nearest catalogue star with a shrinking tolerance, and least-squares fit a TAN WCS
     (reference pixel at the image centre, a 2x2 CD matrix: scale, rotation, mirroring and the
     linear part of differential refraction).

ASTAP (external, optional): astap_cli on a FITS file, its .ini result parsed.

Pixel coordinates: x right, y down, (0, 0) the centre of the top-left pixel.
"""
from __future__ import annotations

import math
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

import numpy as np

from . import skycat

DEG = math.pi / 180
ARCSEC = DEG / 3600


@dataclass
class Solution:
    ok: bool
    message: str
    ra: float = math.nan            # rad, ICRS J2000: the sky at the image centre
    dec: float = math.nan
    cd: np.ndarray = field(default_factory=lambda: np.full((2, 2), np.nan))  # rad/px: (xi, eta) = cd @ (x - cx, y - cy)
    stars: int = 0                  # detected
    matched: int = 0
    rms_arcsec: float = math.nan
    solve_s: float = 0.0
    solver: str = "builtin"
    k1: float = 0.0                 # radial distortion: undistorted = measured * (1 + k1 (r / half-diagonal)^2)

    @property
    def arcsec_per_px(self) -> float:
        return math.sqrt(abs(np.linalg.det(self.cd))) / ARCSEC if self.ok else math.nan

    @property
    def mirrored(self) -> bool:
        return bool(np.linalg.det(self.cd) > 0) if self.ok else False  # sky (east left, north up) seen unmirrored has det < 0

    @property
    def roll_deg(self) -> float:
        """Position angle of the image's up direction (-y), from north through east."""
        if not self.ok:
            return math.nan
        up = -self.cd[:, 1]  # (xi east, eta north) of one pixel up
        return math.degrees(math.atan2(up[0], up[1])) % 360

    def summary(self) -> dict:
        return {"ok": self.ok, "message": self.message, "ra_deg": math.degrees(self.ra), "dec_deg": math.degrees(self.dec),
                "roll_deg": self.roll_deg, "arcsec_per_px": self.arcsec_per_px, "mirrored": self.mirrored,
                "stars": self.stars, "matched": self.matched, "rms_arcsec": self.rms_arcsec, "solve_s": self.solve_s,
                "solver": self.solver, "k1": self.k1}


# -- projection -------------------------------------------------------------------------------
def tan_project(ra0: float, dec0: float, ra, dec):
    """Gnomonic projection about (ra0, dec0): standard coordinates (xi east, eta north), rad;
    and cos of the angular distance (> 0 on the projected hemisphere)."""
    ra, dec = np.asarray(ra, float), np.asarray(dec, float)
    cd, sd, cd0, sd0 = np.cos(dec), np.sin(dec), math.cos(dec0), math.sin(dec0)
    da = ra - ra0
    den = sd * sd0 + cd * cd0 * np.cos(da)
    with np.errstate(divide="ignore", invalid="ignore"):
        xi = cd * np.sin(da) / den
        eta = (sd * cd0 - cd * sd0 * np.cos(da)) / den
    return xi, eta, den


def tan_deproject(ra0: float, dec0: float, xi, eta):
    xi, eta = np.asarray(xi, float), np.asarray(eta, float)
    cd0, sd0 = math.cos(dec0), math.sin(dec0)
    den = cd0 - eta * sd0
    ra = ra0 + np.arctan2(xi, den)
    dec = np.arctan2(sd0 + eta * cd0, np.hypot(xi, den))
    return np.mod(ra, 2 * math.pi), dec


# -- star detection ---------------------------------------------------------------------------
def background(img: np.ndarray, block: int = 64) -> np.ndarray:
    h, w = img.shape
    bh, bw = max(h // block, 1), max(w // block, 1)
    core = img[: bh * block, : bw * block] if h >= block and w >= block else img
    if h >= block and w >= block:
        med = np.median(core.reshape(bh, block, bw, block).swapaxes(1, 2).reshape(bh, bw, -1), axis=-1)
    else:
        med = np.full((1, 1), np.median(img))
    # Bilinear interpolation of the block medians to every pixel.
    ys = (np.arange(h) + 0.5) / block - 0.5
    xs = (np.arange(w) + 0.5) / block - 0.5
    y0 = np.clip(np.floor(ys).astype(int), 0, med.shape[0] - 1)
    x0 = np.clip(np.floor(xs).astype(int), 0, med.shape[1] - 1)
    y1, x1 = np.minimum(y0 + 1, med.shape[0] - 1), np.minimum(x0 + 1, med.shape[1] - 1)
    fy = np.clip(ys - y0, 0, 1)[:, None]
    fx = np.clip(xs - x0, 0, 1)[None, :]
    top = med[y0][:, x0] * (1 - fx) + med[y0][:, x1] * fx
    bot = med[y1][:, x0] * (1 - fx) + med[y1][:, x1] * fx
    return top * (1 - fy) + bot * fy


def detect_stars(img: np.ndarray, max_stars: int = 80, nsigma: float = 5.0, box: int = 3) -> tuple[np.ndarray, float]:
    """Stars in a mono frame: (N, 3) array of x, y, flux (background-subtracted), brightest
    first; and the noise (1 sigma, ADU)."""
    a = np.asarray(img, dtype=np.float32)
    d = a - background(a)
    sample = d[::3, ::3]
    noise = float(1.4826 * np.median(np.abs(sample - np.median(sample)))) or 1.0
    thr = nsigma * noise
    h, w = d.shape
    e = box + 1
    c = d[e:-e, e:-e]
    m = c > thr
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy or dx:
                m &= c >= d[e + dy:h - e + dy, e + dx:w - e + dx]
    # A star spreads over neighbours; a hot pixel does not.
    nb = (d[e - 1:h - e - 1, e:-e] + d[e + 1:h - e + 1, e:-e] + d[e:-e, e - 1:w - e - 1] + d[e:-e, e + 1:w - e + 1]) / 4
    m &= nb > 0.12 * c
    ys, xs = np.nonzero(m)
    if len(ys) == 0:
        return np.zeros((0, 3)), noise
    order = np.argsort(-c[ys, xs])[: max_stars * 3]
    ys, xs = ys[order] + e, xs[order] + e
    out = []
    taken = np.zeros(len(ys), bool)
    for i, (y, x) in enumerate(zip(ys, xs)):
        if taken[i]:
            continue
        near = (np.abs(ys - y) <= box) & (np.abs(xs - x) <= box)
        taken |= near
        win = d[y - box:y + box + 1, x - box:x + box + 1]
        wpos = np.clip(win, 0, None)
        s = float(wpos.sum())
        if s <= 0:
            continue
        gy, gx = np.mgrid[-box:box + 1, -box:box + 1]
        out.append((x + float((gx * wpos).sum()) / s, y + float((gy * wpos).sum()) / s, float(win.sum())))
        if len(out) >= max_stars:
            break
    arr = np.array(out, float)
    return arr[np.argsort(-arr[:, 2])], noise


# -- triangle matching ------------------------------------------------------------------------
def _triangles(p: np.ndarray, max_side: float, min_side: float):
    """All triangles of points p (N, 2): vertex indices ordered (opposite the shortest,
    middle, longest side), invariants (short/long, middle/long) and the longest side."""
    n = len(p)
    if n < 3:
        return np.zeros((0, 3), int), np.zeros((0, 2)), np.zeros(0)
    idx = np.array(list(combinations(range(n), 3)), int)
    a, b, c = p[idx[:, 0]], p[idx[:, 1]], p[idx[:, 2]]
    # side k is opposite vertex k
    s = np.stack([np.linalg.norm(b - c, axis=1), np.linalg.norm(a - c, axis=1), np.linalg.norm(a - b, axis=1)], axis=1)
    order = np.argsort(s, axis=1)
    s_sorted = np.take_along_axis(s, order, axis=1)
    keep = (s_sorted[:, 2] <= max_side) & (s_sorted[:, 0] >= min_side)
    verts = np.take_along_axis(idx, order, axis=1)[keep]
    s_sorted = s_sorted[keep]
    inv = s_sorted[:, :2] / s_sorted[:, 2:3]
    return verts, inv, s_sorted[:, 2]


def _affine(src: np.ndarray, dst: np.ndarray, w: np.ndarray | None = None) -> np.ndarray:
    """Least-squares affine dst = A @ [src, 1] (A is 2 x 3)."""
    x = np.hstack([src, np.ones((len(src), 1))])
    if w is not None:
        x, dst = x * w[:, None], dst * w[:, None]
    sol, *_ = np.linalg.lstsq(x, dst, rcond=None)
    return sol.T


def _apply(a: np.ndarray, p: np.ndarray) -> np.ndarray:
    return p @ a[:, :2].T + a[:, 2]


def _nearest(a: np.ndarray, b: np.ndarray):
    """For each point of a: index of and distance to the nearest point of b."""
    d = np.linalg.norm(a[:, None, :] - b[None, :, :], axis=-1)
    j = np.argmin(d, axis=1)
    return j, d[np.arange(len(a)), j]


class IcrsFrame:
    """Solve in ICRS: catalogue positions (with proper motion to the year) as they are."""

    def __init__(self, cat: skycat.Stars, year: float):
        self._ra, self._dec = cat.at_epoch(year)

    def stars(self, idx):
        return self._ra[idx], self._dec[idx]

    def from_icrs(self, ra, dec):
        return ra, dec

    def to_icrs(self, lon, lat):
        return lon, lat


def solve(img: np.ndarray, hint_ra: float, hint_dec: float, scale_rad_px: float, search_radius: float = 5 * DEG,
          scale_tol: float = 0.12, year: float | None = None, stars: skycat.Stars | None = None,
          detections: tuple[np.ndarray, float] | None = None, frame=None, fit_distortion: bool = True,
          k1: float = 0.0) -> Solution:
    """Solve a mono frame. hint_*: rad (ICRS); search_radius: how far off the hint may be.

    frame: the coordinates the image is a gnomonic projection of (default ICRS). The widefield
    camera passes observed az/el (widefield.ObservedFrame): refraction then is in the catalogue
    positions, not a distortion of the field, so the centre stays accurate near the horizon.
    The result is always ICRS.

    Lens distortion: radial, undistorted = measured * (1 + k1 (r / R)^2) about the image centre,
    R the half-diagonal (k1 = 0.01: 1 % at the corners). k1 is the starting value; with
    fit_distortion it is fitted once 20+ stars match (a machine-vision lens' 0.5-1 % is several
    arcminutes at the edge of a wide field, which a plain TAN fit would leave as residuals)."""
    t0 = time.monotonic()
    cat = stars or skycat.stars()
    year = year if year is not None else 2000.0 + (time.time() - 946728000) / (365.25 * 86400)
    frame = frame or IcrsFrame(cat, year)
    h, w = img.shape[:2]
    cx, cy = (w - 1) / 2, (h - 1) / 2
    det, _noise = detections or detect_stars(img)
    n_det = len(det)

    def fail(msg: str) -> Solution:
        return Solution(False, msg, stars=n_det, solve_s=time.monotonic() - t0)

    if n_det < 6:
        return fail(f"no solution: {n_det} stars detected (cloud, daylight, focus or exposure?)")
    half_diag = 0.5 * math.hypot(w, h) * scale_rad_px
    radius = half_diag + search_radius
    in_cone = cat.cone(hint_ra, hint_dec, radius + 0.5 * DEG, year=year)  # brightest first
    if len(in_cone) < 6:
        return fail("no solution: too few catalogue stars around the hint")
    lon, lat = (np.asarray(v, float) for v in frame.stars(in_cone))
    h_lon, h_lat = (float(v) for v in frame.from_icrs(hint_ra, hint_dec))
    # Image stars, in pixels about the centre; catalogue stars, in pixels at the nominal scale.
    n_img = min(n_det, 22)
    pi = det[:n_img, :2] - (cx, cy)
    area_ratio = (math.pi * radius ** 2) / (w * h * scale_rad_px ** 2)
    n_cat = int(min(len(in_cone), max(40, 1.6 * n_img * area_ratio), 160))
    xi, eta, _ = tan_project(h_lon, h_lat, lon[:n_cat], lat[:n_cat])
    pc = np.stack([xi, eta], axis=1) / scale_rad_px
    diag_px = math.hypot(w, h)
    ti, inv_i, len_i = _triangles(pi, diag_px, 0.04 * diag_px)
    tc, inv_c, len_c = _triangles(pc, diag_px * (1 + scale_tol), 0.04 * diag_px * (1 - scale_tol))
    if not len(ti) or not len(tc):
        return fail("no solution: too few stars for triangles")
    # Candidate triangle pairs: similar shape and size.
    order = np.argsort(inv_c[:, 0])
    inv_cs, len_cs, tcs = inv_c[order], len_c[order], tc[order]
    eps = 0.012
    lo = np.searchsorted(inv_cs[:, 0], inv_i[:, 0] - eps)
    hi = np.searchsorted(inv_cs[:, 0], inv_i[:, 0] + eps)
    pairs_i, pairs_c = [], []
    for k in range(len(ti)):
        if hi[k] <= lo[k]:
            continue
        j = np.arange(lo[k], hi[k])
        ok = (np.abs(inv_cs[j, 1] - inv_i[k, 1]) < eps) & (np.abs(len_cs[j] / len_i[k] - 1) < scale_tol)
        j = j[ok]
        pairs_i.extend([k] * len(j))
        pairs_c.extend(j.tolist())
    if not pairs_i:
        return fail("no solution: no star pattern matched the catalogue")
    pairs_i, pairs_c = np.array(pairs_i), np.array(pairs_c)
    # Votes for star correspondences; the best-supported triangle pairs are tried as solutions.
    votes = np.zeros((n_img, n_cat), int)
    vi, vc = ti[pairs_i], tcs[pairs_c]
    np.add.at(votes, (vi.ravel(), vc.ravel()), 1)
    support = votes[vi, vc].sum(axis=1)
    tol = max(5.0, 0.005 * diag_px)
    best = (0, None)
    for q in np.argsort(-support)[:250]:
        a = _affine(pi[vi[q]], pc[vc[q]])
        s = math.sqrt(abs(np.linalg.det(a[:, :2])))
        if abs(s - 1) > scale_tol:
            continue
        _j, dist = _nearest(_apply(a, pi), pc)
        n = int((dist < tol).sum())
        if n > best[0]:
            best = (n, a)
    if best[1] is None or best[0] < 5:
        return fail(f"no solution: best pattern matched {best[0]} stars")
    # Refine: every detected star, every catalogue star in the field, the projection re-centred
    # on the solution each pass. Detected stars go to the new plane through the sky (the axes of
    # the tangent plane turn as its centre moves, a lot near the pole).
    a, c_lon, c_lat = best[1], h_lon, h_lat
    all_pix = det[:, :2] - (cx, cy)
    r2 = (all_pix ** 2).sum(axis=1, keepdims=True) / (0.5 * diag_px) ** 2
    undist = lambda k: all_pix * (1 + k * r2)  # noqa: E731
    matched, rms_px = 0, math.inf
    for tol_px in (max(8.0, 0.006 * diag_px), 4.0, 2.5, 2.0, 2.0):
        s_lon, s_lat = tan_deproject(c_lon, c_lat, *(_apply(a, undist(k1)) * scale_rad_px).T)
        n_lon, n_lat = (float(v) for v in tan_deproject(c_lon, c_lat, *(a[:, 2] * scale_rad_px)))
        c_lon, c_lat = n_lon, n_lat
        pxi, peta, _ = tan_project(c_lon, c_lat, s_lon, s_lat)
        pred = np.stack([pxi, peta], axis=1) / scale_rad_px
        xi, eta, den = tan_project(c_lon, c_lat, lon, lat)
        infield = den > math.cos(half_diag * 1.05)
        pcat = np.stack([xi[infield], eta[infield]], axis=1) / scale_rad_px
        if len(pcat) < 6:
            return fail("no solution: too few catalogue stars in the field")
        j, dist = _nearest(pred, pcat)
        good = dist < tol_px
        uj, first = np.unique(j[good], return_index=True)  # one catalogue star per image star
        gi = np.flatnonzero(good)[first]
        if len(gi) < 6:
            return fail(f"no solution: only {len(gi)} stars matched after refinement")
        a = _affine(undist(k1)[gi], pcat[j[gi]])
        if fit_distortion and len(gi) >= 20:
            # Affine part and k1 together (k1 alone would be half absorbed by the scale):
            # pcat = L p + t + k1 L (p r^2), linear with L held in the k1 column; iterate.
            p, q, pr = all_pix[gi], pcat[j[gi]], all_pix[gi] * r2[gi]
            for _ in range(3):
                g = pr @ a[:, :2].T
                n = len(p)
                m = np.zeros((2 * n, 7))
                m[0::2, 0:2], m[0::2, 2] = p, 1.0
                m[1::2, 3:5], m[1::2, 5] = p, 1.0
                m[0::2, 6], m[1::2, 6] = g[:, 0], g[:, 1]
                x = np.linalg.lstsq(m, q.ravel(), rcond=None)[0]
                k1 = float(np.clip(x[6], -0.2, 0.2))
                a = _affine(undist(k1)[gi], q)
        res = _apply(a, undist(k1)[gi]) - pcat[j[gi]]
        matched, rms_px = len(gi), float(np.sqrt((res ** 2).sum(axis=1).mean()))
        n_cat_field = len(pcat)
    # The image centre, and the CD matrix about it.
    c_lon, c_lat = (float(v) for v in tan_deproject(c_lon, c_lat, *(a[:, 2] * scale_rad_px)))
    cd = a[:, :2] * scale_rad_px
    need = max(8, int(0.25 * min(n_det, n_cat_field)))
    if matched < need or rms_px > 2.5:
        return fail(f"no solution: {matched} stars matched (need {need}), rms {rms_px:.1f} px")
    ra0, dec0 = (float(v) for v in frame.to_icrs(c_lon, c_lat))
    # CD in ICRS standard coordinates: map a few pixels about the centre through the frame.
    probe = np.array([[0.0, 0.0], [100.0, 0.0], [0.0, 100.0]])
    p_lon, p_lat = tan_deproject(c_lon, c_lat, *(probe @ cd.T).T)
    p_ra, p_dec = frame.to_icrs(np.asarray(p_lon), np.asarray(p_lat))
    q_xi, q_eta, _ = tan_project(ra0, dec0, np.asarray(p_ra, float), np.asarray(p_dec, float))
    cd_icrs = np.array([[q_xi[1] - q_xi[0], q_xi[2] - q_xi[0]], [q_eta[1] - q_eta[0], q_eta[2] - q_eta[0]]]) / 100.0
    scale = math.sqrt(abs(np.linalg.det(cd)))
    dist = f", distortion {100 * k1:+.2f} % at the corners" if fit_distortion and abs(k1) >= 5e-4 else ""
    return Solution(True, f"solved: {matched} of {n_det} stars, rms {rms_px * scale / ARCSEC:.1f}\"{dist}",
                    ra=ra0, dec=dec0, cd=cd_icrs, stars=n_det, matched=matched, rms_arcsec=rms_px * scale / ARCSEC,
                    solve_s=time.monotonic() - t0, k1=k1)


# -- ASTAP ------------------------------------------------------------------------------------
def astap_available(path: str = "astap_cli") -> str | None:
    p = Path(path)
    if p.is_file():
        return str(p)
    return shutil.which(path) or shutil.which("astap")


def solve_astap(fits: bytes, width: int, height: int, hint_ra: float, hint_dec: float, fov_h_deg: float,
                search_radius_deg: float = 10.0, astap: str = "astap_cli", timeout_s: float = 60.0,
                extra: list[str] | None = None) -> Solution:
    """Solve a FITS frame with ASTAP's command-line solver (hinted). ASTAP and its star
    database (e.g. D50 or W08) are installed separately; nothing is linked."""
    t0 = time.monotonic()
    exe = astap_available(astap)
    if exe is None:
        return Solution(False, f"ASTAP not found ({astap})", solver="astap")
    with tempfile.TemporaryDirectory(prefix="maxmount-astap-") as d:
        f = Path(d) / "frame.fits"
        f.write_bytes(fits)
        cmd = [exe, "-f", str(f), "-ra", f"{math.degrees(hint_ra) / 15:.6f}", "-spd", f"{math.degrees(hint_dec) + 90:.6f}",
               "-r", f"{search_radius_deg:g}", "-fov", f"{fov_h_deg:.4f}", "-z", "0", *(extra or [])]
        try:
            subprocess.run(cmd, capture_output=True, timeout=timeout_s, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            return Solution(False, f"ASTAP failed: {e}", solver="astap", solve_s=time.monotonic() - t0)
        ini = f.with_suffix(".ini")
        if not ini.exists():
            return Solution(False, "ASTAP wrote no result", solver="astap", solve_s=time.monotonic() - t0)
        return parse_astap_ini(ini.read_text(errors="replace"), width, height, time.monotonic() - t0)


def parse_astap_ini(text: str, width: int, height: int, solve_s: float = 0.0) -> Solution:
    kv = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            kv[k.strip().upper()] = v.strip().strip("'").strip()
    if kv.get("PLTSOLVD", "F").upper() not in ("T", "TRUE"):
        return Solution(False, f"no solution (ASTAP: {kv.get('WARNING') or kv.get('ERROR') or 'not solved'})",
                        solver="astap", solve_s=solve_s)
    try:
        ra, dec = float(kv["CRVAL1"]) * DEG, float(kv["CRVAL2"]) * DEG
        crpix = np.array([float(kv["CRPIX1"]), float(kv["CRPIX2"])])
        # FITS CD: pixel offsets (column, row) -> intermediate coordinates (xi east, eta north), deg.
        cd_f = np.array([[float(kv["CD1_1"]), float(kv["CD1_2"])], [float(kv["CD2_1"]), float(kv["CD2_2"])]]) * DEG
    except (KeyError, ValueError) as e:
        return Solution(False, f"ASTAP result incomplete: {e}", solver="astap", solve_s=solve_s)
    # FITS pixel (i, j) is 1-based with j counting the stored rows; our (x, y) are 0-based with y
    # counting the same rows (array order), so the axes agree and only the origin differs.
    cd = cd_f
    centre_fits = np.array([(width + 1) / 2, (height + 1) / 2])
    d = cd_f @ (centre_fits - crpix)
    ra_c, dec_c = tan_deproject(ra, dec, d[0], d[1])
    return Solution(True, "solved by ASTAP", ra=float(ra_c), dec=float(dec_c), cd=cd, matched=int(float(kv.get("STARS", 0) or 0)),
                    rms_arcsec=math.nan, solve_s=solve_s, solver="astap")
