// SPDX-License-Identifier: GPL-3.0-or-later
#include <yaml-cpp/yaml.h>

#include <filesystem>
#include <fstream>
#include <set>
#include <sstream>
#include <stdexcept>

#include "maxmount/config/drive_descriptor_yaml.hpp"
#include "maxmount/config/system_config.hpp"

namespace maxmount::config {

namespace {

constexpr double kPi = 3.14159265358979323846;
constexpr double kDeg = kPi / 180.0;
constexpr std::size_t kMaxSystemAxes = 4;  // matches maxmount::kMaxAxes

[[noreturn]] void fail(const std::string& msg) { throw std::runtime_error("system config: " + msg); }

double to_double(const YAML::Node& n, const std::string& what) {
    if (!n || !n.IsScalar()) fail(what + " must be a number");
    try {
        return n.as<double>();
    } catch (const YAML::Exception&) {
        fail(what + ": '" + n.Scalar() + "' is not a number");
    }
}

long long to_int(const YAML::Node& n, const std::string& what) {
    if (!n || !n.IsScalar()) fail(what + " must be an integer");
    try {
        return n.as<long long>();
    } catch (const YAML::Exception&) {
        fail(what + ": '" + n.Scalar() + "' is not an integer");
    }
}

std::string to_string(const YAML::Node& n, const std::string& what) {
    if (!n || !n.IsScalar()) fail(what + " must be a string");
    return n.Scalar();
}

AxisLimits parse_limits(const YAML::Node& n, const std::string& axis) {
    const std::string p = "axis '" + axis + "' limits.";
    if (!n || !n.IsMap()) fail("axis '" + axis + "' needs a limits mapping");
    AxisLimits l;
    l.min_pos = to_double(n["min_deg"], p + "min_deg") * kDeg;
    l.max_pos = to_double(n["max_deg"], p + "max_deg") * kDeg;
    l.max_vel = to_double(n["max_vel_deg_s"], p + "max_vel_deg_s") * kDeg;
    l.max_acc = to_double(n["max_acc_deg_s2"], p + "max_acc_deg_s2") * kDeg;
    if (n["max_following_error_deg"]) {
        l.max_following_error = to_double(n["max_following_error_deg"], p + "max_following_error_deg") * kDeg;
        if (!(l.max_following_error > 0)) fail(p + "max_following_error_deg must be > 0");
    }
    if (!(l.min_pos < l.max_pos)) fail(p + "min_deg must be below max_deg");
    if (!(l.max_vel > 0) || !(l.max_acc > 0)) fail(p + "max_vel_deg_s and max_acc_deg_s2 must be > 0");
    return l;
}

}  // namespace

const AxisEntry* SystemConfig::find_axis(const std::string& axis_name) const {
    for (const auto& a : axes) {
        if (a.axis.name == axis_name) return &a;
    }
    return nullptr;
}

SystemConfig parse_system_config(const std::string& yaml_text, const std::string& drives_dir) {
    YAML::Node root;
    try {
        root = YAML::Load(yaml_text);
    } catch (const YAML::Exception& e) {
        fail(std::string("YAML syntax error: ") + e.what());
    }
    if (!root.IsMap()) fail("top level must be a mapping");

    SystemConfig sys;
    sys.name = root["system"] ? to_string(root["system"], "system") : "";

    const YAML::Node fb = root["fieldbus"];
    if (!fb || !fb.IsMap()) fail("missing fieldbus section");
    sys.backend = to_string(fb["backend"], "fieldbus.backend");
    if (sys.backend != "sim" && sys.backend != "soem") fail("fieldbus.backend must be 'sim' or 'soem'");
    if (sys.backend == "soem") sys.interface = to_string(fb["interface"], "fieldbus.interface");
    if (fb["cycle_us"]) {
        const long long us = to_int(fb["cycle_us"], "fieldbus.cycle_us");
        if (us < 100 || us > 100'000) fail("fieldbus.cycle_us must be 100..100000");
        sys.cycle = us * 1000;
    }

    if (const YAML::Node rt = root["realtime"]) {
        if (rt["cpu"]) sys.rt_cpu = static_cast<int>(to_int(rt["cpu"], "realtime.cpu"));
        if (rt["priority"]) sys.rt_priority = static_cast<int>(to_int(rt["priority"], "realtime.priority"));
        if (rt["command_lead_us"]) sys.command_lead = to_int(rt["command_lead_us"], "realtime.command_lead_us") * 1000;
    }

    if (const YAML::Node z = root["zenoh"]; z && !z.IsNull()) {
        if (!z.IsMap()) fail("zenoh must be a mapping");
        ZenohConfig& zc = sys.zenoh;
        zc.enabled = z["enabled"] ? z["enabled"].as<bool>() : true;
        if (z["site"]) zc.site = to_string(z["site"], "zenoh.site");
        if (zc.site.empty() || zc.site.find_first_of("/*$?#") != std::string::npos) {
            fail("zenoh.site must be a non-empty name without / * $ ? #");
        }
        if (z["config"] && !z["config"].IsNull()) zc.config_file = to_string(z["config"], "zenoh.config");
        if (z["mode"]) zc.mode = to_string(z["mode"], "zenoh.mode");
        if (zc.mode != "peer" && zc.mode != "client" && zc.mode != "router") {
            fail("zenoh.mode must be peer, client or router");
        }
        auto list = [](const YAML::Node& n, const char* what) {
            std::vector<std::string> out;
            if (!n || n.IsNull()) return out;
            if (!n.IsSequence()) fail(std::string(what) + " must be a list");
            for (const auto& e : n) out.push_back(to_string(e, what));
            return out;
        };
        zc.connect = list(z["connect"], "zenoh.connect");
        zc.listen = list(z["listen"], "zenoh.listen");
        if (z["multicast_scouting"]) zc.multicast_scouting = z["multicast_scouting"].as<bool>();
        if (z["publish_raw"]) zc.publish_raw = z["publish_raw"].as<bool>();
    }

    const YAML::Node axes = root["axes"];
    if (!axes || !axes.IsSequence() || axes.size() == 0) fail("axes must be a non-empty list");
    if (axes.size() > kMaxSystemAxes) fail("at most 4 axes are supported");

    std::set<std::string> names;
    std::set<int> positions;
    for (const auto& n : axes) {
        AxisEntry e;
        e.axis.name = to_string(n["name"], "axis name");
        const std::string& nm = e.axis.name;
        if (!names.insert(nm).second) fail("duplicate axis name '" + nm + "'");

        e.drive = to_string(n["drive"], "axis '" + nm + "' drive");
        e.bus_position = static_cast<int>(to_int(n["bus_position"], "axis '" + nm + "' bus_position"));
        if (e.bus_position < 0) fail("axis '" + nm + "' bus_position must be >= 0");
        if (sys.backend == "soem" && !positions.insert(e.bus_position).second) {
            fail("axis '" + nm + "' shares a bus_position with another axis");
        }

        if (const YAML::Node c = n["counts_per_rev"]; c && !c.IsNull()) {
            e.counts_per_rev = to_double(c, "axis '" + nm + "' counts_per_rev");
            if (!(e.counts_per_rev > 0)) fail("axis '" + nm + "' counts_per_rev must be > 0");
            e.scaled = true;
        }
        const bool invert = n["invert"] && n["invert"].as<bool>();
        // Unscaled axes report raw drive counts as if they were radians (monitor mode only).
        e.axis.counts_per_rad = (e.scaled ? e.counts_per_rev / (2 * kPi) : 1.0) * (invert ? -1.0 : 1.0);
        if (n["offset_deg"]) e.axis.offset_rad = to_double(n["offset_deg"], "axis '" + nm + "' offset_deg") * kDeg;
        e.axis.limits = parse_limits(n["limits"], nm);
        e.axis.vel_units_per_rad_s = 0.0;  // velocity feedforward is not configured yet

        const std::string path = (std::filesystem::path(drives_dir) / (e.drive + ".yaml")).string();
        e.descriptor = load_drive_descriptor(path);
        sys.axes.push_back(std::move(e));
    }
    return sys;
}

SystemConfig load_system_config(const std::string& path, std::string drives_dir) {
    std::ifstream f(path);
    if (!f) throw std::runtime_error("system config: cannot open " + path);
    std::stringstream ss;
    ss << f.rdbuf();
    if (drives_dir.empty()) {
        const auto systems = std::filesystem::absolute(path).parent_path().parent_path();
        drives_dir = (systems.parent_path() / "drives").string();
    }
    try {
        return parse_system_config(ss.str(), drives_dir);
    } catch (const std::runtime_error& e) {
        throw std::runtime_error(path + ": " + e.what());
    }
}

SystemConfig select_axes(const SystemConfig& sys, const std::vector<std::string>& names) {
    for (const auto& n : names) {
        if (!sys.find_axis(n)) fail("no axis named '" + n + "'");
    }
    SystemConfig out = sys;
    out.axes.clear();
    for (const auto& a : sys.axes) {
        for (const auto& n : names) {
            if (a.axis.name == n) {
                out.axes.push_back(a);
                break;
            }
        }
    }
    return out;
}

}  // namespace maxmount::config
