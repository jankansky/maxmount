// SPDX-License-Identifier: GPL-3.0-or-later
// Rest-to-rest point-to-point move with a trapezoidal velocity profile (triangular when the
// distance is too short to reach max_vel). Used for jogs and slews; sampled into
// time-tagged trajectory points on the non-real-time side.
#pragma once

#include <algorithm>
#include <cmath>

namespace maxmount {

class MoveProfile {
public:
    struct State {
        double pos;
        double vel;
    };

    // max_vel and max_acc must be > 0.
    MoveProfile(double from, double to, double max_vel, double max_acc) noexcept
        : p0_(from), dir_(to >= from ? 1.0 : -1.0), dist_(std::abs(to - from)), a_(max_acc) {
        // Reach max_vel only if the distance covers both ramps; otherwise peak lower.
        v_ = std::min(max_vel, std::sqrt(dist_ * a_));
        t_ramp_ = v_ / a_;
        t_cruise_ = v_ > 0 ? (dist_ - v_ * t_ramp_) / v_ : 0.0;
    }

    double duration() const noexcept { return 2 * t_ramp_ + t_cruise_; }
    double peak_velocity() const noexcept { return v_; }

    // t in seconds from the start of the move; clamped to [0, duration].
    State at(double t) const noexcept {
        t = std::clamp(t, 0.0, duration());
        double s = 0, v = 0;
        if (t < t_ramp_) {
            v = a_ * t;
            s = 0.5 * a_ * t * t;
        } else if (t < t_ramp_ + t_cruise_) {
            v = v_;
            s = 0.5 * v_ * t_ramp_ + v_ * (t - t_ramp_);
        } else {
            const double td = duration() - t;  // time remaining
            v = a_ * td;
            s = dist_ - 0.5 * a_ * td * td;
        }
        return {p0_ + dir_ * s, dir_ * v};
    }

private:
    double p0_, dir_, dist_, a_;
    double v_ = 0, t_ramp_ = 0, t_cruise_ = 0;
};

}  // namespace maxmount
