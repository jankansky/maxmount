// SPDX-License-Identifier: GPL-3.0-or-later
// Real-time thread setup and a drift-free periodic loop with latency statistics.
#pragma once

#include <atomic>
#include <cstdint>
#include <string>
#include <type_traits>

#include "maxmount/common/time.hpp"

namespace maxmount {

struct RealtimeSetup {
    bool memory_locked = false;
    bool fifo_priority = false;
    bool cpu_pinned = false;
    std::string message;
};

// Locks memory, sets SCHED_FIFO priority and pins the calling thread to cpu (if >= 0).
// On non-Linux platforms this is a no-op that reports best-effort operation.
RealtimeSetup configure_realtime_thread(int priority, int cpu);

void sleep_until_monotonic(nanos deadline) noexcept;

struct LoopStats {
    std::atomic<std::uint64_t> cycles{0};
    std::atomic<std::uint64_t> overruns{0};
    std::atomic<nanos> max_wake_latency_ns{0};  // reset by the reader with exchange(0)
};

class CyclicLoop {
public:
    explicit CyclicLoop(nanos period) : period_(period) {}

    // Calls body() once per period. If body returns a value, it is added to the next wake-up
    // time (used to phase-lock to an EtherCAT distributed clock).
    template <typename Body>
    void run(const std::atomic<bool>& stop, Body&& body) {
        nanos next = monotonic_ns() + period_;
        while (!stop.load(std::memory_order_relaxed)) {
            sleep_until_monotonic(next);
            const nanos latency = monotonic_ns() - next;
            if (latency > stats_.max_wake_latency_ns.load(std::memory_order_relaxed)) {
                stats_.max_wake_latency_ns.store(latency, std::memory_order_relaxed);
            }
            nanos correction = 0;
            if constexpr (std::is_void_v<std::invoke_result_t<Body&>>) {
                body();
            } else {
                correction = static_cast<nanos>(body());
            }
            stats_.cycles.fetch_add(1, std::memory_order_relaxed);
            next += period_ + correction;
            const nanos now = monotonic_ns();
            if (now > next) {  // missed one or more deadlines: count them and re-align
                const nanos missed = (now - next) / period_ + 1;
                stats_.overruns.fetch_add(static_cast<std::uint64_t>(missed),
                                          std::memory_order_relaxed);
                next += missed * period_;
            }
        }
    }

    LoopStats& stats() noexcept { return stats_; }
    nanos period() const noexcept { return period_; }

private:
    nanos period_;
    LoopStats stats_;
};

}  // namespace maxmount
