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
            if (trip_latched_) break;  // a following-error trip must be acknowledged first
            quick_stop_ = false;
            for (std::size_t i = 0; i < axes_.size(); ++i) {
                if (c.axis == kAllAxes || c.axis == i) {
                    axes_[i].enabled = true;
                    axes_[i].stopping = false;
                }
            }
            break;
        case CommandType::Disable:
            for (std::size_t i = 0; i < axes_.size(); ++i) {
                if ((c.axis == kAllAxes || c.axis == i) && axes_[i].enabled) axes_[i].stopping = true;
            }
            break;
        case CommandType::QuickStop:
            quick_stop_ = true;
            for (auto& ax : axes_) {
                ax.enabled = false;
                ax.stopping = false;
            }
            break;
        case CommandType::FaultReset:
            for (auto& ax : axes_) {
                ax.master.request_fault_reset();
                ax.tripped = false;
            }
            trip_latched_ = false;
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
    ts.inputs_fresh = inputs_fresh;
    const FieldbusStats bs = bus_.stats();
    ts.bus_lost_frames = bs.lost_frames;
    ts.bus_wkc_errors = bs.working_counter_errors;

    for (std::size_t i = 0; i < axes_.size(); ++i) {
        AxisRuntime& ax = axes_[i];
        const Cia402Inputs& in = bus_.inputs(i);
        Cia402Outputs& out = bus_.outputs(i);

        const double act = to_axis(ax, ax.unwrap.update(in.position_actual));

        // Following-error trip. last_cmd was for (t - dt + lead), so the measured error also
        // contains velocity x (lead + transport delay), about v x 3 ms: keep the limit well
        // above that at the highest speed. The drive's own following-error protection is the
        // primary guard; this one catches a mis-scaled or mis-signed axis.
        if (ax.enabled && ax.master.state() == cia402::State::OperationEnabled &&
            std::abs(act - ax.last_cmd) > ax.cfg.limits.max_following_error) {
            ax.tripped = true;
            trip_latched_ = true;
            quick_stop_ = true;
            for (auto& other : axes_) {
                other.enabled = false;
                other.stopping = false;
            }
        }

        const std::uint16_t controlword = ax.master.update(in.statusword, ax.enabled, quick_stop_);

        LimiterOutput o{};
        std::uint8_t flags = ax.tripped ? axis_flags::FollowingErrorTrip : 0;
        if (ax.master.state() != cia402::State::OperationEnabled || !ax.enabled) {
            // Not under position control: keep the command glued to the actual position so
            // enabling never causes a jump.
            ax.limiter.reset(act);
            o.pos = act;
            flags |= axis_flags::Holding;
        } else if (ax.stopping) {
            o = ax.limiter.stop(dt);
            flags |= axis_flags::Holding;
            if (o.vel == 0.0) {  // at rest: disable operation from the next cycle
                ax.stopping = false;
                ax.enabled = false;
            }
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
        ax.last_cmd = o.pos;
        if (ax.enabled) ts.enabled = true;

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
        at.following_error = in.following_error;
        at.digital_inputs = in.digital_inputs;
        at.state = ax.master.state();
        at.flags = flags;
        at.buffered = static_cast<std::uint32_t>(ax.traj.size());
    }

    ts.tripped = trip_latched_;

    if (cfg_.telemetry_decimation <= 1 || cycle_ % cfg_.telemetry_decimation == 0) {
        telemetry_.try_push(ts);  // dropped if the consumer falls behind
    }

    bus_.send();
    ++cycle_;
}

}  // namespace maxmount
