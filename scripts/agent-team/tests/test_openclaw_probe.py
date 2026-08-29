from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_team.openclaw_probe import (
    OPENCLAW_BUILD,
    OPENCLAW_VERSION,
    DockerSandboxConfig,
    OpenClawIdentity,
    OpenClawProbe,
    OpenClawProbeError,
    OpenClawProbeStatus,
    ReceiptBundle,
    build_blocked_receipt,
    build_probe_manifest,
    direct_sandbox_off_cell,
    docker_preflight,
    resolve_openclaw_identity,
    serialize_openclaw_manifest,
    serialize_openclaw_receipt,
)
from agent_team.probe_receipts import (
    CleanupInventory,
    PhaseReceipt,
    Receipt,
    ReceiptValidationError,
    ToolEvidence,
    required_phases_for_profile,
)


def fixture_root() -> tuple[tempfile.TemporaryDirectory[str], Path]:
    holder = tempfile.TemporaryDirectory(prefix="openclaw-probe-")
    root = Path(holder.name).resolve()
    root.chmod(0o700)
    return holder, root


def config_for() -> DockerSandboxConfig:
    return DockerSandboxConfig(
        context="rancher-desktop",
        image="openclaw-sandbox@sha256:" + "a" * 64,
    )


def executable_for(root: Path, content: str = "pinned executable") -> Path:
    executable = root / "openclaw.mjs"
    executable.write_text(content, encoding="utf-8")
    executable.chmod(0o755)
    return executable


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


class OpenClawIdentityTest(unittest.TestCase):
    def test_identity_requires_absolute_canonical_regular_executable(self) -> None:
        holder, root = fixture_root()
        self.addCleanup(holder.cleanup)
        executable = executable_for(root)
        digest = hashlib.sha256(executable.read_bytes()).hexdigest()
        with mock.patch("agent_team.openclaw_probe.OPENCLAW_EXECUTABLE_SHA256", digest):
            identity = resolve_openclaw_identity(executable)
            self.assertEqual(identity.path, executable)
            self.assertEqual(identity.version, OPENCLAW_VERSION)
            self.assertEqual(identity.build, OPENCLAW_BUILD)
            with self.assertRaisesRegex(OpenClawProbeError, "absolute canonical"):
                resolve_openclaw_identity(Path("openclaw.mjs"))
            symlink = root / "openclaw-link"
            symlink.symlink_to(executable)
            with self.assertRaisesRegex(OpenClawProbeError, "absolute canonical"):
                resolve_openclaw_identity(symlink)

    def test_identity_fails_closed_on_wrong_hash_and_non_executable(self) -> None:
        holder, root = fixture_root()
        self.addCleanup(holder.cleanup)
        executable = executable_for(root)
        with self.assertRaisesRegex(OpenClawProbeError, "SHA-256"):
            resolve_openclaw_identity(executable)
        executable.chmod(0o644)
        digest = hashlib.sha256(executable.read_bytes()).hexdigest()
        with (
            mock.patch("agent_team.openclaw_probe.OPENCLAW_EXECUTABLE_SHA256", digest),
            self.assertRaisesRegex(OpenClawProbeError, "executable"),
        ):
            resolve_openclaw_identity(executable)

    def test_same_file_attestation_is_rechecked_by_manifest_builder(self) -> None:
        holder, root = fixture_root()
        self.addCleanup(holder.cleanup)
        executable = executable_for(root)
        digest = hashlib.sha256(executable.read_bytes()).hexdigest()
        with mock.patch("agent_team.openclaw_probe.OPENCLAW_EXECUTABLE_SHA256", digest):
            resolved = resolve_openclaw_identity(executable)
            forged_without_attestation = OpenClawIdentity(
                executable, OPENCLAW_VERSION, digest, OPENCLAW_BUILD
            )
            with self.assertRaisesRegex(ReceiptValidationError, "attestation"):
                build_probe_manifest(forged_without_attestation, "read-only")
            executable.write_text("replaced", encoding="utf-8")
            with self.assertRaisesRegex(ReceiptValidationError, "identity"):
                build_probe_manifest(resolved, "read-only")

    def test_identity_constructor_rejects_a_non_pinned_digest(self) -> None:
        holder, root = fixture_root()
        self.addCleanup(holder.cleanup)
        executable = executable_for(root)
        with self.assertRaisesRegex(OpenClawProbeError, "pinned"):
            OpenClawIdentity(executable, OPENCLAW_VERSION, "b" * 64, OPENCLAW_BUILD)


