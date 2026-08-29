from __future__ import annotations

import json
import unittest
from dataclasses import replace

from agent_team.probe_receipts import (
    BLOCKER_CODES,
    CURRENT_SCHEMA_VERSION,
    CleanupInventory,
    ExecutableIdentity,
    Manifest,
    PhaseReceipt,
    ProfileIdentity,
    Receipt,
    ReceiptValidationError,
    ToolEvidence,
    judge_profile,
    parse_manifest,
    parse_receipt,
    required_phases_for_profile,
    serialize_manifest,
    serialize_receipt,
)


def executable() -> ExecutableIdentity:
    return ExecutableIdentity(
        path="/opt/harness/bin/agent",
        version="1.2.3",
        sha256="a" * 64,
    )


def manifest(*, permission_profile: str = "read-only") -> Manifest:
    return Manifest(
        ProfileIdentity(
            schema_version=CURRENT_SCHEMA_VERSION,
            harness_id="opencode",
            permission_profile=permission_profile,
            os_name="linux",
            architecture="x86_64",
            probe_revision="probe-20260830",
            executable=executable(),
            argv_sha256="c" * 64,
            prompt_transport="argv",
            cwd="/tmp/probe-snapshot",
            environment_allowlist=("HOME", "PATH"),
            sandbox_policy_id="snapshot-readonly-v1",
        ),
        required_phases_for_profile(permission_profile),
    )


def evidence_for(phase_id: str) -> tuple[ToolEvidence, ...]:
    if phase_id.startswith("positive-"):
        return (
            ToolEvidence(
                tool="filesystem",
                operation="write" if phase_id == "positive-write" else "read",
                target="workspace",
                result="allowed",
            ),
        )
    if phase_id in {"outside-path", "symlink", "git", "secret"}:
        target = "outside" if phase_id == "outside-path" else phase_id
        return (
            ToolEvidence("filesystem", "read", target, "denied"),
            ToolEvidence("filesystem", "write", target, "denied"),
        )
    if phase_id in {"local-network", "external-network"}:
        return (ToolEvidence("network", "connect", phase_id, "denied"),)
    if phase_id == "process":
        return (ToolEvidence("process", "spawn", "process", "denied"),)
    return (ToolEvidence("cleanup", "inspect", "cleanup", "clean"),)


def phase_receipt(
    phase_id: str,
    expected_result: str,
    *,
    attempted: bool = True,
    tool_used: bool = True,
    outcome: str = "passed",
    exit_code: int | None = 0,
    timed_out: bool = False,
    cleanup: CleanupInventory | None = None,
) -> PhaseReceipt:
    return PhaseReceipt(
        phase_id=phase_id,
        expected_result=expected_result,
        attempted=attempted,
        tool_used=tool_used,
        outcome=outcome,
        exit_code=exit_code,
        timed_out=timed_out,
        evidence=evidence_for(phase_id) if tool_used else (),
        cleanup=cleanup or CleanupInventory(),
    )


def candidate_receipt(
    profile: str = "read-only", *, cleanup: CleanupInventory | None = None
) -> Receipt:
    expected = manifest(permission_profile=profile)
    return Receipt(
        expected.identity,
        None,
        tuple(
            phase_receipt(
                phase.phase_id,
                phase.expected_result,
                cleanup=cleanup,
            )
            for phase in expected.required_phases
        ),
    )


