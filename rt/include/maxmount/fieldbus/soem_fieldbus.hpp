// SPDX-License-Identifier: GPL-3.0-or-later
// EtherCAT backend on SOEM 2.x (Linux). SOEM types stay in the .cpp (pimpl) so this header
// can be included anywhere.
#pragma once

#include <memory>
#include <string>
#include <vector>

#include "maxmount/fieldbus/drive_descriptor.hpp"
#include "maxmount/fieldbus/fieldbus.hpp"

namespace maxmount {

struct SoemDriveConfig {
    int bus_position = 0;  // 0-based position on the EtherCAT segment
    DriveDescriptor descriptor;
};

struct SoemConfig {
    std::string interface = "eth0";
    nanos cycle = 1'000'000;
    nanos sync0_shift = 0;               // SYNC0 shift relative to the DC cycle start
    nanos master_sync_offset = 500'000;  // where the host sends frames, relative to SYNC0
    int receive_timeout_us = 500;
    std::vector<SoemDriveConfig> drives;
};

class SoemFieldbus final : public Fieldbus {
public:
    explicit SoemFieldbus(SoemConfig cfg);
    ~SoemFieldbus() override;
    SoemFieldbus(const SoemFieldbus&) = delete;
    SoemFieldbus& operator=(const SoemFieldbus&) = delete;

    bool start() override;
    void stop() override;
    std::size_t drive_count() const override;
    bool receive() override;
    bool send() override;
    const Cia402Inputs& inputs(std::size_t drive) const override;
    Cia402Outputs& outputs(std::size_t drive) override;
    FieldbusStats stats() const override;
    std::string_view name() const override { return "soem"; }
    nanos cycle_correction_ns() const override;
    std::optional<std::uint32_t> read_sdo(std::size_t drive, std::uint16_t index, std::uint8_t subindex,
                                          std::size_t bytes) override;

    // Human-readable description of the last start() failure.
    const std::string& last_error() const;

    struct Impl;

private:
    std::unique_ptr<Impl> impl_;
};

}  // namespace maxmount
