from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from agent_team.adapters import ProcessResult
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
    resolve_openclaw_identity,
    serialize_openclaw_manifest,
    serialize_openclaw_receipt,
    validate_docker_sandbox_config,
)
from agent_team.probe_receipts import ReceiptValidationError


class FakeRunner:
    def __init__(self, responses: dict[tuple[str, ...], ProcessResult]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...], *, timeout_seconds: float) -> ProcessResult:
        del timeout_seconds
        self.calls.append(argv)
        return self.responses.get(argv, ProcessResult(1, "", "unexpected command"))


def fixture_root() -> tuple[tempfile.TemporaryDirectory[str], Path, Path]:
    holder = tempfile.TemporaryDirectory(prefix="openclaw-probe-")
    root = Path(holder.name).resolve()
    root.chmod(0o700)
    mount = root / "disposable"
    mount.mkdir(mode=0o700)
    mount.chmod(0o700)
    return holder, root, mount


def config_for(
    root: Path, mount: Path, *, image: str | None = None
) -> DockerSandboxConfig:
    return DockerSandboxConfig(
        context="rancher-desktop",
        image=image or f"openclaw-sandbox@sha256:{'a' * 64}",
        disposable_parent=root,
        mount_source=mount,
        session_key="agent:issue9-openclaw:nonce1234",
    )


def executable_for(root: Path, content: str = "pinned executable") -> Path:
    executable = root / "openclaw.mjs"
    executable.write_text(content, encoding="utf-8")
    executable.chmod(0o755)
    return executable


def version_result() -> ProcessResult:
    return ProcessResult(0, f"OpenClaw {OPENCLAW_VERSION} ({OPENCLAW_BUILD})\n", "")


def docker_responses(
    config: DockerSandboxConfig,
    *,
    endpoint: str = "unix:///private/docker.sock",
) -> dict[tuple[str, ...], ProcessResult]:
    prefix = ("docker", "--context", config.context)
    return {
        (*prefix, "context", "show"): ProcessResult(0, config.context + "\n", ""),
        (
            *prefix,
            "context",
            "inspect",
            config.context,
            "--format",
            "{{json .Endpoints.docker.Host}}",
        ): ProcessResult(0, json.dumps(endpoint) + "\n", ""),
        (
            *prefix,
            "version",
            "--format",
            "{{.Client.Version}}\\t{{.Server.Version}}",
        ): ProcessResult(0, "29.7.2\t28.4.0\n", ""),
        (
            *prefix,
            "image",
            "inspect",
            config.image,
            "--format",
            "{{json .RepoDigests}}",
        ): ProcessResult(0, json.dumps([config.image]) + "\n", ""),
    }


class OpenClawIdentityTest(unittest.TestCase):
    def test_identity_requires_absolute_canonical_regular_executable(self) -> None:
        holder, root, _ = fixture_root()
        self.addCleanup(holder.cleanup)
        executable = executable_for(root)
        digest = hashlib.sha256(executable.read_bytes()).hexdigest()
        with mock.patch("agent_team.openclaw_probe.OPENCLAW_EXECUTABLE_SHA256", digest):
            identity = resolve_openclaw_identity(executable)
            self.assertEqual(identity.path, executable)
            with self.assertRaisesRegex(OpenClawProbeError, "absolute canonical"):
                resolve_openclaw_identity(Path("openclaw.mjs"))
            symlink = root / "openclaw-link"
            symlink.symlink_to(executable)
            with self.assertRaisesRegex(OpenClawProbeError, "absolute canonical"):
                resolve_openclaw_identity(symlink)

    def test_identity_fails_closed_on_wrong_version_or_hash(self) -> None:
        holder, root, _ = fixture_root()
        self.addCleanup(holder.cleanup)
        executable = executable_for(root)
        digest = hashlib.sha256(executable.read_bytes()).hexdigest()
        with (
            mock.patch("agent_team.openclaw_probe.OPENCLAW_EXECUTABLE_SHA256", digest),
            self.assertRaisesRegex(OpenClawProbeError, "exact OpenClaw"),
        ):
            resolve_openclaw_identity(executable, "OpenClaw 2026.7.0 (2d2ddc4)")
        with self.assertRaisesRegex(OpenClawProbeError, "SHA-256"):
            resolve_openclaw_identity(
                executable, f"OpenClaw {OPENCLAW_VERSION} ({OPENCLAW_BUILD})"
            )

    def test_hash_attestation_is_rechecked_when_building_manifest(self) -> None:
        holder, root, _ = fixture_root()
        self.addCleanup(holder.cleanup)
        executable = executable_for(root)
        digest = hashlib.sha256(executable.read_bytes()).hexdigest()
        with mock.patch("agent_team.openclaw_probe.OPENCLAW_EXECUTABLE_SHA256", digest):
            resolved = resolve_openclaw_identity(executable)
            forged = OpenClawIdentity(
                executable, OPENCLAW_VERSION, digest, OPENCLAW_BUILD
            )
            with self.assertRaisesRegex(ReceiptValidationError, "attestation"):
                build_probe_manifest(forged, "read-only")
            executable.write_text("replaced", encoding="utf-8")
            with self.assertRaisesRegex(ReceiptValidationError, "identity"):
                build_probe_manifest(resolved, "read-only")

        with self.assertRaisesRegex(OpenClawProbeError, "pinned"):
            OpenClawIdentity(executable, OPENCLAW_VERSION, "b" * 64, OPENCLAW_BUILD)


