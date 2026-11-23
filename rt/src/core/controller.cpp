// SPDX-License-Identifier: GPL-3.0-or-later
#include "maxmount/core/controller.hpp"

#include <algorithm>
#include <cmath>
#include <limits>
#include <stdexcept>

namespace maxmount {

Controller::Controller(Fieldbus& bus, std::vector<AxisConfig> axes, ControllerConfig cfg)
    : bus_(bus), cfg_(cfg) {
    if (axes.empty() || axes.size() > kMaxAxes) {
        throw std::invalid_argument("Controller: axis count must be 1..kMaxAxes");
    }
    if (axes.size() > bus.drive_count()) {
        throw std::invalid_argument("Controller: more axes than drives on the bus");
    }
    axes_.reserve(axes.size());
    for (auto& a : axes) {
        AxisRuntime rt;
        rt.limiter = MotionLimiter(a.limits);
        rt.cfg = std::move(a);
        axes_.push_back(std::move(rt));
    }
}

double Controller::to_axis(const AxisRuntime& ax, std::int64_t counts) noexcept {
    return static_cast<double>(counts) / ax.cfg.counts_per_rad + ax.cfg.offset_rad;
}

std::int32_t Controller::to_counts(const AxisRuntime& ax, double rad) noexcept {
    const auto counts = static_cast<std::int64_t>(std::llround((rad - ax.cfg.offset_rad) * ax.cfg.counts_per_rad));
    // The drive's 32-bit position wraps modulo 2^32, consistent with PositionUnwrapper.
    return static_cast<std::int32_t>(static_cast<std::uint32_t>(counts & 0xFFFFFFFF));
}

std::int32_t Controller::to_velocity_units(const AxisRuntime& ax, double rad_s) noexcept {
    if (ax.cfg.vel_units_per_rad_s == 0.0) return 0;
    const double v = rad_s * ax.cfg.vel_units_per_rad_s;
    constexpr double lo = std::numeric_limits<std::int32_t>::min();
    constexpr double hi = std::numeric_limits<std::int32_t>::max();
    return static_cast<std::int32_t>(std::llround(std::clamp(v, lo, hi)));
}

void Controller::apply(const Command& c) noexcept {
    switch (c.type) {
        case CommandType::Enable:
            enabled_ = true;
            stopping_ = false;
            quick_stop_ = false;
            break;
        case CommandType::Disable:
            if (enabled_) stopping_ = true;
            break;
        case CommandType::QuickStop:
            quick_stop_ = true;
            enabled_ = false;
            stopping_ = false;
            break;
        case CommandType::FaultReset:
            for (auto& ax : axes_) ax.master.request_fault_reset();
            break;
        case CommandType::ClearTrajectory:
            for (std::size_t i = 0; i < axes_.size(); ++i) {
                if (c.axis == kAllAxes || c.axis == i) axes_[i].traj.clear();
            }
            break;
        case CommandType::PushPoint:
            if (c.axis < axes_.size()) axes_[c.axis].traj.push(c.point);
            break;
    }
}

void Controller::cycle(nanos t_now, double dt) noexcept {
    const bool inputs_fresh = bus_.receive();
    for (int n = 0; n < 256; ++n) {
        const auto c = commands_.try_pop();
        if (!c) break;
        apply(*c);
    }

    TelemetrySample ts;
    ts.t = t_now;
    ts.cycle = cycle_;
    ts.n_axes = static_cast<std::uint8_t>(axes_.size());
    ts.enabled = enabled_;
    ts.inputs_fresh = inputs_fresh;
    const FieldbusStats bs = bus_.stats();
    ts.bus_lost_frames = bs.lost_frames;
    ts.bus_wkc_errors = bs.working_counter_errors;

    bool all_at_rest = true;
    for (std::size_t i = 0; i < axes_.size(); ++i) {
        AxisRuntime& ax = axes_[i];
        const Cia402Inputs& in = bus_.inputs(i);
        Cia402Outputs& out = bus_.outputs(i);

        const double act = to_axis(ax, ax.unwrap.update(in.position_actual));
        const std::uint16_t controlword = ax.master.update(in.statusword, enabled_, quick_stop_);

        LimiterOutput o{};
        std::uint8_t flags = 0;
        if (ax.master.state() != cia402::State::OperationEnabled) {
            // Not under position control: keep the command glued to the actual position so
            // enabling never causes a jump.
            ax.limiter.reset(act);
            o.pos = act;
            flags |= axis_flags::Holding;
        } else if (stopping_) {
            o = ax.limiter.stop(dt);
            flags |= axis_flags::Holding;
        } else {
            const BufferedSample s = ax.traj.sample(t_now + cfg_.command_lead_ns);
            switch (s.status) {
                case SampleStatus::Ok:
                    o = ax.limiter.track(s.s.pos, s.s.vel, dt);
                    break;
                case SampleStatus::Starved:
                    flags |= axis_flags::Starved;
                    o = ax.limiter.stop(dt);
                    break;
                case SampleStatus::BeforeStart:
                    flags |= axis_flags::BeforeStart;
                    o = ax.limiter.stop(dt);
                    break;
                case SampleStatus::Empty:
                    flags |= axis_flags::Holding;
                    o = ax.limiter.stop(dt);
                    break;
            }
        }
        if (o.limited) flags |= axis_flags::Limited;
        if (o.vel != 0.0) all_at_rest = false;

        out.controlword = controlword;
        out.mode = static_cast<std::int8_t>(cia402::Mode::CyclicSyncPosition);
        out.target_position = to_counts(ax, o.pos);
        out.velocity_offset = to_velocity_units(ax, o.vel);
        out.torque_offset = 0;

        AxisTelemetry& at = ts.axes[i];
        at.cmd_pos = o.pos;
        at.cmd_vel = o.vel;
        at.act_pos = act;
        at.statusword = in.statusword;
        at.error_code = in.error_code;
        at.state = ax.master.state();
        at.flags = flags;
        at.buffered = static_cast<std::uint32_t>(ax.traj.size());
    }

    if (stopping_ && all_at_rest) {
        stopping_ = false;
        enabled_ = false;
    }

    if (cfg_.telemetry_decimation <= 1 || cycle_ % cfg_.telemetry_decimation == 0) {
        telemetry_.try_push(ts);  // dropped if the consumer falls behind
    }

    bus_.send();
    ++cycle_;
}

}  // namespace maxmount
