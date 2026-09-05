from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_team import cli
from agent_team.contracts import Role
from agent_team.runtime import write_state


def saved_state(
    root: Path,
    *,
    state_home: Path | None = None,
    team_id: str = "snapshot-team",
) -> tuple[Path, Path, Path]:
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    config_path = root / "config.toml"
    state_root = root / "state" if state_home is None else state_home
    state_path = state_root / "agent-team" / team_id / "state.json"
    role_specs = {
        role: {
            "provider": "claude" if role == "main" else "codex",
            "transport": "direct",
            "model": f"saved-{role}-model",
            "effort": "high",
            "permission": (
                "orchestrator"
                if role == "main"
                else "workspace-write"
                if role == "worker"
                else "read-only"
            ),
            "instructions": f"saved {role} instructions",
            "execution": "tui_direct",
        }
        for role in cli.ALL_ROLES
    }
    state = {
        "version": 3,
        "runtime": "orca",
        "team_id": team_id,
        "workspace": str(workspace),
        "config_path": str(config_path),
        "state_path": str(state_path),
        "launcher_path": "/tmp/agent-team",
        "worktree_id": "repo::snapshot-workspace",
        "orca_socket": str(root / "orca.sock"),
        "run_id": "run_snapshot",
        "main_terminal": "term_main",
        "role_specs": role_specs,
        "roles": {},
    }
    write_state(state_path, state)
    return workspace, config_path, state_path


