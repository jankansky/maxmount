# maxmount services

Python services that run on the Pi's non-real-time cores and talk over Zenoh.

| Service | Responsibility | Status |
| --- | --- | --- |
| `mount-svc` | Targets (sidereal, SGP4, fixed), ERFA astrometry, pointing model, pass planner, trajectory streaming to `mount-rt` | v0: Zenoh service + command line |
| `camera-svc` | ZWO and GigE capture, centroiding, plate solving (ASTAP), autofocus | planned |
| `supervisor` | Safety (sun, weather, clock quality, heartbeats) and the night scheduler | planned |
| `gui-gateway` | Serves the web GUI; WebSocket bridge to Zenoh (`mount-svc gui`) | v0: Track screen |

## Install

```sh
python3 -m venv svc/.venv
svc/.venv/bin/pip install -e 'svc[dev,dslr,zwo]'    # dslr: python-gphoto2 for the D500; zwo: zwoasi
svc/.venv/bin/python -m pytest svc/tests        # the end-to-end test needs mount-rt built with Zenoh
```

Message code is generated from `schemas/maxmount/v1/mount.proto` on first import, using the
protoc bundled with `grpcio-tools`, so it always matches the installed protobuf runtime.

## mount-svc

`mount-rt` must run with `--serve`; both read the same system file.

```sh
SYS=config/systems/sim-altaz/system.yaml
build/rt/mount-rt --system $SYS --serve &                       # simulated mount
svc/.venv/bin/mount-svc --system $SYS passes --tle iss.tle      # passes and plans, no mount needed
svc/.venv/bin/mount-svc --system $SYS track-tle --tle iss.tle   # wait for the next pass and track it
svc/.venv/bin/mount-svc --system $SYS adsb-sim                   # simulated ADS-B traffic at adsb.source (no receiver)
svc/.venv/bin/mount-svc --system $SYS dslr                       # stills camera over USB (Nikon D500); --sim without one
svc/.venv/bin/mount-svc --system $SYS zwo                        # ZWO camera (ASI SDK); --sim: a simulated ASI224MC
svc/.venv/bin/mount-svc --system $SYS track-radec --ra 06:45:08.9 --dec -16:42:58 --minutes 10
svc/.venv/bin/mount-svc --system $SYS goto --az 180 --el 45
```

### As a service

`mount-svc serve` keeps running and takes targets over Zenoh from the GUI, the scheduler or the
camera loop (keys `svc/target`, `svc/passes`, `svc/status`; messages in
`schemas/maxmount/v1/target.proto`). `mount-svc request ...` is a command-line client for it:

```sh
svc/.venv/bin/mount-svc --system $SYS serve &
svc/.venv/bin/mount-svc --system $SYS request radec --ra 06:45:08.9 --dec -16:42:58
svc/.venv/bin/mount-svc --system $SYS request offsets --az 30 --el -15      # arcsec, live
svc/.venv/bin/mount-svc --system $SYS request tle --tle iss.tle             # waits for the pass
svc/.venv/bin/mount-svc --system $SYS request status
svc/.venv/bin/mount-svc --system $SYS request stop                          # --hold keeps enabled
```

The reply to a track or go-to request comes after planning: it carries the chosen plan, or the
reason no plan fits (horizon, Sun, rates, cable wrap, clock). A new target takes over from the
current one without disabling the drives. Offsets take effect within about 0.3 s, because the
buffered future points are re-sent. The along-track and cross-track corrections apply to a
satellite target; the axis offsets apply to any target (hand control, camera loop).

Each track enables the axes, slews to where the target will be when the slew ends, then
streams time-tagged points (20 Hz, 5 s ahead) that `mount-rt` interpolates at 1 kHz. Ctrl-C,
a drive fault or a following-error trip disables the axes. If `mount-svc` dies, the trajectory
runs out and `mount-rt` decelerates and holds.

### Accuracy notes

- **Geometry:** TEME→Earth-fixed matches Vallado's published example to 2 cm. Satellite
  az/el agree with Skyfield to 0.02″ for the same UT1−UTC, when light time is excluded
  (mount-svc applies it: 2–5″ for the ISS).
- **UT1−UTC:** `site.dut1_s: auto` reads IERS Bulletin A (cached weekly in `~/.cache/maxmount`).
  For LEO satellites 0.1 s of UT1 error is ~20″.
