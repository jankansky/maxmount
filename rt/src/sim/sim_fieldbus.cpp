// SPDX-License-Identifier: GPL-3.0-or-later
#include "maxmount/sim/sim_fieldbus.hpp"

#include <algorithm>
#include <cmath>

namespace maxmount::sim {

using cia402::Command;
using cia402::State;

SimCia402Drive::SimCia402Drive(SimDriveParams params, double initial_position)
    : p_(params), pos_(initial_position) {
    publish(static_cast<std::int8_t>(cia402::Mode::CyclicSyncPosition));
}

void SimCia402Drive::enter(State s) noexcept {
    state_ = s;
    state_cycles_ = 0;
}

void SimCia402Drive::inject_fault(std::uint16_t error_code) noexcept {
    error_code_ = error_code;
    enter(State::FaultReactionActive);
}

void SimCia402Drive::decelerate(double dt_s) noexcept {
    const double dv = p_.quick_stop_decel * dt_s;
    vel_ = std::abs(vel_) <= dv ? 0.0 : vel_ - std::copysign(dv, vel_);
    pos_ += vel_ * dt_s;
}

void SimCia402Drive::step(const Cia402Outputs& out, double dt_s) noexcept {
    const bool reset_edge = (out.controlword & cia402::cw::FaultReset) != 0 &&
                            (prev_controlword_ & cia402::cw::FaultReset) == 0;
    prev_controlword_ = out.controlword;
    const Command c = cia402::decode_command(out.controlword);
    ++state_cycles_;

    switch (state_) {
        case State::NotReadyToSwitchOn:
            if (state_cycles_ >= p_.boot_cycles) enter(State::SwitchOnDisabled);
            break;
        case State::SwitchOnDisabled:
            if (c == Command::Shutdown) enter(State::ReadyToSwitchOn);
            break;
        case State::ReadyToSwitchOn:
            if (c == Command::DisableVoltage || c == Command::QuickStop) enter(State::SwitchOnDisabled);
            else if (c == Command::SwitchOn || c == Command::EnableOperation) enter(State::SwitchedOn);
            break;
        case State::SwitchedOn:
            if (c == Command::DisableVoltage || c == Command::QuickStop) enter(State::SwitchOnDisabled);
            else if (c == Command::Shutdown) enter(State::ReadyToSwitchOn);
            else if (c == Command::EnableOperation) enter(State::OperationEnabled);
            break;
        case State::OperationEnabled:
            if (c == Command::DisableVoltage) enter(State::SwitchOnDisabled);
            else if (c == Command::QuickStop) enter(State::QuickStopActive);
            else if (c == Command::Shutdown) enter(State::ReadyToSwitchOn);
            else if (c == Command::SwitchOn) enter(State::SwitchedOn);
            break;
        case State::QuickStopActive:
            if (c == Command::DisableVoltage) enter(State::SwitchOnDisabled);
            break;
        case State::FaultReactionActive:
            if (state_cycles_ >= p_.fault_reaction_cycles && vel_ == 0.0) enter(State::Fault);
            break;
        case State::Fault:
            if (reset_edge) {
                error_code_ = 0;
                following_error_ = false;
                enter(State::SwitchOnDisabled);
            }
            break;
        case State::Unknown:
            break;
    }

    const bool csp = out.mode == static_cast<std::int8_t>(cia402::Mode::CyclicSyncPosition);
    if (state_ == State::OperationEnabled && csp) {
        // Like a real CSP drive, interpolate from the previous target to the new one over the
        // cycle and run the servo loops at a faster internal rate.
        const double target = static_cast<double>(out.target_position);
        const double ff = static_cast<double>(out.velocity_offset);
        const int n = std::max(1, p_.servo_substeps);
        const double h = dt_s / n;
        const double alpha = std::min(1.0, h / p_.velocity_tau_s);
        for (int k = 1; k <= n; ++k) {
            const double setpoint = prev_target_ + (target - prev_target_) * k / n;
            const double v_cmd = ff + p_.kp_per_s * (setpoint - pos_);
            vel_ += (v_cmd - vel_) * alpha;
            pos_ += vel_ * h;
        }
        prev_target_ = target;
        if (std::abs(target - pos_) > p_.following_error_limit) {
            following_error_ = true;
            error_code_ = 0x8611;  // CiA 402: following error
            enter(State::FaultReactionActive);
        }
    } else if (state_ == State::QuickStopActive || state_ == State::FaultReactionActive) {
        decelerate(dt_s);
    } else {
        vel_ = 0.0;  // power stage off or unsupported mode: brake holds the axis
    }
    if (state_ != State::OperationEnabled) {
        prev_target_ = pos_;
    }

    publish(out.mode);
}

void SimCia402Drive::publish(std::int8_t mode) noexcept {
    in_.statusword = static_cast<std::uint16_t>(cia402::encode(state_) | cia402::sw::Remote |
                                                (following_error_ ? cia402::sw::FollowingError : 0));
    in_.mode_display = mode;
    in_.position_actual = static_cast<std::int32_t>(std::llround(pos_));
    in_.velocity_actual = static_cast<std::int32_t>(std::llround(vel_));
    in_.torque_actual = 0;
    in_.error_code = error_code_;
}

SimFieldbus::SimFieldbus(std::size_t drive_count, double cycle_s, SimDriveParams params)
    : cycle_s_(cycle_s), drives_(drive_count, SimCia402Drive(params)), outputs_(drive_count) {}

bool SimFieldbus::start() {
    running_ = true;
    return true;
}

void SimFieldbus::stop() { running_ = false; }

bool SimFieldbus::exchange() {
    if (!running_) return false;
    for (std::size_t i = 0; i < drives_.size(); ++i) {
        drives_[i].step(outputs_[i], cycle_s_);
    }
    ++stats_.cycles;
    return true;
}

}  // namespace maxmount::sim
