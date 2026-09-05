from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from agent_team import cursor_probe
from agent_team.adapters import FileIdentity


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def identity(path: Path) -> FileIdentity:
    stat_result = path.stat()
    return FileIdentity(
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_size,
        stat_result.st_mtime_ns,
        digest(path),
    )


def fixture_installation(
    root: Path,
    *,
    symlink: bool = False,
    wrapper_text: str = "#!/bin/sh\necho 'Cursor Agent 2026.05.09-0afadcc'\n",
) -> tuple[Path, cursor_probe.CursorInstallationPin]:
    home = root / "home"
    install_dir = (
        home / ".local/share/mise/installs/http-cursor-agent/2026.05.09-0afadcc"
    )
    bundle_dir = home / ".local/share/mise/http-tarballs/test-bundle"
    install_dir.mkdir(parents=True)
    bundle_dir.mkdir(parents=True)
    executable = install_dir / "cursor-agent"
    canonical = bundle_dir / "cursor-agent"
    if symlink:
        canonical.write_text(wrapper_text, encoding="utf-8")
        canonical.chmod(0o755)
        executable.symlink_to(canonical)
    else:
        executable.write_text(wrapper_text, encoding="utf-8")
        executable.chmod(0o755)
        os.link(executable, canonical)
    bundle = bundle_dir / "index.js"
    bundle.write_text("bundle", encoding="utf-8")
    node = bundle_dir / "node"
    node.write_text("node", encoding="utf-8")
    node.chmod(0o755)
    pin = replace(
        cursor_probe.CURSOR_INSTALLATION_PIN,
        canonical_relative_path=".local/share/mise/http-tarballs/test-bundle/cursor-agent",
        bundle_relative_path=".local/share/mise/http-tarballs/test-bundle/index.js",
        node_relative_path=".local/share/mise/http-tarballs/test-bundle/node",
        wrapper_sha256=digest(canonical),
        bundle_sha256=digest(bundle),
        node_sha256=digest(node),
    )
    return home, pin


