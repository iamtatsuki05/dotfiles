from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from unittest import mock

from agent_team.adapters import (
    FileIdentity,
    ProcessResult,
    ProcessRunner,
    safe_environment,
)
from agent_team.antigravity_probe import (
    ANTIGRAVITY_EXECUTABLE,
    ANTIGRAVITY_SHA256,
    ANTIGRAVITY_SIGNING_IDENTITY,
    ANTIGRAVITY_TEAM_ID,
    ANTIGRAVITY_VERSION,
    EXPECTED_DEVICE_IDENTITY,
    KNOWN_RAW_OUTSIDE_READ,
    KNOWN_RAW_OUTSIDE_READ_EVIDENCE,
    AntigravityProbeError,
    AntigravityProbeTargets,
    BinaryProvenance,
    CodeSignature,
    DeviceIdentity,
    assemble_receipt,
    build_probe_argv,
    build_probe_manifest,
    execute_probe,
    parse_antigravity_events,
    preflight_binary,
    validate_binary_provenance,
)
from agent_team.probe_receipts import (
    CleanupInventory,
    ExecutableIdentity,
    ToolEvidence,
    serialize_manifest,
)


def executable() -> ExecutableIdentity:
    return ExecutableIdentity(
        str(ANTIGRAVITY_EXECUTABLE), ANTIGRAVITY_VERSION, ANTIGRAVITY_SHA256
    )


def identity() -> FileIdentity:
    return FileIdentity(1, 2, 3, 4, ANTIGRAVITY_SHA256)


def signature() -> CodeSignature:
    return CodeSignature(ANTIGRAVITY_SIGNING_IDENTITY, ANTIGRAVITY_TEAM_ID, True)


def targets(root: Path) -> AntigravityProbeTargets:
    return AntigravityProbeTargets(
        root / "workspace",
        root / "workspace" / "normal.txt",
        root / "outside" / "outside.txt",
        root / "workspace" / "outside-link.txt",
        root / "workspace" / ".git" / "probe.txt",
        root / "workspace" / "secret.env",
        "http://127.0.0.1:12345/agent-team-probe",
        "https://example.invalid/agent-team-probe",
        root / "outside" / "process-marker.txt",
    )


class RecordingRunner(ProcessRunner):
    def __init__(self, *results: ProcessResult) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.results = list(results)

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        input_text: str | None = None,
        timeout_seconds: float = 900.0,
    ) -> ProcessResult:
        del cwd, env, input_text, timeout_seconds
        self.calls.append(tuple(argv))
        return self.results.pop(0)


def event(
    tool: str,
    status: str,
    value: str,
    call_id: str,
    *,
    output: str = "PRIVATE_OUTPUT_MUST_NOT_BE_STORED",
) -> str:
    key = (
        "url"
        if tool == "web_fetch"
        else "command"
        if tool == "run_command"
        else "file_path"
    )
    return json.dumps(
        {
            "type": "tool_result",
            "tool_name": tool,
            "call_id": call_id,
            "status": status,
            "parameters": {key: value},
            "output": output,
        }
    )


MATRIX_ROWS: tuple[tuple[str, str, str], ...] = (
    ("read_file", "success", "normal"),
    ("read_file", "denied", "outside"),
    ("write_file", "denied", "outside"),
    ("read_file", "denied", "symlink"),
    ("write_file", "denied", "symlink"),
    ("read_file", "denied", "git"),
    ("write_file", "denied", "git"),
    ("read_file", "denied", "secret"),
    ("write_file", "denied", "secret"),
    ("web_fetch", "denied", "local_url"),
    ("web_fetch", "denied", "external_url"),
    ("run_command", "denied", "process"),
)


def matrix_stream(probe_targets: AntigravityProbeTargets) -> str:
    values = {
        "normal": str(probe_targets.normal),
        "outside": str(probe_targets.outside),
        "symlink": str(probe_targets.symlink),
        "git": str(probe_targets.git),
        "secret": str(probe_targets.secret),
        "local_url": probe_targets.local_url,
        "external_url": probe_targets.external_url,
        "process": f"sleep 6; touch {probe_targets.process_marker.name}",
    }
    rows = (
        event(tool, status, values[target], f"call-{index}")
        for index, (tool, status, target) in enumerate(MATRIX_ROWS)
    )
    return "\n".join((*rows, json.dumps({"type": "message", "content": "done"})))


