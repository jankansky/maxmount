// SPDX-License-Identifier: GPL-3.0-or-later
#include <gtest/gtest.h>

#include <cmath>

#include "maxmount/trajectory/move_profile.hpp"

using namespace maxmount;

TEST(MoveProfile, TrapezoidReachesTargetWithinLimits) {
    const MoveProfile m(1.0, -2.0, 0.5, 0.25);  // 3 rad back at 0.5 rad/s, 0.25 rad/s^2
    EXPECT_DOUBLE_EQ(m.peak_velocity(), 0.5);
    EXPECT_NEAR(m.duration(), 2 * 2.0 + (3.0 - 1.0) / 0.5, 1e-12);  // ramps 2 s, cruise 4 s
    EXPECT_DOUBLE_EQ(m.at(0).pos, 1.0);
    EXPECT_DOUBLE_EQ(m.at(0).vel, 0.0);
    EXPECT_NEAR(m.at(m.duration()).pos, -2.0, 1e-12);
    EXPECT_NEAR(m.at(m.duration()).vel, 0.0, 1e-12);
    EXPECT_NEAR(m.at(1e9).pos, -2.0, 1e-12);  // clamped after the end

    double prev_pos = m.at(0).pos, prev_vel = 0;
    const double dt = 1e-3;
    for (double t = dt; t <= m.duration(); t += dt) {
        const auto s = m.at(t);
        EXPECT_LE(std::abs(s.vel), 0.5 + 1e-12);
        EXPECT_LE(std::abs(s.vel - prev_vel) / dt, 0.25 + 1e-6);
        EXPECT_NEAR((s.pos - prev_pos) / dt, 0.5 * (s.vel + prev_vel), 1e-6);  // vel is d(pos)/dt
        EXPECT_LE(s.vel, 0.0);                                                 // monotonic move
        prev_pos = s.pos;
        prev_vel = s.vel;
    }
}

TEST(MoveProfile, ShortMoveIsTriangular) {
    const MoveProfile m(0.0, 0.01, 1.0, 1.0);
    EXPECT_NEAR(m.peak_velocity(), 0.1, 1e-12);
    EXPECT_NEAR(m.duration(), 0.2, 1e-12);
    EXPECT_NEAR(m.at(0.1).pos, 0.005, 1e-12);
    EXPECT_NEAR(m.at(0.2).pos, 0.01, 1e-12);
}

TEST(MoveProfile, ZeroDistance) {
    const MoveProfile m(0.3, 0.3, 1.0, 1.0);
    EXPECT_DOUBLE_EQ(m.duration(), 0.0);
    EXPECT_DOUBLE_EQ(m.at(0.5).pos, 0.3);
}
