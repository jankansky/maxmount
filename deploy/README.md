# Raspberry Pi deployment (draft)

Target: Raspberry Pi 4B, 64-bit Raspberry Pi OS (Debian 13 "Trixie"), kernel 6.12+ built with
`CONFIG_PREEMPT_RT=y`.

## 1. Real-time kernel

Build the Raspberry Pi kernel (6.12 or later) with `CONFIG_PREEMPT_RT=y`; RT is in mainline
since 6.12, so no external patch is needed. Then reserve core 3 for `mount-rt` by appending to
`/boot/firmware/cmdline.txt` (single line):

```
isolcpus=3 nohz_full=3 rcu_nocbs=3 irqaffinity=0-2
```

Verify with `cyclictest -m -p 90 -a 3 -i 1000 -D 1h` while cameras stream, WiFi is busy and
EtherCAT is running. Target: worst-case latency well under 100 µs.

## 2. Network

| Interface | Role | Configuration |
| --- | --- | --- |
| `eth0` (onboard) | EtherCAT only | No IP; NetworkManager `unmanaged` |
| USB-Ethernet | GigE widefield camera | Static IP on its own subnet, jumbo frames if supported |
| `wlan0` | Laptop / operator | Access point in the field, client mode at a fixed site |

Raise socket buffers for GigE Vision: `net.core.rmem_max = 33554432`.

## 3. Time (GNSS + PPS)

- Wire the GNSS module's PPS output to GPIO 18 (3.3 V logic) and add
  `dtoverlay=pps-gpio,gpiopin=18` to `/boot/firmware/config.txt`.
- Check pulses with `ppstest /dev/pps0`.
- Run `gpsd` on the receiver's serial/USB port and use [`pi/chrony.conf`](pi/chrony.conf).
- The existing USB u-blox M8 (`1546:01a8`) provides position and a coarse time reference only.

## 4. Services

systemd units for `mount-rt`, `mount-svc` and the Zenoh router will be added as they are written.
