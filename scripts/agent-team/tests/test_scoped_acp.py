from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_team import scoped_acp
from agent_team.task_spec import TaskSpec, VerificationSpec


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
                    "task_spec": task.as_dict(),
                }
                role_spec = {
                    "scoped_wrapper_sha256": scoped_acp.checked_digest(wrapper),
                    "scoped_client_sha256": scoped_acp.checked_digest(wrapper),
                    "scoped_policy_sha256": scoped_acp.checked_digest(shared_policy),
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


if __name__ == "__main__":
    unittest.main()
