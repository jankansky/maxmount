# maxmount web GUI

A browser app served by the GUI gateway (`mount-svc gui`) on the Pi. It talks only to the
gateway, over one WebSocket. The gateway bridges to Zenoh, so the browser never reaches
`mount-rt` or `mount-svc` directly.

```sh
SYS=config/systems/sim-altaz/system.yaml           # or your system file
build/rt/mount-rt --system $SYS --serve &           # real-time controller
svc/.venv/bin/mount-svc --system $SYS serve &       # targets and tracking
svc/.venv/bin/mount-svc --system $SYS gui           # http://<pi>:8080
```

**Local network only for now:** there is no login yet. Bind it to the observatory network
(`--host`), and use a VPN (WireGuard/Tailscale) for remote access.

## The Track screen

- **Header:**
  - status line with the latest event or reply;
  - chips for clock error (from chrony), site source, EtherCAT lost frames and Sun elevation;
  - **Dark / Red** theme switch, remembered by the browser.
- **Left column:**
  - mount position (az/el, RA/Dec and LST computed by the gateway, UTC);
  - drive and trip indicators;
  - **STOP:** quick stop sent straight to `mount-rt`, then the target is cleared;
  - **Hold** and **Park**;
  - target card and the **Choose target…** dialog (RA/Dec or az/el).
- **Centre:**
  - widefield and narrow-field camera panels (placeholders until `camera-svc` exists);
  - corrections bar: along-track timing, perpendicular, az/el nudge, all applied live through
    `mount-svc` offsets;
  - upcoming passes for a watch list from a CelesTrak group, with each plan's mode and peak rate,
    and **Track**. Click a pass (or select it and press Enter) to preview it on the sky dome and
    the ground track; click it again or press **Clear** to go back to the live target.
    **Track** is refused while chrony's clock error bound is above `time.max_clock_error_us`
    (10 µs needs GPS PPS). A warning above the table then shows the bound and what it means
    along the track, with a **Track anyway** box that sends `allow_clock_error`.
- **Right column:**
  - sky dome (north up, east left; horizon mask, keyhole, planned track, target, mount, Sun);
  - satellite ground track with visibility circle and night side;
  - pass preview, in orange: on the sky dome, the path from AOS to LOS, solid where the
    satellite is sunlit and dotted in shadow; on the map, zoomed to the pass, the ground track
    above the horizon mask, 15 min either side, and the visibility circle at culmination;
  - target telemetry (rates, range, range rate, altitude, TLE age);
  - strip charts with grid lines: following error, and loop wake-up latency.

## The Drives screen (`#drives`)

For commissioning and maintenance.

- **Bus panel:**
  - EtherCAT backend, interface and cycle;
  - loop cycles, overruns, lost frames, working-counter errors;
  - inputs-fresh, any-enabled, trip and fault indicators;
  - wake-up latency chart;
  - **STOP**, **Fault reset** (all drives, also clears a following-error trip) and **Disable all**.
- **One card per axis in the system file:**
  - CiA 402 state ladder, decoded statusword (0x6041);
  - position in the axis frame (raw counts until `counts_per_rev` is set) against the soft limits;
  - controller flags, limit and home inputs (0x60FD);
  - following-error chart;
  - **Fault:** the error code (0x603F, read by SDO when the drive faults) and the drive's vendor
    error register, decoded bit by bit (Technosoft MER, object 0x2000; the descriptor's
    `diagnostics.error_register`). The last fault stays shown after a reset;
  - configuration from the system file;
  - **Enable / Disable** (straight to `mount-rt`);
  - **Jog:** relative step or absolute **Move to**, at a chosen speed, in the axis frame, through
    `mount-svc` (it owns the trajectory stream). The axis is enabled if needed and holds
    afterwards. Refused while a target is active, outside the soft limits, or while the previous
    jog is moving. On hardware, motion must be armed first (a checkbox that clears on a fault
    or trip).
- **Event log:** state changes, faults and trips seen by this browser.

## Implementation

- **No build step:** plain ES modules plus [Preact](https://preactjs.com) and
  [htm](https://github.com/developit/htm). The pinned standalone bundle,
  `vendor/preact-htm-standalone.module.js` from htm 3.1.1, is kept in the repo with its licenses
  (htm: Apache-2.0, Preact: MIT). It works offline in the field.
- **htm does not know HTML void elements:** write `<input ... />`, `<br />`. An unclosed
  `<input>` swallows the following elements, and leftover closing tags appear as stray text.
- **Coastlines:** Natural Earth 1:110m land (public domain), `vendor/ne_110m_land.json`.
- **Theming:** all colours are CSS variables (`styles.css`), and the canvas drawings read them,
  so the Dark/Red switch covers charts and maps too.
- **Smoke test:** `svc/tests/test_gui_smoke.py` drives the whole simulated stack in headless
  Chromium (Track screen, and a jog on the Drives screen) and fails on any JavaScript error. It needs `pip install playwright` and
  `playwright install chromium`; otherwise it is skipped.
