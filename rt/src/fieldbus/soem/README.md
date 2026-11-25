# SOEM backend

`SoemFieldbus` implements `maxmount::Fieldbus` on [SOEM](https://github.com/OpenEtherCATsociety/SOEM)
v2.0.1 (GPLv3). Linux only; build with `-DMAXMOUNT_WITH_SOEM=ON` (needs CMake ≥ 3.28 for SOEM).

## Start-up sequence

1. `ecx_init` opens a raw socket on the configured interface (`eth0`, EtherCAT only).
2. `ecx_config_init` scans the bus. Each configured drive is checked against its descriptor's
   `vendor_id` / `product_code` when those are set.
3. `ecx_configdc` measures propagation delays and configures distributed clocks.
4. `ecx_config_map_group` calls our PRE-OP → SAFE-OP hook for each drive, which writes:
   startup SDOs, then the PDO mapping (`0x1C12`/`0x1C13` assignment, `0x16xx`/`0x1Axx`
   entries), then activates DC SYNC0. SOEM then requests SAFE-OP.
5. The process-image size reported by each drive is checked against the descriptor's mapping.
6. Outputs are zeroed (controlword 0, so drives stay disabled) and the bus goes to OP.

## Cyclic operation

`receive()` reads the frame sent by the previous `send()`, decodes inputs with `PdoCodec`, and
counts lost frames / working-counter errors. The DC time in each frame feeds a PI loop
(from SOEM's `ec_sample`) whose output, `cycle_correction_ns()`, shifts the host loop's next
wake-up so frames leave a fixed offset after SYNC0.

## Running on hardware

`mount-rt` needs `CAP_NET_RAW` (raw socket), `CAP_NET_ADMIN` (promiscuous mode),
`CAP_SYS_NICE` (SCHED_FIFO) and `CAP_IPC_LOCK` (mlockall). Configure with
`-DMAXMOUNT_SETCAP=ON` to re-apply them after every build, or run
`sudo setcap cap_net_raw,cap_net_admin,cap_sys_nice,cap_ipc_lock+ep build/rt/mount-rt`.

```sh
build/rt/mount-rt --system config/systems/maxmount-reference/system.yaml            # monitor
build/rt/mount-rt --system config/systems/maxmount-reference/system.yaml --axes az  # one drive
build/rt/mount-rt --soem eth0 --descriptor config/drives/technosoft_ipos4808_bx_cat.yaml  # ad hoc
```

**Monitor mode** brings the bus to OP and prints each drive's CiA 402 state, statusword,
position, following error (0x60F4) and digital inputs (0x60FD) once per second. It never
enables the drives. When a drive enters Fault its error code (0x603F, not PDO-mappable on
the iPOS) is read once by SDO and printed.

**Jog mode** (`--jog AXIS --by DEG [--vel DEG_S] [--acc DEG_S2]`) needs the axis's
`counts_per_rev` and `max_following_error_deg` in the system file. It checks for faults,
refuses targets outside the soft limits, asks for confirmation, enables only that axis,
runs a trapezoidal move, holds for a second and disables. A following error above the limit
quick-stops all axes and latches until a fault reset.

Verified on the reference Pi 4 with one iPOS4808 BX-CAT (fw v10), 2026-11-25: OP with DC
SYNC0 at 1 kHz, 0 lost frames and 0 working-counter errors over 15 000 cycles, wake-up
latency below 25 µs.

## Not done yet

- Slave loss / recovery handling (re-config after a cable is unplugged), as in SOEM's
  `ecatcheck`.
- Mapping DC time to UTC so commanded positions are tied to the PPS-disciplined clock.
- Velocity feedforward scaling (the iPOS has no 0x60B1/0x60B2; other drives may).
