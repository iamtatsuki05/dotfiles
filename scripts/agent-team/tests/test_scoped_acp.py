from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_team import scoped_acp
from agent_team.native_acp_dependencies import (
    CodexAcpExecutables,
    CopilotAcpExecutables,
)
from agent_team.runtime import RuntimeValidationError
from agent_team.task_spec import TaskSpec, VerificationSpec


def _copilot_executables() -> CopilotAcpExecutables:
    return CopilotAcpExecutables(
        node=Path("/fixture/node"),
        loader=Path("/fixture/node_modules/@github/copilot/npm-loader.js"),
        copilot=Path("/fixture/copilot"),
        sdk=Path("/fixture/sdk/dist/acp.js"),
        node_sha256="1" * 64,
        copilot_sha256="2" * 64,
        sdk_sha256="3" * 64,
        package_manifest_sha256="4" * 64,
        platform_manifest_sha256="5" * 64,
        sdk_manifest_sha256="6" * 64,
    )


class ScopedAcpTest(unittest.TestCase):
    def test_native_profiles_bind_provider_role_permission_and_adapter(self) -> None:
        for provider in ("claude", "codex"):
            for role in ("planner", "worker", "reviewer"):
                with self.subTest(provider=provider, role=role):
                    profile = scoped_acp.native_profile(provider, role)
                    self.assertEqual(profile["provider"], provider)
                    self.assertEqual(profile["transport"], "acp")
                    self.assertEqual(profile["execution"], "background")
                    self.assertEqual(
                        profile["permission"],
                        "workspace-write" if role == "worker" else "read-only",
                    )
                    self.assertEqual(
                        profile["adapter_id"],
                        "codex-acp-scoped-1.10.0"
                        if provider == "codex"
                        else "claude-acp-scoped-0.70.0"
                        if role == "worker"
                        else "claude-acp-0.70.0",
                    )
        for provider, role in (("unknown", "worker"), ("codex", "main")):
            with (
                self.subTest(provider=provider, role=role),
                self.assertRaises(ValueError),
            ):
                scoped_acp.native_profile(provider, role)

    def test_selected_claude_config_dir_is_protected_and_bound_to_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            state = root / "state" / "state.json"
            state.parent.mkdir()
            private = root / "private"
            private.mkdir(mode=0o700)
            profile = root / "claude-profile"
            profile.mkdir(mode=0o700)
            wrapper = root / "host.mjs"
            wrapper.write_text("export {};\n")
            with (
                mock.patch.object(scoped_acp, "SCOPED_AGENT", wrapper),
                mock.patch.object(scoped_acp, "SCOPED_CLIENT", wrapper),
                mock.patch.object(scoped_acp, "SCOPED_POLICY", wrapper),
            ):
                path, digest = scoped_acp.create_write_policy(
                    private,
                    workspace,
                    state,
                    None,
                    wrapper,
                    permission="read-only",
                    claude_config_dir=profile,
                )
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.assertIn(str(profile), payload["protected_paths"])
                assignment = {
                    "provider_private_root": str(private),
                    "write_policy_path": str(path),
                    "write_policy_sha256": digest,
                    "question_socket": str(private / "q.sock"),
                }
                role_spec = {
                    "scoped_wrapper_sha256": scoped_acp.checked_digest(wrapper),
                    "scoped_client_sha256": scoped_acp.checked_digest(wrapper),
                    "scoped_policy_sha256": scoped_acp.checked_digest(wrapper),
                    "scoped_question_client_sha256": scoped_acp.checked_digest(
                        scoped_acp.SCOPED_QUESTIONS
                    ),
                    "permission": "read-only",
                    "acp_executables": {"agent": str(wrapper)},
                }
                saved = {
                    "workspace": str(workspace),
                    "state_path": str(state),
                    "claude_config_dir": str(profile),
                }
                self.assertEqual(
                    scoped_acp.validate_write_policy(saved, assignment, role_spec), path
                )
                without_selection = {
                    key: value
                    for key, value in saved.items()
                    if key != "claude_config_dir"
                }
                with self.assertRaisesRegex(ValueError, "does not match"):
                    scoped_acp.validate_write_policy(
                        without_selection, assignment, role_spec
                    )

    def test_policy_is_private_and_rejects_changed_scope_and_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            state = root / "state" / "state.json"
            state.parent.mkdir()
            private = root / "private"
            private.mkdir(mode=0o700)
            wrapper = root / "host.mjs"
            wrapper.write_text("export {};\n")
            shared_policy = root / "shared-policy.mjs"
            shared_policy.write_text("export {};\n")
            task = TaskSpec(
                "edit-one",
                "Edit one file",
                ("Only one file changes",),
                ("allowed.txt",),
                ("private/",),
                (),
                (VerificationSpec("test", ("python", "-V"), 10),),
                ("test result",),
                (),
            )
            with (
                mock.patch.object(scoped_acp, "SCOPED_AGENT", wrapper),
                mock.patch.object(scoped_acp, "SCOPED_CLIENT", wrapper),
                mock.patch.object(scoped_acp, "SCOPED_POLICY", shared_policy),
            ):
                path, digest = scoped_acp.create_write_policy(
                    private,
                    workspace,
                    state,
                    task,
                    wrapper,
                    permission="workspace-write",
                )
                assignment = {
                    "provider_private_root": str(private),
                    "write_policy_path": str(path),
                    "write_policy_sha256": digest,
                    "question_socket": str(private / "q.sock"),
                    "task_spec": task.as_dict(),
                }
                role_spec = {
                    "scoped_wrapper_sha256": scoped_acp.checked_digest(wrapper),
                    "scoped_client_sha256": scoped_acp.checked_digest(wrapper),
                    "scoped_policy_sha256": scoped_acp.checked_digest(shared_policy),
                    "scoped_question_client_sha256": scoped_acp.checked_digest(
                        scoped_acp.SCOPED_QUESTIONS
                    ),
                    "permission": "workspace-write",
                    "acp_executables": {"agent": str(wrapper)},
                }
                saved = {"workspace": str(workspace), "state_path": str(state)}
                self.assertEqual(
                    scoped_acp.validate_write_policy(saved, assignment, role_spec), path
                )
                shared_policy.write_text("export const changed = true;\n")
                with self.assertRaisesRegex(
                    ValueError, "shared policy changed since team start"
                ):
                    scoped_acp.validate_write_policy(saved, assignment, role_spec)
                shared_policy.write_text("export {};\n")
                missing_binding = dict(role_spec)
                del missing_binding["scoped_policy_sha256"]
                with self.assertRaisesRegex(
                    ValueError, "shared policy changed since team start"
                ):
                    scoped_acp.validate_write_policy(saved, assignment, missing_binding)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                data = json.loads(path.read_text())
                data["allowed_paths"] = ["outside.txt"]
                path.write_text(json.dumps(data))
                with self.assertRaisesRegex(ValueError, "changed since dispatch"):
                    scoped_acp.validate_write_policy(saved, assignment, role_spec)
                assignment["write_policy_sha256"] = scoped_acp.checked_digest(
                    path, private=True
                )
                with self.assertRaisesRegex(ValueError, "does not match.*TaskSpec"):
                    scoped_acp.validate_write_policy(saved, assignment, role_spec)
                path.unlink()
                path.symlink_to(wrapper)
                with self.assertRaisesRegex(ValueError, "unsafe file"):
                    scoped_acp.validate_write_policy(saved, assignment, role_spec)


