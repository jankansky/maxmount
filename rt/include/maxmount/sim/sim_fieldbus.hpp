// SPDX-License-Identifier: GPL-3.0-or-later
// Simulated CiA 402 drives and fieldbus, so the full stack runs without hardware.
#pragma once

#include <cstdint>
#include <vector>

#include "maxmount/cia402/cia402.hpp"
#include "maxmount/fieldbus/fieldbus.hpp"

namespace maxmount::sim {

struct SimDriveParams {
    double kp_per_s = 400.0;               // position loop gain [1/s]
    double velocity_tau_s = 0.002;         // velocity loop time constant [s]
    int servo_substeps = 10;               // internal servo steps per bus cycle
    double quick_stop_decel = 2.0e8;       // [counts/s^2]
    double following_error_limit = 2.0e5;  // [counts]
    int boot_cycles = 5;                   // cycles spent in Not Ready To Switch On
    int fault_reaction_cycles = 10;        // minimum cycles in Fault Reaction Active
};

// A CSP-capable servo drive with a simple position/velocity loop model.
class SimCia402Drive {
public:
    explicit SimCia402Drive(SimDriveParams params = {}, double initial_position = 0.0);

    void step(const Cia402Outputs& out, double dt_s) noexcept;
    void inject_fault(std::uint16_t error_code) noexcept;

    const Cia402Inputs& inputs() const noexcept { return in_; }
    cia402::State state() const noexcept { return state_; }
    double position() const noexcept { return pos_; }
    double velocity() const noexcept { return vel_; }

private:
    void enter(cia402::State s) noexcept;
    void decelerate(double dt_s) noexcept;
    void publish(std::int8_t mode) noexcept;

    SimDriveParams p_;
    cia402::State state_ = cia402::State::NotReadyToSwitchOn;
    int state_cycles_ = 0;
    double pos_ = 0.0;
    double vel_ = 0.0;
    double prev_target_ = 0.0;
    std::uint16_t prev_controlword_ = 0;
    std::uint16_t error_code_ = 0;
    bool following_error_ = false;
    Cia402Inputs in_{};
};

class SimFieldbus final : public Fieldbus {
public:
    SimFieldbus(std::size_t drive_count, double cycle_s, SimDriveParams params = {});

    bool start() override;
    void stop() override;
    std::size_t drive_count() const override { return drives_.size(); }
    bool receive() override { return running_; }
    bool send() override;
    const Cia402Inputs& inputs(std::size_t drive) const override { return drives_[drive].inputs(); }
    Cia402Outputs& outputs(std::size_t drive) override { return outputs_[drive]; }
    FieldbusStats stats() const override { return stats_; }
    std::string_view name() const override { return "sim"; }

    SimCia402Drive& drive(std::size_t i) { return drives_[i]; }

private:
    double cycle_s_;
    bool running_ = false;
    std::vector<SimCia402Drive> drives_;
    std::vector<Cia402Outputs> outputs_;
    FieldbusStats stats_{};
};

}  // namespace maxmount::sim
