// SPDX-License-Identifier: GPL-3.0-or-later
#include <gtest/gtest.h>

#include "maxmount/cia402/cia402.hpp"
#include "maxmount/sim/sim_fieldbus.hpp"

using namespace maxmount;
using cia402::State;

TEST(Cia402, EncodeDecodeRoundTrip) {
    for (State s : {State::NotReadyToSwitchOn, State::SwitchOnDisabled, State::ReadyToSwitchOn,
                    State::SwitchedOn, State::OperationEnabled, State::QuickStopActive,
                    State::FaultReactionActive, State::Fault}) {
        EXPECT_EQ(cia402::decode(cia402::encode(s)), s) << cia402::to_string(s);
        // Unrelated bits (remote, target reached, warning) must not change the state.
        EXPECT_EQ(cia402::decode(cia402::encode(s) | 0x0680), s) << cia402::to_string(s);
    }
}

namespace {

// Runs master + simulated drive for n cycles, holding the target at the actual position.
State run(cia402::Master& m, sim::SimCia402Drive& d, int n, bool enable, bool quick_stop = false) {
    Cia402Outputs out;
    for (int i = 0; i < n; ++i) {
        out.controlword = m.update(d.inputs().statusword, enable, quick_stop);
        out.target_position = d.inputs().position_actual;
        d.step(out, 0.001);
    }
    return cia402::decode(d.inputs().statusword);
}

}  // namespace

TEST(Cia402, MasterEnablesDrive) {
    cia402::Master m;
    sim::SimCia402Drive d;
    EXPECT_EQ(run(m, d, 20, true), State::OperationEnabled);
    EXPECT_EQ(run(m, d, 10, false), State::SwitchOnDisabled);
}

TEST(Cia402, FaultRequiresExplicitReset) {
    cia402::Master m;
    sim::SimCia402Drive d;
    ASSERT_EQ(run(m, d, 20, true), State::OperationEnabled);
    d.inject_fault(0x2310);
    EXPECT_EQ(run(m, d, 50, true), State::Fault);
    EXPECT_EQ(d.inputs().error_code, 0x2310);
    m.request_fault_reset();
    EXPECT_EQ(run(m, d, 20, true), State::OperationEnabled);
    EXPECT_EQ(d.inputs().error_code, 0);
}

TEST(Cia402, QuickStop) {
    cia402::Master m;
    sim::SimCia402Drive d;
    ASSERT_EQ(run(m, d, 20, true), State::OperationEnabled);
    EXPECT_EQ(run(m, d, 5, true, true), State::QuickStopActive);
    // Releasing quick stop goes through Switch On Disabled and re-enables.
    EXPECT_EQ(run(m, d, 20, true, false), State::OperationEnabled);
}
