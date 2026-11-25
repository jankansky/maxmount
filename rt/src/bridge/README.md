# Zenoh bridge

`ZenohBridge` connects `mount-rt` to the rest of the observatory over [Zenoh](https://zenoh.io)
1.10 using the Protobuf messages in [`schemas/maxmount/v1`](../../../schemas/maxmount/v1/mount.proto)
and the keys in [`schemas/zenoh-keys.md`](../../../schemas/zenoh-keys.md):

| Key (under `maxmount/<site>/`) | Direction | Message |
| --- | --- | --- |
| `mount/trajectory` | put → mount-rt | `TrajectoryUpdate` (serve mode only) |
| `mount/cmd` | query → mount-rt | `MountCommand` → `CommandReply` |
| `mount/telemetry` | mount-rt → pub, 50 Hz | `MountTelemetry` with loop statistics |
| `mount/telemetry/raw` | mount-rt → pub, every cycle in batches of 50 | `MountTelemetryBatch` |

## Threads

The real-time thread never touches Zenoh. Zenoh callbacks run on Zenoh's own threads. They
decode, validate and append to a mutex-protected queue, and reply to command queries at once
(`accepted` means queued; the effect shows in telemetry). The `mount-rt` main thread, the
controller's only command producer, drains that queue into the controller's lock-free ring
every 5 ms in serve mode and publishes telemetry. Disable and QuickStop jump the queue ahead of
buffered trajectory points. `mount-rt` moves itself and the Zenoh threads off the isolated
real-time CPU.

## Safety rules

- Only `mount-rt --serve` accepts trajectories and commands. Monitor, jog and demo modes
  publish telemetry and reject commands.
- Enabling an axis requires `counts_per_rev` in the system file and, on hardware,
  `max_following_error_deg`. It is refused while a following-error trip is latched or the
  drive is in Fault.
- If the trajectory runs out (client crashed or the network dropped), the axis decelerates
  at its limits and holds.
- On shutdown the bridge stops before the drives are disabled, so nothing can re-enable them.

## Building

`-DMAXMOUNT_WITH_ZENOH=ON` needs Protobuf (`protobuf-compiler libprotobuf-dev` on Debian,
`brew install protobuf` on macOS). zenoh-c and zenoh-cpp 1.10.2 are used from an installed
package if CMake finds one; otherwise the pinned, SHA-256-checked prebuilt release archives are
downloaded and zenoh-c is linked statically.

## Trying it

```sh
build/rt/mount-rt --system config/systems/sim-altaz/system.yaml --serve      # terminal 1
build/rt/mount-ctl --system config/systems/sim-altaz/system.yaml watch       # terminal 2
build/rt/mount-ctl --system config/systems/sim-altaz/system.yaml enable el
build/rt/mount-ctl --system config/systems/sim-altaz/system.yaml move el --by 10 --vel 5
build/rt/mount-ctl --system config/systems/sim-altaz/system.yaml disable
```

From another machine, add `--connect tcp/<pi-address>:7447` and give `mount-rt` a matching
`listen` endpoint in the system file's `zenoh:` section, or rely on multicast scouting on the
same LAN.