class ScopedCopilotAcpTest(unittest.TestCase):
    def test_copilot_profile_is_an_internal_scoped_acp_profile(self) -> None:
        for role in ("planner", "worker", "reviewer"):
            with self.subTest(role=role):
                self.assertEqual(
                    scoped_acp.native_profile("copilot", role),
                    {
                        "provider": "copilot",
                        "transport": "acp",
                        "permission": (
                            "workspace-write" if role == "worker" else "read-only"
                        ),
                        "execution": "background",
                        "adapter_id": "copilot-acp-scoped-1.0.91",
                    },
                )
        with self.assertRaises(RuntimeValidationError):
            scoped_acp.native_profile("copilot", "main")

    def _client_argv(self, **changes: object) -> list[str]:
        arguments: dict[str, object] = {
            "harness": "copilot",
            "workspace": Path("/fixture/workspace"),
            "permission": "read-only",
            "model": "gpt-5.2",
            "effort": "high",
            "instructions": "fixture instructions",
            "timeout_seconds": 5,
            "result_file": Path("/fixture/private/client-result.json"),
            "launch_nonce": "planner1234",
            "policy": Path("/fixture/private/write-policy.json"),
            **changes,
        }
        executables = arguments.pop("executables", _copilot_executables())
        return scoped_acp.client_argv(
            executables,  # type: ignore[arg-type]
            "/fixture/copilot --acp --stdio",
            **arguments,  # type: ignore[arg-type]
        )

    def test_client_argv_binds_the_copilot_policy(self) -> None:
        with mock.patch.object(CopilotAcpExecutables, "verify") as verify:
            argv = self._client_argv()
        verify.assert_called_once_with()
        self.assertEqual(argv[argv.index("--harness") + 1], "copilot")
        self.assertEqual(
            argv[argv.index("--sdk-entry") + 1], "/fixture/sdk/dist/acp.js"
        )
        self.assertEqual(
            argv[argv.index("--policy") + 1], "/fixture/private/write-policy.json"
        )
        self.assertNotIn("--question-socket", argv)

    def test_client_argv_rejects_copilot_constraint_violations(self) -> None:
        codex = CodexAcpExecutables(
            node=Path("/fixture/node"),
            agent=Path("/fixture/codex-acp.js"),
            sdk=Path("/fixture/sdk.js"),
            codex=Path("/fixture/codex"),
            node_sha256="a" * 64,
            agent_sha256="b" * 64,
            sdk_sha256="c" * 64,
            codex_sha256="d" * 64,
            agent_manifest_sha256="e" * 64,
            sdk_manifest_sha256="f" * 64,
        )
        cases: tuple[tuple[str, dict[str, object]], ...] = (
            ("missing-policy", {"policy": None}),
            ("relative-policy", {"policy": Path("private/write-policy.json")}),
            ("other-policy-name", {"policy": Path("/fixture/private/policy.json")}),
            ("question-socket", {"question_socket": Path("/fixture/private/q.sock")}),
            ("codex-binding", {"executables": codex}),
            ("codex-policy", {"harness": "codex", "executables": codex}),
        )
        for name, changes in cases:
            with (
                self.subTest(case=name),
                mock.patch.object(CopilotAcpExecutables, "verify") as verify,
                mock.patch.object(CodexAcpExecutables, "verify"),
                self.assertRaises(RuntimeValidationError),
            ):
                self._client_argv(**changes)
            verify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
