// SPDX-License-Identifier: GPL-3.0-or-later
#include "maxmount/config/drive_descriptor_yaml.hpp"

#include <yaml-cpp/yaml.h>

#include <fstream>
#include <limits>
#include <sstream>
#include <stdexcept>

namespace maxmount::config {

namespace {

[[noreturn]] void fail(const std::string& msg) { throw std::runtime_error("drive descriptor: " + msg); }

// Integers are parsed from the scalar text with base auto-detection so 0x6040 works.
std::int64_t to_int(const YAML::Node& n, const char* what) {
    if (!n || !n.IsScalar()) fail(std::string(what) + " must be an integer");
    const std::string s = n.Scalar();
    try {
        std::size_t used = 0;
        const long long v = std::stoll(s, &used, 0);
        if (used != s.size()) throw std::invalid_argument(s);
        return v;
    } catch (const std::exception&) {
        fail(std::string(what) + ": '" + s + "' is not an integer");
    }
}

template <typename T>
T to_uint(const YAML::Node& n, const char* what) {
    const std::int64_t v = to_int(n, what);
    if (v < 0 || static_cast<std::uint64_t>(v) > std::numeric_limits<T>::max()) {
        fail(std::string(what) + " is out of range");
    }
    return static_cast<T>(v);
}

DataType to_type(const YAML::Node& n) {
    const std::string s = n ? n.as<std::string>() : "";
    if (s == "u8") return DataType::U8;
    if (s == "i8") return DataType::I8;
    if (s == "u16") return DataType::U16;
    if (s == "i16") return DataType::I16;
    if (s == "u32") return DataType::U32;
    if (s == "i32") return DataType::I32;
    fail("unknown data type '" + s + "' (expected u8, i8, u16, i16, u32 or i32)");
}

PdoField to_field(const YAML::Node& n) {
    const std::string s = n ? n.as<std::string>() : "none";
    if (s == "none") return PdoField::None;
    if (s == "controlword") return PdoField::Controlword;
    if (s == "mode") return PdoField::Mode;
    if (s == "target_position") return PdoField::TargetPosition;
    if (s == "velocity_offset") return PdoField::VelocityOffset;
    if (s == "torque_offset") return PdoField::TorqueOffset;
    if (s == "statusword") return PdoField::Statusword;
    if (s == "mode_display") return PdoField::ModeDisplay;
    if (s == "position_actual") return PdoField::PositionActual;
    if (s == "velocity_actual") return PdoField::VelocityActual;
    if (s == "torque_actual") return PdoField::TorqueActual;
    if (s == "error_code") return PdoField::ErrorCode;
    if (s == "following_error") return PdoField::FollowingError;
    if (s == "digital_inputs") return PdoField::DigitalInputs;
    fail("unknown PDO field '" + s + "'");
}

std::vector<PdoEntry> to_entries(const YAML::Node& list, const char* what) {
    std::vector<PdoEntry> out;
    if (!list || list.IsNull()) return out;
    if (!list.IsSequence()) fail(std::string(what) + " must be a list");
    for (const auto& e : list) {
        PdoEntry p;
        p.index = to_uint<std::uint16_t>(e["index"], "PDO index");
        p.subindex = e["sub"] ? to_uint<std::uint8_t>(e["sub"], "PDO subindex") : 0;
        p.type = to_type(e["type"]);
        p.field = to_field(e["field"]);
        out.push_back(p);
    }
    return out;
}

std::optional<std::uint32_t> to_optional_u32(const YAML::Node& n, const char* what) {
    if (!n || n.IsNull()) return std::nullopt;
    return to_uint<std::uint32_t>(n, what);
}

}  // namespace

DriveDescriptor parse_drive_descriptor(const std::string& yaml_text) {
    YAML::Node root;
    try {
        root = YAML::Load(yaml_text);
    } catch (const YAML::Exception& e) {
        fail(std::string("YAML syntax error: ") + e.what());
    }
    if (!root.IsMap()) fail("top level must be a mapping");

    DriveDescriptor d;
    try {
        if (root["profile"] && root["profile"].as<std::string>() != "cia402") {
            fail("only profile 'cia402' is supported");
        }
        d.vendor = root["vendor"] ? root["vendor"].as<std::string>() : "";
        d.model = root["model"] ? root["model"].as<std::string>() : "";

        if (const auto id = root["identity"]) {
            d.vendor_id = to_optional_u32(id["vendor_id"], "identity.vendor_id");
            d.product_code = to_optional_u32(id["product_code"], "identity.product_code");
        }
        if (const auto s = root["sync"]) {
            if (s["distributed_clocks"]) d.distributed_clocks = s["distributed_clocks"].as<bool>();
        }
        if (const auto m = root["pdo_mapping"]) {
            if (m["configure"]) d.configure_pdo_mapping = m["configure"].as<bool>();
            if (m["rx_index"]) d.rx_mapping_index = to_uint<std::uint16_t>(m["rx_index"], "pdo_mapping.rx_index");
            if (m["tx_index"]) d.tx_mapping_index = to_uint<std::uint16_t>(m["tx_index"], "pdo_mapping.tx_index");
            if (m["max_pdo_bits"]) d.max_pdo_bits = to_uint<std::uint16_t>(m["max_pdo_bits"], "pdo_mapping.max_pdo_bits");
            if (m["max_pdo_entries"]) {
                d.max_pdo_entries = to_uint<std::uint8_t>(m["max_pdo_entries"], "pdo_mapping.max_pdo_entries");
            }
        }
        d.rx_pdo = to_entries(root["rx_pdo"], "rx_pdo");
        d.tx_pdo = to_entries(root["tx_pdo"], "tx_pdo");

        if (const auto list = root["startup_sdo"]; list && !list.IsNull()) {
            if (!list.IsSequence()) fail("startup_sdo must be a list");
            for (const auto& e : list) {
                SdoWrite w;
                w.index = to_uint<std::uint16_t>(e["index"], "startup_sdo index");
                w.subindex = e["sub"] ? to_uint<std::uint8_t>(e["sub"], "startup_sdo subindex") : 0;
                w.type = to_type(e["type"]);
                w.value = to_int(e["value"], "startup_sdo value");
                d.startup_sdo.push_back(w);
            }
        }
    } catch (const YAML::Exception& e) {
        fail(std::string("bad value: ") + e.what());
    }

    if (const std::string err = validate(d); !err.empty()) fail(err);
    return d;
}

DriveDescriptor load_drive_descriptor(const std::string& path) {
    std::ifstream f(path);
    if (!f) throw std::runtime_error("drive descriptor: cannot open " + path);
    std::stringstream ss;
    ss << f.rdbuf();
    try {
        return parse_drive_descriptor(ss.str());
    } catch (const std::runtime_error& e) {
        throw std::runtime_error(path + ": " + e.what());
    }
}

}  // namespace maxmount::config
