#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Builds the offline sky catalogue in gui/vendor/sky/ from public sources:

    stars.json           HYG database v4.1 (CC BY-SA 4.0, astronexus): stars to mag 8.0
    dso.json             OpenNGC (CC BY-SA 4.0, Mattia Verga): Messier objects and NGC/IC
                         objects that are bright or large enough to matter for imaging
    constellations.json  d3-celestial (BSD-3-Clause, Olaf Frohn): constellation lines, names

The sky chart (GUI) and the plate solver / widefield simulator (mount-svc) both read these.
Positions are ICRS, epoch J2000; proper motions in mas/yr (RA one scaled by cos dec).

    python3 tools/build_skydata.py [--src DIR]    # DIR: cached downloads (fetched if missing)
"""
from __future__ import annotations

import argparse
import csv
import json
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "gui" / "vendor" / "sky"
SOURCES = {
    "hygdata_v41.csv": "https://raw.githubusercontent.com/astronexus/HYG-Database/main/hyg/CURRENT/hygdata_v41.csv",
    "NGC.csv": "https://raw.githubusercontent.com/mattiaverga/OpenNGC/master/database_files/NGC.csv",
    "addendum.csv": "https://raw.githubusercontent.com/mattiaverga/OpenNGC/master/database_files/addendum.csv",
    "constellations.lines.json": "https://raw.githubusercontent.com/ofrohn/d3-celestial/master/data/constellations.lines.json",
    "constellations.json": "https://raw.githubusercontent.com/ofrohn/d3-celestial/master/data/constellations.json",
}
STAR_MAG = 8.0
GREEK = dict(Alp="α", Bet="β", Gam="γ", Del="δ", Eps="ε", Zet="ζ", Eta="η", The="θ", Iot="ι", Kap="κ", Lam="λ",
             Mu="μ", Nu="ν", Xi="ξ", Omi="ο", Pi="π", Rho="ρ", Sig="σ", Tau="τ", Ups="υ", Phi="φ", Chi="χ",
             Psi="ψ", Ome="ω")
# OpenNGC types kept, with the chart's simpler classes.
DSO_TYPES = {"G": "galaxy", "GPair": "galaxy", "GTrpl": "galaxy", "GGroup": "galaxy", "OCl": "open cluster",
             "GCl": "globular cluster", "PN": "planetary nebula", "HII": "nebula", "EmN": "nebula", "Neb": "nebula",
             "RfN": "nebula", "Cl+N": "nebula", "SNR": "supernova remnant", "*Ass": "open cluster"}


def fetch(src: Path) -> None:
    src.mkdir(parents=True, exist_ok=True)
    for name, url in SOURCES.items():
        if not (src / name).exists():
            print("fetching", url)
            req = urllib.request.Request(url, headers={"User-Agent": "maxmount build_skydata"})
            with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 - fixed public URLs
                (src / name).write_bytes(r.read())


def designation(row: dict) -> str:
    con = row["con"]
    if row["bayer"]:
        b = row["bayer"]
        letters = b.rstrip("0123456789-")
        greek = GREEK.get(letters, letters)
        return f"{greek}{b[len(letters):].lstrip('-')} {con}"
    if row["flam"]:
        return f"{row['flam']} {con}"
    return ""


def stars(src: Path) -> dict:
    cols = {k: [] for k in ("ra", "dec", "mag", "bv", "pmra", "pmdec", "hip")}
    names = {}
    for row in csv.DictReader(open(src / "hygdata_v41.csv", newline="")):
        if row["proper"] == "Sol" or not row["mag"] or float(row["mag"]) > STAR_MAG:
            continue
        ra, dec = float(row["ra"]) * 15, float(row["dec"])
        i = len(cols["ra"])
        cols["ra"].append(round(ra, 4)); cols["dec"].append(round(dec, 4))
        cols["mag"].append(round(float(row["mag"]), 2))
        cols["bv"].append(round(float(row["ci"]), 2) if row["ci"] else 0.6)
        # HYG proper motions are mas/yr, RA already scaled by cos dec.
        cols["pmra"].append(round(float(row["pmra"] or 0))); cols["pmdec"].append(round(float(row["pmdec"] or 0)))
        cols["hip"].append(int(row["hip"]) if row["hip"] else 0)
        d = designation(row)
        if row["proper"] or (d and float(row["mag"]) <= 6.0):
            names[i] = [row["proper"], d]
    order = sorted(range(len(cols["ra"])), key=lambda i: cols["mag"][i])  # brightest first
    out = {k: [v[i] for i in order] for k, v in cols.items()}
    inv = {old: new for new, old in enumerate(order)}
    out["names"] = {str(inv[i]): n for i, n in names.items()}
    out["source"] = "HYG database v4.1 (astronexus.com, CC BY-SA 4.0); ICRS J2000, pm mas/yr"
    return out


def sexa(s: str, hours: bool) -> float:
    sign = -1 if s.startswith("-") else 1
    d, m, x = (float(p) for p in s.lstrip("+-").split(":"))
    return sign * (d + m / 60 + x / 3600) * (15 if hours else 1)


def dso(src: Path) -> dict:
    out = []
    for fn in ("NGC.csv", "addendum.csv"):
        for r in csv.DictReader(open(src / fn, newline=""), delimiter=";"):
            kind = DSO_TYPES.get(r["Type"])
            if not kind or not r["RA"]:
                continue
            mag = r["V-Mag"] or r["B-Mag"]
            mag = float(mag) if mag else None
            maj = float(r["MajAx"]) if r["MajAx"] else 0.0
            messier = int(r["M"]) if r["M"] else 0
            big = maj >= 8 and kind in ("nebula", "supernova remnant", "open cluster")
            if not (messier or (mag is not None and mag <= 11.5) or big):
                continue
            name = r["Name"]
            for pre in ("NGC", "IC"):
                if name.startswith(pre) and name[len(pre):len(pre) + 4].isdigit():
                    name = f"{pre} {name[len(pre):].lstrip('0')}"
            if name.startswith("Mel0"):
                name = "Mel " + name[3:].lstrip("0")
            out.append({
                "id": f"M {messier}" if messier else name, "alt": name if messier else "",
                "type": kind, "otype": r["Type"],
                "ra": round(sexa(r["RA"], True), 4), "dec": round(sexa(r["Dec"], False), 4),
                "mag": mag, "maj": maj, "min": float(r["MinAx"]) if r["MinAx"] else maj,
                "pa": float(r["PosAng"]) if r["PosAng"] else 0.0,
                "con": r["Const"], "common": r["Common names"].split(",")[0] if r["Common names"] else "",
            })
    out.sort(key=lambda d: (d["mag"] if d["mag"] is not None else 99))
    return {"objects": out, "source": "OpenNGC (github.com/mattiaverga/OpenNGC, CC BY-SA 4.0); ICRS J2000, sizes in arcmin"}


def constellations(src: Path) -> dict:
    lines = json.loads((src / "constellations.lines.json").read_text())
    names = json.loads((src / "constellations.json").read_text())
    ra = lambda lon: round(lon % 360, 3)  # noqa: E731 - d3-celestial longitudes are -180..180
    out = {"lines": {}, "labels": {}, "source": "d3-celestial (github.com/ofrohn/d3-celestial, BSD-3-Clause)"}
    for f in lines["features"]:
        out["lines"][f["id"]] = [[[ra(p[0]), round(p[1], 3)] for p in seg] for seg in f["geometry"]["coordinates"]]
    for f in names["features"]:
        lon, lat = f["geometry"]["coordinates"]
        out["labels"][f["id"]] = {"name": f["properties"]["name"], "ra": ra(lon), "dec": round(lat, 3)}
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", type=Path, default=REPO / "build" / "skysrc")
    a = ap.parse_args()
    fetch(a.src)
    OUT.mkdir(parents=True, exist_ok=True)
    for name, doc in (("stars.json", stars(a.src)), ("dso.json", dso(a.src)), ("constellations.json", constellations(a.src))):
        text = json.dumps(doc, separators=(",", ":"), ensure_ascii=False)
        (OUT / name).write_text(text)
        print(f"{name}: {len(text) / 1e6:.2f} MB")


if __name__ == "__main__":
    main()
