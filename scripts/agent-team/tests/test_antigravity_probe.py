from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from agent_team.adapters import FileIdentity
from agent_team.antigravity_probe import (
    ANTIGRAVITY_EXECUTABLE,
    ANTIGRAVITY_SHA256,
    ANTIGRAVITY_SIGNING_IDENTITY,
    ANTIGRAVITY_TEAM_ID,
    ANTIGRAVITY_VERSION,
    EXPECTED_DEVICE_IDENTITY,
    AntigravityProbeError,
    CodeSignature,
    DeviceIdentity,
    HistoricalOutsideReadEvidence,
    HistoricalOutsideReadReceipt,
    StaticProvenance,
    build_raw_historical_receipt,
    build_raw_role_manifest,
    build_snapshot_blocked_receipt,
    build_snapshot_not_run_receipt,
    build_snapshot_receipt,
    build_snapshot_role_manifest,
    inspect_file_same_fd,
    inspect_static_binary,
    parse_leaf_signature,
    serialize_raw_historical_receipt,
    serialize_role_manifest,
    serialize_snapshot_receipt,
    validate_static_provenance,
)


def file_identity(digest: str = ANTIGRAVITY_SHA256) -> FileIdentity:
    return FileIdentity(1, 2, 3, 4, digest)


def provenance(**changes: object) -> StaticProvenance:
    values: dict[str, object] = {
        "executable_path": str(ANTIGRAVITY_EXECUTABLE),
        "version": ANTIGRAVITY_VERSION,
        "sha256": ANTIGRAVITY_SHA256,
        "signature": CodeSignature(ANTIGRAVITY_SIGNING_IDENTITY, ANTIGRAVITY_TEAM_ID),
        "device_identity": EXPECTED_DEVICE_IDENTITY,
        "file_identity": file_identity(),
    }
    values.update(changes)
    return StaticProvenance(**values)