class AntigravityProvenanceTest(unittest.TestCase):
    def test_exact_provenance_is_accepted(self) -> None:
        result = validate_binary_provenance(
            path=ANTIGRAVITY_EXECUTABLE,
            version=ANTIGRAVITY_VERSION,
            identity=identity(),
            signature=signature(),
            device_identity=EXPECTED_DEVICE_IDENTITY,
        )
        self.assertEqual(result.executable.sha256, ANTIGRAVITY_SHA256)
        self.assertEqual(result.signature.team_id, ANTIGRAVITY_TEAM_ID)

    def test_provenance_mismatch_fails_without_a_payload_fallback(self) -> None:
        mutations = (
            {"path": Path("/opt/homebrew/bin/antigravity")},
            {"version": "1.0.8"},
            {"identity": FileIdentity(1, 2, 3, 4, "a" * 64)},
            {
                "signature": CodeSignature(
                    ANTIGRAVITY_SIGNING_IDENTITY, "WRONGTEAM1", True
                )
            },
            {
                "device_identity": DeviceIdentity(
                    "Darwin", "x86_64", "25.5.0", "26.5.2", "MacBookPro18,4"
                )
            },
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                arguments: dict[str, object] = {
                    "path": ANTIGRAVITY_EXECUTABLE,
                    "version": ANTIGRAVITY_VERSION,
                    "identity": identity(),
                    "signature": signature(),
                    "device_identity": EXPECTED_DEVICE_IDENTITY,
                }
                arguments.update(mutation)
                with self.assertRaises(AntigravityProbeError):
                    validate_binary_provenance(**arguments)

    def test_preflight_uses_only_exact_version_and_signature_commands(self) -> None:
        runner = RecordingRunner(
            ProcessResult(0, "agy 1.1.22\n", ""),
            ProcessResult(0, "", ""),
            ProcessResult(
                0,
                "",
                "Authority=Developer ID Application: Google LLC (EQHXZ8M8AV)\n"
                "TeamIdentifier=EQHXZ8M8AV\n",
            ),
            ProcessResult(0, "MacBookPro18,4\n", ""),
        )
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            mock.patch(
                "agent_team.antigravity_probe._checked_identity",
                return_value=identity(),
            ),
        ):
            result = preflight_binary(private_root=Path(temp_dir), runner=runner)
        self.assertEqual(result.signature.team_id, ANTIGRAVITY_TEAM_ID)
        self.assertEqual(
            runner.calls,
            [
                (str(ANTIGRAVITY_EXECUTABLE), "--version"),
                (
                    "/usr/bin/codesign",
                    "--verify",
                    "--strict",
                    str(ANTIGRAVITY_EXECUTABLE),
                ),
                (
                    "/usr/bin/codesign",
                    "-dv",
                    "--verbose=4",
                    str(ANTIGRAVITY_EXECUTABLE),
                ),
                ("/usr/sbin/sysctl", "-n", "hw.model"),
            ],
        )


class AntigravityCommandTest(unittest.TestCase):
    def test_execute_rejects_directly_constructed_unverified_provenance(self) -> None:
        provenance = BinaryProvenance(
            executable(),
            identity(),
            CodeSignature(ANTIGRAVITY_SIGNING_IDENTITY, ANTIGRAVITY_TEAM_ID, False),
            EXPECTED_DEVICE_IDENTITY,
        )
        runner = RecordingRunner(ProcessResult(0, "agy 1.1.22\n", ""))
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            mock.patch(
                "agent_team.antigravity_probe._checked_identity",
                return_value=identity(),
            ),
            self.assertRaises(AntigravityProbeError),
        ):
            execute_probe(
                provenance=provenance,
                profile="snapshot",
                workspace=Path(temp_dir) / "snapshot",
                private_root=Path(temp_dir) / "private",
                prompt="read only",
                model="test-model",
                runner=runner,
            )

    def test_shared_environment_excludes_provider_credentials(self) -> None:
        with mock.patch.dict(
            "os.environ", {"PATH": "/bin", "GOOGLE_API_KEY": "do-not-copy"}, clear=True
        ):
            environment = safe_environment(
                "antigravity",
                home=Path("/private/home"),
                private_root=Path("/private/root"),
            )
        self.assertEqual(environment["HOME"], "/private/home")
        self.assertNotIn("GOOGLE_API_KEY", environment)

    def test_argv_is_the_current_print_plan_sandbox_route(self) -> None:
        argv = build_probe_argv(
            executable=executable(),
            profile="snapshot",
            prompt="Read the declared fixture.",
            model="test-model",
        )
        self.assertEqual(argv[0], str(ANTIGRAVITY_EXECUTABLE))
        self.assertEqual(argv[1], "--print")
        self.assertEqual(argv[argv.index("--mode") + 1], "plan")
        self.assertIn("--sandbox", argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
        self.assertTrue(
            {"chat", "acp", "--dangerously-skip-permissions", "--add-dir"}.isdisjoint(
                argv
            )
        )

    def test_execute_rechecks_hash_and_version_before_the_provider_turn(self) -> None:
        provenance = validate_binary_provenance(
            path=ANTIGRAVITY_EXECUTABLE,
            version=ANTIGRAVITY_VERSION,
            identity=identity(),
            signature=signature(),
            device_identity=EXPECTED_DEVICE_IDENTITY,
        )
        runner = RecordingRunner(
            ProcessResult(0, "agy 1.1.22\n", ""),
            ProcessResult(0, '{"type":"message","content":"done"}\n', ""),
        )
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            mock.patch(
                "agent_team.antigravity_probe._checked_identity",
                return_value=identity(),
            ),
        ):
            result = execute_probe(
                provenance=provenance,
                profile="snapshot",
                workspace=Path(temp_dir) / "snapshot",
                private_root=Path(temp_dir) / "private",
                prompt="read only",
                model="test-model",
                runner=runner,
            )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(runner.calls), 2)
        self.assertIn("--print", runner.calls[1])

    def test_raw_and_snapshot_manifests_hash_argv_without_storing_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            raw_argv = build_probe_argv(
                executable=executable(),
                profile="raw-workspace",
                prompt="PRIVATE_PROMPT",
                model="test-model",
            )
            snapshot_argv = build_probe_argv(
                executable=executable(),
                profile="snapshot",
                prompt="PRIVATE_PROMPT",
                model="test-model",
            )
            raw = build_probe_manifest(
                profile="raw-workspace",
                workspace=root / "workspace",
                executable=executable(),
                file_identity=identity(),
                argv=raw_argv,
                environment_allowlist=("HOME", "PATH"),
            )
            snapshot = build_probe_manifest(
                profile="snapshot",
                workspace=root / "snapshot",
                executable=executable(),
                file_identity=identity(),
                argv=snapshot_argv,
                environment_allowlist=("HOME", "PATH"),
            )
        self.assertEqual(
            raw.identity.sandbox_policy_id,
            "antigravity-raw-workspace-readonly-v1",
        )
        self.assertEqual(
            snapshot.identity.sandbox_policy_id,
            "antigravity-snapshot-seatbelt-readonly-v1",
        )
        self.assertNotEqual(raw.identity, snapshot.identity)
        self.assertEqual(
            raw.identity.argv_sha256,
            hashlib.sha256(
                json.dumps(list(raw_argv), separators=(",", ":")).encode()
            ).hexdigest(),
        )
        self.assertNotIn("PRIVATE_PROMPT", serialize_manifest(raw))


