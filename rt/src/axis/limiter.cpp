// SPDX-License-Identifier: GPL-3.0-or-later
#include <algorithm>
#include <cmath>

#include "maxmount/axis/axis.hpp"

namespace maxmount {

namespace {

// Largest speed from which a discrete-time axis (steps of dt, decelerating by acc*dt per
// step) stops within `distance`: solves v^2 / (2 acc) + v dt / 2 = distance.
double braking_speed(double distance, double acc, double dt) noexcept {
    return acc * (std::sqrt(0.25 * dt * dt + 2.0 * distance / acc) - 0.5 * dt);
}

}  // namespace

LimiterOutput MotionLimiter::track(double target_pos, double target_vel, double dt) noexcept {
    const double target = std::clamp(target_pos, lim_.min_pos, lim_.max_pos);

    // Residual error after moving one cycle with the feedforward velocity. For a feasible
    // trajectory |e| is O(acc * dt^2) and the correction is exactly e / dt; for large errors
    // it is capped by a braking envelope so the axis converges without overshoot.
    const double e = target - p_ - target_vel * dt;
    const double ae = std::abs(e);
    const double correction = std::copysign(std::min(ae / dt, braking_speed(ae, lim_.max_acc, dt)), e);

    LimiterOutput out = finish(target_vel + correction, dt);
    out.limited = out.limited || target != target_pos;
    return out;
}

LimiterOutput MotionLimiter::stop(double dt) noexcept {
    LimiterOutput out = finish(0.0, dt);
    out.limited = false;
    return out;
}

LimiterOutput MotionLimiter::finish(double v_des, double dt) noexcept {
    double v = std::clamp(v_des, -lim_.max_vel, lim_.max_vel);

    // Braking envelope: never move toward a soft limit faster than we can stop before it.
    const double room_up = std::max(0.0, lim_.max_pos - p_);
    const double room_down = std::max(0.0, p_ - lim_.min_pos);
    v = std::min(v, braking_speed(room_up, lim_.max_acc, dt));
    v = std::max(v, -braking_speed(room_down, lim_.max_acc, dt));

    const double dv = lim_.max_acc * dt;
    v = std::clamp(v, v_ - dv, v_ + dv);

    double p = p_ + v * dt;
    if (p > lim_.max_pos) {
        p = lim_.max_pos;
        v = (p - p_) / dt;
    } else if (p < lim_.min_pos) {
        p = lim_.min_pos;
        v = (p - p_) / dt;
    }

    LimiterOutput out;
    out.pos = p;
    out.vel = v;
    out.acc = (v - v_) / dt;
    out.limited = std::abs(v - v_des) > 1e-9 * std::max(1.0, std::abs(v_des));
    p_ = p;
    v_ = v;
    return out;
}

}  // namespace maxmount