class OpenClawPolicyTest(unittest.TestCase):
    def test_direct_sandbox_off_is_always_non_candidate(self) -> None:
        cell = direct_sandbox_off_cell()
        self.assertEqual(cell.cell_id, "direct-sandbox-off")
        self.assertEqual(cell.status, OpenClawProbeStatus.NOT_RUN)
        self.assertIn("sandbox-off", cell.reason)

    def test_mount_policy_requires_disposable_parent_owner_mode_and_clean_names(
        self,
    ) -> None:
        holder, root, mount = fixture_root()
        self.addCleanup(holder.cleanup)
        valid = config_for(root, mount)
        validate_docker_sandbox_config(valid)

        for field, replacement, message in (
            ("image", "openclaw-sandbox:latest", "digest"),
            ("network", "host", "network=none"),
            ("privileged", True, "privileged"),
            ("docker_socket", True, "socket"),
            ("credential_mounts", ("/secret",), "credential"),
            ("mount_target", "/tmp", "mount target"),
            ("disposable_parent", root.parent, "disposable"),
        ):
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(OpenClawProbeError, message),
            ):
                validate_docker_sandbox_config(replace(valid, **{field: replacement}))

        mount.chmod(0o755)
        with self.assertRaisesRegex(OpenClawProbeError, "mode"):
            validate_docker_sandbox_config(valid)
        mount.chmod(0o700)
        (mount / ".git").mkdir()
        with self.assertRaisesRegex(OpenClawProbeError, "reserved"):
            validate_docker_sandbox_config(valid)

    def test_mount_policy_rejects_symlink_parent_and_secret_entries(self) -> None:
        holder, root, mount = fixture_root()
        self.addCleanup(holder.cleanup)
        symlink_parent = root / "parent-link"
        symlink_parent.symlink_to(root, target_is_directory=True)
        linked_mount = symlink_parent / "disposable"
        with self.assertRaisesRegex(OpenClawProbeError, "canonical"):
            validate_docker_sandbox_config(
                replace(
                    config_for(root, mount),
                    disposable_parent=symlink_parent,
                    mount_source=linked_mount,
                )
            )
        (mount / ".env").write_text("not persisted", encoding="utf-8")
        with self.assertRaisesRegex(OpenClawProbeError, "reserved"):
            validate_docker_sandbox_config(config_for(root, mount))


