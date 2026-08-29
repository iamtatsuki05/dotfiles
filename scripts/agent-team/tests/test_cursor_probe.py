from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from agent_team import cursor_probe
from agent_team.adapters import FileIdentity
from agent_team.probe_receipts import Judgment


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
    wrapper_text: str = "#!/bin/sh\necho 'Cursor Agent 2026.05.09-0afadcc'\n",
) -> tuple[Path, Path, Path, cursor_probe.CursorInstallationPin]:
    home = root / "home"
    install_dir = (
        home / ".local/share/mise/installs/http-cursor-agent/2026.05.09-0afadcc"
    )
    bundle_dir = home / ".local/share/mise/http-tarballs/test-bundle"
    install_dir.mkdir(parents=True)
    bundle_dir.mkdir(parents=True)
    executable = install_dir / "cursor-agent"
    canonical = bundle_dir / "cursor-agent"
    executable.write_text(wrapper_text, encoding="utf-8")
    executable.chmod(0o755)
    os.link(executable, canonical)
    bundle = bundle_dir / "index.js"
    bundle.write_text("bundle", encoding="utf-8")
    node = bundle_dir / "node"
    node.write_text("node", encoding="utf-8")
    node.chmod(0o755)
    private_root = root / "private"
    private_root.mkdir(mode=0o700)
    workspace = root / "workspace"
    workspace.mkdir()
    pin = replace(
        cursor_probe.CURSOR_INSTALLATION_PIN,
        canonical_relative_path=".local/share/mise/http-tarballs/test-bundle/cursor-agent",
        bundle_relative_path=".local/share/mise/http-tarballs/test-bundle/index.js",
        node_relative_path=".local/share/mise/http-tarballs/test-bundle/node",
        wrapper_sha256=digest(executable),
        bundle_sha256=digest(bundle),
    )
    return home, workspace, private_root, pin


class CursorStaticProbeTest(unittest.TestCase):
    def run_static(
        self,
        root: Path,
        *,
        profile: cursor_probe.CursorProfile = "direct-plan",
        pin: cursor_probe.CursorInstallationPin | None = None,
        wrapper_text: str | None = None,
    ) -> tuple[cursor_probe.CursorStaticReport, Path, Path, Path]:
        home, workspace, private_root, fixture_pin = fixture_installation(
            root, wrapper_text=wrapper_text or "#!/bin/sh\necho banner\n"
        )
        with (
            mock.patch.object(Path, "home", return_value=home),
            mock.patch.object(
                cursor_probe,
                "CURSOR_INSTALLATION_PIN",
                pin or fixture_pin,
            ),
        ):
            report = cursor_probe.static_probe(
                profile,
                workspace=workspace,
                private_root=private_root,
            )
        return report, home, workspace, private_root

    def test_production_pin_is_the_inventory_singleton(self) -> None:
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
        self.assertEqual(pin.node_version, "v24.5.0")

    def test_static_preflight_accepts_same_banner_without_executing_a_provider(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home, _, private_root, pin = fixture_installation(root)
            with (
                mock.patch.object(Path, "home", return_value=home),
                mock.patch.object(cursor_probe, "CURSOR_INSTALLATION_PIN", pin),
            ):
                observed = cursor_probe.static_preflight(private_root=private_root)

            self.assertEqual(observed.wrapper_identity.sha256, pin.wrapper_sha256)
            self.assertEqual(observed.bundle_identity.sha256, pin.bundle_sha256)
            self.assertEqual(observed.node_identity.size, 4)

    def test_same_banner_with_wrong_wrapper_hash_fails_before_any_provider_call(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home, _, private_root, pin = fixture_installation(root)
            wrong_pin = replace(pin, wrapper_sha256="0" * 64)
            with (
                mock.patch.object(Path, "home", return_value=home),
                mock.patch.object(cursor_probe, "CURSOR_INSTALLATION_PIN", wrong_pin),
                self.assertRaises(cursor_probe.CursorStaticPreflightError),
            ):
                cursor_probe.static_preflight(private_root=private_root)

    def test_caller_cannot_select_an_alternate_binary_or_run_version_preflight(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home, _, private_root, pin = fixture_installation(root)
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
                observed = cursor_probe.static_preflight(private_root=private_root)

            self.assertNotEqual(observed.executable_path, alternate)
            self.assertEqual(
                observed.executable_path, home / pin.executable_relative_path
            )

    def test_direct_and_acp_reports_are_separate_and_only_not_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            direct, _, _, _ = self.run_static(root)
            acp, _, _, _ = self.run_static(root / "acp", profile="acp")

        self.assertEqual(direct.judgment.status, "not-run")
        self.assertEqual(acp.judgment.status, "not-run")
        self.assertEqual(direct.judgment.reason_codes, ("phase-not-attempted",))
        self.assertNotEqual(
            direct.manifest.identity.prompt_transport,
            acp.manifest.identity.prompt_transport,
        )
        self.assertNotEqual(
            direct.manifest.identity.sandbox_policy_id,
            acp.manifest.identity.sandbox_policy_id,
        )
        self.assertTrue(all(not phase.attempted for phase in direct.receipt.phases))

    def test_static_report_serializer_recomputes_forged_judgment_and_redacts_paths(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            report, home, workspace, private_root = self.run_static(root)
            forged = replace(
                report,
                judgment=Judgment("cursor", "read-only", "candidate", ()),
            )
            with (
                mock.patch.object(Path, "home", return_value=home),
                mock.patch.object(
                    cursor_probe, "CURSOR_INSTALLATION_PIN", report.preflight.pin
                ),
            ):
                serialized = cursor_probe.serialize_static_report(forged)
                repeated = cursor_probe.serialize_static_report(report)

        self.assertEqual(serialized, repeated)
        self.assertNotIn("candidate", serialized)
        self.assertNotIn(str(home), serialized)
        self.assertNotIn(str(workspace), serialized)
        self.assertNotIn(str(private_root), serialized)
        self.assertNotIn("--force", serialized)
        self.assertIn("authenticated-at-inventory", serialized)
        self.assertIn("wrapper_sha256", serialized)

    def test_private_root_must_be_fresh_owner_only_and_non_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home, _, private_root, pin = fixture_installation(root)
            private_root.chmod(0o755)
            with (
                mock.patch.object(Path, "home", return_value=home),
                mock.patch.object(cursor_probe, "CURSOR_INSTALLATION_PIN", pin),
                self.assertRaises(cursor_probe.CursorStaticPreflightError),
            ):
                cursor_probe.static_preflight(private_root=private_root)

            private_root.chmod(0o700)
            (private_root / "stale").write_text("stale", encoding="utf-8")
            with (
                mock.patch.object(Path, "home", return_value=home),
                mock.patch.object(cursor_probe, "CURSOR_INSTALLATION_PIN", pin),
                self.assertRaises(cursor_probe.CursorStaticPreflightError),
            ):
                cursor_probe.static_preflight(private_root=private_root)

    def test_bundle_and_canonical_wrapper_drift_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home, _, private_root, pin = fixture_installation(root)
            canonical = home / pin.canonical_relative_path
            canonical.unlink()
            canonical.write_text("different", encoding="utf-8")
            with (
                mock.patch.object(Path, "home", return_value=home),
                mock.patch.object(cursor_probe, "CURSOR_INSTALLATION_PIN", pin),
                self.assertRaises(cursor_probe.CursorStaticPreflightError),
            ):
                cursor_probe.static_preflight(private_root=private_root)


if __name__ == "__main__":
    unittest.main()
