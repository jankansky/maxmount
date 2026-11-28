# SPDX-License-Identifier: GPL-3.0-or-later
"""mount-svc settings from the shared system file (config/systems/<name>/system.yaml).

mount-svc reads the sections it needs: site, tracking, axes (names and limits), zenoh and
time. mount-rt ignores site and tracking.
"""
from __future__ import annotations

import math
from dataclasses import MISSING, dataclass, field
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
class ZwoSettings:
    backend: str = "sdk"              # sdk | sim
    sdk_lib: str = "/usr/local/lib/libASICamera2.so"  # ZWO's ASI SDK library (from ZWO's developer page)
    data_dir: str = "~/maxmount-data"  # frames go to <data_dir>/zwo/<date>/
    liveview_fps: float = 8.0         # preview frames to the GUI (the camera streams faster)
    record_buffer_mb: float = 512.0   # SER recording: frames queued in RAM while the card catches up
    record_min_free_mb: float = 2048.0  # SER recording stops when the disk has less free space than this
    roi_latency_frames: float = 2.0   # ROI tracking: frames still read at the old position after a move
    # Simulator only: the "true" errors the camera loop has to find (the sim draws the target
    # where it really is relative to where the mount points) and how the sensor really sits.
    sim_time_error_s: float = 0.12        # satellites: along-track (TLE) error
    sim_cross_track_arcsec: float = 25.0  # satellites: cross-track error
    sim_az_error_arcsec: float = 40.0     # every target: pointing error (on the sky)
    sim_el_error_arcsec: float = -25.0
    sim_rotation_deg: float = 20.0        # sensor +x from +azimuth (see closedloop.CameraGeometry)
    sim_flip: float = 0.0                 # 1 = mirrored
    sim_arcsec_per_px: float = 0.38       # ASI224MC (3.75 um) at 2032 mm


def _default_sim_model() -> dict:
    # The simulated mount's "true" pointing errors as the widefield sees them (arcsec, terms as
    # in pointing.py): a pointing run should find these.
    return {"IA": 420.0, "IE": -300.0, "CA": 150.0, "NPAE": 40.0, "AN": 90.0, "AW": -60.0, "TF": 30.0}


@dataclass
class WidefieldSettings:
    """The widefield (finder) GigE camera, `mount-svc widefield` (system file cameras.widefield).
    Defaults: Allied Vision Prosilica GS1380 (Sony ICX285 CCD, mono, global shutter, 1360 x 1024,
    6.45 um) behind a 35 mm lens: 14.3 x 10.8 deg, 38"/px."""
    backend: str = "aravis"           # aravis | sim
    camera: str = ""                  # Aravis device id (arv-tool-0.8 lists them), or "" for the first camera found
    data_dir: str = "~/maxmount-data"  # frames go to <data_dir>/widefield/<date>/, solves to .../widefield/solves.jsonl
    liveview_fps: float = 2.0
    pixel_um: float = 6.45
    width: int = 1360
    height: int = 1024
    focal_mm: float = 35.0            # nominal; the solver accepts +-12 %; replace with the solved value
    distortion_k1: float = 0.0        # the lens' radial distortion at the corners (0.01 = 1 %); solves refit it
    latency_ms: float = 0.0           # frame arrival after the end of the exposure (readout + transfer); measure
    exposure_ms: float = 500.0
    gain: float = 10.0                # dB
    packet_size: int = 0              # GigE packet size (0: negotiate; 8192+ with jumbo frames)
    solver: str = "builtin"           # builtin | astap
    astap_path: str = "astap_cli"
    search_radius_deg: float = 5.0    # how far from the mount's position a solve looks
    # Simulator only: how the camera really sits and the mount's true pointing errors.
    sim_rotation_deg: float = 12.0    # sensor +x from +azimuth towards +elevation
    sim_flip: bool = False
    sim_model: dict = field(default_factory=_default_sim_model)
    sim_cloudy: bool = False          # no stars (solves fail)
    sim_distortion_k1: float = 0.0    # the simulated lens' radial distortion (platesolve's convention)

    @property
    def arcsec_per_px(self) -> float:
        return RAD_AS * self.pixel_um / (self.focal_mm * 1000)

    @property
    def fov_deg(self) -> tuple[float, float]:
        f = self.focal_mm * 1000
        return (2 * math.degrees(math.atan(self.width * self.pixel_um / 2 / f)),
                2 * math.degrees(math.atan(self.height * self.pixel_um / 2 / f)))


