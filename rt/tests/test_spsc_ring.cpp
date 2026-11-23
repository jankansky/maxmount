// SPDX-License-Identifier: GPL-3.0-or-later
#include <gtest/gtest.h>

#include <cstdint>
#include <memory>
#include <thread>

#include "maxmount/common/spsc_ring.hpp"

using namespace maxmount;

TEST(SpscRing, FifoAndFull) {
    SpscRing<int, 4> r;
    EXPECT_FALSE(r.try_pop());
    for (int i = 0; i < 4; ++i) EXPECT_TRUE(r.try_push(i));
    EXPECT_FALSE(r.try_push(99));
    for (int i = 0; i < 4; ++i) EXPECT_EQ(r.try_pop().value(), i);
    EXPECT_FALSE(r.try_pop());
}

TEST(SpscRing, ConcurrentProducerConsumer) {
    auto r = std::make_unique<SpscRing<std::uint64_t, 1024>>();
    constexpr std::uint64_t kN = 200000;
    std::thread producer([&] {
        for (std::uint64_t i = 0; i < kN;) {
            if (r->try_push(i)) ++i;
        }
    });
    std::uint64_t expected = 0;
    while (expected < kN) {
        if (auto v = r->try_pop()) {
            ASSERT_EQ(*v, expected);
            ++expected;
        }
    }
    producer.join();
}
