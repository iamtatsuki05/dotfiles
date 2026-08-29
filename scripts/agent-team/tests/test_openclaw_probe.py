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
    OPENCLAW_EXECUTABLE_SHA256,
    OPENCLAW_VERSION,
    DockerSandboxConfig,
    LiveAuthorization,
    OpenClawIdentity,
    OpenClawProbe,
    OpenClawProbeError,
    OpenClawProbeStatus,
    build_blocked_receipt,
    build_probe_manifest,
    direct_sandbox_off_cell,
    resolve_openclaw_identity,
    validate_docker_sandbox_config,
)
from agent_team.probe_receipts import serialize_receipt


class FakeDockerRunner:
    def __init__(self, responses: dict[tuple[str, ...], ProcessResult]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...], *, timeout_seconds: float) -> ProcessResult:
        del timeout_seconds
        self.calls.append(argv)
        return self.responses.get(argv, ProcessResult(1, "", "unexpected command"))


class SequencedDockerRunner:
    def __init__(
        self, config: DockerSandboxConfig, executable: Path, start: ProcessResult
    ) -> None:
        self.config = config
        self.executable = executable
        self.start = start
        self.calls: list[tuple[str, ...]] = []
        self.cleanup_result = ProcessResult(0, "", "")
        self.inspect_result = ProcessResult(1, "", "no such container")

    def run(self, argv: tuple[str, ...], *, timeout_seconds: float) -> ProcessResult:
        del timeout_seconds
        self.calls.append(argv)
        action = (
            argv[3]
            if len(argv) > 3
            and argv[:3]
            == (
                "docker",
                "--context",
                self.config.context,
            )
            else ""
        )
        if argv == (str(self.executable), "--version"):
            return version_result()
        if action == "context":
            return ProcessResult(0, self.config.context + "\n", "")
        if action == "version":
            return ProcessResult(0, "29.7.2\t28.4.0\n", "")
        if action == "image":
            digest = self.config.image.split("@", 1)[1]
            return ProcessResult(0, json.dumps([f"openclaw-sandbox@{digest}"]), "")
        if action == "create":
            return ProcessResult(0, "container-id\n", "")
        if action == "start":
            return self.start
        if action == "rm":
            return self.cleanup_result
        if action == "inspect":
            return self.inspect_result
        return ProcessResult(1, "", "unexpected command")


def sandbox_config(root: Path, *, image: str | None = None) -> DockerSandboxConfig:
    mount = root / "disposable"
    mount.mkdir()
    return DockerSandboxConfig(
        context="rancher-desktop",
        image=image or f"openclaw-sandbox@sha256:{'a' * 64}",
        mount_source=mount,
        session_key="agent:issue9-openclaw:nonce1234",
    )


def version_result() -> ProcessResult:
    return ProcessResult(0, f"OpenClaw {OPENCLAW_VERSION} ({OPENCLAW_BUILD})\n", "")


def docker_responses(
    config: DockerSandboxConfig,
) -> dict[tuple[str, ...], ProcessResult]:
    prefix = ("docker", "--context", config.context)
    image_digest = config.image.split("@", 1)[1]
    return {
        (*prefix, "context", "show"): ProcessResult(0, config.context + "\n", ""),
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
        ): ProcessResult(
            0,
            json.dumps([f"openclaw-sandbox:{OPENCLAW_VERSION}@{image_digest} "]) + "\n",
            "",
        ),
    }


class OpenClawIdentityTest(unittest.TestCase):
    def test_identity_requires_pinned_version_build_and_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            executable = Path(temp_dir) / "openclaw"
            executable.write_text("pinned executable", encoding="utf-8")
            digest = hashlib.sha256(executable.read_bytes()).hexdigest()
            with mock.patch(
                "agent_team.openclaw_probe.OPENCLAW_EXECUTABLE_SHA256", digest
            ):
                identity = resolve_openclaw_identity(
                    executable,
                    f"OpenClaw {OPENCLAW_VERSION} ({OPENCLAW_BUILD})",
                )
            self.assertEqual(identity.version, OPENCLAW_VERSION)
            self.assertEqual(identity.build, OPENCLAW_BUILD)
            self.assertEqual(identity.sha256, digest)

            with self.assertRaisesRegex(OpenClawProbeError, "exact OpenClaw"):
                resolve_openclaw_identity(
                    executable, f"OpenClaw 2026.7.0 ({OPENCLAW_BUILD})"
                )

    def test_identity_rejects_hash_drift_before_any_docker_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            executable = Path(temp_dir) / "openclaw"
            executable.write_text("different executable", encoding="utf-8")
            with self.assertRaisesRegex(OpenClawProbeError, "SHA-256"):
                resolve_openclaw_identity(
                    executable,
                    f"OpenClaw {OPENCLAW_VERSION} ({OPENCLAW_BUILD})",
                )