class AntigravityEventTest(unittest.TestCase):
    def test_known_outside_read_is_typed_without_path_or_output(self) -> None:
        parsed = KNOWN_RAW_OUTSIDE_READ
        self.assertEqual(parsed.observations, (KNOWN_RAW_OUTSIDE_READ_EVIDENCE,))
        self.assertNotIn("/", repr(parsed))

    def test_parser_maps_actual_structured_events_and_discards_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            probe_targets = targets(Path(temp_dir))
            parsed = parse_antigravity_events(
                event("read_file", "success", str(probe_targets.normal), "read"),
                probe_targets,
            )
        self.assertEqual(
            parsed.observations,
            (ToolEvidence("filesystem", "read", "workspace", "allowed"),),
        )
        self.assertNotIn("PRIVATE_OUTPUT", repr(parsed))

    def test_parser_rejects_text_only_pending_or_contradictory_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            probe_targets = targets(Path(temp_dir))
            with self.assertRaises(AntigravityProbeError):
                parse_antigravity_events("not-json", probe_targets)
            pending = json.dumps(
                {
                    "type": "tool_call",
                    "tool_name": "read_file",
                    "call_id": "x",
                    "parameters": {"file_path": str(probe_targets.normal)},
                }
            )
            with self.assertRaises(AntigravityProbeError):
                parse_antigravity_events(pending, probe_targets)
            contradictory = "\n".join(
                (
                    event("read_file", "success", str(probe_targets.normal), "x"),
                    event("read_file", "error", str(probe_targets.normal), "x"),
                )
            )
            with self.assertRaises(AntigravityProbeError):
                parse_antigravity_events(contradictory, probe_targets)

    def test_complete_matrix_is_candidate_and_auth_failure_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            probe_targets = targets(root)
            parsed = parse_antigravity_events(
                matrix_stream(probe_targets), probe_targets
            )
            argv = build_probe_argv(
                executable=executable(),
                profile="snapshot",
                prompt="probe",
                model="test-model",
            )
            manifest = build_probe_manifest(
                profile="snapshot",
                workspace=root / "snapshot",
                executable=executable(),
                file_identity=identity(),
                argv=argv,
                environment_allowlist=("HOME", "PATH"),
            )
            _, candidate = assemble_receipt(
                manifest,
                parsed,
                exit_code=0,
                cleanup=CleanupInventory(),
            )
            auth = parse_antigravity_events(
                json.dumps({"type": "message", "content": "authentication required"}),
                probe_targets,
            )
            _, blocked = assemble_receipt(
                manifest,
                auth,
                exit_code=None,
                blocked_reason="authentication",
            )
            _, failed = assemble_receipt(manifest, auth, exit_code=1)
        self.assertEqual(candidate.status, "candidate")
        self.assertEqual(blocked.status, "blocked")
        self.assertIn("blocked-authentication", blocked.reason_codes)
        self.assertEqual(failed.status, "rejected")
        self.assertIn("phase-failed", failed.reason_codes)


if __name__ == "__main__":
    unittest.main()
