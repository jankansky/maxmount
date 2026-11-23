# maxmount — Observatory Control System Design

## Overview and goals

maxmount is an open-source (GPLv3) observatory control system: a Raspberry Pi acts as EtherCAT
master for a high-rate alt-az mount, with the primary goal of tracking LEO satellites. LinuxCNC
is replaced by a dedicated C++ real-time controller.

- **Satellite tracking:** LEO passes at up to ~1.1°/s with arcsecond-class pointing, including overhead passes.
- **Astronomical tracking:** sidereal and non-sidereal targets, with autofocus and long-exposure support.
- **Automation:** fully unattended remote operation, with a web GUI for monitoring and manual control.
- **Portability:** set up on a tripod pier at a new site and re-model in minutes.
- **Generic platform:** any CiA 402 EtherCAT drive and any supported mount geometry, by configuration. The hardware below is the reference setup.

## Reference hardware

Custom alt-az mount on an iOptron tri-pier, with elevation travel from −45° through zenith to +180°.

| Subsystem | Hardware | Notes |
| --- | --- | --- |
| Azimuth axis | Aerotech ADRT-150-115 direct-drive stage (ES17474) | Sin/cos encoder + Hall sensors; no gearing |
| Elevation axis | Teknic Hudson M-2321P-LN-16D → Bell-Everman ServoBelt reduction | Dual-loop: motor encoder for velocity, load encoder for position |
| Elevation load encoder | Renishaw RGH41 tape readhead + REE2000 interpolator | About 10 nm per count; check max count rate vs. drive input |
| Drives | 2× Technosoft iPOS4808 BX-CAT | CiA 402 over EtherCAT, CSP mode |
| Computer | Raspberry Pi 4B with PREEMPT_RT kernel | One core isolated for the RT loop |
| Telescope | Celestron C8, primary mirror to be locked | External motorized focuser |
| Widefield camera | The Imaging Source GigE | Through-axis Ethernet; acquisition and plate solving |
| Narrow-field camera | ZWO USB3 behind the C8 | Through-axis USB; fast region-of-interest tracking |
| GNSS | u-blox M8 USB receiver (1546:01a8) | Location and backup time; add a PPS-capable module for timing |

## System architecture

The Pi 4 runs everything needed to move the mount and track a target; the laptop is a browser
client over WiFi, so losing it never affects a pass in progress.

```
                         Laptop (browser GUI)
                                 │ WiFi
┌─────────────────────── Raspberry Pi 4 · PREEMPT_RT ───────────────────────┐
│  gui-gateway      mount-svc            camera-svc          supervisor     │
│  web GUI, REST,   targets, SGP4, ERFA, capture, centroid,  safety, weather,│
│  WebSocket        model, pass planner  plate solve, focus  scheduler      │
│ ───────────── Zenoh router · pub/sub + queries · Protobuf ─────────────── │
│                                  │ trajectory ↓ / telemetry ↑             │
│   isolated core ─►  mount-rt · C++ · 1 kHz                chrony + gpsd   │
│                     SOEM CiA 402 CSP, Hermite interp,     GPS-disciplined │
│                     limits, watchdog, encoder history     system clock    │
└──────────┬───────────────────┬──────────────┬──────────────────┬──────────┘
   eth0 · EtherCAT        USB-Ethernet       USB3            PPS (GPIO), USB
           │                   │              │                  │
  iPOS Az ── iPOS El       GigE camera     ZWO camera        GNSS + PPS
  (ADRT)    (Teknic+tape)  (widefield)     (behind C8)
```

Non-RT services share cores 0–2 and talk over Zenoh; `mount-rt` owns the isolated core and
exchanges data with Zenoh only through a lock-free ring and a bridge thread. Each Ethernet port
has one job: `eth0` is EtherCAT only, the USB-Ethernet adapter serves the GigE camera, and WiFi
serves the laptop (the Pi acts as an access point in the field).

## Real-time mount controller

`mount-rt` runs CiA 402 Cyclic Synchronous Position (CSP, mode 8) at 1 kHz with distributed
clocks, using SOEM as the EtherCAT master. CSP was chosen over the drive's buffered PVT mode for
low correction latency (~1–2 ms vs. 60 ms+) and direct alignment of motion to UTC.

- **Process model:** one `SCHED_FIFO` thread on an isolated core; memory locked; no allocation, I/O or Zenoh calls in the loop.
- **Trajectory input:** time-tagged (t, position, velocity) points at 20–50 Hz from `mount-svc`, buffered about 0.5–2 s ahead.
- **Interpolation:** cubic Hermite per 1 ms cycle, giving the same smoothness as drive-side PVT. Velocity feedforward via 0x60B1 and torque feedforward via 0x60B2 where the drive supports them.
- **Limits:** soft position limits with a braking envelope, azimuth cable-wrap accounting, velocity and acceleration clamps per axis.
- **Fault handling:** CiA 402 state machine per drive; faults are never reset automatically; quick stop on fault, trajectory starvation or lost heartbeat.
- **Telemetry:** a lock-free ring carries per-cycle commanded and actual positions to a bridge thread, which publishes on Zenoh. A timestamped encoder history lets camera frames be matched to true pointing.
- **Backends:** a `Fieldbus` interface with a SOEM CSP backend, a simulated backend for development and CI, and an optional PVT backend later.
- **Acceptance test:** 1 kHz with cameras, WiFi and services loaded; worst-case cycle jitter well under 1 ms and zero lost frames over several hours.

