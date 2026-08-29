from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from unittest import mock

from agent_team.adapters import FileIdentity, ProcessResult
from agent_team.cursor_probe import (
    CURSOR_PINNED_VERSION,
    CursorExecutablePin,
    CursorExecutionError,
    CursorIdentityError,
    CursorProbe,
    CursorProbeError,
    CursorProfile,
    capture_cursor_identity,
    evaluate_profile,
    redacted_record,
    safe_environment,
)
from agent_team.probe_receipts import (
    CleanupInventory,
    Manifest,
    PhaseReceipt,
    ToolEvidence,
    judge_profile,
    parse_receipt,
    required_phases_for_profile,
    serialize_receipt,
)


def file_identity(path: Path) -> FileIdentity:
    resolved = path.resolve(strict=True)
    file_stat = resolved.stat()
    return FileIdentity(
        device=file_stat.st_dev,
        inode=file_stat.st_ino,
        size=file_stat.st_size,
        mtime_ns=file_stat.st_mtime_ns,
        sha256=hashlib.sha256(resolved.read_bytes()).hexdigest(),
    )


class FakeCursorRunner:
    def __init__(self, *, version: str = CURSOR_PINNED_VERSION) -> None:
        self.version = version
        self.calls: list[dict[str, object]] = []
        self.command_result = ProcessResult(0, "CURSOR_RESULT", "")

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        input_text: str | None = None,
        timeout_seconds: float = 900.0,
    ) -> ProcessResult:
        self.calls.append(
            {
                "argv": tuple(argv),
                "cwd": cwd,
                "env": dict(env),
                "input_text": input_text,
                "timeout_seconds": timeout_seconds,
            }
        )
        if tuple(argv[1:]) == ("--version",):
            return ProcessResult(0, f"Cursor Agent {self.version}\n", "")
        return self.command_result


