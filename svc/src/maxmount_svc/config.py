# SPDX-License-Identifier: GPL-3.0-or-later
"""mount-svc settings from the shared system file (config/systems/<name>/system.yaml).

mount-svc reads the sections it needs: site, tracking, axes (names and limits), zenoh and
time. mount-rt ignores site and tracking.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEG = math.pi / 180.0


@dataclass
class Site:
    latitude_deg: float
    longitude_deg: float  # east positive
    height_m: float = 0.0
    pressure_hpa: float = 1013.25  # 0 disables refraction
    temperature_c: float = 10.0
    humidity: float = 0.5  # 0..1
    wavelength_um: float = 0.55
    dut1_s: float = 0.0  # UT1 - UTC; "auto" in the system file reads IERS Bulletin A
    dut1_source: str = "system file"
    position_source: str = "system file"


@dataclass
class AxisLimits:
    min_rad: float
    max_rad: float
    max_vel: float  # rad/s
    max_acc: float  # rad/s^2


@dataclass
class Tracking:
    horizon_min_el_deg: float = 10.0
    sun_min_sep_deg: float = 30.0
    slew_vel_deg_s: float = 5.0
    slew_acc_deg_s2: float = 5.0
    rate_margin: float = 0.9  # plans may use this fraction of the axis velocity/acceleration
    over_the_top_tolerance_deg: float = 1.0  # max pointing error accepted in over-the-top mode
    point_rate_hz: float = 20.0
    lookahead_s: float = 5.0


@dataclass
class ZenohSettings:
    site: str = "default"
    mode: str = "peer"
    connect: list[str] = field(default_factory=list)
    listen: list[str] = field(default_factory=list)
    multicast_scouting: bool = True


@dataclass
class DslrSettings:
    backend: str = "gphoto2"          # gphoto2 | sim
    data_dir: str = "~/maxmount-data"  # captures go to <data_dir>/dslr/<date>/
    liveview_fps: float = 8.0
    save_to: str = "card"             # card | pi | both (the GUI can change it)


@dataclass
class AdsbSettings:
    # readsb/dump1090 aircraft.json: a path, or an http(s) URL on the local network
    # (e.g. tar1090's http://host/tar1090/data/aircraft.json).
    source: str = "/run/readsb/aircraft.json"
    max_age_s: float = 10.0     # hold the mount if a tracked aircraft's position is older
    max_track_s: float = 600.0  # longest aircraft track before re-planning is needed


RAD_AS = 206264.806


@dataclass
class ImagingProfile:
    """One narrow-field optical configuration (telescope, reducer or Barlow, camera)."""
    key: str
    name: str
    pixel_um: float
    focal_mm: float
    aperture_mm: float = 203.2
    sensor_px: tuple[int, int] | None = None
    camera: str = ""
    note: str = ""
    color: bool = False  # Bayer sensor: each colour is sampled more coarsely than the pixel grid
    camera_service: str = ""  # the camera service of the camera on the C8: dslr (D500) or zwo

    @property
    def arcsec_per_px(self) -> float:
        return RAD_AS * self.pixel_um / (self.focal_mm * 1000)

    @property
    def f_ratio(self) -> float:
        return self.focal_mm / self.aperture_mm

    def nyquist_px_arcsec(self, wavelength_nm: float) -> float:
        """The largest pixel that samples the diffraction limit: lambda / (2 D)."""
        return RAD_AS * wavelength_nm * 1e-9 / (2 * self.aperture_mm * 1e-3)

    def summary(self) -> dict:
        d = {
            "key": self.key, "name": self.name, "camera": self.camera, "note": self.note, "color": self.color,
            "camera_service": self.camera_service,
            "pixel_um": self.pixel_um, "focal_mm": self.focal_mm, "aperture_mm": self.aperture_mm,
            "f_ratio": round(self.f_ratio, 2), "arcsec_per_px": round(self.arcsec_per_px, 4),
            "diffraction_arcsec": round(RAD_AS * 550e-9 / (self.aperture_mm * 1e-3), 3),  # lambda/D at 550 nm
            # Pixel scale over the Nyquist pixel: <= 1 samples the diffraction limit.
            "sampling_550": round(self.arcsec_per_px / self.nyquist_px_arcsec(550), 2),
            "sampling_685": round(self.arcsec_per_px / self.nyquist_px_arcsec(685), 2),
        }
        if self.sensor_px:
            d["fov_arcmin"] = [round(n * self.arcsec_per_px / 60, 1) for n in self.sensor_px]
        return d


@dataclass
class ImagingSettings:
    """Narrow-field imaging profiles (system file `imaging:`); the operator picks the active
    one in the GUI (remembered by the gateway, see gateway.ImagingState)."""
    profiles: dict[str, ImagingProfile] = field(default_factory=lambda: {
        "default": ImagingProfile("default", "C8 + 0.63x corrector + 3.76 um camera", 3.76, 1350.0, sensor_px=(3008, 3008))})
    default: str = "default"


def _imaging(doc: dict) -> ImagingSettings:
    im = doc.get("imaging") or {}
    if not im:
        return ImagingSettings()
    if "narrow_pixel_um" in im:  # the earlier single-camera form
        p = ImagingProfile("default", "narrow field", float(im["narrow_pixel_um"]), float(im.get("narrow_focal_mm", 1350)))
        return ImagingSettings({"default": p}, "default")
    profiles = {}
    for key, v in (im.get("profiles") or {}).items():
        try:
            profiles[str(key)] = ImagingProfile(
                key=str(key), name=str(v.get("name", key)), pixel_um=float(v["pixel_um"]), focal_mm=float(v["focal_mm"]),
                aperture_mm=float(v.get("aperture_mm", 203.2)),
                sensor_px=tuple(int(x) for x in v["sensor_px"]) if v.get("sensor_px") else None,
                camera=str(v.get("camera", "")), note=str(v.get("note", "")), color=bool(v.get("color", False)),
                camera_service=str(v.get("camera_service", "")))
        except (KeyError, TypeError, ValueError) as e:
            raise ConfigError(f"imaging profile '{key}': {e}") from None
    if not profiles:
        raise ConfigError("imaging: no profiles")
    default = str(im.get("default") or next(iter(profiles)))
    if default not in profiles:
        raise ConfigError(f"imaging.default '{default}' is not a profile")
    return ImagingSettings(profiles, default)


@dataclass
class SystemSettings:
    name: str
    backend: str
    site: Site | None
    tracking: Tracking
    az: AxisLimits
    el: AxisLimits
    zenoh: ZenohSettings
    max_clock_error_us: float = 10.0
    pointing_file: Path | None = None
    # Every axis of the system file, for display (Drives screen): name, drive, bus position,
    # scaling, limits in degrees and the drive descriptor's vendor/model/error register.
    axes_info: list[dict] = field(default_factory=list)
    fieldbus: dict = field(default_factory=dict)
    adsb: AdsbSettings = field(default_factory=AdsbSettings)
    imaging: ImagingSettings = field(default_factory=ImagingSettings)
    dslr: DslrSettings = field(default_factory=DslrSettings)


class ConfigError(RuntimeError):
    pass


def _limits(axis: dict, name: str) -> AxisLimits:
    lim = axis.get("limits") or {}
    try:
        return AxisLimits(
            float(lim["min_deg"]) * DEG,
            float(lim["max_deg"]) * DEG,
            float(lim["max_vel_deg_s"]) * DEG,
            float(lim["max_acc_deg_s2"]) * DEG,
        )
    except (KeyError, TypeError, ValueError) as e:
        raise ConfigError(f"axis '{name}': incomplete limits ({e})") from None


def _axes_info(doc: dict, drives_dir: Path) -> list[dict]:
    out = []
    for a in doc.get("axes") or []:
        lim = a.get("limits") or {}
        info = {
            "name": a.get("name"),
            "drive": a.get("drive", ""),
            "bus_position": a.get("bus_position"),
            "mechanics": a.get("mechanics", ""),
            "counts_per_rev": a.get("counts_per_rev"),
            "commissioned": a.get("counts_per_rev") is not None,
            "invert": bool(a.get("invert", False)),
            "offset_deg": float(a.get("offset_deg") or 0.0),
            "limits": {k: (None if v is None else float(v)) for k, v in lim.items()},
            "vendor": "", "model": "", "error_register": None,
        }
        try:
            d = yaml.safe_load((drives_dir / f"{info['drive']}.yaml").read_text()) or {}
            info["vendor"], info["model"] = str(d.get("vendor", "")), str(d.get("model", ""))
            er = (d.get("diagnostics") or {}).get("error_register")
            if er:
                info["error_register"] = {"name": er.get("name", ""), "decode": er.get("decode", ""),
                                          "index": er.get("index")}
        except (OSError, yaml.YAMLError, AttributeError):
            pass
        out.append(info)
    return out


def load_system(path: str | Path) -> SystemSettings:
    path = Path(path)
    try:
        doc = yaml.safe_load(path.read_text())
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e}") from None
    if not isinstance(doc, dict):
        raise ConfigError(f"{path}: top level must be a mapping")

    axes = {a.get("name"): a for a in doc.get("axes") or []}
    for needed in ("az", "el"):
        if needed not in axes:
            raise ConfigError(f"{path}: mount-svc needs axes named 'az' and 'el'")

    site = None
    s = dict(doc.get("site") or {})
    if str(s.get("source", "")).strip().lower() == "gpsd":
        from . import gnss

        try:
            fix, how = gnss.site_fix(str(s.get("gpsd_host", "localhost")), int(s.get("gpsd_port", 2947)))
        except gnss.GnssError as e:
            raise ConfigError(f"site position: {e}") from None
        s.update(latitude_deg=fix.latitude_deg, longitude_deg=fix.longitude_deg, height_m=fix.height_m)
        position_source = how
    else:
        position_source = "system file"
    if s.get("latitude_deg") is not None and s.get("longitude_deg") is not None:
        auto_dut1 = str(s.get("dut1_s", "")).strip().lower() == "auto"
        if auto_dut1:
            s.pop("dut1_s")
        site = Site(**{k: float(v) for k, v in s.items() if k in Site.__dataclass_fields__ and v is not None})
        if auto_dut1:
            import time

            from . import iers

            site.dut1_s, source = iers.dut1_at(time.time_ns())
            site.dut1_source = source
        site.position_source = position_source

    t = doc.get("tracking") or {}
    tracking = Tracking(**{k: float(v) for k, v in t.items() if k in Tracking.__dataclass_fields__})

    z = doc.get("zenoh") or {}
    zenoh = ZenohSettings(
        site=str(z.get("site", "default")),
        mode=str(z.get("mode", "peer")),
        connect=list(z.get("connect") or []),
        listen=list(z.get("listen") or []),
        multicast_scouting=bool(z.get("multicast_scouting", True)),
    )

    pointing = doc.get("pointing")
    pointing_file = (path.parent / pointing).resolve() if pointing else None

    return SystemSettings(
        name=str(doc.get("system", path.parent.name)),
        backend=str((doc.get("fieldbus") or {}).get("backend", "sim")),
        site=site,
        tracking=tracking,
        az=_limits(axes["az"], "az"),
        el=_limits(axes["el"], "el"),
        zenoh=zenoh,
        max_clock_error_us=float((doc.get("time") or {}).get("max_clock_error_us", 10.0)),
        pointing_file=pointing_file,
        axes_info=_axes_info(doc, path.resolve().parent.parent.parent / "drives"),
        fieldbus=dict(doc.get("fieldbus") or {}),
        imaging=_imaging(doc),
        dslr=DslrSettings(**{k: (float(v) if k == "liveview_fps" else str(v))
                             for k, v in ((doc.get("cameras") or {}).get("dslr") or {}).items() if k in DslrSettings.__dataclass_fields__}),
        adsb=AdsbSettings(**{k: (str(v) if k == "source" else float(v))
                             for k, v in (doc.get("adsb") or {}).items() if k in AdsbSettings.__dataclass_fields__}),
    )
