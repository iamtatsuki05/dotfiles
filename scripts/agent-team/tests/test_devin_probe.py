from __future__ import annotations

import hashlib
import inspect
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_team.adapters import FileIdentity
from agent_team.devin_probe import (
    DEVIN_BUILD,
    DEVIN_CDHASH,
    DEVIN_IDENTIFIER,
    DEVIN_SHA256,
    DEVIN_TEAM_IDENTIFIER,
    DEVIN_VERSION,
    PROFILE_ACP,
    PROFILE_DIRECT,
    PROFILES,
    BlockedObservation,
    BlockedState,
    CodesignMetadata,
    DevinExecutablePin,
    DevinProbeError,
    DevinProfileRecord,
    build_static_probe,
    serialize_static_probe,
    static_preflight,
    validate_static_probe,
)
from agent_team.probe_receipts import (
    _PHASE_EVIDENCE,
    CleanupInventory,
    Judgment,
    PhaseReceipt,
    Receipt,
    ToolEvidence,
)


def fake_pin(path: Path = Path("/private/devin/bin/devin")) -> DevinExecutablePin:
    return DevinExecutablePin(
        path,
        DEVIN_VERSION,
        DEVIN_BUILD,
        FileIdentity(
            16777234, 379302504, 157560304, 1_750_000_000_000_000_000, DEVIN_SHA256
        ),
        CodesignMetadata(DEVIN_IDENTIFIER, DEVIN_CDHASH, DEVIN_TEAM_IDENTIFIER),
    )


def candidate_receipt(record: DevinProfileRecord) -> Receipt:
    manifest = record.manifest
    return Receipt(
        manifest.identity,
        None,
        tuple(
            PhaseReceipt(
                spec.phase_id,
                spec.expected_result,
                True,
                True,
                "passed",
                0,
                False,
                tuple(ToolEvidence(*item) for item in _PHASE_EVIDENCE[spec.phase_id]),
                CleanupInventory(),
            )
            for spec in manifest.required_phases
        ),
    )


