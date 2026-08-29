from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import replace
from pathlib import Path
from typing import cast

from agent_team.adapters import ProcessResult
from agent_team.hermes_probe import (
    HERMES_LAUNCHER_IDENTITY,
    HERMES_RELEASE,
    HERMES_SOURCE_COMMIT,
    HERMES_SOURCE_DESCRIBE,
    HERMES_TARGET_IDENTITY,
    HERMES_VERSION,
    HERMES_VERSION_BANNER,
    ExternalSandbox,
    HermesExecutableIdentity,
    HermesProbeError,
    HermesProfile,
    HermesRunArtifact,
    HermesSandboxPreflight,
    build_probe_manifest,
    build_rejected_local_receipt,
    build_unaccepted_acp_receipt,
    inspect_hermes_identity,
    parse_launcher_target,
    preflight_external_sandbox,
    run_external_probe,
    serialize_hermes_receipt,
)
from agent_team.probe_receipts import (
    CleanupInventory,
    ExecutableIdentity,
    Manifest,
    PhaseReceipt,
    ToolEvidence,
    required_phases_for_profile,
    serialize_manifest,
)


def hermes_identity() -> HermesExecutableIdentity:
    return HermesExecutableIdentity(
        launcher=HERMES_LAUNCHER_IDENTITY,
        target=HERMES_TARGET_IDENTITY,
        version=HERMES_VERSION,
        release=HERMES_RELEASE,
        source_commit=HERMES_SOURCE_COMMIT,
        source_describe=HERMES_SOURCE_DESCRIBE,
    )


