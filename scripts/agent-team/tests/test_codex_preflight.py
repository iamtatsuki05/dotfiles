from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_team import codex_preflight
from agent_team.runtime import RuntimeValidationError


class CodexFileAuthPrelaunchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="agent-team-fake-auth-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name).resolve() / "auth.json"
        self.now = 1_800_000_000

    @staticmethod
    def token(payload: object) -> str:
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
        return "e30." + encoded.rstrip("=") + ".fixture-signature"

    def write_auth(self, *, plan: str = "pro", expiry: object = None) -> dict:
        value = {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "id_token": self.token(
                    {"https://api.openai.com/auth": {"chatgpt_plan_type": plan}}
                ),
                "access_token": self.token(
                    {"exp": self.now + 3_600 if expiry is None else expiry}
                ),
                "refresh_token": "synthetic-refresh-never-used",
                "account_id": "synthetic-account",
            },
            "last_refresh": "2000-01-01T00:00:00Z",
        }
        self.path.write_text(json.dumps(value), encoding="utf-8")
        self.path.chmod(0o600)
        return value

    def test_fresh_personal_file_is_read_only_and_contains_no_credentials(self) -> None:
        self.write_auth()
        before = self.path.read_bytes()
        binding = codex_preflight.inspect_file_auth(self.path, now=self.now)
        self.assertEqual(binding.plan_type, "pro")
        self.assertEqual(binding.expires_at, self.now + 3_600)
        self.assertEqual(binding.sha256, hashlib.sha256(before).hexdigest())
        self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn("synthetic-", repr(binding))

    def test_codex_default_chatgpt_mode_requires_absent_alternative_credentials(
        self,
    ) -> None:
        for mode in ("omitted", None):
            with self.subTest(mode=mode):
                value = self.write_auth()
                if mode == "omitted":
                    del value["auth_mode"]
                else:
                    value["auth_mode"] = None
                self.path.write_text(json.dumps(value))
                self.assertEqual(
                    codex_preflight.inspect_file_auth(
                        self.path, now=self.now
                    ).plan_type,
                    "pro",
                )
        for alternative in (
            "OPENAI_API_KEY",
            "personal_access_token",
            "bedrock_api_key",
            "bedrock_access_keys",
            "agent_identity",
        ):
            with self.subTest(alternative=alternative):
                value = self.write_auth()
                del value["auth_mode"]
                value[alternative] = ""
                self.path.write_text(json.dumps(value))
                with self.assertRaises(RuntimeValidationError):
                    codex_preflight.inspect_file_auth(self.path, now=self.now)

    def test_cloud_eligible_or_unknown_plan_and_stale_access_token_fail(self) -> None:
        for plan in ("business", "enterprise", "edu", "education", "hc", "unknown"):
            with self.subTest(plan=plan):
                self.write_auth(plan=plan)
                with self.assertRaisesRegex(RuntimeValidationError, "personal"):
                    codex_preflight.inspect_file_auth(self.path, now=self.now)
        for expiry in (self.now - 1, self.now + 360, True, "future", 2**64):
            with self.subTest(expiry=expiry):
                self.write_auth(expiry=expiry)
                with self.assertRaisesRegex(
                    RuntimeValidationError, "invalid" if expiry == 2**64 else "fresh"
                ):
                    codex_preflight.inspect_file_auth(self.path, now=self.now)

    def test_no_last_refresh_or_auth_source_fallback(self) -> None:
        for change in ("opaque_access", "api_key", "missing_refresh", "mode"):
            with self.subTest(change=change):
                value = self.write_auth()
                value["last_refresh"] = "2030-01-01T00:00:00Z"
                if change == "opaque_access":
                    value["tokens"]["access_token"] = "opaque-fixture-token"
                elif change == "api_key":
                    value["OPENAI_API_KEY"] = "synthetic-api-key"
                elif change == "missing_refresh":
                    del value["tokens"]["refresh_token"]
                else:
                    value["auth_mode"] = "apikey"
                self.path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(RuntimeValidationError) as error:
                    codex_preflight.inspect_file_auth(self.path, now=self.now)
                self.assertNotIn("synthetic-", str(error.exception))

    def test_expiry_parser_rejects_payloads_codex_cannot_use_for_freshness(
        self,
    ) -> None:
        duplicate = (
            base64.urlsafe_b64encode(b'{"exp":0,"exp":1800003600}').decode().rstrip("=")
        )
        for token in (
            "e30." + duplicate + ".signature",
            self.token({"exp": self.now + 3_600}).replace(".fixture", "=.fixture"),
            self.token({"exp": self.now + 3_600})[3:],
        ):
            with self.subTest(token_kind=token[:3]):
                value = self.write_auth()
                value["tokens"]["access_token"] = token
                self.path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(RuntimeValidationError):
                    codex_preflight.inspect_file_auth(self.path, now=self.now)

    def test_invalid_unused_claims_cannot_trigger_codex_refresh_fallback(self) -> None:
        for extra in (float("nan"), float("inf"), -float("inf"), "\ud800", 2**65):
            with self.subTest(extra_type=type(extra).__name__):
                value = self.write_auth()
                value["tokens"]["access_token"] = self.token(
                    {"exp": self.now + 3_600, "extra": extra}
                )
                self.path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(RuntimeValidationError):
                    codex_preflight.inspect_file_auth(self.path, now=self.now)

    def test_expiry_payload_must_be_utf8_without_bom(self) -> None:
        for encoding in ("utf-16", "utf-32", "utf-8-sig"):
            with self.subTest(encoding=encoding):
                value = self.write_auth()
                payload = json.dumps({"exp": self.now + 3_600}).encode(encoding)
                encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
                value["tokens"]["access_token"] = "e30." + encoded + ".signature"
                self.path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(RuntimeValidationError):
                    codex_preflight.inspect_file_auth(self.path, now=self.now)

    def test_symlink_hardlink_nonprivate_and_nonregular_file_are_rejected(self) -> None:
        self.write_auth()
        link = self.path.with_name("link")
        link.symlink_to(self.path)
        with self.assertRaises(RuntimeValidationError):
            codex_preflight.inspect_file_auth(link, now=self.now)
        link.unlink()
        os.link(self.path, link)
        with self.assertRaises(RuntimeValidationError):
            codex_preflight.inspect_file_auth(self.path, now=self.now)
        link.unlink()
        self.path.chmod(0o644)
        with self.assertRaises(RuntimeValidationError):
            codex_preflight.inspect_file_auth(self.path, now=self.now)
        self.path.unlink()
        os.mkfifo(self.path, 0o600)
        with self.assertRaises(RuntimeValidationError):
            codex_preflight.inspect_file_auth(self.path, now=self.now)


class CodexProjectConfigurationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(
            prefix="agent-team-project-config-"
        )
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.repo = self.root / "repo"
        self.cwd = self.repo / "nested"
        self.cwd.mkdir(parents=True)
        (self.repo / ".git").mkdir()
        (self.repo / ".git" / "HEAD").write_text("ref: refs/heads/test\n")
        (self.repo / ".codex").mkdir()
        self.config = self.repo / ".codex" / "config.toml"

    def test_snapshot_includes_ancestor_config_and_absent_workspace_config(
        self,
    ) -> None:
        self.config.write_text('[mcp_servers.fixture]\ncommand = "never-run"\n')
        snapshot = codex_preflight.project_configuration_snapshot(self.cwd)
        self.assertEqual(
            snapshot,
            {
                str(self.config): hashlib.sha256(self.config.read_bytes()).hexdigest(),
                str(self.cwd / ".codex" / "config.toml"): None,
            },
        )
        self.config.write_text('model = "new-model"\n')
        self.assertNotEqual(
            snapshot, codex_preflight.project_configuration_snapshot(self.cwd)
        )

    def test_unsupported_fields_and_invalid_toml_fail_without_value_disclosure(
        self,
    ) -> None:
        for value in (
            'model_instructions_file = "/synthetic-secret"',
            'developer_instructions = "synthetic-secret"',
            'model_catalog_json = "/synthetic-secret"',
            "[features]\nfuture_network_feature = true",
            'future_field = "synthetic-secret"',
            "invalid = [",
        ):
            with self.subTest(kind=value.split("=")[0]):
                self.config.write_text(value)
                with self.assertRaises(RuntimeValidationError) as error:
                    codex_preflight.project_configuration_snapshot(self.cwd)
                self.assertNotIn("synthetic-secret", str(error.exception))

    def test_unmarked_parent_is_not_a_project_layer(self) -> None:
        (self.repo / ".git" / "HEAD").unlink()
        self.config.write_text('model_instructions_file = "/must-not-read"')
        self.assertEqual(
            codex_preflight.project_configuration_snapshot(self.cwd),
            {
                str(self.cwd / ".codex" / "config.toml"): None,
            },
        )

    def test_symlinked_project_config_directory_is_rejected(self) -> None:
        (self.cwd / ".codex").symlink_to(self.repo / ".codex", target_is_directory=True)
        with self.assertRaises(RuntimeValidationError):
            codex_preflight.project_configuration_snapshot(self.cwd)

    def test_normal_auth_selector_allows_managed_file_link_but_not_other_stores(
        self,
    ) -> None:
        normal = self.root / "normal-codex"
        normal.mkdir()
        self.assertEqual(codex_preflight.file_auth_path(normal), normal / "auth.json")
        source = self.root / "managed-config.toml"
        source.write_text('cli_auth_credentials_store="file"\n')
        config = normal / "config.toml"
        config.symlink_to(source)
        self.assertEqual(codex_preflight.file_auth_path(normal), normal / "auth.json")
        source.write_text('cli_auth_credentials_store="keyring"\n')
        with self.assertRaisesRegex(RuntimeValidationError, "file-backed"):
            codex_preflight.file_auth_path(normal)
        source.unlink()
        with self.assertRaises(RuntimeValidationError):
            codex_preflight.file_auth_path(normal)
        config.unlink()
        os.mkfifo(config, 0o600)
        with self.assertRaises(RuntimeValidationError):
            codex_preflight.file_auth_path(normal)


class CodexManagedPrelaunchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(
            prefix="agent-team-codex-preflight-"
        )
        self.root = Path(self.directory.name)
        self.paths = (
            self.root / "config.toml",
            self.root / "requirements.toml",
            self.root / "managed_config.toml",
        )
        self.paths_patch = mock.patch.object(
            codex_preflight, "_SYSTEM_PATHS", self.paths
        )
        self.paths_patch.start()
        self.addCleanup(self.paths_patch.stop)
        self.addCleanup(self.directory.cleanup)

    def test_absent_linux_managed_sources_do_not_query_macos(self) -> None:
        with (
            mock.patch.object(codex_preflight.sys, "platform", "linux"),
            mock.patch.object(codex_preflight, "_macos_managed_preferences") as query,
        ):
            codex_preflight.assert_no_system_configuration()
        query.assert_not_called()

    def test_any_managed_file_or_broken_symlink_is_rejected_without_reading(
        self,
    ) -> None:
        for path in self.paths:
            with self.subTest(path=path.name):
                path.write_text("invalid TOML is still managed", encoding="utf-8")
                with (
                    mock.patch.object(
                        Path, "read_text", side_effect=AssertionError("must not read")
                    ),
                    self.assertRaisesRegex(
                        RuntimeValidationError, "managed configuration"
                    ),
                ):
                    codex_preflight.assert_no_system_configuration()
                path.unlink()
        self.paths[0].symlink_to(self.root / "missing")
        with self.assertRaisesRegex(RuntimeValidationError, "managed configuration"):
            codex_preflight.assert_no_system_configuration()

    def test_unreadable_source_does_not_count_as_absent(self) -> None:
        with (
            mock.patch.object(Path, "lstat", side_effect=PermissionError("denied")),
            self.assertRaisesRegex(RuntimeValidationError, "cannot establish"),
        ):
            codex_preflight.assert_no_system_configuration()

    def test_macos_queries_both_managed_keys_and_rejects_either(self) -> None:
        for keys in ((), ("config_toml_base64",), ("requirements_toml_base64",)):
            with (
                self.subTest(keys=keys),
                mock.patch.object(codex_preflight.sys, "platform", "darwin"),
                mock.patch.object(
                    codex_preflight, "_macos_managed_preferences", return_value=keys
                ) as query,
            ):
                if keys:
                    with self.assertRaisesRegex(
                        RuntimeValidationError, "managed preferences"
                    ):
                        codex_preflight.assert_no_system_configuration()
                else:
                    codex_preflight.assert_no_system_configuration()
                query.assert_called_once_with()

    def test_macos_query_failure_and_unsupported_platform_fail_closed(self) -> None:
        with (
            mock.patch.object(codex_preflight.sys, "platform", "darwin"),
            mock.patch.object(
                codex_preflight,
                "_macos_managed_preferences",
                side_effect=OSError("unavailable"),
            ),
            self.assertRaisesRegex(RuntimeValidationError, "cannot establish"),
        ):
            codex_preflight.assert_no_system_configuration()
        with (
            mock.patch.object(codex_preflight.sys, "platform", "unverified"),
            self.assertRaisesRegex(RuntimeValidationError, "macOS or Linux"),
        ):
            codex_preflight.assert_no_system_configuration()


if __name__ == "__main__":
    unittest.main()
