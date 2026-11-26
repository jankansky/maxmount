# SPDX-License-Identifier: GPL-3.0-or-later
"""Imaging profiles in the system file: parsing, the earlier single-camera form, derived numbers."""
from pathlib import Path

import pytest
import yaml

from maxmount_svc.config import ConfigError, _imaging

REPO = Path(__file__).resolve().parents[2]


def test_reference_profiles():
    im = _imaging(yaml.safe_load((REPO / "config/systems/maxmount-reference/system.yaml").read_text()))
    assert im.default == "deep_sky_224"
    assert set(im.profiles) == {"deep_sky_224", "high_res_224", "high_res_224_barlow", "deep_sky_d500", "high_res_d500"}
    d = im.profiles["deep_sky_224"].summary()
    assert d["arcsec_per_px"] == pytest.approx(0.573, abs=0.002) and d["sampling_550"] == pytest.approx(2.05, abs=0.02)
    assert d["diffraction_arcsec"] == pytest.approx(0.558, abs=0.002) and d["fov_arcmin"] == [12.5, 9.3] and d["color"]
    assert im.profiles["high_res_224_barlow"].summary()["sampling_550"] < 1  # samples the diffraction limit
    assert im.profiles["deep_sky_d500"].summary()["fov_arcmin"] == [59.8, 39.9]


def test_legacy_single_camera_and_errors():
    im = _imaging({"imaging": {"narrow_pixel_um": 2.9, "narrow_focal_mm": 2032}})
    assert list(im.profiles) == ["default"] and im.profiles["default"].arcsec_per_px == pytest.approx(0.2944, abs=1e-3)
    assert _imaging({}).default == "default"
    with pytest.raises(ConfigError):
        _imaging({"imaging": {"profiles": {"x": {"pixel_um": 3.76}}}})              # no focal length
    with pytest.raises(ConfigError):
        _imaging({"imaging": {"default": "y", "profiles": {"x": {"pixel_um": 3.76, "focal_mm": 1000}}}})