def executable() -> ExecutableIdentity:
    return ExecutableIdentity(
        "/private/hermes/bin/hermes",
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
        argv=(
            "/private/hermes/bin/hermes",
            "--safe-mode",
            "--toolsets",
            "file",
            "--oneshot",
            "DO_NOT_PERSIST_THIS_PROMPT",
        ),
        environment_allowlist=("HOME", "PATH"),
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


def candidate_phases() -> tuple[PhaseReceipt, ...]:
    return tuple(
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


class HermesProbeContractTest(unittest.TestCase):
    def test_exact_identity_requires_version_commit_and_every_file_identity_field(
        self,
    ) -> None:
        observed = inspect_hermes_identity(
            hermes_identity().launcher,
            hermes_identity().target,
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
                    version_banner=HERMES_VERSION_BANNER,
                    source_commit=HERMES_SOURCE_COMMIT,
                    source_describe=HERMES_SOURCE_DESCRIBE,
                )

        with self.assertRaises(HermesProbeError):
            inspect_hermes_identity(
                observed.launcher,
                observed.target,
                version_banner="Hermes Agent v0.20.5 (2026.8.18)",
                source_commit=HERMES_SOURCE_COMMIT,
                source_describe=HERMES_SOURCE_DESCRIBE,
            )
        with self.assertRaises(HermesProbeError):
            inspect_hermes_identity(
                observed.launcher,
                observed.target,
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

    def test_manifest_profiles_have_distinct_policies_and_only_store_argv_digest(
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
            manifests["direct-local-oneshot"].identity.permission_profile,
            "read-only",
        )
        self.assertEqual(
            manifests["direct-local-oneshot"].identity.prompt_transport,
            "argv",
        )
        self.assertEqual(manifests["acp"].identity.prompt_transport, "stdin")
        expected_digest = hashlib.sha256(
            json.dumps(
                [
                    "/private/hermes/bin/hermes",
                    "--safe-mode",
                    "--toolsets",
                    "file",
                    "--oneshot",
                    "DO_NOT_PERSIST_THIS_PROMPT",
                ],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        self.assertEqual(
            manifests["direct-local-oneshot"].identity.argv_sha256,
            expected_digest,
        )
        serialized = serialize_manifest(manifests["direct-local-oneshot"])
        self.assertNotIn("DO_NOT_PERSIST_THIS_PROMPT", serialized)

        with self.assertRaises(HermesProbeError):
            build_probe_manifest(
                profile="direct-local-oneshot",
                workspace=Path("/private/hermes-workspace"),
                executable=executable(),
                file_identity=replace(hermes_identity().target, inode=100),
                hermes_identity=hermes_identity(),
                argv=("/private/hermes/bin/hermes", "--oneshot", "prompt"),
                environment_allowlist=("HOME", "PATH"),
            )

    def test_known_direct_writes_are_preserved_and_judged_rejected(self) -> None:
        receipt = build_rejected_local_receipt(manifest())

        self.assertEqual(receipt.judgment.status, "rejected")
        self.assertIn("boundary-violation", receipt.judgment.reason_codes)
        self.assertEqual(
            receipt.observed,
            (
                ToolEvidence("filesystem", "write", "workspace", "allowed"),
                ToolEvidence("filesystem", "write", "git", "allowed"),
                ToolEvidence("filesystem", "write", "outside", "allowed"),
            ),
        )
        self.assertEqual(receipt.generic_judgment.status, "rejected")

        serialized = serialize_hermes_receipt(receipt)
        self.assertIn('"status":"rejected"', serialized)
        self.assertIn('"target":"outside"', serialized)
        self.assertNotIn("DO_NOT_PERSIST_THIS_PROMPT", serialized)
        self.assertNotIn("raw provider output", serialized)

        with self.assertRaises(HermesProbeError):
            build_rejected_local_receipt(
                manifest(),
                observed=receipt.observed[:2],
            )

    def test_acp_and_safe_mode_are_not_filesystem_sandbox_evidence(self) -> None:
        receipt = build_unaccepted_acp_receipt(manifest("acp"))

        self.assertEqual(receipt.judgment.status, "rejected")
        self.assertIn("not-a-filesystem-sandbox", receipt.judgment.reason_codes)
        self.assertEqual(receipt.observed, ())
        self.assertEqual(receipt.generic_judgment.status, "not-run")

    def test_external_preflight_is_read_only_and_does_not_fallback(self) -> None:
        calls: list[tuple[str, ...]] = []

        def run(argv: tuple[str, ...]) -> ProcessResult:
            calls.append(argv)
            return ProcessResult(1, "", "daemon unavailable; raw provider output")

        result = preflight_external_sandbox(
            "docker",
            runner=run,
            lookup=lambda name: "/usr/bin/docker" if name == "docker" else None,
        )
        self.assertFalse(result.available)
        self.assertEqual(result.blocked_reason, "docker")
        self.assertEqual(
            calls, [("/usr/bin/docker", "info", "--format", "{{.ServerVersion}}")]
        )
        self.assertNotIn("daemon unavailable", repr(result))

        empty = preflight_external_sandbox(
            "docker",
            runner=lambda _argv: ProcessResult(0, "", ""),
            lookup=lambda _name: "/usr/bin/docker",
        )
        self.assertFalse(empty.available)

        with self.assertRaises(HermesProbeError):
            preflight_external_sandbox(
                "docker",
                runner=run,
                lookup=lambda _name: "docker",
            )

        unavailable = preflight_external_sandbox(
            "openshell",
            runner=run,
            lookup=lambda _name: None,
        )
        self.assertFalse(unavailable.available)
        self.assertEqual(unavailable.blocked_reason, "platform")
        self.assertEqual(len(calls), 1)

        with self.assertRaises(HermesProbeError):
            preflight_external_sandbox(
                cast(ExternalSandbox, "unknown"),
                runner=run,
                lookup=lambda _: None,
            )

    def test_blocked_external_preflight_prevents_provider_turn(self) -> None:
        preflight = HermesSandboxPreflight(
            runtime="docker",
            available=False,
            policy_id="hermes-external-docker-v1",
            blocked_reason="docker",
        )
        called = False

        def provider(_manifest: object) -> HermesRunArtifact:
            nonlocal called
            called = True
            raise AssertionError("provider must not start for a blocked preflight")

        receipt = run_external_probe(manifest("external-docker"), preflight, provider)

        self.assertFalse(called)
        self.assertEqual(receipt.judgment.status, "blocked")
        self.assertIn("blocked-docker", receipt.judgment.reason_codes)
        self.assertTrue(all(not phase.attempted for phase in receipt.receipt.phases))

    def test_available_external_preflight_is_the_only_path_to_provider_runner(
        self,
    ) -> None:
        preflight = HermesSandboxPreflight(
            runtime="docker",
            available=True,
            policy_id="hermes-external-docker-v1",
            blocked_reason=None,
        )
        calls: list[str] = []

        def provider(_manifest: object) -> HermesRunArtifact:
            calls.append("provider")
            return HermesRunArtifact(candidate_phases(), ())

        receipt = run_external_probe(manifest("external-docker"), preflight, provider)

        self.assertEqual(calls, ["provider"])
        self.assertEqual(receipt.judgment.status, "candidate")

        def unsafe_provider(_manifest: object) -> HermesRunArtifact:
            return HermesRunArtifact(
                candidate_phases(),
                (ToolEvidence("filesystem", "write", "outside", "allowed"),),
            )

        unsafe = run_external_probe(
            manifest("external-docker"), preflight, unsafe_provider
        )
        self.assertEqual(unsafe.judgment.status, "rejected")
        self.assertIn("boundary-violation", unsafe.judgment.reason_codes)

        with self.assertRaises(HermesProbeError):
            run_external_probe(manifest("direct-local-oneshot"), preflight, provider)

        with self.assertRaises(HermesProbeError):
            run_external_probe(
                manifest("external-docker"),
                replace(preflight, policy_id="hermes-external-openshell-v1"),
                provider,
            )


if __name__ == "__main__":
    unittest.main()
