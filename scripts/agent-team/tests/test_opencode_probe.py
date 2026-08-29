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
    _identity,
)
from agent_team.opencode_probe import (
    HistoricalSymlinkEvidence,
    OpenCodeProbeError,
    OpenCodeToolObservation,
    ParsedOpenCodeEvents,
    ProbeBinding,
    ProbeTargets,
    assemble_receipt,
    attest_phase,
    attest_profile,
    build_probe_binding,
    build_probe_manifest,
    canonical_opencode_argv,
    make_historical_receipt,
    parse_opencode_events,
)
from agent_team.probe_receipts import (
    CleanupInventory,
    ExecutableIdentity,
    Manifest,
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
    error_code: str | None = None,
    final: bool = False,
) -> str:
    state: dict[str, object] = {
        "status": status,
        "input": input_value,
        "output": output,
    }
    if error_code is not None:
        state["error"] = {"code": error_code}
    return json.dumps(
        {
            "type": "tool_use",
            "part": {
                "type": "tool",
                "tool": tool,
                "callID": call_id or f"call-{tool}-{status}",
                "state": state,
                "final": final,
            },
        }
    )


def executable() -> ExecutableIdentity:
    return ExecutableIdentity("/private/opencode", "1.18.25", "a" * 64)


def manifest_for(
    root: Path, *, profile: str = "snapshot", prompt: str = "probe"
) -> Manifest:
    workspace = root / profile
    argv = canonical_opencode_argv(
        executable(),
        workspace,
        model="opencode-go/kimi-k2.6",
        variant="low",
        prompt=prompt,
    )
    return build_probe_manifest(
        profile=profile,  # type: ignore[arg-type]
        workspace=workspace,
        executable=executable(),
        file_identity=FileIdentity(1, 2, 3, 4, "a" * 64),
        argv=argv,
        environment_allowlist=("HOME", "PATH", "XDG_CONFIG_HOME"),
    )


