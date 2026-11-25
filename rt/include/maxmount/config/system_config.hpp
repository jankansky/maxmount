// SPDX-License-Identifier: GPL-3.0-or-later
// Complete system configuration (config/systems/<name>/system.yaml): fieldbus, real-time
// settings and, per axis, its drive descriptor, bus position, scaling and limits.
#pragma once

#include <string>
#include <vector>

#include "maxmount/axis/axis.hpp"
#include "maxmount/common/time.hpp"
#include "maxmount/fieldbus/drive_descriptor.hpp"

namespace maxmount::config {

struct AxisEntry {
    AxisConfig axis;              // scaling (counts per radian, sign = direction) and limits
    std::string drive;            // descriptor name, config/drives/<drive>.yaml
    int bus_position = 0;         // 0-based position on the EtherCAT segment
    DriveDescriptor descriptor;
    bool scaled = false;          // false while counts_per_rev is unknown (axis not commissioned)
    double counts_per_rev = 0.0;  // as configured (unsigned); 0 when unknown
};

// Optional `zenoh:` section. Without it mount-rt runs without the Zenoh bridge.
struct ZenohConfig {
    bool enabled = false;
    std::string site = "default";       // keys live under maxmount/<site>/
    std::string config_file;            // optional Zenoh JSON5 config; overrides the fields below
    std::string mode = "peer";          // peer, client or router
    std::vector<std::string> connect;   // e.g. tcp/192.168.1.10:7447
    std::vector<std::string> listen;
    bool multicast_scouting = true;
    bool publish_raw = true;            // every-cycle telemetry on mount/telemetry/raw
};

struct SystemConfig {
    std::string name;
    std::string backend = "sim";  // "sim" or "soem"
    std::string interface;        // EtherCAT network interface (soem)
    nanos cycle = 1'000'000;
    int rt_cpu = -1;
    int rt_priority = 80;
    nanos command_lead = 1'000'000;
    std::vector<AxisEntry> axes;  // controller axis i is drive i on the bus, in this order
    ZenohConfig zenoh;

    const AxisEntry* find_axis(const std::string& name) const;
};

// Throw std::runtime_error with a readable message on any error. Drive descriptors are loaded
// from <drives_dir>/<drive>.yaml. load_system_config defaults drives_dir to the "drives"
// directory next to the "systems" directory that holds the file
// (config/systems/<name>/system.yaml -> config/drives).
SystemConfig parse_system_config(const std::string& yaml_text, const std::string& drives_dir);
SystemConfig load_system_config(const std::string& path, std::string drives_dir = {});

// Keeps only the named axes (in system order), e.g. a test bench with one drive connected.
// Throws if a name is unknown. Bus positions are left as configured.
SystemConfig select_axes(const SystemConfig& sys, const std::vector<std::string>& names);

}  // namespace maxmount::config
