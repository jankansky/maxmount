# maxmount

An open-source observatory control system for EtherCAT-driven telescope mounts, built for
high-rate tracking of LEO satellites as well as astronomical targets. A Raspberry Pi with a
PREEMPT_RT kernel acts as the EtherCAT master and observatory computer; a web GUI provides
remote control.

> **Status:** early scaffold. The real-time controller core runs against a simulated
> CiA 402 drive on any POSIX machine. The SOEM EtherCAT backend, Python services,
> Zenoh bridge and GUI are not written yet.

## Architecture in one paragraph

`mount-rt` (C++20) runs a 1 kHz CiA 402 Cyclic Synchronous Position (CSP) loop on an isolated
CPU core. It receives time-tagged position/velocity trajectory points from `mount-svc`
(Python: astrometry, SGP4, pointing model, pass planner), interpolates them with cubic Hermite
splines, applies soft limits and rate limits, and streams telemetry. Services communicate over
[Zenoh](https://zenoh.io). Hardware specifics live in configuration files and small plugins, so
the same code can drive other CiA 402 EtherCAT drives and mount geometries. See
[`docs/`](docs/) for the full design.

## Repository layout

```
rt/        C++ real-time controller: cia402/, fieldbus/, sim/, trajectory/, axis/, core/, apps/
svc/       Python services (mount-svc, camera, safety, scheduler, gui-gateway)
gui/       Web frontend
schemas/   Protobuf messages and Zenoh key expressions
config/    Drive descriptors and complete system configurations
deploy/    Raspberry Pi setup: RT kernel, time (gpsd/chrony/PPS), networking
docs/      Design and porting guides
```

## Build and run (simulation)

```sh
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=RelWithDebInfo
cmake --build build
ctest --test-dir build --output-on-failure
./build/rt/mount-rt --duration 10
```

`mount-rt` currently runs two simulated axes through a demonstration trajectory and prints
tracking telemetry once per second. On Linux it will request `SCHED_FIFO` priority and lock
memory if permitted (`--rt-prio`, `--rt-cpu`).

## Reference hardware

Custom alt-az mount: Aerotech ADRT-150 direct-drive azimuth, Teknic Hudson motor with
Bell-Everman ServoBelt elevation stage and Renishaw tape encoder, two Technosoft
iPOS4808 BX-CAT drives, Raspberry Pi 4, Celestron C8, ZWO and The Imaging Source cameras.
See [`config/systems/maxmount-reference`](config/systems/maxmount-reference).

## License

GPL-3.0-or-later. See [LICENSE](LICENSE). The SOEM EtherCAT master (v2.x) is itself
licensed GPLv3 (or commercially by RT-Labs).
