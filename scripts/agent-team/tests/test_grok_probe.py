from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_team.grok_probe import (
    GROK_COMMIT,
    GROK_VERSION,
    GrokProbeError,
    build_isolated_environment,
    build_profile_command,
    offline_preflight,
    parse_grok_version,
    prepare_bounded_command,
    serialize_grok_receipt,
    validate_grok_identity,
)

_BANNER = f"grok {GROK_VERSION} ({GROK_COMMIT}) [alpha]"


def _write_executable(path: Path, banner: str) -> None:
    path.write_text(
        "#!/bin/sh\nprintf '%s\\n' " + repr(banner) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _probe_tree(root: Path) -> tuple[Path, Path, Path]:
    bin_dir = root / "grok-bin"
    bin_dir.mkdir()
    canonical_target = bin_dir / "grok-1.0.13"
    _write_executable(canonical_target, _BANNER)
    stale_target = bin_dir / "grok-1.0.5"
    _write_executable(stale_target, "grok 1.0.5 (5115b46bc909) [alpha]")
    canonical_link = bin_dir / "grok"
    canonical_link.symlink_to(canonical_target.name)
    path_entry = root / "path-grok"
    _write_executable(path_entry, _BANNER)
    return canonical_link, canonical_target, path_entry


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
    def test_resolver_fixes_canonical_target_and_path_identity_without_fallback(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            canonical_link, canonical_target, path_entry = _probe_tree(root)
            calls: list[Path] = []

            def version_probe(path: Path) -> str:
                calls.append(path)
                return (
                    _BANNER
                    if path == canonical_target.resolve()
                    or path == path_entry.resolve()
                    else ""
                )

            from agent_team.grok_probe import GrokBinaryResolver

            identity = GrokBinaryResolver(
                canonical_link=canonical_link,
                path_lookup=lambda: path_entry,
                version_probe=version_probe,
            ).resolve()

            self.assertEqual(identity.canonical_target, canonical_target.resolve())
            self.assertEqual(identity.canonical_link, canonical_link)
            self.assertEqual(identity.path_entry, path_entry)
            self.assertEqual(identity.version, GROK_VERSION)
            self.assertEqual(identity.commit, GROK_COMMIT)
            self.assertEqual(
                identity.sha256,
                hashlib.sha256(canonical_target.read_bytes()).hexdigest(),
            )
            self.assertEqual(identity.device, canonical_target.stat().st_dev)
            self.assertEqual(identity.inode, canonical_target.stat().st_ino)
            self.assertEqual(identity.symlink_device, canonical_link.lstat().st_dev)
            self.assertEqual(identity.symlink_inode, canonical_link.lstat().st_ino)
            self.assertEqual(calls, [canonical_target.resolve(), path_entry.resolve()])

    def test_stale_canonical_target_is_rejected_even_when_other_binary_exists(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            canonical_link, _, path_entry = _probe_tree(root)
            stale = root / "grok-bin" / "grok-1.0.5"
            canonical_link.unlink()
            canonical_link.symlink_to(stale.name)
            from agent_team.grok_probe import GrokBinaryResolver

            with self.assertRaisesRegex(GrokProbeError, "canonical target"):
                GrokBinaryResolver(
                    canonical_link=canonical_link,
                    path_lookup=lambda: path_entry,
                    version_probe=lambda _: "grok 1.0.5 (5115b46bc909) [alpha]",
                ).resolve()

    def test_identity_validation_rejects_path_or_symlink_drift_without_fallback(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            canonical_link, _, path_entry = _probe_tree(root)
            from agent_team.grok_probe import GrokBinaryResolver

            resolver = GrokBinaryResolver(
                canonical_link=canonical_link,
                path_lookup=lambda: path_entry,
                version_probe=lambda _: _BANNER,
            )
            identity = resolver.resolve()
            _write_executable(path_entry, "grok 1.0.5 (5115b46bc909) [alpha]")
            with self.assertRaisesRegex(GrokProbeError, "identity changed"):
                validate_grok_identity(identity, resolver)

            _write_executable(path_entry, _BANNER)
            canonical_link.unlink()
            canonical_link.symlink_to("grok-1.0.5")
            with self.assertRaisesRegex(GrokProbeError, "canonical target"):
                validate_grok_identity(identity, resolver)


class GrokProfileContractTest(unittest.TestCase):
    def test_direct_and_native_stdio_commands_are_separate_and_never_acpx(
        self,
    ) -> None:
        executable = Path("/__agent_team_probe__/grok/grok-1.0.13")
        direct = build_profile_command(
            "direct",
            executable,
            prompt_file=Path("/__agent_team_probe__/prompt.txt"),
        )
        native = build_profile_command("native-stdio", executable)

        self.assertNotEqual(direct, native)
        self.assertEqual(native, (str(executable), "agent", "--no-leader", "stdio"))
        self.assertIn("--no-subagents", direct)
        self.assertIn("MCPTool(*)", direct)
        self.assertIn("WebFetch(*)", direct)
        self.assertNotIn("acpx", " ".join(direct + native))

    def test_live_command_revalidates_exact_identity_before_building_argv(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            canonical_link, _, path_entry = _probe_tree(root)
            from agent_team.grok_probe import GrokBinaryResolver

            resolver = GrokBinaryResolver(
                canonical_link=canonical_link,
                path_lookup=lambda: path_entry,
                version_probe=lambda _: _BANNER,
            )
            identity = resolver.resolve()
            command = prepare_bounded_command(
                "direct",
                identity,
                resolver=resolver,
                private_root=root / "private",
                prompt_file=root / "private" / "prompt.txt",
            )
            self.assertEqual(command.argv[0], str(identity.canonical_target))
            self.assertEqual(command.timeout_seconds, 900.0)

            _write_executable(path_entry, "grok 1.0.5 (5115b46bc909) [alpha]")
            with self.assertRaisesRegex(GrokProbeError, "identity changed"):
                prepare_bounded_command(
                    "direct",
                    identity,
                    resolver=resolver,
                    private_root=root / "private",
                    prompt_file=root / "private" / "prompt.txt",
                )


class GrokIsolationAndReceiptTest(unittest.TestCase):
    def test_isolated_environment_drops_credentials_and_ambient_controls(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            private_root = Path(temp_dir) / "private"
            environment = build_isolated_environment(
                private_root,
                source={
                    "PATH": "/usr/bin",
                    "HOME": "/Users/personal",
                    "XAI_API_KEY": "do-not-read",
                    "MCP_CONFIG": "/Users/personal/mcp.json",
                    "NODE_OPTIONS": "--require ambient.js",
                },
            )

        self.assertEqual(environment["HOME"], str(private_root / "home"))
        self.assertEqual(environment["GROK_HOME"], str(private_root / "grok-home"))
        for name in (
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "XDG_CACHE_HOME",
        ):
            self.assertEqual(environment[name], str(private_root / name.lower()))
        self.assertEqual(environment["GROK_SUBAGENTS"], "0")
        self.assertEqual(environment["GROK_DISABLE_AUTOUPDATER"], "1")
        self.assertNotIn("XAI_API_KEY", environment)
        self.assertNotIn("MCP_CONFIG", environment)
        self.assertNotIn("NODE_OPTIONS", environment)
        self.assertNotIn("/Users/personal", json.dumps(environment))

    def test_missing_auth_makes_both_profile_receipts_blocked_and_cells_not_run(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            canonical_link, _, path_entry = _probe_tree(root)
            from agent_team.grok_probe import GrokBinaryResolver

            resolver = GrokBinaryResolver(
                canonical_link=canonical_link,
                path_lookup=lambda: path_entry,
                version_probe=lambda _: _BANNER,
            )
            results = {
                profile: offline_preflight(
                    profile,
                    resolver=resolver,
                    private_root=root / profile,
                    auth_path=root / "missing-auth.json",
                    source_environment={"PATH": "/usr/bin"},
                )
                for profile in ("direct", "native-stdio")
            }

        for profile, result in results.items():
            with self.subTest(profile=profile):
                self.assertEqual(result.receipt.blocked_reason, "authentication")
                self.assertEqual(result.judgment.status, "blocked")
                self.assertEqual(result.matrix_status, "not-run")
                self.assertEqual(result.acpx_status, "not-run")
                self.assertTrue(
                    all(phase.outcome == "not-run" for phase in result.receipt.phases)
                )
                self.assertTrue(
                    all(not phase.attempted for phase in result.receipt.phases)
                )
        self.assertNotEqual(
            results["direct"].manifest.identity.prompt_transport,
            results["native-stdio"].manifest.identity.prompt_transport,
        )
        self.assertNotEqual(
            results["direct"].manifest.identity.sandbox_policy_id,
            results["native-stdio"].manifest.identity.sandbox_policy_id,
        )

    def test_receipt_serialization_redacts_paths_prompts_logs_and_environment_values(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            canonical_link, _, path_entry = _probe_tree(root)
            from agent_team.grok_probe import GrokBinaryResolver

            resolver = GrokBinaryResolver(
                canonical_link=canonical_link,
                path_lookup=lambda: path_entry,
                version_probe=lambda _: _BANNER,
            )
            result = offline_preflight(
                "direct",
                resolver=resolver,
                private_root=root / "private-personal-path",
                auth_path=root / "auth.json",
                source_environment={"PATH": "/usr/bin", "XAI_API_KEY": "secret"},
            )
            serialized = serialize_grok_receipt(result)
            payload = json.loads(serialized)

        self.assertEqual(serialized, serialize_grok_receipt(result))
        self.assertEqual(payload["artifact"], "grok-probe-receipt")
        self.assertEqual(payload["auth_status"], "blocked")
        self.assertEqual(payload["matrix_status"], "not-run")
        self.assertEqual(payload["acpx_status"], "not-run")
        self.assertEqual(payload["binary"]["version"], GROK_VERSION)
        self.assertEqual(payload["binary"]["commit"], GROK_COMMIT)
        self.assertNotIn("do-not-read", serialized)
        self.assertNotIn("XAI_API_KEY", serialized)
        self.assertNotIn("private-personal-path", serialized)
        self.assertNotIn("auth.json", serialized)
        self.assertNotIn("credential-must-not-be-read", serialized)
        self.assertNotIn("raw", serialized.lower())

    def test_auth_marker_is_never_read_and_stays_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            canonical_link, _, path_entry = _probe_tree(root)
            auth_path = root / "auth.json"
            auth_path.write_text("credential-must-not-be-read", encoding="utf-8")
            from agent_team.grok_probe import GrokBinaryResolver

            resolver = GrokBinaryResolver(
                canonical_link=canonical_link,
                path_lookup=lambda: path_entry,
                version_probe=lambda _: _BANNER,
            )
            with mock.patch.object(Path, "read_text", side_effect=AssertionError):
                result = offline_preflight(
                    "native-stdio",
                    resolver=resolver,
                    private_root=root / "private",
                    auth_path=auth_path,
                    source_environment={"PATH": "/usr/bin"},
                )

        self.assertEqual(result.receipt.blocked_reason, "authentication")
        self.assertEqual(result.judgment.status, "blocked")


if __name__ == "__main__":
    unittest.main()
