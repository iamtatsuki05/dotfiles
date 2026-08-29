from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Literal, cast

from agent_team.adapters import ExecutionError, ProcessResult
from agent_team.hermes_probe import (
    EXTERNAL_DOCKER_POLICY_ID,
    HERMES_ENVIRONMENT_ALLOWLIST,
    HERMES_LAUNCHER_IDENTITY,
    HERMES_RELEASE,
    HERMES_SOURCE_COMMIT,
    HERMES_SOURCE_DESCRIBE,
    HERMES_TARGET_IDENTITY,
    HERMES_VERSION,
    HERMES_VERSION_BANNER,
    HISTORICAL_PROVENANCE,
    HermesExecutableIdentity,
    HermesExternalPreflight,
    HermesProbeError,
    HermesProbeReceipt,
    HermesProfile,
    build_blocked_external_receipt,
    build_probe_manifest,
    build_rejected_local_receipt,
    build_unaccepted_acp_receipt,
    inspect_hermes_identity,
    inspect_installed_hermes,
    parse_launcher_target,
    preflight_external_sandbox,
    serialize_hermes_receipt,
)
from agent_team.probe_receipts import (
    CleanupInventory,
    ExecutableIdentity,
    Manifest,
    PhaseReceipt,
    Receipt,
    ToolEvidence,
    required_phases_for_profile,
)


def hermes_identity() -> HermesExecutableIdentity:
    return HermesExecutableIdentity(
        launcher_path=Path("/private/hermes/launcher"),
        target_path=Path("/private/hermes/bin/hermes"),
        launcher=HERMES_LAUNCHER_IDENTITY,
        target=HERMES_TARGET_IDENTITY,
        version=HERMES_VERSION,
        release=HERMES_RELEASE,
        source_commit=HERMES_SOURCE_COMMIT,
        source_describe=HERMES_SOURCE_DESCRIBE,
    )


def executable() -> ExecutableIdentity:
    return ExecutableIdentity(
        str(hermes_identity().target_path),
        HERMES_VERSION,
        HERMES_TARGET_IDENTITY.sha256,
    )


def manifest(profile: HermesProfile = "direct-local-oneshot") -> Manifest:
    return build_probe_manifest(
        profile=profile,
        workspace=Path("/private/hermes-workspace"),
        executable=executable(),
        file_identity=hermes_identity().target,
        hermes_identity=hermes_identity(),
    )


def expected_phase(phase_id: str) -> tuple[ToolEvidence, ...]:
    if phase_id == "positive-read":
        return (ToolEvidence("filesystem", "read", "workspace", "allowed"),)
    if phase_id == "outside-path":
        return (
            ToolEvidence("filesystem", "read", "outside", "denied"),
            ToolEvidence("filesystem", "write", "outside", "denied"),
        )
    if phase_id in {"symlink", "git", "secret"}:
        return (
            ToolEvidence("filesystem", "read", phase_id, "denied"),
            ToolEvidence("filesystem", "write", phase_id, "denied"),
        )
    if phase_id in {"local-network", "external-network"}:
        return (ToolEvidence("network", "connect", phase_id, "denied"),)
    if phase_id == "process":
        return (ToolEvidence("process", "spawn", "process", "denied"),)
    return (ToolEvidence("cleanup", "inspect", "cleanup", "clean"),)


def candidate_receipt(profile_manifest: Manifest) -> Receipt:
    phases = tuple(
        PhaseReceipt(
            spec.phase_id,
            spec.expected_result,
            True,
            True,
            "passed",
            0,
            False,
            expected_phase(spec.phase_id),
            CleanupInventory(),
        )
        for spec in required_phases_for_profile("read-only")
    )
    return Receipt(profile_manifest.identity, None, phases)


