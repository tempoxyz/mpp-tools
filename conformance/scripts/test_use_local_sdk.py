#!/usr/bin/env python3
"""Tests for local SDK adapter configuration."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from use_local_sdk import configure_go, go_sdk_version


class GoSdkVersionTest(unittest.TestCase):
    def test_reads_declared_checkout_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sdk_path = Path(tmp)
            (sdk_path / "go.mod").write_text(
                "// changelogs:version 0.3.0\nmodule github.com/tempoxyz/mpp-go\n",
                encoding="utf-8",
            )

            self.assertEqual(go_sdk_version(sdk_path), "v0.3.0")

    def test_requires_declared_checkout_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sdk_path = Path(tmp)
            (sdk_path / "go.mod").write_text(
                "module github.com/tempoxyz/mpp-go\n", encoding="utf-8"
            )

            with self.assertRaisesRegex(RuntimeError, "changelogs:version"):
                go_sdk_version(sdk_path)

    def test_configures_adapter_requirement_to_checkout_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conformance_dir = root / "conformance"
            adapter_dir = conformance_dir / "adapters" / "go"
            adapter_dir.mkdir(parents=True)
            sdk_path = root / "sdk"
            sdk_path.mkdir()
            (sdk_path / "go.mod").write_text(
                "// changelogs:version 0.3.0\nmodule github.com/tempoxyz/mpp-go\n",
                encoding="utf-8",
            )
            completed = subprocess.CompletedProcess([], 0, "", "")

            with patch("use_local_sdk.subprocess.run", return_value=completed) as run:
                configure_go(conformance_dir, sdk_path)

            edit_command = run.call_args_list[0].args[0]
            self.assertIn(
                "github.com/tempoxyz/mpp-go@v0.3.0",
                edit_command,
            )
            self.assertIn(
                f"github.com/tempoxyz/mpp-go={sdk_path}",
                edit_command,
            )


if __name__ == "__main__":
    unittest.main()
