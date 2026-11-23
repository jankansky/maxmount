// SPDX-License-Identifier: GPL-3.0-or-later
// mount-rt: real-time mount controller. This scaffold runs against simulated drives and feeds
// a demonstration trajectory; the SOEM backend and Zenoh bridge will replace those pieces.
#include <atomic>
#include <cmath>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <string>
#include <thread>
#include <vector>

#include "maxmount/core/controller.hpp"
#include "maxmount/core/realtime.hpp"
#include "maxmount/sim/sim_fieldbus.hpp"

namespace {

std::atomic<bool> g_stop{false};

void on_signal(int) { g_stop.store(true); }

struct Options {
    double duration_s = 10.0;
    int period_us = 1000;
    int rt_priority = 80;
    int rt_cpu = -1;
};

void usage() {
    std::puts(
        "usage: mount-rt [--duration S] [--period-us N] [--rt-prio P] [--rt-cpu C]\n"
        "  Runs the controller against simulated CiA 402 drives with a demo trajectory.");
}

Options parse(int argc, char** argv) {
    Options o;
    for (int i = 1; i < argc; ++i) {
        const std::string a = argv[i];
        auto next = [&]() -> const char* {
            if (i + 1 >= argc) {
                usage();
                std::exit(2);
            }
            return argv[++i];
        };
        if (a == "--duration") o.duration_s = std::atof(next());
        else if (a == "--period-us") o.period_us = std::atoi(next());
        else if (a == "--rt-prio") o.rt_priority = std::atoi(next());
        else if (a == "--rt-cpu") o.rt_cpu = std::atoi(next());
        else {
            usage();
            std::exit(a == "-h" || a == "--help" ? 0 : 2);
        }
    }
    return o;
}

constexpr double kPi = 3.14159265358979323846;
constexpr double kDeg = kPi / 180.0;
constexpr double kArcsec = kDeg / 3600.0;

// Demonstration profile: smooth 1 - cos moves starting at rest from the zero position.
struct DemoAxis {
    double amplitude_rad;
    double period_s;
    double pos(double t) const { return amplitude_rad * (1.0 - std::cos(2 * kPi * t / period_s)); }
    double vel(double t) const {
        return amplitude_rad * (2 * kPi / period_s) * std::sin(2 * kPi * t / period_s);
    }
};

}  // namespace

int main(int argc, char** argv) {
    using namespace maxmount;
    const Options opt = parse(argc, argv);
    std::signal(SIGINT, on_signal);
    std::signal(SIGTERM, on_signal);

    const nanos period = static_cast<nanos>(opt.period_us) * 1000;
    const double dt = ns_to_s(period);

    // Simulated axes: 2^24 counts per revolution, velocity feedforward in counts/s.
    const double counts_per_rad = 16777216.0 / (2 * kPi);
    std::vector<AxisConfig> axes(2);
    axes[0].name = "az";
    axes[0].counts_per_rad = counts_per_rad;
    axes[0].vel_units_per_rad_s = counts_per_rad;
    axes[0].limits = {-270 * kDeg, 270 * kDeg, 30 * kDeg, 20 * kDeg};
    axes[1].name = "el";
    axes[1].counts_per_rad = counts_per_rad;
    axes[1].vel_units_per_rad_s = counts_per_rad;
    axes[1].limits = {-45 * kDeg, 180 * kDeg, 10 * kDeg, 10 * kDeg};

    sim::SimFieldbus bus(axes.size(), dt);
    auto controller = std::make_unique<Controller>(bus, axes);
    if (!bus.start()) {
        std::fprintf(stderr, "fieldbus %s failed to start\n", std::string(bus.name()).c_str());
        return 1;
    }

    CyclicLoop loop(period);
    std::thread rt_thread([&] {
        const RealtimeSetup rs = configure_realtime_thread(opt.rt_priority, opt.rt_cpu);
        std::printf("rt thread: %s\n", rs.message.c_str());
        loop.run(g_stop, [&] { controller->cycle(realtime_ns(), dt); });
    });

    const std::vector<DemoAxis> demo = {{60 * kDeg, 20.0}, {30 * kDeg, 30.0}};
    const nanos t_start = realtime_ns() + 500'000'000;  // trajectory begins 0.5 s from now
    const nanos t_end = t_start + static_cast<nanos>(opt.duration_s * 1e9);
    const nanos point_step = 50'000'000;  // 20 Hz trajectory points
    const nanos lookahead = 1'000'000'000;
    nanos next_point = t_start;

    controller->post({CommandType::Enable, 0, {}});

    std::vector<double> prev_cmd(axes.size(), 0.0);
    std::vector<double> max_err(axes.size(), 0.0);
    TelemetrySample last{};
    nanos next_report = realtime_ns() + 1'000'000'000;
    bool disabling = false;

    while (!g_stop.load()) {
        const nanos now = realtime_ns();

        // Feed trajectory points (this is mount-svc's job in the full system).
        while (!disabling && next_point <= now + lookahead && next_point <= t_end) {
            const double t = ns_to_s(next_point - t_start);
            for (std::size_t i = 0; i < axes.size(); ++i) {
                controller->post({CommandType::PushPoint, static_cast<std::uint8_t>(i),
                                  {next_point, demo[i].pos(t), demo[i].vel(t)}});
            }
            next_point += point_step;
        }

        // Drain telemetry. Command lead equals one cycle, so the previous cycle's command is
        // the setpoint for "now"; compare it with the actual position.
        while (auto s = controller->poll_telemetry()) {
            for (std::size_t i = 0; i < s->n_axes; ++i) {
                if (s->axes[i].state == cia402::State::OperationEnabled) {
                    max_err[i] = std::max(max_err[i], std::abs(s->axes[i].act_pos - prev_cmd[i]));
                }
                prev_cmd[i] = s->axes[i].cmd_pos;
            }
            last = *s;
        }

        if (now >= next_report) {
            next_report += 1'000'000'000;
            std::printf("t=%6.1fs  loop max-latency %6.1f us  overruns %llu |",
                        ns_to_s(now - t_start), ns_to_s(loop.stats().max_wake_latency_ns.exchange(0)) * 1e6,
                        static_cast<unsigned long long>(loop.stats().overruns.load()));
            for (std::size_t i = 0; i < axes.size(); ++i) {
                const AxisTelemetry& a = last.axes[i];
                std::printf("  %s %-16s act %8.3f deg  err %7.2f\" flags 0x%02x |", axes[i].name.c_str(),
                            cia402::to_string(a.state), a.act_pos / kDeg, max_err[i] / kArcsec, a.flags);
                max_err[i] = 0.0;
            }
            std::printf("\n");
            std::fflush(stdout);
        }

        if (!disabling && now > t_end + 500'000'000) {
            controller->post({CommandType::Disable, 0, {}});
            disabling = true;
        }
        if (disabling && !last.enabled) break;

        std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }

    g_stop.store(true);
    rt_thread.join();
    bus.stop();
    std::printf("stopped after %llu cycles, %llu overruns\n",
                static_cast<unsigned long long>(loop.stats().cycles.load()),
                static_cast<unsigned long long>(loop.stats().overruns.load()));
    return 0;
}