def binding_for(root: Path, *, profile: str = "snapshot") -> ProbeBinding:
    manifest = manifest_for(root, profile=profile)
    return build_probe_binding(
        manifest,
        profile=profile,  # type: ignore[arg-type]
        run_nonce="run-1",
        targets=targets(root),
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
                binding=binding_for(root),
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
                    binding=binding_for(root),
                )
            with self.assertRaises(OpenCodeProbeError):
                parse_opencode_events(
                    "not-json", targets(root), binding=binding_for(root)
                )

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
                        error_code="permission_denied",
                    ),
                    event(
                        tool="webfetch",
                        status="denied",
                        input_value={"url": probe_targets.local_url},
                        error_code="permission_denied",
                    ),
                    event(
                        tool="bash",
                        status="error",
                        input_value={"command": probe_targets.process_command},
                        error_code="permission_denied",
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

            observed = parse_opencode_events(
                raw, probe_targets, binding=binding_for(root)
            )

        self.assertEqual(
            observed.evidence,
            (
                ToolEvidence("filesystem", "write", "symlink", "denied"),
                ToolEvidence("network", "connect", "local-network", "denied"),
                ToolEvidence("process", "spawn", "process", "denied"),
            ),
        )

    def test_provider_error_is_not_attested_as_permission_denial(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            probe_targets = targets(root)
            observed = parse_opencode_events(
                event(
                    tool="edit",
                    status="error",
                    input_value={"filePath": str(probe_targets.outside)},
                ),
                probe_targets,
                binding=binding_for(root),
            )

        self.assertIsNone(observed.observations[0].evidence)
        self.assertEqual(observed.observations[0].result, "inconclusive")
        self.assertTrue(observed.provider_failed)

    def test_top_level_provider_failure_blocks_even_with_final_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            probe_targets = targets(root)
            raw = "\n".join(
                (
                    json.dumps(
                        {
                            "type": "text",
                            "part": {
                                "type": "text",
                                "text": "OPENCODE_PROBE_DONE",
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "step_finish",
                            "part": {"type": "step-finish", "reason": "stop"},
                        }
                    ),
                    json.dumps({"type": "provider_error", "error": "failed"}),
                )
            )
            parsed = parse_opencode_events(
                raw, probe_targets, binding=binding_for(root)
            )

        self.assertFalse(parsed.final_completion)
        self.assertTrue(parsed.provider_failed)
        self.assertFalse(parsed.candidate_ready)

    def test_only_explicit_permission_code_is_attested_as_denied(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            probe_targets = targets(root)
            observed = parse_opencode_events(
                event(
                    tool="edit",
                    status="error",
                    input_value={"filePath": str(probe_targets.outside)},
                    error_code="permission_denied",
                ),
                probe_targets,
                binding=binding_for(root),
            )

        self.assertEqual(
            observed.evidence,
            (ToolEvidence("filesystem", "write", "outside", "denied"),),
        )
        self.assertFalse(observed.provider_failed)

    def test_missing_call_id_and_duplicate_terminal_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            probe_targets = targets(root)
            missing_id = json.loads(
                event(
                    tool="read",
                    status="completed",
                    input_value={"filePath": str(probe_targets.normal)},
                )
            )
            del missing_id["part"]["callID"]
            with self.assertRaises(OpenCodeProbeError):
                parse_opencode_events(
                    json.dumps(missing_id),
                    probe_targets,
                    binding=binding_for(root),
                )

            duplicate = "\n".join(
                (
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
                        call_id="read-1",
                    ),
                )
            )
            with self.assertRaises(OpenCodeProbeError):
                parse_opencode_events(
                    duplicate,
                    probe_targets,
                    binding=binding_for(root),
                )

    def test_duplicate_operation_is_recorded_and_blocks_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            probe_targets = targets(root)
            raw = "\n".join(
                (
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
                            "part": {
                                "type": "text",
                                "text": "OPENCODE_PROBE_DONE",
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "step_finish",
                            "part": {"type": "step-finish", "reason": "stop"},
                        }
                    ),
                )
            )
            parsed = parse_opencode_events(
                raw, probe_targets, binding=binding_for(root)
            )

        self.assertIn("duplicate-operation", parsed.integrity_errors)
        self.assertFalse(parsed.candidate_ready)

    def test_complete_bound_current_run_is_the_only_candidate_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            probe_targets = targets(root)
            lines = [
                event(
                    tool="read",
                    status="completed",
                    input_value={"filePath": str(probe_targets.normal)},
                    call_id="positive-read",
                )
            ]
            for phase_id, path in (
                ("outside-path", probe_targets.outside),
                ("symlink", probe_targets.symlink),
                ("git", probe_targets.git),
                ("secret", probe_targets.secret),
            ):
                lines.extend(
                    (
                        event(
                            tool="read",
                            status="error",
                            input_value={"filePath": str(path)},
                            error_code="permission_denied",
                            call_id=f"{phase_id}-read",
                        ),
                        event(
                            tool="edit",
                            status="error",
                            input_value={"filePath": str(path)},
                            error_code="permission_denied",
                            call_id=f"{phase_id}-write",
                        ),
                    )
                )
            lines.extend(
                (
                    event(
                        tool="webfetch",
                        status="error",
                        input_value={"url": probe_targets.local_url},
                        error_code="permission_denied",
                        call_id="local-network",
                    ),
                    event(
                        tool="webfetch",
                        status="error",
                        input_value={"url": probe_targets.external_url},
                        error_code="permission_denied",
                        call_id="external-network",
                    ),
                    event(
                        tool="bash",
                        status="error",
                        input_value={"command": probe_targets.process_command},
                        error_code="permission_denied",
                        call_id="process",
                    ),
                    json.dumps(
                        {
                            "type": "text",
                            "part": {
                                "type": "text",
                                "text": "OPENCODE_PROBE_DONE",
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "step_finish",
                            "part": {"type": "step-finish", "reason": "stop"},
                        }
                    ),
                )
            )
            parsed = parse_opencode_events(
                "\n".join(lines),
                probe_targets,
                binding=binding_for(root),
            )
            manifest = manifest_for(root)
            phases = attest_profile(parsed, exit_code=0, cleanup=CleanupInventory())
            receipt, judgment = assemble_receipt(
                manifest,
                phases,
                attestation=parsed,
                run_nonce="run-1",
                targets_fingerprint=probe_targets.fingerprint,
            )

        self.assertTrue(parsed.candidate_ready)
        self.assertEqual(judgment.status, "candidate")
        self.assertIsNone(receipt.blocked_reason)

    def test_final_completion_requires_marker_and_terminal_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            probe_targets = targets(root)
            intermediate = "\n".join(
                (
                    event(
                        tool="read",
                        status="completed",
                        input_value={"filePath": str(probe_targets.normal)},
                    ),
                    json.dumps(
                        {
                            "type": "text",
                            "part": {"type": "text", "text": "OPENCODE_PROBE_DONE"},
                        }
                    ),
                )
            )
            parsed = parse_opencode_events(
                intermediate, probe_targets, binding=binding_for(root)
            )

        self.assertTrue(parsed.final_text_seen)
        self.assertFalse(parsed.final_completion)
        self.assertFalse(parsed.candidate_ready)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest = manifest_for(root)
            phases = tuple(
                attest_phase(
                    phase.phase_id,
                    _expected(phase.phase_id),
                    exit_code=0,
                    cleanup=CleanupInventory() if phase.phase_id == "cleanup" else None,
                )
                for phase in manifest.required_phases
            )
            with self.assertRaises(ReceiptValidationError):
                assemble_receipt(
                    manifest,
                    phases,
                    attestation=parsed,
                    run_nonce="run-1",
                    targets_fingerprint=parsed.binding.targets_sha256,
                )

    def test_raw_binding_cannot_be_assembled_into_snapshot_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            probe_targets = targets(root)
            parsed = parse_opencode_events(
                event(
                    tool="read",
                    status="completed",
                    input_value={"filePath": str(probe_targets.normal)},
                ),
                probe_targets,
                binding=binding_for(root, profile="raw-workspace"),
            )
            snapshot_manifest = manifest_for(root, profile="snapshot")
            phases = attest_profile(parsed, exit_code=0, cleanup=CleanupInventory())
            with self.assertRaises(ReceiptValidationError):
                assemble_receipt(
                    snapshot_manifest,
                    phases,
                    attestation=parsed,
                    run_nonce="run-1",
                    targets_fingerprint=probe_targets.fingerprint,
                )

    def test_historical_symlink_evidence_has_separate_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest = manifest_for(root, profile="raw-workspace")
            history = make_historical_receipt(
                manifest,
                HistoricalSymlinkEvidence(
                    profile="raw-workspace",
                    observed_at="2026-08-29T00:00:00Z",
                    source_digest="b" * 64,
                    verification_status="verified",
                    evidence=(
                        ToolEvidence("filesystem", "read", "symlink", "allowed"),
                        ToolEvidence("filesystem", "write", "symlink", "denied"),
                    ),
                ),
            )

        self.assertEqual(history[1].status, "rejected")
        self.assertIn("boundary-violation", history[1].reason_codes)

    def test_manifest_rejects_noncanonical_argv_and_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            unsafe_argv = (
                "/private/opencode",
                "--pure",
                "run",
                "probe",
                "--format",
                "json",
                "--model",
                "opencode-go/kimi-k2.6",
                "--dir",
                str(root / "snapshot"),
                "--auto",
            )
            with self.assertRaises(OpenCodeProbeError):
                build_probe_manifest(
                    profile="snapshot",
                    workspace=root / "snapshot",
                    executable=executable(),
                    file_identity=FileIdentity(1, 2, 3, 4, "a" * 64),
                    argv=unsafe_argv,
                    environment_allowlist=("HOME", "PATH"),
                )
            argv = canonical_opencode_argv(
                executable(),
                root / "snapshot",
                model="opencode-go/kimi-k2.6",
                variant="low",
                prompt="probe",
            )
            with self.assertRaises(OpenCodeProbeError):
                build_probe_manifest(
                    profile="snapshot",
                    workspace=root / "snapshot",
                    executable=executable(),
                    file_identity=FileIdentity(1, 2, 3, 4, "a" * 64),
                    argv=argv,
                    environment_allowlist=("HOME", "OPENAI_API_KEY"),
                )

    def test_process_command_requires_exact_fixed_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            probe_targets = targets(root)
            parsed = parse_opencode_events(
                event(
                    tool="bash",
                    status="error",
                    input_value={
                        "command": f"echo sleep 6; printf PROCESS_ESCAPED > {probe_targets.process_marker}"
                    },
                    error_code="permission_denied",
                ),
                probe_targets,
                binding=binding_for(root),
            )

        self.assertIsNone(parsed.observations[0].evidence)
        self.assertTrue(parsed.provider_failed)

    def test_execution_preserves_runner_timeout(self) -> None:
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
        runner.run.return_value = ProcessResult(
            0,
            '{"type":"text","part":{"type":"text","text":"done"}}\n',
            "",
            True,
        )
        with (
            mock.patch("agent_team.adapters._validate_snapshot"),
            mock.patch.object(OpenCodeReadOnlyAdapter, "_write_config"),
            mock.patch.object(
                OpenCodeReadOnlyAdapter,
                "_prepare_verified_executable",
                return_value=snapshot,
            ),
        ):
            observed = OpenCodeReadOnlyAdapter().execute(
                context, snapshot, "probe", runner
            )

        self.assertTrue(observed.timed_out)
        self.assertEqual(observed.output, "")

    def test_prepare_verified_executable_runs_private_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "opencode"
            source.write_text("#!/bin/sh\necho 1.18.25\n", encoding="utf-8")
            source.chmod(0o755)
            private = root / "provider"
            private.mkdir()
            source_identity = _identity(source)
            snapshot = AdapterSnapshot(
                "opencode-direct-readonly-1.18.25",
                "1.18.25",
                source,
                "1.18.25",
                source_identity,
            )
            context = AdapterContext(
                "opencode",
                "reviewer",
                "opencode-go/kimi-k2.6",
                "low",
                root / "workspace",
                private,
            )
            context.workspace.mkdir()
            runner = mock.Mock()
            runner.run.return_value = ProcessResult(0, "1.18.25\n", "")
            prepared = OpenCodeReadOnlyAdapter()._prepare_verified_executable(
                context, snapshot, runner
            )
            original = source.read_bytes()
            source.write_text("#!/bin/sh\necho changed\n", encoding="utf-8")
            self.assertNotEqual(prepared.executable, source)
            self.assertEqual(prepared.identity.sha256, source_identity.sha256)
            self.assertEqual(prepared.executable.read_bytes(), original)
            self.assertEqual(
                prepared.executable.parent,
                private.resolve() / "verified-executable",
            )

    def test_execute_raw_passes_private_copy_to_runner(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "opencode"
            source.write_text("#!/bin/sh\necho 1.18.25\n", encoding="utf-8")
            source.chmod(0o755)
            private = root / "provider"
            private.mkdir()
            snapshot = AdapterSnapshot(
                "opencode-direct-readonly-1.18.25",
                "1.18.25",
                source,
                "1.18.25",
                _identity(source),
            )
            context = AdapterContext(
                "opencode",
                "reviewer",
                "opencode-go/kimi-k2.6",
                "low",
                root / "workspace",
                private,
            )
            context.workspace.mkdir()
            runner = mock.Mock()
            runner.run.return_value = ProcessResult(0, "1.18.25\n", "")

            OpenCodeReadOnlyAdapter().execute_raw(context, snapshot, "probe", runner)

            executed_argv = runner.run.call_args_list[-1].args[0]

        self.assertNotEqual(executed_argv[0], str(source))
        self.assertIn("verified-executable", executed_argv[0])

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
                argv=canonical_opencode_argv(
                    executable,
                    root / "workspace",
                    model="opencode-go/kimi-k2.6",
                    variant="low",
                    prompt="probe",
                ),
                environment_allowlist=("HOME", "PATH"),
            )
            snapshot = build_probe_manifest(
                profile="snapshot",
                workspace=root / "snapshot",
                executable=executable,
                file_identity=identity,
                argv=canonical_opencode_argv(
                    executable,
                    root / "snapshot",
                    model="opencode-go/kimi-k2.6",
                    variant="low",
                    prompt="probe",
                ),
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
                    [
                        "/private/opencode",
                        "--pure",
                        "run",
                        "probe",
                        "--format",
                        "json",
                        "--model",
                        "opencode-go/kimi-k2.6",
                        "--dir",
                        str((root / "workspace").resolve()),
                        "--variant",
                        "low",
                    ],
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
                argv=canonical_opencode_argv(
                    executable(),
                    probe_targets.workspace,
                    model="opencode-go/kimi-k2.6",
                    variant="low",
                    prompt="probe",
                ),
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
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            parsed = ParsedOpenCodeEvents(
                tuple(
                    OpenCodeToolObservation(
                        "fixture", item, f"fixture-{index}", item.result
                    )
                    for index, item in enumerate(evidence)
                ),
                binding_for(root),
                True,
                True,
            )
            profile = build_probe_manifest(
                profile="snapshot",
                workspace=root / "snapshot",
                executable=ExecutableIdentity("/private/opencode", "1.18.25", "a" * 64),
                file_identity=FileIdentity(1, 2, 3, 4, "a" * 64),
                argv=canonical_opencode_argv(
                    executable(),
                    root / "snapshot",
                    model="opencode-go/kimi-k2.6",
                    variant="low",
                    prompt="probe",
                ),
                environment_allowlist=("HOME", "PATH"),
            )

        missing_phases = attest_profile(parsed, exit_code=0)
        with self.assertRaises(ReceiptValidationError):
            assemble_receipt(profile, missing_phases)
        observed_phases = attest_profile(
            parsed,
            exit_code=0,
            cleanup=CleanupInventory(),
        )
        with self.assertRaises(ReceiptValidationError):
            assemble_receipt(profile, observed_phases)

        cleanup_phase = next(
            phase for phase in missing_phases if phase.phase_id == "cleanup"
        )
        self.assertFalse(cleanup_phase.attempted)
        self.assertEqual(cleanup_phase.outcome, "not-run")

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
            mock.patch.object(
                OpenCodeReadOnlyAdapter,
                "_prepare_verified_executable",
                return_value=snapshot,
            ),
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

            observed = parse_opencode_events(
                raw, probe_targets, binding=binding_for(root)
            )

        self.assertEqual(observed.tool_event_count, 2)
        self.assertIn("duplicate-operation", observed.integrity_errors)
        self.assertFalse(observed.candidate_ready)
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
                argv=canonical_opencode_argv(
                    executable(),
                    root / "snapshot",
                    model="opencode-go/kimi-k2.6",
                    variant="low",
                    prompt="probe",
                ),
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
