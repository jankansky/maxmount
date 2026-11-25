// SPDX-License-Identifier: GPL-3.0-or-later
// Fieldbus abstraction. A backend (SOEM, simulation, ...) maps each drive's process data to
// the vendor-neutral CiA 402 cyclic objects below, using the drive descriptor's PDO map.
//
// Cycle order (driven by the Controller):
//   receive()  -> read the frame sent last cycle and update inputs()
//   ...compute outputs...
//   send()     -> write outputs() and transmit the frame
#pragma once

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string_view>

#include "maxmount/common/time.hpp"

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
    std::int32_t following_error = 0;   // 0x60F4
    std::uint32_t digital_inputs = 0;   // 0x60FD (limit and home switches in bits 0-2)
};

struct FieldbusStats {
    std::uint64_t cycles = 0;
    std::uint64_t lost_frames = 0;             // no frame returned
    std::uint64_t working_counter_errors = 0;  // frame returned with too low a working counter
    int last_working_counter = 0;
    int expected_working_counter = 0;
};

class Fieldbus {
public:
    virtual ~Fieldbus() = default;

    // Non-real-time: scan, configure PDOs/DC, and bring the bus to OP.
    virtual bool start() = 0;
    virtual void stop() = 0;

    virtual std::size_t drive_count() const = 0;

    // Real-time. receive() returns false if inputs could not be refreshed this cycle.
    virtual bool receive() = 0;
    virtual bool send() = 0;

    virtual const Cia402Inputs& inputs(std::size_t drive) const = 0;
    virtual Cia402Outputs& outputs(std::size_t drive) = 0;

    virtual FieldbusStats stats() const = 0;
    virtual std::string_view name() const = 0;

    // Adjustment to add to the next cycle's wake-up time so the host loop stays phase-locked
    // to the bus's distributed clock. Zero for backends without DC.
    virtual nanos cycle_correction_ns() const { return 0; }

    // Non-RT: reads a CoE object (1, 2 or 4 bytes, little-endian, zero-extended) through the
    // drive's mailbox, e.g. the error code 0x603F after a fault when it is not in the PDOs.
    // Safe to call from a non-RT thread while the RT thread cycles. nullopt on failure or if
    // the backend has no mailbox.
    virtual std::optional<std::uint32_t> read_sdo(std::size_t /*drive*/, std::uint16_t /*index*/,
                                                  std::uint8_t /*subindex*/, std::size_t /*bytes*/) {
        return std::nullopt;
    }
};

}  // namespace maxmount