class HermesProbeContractTest(unittest.TestCase):
    def test_exact_identity_requires_version_commit_and_every_file_identity_field(
        self,
    ) -> None:
        observed = inspect_hermes_identity(
            hermes_identity().launcher,
            hermes_identity().target,
            launcher_path=hermes_identity().launcher_path,
            target_path=hermes_identity().target_path,
            version_banner=HERMES_VERSION_BANNER,
            source_commit=HERMES_SOURCE_COMMIT,
            source_describe=HERMES_SOURCE_DESCRIBE,
        )
        self.assertEqual(observed, hermes_identity())

        for field, changed in (
            ("target hash", replace(observed.target, sha256="0" * 64)),
            ("target device", replace(observed.target, device=999)),
            ("target inode", replace(observed.target, inode=999)),
            ("launcher mtime", replace(observed.launcher, mtime_ns=999)),
        ):
            with self.subTest(field=field), self.assertRaises(HermesProbeError):
                inspect_hermes_identity(
                    changed if field == "launcher mtime" else observed.launcher,
                    changed if field.startswith("target") else observed.target,
                    launcher_path=observed.launcher_path,
                    target_path=observed.target_path,
                    version_banner=HERMES_VERSION_BANNER,
                    source_commit=HERMES_SOURCE_COMMIT,
                    source_describe=HERMES_SOURCE_DESCRIBE,
                )

        with self.assertRaises(HermesProbeError):
            inspect_hermes_identity(
                observed.launcher,
                observed.target,
                launcher_path=observed.launcher_path,
                target_path=observed.target_path,
                version_banner="Hermes Agent v0.20.5 (2026.8.18)",
                source_commit=HERMES_SOURCE_COMMIT,
                source_describe=HERMES_SOURCE_DESCRIBE,
            )
        with self.assertRaises(HermesProbeError):
            inspect_hermes_identity(
                observed.launcher,
                observed.target,
                launcher_path=observed.launcher_path,
                target_path=observed.target_path,
                version_banner=HERMES_VERSION_BANNER,
                source_commit="0" * 40,
                source_describe=HERMES_SOURCE_DESCRIBE,
            )

    def test_launcher_target_parser_does_not_accept_shell_substitution(self) -> None:
        self.assertEqual(
            parse_launcher_target(
                "#!/usr/bin/env bash\n"
                "unset PYTHONPATH\n"
                'exec "/private/hermes/venv/bin/hermes" "$@"\n'
            ),
            Path("/private/hermes/venv/bin/hermes"),
        )
        for source in (
            'exec "$(touch /tmp/side-effect)" "$@"',
            'exec "/private/hermes/venv/bin/hermes" "$@" extra',
            'exec "/private/hermes/venv/bin/other" "$@"',
        ):
            with self.subTest(source=source), self.assertRaises(HermesProbeError):
                parse_launcher_target(source)

    def test_installed_identity_reader_uses_real_files_without_provider_start(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            launcher = root / "hermes-launcher"
            target = root / "hermes"
            launcher.write_text(f'exec "{target}" "$@"\n', encoding="utf-8")
            target.write_text("#!/bin/sh\n", encoding="utf-8")
            launcher.chmod(0o700)
            target.chmod(0o700)

            with self.assertRaises(HermesProbeError):
                # The files are deliberately not the pinned installation. The
                # read-only inspection must fail before any provider turn.
                inspect_installed_hermes(
                    launcher,
                    target,
                    version_banner=HERMES_VERSION_BANNER,
                    source_commit=HERMES_SOURCE_COMMIT,
                    source_describe=HERMES_SOURCE_DESCRIBE,
                )

    def test_manifest_has_internal_profile_contract_and_rejects_caller_argv_env(
        self,
    ) -> None:
        manifests = {
            profile: manifest(profile)
            for profile in (
                "direct-local-oneshot",
                "acp",
                "external-docker",
            )
        }
        self.assertEqual(
            len({item.identity.sandbox_policy_id for item in manifests.values()}),
            3,
        )
        self.assertEqual(
            manifests["direct-local-oneshot"].identity.prompt_transport,
            "argv",
        )
        self.assertEqual(manifests["acp"].identity.prompt_transport, "stdin")
        self.assertEqual(
            manifests["direct-local-oneshot"].identity.environment_allowlist,
            HERMES_ENVIRONMENT_ALLOWLIST,
        )
        self.assertEqual(
            manifests["direct-local-oneshot"].identity.os_name,
            "darwin",
        )
        self.assertEqual(
            manifests["direct-local-oneshot"].identity.architecture,
            "arm64",
        )
        self.assertNotEqual(
            manifests["direct-local-oneshot"].identity.argv_sha256,
            manifests["acp"].identity.argv_sha256,
        )

        for kwargs in (
            {"argv": ("hermes", "--yolo", "--oneshot", "prompt")},
            {"environment_allowlist": ("HOME", "OPENCODE_API_KEY")},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(HermesProbeError):
                build_probe_manifest(
                    profile="direct-local-oneshot",
                    workspace=Path("/private/hermes-workspace"),
                    executable=executable(),
                    file_identity=hermes_identity().target,
                    hermes_identity=hermes_identity(),
                    **kwargs,
                )

        with self.assertRaises(HermesProbeError):
            build_probe_manifest(
                profile=cast(HermesProfile, "unknown"),
                workspace=Path("/private/hermes-workspace"),
                executable=executable(),
                file_identity=hermes_identity().target,
                hermes_identity=hermes_identity(),
            )

        with self.assertRaises(HermesProbeError):
            build_probe_manifest(
                profile="direct-local-oneshot",
                workspace=Path("/private/hermes-workspace"),
                executable=ExecutableIdentity(
                    "/private/another-hermes/hermes",
                    HERMES_VERSION,
                    HERMES_TARGET_IDENTITY.sha256,
                ),
                file_identity=hermes_identity().target,
                hermes_identity=hermes_identity(),
            )

    def test_known_direct_writes_keep_historical_provenance_and_are_rejected(
        self,
    ) -> None:
        receipt = build_rejected_local_receipt(manifest())

        self.assertEqual(receipt.judgment.status, "rejected")
        self.assertIn("boundary-violation", receipt.judgment.reason_codes)
        self.assertEqual(receipt.provenance, HISTORICAL_PROVENANCE)
        self.assertEqual(
            receipt.observed,
            (
                ToolEvidence("filesystem", "write", "workspace", "allowed"),
                ToolEvidence("filesystem", "write", "git", "allowed"),
                ToolEvidence("filesystem", "write", "outside", "allowed"),
            ),
        )
        self.assertEqual(receipt.generic_judgment.status, "rejected")

        serialized = json.loads(serialize_hermes_receipt(receipt))
        self.assertEqual(serialized["status"], "rejected")
        self.assertEqual(serialized["provenance"]["observed_at"], "2026-08-29")
        self.assertEqual(
            serialized["provenance"]["source_artifact_sha256"],
            HISTORICAL_PROVENANCE.source_artifact_sha256,
        )
        self.assertEqual(
            serialized["provenance"]["historical_verification_status"],
            "historical-unverified",
        )
        self.assertEqual(
            serialized["provenance"]["current_verification_status"],
            "static-identity-verified",
        )
        serialized_text = json.dumps(serialized)
        self.assertNotIn("DO_NOT_PERSIST", serialized_text)
        self.assertNotIn("raw provider output", serialized_text)
        self.assertNotIn("/Users/", serialized_text)

        with self.assertRaises(HermesProbeError):
            build_rejected_local_receipt(manifest(), observed=receipt.observed[:2])

    def test_acp_is_rejected_as_protocol_and_not_sandbox_evidence(self) -> None:
        receipt = build_unaccepted_acp_receipt(manifest("acp"))

        self.assertEqual(receipt.judgment.status, "rejected")
        self.assertEqual(receipt.judgment.reason_codes, ("not-a-filesystem-sandbox",))
        self.assertEqual(receipt.observed, ())
        self.assertIsNone(receipt.provenance)
        self.assertEqual(receipt.generic_judgment.status, "not-run")

    def test_external_preflight_never_produces_available_or_candidate(self) -> None:
        calls: list[tuple[str, ...]] = []

        def run(argv: tuple[str, ...]) -> ProcessResult:
            calls.append(argv)
            return ProcessResult(0, "29.7.2\n", "daemon output must not persist")

        result = preflight_external_sandbox(
            "docker",
            runner=run,
            lookup=lambda name: "/usr/bin/docker" if name == "docker" else None,
        )
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.classification, "sandbox-unverified")
        self.assertEqual(result.blocked_reason, "docker")
        self.assertEqual(
            calls, [("/usr/bin/docker", "info", "--format", "{{.ServerVersion}}")]
        )

        receipt = build_blocked_external_receipt(manifest("external-docker"), result)
        self.assertEqual(receipt.judgment.status, "blocked")
        self.assertIn("external-sandbox-unverified", receipt.judgment.reason_codes)
        self.assertTrue(all(not phase.attempted for phase in receipt.receipt.phases))
        self.assertNotIn("daemon output", serialize_hermes_receipt(receipt))

    def test_external_preflight_missing_timeout_and_execution_error_are_blocked(
        self,
    ) -> None:
        missing = preflight_external_sandbox(
            "openshell",
            runner=lambda _argv: ProcessResult(0, "unused", "unused"),
            lookup=lambda _name: None,
        )
        self.assertEqual(missing.classification, "runtime-unavailable")
        self.assertEqual(missing.blocked_reason, "platform")

        def timeout(_argv: tuple[str, ...]) -> ProcessResult:
            raise TimeoutError("secret timeout detail")

        timed_out = preflight_external_sandbox(
            "docker", runner=timeout, lookup=lambda _name: "/usr/bin/docker"
        )
        self.assertEqual(timed_out.classification, "runtime-timeout")

        def failed(_argv: tuple[str, ...]) -> ProcessResult:
            raise ExecutionError("raw provider output and token=must-not-persist")

        execution_failed = preflight_external_sandbox(
            "docker", runner=failed, lookup=lambda _name: "/usr/bin/docker"
        )
        self.assertEqual(execution_failed.classification, "runtime-execution-failed")
        self.assertNotIn("token=must-not-persist", repr(execution_failed))

    def test_external_preflight_rejects_relative_runtime_and_wrong_profile(
        self,
    ) -> None:
        with self.assertRaises(HermesProbeError):
            preflight_external_sandbox(
                "docker",
                runner=lambda _argv: ProcessResult(0, "ok", ""),
                lookup=lambda _name: "docker",
            )

        preflight = HermesExternalPreflight(
            runtime="docker",
            policy_id=EXTERNAL_DOCKER_POLICY_ID,
            blocked_reason="docker",
            classification="sandbox-unverified",
        )
        with self.assertRaises(HermesProbeError):
            build_blocked_external_receipt(manifest(), preflight)

        with self.assertRaises(HermesProbeError):
            HermesExternalPreflight(
                runtime="docker",
                policy_id=EXTERNAL_DOCKER_POLICY_ID,
                blocked_reason="docker",
                classification="sandbox-unverified",
                status=cast(Literal["blocked"], "available"),
            )

    def test_serializer_recomputes_status_and_rejects_forged_candidate(self) -> None:
        profile_manifest = manifest("external-docker")
        forged = HermesProbeReceipt(
            profile_manifest,
            candidate_receipt(profile_manifest),
            (),
            None,
            HermesExternalPreflight(
                runtime="docker",
                policy_id=EXTERNAL_DOCKER_POLICY_ID,
                blocked_reason="docker",
                classification="sandbox-unverified",
            ),
        )
        with self.assertRaises(HermesProbeError):
            serialize_hermes_receipt(forged)

        local = build_rejected_local_receipt(manifest())
        tampered_manifest = replace(
            local.manifest,
            identity=replace(local.manifest.identity, prompt_transport="stdin"),
        )
        with self.assertRaises(HermesProbeError):
            replace(local, manifest=tampered_manifest)

        with self.assertRaises(HermesProbeError):
            replace(
                build_blocked_external_receipt(
                    profile_manifest,
                    HermesExternalPreflight(
                        runtime="docker",
                        policy_id=EXTERNAL_DOCKER_POLICY_ID,
                        blocked_reason="docker",
                        classification="sandbox-unverified",
                    ),
                ),
                external_preflight=HermesExternalPreflight(
                    runtime="openshell",
                    policy_id="hermes-external-openshell-v1",
                    blocked_reason="platform",
                    classification="sandbox-unverified",
                ),
            )

    def test_synthetic_candidate_api_is_not_public(self) -> None:
        import agent_team.hermes_probe as module

        self.assertFalse(hasattr(module, "HermesRunArtifact"))
        self.assertFalse(hasattr(module, "run_external_probe"))


if __name__ == "__main__":
    unittest.main()
