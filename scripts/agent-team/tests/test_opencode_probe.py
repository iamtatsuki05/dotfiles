from __future__ import annotations

import hashlib
import inspect
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from agent_team.adapters import FileIdentity
from agent_team.opencode_probe import (
    AUTH_SOURCE_DIGEST,
    HISTORICAL_SOURCE_DIGEST,
    OPENCODE_SHA256,
    OPENCODE_VERSION,
    PROBE_REVISION,
    PROFILE_RAW,
    PROFILE_SNAPSHOT,
    PROFILES,
    _build_static_probe,
    _HistoricalSymlinkProvenance,
    _OpenCodeExecutablePin,
    _OpenCodeProbeError,
    _OpenCodeStaticProbe,
    _static_preflight,
    build_static_artifact,
    serialize_static_artifact,
)


def fake_pin(path: Path = Path("/private/opencode")) -> _OpenCodeExecutablePin:
    return _OpenCodeExecutablePin(
        path,
        OPENCODE_VERSION,
        FileIdentity(
            16777234, 379304228, 144123746, 1787973821923460802, OPENCODE_SHA256
        ),
    )


class OpenCodeStaticProbeTest(unittest.TestCase):
    def test_static_preflight_reads_same_fd_without_invoking_provider(self) -> None:
        data = b"fake opencode binary"
        digest = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "opencode"
            path.write_bytes(data)
            path.chmod(0o755)
            observed = path.stat()
            with (
                mock.patch("agent_team.opencode_probe.OPENCODE_CANONICAL_PATH", path),
                mock.patch("agent_team.opencode_probe.OPENCODE_SHA256", digest),
            ):
                pin = _static_preflight()

        self.assertEqual(pin.path, path.resolve())
        self.assertEqual(pin.file_identity.sha256, digest)
        self.assertEqual(pin.file_identity.device, observed.st_dev)
        self.assertEqual(pin.file_identity.inode, observed.st_ino)

    def test_static_preflight_rejects_final_symlink_and_wrong_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "real-opencode"
            target.write_bytes(b"fake")
            target.chmod(0o755)
            link = root / "opencode"
            link.symlink_to(target)
            with (
                mock.patch("agent_team.opencode_probe.OPENCODE_CANONICAL_PATH", link),
                mock.patch(
                    "agent_team.opencode_probe.OPENCODE_SHA256",
                    hashlib.sha256(b"fake").hexdigest(),
                ),
                self.assertRaises(_OpenCodeProbeError),
            ):
                _static_preflight()
            with (
                mock.patch("agent_team.opencode_probe.OPENCODE_CANONICAL_PATH", target),
                mock.patch("agent_team.opencode_probe.OPENCODE_SHA256", "0" * 64),
                self.assertRaises(_OpenCodeProbeError),
            ):
                _static_preflight()

    def test_public_builder_freshly_validates_and_returns_redacted_artifact(
        self,
    ) -> None:
        pin = fake_pin()
        with mock.patch(
            "agent_team.opencode_probe._static_preflight", return_value=pin
        ) as preflight:
            artifact = build_static_artifact()

        self.assertGreaterEqual(preflight.call_count, 2)
        self.assertEqual(
            tuple(item["profile"] for item in artifact["profiles"]), PROFILES
        )
        for item in artifact["profiles"]:
            self.assertEqual(item["status"], "blocked")
            self.assertEqual(item["blocked_reason"], "authentication")
            self.assertTrue(
                all(outcome == "not-run" for outcome in item["phase_outcomes"])
            )
        self.assertEqual(artifact["blocked"]["reason"], "authentication")
        observations = artifact["blocked"]["provenance"]
        self.assertEqual(observations[0]["verification_status"], "unverified")
        self.assertEqual(
            observations[1]["verification_status"], "historical-unverified"
        )
        self.assertEqual(observations[1]["executable_version"], OPENCODE_VERSION)
        self.assertEqual(observations[1]["executable_sha256"], OPENCODE_SHA256)
        self.assertNotIn(str(pin.path), repr(artifact))
        self.assertNotIn("Manifest", repr(artifact))
        self.assertNotIn("Receipt", repr(artifact))
        self.assertNotIn("Judgment", repr(artifact))

    def test_profiles_use_distinct_fixed_role_tokens_and_redacted_paths(self) -> None:
        pin = fake_pin()
        with mock.patch(
            "agent_team.opencode_probe._static_preflight", return_value=pin
        ):
            artifact = build_static_artifact()

        profiles = artifact["profiles"]
        self.assertNotEqual(
            profiles[0]["role_token_digest"], profiles[1]["role_token_digest"]
        )
        self.assertEqual(
            tuple(item["profile"] for item in profiles),
            (PROFILE_RAW, PROFILE_SNAPSHOT),
        )
        self.assertEqual(artifact["probe_revision"], PROBE_REVISION)
        self.assertEqual(artifact["pin"]["path"], "/probe/opencode")
        self.assertEqual(artifact["historical_symlink"]["verdict"], "rejected")

    def test_historical_symlink_provenance_is_separate_and_unverified(self) -> None:
        provenance = _HistoricalSymlinkProvenance(
            observed_at="2026-08-29",
            source_digest=HISTORICAL_SOURCE_DIGEST,
            verification_status="unverified",
            executable_version=OPENCODE_VERSION,
            executable_sha256=OPENCODE_SHA256,
            policy_id="opencode-raw-workspace-readonly-static-v3",
        )
        self.assertEqual(provenance.verification_status, "unverified")
        self.assertEqual(
            provenance.policy_id, "opencode-raw-workspace-readonly-static-v3"
        )

    def test_auth_provenance_is_historical_unverified_and_structured(self) -> None:
        pin = fake_pin()
        with mock.patch(
            "agent_team.opencode_probe._static_preflight", return_value=pin
        ):
            artifact = build_static_artifact()

        auth = artifact["blocked"]["provenance"][1]
        self.assertEqual(auth["source"], "historical")
        self.assertEqual(auth["code"], "auth-list-zero-credentials")
        self.assertEqual(auth["observed_at"], "2026-08-30")
        self.assertEqual(auth["source_digest"], AUTH_SOURCE_DIGEST)
        self.assertEqual(auth["verification_status"], "historical-unverified")
        self.assertEqual(auth["executable_version"], OPENCODE_VERSION)
        self.assertEqual(auth["executable_sha256"], OPENCODE_SHA256)

    def test_serializer_has_no_object_input_and_recomputes_safe_json(self) -> None:
        pin = fake_pin()
        with mock.patch(
            "agent_team.opencode_probe._static_preflight", return_value=pin
        ):
            text = serialize_static_artifact()

        self.assertEqual(tuple(inspect.signature(build_static_artifact).parameters), ())
        self.assertEqual(
            tuple(inspect.signature(serialize_static_artifact).parameters), ()
        )
        payload = json.loads(text)
        self.assertEqual(payload["pin"]["path"], "/probe/opencode")
        self.assertNotIn(str(pin.path), text)

    def test_forged_internal_probe_is_rejected_and_not_public(self) -> None:
        pin = fake_pin()
        with mock.patch(
            "agent_team.opencode_probe._static_preflight", return_value=pin
        ):
            valid = _build_static_probe()
        forged = replace(valid.profiles[0], status="candidate", blocked_reason="")
        with self.assertRaises(_OpenCodeProbeError):
            _OpenCodeStaticProbe(
                pin, (forged, valid.profiles[1]), valid.blocked, valid.historical
            )

        import agent_team.opencode_probe as module

        self.assertEqual(
            module.__all__, ("build_static_artifact", "serialize_static_artifact")
        )
        for name in (
            "OpenCodeExecutablePin",
            "OpenCodeStaticProbe",
            "OpenCodeProbeError",
            "Manifest",
            "Receipt",
            "Judgment",
            "build_static_probe",
            "serialize_static_probe",
            "validate_static_probe",
            "static_preflight",
            "run_live_probe",
            "parse_opencode_events",
            "attest_profile",
            "assemble_receipt",
            "execute_raw",
        ):
            self.assertFalse(hasattr(module, name), name)


if __name__ == "__main__":
    unittest.main()
