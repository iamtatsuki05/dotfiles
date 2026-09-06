from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_team.native_acp_dependencies import (
    NativeAcpDependencyError,
    NativeAcpExecutables,
)


class NativeAcpDependenciesTest(unittest.TestCase):
    def make_layout(self, root: Path) -> tuple[Path, Path, Path]:
        bin_dir = root / "bin"
        bin_dir.mkdir()
        node = bin_dir / "node"
        node.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        node.chmod(0o755)

        packages = root / "node_modules" / "@agentclientprotocol"
        agent_root = packages / "claude-agent-acp"
        agent_dist = agent_root / "dist"
        agent_dist.mkdir(parents=True)
        (agent_root / "package.json").write_text(
            json.dumps(
                {
                    "name": "@agentclientprotocol/claude-agent-acp",
                    "version": "0.70.0",
                    "bin": {"claude-agent-acp": "dist/index.js"},
                    "dependencies": {"@agentclientprotocol/sdk": "1.3.0"},
                    "exports": {".": {"import": "./dist/lib.js"}},
                }
            ),
            encoding="utf-8",
        )
        agent = agent_dist / "index.js"
        agent.write_text("#!/usr/bin/env node\n", encoding="utf-8")
        agent.chmod(0o755)
        (agent_dist / "lib.js").write_text(
            "export const runAcp = () => ({})\n", encoding="utf-8"
        )

        sdk_root = packages / "sdk"
        sdk_dist = sdk_root / "dist"
        sdk_dist.mkdir(parents=True)
        (sdk_root / "package.json").write_text(
            json.dumps(
                {
                    "name": "@agentclientprotocol/sdk",
                    "version": "1.3.0",
                    "main": "dist/acp.js",
                    "exports": {
                        ".": {
                            "import": "./dist/acp.js",
                            "default": "./dist/acp.js",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        sdk = sdk_dist / "acp.js"
        sdk.write_text("export const version = 1.3;\n", encoding="utf-8")
        sdk.chmod(0o644)

        (bin_dir / "claude-agent-acp").symlink_to(agent)
        return bin_dir, node, sdk

    def test_resolves_node_agent_and_sdk_without_acpx_or_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir, node, sdk = self.make_layout(root)
            with (
                mock.patch(
                    "subprocess.Popen",
                    side_effect=AssertionError("resolver must not execute programs"),
                ),
                mock.patch(
                    "agent_team.native_acp_dependencies.shutil.which",
                    wraps=shutil.which,
                ) as which,
            ):
                selected = NativeAcpExecutables.resolve(path=str(bin_dir))

        self.assertEqual(selected.node, node.resolve())
        self.assertEqual(
            selected.agent,
            (
                node.parent.parent
                / "node_modules"
                / "@agentclientprotocol"
                / "claude-agent-acp"
                / "dist"
                / "index.js"
            ).resolve(),
        )
        self.assertEqual(selected.sdk, sdk.resolve())
        self.assertEqual(selected.library, selected.agent.parent / "lib.js")
        self.assertEqual(
            [call.args[0] for call in which.call_args_list],
            ["node", "claude-agent-acp"],
        )

    def test_missing_selected_commands_only_probes_node_and_claude_agent_acp(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory))
            with (
                mock.patch(
                    "agent_team.native_acp_dependencies.shutil.which",
                    return_value=None,
                ) as which,
                self.assertRaisesRegex(
                    NativeAcpDependencyError,
                    "native Claude ACP profile.*node.*claude-agent-acp.*install",
                ),
            ):
                NativeAcpExecutables.resolve(path=path)

        self.assertEqual(
            which.call_args_list,
            [mock.call("node", path=path), mock.call("claude-agent-acp", path=path)],
        )

    def test_changed_sdk_bytes_are_rejected_by_saved_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = NativeAcpExecutables.resolve(path=str(self.make_layout(root)[0]))
            selected.sdk.write_text("changed\n", encoding="utf-8")
            with self.assertRaisesRegex(NativeAcpDependencyError, "changed"):
                selected.verify()

    def test_changed_agent_library_bytes_are_rejected_by_saved_fingerprint(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = NativeAcpExecutables.resolve(path=str(self.make_layout(root)[0]))
            selected.library.write_text("changed library\n", encoding="utf-8")
            with self.assertRaisesRegex(NativeAcpDependencyError, "library changed"):
                selected.verify()

    def test_agent_library_symlink_is_rejected_by_saved_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = NativeAcpExecutables.resolve(path=str(self.make_layout(root)[0]))
            outside = root / "outside-lib.js"
            outside.write_text("export const untrusted = true\n", encoding="utf-8")
            selected.library.unlink()
            selected.library.symlink_to(outside)
            with self.assertRaisesRegex(NativeAcpDependencyError, "canonical"):
                selected.verify()

    def test_saved_agent_library_must_be_the_package_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = NativeAcpExecutables.resolve(path=str(self.make_layout(root)[0]))
            outside = root / "outside-lib.js"
            outside.write_bytes(selected.library.read_bytes())
            data = selected.as_dict()
            data["library"] = str(outside.resolve())
            with self.assertRaisesRegex(NativeAcpDependencyError, "sibling library"):
                NativeAcpExecutables.from_dict(data).verify()

    def test_changed_sdk_package_version_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir, _, _ = self.make_layout(root)
            selected = NativeAcpExecutables.resolve(path=str(bin_dir))
            manifest = selected.sdk.parents[1] / "package.json"
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["version"] = "1.4.0"
            manifest.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(
                NativeAcpDependencyError, "@agentclientprotocol/sdk@1.3.0"
            ):
                selected.verify()

    def test_sdk_must_be_readable_but_need_not_be_executable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = NativeAcpExecutables.resolve(path=str(self.make_layout(root)[0]))
            self.assertFalse(os.access(selected.sdk, os.X_OK))
            selected.verify()

    def test_resolution_does_not_import_package_or_use_network_or_install(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir, _, _ = self.make_layout(root)
            with (
                mock.patch(
                    "subprocess.run",
                    side_effect=AssertionError("resolver must not run Node"),
                ),
                mock.patch(
                    "subprocess.Popen",
                    side_effect=AssertionError("resolver must not run npm"),
                ),
            ):
                selected = NativeAcpExecutables.resolve(path=str(bin_dir))
            self.assertEqual(selected.sdk.name, "acp.js")

    def test_round_trip_has_exact_metadata_and_rejects_old_or_extra_fields(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = NativeAcpExecutables.resolve(path=str(self.make_layout(root)[0]))
            data = selected.as_dict()

        self.assertEqual(
            set(data),
            {
                "node",
                "agent",
                "sdk",
                "library",
                "node_sha256",
                "agent_sha256",
                "sdk_sha256",
                "library_sha256",
            },
        )
        self.assertEqual(selected, NativeAcpExecutables.from_dict(data))
        for modified in (
            {**data, "client": data["agent"]},
            {key: value for key, value in data.items() if key != "sdk"},
            {**data, "unexpected": "value"},
        ):
            with (
                self.subTest(modified=modified),
                self.assertRaises(NativeAcpDependencyError),
            ):
                NativeAcpExecutables.from_dict(modified)


if __name__ == "__main__":
    unittest.main()
