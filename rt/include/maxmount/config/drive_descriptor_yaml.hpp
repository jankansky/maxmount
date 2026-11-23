// SPDX-License-Identifier: GPL-3.0-or-later
// YAML loader for drive descriptors (config/drives/*.yaml). Lives in the maxmount_config
// library so the real-time core has no YAML dependency.
#pragma once

#include <string>

#include "maxmount/fieldbus/drive_descriptor.hpp"

namespace maxmount::config {

// Both throw std::runtime_error with a readable message on any error, including a descriptor
// that fails validate().
DriveDescriptor parse_drive_descriptor(const std::string& yaml_text);
DriveDescriptor load_drive_descriptor(const std::string& path);

}  // namespace maxmount::config
