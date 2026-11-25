# maxmount

An open-source observatory control system for EtherCAT-driven telescope mounts, built for
high-rate tracking of LEO satellites as well as astronomical targets. A Raspberry Pi with a
PREEMPT_RT kernel acts as the EtherCAT master and observatory computer; a web GUI provides
remote control.

> **Status:** early development. The real-time controller runs against a simulated CiA 402
> drive on any POSIX machine. On the reference Pi 4 the SOEM EtherCAT backend brings a
> Technosoft iPOS4808 to OP at 1 kHz with distributed clocks (monitor mode). Powered motion on
> hardware awaits axis commissioning. The Python services and GUI are not written yet. The
> Zenoh bridge (`--serve`) accepts trajectories and commands and publishes
> telemetry; `mount-ctl` is a command-line client for it.

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

Without options `mount-rt` runs a built-in two-axis simulation through a demonstration
trajectory and prints tracking telemetry once per second. On Linux it requests `SCHED_FIFO`
priority, locks memory and pins the loop to a CPU if permitted. Other examples:

```sh
./build/rt/mount-rt --system config/systems/sim-altaz/system.yaml --jog el --by -5 --vel 2
./build/rt/mount-rt --help
```

### EtherCAT (Linux)

```sh
# Fetches SOEM v2.0.1 (needs CMake >= 3.28). MAXMOUNT_SETCAP re-applies the capabilities
# mount-rt needs after every link, using passwordless sudo.
cmake -S . -B build -G Ninja -DMAXMOUNT_WITH_SOEM=ON -DMAXMOUNT_SETCAP=ON
cmake --build build
./build/rt/mount-rt --system config/systems/maxmount-reference/system.yaml --axes az
```

On an EtherCAT system `mount-rt` defaults to **monitor mode**: it brings the bus to OP and
reports drive state, position, following error and digital inputs without ever enabling the
drives. `--axes` selects the drives present on a test bench. Once an axis is commissioned
and its `counts_per_rev` is set in the system file, `--jog AXIS --by DEG` performs a
supervised move of that axis alone. Ctrl-C decelerates and disables; press it twice to
quick-stop. See [`rt/src/fieldbus/soem/README.md`](rt/src/fieldbus/soem/README.md).

### Zenoh bridge

```sh
cmake -S . -B build -G Ninja -DMAXMOUNT_WITH_ZENOH=ON   # needs Protobuf; fetches zenoh-c 1.10.2
cmake --build build
./build/rt/mount-rt --system config/systems/sim-altaz/system.yaml --serve
./build/rt/mount-ctl --system config/systems/sim-altaz/system.yaml watch     # another terminal
```

`--serve` accepts trajectories and commands from `mount-svc`, the GUI or `mount-ctl`. With a
`zenoh:` section in the system file every other mode publishes telemetry too. See
[`rt/src/bridge/README.md`](rt/src/bridge/README.md).

To build and test the Linux configuration from macOS, use the container in
[`tools/docker`](tools/docker/linux-build.Dockerfile) (arm64 matches the Pi 4):

```sh
docker build -t maxmount-linux-build -f tools/docker/linux-build.Dockerfile tools/docker
docker run --rm -v "$PWD":/src:ro maxmount-linux-build bash -c \
  'cmake -S /src -B /tmp/b -G Ninja -DMAXMOUNT_WITH_SOEM=ON -DMAXMOUNT_WITH_ZENOH=ON && cmake --build /tmp/b && ctest --test-dir /tmp/b'
```

## Reference hardware

Custom alt-az mount: Aerotech ADRT-150 direct-drive azimuth, Teknic Hudson motor with
Bell-Everman ServoBelt elevation stage and Renishaw tape encoder, two Technosoft
iPOS4808 BX-CAT drives, Raspberry Pi 4, Celestron C8, ZWO and The Imaging Source cameras.
See [`config/systems/maxmount-reference`](config/systems/maxmount-reference).

## License

GPL-3.0-or-later. See [LICENSE](LICENSE). The SOEM EtherCAT master (v2.x) is itself
licensed GPLv3 (or commercially by RT-Labs).
