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
    BlockedObservation,
    HistoricalSymlinkProvenance,
    OpenCodeExecutablePin,
    OpenCodeProbeError,
    OpenCodeStaticProbe,
    build_static_probe,
    serialize_static_probe,
    static_preflight,
    validate_static_probe,
)


def fake_pin(path: Path = Path("/private/opencode")) -> OpenCodeExecutablePin:
    return OpenCodeExecutablePin(
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
                pin = static_preflight()

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
                self.assertRaises(OpenCodeProbeError),
            ):
                static_preflight()
            with (
                mock.patch("agent_team.opencode_probe.OPENCODE_CANONICAL_PATH", target),
                mock.patch("agent_team.opencode_probe.OPENCODE_SHA256", "0" * 64),
                self.assertRaises(OpenCodeProbeError),
            ):
                static_preflight()

    def test_static_probe_has_fixed_raw_and_snapshot_blocked_profiles(self) -> None:
        pin = fake_pin()
        with mock.patch("agent_team.opencode_probe.static_preflight", return_value=pin):
            probe = build_static_probe()

        self.assertEqual(tuple(item.profile for item in probe.profiles), PROFILES)
        self.assertEqual(PROFILES, (PROFILE_RAW, PROFILE_SNAPSHOT))
        for item in probe.profiles:
            self.assertEqual(item.status, "blocked")
            self.assertEqual(item.blocked_reason, "authentication")
            self.assertTrue(
                all(outcome == "not-run" for outcome in item.phase_outcomes)
            )
            self.assertEqual(item.permission_profile, "read-only")
        self.assertEqual(probe.blocked.reason, "authentication")
        self.assertEqual(
            probe.blocked.observations,
            (
                BlockedObservation(
                    "historical",
                    "raw-symlink-escape",
                    "2026-08-29",
                    HISTORICAL_SOURCE_DIGEST,
                    "unverified",
                ),
                BlockedObservation(
                    "current",
                    "auth-list-zero-credentials",
                    "2026-08-30",
                    AUTH_SOURCE_DIGEST,
                    "verified",
                ),
            ),
        )

    def test_profiles_use_distinct_fixed_role_tokens_and_no_generic_objects(
        self,
    ) -> None:
        pin = fake_pin()
        with mock.patch("agent_team.opencode_probe.static_preflight", return_value=pin):
            probe = build_static_probe()
            payload = json.loads(serialize_static_probe(probe))

        self.assertNotEqual(
            probe.profiles[0].role_token_digest,
            probe.profiles[1].role_token_digest,
        )
        self.assertEqual(
            tuple(item["profile"] for item in payload["profiles"]), PROFILES
        )
        self.assertNotIn("Manifest", repr(payload))
        self.assertNotIn("Receipt", repr(payload))
        self.assertNotIn(str(pin.path), repr(payload))
        self.assertEqual(payload["probe_revision"], PROBE_REVISION)
        self.assertEqual(payload["historical_symlink"]["verdict"], "rejected")

    def test_historical_symlink_provenance_is_separate_and_unverified(self) -> None:
        provenance = HistoricalSymlinkProvenance(
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

    def test_serializer_recomputes_and_rejects_forged_candidate_or_pin(self) -> None:
        pin = fake_pin()
        with mock.patch("agent_team.opencode_probe.static_preflight", return_value=pin):
            probe = build_static_probe()
            with self.assertRaises(OpenCodeProbeError):
                serialize_static_probe(
                    replace(
                        probe,
                        profiles=(
                            replace(probe.profiles[0], status="candidate"),
                            probe.profiles[1],
                        ),
                    )
                )
            with self.assertRaises(OpenCodeProbeError):
                validate_static_probe(
                    replace(
                        probe,
                        pin=replace(
                            pin, file_identity=FileIdentity(1, 2, 3, 4, "b" * 64)
                        ),
                    )
                )

    def test_static_builder_has_no_live_parser_or_caller_command_api(self) -> None:
        import agent_team.opencode_probe as module

        self.assertEqual(tuple(inspect.signature(build_static_probe).parameters), ())
        self.assertEqual(tuple(inspect.signature(static_preflight).parameters), ())
        for name in (
            "run_live_probe",
            "parse_opencode_events",
            "attest_profile",
            "assemble_receipt",
            "execute_raw",
        ):
            self.assertFalse(hasattr(module, name), name)

    def test_blocked_profile_record_cannot_be_constructed_as_candidate(self) -> None:
        pin = fake_pin()
        with mock.patch("agent_team.opencode_probe.static_preflight", return_value=pin):
            probe = build_static_probe()
        forged = replace(probe.profiles[0], status="candidate", blocked_reason=None)
        with self.assertRaises(OpenCodeProbeError):
            OpenCodeStaticProbe(
                pin, (forged, probe.profiles[1]), probe.blocked, probe.historical
            )


if __name__ == "__main__":
    unittest.main()
