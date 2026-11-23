// SPDX-License-Identifier: GPL-3.0-or-later
#include "maxmount/trajectory/trajectory.hpp"

namespace maxmount {

KinematicSample hermite(const TrajPoint& a, const TrajPoint& b, nanos t) noexcept {
    const nanos span = b.t - a.t;
    const double h = ns_to_s(span);
    const double s = static_cast<double>(t - a.t) / static_cast<double>(span);
    const double s2 = s * s;
    const double s3 = s2 * s;

    const double m0 = h * a.vel;
    const double m1 = h * b.vel;

    KinematicSample out;
    out.pos = (2 * s3 - 3 * s2 + 1) * a.pos + (s3 - 2 * s2 + s) * m0 + (-2 * s3 + 3 * s2) * b.pos +
              (s3 - s2) * m1;
    out.vel = ((6 * s2 - 6 * s) * a.pos + (3 * s2 - 4 * s + 1) * m0 + (-6 * s2 + 6 * s) * b.pos +
               (3 * s2 - 2 * s) * m1) /
              h;
    out.acc = ((12 * s - 6) * a.pos + (6 * s - 4) * m0 + (-12 * s + 6) * b.pos + (6 * s - 2) * m1) /
              (h * h);
    return out;
}

bool TrajectoryBuffer::push(const TrajPoint& p) noexcept {
    while (count_ > 0 && at(count_ - 1).t >= p.t) {
        --count_;
    }
    if (count_ == kCapacity) {
        return false;
    }
    buf_[(head_ + count_) % kCapacity] = p;
    ++count_;
    return true;
}

void TrajectoryBuffer::clear() noexcept {
    head_ = 0;
    count_ = 0;
}

BufferedSample TrajectoryBuffer::sample(nanos t) noexcept {
    if (count_ == 0) {
        return {SampleStatus::Empty, {}};
    }
    if (t < at(0).t) {
        return {SampleStatus::BeforeStart, {at(0).pos, 0.0, 0.0}};
    }
    while (count_ >= 2 && at(1).t <= t) {
        head_ = (head_ + 1) % kCapacity;
        --count_;
    }
    if (count_ == 1) {
        const TrajPoint& p = at(0);
        const SampleStatus status = (t == p.t) ? SampleStatus::Ok : SampleStatus::Starved;
        return {status, {p.pos, p.vel, 0.0}};
    }
    return {SampleStatus::Ok, hermite(at(0), at(1), t)};
}

}  // namespace maxmount