class ProbeReceiptContractTest(unittest.TestCase):
    def test_candidate_requires_the_complete_profile_matrix(self) -> None:
        result = judge_profile(manifest(), candidate_receipt())

        self.assertEqual(result.status, "candidate")
        self.assertEqual(result.reason_codes, ())
        self.assertEqual(
            tuple(phase.phase_id for phase in manifest().required_phases),
            (
                "positive-read",
                "outside-path",
                "symlink",
                "git",
                "secret",
                "local-network",
                "external-network",
                "process",
                "cleanup",
            ),
        )

    def test_workspace_write_uses_a_separate_positive_phase(self) -> None:
        profile_manifest = manifest(permission_profile="workspace-write")

        self.assertEqual(profile_manifest.required_phases[0].phase_id, "positive-write")
        self.assertEqual(
            judge_profile(
                profile_manifest, candidate_receipt("workspace-write")
            ).status,
            "candidate",
        )

    def test_missing_attempt_is_not_run_and_never_candidate(self) -> None:
        receipt = candidate_receipt()
        phases = list(receipt.phases)
        phases[0] = phase_receipt(
            phases[0].phase_id,
            phases[0].expected_result,
            attempted=False,
            tool_used=False,
            outcome="not-run",
            exit_code=None,
        )
        receipt = replace(receipt, phases=tuple(phases))

        result = judge_profile(manifest(), receipt)

        self.assertEqual(result.status, "not-run")
        self.assertIn("phase-not-attempted", result.reason_codes)

    def test_tool_missing_timeout_and_cleanup_residual_are_rejected(self) -> None:
        for field, replacement, reason in (
            (
                "tool_missing",
                phase_receipt(
                    "local-network",
                    "deny",
                    tool_used=False,
                    outcome="failed",
                    exit_code=1,
                ),
                "tool-not-used",
            ),
            (
                "timeout",
                phase_receipt(
                    "process",
                    "deny",
                    outcome="timeout",
                    exit_code=None,
                    timed_out=True,
                ),
                "phase-timeout",
            ),
            (
                "cleanup",
                phase_receipt(
                    "cleanup",
                    "clean",
                    cleanup=CleanupInventory(child_processes=1),
                ),
                "cleanup-residual",
            ),
        ):
            with self.subTest(field=field):
                receipt = candidate_receipt()
                phases = list(receipt.phases)
                index = next(
                    i
                    for i, phase in enumerate(phases)
                    if phase.phase_id == replacement.phase_id
                )
                phases[index] = replacement
                receipt = replace(receipt, phases=tuple(phases))
                result = judge_profile(manifest(), receipt)
                self.assertEqual(result.status, "rejected")
                self.assertIn(reason, result.reason_codes)

    def test_inconclusive_phase_is_known_but_never_a_candidate(self) -> None:
        receipt = candidate_receipt()
        phases = list(receipt.phases)
        phases[0] = replace(phases[0], outcome="inconclusive")

        result = judge_profile(manifest(), replace(receipt, phases=tuple(phases)))

        self.assertEqual(result.status, "rejected")
        self.assertIn("phase-inconclusive", result.reason_codes)

    def test_blocked_prerequisite_is_distinct_from_boundary_rejection(self) -> None:
        for blocker in BLOCKER_CODES:
            with self.subTest(blocker=blocker):
                expected = manifest()
                receipt = Receipt(
                    expected.identity,
                    blocker,
                    tuple(
                        phase_receipt(
                            phase.phase_id,
                            phase.expected_result,
                            attempted=False,
                            tool_used=False,
                            outcome="not-run",
                            exit_code=None,
                        )
                        for phase in expected.required_phases
                    ),
                )
                result = judge_profile(expected, receipt)
                self.assertEqual(result.status, "blocked")
                self.assertIn(f"blocked-{blocker}", result.reason_codes)

    def test_identity_revision_and_policy_mismatch_reject_stale_receipt(self) -> None:
        receipt = candidate_receipt()
        for field, identity, reason in (
            (
                "probe_revision",
                replace(receipt.identity, probe_revision="probe-old"),
                "probe-revision-mismatch",
            ),
            (
                "sandbox_policy_id",
                replace(receipt.identity, sandbox_policy_id="different-policy"),
                "sandbox-policy-mismatch",
            ),
            (
                "executable",
                replace(
                    receipt.identity,
                    executable=ExecutableIdentity(
                        "/opt/harness/bin/agent", "1.2.3", "b" * 64
                    ),
                ),
                "executable-identity-mismatch",
            ),
        ):
            with self.subTest(field=field):
                result = judge_profile(
                    manifest(),
                    replace(receipt, identity=identity),
                )
                self.assertEqual(result.status, "rejected")
                self.assertIn(reason, result.reason_codes)

    def test_manifest_and_receipt_serialization_is_deterministic_and_round_trips(
        self,
    ) -> None:
        expected_manifest = manifest()
        expected_receipt = candidate_receipt()
        manifest_json = serialize_manifest(expected_manifest)
        receipt_json = serialize_receipt(expected_receipt)

        self.assertEqual(
            manifest_json, serialize_manifest(parse_manifest(manifest_json))
        )
        self.assertEqual(receipt_json, serialize_receipt(parse_receipt(receipt_json)))
        self.assertEqual(manifest_json, serialize_manifest(expected_manifest))
        self.assertEqual(receipt_json, serialize_receipt(expected_receipt))
        self.assertEqual(
            json.loads(manifest_json)["schema_version"], CURRENT_SCHEMA_VERSION
        )

    def test_malformed_unknown_duplicate_and_contradictory_data_fails_fast(
        self,
    ) -> None:
        valid_manifest = json.loads(serialize_manifest(manifest()))
        with self.assertRaises(ReceiptValidationError):
            parse_manifest(json.dumps({**valid_manifest, "schema_version": 999}))
        with self.assertRaises(ReceiptValidationError):
            parse_manifest(json.dumps({**valid_manifest, "unknown": True}))
        with self.assertRaises(ReceiptValidationError):
            parse_manifest(
                json.dumps(
                    {
                        **valid_manifest,
                        "required_phases": [
                            *valid_manifest["required_phases"],
                            valid_manifest["required_phases"][0],
                        ],
                    }
                )
            )
        with self.assertRaises(ReceiptValidationError):
            parse_receipt(
                '{"artifact":"receipt","schema_version":1,"schema_version":1}'
            )

        valid_receipt = json.loads(serialize_receipt(candidate_receipt()))
        phase = valid_receipt["phases"][0]
        phase["attempted"] = False
        phase["outcome"] = "passed"
        with self.assertRaises(ReceiptValidationError):
            parse_receipt(json.dumps(valid_receipt))

        contradictory = json.loads(serialize_receipt(candidate_receipt()))
        contradictory["phases"][1]["evidence"][0]["result"] = "allowed"
        with self.assertRaises(ReceiptValidationError):
            parse_receipt(json.dumps(contradictory))

        for field, value in (
            ("tool", "filesystem"),
            ("operation", "read"),
            ("target", "workspace"),
        ):
            with self.subTest(field=field):
                wrong_phase = json.loads(serialize_receipt(candidate_receipt()))
                network = next(
                    phase
                    for phase in wrong_phase["phases"]
                    if phase["phase_id"] == "local-network"
                )
                network["evidence"][0][field] = value
                with self.assertRaises(ReceiptValidationError):
                    parse_receipt(json.dumps(wrong_phase))

    def test_sensitive_values_are_not_accepted_in_receipt_fields(self) -> None:
        manifest_with_key_name = replace(
            manifest().identity, environment_allowlist=("PATH", "OPENAI_API_KEY")
        )
        safe_manifest = Manifest(
            manifest_with_key_name,
            required_phases_for_profile("read-only"),
        )
        self.assertIn("OPENAI_API_KEY", serialize_manifest(safe_manifest))
        self.assertNotIn("sk-test-secret", serialize_manifest(safe_manifest))
        with self.assertRaises(ReceiptValidationError):
            replace(manifest().identity, argv_sha256="not-a-digest")
        payload = json.loads(serialize_manifest(manifest()))
        payload["fixed_argv"] = ["copilot", "-p", "prompt text"]
        with self.assertRaises(ReceiptValidationError):
            parse_manifest(json.dumps(payload))
        with self.assertRaises(ReceiptValidationError):
            parse_manifest(
                serialize_manifest(manifest()).replace(
                    '"sandbox_policy_id":"snapshot-readonly-v1"',
                    '"sandbox_policy_id":"raw-log=do-not-save"',
                )
            )
        with self.assertRaises(ReceiptValidationError):
            replace(manifest().identity, os_name="mac\ud800")

    def test_direct_construction_cannot_bypass_contract_validation(self) -> None:
        with self.assertRaises(ReceiptValidationError):
            ExecutableIdentity("relative/path", "1.0", "bad-digest")
        with self.assertRaises(ReceiptValidationError):
            CleanupInventory(child_processes=-1)
        with self.assertRaises(ReceiptValidationError):
            ToolEvidence("filesystem", "read", "outside", "unknown")

        valid = phase_receipt("outside-path", "deny")
        with self.assertRaises(ReceiptValidationError):
            replace(valid, attempted=False, outcome="not-run")
        expected = candidate_receipt()
        with self.assertRaises(ReceiptValidationError):
            Receipt(
                expected.identity,
                None,
                list(expected.phases),  # type: ignore[arg-type]
            )


if __name__ == "__main__":
    unittest.main()
