// SPDX-License-Identifier: GPL-3.0-or-later
// Time-tagged trajectory points and cubic Hermite interpolation. mount-svc sends points at
// 20-50 Hz; the RT loop samples the spline every cycle, which matches the smoothness of
// drive-side PVT mode while keeping CSP's low latency.
#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

#include "maxmount/common/time.hpp"

namespace maxmount {

struct TrajPoint {
    nanos t = 0;        // realtime ns (UTC, Unix epoch)
    double pos = 0.0;   // axis units (rad)
    double vel = 0.0;   // axis units per second
};

struct KinematicSample {
    double pos = 0.0;
    double vel = 0.0;
    double acc = 0.0;
};

// Cubic Hermite segment between a and b (a.t < b.t), evaluated at t.
KinematicSample hermite(const TrajPoint& a, const TrajPoint& b, nanos t) noexcept;

enum class SampleStatus : std::uint8_t {
    Ok,
    Empty,        // no points buffered
    BeforeStart,  // t precedes the first point
    Starved,      // t is past the last point
};

struct BufferedSample {
    SampleStatus status = SampleStatus::Empty;
    KinematicSample s{};
};

// Fixed-capacity, allocation-free point buffer owned by the RT thread.
class TrajectoryBuffer {
public:
    static constexpr std::size_t kCapacity = 2048;

    // Appends p. Buffered points at or after p.t are discarded first, so a new stream
    // replaces the buffered future. Returns false if the buffer is full.
    bool push(const TrajPoint& p) noexcept;
    void clear() noexcept;

    // Samples at t and discards segments that ended before t.
    BufferedSample sample(nanos t) noexcept;

    std::size_t size() const noexcept { return count_; }
    nanos horizon() const noexcept { return count_ == 0 ? 0 : at(count_ - 1).t; }

private:
    const TrajPoint& at(std::size_t i) const noexcept { return buf_[(head_ + i) % kCapacity]; }

    std::array<TrajPoint, kCapacity> buf_{};
    std::size_t head_ = 0;
    std::size_t count_ = 0;
};

}  // namespace maxmount
