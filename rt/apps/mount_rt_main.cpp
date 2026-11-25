// SPDX-License-Identifier: GPL-3.0-or-later
// mount-rt: real-time mount controller.
//
// The system comes from --system FILE (config/systems/<name>/system.yaml); without it a
// built-in two-axis simulation is used. Modes:
//   --monitor   bring the bus up and report drive state; drives are NEVER enabled.
//               Default for EtherCAT systems.
//   --demo      follow a demonstration trajectory. Simulation only; default there.
//   --jog AXIS --by DEG
//               supervised point-to-point move of one axis (enables only that axis), then
//               disable. Asks for confirmation on hardware unless --yes.
//   --serve     accept trajectories and commands over Zenoh (mount-svc, GUI) until Ctrl-C.
// With a `zenoh:` section in the system file (and -DMAXMOUNT_WITH_ZENOH=ON) telemetry is
// published in every mode; only --serve accepts commands.
// Ctrl-C: first press decelerates and disables the drives, a second press quick-stops them.
// The real-time loop keeps running until the drives are disabled.
#include <algorithm>
#include <atomic>
#include <cmath>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <iostream>
#include <memory>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include "maxmount/config/drive_descriptor_yaml.hpp"
#include "maxmount/config/system_config.hpp"
#include "maxmount/core/controller.hpp"
#include "maxmount/core/realtime.hpp"
#include "maxmount/sim/sim_fieldbus.hpp"
#include "maxmount/trajectory/move_profile.hpp"
#if defined(MAXMOUNT_HAVE_ZENOH)
#include "maxmount/bridge/zenoh_bridge.hpp"
#endif
#if defined(MAXMOUNT_HAVE_SOEM)
#include "maxmount/fieldbus/soem_fieldbus.hpp"
#endif

