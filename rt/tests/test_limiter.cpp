// SPDX-License-Identifier: GPL-3.0-or-later
#include <gtest/gtest.h>

#include <cmath>

#include "maxmount/axis/axis.hpp"

using namespace maxmount;

namespace {
constexpr double kPi = 3.14159265358979323846;
constexpr double kDt = 0.001;
}  // namespace

TEST(MotionLimiter, FeasibleTrajectoryPassesThroughUnchanged) {
    // 1 - cos profile: starts at rest, peak acceleration A*w^2 = 0.5 * (2pi/10)^2 ~ 0.2 rad/s^2.
    const double A = 0.5, w = 2 * kPi / 10.0;
    MotionLimiter lim({-10.0, 10.0, 1.0, 1.0});
    lim.reset(0.0);
    for (int k = 1; k <= 20000; ++k) {
        const double t = k * kDt;
        const LimiterOutput o = lim.track(A * (1 - std::cos(w * t)), A * w * std::sin(w * t), kDt);
        ASSERT_FALSE(o.limited) << "t=" << t;
        ASSERT_NEAR(o.pos, A * (1 - std::cos(w * t)), 1e-12) << "t=" << t;
    }
}

TEST(MotionLimiter, StepRespectsVelocityAndAcceleration) {
    const AxisLimits l{-10.0, 10.0, 0.5, 2.0};
    MotionLimiter lim(l);
    lim.reset(0.0);
    double prev_v = 0.0;
    bool saw_limit = false;
    for (int k = 0; k < 10000; ++k) {
        const LimiterOutput o = lim.track(1.0, 0.0, kDt);
        saw_limit = saw_limit || o.limited;
        ASSERT_LE(std::abs(o.vel), l.max_vel + 1e-12);
        ASSERT_LE(std::abs(o.vel - prev_v), l.max_acc * kDt + 1e-12);
        ASSERT_LE(o.pos, 1.0 + 1e-6);  // no significant overshoot
        prev_v = o.vel;
    }
    EXPECT_TRUE(saw_limit);
    EXPECT_NEAR(lim.position(), 1.0, 1e-9);
    EXPECT_NEAR(lim.velocity(), 0.0, 1e-9);
}

TEST(MotionLimiter, SoftLimitIsNeverCrossed) {
    const AxisLimits l{-1.0, 1.0, 1.0, 1.0};
    MotionLimiter lim(l);
    lim.reset(0.0);
    for (int k = 0; k < 10000; ++k) {
        // Request a ramp that runs straight through the upper limit.
        const LimiterOutput o = lim.track(0.8 * k * kDt, 0.8, kDt);
        ASSERT_LE(o.pos, l.max_pos);
    }
    EXPECT_NEAR(lim.position(), l.max_pos, 1e-6);
    EXPECT_NEAR(lim.velocity(), 0.0, 1e-6);
}

TEST(MotionLimiter, StopDeceleratesAtMaxAcceleration) {
    const AxisLimits l{-100.0, 100.0, 1.0, 2.0};
    MotionLimiter lim(l);
    lim.reset(0.0);
    for (int k = 1; k <= 2000; ++k) lim.track(0.5 * k * kDt, 0.5, kDt);  // cruise at 0.5 rad/s
    ASSERT_NEAR(lim.velocity(), 0.5, 1e-9);
    int cycles = 0;
    while (lim.velocity() != 0.0 && cycles < 10000) {
        lim.stop(kDt);
        ++cycles;
    }
    EXPECT_NEAR(cycles, 0.5 / (l.max_acc * kDt), 1.0);
}

TEST(PositionUnwrapper, HandlesInt32Wrap) {
    PositionUnwrapper u;
    EXPECT_EQ(u.update(2147483000), 2147483000);
    EXPECT_EQ(u.update(-2147483000), 2147483000LL + 1296);  // wrapped forward by 1296 counts
    EXPECT_EQ(u.update(2147483000), 2147483000);            // and back
}
