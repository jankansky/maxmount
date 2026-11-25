// SPDX-License-Identifier: GPL-3.0-or-later
#include <gtest/gtest.h>

#include <limits>

#include "maxmount/bridge/codec.hpp"
#include "maxmount/v1/mount.pb.h"

using namespace maxmount;
using namespace maxmount::bridge;
namespace mpb = maxmount::v1;

namespace {
const std::vector<std::string> kAxes = {"az", "el"};

std::string trajectory(const std::string& axis, std::initializer_list<std::tuple<std::int64_t, double, double>> pts) {
    mpb::TrajectoryUpdate u;
    auto* a = u.add_axes();
    a->set_axis(axis);
    for (const auto& [t, p, v] : pts) {
        auto* q = a->add_points();
        q->set_t_ns(t);
        q->set_position_rad(p);
        q->set_velocity_rad_s(v);
    }
    return u.SerializeAsString();
}
}  // namespace

TEST(BridgeCodec, DecodesTrajectoryIntoPushPoints) {
    const Decoded d = decode_trajectory(trajectory("el", {{100, 0.1, 0.01}, {200, 0.2, 0.02}}), kAxes);
    ASSERT_TRUE(d.ok()) << d.error;
    ASSERT_EQ(d.commands.size(), 2u);
    EXPECT_EQ(d.commands[0].type, CommandType::PushPoint);
    EXPECT_EQ(d.commands[0].axis, 1);
    EXPECT_EQ(d.commands[1].point.t, 200);
    EXPECT_DOUBLE_EQ(d.commands[1].point.pos, 0.2);
    EXPECT_DOUBLE_EQ(d.commands[1].point.vel, 0.02);
}

TEST(BridgeCodec, RejectsBadTrajectories) {
    EXPECT_FALSE(decode_trajectory(trajectory("ra", {{1, 0, 0}}), kAxes).ok());
    EXPECT_FALSE(decode_trajectory(trajectory("az", {{2, 0, 0}, {2, 0, 0}}), kAxes).ok());
    EXPECT_FALSE(decode_trajectory(trajectory("az", {{1, std::numeric_limits<double>::quiet_NaN(), 0}}), kAxes).ok());
    const Decoded bad = decode_trajectory("\xff\xff\xff", kAxes);
    EXPECT_FALSE(bad.ok());
    EXPECT_TRUE(bad.commands.empty());
}

TEST(BridgeCodec, DecodesCommands) {
    mpb::MountCommand m;
    m.mutable_enable()->set_axis("el");
    Decoded d = decode_command(m.SerializeAsString(), kAxes);
    ASSERT_TRUE(d.ok());
    EXPECT_EQ(d.commands[0].type, CommandType::Enable);
    EXPECT_EQ(d.commands[0].axis, 1);

    m.mutable_disable();  // empty axis = all
    d = decode_command(m.SerializeAsString(), kAxes);
    ASSERT_TRUE(d.ok());
    EXPECT_EQ(d.commands[0].type, CommandType::Disable);
    EXPECT_EQ(d.commands[0].axis, kAllAxes);

    m.mutable_clear_trajectory()->set_axis("dec");
    EXPECT_FALSE(decode_command(m.SerializeAsString(), kAxes).ok());
    EXPECT_FALSE(decode_command(mpb::MountCommand().SerializeAsString(), kAxes).ok());
}

TEST(BridgeCodec, EncodesTelemetryAndBatches) {
    TelemetrySample s;
    s.t = 1'800'000'000'000'000'000;
    s.cycle = 42;
    s.n_axes = 2;
    s.enabled = true;
    s.tripped = true;
    s.bus_lost_frames = 3;
    s.axes[1].act_pos = 0.5;
    s.axes[1].state = cia402::State::OperationEnabled;
    s.axes[1].flags = axis_flags::FollowingErrorTrip;
    s.axes[1].following_error = -7;
    s.axes[1].digital_inputs = 0x80000004u;

    const LoopSnapshot loop{1000, 2, 25'000};
    mpb::MountTelemetry m;
    ASSERT_TRUE(m.ParseFromString(encode_telemetry(s, kAxes, &loop)));
    EXPECT_EQ(m.t_ns(), s.t);
    EXPECT_EQ(m.cycle(), 42u);
    EXPECT_TRUE(m.tripped());
    EXPECT_EQ(m.bus().lost_frames(), 3u);
    EXPECT_EQ(m.loop().max_wake_latency_ns(), 25'000);
    ASSERT_EQ(m.axes_size(), 2);
    EXPECT_EQ(m.axes(1).axis(), "el");
    EXPECT_EQ(m.axes(1).state(), mpb::DRIVE_STATE_OPERATION_ENABLED);
    EXPECT_EQ(m.axes(1).flags(), axis_flags::FollowingErrorTrip);
    EXPECT_EQ(m.axes(1).following_error_counts(), -7);
    EXPECT_EQ(m.axes(1).digital_inputs(), 0x80000004u);

    BatchEncoder b(kAxes);
    b.add(s);
    s.cycle = 43;
    b.add(s);
    EXPECT_EQ(b.size(), 2u);
    mpb::MountTelemetryBatch batch;
    ASSERT_TRUE(batch.ParseFromString(b.take()));
    EXPECT_EQ(b.size(), 0u);
    ASSERT_EQ(batch.samples_size(), 2);
    EXPECT_EQ(batch.samples(1).cycle(), 43u);
    EXPECT_FALSE(batch.samples(0).has_loop());

    mpb::CommandReply r;
    ASSERT_TRUE(r.ParseFromString(encode_reply(false, "nope")));
    EXPECT_FALSE(r.accepted());
    EXPECT_EQ(r.message(), "nope");
}
