// SPDX-License-Identifier: GPL-3.0-or-later
// mount-ctl: command-line Zenoh client for mount-rt (testing and manual operation).
//
//   mount-ctl [--system FILE | --site S] [--connect EP] COMMAND
//     watch [SECONDS]          print telemetry once a second
//     enable [AXIS]            enable an axis (all if omitted)
//     disable [AXIS]           decelerate and disable
//     quick-stop | fault-reset | clear [AXIS]
//     move AXIS --by DEG [--vel DEG_S] [--acc DEG_S2]
//                              send a trapezoidal move as a time-tagged trajectory
//                              (the axis must be enabled to follow it)
//
// mount-rt must run with --serve to accept anything but watch.
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <vector>

#include "maxmount/common/time.hpp"
#include "maxmount/config/system_config.hpp"
#include "maxmount/trajectory/move_profile.hpp"
#include "maxmount/v1/mount.pb.h"
#include "zenoh.hxx"

namespace {

namespace mpb = maxmount::v1;
using namespace std::chrono_literals;
constexpr double kPi = 3.14159265358979323846;
constexpr double kDeg = kPi / 180.0;

const char* state_name(mpb::DriveState s) {
    switch (s) {
        case mpb::DRIVE_STATE_NOT_READY_TO_SWITCH_ON: return "NotReady";
        case mpb::DRIVE_STATE_SWITCH_ON_DISABLED: return "SwitchOnDisabled";
        case mpb::DRIVE_STATE_READY_TO_SWITCH_ON: return "ReadyToSwitchOn";
        case mpb::DRIVE_STATE_SWITCHED_ON: return "SwitchedOn";
        case mpb::DRIVE_STATE_OPERATION_ENABLED: return "OperationEnabled";
        case mpb::DRIVE_STATE_QUICK_STOP_ACTIVE: return "QuickStopActive";
        case mpb::DRIVE_STATE_FAULT_REACTION_ACTIVE: return "FaultReaction";
        case mpb::DRIVE_STATE_FAULT: return "Fault";
        default: return "Unknown";
    }
}

[[noreturn]] void usage() {
    std::puts(
        "usage: mount-ctl [--system FILE | --site S] [--connect ENDPOINT] COMMAND\n"
        "  watch [SECONDS]                 print telemetry once a second (default: until Ctrl-C)\n"
        "  enable [AXIS] | disable [AXIS]  (all axes if AXIS is omitted)\n"
        "  quick-stop | fault-reset | clear [AXIS]\n"
        "  move AXIS --by DEG [--vel DEG_S] [--acc DEG_S2]\n"
        "--system reads zenoh.site/connect from a system file; --connect adds an endpoint\n"
        "such as tcp/maxmount.local:7447 (repeatable).");
    std::exit(2);
}

class Client {
public:
    Client(const std::string& site, const std::vector<std::string>& connect, bool scouting) : prefix_("maxmount/" + site + "/") {
        zenoh::Config c = zenoh::Config::create_default();
        c.insert_json5("mode", "\"peer\"");
        if (!connect.empty()) {
            std::string list = "[";
            for (std::size_t i = 0; i < connect.size(); ++i) list += (i ? ",\"" : "\"") + connect[i] + "\"";
            c.insert_json5("connect/endpoints", list + "]");
        }
        c.insert_json5("scouting/multicast/enabled", scouting ? "true" : "false");
        session_.emplace(zenoh::Session::open(std::move(c)));
    }
    ~Client() {
        sub_.reset();
        if (session_) session_->close();
    }

    std::string key(const std::string& s) const { return prefix_ + s; }

    void subscribe() {
        sub_.emplace(session_->declare_subscriber(
            zenoh::KeyExpr(key("mount/telemetry")),
            [this](const zenoh::Sample& s) {
                const auto v = s.get_payload().as_vector();
                mpb::MountTelemetry m;
                if (m.ParseFromArray(v.data(), static_cast<int>(v.size()))) {
                    std::lock_guard lock(mu_);
                    last_ = m;
                    cv_.notify_all();
                }
            },
            zenoh::closures::none));
    }