namespace {

using namespace maxmount;

std::atomic<int> g_interrupts{0};
void on_signal(int) { g_interrupts.fetch_add(1); }

constexpr double kPi = 3.14159265358979323846;
constexpr double kDeg = kPi / 180.0;
constexpr double kArcsec = kDeg / 3600.0;
constexpr nanos kSecond = 1'000'000'000;

enum class Mode { Default, Monitor, Demo, Jog, Serve };

struct Options {
    std::string system_path;
    std::vector<std::string> axes;  // empty = all
    Mode mode = Mode::Default;
    double duration_s = -1;  // < 0: mode default; 0: until Ctrl-C
    std::optional<int> period_us, rt_priority, rt_cpu;
    // jog
    std::string jog_axis;
    std::optional<double> jog_by_deg, jog_vel_deg_s, jog_acc_deg_s2;
    bool yes = false;
    bool no_zenoh = false;
    // ad-hoc EtherCAT monitor without a system file
    std::string soem_interface, descriptor_path;
    int drives = 1;
};

void usage() {
    std::puts(
        "usage: mount-rt [--system FILE] [--axes A,B] [mode] [options]\n"
        "modes:\n"
        "  --monitor             report drive state, never enable (default for EtherCAT)\n"
        "  --demo                demonstration trajectory (simulation only; default there)\n"
        "  --jog AXIS --by DEG   move one axis by DEG, then disable\n"
        "      --vel DEG_S       jog speed (default 1, capped by the axis limit)\n"
        "      --acc DEG_S2      jog acceleration (default 2, capped by the axis limit)\n"
        "      --yes             skip the confirmation prompt\n"
        "  --serve               accept trajectories and commands over Zenoh until Ctrl-C\n"
        "  --no-zenoh            do not start the Zenoh bridge even if configured\n"
        "options:\n"
        "  --system FILE         system configuration (default: built-in simulation)\n"
        "  --axes A,B            use only these axes, e.g. a test bench with one drive\n"
        "  --duration S          monitor/demo run time (0 = until Ctrl-C)\n"
        "  --period-us N         override the cycle period\n"
        "  --rt-prio P           override SCHED_FIFO priority (0 = none)\n"
        "  --rt-cpu C            override the CPU the RT thread is pinned to\n"
        "ad-hoc EtherCAT monitor without a system file:\n"
        "  --soem IFNAME --descriptor FILE [--drives N]");
}

std::vector<std::string> split_list(const std::string& s) {
    std::vector<std::string> out;
    std::stringstream ss(s);
    for (std::string item; std::getline(ss, item, ',');) {
        if (!item.empty()) out.push_back(item);
    }
    return out;
}

Options parse(int argc, char** argv) {
    Options o;
    for (int i = 1; i < argc; ++i) {
        const std::string a = argv[i];
        auto next = [&]() -> std::string {
            if (i + 1 >= argc) {
                usage();
                std::exit(2);
            }
            return argv[++i];
        };
        auto num = [&]() {
            const std::string v = next();
            try {
                return std::stod(v);
            } catch (const std::exception&) {
                std::fprintf(stderr, "%s: '%s' is not a number\n", a.c_str(), v.c_str());
                std::exit(2);
            }
        };
        if (a == "--system") o.system_path = next();
        else if (a == "--axes") o.axes = split_list(next());
        else if (a == "--monitor") o.mode = Mode::Monitor;
        else if (a == "--demo") o.mode = Mode::Demo;
        else if (a == "--serve") o.mode = Mode::Serve;
        else if (a == "--no-zenoh") o.no_zenoh = true;
        else if (a == "--jog") { o.mode = Mode::Jog; o.jog_axis = next(); }
        else if (a == "--by") o.jog_by_deg = num();
        else if (a == "--vel") o.jog_vel_deg_s = num();
        else if (a == "--acc") o.jog_acc_deg_s2 = num();
        else if (a == "--yes") o.yes = true;
        else if (a == "--duration") o.duration_s = num();
        else if (a == "--period-us") o.period_us = static_cast<int>(num());
        else if (a == "--rt-prio") o.rt_priority = static_cast<int>(num());
        else if (a == "--rt-cpu") o.rt_cpu = static_cast<int>(num());
        else if (a == "--soem") o.soem_interface = next();
        else if (a == "--descriptor") o.descriptor_path = next();
        else if (a == "--drives") o.drives = static_cast<int>(num());
        else {
            usage();
            std::exit(a == "-h" || a == "--help" ? 0 : 2);
        }
    }
    return o;
}

// Built-in simulation, equivalent to config/systems/sim-altaz.
config::SystemConfig builtin_simulation() {
    config::SystemConfig s;
    s.name = "builtin-sim";
    s.backend = "sim";
    const double counts_per_rad = 16777216.0 / (2 * kPi);
    const char* names[] = {"az", "el"};
    const AxisLimits limits[] = {{-270 * kDeg, 270 * kDeg, 30 * kDeg, 20 * kDeg, 1 * kDeg},
                                 {-45 * kDeg, 180 * kDeg, 10 * kDeg, 10 * kDeg, 1 * kDeg}};
    for (int i = 0; i < 2; ++i) {
        config::AxisEntry e;
        e.axis.name = names[i];
        e.axis.counts_per_rad = counts_per_rad;
        e.axis.limits = limits[i];
        e.drive = "sim_cia402";
        e.bus_position = i;
        e.scaled = true;
        e.counts_per_rev = 16777216.0;
        s.axes.push_back(e);
    }
    return s;
}

// Ad-hoc monitor: N identical drives at bus positions 0..N-1, unscaled.
config::SystemConfig adhoc_soem(const Options& o) {
    if (o.descriptor_path.empty() || o.drives < 1 || o.drives > static_cast<int>(kMaxAxes)) {
        throw std::runtime_error("--soem needs --descriptor FILE and --drives 1.." + std::to_string(kMaxAxes));
    }
    config::SystemConfig s;
    s.name = "adhoc";
    s.backend = "soem";
    s.interface = o.soem_interface;
    const DriveDescriptor d = config::load_drive_descriptor(o.descriptor_path);
    for (int i = 0; i < o.drives; ++i) {
        config::AxisEntry e;
        e.axis.name = "drive" + std::to_string(i);
        e.drive = o.descriptor_path;
        e.bus_position = i;
        e.descriptor = d;
        s.axes.push_back(e);
    }
    return s;
}

// CiA 402 / CiA 301 error code classes (the high byte is standardised; details are
// manufacturer-specific, see the drive manual).
const char* error_class(std::uint16_t code) {
    if (code == 0) return "no error";
    if (code == 0x8611) return "following error";
    switch (code >> 12) {
        case 0x2: return "current";
        case 0x3: return "voltage";
        case 0x4: return "temperature";
        case 0x5: return "device hardware";
        case 0x6: return "device software";
        case 0x7: return (code >> 8) == 0x73 ? "sensor / feedback" : "additional modules";
        case 0x8: return (code >> 8) == 0x81 ? "communication" : "monitoring";
        case 0x9: return "external error";
        case 0xF: return "manufacturer-specific / additional functions";
        default: return "unclassified";
    }
}

class App {
public:
    App(const Options& opt, config::SystemConfig sys) : opt_(opt), sys_(std::move(sys)) {}

