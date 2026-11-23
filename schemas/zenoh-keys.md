# Zenoh key expressions (draft)

All keys live under `maxmount/<site>/`. Payloads are Protobuf messages from
[`maxmount/v1`](maxmount/v1/) unless noted.

| Key | Kind | Payload | Producer → consumer |
| --- | --- | --- | --- |
| `mount/trajectory` | put | `TrajectoryUpdate` | mount-svc → mount-rt |
| `mount/cmd` | queryable | `MountCommand` → `CommandReply` | GUI, scheduler → mount-rt |
| `mount/telemetry` | pub (50 Hz) | `MountTelemetry` | mount-rt → all |
| `mount/telemetry/raw` | pub (batched 1 kHz) | `MountTelemetryBatch` | mount-rt → loggers, camera-svc |
| `mount/target` | queryable / pub | target and tracking state (TBD) | mount-svc |
| `camera/<id>/preview` | pub | JPEG/H.264 frame (TBD) | camera-svc → GUI |
| `camera/<id>/centroid` | pub | measured offset + mid-exposure time (TBD) | camera-svc → mount-svc |
| `time/status` | pub | clock source, offset, error estimate (TBD) | time monitor → mount-svc, GUI |
| `safety/status` | pub | safe/unsafe + reasons (TBD) | supervisor → all |
| `weather/*` | pub | sensor readings (TBD) | weather service → supervisor |

Rule: `mount-rt` never calls Zenoh from its real-time thread. A bridge thread moves data
between Zenoh and the controller's lock-free rings.
