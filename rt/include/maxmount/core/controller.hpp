// SPDX-License-Identifier: GPL-3.0-or-later
// The mount controller: one cycle() call per fieldbus period. Commands enter and telemetry
// leaves through lock-free SPSC rings, so cycle() never blocks or allocates.
#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <vector>

#include "maxmount/axis/axis.hpp"
#include "maxmount/cia402/cia402.hpp"
#include "maxmount/common/spsc_ring.hpp"
#include "maxmount/common/time.hpp"
#include "maxmount/fieldbus/fieldbus.hpp"
#include "maxmount/trajectory/trajectory.hpp"

namespace maxmount {

inline constexpr std::size_t kMaxAxes = 4;
inline constexpr std::uint8_t kAllAxes = 0xFF;

enum class CommandType : std::uint8_t {
    Enable,           // axis = index or kAllAxes: Operation Enabled, follow the trajectory
    Disable,          // axis = index or kAllAxes: decelerate to rest, then disable
    QuickStop,        // all axes: drive-side quick stop
    FaultReset,       // all axes: request a fault reset; also clears a following-error trip
    ClearTrajectory,  // axis = index or kAllAxes
    PushPoint,        // append point to axis
};

struct Command {
    CommandType type = CommandType::Enable;
    std::uint8_t axis = 0;
    TrajPoint point{};
};

namespace axis_flags {
inline constexpr std::uint8_t Limited = 1u << 0;
inline constexpr std::uint8_t Starved = 1u << 1;
inline constexpr std::uint8_t Holding = 1u << 2;
inline constexpr std::uint8_t BeforeStart = 1u << 3;
inline constexpr std::uint8_t FollowingErrorTrip = 1u << 4;  // this axis caused the trip
}  // namespace axis_flags

struct AxisTelemetry {
    double cmd_pos = 0.0;  // rad, commanded for t + command_lead
    double cmd_vel = 0.0;  // rad/s
    double act_pos = 0.0;  // rad, actual at t
    std::uint16_t statusword = 0;
    std::uint16_t error_code = 0;
    std::uint32_t vendor_error = 0;    // descriptor's error_register, read by SDO on a fault
    std::int32_t following_error = 0;  // drive counts, if mapped
    std::uint32_t digital_inputs = 0;  // raw 0x60FD, if mapped
    cia402::State state = cia402::State::Unknown;
    std::uint8_t flags = 0;
    std::uint32_t buffered = 0;
};

struct TelemetrySample {
    nanos t = 0;
    std::uint64_t cycle = 0;
    std::uint8_t n_axes = 0;
    bool enabled = false;       // any axis enabled
    bool tripped = false;       // following-error trip latched; needs FaultReset to re-enable
    bool inputs_fresh = false;  // fieldbus delivered new inputs this cycle
    std::uint64_t bus_lost_frames = 0;
    std::uint64_t bus_wkc_errors = 0;
    std::array<AxisTelemetry, kMaxAxes> axes{};
};

struct ControllerConfig {
    nanos command_lead_ns = 1'000'000;  // commanded position is for t + lead (bus/drive delay)
    std::uint32_t telemetry_decimation = 1;
};

class Controller {
public:
    Controller(Fieldbus& bus, std::vector<AxisConfig> axes, ControllerConfig cfg = {});

    // Non-RT side: exactly one producer thread for post(), one consumer for poll_telemetry().
    bool post(const Command& c) noexcept { return commands_.try_push(c); }
    std::optional<TelemetrySample> poll_telemetry() noexcept { return telemetry_.try_pop(); }

    // RT side: receive inputs, compute outputs, send.
    void cycle(nanos t_now, double dt) noexcept;

    std::size_t axis_count() const noexcept { return axes_.size(); }
    const AxisConfig& axis_config(std::size_t i) const { return axes_[i].cfg; }

private:
    struct AxisRuntime {
        AxisConfig cfg;
        cia402::Master master;
        TrajectoryBuffer traj;
        MotionLimiter limiter;
        PositionUnwrapper unwrap;
        bool enabled = false;
        bool stopping = false;
        bool tripped = false;  // this axis exceeded max_following_error
        double last_cmd = 0.0;
    };

    void apply(const Command& c) noexcept;
    static double to_axis(const AxisRuntime& ax, std::int64_t counts) noexcept;
    static std::int32_t to_counts(const AxisRuntime& ax, double rad) noexcept;
    static std::int32_t to_velocity_units(const AxisRuntime& ax, double rad_s) noexcept;

    Fieldbus& bus_;
    ControllerConfig cfg_;
    std::vector<AxisRuntime> axes_;
    bool quick_stop_ = false;
    bool trip_latched_ = false;
    std::uint64_t cycle_ = 0;
    SpscRing<Command, 4096> commands_;
    SpscRing<TelemetrySample, 4096> telemetry_;
};

}  // namespace maxmount
