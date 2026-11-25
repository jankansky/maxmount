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
| Telescope | Celestron C8, primary mirror to be locked, Starizona SCT Corrector IV (0.63× reducer/coma corrector) | External motorized focuser |
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

The elevation axis reaching +180° does **not** remove the alt-az zenith keyhole. Every sky
direction can be reached as `(az, el)` or `(az + 180°, 180° − el)`, and the two coincide only at
the zenith. A pass that misses the zenith therefore has two exact tracking solutions with
identical azimuth rates. What the extra range adds is an "over-the-top" mode: azimuth is held
on the vertical great circle that best fits the pass and elevation sweeps through the zenith.
This trades the impossible azimuth rate of a near-zenith pass for a pointing error equal to the
pass's distance from that circle. (An earlier version of this document claimed the keyhole was
removed; the planner's tests check the corrected statement.)

- **Target providers:** sidereal (catalog RA/Dec with proper motion), satellites from TLE/OMM via SGP4 (CelesTrak, Space-Track), ephemeris tables (OEM/CPF), and fixed alt-az positions.
- **Astrometry:** ERFA for time scales, precession-nutation, aberration and refraction from measured pressure and temperature.
- **Pass planner:** plans each pass three ways: exact on the normal branch, exact on the flipped branch (which can suit the cable wrap or elevation limits better), and over-the-top with a bounded pointing error. It checks velocity, acceleration, cable wrap, horizon mask and Sun separation. It prefers an exact plan, falls back to over-the-top within a configured error tolerance, and otherwise rejects the pass.
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
- **Corrector:** Starizona SCT Corrector IV, 0.63×, 27 mm image circle. Retailers list a C8 at 1450 mm f/7.1 or f/6.8, and one user measured about 1260 mm; the actual focal length depends on the spacing and will come from plate solving. Back focus is 90.3 mm from the camera-side thread base, held to ±2 mm for large sensors. That budget must hold the rotator, filter wheel or off-axis guider, and the camera's own back focus. At 1260–1450 mm, 3.76 µm pixels sample 0.62–0.53″.
- **Focuser:** with the corrector at f/6.3–7.1 the critical focus zone is about 110–135 µm (270 µm at f/10), so any external focuser with 5–10 µm steps suffices; load capacity, low tilt and a temperature sensor matter more. Candidates: Starlight Feather Touch with MicroTouch motor, MoonLite CSL, PrimaLuce Esatto, or a crayford driven by a CiA 402 stepper drive on EtherCAT.
- **Autofocus:** half-flux-radius curve fit; triggered by temperature change, filter change, side flip, elapsed time and before scheduled passes; learned µm/°C compensation between runs.
- **Rotator:** needed early; see [Field rotator](#field-rotator).

## Field rotator

On an alt-az mount the field turns during an exposure at
ω = ω⊕ · cos(latitude) · cos(az) / cos(el). At 40° latitude: 0 due east or west, 0.0042 °/s
(15°/h) on the meridian at 40° elevation, 0.0064 °/s at 60°, 0.037 °/s at 85°. With a C8 at f/10
and an ASI533-class sensor (3.76 µm pixels, 8 mm centre to corner), one pixel of smear at the
corner is 0.027°. Without a rotator, sub-exposures near the meridian are therefore limited to a
few seconds. That makes a rotator a requirement for deep-sky imaging, not an option.

Requirements: continuous, smooth motion during the exposure with 0.01–0.02° tracking error; up
to ~0.04 °/s (long exposures within ~5° of the zenith stay impractical); continuous rotation or
enough range to plan around (more than 180° per night, plus 180° when the mount goes over the
top); low backlash; a known zero; ~1 kg load; fits the corrector's 90.3 mm back-focus budget together
with the filter wheel or off-axis guider and the camera. Thickness is therefore a selection
criterion. The pixel-smear numbers above depend only on the sensor, so the corrector does not
change them.

| | A. Third EtherCAT axis (preferred end state) | B. Commercial USB rotator (quickest) |
| --- | --- | --- |
| Hardware | Large-bore bearing with belt or worm, small servo or stepper with encoder, any small CiA 402 EtherCAT drive | Off the shelf (e.g. Pegasus Astro Falcon, Optec Pyxis/Gemini, ZWO CAA); check step size, backlash, load, back focus and Linux driver |
| Software | `mount-rt` already handles up to four axes from drive descriptors; `mount-svc` streams a `rot` trajectory with az and el, sharing timing, limits, following-error trip and telemetry | A `rotator-svc` sends position updates at ~1 Hz through INDI or the vendor protocol |
| Derotation | Synchronised with the mount at 1 kHz on the same clock | Adequate if it moves smoothly in fine steps |

`mount-svc` computes the rotator angle as the parallactic angle, plus 180° on the flipped
(over-the-top) branch, plus the camera's position-angle offset. For satellites the rotator can
instead align the frame with the direction of motion.

## Safety and autonomy

| Layer | Mechanism |
| --- | --- |
| Hardware | E-stop wired to iPOS STO inputs; hardware limit switches; brake if the tube is unbalanced |
| Drive | EtherCAT sync-manager watchdog triggers quick stop on lost communication |
| `mount-rt` | Soft limits, cable wrap, rate clamps, trajectory-starvation stop, heartbeat timeout |
| `mount-rt` (planned) | Sun keep-out zone enforced on every motion (slews, camera-loop corrections, hand offsets), required before any daytime imaging |
| `mount-svc` | Horizon mask, sun avoidance as a hard veto on plans, clock-quality gate, pass feasibility checks |
| Safety supervisor | Weather, UPS and heartbeat inputs trigger park (and close, once an enclosure exists); independent of GUI and network |
| Remote access | WireGuard or Tailscale, no open ports; network power switch; full logging |

## Operations and GUI

**Custom GUI, standard protocols.** Everything that makes this system distinctive has no
existing open-source GUI: fast movers, prediction combined with closed-loop camera tracking, a
1 kHz trajectory mount. Satellite tools such as Gpredict send coarse az/el commands to slow radio
rotators. Deep-sky sequencing is the exception: KStars/Ekos (INDI) and N.I.N.A. (ASCOM) are mature.
So the GUI is custom, and the mount also gets an **ASCOM Alpaca and INDI driver** early. That lets
those tools run deep-sky sequences from day one and sets the bar for the built-in sequencer.

**One engine, many target sources.** Every activity is "predict where to point, then correct
with a camera":

| Source | Prediction | Correction |
| --- | --- | --- |
| Star, galaxy | RA/Dec via ERFA | Pointing model, guiding |
| Satellite | TLE via SGP4 | Along-track time and cross-track offsets from the camera loop |
| Conjunction (CelesTrak SOCRATES) | Both TLEs, time of closest approach | Schedule both objects around that time |
| Aircraft | ADS-B position and velocity (RTL-SDR + readsb), extrapolated over the data latency | Camera loop |
| Bird (e.g. hawk) | None: the widefield camera detects it | Camera loop only (visual servoing) |

A **sequence/campaign engine** on top of `mount-svc` runs astrophotography sequences ("M31,
30 s subs, 2 h, refocus on temperature change") and tracking campaigns ("every pass of X
tonight", "the top SOCRATES conjunctions within reach"). It honours the same horizon, Sun,
clock and limit checks.

**GUI architecture.** A browser app on the laptop, served by `gui-gateway` on the Pi, which
bridges WebSocket to Zenoh. The browser never talks to Zenoh or `mount-rt` directly, so
authentication and a single-controller lock live in one place. Local network first. Remote
access only through a VPN (WireGuard/Tailscale) plus login, never an exposed port. Laptop-only,
so dense, full-featured layouts are fine.

**Panels, in build order:**

1. Status bar, always visible: service state, target, drives, clock error, GPS, Sun distance, connection; STOP and HOLD.
2. Sky view: all-sky az/el with horizon mask, mount, target, planned track, Sun, Moon, upcoming passes, keyhole zone.
3. Target: name search (bright stars, Messier/NGC, planets), RA/Dec, az/el go-to, park.
4. Satellites: TLE groups from CelesTrak, pass table (time, max elevation, plan mode, peak rates, countdown), track a pass.
5. Tracking control: along-track and cross-track adjustments, az/el pad with step sizes, reset.
6. Telemetry: command vs actual, following error, loop latency, bus errors.
7. Drives (commissioning and maintenance): states, decoded faults, reset, limit and home inputs, supervised jog.
8. Cameras: widefield and narrow previews, click to center, detection overlay.
9. Sequences and campaigns; pointing model runs and residuals; rotator.

Dark theme by default with a red night mode. Monospace figures for positions and times.
Safety-critical state is never more than a glance away.

**Compute.** Widefield motion detection fits on the Pi 4. Recording high-rate narrow-field
video needs a USB 3 SSD. Machine-learning detection (bird or not) needs an accelerator (Coral,
Hailo) or runs on the laptop. The camera service publishes over Zenoh, so either placement works.

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
5. **Satellites:** SGP4, pass planner with over-the-top mode, open-loop passes. *Gate:* ISS pass kept within the widefield field.
6. **GUI core:** gateway, status bar, sky view, targets, satellites, tracking control, telemetry, drives panel. *Gate:* a pass and a star tracked from the GUI alone.
7. **Field rotator:** rotator axis or service, parallactic-angle derotation. *Gate:* 120 s subframes at 60° elevation with ≤ 1 px corner smear.
8. **Interoperability:** ASCOM Alpaca/INDI mount driver. *Gate:* an Ekos or N.I.N.A. sequence runs end to end.
9. **Closed loop:** widefield acquisition, ZWO ROI tracking, offset corrections. *Gate:* target held in the C8 field for a full pass.
10. **Daytime safety:** Sun keep-out in `mount-rt` for all motion. *Gate:* no motion command, slew or offset can enter the zone.
11. **Sequencer and campaigns:** astrophotography sequences, SOCRATES campaigns. *Gate:* a full night without intervention.
12. **Fast movers:** ADS-B source; visual tracking with widefield detection. *Gate:* an aircraft and a bird held in the C8 field.
13. **Observatory:** focuser and autofocus, weather, power, safety supervisor. *Gate:* fault-injection suite passes.

## Open items and risks

- [x] iPOS4808 BX-CAT firmware supports CSP (0x6502 bit 7). It has no 0x60B1/0x60B2, so CSP runs without feedforward. Each PDO holds at most 64 bits.
- [ ] Choose the field rotator: third EtherCAT axis or commercial USB rotator.
- [ ] Confirm the iPOS accepts the ADRT sin/cos signal directly; record the encoder line count.
- [ ] Check REE2000 max output frequency and drive encoder input bandwidth against worst-case elevation rate.
- [ ] Measure azimuth cable-wrap travel allowed by the through-axis cables.
- [ ] Obtain a GNSS module with a PPS pin wired to a Pi GPIO.
- [ ] Check whether the ZWO and Imaging Source cameras have hardware trigger inputs or PTP.
- [ ] Choose a C8 primary lock approach and an external focuser.
- [ ] Measure payload inertia and axis torque limits to set pass-planner rate limits.

The biggest technical risk is Pi 4 real-time jitter with cameras and WiFi active; the fallback is
drive-side PVT mode with a slower camera loop.
