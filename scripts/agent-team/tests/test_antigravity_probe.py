from __future__ import annotations

import hashlib
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_team import antigravity_probe as probe
from agent_team.adapters import FileIdentity


def fake_attestation(
    digest: str = "7b1317779085913d338bde0e9b39b72323d9083a879525f944fd469c8ecca906",
) -> probe._StaticAttestation:
    return probe._StaticAttestation(
        file_identity=FileIdentity(1, 2, 3, 4, digest),
        signature=probe._EXPECTED_SIGNATURE,
        device_identity=probe._EXPECTED_DEVICE_IDENTITY,
        signature_verification="historical-unverified",
        device_verification="historical-unverified",
        live_gate="ineligible",
    )


class AntigravityPublicSurfaceTest(unittest.TestCase):
    def test_public_surface_is_only_path_free_artifact_serializers(self) -> None:
        expected = (
            "serialize_raw_historical_artifact",
            "serialize_raw_role_manifest_artifact",
            "serialize_snapshot_blocked_artifact",
            "serialize_snapshot_not_run_artifact",
            "serialize_snapshot_role_manifest_artifact",
        )
        self.assertEqual(probe.__all__, expected)
        self.assertNotIn("StaticProvenance", probe.__all__)
        for name in probe.__all__:
            parameters = inspect.signature(getattr(probe, name)).parameters
            self.assertTrue(
                {
                    "path",
                    "file_identity",
                    "signature",
                    "device_identity",
                    "provenance",
                }.isdisjoint(parameters),
                name,
            )

    def test_public_artifacts_reinspect_current_pinned_path(self) -> None:
        calls: list[str] = []

        def current() -> probe._StaticAttestation:
            calls.append("inspect")
            return fake_attestation()

        with mock.patch.object(probe, "_current_attestation", side_effect=current):
            probe.serialize_raw_historical_artifact(
                observed_at="2026-08-29T12:00:00Z", source_sha256="a" * 64
            )
            probe.serialize_raw_role_manifest_artifact("planner")
            probe.serialize_snapshot_role_manifest_artifact("reviewer")
            probe.serialize_snapshot_blocked_artifact()
            probe.serialize_snapshot_not_run_artifact()
        self.assertEqual(calls, ["inspect"] * 5)

    def test_public_artifacts_reject_caller_identity_arguments(self) -> None:
        with self.assertRaises(TypeError):
            probe.serialize_snapshot_not_run_artifact(file_identity=object())


