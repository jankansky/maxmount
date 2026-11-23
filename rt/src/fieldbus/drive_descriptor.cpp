// SPDX-License-Identifier: GPL-3.0-or-later
#include "maxmount/fieldbus/drive_descriptor.hpp"

#include <cstdio>
#include <set>

namespace maxmount {

std::size_t size_of(DataType t) noexcept {
    switch (t) {
        case DataType::U8:
        case DataType::I8: return 1;
        case DataType::U16:
        case DataType::I16: return 2;
        case DataType::U32:
        case DataType::I32: return 4;
    }
    return 0;
}

bool is_output_field(PdoField f) noexcept {
    switch (f) {
        case PdoField::Controlword:
        case PdoField::Mode:
        case PdoField::TargetPosition:
        case PdoField::VelocityOffset:
        case PdoField::TorqueOffset: return true;
        default: return false;
    }
}

bool is_input_field(PdoField f) noexcept {
    switch (f) {
        case PdoField::Statusword:
        case PdoField::ModeDisplay:
        case PdoField::PositionActual:
        case PdoField::VelocityActual:
        case PdoField::TorqueActual:
        case PdoField::ErrorCode: return true;
        default: return false;
    }
}

namespace {

std::string check_list(const std::vector<PdoEntry>& entries, bool outputs,
                       std::initializer_list<PdoField> required, const char* what) {
    std::set<PdoField> seen;
    for (const auto& e : entries) {
        if (e.field == PdoField::None) continue;
        if (outputs ? !is_output_field(e.field) : !is_input_field(e.field)) {
            char idx[16];
            std::snprintf(idx, sizeof(idx), "0x%04X", e.index);
            return std::string(what) + ": entry " + idx + " maps a field of the wrong direction";
        }
        if (!seen.insert(e.field).second) {
            return std::string(what) + ": a field is mapped more than once";
        }
    }
    for (PdoField f : required) {
        if (!seen.count(f)) {
            return std::string(what) + ": a required field (controlword/target_position or "
                                       "statusword/position_actual) is not mapped";
        }
    }
    return {};
}

}  // namespace

std::string validate(const DriveDescriptor& d) {
    const bool uses_pdos = d.configure_pdo_mapping || !d.rx_pdo.empty() || !d.tx_pdo.empty();
    if (!uses_pdos) return {};
    if (auto e = check_list(d.rx_pdo, true, {PdoField::Controlword, PdoField::TargetPosition}, "rx_pdo");
        !e.empty()) {
        return e;
    }
    if (auto e = check_list(d.tx_pdo, false, {PdoField::Statusword, PdoField::PositionActual}, "tx_pdo");
        !e.empty()) {
        return e;
    }
    for (const auto& w : d.startup_sdo) {
        if (size_of(w.type) == 0) return "startup_sdo: invalid data type";
    }
    return {};
}

std::vector<SdoWrite> pdo_mapping_sdos(const DriveDescriptor& d) {
    std::vector<SdoWrite> out;
    if (!d.configure_pdo_mapping) return out;

    auto program = [&out](std::uint16_t assign, std::uint16_t map, const std::vector<PdoEntry>& entries) {
        out.push_back({assign, 0, DataType::U8, 0});  // disable SM assignment
        out.push_back({map, 0, DataType::U8, 0});     // disable mapping object
        for (std::size_t i = 0; i < entries.size(); ++i) {
            const auto& e = entries[i];
            const std::uint32_t value = (static_cast<std::uint32_t>(e.index) << 16) |
                                        (static_cast<std::uint32_t>(e.subindex) << 8) |
                                        static_cast<std::uint32_t>(size_of(e.type) * 8);
            out.push_back({map, static_cast<std::uint8_t>(i + 1), DataType::U32, value});
        }
        out.push_back({map, 0, DataType::U8, static_cast<std::int64_t>(entries.size())});
        out.push_back({assign, 1, DataType::U16, map});
        out.push_back({assign, 0, DataType::U8, 1});
    };
    program(0x1C12, d.rx_mapping_index, d.rx_pdo);
    program(0x1C13, d.tx_mapping_index, d.tx_pdo);
    return out;
}

void write_le(std::uint8_t* dst, std::uint64_t value, std::size_t bytes) noexcept {
    for (std::size_t i = 0; i < bytes; ++i) {
        dst[i] = static_cast<std::uint8_t>((value >> (8 * i)) & 0xFF);
    }
}

std::int64_t read_le(const std::uint8_t* src, DataType type) noexcept {
    std::uint64_t v = 0;
    const std::size_t n = size_of(type);
    for (std::size_t i = 0; i < n; ++i) {
        v |= static_cast<std::uint64_t>(src[i]) << (8 * i);
    }
    switch (type) {
        case DataType::U8: return static_cast<std::uint8_t>(v);
        case DataType::I8: return static_cast<std::int8_t>(v);
        case DataType::U16: return static_cast<std::uint16_t>(v);
        case DataType::I16: return static_cast<std::int16_t>(v);
        case DataType::U32: return static_cast<std::uint32_t>(v);
        case DataType::I32: return static_cast<std::int32_t>(v);
    }
    return 0;
}

PdoCodec::PdoCodec(const DriveDescriptor& d) {
    for (const auto& e : d.rx_pdo) {
        rx_.push_back({output_size_, e.type, e.field});
        output_size_ += size_of(e.type);
    }
    for (const auto& e : d.tx_pdo) {
        tx_.push_back({input_size_, e.type, e.field});
        input_size_ += size_of(e.type);
    }
}

void PdoCodec::encode(const Cia402Outputs& out, std::uint8_t* dst) const noexcept {
    for (const Slot& s : rx_) {
        std::int64_t v = 0;
        switch (s.field) {
            case PdoField::Controlword: v = out.controlword; break;
            case PdoField::Mode: v = out.mode; break;
            case PdoField::TargetPosition: v = out.target_position; break;
            case PdoField::VelocityOffset: v = out.velocity_offset; break;
            case PdoField::TorqueOffset: v = out.torque_offset; break;
            default: v = 0; break;  // unused or padding entries are sent as zero
        }
        write_le(dst + s.offset, static_cast<std::uint64_t>(v), size_of(s.type));
    }
}

void PdoCodec::decode(const std::uint8_t* src, Cia402Inputs& in) const noexcept {
    for (const Slot& s : tx_) {
        const std::int64_t v = read_le(src + s.offset, s.type);
        switch (s.field) {
            case PdoField::Statusword: in.statusword = static_cast<std::uint16_t>(v); break;
            case PdoField::ModeDisplay: in.mode_display = static_cast<std::int8_t>(v); break;
            case PdoField::PositionActual: in.position_actual = static_cast<std::int32_t>(v); break;
            case PdoField::VelocityActual: in.velocity_actual = static_cast<std::int32_t>(v); break;
            case PdoField::TorqueActual: in.torque_actual = static_cast<std::int16_t>(v); break;
            case PdoField::ErrorCode: in.error_code = static_cast<std::uint16_t>(v); break;
            default: break;
        }
    }
}

}  // namespace maxmount
