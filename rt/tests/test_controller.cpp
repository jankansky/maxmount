// SPDX-License-Identifier: GPL-3.0-or-later
// End-to-end: controller + simulated bus, driven with simulated (not wall-clock) time.
#include <gtest/gtest.h>

#include <cmath>
#include <memory>

#include "maxmount/core/controller.hpp"
#include "maxmount/sim/sim_fieldbus.hpp"

using namespace maxmount;

namespace {

constexpr double kPi = 3.14159265358979323846;
constexpr double kDt = 0.001;
constexpr nanos kPeriod = 1'000'000;
constexpr nanos kT0 = 1'800'000'000'000'000'000;
constexpr double kCountsPerRad = 1e7;

struct Profile {
    double A, w;
    double pos(double t) const { return t <= 0 ? 0.0 : A * (1 - std::cos(w * t)); }
    double vel(double t) const { return t <= 0 ? 0.0 : A * w * std::sin(w * t); }
};

class ControllerTest : public ::testing::Test {
protected:
    void SetUp() override {
        std::vector<AxisConfig> axes(2);
        for (auto& a : axes) {
            a.counts_per_rad = kCountsPerRad;
            a.vel_units_per_rad_s = kCountsPerRad;
            a.limits = {-3.0, 3.0, 1.0, 1.0};
        }
        axes[0].name = "az";
        axes[1].name = "el";
        bus = std::make_unique<sim::SimFieldbus>(2, kDt);
        bus->start();
        ctl = std::make_unique<Controller>(*bus, axes);
    }

    // Advance n cycles; returns the last telemetry sample.
    TelemetrySample step(int n) {
        TelemetrySample last{};
        for (int i = 0; i < n; ++i) {
            ctl->cycle(t, kDt);
            t += kPeriod;
            while (auto s = ctl->poll_telemetry()) last = *s;
        }
        return last;
    }

    void push_profile(const Profile& p, double from_s, double to_s) {
        for (double ts = from_s; ts <= to_s + 1e-9; ts += 0.05) {
            const nanos tn = kT0 + static_cast<nanos>(std::llround(ts * 1e9));
            for (std::uint8_t ax = 0; ax < 2; ++ax) {
                ASSERT_TRUE(ctl->post({CommandType::PushPoint, ax, {tn, p.pos(ts), p.vel(ts)}}));
            }
        }
    }

    std::unique_ptr<sim::SimFieldbus> bus;
    std::unique_ptr<Controller> ctl;
    nanos t = kT0 - 200 * kPeriod;
};

}  // namespace

TEST_F(ControllerTest, EnablesAndTracksTrajectory) {
    const Profile p{0.3, 2 * kPi / 8.0};
    push_profile(p, 0.0, 6.0);
    ctl->post({CommandType::Enable, 0, {}});

    TelemetrySample s = step(200);  // reach t = kT0
    for (std::size_t i = 0; i < 2; ++i) {
        EXPECT_EQ(s.axes[i].state, cia402::State::OperationEnabled);
    }

    double max_cmd_err = 0.0;
    double max_act_err = 0.0;
    for (int k = 0; k < 5000; ++k) {
        s = step(1);
        const double ts = ns_to_s(s.t - kT0);
        const double t_cmd = ts + 1e-3;  // command lead = one cycle
        for (std::size_t i = 0; i < 2; ++i) {
            EXPECT_EQ(s.axes[i].flags & axis_flags::Limited, 0) << "t=" << ts;
            max_cmd_err = std::max(max_cmd_err, std::abs(s.axes[i].cmd_pos - p.pos(t_cmd)));
            max_act_err = std::max(max_act_err, std::abs(s.axes[i].act_pos - p.pos(ts)));
        }
    }
    // Setpoints reproduce the 20 Hz trajectory to within Hermite interpolation error.
    EXPECT_LT(max_cmd_err, 1e-7);
    // Actual position is limited by the crude simulated servo (~one internal substep of lag).
    EXPECT_LT(max_act_err, 5e-5);
}

TEST_F(ControllerTest, StarvationStopsAndHolds) {
    const Profile p{0.3, 2 * kPi / 8.0};
    push_profile(p, 0.0, 1.0);  // only one second of trajectory
    ctl->post({CommandType::Enable, 0, {}});
    step(200 + 1000);
    TelemetrySample s = step(2000);
    for (std::size_t i = 0; i < 2; ++i) {
        EXPECT_NE(s.axes[i].flags & axis_flags::Starved, 0);
        EXPECT_EQ(s.axes[i].state, cia402::State::OperationEnabled);
        EXPECT_EQ(s.axes[i].cmd_vel, 0.0);
    }
}

TEST_F(ControllerTest, DisableDeceleratesThenDisables) {
    const Profile p{0.3, 2 * kPi / 8.0};
    push_profile(p, 0.0, 6.0);
    ctl->post({CommandType::Enable, 0, {}});
    step(200 + 2000);  // moving at speed
    ctl->post({CommandType::Disable, 0, {}});
    TelemetrySample s = step(2000);
    EXPECT_FALSE(s.enabled);
    for (std::size_t i = 0; i < 2; ++i) {
        EXPECT_EQ(s.axes[i].state, cia402::State::SwitchOnDisabled);  // power stage off
        EXPECT_EQ(s.axes[i].cmd_vel, 0.0);
    }
}

TEST_F(ControllerTest, FollowingErrorFaultNeedsReset) {
    ctl->post({CommandType::Enable, 0, {}});
    step(50);
    bus->drive(0).inject_fault(0x8611);
    TelemetrySample s = step(100);
    EXPECT_EQ(s.axes[0].state, cia402::State::Fault);
    EXPECT_EQ(s.axes[1].state, cia402::State::OperationEnabled);
    ctl->post({CommandType::FaultReset, 0, {}});
    s = step(50);
    EXPECT_EQ(s.axes[0].state, cia402::State::OperationEnabled);
}

// The controller must be fed the scheduled cycle time, not the jittered wake-up time: then a
// feasible trajectory passes through the limiter untouched on every cycle.
TEST_F(ControllerTest, FeasibleTrajectoryIsNeverLimited) {
    const Profile p{60 * kPi / 180, 2 * kPi / 20.0};  // the mount-rt demo az profile
    push_profile(p, 0.0, 10.0);
    ctl->post({CommandType::Enable, 0, {}});
    step(200);
    int limited = 0;
    for (int i = 0; i < 8000; ++i) {
        ctl->cycle(t, kDt);
        t += kPeriod;
        while (auto s = ctl->poll_telemetry()) {
            if (s->axes[0].flags & axis_flags::Limited) ++limited;
        }
    }
    EXPECT_EQ(limited, 0);
}
