# Zenoh key expressions

All keys live under `maxmount/<site>/`, where `<site>` comes from the `zenoh.site` entry of
the system file. Payloads are Protobuf messages from [`maxmount/v1`](maxmount/v1/) unless
noted. The `mount/*` keys are implemented by `mount-rt` (`rt/src/bridge`) and the `svc/*` keys
by `mount-svc serve` (`svc/src/maxmount_svc/service.py`, messages in `target.proto`); the rest
are planned.

| Key | Kind | Payload | Producer → consumer |
| --- | --- | --- | --- |
| `mount/trajectory` | put | `TrajectoryUpdate` | mount-svc → mount-rt (serve mode only) |
| `mount/cmd` | queryable | `MountCommand` → `CommandReply` | GUI, scheduler → mount-rt |
| `mount/telemetry` | pub (50 Hz) | `MountTelemetry` | mount-rt → all |
| `mount/telemetry/raw` | pub (every cycle, in batches of 50) | `MountTelemetryBatch` | mount-rt → loggers, camera-svc |
| `svc/target` | queryable | `TargetCommand` → `TargetReply` (track, go to, offsets, stop, axis jog) | GUI, scheduler, camera loop → mount-svc |
| `svc/passes` | queryable | `PassesRequest` → `PassesReply` | GUI, scheduler → mount-svc |
| `svc/status` | pub (2 Hz) | `ServiceStatus` | mount-svc → GUI, scheduler |
| `camera/<id>/status` | pub (1 Hz and on change) | `CameraStatus` (camera.proto) | camera service (`mount-svc dslr`: id `dslr`; `mount-svc zwo`: id `zwo`) → GUI |
| `camera/<id>/frame` | pub | `CameraFrame`: live view JPEG, and each capture's preview with histogram | camera service → GUI |
| `camera/<id>/cmd` | queryable | `CameraCommand` → `CameraReply` (settings, capture, bulb, live view, save to, abort, SER recording) | GUI → camera service |
| `camera/<id>/centroid` | pub | measured offset + mid-exposure time (TBD) | camera-svc → mount-svc |
| `time/status` | pub | clock source, offset, error estimate (TBD) | time monitor → mount-svc, GUI |
| `safety/status` | pub | safe/unsafe + reasons (TBD) | supervisor → all |
| `weather/*` | pub | sensor readings (TBD) | weather service → supervisor |

Rule: `mount-rt` never calls Zenoh from its real-time thread. Zenoh callbacks only decode,
validate and queue. The main thread, the controller's single command producer, drains that
queue into the controller's lock-free ring and publishes telemetry.

`mount/cmd` replies with `accepted: false` and a reason when `mount-rt` is not in serve mode
(monitor, jog, demo), for an unknown axis, or when enabling an axis that is not commissioned.
In serve mode a trajectory that runs out (for example when mount-svc stops) makes the axes
decelerate and hold, so a crashed client cannot leave the mount moving.
