// SPDX-License-Identifier: GPL-3.0-or-later
// End to end over Zenoh (loopback TCP, no scouting): a client session sends trajectories and
// commands to a ZenohBridge in front of a Controller with simulated drives, and receives
// telemetry.
#include <gtest/gtest.h>
#include <unistd.h>

#include <chrono>
#include <condition_variable>
#include <mutex>
#include <optional>
#include <thread>

#include "maxmount/bridge/zenoh_bridge.hpp"
#include "maxmount/sim/sim_fieldbus.hpp"
#include "maxmount/v1/mount.pb.h"
#include "zenoh.hxx"

using namespace maxmount;
using namespace maxmount::bridge;
namespace mpb = maxmount::v1;
using namespace std::chrono_literals;

namespace {

constexpr double kDt = 0.001;
constexpr nanos kPeriod = 1'000'000;

class ZenohBridgeTest : public ::testing::Test {
protected:
    void SetUp() override {
        port_ = 20000 + static_cast<int>(getpid() % 20000) + port_offset_++;
        site_ = "test" + std::to_string(getpid());
    }

    void make(bool serve) {
        std::vector<AxisConfig> axes(2);
        axes[0].name = "az";
        axes[1].name = "el";
        for (auto& a : axes) {
            a.counts_per_rad = 1e7;
            a.limits = {-3.0, 3.0, 1.0, 1.0};
        }
        bus_ = std::make_unique<sim::SimFieldbus>(2, kDt);
        bus_->start();
        ctl_ = std::make_unique<Controller>(*bus_, axes);

        BridgeOptions o;
        o.zenoh.enabled = true;
        o.zenoh.site = site_;
        o.zenoh.listen = {"tcp/127.0.0.1:" + std::to_string(port_)};
        o.zenoh.multicast_scouting = false;
        o.axis_names = {"az", "el"};
        o.cycle = kPeriod;
        o.accept_commands = serve;
        o.validate = [](const Command& c, const TelemetrySample&) -> std::string {
            if (c.type == CommandType::Enable && (c.axis == 1 || c.axis == kAllAxes)) return "el is not commissioned";
            return {};
        };
        bridge_ = std::make_unique<ZenohBridge>(o);
        bridge_->start();

        zenoh::Config cc = zenoh::Config::create_default();
        cc.insert_json5("mode", "\"peer\"");
        cc.insert_json5("connect/endpoints", "[\"tcp/127.0.0.1:" + std::to_string(port_) + "\"]");
        cc.insert_json5("scouting/multicast/enabled", "false");
        client_.emplace(zenoh::Session::open(std::move(cc)));
        sub_.emplace(client_->declare_subscriber(
            zenoh::KeyExpr(bridge_->key("mount/telemetry")),
            [this](const zenoh::Sample& s) {
                mpb::MountTelemetry m;
                const auto v = s.get_payload().as_vector();
                if (m.ParseFromArray(v.data(), static_cast<int>(v.size()))) {
                    std::lock_guard lock(mu_);
                    telemetry_ = m;
                    ++n_telemetry_;
                }
            },
            zenoh::closures::none));
        raw_.emplace(client_->declare_subscriber(
            zenoh::KeyExpr(bridge_->key("mount/telemetry/raw")),
            [this](const zenoh::Sample& s) {
                mpb::MountTelemetryBatch b;
                const auto v = s.get_payload().as_vector();
                if (b.ParseFromArray(v.data(), static_cast<int>(v.size()))) {
                    std::lock_guard lock(mu_);
                    n_raw_samples_ += static_cast<std::size_t>(b.samples_size());
                }
            },
            zenoh::closures::none));
    }

    void TearDown() override {
        raw_.reset();
        sub_.reset();
        if (client_) client_->close();
        client_.reset();
        if (bridge_) bridge_->stop();
    }

    // Runs n controller cycles in simulated time, like mount-rt's main and RT threads.
    void run(int n) {
        for (int i = 0; i < n; ++i) {
            bridge_->forward(*ctl_);
            ctl_->cycle(t_, kDt);
            t_ += kPeriod;
            while (auto s = ctl_->poll_telemetry()) bridge_->on_telemetry(*s, LoopSnapshot{s->cycle, 0, 0});
        }
    }

    // Waits (in wall-clock time, cycling the controller) until pred() holds.
    template <typename Pred>
    bool eventually(Pred pred, std::chrono::milliseconds timeout = 5000ms) {
        const auto end = std::chrono::steady_clock::now() + timeout;
        while (std::chrono::steady_clock::now() < end) {
            run(20);
            {
                std::lock_guard lock(mu_);
                if (pred()) return true;
            }
            std::this_thread::sleep_for(5ms);
        }
        return false;
    }

