// SPDX-License-Identifier: GPL-3.0-or-later
#include "maxmount/bridge/codec.hpp"

#include <cmath>
#include <utility>

#include "maxmount/v1/mount.pb.h"

namespace maxmount::bridge {

namespace pb = ::maxmount::v1;

namespace {

// Axis name -> controller index; "" -> kAllAxes when allow_all. -1 if unknown.
int axis_index(const std::string& name, const std::vector<std::string>& names, bool allow_all) {
    if (name.empty()) return allow_all ? kAllAxes : -1;
    for (std::size_t i = 0; i < names.size(); ++i) {
        if (names[i] == name) return static_cast<int>(i);
    }
    return -1;
}

pb::DriveState to_pb(cia402::State s) {
    using S = cia402::State;
    switch (s) {
        case S::NotReadyToSwitchOn: return pb::DRIVE_STATE_NOT_READY_TO_SWITCH_ON;
        case S::SwitchOnDisabled: return pb::DRIVE_STATE_SWITCH_ON_DISABLED;
        case S::ReadyToSwitchOn: return pb::DRIVE_STATE_READY_TO_SWITCH_ON;
        case S::SwitchedOn: return pb::DRIVE_STATE_SWITCHED_ON;
        case S::OperationEnabled: return pb::DRIVE_STATE_OPERATION_ENABLED;
        case S::QuickStopActive: return pb::DRIVE_STATE_QUICK_STOP_ACTIVE;
        case S::FaultReactionActive: return pb::DRIVE_STATE_FAULT_REACTION_ACTIVE;
        case S::Fault: return pb::DRIVE_STATE_FAULT;
        case S::Unknown: break;
    }
    return pb::DRIVE_STATE_UNKNOWN;
}

void fill(pb::MountTelemetry& m, const TelemetrySample& s, const std::vector<std::string>& names) {
    m.set_t_ns(s.t);
    m.set_cycle(s.cycle);
    m.set_enabled(s.enabled);
    m.set_tripped(s.tripped);
    m.set_inputs_fresh(s.inputs_fresh);
    m.mutable_bus()->set_lost_frames(s.bus_lost_frames);
    m.mutable_bus()->set_working_counter_errors(s.bus_wkc_errors);
    for (std::size_t i = 0; i < s.n_axes; ++i) {
        const AxisTelemetry& a = s.axes[i];
        pb::AxisTelemetry* p = m.add_axes();
        p->set_axis(i < names.size() ? names[i] : std::string());
        p->set_cmd_position_rad(a.cmd_pos);
        p->set_cmd_velocity_rad_s(a.cmd_vel);
        p->set_act_position_rad(a.act_pos);
        p->set_state(to_pb(a.state));
        p->set_statusword(a.statusword);
        p->set_error_code(a.error_code);
        p->set_vendor_error_register(a.vendor_error);
        p->set_flags(a.flags);
        p->set_buffered_points(a.buffered);
        p->set_following_error_counts(a.following_error);
        p->set_digital_inputs(a.digital_inputs);
    }
}

}  // namespace

Decoded decode_trajectory(std::string_view bytes, const std::vector<std::string>& axis_names) {
    Decoded d;
    pb::TrajectoryUpdate u;
    if (!u.ParseFromArray(bytes.data(), static_cast<int>(bytes.size()))) {
        d.error = "TrajectoryUpdate: cannot parse";
        return d;
    }
    for (const pb::AxisTrajectory& at : u.axes()) {
        const int idx = axis_index(at.axis(), axis_names, false);
        if (idx < 0) {
            d.error = "TrajectoryUpdate: unknown axis '" + at.axis() + "'";
            d.commands.clear();
            return d;
        }
        std::int64_t prev_t = 0;
        bool first = true;
        for (const pb::TrajectoryPoint& p : at.points()) {
            if (!std::isfinite(p.position_rad()) || !std::isfinite(p.velocity_rad_s())) {
                d.error = "TrajectoryUpdate: non-finite value for axis '" + at.axis() + "'";
            } else if (!first && p.t_ns() <= prev_t) {
                d.error = "TrajectoryUpdate: times must strictly increase for axis '" + at.axis() + "'";
            }
            if (!d.ok()) {
                d.commands.clear();
                return d;
            }
            Command c;
            c.type = CommandType::PushPoint;
            c.axis = static_cast<std::uint8_t>(idx);
            c.point = {p.t_ns(), p.position_rad(), p.velocity_rad_s()};
            d.commands.push_back(c);
            prev_t = p.t_ns();
            first = false;
        }
    }
    return d;
}

Decoded decode_command(std::string_view bytes, const std::vector<std::string>& axis_names) {
    Decoded d;
    pb::MountCommand m;
    if (!m.ParseFromArray(bytes.data(), static_cast<int>(bytes.size()))) {
        d.error = "MountCommand: cannot parse";
        return d;
    }
    Command c;
    std::string axis;
    switch (m.command_case()) {
        case pb::MountCommand::kEnable:
            c.type = CommandType::Enable;
            axis = m.enable().axis();
            break;
        case pb::MountCommand::kDisable:
            c.type = CommandType::Disable;
            axis = m.disable().axis();
            break;
        case pb::MountCommand::kQuickStop:
            c.type = CommandType::QuickStop;
            break;
        case pb::MountCommand::kFaultReset:
            c.type = CommandType::FaultReset;
            break;
        case pb::MountCommand::kClearTrajectory:
            c.type = CommandType::ClearTrajectory;
            axis = m.clear_trajectory().axis();
            break;
        case pb::MountCommand::COMMAND_NOT_SET:
            d.error = "MountCommand: no command set";
            return d;
    }
    const int idx = axis_index(axis, axis_names, true);
    if (idx < 0) {
        d.error = "MountCommand: unknown axis '" + axis + "'";
        return d;
    }
    c.axis = static_cast<std::uint8_t>(idx);
    d.commands.push_back(c);
    return d;
}

std::string encode_reply(bool accepted, const std::string& message) {
    pb::CommandReply r;
    r.set_accepted(accepted);
    r.set_message(message);
    return r.SerializeAsString();
}

std::string encode_telemetry(const TelemetrySample& s, const std::vector<std::string>& axis_names,
                             const LoopSnapshot* loop) {
    pb::MountTelemetry m;
    fill(m, s, axis_names);
    if (loop) {
        m.mutable_loop()->set_cycles(loop->cycles);
        m.mutable_loop()->set_overruns(loop->overruns);
        m.mutable_loop()->set_max_wake_latency_ns(loop->max_wake_latency_ns);
    }
    return m.SerializeAsString();
}

struct BatchEncoder::Impl {
    std::vector<std::string> names;
    pb::MountTelemetryBatch batch;
};

BatchEncoder::BatchEncoder(std::vector<std::string> axis_names) : impl_(std::make_unique<Impl>()) {
    impl_->names = std::move(axis_names);
}
BatchEncoder::~BatchEncoder() = default;

void BatchEncoder::add(const TelemetrySample& s) { fill(*impl_->batch.add_samples(), s, impl_->names); }
std::size_t BatchEncoder::size() const noexcept { return static_cast<std::size_t>(impl_->batch.samples_size()); }

std::string BatchEncoder::take() {
    std::string out = impl_->batch.SerializeAsString();
    impl_->batch.Clear();
    return out;
}

}  // namespace maxmount::bridge