class AntigravityStaticPinTest(unittest.TestCase):
    def test_exact_static_pin_is_accepted(self) -> None:
        result = validate_static_provenance(provenance())
        self.assertEqual(result.sha256, ANTIGRAVITY_SHA256)
        self.assertEqual(result.signature.team_id, ANTIGRAVITY_TEAM_ID)

    def test_every_pin_dimension_fails_closed_without_cask_fallback(self) -> None:
        mutations = (
            {"executable_path": "/opt/homebrew/bin/antigravity"},
            {"version": "1.0.8"},
            {
                "sha256": "c9f28a5e013c067536dbed531b2d2c43aa3224b27fd0a44a2e426a9095504b91"
            },
            {"signature": CodeSignature(ANTIGRAVITY_SIGNING_IDENTITY, "WRONGTEAM1")},
            {
                "device_identity": DeviceIdentity(
                    "Darwin", "x86_64", "25.5.0", "26.5.2", "MacBookPro18,4"
                )
            },
        )
        for mutation in mutations:
            with (
                self.subTest(mutation=mutation),
                self.assertRaises(AntigravityProbeError),
            ):
                provenance(**mutation)

    def test_same_fd_hash_reads_a_regular_executable_without_following_links(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "agy-fixture"
            payload = b"static executable fixture\n"
            path.write_bytes(payload)
            path.chmod(0o700)
            result = inspect_file_same_fd(path)
        self.assertEqual(result.sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual(result.size, len(payload))

    def test_same_fd_hash_rejects_non_executable_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "not-executable"
            path.write_bytes(b"fixture")
            path.chmod(0o600)
            with self.assertRaises(AntigravityProbeError):
                inspect_file_same_fd(path)

    def test_same_fd_hash_rejects_symlinked_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.write_bytes(b"fixture")
            target.chmod(0o700)
            link = root / "agy"
            link.symlink_to(target)
            with self.assertRaises(AntigravityProbeError):
                inspect_file_same_fd(link)

    def test_static_inspection_never_runs_provider_or_version_commands(self) -> None:
        with mock.patch(
            "agent_team.antigravity_probe.inspect_file_same_fd",
            return_value=file_identity(),
        ) as inspect:
            result = inspect_static_binary(
                path=ANTIGRAVITY_EXECUTABLE,
                version=ANTIGRAVITY_VERSION,
                signature_metadata=(
                    "Authority=Developer ID Application: Google LLC (EQHXZ8M8AV)\n"
                    "Authority=Apple Root CA\n"
                    "TeamIdentifier=EQHXZ8M8AV\n"
                ),
                device_identity=EXPECTED_DEVICE_IDENTITY,
            )
        inspect.assert_called_once_with(ANTIGRAVITY_EXECUTABLE)
        self.assertEqual(result.signature.identifier, ANTIGRAVITY_SIGNING_IDENTITY)

    def test_static_inspection_rejects_non_pinned_path_before_opening(self) -> None:
        with (
            mock.patch("agent_team.antigravity_probe.inspect_file_same_fd") as inspect,
            self.assertRaises(AntigravityProbeError),
        ):
            inspect_static_binary(
                path=Path("/opt/homebrew/bin/antigravity"),
                version=ANTIGRAVITY_VERSION,
                signature_metadata="Authority=wrong\nTeamIdentifier=wrong\n",
                device_identity=EXPECTED_DEVICE_IDENTITY,
            )
        inspect.assert_not_called()


class AntigravitySignatureTest(unittest.TestCase):
    def test_leaf_authority_is_first_authority_not_last(self) -> None:
        result = parse_leaf_signature(
            "Authority=Developer ID Application: Google LLC (EQHXZ8M8AV)\n"
            "Authority=Developer ID Certification Authority\n"
            "Authority=Apple Root CA\n"
            "TeamIdentifier=EQHXZ8M8AV\n"
        )
        self.assertEqual(
            result,
            CodeSignature(ANTIGRAVITY_SIGNING_IDENTITY, ANTIGRAVITY_TEAM_ID),
        )

    def test_wrong_leaf_is_rejected_even_when_a_later_authority_matches(self) -> None:
        with self.assertRaises(AntigravityProbeError):
            parse_leaf_signature(
                "Authority=Developer ID Application: Someone Else (WRONGTEAM1)\n"
                "Authority=Developer ID Application: Google LLC (EQHXZ8M8AV)\n"
                "TeamIdentifier=EQHXZ8M8AV\n"
            )

    def test_signature_parser_returns_no_raw_metadata(self) -> None:
        private_marker = "PRIVATE_SIGNATURE_OUTPUT"
        result = parse_leaf_signature(
            "Authority=Developer ID Application: Google LLC (EQHXZ8M8AV)\n"
            "TeamIdentifier=EQHXZ8M8AV\n"
        )
        self.assertNotIn(private_marker, repr(result))


class AntigravityManifestTest(unittest.TestCase):
    def test_raw_and_snapshot_manifests_have_separate_fixed_profiles_and_roles(
        self,
    ) -> None:
        raw = build_raw_role_manifest("planner", provenance())
        snapshot = build_snapshot_role_manifest("reviewer", provenance())
        raw_payload = json.loads(serialize_role_manifest(raw))
        snapshot_payload = json.loads(serialize_role_manifest(snapshot))
        self.assertEqual(raw_payload["profile"], "raw-workspace")
        self.assertEqual(snapshot_payload["profile"], "snapshot")
        self.assertEqual(raw_payload["role"], "planner")
        self.assertEqual(snapshot_payload["role"], "reviewer")
        self.assertEqual(raw_payload["route"], "--print")
        self.assertEqual(raw_payload["permission_profile"], "read-only")
        self.assertNotEqual(
            raw_payload["sandbox_policy_id"], snapshot_payload["sandbox_policy_id"]
        )

    def test_manifest_rejects_unfixed_role_tokens(self) -> None:
        for role in ("worker", "admin", ""):
            with self.subTest(role=role), self.assertRaises(AntigravityProbeError):
                build_raw_role_manifest(role, provenance())  # type: ignore[arg-type]

    def test_manifest_serializer_does_not_store_prompt_model_environment_or_workspace(
        self,
    ) -> None:
        serialized = serialize_role_manifest(
            build_raw_role_manifest("planner", provenance())
        )
        for secret in (
            "PRIVATE_PROMPT",
            "test-model",
            "GOOGLE_API_KEY",
            "/Users/",
        ):
            self.assertNotIn(secret, serialized)

    def test_manifest_serializer_revalidates_provenance(self) -> None:
        manifest = build_raw_role_manifest("planner", provenance())
        with self.assertRaises(AntigravityProbeError):
            serialize_role_manifest(
                replace(
                    manifest,
                    provenance=provenance(
                        signature=CodeSignature(
                            "Developer ID Application: Someone Else (WRONGTEAM1)",
                            "WRONGTEAM1",
                        )
                    ),
                )
            )


class AntigravityReceiptTest(unittest.TestCase):
    def test_historical_outside_read_is_rejected_and_provenance_is_typed(self) -> None:
        receipt = build_raw_historical_receipt(
            observed_at="2026-08-29T12:00:00Z",
            source_sha256="a" * 64,
            provenance=provenance(),
        )
        self.assertIsInstance(receipt, HistoricalOutsideReadReceipt)
        self.assertEqual(receipt.status, "rejected")
        self.assertEqual(receipt.profile, "raw-workspace")
        self.assertTrue(receipt.historical_unverified)
        self.assertEqual(receipt.evidence, HistoricalOutsideReadEvidence())
        serialized = serialize_raw_historical_receipt(receipt)
        self.assertIn('"source_sha256":"' + "a" * 64 + '"', serialized)
        self.assertIn('"observed_at":"2026-08-29T12:00:00Z"', serialized)
        self.assertNotIn("PRIVATE_OUTPUT", serialized)
        self.assertNotIn("original-workspace", serialized)

    def test_historical_receipt_requires_timezone_and_digest(self) -> None:
        for observed_at, digest in (
            ("2026-08-29", "a" * 64),
            ("2026-08-29T12:00:00Z", "bad"),
        ):
            with (
                self.subTest(observed_at=observed_at, digest=digest),
                self.assertRaises(AntigravityProbeError),
            ):
                build_raw_historical_receipt(
                    observed_at=observed_at,
                    source_sha256=digest,
                    provenance=provenance(),
                )

    def test_snapshot_receipts_are_blocked_or_not_run_never_candidates(self) -> None:
        blocked = build_snapshot_blocked_receipt(provenance())
        not_run = build_snapshot_not_run_receipt(provenance())
        for receipt in (blocked, not_run):
            payload = json.loads(serialize_snapshot_receipt(receipt))
            self.assertIn(payload["status"], {"blocked", "not-run"})
            self.assertNotIn("candidate", payload.values())
            self.assertEqual(payload["profile"], "snapshot")

    def test_snapshot_receipt_rejects_candidate_status(self) -> None:
        with self.assertRaises(AntigravityProbeError):
            build_snapshot_receipt(  # type: ignore[arg-type]
                provenance(), status="candidate"
            )

    def test_receipt_serializer_revalidates_current_static_identity(self) -> None:
        receipt = build_snapshot_not_run_receipt(provenance())
        with self.assertRaises(AntigravityProbeError):
            serialize_snapshot_receipt(
                replace(
                    receipt,
                    provenance=provenance(
                        device_identity=DeviceIdentity(
                            "Darwin", "arm64", "25.5.0", "26.5.2", "MacBookPro18,3"
                        )
                    ),
                )
            )

    def test_historical_serializer_rejects_tampered_status(self) -> None:
        receipt = build_raw_historical_receipt(
            observed_at="2026-08-29T12:00:00Z",
            source_sha256="b" * 64,
            provenance=provenance(),
        )
        object.__setattr__(receipt, "status", "candidate")
        with self.assertRaises(AntigravityProbeError):
            serialize_raw_historical_receipt(receipt)


if __name__ == "__main__":
    unittest.main()
