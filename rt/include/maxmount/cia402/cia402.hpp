// SPDX-License-Identifier: GPL-3.0-or-later
// Vendor-neutral CiA 402 (IEC 61800-7-201) drive profile: statusword decoding, controlword
// commands and a master-side state machine that brings a drive to Operation Enabled.
#pragma once

#include <cstdint>

namespace maxmount::cia402 {

enum class State : std::uint8_t {
    NotReadyToSwitchOn,
    SwitchOnDisabled,
    ReadyToSwitchOn,
    SwitchedOn,
    OperationEnabled,
    QuickStopActive,
    FaultReactionActive,
    Fault,
    Unknown,
};

enum class Mode : std::int8_t {
    ProfilePosition = 1,
    ProfileVelocity = 3,
    Homing = 6,
    InterpolatedPosition = 7,
    CyclicSyncPosition = 8,
    CyclicSyncVelocity = 9,
    CyclicSyncTorque = 10,
};

// Statusword (0x6041) bits.
namespace sw {
inline constexpr std::uint16_t ReadyToSwitchOn = 1u << 0;
inline constexpr std::uint16_t SwitchedOn = 1u << 1;
inline constexpr std::uint16_t OperationEnabled = 1u << 2;
inline constexpr std::uint16_t Fault = 1u << 3;
inline constexpr std::uint16_t VoltageEnabled = 1u << 4;
inline constexpr std::uint16_t QuickStop = 1u << 5;  // 0 = quick stop active
inline constexpr std::uint16_t SwitchOnDisabled = 1u << 6;
inline constexpr std::uint16_t Warning = 1u << 7;
inline constexpr std::uint16_t Remote = 1u << 9;
inline constexpr std::uint16_t TargetReached = 1u << 10;
inline constexpr std::uint16_t InternalLimit = 1u << 11;
inline constexpr std::uint16_t FollowingError = 1u << 13;
}  // namespace sw

// Controlword (0x6040) command patterns.
namespace cw {
inline constexpr std::uint16_t DisableVoltage = 0x0000;
inline constexpr std::uint16_t QuickStop = 0x0002;
inline constexpr std::uint16_t Shutdown = 0x0006;
inline constexpr std::uint16_t SwitchOn = 0x0007;
inline constexpr std::uint16_t DisableOperation = 0x0007;
inline constexpr std::uint16_t EnableOperation = 0x000F;
inline constexpr std::uint16_t FaultReset = 0x0080;  // acts on the rising edge
}  // namespace cw

// Device-side interpretation of a controlword (excluding the fault-reset edge).
enum class Command : std::uint8_t {
    None,
    DisableVoltage,
    QuickStop,
    Shutdown,
    SwitchOn,  // also "disable operation" when in Operation Enabled
    EnableOperation,
};

State decode(std::uint16_t statusword) noexcept;
std::uint16_t encode(State state) noexcept;  // canonical statusword bits for a state
Command decode_command(std::uint16_t controlword) noexcept;
const char* to_string(State state) noexcept;

// Master-side state machine. Call update() once per cycle with the latest statusword; it
// returns the controlword to send. Faults are never reset automatically: call
// request_fault_reset() explicitly.
class Master {
public:
    std::uint16_t update(std::uint16_t statusword, bool want_enabled, bool quick_stop) noexcept;
    void request_fault_reset() noexcept { reset_requested_ = true; }
    State state() const noexcept { return state_; }

private:
    State state_ = State::Unknown;
    bool reset_requested_ = false;
};

}  // namespace maxmount::cia402
