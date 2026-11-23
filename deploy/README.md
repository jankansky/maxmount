# Raspberry Pi deployment (draft)

Target: Raspberry Pi 4B, 64-bit Raspberry Pi OS Lite (Debian 13 "Trixie") with Raspberry Pi's
PREEMPT_RT kernel package. This setup was tested with `6.18.60+rpt-rpi-v8-rt`.

## 0. First boot without a monitor

Raspberry Pi OS (Trixie) configures itself on first boot with cloud-init. After flashing the
card, run the following on the machine that flashed it (tested on macOS):

```sh
deploy/pi/make-cloud-init.sh            # default boot volume: /Volumes/bootfs
```

It asks for a username, password (stored only as a SHA-512 hash), SSH public key, hostname,
timezone and optional Wi-Fi network. It then writes `user-data` and `network-config` to the boot
partition, keeping the originals as `*.orig`. After the first boot, which takes 2-3 minutes,
log in with `ssh USER@HOSTNAME.local`. If setup fails, check `sudo cloud-init status --long`.

## 1. Real-time kernel

Raspberry Pi OS ships a PREEMPT_RT build of its Pi 4 kernel, so no kernel build is needed:

```sh
sudo apt install -y linux-image-rpi-v8-rt     # installs kernel8_rt.img + initramfs8_rt
sudo cp /boot/firmware/config.txt /boot/firmware/config.txt.bak
printf '\n[pi4]\nkernel=kernel8_rt.img\n[all]\n' | sudo tee -a /boot/firmware/config.txt
sudo reboot
uname -v                                      # should contain PREEMPT_RT
```

`auto_initramfs=1` (the default) loads `initramfs8_rt` automatically for that kernel. To go back
to the standard kernel, delete the `kernel=` line, which you can also do from another computer
by editing `config.txt` on the boot partition. Do not use Debian's generic `linux-image-rt-arm64`
kernel; it lacks Raspberry Pi drivers and device-tree setup.

Then reserve core 3 for `mount-rt` by appending to `/boot/firmware/cmdline.txt` (single line):

```
isolcpus=3 nohz_full=3 rcu_nocbs=3 irqaffinity=0-2
```

Use the `performance` CPU frequency governor so frequency changes don't stall the RT core.
Append to the same line in `cmdline.txt`:

```
cpufreq.default_governor=performance
```

Verify with `cyclictest -m -p 90 -a 3 -t 1 -i 1000 -D 1h` while cameras stream, WiFi is busy and
EtherCAT is running. The SOEM backend sends each frame 500 µs before SYNC0, so worst-case wake
latency must stay well below about 400 µs.

Measured on the reference Pi 4 (6.18.60 RT kernel, core 3 isolated, `performance` governor),
running `cyclictest` for 60 s alongside one `stress-ng` load at a time:

| Load on cores 0-2 | Max latency (µs) |
| --- | --- |
| none | 11 |
| `--cpu 3` | 32 |
| `--io 2` | 21 |
| `--memcpy 3` | 17 |
| `--vm 1 --vm-bytes 256M` | 101 |
| `--cpu 3 --io 2 --vm 1` started together | 187 (at stress start) |

Memory bandwidth (`memcpy`) barely affects the isolated core. What does is repeatedly mapping
and unmapping memory (`vm`), because on ARM each change to the page tables broadcasts
TLB invalidations to all cores. Services that run on the Pi should reuse frame buffers instead
of allocating a large array for every camera frame. glibc serves allocations above 128 KiB with
`mmap`/`munmap`.

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