class ManagementSnapshotTest(unittest.TestCase):
    def test_management_commands_use_saved_state_when_config_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, config_path, state_path = saved_state(root)
            config_path.write_text(
                "this config is no longer authoritative", encoding="utf-8"
            )
            config_path.unlink()
            observed: list[tuple[str, dict[str, object], str | None]] = []
            observed_specs = []

            def fake_manage(
                command: str, plan: dict[str, object], role: str | None
            ) -> dict[str, object]:
                observed.append((command, plan, role))
                observed_specs.append(cli._start_spec(plan, attach=False))
                return {"status": "ok"}

            with (
                mock.patch.dict(
                    os.environ,
                    {"XDG_STATE_HOME": str(root / "state")},
                ),
                mock.patch.object(
                    cli,
                    "load_config",
                    side_effect=AssertionError("management must not load config"),
                ),
                mock.patch.object(
                    cli,
                    "build_plan",
                    side_effect=AssertionError(
                        "management must not build a config plan"
                    ),
                ),
                mock.patch.object(cli, "manage_team", side_effect=fake_manage),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                for command, arguments in (
                    ("status", ()),
                    ("attach", ("main",)),
                    ("stop", ()),
                ):
                    with self.subTest(command=command):
                        result = cli.main(
                            [
                                command,
                                *arguments,
                                "--cwd",
                                str(workspace),
                                "--config",
                                str(config_path),
                            ]
                        )
                        self.assertEqual(result, 0)

            self.assertEqual(
                [(command, role) for command, _, role in observed],
                [("status", None), ("attach", "main"), ("stop", None)],
            )
            for command, plan, _ in observed:
                self.assertIn(command, {"status", "attach", "stop"})
                self.assertEqual(plan["team_id"], "snapshot-team")
                self.assertEqual(plan["workspace"], str(workspace))
                self.assertEqual(plan["config_path"], str(config_path))
                self.assertEqual(plan["state_path"], str(state_path))
                roles = plan["roles"]
                self.assertIsInstance(roles, dict)
                assert isinstance(roles, dict)
                self.assertEqual(roles["worker"]["model"], "saved-worker-model")
                self.assertEqual(roles["worker"]["permission"], "workspace-write")
                self.assertEqual(
                    roles["worker"]["instructions"], "saved worker instructions"
                )
            for spec in observed_specs:
                self.assertEqual(spec.config_path, Path(config_path))
                self.assertEqual(
                    spec.role_specs[Role.WORKER].model, "saved-worker-model"
                )
                self.assertEqual(
                    spec.role_specs[Role.WORKER].permission, "workspace-write"
                )

    def test_management_state_selection_rejects_ambiguous_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, _, _ = saved_state(root / "first", state_home=root / "state")
            _, _, second_state_path = saved_state(
                root / "second",
                state_home=root / "state",
                team_id="snapshot-team-two",
            )
            second_state = json.loads(second_state_path.read_text(encoding="utf-8"))
            second_state["workspace"] = str(workspace.resolve())
            write_state(second_state_path, second_state, require_existing=True)
            stderr = io.StringIO()

            with (
                mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}),
                contextlib.redirect_stderr(stderr),
            ):
                result = cli.main(
                    [
                        "status",
                        "--cwd",
                        str(workspace),
                    ]
                )

        self.assertEqual(result, 2)
        self.assertIn("ambiguous", stderr.getvalue())
        self.assertIn("--state", stderr.getvalue())

    def test_explicit_state_selection_does_not_depend_on_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, config_path, state_path = saved_state(root)
            other_workspace = root / "other-workspace"
            other_workspace.mkdir()
            observed: list[dict[str, object]] = []

            def fake_manage(
                _command: str, plan: dict[str, object], _role: str | None
            ) -> dict[str, object]:
                observed.append(plan)
                return {"status": "ok"}

            with (
                mock.patch.object(cli, "load_config", side_effect=AssertionError),
                mock.patch.object(cli, "build_plan", side_effect=AssertionError),
                mock.patch.object(cli, "manage_team", side_effect=fake_manage),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = cli.main(
                    [
                        "status",
                        "--state",
                        str(state_path),
                        "--cwd",
                        str(other_workspace),
                        "--config",
                        str(config_path),
                    ]
                )

        self.assertEqual(result, 0)
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["workspace"], str(workspace))

    def test_explicit_config_never_selects_another_saved_team(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, _, state_path = saved_state(root)
            for command, arguments in (
                ("status", ()),
                ("attach", ("main",)),
                ("stop", ()),
            ):
                for state_arguments in ((), ("--state", str(state_path))):
                    with (
                        self.subTest(
                            command=command, explicit_state=bool(state_arguments)
                        ),
                        mock.patch.dict(
                            os.environ, {"XDG_STATE_HOME": str(root / "state")}
                        ),
                        mock.patch.object(cli, "manage_team") as manage,
                        contextlib.redirect_stderr(io.StringIO()),
                    ):
                        result = cli.main(
                            [
                                command,
                                *arguments,
                                *state_arguments,
                                "--cwd",
                                str(workspace),
                                "--config",
                                str(root / "another-team.toml"),
                            ]
                        )
                        self.assertEqual(result, 2)
                        manage.assert_not_called()

    def test_named_team_uses_saved_state_without_loading_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = (root / "workspace").resolve()
            team_id = cli.team_name("build", workspace)
            workspace, config_path, state_path = saved_state(root, team_id=team_id)
            with (
                mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}),
                mock.patch.object(cli, "read_config_file", side_effect=AssertionError),
                mock.patch.object(cli, "load_config", side_effect=AssertionError),
                mock.patch.object(
                    cli, "manage_team", return_value={"status": "ok"}
                ) as manage,
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                for command, arguments in (
                    ("status", ()),
                    ("attach", ("main",)),
                    ("stop", ()),
                ):
                    result = cli.main(
                        [
                            command,
                            *arguments,
                            "--cwd",
                            str(workspace),
                            "--config",
                            str(config_path),
                            "--team",
                            "build",
                        ]
                    )
                    self.assertEqual(result, 0)
                    self.assertEqual(
                        manage.call_args.args[1]["state_path"], str(state_path)
                    )
                manage.reset_mock()
                for selection in (
                    ("--team", "other"),
                    ("--team", "build", "--team", "build"),
                ):
                    result = cli.main(["stop", "--cwd", str(workspace), *selection])
                    self.assertEqual(result, 2)
                    manage.assert_not_called()


if __name__ == "__main__":
    unittest.main()