    int run() {
        if (const int rc = check_mode(); rc != 0) return rc;
        if (!start_bus()) return 1;

        std::vector<AxisConfig> axes;
        for (const auto& a : sys_.axes) axes.push_back(a.axis);
        ControllerConfig cc;
        cc.command_lead_ns = sys_.command_lead;
        controller_ = std::make_unique<Controller>(*bus_, axes, cc);
        start_rt_thread();
        // Keep this thread, and the Zenoh threads it starts, off the real-time core.
        if (!avoid_cpu(sys_.rt_cpu)) std::printf("note: could not move non-RT threads off CPU %d\n", sys_.rt_cpu);
        if (!start_bridge()) {
            shutdown();
            return 1;
        }

        int rc = 0;
        switch (mode_) {
            case Mode::Monitor: rc = run_monitor(); break;
            case Mode::Demo: rc = run_demo(); break;
            case Mode::Jog: rc = run_jog(); break;
            case Mode::Serve: rc = run_serve(); break;
            case Mode::Default: break;
        }
        if (!shutdown()) rc = rc ? rc : 1;
        return rc;
    }

private:
    bool hardware() const { return sys_.backend == "soem"; }
    double dt() const { return ns_to_s(sys_.cycle); }

    int check_mode() {
        mode_ = opt_.mode == Mode::Default ? (hardware() ? Mode::Monitor : Mode::Demo) : opt_.mode;
        if (mode_ == Mode::Demo && hardware()) {
            std::fprintf(stderr, "--demo is simulation-only; use --jog for supervised moves on hardware\n");
            return 2;
        }
        if (mode_ == Mode::Serve) {
#if !defined(MAXMOUNT_HAVE_ZENOH)
            std::fprintf(stderr, "--serve needs mount-rt built with -DMAXMOUNT_WITH_ZENOH=ON\n");
            return 2;
#else
            if (!sys_.zenoh.enabled || opt_.no_zenoh) {
                std::fprintf(stderr, "--serve needs a zenoh: section in the system file\n");
                return 2;
            }
#endif
        }
        if (mode_ == Mode::Demo && sys_.axes.size() > 2) {
            std::fprintf(stderr, "--demo supports at most 2 axes\n");
            return 2;
        }
        if (mode_ != Mode::Jog) return 0;

        jog_index_ = -1;
        for (std::size_t i = 0; i < sys_.axes.size(); ++i) {
            if (sys_.axes[i].axis.name == opt_.jog_axis) jog_index_ = static_cast<int>(i);
        }
        if (jog_index_ < 0) {
            std::fprintf(stderr, "--jog: no axis named '%s' (check --axes)\n", opt_.jog_axis.c_str());
            return 2;
        }
        const config::AxisEntry& a = sys_.axes[static_cast<std::size_t>(jog_index_)];
        if (!opt_.jog_by_deg || *opt_.jog_by_deg == 0.0) {
            std::fprintf(stderr, "--jog needs --by DEG (non-zero)\n");
            return 2;
        }
        if (!a.scaled) {
            std::fprintf(stderr,
                         "--jog: axis '%s' has no counts_per_rev in the system file; commission the "
                         "axis and set it first\n",
                         a.axis.name.c_str());
            return 2;
        }
        const AxisLimits& l = a.axis.limits;
        jog_vel_ = opt_.jog_vel_deg_s.value_or(1.0) * kDeg;
        jog_acc_ = opt_.jog_acc_deg_s2.value_or(2.0) * kDeg;
        if (!(jog_vel_ > 0) || !(jog_acc_ > 0)) {
            std::fprintf(stderr, "--vel and --acc must be > 0\n");
            return 2;
        }
        if (jog_vel_ > l.max_vel || jog_acc_ > l.max_acc) {
            std::fprintf(stderr, "--vel/--acc exceed the axis limits (%.2f deg/s, %.2f deg/s^2)\n",
                         l.max_vel / kDeg, l.max_acc / kDeg);
            return 2;
        }
        if (hardware() && std::isinf(l.max_following_error)) {
            std::fprintf(stderr, "--jog on hardware needs limits.max_following_error_deg for axis '%s'\n",
                         a.axis.name.c_str());
            return 2;
        }
        return 0;
    }

