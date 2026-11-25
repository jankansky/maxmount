# SPDX-License-Identifier: GPL-3.0-or-later
find_package(Threads REQUIRED)
include(FetchContent)
if (POLICY CMP0135)  # extracted archives get the extraction time (CMake >= 3.24)
    cmake_policy(SET CMP0135 NEW)
endif ()

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

# ---- Zenoh bridge (optional) ----------------------------------------------------------------
# Needs Protobuf (protoc + libprotobuf) and zenoh-c/zenoh-cpp. Installed zenoh packages are
# used if found (e.g. libzenohc-dev from the Eclipse Debian repository); otherwise the pinned
# prebuilt release archives are downloaded. Zenoh is EPL-2.0 OR Apache-2.0 (used under
# Apache-2.0, which is compatible with GPLv3).
if (MAXMOUNT_WITH_ZENOH)
    set(MAXMOUNT_ZENOH_VERSION 1.10.2)
    set(_zenoh_prebuilt OFF)
    find_package(zenohc ${MAXMOUNT_ZENOH_VERSION} QUIET)
    if (NOT TARGET zenohc::lib)
        set(_zenoh_prebuilt ON)
        set(_zbase https://github.com/eclipse-zenoh/zenoh-c/releases/download/${MAXMOUNT_ZENOH_VERSION})
        string(TOLOWER "${CMAKE_SYSTEM_PROCESSOR}" _cpu)
        if (APPLE AND _cpu MATCHES "arm64|aarch64")
            set(_zfile aarch64-apple-darwin)
            set(_zsha 633312653225a83093ce62b4440c96892bf735f29e1cf50355a43ab1c99e9964)
        elseif (APPLE)
            set(_zfile x86_64-apple-darwin)
            set(_zsha 64eaa7a9ae93c4abb309dc18aef678730f0376c1a6ef6bdf501a3597701880dd)
        elseif (CMAKE_SYSTEM_NAME STREQUAL "Linux" AND _cpu MATCHES "aarch64|arm64")
            set(_zfile aarch64-unknown-linux-gnu)
            set(_zsha 0fdafdf08cee6f0da361735da159cd6cbe4dfcf351ee578cb0ea9f05ce083b47)
        elseif (CMAKE_SYSTEM_NAME STREQUAL "Linux" AND _cpu MATCHES "x86_64|amd64")
            set(_zfile x86_64-unknown-linux-gnu)
            set(_zsha d2da2ac370381d553cb6619abe23549966b80207687ebea6de9c4a491fda5f67)
        else ()
            message(FATAL_ERROR "No prebuilt zenoh-c for ${CMAKE_SYSTEM_NAME}/${CMAKE_SYSTEM_PROCESSOR}; "
                    "install zenoh-c ${MAXMOUNT_ZENOH_VERSION} and set zenohc_DIR")
        endif ()
        FetchContent_Declare(zenohc_prebuilt
                URL ${_zbase}/zenoh-c-${MAXMOUNT_ZENOH_VERSION}-${_zfile}-standalone.zip
                URL_HASH SHA256=${_zsha})
        FetchContent_MakeAvailable(zenohc_prebuilt)
        find_package(zenohc ${MAXMOUNT_ZENOH_VERSION} REQUIRED CONFIG
                PATHS ${zenohc_prebuilt_SOURCE_DIR}/lib/cmake NO_DEFAULT_PATH)
    endif ()
    find_package(zenohcxx ${MAXMOUNT_ZENOH_VERSION} QUIET)
    if (NOT TARGET zenohcxx::zenohc)
        FetchContent_Declare(zenohcxx_prebuilt
                URL https://github.com/eclipse-zenoh/zenoh-cpp/releases/download/${MAXMOUNT_ZENOH_VERSION}/zenohcpp-${MAXMOUNT_ZENOH_VERSION}-standalone.zip
                URL_HASH SHA256=3df2c5192dd02e330b56c737764fc4ec8e9d8087e77099c910c23cab98434ed1)
        FetchContent_MakeAvailable(zenohcxx_prebuilt)
        find_package(zenohcxx ${MAXMOUNT_ZENOH_VERSION} REQUIRED CONFIG
                PATHS ${zenohcxx_prebuilt_SOURCE_DIR}/lib/cmake NO_DEFAULT_PATH)
    endif ()

    # One target for the C++ API. The prebuilt archives are linked statically: their shared
    # library carries the CI build path as its install name (macOS), and a static mount-rt
    # needs no library path on the Pi. The macOS static target names frameworks by the CI
    # machine's Xcode path, so replace those with plain -framework flags.
    add_library(maxmount_zenoh INTERFACE)
    get_target_property(_zcxx_inc zenohcxx::zenohc INTERFACE_INCLUDE_DIRECTORIES)
    target_include_directories(maxmount_zenoh SYSTEM INTERFACE ${_zcxx_inc})
    target_compile_definitions(maxmount_zenoh INTERFACE ZENOHCXX_ZENOHC)
    if (zenohc_DIR MATCHES "zenohc_prebuilt-src")  # also on reconfigure, via the cache
        set(_zenoh_prebuilt ON)
    endif ()
    if (_zenoh_prebuilt)
        if (APPLE)
            set_property(TARGET __zenohc_static PROPERTY INTERFACE_LINK_LIBRARIES
                    "-framework Foundation" "-framework Security")
        endif ()
        target_link_libraries(maxmount_zenoh INTERFACE zenohc::static)
    else ()
        target_link_libraries(maxmount_zenoh INTERFACE zenohc::lib)
    endif ()

    # Protobuf: prefer the CMake package config (Homebrew, recent protobuf); fall back to
    # CMake's FindProtobuf (Debian's libprotobuf-dev).
    find_package(Protobuf CONFIG QUIET)
    if (NOT Protobuf_FOUND)
        find_package(Protobuf REQUIRED)
    endif ()
    set(_proto_out ${CMAKE_CURRENT_BINARY_DIR}/proto)
    file(MAKE_DIRECTORY ${_proto_out})
    add_library(maxmount_proto ${PROJECT_SOURCE_DIR}/schemas/maxmount/v1/mount.proto)
    target_link_libraries(maxmount_proto PUBLIC protobuf::libprotobuf)
    target_include_directories(maxmount_proto PUBLIC ${_proto_out})
    protobuf_generate(TARGET maxmount_proto LANGUAGE cpp
            IMPORT_DIRS ${PROJECT_SOURCE_DIR}/schemas
            PROTOC_OUT_DIR ${_proto_out})

    add_library(maxmount_bridge src/bridge/codec.cpp src/bridge/zenoh_bridge.cpp)
    add_library(maxmount::bridge ALIAS maxmount_bridge)
    target_link_libraries(maxmount_bridge
            PUBLIC maxmount::config
            PRIVATE maxmount_proto maxmount_zenoh)
    target_compile_options(maxmount_bridge PRIVATE ${MAXMOUNT_WARNINGS})
endif ()

# ---- Applications ---------------------------------------------------------------------------
add_executable(mount-rt apps/mount_rt_main.cpp)
target_link_libraries(mount-rt PRIVATE maxmount::config)
target_compile_options(mount-rt PRIVATE ${MAXMOUNT_WARNINGS})
if (MAXMOUNT_WITH_SOEM)
    target_link_libraries(mount-rt PRIVATE maxmount::soem)
    target_compile_definitions(mount-rt PRIVATE MAXMOUNT_HAVE_SOEM=1)
endif ()
if (MAXMOUNT_WITH_ZENOH)
    target_link_libraries(mount-rt PRIVATE maxmount::bridge)
    target_compile_definitions(mount-rt PRIVATE MAXMOUNT_HAVE_ZENOH=1)

    add_executable(mount-ctl apps/mount_ctl_main.cpp)
    target_link_libraries(mount-ctl PRIVATE maxmount::config maxmount_proto maxmount_zenoh)
    target_compile_options(mount-ctl PRIVATE ${MAXMOUNT_WARNINGS})
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
    if (MAXMOUNT_WITH_ZENOH)
        target_sources(maxmount_rt_tests PRIVATE tests/test_bridge_codec.cpp tests/test_zenoh_bridge.cpp)
        target_link_libraries(maxmount_rt_tests PRIVATE maxmount::bridge maxmount_proto maxmount_zenoh)
    endif ()
    target_compile_definitions(maxmount_rt_tests PRIVATE
            MAXMOUNT_CONFIG_DIR="${PROJECT_SOURCE_DIR}/config")
    gtest_discover_tests(maxmount_rt_tests)
endif ()