class AntigravityStaticInspectionTest(unittest.TestCase):
    def test_same_fd_hash_reads_regular_executable_without_following_links(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "agy-fixture"
            payload = b"static executable fixture\n"
            path.write_bytes(payload)
            path.chmod(0o700)
            result = probe._inspect_file_same_fd(path)
        self.assertEqual(result.sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual(result.size, len(payload))

    def test_same_fd_hash_rejects_non_executable_and_symlink_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            non_executable = root / "not-executable"
            non_executable.write_bytes(b"fixture")
            non_executable.chmod(0o600)
            target = root / "target"
            target.write_bytes(b"fixture")
            target.chmod(0o700)
            link = root / "agy"
            link.symlink_to(target)
            for path in (non_executable, link):
                with (
                    self.subTest(path=path),
                    self.assertRaises(probe.AntigravityProbeError),
                ):
                    probe._inspect_file_same_fd(path)

    def test_current_attestation_reinspects_only_pinned_path_and_hash(self) -> None:
        identity = FileIdentity(1, 2, 3, 4, probe._ANTIGRAVITY_SHA256)
        with mock.patch.object(
            probe, "_inspect_file_same_fd", return_value=identity
        ) as inspect_file:
            attestation = probe._current_attestation()
        inspect_file.assert_called_once_with(probe._ANTIGRAVITY_EXECUTABLE)
        self.assertEqual(attestation.file_identity, identity)
        self.assertEqual(attestation.signature_verification, "historical-unverified")
        self.assertEqual(attestation.device_verification, "historical-unverified")
        self.assertEqual(attestation.live_gate, "ineligible")

    def test_current_attestation_rejects_cask_payload_hash_without_fallback(
        self,
    ) -> None:
        cask_hash = "c9f28a5e013c067536dbed531b2d2c43aa3224b27fd0a44a2e426a9095504b91"
        with (
            mock.patch.object(
                probe,
                "_inspect_file_same_fd",
                return_value=FileIdentity(1, 2, 3, 4, cask_hash),
            ),
            self.assertRaises(probe.AntigravityProbeError),
        ):
            probe._current_attestation()


class AntigravitySignatureTest(unittest.TestCase):
    def test_leaf_authority_is_first_authority_not_last(self) -> None:
        result = probe._parse_leaf_signature(
            "Authority=Developer ID Application: Google LLC (EQHXZ8M8AV)\n"
            "Authority=Developer ID Certification Authority\n"
            "Authority=Apple Root CA\n"
            "TeamIdentifier=EQHXZ8M8AV\n"
        )
        self.assertEqual(result, probe._EXPECTED_SIGNATURE)

    def test_wrong_leaf_is_rejected_even_when_a_later_authority_matches(self) -> None:
        with self.assertRaises(probe.AntigravityProbeError):
            probe._parse_leaf_signature(
                "Authority=Developer ID Application: Someone Else (WRONGTEAM1)\n"
                "Authority=Developer ID Application: Google LLC (EQHXZ8M8AV)\n"
                "TeamIdentifier=EQHXZ8M8AV\n"
            )

    def test_signature_and_device_are_marked_historical_unverified(self) -> None:
        with mock.patch.object(
            probe, "_current_attestation", return_value=fake_attestation()
        ):
            payload = json.loads(
                probe.serialize_raw_historical_artifact(
                    observed_at="2026-08-29T12:00:00Z", source_sha256="a" * 64
                )
            )
        provenance = payload["provenance"]
        self.assertEqual(
            provenance["signature"]["verification"], "historical-unverified"
        )
        self.assertEqual(provenance["device"]["verification"], "historical-unverified")
        self.assertEqual(provenance["live_gate"], "ineligible")


class AntigravityArtifactTest(unittest.TestCase):
    def test_raw_historical_artifact_is_rejected_and_redacted(self) -> None:
        with mock.patch.object(
            probe, "_current_attestation", return_value=fake_attestation()
        ):
            payload = json.loads(
                probe.serialize_raw_historical_artifact(
                    observed_at="2026-08-29T12:00:00Z", source_sha256="a" * 64
                )
            )
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["profile"], "raw-workspace")
        self.assertTrue(payload["historical_unverified"])
        self.assertEqual(payload["evidence"]["result"], "allowed")
        self.assertNotIn("path", json.dumps(payload))
        self.assertNotIn("/opt/homebrew", json.dumps(payload))
        self.assertNotIn("PRIVATE_OUTPUT", json.dumps(payload))

    def test_raw_historical_artifact_validates_observed_at_and_digest(self) -> None:
        for observed_at, digest in (
            ("2026-08-29", "a" * 64),
            ("2026-08-29T12:00:00Z", "bad"),
        ):
            with (
                self.subTest(observed_at=observed_at, digest=digest),
                mock.patch.object(
                    probe, "_current_attestation", return_value=fake_attestation()
                ),
                self.assertRaises(probe.AntigravityProbeError),
            ):
                probe.serialize_raw_historical_artifact(
                    observed_at=observed_at, source_sha256=digest
                )

    def test_raw_and_snapshot_role_artifacts_use_fixed_profiles_and_roles(self) -> None:
        with mock.patch.object(
            probe, "_current_attestation", return_value=fake_attestation()
        ):
            raw = json.loads(probe.serialize_raw_role_manifest_artifact("planner"))
            snapshot = json.loads(
                probe.serialize_snapshot_role_manifest_artifact("reviewer")
            )
        self.assertEqual(raw["profile"], "raw-workspace")
        self.assertEqual(snapshot["profile"], "snapshot")
        self.assertEqual(raw["role"], "planner")
        self.assertEqual(snapshot["role"], "reviewer")
        self.assertEqual(raw["route"], "--print")
        self.assertEqual(raw["permission_profile"], "read-only")
        self.assertNotEqual(raw["sandbox_policy_id"], snapshot["sandbox_policy_id"])
        self.assertNotIn("path", json.dumps(raw))

    def test_role_artifact_rejects_unfixed_role_tokens(self) -> None:
        for role in ("worker", "admin", ""):
            with (
                self.subTest(role=role),
                mock.patch.object(
                    probe, "_current_attestation", return_value=fake_attestation()
                ),
                self.assertRaises(probe.AntigravityProbeError),
            ):
                probe.serialize_raw_role_manifest_artifact(role)  # type: ignore[arg-type]

    def test_snapshot_artifacts_are_fixed_blocked_or_not_run_pairs(self) -> None:
        with mock.patch.object(
            probe, "_current_attestation", return_value=fake_attestation()
        ):
            blocked = json.loads(probe.serialize_snapshot_blocked_artifact())
            not_run = json.loads(probe.serialize_snapshot_not_run_artifact())
        self.assertEqual(
            (blocked["status"], blocked["reason"]),
            ("blocked", "outer-sandbox-unverified"),
        )
        self.assertEqual(
            (not_run["status"], not_run["reason"]),
            ("not-run", "provider-not-run"),
        )
        self.assertNotIn("candidate", json.dumps(blocked))
        self.assertNotIn("candidate", json.dumps(not_run))

    def test_private_snapshot_factory_rejects_other_status_reason_pairs(self) -> None:
        with self.assertRaises(probe.AntigravityProbeError):
            probe._build_snapshot_artifact(
                "blocked", "provider-not-run", fake_attestation()
            )
        with self.assertRaises(probe.AntigravityProbeError):
            probe._build_snapshot_artifact(
                "not-run", "outer-sandbox-unverified", fake_attestation()
            )


if __name__ == "__main__":
    unittest.main()