- **Clock:** satellite passes are refused when chrony's error bound exceeds
  `time.max_clock_error_us` (simulated systems are exempt; `--allow-clock-error` overrides).
  Without a PPS source the Pi's NTP clock is good to about a millisecond, which is ~4″
  along-track near the zenith.

### Over the top

The mount's elevation axis runs 0–180°. Every sky direction can then be reached two ways,
`(az, el)` and `(az + 180°, 180° − el)`, which coincide only at the zenith. A pass that does
not go exactly through the zenith therefore has two exact tracking solutions with **identical
azimuth rates**. Going over the top does not remove the alt-az keyhole. What it adds is a third
option: hold azimuth on the vertical great circle that best fits the pass and sweep elevation
through the zenith. The pointing error is then the pass's distance from that circle, largest
near culmination. The planner tries `normal`, `flipped` and `over_the_top`, and picks the first
exact plan within the axis limits (with `tracking.rate_margin`). If none fits, it uses
over-the-top when its error is within `tracking.over_the_top_tolerance_deg`.

### Pointing model

TPOINT-style terms (IA, IE, CA, NPAE, AN, AW, TF, BE) are applied to the mechanical angles. The
`pointing:` entry in the system file names a YAML file with `terms: {IA: 12.3, ...}` in
arcseconds. Fitting comes with the camera service.

## ZWO camera setup (Pi)

`mount-svc zwo` uses ZWO's ASI SDK through the `zwoasi` package. The SDK is a binary library
from ZWO (not redistributed here):

1. Download the "ASI Camera SDK" for Linux from ZWO's developer downloads and copy
   `lib/armv8/libASICamera2.so` to `/usr/local/lib/` (or set `cameras.zwo.sdk_lib`).
2. Install its udev rules so the camera works without root:
   `sudo install -m 644 lib/asi.rules /etc/udev/rules.d/ && sudo udevadm control --reload`,
   then replug the camera.
3. High frame rates need more USB buffer memory than Linux's default 16 MB: add
   `usbcore.usbfs_memory_mb=200` to `/boot/firmware/cmdline.txt` and reboot.

Without a camera, `mount-svc zwo --sim` simulates an ASI224MC.

**SER video.** The record command (GUI: Record SER) writes the video stream to
`<data_dir>/zwo/<date>/<time>_<format>_<w>x<h>_<exposure>.ser` with a JSON sidecar (settings,
mount target at the start, frames, drops, why it stopped). SER files open in AutoStakkert!,
PIPP, Siril, RegiStax and SER Player; each frame's timestamp is the start of its exposure
(arrival on the Pi minus the exposure, so approximate). Raw colour frames are stored as an
RGGB Bayer mosaic, RAW16 as 16-bit little-endian (the 12-bit data shifted up by 4), RGB24 as
RGB. Frames wait in a RAM buffer (`cameras.zwo.record_buffer_mb`, allocated when recording
starts) for the disk; when it is full, frames are dropped and counted rather than slowing the
camera. Recording stops when free space falls below `cameras.zwo.record_min_free_mb`.
`maxmount_svc.ser.read_ser()` reads the files back in Python. A `.csv` next to each SER logs
every frame's ROI position and, when ROI tracking, where the target was.

**ROI tracking** (the roi_track command; GUI: Track ROI). The ROI follows the target across
the sensor: each frame is searched for the target (the brightest extended object, or the one
nearest a picked point, then only near where it is expected), an alpha-beta filter estimates
its position and speed, and the ROI's start position is moved (its size stays, so recording
continues) to keep the target centred. A ZWO camera keeps delivering a few frames read at the
old position after a move; `cameras.zwo.roi_latency_frames` (default 2, to be measured on the
real camera) says how many, so each frame is measured and logged with the position it was
really read at. Each measurement is published on `camera/zwo/centroid` with the target's
offset from the sensor centre, for the future mount closed loop. The simulator's target drifts
up to ~350 px from the centre, so without tracking it leaves a small ROI.

Disk speed limits long recordings. The Pi's SD card sustains about 34 MB/s
(`dd ... conv=fdatasync`, 2026-11-26), about 110 fps of 640 × 480 RAW8 or 26 fps full frame.
Linux's write cache and the RAM buffer absorb faster bursts: on the Pi, 20 s at 150 fps of
640 × 480 and 10 s at 60 fps full frame (76 MB/s) recorded with no drops, the latter taking
18 s more to finish writing (the GUI shows "Finishing…"). For long fast recordings, record to
a USB 3 SSD (set `cameras.zwo.data_dir`).
