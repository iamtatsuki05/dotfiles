from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from copilot_prefix import (
    make_copilot_prefix,
    package_manifest,
    pinned_fake_copilot,
    platform_manifest,
    sdk_manifest,
    sha256,
    write_json,
)

from agent_team.native_acp_dependencies import (
    CodexAcpExecutables,
    CopilotAcpExecutables,
    NativeAcpDependencyError,
    NativeAcpExecutables,
    codex_adapter_snapshot,
    copilot_adapter_snapshot,
)

COPILOT_E1 = (
    "native Copilot ACP profile requires installed commands: {names}; install "
    "@github/copilot@1.0.91 and @agentclientprotocol/sdk@1.4.0 into one npm prefix "
    "(npm install --prefix DIR @github/copilot@1.0.91 "
    "@agentclientprotocol/sdk@1.4.0), then put DIR/node_modules/.bin and Node.js 22 "
    "or newer first on PATH"
)
COPILOT_E2 = (
    "selected copilot is not the @github/copilot@1.0.91 npm-loader.js: {path}; put "
    "DIR/node_modules/.bin first on PATH (another copilot, such as AWS Copilot, must "
    "not shadow it)"
)
COPILOT_E3 = (
    "selected @github/copilot@1.0.91 is required; install exactly "
    "@github/copilot@1.0.91 into the selected npm prefix"
)
COPILOT_E4 = (
    "native Copilot ACP profile is verified only on darwin-arm64; this host is {host}"
)
COPILOT_E5 = (
    "selected @github/copilot@1.0.91 requires @github/copilot-darwin-arm64@1.0.91"
)
COPILOT_E6 = (
    "selected @github/copilot-darwin-arm64@1.0.91 binary is not the verified build; "
    "reinstall @github/copilot@1.0.91 into the selected npm prefix"
)
COPILOT_E7 = (
    "native Copilot ACP client requires @agentclientprotocol/sdk@1.4.0 in the same "
    "npm prefix as @github/copilot@1.0.91"
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

    def make_codex_layout(self, root: Path) -> tuple[Path, Path, Path, Path]:
        bin_dir = root / "codex-bin"
        bin_dir.mkdir()
        node = bin_dir / "node"
        node.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        node.chmod(0o755)
        codex = bin_dir / "codex"
        codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        codex.chmod(0o755)

        package_root = root / "node_modules" / "@agentclientprotocol" / "codex-acp"
        package_dist = package_root / "dist"
        package_dist.mkdir(parents=True)
        (package_root / "package.json").write_text(
            json.dumps(
                {
                    "name": "@agentclientprotocol/codex-acp",
                    "version": "1.10.0",
                    "main": "dist/index.js",
                    "bin": {"codex-acp": "dist/index.js"},
                    "dependencies": {
                        "@agentclientprotocol/sdk": "^1.4.0",
                        "@openai/codex": "^0.153.3",
                    },
                }
            ),
            encoding="utf-8",
        )
        agent = package_dist / "index.js"
        agent.write_text("#!/usr/bin/env node\n", encoding="utf-8")
        agent.chmod(0o755)
        (bin_dir / "codex-acp").symlink_to(agent)

        sdk_root = package_root / "node_modules" / "@agentclientprotocol" / "sdk"
        sdk_dist = sdk_root / "dist"
        sdk_dist.mkdir(parents=True)
        (sdk_root / "package.json").write_text(
            json.dumps(
                {
                    "name": "@agentclientprotocol/sdk",
                    "version": "1.4.0",
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
        sdk.write_text("export const version = 1.4;\n", encoding="utf-8")

        outer_sdk_root = root / "node_modules" / "@agentclientprotocol" / "sdk"
        outer_sdk_dist = outer_sdk_root / "dist"
        outer_sdk_dist.mkdir(parents=True)
        (outer_sdk_root / "package.json").write_text(
            json.dumps(
                {
                    "name": "@agentclientprotocol/sdk",
                    "version": "1.3.0",
                    "main": "dist/acp.js",
                    "exports": {".": {"import": "./dist/acp.js"}},
                }
            ),
            encoding="utf-8",
        )
        (outer_sdk_dist / "acp.js").write_text(
            "export const version = 1.3;\n", encoding="utf-8"
        )
        return bin_dir, node, agent, sdk

    def test_codex_resolves_selected_path_dependencies_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir, node, agent, sdk = self.make_codex_layout(root)
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
                selected = CodexAcpExecutables.resolve(path=str(bin_dir))

        self.assertEqual(selected.node, node.resolve())
        self.assertEqual(selected.agent, agent.resolve())
        self.assertEqual(selected.sdk, sdk.resolve())
        self.assertEqual(selected.codex, (bin_dir / "codex").resolve())
        self.assertEqual(
            [call.args[0] for call in which.call_args_list],
            ["node", "codex-acp", "codex"],
        )

    def test_codex_missing_selected_commands_reports_pinned_versions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory))
            with (
                mock.patch(
                    "agent_team.native_acp_dependencies.shutil.which",
                    return_value=None,
                ) as which,
                self.assertRaisesRegex(
                    NativeAcpDependencyError,
                    "node.*codex-acp.*codex.*1.10.0.*1.4.0.*0.153.4",
                ),
            ):
                CodexAcpExecutables.resolve(path=path)

        self.assertEqual(
            which.call_args_list,
            [
                mock.call("node", path=path),
                mock.call("codex-acp", path=path),
                mock.call("codex", path=path),
            ],
        )

    def test_codex_drift_and_symlink_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = CodexAcpExecutables.resolve(
                path=str(self.make_codex_layout(root)[0])
            )
            selected.codex.write_text("changed\n", encoding="utf-8")
            with self.assertRaisesRegex(NativeAcpDependencyError, "codex changed"):
                selected.verify()

            selected.codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            outside = root / "outside-codex"
            outside.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            selected.codex.unlink()
            selected.codex.symlink_to(outside)
            with self.assertRaisesRegex(NativeAcpDependencyError, "canonical"):
                selected.verify()

    def test_codex_rejects_outer_sdk_1_3_and_manifest_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir, _, _, sdk = self.make_codex_layout(root)
            selected = CodexAcpExecutables.resolve(path=str(bin_dir))
            manifest = selected.agent.parents[1] / "package.json"
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["dependencies"]["@agentclientprotocol/sdk"] = "1.3.0"
            manifest.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(NativeAcpDependencyError, "requires"):
                selected.verify()

            self.assertEqual(selected.sdk, sdk.resolve())

    def test_codex_manifest_bytes_are_pinned_even_for_unrelated_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = CodexAcpExecutables.resolve(
                path=str(self.make_codex_layout(root)[0])
            )
            manifest = selected.agent.parents[1] / "package.json"
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["description"] = "drift"
            manifest.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(
                NativeAcpDependencyError, "agent manifest changed"
            ):
                selected.verify()

    def test_codex_manifest_owner_and_mode_are_verified(self) -> None:
        for manifest_name in ("agent", "sdk"):
            with (
                self.subTest(manifest_name=manifest_name),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                selected = CodexAcpExecutables.resolve(
                    path=str(self.make_codex_layout(root)[0])
                )
                manifest = (
                    selected.agent.parents[1] / "package.json"
                    if manifest_name == "agent"
                    else selected.sdk.parents[1] / "package.json"
                )
                manifest.chmod(0o666)
                with self.assertRaisesRegex(NativeAcpDependencyError, "writable"):
                    selected.verify()

    def test_codex_nearest_sdk_permission_error_does_not_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir, _, _, sdk = self.make_codex_layout(root)
            outer_manifest = (
                root / "node_modules" / "@agentclientprotocol" / "sdk" / "package.json"
            )
            outer_data = json.loads(outer_manifest.read_text(encoding="utf-8"))
            outer_data["version"] = "1.4.0"
            outer_manifest.write_text(json.dumps(outer_data), encoding="utf-8")
            nested_namespace = sdk.parents[2]
            nested_namespace.chmod(0o000)
            try:
                with self.assertRaisesRegex(
                    NativeAcpDependencyError, "dependency is unavailable"
                ):
                    CodexAcpExecutables.resolve(path=str(bin_dir))
            finally:
                nested_namespace.chmod(0o755)

    def test_codex_does_not_fallback_to_outer_sdk_1_3(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir, _, _, sdk = self.make_codex_layout(root)
            shutil.rmtree(sdk.parents[1])
            with self.assertRaisesRegex(
                NativeAcpDependencyError, "@agentclientprotocol/sdk@1.4.0"
            ):
                CodexAcpExecutables.resolve(path=str(bin_dir))

    def test_codex_round_trip_is_strict_and_snapshot_is_pinned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = CodexAcpExecutables.resolve(
                path=str(self.make_codex_layout(root)[0])
            )
            data = selected.as_dict()
            snapshot = codex_adapter_snapshot(selected)

        self.assertEqual(
            set(data),
            {
                "node",
                "agent",
                "sdk",
                "codex",
                "node_sha256",
                "agent_sha256",
                "sdk_sha256",
                "codex_sha256",
                "agent_manifest_sha256",
                "sdk_manifest_sha256",
            },
        )
        self.assertEqual(selected, CodexAcpExecutables.from_dict(data))
        self.assertEqual(snapshot["adapter_id"], "codex-acp-1.10.0")
        self.assertEqual(snapshot["revision"], "@agentclientprotocol/sdk@1.4.0")
        self.assertEqual(snapshot["version"], "@agentclientprotocol/codex-acp@1.10.0")
        for modified in (
            {**data, "library": data["agent"]},
            {key: value for key, value in data.items() if key != "sdk"},
            {**data, "node_sha256": "bad"},
            {**data, "codex": "relative/codex"},
            {**data, "agent_manifest_sha256": "bad"},
        ):
            with (
                self.subTest(modified=modified),
                self.assertRaises(NativeAcpDependencyError),
            ):
                CodexAcpExecutables.from_dict(modified)

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


class CopilotAcpDependenciesTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="agent-team-copilot-deps-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()

    def _resolve_error(self, path: str) -> str:
        with self.assertRaises(NativeAcpDependencyError) as raised:
            CopilotAcpExecutables.resolve(path=path)
        return str(raised.exception)

    def test_resolves_hoisted_and_nested_prefixes_without_execution(self) -> None:
        for hoisted in (True, False):
            with self.subTest(hoisted=hoisted):
                root = self.root / f"hoisted-{hoisted}"
                root.mkdir()
                prefix = make_copilot_prefix(root, hoisted=hoisted)
                with (
                    pinned_fake_copilot(prefix.binary),
                    mock.patch(
                        "subprocess.Popen",
                        side_effect=AssertionError("resolver must not run Copilot"),
                    ),
                    mock.patch(
                        "subprocess.run",
                        side_effect=AssertionError("resolver must not run Copilot"),
                    ),
                    mock.patch(
                        "agent_team.native_acp_dependencies.shutil.which",
                        wraps=shutil.which,
                    ) as which,
                ):
                    selected = CopilotAcpExecutables.resolve(path=prefix.path)
                    snapshot = copilot_adapter_snapshot(selected)

                self.assertEqual(
                    [call.args[0] for call in which.call_args_list], ["node", "copilot"]
                )
                self.assertEqual(selected.node, prefix.node)
                self.assertEqual(selected.loader, prefix.loader)
                self.assertEqual(selected.copilot, prefix.binary)
                self.assertEqual(selected.sdk, prefix.sdk)
                self.assertEqual(selected.copilot_sha256, sha256(prefix.binary))
                self.assertEqual(
                    selected.package_manifest_sha256,
                    sha256(prefix.package_root / "package.json"),
                )
                self.assertEqual(
                    selected.platform_manifest_sha256,
                    sha256(prefix.platform_root / "package.json"),
                )
                self.assertEqual(
                    selected.sdk_manifest_sha256,
                    sha256(prefix.sdk_root / "package.json"),
                )
                self.assertEqual(
                    snapshot,
                    {
                        "adapter_id": "copilot-acp-1.0.91",
                        "revision": "@agentclientprotocol/sdk@1.4.0",
                        "executable": str(prefix.binary),
                        "version": "@github/copilot@1.0.91",
                        "identity": {
                            "device": prefix.binary.stat().st_dev,
                            "inode": prefix.binary.stat().st_ino,
                            "size": prefix.binary.stat().st_size,
                            "mtime_ns": prefix.binary.stat().st_mtime_ns,
                            "sha256": sha256(prefix.binary),
                        },
                    },
                )

    def test_missing_commands_name_the_single_prefix_install(self) -> None:
        for found, names in (
            ((None, None), "node, copilot"),
            (("/usr/bin/node", None), "copilot"),
        ):
            with (
                self.subTest(names=names),
                mock.patch(
                    "agent_team.native_acp_dependencies.shutil.which",
                    side_effect=found,
                ) as which,
            ):
                message = self._resolve_error(str(self.root))
            self.assertEqual(message, COPILOT_E1.format(names=names))
            self.assertEqual(
                which.call_args_list,
                [
                    mock.call("node", path=str(self.root)),
                    mock.call("copilot", path=str(self.root)),
                ],
            )

    def test_another_copilot_earlier_on_path_is_rejected(self) -> None:
        prefix = make_copilot_prefix(self.root)
        aws = self.root / "aws-bin" / "copilot"
        aws.parent.mkdir()
        aws.write_text("#!/bin/sh\necho aws copilot\n", encoding="utf-8")
        aws.chmod(0o755)
        with pinned_fake_copilot(prefix.binary):
            message = self._resolve_error(f"{aws.parent}{os.pathsep}{prefix.path}")
        self.assertEqual(message, COPILOT_E2.format(path=aws))

    def test_package_manifest_must_be_exactly_1_0_91(self) -> None:
        optional = package_manifest()["optionalDependencies"]
        assert isinstance(optional, dict)
        for name, changes in (
            ("version", {"version": "1.0.90"}),
            ("name", {"name": "@github/copilot-cli"}),
            ("bin", {"bin": {"copilot": "index.js"}}),
            (
                "platform-pin",
                {
                    "optionalDependencies": {
                        **optional,
                        "@github/copilot-darwin-arm64": "^1.0.91",
                    }
                },
            ),
        ):
            with self.subTest(case=name):
                root = self.root / name
                root.mkdir()
                prefix = make_copilot_prefix(root)
                write_json(
                    prefix.package_root / "package.json", package_manifest(**changes)
                )
                with pinned_fake_copilot(prefix.binary):
                    self.assertEqual(self._resolve_error(prefix.path), COPILOT_E3)

    def test_only_darwin_arm64_hosts_are_verified(self) -> None:
        prefix = make_copilot_prefix(self.root)
        for host in (("linux", "x86_64"), ("darwin", "x86_64"), ("linux", "aarch64")):
            with self.subTest(host=host), pinned_fake_copilot(prefix.binary, host=host):
                self.assertEqual(
                    self._resolve_error(prefix.path),
                    COPILOT_E4.format(host="-".join(host)),
                )

    def test_platform_package_must_be_the_pinned_one_in_the_prefix(self) -> None:
        def remove(prefix_root: Path) -> None:
            shutil.rmtree(prefix_root)

        def wrong_version(prefix_root: Path) -> None:
            write_json(
                prefix_root / "package.json", platform_manifest(version="1.0.90")
            )

        def wrong_export(prefix_root: Path) -> None:
            write_json(
                prefix_root / "package.json",
                platform_manifest(exports={".": "./other"}),
            )

        def symlinked(prefix_root: Path) -> None:
            moved = prefix_root.parent / "moved-platform"
            prefix_root.rename(moved)
            prefix_root.symlink_to(moved)

        for name, change in (
            ("missing", remove),
            ("version", wrong_version),
            ("export", wrong_export),
            ("symlink", symlinked),
        ):
            with self.subTest(case=name):
                root = self.root / name
                root.mkdir()
                prefix = make_copilot_prefix(root)
                with pinned_fake_copilot(prefix.binary):
                    change(prefix.platform_root)
                    self.assertEqual(self._resolve_error(prefix.path), COPILOT_E5)

    def test_platform_package_outside_the_prefix_is_rejected(self) -> None:
        prefix = make_copilot_prefix(self.root)
        outside = self.root / "node_modules" / "@github" / "copilot-darwin-arm64"
        outside.parent.mkdir(parents=True)
        prefix.platform_root.rename(outside)
        with pinned_fake_copilot(outside / "copilot"):
            self.assertEqual(self._resolve_error(prefix.path), COPILOT_E5)

    def test_binary_must_be_the_verified_build(self) -> None:
        prefix = make_copilot_prefix(self.root)
        with (
            pinned_fake_copilot(prefix.binary),
            mock.patch(
                "agent_team.native_acp_dependencies._COPILOT_BINARY_SHA256", "0" * 64
            ),
        ):
            self.assertEqual(self._resolve_error(prefix.path), COPILOT_E6)

        with pinned_fake_copilot(prefix.binary):
            data = CopilotAcpExecutables.resolve(path=prefix.path).as_dict()
            data["copilot_sha256"] = "0" * 64
            with self.assertRaises(NativeAcpDependencyError) as raised:
                CopilotAcpExecutables.from_dict(data).verify()
        self.assertEqual(str(raised.exception), COPILOT_E6)

    def test_sdk_must_be_1_4_0_inside_the_selected_prefix(self) -> None:
        def remove(prefix_root: Path, root: Path) -> None:
            del root
            shutil.rmtree(prefix_root)

        def old_sdk(prefix_root: Path, root: Path) -> None:
            del root
            write_json(prefix_root / "package.json", sdk_manifest("1.3.0"))

        def symlinked(prefix_root: Path, root: Path) -> None:
            moved = root / "moved-sdk"
            prefix_root.rename(moved)
            prefix_root.symlink_to(moved)

        def above_prefix(prefix_root: Path, root: Path) -> None:
            outside = root / "node_modules" / "@agentclientprotocol" / "sdk"
            outside.parent.mkdir(parents=True)
            prefix_root.rename(outside)

        for name, change in (
            ("missing", remove),
            ("sdk-1.3.0", old_sdk),
            ("symlink", symlinked),
            ("above-prefix", above_prefix),
        ):
            with self.subTest(case=name):
                root = self.root / name
                root.mkdir()
                prefix = make_copilot_prefix(root)
                with pinned_fake_copilot(prefix.binary):
                    change(prefix.sdk_root, root)
                    self.assertEqual(self._resolve_error(prefix.path), COPILOT_E7)

    def test_each_binding_drift_is_reported(self) -> None:
        for label, target in (
            ("Node", lambda prefix: prefix.node),
            ("copilot", lambda prefix: prefix.binary),
            ("SDK", lambda prefix: prefix.sdk),
            ("package manifest", lambda prefix: prefix.package_root / "package.json"),
            (
                "platform manifest",
                lambda prefix: prefix.platform_root / "package.json",
            ),
            ("SDK manifest", lambda prefix: prefix.sdk_root / "package.json"),
        ):
            with self.subTest(label=label):
                root = self.root / label.replace(" ", "-")
                root.mkdir()
                prefix = make_copilot_prefix(root)
                with pinned_fake_copilot(prefix.binary):
                    selected = CopilotAcpExecutables.resolve(path=prefix.path)
                    path = target(prefix)
                    if path.name == "package.json":
                        data = json.loads(path.read_text(encoding="utf-8"))
                        data["description"] = "drift"
                        write_json(path, data)
                    else:
                        with path.open("a", encoding="utf-8") as handle:
                            handle.write("// drift\n")
                    with self.assertRaises(NativeAcpDependencyError) as raised:
                        selected.verify()
                self.assertEqual(
                    str(raised.exception),
                    f"selected native Copilot ACP {label} changed",
                )

    def test_round_trip_is_strict(self) -> None:
        prefix = make_copilot_prefix(self.root)
        with pinned_fake_copilot(prefix.binary):
            selected = CopilotAcpExecutables.resolve(path=prefix.path)
            data = selected.as_dict()
            self.assertEqual(selected, CopilotAcpExecutables.from_dict(data))
            CopilotAcpExecutables.from_dict(data).verify()
        self.assertEqual(
            set(data),
            {
                "node",
                "loader",
                "copilot",
                "sdk",
                "node_sha256",
                "copilot_sha256",
                "sdk_sha256",
                "package_manifest_sha256",
                "platform_manifest_sha256",
                "sdk_manifest_sha256",
            },
        )
        for modified in (
            {**data, "agent": data["copilot"]},
            {key: value for key, value in data.items() if key != "loader"},
            {**data, "sdk_sha256": "bad"},
            {**data, "copilot": "relative/copilot"},
            {**data, "platform_manifest_sha256": "A" * 64},
        ):
            with (
                self.subTest(modified=sorted(modified)),
                self.assertRaises(NativeAcpDependencyError),
            ):
                CopilotAcpExecutables.from_dict(modified)


if __name__ == "__main__":
    unittest.main()
