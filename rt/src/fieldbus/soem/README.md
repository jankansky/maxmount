# SOEM backend (planned)

`SoemFieldbus` will implement `maxmount::Fieldbus` using [SOEM](https://github.com/OpenEtherCATsociety/SOEM) 2.x (GPLv3):

1. Open the raw socket on the configured interface (`eth0`, EtherCAT only).
2. Scan slaves and match each against a drive descriptor (`config/drives/*.yaml`) by vendor ID and product code.
3. Apply the descriptor's startup SDOs and PDO mapping in PRE-OP → SAFE-OP.
4. Configure distributed clocks (SYNC0 = cycle period) and align the DC reference to the
   PPS-disciplined system clock.
5. Bring the bus to OP and map each drive's process image to `Cia402Inputs` / `Cia402Outputs`.
6. `exchange()`: send process data, receive with timeout, check the working counter, update stats.

It builds only on Linux, behind `-DMAXMOUNT_WITH_SOEM=ON`.
