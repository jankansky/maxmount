# SPDX-License-Identifier: GPL-3.0-or-later
# Linux build environment matching the target (Debian 13 "Trixie"; use an arm64 host or
# --platform linux/arm64 for a Raspberry Pi 4-equivalent build).
FROM debian:trixie
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake ninja-build git ca-certificates \
        libyaml-cpp-dev libgtest-dev iproute2 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
