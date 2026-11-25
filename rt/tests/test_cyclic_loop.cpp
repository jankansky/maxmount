// SPDX-License-Identifier: GPL-3.0-or-later
#include <gtest/gtest.h>

#include <vector>

#include "maxmount/core/realtime.hpp"

using namespace maxmount;

// The body receives the scheduled cycle times: exact multiples of the period (plus any
// correction it returned), independent of how late each wake-up actually was.
TEST(CyclicLoop, PassesScheduledTimesIncludingCorrection) {
    constexpr nanos kPeriod = 2'000'000;
    constexpr nanos kCorrection = 1'000;
    CyclicLoop loop(kPeriod);
    std::atomic<bool> stop{false};
    std::vector<nanos> scheduled;
    loop.run(stop, [&](nanos t) -> nanos {
        scheduled.push_back(t);
        if (scheduled.size() == 20) stop.store(true);
        return kCorrection;
    });
    ASSERT_EQ(scheduled.size(), 20u);
    for (std::size_t i = 1; i < scheduled.size(); ++i) {
        // An overrun skips whole periods; otherwise the spacing is exact.
        const nanos d = scheduled[i] - scheduled[i - 1] - kCorrection;
        EXPECT_EQ(d % kPeriod, 0) << "cycle " << i;
        EXPECT_GE(d, kPeriod);
    }
}
