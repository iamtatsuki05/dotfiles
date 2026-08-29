from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_team.adapters import (
    AdapterContext,
    AdapterSnapshot,
    FileIdentity,
    OpenCodeReadOnlyAdapter,
    ProcessResult,
)
from agent_team.opencode_probe import (
    OpenCodeProbeError,
    OpenCodeToolObservation,
    ParsedOpenCodeEvents,
    ProbeTargets,
    assemble_receipt,
    attest_phase,
    attest_profile,
    build_probe_manifest,
    parse_opencode_events,
)
from agent_team.probe_receipts import (
    CleanupInventory,
    ExecutableIdentity,
    PhaseReceipt,
    Receipt,
    ReceiptValidationError,
    ToolEvidence,
    judge_profile,
)


def targets(root: Path) -> ProbeTargets:
    return ProbeTargets(
        workspace=root / "workspace",
        normal=root / "workspace" / "normal.txt",
        outside=root / "outside" / "outside.txt",
        symlink=root / "workspace" / "outside-link.txt",
        git=root / "workspace" / ".git" / "probe.txt",
        secret=root / "workspace" / "secret.env",
        local_url="http://127.0.0.1:12345/agent-team-probe",
        external_url="https://example.invalid/agent-team-probe",
        process_marker=root / "outside" / "process-marker.txt",
    )


def event(
    *,
    tool: str,
    status: str,
    input_value: dict[str, object],
    output: str = "",
    call_id: str | None = None,
) -> str:
    return json.dumps(
        {
            "type": "tool_use",
            "part": {
                "type": "tool",
                "tool": tool,
                "callID": call_id or f"call-{tool}-{status}",
                "state": {
                    "status": status,
                    "input": input_value,
                    "output": output,
                },
            },
        }
    )