    std::optional<mpb::CommandReply> command(const mpb::MountCommand& m) {
        zenoh::Session::GetOptions opts = zenoh::Session::GetOptions::create_default();
        opts.payload = zenoh::Bytes(m.SerializeAsString());
        opts.timeout_ms = 3000;
        std::mutex mu;
        std::condition_variable cv;
        bool done = false;
        std::optional<mpb::CommandReply> reply;
        client_->get(
            zenoh::KeyExpr(bridge_->key("mount/cmd")), "",
            [&](const zenoh::Reply& r) {
                if (!r.is_ok()) return;
                const auto v = r.get_ok().get_payload().as_vector();
                mpb::CommandReply cr;
                if (cr.ParseFromArray(v.data(), static_cast<int>(v.size()))) {
                    std::lock_guard lock(mu);
                    reply = cr;
                }
            },
            [&]() {
                std::lock_guard lock(mu);
                done = true;
                cv.notify_all();
            },
            std::move(opts));
        std::unique_lock lock(mu);
        cv.wait_for(lock, 5s, [&] { return done; });
        return reply;
    }

    // Waits until the client's queries reach the bridge (the TCP session is up).
    void wait_connected() {
        mpb::MountCommand probe;
        probe.mutable_clear_trajectory();
        for (int i = 0; i < 50; ++i) {
            if (command(probe)) return;
            std::this_thread::sleep_for(100ms);
        }
        FAIL() << "client never reached the bridge";
    }

    static inline int port_offset_ = 0;
    int port_ = 0;
    std::string site_;
    std::unique_ptr<sim::SimFieldbus> bus_;
    std::unique_ptr<Controller> ctl_;
    std::unique_ptr<ZenohBridge> bridge_;
    std::optional<zenoh::Session> client_;
    std::optional<zenoh::Subscriber<void>> sub_, raw_;

    std::mutex mu_;
    std::optional<mpb::MountTelemetry> telemetry_;
    std::size_t n_telemetry_ = 0;
    std::size_t n_raw_samples_ = 0;
    nanos t_ = 1'800'000'000'000'000'000;
};

}  // namespace

TEST_F(ZenohBridgeTest, ServeModeAcceptsTrajectoryAndCommands) {
    make(true);
    wait_connected();

    // A 0.1 rad move on az over 2 s, starting 0.2 s from now in simulated time.
    mpb::TrajectoryUpdate u;
    auto* az = u.add_axes();
    az->set_axis("az");
    for (int k = 0; k <= 44; ++k) {
        const double ts = 0.05 * k;
        const double s = ts < 0.2 ? 0.0 : std::min(1.0, (ts - 0.2) / 2.0);
        auto* p = az->add_points();
        p->set_t_ns(t_ + static_cast<nanos>(ts * 1e9));
        p->set_position_rad(0.1 * s * s * (3 - 2 * s));
        p->set_velocity_rad_s(ts < 0.2 || s >= 1.0 ? 0.0 : 0.1 * 6 * s * (1 - s) / 2.0);
    }
    zenoh::Session::PutOptions po = zenoh::Session::PutOptions::create_default();
    client_->put(zenoh::KeyExpr(bridge_->key("mount/trajectory")), zenoh::Bytes(u.SerializeAsString()));
    ASSERT_TRUE(eventually([&] { return bridge_->stats().trajectories == 1; }));

    mpb::MountCommand en;
    en.mutable_enable()->set_axis("el");
    auto r = command(en);
    ASSERT_TRUE(r);
    EXPECT_FALSE(r->accepted());
    EXPECT_EQ(r->message(), "el is not commissioned");

    en.mutable_enable()->set_axis("az");
    r = command(en);
    ASSERT_TRUE(r);
    EXPECT_TRUE(r->accepted()) << r->message();

    mpb::MountCommand bad;
    bad.mutable_enable()->set_axis("dec");
    r = command(bad);
    ASSERT_TRUE(r);
    EXPECT_FALSE(r->accepted());

    // Telemetry shows az enabled and arriving at 0.1 rad; el untouched.
    ASSERT_TRUE(eventually(
        [&] {
            return telemetry_ && telemetry_->axes_size() == 2 &&
                   telemetry_->axes(0).state() == mpb::DRIVE_STATE_OPERATION_ENABLED &&
                   std::abs(telemetry_->axes(0).act_position_rad() - 0.1) < 1e-6;
        },
        10000ms));
    {
        std::lock_guard lock(mu_);
        EXPECT_EQ(telemetry_->axes(0).axis(), "az");
        EXPECT_EQ(telemetry_->axes(1).state(), mpb::DRIVE_STATE_SWITCH_ON_DISABLED);
        EXPECT_TRUE(telemetry_->has_loop());
        EXPECT_GT(n_raw_samples_, n_telemetry_);  // raw stream carries every cycle
    }

    mpb::MountCommand dis;
    dis.mutable_disable();
    r = command(dis);
    ASSERT_TRUE(r && r->accepted());
    EXPECT_TRUE(eventually([&] { return telemetry_ && !telemetry_->enabled(); }));
}

TEST_F(ZenohBridgeTest, OtherModesRejectCommandsButPublishTelemetry) {
    make(false);
    wait_connected();
    mpb::MountCommand en;
    en.mutable_enable()->set_axis("az");
    const auto r = command(en);
    ASSERT_TRUE(r);
    EXPECT_FALSE(r->accepted());
    EXPECT_EQ(r->message(), "mount-rt is not in serve mode");
    EXPECT_TRUE(eventually([&] { return n_telemetry_ > 0; }));
}