class OpenClawPolicyTest(unittest.TestCase):
    def test_direct_sandbox_off_is_never_a_safe_candidate(self) -> None:
        cell = direct_sandbox_off_cell()
        self.assertEqual(cell.cell_id, "direct-sandbox-off")
        self.assertEqual(cell.status, "not-run")
        self.assertNotEqual(cell.status, "candidate")
        self.assertIn("sandbox-off", cell.reason)

    def test_config_requires_immutable_image_and_closed_docker_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            valid = sandbox_config(root)
            validate_docker_sandbox_config(valid)
            validate_docker_sandbox_config(
                replace(valid, mount_mode="rw", mount_target="/workspace")
            )

            for field, replacement, message in (
                (
                    "image",
                    "openclaw-sandbox:latest",
                    "digest",
                ),
                ("network", "host", "network=none"),
                ("privileged", True, "privileged"),
                ("docker_socket", True, "socket"),
                ("credential_mounts", ("/secret",), "credential"),
                ("mount_target", "/tmp", "mount target"),
                ("model", "api-key-model", "sensitive"),
                ("quota_budget", 2, "quota budget"),
                ("mount_source", Path("/tmp/openclaw,readonly"), "unsafe character"),
            ):
                with self.subTest(field=field):
                    config = replace(valid, **{field: replacement})
                    with self.assertRaisesRegex(OpenClawProbeError, message):
                        validate_docker_sandbox_config(config)

    def test_config_rejects_home_mount_and_non_disposable_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            valid = sandbox_config(root)
            home_mount = replace(valid, mount_source=Path.home())
            with self.assertRaisesRegex(OpenClawProbeError, "home"):
                validate_docker_sandbox_config(home_mount)

            symlink = root / "symlink"
            symlink.symlink_to(valid.mount_source, target_is_directory=True)
            symlink_mount = replace(valid, mount_source=symlink)
            with self.assertRaisesRegex(OpenClawProbeError, "symlink"):
                validate_docker_sandbox_config(symlink_mount)

    def test_config_rejects_non_boolean_and_non_tuple_safety_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            valid = sandbox_config(Path(temp_dir))
            invalid_values = (
                ("privileged", 1),
                ("auto_pull", 1),
                ("cap_drop", ["ALL"]),
                ("environment_allowlist", ["PATH"]),
                ("session_key", 42),
            )
            for field, value in invalid_values:
                with self.subTest(field=field):
                    config = replace(valid, **{field: value})
                    with self.assertRaises(OpenClawProbeError):
                        validate_docker_sandbox_config(config)


