// SPDX-License-Identifier: GPL-3.0-or-later
// Zenoh bridge for mount-rt (keys: schemas/zenoh-keys.md).
//
// Threading: Zenoh delivers trajectories and commands on its own threads. Those callbacks only
// decode, validate and append to a mutex-protected queue, and reply to command queries. The
// owner's main thread, which must be the controller's only command producer, calls forward()
// to move queued commands into the controller and on_telemetry() for every telemetry sample.
// Nothing here runs on the real-time thread.
#pragma once

#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <vector>

#include "maxmount/bridge/codec.hpp"
#include "maxmount/config/system_config.hpp"
#include "maxmount/core/controller.hpp"

namespace maxmount::bridge {

struct BridgeOptions {
    config::ZenohConfig zenoh;
    std::vector<std::string> axis_names;  // controller order
    nanos cycle = 1'000'000;
    // Serve mode: accept trajectories and commands. Otherwise every command is rejected with
    // reject_reason and trajectories are dropped; telemetry is published either way.
    bool accept_commands = false;
    std::string reject_reason = "mount-rt is not in serve mode";
    // Extra check for each accepted command, called on a Zenoh thread with the latest
    // telemetry sample. Return a non-empty reason to reject. Not called for PushPoint.
    std::function<std::string(const Command&, const TelemetrySample&)> validate;
    std::size_t max_pending = 20000;      // queued commands before new input is refused
    nanos telemetry_period = 20'000'000;  // mount/telemetry rate (50 Hz)
    std::size_t raw_batch = 50;           // samples per mount/telemetry/raw message
};

struct BridgeStats {
    std::uint64_t trajectories = 0;  // accepted TrajectoryUpdate messages
    std::uint64_t commands = 0;      // accepted MountCommand queries
    std::uint64_t rejected = 0;      // rejected updates or commands
    std::uint64_t published = 0;     // telemetry messages published
};

class ZenohBridge {
public:
    explicit ZenohBridge(BridgeOptions options);
    ~ZenohBridge();
    ZenohBridge(const ZenohBridge&) = delete;
    ZenohBridge& operator=(const ZenohBridge&) = delete;

    // Opens the session and declares keys. Throws std::runtime_error on failure.
    void start();
    void stop();

    // Main thread: moves queued commands into the controller (keeps the rest if its ring is
    // full). Returns the number posted.
    std::size_t forward(Controller& controller);

    // Main thread: called for every telemetry sample, in order.
    void on_telemetry(const TelemetrySample& s, const LoopSnapshot& loop);

    BridgeStats stats() const;
    std::string key(const std::string& suffix) const;  // maxmount/<site>/<suffix>

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace maxmount::bridge
