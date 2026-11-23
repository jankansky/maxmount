# SPDX-License-Identifier: GPL-3.0-or-later
find_package(Threads REQUIRED)

add_library(maxmount_rt
        src/cia402/cia402.cpp
        src/sim/sim_fieldbus.cpp
        src/trajectory/trajectory.cpp
        src/axis/limiter.cpp
        src/core/controller.cpp
        src/core/realtime.cpp
)
add_library(maxmount::rt ALIAS maxmount_rt)
target_include_directories(maxmount_rt PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_link_libraries(maxmount_rt PUBLIC Threads::Threads)
target_compile_options(maxmount_rt PRIVATE
        $<$<CXX_COMPILER_ID:GNU,Clang,AppleClang>:-Wall -Wextra -Wpedantic -Wshadow>)

if (MAXMOUNT_WITH_SOEM)
    message(FATAL_ERROR "The SOEM backend is not implemented yet (see rt/src/fieldbus/soem/README.md)")
endif ()

add_executable(mount-rt apps/mount_rt_main.cpp)
target_link_libraries(mount-rt PRIVATE maxmount::rt)
target_compile_options(mount-rt PRIVATE
        $<$<CXX_COMPILER_ID:GNU,Clang,AppleClang>:-Wall -Wextra -Wpedantic -Wshadow>)

if (MAXMOUNT_BUILD_TESTS)
    find_package(GTest QUIET)
    if (NOT GTest_FOUND)
        include(FetchContent)
        FetchContent_Declare(googletest
                URL https://github.com/google/googletest/archive/refs/tags/v1.15.2.tar.gz)
        set(INSTALL_GTEST OFF CACHE BOOL "" FORCE)
        FetchContent_MakeAvailable(googletest)
    endif ()
    include(GoogleTest)

    add_executable(maxmount_rt_tests
            tests/test_cia402.cpp
            tests/test_trajectory.cpp
            tests/test_limiter.cpp
            tests/test_spsc_ring.cpp
            tests/test_controller.cpp
    )
    target_link_libraries(maxmount_rt_tests PRIVATE maxmount::rt GTest::gtest_main)
    gtest_discover_tests(maxmount_rt_tests)
endif ()
