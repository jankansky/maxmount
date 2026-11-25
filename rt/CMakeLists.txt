# SPDX-License-Identifier: GPL-3.0-or-later
find_package(Threads REQUIRED)
include(FetchContent)

set(MAXMOUNT_WARNINGS $<$<CXX_COMPILER_ID:GNU,Clang,AppleClang>:-Wall -Wextra -Wpedantic -Wshadow>)

# ---- Real-time core: no third-party dependencies -------------------------------------------
add_library(maxmount_rt
        src/cia402/cia402.cpp
        src/fieldbus/drive_descriptor.cpp
        src/sim/sim_fieldbus.cpp
        src/trajectory/trajectory.cpp
        src/axis/limiter.cpp
        src/core/controller.cpp
        src/core/realtime.cpp
)
add_library(maxmount::rt ALIAS maxmount_rt)
target_include_directories(maxmount_rt PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_link_libraries(maxmount_rt PUBLIC Threads::Threads)
target_compile_options(maxmount_rt PRIVATE ${MAXMOUNT_WARNINGS})

# ---- Configuration loading (yaml-cpp) -------------------------------------------------------
find_package(yaml-cpp 0.7 QUIET)
if (NOT TARGET yaml-cpp::yaml-cpp)
    set(YAML_CPP_BUILD_TESTS OFF CACHE BOOL "" FORCE)
    set(YAML_CPP_BUILD_TOOLS OFF CACHE BOOL "" FORCE)
    set(YAML_CPP_BUILD_CONTRIB OFF CACHE BOOL "" FORCE)
    set(YAML_CPP_INSTALL OFF CACHE BOOL "" FORCE)
    FetchContent_Declare(yaml-cpp
            URL https://github.com/jbeder/yaml-cpp/archive/refs/tags/0.8.0.tar.gz)
    FetchContent_MakeAvailable(yaml-cpp)
endif ()

add_library(maxmount_config
        src/config/drive_descriptor_yaml.cpp
        src/config/system_config_yaml.cpp)
add_library(maxmount::config ALIAS maxmount_config)
target_link_libraries(maxmount_config PUBLIC maxmount::rt PRIVATE yaml-cpp::yaml-cpp)
target_compile_options(maxmount_config PRIVATE ${MAXMOUNT_WARNINGS})

# ---- SOEM EtherCAT backend (Linux, optional) ------------------------------------------------
if (MAXMOUNT_WITH_SOEM)
    if (NOT CMAKE_SYSTEM_NAME STREQUAL "Linux")
        message(FATAL_ERROR "MAXMOUNT_WITH_SOEM requires Linux (SOEM has no macOS port)")
    endif ()
    enable_language(C)
    # SOEM 2.x is GPLv3 (or commercial from RT-Labs). Override the source with
    # -DFETCHCONTENT_SOURCE_DIR_SOEM=/path/to/SOEM to use a local checkout.
    FetchContent_Declare(soem
            GIT_REPOSITORY https://github.com/OpenEtherCATsociety/SOEM.git
            GIT_TAG v2.0.1
            GIT_SHALLOW TRUE
            SYSTEM)  # treat SOEM headers as system headers: no warnings from third-party code
    FetchContent_MakeAvailable(soem)

    add_library(maxmount_soem src/fieldbus/soem/soem_fieldbus.cpp)
    add_library(maxmount::soem ALIAS maxmount_soem)
    target_link_libraries(maxmount_soem PUBLIC maxmount::rt PRIVATE soem)
    target_compile_options(maxmount_soem PRIVATE ${MAXMOUNT_WARNINGS})
endif ()

# ---- Applications ---------------------------------------------------------------------------
add_executable(mount-rt apps/mount_rt_main.cpp)
target_link_libraries(mount-rt PRIVATE maxmount::config)
target_compile_options(mount-rt PRIVATE ${MAXMOUNT_WARNINGS})
if (MAXMOUNT_WITH_SOEM)
    target_link_libraries(mount-rt PRIVATE maxmount::soem)
    target_compile_definitions(mount-rt PRIVATE MAXMOUNT_HAVE_SOEM=1)
endif ()

# Linking replaces the binary and drops its file capabilities. With MAXMOUNT_SETCAP=ON they
# are re-applied after every link via passwordless sudo (sudo -n); if that is not available
# the build prints the command to run instead of failing.
option(MAXMOUNT_SETCAP "Re-apply mount-rt file capabilities after linking (Linux, sudo -n)" OFF)
if (MAXMOUNT_SETCAP AND CMAKE_SYSTEM_NAME STREQUAL "Linux")
    set(_caps cap_net_raw,cap_net_admin,cap_sys_nice,cap_ipc_lock+ep)
    add_custom_command(TARGET mount-rt POST_BUILD
            COMMAND sh -c "sudo -n setcap ${_caps} '$<TARGET_FILE:mount-rt>' || echo 'MAXMOUNT_SETCAP: sudo -n failed; run: sudo setcap ${_caps} $<TARGET_FILE:mount-rt>'"
            VERBATIM)
endif ()

# ---- Tests ----------------------------------------------------------------------------------
if (MAXMOUNT_BUILD_TESTS)
    find_package(GTest QUIET)
    if (NOT GTest_FOUND)
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
            tests/test_drive_descriptor.cpp
            tests/test_cyclic_loop.cpp
            tests/test_move_profile.cpp
            tests/test_system_config.cpp
    )
    target_link_libraries(maxmount_rt_tests PRIVATE maxmount::config GTest::gtest_main)
    target_compile_definitions(maxmount_rt_tests PRIVATE
            MAXMOUNT_CONFIG_DIR="${PROJECT_SOURCE_DIR}/config")
    gtest_discover_tests(maxmount_rt_tests)
endif ()
