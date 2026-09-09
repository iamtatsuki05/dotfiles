from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

from agent_team import native_acp_dependencies, role_snapshot
from agent_team.contracts import ErrorCode, NodeRef, Role, RoleSpec, RuntimeFailure


class _FakeExecutables:
    calls: ClassVar[list[object]] = []

    def __init__(self, raw: object) -> None:
        self.raw = raw

    @classmethod
    def from_dict(cls, raw: object) -> _FakeExecutables:
        cls.calls.append(raw)
        return cls(raw)

    def verify(self) -> None:
        return None

    def as_dict(self) -> dict[str, object]:
        return {"selected": True}


class RoleSnapshotTest(unittest.TestCase):
    def _spec(self) -> RoleSpec:
        return RoleSpec(
            provider="codex",
            transport="acp",
            model="model",
            effort="high",
            permission="read-only",
            instructions="instructions",
            execution="background",
            adapter_id="codex-acp-scoped-1.10.0",
            acp_executables={"binding": {"nested": True}},
            scoped_wrapper_sha256="w" * 64,
            scoped_client_sha256="c" * 64,
            scoped_policy_sha256="p" * 64,
            scoped_question_client_sha256="q" * 64,
            provider_snapshot={"nested": {"value": "original"}},
        )

    def test_role_spec_snapshot_preserves_optional_fields_and_deep_copies_provider(
        self,
    ) -> None:
        spec = self._spec()
        snapshot = role_snapshot.role_spec_snapshot(
            spec, NodeRef("worker-a", Role.WORKER)
        )

        self.assertEqual(snapshot["provider"], "codex")
        self.assertEqual(snapshot["adapter_id"], "codex-acp-scoped-1.10.0")
        self.assertEqual(snapshot["acp_executables"], {"binding": {"nested": True}})
        self.assertIsNot(snapshot["provider_snapshot"], spec.provider_snapshot)
        copied = snapshot["provider_snapshot"]
        self.assertIsInstance(copied, dict)
        copied["nested"]["value"] = "changed"  # type: ignore[index]
        self.assertEqual(spec.provider_snapshot["nested"]["value"], "original")  # type: ignore[index]

    def test_role_spec_snapshot_rejects_incomplete_spec_with_role_context(self) -> None:
        with self.assertRaisesRegex(
            RuntimeFailure, "role spec is incomplete: planner"
        ) as raised:
            role_snapshot.role_spec_snapshot(
                RoleSpec(
                    provider="claude",
                    transport="acp",
                    model="",
                    effort="high",
                    permission="read-only",
                    instructions="instructions",
                    execution="background",
                ),
                Role.PLANNER,
            )
        self.assertIs(raised.exception.code, ErrorCode.INVALID_REQUEST)

    def test_preflight_false_validates_profile_and_required_binding_without_loading_it(
        self,
    ) -> None:
        normalized: dict[str, object] = {
            "provider": "claude",
            "transport": "acp",
            "model": "model",
            "effort": "high",
            "permission": "read-only",
            "instructions": "instructions",
            "execution": "background",
            "adapter_id": "claude-acp-0.70.0",
            "acp_executables": {"binding": "saved"},
        }
        with tempfile.TemporaryDirectory() as directory:
            result = role_snapshot.preflight_scoped_role(
                normalized,
                Role.PLANNER,
                Path(directory),
                preflight=False,
            )
        self.assertIsNone(result)
        self.assertEqual(normalized["acp_executables"], {"binding": "saved"})

    def test_preflight_requires_exact_profile_and_binding(self) -> None:
        normalized: dict[str, object] = {
            "provider": "claude",
            "transport": "direct",
            "model": "model",
            "effort": "high",
            "permission": "read-only",
            "instructions": "instructions",
            "execution": "background",
            "adapter_id": "claude-acp-scoped-0.70.0",
        }
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                RuntimeFailure, "does not match its scoped ACP profile"
            ) as profile_error:
                role_snapshot.preflight_scoped_role(
                    normalized, Role.PLANNER, Path(directory), preflight=False
                )
            self.assertIs(profile_error.exception.code, ErrorCode.INVALID_REQUEST)

            normalized["transport"] = "acp"
            normalized["adapter_id"] = "claude-acp-0.70.0"
            with self.assertRaisesRegex(
                RuntimeFailure, "is missing ACP executable bindings"
            ):
                role_snapshot.preflight_scoped_role(
                    normalized, Role.PLANNER, Path(directory), preflight=False
                )

    def test_codex_preflight_verifies_auth_snapshot_and_saves_selected_binding(
        self,
    ) -> None:
        normalized: dict[str, object] = {
            "provider": "codex",
            "transport": "acp",
            "model": "model",
            "effort": "high",
            "permission": "read-only",
            "instructions": "instructions",
            "execution": "background",
            "adapter_id": "codex-acp-scoped-1.10.0",
            "acp_executables": {"binding": "saved"},
            "provider_snapshot": {"auth": "snapshot"},
        }
        workspace = Path(tempfile.mkdtemp())
        _FakeExecutables.calls.clear()
        with (
            mock.patch.object(
                native_acp_dependencies,
                "CodexAcpExecutables",
                _FakeExecutables,
            ),
            mock.patch.object(
                native_acp_dependencies,
                "codex_adapter_snapshot",
                return_value={"adapter": "snapshot"},
            ) as adapter_snapshot,
            mock.patch("agent_team.codex_acp.verify_snapshot") as verify_snapshot,
        ):
            selected = role_snapshot.preflight_scoped_role(
                normalized, Role.PLANNER, workspace
            )

        self.assertIsInstance(selected, _FakeExecutables)
        self.assertEqual(_FakeExecutables.calls, [{"binding": "saved"}])
        verify_snapshot.assert_called_once_with(
            normalized["provider_snapshot"], workspace
        )
        adapter_snapshot.assert_called_once_with(selected)
        self.assertEqual(normalized["acp_executables"], {"selected": True})

    def test_claude_preflight_saves_all_four_scoped_digests(self) -> None:
        normalized: dict[str, object] = {
            "provider": "claude",
            "transport": "acp",
            "model": "model",
            "effort": "high",
            "permission": "workspace-write",
            "instructions": "instructions",
            "execution": "background",
            "adapter_id": "claude-acp-scoped-0.70.0",
            "acp_executables": {"binding": "saved"},
        }
        workspace = Path(tempfile.mkdtemp())
        _FakeExecutables.calls.clear()
        with (
            mock.patch.object(
                native_acp_dependencies,
                "NativeAcpExecutables",
                _FakeExecutables,
            ),
            mock.patch.object(
                native_acp_dependencies,
                "adapter_snapshot",
                return_value={"adapter": "snapshot"},
            ) as adapter_snapshot,
            mock.patch.object(
                role_snapshot,
                "checked_digest",
                side_effect=["w" * 64, "c" * 64, "p" * 64, "q" * 64],
            ),
        ):
            selected = role_snapshot.preflight_scoped_role(
                normalized, Role.WORKER, workspace
            )

        self.assertIsInstance(selected, _FakeExecutables)
        adapter_snapshot.assert_called_once_with(selected)
        self.assertEqual(normalized["acp_executables"], {"selected": True})
        self.assertEqual(normalized["scoped_wrapper_sha256"], "w" * 64)
        self.assertEqual(normalized["scoped_client_sha256"], "c" * 64)
        self.assertEqual(normalized["scoped_policy_sha256"], "p" * 64)
        self.assertEqual(normalized["scoped_question_client_sha256"], "q" * 64)


if __name__ == "__main__":
    unittest.main()