    bool start_bus() {
        if (!hardware()) {
            bus_ = std::make_unique<sim::SimFieldbus>(sys_.axes.size(), dt());
            bus_->start();
            return true;
        }
#if defined(MAXMOUNT_HAVE_SOEM)
        SoemConfig sc;
        sc.interface = sys_.interface;
        sc.cycle = sys_.cycle;
        for (const auto& a : sys_.axes) sc.drives.push_back({a.bus_position, a.descriptor});
        auto soem = std::make_unique<SoemFieldbus>(sc);
        if (!soem->start()) {
            std::fprintf(stderr, "EtherCAT start failed: %s\n", soem->last_error().c_str());
            return false;
        }
        std::printf("EtherCAT bus is OP on %s with %zu drive(s)\n", sc.interface.c_str(), sc.drives.size());
        bus_ = std::move(soem);
        return true;
#else
        std::fprintf(stderr, "built without SOEM support (configure with -DMAXMOUNT_WITH_SOEM=ON on Linux)\n");
        return false;
#endif
    }

    void start_rt_thread() {
        loop_ = std::make_unique<CyclicLoop>(sys_.cycle);
        rt_thread_ = std::thread([this] {
            const RealtimeSetup rs = configure_realtime_thread(sys_.rt_priority, sys_.rt_cpu);
            std::printf("rt thread: %s\n", rs.message.c_str());
            // Commands are evaluated at the scheduled cycle time on the UTC (CLOCK_REALTIME)
            // scale. The offset is fixed at start-up; tying it to the drives' distributed
            // clock and PPS-disciplined UTC is a later step.
            const nanos mono_to_utc = realtime_ns() - monotonic_ns();
            const double step = dt();
            loop_->run(rt_stop_, [&](nanos scheduled) -> nanos {
                controller_->cycle(scheduled + mono_to_utc, step);
                return bus_->cycle_correction_ns();
            });
        });
    }

    // Drains telemetry into last_, tracks per-axis max |actual - previous command| and reads
    // the error code (by SDO) of any drive that has entered Fault.
    // Why an axis may not be enabled from Zenoh (empty = allowed).
    std::string enable_refusal(std::size_t i) const {
        const config::AxisEntry& a = sys_.axes[i];
        if (!a.scaled) return "axis '" + a.axis.name + "' has no counts_per_rev (not commissioned)";
        if (hardware() && std::isinf(a.axis.limits.max_following_error)) {
            return "axis '" + a.axis.name + "' has no max_following_error_deg";
        }
        return {};
    }

    bool start_bridge() {
        if (!sys_.zenoh.enabled || opt_.no_zenoh) return true;
#if defined(MAXMOUNT_HAVE_ZENOH)
        bridge::BridgeOptions o;
        o.zenoh = sys_.zenoh;
        for (const auto& a : sys_.axes) o.axis_names.push_back(a.axis.name);
        o.cycle = sys_.cycle;
        o.accept_commands = mode_ == Mode::Serve;
        o.reject_reason = "mount-rt is not in serve mode";
        // Runs on a Zenoh thread: reads only immutable configuration and the sample passed in.
        o.validate = [this](const Command& c, const TelemetrySample& last) -> std::string {
            if (c.type != CommandType::Enable) return {};
            if (last.tripped) return "following-error trip latched; send FaultReset first";
            for (std::size_t i = 0; i < sys_.axes.size(); ++i) {
                if (c.axis != kAllAxes && c.axis != i) continue;
                if (std::string why = enable_refusal(i); !why.empty()) return why;
                if (last.axes[i].state == cia402::State::Fault) {
                    return "axis '" + sys_.axes[i].axis.name + "' is in Fault; send FaultReset first";
                }
            }
            return {};
        };
        try {
            bridge_ = std::make_unique<bridge::ZenohBridge>(o);
            bridge_->start();
        } catch (const std::exception& e) {
            std::fprintf(stderr, "Zenoh bridge failed to start: %s\n", e.what());
            return false;
        }
        std::printf("zenoh: %s on %s* (%s)\n", mode_ == Mode::Serve ? "serving" : "publishing telemetry",
                    bridge_->key("").c_str(), sys_.zenoh.mode.c_str());
#else
        std::printf("note: system file has a zenoh section but mount-rt was built without Zenoh\n");
#endif
        return true;
    }