class OpenClawPreflightTest(unittest.TestCase):
    def _probe(self) -> tuple[tempfile.TemporaryDirectory[str], Path, Path, Path, str]:
        holder, root, mount = fixture_root()
        executable = executable_for(root)
        digest = hashlib.sha256(executable.read_bytes()).hexdigest()
        return holder, root, mount, executable, digest

    def test_arbitrary_image_is_blocked_before_any_docker_command(self) -> None:
        holder, root, mount, executable, digest = self._probe()
        self.addCleanup(holder.cleanup)
        config = config_for(root, mount, image="attacker/image@sha256:" + "a" * 64)
        runner = FakeRunner({(str(executable), "--version"): version_result()})
        with mock.patch("agent_team.openclaw_probe.OPENCLAW_EXECUTABLE_SHA256", digest):
            report = OpenClawProbe(executable, config, runner).preflight()
            self.assertEqual(report.docker.reason, "blocked-image")
            self.assertEqual(runner.calls, [(str(executable), "--version")])
            self.assertEqual(report.receipt.judgment.status, "blocked")
            self.assertTrue(
                all(not phase.attempted for phase in report.receipt.receipt.phases)
            )

    def test_unverified_relative_or_symlink_executable_is_never_run(self) -> None:
        holder, root, mount, executable, digest = self._probe()
        self.addCleanup(holder.cleanup)
        config = config_for(root, mount)
        symlink = root / "openclaw-link"
        symlink.symlink_to(executable)
        for candidate in (Path("openclaw.mjs"), symlink):
            with self.subTest(candidate=candidate):
                runner = FakeRunner({})
                with (
                    mock.patch(
                        "agent_team.openclaw_probe.OPENCLAW_EXECUTABLE_SHA256", digest
                    ),
                    self.assertRaises(OpenClawProbeError),
                ):
                    OpenClawProbe(candidate, config, runner).preflight()
                self.assertEqual(runner.calls, [])

    def test_hash_mismatch_is_rejected_before_version_probe(self) -> None:
        holder, root, mount, executable, _digest = self._probe()
        self.addCleanup(holder.cleanup)
        config = config_for(root, mount)
        runner = FakeRunner({(str(executable), "--version"): version_result()})
        with self.assertRaisesRegex(OpenClawProbeError, "SHA-256"):
            OpenClawProbe(executable, config, runner).preflight()
        self.assertEqual(runner.calls, [])

    def test_arbitrary_context_is_blocked_before_daemon_probe(self) -> None:
        holder, root, mount, executable, digest = self._probe()
        self.addCleanup(holder.cleanup)
        config = config_for(root, mount)
        runner = FakeRunner({(str(executable), "--version"): version_result()})
        with (
            mock.patch("agent_team.openclaw_probe.OPENCLAW_EXECUTABLE_SHA256", digest),
            mock.patch(
                "agent_team.openclaw_probe.AUDITED_OPENCLAW_IMAGE_PIN", config.image
            ),
        ):
            report = OpenClawProbe(executable, config, runner).preflight()
        self.assertEqual(report.docker.reason, "blocked-context")
        self.assertEqual(runner.calls, [(str(executable), "--version")])

    def test_daemon_failure_is_blocked_before_image_inspection(self) -> None:
        holder, root, mount, executable, digest = self._probe()
        self.addCleanup(holder.cleanup)
        config = config_for(root, mount)
        responses = docker_responses(config)
        version_command = next(command for command in responses if "version" in command)
        responses[version_command] = ProcessResult(
            1, "", "Cannot connect to the Docker daemon"
        )
        runner = FakeRunner(
            {(str(executable), "--version"): version_result(), **responses}
        )
        endpoint_digest = hashlib.sha256(b"unix:///private/docker.sock").hexdigest()
        with (
            mock.patch("agent_team.openclaw_probe.OPENCLAW_EXECUTABLE_SHA256", digest),
            mock.patch(
                "agent_team.openclaw_probe.AUDITED_OPENCLAW_IMAGE_PIN", config.image
            ),
            mock.patch(
                "agent_team.openclaw_probe.AUDITED_DOCKER_CONTEXT", config.context
            ),
            mock.patch(
                "agent_team.openclaw_probe.AUDITED_DOCKER_ENDPOINT_SHA256",
                endpoint_digest,
            ),
        ):
            report = OpenClawProbe(executable, config, runner).preflight()
        self.assertEqual(report.docker.reason, "docker-daemon-unavailable")
        self.assertFalse(any("image" in command for command in runner.calls))
        self.assertFalse(
            any(
                command[3] in {"create", "start", "rm", "run"}
                for command in runner.calls
                if len(command) > 3
            )
        )

    def test_endpoint_and_image_repository_must_match_audited_pins(self) -> None:
        holder, root, mount, executable, digest = self._probe()
        self.addCleanup(holder.cleanup)
        config = config_for(root, mount)
        responses = docker_responses(config, endpoint="unix:///other.sock")
        runner = FakeRunner(
            {(str(executable), "--version"): version_result(), **responses}
        )
        endpoint_digest = hashlib.sha256(b"unix:///private/docker.sock").hexdigest()
        with (
            mock.patch("agent_team.openclaw_probe.OPENCLAW_EXECUTABLE_SHA256", digest),
            mock.patch(
                "agent_team.openclaw_probe.AUDITED_OPENCLAW_IMAGE_PIN", config.image
            ),
            mock.patch(
                "agent_team.openclaw_probe.AUDITED_DOCKER_CONTEXT", config.context
            ),
            mock.patch(
                "agent_team.openclaw_probe.AUDITED_DOCKER_ENDPOINT_SHA256",
                endpoint_digest,
            ),
        ):
            report = OpenClawProbe(executable, config, runner).preflight()
        self.assertEqual(report.docker.reason, "blocked-context-endpoint")
        self.assertFalse(any("image" in command for command in runner.calls))

        responses = docker_responses(config)
        image_command = next(command for command in responses if "image" in command)
        responses[image_command] = ProcessResult(
            0,
            json.dumps(["other/image@sha256:" + "a" * 64]),
            "",
        )
        runner = FakeRunner(
            {(str(executable), "--version"): version_result(), **responses}
        )
        with (
            mock.patch("agent_team.openclaw_probe.OPENCLAW_EXECUTABLE_SHA256", digest),
            mock.patch(
                "agent_team.openclaw_probe.AUDITED_OPENCLAW_IMAGE_PIN", config.image
            ),
            mock.patch(
                "agent_team.openclaw_probe.AUDITED_DOCKER_CONTEXT", config.context
            ),
            mock.patch(
                "agent_team.openclaw_probe.AUDITED_DOCKER_ENDPOINT_SHA256",
                endpoint_digest,
            ),
        ):
            report = OpenClawProbe(executable, config, runner).preflight()
        self.assertEqual(report.docker.reason, "blocked-image")

    def test_preflight_never_calls_container_lifecycle_or_provider_commands(
        self,
    ) -> None:
        holder, root, mount, executable, digest = self._probe()
        self.addCleanup(holder.cleanup)
        config = config_for(root, mount)
        responses = docker_responses(config)
        endpoint_digest = hashlib.sha256(b"unix:///private/docker.sock").hexdigest()
        runner = FakeRunner(
            {(str(executable), "--version"): version_result(), **responses}
        )
        with (
            mock.patch("agent_team.openclaw_probe.OPENCLAW_EXECUTABLE_SHA256", digest),
            mock.patch(
                "agent_team.openclaw_probe.AUDITED_OPENCLAW_IMAGE_PIN", config.image
            ),
            mock.patch(
                "agent_team.openclaw_probe.AUDITED_DOCKER_CONTEXT", config.context
            ),
            mock.patch(
                "agent_team.openclaw_probe.AUDITED_DOCKER_ENDPOINT_SHA256",
                endpoint_digest,
            ),
        ):
            report = OpenClawProbe(executable, config, runner).preflight()
        self.assertEqual(report.status, OpenClawProbeStatus.READY)
        self.assertFalse(
            any(
                command[3] in {"create", "start", "rm", "run", "pull"}
                for command in runner.calls
                if len(command) > 3
            )
        )
        self.assertFalse(
            any(
                Path(command[0]).name == "openclaw" and command[-1] != "--version"
                for command in runner.calls
            )
        )
        self.assertFalse(hasattr(OpenClawProbe, "run_live"))

    def test_version_timeout_fails_before_docker(self) -> None:
        holder, root, mount, executable, digest = self._probe()
        self.addCleanup(holder.cleanup)
        config = config_for(root, mount)
        runner = FakeRunner(
            {(str(executable), "--version"): ProcessResult(-1, "", "", timed_out=True)}
        )
        with (
            mock.patch("agent_team.openclaw_probe.OPENCLAW_EXECUTABLE_SHA256", digest),
            self.assertRaisesRegex(OpenClawProbeError, "timed out"),
        ):
            OpenClawProbe(executable, config, runner).preflight()
        self.assertEqual(runner.calls, [(str(executable), "--version")])


