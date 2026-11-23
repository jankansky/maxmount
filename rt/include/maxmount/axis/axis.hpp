// SPDX-License-Identifier: GPL-3.0-or-later
// Axis configuration, unit conversion, encoder unwrapping and the safety motion limiter.
#pragma once

#include <cstdint>
#include <limits>
#include <string>

namespace maxmount {

struct AxisLimits {
    double min_pos = -std::numeric_limits<double>::infinity();  // rad
    double max_pos = std::numeric_limits<double>::infinity();   // rad
    double max_vel = 1.0;                                        // rad/s
    double max_acc = 1.0;                                        // rad/s^2
};

struct AxisConfig {
    std::string name;
    double counts_per_rad = 1.0;       // drive position units per axis radian (sign = direction)
    double offset_rad = 0.0;           // axis angle at drive position 0
    double vel_units_per_rad_s = 0.0;  // velocity-offset units per rad/s; 0 disables feedforward
    AxisLimits limits{};
};

// Extends the drive's 32-bit position to 64 bits across wrap-arounds.
class PositionUnwrapper {
public:
    std::int64_t update(std::int32_t raw) noexcept {
        if (!initialized_) {
            initialized_ = true;
            accumulated_ = raw;
        } else {
            const auto delta = static_cast<std::int32_t>(static_cast<std::uint32_t>(raw) -
                                                         static_cast<std::uint32_t>(last_));
            accumulated_ += delta;
        }
        last_ = raw;
        return accumulated_;
    }

private:
    bool initialized_ = false;
    std::int32_t last_ = 0;
    std::int64_t accumulated_ = 0;
};

struct LimiterOutput {
    double pos = 0.0;
    double vel = 0.0;
    double acc = 0.0;
    bool limited = false;  // true when the request had to be modified
};

// Last line of defence between the trajectory and the drive. Enforces soft position limits
// (with a braking envelope), velocity and acceleration limits. A trajectory that respects the
// limits passes through unchanged.
class MotionLimiter {
public:
    explicit MotionLimiter(AxisLimits limits = {}) : lim_(limits) {}

    void reset(double pos) noexcept {
        p_ = pos;
        v_ = 0.0;
    }

    // Follow a target position with feedforward velocity.
    LimiterOutput track(double target_pos, double target_vel, double dt) noexcept;

    // Decelerate to rest at max acceleration and hold.
    LimiterOutput stop(double dt) noexcept;

    double position() const noexcept { return p_; }
    double velocity() const noexcept { return v_; }
    const AxisLimits& limits() const noexcept { return lim_; }

private:
    LimiterOutput finish(double v_des, double dt) noexcept;

    AxisLimits lim_;
    double p_ = 0.0;
    double v_ = 0.0;
};

}  // namespace maxmount