class CursorStaticProbeTest(unittest.TestCase):
    def run_static(
        self,
        root: Path,
        *,
        profile: cursor_probe.CursorProfile = "direct-plan",
        symlink: bool = False,
    ) -> tuple[
        cursor_probe.CursorStaticReport, Path, cursor_probe.CursorInstallationPin
    ]:
        home, pin = fixture_installation(root, symlink=symlink)
        with (
            mock.patch.object(Path, "home", return_value=home),
            mock.patch.object(cursor_probe, "CURSOR_INSTALLATION_PIN", pin),
        ):
            report = cursor_probe.static_probe(profile)
        return report, home, pin

    def test_production_pin_contains_inventory_identity(self) -> None:
        pin = cursor_probe.CURSOR_INSTALLATION_PIN

        self.assertEqual(pin.version, "2026.05.09-0afadcc")
        self.assertEqual(
            pin.executable_relative_path,
            ".local/share/mise/installs/http-cursor-agent/2026.05.09-0afadcc/cursor-agent",
        )
        self.assertEqual(
            pin.wrapper_sha256,
            "b7babf47d8b1eee28ac27a74affa02a559bb38103a6e71fbb1f120805d51fedf",
        )
        self.assertEqual(
            pin.bundle_sha256,
            "cbe95bd372a165cebd83658a293d844cae2dfddc7e1e0aef59e704d16d960257",
        )
        self.assertEqual(
            pin.node_sha256,
            "336b5b3ebc5deb86df842102b20b6e4761605b7a667823e68dda7761b91a161b",
        )
        self.assertEqual(pin.node_version, "v24.5.0")

    def test_static_preflight_checks_same_banner_files_without_provider_execution(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home, pin = fixture_installation(root)
            with (
                mock.patch.object(Path, "home", return_value=home),
                mock.patch.object(cursor_probe, "CURSOR_INSTALLATION_PIN", pin),
            ):
                observed = cursor_probe.static_preflight()

            self.assertEqual(observed.wrapper_identity.sha256, pin.wrapper_sha256)
            self.assertEqual(observed.bundle_identity.sha256, pin.bundle_sha256)
            self.assertEqual(observed.node_identity.sha256, pin.node_sha256)

    def test_same_banner_with_wrong_hash_fails_closed_before_any_version_command(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home, pin = fixture_installation(root)
            wrong_pin = replace(pin, wrapper_sha256="0" * 64)
            with (
                mock.patch.object(Path, "home", return_value=home),
                mock.patch.object(cursor_probe, "CURSOR_INSTALLATION_PIN", wrong_pin),
                self.assertRaises(cursor_probe.CursorStaticPreflightError),
            ):
                cursor_probe.static_preflight()

    def test_wrong_node_hash_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home, pin = fixture_installation(root)
            wrong_pin = replace(pin, node_sha256="0" * 64)
            with (
                mock.patch.object(Path, "home", return_value=home),
                mock.patch.object(cursor_probe, "CURSOR_INSTALLATION_PIN", wrong_pin),
                self.assertRaises(cursor_probe.CursorStaticPreflightError),
            ):
                cursor_probe.static_preflight()

    def test_caller_cannot_select_an_alternate_binary_or_path_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home, pin = fixture_installation(root)
            alternate = root / "evil"
            alternate.write_text(
                "#!/bin/sh\necho 'Cursor Agent 2026.05.09-0afadcc'\n", encoding="utf-8"
            )
            alternate.chmod(0o755)
            with (
                mock.patch.object(Path, "home", return_value=home),
                mock.patch.object(cursor_probe, "CURSOR_INSTALLATION_PIN", pin),
                mock.patch("shutil.which", side_effect=AssertionError("PATH lookup")),
            ):
                observed = cursor_probe.static_preflight()

            self.assertNotEqual(observed.wrapper_path_sha256, digest(alternate))
            self.assertEqual(
                observed.wrapper_path_sha256,
                hashlib.sha256(
                    str(home / pin.executable_relative_path).encode("utf-8")
                ).hexdigest(),
            )

    def test_direct_and_acp_reports_are_separate_and_only_not_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            direct, _, _ = self.run_static(Path(temp_dir))
            acp, _, _ = self.run_static(Path(temp_dir) / "acp", profile="acp")

        self.assertEqual(direct.status, "not-run")
        self.assertEqual(acp.status, "not-run")
        self.assertEqual(direct.reason_codes, ("phase-not-attempted",))
        self.assertNotEqual(direct.prompt_transport, acp.prompt_transport)
        self.assertNotEqual(direct.sandbox_policy_id, acp.sandbox_policy_id)
        self.assertEqual(direct.required_phases, acp.required_phases)
        self.assertTrue(all(outcome == "not-run" for outcome in direct.phase_outcomes))

    def test_public_report_has_no_generic_receipt_or_raw_paths_and_rejects_candidate_constructor(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home, pin = fixture_installation(root)
            with (
                mock.patch.object(Path, "home", return_value=home),
                mock.patch.object(cursor_probe, "CURSOR_INSTALLATION_PIN", pin),
            ):
                report = cursor_probe.static_probe("direct-plan")
                self.assertFalse(hasattr(report, "manifest"))
                self.assertFalse(hasattr(report, "receipt"))
                self.assertFalse(hasattr(report, "judgment"))
                self.assertFalse(hasattr(report, "workspace"))
                with self.assertRaises(cursor_probe.CursorStaticPreflightError):
                    replace(report, status="candidate")  # type: ignore[arg-type]
                serialized = cursor_probe.serialize_static_report(report)

            self.assertNotIn(str(home), serialized)
            self.assertNotIn("candidate", serialized)
            self.assertNotIn("<private-root>", serialized)

    def test_focused_probe_does_not_require_cursor_under_an_empty_home(self) -> None:
        with (
            mock.patch.object(Path, "home", return_value=Path("/tmp")),
            self.assertRaises(cursor_probe.CursorStaticPreflightError),
        ):
            cursor_probe.static_preflight()

    def test_serializer_recomputes_forged_identity_and_records_historical_auth(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            report, home, pin = self.run_static(Path(temp_dir))
            forged = replace(
                report,
                installation=replace(
                    report.installation,
                    wrapper_identity=replace(
                        report.installation.wrapper_identity, sha256="0" * 64
                    ),
                ),
            )
            with (
                mock.patch.object(Path, "home", return_value=home),
                mock.patch.object(cursor_probe, "CURSOR_INSTALLATION_PIN", pin),
            ):
                serialized = cursor_probe.serialize_static_report(forged)

        payload = json.loads(serialized)
        self.assertEqual(payload["status"], "not-run")
        self.assertEqual(payload["auth"]["status"], "historical-unverified")
        self.assertEqual(payload["auth"]["observed_at"], "2026-08-29T18:51:06Z")
        self.assertRegex(payload["auth"]["source_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(payload["installation"]["wrapper_sha256"], pin.wrapper_sha256)

    def test_public_auth_provenance_cannot_be_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            report, _, _ = self.run_static(Path(temp_dir))

            with self.assertRaises(cursor_probe.CursorStaticPreflightError):
                replace(
                    report,
                    auth=replace(report.auth, source_sha256="0" * 64),
                )

    def test_private_root_argument_and_live_status_api_are_not_available(self) -> None:
        with self.assertRaises(TypeError):
            cursor_probe.static_preflight(private_root=Path("/tmp/private"))  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            cursor_probe.static_probe("direct-plan", workspace=Path("/tmp"))  # type: ignore[call-arg]
        self.assertFalse(hasattr(cursor_probe, "CursorProbe"))
        self.assertFalse(hasattr(cursor_probe, "evaluate_profile"))

    def test_symlink_target_and_readlink_drift_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home, pin = fixture_installation(root, symlink=True)
            executable = home / pin.executable_relative_path
            alternate = root / "alternate"
            alternate.write_text("alternate", encoding="utf-8")
            alternate.chmod(0o755)
            with (
                mock.patch.object(Path, "home", return_value=home),
                mock.patch.object(cursor_probe, "CURSOR_INSTALLATION_PIN", pin),
            ):
                first = cursor_probe.static_preflight()
                executable.unlink()
                executable.symlink_to(alternate)
                with self.assertRaises(cursor_probe.CursorStaticPreflightError):
                    cursor_probe.static_preflight()

            self.assertEqual(first.wrapper_identity.sha256, pin.wrapper_sha256)


if __name__ == "__main__":
    unittest.main()