## Targets and tracking

The elevation axis reaching +180° removes the alt-az zenith keyhole: overhead passes are tracked
"over the top" with little azimuth motion.

- **Target providers:** sidereal (catalog RA/Dec with proper motion), satellites from TLE/OMM via SGP4 (CelesTrak, Space-Track), ephemeris tables (OEM/CPF), and fixed alt-az positions.
- **Astrometry:** ERFA for time scales, precession-nutation, aberration and refraction from measured pressure and temperature.
- **Pass planner:** simulates each pass in normal mode (elevation 0–90°) and flip mode (azimuth set near the pass direction, elevation sweeping 0→180°). It picks the mode with the lowest peak axis rates that fits velocity, acceleration, cable-wrap and horizon limits, and rejects passes that do not fit.
- **TLE error correction:** live along-track time offset and cross-track offset, adjustable from the GUI or by the camera loop.
- **Closed-loop optical tracking:**
    1. The widefield GigE camera detects the satellite and measures its offset from prediction.
    2. `mount-svc` updates the time and cross-track offsets, which brings the target into the C8 field.
    3. The ZWO camera tracks a small region of interest at 50–200 fps and holds the target centered.
- **Hand control:** jog and offset commands from the GUI apply on top of any target.

## Timekeeping

The system clock must be accurate to about 10 µs, because a LEO target near zenith moves about
4″ per millisecond.

- **Source:** a GNSS module with PPS wired to a Pi GPIO pin (`pps-gpio`), disciplined by chrony through gpsd. The USB u-blox M8 gives position and a backup clock only, since USB carries no PPS.
- **Gate:** `mount-svc` reads chrony's error estimate and refuses to start a satellite pass when the clock is not trustworthy.
- **EtherCAT clocks:** the distributed-clock reference is tied to the PPS-disciplined system clock, so each commanded position happens at a known UTC time.
- **Camera timestamps:** best is a hardware trigger from a GPIO at known times; next is camera PTP; last is host receive time minus a calibrated latency.
- **Pairing:** each frame's mid-exposure time is matched to the `mount-rt` encoder history.

## Automated pointing model

Setting up at a new site takes about 5 minutes: home the axes, plate-solve 6–12 points, and refit
only the site terms.

1. **Plan points:** spread over the sky within the horizon mask, on both sides of zenith, away from the Moon, ordered to minimize slews and cable wrap. Presets: site refit (6–12 points) and full model (30–60 points).
2. **Measure:** slew, settle, expose the widefield camera, plate-solve with ASTAP. Optionally solve the C8/ZWO frame too, which measures the boresight offset between cameras.
3. **Record:** commanded and encoder axis angles, mid-exposure UTC, solved RA/Dec, side of zenith, temperature, pressure and focuser position.
4. **Reduce:** ERFA converts each solved position to apparent topocentric az/el, including refraction.
5. **Fit:** weighted least squares over a term library, with robust outlier rejection and significance tests to drop unsupported terms.
6. **Validate and apply:** residual RMS, residual sky map and term uncertainties shown in the GUI; the model is versioned and applied (inverse) on every pointing.
7. **Keep learning:** every plate solve in normal operation is logged as a candidate point.

| Term group | Examples | Kept across sites? |
| --- | --- | --- |
| Axis zero points | IA, IE | No, refit per site |
| Azimuth axis tilt | AN, AW | No, refit per site (an inclinometer can seed them) |
| Optical/mechanical | CA, NPAE, TF (tube flexure) | Yes |
| Encoder and bearing errors | Harmonic terms in az and el | Yes |
| Flip-side terms | Offsets for elevation > 90° | Yes |
| Camera boresight | Widefield-to-C8 offset | Yes, refit occasionally |

## Cameras and focus

- **Widefield (GigE, Aravis):** 1–10 fps, plate solving for the pointing model, satellite acquisition. Own USB-Ethernet port, static IP, jumbo frames if supported, raised socket receive buffer.
- **Narrow field (ZWO SDK):** region-of-interest capture at 50–200 fps during tracking; full frame for imaging and autofocus.
- **Live view:** compressed preview (JPEG or H.264) to the GUI over WiFi, never raw frames.
- **Locked primary:** no commercial mirror-lock kit for the standard C8 was found; options are a DIY clamp or an EdgeHD 8. Until then, flip-side pointing terms and the camera loop absorb mirror shift.
- **Focuser:** at f/10 the critical focus zone is about 270 µm, so any external focuser with 5–10 µm steps suffices; load capacity, low tilt and a temperature sensor matter more. Candidates: Starlight Feather Touch with MicroTouch motor, MoonLite CSL, PrimaLuce Esatto, or a crayford driven by a CiA 402 stepper drive on EtherCAT.
- **Autofocus:** half-flux-radius curve fit; triggered by temperature change, filter change, side flip, elapsed time and before scheduled passes; learned µm/°C compensation between runs.
- **Rotator:** deferred. The design leaves room for a third CSP axis for field derotation.

