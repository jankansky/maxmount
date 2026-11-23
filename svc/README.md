# maxmount services (planned)

Python services that run on the Pi's non-real-time cores and talk over Zenoh.

| Service | Responsibility |
| --- | --- |
| `mount-svc` | Target providers (sidereal, SGP4, ephemeris tables), ERFA astrometry, pointing model, pass planner (normal / over-the-top), trajectory generation |
| `camera-svc` | ZWO and GigE capture, centroiding, plate solving (ASTAP), autofocus |
| `supervisor` | Safety (sun, weather, clock quality, heartbeats) and the night scheduler |
| `gui-gateway` | Serves the web GUI; REST + WebSocket bridge to Zenoh |

Nothing is implemented yet; this package is a placeholder for the layout.