    void pump() {
#if defined(MAXMOUNT_HAVE_ZENOH)
        if (bridge_) bridge_->forward(*controller_);
        const bridge::LoopSnapshot loop{loop_->stats().cycles.load(), loop_->stats().overruns.load(),
                                        loop_->stats().max_wake_latency_ns.load()};
#endif
        while (auto s = controller_->poll_telemetry()) {
#if defined(MAXMOUNT_HAVE_ZENOH)
            if (bridge_) bridge_->on_telemetry(*s, loop);
#endif
            if (!s->inputs_fresh) ++stale_;
            for (std::size_t i = 0; i < s->n_axes; ++i) {
                if (s->axes[i].state == cia402::State::OperationEnabled) {
                    max_err_[i] = std::max(max_err_[i], std::abs(s->axes[i].act_pos - prev_cmd_[i]));
                }
                prev_cmd_[i] = s->axes[i].cmd_pos;
            }
            last_ = *s;
            have_telemetry_ = true;
        }
        for (std::size_t i = 0; i < last_.n_axes; ++i) {
            const bool fault = last_.axes[i].state == cia402::State::Fault;
            if (fault && !fault_reported_[i]) {
                fault_reported_[i] = true;
                const auto code = bus_->read_sdo(i, 0x603F, 0, 2);
                if (code) {
                    std::printf("!! %s: drive fault, error code 0x%04X (%s)\n", sys_.axes[i].axis.name.c_str(),
                                *code, error_class(static_cast<std::uint16_t>(*code)));
                } else {
                    std::printf("!! %s: drive fault (error code could not be read)\n",
                                sys_.axes[i].axis.name.c_str());
                }
            }
            if (!fault) fault_reported_[i] = false;
        }
        if (last_.tripped && !trip_reported_) {
            trip_reported_ = true;
            for (std::size_t i = 0; i < last_.n_axes; ++i) {
                if (last_.axes[i].flags & axis_flags::FollowingErrorTrip) {
                    std::printf("!! %s: following error above %.3f deg: all axes quick-stopped\n",
                                sys_.axes[i].axis.name.c_str(), sys_.axes[i].axis.limits.max_following_error / kDeg);
                }
            }
        }
    }

    void report(nanos t0) {
        std::printf("t=%6.1fs  max-lat %6.1f us  overruns %llu", ns_to_s(realtime_ns() - t0),
                    ns_to_s(loop_->stats().max_wake_latency_ns.exchange(0)) * 1e6,
                    static_cast<unsigned long long>(loop_->stats().overruns.load()));
        if (hardware()) {
            std::printf("  stale %llu lost %llu wkc-err %llu", static_cast<unsigned long long>(stale_),
                        static_cast<unsigned long long>(last_.bus_lost_frames),
                        static_cast<unsigned long long>(last_.bus_wkc_errors));
        }
        std::printf(" |");
        for (std::size_t i = 0; i < sys_.axes.size(); ++i) {
            const AxisTelemetry& a = last_.axes[i];
            const config::AxisEntry& e = sys_.axes[i];
            std::printf(" %s %-17s", e.axis.name.c_str(), cia402::to_string(a.state));
            if (hardware()) std::printf(" sw 0x%04x", a.statusword);
            if (e.scaled) {
                std::printf(" act %9.4f deg err %7.2f\"", a.act_pos / kDeg, max_err_[i] / kArcsec);
            } else {
                std::printf(" pos %11.0f cnt", a.act_pos);
            }
            if (hardware()) std::printf(" ferr %6d di 0x%08x", a.following_error, a.digital_inputs);
            std::printf(" flags 0x%02x |", a.flags);
            max_err_[i] = 0.0;
        }
        std::printf("\n");
        std::fflush(stdout);
    }