## Safety and autonomy

| Layer | Mechanism |
| --- | --- |
| Hardware | E-stop wired to iPOS STO inputs; hardware limit switches; brake if the tube is unbalanced |
| Drive | EtherCAT sync-manager watchdog triggers quick stop on lost communication |
| `mount-rt` | Soft limits, cable wrap, rate clamps, trajectory-starvation stop, heartbeat timeout |
| `mount-svc` | Horizon mask, sun avoidance as a hard veto, clock-quality gate, pass feasibility checks |
| Safety supervisor | Weather, UPS and heartbeat inputs trigger park (and close, once an enclosure exists); independent of GUI and network |
| Remote access | WireGuard or Tailscale, no open ports; network power switch; full logging |

## Genericity and portability

| Layer | Generic part | Reference-specific part |
| --- | --- | --- |
| Fieldbus | `Fieldbus` interface: scan, configure, cyclic exchange, DC sync | SOEM backend; simulated backend; IgH possible later |
| Drive | Vendor-neutral CiA 402 core: state machine, CSP/CSV, faults, homing | Drive descriptor YAML: identity, PDO map, startup SDOs, capabilities, units |
| Vendor quirks | Optional C++ hook plugins | `technosoft_ipos` plugin if needed |
| Axis | Scaling, gearing, limits, cable wrap, homing, rate limits | `system.yaml` axis entries |
| Kinematics | `Kinematics` plugin: sky ↔ axis angles, singular regions | `altaz` with over-the-top support |
| Pointing model | Term library selectable per kinematics | Fitted coefficients per site |
| Targets | `TargetProvider` interface | — |
| Devices | Camera, focuser, weather interfaces as Zenoh services; INDI and ASCOM Alpaca bridges | Aravis and ZWO backends |
| Interfaces | Versioned Protobuf schemas and documented Zenoh key expressions | — |

## Licensing

GPL-3.0-or-later, matching SOEM 2.x. Summary, not legal advice:

| Dependency | License | Fit with GPLv3 |
| --- | --- | --- |
| [SOEM 2.x](https://github.com/OpenEtherCATsociety/SOEM/blob/master/LICENSE.md) | GPLv3 or commercial (RT-Labs) | Compatible |
| IgH EtherCAT master (optional) | GPLv2 kernel module, LGPLv2.1 user library | User library compatible |
| Zenoh | EPL-2.0 or Apache-2.0 | Compatible via Apache-2.0 |
| ERFA | BSD-3-Clause | Compatible (used instead of SOFA) |
| Vallado SGP4 | Freely usable reference code | Compatible |
| ASTAP | External program | Not linked |

## Roadmap

1. **Platform:** PREEMPT_RT kernel, GPS/PPS + chrony, networking. *Gate:* `cyclictest` worst case < 100 µs under load; PPS-locked clock.
2. **Drive bring-up:** EasyMotion Studio setup (az sin/cos + Halls, el dual-loop), tuning, homing, encoder count-rate checks. *Gate:* both axes jog and home reliably.
3. **Real-time controller:** `mount-rt` with SOEM CSP, trajectory buffer, limits, telemetry, simulated backend. *Gate:* multi-hour 1 kHz run with zero lost frames.
4. **Astronomy and pointing:** ERFA, sidereal tracking, automated pointing model. *Gate:* site refit under 10 minutes with arcsecond-class residuals.
5. **Satellites:** SGP4, pass planner with flip mode, open-loop passes. *Gate:* ISS pass kept within the widefield field.
6. **Closed loop:** widefield acquisition, ZWO ROI tracking, offset corrections. *Gate:* target held in the C8 field for a full pass.
7. **Observatory:** focuser and autofocus, weather, power, safety supervisor. *Gate:* fault-injection suite passes.
8. **GUI and scheduler:** web GUI, night queue, unattended operation. *Gate:* a full night without intervention.

## Open items and risks

- [ ] Confirm iPOS4808 BX-CAT firmware supports CSP (mode 8) and objects 0x60B1/0x60B2.
- [ ] Confirm the iPOS accepts the ADRT sin/cos signal directly; record the encoder line count.
- [ ] Check REE2000 max output frequency and drive encoder input bandwidth against worst-case elevation rate.
- [ ] Measure azimuth cable-wrap travel allowed by the through-axis cables.
- [ ] Obtain a GNSS module with a PPS pin wired to a Pi GPIO.
- [ ] Check whether the ZWO and Imaging Source cameras have hardware trigger inputs or PTP.
- [ ] Choose a C8 primary lock approach and an external focuser.
- [ ] Measure payload inertia and axis torque limits to set pass-planner rate limits.

The biggest technical risk is Pi 4 real-time jitter with cameras and WiFi active; the fallback is
drive-side PVT mode with a slower camera loop.
