// SPDX-License-Identifier: GPL-3.0-or-later
#include "maxmount/core/realtime.hpp"

#include <cerrno>
#include <cstring>
#include <thread>

#if defined(__linux__)
#include <pthread.h>
#include <sched.h>
#include <sys/mman.h>
#endif

namespace maxmount {

RealtimeSetup configure_realtime_thread(int priority, int cpu) {
    RealtimeSetup r;
#if defined(__linux__)
    if (mlockall(MCL_CURRENT | MCL_FUTURE) == 0) {
        r.memory_locked = true;
    } else {
        r.message += std::string("mlockall failed: ") + std::strerror(errno) + "; ";
    }

    // Pre-fault a chunk of stack so the RT loop never takes a page fault on it.
    {
        volatile unsigned char stack[64 * 1024];
        for (std::size_t i = 0; i < sizeof(stack); i += 4096) stack[i] = 0;
    }

    if (priority > 0) {  // priority <= 0: stay in the default scheduling class
        sched_param sp{};
        sp.sched_priority = priority;
        const int rc = pthread_setschedparam(pthread_self(), SCHED_FIFO, &sp);
        if (rc == 0) {
            r.fifo_priority = true;
        } else {
            r.message += std::string("SCHED_FIFO failed: ") + std::strerror(rc) + "; ";
        }
    }

    if (cpu >= 0) {
        cpu_set_t set;
        CPU_ZERO(&set);
        CPU_SET(cpu, &set);
        const int arc = pthread_setaffinity_np(pthread_self(), sizeof(set), &set);
        if (arc == 0) {
            r.cpu_pinned = true;
        } else {
            r.message += std::string("CPU affinity failed: ") + std::strerror(arc) + "; ";
        }
    }
    if (r.message.empty()) r.message = "real-time setup OK";
#else
    (void)priority;
    (void)cpu;
    r.message = "real-time scheduling not supported on this platform; running best-effort";
#endif
    return r;
}

void sleep_until_monotonic(nanos deadline) noexcept {
#if defined(__linux__)
    // std::steady_clock is CLOCK_MONOTONIC on Linux, so deadlines share its time base.
    const timespec ts = to_timespec(deadline);
    while (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, nullptr) == EINTR) {
    }
#else
    std::this_thread::sleep_until(
        std::chrono::steady_clock::time_point(std::chrono::nanoseconds(deadline)));
#endif
}

}  // namespace maxmount
