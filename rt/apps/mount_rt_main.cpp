// SPDX-License-Identifier: GPL-3.0-or-later
// mount-rt: real-time mount controller.
//
// Modes in this version:
//   simulation (default)       two simulated axes follow a demonstration trajectory
//   --soem IFNAME --descriptor F
//                              EtherCAT monitor: brings the bus to OP and reports drive state,
//                              statusword, position and errors. Drives are NEVER enabled in
//                              this mode; motion on hardware arrives with mount-svc.
#include <atomic>
#include <cmath>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <memory>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include "maxmount/config/drive_descriptor_yaml.hpp"
#include "maxmount/core/controller.hpp"
#include "maxmount/core/realtime.hpp"
#include "maxmount/sim/sim_fieldbus.hpp"
#if defined(MAXMOUNT_HAVE_SOEM)
#include "maxmount/fieldbus/soem_fieldbus.hpp"
#endif

namespace {

std::atomic<bool> g_stop{false};

void on_signal(int) { g_stop.store(true); }

struct Options {
    double duration_s = 10.0;
    int period_us = 1000;
    int rt_priority = 80;
    int rt_cpu = -1;
    std::string soem_interface;  // empty = simulation
    std::string descriptor_path;
    int drives = 2;
};

void usage() {
    std::puts(
        "usage: mount-rt [options]\n"
        "  --duration S        run time in seconds (default 10; 0 = until Ctrl-C)\n"
        "  --period-us N       cycle period (default 1000)\n"
        "  --rt-prio P         SCHED_FIFO priority (default 80, Linux)\n"
        "  --rt-cpu C          pin the RT thread to CPU C (Linux)\n"
        "  --soem IFNAME       EtherCAT monitor mode on IFNAME (drives are never enabled)\n"
        "  --descriptor FILE   drive descriptor YAML for --soem\n"
        "  --drives N          number of drives at bus positions 0..N-1 (default 2)");
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
        else if (a == "--soem") o.soem_interface = next();
        else if (a == "--descriptor") o.descriptor_path = next();
        else if (a == "--drives") o.drives = std::atoi(next());
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
    const bool hardware = !opt.soem_interface.empty();

    std::vector<AxisConfig> axes;
    std::unique_ptr<Fieldbus> bus;

    if (hardware) {
#if defined(MAXMOUNT_HAVE_SOEM)
        if (opt.descriptor_path.empty() || opt.drives < 1 || opt.drives > static_cast<int>(kMaxAxes)) {
            usage();
            return 2;
        }
        SoemConfig sc;
        sc.interface = opt.soem_interface;
        sc.cycle = period;
        try {
            const DriveDescriptor d = config::load_drive_descriptor(opt.descriptor_path);
            for (int i = 0; i < opt.drives; ++i) sc.drives.push_back({i, d});
        } catch (const std::exception& e) {
            std::fprintf(stderr, "%s\n", e.what());
            return 1;
        }
        // Monitor mode reports raw drive counts: one "radian" per count, no limits needed.
        for (int i = 0; i < opt.drives; ++i) {
            AxisConfig a;
            a.name = "drive" + std::to_string(i);
            axes.push_back(a);
        }
        auto soem = std::make_unique<SoemFieldbus>(sc);
        if (!soem->start()) {
            std::fprintf(stderr, "EtherCAT start failed: %s\n", soem->last_error().c_str());
            return 1;
        }
        std::printf("EtherCAT bus is OP on %s with %d drive(s); monitoring only\n", sc.interface.c_str(),
                    opt.drives);
        bus = std::move(soem);
#else
        std::fprintf(stderr, "built without SOEM support (configure with -DMAXMOUNT_WITH_SOEM=ON on Linux)\n");
        return 2;
#endif
    } else {
        // Simulated axes: 2^24 counts per revolution, velocity feedforward in counts/s.
        const double counts_per_rad = 16777216.0 / (2 * kPi);
        axes.resize(2);
        axes[0].name = "az";
        axes[0].limits = {-270 * kDeg, 270 * kDeg, 30 * kDeg, 20 * kDeg};
        axes[1].name = "el";
        axes[1].limits = {-45 * kDeg, 180 * kDeg, 10 * kDeg, 10 * kDeg};
        for (auto& a : axes) {
            a.counts_per_rad = counts_per_rad;
            a.vel_units_per_rad_s = counts_per_rad;
        }
        bus = std::make_unique<sim::SimFieldbus>(axes.size(), dt);
        bus->start();
    }

    auto controller = std::make_unique<Controller>(*bus, axes);

    CyclicLoop loop(period);
    std::thread rt_thread([&] {
        const RealtimeSetup rs = configure_realtime_thread(opt.rt_priority, opt.rt_cpu);
        std::printf("rt thread: %s\n", rs.message.c_str());
        // Commands are evaluated at the scheduled cycle time on the UTC (CLOCK_REALTIME)
        // scale. The offset is fixed at start-up; tying it to the drives' distributed clock
        // and PPS-disciplined UTC is a later step.
        const nanos mono_to_utc = realtime_ns() - monotonic_ns();
        loop.run(g_stop, [&](nanos scheduled) -> nanos {
            controller->cycle(scheduled + mono_to_utc, dt);
            return bus->cycle_correction_ns();
        });
    });

    const std::vector<DemoAxis> demo = {{60 * kDeg, 20.0}, {30 * kDeg, 30.0}};
    const nanos t_start = realtime_ns() + 500'000'000;
    const nanos t_end = opt.duration_s > 0 ? t_start + static_cast<nanos>(opt.duration_s * 1e9) : INT64_MAX;
    const nanos point_step = 50'000'000;  // 20 Hz trajectory points
    const nanos lookahead = 1'000'000'000;
    nanos next_point = t_start;

    if (!hardware) controller->post({CommandType::Enable, 0, {}});

    std::vector<double> prev_cmd(axes.size(), 0.0);
    std::vector<double> max_err(axes.size(), 0.0);
    TelemetrySample last{};
    std::uint64_t stale_cycles = 0;
    nanos next_report = realtime_ns() + 1'000'000'000;
    bool disabling = false;

    while (!g_stop.load()) {
        const nanos now = realtime_ns();

        while (!hardware && !disabling && next_point <= now + lookahead && next_point <= t_end) {
            const double t = ns_to_s(next_point - t_start);
            for (std::size_t i = 0; i < axes.size(); ++i) {
                controller->post({CommandType::PushPoint, static_cast<std::uint8_t>(i),
                                  {next_point, demo[i].pos(t), demo[i].vel(t)}});
            }
            next_point += point_step;
        }

        while (auto s = controller->poll_telemetry()) {
            if (!s->inputs_fresh) ++stale_cycles;
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
            std::printf("t=%6.1fs  max-lat %6.1f us  overruns %llu", ns_to_s(now - t_start),
                        ns_to_s(loop.stats().max_wake_latency_ns.exchange(0)) * 1e6,
                        static_cast<unsigned long long>(loop.stats().overruns.load()));
            if (hardware) {
                std::printf("  stale %llu lost %llu wkc-err %llu", static_cast<unsigned long long>(stale_cycles),
                            static_cast<unsigned long long>(last.bus_lost_frames),
                            static_cast<unsigned long long>(last.bus_wkc_errors));
            }
            std::printf(" |");
            for (std::size_t i = 0; i < axes.size(); ++i) {
                const AxisTelemetry& a = last.axes[i];
                if (hardware) {
                    std::printf(" %s %-18s sw 0x%04x err 0x%04x pos %11.0f ferr %6d di 0x%08x |",
                                axes[i].name.c_str(), cia402::to_string(a.state), a.statusword,
                                a.error_code, a.act_pos, a.following_error, a.digital_inputs);
                } else {
                    std::printf(" %s %-16s act %8.3f deg err %7.2f\" flags 0x%02x |", axes[i].name.c_str(),
                                cia402::to_string(a.state), a.act_pos / kDeg, max_err[i] / kArcsec, a.flags);
                }
                max_err[i] = 0.0;
            }
            std::printf("\n");
            std::fflush(stdout);
        }

        if (hardware) {
            if (now > t_end) break;
        } else {
            if (!disabling && now > t_end + 500'000'000) {
                controller->post({CommandType::Disable, 0, {}});
                disabling = true;
            }
            if (disabling && !last.enabled) break;
        }

        std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }

    g_stop.store(true);
    rt_thread.join();
    bus->stop();
    std::printf("stopped after %llu cycles, %llu overruns\n",
                static_cast<unsigned long long>(loop.stats().cycles.load()),
                static_cast<unsigned long long>(loop.stats().overruns.load()));
    return 0;
}
