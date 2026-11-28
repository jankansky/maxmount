# SPDX-License-Identifier: GPL-3.0-or-later
"""Gateway requests for the GUI's Sky chart screen (they run in the gateway's worker threads):

    sky_frame     the ICRS -> east/north/up rotation, refraction constants, Sun/Moon/planets
    sky_search    stars, deep-sky objects and planets by name or catalogue id
    sky_object    altitude/azimuth now and rise/transit/set above the horizon mask (next 24 h)
    sky_tonight   deep-sky objects (and planets) well placed during the coming dark hours
"""
from __future__ import annotations

import math
import threading
import time

import erfa
import numpy as np

from . import astro, skycat, skyframe
from .config import SystemSettings

NS = 1_000_000_000
DEG = math.pi / 180


class SkyOps:
    def __init__(self, settings: SystemSettings):
        self.settings = settings
        self._lock = threading.Lock()
        self._bodies: tuple[float, list] | None = None
        self._cache = skyframe.Cache()

    def handle(self, op: str, args: dict):
        site = self.settings.site
        if site is None:
            return False, "no site location: the sky chart needs one (system file or GNSS)", None
        fn = {"sky_frame": self._frame, "sky_search": self._search, "sky_object": self._object,
              "sky_tonight": self._tonight}.get(op)
        if fn is None:
            return False, f"unknown op '{op}'", None
        return fn(site, args)

    # -- frame and solar system -----------------------------------------------------------------
    def bodies(self, site, R=None) -> list[dict]:
        """Sun, Moon and planets, recomputed every 5 s (the Moon moves ~2.5" in that time)."""
        with self._lock:
            if self._bodies is None or time.monotonic() - self._bodies[0] > 5:
                self._bodies = (time.monotonic(), skyframe.solar_system(time.time_ns(), site, R))
            return self._bodies[1]

    def _frame(self, site, _args):
        t = time.time_ns()
        R = skyframe.frame_matrix(t, site)
        refa, refb = skyframe.refraction_constants(site)
        u1, u2 = astro.utc_jd(np.int64(t))
        ut1 = erfa.utcut1(u1, u2, site.dut1_s)
        tt = astro.tt_jd(np.int64(t))
        lst = (erfa.gst06a(ut1[0], ut1[1], tt[0], tt[1]) + math.radians(site.longitude_deg)) % (2 * math.pi)
        return True, "sky frame", {
            "t_ns": t, "R": [round(float(x), 12) for x in R.flatten()], "refa": refa, "refb": refb,
            "lat_deg": site.latitude_deg, "lst_h": lst * 12 / math.pi,
            "mask_deg": self.settings.tracking.horizon_min_el_deg, "bodies": self.bodies(site, R),
        }

    # -- search ---------------------------------------------------------------------------------
    def _search(self, site, args):
        q = str(args.get("q", "")).strip()
        hits = skyframe.search(q, int(args.get("limit", 20)))
        pos = {b["body"]: b for b in self.bodies(site)}
        for h in hits:
            if h["kind"] == "body":
                b = pos[h["body"]]
                h.update(ra_deg=b["ra_deg"], dec_deg=b["dec_deg"], mag=b["mag"])
        return True, f"{len(hits)} match{'es' if len(hits) != 1 else ''} for '{q}'", {"results": hits}

    # -- one object -----------------------------------------------------------------------------
    def _object(self, site, args):
        """{ra_deg, dec_deg} (ICRS J2000) or {body}: where it is now, and when it is up."""
        mask = self.settings.tracking.horizon_min_el_deg
        t, Rs = self._cache.grid(site, 24, 300)
        now = time.time_ns()
        body = args.get("body")
        if body:
            target = skyframe.BodyTarget(site, str(body))
            az, el = target.observed(np.int64(now))
            _a, alt, _d = skyframe.body_topocentric(target.body, t, site)
        else:
            ra, dec = float(args["ra_deg"]) * DEG, float(args["dec_deg"]) * DEG
            az, el = astro.observed_from_icrs(np.int64(now), site, ra, dec)
            alt = skyframe.fixed_altitudes(Rs, args["ra_deg"], args["dec_deg"])[0]
        ev = skyframe.events(t, alt, mask * DEG)
        sun_alt = skyframe.fixed_altitudes(Rs, *self._sun_radec(site))[0]
        dark = skyframe.darkness(t, sun_alt, -12.0)
        return True, "ok", {"az_deg": math.degrees(float(az)), "el_deg": math.degrees(float(el)), "mask_deg": mask,
                            **ev, "dark": dark and {"start_ns": dark[0], "end_ns": dark[1]}}

    def _sun_radec(self, site):
        s = next(b for b in self.bodies(site) if b["body"] == "sun")
        return s["ra_deg"], s["dec_deg"]

    # -- tonight ---------------------------------------------------------------------------------
    def _tonight(self, site, args):
        """Deep-sky objects above min_alt_deg for at least min_hours during the next dark period
        (Sun below sun_alt_deg), filtered by type, magnitude and size; sorted by `sort`:
        "alt" (highest altitude reached in the dark), "size", "mag" or "time" (when highest)."""
        min_alt = float(args.get("min_alt_deg", 30))
        sun_lim = float(args.get("sun_alt_deg", -12))
        types = set(args.get("types") or [])
        mag_max = args.get("mag_max")
        min_size = float(args.get("min_size_arcmin", 0) or 0)
        min_hours = float(args.get("min_hours", 0.5))
        sort = str(args.get("sort", "alt"))
        limit = int(args.get("limit", 150))
        t, Rs = self._cache.grid(site, 24, 600)
        sun_alt = skyframe.fixed_altitudes(Rs, *self._sun_radec(site))[0]
        dark = skyframe.darkness(t, sun_alt, sun_lim)
        if dark is None:
            return True, f"no darkness (Sun below {sun_lim:g}°) in the next 24 h", {"objects": [], "dark": None}
        sel = (t >= dark[0]) & (t <= dark[1])
        tw = t[sel]
        objs = [o for o in skycat.dso()
                if (not types or o["type"] in types)
                and (mag_max is None or o["mag"] is None or o["mag"] <= float(mag_max))
                and o["maj"] >= min_size]
        out = []
        step_h = (t[1] - t[0]) / NS / 3600
        if objs:
            alt = np.degrees(skyframe.fixed_altitudes(Rs[sel], [o["ra"] for o in objs], [o["dec"] for o in objs]))
            alt_now = np.degrees(skyframe.fixed_altitudes(Rs[:1], [o["ra"] for o in objs], [o["dec"] for o in objs]))[:, 0]
            hours = (alt >= min_alt).sum(axis=1) * step_h
            kmax = alt.argmax(axis=1)
            for i, o in enumerate(objs):
                if hours[i] < min_hours:
                    continue
                out.append({"kind": "dso", "name": o["id"], **{k: o[k] for k in ("alt", "common", "type", "mag", "maj", "min", "pa", "con")},
                            "ra_deg": o["ra"], "dec_deg": o["dec"], "max_alt_deg": round(float(alt[i, kmax[i]]), 1),
                            "best_ns": int(tw[kmax[i]]), "hours_up": round(float(hours[i]), 1), "alt_now_deg": round(float(alt_now[i]), 1)})
        if args.get("planets", True) and (not types or "planet" in types):
            for b in self.bodies(site):
                if b["body"] in ("sun", "moon"):
                    continue
                a = np.degrees(skyframe.fixed_altitudes(Rs[sel], b["ra_deg"], b["dec_deg"])[0])
                h = (a >= min_alt).sum() * step_h
                if h >= min_hours and (mag_max is None or b["mag"] <= float(mag_max)):
                    k = int(a.argmax())
                    out.append({"kind": "body", "body": b["body"], "name": b["name"], "type": "planet", "mag": b["mag"],
                                "maj": b["diameter_arcsec"] / 60, "min": b["diameter_arcsec"] / 60, "ra_deg": b["ra_deg"],
                                "dec_deg": b["dec_deg"], "max_alt_deg": round(float(a[k]), 1), "best_ns": int(tw[k]),
                                "hours_up": round(float(h), 1), "alt_now_deg": round(float(b["el_deg"]), 1), "common": ""})
        key = {"size": lambda o: -o["maj"], "mag": lambda o: o["mag"] if o["mag"] is not None else 99,
               "time": lambda o: o["best_ns"]}.get(sort, lambda o: -o["max_alt_deg"])
        out.sort(key=key)
        moon = next(b for b in self.bodies(site) if b["body"] == "moon")
        msg = (f"{len(out)} objects above {min_alt:g}° for ≥ {min_hours:g} h while the Sun is below {sun_lim:g}°"
               f" · Moon {round(moon['phase'] * 100)}% lit")
        return True, msg, {"objects": out[:limit], "total": len(out), "dark": {"start_ns": dark[0], "end_ns": dark[1]},
                           "moon_phase": moon["phase"]}