class OpenClawDockerPolicyTest(unittest.TestCase):
    def test_direct_sandbox_off_is_always_non_candidate(self) -> None:
        cell = direct_sandbox_off_cell()
        self.assertEqual(cell.cell_id, "direct-sandbox-off")
        self.assertEqual(cell.status, OpenClawProbeStatus.NOT_RUN)
        self.assertIn("sandbox-off", cell.reason)

    def test_arbitrary_image_is_blocked_before_any_runtime_probe(self) -> None:
        config = config_for()
        result = docker_preflight(config)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.reason, "blocked-image")

    def test_invalid_context_is_blocked_without_runtime_probe(self) -> None:
        result = docker_preflight(
            DockerSandboxConfig(
                context="../default",
                image="openclaw-sandbox@sha256:" + "a" * 64,
            )
        )
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.reason, "blocked-context")

    def test_preflight_only_config_has_no_live_mount_or_provider_fields(self) -> None:
        config_fields = set(DockerSandboxConfig.__dataclass_fields__)
        self.assertEqual(config_fields, {"context", "image"})
        self.assertFalse(hasattr(DockerSandboxConfig, "mount_source"))
        self.assertFalse(hasattr(DockerSandboxConfig, "session_key"))
        self.assertFalse(hasattr(OpenClawProbe, "run_live"))


class OpenClawPreflightTest(unittest.TestCase):
    def test_preflight_static_identity_and_unpinned_docker_are_blocked(self) -> None:
        holder, root = fixture_root()
        self.addCleanup(holder.cleanup)
        executable = executable_for(root)
        digest = hashlib.sha256(executable.read_bytes()).hexdigest()
        with mock.patch("agent_team.openclaw_probe.OPENCLAW_EXECUTABLE_SHA256", digest):
            report = OpenClawProbe(executable, config_for()).preflight()
            self.assertEqual(report.status, OpenClawProbeStatus.BLOCKED)
            self.assertEqual(report.docker.reason, "blocked-image")
            self.assertEqual(report.receipt.judgment.status, "blocked")
            self.assertTrue(
                all(not phase.attempted for phase in report.receipt.receipt.phases)
            )
        self.assertEqual(
            report.cells[0].status,
            OpenClawProbeStatus.NOT_RUN,
        )
        self.assertEqual(
            [cell.status for cell in report.cells[1:]],
            [OpenClawProbeStatus.BLOCKED, OpenClawProbeStatus.BLOCKED],
        )

    def test_hash_mismatch_is_rejected_before_any_runtime_operation(self) -> None:
        holder, root = fixture_root()
        self.addCleanup(holder.cleanup)
        executable = executable_for(root, "not-pinned")
        with self.assertRaisesRegex(OpenClawProbeError, "SHA-256"):
            OpenClawProbe(executable, config_for()).preflight()


class OpenClawReceiptTest(unittest.TestCase):
    def test_serializer_recomputes_judgment_and_keeps_blocked_phases_unattempted(
        self,
    ) -> None:
        holder, root = fixture_root()
        self.addCleanup(holder.cleanup)
        executable = executable_for(root)
        digest = hashlib.sha256(executable.read_bytes()).hexdigest()
        with mock.patch("agent_team.openclaw_probe.OPENCLAW_EXECUTABLE_SHA256", digest):
            identity = resolve_openclaw_identity(executable)
            bundle = build_blocked_receipt(identity, "read-only", "docker")
            self.assertEqual(bundle.judgment.status, "blocked")
            manifest = serialize_openclaw_manifest(identity, "read-only")
            serialized = serialize_openclaw_receipt(bundle)
        self.assertNotIn(str(executable), serialized)
        self.assertNotIn(str(executable), manifest)
        self.assertNotIn("raw-log", serialized)
        self.assertNotIn("prompt text", serialized)
        self.assertNotIn("OPENAI_API_KEY=", serialized)
        payload = json.loads(serialized)
        self.assertEqual(payload["blocked_reason"], "docker")
        self.assertTrue(all(not phase["attempted"] for phase in payload["phases"]))
        self.assertTrue(
            all(phase["outcome"] == "not-run" for phase in payload["phases"])
        )

    def test_untrusted_all_passed_phases_cannot_be_serialized_as_candidate(
        self,
    ) -> None:
        holder, root = fixture_root()
        self.addCleanup(holder.cleanup)
        executable = executable_for(root)
        digest = hashlib.sha256(executable.read_bytes()).hexdigest()
        with mock.patch("agent_team.openclaw_probe.OPENCLAW_EXECUTABLE_SHA256", digest):
            identity = resolve_openclaw_identity(executable)
            manifest = build_probe_manifest(identity, "read-only")
            phases = tuple(
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
            )
            forged = Receipt(manifest.identity, None, phases)
            with self.assertRaisesRegex(ReceiptValidationError, "provenance"):
                serialize_openclaw_receipt(ReceiptBundle(forged, identity, "read-only"))

    def test_forged_bundle_profile_is_not_accepted(self) -> None:
        holder, root = fixture_root()
        self.addCleanup(holder.cleanup)
        executable = executable_for(root)
        digest = hashlib.sha256(executable.read_bytes()).hexdigest()
        with mock.patch("agent_team.openclaw_probe.OPENCLAW_EXECUTABLE_SHA256", digest):
            identity = resolve_openclaw_identity(executable)
            read_only = build_blocked_receipt(identity, "read-only", "docker")
            with self.assertRaisesRegex(ReceiptValidationError, "profile"):
                serialize_openclaw_receipt(
                    ReceiptBundle(read_only.receipt, identity, "workspace-write")
                )


if __name__ == "__main__":
    unittest.main()
