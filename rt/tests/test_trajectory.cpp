// SPDX-License-Identifier: GPL-3.0-or-later
#include <gtest/gtest.h>

#include "maxmount/trajectory/trajectory.hpp"

using namespace maxmount;

namespace {
// f(t) = 1 + 2t - 0.5t^2 + 0.25t^3 (t in seconds)
double f(double t) { return 1 + 2 * t - 0.5 * t * t + 0.25 * t * t * t; }
double df(double t) { return 2 - t + 0.75 * t * t; }
double ddf(double t) { return -1 + 1.5 * t; }
constexpr nanos kBase = 1'800'000'000'000'000'000;  // a realistic epoch-scale timestamp
nanos at_s(double t) { return kBase + static_cast<nanos>(t * 1e9); }
}  // namespace

TEST(Hermite, ReproducesCubicExactly) {
    const TrajPoint a{at_s(0.0), f(0.0), df(0.0)};
    const TrajPoint b{at_s(1.5), f(1.5), df(1.5)};
    for (double t : {0.0, 0.1, 0.5, 0.75, 1.2, 1.5}) {
        const KinematicSample s = hermite(a, b, at_s(t));
        EXPECT_NEAR(s.pos, f(t), 1e-12);
        EXPECT_NEAR(s.vel, df(t), 1e-9);
        EXPECT_NEAR(s.acc, ddf(t), 1e-6);
    }
}

TEST(TrajectoryBuffer, StatusTransitions) {
    TrajectoryBuffer buf;
    EXPECT_EQ(buf.sample(at_s(0)).status, SampleStatus::Empty);

    for (int i = 0; i <= 10; ++i) {
        const double t = 0.1 * i + 1.0;
        ASSERT_TRUE(buf.push({at_s(t), f(t), df(t)}));
    }
    EXPECT_EQ(buf.sample(at_s(0.5)).status, SampleStatus::BeforeStart);

    const BufferedSample mid = buf.sample(at_s(1.53));
    EXPECT_EQ(mid.status, SampleStatus::Ok);
    EXPECT_NEAR(mid.s.pos, f(1.53), 1e-12);
    EXPECT_LT(buf.size(), 11u);  // consumed segments were discarded

    EXPECT_EQ(buf.sample(at_s(2.0)).status, SampleStatus::Ok);  // exactly the last point
    EXPECT_EQ(buf.sample(at_s(2.001)).status, SampleStatus::Starved);
}

TEST(TrajectoryBuffer, NewPointsReplaceBufferedFuture) {
    TrajectoryBuffer buf;
    for (int i = 0; i < 10; ++i) buf.push({at_s(i), 0.0, 0.0});
    EXPECT_EQ(buf.size(), 10u);
    // A point at t=4.5 replaces everything from t=5 onward.
    buf.push({at_s(4.5), 1.0, 0.0});
    EXPECT_EQ(buf.size(), 6u);
    EXPECT_EQ(buf.horizon(), at_s(4.5));
}
