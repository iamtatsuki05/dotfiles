from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_team.adapters import (
    AdapterContext,
    AdapterError,
    AdapterSnapshot,
    CopilotReadOnlyAdapter,
    ExecutionError,
    FileIdentity,
    OpenCodeReadOnlyAdapter,
    ProcessRunner,
    ProcessResult,
    SnapshotError,
    _extract_opencode_final,
    _exact_version_present,
    _safe_source_path,
    create_read_snapshot,
    remove_owned_tree,
    safe_environment,
)


class AdapterSafetyTest(unittest.TestCase):
    def test_exact_version_accepts_banner_punctuation_not_numeric_prefix(self) -> None:
        self.assertTrue(
            _exact_version_present("GitHub Copilot CLI 1.0.81.", "1.0.81")
        )
        self.assertFalse(
            _exact_version_present("GitHub Copilot CLI 1.0.810", "1.0.81")
        )

    def test_safe_environment_has_no_ambient_agent_credentials_or_overrides(
        self,
    ) -> None:
        environment = safe_environment(
            "copilot",
            home=Path("/private/home"),
            private_root=Path("/private/team"),
            source={
                "PATH": "/bin",
                "HOME": "/real/home",
                "GITHUB_TOKEN": "must-not-pass",
                "GH_TOKEN": "must-not-pass",
                "OPENAI_API_KEY": "must-not-pass",
                "HTTP_PROXY": "must-not-pass",
                "ORCA_SOCKET": "must-not-pass",
                "NODE_OPTIONS": "must-not-pass",
            },
        )
        self.assertEqual(environment["HOME"], "/private/home")
        self.assertEqual(environment["COPILOT_HOME"], "/private/team/copilot-home")
        for key in (
            "GITHUB_TOKEN",
            "GH_TOKEN",
            "OPENAI_API_KEY",
            "HTTP_PROXY",
            "ORCA_SOCKET",
            "NODE_OPTIONS",
        ):
            self.assertNotIn(key, environment)

    def test_copilot_preflight_rejects_aws_collision_without_github_marker(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            executable = Path(temp_dir) / "copilot"
            executable.write_text(
                "#!/bin/sh\necho 'copilot version: v1.34.1'\n", encoding="utf-8"
            )
            executable.chmod(0o755)
            context = AdapterContext(
                "copilot",
                "planner",
                "gpt-test",
                "high",
                Path(temp_dir),
                Path(temp_dir) / "private",
            )
            context.private_root.mkdir()
            with (
                mock.patch.dict(os.environ, {"PATH": temp_dir}, clear=False),
                mock.patch(
                    "agent_team.adapters.shutil.which",
                    side_effect=lambda name: (
                        str(executable) if name == "copilot" else None
                    ),
                ),
                self.assertRaisesRegex(Exception, "exact 1.0.81"),
            ):
                CopilotReadOnlyAdapter().preflight(context)

    def test_runner_rejects_executable_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            executable = (
                root
                / "lib/node_modules/@github/copilot/node_modules"
                / "@github/copilot-darwin-arm64/copilot"
            )
            executable.parent.mkdir(parents=True)
            executable.write_text(
                "#!/bin/sh\necho 'GitHub Copilot CLI 1.0.81'\n", encoding="utf-8"
            )
            executable.chmod(0o755)
            context = AdapterContext(
                "copilot", "planner", "gpt-test", "high", root, root / "private"
            )
            context.private_root.mkdir()
            adapter = CopilotReadOnlyAdapter()
            with (
                mock.patch("agent_team.adapters._resolve_mise", return_value=root),
                mock.patch("agent_team.adapters.sys.platform", "darwin"),
                mock.patch("agent_team.adapters.os.uname") as uname,
            ):
                uname.return_value.machine = "arm64"
                snapshot = adapter.preflight(context)
            executable.write_text(
                "#!/bin/sh\necho 'GitHub Copilot CLI 1.0.81 changed'\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(Exception, "identity changed"):
                adapter.execute(context, snapshot, "read", ProcessRunner())

    def test_copilot_preflight_rejects_version_prefix_collision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            executable = Path(temp_dir) / "copilot"
            executable.write_text(
                "#!/bin/sh\necho 'GitHub Copilot CLI 1.0.810'\n", encoding="utf-8"
            )
            executable.chmod(0o755)
            context = AdapterContext(
                "copilot",
                "planner",
                "gpt-test",
                "high",
                Path(temp_dir),
                Path(temp_dir) / "private",
            )
            context.private_root.mkdir()
            with (
                mock.patch(
                    "agent_team.adapters.shutil.which",
                    side_effect=lambda name: str(executable) if name == "copilot" else None,
                ),
                self.assertRaisesRegex(Exception, "exact 1.0.81"),
            ):
                CopilotReadOnlyAdapter().preflight(context)

    def test_copilot_preflight_prefers_pinned_native_binary_over_npm_loader(
        self,
    ) -> None:
        for system, machine, package_name in (
            ("darwin", "arm64", "@github/copilot-darwin-arm64"),
            ("linux", "x86_64", "@github/copilot-linux-x64"),
        ):
            with self.subTest(system=system), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                loader = root / "bin/copilot"
                loader.parent.mkdir(parents=True)
                loader.write_text(
                    "#!/bin/sh\necho 'GitHub Copilot CLI 1.0.81.'\n",
                    encoding="utf-8",
                )
                loader.chmod(0o755)
                native = (
                    root
                    / "lib/node_modules/@github/copilot/node_modules"
                    / package_name
                    / "copilot"
                )
                native.parent.mkdir(parents=True)
                native.write_text(
                    "#!/bin/sh\necho 'GitHub Copilot CLI 1.0.81.'\n",
                    encoding="utf-8",
                )
                native.chmod(0o755)
                private = Path(tempfile.mkdtemp(prefix="agent-team-provider-"))
                try:
                    context = AdapterContext(
                        "copilot",
                        "planner",
                        "auto",
                        "none",
                        root,
                        private,
                    )
                    with (
                        mock.patch(
                            "agent_team.adapters.shutil.which",
                            side_effect=lambda name: (
                                str(loader) if name == "copilot" else None
                            ),
                        ),
                        mock.patch(
                            "agent_team.adapters._resolve_mise",
                            return_value=root,
                        ),
                        mock.patch("agent_team.adapters.sys.platform", system),
                        mock.patch("agent_team.adapters.os.uname") as uname,
                    ):
                        uname.return_value.machine = machine
                        snapshot = CopilotReadOnlyAdapter().preflight(context)
                    self.assertEqual(snapshot.executable, native.resolve())
                finally:
                    remove_owned_tree(private)

    def test_copilot_preflight_rejects_loader_on_unsupported_platform(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            loader = root / "bin/copilot"
            loader.parent.mkdir(parents=True)
            loader.write_text(
                "#!/bin/sh\necho 'GitHub Copilot CLI 1.0.81.'\n",
                encoding="utf-8",
            )
            loader.chmod(0o755)
            private = Path(tempfile.mkdtemp(prefix="agent-team-provider-"))
            try:
                context = AdapterContext(
                    "copilot", "planner", "auto", "none", root, private
                )
                with (
                    mock.patch(
                        "agent_team.adapters.shutil.which",
                        side_effect=lambda name: (
                            str(loader) if name == "copilot" else None
                        ),
                    ),
                    mock.patch("agent_team.adapters._resolve_mise", return_value=root),
                    mock.patch("agent_team.adapters.sys.platform", "win32"),
                    mock.patch(
                        "agent_team.adapters.os.uname",
                        side_effect=AssertionError("unsupported platform must not call uname"),
                    ),
                    self.assertRaisesRegex(Exception, "exact 1.0.81"),
                ):
                    CopilotReadOnlyAdapter().preflight(context)
            finally:
                remove_owned_tree(private)

    def test_copilot_settings_use_the_official_sandbox_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            private = root / "private"
            private.mkdir()
            workspace = root / "snapshot"
            workspace.mkdir()
            context = AdapterContext(
                "copilot", "planner", "gpt-test", "high", workspace, private
            )

            CopilotReadOnlyAdapter()._write_settings(context)
            settings = json.loads(
                (private / "copilot-home" / "settings.json").read_text()
            )

            self.assertEqual(settings["disableAllHooks"], True)
            self.assertEqual(settings["remote"], "off")
            self.assertEqual(settings["remoteExport"], False)
            self.assertEqual(settings["sandbox"]["enabled"], True)
            self.assertEqual(settings["sandbox"]["allowBypass"], False)
            self.assertEqual(
                settings["sandbox"]["auth"], {"git": False, "gh": False}
            )
            self.assertEqual(
                settings["sandbox"]["userPolicy"]["deniedPaths"],
                [
                    denied
                    for path in (private, workspace / ".git")
                    for denied in (str(path), str(path / "**"))
                ],
            )
            self.assertEqual(
                settings["sandbox"]["userPolicy"]["network"],
                {"allowOutbound": False, "allowLocalNetwork": False},
            )
            self.assertEqual(
                settings["sandbox"]["userPolicy"]["seatbelt"],
                {"keychainAccess": False},
            )
            for incorrect_key in ("hooks", "auth", "network", "deniedPaths"):
                self.assertNotIn(incorrect_key, settings)

    def test_copilot_failure_reports_bounded_stderr_detail(self) -> None:
        context = AdapterContext(
            "copilot",
            "planner",
            "auto",
            "none",
            Path("/private/snapshot"),
            Path("/private/provider"),
        )
        snapshot = AdapterSnapshot(
            "github-copilot-direct-readonly-1.0.81",
            "revision",
            Path("/private/copilot"),
            "1.0.81",
            FileIdentity(1, 2, 3, 4, "hash"),
        )
        runner = mock.Mock()
        runner.run.return_value = ProcessResult(
            1,
            "",
            "specific provider failure",
        )
        with (
            mock.patch("agent_team.adapters._validate_snapshot"),
            mock.patch.object(CopilotReadOnlyAdapter, "_write_settings"),
            self.assertRaisesRegex(ExecutionError, "specific provider failure"),
        ):
            CopilotReadOnlyAdapter().execute(
                context,
                snapshot,
                "prompt",
                runner,
            )

    def test_opencode_config_limits_read_tools_to_snapshot_and_disables_extensions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            private = root / "private"
            private.mkdir()
            workspace = root / "snapshot"
            workspace.mkdir()
            context = AdapterContext(
                "opencode", "reviewer", "openai/gpt-test", "high", workspace, private
            )
            adapter = OpenCodeReadOnlyAdapter()
            adapter._write_config(context)
            config = json.loads(
                (private / "xdg_config_home" / "opencode" / "opencode.json").read_text()
            )
            self.assertEqual(config["instructions"], [])
            self.assertEqual(config["plugin"], [])
            self.assertEqual(config["mcp"], {})
            self.assertEqual(config["permission"]["edit"], "deny")
            self.assertEqual(config["permission"]["read"]["*"], "deny")
            self.assertEqual(
                config["permission"]["read"][str(workspace / "**")], "allow"
            )

    def test_opencode_final_output_requires_json_events(self) -> None:
        self.assertEqual(
            _extract_opencode_final('{"type":"text","part":{"text":"done"}}\n'),
            "done",
        )
        with self.assertRaises(ExecutionError):
            _extract_opencode_final("plain output\n")

    def test_process_runner_bounds_output_and_does_not_use_shell(self) -> None:
        runner = ProcessRunner(max_output_bytes=64)
        with tempfile.TemporaryDirectory() as temp_dir:
            script = Path(temp_dir) / "emit.py"
            script.write_text("import sys; print('x' * 1000)", encoding="utf-8")
            with self.assertRaises(ExecutionError):
                runner.run(
                    (sys.executable, str(script)),
                    cwd=Path(temp_dir),
                    env={"PATH": os.environ["PATH"]},
                )

    def test_process_runner_terminates_process_group_on_timeout(self) -> None:
        runner = ProcessRunner(max_output_bytes=1024)
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = Path(temp_dir) / "child-alive"
            script = Path(temp_dir) / "spawn.py"
            script.write_text(
                "import subprocess,sys,time; subprocess.Popen([sys.executable, '-c', "
                '\'import time; time.sleep(5); open(sys.argv[1], \\"w\\").write(\\"alive\\")\', sys.argv[1]]); time.sleep(10)',
                encoding="utf-8",
            )
            with self.assertRaises(ExecutionError):
                runner.run(
                    (sys.executable, str(script), str(marker)),
                    cwd=Path(temp_dir),
                    env={"PATH": os.environ["PATH"]},
                    timeout_seconds=0.1,
                )
            time.sleep(0.2)
            self.assertFalse(marker.exists())

    def test_snapshot_excludes_secrets_ignored_metadata_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "repo"
            root.mkdir()
            (root / ".git").mkdir()
            (root / "src").mkdir()
            (root / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")
            (root / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
            (root / "credentials.json").write_text("secret", encoding="utf-8")
            (root / "AGENTS.md").write_text("instructions", encoding="utf-8")
            (root / ".agent").mkdir()
            (root / ".agent" / "state.json").write_text("state", encoding="utf-8")
            outside = Path(temp_dir) / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            (root / "outside-link").symlink_to(outside)
            (root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
            (root / "ignored.txt").write_text("ignored", encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(
                ["git", "-C", str(root), "add", "src/main.py", ".gitignore"], check=True
            )
            snapshot = create_read_snapshot(root)
            try:
                self.assertEqual(
                    (snapshot.root / "src/main.py").read_text(), "print('ok')\n"
                )
                self.assertFalse((snapshot.root / ".env").exists())
                self.assertFalse((snapshot.root / "credentials.json").exists())
                self.assertFalse((snapshot.root / "AGENTS.md").exists())
                self.assertFalse((snapshot.root / "outside-link").exists())
                self.assertFalse((snapshot.root / "ignored.txt").exists())
                self.assertNotIn(".git", str(snapshot.root.rglob("*")))
                self.assertTrue(
                    all(
                        stat.S_IMODE(p.stat().st_mode) == 0o444
                        for p in snapshot.root.rglob("*")
                        if p.is_file()
                    )
                )
            finally:
                snapshot.cleanup()
            self.assertFalse(snapshot.root.exists())

    def test_snapshot_hardlink_is_copied_as_independent_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "repo"
            root.mkdir()
            (root / ".git").mkdir()
            source = root / "a.txt"
            source.write_text("a", encoding="utf-8")
            os.link(source, root / "b.txt")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(
                ["git", "-C", str(root), "add", "a.txt", "b.txt"], check=True
            )
            snapshot = create_read_snapshot(root)
            try:
                self.assertNotEqual(
                    (snapshot.root / "a.txt").stat().st_ino,
                    (snapshot.root / "b.txt").stat().st_ino,
                )
            finally:
                snapshot.cleanup()

    def test_snapshot_rejects_parent_symlink_swap_before_file_open(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "repo"
            root.mkdir()
            source_dir = root / "src"
            source_dir.mkdir()
            (source_dir / "main.py").write_text("safe\n", encoding="utf-8")
            outside = Path(temp_dir) / "outside"
            outside.mkdir()
            (outside / "main.py").write_text("outside-secret\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(
                ["git", "-C", str(root), "add", "src/main.py"], check=True
            )
            swapped = False

            def swap_parent(workspace: Path, relative: str) -> Path:
                nonlocal swapped
                candidate = _safe_source_path(workspace, relative)
                if relative == "src/main.py" and not swapped:
                    swapped = True
                    moved = root / "src-original"
                    source_dir.rename(moved)
                    source_dir.symlink_to(outside, target_is_directory=True)
                return candidate

            with (
                mock.patch(
                    "agent_team.adapters._safe_source_path",
                    side_effect=swap_parent,
                ),
                self.assertRaises(SnapshotError),
            ):
                create_read_snapshot(root)

    def test_snapshot_rejects_state_root_overlap_and_cleans_partial_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "repo"
            root.mkdir()
            (root / ".git").mkdir()
            (root / "file.txt").write_text("x", encoding="utf-8")
            with self.assertRaises(SnapshotError):
                create_read_snapshot(root, state_root=root / "state")
            self.assertEqual(list(Path(temp_dir).glob("agent-team-snapshot-*")), [])

    def test_snapshot_excludes_symlinked_parent_without_following_outside(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "repo"
            root.mkdir()
            (root / ".git").mkdir()
            outside = Path(temp_dir) / "outside"
            outside.mkdir()
            (outside / "secret.txt").write_text("secret", encoding="utf-8")
            (root / "linked").symlink_to(outside, target_is_directory=True)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "add", "linked"], check=True)
            snapshot = create_read_snapshot(root)
            try:
                self.assertFalse((snapshot.root / "linked").exists())
            finally:
                snapshot.cleanup()

    def test_copilot_and_opencode_argv_are_fixed_and_prompt_is_data(self) -> None:
        context = AdapterContext(
            provider="copilot",
            role="planner",
            model="gpt-test",
            effort="high",
            workspace=Path("/private/snapshot"),
            private_root=Path("/private/private"),
        )
        copilot = CopilotReadOnlyAdapter()
        copilot_argv = copilot.build_argv(context, "$(touch PWNED); read this")
        self.assertIn("--no-auto-update", copilot_argv)
        self.assertIn("--mode", copilot_argv)
        self.assertIn("plan", copilot_argv)
        self.assertNotIn("--allow-all-tools", copilot_argv)
        self.assertIn("--available-tools", copilot_argv)
        self.assertEqual(
            copilot_argv[copilot_argv.index("--available-tools") + 1],
            "view,grep,glob",
        )
        copilot_prompt = copilot_argv[copilot_argv.index("-p") + 1]
        self.assertIn("$(touch PWNED); read this", copilot_prompt)
        self.assertIn("private read snapshot", copilot_prompt)
        self.assertIn("relative to the current working directory", copilot_prompt)
        self.assertNotIn("shell=True", copilot_argv)

        opencode = OpenCodeReadOnlyAdapter()
        opencode_argv = opencode.build_argv(
            AdapterContext(
                provider="opencode",
                role="reviewer",
                model="openai/gpt-test",
                effort="high",
                workspace=Path("/private/snapshot"),
                private_root=Path("/private/private"),
            ),
            "review $(touch PWNED)",
        )
        self.assertIn("--pure", opencode_argv)
        self.assertIn("review $(touch PWNED)", opencode_argv)

    def test_cleanup_rejects_non_agent_team_temp_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ordinary-directory-") as temp_dir:
            root = Path(temp_dir)
            sentinel = root / "keep.txt"
            sentinel.write_text("keep", encoding="utf-8")
            with self.assertRaises(SnapshotError):
                remove_owned_tree(root)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_copilot_auto_model_omits_effort_and_rejects_non_none_effort(self) -> None:
        adapter = CopilotReadOnlyAdapter()
        auto_context = AdapterContext(
            "copilot",
            "planner",
            "auto",
            "none",
            Path("/private/snapshot"),
            Path("/private/private"),
        )
        auto_argv = adapter.build_argv(auto_context, "plan")
        self.assertNotIn("--effort", auto_argv)
        with self.assertRaisesRegex(AdapterError, "auto"):
            adapter.build_argv(
                AdapterContext(
                    "copilot",
                    "planner",
                    "auto",
                    "high",
                    Path("/private/snapshot"),
                    Path("/private/private"),
                ),
                "plan",
            )


if __name__ == "__main__":
    unittest.main()