    std::optional<mpb::MountTelemetry> wait_telemetry(std::chrono::milliseconds timeout) {
        std::unique_lock lock(mu_);
        cv_.wait_for(lock, timeout, [&] { return last_.has_value(); });
        return last_;
    }

    std::optional<mpb::CommandReply> command(const mpb::MountCommand& m) {
        auto opts = zenoh::Session::GetOptions::create_default();
        opts.payload = zenoh::Bytes(m.SerializeAsString());
        opts.timeout_ms = 3000;
        std::mutex mu;
        std::condition_variable cv;
        bool done = false;
        std::optional<mpb::CommandReply> reply;
        session_->get(
            zenoh::KeyExpr(key("mount/cmd")), "",
            [&](const zenoh::Reply& r) {
                if (!r.is_ok()) return;
                const auto v = r.get_ok().get_payload().as_vector();
                mpb::CommandReply cr;
                if (cr.ParseFromArray(v.data(), static_cast<int>(v.size()))) {
                    std::lock_guard l(mu);
                    reply = cr;
                }
            },
            [&] {
                std::lock_guard l(mu);
                done = true;
                cv.notify_all();
            },
            std::move(opts));
        std::unique_lock l(mu);
        cv.wait_for(l, 5s, [&] { return done; });
        return reply;
    }

    void put_trajectory(const mpb::TrajectoryUpdate& u) {
        session_->put(zenoh::KeyExpr(key("mount/trajectory")), zenoh::Bytes(u.SerializeAsString()));
    }

private:
    std::string prefix_;
    std::optional<zenoh::Session> session_;
    std::optional<zenoh::Subscriber<void>> sub_;
    std::mutex mu_;
    std::condition_variable cv_;
    std::optional<mpb::MountTelemetry> last_;
};

void print(const mpb::MountTelemetry& m) {
    std::printf("cycle %10llu %s%s lat %5.1f us overruns %llu |", static_cast<unsigned long long>(m.cycle()),
                m.enabled() ? "ENABLED " : "disabled", m.tripped() ? " TRIPPED" : "",
                static_cast<double>(m.loop().max_wake_latency_ns()) / 1000.0,
                static_cast<unsigned long long>(m.loop().overruns()));
    for (const auto& a : m.axes()) {
        std::printf(" %s %-16s cmd %9.4f act %9.4f deg flags 0x%02x buf %u |", a.axis().c_str(), state_name(a.state()),
                    a.cmd_position_rad() / kDeg, a.act_position_rad() / kDeg, a.flags(), a.buffered_points());
    }
    std::printf("\n");
    std::fflush(stdout);
}

int report(const std::optional<mpb::CommandReply>& r) {
    if (!r) {
        std::fprintf(stderr, "no reply: is mount-rt running with a zenoh section on the same site?\n");
        return 1;
    }
    std::printf("%s: %s\n", r->accepted() ? "accepted" : "rejected", r->message().c_str());
    return r->accepted() ? 0 : 1;
}

}  // namespace

