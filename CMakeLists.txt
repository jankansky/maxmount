# SPDX-License-Identifier: GPL-3.0-or-later
cmake_minimum_required(VERSION 3.20)
project(maxmount VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

option(MAXMOUNT_BUILD_TESTS "Build unit tests" ON)
option(MAXMOUNT_WITH_ZENOH "Build the Zenoh bridge (needs Protobuf; downloads zenoh-c if not installed)" OFF)
option(MAXMOUNT_WITH_SOEM "Build the SOEM EtherCAT backend (Linux only)" OFF)

if (MAXMOUNT_BUILD_TESTS)
    enable_testing()
endif ()

add_subdirectory(rt)
