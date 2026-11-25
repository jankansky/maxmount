// SPDX-License-Identifier: GPL-3.0-or-later
#include <gtest/gtest.h>

#include <array>
#include <stdexcept>

#include "maxmount/config/drive_descriptor_yaml.hpp"
#include "maxmount/fieldbus/drive_descriptor.hpp"

using namespace maxmount;

namespace {

DriveDescriptor csp_descriptor() {
    DriveDescriptor d;
    d.rx_pdo = {{0x6040, 0, DataType::U16, PdoField::Controlword},
                {0x6060, 0, DataType::I8, PdoField::Mode},
                {0x607A, 0, DataType::I32, PdoField::TargetPosition},
                {0x60B1, 0, DataType::I32, PdoField::VelocityOffset}};
    d.tx_pdo = {{0x6041, 0, DataType::U16, PdoField::Statusword},
                {0x6064, 0, DataType::I32, PdoField::PositionActual},
                {0x2000, 0, DataType::U8, PdoField::None},  // vendor object, ignored by core
                {0x603F, 0, DataType::U16, PdoField::ErrorCode}};
    return d;
}

}  // namespace

TEST(PdoCodec, LayoutAndLittleEndianEncoding) {
    const PdoCodec codec(csp_descriptor());
    EXPECT_EQ(codec.output_size(), 2u + 1 + 4 + 4);
    EXPECT_EQ(codec.input_size(), 2u + 4 + 1 + 2);

    Cia402Outputs out;
    out.controlword = 0x000F;
    out.mode = 8;
    out.target_position = -2;            // 0xFFFFFFFE
    out.velocity_offset = 0x01020304;
    std::array<std::uint8_t, 11> buf{};
    codec.encode(out, buf.data());
    const std::array<std::uint8_t, 11> expected = {0x0F, 0x00, 0x08, 0xFE, 0xFF, 0xFF, 0xFF,
                                                   0x04, 0x03, 0x02, 0x01};
    EXPECT_EQ(buf, expected);
}

TEST(PdoCodec, DecodesSignedAndSkipsUnmapped) {
    const PdoCodec codec(csp_descriptor());
    const std::array<std::uint8_t, 9> buf = {0x37, 0x02, 0x00, 0x00, 0x00, 0x80, 0xAA, 0x11, 0x86};
    Cia402Inputs in;
    codec.decode(buf.data(), in);
    EXPECT_EQ(in.statusword, 0x0237);
    EXPECT_EQ(in.position_actual, INT32_MIN);
    EXPECT_EQ(in.error_code, 0x8611);
}

TEST(DriveDescriptor, PdoMappingSdoSequence) {
    const auto w = pdo_mapping_sdos(csp_descriptor());
    // rx: 2 disables + 4 entries + count + assign + enable = 9; tx likewise = 9
    ASSERT_EQ(w.size(), 18u);
    EXPECT_EQ(w[0].index, 0x1C12);
    EXPECT_EQ(w[0].value, 0);
    EXPECT_EQ(w[2].index, 0x1600);
    EXPECT_EQ(w[2].subindex, 1);
    EXPECT_EQ(w[2].type, DataType::U32);
    EXPECT_EQ(w[2].value, 0x60400010);  // 0x6040:00, 16 bits
    EXPECT_EQ(w[4].value, 0x607A0020);  // 0x607A:00, 32 bits
    EXPECT_EQ(w[6].index, 0x1600);
    EXPECT_EQ(w[6].value, 4);           // entry count
    EXPECT_EQ(w[7].index, 0x1C12);
    EXPECT_EQ(w[7].value, 0x1600);
    EXPECT_EQ(w[9].index, 0x1C13);
    EXPECT_EQ(w[13].value, 0x20000008);  // padding entry still mapped
}

TEST(DriveDescriptor, ValidationCatchesMistakes) {
    auto d = csp_descriptor();
    EXPECT_EQ(validate(d), "");

    auto missing = d;
    missing.rx_pdo.erase(missing.rx_pdo.begin());  // no controlword
    EXPECT_NE(validate(missing), "");

    auto wrong_dir = d;
    wrong_dir.tx_pdo.push_back({0x607A, 0, DataType::I32, PdoField::TargetPosition});
    EXPECT_NE(validate(wrong_dir).find("0x607A"), std::string::npos);
}

TEST(DriveDescriptorYaml, ParsesHexAndFields) {
    const auto d = config::parse_drive_descriptor(R"(
vendor: Example
profile: cia402
identity: {vendor_id: 0x000001A3, product_code: null}
pdo_mapping: {configure: true, rx_index: 0x1601}
rx_pdo:
  - {index: 0x6040, type: u16, field: controlword}
  - {index: 0x607A, sub: 0, type: i32, field: target_position}
tx_pdo:
  - {index: 0x6041, type: u16, field: statusword}
  - {index: 0x6064, type: i32, field: position_actual}
startup_sdo:
  - {index: 0x60C2, sub: 1, type: u8, value: 1}
)");
    EXPECT_EQ(d.vendor, "Example");
    ASSERT_TRUE(d.vendor_id.has_value());
    EXPECT_EQ(*d.vendor_id, 0x1A3u);
    EXPECT_FALSE(d.product_code.has_value());
    EXPECT_EQ(d.rx_mapping_index, 0x1601);
    ASSERT_EQ(d.rx_pdo.size(), 2u);
    EXPECT_EQ(d.rx_pdo[1].index, 0x607A);
    EXPECT_EQ(d.rx_pdo[1].field, PdoField::TargetPosition);
    ASSERT_EQ(d.startup_sdo.size(), 1u);
    EXPECT_EQ(d.startup_sdo[0].index, 0x60C2);
}

TEST(DriveDescriptorYaml, RejectsBadInput) {
    EXPECT_THROW(config::parse_drive_descriptor("rx_pdo: [{index: 0x6040, type: u17, field: controlword}]"),
                 std::runtime_error);
    EXPECT_THROW(config::parse_drive_descriptor("rx_pdo: [{index: 0x16040, type: u16, field: controlword}]"),
                 std::runtime_error);
    EXPECT_THROW(config::parse_drive_descriptor("rx_pdo: [{index: 0x6040, type: u16, field: speed}]"),
                 std::runtime_error);
}

TEST(DriveDescriptorYaml, RepositoryDescriptorsLoad) {
    const auto ipos = config::load_drive_descriptor(MAXMOUNT_CONFIG_DIR "/drives/technosoft_ipos4808_bx_cat.yaml");
    EXPECT_EQ(ipos.rx_pdo.size(), 5u);
    EXPECT_EQ(ipos.tx_pdo.size(), 6u);
    EXPECT_EQ(ipos.vendor_id, 0x000001A3u);  // read from the drive with slaveinfo
    EXPECT_EQ(ipos.product_code, 0x019F418Du);
    EXPECT_EQ(PdoCodec(ipos).output_size(), 13u);
    EXPECT_EQ(PdoCodec(ipos).input_size(), 15u);

    const auto sim = config::load_drive_descriptor(MAXMOUNT_CONFIG_DIR "/drives/sim_cia402.yaml");
    EXPECT_FALSE(sim.configure_pdo_mapping);
}
