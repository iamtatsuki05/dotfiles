from __future__ import annotations

import json
import os
import shlex
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from copilot_prefix import CopilotPrefix, make_copilot_prefix, pinned_fake_copilot

from agent_team import copilot_acp
from agent_team.native_acp_dependencies import CopilotAcpExecutables
from agent_team.runtime import RuntimeValidationError
from agent_team.scoped_acp import SCOPED_CLIENT, SCOPED_POLICY, checked_digest
from agent_team.task_spec import TaskSpec, VerificationSpec

COMMON_FLAGS = [
    "--no-auto-update",
    "--no-custom-instructions",
    "--disable-builtin-mcps",
    "--no-remote",
    "--no-remote-export",
    "--disallow-temp-dir",
    "--no-ask-user",
]
FORBIDDEN_FLAGS = (
    "--allow-tool",
    "--allow-all",
    "--allow-all-tools",
    "--allow-all-paths",
    "--allow-all-urls",
    "--yolo",
    "--mode",
    "--add-dir",
)


class CopilotAcpTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="agent-team-copilot-acp-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.workspace = self.root / "workspace"
        (self.workspace / "src").mkdir(parents=True)
        (self.workspace / ".git").mkdir()
        self.state_path = self.root / "state" / "state.json"
        self.state_path.parent.mkdir()
        self.home = self.root / "home"
        (self.home / ".copilot").mkdir(parents=True)
        (self.home / ".config" / "gh").mkdir(parents=True)
        self.prefix: CopilotPrefix = make_copilot_prefix(self.root / "deps")
        environment = {
            "HOME": str(self.home),
            "PATH": "/opt/agent-team/bin:/usr/bin",
            "USER": "fixture-user",
            "LOGNAME": "fixture-user",
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
            "SHELL": "/bin/zsh",
            "TERM": "xterm-256color",
            "TMPDIR": "/var/tmp/outer",
            "GH_TOKEN": "gho_fixture",
            "GITHUB_TOKEN": "ghp_fixture",
            "COPILOT_GITHUB_TOKEN": "github_pat_fixture",
            "COPILOT_MODEL": "byok-model",
            "COPILOT_PROVIDER_BASE_URL": "https://provider.invalid",
            "HTTPS_PROXY": "http://proxy.invalid:8080",
            "https_proxy": "http://proxy.invalid:8080",
            "NO_PROXY": "*",
            "NODE_OPTIONS": "--require /tmp/inject.js",
            "XDG_CONFIG_HOME": str(self.root / "xdg"),
        }
        self.enterContext(mock.patch.dict(os.environ, environment, clear=True))
        self.enterContext(pinned_fake_copilot(self.prefix.binary))
        self.executables = CopilotAcpExecutables.resolve(path=self.prefix.path)

    def _task(self) -> TaskSpec:
        return TaskSpec(
            "copilot-task",
            "Update one fixture",
            ("The fixture remains valid",),
            ("src/allowed.txt",),
            ("src/forbidden.txt",),
            (),
            (VerificationSpec("check", ("python3", "-V"), 10),),
            ("check output",),
            (),
        )

    def _private_root(self, name: str = "private") -> Path:
        root = self.root / name
        root.mkdir(mode=0o700)
        root.chmod(0o700)
        return root

    def _prepare(
        self,
        root: Path,
        *,
        permission: str = "workspace-write",
        task: TaskSpec | None = None,
    ) -> dict[str, object]:
        return copilot_acp.prepare_assignment(
            root,
            self.workspace,
            self.state_path,
            self._task() if task is None and permission == "workspace-write" else task,
            permission,
            self.executables,
            "gpt-5.2",
            "high",
        )

    def _spec(self, permission: str = "workspace-write") -> dict[str, object]:
        return {
            "provider": "copilot",
            "transport": "acp",
            "permission": permission,
            "execution": "background",
            "adapter_id": copilot_acp.ADAPTER_ID,
            "model": "gpt-5.2",
            "effort": "high",
            "instructions": "fixture instructions",
            "acp_executables": self.executables.as_dict(),
            "scoped_client_sha256": checked_digest(SCOPED_CLIENT),
            "scoped_policy_sha256": checked_digest(SCOPED_POLICY),
        }

    def _assignment(
        self, fields: dict[str, object], root: Path, permission: str = "workspace-write"
    ) -> dict[str, object]:
        return {
            **fields,
            "adapter_id": copilot_acp.ADAPTER_ID,
            "agent_command": copilot_acp.agent_command(
                self.executables, permission=permission, model="gpt-5.2", effort="high"
            ),
            "provider_private_root": str(root),
            **(
                {"task_spec": self._task().as_dict()}
                if permission == "workspace-write"
                else {}
            ),
        }

    def _state(self) -> dict[str, object]:
        return {"workspace": str(self.workspace), "state_path": str(self.state_path)}

    def test_argv_is_fixed_for_each_permission(self) -> None:
        binary = str(self.prefix.binary)
        self.assertEqual(
            copilot_acp.agent_argv(
                self.executables, permission="read-only", model="gpt-5.2", effort="low"
            ),
            [
                binary,
                "--acp",
                "--stdio",
                "--model",
                "gpt-5.2",
                "--effort",
                "low",
                *COMMON_FLAGS,
                "--available-tools",
                "view,grep,glob",
                "--deny-tool",
                "shell",
                "--deny-tool",
                "write",
                "--deny-tool",
                "url",
            ],
        )
        worker = copilot_acp.agent_argv(
            self.executables,
            permission="workspace-write",
            model="claude-sonnet-4.5",
            effort="max",
        )
        self.assertEqual(
            worker,
            [
                binary,
                "--acp",
                "--stdio",
                "--model",
                "claude-sonnet-4.5",
                "--effort",
                "max",
                *COMMON_FLAGS,
                "--available-tools",
                "view,grep,glob,edit,create",
                "--deny-tool",
                "shell",
                "--deny-tool",
                "url",
            ],
        )
        for permission in ("read-only", "workspace-write"):
            argv = copilot_acp.agent_argv(
                self.executables, permission=permission, model="gpt-5.2", effort="high"
            )
            self.assertEqual(
                shlex.split(
                    copilot_acp.agent_command(
                        self.executables,
                        permission=permission,
                        model="gpt-5.2",
                        effort="high",
                    )
                ),
                argv,
            )
            for flag in FORBIDDEN_FLAGS:
                self.assertFalse(
                    any(item == flag or item.startswith(f"{flag}=") for item in argv),
                    flag,
                )

    def test_model_effort_and_permission_are_validated(self) -> None:
        for model in ("gpt-5.2", "claude-sonnet-4.5", "org/model:tag", "a" * 128):
            with self.subTest(model=model):
                copilot_acp.agent_argv(
                    self.executables, permission="read-only", model=model, effort="high"
                )
        for model in (
            "auto",
            "",
            "-x",
            "--yolo",
            ".hidden",
            "a b",
            "a\nb",
            "a;b",
            "modèle",
            "a" * 129,
        ):
            with (
                self.subTest(model=model),
                self.assertRaisesRegex(RuntimeValidationError, "model is invalid"),
            ):
                copilot_acp.agent_argv(
                    self.executables, permission="read-only", model=model, effort="high"
                )
        for effort in copilot_acp.EFFORTS:
            copilot_acp.agent_argv(
                self.executables, permission="read-only", model="gpt-5.2", effort=effort
            )
        for effort in ("none", "minimal", "HIGH", "", "--yolo"):
            with (
                self.subTest(effort=effort),
                self.assertRaisesRegex(RuntimeValidationError, "effort is invalid"),
            ):
                copilot_acp.agent_argv(
                    self.executables,
                    permission="read-only",
                    model="gpt-5.2",
                    effort=effort,
                )
        for permission in ("orchestrator", "bypass", ""):
            with (
                self.subTest(permission=permission),
                self.assertRaisesRegex(RuntimeValidationError, "permission is invalid"),
            ):
                copilot_acp.agent_argv(
                    self.executables,
                    permission=permission,
                    model="gpt-5.2",
                    effort="high",
                )

    def test_environment_is_an_allowlist(self) -> None:
        root = self._private_root()
        self.assertEqual(
            copilot_acp.environment(root),
            {
                "HOME": str(self.home),
                "PATH": "/usr/bin:/bin",
                "TMPDIR": str(root / "tmp"),
                "COPILOT_HOME": str(root / "copilot-home"),
                "USER": "fixture-user",
                "LOGNAME": "fixture-user",
                "LANG": "en_US.UTF-8",
                "LC_ALL": "en_US.UTF-8",
            },
        )

    def test_prepare_creates_only_the_private_home_and_policy(self) -> None:
        root = self._private_root()
        fields = self._prepare(root)

        policy_path = root / "write-policy.json"
        self.assertEqual(
            fields,
            {
                "write_policy_path": str(policy_path),
                "write_policy_sha256": checked_digest(policy_path, private=True),
            },
        )
        self.assertEqual(
            sorted(path.name for path in root.iterdir()),
            ["copilot-home", "tmp", "write-policy.json"],
        )
        for directory in (root / "copilot-home", root / "tmp"):
            self.assertEqual(stat.S_IMODE(directory.lstat().st_mode), 0o700)
        self.assertEqual(list((root / "tmp").iterdir()), [])
        settings_path = root / "copilot-home" / "settings.json"
        self.assertEqual(list((root / "copilot-home").iterdir()), [settings_path])
        self.assertEqual(stat.S_IMODE(settings_path.lstat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(policy_path.lstat().st_mode), 0o600)
        denied = [
            str(root),
            f"{root}/**",
            str(self.workspace / ".git"),
            f"{self.workspace / '.git'}/**",
            str(self.state_path.parent),
            f"{self.state_path.parent}/**",
        ]
        self.assertEqual(
            json.loads(settings_path.read_text(encoding="utf-8")),
            {
                "disableAllHooks": True,
                "remote": "off",
                "remoteExport": False,
                "sandbox": {
                    "enabled": True,
                    "allowBypass": False,
                    "auth": {"git": False, "gh": False},
                    "userPolicy": {
                        "deniedPaths": denied,
                        "network": {
                            "allowOutbound": False,
                            "allowLocalNetwork": False,
                        },
                        "seatbelt": {"keychainAccess": False},
                    },
                },
            },
        )
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        self.assertEqual(policy["permission"], "workspace-write")
        self.assertEqual(policy["workspace"], str(self.workspace))
        self.assertEqual(policy["allowed_paths"], ["src/allowed.txt"])
        self.assertEqual(policy["forbidden_paths"], ["src/forbidden.txt"])
        protected = policy["protected_paths"]
        for path in (
            root,
            self.state_path.parent,
            self.prefix.node,
            self.prefix.loader,
            self.prefix.binary,
            self.prefix.sdk,
            self.prefix.package_root,
            self.prefix.platform_root,
            self.prefix.sdk_root,
            self.home / ".copilot",
            self.home / ".config" / "gh",
        ):
            self.assertIn(str(path), protected)
        self.assertNotIn(str(self.home / ".config" / "github-copilot"), protected)
        self.assertEqual(len(protected), len(set(protected)))

    def test_prepare_rejects_invalid_requests_before_any_artifact(self) -> None:
        cases = (
            ("model", {"model": "auto"}),
            ("effort", {"effort": "none"}),
            ("permission", {"permission": "orchestrator"}),
            ("worker-without-task", {"permission": "workspace-write", "task": None}),
        )
        for name, changes in cases:
            with self.subTest(case=name):
                root = self._private_root(f"private-{name}")
                arguments: dict[str, object] = {
                    "private_root": root,
                    "workspace": self.workspace,
                    "state_path": self.state_path,
                    "task": self._task(),
                    "permission": "workspace-write",
                    "executables": self.executables,
                    "model": "gpt-5.2",
                    "effort": "high",
                    **changes,
                }
                with self.assertRaises(RuntimeValidationError):
                    copilot_acp.prepare_assignment(**arguments)  # type: ignore[arg-type]
                self.assertEqual(list(root.iterdir()), [])

        occupied = self._private_root("private-occupied")
        (occupied / "leftover").write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeValidationError, "new 0700 directory"):
            self._prepare(occupied)

    def test_validate_accepts_the_prepared_assignment(self) -> None:
        for permission in ("read-only", "workspace-write"):
            with self.subTest(permission=permission):
                root = self._private_root(f"private-{permission}")
                fields = self._prepare(root, permission=permission)
                copilot_acp.validate_assignment(
                    self._state(),
                    self._assignment(fields, root, permission),
                    self._spec(permission),
                )

    def test_validate_rejects_artifact_and_binding_drift(self) -> None:
        def extra_root_entry(root: Path, _assignment: dict[str, object]) -> None:
            (root / "client-result.pending").write_text("x", encoding="utf-8")

        def extra_home_entry(root: Path, _assignment: dict[str, object]) -> None:
            (root / "copilot-home" / "config.json").write_text("{}", encoding="utf-8")

        def temporary_file(root: Path, _assignment: dict[str, object]) -> None:
            (root / "tmp" / "left").write_text("x", encoding="utf-8")

        def settings_bytes(root: Path, _assignment: dict[str, object]) -> None:
            path = root / "copilot-home" / "settings.json"
            path.write_text('{"disableAllHooks":false}', encoding="utf-8")

        def settings_symlink(root: Path, _assignment: dict[str, object]) -> None:
            path = root / "copilot-home" / "settings.json"
            target = root.parent / "outside-settings.json"
            target.write_bytes(path.read_bytes())
            path.unlink()
            path.symlink_to(target)

        def policy_scope(root: Path, assignment: dict[str, object]) -> None:
            path = root / "write-policy.json"
            policy = json.loads(path.read_text(encoding="utf-8"))
            policy["allowed_paths"] = ["src/"]
            path.write_text(json.dumps(policy), encoding="utf-8")
            assignment["write_policy_sha256"] = checked_digest(path, private=True)

        def policy_digest(_root: Path, assignment: dict[str, object]) -> None:
            assignment["write_policy_sha256"] = "0" * 64

        def agent_command(_root: Path, assignment: dict[str, object]) -> None:
            assignment["agent_command"] = str(assignment["agent_command"]).replace(
                "--deny-tool shell", "--allow-tool shell"
            )

        def question_socket(root: Path, assignment: dict[str, object]) -> None:
            assignment["question_socket"] = str(root / "q.sock")

        def adapter(_root: Path, assignment: dict[str, object]) -> None:
            assignment["adapter_id"] = "claude-acp-scoped-0.70.0"

        def node_drift(_root: Path, _assignment: dict[str, object]) -> None:
            with self.prefix.node.open("a", encoding="utf-8") as handle:
                handle.write("# drift\n")

        cases = (
            ("root-entry", extra_root_entry, "unexpected artifacts"),
            ("home-entry", extra_home_entry, "private home"),
            ("tmp", temporary_file, "not empty"),
            ("settings", settings_bytes, "settings changed"),
            ("settings-symlink", settings_symlink, "unsafe"),
            ("policy-scope", policy_scope, "does not match the saved TaskSpec"),
            ("policy-digest", policy_digest, "write policy changed"),
            ("agent-command", agent_command, "agent command changed"),
            ("question-socket", question_socket, "question socket"),
            ("adapter", adapter, "adapter does not match"),
            ("node", node_drift, "executable binding changed"),
        )
        original_node = self.prefix.node.read_bytes()
        for name, change, message in cases:
            with self.subTest(case=name):
                root = self._private_root(f"private-{name}")
                assignment = self._assignment(self._prepare(root), root)
                spec = self._spec()
                change(root, assignment)
                try:
                    with self.assertRaisesRegex(RuntimeValidationError, message):
                        copilot_acp.validate_assignment(self._state(), assignment, spec)
                finally:
                    self.prefix.node.write_bytes(original_node)

    def test_validate_requires_the_saved_runtime_digests(self) -> None:
        root = self._private_root()
        assignment = self._assignment(self._prepare(root), root)
        for key, message in (
            ("scoped_client_sha256", "client changed"),
            ("scoped_policy_sha256", "shared policy changed"),
        ):
            with self.subTest(key=key):
                spec = self._spec()
                spec[key] = "0" * 64
                with self.assertRaisesRegex(RuntimeValidationError, message):
                    copilot_acp.validate_assignment(self._state(), assignment, spec)


if __name__ == "__main__":
    unittest.main()