int main(int argc, char** argv) {
    std::string site = "default";
    std::vector<std::string> connect;
    bool scouting = true;
    std::vector<std::string> args;
    std::optional<double> by, vel, acc;
    try {
        for (int i = 1; i < argc; ++i) {
            const std::string a = argv[i];
            auto next = [&]() -> std::string {
                if (i + 1 >= argc) usage();
                return argv[++i];
            };
            if (a == "--system") {
                const auto sys = maxmount::config::load_system_config(next());
                site = sys.zenoh.site;
                connect = sys.zenoh.connect;
                scouting = sys.zenoh.multicast_scouting;
            } else if (a == "--site") {
                site = next();
            } else if (a == "--connect") {
                connect.push_back(next());
            } else if (a == "--by") {
                by = std::stod(next());
            } else if (a == "--vel") {
                vel = std::stod(next());
            } else if (a == "--acc") {
                acc = std::stod(next());
            } else if (a == "-h" || a == "--help") {
                usage();
            } else {
                args.push_back(a);
            }
        }
    } catch (const std::exception& e) {
        std::fprintf(stderr, "%s\n", e.what());
        return 2;
    }
    if (args.empty()) usage();
    const std::string cmd = args[0];
    const std::string axis = args.size() > 1 ? args[1] : "";

    try {
        Client c(site, connect, scouting);
        mpb::MountCommand m;
        if (cmd == "watch") {
            c.subscribe();
            const double seconds = args.size() > 1 ? std::stod(args[1]) : 0.0;
            const auto end = std::chrono::steady_clock::now() + std::chrono::duration<double>(seconds);
            if (!c.wait_telemetry(3s)) {
                std::fprintf(stderr, "no telemetry on %s\n", c.key("mount/telemetry").c_str());
                return 1;
            }
            while (seconds <= 0 || std::chrono::steady_clock::now() < end) {
                print(*c.wait_telemetry(1s));
                std::this_thread::sleep_for(1s);
            }
            return 0;
        }
        if (cmd == "enable") {
            m.mutable_enable()->set_axis(axis);
        } else if (cmd == "disable") {
            m.mutable_disable()->set_axis(axis);
        } else if (cmd == "quick-stop") {
            m.mutable_quick_stop();
        } else if (cmd == "fault-reset") {
            m.mutable_fault_reset();
        } else if (cmd == "clear") {
            m.mutable_clear_trajectory()->set_axis(axis);
        } else if (cmd == "move") {
            if (axis.empty() || !by) usage();
            c.subscribe();
            const auto t = c.wait_telemetry(3s);
            if (!t) {
                std::fprintf(stderr, "no telemetry: cannot read the current position\n");
                return 1;
            }
            const mpb::AxisTelemetry* at = nullptr;
            for (const auto& a : t->axes()) {
                if (a.axis() == axis) at = &a;
            }
            if (!at) {
                std::fprintf(stderr, "no axis '%s' in telemetry\n", axis.c_str());
                return 1;
            }
            // Start from the commanded position, which is continuous with what the axis follows.
            const double from = at->state() == mpb::DRIVE_STATE_OPERATION_ENABLED ? at->cmd_position_rad()
                                                                                   : at->act_position_rad();
            const maxmount::MoveProfile p(from, from + *by * kDeg, vel.value_or(1.0) * kDeg, acc.value_or(2.0) * kDeg);
            const maxmount::nanos t0 = maxmount::realtime_ns() + 500'000'000;
            mpb::TrajectoryUpdate u;
            auto* traj = u.add_axes();
            traj->set_axis(axis);
            const auto n = static_cast<int>(std::ceil(p.duration() / 0.05));
            for (int k = 0; k <= n + 20; ++k) {  // 1 s of hold at the end
                const double ts = 0.05 * k;
                const auto s = p.at(ts);
                auto* q = traj->add_points();
                q->set_t_ns(t0 + static_cast<maxmount::nanos>(ts * 1e9));
                q->set_position_rad(s.pos);
                q->set_velocity_rad_s(s.vel);
            }
            c.put_trajectory(u);
            std::printf("sent %d points: %s %.4f -> %.4f deg over %.1f s, starting in 0.5 s\n", traj->points_size(),
                        axis.c_str(), from / kDeg, (from + *by * kDeg) / kDeg, p.duration());
            std::this_thread::sleep_for(200ms);  // let the put leave before closing the session
            return 0;
        } else {
            usage();
        }
        return report(c.command(m));
    } catch (const std::exception& e) {
        std::fprintf(stderr, "mount-ctl: %s\n", e.what());
        return 1;
    }
}
