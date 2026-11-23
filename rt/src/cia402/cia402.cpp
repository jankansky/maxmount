// SPDX-License-Identifier: GPL-3.0-or-later
#include "maxmount/cia402/cia402.hpp"

namespace maxmount::cia402 {

State decode(std::uint16_t s) noexcept {
    if ((s & 0x4F) == 0x00) return State::NotReadyToSwitchOn;
    if ((s & 0x4F) == 0x40) return State::SwitchOnDisabled;
    if ((s & 0x6F) == 0x21) return State::ReadyToSwitchOn;
    if ((s & 0x6F) == 0x23) return State::SwitchedOn;
    if ((s & 0x6F) == 0x27) return State::OperationEnabled;
    if ((s & 0x6F) == 0x07) return State::QuickStopActive;
    if ((s & 0x4F) == 0x0F) return State::FaultReactionActive;
    if ((s & 0x4F) == 0x08) return State::Fault;
    return State::Unknown;
}

std::uint16_t encode(State state) noexcept {
    switch (state) {
        case State::NotReadyToSwitchOn: return 0x0000;
        case State::SwitchOnDisabled: return 0x0040;
        case State::ReadyToSwitchOn: return 0x0031;
        case State::SwitchedOn: return 0x0033;
        case State::OperationEnabled: return 0x0037;
        case State::QuickStopActive: return 0x0017;
        case State::FaultReactionActive: return 0x001F;
        case State::Fault: return 0x0018;
        case State::Unknown: break;
    }
    return 0x0000;
}

Command decode_command(std::uint16_t c) noexcept {
    if ((c & 0x0002) == 0x0000) return Command::DisableVoltage;
    if ((c & 0x0006) == 0x0002) return Command::QuickStop;
    if ((c & 0x0087) == 0x0006) return Command::Shutdown;
    if ((c & 0x008F) == 0x0007) return Command::SwitchOn;
    if ((c & 0x008F) == 0x000F) return Command::EnableOperation;
    return Command::None;
}

const char* to_string(State state) noexcept {
    switch (state) {
        case State::NotReadyToSwitchOn: return "NotReadyToSwitchOn";
        case State::SwitchOnDisabled: return "SwitchOnDisabled";
        case State::ReadyToSwitchOn: return "ReadyToSwitchOn";
        case State::SwitchedOn: return "SwitchedOn";
        case State::OperationEnabled: return "OperationEnabled";
        case State::QuickStopActive: return "QuickStopActive";
        case State::FaultReactionActive: return "FaultReactionActive";
        case State::Fault: return "Fault";
        case State::Unknown: break;
    }
    return "Unknown";
}

std::uint16_t Master::update(std::uint16_t statusword, bool want_enabled, bool quick_stop) noexcept {
    state_ = decode(statusword);
    if (state_ != State::Fault) {
        reset_requested_ = false;  // a reset request only applies to the current fault
    }
    const bool enable = want_enabled && !quick_stop;

    switch (state_) {
        case State::Fault:
            if (reset_requested_) {
                // Previous cycle sent bit 7 low, so this produces the required rising edge.
                reset_requested_ = false;
                return cw::FaultReset;
            }
            return cw::DisableVoltage;
        case State::SwitchOnDisabled:
            return enable ? cw::Shutdown : cw::DisableVoltage;
        case State::ReadyToSwitchOn:
            if (quick_stop) return cw::QuickStop;
            return enable ? cw::SwitchOn : cw::DisableVoltage;
        case State::SwitchedOn:
            if (quick_stop) return cw::QuickStop;
            return enable ? cw::EnableOperation : cw::DisableVoltage;
        case State::OperationEnabled:
            if (quick_stop) return cw::QuickStop;
            return enable ? cw::EnableOperation : cw::DisableOperation;
        case State::QuickStopActive:
            return quick_stop ? cw::QuickStop : cw::DisableVoltage;
        case State::NotReadyToSwitchOn:
        case State::FaultReactionActive:
        case State::Unknown:
            break;
    }
    return cw::DisableVoltage;
}

}  // namespace maxmount::cia402
