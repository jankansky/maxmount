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
    and **Track**.
- **Right column:**
  - sky dome (north up, east left; horizon mask, keyhole, planned track, target, mount, Sun);
  - satellite ground track with visibility circle and night side;
  - target telemetry (rates, range, range rate, altitude, TLE age);
  - strip charts with grid lines: following error, and loop wake-up latency.

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
  Chromium and fails on any JavaScript error. It needs `pip install playwright` and
  `playwright install chromium`; otherwise it is skipped.