class CursorProbeTest(unittest.TestCase):
    def make_probe(
        self,
        root: Path,
        *,
        profile: CursorProfile = "direct-plan",
        content: str = "#!/bin/sh\nprintf '%s\\n' cursor\n",
    ) -> tuple[CursorProbe, Path, Path]:
        workspace = root / "workspace"
        workspace.mkdir(parents=True)
        executable = root / "pinned" / "cursor-agent"
        executable.parent.mkdir()
        executable.write_text(content, encoding="utf-8")
        executable.chmod(0o755)
        pin = CursorExecutablePin(
            path=executable,
            version=CURSOR_PINNED_VERSION,
            identity=file_identity(executable),
        )
        return (
            CursorProbe(pin, profile=profile, workspace=workspace),
            executable,
            workspace,
        )

    def test_profiles_build_distinct_commands_and_acp_has_no_sandbox_claim(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            direct, executable, workspace = self.make_probe(root)
            acp, acp_executable, _ = self.make_probe(root / "acp", profile="acp")

            direct_invocation = direct.build_invocation("PROMPT_DO_NOT_PERSIST")
            acp_invocation = acp.build_invocation("PROMPT_DO_NOT_PERSIST")

            self.assertEqual(
                direct_invocation.argv,
                (
                    str(executable),
                    "--print",
                    "--mode",
                    "plan",
                    "--sandbox",
                    "enabled",
                    "--workspace",
                    str(workspace),
                    "--output-format",
                    "text",
                    "PROMPT_DO_NOT_PERSIST",
                ),
            )
            self.assertEqual(acp_invocation.argv, (str(acp_executable), "acp"))
            self.assertEqual(acp_invocation.input_text, "PROMPT_DO_NOT_PERSIST")
            self.assertNotIn("--sandbox", acp_invocation.argv)
            self.assertNotIn("--mode", acp_invocation.argv)
            self.assertEqual(direct_invocation.prompt_transport, "argv")
            self.assertEqual(acp_invocation.prompt_transport, "stdin")

            direct_manifest = direct.manifest(direct_invocation)
            acp_manifest = acp.manifest(acp_invocation)
            self.assertIsInstance(direct_manifest, Manifest)
            self.assertEqual(direct_manifest.identity.harness_id, "cursor")
            self.assertNotEqual(
                direct_manifest.identity.prompt_transport,
                acp_manifest.identity.prompt_transport,
            )
            self.assertEqual(
                direct_manifest.identity.sandbox_policy_id,
                "cursor-advertised-plan-v1",
            )
            self.assertEqual(
                acp_manifest.identity.sandbox_policy_id,
                "cursor-acp-no-policy-v1",
            )

    def test_preflight_uses_only_the_exact_pin_and_never_path_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            probe, executable, _ = self.make_probe(root)
            alternate = root / "alternate" / "cursor-agent"
            alternate.parent.mkdir()
            alternate.write_text("alternate", encoding="utf-8")
            alternate.chmod(0o755)
            runner = FakeCursorRunner()

            with mock.patch("shutil.which", side_effect=AssertionError("PATH lookup")):
                snapshot = probe.preflight(runner)

            self.assertEqual(snapshot.executable, executable.resolve())
            self.assertEqual(runner.calls[0]["argv"], (str(executable), "--version"))

            missing_pin = replace(probe.pin, path=root / "missing" / "cursor-agent")
            missing_probe = CursorProbe(
                missing_pin,
                profile="direct-plan",
                workspace=probe.workspace,
            )
            with self.assertRaises(CursorIdentityError):
                missing_probe.preflight(runner)
            self.assertEqual(len(runner.calls), 1)
            self.assertNotEqual(snapshot.executable, alternate.resolve())

    def test_preflight_rejects_a_pinned_symlink_target_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspace"
            workspace.mkdir()
            first = root / "first"
            second = root / "second"
            first.write_text("first", encoding="utf-8")
            second.write_text("second", encoding="utf-8")
            first.chmod(0o755)
            second.chmod(0o755)
            executable = root / "cursor-agent"
            executable.symlink_to(first)
            pin = CursorExecutablePin(
                path=executable,
                version=CURSOR_PINNED_VERSION,
                identity=file_identity(first),
            )
            probe = CursorProbe(pin, profile="direct-plan", workspace=workspace)
            runner = FakeCursorRunner()
            executable.unlink()
            executable.symlink_to(second)

            with self.assertRaises(CursorIdentityError):
                probe.preflight(runner)
            self.assertEqual(runner.calls, [])

    def test_capture_identity_matches_the_explicit_pin_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, executable, _ = self.make_probe(root)

            self.assertEqual(
                capture_cursor_identity(executable), file_identity(executable)
            )

    def test_execute_rechecks_identity_before_provider_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            probe, executable, _ = self.make_probe(root)
            invocation = probe.build_invocation("read")
            runner = FakeCursorRunner()
            probe.preflight(runner)
            executable.write_text(
                "#!/bin/sh\nprintf '%s\\n' changed\n", encoding="utf-8"
            )
            executable.chmod(0o755)

            with self.assertRaises(CursorIdentityError):
                probe.execute(invocation, runner)
            self.assertEqual(len(runner.calls), 1)

    def test_execute_rejects_metadata_only_identity_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            probe, executable, _ = self.make_probe(root)
            invocation = probe.build_invocation("read")
            runner = FakeCursorRunner()
            probe.preflight(runner)
            original = executable.stat()
            os.utime(
                executable,
                ns=(original.st_atime_ns, original.st_mtime_ns + 1_000_000),
            )

            with self.assertRaises(CursorIdentityError):
                probe.execute(invocation, runner)
            self.assertEqual(len(runner.calls), 1)

    def test_bundle_identity_is_checked_and_redacted_without_bundle_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, executable, workspace = self.make_probe(root)
            bundle = root / "bundle" / "index.js"
            bundle.parent.mkdir()
            bundle.write_text("bundle", encoding="utf-8")
            bundle_identity = file_identity(bundle)
            pin = CursorExecutablePin(
                path=executable,
                version=CURSOR_PINNED_VERSION,
                identity=file_identity(executable),
                bundle_path=bundle,
                bundle_identity=bundle_identity,
            )
            probe = CursorProbe(pin, profile="direct-plan", workspace=workspace)
            invocation = probe.build_invocation("read")
            runner = FakeCursorRunner()
            probe.preflight(runner)
            record = redacted_record(
                probe.manifest(invocation),
                invocation,
                status="blocked",
                reason_codes=("blocked-authentication",),
            )
            serialized = json.dumps(record, sort_keys=True)
            self.assertIn(bundle_identity.sha256, serialized)
            self.assertNotIn(str(bundle), serialized)
            bundle.write_text("changed", encoding="utf-8")
            with self.assertRaises(CursorIdentityError):
                probe.execute(invocation, runner)
            self.assertEqual(len(runner.calls), 1)

    def test_run_is_bounded_and_provider_failures_never_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            probe, executable, workspace = self.make_probe(root)
            runner = FakeCursorRunner()
            result = probe.run("PROMPT_IN_MEMORY", runner, timeout_seconds=17)

            self.assertEqual(result.stdout, "CURSOR_RESULT")
            self.assertEqual(len(runner.calls), 3)
            self.assertEqual(runner.calls[-1]["timeout_seconds"], 17)
            self.assertEqual(
                runner.calls[-1]["argv"],
                (
                    str(executable),
                    "--print",
                    "--mode",
                    "plan",
                    "--sandbox",
                    "enabled",
                    "--workspace",
                    str(workspace),
                    "--output-format",
                    "text",
                    "PROMPT_IN_MEMORY",
                ),
            )
            for process_result, message in (
                (ProcessResult(1, "", "provider failure"), "failed"),
                (ProcessResult(0, "", "", timed_out=True), "timed out"),
            ):
                failing_runner = FakeCursorRunner()
                probe.preflight(failing_runner)
                failing_runner.command_result = process_result
                with (
                    self.subTest(message=message),
                    self.assertRaisesRegex(CursorExecutionError, message),
                ):
                    probe.execute(
                        probe.build_invocation("read", timeout_seconds=12),
                        failing_runner,
                    )

    def test_execute_rejects_write_or_bypass_flags_added_to_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            probe, _, _ = self.make_probe(root)
            invocation = probe.build_invocation("read")
            unsafe = replace(
                invocation,
                argv=(*invocation.argv[:-1], "--force", invocation.argv[-1]),
            )
            runner = FakeCursorRunner()

            with self.assertRaises(CursorProbeError):
                probe.execute(unsafe, runner)
            self.assertEqual(runner.calls, [])

    def test_safe_environment_and_redacted_record_drop_values_prompts_and_paths(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            probe, executable, workspace = self.make_probe(root)
            invocation = probe.build_invocation("PROMPT_SECRET_VALUE")
            environment = safe_environment(
                {
                    "PATH": "/bin",
                    "HOME": "/personal/home",
                    "LANG": "ja_JP.UTF-8",
                    "LC_ALL": "ja_JP.UTF-8",
                    "CURSOR_API_KEY": "token-value",
                    "OPENAI_API_KEY": "other-token",
                },
                home=root / "isolated-home",
            )
            record = redacted_record(
                probe.manifest(invocation),
                invocation,
                status="blocked",
                reason_codes=("blocked-authentication",),
            )
            serialized = json.dumps(record, ensure_ascii=False, sort_keys=True)

            self.assertEqual(environment["HOME"], str(root / "isolated-home"))
            self.assertNotIn("CURSOR_API_KEY", environment)
            self.assertNotIn("token-value", environment)
            self.assertNotIn("PROMPT_SECRET_VALUE", serialized)
            self.assertNotIn(str(executable), serialized)
            self.assertNotIn(str(workspace), serialized)
            self.assertNotIn("personal/home", serialized)
            self.assertNotIn("PROMPT_SECRET_VALUE", repr(invocation))
            self.assertNotIn(str(executable), repr(probe.pin))
            self.assertIn("argv_sha256", serialized)
            self.assertIn("executable_path_sha256", serialized)
            self.assertIn(file_identity(executable).sha256, serialized)

    def test_complete_fake_phase_matrix_is_candidate_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            probe, _, _ = self.make_probe(root)
            manifest = probe.manifest(probe.build_invocation("phase prompt"))
            decision = evaluate_profile(manifest, self.complete_phases())

            self.assertEqual(decision.status, "candidate")
            self.assertEqual(decision.reason_codes, ())
            self.assertIsNotNone(decision.receipt)
            assert decision.receipt is not None
            serialized = serialize_receipt(decision.receipt)
            self.assertEqual(serialized, serialize_receipt(parse_receipt(serialized)))

    def test_unrun_blocker_and_missing_tool_failure_never_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            probe, _, _ = self.make_probe(root)
            manifest = probe.manifest(probe.build_invocation("phase prompt"))
            unrun = tuple(
                replace(
                    phase,
                    attempted=False,
                    tool_used=False,
                    outcome="not-run",
                    exit_code=None,
                    timed_out=False,
                    evidence=(),
                )
                for phase in self.complete_phases()
            )
            blocked = evaluate_profile(
                manifest,
                unrun,
                blocked_reason="authentication",
            )
            self.assertEqual(blocked.status, "blocked")
            self.assertEqual(blocked.reason_codes, ("blocked-authentication",))

            failed = list(self.complete_phases())
            failed[0] = replace(
                failed[0],
                tool_used=False,
                outcome="failed",
                exit_code=1,
                evidence=(),
            )
            failed_decision = evaluate_profile(manifest, tuple(failed))
            self.assertEqual(failed_decision.status, "rejected")
            self.assertIn("tool-not-used", failed_decision.reason_codes)

    def test_boundary_escape_is_rejected_by_generic_receipt_judge(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            probe, _, _ = self.make_probe(root)
            manifest = probe.manifest(probe.build_invocation("phase prompt"))
            phases = self.complete_phases()
            phases[1] = PhaseReceipt(
                "outside-path",
                "deny",
                True,
                True,
                "failed",
                1,
                False,
                (
                    ToolEvidence("filesystem", "read", "outside", "allowed"),
                    ToolEvidence("filesystem", "write", "outside", "denied"),
                ),
                CleanupInventory(),
            )

            decision = evaluate_profile(manifest, tuple(phases))

            self.assertEqual(decision.status, "rejected")
            self.assertIn("boundary-violation", decision.reason_codes)
            self.assertIsNotNone(decision.receipt)
            assert decision.receipt is not None
            self.assertEqual(
                judge_profile(manifest, decision.receipt).status,
                "rejected",
            )

    def complete_phases(self) -> list[PhaseReceipt]:
        return [
            PhaseReceipt(
                spec.phase_id,
                spec.expected_result,
                True,
                True,
                "passed",
                0,
                False,
                expected_evidence(spec.phase_id),
                CleanupInventory(),
            )
            for spec in required_phases_for_profile("read-only")
        ]


def expected_evidence(phase_id: str) -> tuple[ToolEvidence, ...]:
    if phase_id == "positive-read":
        return (ToolEvidence("filesystem", "read", "workspace", "allowed"),)
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
