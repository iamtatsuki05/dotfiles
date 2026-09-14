from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_team.acp_dependencies import AcpDependencyError, AcpExecutables


class AcpDependenciesTest(unittest.TestCase):
    def make_binaries(self, root: Path) -> Path:
        bin_dir = root / "bin"
        bin_dir.mkdir()
        node = bin_dir / "node"
        node.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        node.chmod(0o700)
        for name, version, command in (
            ("acpx", "0.13.2", "acpx"),
            (
                "@agentclientprotocol/claude-agent-acp",
                "0.70.0",
                "claude-agent-acp",
            ),
        ):
            package = root / "node_modules" / name
            (package / "dist").mkdir(parents=True)
            (package / "package.json").write_text(
                json.dumps(
                    {
                        "name": name,
                        "version": version,
                        "bin": {command: "dist/cli.js"},
                    }
                ),
                encoding="utf-8",
            )
            entry = package / "dist" / "cli.js"
            entry.write_text("#!/usr/bin/env node\n", encoding="utf-8")
            entry.chmod(0o755)
            (bin_dir / command).symlink_to(entry)
        return bin_dir

    def test_resolves_only_selected_installed_programs_without_running_them(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = self.make_binaries(root)
            with mock.patch(
                "subprocess.Popen",
                side_effect=AssertionError("dependency resolution must not execute"),
            ):
                selected = AcpExecutables.resolve(path=str(bin_dir))
                restored = AcpExecutables.from_dict(selected.as_dict())
                restored.verify()
            self.assertEqual(selected.node, (bin_dir / "node").resolve())
            self.assertEqual(
                selected.client,
                (root / "node_modules/acpx/dist/cli.js").resolve(),
            )
            self.assertEqual(selected, restored)

    def test_missing_program_fails_without_installation_or_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                mock.patch(
                    "subprocess.Popen",
                    side_effect=AssertionError("must not run npm or another program"),
                ),
                self.assertRaisesRegex(AcpDependencyError, "node.*acpx"),
            ):
                AcpExecutables.resolve(path=str(root))
            self.assertEqual(list(root.iterdir()), [])

    def test_package_version_and_entrypoint_must_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = self.make_binaries(root)
            manifest = root / "node_modules/acpx/package.json"
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["version"] = "0.0.0"
            manifest.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(AcpDependencyError, "acpx@0.13.2"):
                AcpExecutables.resolve(path=str(bin_dir))
            data["version"] = "0.13.2"
            data["bin"]["acpx"] = "dist/other.js"
            manifest.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(AcpDependencyError, "entrypoint"):
                AcpExecutables.resolve(path=str(bin_dir))

    def test_changed_executable_cannot_be_reused_from_a_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = AcpExecutables.resolve(path=str(self.make_binaries(root)))
            selected.agent.write_text("changed executable", encoding="utf-8")
            with self.assertRaisesRegex(AcpDependencyError, "changed"):
                selected.verify()

    def test_shared_writable_program_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = self.make_binaries(root)
            os.chmod(bin_dir / "node", 0o777)
            with self.assertRaisesRegex(AcpDependencyError, "writable"):
                AcpExecutables.resolve(path=str(bin_dir))

    def test_malformed_saved_path_uses_dependency_error_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = AcpExecutables.resolve(path=str(self.make_binaries(root)))
            for suffix in ("\0", "\ud800"):
                with self.subTest(suffix=repr(suffix)):
                    data = selected.as_dict()
                    data["node"] = str(root / "bad") + suffix
                    with self.assertRaises(AcpDependencyError):
                        AcpExecutables.from_dict(data).verify()

    def test_malformed_manifest_path_uses_dependency_error_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = self.make_binaries(root)
            manifest = root / "node_modules/acpx/package.json"
            data = json.loads(manifest.read_text(encoding="utf-8"))
            for suffix in ("\0", "\ud800"):
                with self.subTest(suffix=repr(suffix)):
                    data["bin"]["acpx"] = "dist/cli.js" + suffix
                    manifest.write_text(json.dumps(data), encoding="utf-8")
                    with self.assertRaises(AcpDependencyError):
                        AcpExecutables.resolve(path=str(bin_dir))


if __name__ == "__main__":
    unittest.main()