class DevinStaticProbeTest(unittest.TestCase):
    def test_preflight_reads_fixed_binary_and_codesign_only(self) -> None:
        data = b"fake Devin binary"
        digest = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "devin"
            path.write_bytes(data)
            path.chmod(0o755)
            file_stat = path.stat()
            codesign = SimpleNamespace(
                returncode=0,
                stdout="",
                stderr=(
                    "Identifier=devin\n"
                    "CDHash=30bb4bb91719ca3457ff3af32ad7b0614d3ff379\n"
                    "TeamIdentifier=83Z2LHX6XW\n"
                ),
            )
            with (
                mock.patch("agent_team.devin_probe.DEVIN_CANONICAL_PATH", path),
                mock.patch("agent_team.devin_probe.DEVIN_SHA256", digest),
                mock.patch(
                    "agent_team.devin_probe.subprocess.run", return_value=codesign
                ) as run,
            ):
                pin = static_preflight()

        self.assertEqual(pin.file_identity.sha256, digest)
        self.assertEqual(pin.file_identity.device, file_stat.st_dev)
        self.assertEqual(pin.file_identity.inode, file_stat.st_ino)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][0:2], ("codesign", "--display"))
        self.assertNotIn("--version", run.call_args.args[0])

    def test_preflight_rejects_fake_signature_and_non_pinned_release(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "devin"
            path.write_bytes(b"fake")
            path.chmod(0o755)
            digest = hashlib.sha256(b"fake").hexdigest()
            bad_codesign = SimpleNamespace(
                returncode=0,
                stdout="",
                stderr="Identifier=other\nCDHash=bad\nTeamIdentifier=other\n",
            )
            with (
                mock.patch("agent_team.devin_probe.DEVIN_CANONICAL_PATH", path),
                mock.patch("agent_team.devin_probe.DEVIN_SHA256", digest),
                mock.patch(
                    "agent_team.devin_probe.subprocess.run", return_value=bad_codesign
                ),
                self.assertRaises(DevinProbeError),
            ):
                static_preflight()
            with self.assertRaises(DevinProbeError):
                DevinExecutablePin(
                    path,
                    "3000.6.6",
                    DEVIN_BUILD,
                    FileIdentity(1, 2, 4, 5, DEVIN_SHA256),
                    CodesignMetadata(
                        DEVIN_IDENTIFIER, DEVIN_CDHASH, DEVIN_TEAM_IDENTIFIER
                    ),
                )

    def test_fixed_pin_rejects_unowned_or_non_executable_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "devin"
            path.write_bytes(b"fake")
            with (
                mock.patch("agent_team.devin_probe.DEVIN_CANONICAL_PATH", path),
                mock.patch(
                    "agent_team.devin_probe.DEVIN_SHA256",
                    hashlib.sha256(b"fake").hexdigest(),
                ),
                self.assertRaises(DevinProbeError),
            ):
                static_preflight()

    def test_static_probe_has_two_fixed_profiles_and_all_phases_not_run(self) -> None:
        pin = fake_pin()
        with mock.patch("agent_team.devin_probe.static_preflight", return_value=pin):
            probe = build_static_probe()
        self.assertEqual(tuple(record.profile for record in probe.profiles), PROFILES)
        self.assertEqual(PROFILES, (PROFILE_DIRECT, PROFILE_ACP))
        for record in probe.profiles:
            self.assertEqual(record.receipt.blocked_reason, "account")
            self.assertEqual(record.judgment.status, "blocked")
            self.assertTrue(all(not phase.attempted for phase in record.receipt.phases))
            self.assertEqual(record.manifest.identity.permission_profile, "read-only")
        self.assertEqual(
            probe.blocked.observations,
            (
                BlockedObservation("historical", "free-tier-tool-turn-not-established"),
                BlockedObservation("current", "tool-turn-not-run-without-tier-change"),
            ),
        )

    def test_direct_and_acp_manifests_use_distinct_fixed_role_token_digests(
        self,
    ) -> None:
        pin = fake_pin()
        with mock.patch("agent_team.devin_probe.static_preflight", return_value=pin):
            probe = build_static_probe()
            direct, acp = probe.profiles
            serialized = serialize_static_probe(probe)
        self.assertNotEqual(
            direct.manifest.identity.argv_sha256,
            acp.manifest.identity.argv_sha256,
        )
        self.assertEqual(direct.manifest.identity.prompt_transport, "stdin")
        self.assertEqual(acp.manifest.identity.prompt_transport, "stdin")
        self.assertNotIn("/private/devin", serialized)

    def test_serializer_recomputes_pin_profiles_receipts_judgments_and_blocker(
        self,
    ) -> None:
        pin = fake_pin()
        with mock.patch("agent_team.devin_probe.static_preflight", return_value=pin):
            probe = build_static_probe()
            payload = json.loads(serialize_static_probe(probe))
        self.assertEqual(payload["artifact"], "devin-static-probe")
        self.assertEqual(payload["pin"]["path"], "/probe/devin")
        self.assertEqual(payload["pin"]["sha256"], DEVIN_SHA256)
        self.assertEqual(len(payload["profiles"]), 2)
        self.assertEqual(payload["blocked"]["reason"], "account")
        self.assertEqual(
            tuple(item["source"] for item in payload["blocked"]["provenance"]),
            ("historical", "current"),
        )
        for profile in payload["profiles"]:
            self.assertEqual(profile["receipt"]["blocked_reason"], "account")
            self.assertEqual(
                profile["manifest"]["environment_allowlist"],
                ["HOME", "LANG", "PATH", "SHELL", "TERM", "TMPDIR"],
            )
            self.assertNotIn(
                "DEVIN_TOKEN", profile["manifest"]["environment_allowlist"]
            )
            self.assertTrue(
                all(not phase["attempted"] for phase in profile["receipt"]["phases"])
            )
            self.assertEqual(profile["judgment"]["status"], "blocked")

    def test_serializer_rejects_forged_identity_and_candidate_receipt(self) -> None:
        pin = fake_pin()
        with mock.patch("agent_team.devin_probe.static_preflight", return_value=pin):
            probe = build_static_probe()
            forged_pin = replace(
                probe,
                pin=replace(
                    probe.pin,
                    file_identity=FileIdentity(1, 2, 3, 4, DEVIN_SHA256),
                ),
            )
            with self.assertRaises(DevinProbeError):
                serialize_static_probe(forged_pin)
            forged_record = replace(
                probe.profiles[0],
                judgment=Judgment("devin", "read-only", "candidate", ()),
            )
            forged_candidate = replace(
                probe, profiles=(forged_record, probe.profiles[1])
            )
            with self.assertRaises(DevinProbeError):
                serialize_static_probe(forged_candidate)
            acp_candidate = replace(
                probe.profiles[1],
                receipt=candidate_receipt(probe.profiles[1]),
                judgment=Judgment("devin", "read-only", "candidate", ()),
            )
            with self.assertRaises(DevinProbeError):
                serialize_static_probe(
                    replace(probe, profiles=(probe.profiles[0], acp_candidate))
                )
            unblocked_record = replace(
                probe.profiles[1],
                receipt=replace(probe.profiles[1].receipt, blocked_reason=None),
            )
            with self.assertRaises(DevinProbeError):
                serialize_static_probe(
                    replace(probe, profiles=(probe.profiles[0], unblocked_record))
                )

    def test_serializer_rejects_profile_order_policy_and_blocker_tampering(
        self,
    ) -> None:
        pin = fake_pin()
        with mock.patch("agent_team.devin_probe.static_preflight", return_value=pin):
            probe = build_static_probe()
            swapped = replace(probe, profiles=tuple(reversed(probe.profiles)))
            with self.assertRaises(DevinProbeError):
                validate_static_probe(swapped)
            wrong_blocker = replace(
                probe,
                blocked=BlockedState(
                    "authentication",
                    probe.blocked.observations,
                ),
            )
            with self.assertRaises(DevinProbeError):
                validate_static_probe(wrong_blocker)

    def test_public_static_builder_has_no_caller_argv_or_environment_inputs(
        self,
    ) -> None:
        self.assertEqual(tuple(inspect.signature(build_static_probe).parameters), ())
        self.assertEqual(tuple(inspect.signature(static_preflight).parameters), ())
        self.assertFalse(
            hasattr(
                __import__("agent_team.devin_probe", fromlist=["x"]), "run_live_probe"
            )
        )
        self.assertFalse(
            hasattr(
                __import__("agent_team.devin_probe", fromlist=["x"]),
                "parse_devin_events",
            )
        )
        self.assertFalse(
            hasattr(
                __import__("agent_team.devin_probe", fromlist=["x"]), "attest_profile"
            )
        )


if __name__ == "__main__":
    unittest.main()