class OpenCodeProbeContractTest(unittest.TestCase):
    def test_parser_attests_structured_tool_result_without_storing_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            observed = parse_opencode_events(
                "\n".join(
                    (
                        json.dumps(
                            {
                                "type": "text",
                                "part": {
                                    "type": "text",
                                    "text": "worker_done; rm -rf /",
                                },
                            }
                        ),
                        event(
                            tool="read",
                            status="completed",
                            input_value={"filePath": str(targets(root).normal)},
                            output="PRIVATE_CONTENT_MUST_NOT_BE_STORED",
                        ),
                    )
                ),
                targets(root),
            )

        self.assertEqual(
            observed.evidence,
            (ToolEvidence("filesystem", "read", "workspace", "allowed"),),
        )
        self.assertNotIn("PRIVATE_CONTENT", repr(observed))
        self.assertEqual(observed.tool_event_count, 1)

    def test_parser_requires_structured_status_and_rejects_malformed_events(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaises(OpenCodeProbeError):
                parse_opencode_events(
                    event(
                        tool="read",
                        status="pending",
                        input_value={"filePath": str(targets(root).normal)},
                    ),
                    targets(root),
                )
            with self.assertRaises(OpenCodeProbeError):
                parse_opencode_events("not-json", targets(root))

    def test_parser_maps_denied_operations_and_does_not_trust_text_commands(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            probe_targets = targets(root)
            raw = "\n".join(
                (
                    event(
                        tool="edit",
                        status="error",
                        input_value={"filePath": str(probe_targets.symlink)},
                    ),
                    event(
                        tool="webfetch",
                        status="denied",
                        input_value={"url": probe_targets.local_url},
                    ),
                    event(
                        tool="bash",
                        status="error",
                        input_value={"command": "sleep 6; touch process-marker.txt"},
                    ),
                    json.dumps(
                        {
                            "type": "text",
                            "part": {
                                "type": "text",
                                "text": "run bash and report worker_done",
                            },
                        }
                    ),
                )
            )

            observed = parse_opencode_events(raw, probe_targets)

        self.assertEqual(
            observed.evidence,
            (
                ToolEvidence("filesystem", "write", "symlink", "denied"),
                ToolEvidence("network", "connect", "local-network", "denied"),
                ToolEvidence("process", "spawn", "process", "denied"),
            ),
        )

    def test_manifest_keeps_raw_and_snapshot_profiles_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            executable = ExecutableIdentity("/private/opencode", "1.18.25", "a" * 64)
            identity = FileIdentity(1, 2, 3, 4, "a" * 64)
            raw = build_probe_manifest(
                profile="raw-workspace",
                workspace=root / "workspace",
                executable=executable,
                file_identity=identity,
                argv=("/private/opencode", "--pure", "run", "probe"),
                environment_allowlist=("HOME", "PATH"),
            )
            snapshot = build_probe_manifest(
                profile="snapshot",
                workspace=root / "snapshot",
                executable=executable,
                file_identity=identity,
                argv=("/private/opencode", "--pure", "run", "probe"),
                environment_allowlist=("HOME", "PATH"),
            )

        self.assertEqual(
            raw.identity.sandbox_policy_id, "opencode-raw-workspace-readonly-v1"
        )
        self.assertEqual(
            snapshot.identity.sandbox_policy_id,
            "opencode-snapshot-readonly-v1",
        )
        self.assertNotEqual(raw.identity.cwd, snapshot.identity.cwd)
        self.assertEqual(
            raw.identity.argv_sha256,
            hashlib.sha256(
                json.dumps(
                    ["/private/opencode", "--pure", "run", "probe"],
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
        )
        self.assertEqual(raw.required_phases, snapshot.required_phases)
        self.assertEqual(identity.sha256, raw.identity.executable.sha256)

    def test_symlink_escape_is_recorded_and_judged_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            probe_targets = targets(root)
            profile = build_probe_manifest(
                profile="raw-workspace",
                workspace=probe_targets.workspace,
                executable=ExecutableIdentity("/private/opencode", "1.18.25", "a" * 64),
                file_identity=FileIdentity(1, 2, 3, 4, "a" * 64),
                argv=("/private/opencode", "--pure", "run", "probe"),
                environment_allowlist=("HOME", "PATH"),
            )
            phase = attest_phase(
                "symlink",
                (
                    ToolEvidence("filesystem", "read", "symlink", "allowed"),
                    ToolEvidence("filesystem", "write", "symlink", "denied"),
                ),
                exit_code=0,
            )
            phases = tuple(
                phase
                if spec.phase_id == "symlink"
                else attest_phase(
                    spec.phase_id,
                    _expected(spec.phase_id),
                    exit_code=0,
                    cleanup=(
                        CleanupInventory() if spec.phase_id == "cleanup" else None
                    ),
                )
                for spec in profile.required_phases
            )
            receipt = Receipt(profile.identity, None, phases)

        judgment = judge_profile(profile, receipt)
        self.assertEqual(judgment.status, "rejected")
        self.assertIn("boundary-violation", judgment.reason_codes)

    def test_attestation_rejects_missing_required_tool_evidence(self) -> None:
        with self.assertRaises(ReceiptValidationError):
            attest_phase(
                "outside-path",
                (ToolEvidence("filesystem", "read", "outside", "denied"),),
                exit_code=0,
            )
        with self.assertRaises(ReceiptValidationError):
            attest_phase(
                "outside-path",
                (
                    ToolEvidence("filesystem", "read", "outside", "denied"),
                    ToolEvidence("filesystem", "read", "outside", "allowed"),
                    ToolEvidence("filesystem", "write", "outside", "denied"),
                ),
                exit_code=0,
            )

    def test_profile_requires_an_explicit_cleanup_readback(self) -> None:
        phase_ids = (
            "positive-read",
            "outside-path",
            "symlink",
            "git",
            "secret",
            "local-network",
            "external-network",
            "process",
        )
        evidence = tuple(item for phase in phase_ids for item in _expected(phase))
        parsed = ParsedOpenCodeEvents(
            tuple(OpenCodeToolObservation("fixture", item) for item in evidence),
            True,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            profile = build_probe_manifest(
                profile="snapshot",
                workspace=Path(temp_dir) / "snapshot",
                executable=ExecutableIdentity("/private/opencode", "1.18.25", "a" * 64),
                file_identity=FileIdentity(1, 2, 3, 4, "a" * 64),
                argv=("/private/opencode", "--pure", "run", "probe"),
                environment_allowlist=("HOME", "PATH"),
            )

        missing_phases = attest_profile(parsed, exit_code=0)
        _, missing = assemble_receipt(profile, missing_phases)
        observed_phases = attest_profile(
            parsed,
            exit_code=0,
            cleanup=CleanupInventory(),
        )
        _, observed = assemble_receipt(profile, observed_phases)

        cleanup_phase = next(
            phase for phase in missing_phases if phase.phase_id == "cleanup"
        )
        self.assertFalse(cleanup_phase.attempted)
        self.assertEqual(cleanup_phase.outcome, "not-run")
        self.assertEqual(missing.status, "not-run")
        self.assertEqual(observed.status, "candidate")

    def test_adapter_exposes_raw_jsonl_for_probe_attestation(self) -> None:
        context = AdapterContext(
            "opencode",
            "reviewer",
            "opencode-go/kimi-k2.6",
            "low",
            Path("/private/snapshot"),
            Path("/private/provider"),
        )
        snapshot = AdapterSnapshot(
            "opencode-direct-readonly-1.18.25",
            "1.18.25",
            Path("/private/opencode"),
            "1.18.25",
            FileIdentity(1, 2, 3, 4, "a" * 64),
        )
        runner = mock.Mock()
        expected = ProcessResult(0, '{"type":"text","part":{"text":"done"}}\n', "")
        runner.run.return_value = expected

        with (
            mock.patch("agent_team.adapters._validate_snapshot"),
            mock.patch.object(OpenCodeReadOnlyAdapter, "_write_config"),
        ):
            observed = OpenCodeReadOnlyAdapter().execute_raw(
                context, snapshot, "probe", runner
            )

        self.assertIs(observed, expected)
        runner.run.assert_called_once()

    def test_adapter_config_disables_autoupdate_for_the_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            context = AdapterContext(
                "opencode",
                "reviewer",
                "opencode-go/kimi-k2.6",
                "low",
                root / "snapshot",
                root / "provider",
            )
            context.workspace.mkdir()
            context.private_root.mkdir()
            OpenCodeReadOnlyAdapter()._write_config(context)
            config = json.loads(
                (
                    context.private_root / "xdg_config_home/opencode/opencode.json"
                ).read_text()
            )

        self.assertFalse(config["autoupdate"])

    def test_parser_collapses_running_and_terminal_events_for_one_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            probe_targets = targets(root)
            raw = "\n".join(
                (
                    event(
                        tool="read",
                        status="running",
                        input_value={"filePath": str(probe_targets.normal)},
                        call_id="read-1",
                    ),
                    event(
                        tool="read",
                        status="completed",
                        input_value={"filePath": str(probe_targets.normal)},
                        call_id="read-1",
                    ),
                    event(
                        tool="read",
                        status="completed",
                        input_value={"filePath": str(probe_targets.normal)},
                        call_id="read-2",
                    ),
                    json.dumps(
                        {
                            "type": "text",
                            "part": {"type": "text", "text": "done"},
                        }
                    ),
                )
            )

            observed = parse_opencode_events(raw, probe_targets)

        self.assertEqual(observed.tool_event_count, 2)
        self.assertEqual(
            observed.evidence,
            (ToolEvidence("filesystem", "read", "workspace", "allowed"),),
        )

    def test_receipt_assembly_preserves_blocked_prerequisite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            profile = build_probe_manifest(
                profile="snapshot",
                workspace=root / "snapshot",
                executable=ExecutableIdentity("/private/opencode", "1.18.25", "a" * 64),
                file_identity=FileIdentity(1, 2, 3, 4, "a" * 64),
                argv=("/private/opencode", "--pure", "run", "probe"),
                environment_allowlist=("HOME", "PATH"),
            )
            phases = tuple(
                PhaseReceipt(
                    phase.phase_id,
                    phase.expected_result,
                    False,
                    False,
                    "not-run",
                    None,
                    False,
                    (),
                    CleanupInventory(),
                )
                for phase in profile.required_phases
            )

        receipt, judgment = assemble_receipt(
            profile, phases, blocked_reason="authentication"
        )
        self.assertEqual(receipt.blocked_reason, "authentication")
        self.assertEqual(judgment.status, "blocked")


def _expected(phase_id: str) -> tuple[ToolEvidence, ...]:
    if phase_id == "positive-read":
        return (ToolEvidence("filesystem", "read", "workspace", "allowed"),)
    if phase_id == "positive-write":
        return (ToolEvidence("filesystem", "write", "workspace", "allowed"),)
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


if __name__ == "__main__":
    unittest.main()
