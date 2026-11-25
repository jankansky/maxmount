// SPDX-License-Identifier: GPL-3.0-or-later
#include <gtest/gtest.h>

#include <cmath>
#include <stdexcept>

#include "maxmount/config/system_config.hpp"

using namespace maxmount;
using namespace maxmount::config;

namespace {
constexpr double kPi = 3.14159265358979323846;
const std::string kDrives = MAXMOUNT_CONFIG_DIR "/drives";

const char* kTwoAxes = R"(
system: test
fieldbus: {backend: soem, interface: eth0, cycle_us: 1000}
realtime: {cpu: 3, priority: 70, command_lead_us: 1500}
axes:
  - name: az
    drive: technosoft_ipos4808_bx_cat
    bus_position: 0
    counts_per_rev: 1000000
    invert: true
    offset_deg: 90
    limits: {min_deg: -270, max_deg: 270, max_vel_deg_s: 30, max_acc_deg_s2: 20, max_following_error_deg: 0.5}
  - name: el
    drive: sim_cia402
    bus_position: 1
    counts_per_rev: null
    limits: {min_deg: -45, max_deg: 180, max_vel_deg_s: 10, max_acc_deg_s2: 10}
)";
}  // namespace

TEST(SystemConfig, ParsesAxesScalingAndDescriptors) {
    const SystemConfig s = parse_system_config(kTwoAxes, kDrives);
    EXPECT_EQ(s.name, "test");
    EXPECT_EQ(s.backend, "soem");
    EXPECT_EQ(s.interface, "eth0");
    EXPECT_EQ(s.cycle, 1'000'000);
    EXPECT_EQ(s.rt_cpu, 3);
    EXPECT_EQ(s.rt_priority, 70);
    EXPECT_EQ(s.command_lead, 1'500'000);
    ASSERT_EQ(s.axes.size(), 2u);

    const AxisEntry& az = s.axes[0];
    EXPECT_TRUE(az.scaled);
    EXPECT_NEAR(az.axis.counts_per_rad, -1e6 / (2 * kPi), 1e-6);  // inverted
    EXPECT_NEAR(az.axis.offset_rad, kPi / 2, 1e-12);
    EXPECT_NEAR(az.axis.limits.max_vel, 30 * kPi / 180, 1e-12);
    EXPECT_NEAR(az.axis.limits.max_following_error, 0.5 * kPi / 180, 1e-12);
    EXPECT_EQ(az.descriptor.vendor_id, 0x000001A3u);

    const AxisEntry& el = s.axes[1];
    EXPECT_FALSE(el.scaled);
    EXPECT_DOUBLE_EQ(el.axis.counts_per_rad, 1.0);
    EXPECT_TRUE(std::isinf(el.axis.limits.max_following_error));
    EXPECT_EQ(el.bus_position, 1);

    EXPECT_EQ(s.find_axis("el"), &s.axes[1]);
    EXPECT_EQ(s.find_axis("ra"), nullptr);
}

TEST(SystemConfig, SelectAxesKeepsSystemOrder) {
    const SystemConfig s = parse_system_config(kTwoAxes, kDrives);
    const SystemConfig one = select_axes(s, {"el"});
    ASSERT_EQ(one.axes.size(), 1u);
    EXPECT_EQ(one.axes[0].axis.name, "el");
    EXPECT_EQ(select_axes(s, {"el", "az"}).axes[0].axis.name, "az");
    EXPECT_THROW(select_axes(s, {"ha"}), std::runtime_error);
}

TEST(SystemConfig, RejectsMistakes) {
    auto bad = [](const std::string& yaml) {
        EXPECT_THROW(parse_system_config(yaml, kDrives), std::runtime_error) << yaml;
    };
    const std::string axis =
        "  - {name: az, drive: sim_cia402, bus_position: 0, limits: {min_deg: -1, max_deg: 1, "
        "max_vel_deg_s: 1, max_acc_deg_s2: 1}}\n";
    EXPECT_NO_THROW(parse_system_config("fieldbus: {backend: sim}\naxes:\n" + axis, kDrives));
    bad("fieldbus: {backend: can}\naxes:\n" + axis);
    bad("fieldbus: {backend: soem}\naxes:\n" + axis);  // no interface
    bad("fieldbus: {backend: sim}\naxes: []\n");
    bad("fieldbus: {backend: sim}\naxes:\n" + axis + axis);  // duplicate name
    bad("fieldbus: {backend: sim}\naxes:\n  - {name: az, drive: no_such_drive, bus_position: 0, limits: "
        "{min_deg: -1, max_deg: 1, max_vel_deg_s: 1, max_acc_deg_s2: 1}}\n");
    bad("fieldbus: {backend: sim}\naxes:\n  - {name: az, drive: sim_cia402, bus_position: 0, limits: "
        "{min_deg: 1, max_deg: -1, max_vel_deg_s: 1, max_acc_deg_s2: 1}}\n");
}

TEST(SystemConfig, RepositorySystemsLoad) {
    const SystemConfig ref = load_system_config(MAXMOUNT_CONFIG_DIR "/systems/maxmount-reference/system.yaml");
    EXPECT_EQ(ref.backend, "soem");
    ASSERT_EQ(ref.axes.size(), 2u);
    EXPECT_EQ(ref.axes[0].axis.name, "az");
    const SystemConfig sim = load_system_config(MAXMOUNT_CONFIG_DIR "/systems/sim-altaz/system.yaml");
    EXPECT_EQ(sim.backend, "sim");
    EXPECT_TRUE(sim.axes[0].scaled);
}

TEST(SystemConfig, ParsesZenohSection) {
    const std::string axis =
        "axes:\n  - {name: az, drive: sim_cia402, bus_position: 0, limits: {min_deg: -1, max_deg: 1, "
        "max_vel_deg_s: 1, max_acc_deg_s2: 1}}\n";
    const SystemConfig none = parse_system_config("fieldbus: {backend: sim}\n" + axis, kDrives);
    EXPECT_FALSE(none.zenoh.enabled);

    const SystemConfig z = parse_system_config(
        "fieldbus: {backend: sim}\nzenoh: {site: lab, mode: client, connect: [tcp/10.0.0.2:7447], "
        "multicast_scouting: false, publish_raw: false}\n" + axis,
        kDrives);
    EXPECT_TRUE(z.zenoh.enabled);
    EXPECT_EQ(z.zenoh.site, "lab");
    EXPECT_EQ(z.zenoh.mode, "client");
    ASSERT_EQ(z.zenoh.connect.size(), 1u);
    EXPECT_EQ(z.zenoh.connect[0], "tcp/10.0.0.2:7447");
    EXPECT_FALSE(z.zenoh.multicast_scouting);
    EXPECT_FALSE(z.zenoh.publish_raw);

    EXPECT_THROW(parse_system_config("fieldbus: {backend: sim}\nzenoh: {site: a/b}\n" + axis, kDrives),
                 std::runtime_error);
    EXPECT_THROW(parse_system_config("fieldbus: {backend: sim}\nzenoh: {mode: mesh}\n" + axis, kDrives),
                 std::runtime_error);
}
