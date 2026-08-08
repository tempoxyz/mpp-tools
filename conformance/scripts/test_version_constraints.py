#!/usr/bin/env python3
"""Tests for SemVer conformance scenario constraints."""

from __future__ import annotations

import unittest

from version_constraints import SemVer, matches_constraint


class VersionConstraintTest(unittest.TestCase):
    def test_comparators(self) -> None:
        cases = [
            ("1.2.3", "=1.2.3", True),
            ("1.2.3", "==1.2.3", True),
            ("1.2.3", "<1.2.4", True),
            ("1.2.3", "<=1.2.3", True),
            ("1.2.3", ">1.2.2", True),
            ("1.2.3", ">=1.2.3", True),
            ("1.2.3", ">1.2.3", False),
            ("1.2.3", ">=1.2.0, <2.0.0", True),
            ("1.2.3", ">=1.2.0 <1.2.3", False),
            ("v1.2.3", "=1.2.3", True),
            ("1.2.3+build.7", "=1.2.3", True),
            ("1.2.3-alpha.1", "<1.2.3", True),
            ("1.2.3-alpha.2", ">1.2.3-alpha.1", True),
            ("1.2.3-alpha", ">1.2.3-1", True),
        ]

        for version, constraint, expected in cases:
            with self.subTest(version=version, constraint=constraint):
                self.assertEqual(matches_constraint(version, constraint), expected)

    def test_invalid_versions(self) -> None:
        for version in ["1", "1.2", "1.02.3", "1.2.3-01", "not-a-version"]:
            with self.subTest(version=version):
                with self.assertRaisesRegex(ValueError, "Invalid SemVer version"):
                    SemVer.parse(version)

    def test_invalid_constraints(self) -> None:
        constraints = [
            "",
            "1.2.3",
            "=>1.2.3",
            ">=1.2",
            ", >=1.2.3",
            ">=1.2.3,",
        ]
        for constraint in constraints:
            with self.subTest(constraint=constraint):
                with self.assertRaisesRegex(ValueError, "constraint"):
                    matches_constraint("1.2.3", constraint)


if __name__ == "__main__":
    unittest.main()
