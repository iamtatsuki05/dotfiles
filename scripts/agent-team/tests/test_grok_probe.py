from __future__ import annotations

import json
import os
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest import mock

from agent_team import grok_probe
from agent_team.grok_probe import (
    GROK_BINARY_SHA256,
    GROK_CDHASH,
    GROK_COMMIT,
    GROK_PACKAGE_SHA256,
    GROK_TEAM_ID,
    GROK_VERSION,
    GROK_WRAPPER_SHA256,
    PINNED_GROK_PROVENANCE,
    AuthMarkerStatus,
    GrokBinaryIdentity,
    GrokBinaryResolver,
    GrokProbeError,
    GrokProvenance,
    GrokSignature,
    _FileSnapshot,
    build_isolated_environment,
    build_profile_manifest,
    offline_preflight,
    parse_grok_version,
    serialize_grok_receipt,
    validate_grok_identity,
)
from agent_team.probe_receipts import Judgment

_BANNER = f"grok {GROK_VERSION} ({GROK_COMMIT}) [alpha]"


def _write_file(path: Path, content: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if executable:
        path.chmod(0o755)


def _fixture_tree(root: Path) -> tuple[Path, Path, Path]:
    home = root / "home"
    canonical_dir = home / ".grok" / "bin"
    target = canonical_dir / "grok-1.0.13"
    wrapper = (
        home
        / ".local"
        / "share"
        / "mise"
        / "installs"
        / "npm-xai-official-grok"
        / GROK_VERSION
        / "lib/node_modules/@xai-official/grok/bin/grok"
    )
    package = wrapper.parent.parent / "package.json"
    install_root = wrapper.parents[5]
    path_entry = install_root / "bin" / "grok"
    _write_file(
        target,
        f"#!/bin/sh\nprintf '%s\\n' '{_BANNER}'\ntouch should-not-run\n",
        executable=True,
    )
    _write_file(
        wrapper,
        f"#!/bin/sh\nprintf '%s\\n' '{_BANNER}'\ntouch should-not-run\n",
        executable=True,
    )
    _write_file(package, '{"name":"@xai-official/grok","version":"1.0.13"}\n')
    canonical_dir.mkdir(parents=True, exist_ok=True)
    (home / ".grok" / "bin" / "grok").symlink_to(target.name)
    path_entry.parent.mkdir(parents=True, exist_ok=True)
    path_entry.symlink_to(os.path.relpath(wrapper, path_entry.parent))
    return home, path_entry, target


def _resolver(root: Path) -> tuple[GrokBinaryResolver, Path, Path]:
    home, path_entry, target = _fixture_tree(root)
    resolver = GrokBinaryResolver(
        home=home,
        expected_path_entry=path_entry,
        path_lookup=lambda: path_entry,
    )
    return resolver, path_entry, target


@contextmanager
def _static_fixture(
    *,
    wrapper_sha256: str = GROK_WRAPPER_SHA256,
    package_sha256: str = GROK_PACKAGE_SHA256,
    signature: GrokSignature | None = None,
) -> Iterator[None]:
    def file_identity(_path: Path, field: str) -> _FileSnapshot:
        path_stat = _path.stat()
        expected = {
            "canonical target": GROK_BINARY_SHA256,
            "Grok wrapper": wrapper_sha256,
            "Grok package metadata": package_sha256,
        }[field]
        return _FileSnapshot(
            expected,
            path_stat.st_dev,
            path_stat.st_ino,
            path_stat.st_size,
            path_stat.st_mtime_ns,
            path_stat.st_ctime_ns,
            None,
        )

    with (
        mock.patch("agent_team.grok_probe._regular_file_identity", file_identity),
        mock.patch(
            "agent_team.grok_probe._codesign_signature",
            return_value=signature or GrokSignature(GROK_TEAM_ID, GROK_CDHASH),
        ),
    ):
        yield


def _resolve_fixture(resolver: GrokBinaryResolver) -> GrokBinaryIdentity:
    with (
        _static_fixture(),
        mock.patch(
            "agent_team.grok_probe._codesign_signature",
            return_value=GrokSignature(GROK_TEAM_ID, GROK_CDHASH),
        ),
    ):
        return resolver.resolve()


def _fresh_private(root: Path, name: str = "private") -> Path:
    private = root / name
    private.mkdir()
    private.chmod(0o700)
    return private


class GrokVersionContractTest(unittest.TestCase):
    def test_current_alpha_banner_returns_exact_version_and_commit(self) -> None:
        self.assertEqual(parse_grok_version(_BANNER), (GROK_VERSION, GROK_COMMIT))

    def test_version_parser_rejects_stale_or_non_alpha_banners(self) -> None:
        for banner in (
            "grok 1.0.5 (5115b46bc909) [alpha]",
            f"grok {GROK_VERSION} (wrongcommit) [alpha]",
            f"grok {GROK_VERSION} ({GROK_COMMIT}) [beta]",
        ):
            with self.subTest(banner=banner), self.assertRaises(GrokProbeError):
                parse_grok_version(banner)


class GrokIdentityContractTest(unittest.TestCase):
    def test_static_resolver_pins_layout_hashes_and_signature_without_running_wrapper(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            resolver, path_entry, target = _resolver(root)
            with mock.patch(
                "agent_team.grok_probe.subprocess.run",
                side_effect=AssertionError("static preflight must not spawn"),
            ):
                identity = _resolve_fixture(resolver)

            self.assertEqual(identity.canonical_link, root / "home/.grok/bin/grok")
            self.assertEqual(identity.canonical_target, target.resolve())
            self.assertEqual(identity.path_entry, path_entry)
            self.assertEqual(identity.version, GROK_VERSION)
            self.assertEqual(identity.commit, GROK_COMMIT)
            self.assertEqual(identity.sha256, GROK_BINARY_SHA256)
            self.assertEqual(identity.wrapper_sha256, GROK_WRAPPER_SHA256)
            self.assertEqual(identity.package_sha256, GROK_PACKAGE_SHA256)
            self.assertEqual(identity.team_id, GROK_TEAM_ID)
            self.assertEqual(identity.cdhash, GROK_CDHASH)
            self.assertEqual(identity.device, target.stat().st_dev)
            self.assertEqual(identity.inode, target.stat().st_ino)
            self.assertFalse((root / "should-not-run").exists())

    def test_expected_provenance_rejects_same_banner_fake_binary_and_wrapper(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home, path_entry, _ = _fixture_tree(root)
            with self.assertRaisesRegex(GrokProbeError, "binary hash"):
                GrokBinaryResolver(
                    home=home,
                    expected_path_entry=path_entry,
                    path_lookup=lambda: path_entry,
                ).resolve()

    def test_wrong_wrapper_package_or_signature_provenance_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home, path_entry, _ = _fixture_tree(root)
            for field in ("wrapper_sha256", "package_sha256"):
                fixture = (
                    _static_fixture(wrapper_sha256="e" * 64)
                    if field == "wrapper_sha256"
                    else _static_fixture(package_sha256="e" * 64)
                )
                with (
                    self.subTest(field=field),
                    fixture,
                    self.assertRaises(GrokProbeError),
                ):
                    GrokBinaryResolver(
                        home=home,
                        expected_path_entry=path_entry,
                        path_lookup=lambda: path_entry,
                    ).resolve()

            wrong_signature = GrokBinaryResolver(
                home=home,
                expected_path_entry=path_entry,
                path_lookup=lambda: path_entry,
            )
            with (
                _static_fixture(),
                mock.patch(
                    "agent_team.grok_probe._codesign_signature",
                    return_value=GrokSignature(GROK_TEAM_ID, "f" * 40),
                ),
                self.assertRaisesRegex(GrokProbeError, "signature"),
            ):
                wrong_signature.resolve()

    def test_production_resolver_has_no_provenance_or_signature_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home, path_entry, _ = _fixture_tree(root)
            with self.assertRaises(TypeError):
                GrokBinaryResolver(
                    home=home,
                    expected_path_entry=path_entry,
                    path_lookup=lambda: path_entry,
                    provenance=GrokProvenance(),  # type: ignore[call-arg]
                )
            with self.assertRaises(TypeError):
                GrokBinaryResolver(
                    home=home,
                    expected_path_entry=path_entry,
                    path_lookup=lambda: path_entry,
                    signature_probe=lambda _: GrokSignature(GROK_TEAM_ID, GROK_CDHASH),  # type: ignore[call-arg]
                )

    def test_provenance_and_identity_constructors_reject_unpinned_fields(self) -> None:
        with self.assertRaises(GrokProbeError):
            replace(PINNED_GROK_PROVENANCE, binary_sha256="e" * 64)
        with tempfile.TemporaryDirectory() as temp_dir:
            resolver, _, _ = _resolver(Path(temp_dir))
            identity = _resolve_fixture(resolver)
            with self.assertRaises(GrokProbeError):
                replace(identity, sha256="e" * 64)
            with self.assertRaises(GrokProbeError):
                replace(identity, team_id="OTHERTEAM")

    def test_static_hash_uses_one_no_follow_fd_and_rejects_fstat_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            regular = root / "regular"
            regular.write_bytes(b"regular")
            link = root / "link"
            link.symlink_to(regular)
            with self.assertRaisesRegex(GrokProbeError, "unavailable"):
                grok_probe._regular_file_identity(link, "probe")

            real_fstat = os.fstat
            calls = 0

            def drifting_fstat(fd: int) -> os.stat_result:
                nonlocal calls
                calls += 1
                result = real_fstat(fd)
                if calls == 2:
                    values = list(result)
                    values[1] += 1
                    return os.stat_result(values)
                return result

            with (
                mock.patch("agent_team.grok_probe.os.fstat", drifting_fstat),
                self.assertRaisesRegex(GrokProbeError, "identity changed"),
            ):
                grok_probe._regular_file_identity(regular, "probe")

    def test_static_hash_rejects_same_size_mtime_and_ctime_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            regular = Path(temp_dir) / "regular"
            regular.write_bytes(b"same-size-content")
            real_fstat = os.fstat
            calls = 0

            def drifting_times(fd: int) -> os.stat_result:
                nonlocal calls
                calls += 1
                result = real_fstat(fd)
                if calls == 2:
                    values = list(result)
                    values[8] += 1
                    values[9] += 1
                    return os.stat_result(values)
                return result

            with (
                mock.patch("agent_team.grok_probe.os.fstat", drifting_times),
                self.assertRaisesRegex(GrokProbeError, "identity changed"),
            ):
                grok_probe._regular_file_identity(regular, "probe")

    def test_resolver_rehashes_after_signature_and_rejects_same_size_content_race(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            resolver, _, target = _resolver(root)
            original_identity = grok_probe._regular_file_identity
            calls: dict[str, int] = {}

            def fixture_identity(path: Path, field: str) -> grok_probe._FileSnapshot:
                actual = original_identity(path, field)
                calls[field] = calls.get(field, 0) + 1
                if field == "canonical target" and calls[field] > 1:
                    return actual
                expected = {
                    "canonical target": GROK_BINARY_SHA256,
                    "Grok wrapper": GROK_WRAPPER_SHA256,
                    "Grok package metadata": GROK_PACKAGE_SHA256,
                }[field]
                return replace(actual, sha256=expected)

            original_content = target.read_bytes()

            def mutate_after_static_hash(_: Path) -> GrokSignature:
                target.write_bytes(b"X" + original_content[1:])
                return GrokSignature(GROK_TEAM_ID, GROK_CDHASH)

            with (
                mock.patch(
                    "agent_team.grok_probe._regular_file_identity",
                    fixture_identity,
                ),
                mock.patch(
                    "agent_team.grok_probe._codesign_signature",
                    mutate_after_static_hash,
                ),
                self.assertRaisesRegex(GrokProbeError, "changed|identity"),
            ):
                resolver.resolve()

            self.assertEqual(calls["canonical target"], 2)

    def test_canonical_link_must_be_the_fixed_home_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home, path_entry, _ = _fixture_tree(root)
            with self.assertRaisesRegex(GrokProbeError, "fixed"):
                GrokBinaryResolver(
                    home=home,
                    canonical_link=root / "alternate" / "grok",
                    expected_path_entry=path_entry,
                    path_lookup=lambda: path_entry,
                )

    def test_path_entry_is_metadata_only_and_never_executes_ambient_wrapper(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            resolver, path_entry, _ = _resolver(root)
            marker = root / "path-entry-ran"
            _write_file(
                path_entry.resolve(),
                f"#!/bin/sh\ntouch {marker}\n",
                executable=True,
            )
            with self.assertRaises(GrokProbeError):
                resolver.resolve()
            self.assertFalse(marker.exists())

    def test_identity_validation_rejects_path_and_symlink_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            resolver, path_entry, _ = _resolver(root)
            identity = _resolve_fixture(resolver)
            replacement = path_entry.parent / "replacement-wrapper"
            _write_file(replacement, "replacement", executable=True)
            path_entry.unlink()
            path_entry.symlink_to(replacement)
            with _static_fixture(), self.assertRaisesRegex(GrokProbeError, "PATH"):
                validate_grok_identity(identity, resolver)

            path_entry.unlink()
            expected_wrapper = (
                root
                / "home/.local/share/mise/installs/npm-xai-official-grok"
                / GROK_VERSION
                / "lib/node_modules/@xai-official/grok/bin/grok"
            )
            path_entry.symlink_to(os.path.relpath(expected_wrapper, path_entry.parent))
            canonical_link = root / "home/.grok/bin/grok"
            _write_file(
                root / "home/.grok/bin/grok-1.0.5",
                "stale",
                executable=True,
            )
            canonical_link.unlink()
            canonical_link.symlink_to("grok-1.0.5")
            with (
                _static_fixture(),
                self.assertRaisesRegex(GrokProbeError, "canonical target"),
            ):
                validate_grok_identity(identity, resolver)


class GrokProfileContractTest(unittest.TestCase):
    def test_direct_and_native_manifests_are_separate_without_live_command_api(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            resolver, _, _ = _resolver(root)
            identity = _resolve_fixture(resolver)
            direct = build_profile_manifest("direct", identity)
            native = build_profile_manifest("native-stdio", identity)

        self.assertNotEqual(direct.identity.argv_sha256, native.identity.argv_sha256)
        self.assertEqual(direct.identity.prompt_transport, "file")
        self.assertEqual(native.identity.prompt_transport, "stdin")
        self.assertIn("unverified", native.identity.sandbox_policy_id)
        self.assertNotIn("acpx", direct.identity.sandbox_policy_id)
        self.assertNotIn("acpx", native.identity.sandbox_policy_id)
        self.assertFalse(hasattr(grok_probe, "prepare_bounded_command"))
        self.assertFalse(hasattr(grok_probe, "build_profile_command"))

    def test_manifest_digest_uses_role_tokens_not_actual_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            resolver, _, _ = _resolver(root)
            first = _resolve_fixture(resolver)
            other_root = root / "other"
            other_root.mkdir()
            other_resolver, _, _ = _resolver(other_root)
            second = _resolve_fixture(other_resolver)
            first_manifest = build_profile_manifest("direct", first)
            second_manifest = build_profile_manifest("direct", second)

        self.assertEqual(
            first_manifest.identity.argv_sha256,
            second_manifest.identity.argv_sha256,
        )
        self.assertEqual(
            first_manifest.identity.executable.sha256,
            second_manifest.identity.executable.sha256,
        )


class GrokIsolationAndReceiptTest(unittest.TestCase):
    def test_private_root_must_be_fresh_owner_only_non_symlink_and_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            private = _fresh_private(root)
            environment = build_isolated_environment(
                private,
                source={
                    "HOME": "/Users/personal",
                    "XAI_API_KEY": "do-not-read",
                    "MCP_CONFIG": "/Users/personal/mcp.json",
                    "NODE_OPTIONS": "--require ambient.js",
                },
            )
            self.assertEqual(environment["HOME"], str(private / "home"))
            self.assertEqual(environment["GROK_HOME"], str(private / "grok-home"))
            self.assertNotIn("XAI_API_KEY", environment)
            self.assertNotIn("MCP_CONFIG", environment)
            self.assertNotIn("NODE_OPTIONS", environment)
            self.assertNotIn("/Users/personal", json.dumps(environment))

            (private / "ambient").touch()
            with self.assertRaisesRegex(GrokProbeError, "empty"):
                build_isolated_environment(private)

            empty = root / "empty"
            empty.mkdir()
            empty.chmod(0o755)
            with self.assertRaisesRegex(GrokProbeError, "owner-only"):
                build_isolated_environment(empty)

            target = root / "target"
            target.mkdir()
            target.chmod(0o700)
            link = root / "link"
            link.symlink_to(target)
            with self.assertRaisesRegex(GrokProbeError, "symlink"):
                build_isolated_environment(link)

    def test_missing_auth_blocks_both_profiles_and_keeps_matrix_not_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            resolver, _, _ = _resolver(root)
            results = {}
            for profile in ("direct", "native-stdio"):
                with _static_fixture():
                    results[profile] = offline_preflight(
                        profile,
                        resolver=resolver,
                        private_root=_fresh_private(root, f"private-{profile}"),
                        auth_path=root / "missing-auth.json",
                        source_environment={"PATH": "/usr/bin"},
                    )

        for profile, result in results.items():
            with self.subTest(profile=profile):
                self.assertEqual(result.receipt.blocked_reason, "authentication")
                self.assertEqual(result.judgment.status, "blocked")
                self.assertEqual(result.matrix_status, "not-run")
                self.assertEqual(result.acpx_status, "not-run")
                self.assertTrue(
                    all(
                        not phase.attempted and phase.outcome == "not-run"
                        for phase in result.receipt.phases
                    )
                )

    def test_auth_marker_is_metadata_only_and_does_not_read_credential_content(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            resolver, _, _ = _resolver(root)
            private = _fresh_private(root)
            auth_path = root / "auth.json"
            auth_path.write_text("credential-must-not-be-read", encoding="utf-8")
            with (
                mock.patch.object(Path, "read_text", side_effect=AssertionError),
                _static_fixture(),
            ):
                result = offline_preflight(
                    "native-stdio",
                    resolver=resolver,
                    private_root=private,
                    auth_path=auth_path,
                    source_environment={"PATH": "/usr/bin"},
                )

        self.assertEqual(result.auth_status, "blocked")
        self.assertEqual(result.auth_marker_status, "present-unverified")
        self.assertEqual(result.judgment.status, "blocked")

    def test_serializer_rechecks_correlation_and_rejects_forged_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            resolver, _, _ = _resolver(root)
            with (
                _static_fixture(),
                mock.patch(
                    "agent_team.grok_probe._default_path_lookup",
                    return_value=resolver.expected_path_entry,
                ),
            ):
                result = offline_preflight(
                    "direct",
                    resolver=resolver,
                    private_root=_fresh_private(root),
                    auth_path=root / "missing-auth.json",
                    source_environment={"PATH": "/usr/bin"},
                )
            with (
                _static_fixture(),
                mock.patch(
                    "agent_team.grok_probe._default_path_lookup",
                    return_value=result.identity.path_entry,
                ),
            ):
                serialized = serialize_grok_receipt(result)
                forged = replace(
                    result,
                    judgment=Judgment("grok", "read-only", "candidate", ()),
                )
                forged_profile = replace(
                    result,
                    manifest=build_profile_manifest("native-stdio", result.identity),
                )
                forged_status = replace(
                    result,
                    auth_marker_status=cast(AuthMarkerStatus, "unexpected"),
                )
                forged_identity = replace(
                    result.identity,
                    path_entry_device=result.identity.path_entry_device + 1,
                )
                forged_identity_result = replace(
                    result,
                    identity=forged_identity,
                )

                payload = json.loads(serialized)
                self.assertEqual(payload["auth_status"], "blocked")
                self.assertEqual(payload["matrix_status"], "not-run")
                self.assertEqual(payload["judgment"]["status"], "blocked")
                self.assertNotIn(str(root), serialized)
                self.assertNotIn("credential", serialized)
                self.assertNotIn("prompt text", serialized)
                with self.assertRaisesRegex(GrokProbeError, "judgment"):
                    serialize_grok_receipt(forged)
                with self.assertRaisesRegex(GrokProbeError, "manifest"):
                    serialize_grok_receipt(forged_profile)
                with self.assertRaisesRegex(GrokProbeError, "auth marker"):
                    serialize_grok_receipt(forged_status)
                with self.assertRaisesRegex(GrokProbeError, "fresh static resolver"):
                    serialize_grok_receipt(forged_identity_result)


if __name__ == "__main__":
    unittest.main()