class OpenClawReceiptTest(unittest.TestCase):
    def test_serializer_recomputes_judgment_and_keeps_blocked_phases_unattempted(
        self,
    ) -> None:
        holder, root, _mount = fixture_root()
        self.addCleanup(holder.cleanup)
        executable = executable_for(root)
        digest = hashlib.sha256(executable.read_bytes()).hexdigest()
        with mock.patch("agent_team.openclaw_probe.OPENCLAW_EXECUTABLE_SHA256", digest):
            resolved = resolve_openclaw_identity(executable)
            bundle = build_blocked_receipt(resolved, "read-only", "docker")
            self.assertEqual(bundle.judgment.status, "blocked")
            manifest = serialize_openclaw_manifest(resolved, "read-only")
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

    def test_forged_bundle_profile_or_judgment_is_not_accepted(self) -> None:
        holder, root, _mount = fixture_root()
        self.addCleanup(holder.cleanup)
        executable = executable_for(root)
        digest = hashlib.sha256(executable.read_bytes()).hexdigest()
        with mock.patch("agent_team.openclaw_probe.OPENCLAW_EXECUTABLE_SHA256", digest):
            resolved = resolve_openclaw_identity(executable)
            bundle = build_blocked_receipt(resolved, "read-only", "docker")
            forged = ReceiptBundle(bundle.receipt, resolved, "read-only")
            self.assertEqual(forged.judgment, bundle.judgment)
            mismatched = build_blocked_receipt(resolved, "workspace-write", "docker")
            with self.assertRaisesRegex(ReceiptValidationError, "profile"):
                serialize_openclaw_receipt(
                    ReceiptBundle(mismatched.receipt, resolved, "read-only")
                )


if __name__ == "__main__":
    unittest.main()
