# SPDX-License-Identifier: GPL-3.0-or-later
from maxmount_svc import iers

# Two lines in IERS finals.daily.iau2000 format (Bulletin A, 2026-11-20 and -30).
SAMPLE = (
    "261120 61364.00 I  0.115426 0.000042  0.331354 0.000042  I-0.0765597 0.0000116\n"
    "261130 61374.00 P  0.107853 0.001668  0.337452 0.001540  P-0.0893990 0.0000279\n"
)


def test_parses_bulletin_a_columns():
    assert iers.parse(SAMPLE) == [(61364.0, -0.0765597), (61374.0, -0.0893990)]


def test_interpolates_and_clamps():
    rows = iers.parse(SAMPLE)
    assert abs(iers.interpolate(rows, 61369.0) - (-0.08297935)) < 1e-9
    assert iers.interpolate(rows, 61000.0) == -0.0765597
    assert iers.interpolate(rows, 62000.0) == -0.0893990


def test_does_not_interpolate_across_a_leap_second():
    rows = [(1.0, -0.6), (2.0, 0.4)]
    assert iers.interpolate(rows, 1.4) == -0.6 and iers.interpolate(rows, 1.9) == -0.6