class OpenClawPreflightTest(unittest.TestCase):
    def test_daemon_unavailable_returns_blocked_receipts_without_side_effects(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = sandbox_config(root)
            executable = root / "openclaw"
            executable.write_text("pinned executable", encoding="utf-8")
            digest = hashlib.sha256(executable.read_bytes()).hexdigest()
            responses = docker_responses(config)
            prefix = ("docker", "--context", config.context)
            responses[
                (
                    *prefix,
                    "version",
                    "--format",
                    "{{.Client.Version}}\\t{{.Server.Version}}",
                )
            ] = ProcessResult(1, "", "Cannot connect to the Docker daemon")
            runner = FakeDockerRunner(
                {
                    (str(executable), "--version"): version_result(),
                    **responses,
                }
            )
            with mock.patch(
                "agent_team.openclaw_probe.OPENCLAW_EXECUTABLE_SHA256", digest
            ):
                report = OpenClawProbe(executable, config, runner).preflight()

            self.assertEqual(report.status, OpenClawProbeStatus.BLOCKED)
            self.assertEqual(report.docker.status, "blocked")
            self.assertEqual(report.docker.reason, "docker-daemon-unavailable")
            self.assertEqual(report.receipt.judgment.status, "blocked")
            self.assertIn("blocked-docker", report.receipt.judgment.reason_codes)
            self.assertEqual(
                [phase.attempted for phase in report.receipt.receipt.phases],
                [False] * len(report.receipt.receipt.phases),
            )
            self.assertFalse(
                any(
                    command[3] in {"run", "create", "start", "pull"}
                    for command in runner.calls
                    if len(command) > 3 and command[0] == "docker"
                )
            )
            self.assertFalse(
                any(
                    command
                    and Path(command[0]).name == "openclaw"
                    and command[-1] not in {"--version"}
                    for command in runner.calls
                )
            )

    def test_context_and_image_digest_are_verified_before_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = sandbox_config(root)
            executable = root / "openclaw"
            executable.write_text("pinned executable", encoding="utf-8")
            digest = hashlib.sha256(executable.read_bytes()).hexdigest()
            responses = docker_responses(config)
            responses[("docker", "--context", config.context, "context", "show")] = (
                ProcessResult(0, "wrong-context\n", "")
            )
            runner = FakeDockerRunner(
                {
                    (str(executable), "--version"): version_result(),
                    **responses,
                }
            )
            with mock.patch(
                "agent_team.openclaw_probe.OPENCLAW_EXECUTABLE_SHA256", digest
            ):
                report = OpenClawProbe(executable, config, runner).preflight()
            self.assertEqual(report.status, OpenClawProbeStatus.BLOCKED)
            self.assertEqual(report.docker.reason, "docker-context-mismatch")
            self.assertNotIn(
                (
                    "docker",
                    "--context",
                    config.context,
                    "image",
                    "inspect",
                    config.image,
                    "--format",
                    "{{json .RepoDigests}}",
                ),
                runner.calls,
            )

    def test_ready_preflight_is_still_not_run_until_explicit_live_authorization(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = sandbox_config(root)
            executable = root / "openclaw"
            executable.write_text("pinned executable", encoding="utf-8")
            digest = hashlib.sha256(executable.read_bytes()).hexdigest()
            runner = FakeDockerRunner(
                {
                    (str(executable), "--version"): version_result(),
                    **docker_responses(config),
                }
            )
            with mock.patch(
                "agent_team.openclaw_probe.OPENCLAW_EXECUTABLE_SHA256", digest
            ):
                probe = OpenClawProbe(executable, config, runner)
                report = probe.preflight()
                before_live = len(runner.calls)
                result = probe.run_live(LiveAuthorization())

            self.assertEqual(report.status, OpenClawProbeStatus.READY)
            self.assertEqual(result.status, "blocked")
            self.assertIn("explicit", result.reason or "")
            self.assertEqual(len(runner.calls), before_live)

    def test_malformed_authorization_cannot_enable_a_live_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = sandbox_config(root)
            executable = root / "openclaw"
            executable.write_text("pinned executable", encoding="utf-8")
            runner = FakeDockerRunner({})
            probe = OpenClawProbe(executable, config, runner)
            malformed = LiveAuthorization(1, True, config.session_key)  # type: ignore[arg-type]
            result = probe.run_live(malformed)
            self.assertEqual(result.status, "blocked")
            self.assertEqual(runner.calls, [])

    def test_daemon_timeout_is_blocked_without_image_inspection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = sandbox_config(root)
            executable = root / "openclaw"
            executable.write_text("pinned executable", encoding="utf-8")
            digest = hashlib.sha256(executable.read_bytes()).hexdigest()
            prefix = ("docker", "--context", config.context)
            runner = FakeDockerRunner(
                {
                    (str(executable), "--version"): version_result(),
                    (*prefix, "context", "show"): ProcessResult(
                        0, config.context + "\n", ""
                    ),
                    (
                        *prefix,
                        "version",
                        "--format",
                        "{{.Client.Version}}\\t{{.Server.Version}}",
                    ): ProcessResult(-1, "", "", timed_out=True),
                }
            )
            with mock.patch(
                "agent_team.openclaw_probe.OPENCLAW_EXECUTABLE_SHA256", digest
            ):
                report = OpenClawProbe(executable, config, runner).preflight()
            self.assertEqual(report.docker.reason, "docker-daemon-timeout")
            self.assertFalse(any("image" in command for command in runner.calls))

    def test_image_digest_mismatch_is_blocked_without_container_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = sandbox_config(root)
            executable = root / "openclaw"
            executable.write_text("pinned executable", encoding="utf-8")
            digest = hashlib.sha256(executable.read_bytes()).hexdigest()
            responses = docker_responses(config)
            image_command = next(command for command in responses if "image" in command)
            responses[image_command] = ProcessResult(
                0, json.dumps(["openclaw-sandbox@sha256:" + "b" * 64]), ""
            )
            runner = FakeDockerRunner(
                {(str(executable), "--version"): version_result(), **responses}
            )
            with mock.patch(
                "agent_team.openclaw_probe.OPENCLAW_EXECUTABLE_SHA256", digest
            ):
                report = OpenClawProbe(executable, config, runner).preflight()
            self.assertEqual(report.docker.reason, "docker-image-digest-mismatch")
            self.assertFalse(
                any(
                    command[3] == "create"
                    for command in runner.calls
                    if len(command) > 3
                )
            )


class OpenClawReceiptTest(unittest.TestCase):
    def test_blocked_receipt_is_redacted_and_profile_cells_are_separate(self) -> None:
        identity = resolve_openclaw_identity_for_test()
        read_only = build_blocked_receipt(identity, "read-only", "docker")
        workspace_write = build_blocked_receipt(identity, "workspace-write", "docker")
        self.assertEqual(read_only.judgment.status, "blocked")
        self.assertEqual(workspace_write.judgment.status, "blocked")
        self.assertNotEqual(
            read_only.receipt.identity.sandbox_policy_id,
            workspace_write.receipt.identity.sandbox_policy_id,
        )
        serialized = serialize_receipt(read_only.receipt)
        self.assertNotIn(str(identity.path), serialized)
        self.assertNotIn("raw-log", serialized)
        self.assertNotIn("prompt text", serialized)
        self.assertNotIn("OPENAI_API_KEY=", serialized)

    def test_manifest_exposes_only_redacted_path_and_fixed_identity(self) -> None:
        identity = resolve_openclaw_identity_for_test()
        manifest = build_probe_manifest(identity, "read-only")
        self.assertEqual(
            manifest.identity.executable.version,
            f"OpenClaw {OPENCLAW_VERSION} ({OPENCLAW_BUILD})",
        )
        self.assertEqual(
            manifest.identity.executable.path,
            "/redacted/openclaw/2026.7.1/bin/openclaw",
        )
        self.assertNotIn("/private/user", manifest.identity.executable.path)


class OpenClawLiveRunTest(unittest.TestCase):
    def _probe(
        self, root: Path, start: ProcessResult
    ) -> tuple[OpenClawProbe, SequencedDockerRunner, str]:
        config = sandbox_config(root)
        executable = root / "openclaw"
        executable.write_text("pinned executable", encoding="utf-8")
        digest = hashlib.sha256(executable.read_bytes()).hexdigest()
        runner = SequencedDockerRunner(config, executable, start)
        patcher = mock.patch(
            "agent_team.openclaw_probe.OPENCLAW_EXECUTABLE_SHA256", digest
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return OpenClawProbe(executable, config, runner), runner, config.session_key

    def test_provider_timeout_is_reported_and_exact_container_is_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            probe, runner, session_key = self._probe(
                Path(temp_dir), ProcessResult(-1, "", "", timed_out=True)
            )
            result = probe.run_live(LiveAuthorization(True, True, session_key))
            self.assertEqual(result.status, "timeout")
            self.assertTrue(result.timed_out)
            self.assertFalse(result.cleanup.has_residuals)
            self.assertIsNotNone(result.container_name)
            self.assertIn(
                (
                    "docker",
                    "--context",
                    "rancher-desktop",
                    "rm",
                    "--force",
                    result.container_name,
                ),
                runner.calls,
            )

    def test_provider_failure_is_reported_and_does_not_skip_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            probe, runner, session_key = self._probe(
                Path(temp_dir), ProcessResult(17, "", "provider failed")
            )
            result = probe.run_live(LiveAuthorization(True, True, session_key))
            self.assertEqual(result.status, "failed")
            self.assertFalse(result.cleanup.has_residuals)
            self.assertTrue(
                any(len(call) > 3 and call[3] == "rm" for call in runner.calls)
            )
            self.assertTrue(
                all(
                    "provider failed" not in part
                    for call in runner.calls
                    for part in call
                )
            )

    def test_cleanup_residual_rejects_even_after_successful_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            probe, runner, session_key = self._probe(
                Path(temp_dir), ProcessResult(0, "provider output", "")
            )
            runner.inspect_result = ProcessResult(0, "container still exists", "")
            result = probe.run_live(LiveAuthorization(True, True, session_key))
            self.assertEqual(result.status, "rejected")
            self.assertEqual(result.reason, "container-cleanup-residual")
            self.assertEqual(result.cleanup.containers, 1)

    def test_image_failure_blocks_container_creation_and_provider_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = sandbox_config(root)
            executable = root / "openclaw"
            executable.write_text("pinned executable", encoding="utf-8")
            digest = hashlib.sha256(executable.read_bytes()).hexdigest()
            runner = FakeDockerRunner(
                {
                    (str(executable), "--version"): version_result(),
                    (
                        "docker",
                        "--context",
                        config.context,
                        "context",
                        "show",
                    ): ProcessResult(0, config.context + "\n", ""),
                    (
                        "docker",
                        "--context",
                        config.context,
                        "version",
                        "--format",
                        "{{.Client.Version}}\\t{{.Server.Version}}",
                    ): ProcessResult(0, "29.7.2\t28.4.0\n", ""),
                    (
                        "docker",
                        "--context",
                        config.context,
                        "image",
                        "inspect",
                        config.image,
                        "--format",
                        "{{json .RepoDigests}}",
                    ): ProcessResult(1, "", "image not found"),
                }
            )
            with mock.patch(
                "agent_team.openclaw_probe.OPENCLAW_EXECUTABLE_SHA256", digest
            ):
                probe = OpenClawProbe(executable, config, runner)
                result = probe.run_live(
                    LiveAuthorization(True, True, config.session_key)
                )
            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.reason, "docker-image-unavailable")
            self.assertFalse(
                any(
                    call[3] in {"create", "start"}
                    for call in runner.calls
                    if len(call) > 3
                )
            )

    def test_identity_drift_after_preflight_blocks_container_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = sandbox_config(root)
            executable = root / "openclaw"
            executable.write_text("pinned executable", encoding="utf-8")
            digest = hashlib.sha256(executable.read_bytes()).hexdigest()
            runner = SequencedDockerRunner(
                config,
                executable,
                ProcessResult(0, "provider output", ""),
            )
            original_run = runner.run
            mutated = False

            def mutate_after_image(
                argv: tuple[str, ...], *, timeout_seconds: float
            ) -> ProcessResult:
                nonlocal mutated
                result = original_run(argv, timeout_seconds=timeout_seconds)
                if not mutated and len(argv) > 3 and argv[3] == "image":
                    executable.write_text("replaced executable", encoding="utf-8")
                    mutated = True
                return result

            runner.run = mutate_after_image  # type: ignore[method-assign]
            with mock.patch(
                "agent_team.openclaw_probe.OPENCLAW_EXECUTABLE_SHA256", digest
            ):
                result = OpenClawProbe(executable, config, runner).run_live(
                    LiveAuthorization(True, True, config.session_key)
                )
            self.assertEqual(result.status, "rejected")
            self.assertEqual(result.reason, "openclaw-identity-drift")
            self.assertFalse(
                any(call[3] == "create" for call in runner.calls if len(call) > 3)
            )


def resolve_openclaw_identity_for_test() -> OpenClawIdentity:
    return OpenClawIdentity(
        Path(
            "/private/user/.local/share/mise/installs/npm-openclaw/2026.7.1/bin/openclaw"
        ),
        OPENCLAW_VERSION,
        OPENCLAW_EXECUTABLE_SHA256,
        OPENCLAW_BUILD,
    )


if __name__ == "__main__":
    unittest.main()
