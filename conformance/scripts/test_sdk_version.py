#!/usr/bin/env python3
"""Tests for SDK version reporting helpers."""

from __future__ import annotations

import unittest

import sdk_version


class InstalledVersionTest(unittest.TestCase):
    def test_extracts_version_from_summary(self) -> None:
        original = sdk_version.VERSIONS["python"]
        sdk_version.VERSIONS["python"] = lambda: "pympp@0.9.1"
        try:
            self.assertEqual(sdk_version.installed_version("python"), "0.9.1")
        finally:
            sdk_version.VERSIONS["python"] = original

    def test_rejects_malformed_summary(self) -> None:
        original = sdk_version.VERSIONS["python"]
        sdk_version.VERSIONS["python"] = lambda: "pympp"
        try:
            with self.assertRaisesRegex(RuntimeError, "Could not parse SDK version"):
                sdk_version.installed_version("python")
        finally:
            sdk_version.VERSIONS["python"] = original


if __name__ == "__main__":
    unittest.main()
