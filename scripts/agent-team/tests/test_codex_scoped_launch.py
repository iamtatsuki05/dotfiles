from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "agent_team" / "codex_scoped_launch.mjs"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Node.js is required for scoped Codex launch")
class CodexScopedLaunchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="agent-team-codex-launch-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.private = self.root / "private"
        self.private.mkdir(mode=0o700)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        for directory in ("home", "codex-home", "tmp"):
            (self.private / directory).mkdir(mode=0o700)
        self.auth = self.root / "auth.json"
        self.auth.write_text("synthetic credentials never parsed or sent")
        self.auth.chmod(0o600)
        (self.private / "codex-home" / "auth.json").symlink_to(self.auth)
        config = self.private / "codex-home" / "config.toml"
        config.write_text('cli_auth_credentials_store = "file"\n')
        config.chmod(0o600)
        self.binary = self.root / "codex"
        self.binary.write_text("#!/bin/sh\nexit 19\n")
        self.binary.chmod(0o700)
        self.policy_path = self.private / "write-policy.json"
        self.policy_path.write_text(
            json.dumps(
                {
                    "workspace": str(self.workspace),
                    "permission": "workspace-write",
                    "allowed_paths": ["answer.txt"],
                    "forbidden_paths": [],
                    "protected_paths": [
                        str(self.private),
                        str(LAUNCHER.parent),
                        str(self.auth),
                    ],
                }
            )
        )
        self.policy_path.chmod(0o600)
        self.value = {
            "version": 1,
            "codex": str(self.binary),
            "codex_sha256": self.digest(self.binary),
            "policy_sha256": self.digest(self.policy_path),
            "config_sha256": self.digest(config),
            "auth_path": str(self.auth),
            "auth_sha256": self.digest(self.auth),
            "auth_expires_at": int(time.time()) + 3_600,
            "model": "gpt-6-astra",
            "effort": "medium",
            "instructions": "日本語で対象だけ変更してください。",
            "config_snapshot": "1" * 64,
        }
        self.manifest = self.private / "codex-launch.json"

    @staticmethod
    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def run_probe(self, *, mutate: str = "", args: list[str] | None = None) -> dict:
        self.manifest.write_text(json.dumps(self.value))
        self.manifest.chmod(0o600)
        digest = self.digest(self.manifest)
        if mutate == "manifest":
            self.manifest.write_text("{}")
        elif mutate == "policy":
            self.policy_path.write_text("{}")
        elif mutate == "auth":
            self.auth.write_text("changed synthetic credentials")
        elif mutate == "config":
            (self.private / "codex-home" / "config.toml").write_text("changed")
        elif mutate == "link":
            link = self.private / "codex-home" / "auth.json"
            link.unlink()
            link.write_text("must not copy credentials")
        assert NODE is not None
        result = subprocess.run(
            [
                NODE,
                "--input-type=module",
                "-e",
                """
            const {prepareLaunch} = await import(process.argv[2]);
            try {
              const value = prepareLaunch(process.argv[3], process.argv[4], JSON.parse(process.argv[5]));
              process.stdout.write(JSON.stringify({ok: true, value}));
            } catch (error) {
              process.stdout.write(JSON.stringify({ok: false, error: error.message}));
            }
            """,
                "launch-probe",
                str(LAUNCHER),
                str(self.manifest),
                digest,
                json.dumps(["app-server"] if args is None else args),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            env={
                "HOME": str(self.root),
                "OPENAI_API_KEY": "must-not-inherit",
                "CODEX_ACCESS_TOKEN": "must-not-inherit",
                "NODE_OPTIONS": "",
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def test_fixed_launch_preserves_cwd_and_excludes_ambient_credentials(self) -> None:
        result = self.run_probe()
        self.assertTrue(result["ok"], result)
        plan = result["value"]
        self.assertEqual(plan["cwd"], str(self.workspace))
        self.assertEqual(plan["command"], str(self.binary))
        self.assertEqual(plan["argv"][-1], "app-server")
        self.assertEqual(plan["env"]["CODEX_HOME"], str(self.private / "codex-home"))
        self.assertNotIn("OPENAI_API_KEY", plan["env"])
        self.assertNotIn("CODEX_ACCESS_TOKEN", plan["env"])
        self.assertNotIn("NODE_OPTIONS", plan["env"])
        self.assertEqual(plan["bridgeOptions"]["model"], "gpt-6-astra")
        self.assertIn("features.plugins=false", plan["argv"])
        self.assertIn('cli_auth_credentials_store="file"', plan["argv"])

    def test_modified_bindings_and_non_symlink_auth_fail_before_spawn(self) -> None:
        for kind in ("manifest", "policy", "auth", "config", "link"):
            with self.subTest(kind=kind):
                self.setUp()
                result = self.run_probe(mutate=kind)
                self.assertFalse(result["ok"], result)
                self.assertNotIn("synthetic credentials", result["error"])

    def test_invalid_invocation_unknown_fields_and_expiring_auth_fail(self) -> None:
        for args in (
            [],
            ["--version"],
            ["app-server", "--config", "features.plugins=true"],
        ):
            with self.subTest(args=args):
                self.assertFalse(self.run_probe(args=args)["ok"])
        self.value["extra"] = True
        self.assertFalse(self.run_probe()["ok"])
        del self.value["extra"]
        self.value["auth_expires_at"] = int(time.time()) + 300
        self.assertFalse(self.run_probe()["ok"])

    def test_inspection_phase_requires_unbound_snapshot_and_cannot_run_a_turn(
        self,
    ) -> None:
        self.assertFalse(self.run_probe(args=["inspect-config"])["ok"])
        self.value["config_snapshot"] = None
        self.assertFalse(self.run_probe(args=["app-server"])["ok"])
        result = self.run_probe(args=["inspect-config"])
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["value"]["inspection"])
        self.assertEqual(result["value"]["argv"][-1], "app-server")


if __name__ == "__main__":
    unittest.main()
