// SPDX-License-Identifier: GPL-3.0-or-later
// Drive descriptor: everything needed to talk to a CiA 402 drive over EtherCAT, as data.
// Loaded from config/drives/*.yaml. Independent of any EtherCAT master library.
#pragma once

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

#include "maxmount/fieldbus/fieldbus.hpp"

namespace maxmount {

enum class DataType : std::uint8_t { U8, I8, U16, I16, U32, I32 };

std::size_t size_of(DataType t) noexcept;

// Which Cia402Outputs / Cia402Inputs member a PDO entry carries. None = padding or an object
// the core does not use (still mapped and transferred).
enum class PdoField : std::uint8_t {
    None,
    // outputs (RxPDO)
    Controlword,
    Mode,
    TargetPosition,
    VelocityOffset,
    TorqueOffset,
    // inputs (TxPDO)
    Statusword,
    ModeDisplay,
    PositionActual,
    VelocityActual,
    TorqueActual,
    ErrorCode,
    FollowingError,
    DigitalInputs,
};

bool is_output_field(PdoField f) noexcept;
bool is_input_field(PdoField f) noexcept;

struct PdoEntry {
    std::uint16_t index = 0;
    std::uint8_t subindex = 0;
    DataType type = DataType::U8;
    PdoField field = PdoField::None;
};

struct SdoWrite {
    std::uint16_t index = 0;
    std::uint8_t subindex = 0;
    DataType type = DataType::U8;
    std::int64_t value = 0;
};

// A vendor register read by SDO when the drive enters Fault, for diagnostics (e.g. the
// Technosoft Motion Error Register). `decode` names a bit table the GUI knows.
struct ErrorRegister {
    std::uint16_t index = 0;
    std::uint8_t subindex = 0;
    std::uint8_t size = 2;  // bytes, 1..4
    std::string name;
    std::string decode;
};

struct DriveDescriptor {
    std::string vendor;
    std::string model;
    std::optional<std::uint32_t> vendor_id;     // checked against the slave's EEPROM if set
    std::optional<std::uint32_t> product_code;  // checked against the slave's EEPROM if set
    bool distributed_clocks = true;
    // If true, the PDO mapping below is written to the drive at PRE-OP -> SAFE-OP.
    // If false, rx_pdo / tx_pdo must describe the drive's existing (default) mapping.
    bool configure_pdo_mapping = true;
    std::uint16_t rx_mapping_index = 0x1600;  // first RxPDO mapping object
    std::uint16_t tx_mapping_index = 0x1A00;  // first TxPDO mapping object
    // Per-PDO limits (0 = none). A list longer than this is split, in order, across
    // consecutive mapping objects (0x1A00, 0x1A01, ...), all assigned to the sync manager.
    // Drives with CANopen heritage often limit each PDO to 64 bits.
    std::uint16_t max_pdo_bits = 0;
    std::uint8_t max_pdo_entries = 0;
    std::vector<PdoEntry> rx_pdo;  // master -> drive, in process-image order
    std::vector<PdoEntry> tx_pdo;  // drive -> master, in process-image order
    std::vector<SdoWrite> startup_sdo;
    std::optional<ErrorRegister> error_register;
};

// Returns an empty string if the descriptor is usable, otherwise a description of the problem.
std::string validate(const DriveDescriptor& d);

// Splits a PDO entry list into consecutive PDOs that respect max_pdo_bits / max_pdo_entries,
// keeping the order (and therefore the process-image layout). Entry order is preserved.
std::vector<std::vector<PdoEntry>> split_pdos(const std::vector<PdoEntry>& entries,
                                              std::uint16_t max_bits, std::uint8_t max_entries);

// The ordered SDO writes that program the PDO mapping objects and sync-manager assignment
// (0x1C12 / 0x1C13). Empty if configure_pdo_mapping is false.
std::vector<SdoWrite> pdo_mapping_sdos(const DriveDescriptor& d);

// Little-endian helpers for SDO payloads and process data.
void write_le(std::uint8_t* dst, std::uint64_t value, std::size_t bytes) noexcept;
std::int64_t read_le(const std::uint8_t* src, DataType type) noexcept;

// Encodes Cia402Outputs into, and decodes Cia402Inputs from, one drive's slice of the
// process image according to the descriptor's PDO layout.
class PdoCodec {
public:
    PdoCodec() = default;
    explicit PdoCodec(const DriveDescriptor& d);

    std::size_t output_size() const noexcept { return output_size_; }
    std::size_t input_size() const noexcept { return input_size_; }

    void encode(const Cia402Outputs& out, std::uint8_t* dst) const noexcept;
    void decode(const std::uint8_t* src, Cia402Inputs& in) const noexcept;

private:
    struct Slot {
        std::size_t offset;
        DataType type;
        PdoField field;
    };
    std::vector<Slot> rx_;
    std::vector<Slot> tx_;
    std::size_t output_size_ = 0;
    std::size_t input_size_ = 0;
};

}  // namespace maxmount
