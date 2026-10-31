from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar, cast
from unittest import mock

from agent_team import codex_acp
from agent_team.adapters import ExecutionError, ProcessResult
from agent_team.native_acp_dependencies import CodexAcpExecutables
from agent_team.runtime import RuntimeValidationError
from agent_team.scoped_acp import checked_digest
from agent_team.task_spec import TaskSpec, VerificationSpec


class _FakeRunner:
    result = ProcessResult(
        0, '{"configSnapshot":"' + "c" * 64 + '"}\n', "secret-stderr"
    )
    calls: ClassVar[list[dict[str, object]]] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    def run(
        self,
        argv: tuple[str, ...] | list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        input_text: str | None = None,
        timeout_seconds: float = 0.0,
    ) -> ProcessResult:
        self.calls.append(
            {
                "argv": list(argv),
                "cwd": cwd,
                "env": dict(env),
                "input_text": input_text,
                "timeout_seconds": timeout_seconds,
            }
        )
        return self.result


class CodexAcpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="agent-team-codex-acp-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.workspace = self.root / "repo" / "nested"
        self.workspace.mkdir(parents=True)
        (self.root / "repo" / ".git").mkdir()
        (self.root / "repo" / ".git" / "HEAD").write_text(
            "ref: refs/heads/fixture\n", encoding="utf-8"
        )
        (self.root / "repo" / ".codex").mkdir()
        (self.root / "repo" / ".codex" / "config.toml").write_text(
            'model = "fixture-model"\n', encoding="utf-8"
        )
        self.state_path = self.root / "state" / "state.json"
        self.state_path.parent.mkdir()
        self.auth_path = self.root / "normal-auth.json"
        self.auth_bytes = self._write_auth()
        self.runtime_root = self.root / "runtime"
        self.runtime_root.mkdir()
        self.runtime_files = {}
        for name in codex_acp.RUNTIME_FILENAMES:
            path = self.runtime_root / name
            path.write_text(f"fixture:{name}\n", encoding="utf-8")
            self.runtime_files[name] = path
        self.bin_dir, self.executables = self._make_codex_layout()
        self.runtime_patch = mock.patch.object(
            codex_acp,
            "_RUNTIME_FILES",
            {name: path for name, path in self.runtime_files.items()},
        )
        self.runtime_patch.start()
        self.addCleanup(self.runtime_patch.stop)

    @staticmethod
    def _token(payload: object) -> str:
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
        return "e30." + encoded.rstrip("=") + ".fixture-signature"

    def _write_auth(self) -> bytes:
        value = {
            "auth_mode": None,
            "OPENAI_API_KEY": None,
            "tokens": {
                "id_token": self._token(
                    {"https://api.openai.com/auth": {"chatgpt_plan_type": "pro"}}
                ),
                "access_token": self._token(
                    {"exp": int(__import__("time").time()) + 3_600}
                ),
                "refresh_token": "fixture-refresh-token",
                "account_id": "fixture-account",
            },
        }
        payload = json.dumps(value, separators=(",", ":")).encode()
        self.auth_path.write_bytes(payload)
        self.auth_path.chmod(0o600)
        return payload

    def _make_codex_layout(self) -> tuple[Path, CodexAcpExecutables]:
        bin_dir = self.root / "codex-bin"
        bin_dir.mkdir()
        node = bin_dir / "node"
        node.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        node.chmod(0o755)
        codex = bin_dir / "codex"
        codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        codex.chmod(0o755)
        package_root = self.root / "node_modules" / "@agentclientprotocol" / "codex-acp"
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
        selected = CodexAcpExecutables(
            node=node,
            agent=agent,
            sdk=sdk,
            codex=codex,
            node_sha256=hashlib.sha256(node.read_bytes()).hexdigest(),
            agent_sha256=hashlib.sha256(agent.read_bytes()).hexdigest(),
            sdk_sha256=hashlib.sha256(sdk.read_bytes()).hexdigest(),
            codex_sha256=hashlib.sha256(codex.read_bytes()).hexdigest(),
            agent_manifest_sha256=hashlib.sha256(
                (package_root / "package.json").read_bytes()
            ).hexdigest(),
            sdk_manifest_sha256=hashlib.sha256(
                (sdk_root / "package.json").read_bytes()
            ).hexdigest(),
        )
        return bin_dir, selected

    def _snapshot(self) -> dict[str, object]:
        return codex_acp.snapshot(self.workspace, self.auth_path)

    def _task(self) -> TaskSpec:
        return TaskSpec(
            "codex-task",
            "Update one fixture",
            ("The fixture remains valid",),
            ("allowed.txt",),
            ("private/",),
            (),
            (VerificationSpec("check", ("python", "-V"), 10),),
            ("check output",),
            (),
        )

    def _spec(self, provider_snapshot: dict[str, object]) -> dict[str, object]:
        return {
            "provider": "codex",
            "transport": "acp",
            "permission": "workspace-write",
            "execution": "background",
            "adapter_id": codex_acp.ADAPTER_ID,
            "model": "fixture-model",
            "effort": "high",
            "instructions": "fixture instructions",
            "acp_executables": self.executables.as_dict(),
            "provider_snapshot": provider_snapshot,
        }

    def _assignment(
        self, fields: dict[str, object], private_root: Path, task: TaskSpec
    ) -> dict[str, object]:
        return {
            **fields,
            "adapter_id": codex_acp.ADAPTER_ID,
            "agent_command": codex_acp.agent_command(self.executables),
            "provider_private_root": str(private_root),
            "task_spec": task.as_dict(),
        }

    def test_snapshot_binds_runtime_auth_and_project_layers(self) -> None:
        provider_snapshot = self._snapshot()
        self.assertEqual(
            set(provider_snapshot),
            {
                "runtime_sha256",
                "auth_path",
                "auth_sha256",
                "auth_expires_at",
                "project_configs",
            },
        )
        runtime = cast(dict[str, object], provider_snapshot["runtime_sha256"])
        self.assertEqual(set(runtime), set(self.runtime_files))
        self.assertEqual(provider_snapshot["auth_path"], str(self.auth_path))
        self.assertEqual(
            provider_snapshot["auth_sha256"],
            hashlib.sha256(self.auth_bytes).hexdigest(),
        )
        project_configs = cast(dict[str, object], provider_snapshot["project_configs"])
        self.assertIn(
            str(self.root / "repo" / ".codex" / "config.toml"), project_configs
        )
        self.assertNotIn("fixture-refresh-token", repr(provider_snapshot))

    def test_prepare_and_validate_round_trip_uses_inspection_only(self) -> None:
        provider_snapshot = self._snapshot()
        private_root = self.root / "private"
        private_root.mkdir(mode=0o700)
        _FakeRunner.calls.clear()
        with (
            mock.patch.object(codex_acp, "ProcessRunner", _FakeRunner),
            mock.patch.object(codex_acp, "assert_no_system_configuration"),
        ):
            task = self._task()
            fields = codex_acp.prepare_assignment(
                private_root,
                self.workspace,
                self.state_path,
                task,
                "workspace-write",
                self.executables,
                "fixture-model",
                "high",
                "fixture instructions",
                provider_snapshot,
            )
            assignment = self._assignment(fields, private_root, task)
            codex_acp.validate_assignment(
                {"workspace": str(self.workspace), "state_path": str(self.state_path)},
                assignment,
                self._spec(provider_snapshot),
            )
        self.assertEqual(self.auth_path.read_bytes(), self.auth_bytes)
        self.assertTrue((private_root / "codex-home" / "auth.json").is_symlink())
        self.assertEqual(
            os.readlink(private_root / "codex-home" / "auth.json"), str(self.auth_path)
        )
        self.assertEqual(len(_FakeRunner.calls), 1)
        self.assertEqual(_FakeRunner.calls[0]["input_text"], None)
        argv = cast(list[str], _FakeRunner.calls[0]["argv"])
        self.assertIn("inspect-config", argv)
        self.assertNotIn("fixture-refresh-token", repr(assignment))
        self.assertEqual(assignment["codex_config_snapshot"], "c" * 64)
        self.assertEqual(
            set(codex_acp.environment(private_root, self.executables)),
            {
                "HOME",
                "CODEX_HOME",
                "TMPDIR",
                "XDG_CONFIG_HOME",
                "XDG_DATA_HOME",
                "XDG_CACHE_HOME",
                "PATH",
                "CODEX_PATH",
            },
        )

    def test_invalid_permission_has_no_artifact_or_process_effect(self) -> None:
        provider_snapshot = self._snapshot()
        private_root = self.root / "private"
        private_root.mkdir(mode=0o700)
        with (
            mock.patch.object(codex_acp, "ProcessRunner") as runner,
            mock.patch.object(codex_acp, "assert_no_system_configuration"),
            self.assertRaises(RuntimeValidationError),
        ):
            codex_acp.prepare_assignment(
                private_root,
                self.workspace,
                self.state_path,
                self._task(),
                "bypass",
                self.executables,
                "fixture-model",
                "high",
                "fixture instructions",
                provider_snapshot,
            )
        runner.assert_not_called()
        self.assertEqual(list(private_root.iterdir()), [])

    def test_prepare_rejects_auth_drift_before_process(self) -> None:
        provider_snapshot = self._snapshot()
        self.auth_path.write_bytes(self.auth_bytes + b"drift")
        private_root = self.root / "private"
        private_root.mkdir(mode=0o700)
        runner = mock.Mock(side_effect=AssertionError("inspection must not run"))
        with (
            mock.patch.object(codex_acp, "ProcessRunner", runner),
            mock.patch.object(codex_acp, "assert_no_system_configuration"),
            self.assertRaises(RuntimeValidationError),
        ):
            codex_acp.prepare_assignment(
                private_root,
                self.workspace,
                self.state_path,
                self._task(),
                "workspace-write",
                self.executables,
                "fixture-model",
                "high",
                "fixture instructions",
                provider_snapshot,
            )
        runner.assert_not_called()

    def test_process_failure_preserves_cleanup_confirmation_without_stderr(
        self,
    ) -> None:
        provider_snapshot = self._snapshot()
        private_root = self.root / "private"
        private_root.mkdir(mode=0o700)

        class FailedRunner(_FakeRunner):
            def run(self, *args: object, **kwargs: object) -> ProcessResult:
                del args, kwargs
                raise ExecutionError("secret-stderr", cleanup_confirmed=True)

        with (
            mock.patch.object(codex_acp, "ProcessRunner", FailedRunner),
            mock.patch.object(codex_acp, "assert_no_system_configuration"),
            self.assertRaises(ExecutionError) as raised,
        ):
            codex_acp.prepare_assignment(
                private_root,
                self.workspace,
                self.state_path,
                self._task(),
                "workspace-write",
                self.executables,
                "fixture-model",
                "high",
                "fixture instructions",
                provider_snapshot,
            )
        self.assertTrue(raised.exception.cleanup_confirmed)
        self.assertNotIn("secret-stderr", str(raised.exception))

    def test_validate_rejects_unknown_manifest_field_and_policy_drift(self) -> None:
        provider_snapshot = self._snapshot()
        private_root = self.root / "private"
        private_root.mkdir(mode=0o700)
        with (
            mock.patch.object(codex_acp, "ProcessRunner", _FakeRunner),
            mock.patch.object(codex_acp, "assert_no_system_configuration"),
        ):
            task = self._task()
            fields = codex_acp.prepare_assignment(
                private_root,
                self.workspace,
                self.state_path,
                task,
                "workspace-write",
                self.executables,
                "fixture-model",
                "high",
                "fixture instructions",
                provider_snapshot,
            )
            assignment = self._assignment(fields, private_root, task)
            launch_path = Path(str(assignment["codex_launch_path"]))
            manifest = json.loads(launch_path.read_text(encoding="utf-8"))
            manifest["unknown"] = "fixture"
            launch_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(RuntimeValidationError):
                codex_acp.validate_assignment(
                    {
                        "workspace": str(self.workspace),
                        "state_path": str(self.state_path),
                    },
                    assignment,
                    self._spec(provider_snapshot),
                )

    def test_validate_rejects_policy_and_path_drift(self) -> None:
        provider_snapshot = self._snapshot()
        private_root = self.root / "private"
        private_root.mkdir(mode=0o700)
        with (
            mock.patch.object(codex_acp, "ProcessRunner", _FakeRunner),
            mock.patch.object(codex_acp, "assert_no_system_configuration"),
        ):
            task = self._task()
            fields = codex_acp.prepare_assignment(
                private_root,
                self.workspace,
                self.state_path,
                task,
                "workspace-write",
                self.executables,
                "fixture-model",
                "high",
                "fixture instructions",
                provider_snapshot,
            )
            assignment = self._assignment(fields, private_root, task)
            assignment["codex_launch_path"] = str(private_root / "other.json")
            with self.assertRaises(RuntimeValidationError):
                codex_acp.validate_assignment(
                    {
                        "workspace": str(self.workspace),
                        "state_path": str(self.state_path),
                    },
                    assignment,
                    self._spec(provider_snapshot),
                )
            assignment["codex_launch_path"] = str(private_root / "codex-launch.json")
            policy_path = Path(str(assignment["write_policy_path"]))
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            policy["allowed_paths"] = ["other.txt"]
            policy_path.write_text(json.dumps(policy), encoding="utf-8")
            assignment["write_policy_sha256"] = checked_digest(
                policy_path, private=True
            )
            with self.assertRaises(RuntimeValidationError):
                codex_acp.validate_assignment(
                    {
                        "workspace": str(self.workspace),
                        "state_path": str(self.state_path),
                    },
                    assignment,
                    self._spec(provider_snapshot),
                )


if __name__ == "__main__":
    unittest.main()