@dataclass
class PointingRunSettings:
    """Pointing model runs in mount-svc (system file `pointing_run:`)."""
    camera: str = "widefield"
    settle_s: float = 2.0             # after the mount reaches a point, before the exposure
    min_el_deg: float = 0.0           # 0: the horizon mask + 5 deg
    max_el_deg: float = 80.0          # the zenith keyhole stays out
    solve_timeout_s: float = 60.0
    sun_min_sep_deg: float = 0.0      # 0: tracking.sun_min_sep_deg
    moon_min_sep_deg: float = 15.0


@dataclass
class ClosedLoopSettings:
    """mount-svc's camera loop (system file `closed_loop:`): centroids from camera/<camera>/
    centroid become tracking corrections. The geometry here is the starting guess; a
    calibration in the GUI measures and remembers the real one."""
    camera: str = "zwo"
    rotation_deg: float = 0.0     # sensor +x from the +azimuth direction, towards +elevation
    flip: bool = False
    arcsec_per_px: float = 0.0    # 0 = from the imaging profile used by this camera
    gain: float = 0.5
    settle_s: float = 0.6
    avg_s: float = 0.25
    min_snr: float = 8.0
    max_step_arcsec: float = 120.0
    max_total_arcsec: float = 600.0
    max_step_px: float = 20.0
    calibrate_step_arcsec: float = 0.0  # 0 = max_step_px on the sensor


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
    zwo: ZwoSettings = field(default_factory=ZwoSettings)
    closed_loop: ClosedLoopSettings = field(default_factory=ClosedLoopSettings)
    widefield: WidefieldSettings = field(default_factory=WidefieldSettings)
    pointing_run: PointingRunSettings = field(default_factory=PointingRunSettings)

    def loop_arcsec_per_px(self) -> float:
        """The camera loop's starting pixel scale: configured, or the imaging profile of the
        loop's camera (the default profile first), else a C8 at f/10 with 3.75 um pixels."""
        if self.closed_loop.arcsec_per_px > 0:
            return self.closed_loop.arcsec_per_px
        im = self.imaging
        cands = [im.profiles[im.default]] + list(im.profiles.values())
        for p in cands:
            if p.camera_service == self.closed_loop.camera:
                return p.arcsec_per_px
        return 0.38


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


def _closed_loop(d: dict) -> ClosedLoopSettings:
    try:
        return ClosedLoopSettings(**{k: (str(v) if k == "camera" else bool(v) if k == "flip" else float(v))
                                     for k, v in d.items() if k in ClosedLoopSettings.__dataclass_fields__})
    except (TypeError, ValueError) as e:
        raise ConfigError(f"closed_loop: {e}") from None


def _typed(cls, d: dict, section: str):
    """A settings dataclass from a mapping: known keys only, each converted to its default's type."""
    out = {}
    for k, v in (d or {}).items():
        f = cls.__dataclass_fields__.get(k)
        if f is None:
            continue
        default = None if f.default is MISSING else f.default
        try:
            if isinstance(default, bool):
                out[k] = bool(v)
            elif isinstance(default, int):
                out[k] = int(v)
            elif isinstance(default, float):
                out[k] = float(v)
            elif isinstance(default, str):
                out[k] = str(v)
            elif k == "sim_model":
                out[k] = {str(t): float(c) for t, c in (v or {}).items()}
            else:
                out[k] = v
        except (TypeError, ValueError, AttributeError) as e:
            raise ConfigError(f"{section}.{k}: {e}") from None
    return cls(**out)


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
        zwo=ZwoSettings(**{k: (str(v) if k in ("backend", "sdk_lib", "data_dir") else float(v))
                           for k, v in ((doc.get("cameras") or {}).get("zwo") or {}).items() if k in ZwoSettings.__dataclass_fields__}),
        closed_loop=_closed_loop(doc.get("closed_loop") or {}),
        widefield=_typed(WidefieldSettings, (doc.get("cameras") or {}).get("widefield") or {}, "cameras.widefield"),
        pointing_run=_typed(PointingRunSettings, doc.get("pointing_run") or {}, "pointing_run"),
        adsb=AdsbSettings(**{k: (str(v) if k == "source" else float(v))
                             for k, v in (doc.get("adsb") or {}).items() if k in AdsbSettings.__dataclass_fields__}),
    )