    // Runs until the deadline or Ctrl-C, reporting once a second. extra() runs every pass.
    template <typename Extra>
    void loop_until(nanos deadline, nanos t0, Extra&& extra) {
        nanos next_report = realtime_ns() + kSecond;
        while (g_interrupts.load() == 0 && realtime_ns() < deadline) {
            pump();
            if (!extra()) break;
            if (realtime_ns() >= next_report) {
                next_report += kSecond;
                report(t0);
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(mode_ == Mode::Serve ? 5 : 20));
        }
    }

    nanos deadline_from(double default_s) const {
        const double s = opt_.duration_s < 0 ? default_s : opt_.duration_s;
        return s > 0 ? realtime_ns() + static_cast<nanos>(s * 1e9) : INT64_MAX;
    }

    int run_serve() {
        std::printf("serving: waiting for trajectories and commands (Ctrl-C to stop)\n");
        const nanos t0 = realtime_ns();
        loop_until(deadline_from(0), t0, [] { return true; });
#if defined(MAXMOUNT_HAVE_ZENOH)
        if (bridge_) {
            const auto st = bridge_->stats();
            std::printf("zenoh: %llu trajectories, %llu commands, %llu rejected, %llu telemetry messages\n",
                        static_cast<unsigned long long>(st.trajectories), static_cast<unsigned long long>(st.commands),
                        static_cast<unsigned long long>(st.rejected), static_cast<unsigned long long>(st.published));
        }
#endif
        return 0;
    }

    int run_monitor() {
        std::printf("monitoring only: drives are not enabled\n");
        const nanos t0 = realtime_ns();
        loop_until(deadline_from(0), t0, [] { return true; });
        return 0;
    }

    int run_demo() {
        struct DemoAxis {
            double amplitude_rad, period_s;
            double pos(double t) const { return amplitude_rad * (1.0 - std::cos(2 * kPi * t / period_s)); }
            double vel(double t) const { return amplitude_rad * (2 * kPi / period_s) * std::sin(2 * kPi * t / period_s); }
        };
        const DemoAxis demo[] = {{60 * kDeg, 20.0}, {30 * kDeg, 30.0}};
        const nanos t0 = realtime_ns() + 500'000'000;
        const double run_s = opt_.duration_s < 0 ? 10.0 : opt_.duration_s;
        const nanos t_end = run_s > 0 ? t0 + static_cast<nanos>(run_s * 1e9) : INT64_MAX;
        nanos next_point = t0;
        controller_->post({CommandType::Enable, kAllAxes, {}});
        loop_until(t_end == INT64_MAX ? INT64_MAX : t_end + 500'000'000, t0, [&] {
            while (next_point <= realtime_ns() + kSecond && next_point <= t_end) {
                const double t = ns_to_s(next_point - t0);
                for (std::size_t i = 0; i < sys_.axes.size(); ++i) {
                    controller_->post({CommandType::PushPoint, static_cast<std::uint8_t>(i),
                                       {next_point, demo[i].pos(t), demo[i].vel(t)}});
                }
                next_point += 50'000'000;  // 20 Hz trajectory points
            }
            return !last_.tripped;
        });
        return last_.tripped ? 1 : 0;
    }

