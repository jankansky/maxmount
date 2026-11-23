// SPDX-License-Identifier: GPL-3.0-or-later
// Fieldbus abstraction. A backend (SOEM, simulation, ...) maps each drive's process data to
// the vendor-neutral CiA 402 cyclic objects below, using the drive descriptor's PDO map.
#pragma once

#include <cstddef>
#include <cstdint>
#include <string_view>

namespace maxmount {

// Master -> drive (RxPDO), CSP subset.
struct Cia402Outputs {
    std::uint16_t controlword = 0;      // 0x6040
    std::int8_t mode = 8;               // 0x6060 (8 = CSP)
    std::int32_t target_position = 0;   // 0x607A, drive position units
    std::int32_t velocity_offset = 0;   // 0x60B1, drive velocity units (feedforward)
    std::int16_t torque_offset = 0;     // 0x60B2, per mille of rated torque (feedforward)
};

// Drive -> master (TxPDO).
struct Cia402Inputs {
    std::uint16_t statusword = 0;       // 0x6041
    std::int8_t mode_display = 0;       // 0x6061
    std::int32_t position_actual = 0;   // 0x6064
    std::int32_t velocity_actual = 0;   // 0x606C
    std::int16_t torque_actual = 0;     // 0x6077
    std::uint16_t error_code = 0;       // 0x603F
};

struct FieldbusStats {
    std::uint64_t cycles = 0;
    std::uint64_t lost_frames = 0;
    std::uint64_t working_counter_errors = 0;
};

class Fieldbus {
public:
    virtual ~Fieldbus() = default;

    // Non-real-time: scan, configure PDOs/DC, and bring the bus to OP.
    virtual bool start() = 0;
    virtual void stop() = 0;

    virtual std::size_t drive_count() const = 0;

    // Real-time: send outputs and receive inputs for one cycle.
    virtual bool exchange() = 0;

    virtual const Cia402Inputs& inputs(std::size_t drive) const = 0;
    virtual Cia402Outputs& outputs(std::size_t drive) = 0;

    virtual FieldbusStats stats() const = 0;
    virtual std::string_view name() const = 0;
};

}  // namespace maxmount
