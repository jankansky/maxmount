// SPDX-License-Identifier: GPL-3.0-or-later
// Time helpers. Two time bases are used:
//  - monotonic: std::steady_clock (== CLOCK_MONOTONIC on Linux), for loop scheduling only.
//  - realtime:  CLOCK_REALTIME, nanoseconds since the Unix epoch (POSIX/UTC). All trajectory
//               and telemetry timestamps use this base; it is GPS/PPS-disciplined via chrony.
#pragma once

#include <chrono>
#include <cstdint>
#include <ctime>

namespace maxmount {

using nanos = std::int64_t;

inline constexpr nanos kNsPerSec = 1'000'000'000;

inline nanos to_ns(const timespec& ts) noexcept {
    return static_cast<nanos>(ts.tv_sec) * kNsPerSec + static_cast<nanos>(ts.tv_nsec);
}

inline timespec to_timespec(nanos ns) noexcept {
    timespec ts{};
    ts.tv_sec = static_cast<time_t>(ns / kNsPerSec);
    ts.tv_nsec = static_cast<long>(ns % kNsPerSec);
    if (ts.tv_nsec < 0) {
        ts.tv_nsec += kNsPerSec;
        ts.tv_sec -= 1;
    }
    return ts;
}

inline nanos realtime_ns() noexcept {
    timespec ts{};
    clock_gettime(CLOCK_REALTIME, &ts);
    return to_ns(ts);
}

inline nanos monotonic_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

inline constexpr double ns_to_s(nanos ns) noexcept { return static_cast<double>(ns) * 1e-9; }

}  // namespace maxmount
