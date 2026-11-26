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

The header is shared by every screen:
- status line with the latest event or reply;
- chips for clock error (from chrony), site source, EtherCAT lost frames and Sun elevation;
- **Dark / Red** theme switch, remembered by the browser.

Screens: **Passes** (`#passes`) to choose a satellite pass, **Track** (the default) to follow
it, **Drives** (`#drives`).

## The Passes screen (`#passes`)

- **Ground track** (left, large) and **Sky** (right), sized to the window. With a pass
  selected, the map zooms to it: thick line above the horizon mask (AOS to LOS), thin 15 min
  either side, the visibility circle at culmination, and the site. The sky dome shows the path
  from AOS to LOS, solid where the satellite is sunlit and dotted in shadow. With nothing
  selected they show the live target. The map shades daylight, civil, nautical and
  astronomical twilight and night, and marks the subsolar point: at the pass's highest point
  for a preview, otherwise now.
- **Upcoming passes** from a CelesTrak group, with each plan's mode and peak rate, and **Track**.
  - **Objects** is either the watch list or **all in group** (every object, up to 400;
    passes are planned when you select one).
  - **Watch** takes names, parts of names (`ISS`, `noaa 19`) or catalog numbers (`25544`),
    comma separated; **Add** picks names from the group. Docked modules with the same passes
    (ISS, CSS) are merged into one row (`+1`).
  - **Lighting:** *sunlit* keeps passes where the satellite is in sunlight for part of the
    pass; *sunlit, dark sky* also needs the Sun below −6° at culmination. The **Sunlit** column
    gives the sunlit share of each pass (☀: the sky is still bright); hover for the Sun's
    elevation.
  - **Mag** and **Size:** brightest predicted visual magnitude while sunlit, and the largest
    angular size (largest dimension over the closest range). From Mike McCants'
    `quicksat.mag` (downloaded monthly by the gateway) plus `config/satellites.yaml`; values
    marked ≈ are estimated from the radar cross-section; see `svc/src/maxmount_svc/satcat.py`
    for the model. Many recent objects (CSS, Starlink) have no data until added to
    `config/satellites.yaml`. The **Mag** filter hides passes fainter than the limit, and
    passes without a magnitude. Flares and tumbling are not predicted.
  - **Max el ≥** hides low passes. Changing a selector searches again; after editing Watch,
    press Enter or **Refresh**. An empty list says why.
  - Click a pass (or select it and press Enter) to preview it; click it again or press
    **Clear** to go back to the live target.
  - **Track** names the pass by its rise time, so the right pass is tracked even after
    earlier ones in the list have gone by. Once accepted, **Open the Track screen →** appears.
  - **Track** is refused while chrony's clock error bound is above `time.max_clock_error_us`
    (10 µs needs GPS PPS). A warning above the table then shows the bound and what it means
    along the track, with a **Track anyway** box that sends `allow_clock_error`.
- **Pass details:** AOS, culmination and LOS times and directions, duration, lighting, the
  chosen plan and its peak rates, or why no plan fits.

## The Track screen

- **Left column:**
  - mount position (az/el, RA/Dec and LST computed by the gateway, UTC);
  - drive and trip indicators;
  - **STOP:** quick stop sent straight to `mount-rt`, then the target is cleared;
  - **Hold** and **Park**;
  - target card and the **Choose target…** dialog (RA/Dec or az/el).
- **Centre:**
  - widefield and narrow-field camera panels (placeholders until `camera-svc` exists);
  - corrections bar: along-track timing, perpendicular, az/el nudge, all applied live through
    `mount-svc` offsets.
- **Right column:**
  - sky dome (north up, east left; horizon mask, keyhole, planned track, target, mount, Sun)
    and the live target's ground track with visibility circle and night side;
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

Dropdowns use `Select` (`lib/ui.js`), which builds its options once: the GUI re-renders about
ten times a second, and rebuilding the options each time closed or reset an open dropdown.


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
