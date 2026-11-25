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

```sh
sudo setcap cap_net_raw,cap_net_admin,cap_sys_nice,cap_ipc_lock+ep build/rt/mount-rt
build/rt/mount-rt --soem eth0 --descriptor config/drives/technosoft_ipos4808_bx_cat.yaml \
                  --drives 2 --rt-cpu 3 --duration 0
```

This is **monitor mode**: it brings the bus to OP and prints each drive's CiA 402 state,
statusword, error code and position once per second. It never enables the drives.

## Not done yet

- Slave loss / recovery handling (re-config after a cable is unplugged), as in SOEM's
  `ecatcheck`.
- Mapping DC time to UTC so commanded positions are tied to the PPS-disciplined clock.
- Per-axis bus positions and descriptors from `config/systems/*/system.yaml`
  (monitor mode currently uses one descriptor for drives at positions 0..N-1).
