// SPDX-License-Identifier: GPL-3.0-or-later
// Conversion between maxmount.v1 Protobuf messages (schemas/maxmount/v1/mount.proto) and the
// controller's commands and telemetry. Protobuf types stay in the .cpp so users of this
// header do not depend on generated code.
#pragma once

#include <cstdint>
#include <memory>
#include <string>
#include <string_view>
#include <vector>

#include "maxmount/core/controller.hpp"

namespace maxmount::bridge {

// Inbound message decoded into controller commands, or a reason it was rejected.
struct Decoded {
    std::vector<Command> commands;
    std::string error;  // empty = valid
    bool ok() const noexcept { return error.empty(); }
};

// TrajectoryUpdate -> PushPoint commands. Rejects unknown axes, non-increasing times and
// non-finite values; the whole update is rejected if any part is invalid.
Decoded decode_trajectory(std::string_view bytes, const std::vector<std::string>& axis_names);

// MountCommand -> one command. An empty axis name means all axes.
Decoded decode_command(std::string_view bytes, const std::vector<std::string>& axis_names);

std::string encode_reply(bool accepted, const std::string& message);

struct LoopSnapshot {
    std::uint64_t cycles = 0;
    std::uint64_t overruns = 0;
    nanos max_wake_latency_ns = 0;
};

// One MountTelemetry message; loop statistics are included when loop is non-null.
std::string encode_telemetry(const TelemetrySample& s, const std::vector<std::string>& axis_names,
                             const LoopSnapshot* loop = nullptr);

// Accumulates samples into a MountTelemetryBatch.
class BatchEncoder {
public:
    explicit BatchEncoder(std::vector<std::string> axis_names);
    ~BatchEncoder();
    BatchEncoder(const BatchEncoder&) = delete;
    BatchEncoder& operator=(const BatchEncoder&) = delete;

    void add(const TelemetrySample& s);
    std::size_t size() const noexcept;
    std::string take();  // serialises and clears

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace maxmount::bridge