    // Waits up to timeout for pred(), pumping telemetry. False on timeout, Ctrl-C or trip.
    template <typename Pred>
    bool wait_for(nanos timeout, Pred&& pred) {
        const nanos end = realtime_ns() + timeout;
        while (realtime_ns() < end) {
            pump();
            if (have_telemetry_ && pred()) return true;
            if (g_interrupts.load() > 0 || last_.tripped) return false;
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        return false;
    }

    int run_jog() {
        const auto j = static_cast<std::size_t>(jog_index_);
        const config::AxisEntry& a = sys_.axes[j];
        const AxisLimits& l = a.axis.limits;

        // Settle: fresh inputs and no faults before anything is enabled.
        if (!wait_for(2 * kSecond, [&] { return last_.inputs_fresh; })) {
            std::fprintf(stderr, "jog: no process data from the drives\n");
            return 1;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
        pump();
        for (std::size_t i = 0; i < last_.n_axes; ++i) {
            if (last_.axes[i].state == cia402::State::Fault) {
                std::fprintf(stderr, "jog: %s is in Fault; fix the cause, then power-cycle or reset the drive\n",
                             sys_.axes[i].axis.name.c_str());
                return 1;
            }
        }

        const double start = last_.axes[j].act_pos;
        const double target = start + *opt_.jog_by_deg * kDeg;
        if (target < l.min_pos || target > l.max_pos) {
            std::fprintf(stderr, "jog: target %.4f deg is outside the soft limits [%.1f, %.1f] deg\n",
                         target / kDeg, l.min_pos / kDeg, l.max_pos / kDeg);
            return 1;
        }
        const MoveProfile move(start, target, jog_vel_, jog_acc_);
        std::printf("jog %s: %.4f -> %.4f deg (%+.4f deg), %.2f deg/s, %.2f deg/s^2, %.1f s; "
                    "following-error trip %.3f deg\n",
                    a.axis.name.c_str(), start / kDeg, target / kDeg, *opt_.jog_by_deg, move.peak_velocity() / kDeg,
                    jog_acc_ / kDeg, move.duration(), l.max_following_error / kDeg);

        if (hardware() && !opt_.yes) {
            std::printf("Only axis '%s' will be enabled. Keep clear of the mount and be ready to cut motor "
                        "power.\nType 'yes' to move: ",
                        a.axis.name.c_str());
            std::fflush(stdout);
            std::string answer;
            std::getline(std::cin, answer);
            if (answer != "yes") {
                std::printf("cancelled\n");
                return 0;
            }
        }

        controller_->post({CommandType::ClearTrajectory, kAllAxes, {}});
        controller_->post({CommandType::Enable, static_cast<std::uint8_t>(j), {}});
        if (!wait_for(3 * kSecond, [&] { return last_.axes[j].state == cia402::State::OperationEnabled; })) {
            std::fprintf(stderr, "jog: %s did not reach Operation Enabled (state %s)\n", a.axis.name.c_str(),
                         cia402::to_string(last_.axes[j].state));
            return 1;
        }

        // Re-read the start: the axis may have settled while enabling. Abort on a jump.
        pump();
        const double start2 = last_.axes[j].act_pos;
        if (std::abs(start2 - start) > l.max_following_error) {
            std::fprintf(stderr, "jog: %s moved %.4f deg while enabling; aborting\n", a.axis.name.c_str(),
                         (start2 - start) / kDeg);
            return 1;
        }

        const nanos t0 = realtime_ns() + 300'000'000;
        const nanos t_end = t0 + static_cast<nanos>(move.duration() * 1e9);
        for (nanos t = t0;; t += 50'000'000) {
            const nanos tp = std::min(t, t_end);
            const auto s = move.at(ns_to_s(tp - t0));
            controller_->post({CommandType::PushPoint, static_cast<std::uint8_t>(j), {tp, s.pos, s.vel}});
            if (tp == t_end) break;
        }
        // Hold the target briefly after the move.
        controller_->post({CommandType::PushPoint, static_cast<std::uint8_t>(j), {t_end + kSecond, target, 0.0}});

        double max_err = 0;
        loop_until(t_end + kSecond, t0, [&] {
            max_err = std::max(max_err, max_err_[j]);
            return !last_.tripped && last_.axes[j].state == cia402::State::OperationEnabled;
        });
        max_err = std::max(max_err, max_err_[j]);
        pump();

        if (last_.tripped || last_.axes[j].state != cia402::State::OperationEnabled) {
            std::fprintf(stderr, "jog: aborted (state %s)\n", cia402::to_string(last_.axes[j].state));
            return 1;
        }
        if (g_interrupts.load() > 0) {
            std::printf("jog interrupted: %s at %.4f deg\n", a.axis.name.c_str(), last_.axes[j].act_pos / kDeg);
            return 1;
        }
        std::printf("jog done: %s at %.4f deg (target %.4f, error %.2f\"), max |act - cmd| %.2f\"\n",
                    a.axis.name.c_str(), last_.axes[j].act_pos / kDeg, target / kDeg,
                    (last_.axes[j].act_pos - target) / kArcsec, max_err / kArcsec);
        return 0;
    }

    // Disable (controlled stop), quick-stop if that fails or on a second Ctrl-C, then stop
    // the RT thread and the bus. True if the drives ended disabled.
    bool shutdown() {
        const int interrupts_at_start = g_interrupts.load();
#if defined(MAXMOUNT_HAVE_ZENOH)
        // Stop taking commands first so nothing can re-enable the drives while stopping.
        if (bridge_) {
            bridge_->stop();
            bridge_.reset();
        }
#endif
        pump();
        bool ok = true;
        if (last_.enabled) {
            std::printf("disabling drives...\n");
            controller_->post({CommandType::Disable, kAllAxes, {}});
            const nanos end = realtime_ns() + 10 * kSecond;
            while (realtime_ns() < end && last_.enabled && g_interrupts.load() <= std::max(interrupts_at_start, 1)) {
                pump();
                std::this_thread::sleep_for(std::chrono::milliseconds(10));
            }
            if (last_.enabled) {
                std::printf("quick stop\n");
                controller_->post({CommandType::QuickStop, kAllAxes, {}});
                ok = false;
            }
        }
        // Let the drives see the disable / quick stop before the bus goes down.
        const nanos end = realtime_ns() + 2 * kSecond;
        while (realtime_ns() < end) {
            pump();
            bool any_op = false;
            for (std::size_t i = 0; i < last_.n_axes; ++i) {
                any_op = any_op || last_.axes[i].state == cia402::State::OperationEnabled;
            }
            if (!any_op) break;
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
        rt_stop_.store(true);
        if (rt_thread_.joinable()) rt_thread_.join();
        bus_->stop();
        std::printf("stopped after %llu cycles, %llu overruns\n",
                    static_cast<unsigned long long>(loop_->stats().cycles.load()),
                    static_cast<unsigned long long>(loop_->stats().overruns.load()));
        return ok;
    }

    const Options& opt_;
    config::SystemConfig sys_;
    Mode mode_ = Mode::Default;
    int jog_index_ = -1;
    double jog_vel_ = 0, jog_acc_ = 0;

    std::unique_ptr<Fieldbus> bus_;
    std::unique_ptr<Controller> controller_;
    std::unique_ptr<CyclicLoop> loop_;
    std::thread rt_thread_;
    std::atomic<bool> rt_stop_{false};

#if defined(MAXMOUNT_HAVE_ZENOH)
    std::unique_ptr<bridge::ZenohBridge> bridge_;
#endif
    TelemetrySample last_{};
    bool have_telemetry_ = false;
    std::uint64_t stale_ = 0;
    std::array<double, kMaxAxes> prev_cmd_{}, max_err_{};
    std::array<bool, kMaxAxes> fault_reported_{};
    bool trip_reported_ = false;
};

}  // namespace

int main(int argc, char** argv) {
    const Options opt = parse(argc, argv);
    std::signal(SIGINT, on_signal);
    std::signal(SIGTERM, on_signal);

    config::SystemConfig sys;
    try {
        if (!opt.system_path.empty()) {
            sys = config::load_system_config(opt.system_path);
        } else if (!opt.soem_interface.empty()) {
            sys = adhoc_soem(opt);
        } else {
            sys = builtin_simulation();
        }
        if (!opt.axes.empty()) sys = config::select_axes(sys, opt.axes);
    } catch (const std::exception& e) {
        std::fprintf(stderr, "%s\n", e.what());
        return 1;
    }
    if (opt.period_us) sys.cycle = static_cast<nanos>(*opt.period_us) * 1000;
    if (opt.rt_priority) sys.rt_priority = *opt.rt_priority;
    if (opt.rt_cpu) sys.rt_cpu = *opt.rt_cpu;

    try {
        App app(opt, std::move(sys));
        return app.run();
    } catch (const std::exception& e) {
        std::fprintf(stderr, "mount-rt: %s\n", e.what());
        return 1;
    }
}
