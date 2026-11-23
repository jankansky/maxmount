#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Write cloud-init first-boot files (user, password, SSH key, optional Wi-Fi) onto a freshly
# flashed Raspberry Pi OS (trixie or later) SD card, so the Pi can be reached headless.
#
# usage: deploy/pi/make-cloud-init.sh [BOOT_VOLUME]   (default /Volumes/bootfs)
set -euo pipefail

BOOT="${1:-/Volumes/bootfs}"
[[ -f "$BOOT/meta-data" ]] || { echo "no cloud-init files in $BOOT; is the card mounted?" >&2; exit 1; }

read -r -p "Username: " USERNAME
[[ "$USERNAME" =~ ^[a-z_][a-z0-9_-]*$ ]] || { echo "username must be lowercase letters, digits, _ or -" >&2; exit 1; }
read -r -p "Hostname [maxmount]: " HOSTNAME_; HOSTNAME_="${HOSTNAME_:-maxmount}"
read -r -p "Timezone [America/New_York]: " TZ_; TZ_="${TZ_:-America/New_York}"

while :; do
    read -r -s -p "Password: " P1; echo
    read -r -s -p "Password again: " P2; echo
    [[ -n "$P1" && "$P1" == "$P2" ]] && break
    echo "passwords empty or different; try again"
done
HASH="$(printf '%s' "$P1" | openssl passwd -6 -stdin)"
unset P1 P2

KEY_LINE=""
read -r -p "SSH public key file [~/.ssh/id_rsa.pub, '-' for none]: " KEYFILE
KEYFILE="${KEYFILE:-$HOME/.ssh/id_rsa.pub}"
if [[ "$KEYFILE" != "-" ]]; then
    KEYFILE="${KEYFILE/#\~/$HOME}"
    KEY_LINE="$(head -n1 "$KEYFILE")"
fi

yaml_sq() { local q="'"; printf "'%s'" "${1//$q/$q$q}"; }  # YAML single-quoted scalar

[[ -f "$BOOT/user-data.orig" ]] || cp "$BOOT/user-data" "$BOOT/user-data.orig"
{
    echo "#cloud-config"
    echo "hostname: $HOSTNAME_"
    echo "manage_etc_hosts: true"
    echo "timezone: $TZ_"
    echo "users:"
    echo "  - name: $USERNAME"
    echo "    groups: users,adm,dialout,audio,netdev,video,plugdev,input,gpio,i2c,spi,render,sudo"
    echo "    shell: /bin/bash"
    echo "    sudo: ALL=(ALL) NOPASSWD:ALL"
    echo "    lock_passwd: false"
    echo "    passwd: $(yaml_sq "$HASH")"
    if [[ -n "$KEY_LINE" ]]; then
        echo "    ssh_authorized_keys:"
        echo "      - $(yaml_sq "$KEY_LINE")"
    fi
    echo "ssh_pwauth: true"
    echo "runcmd:"
    echo "  - systemctl enable --now ssh"
} > "$BOOT/user-data"

read -r -p "Configure Wi-Fi? [y/N]: " WIFI
if [[ "$WIFI" =~ ^[Yy] ]]; then
    read -r -p "SSID: " SSID
    read -r -s -p "Wi-Fi password: " WPASS; echo
    read -r -p "Country code [US]: " CC; CC="${CC:-US}"
    [[ -f "$BOOT/network-config.orig" ]] || cp "$BOOT/network-config" "$BOOT/network-config.orig"
    {
        echo "network:"
        echo "  version: 2"
        echo "  wifis:"
        echo "    wlan0:"
        echo "      dhcp4: true"
        echo "      optional: true"
        echo "      regulatory-domain: $CC"
        echo "      access-points:"
        echo "        $(yaml_sq "$SSID"):"
        echo "          password: $(yaml_sq "$WPASS")"
    } > "$BOOT/network-config"
    unset WPASS
fi

# Check the YAML parses, if a YAML parser is available.
for f in user-data network-config; do
    if python3 -c 'import yaml' 2>/dev/null; then
        python3 -c 'import sys, yaml; yaml.safe_load(open(sys.argv[1]))' "$BOOT/$f" && echo "$f: YAML OK"
    elif command -v ruby >/dev/null; then
        ruby -ryaml -e 'YAML.load_file(ARGV[0])' "$BOOT/$f" && echo "$f: YAML OK"
    fi
done

echo
echo "Wrote $BOOT/user-data. Eject with:  diskutil eject $BOOT"
echo "After first boot (allow 2-3 minutes):  ssh $USERNAME@$HOSTNAME_.local"
