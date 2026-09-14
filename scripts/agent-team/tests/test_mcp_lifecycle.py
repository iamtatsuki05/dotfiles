from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_team import mcp_server
from agent_team.adapters import AdapterSnapshot, FileIdentity, ReadSnapshot


class McpLifecycleTest(unittest.TestCase):
    def test_background_start_failure_keeps_unconfirmed_resources(self) -> None:
        for failure in (
            "unknown_stop",
            "verified_stop",
            "foreign_dispatch",
            "unknown_create",
            "context_stop",
            "unknown_close",
        ):
            with (
                self.subTest(failure=failure),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                state_path = self.make_state(root)
                state = mcp_server.runtime_read_state(state_path)
                state["role_specs"] = {
                    "planner": {
                        "provider": "copilot",
                        "transport": "direct",
                        "model": "gpt-6-astra",
                        "effort": "high",
                        "permission": "read-only",
                        "instructions": "read only",
                        "execution": "background",
                        "adapter_id": "github-copilot-direct-readonly-1.0.81",
                    }
                }
                mcp_server.runtime_write_state(state_path, state)
                private_root = root / "agent-team-provider-fixture"
                snapshot_root = root / "agent-team-snapshot-fixture"
                private_root.mkdir(mode=0o700)
                snapshot_root.mkdir(mode=0o700)
                adapter = mock.Mock()
                adapter.preflight.return_value = AdapterSnapshot(
                    "github-copilot-direct-readonly-1.0.81",
                    "fixture",
                    Path("/bin/true"),
                    "1.0.81",
                    FileIdentity(1, 2, 3, 4, "f" * 64),
                )
                calls: list[list[str]] = []

                def fake_orca(
                    _state: dict[str, object],
                    args: list[str],
                    *,
                    failure: str = failure,
                    calls: list[list[str]] = calls,
                ) -> dict[str, object]:
                    calls.append(args[:2])
                    if args[:2] == ["orchestration", "task-create"]:
                        return {"task": {"id": "task_1"}}
                    if args[:2] == ["terminal", "create"]:
                        if failure == "unknown_create":
                            raise RuntimeError("terminal create response was lost")
                        return {"terminal": {"handle": "term_planner"}}
                    if args[:2] == ["orchestration", "dispatch"]:
                        return {
                            "injected": False,
                            "dispatch": {
                                "id": "dispatch_1",
                                "task_id": "other"
                                if failure == "foreign_dispatch"
                                else "task_1",
                                "assignee_handle": "term_planner",
                                "run_id": "run_1",
                                "state": "ready",
                            },
                        }
                    if args[:2] == ["terminal", "send"]:
                        raise RuntimeError("send failed")
                    if args[:2] == ["orchestration", "worker-stop"]:
                        if failure == "unknown_stop":
                            return {}
                        if failure in {"context_stop", "unknown_close"}:
                            return {
                                "dispatchId": "dispatch_1",
                                "state": "stopped",
                                "processAction": "none",
                                "alreadySettled": False,
                            }
                        return {
                            "dispatchId": "dispatch_1",
                            "state": "stopped",
                            "processAction": "closed_agent_terminal",
                            "alreadySettled": False,
                            "close": {"handle": "term_planner", "ptyKilled": True},
                        }
                    if args[:2] == ["terminal", "close"]:
                        self.assertNotIn("--tab", args)
                        if failure == "unknown_close":
                            return {}
                        return {
                            "close": {
                                "handle": "term_planner",
                                "tabId": "tab_1",
                                "ptyKilled": True,
                            }
                        }
                    if args[:2] == ["orchestration", "task-update"]:
                        return {"task": {"id": "task_1", "status": "failed"}}
                    raise AssertionError(args)

                with (
                    mock.patch.object(
                        mcp_server, "background_adapter", return_value=adapter
                    ),
                    mock.patch.object(
                        mcp_server,
                        "create_read_snapshot",
                        return_value=ReadSnapshot(snapshot_root, ()),
                    ),
                    mock.patch.object(
                        mcp_server.tempfile, "mkdtemp", return_value=str(private_root)
                    ),
                    mock.patch.object(
                        mcp_server.tempfile, "gettempdir", return_value=str(root)
                    ),
                    mock.patch.object(mcp_server, "run_orca", side_effect=fake_orca),
                    self.assertRaises(RuntimeError) as raised,
                ):
                    mcp_server.start_background_role(
                        state_path, state, "planner", "read only"
                    )
                saved = mcp_server.runtime_read_state(state_path)
                if failure in {"verified_stop", "context_stop"}:
                    self.assertEqual(saved["roles"], {}, str(raised.exception))
                    self.assertFalse(private_root.exists())
                    self.assertFalse(snapshot_root.exists())
                else:
                    self.assertTrue(private_root.is_dir())
                    self.assertTrue(snapshot_root.is_dir())
                    if failure != "unknown_close":
                        self.assertNotIn(["terminal", "close"], calls)
                    if failure in {"foreign_dispatch", "unknown_create"}:
                        pending = saved["pending_role_start"]
                        self.assertTrue(pending["terminal_creation_attempted"])
                        if failure == "foreign_dispatch":
                            self.assertEqual(pending["terminal_handle"], "term_planner")
                        else:
                            self.assertNotIn("terminal_handle", pending)
                        self.assertNotIn("dispatch_id", saved["pending_role_start"])
                        self.assertNotIn(["orchestration", "worker-stop"], calls)
                    else:
                        self.assertIn("planner", saved["roles"])

    def make_state(self, root: Path) -> Path:
        workspace = root / "project"
        workspace.mkdir()
        state_path = root / "state" / "state.json"
        config_path = root / "config.toml"
        state = {
            "version": 3,
            "runtime": "orca",
            "team_id": "agent-team-project-1",
            "workspace": str(workspace),
            "config_path": str(config_path),
            "state_path": str(state_path),
            "launcher_path": "/tmp/agent-team",
            "worktree_id": "repo::project",
            "orca_socket": str(root / "orca.sock"),
            "run_id": "run_1",
            "main_terminal": "term_main",
            "role_specs": {
                "worker": {
                    "provider": "codex",
                    "transport": "direct",
                    "model": "gpt-6-astra",
                    "effort": "medium",
                    "permission": "workspace-write",
                    "instructions": "worker instructions",
                    "execution": "tui_direct",
                }
            },
            "roles": {},
        }
        mcp_server.runtime_write_state(state_path, state)
        return state_path

    def test_run_orca_redacts_invalid_json_and_provider_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            state = {"workspace": str(workspace)}
            raw = "secret-output /private/workspace --prompt raw-prompt"
            malformed = subprocess.CompletedProcess(
                ["orca", "status", "--json"], 0, raw, "secret-stderr"
            )
            command_failed = subprocess.CompletedProcess(
                ["orca", "status", "--json", "--path", "/private/workspace"],
                1,
                json.dumps(
                    {
                        "ok": False,
                        "error": {"message": raw},
                    }
                ),
                "provider-stderr",
            )
            raw_argv = [
                "orca",
                "orchestration",
                "check",
                "--terminal",
                "/private/terminal",
                "--prompt",
                "raw-prompt",
            ]
            timeout = subprocess.TimeoutExpired(
                raw_argv,
                1,
                output="raw-output /private/workspace",
                stderr="raw-stderr",
            )
            with (
                mock.patch.object(mcp_server, "orca_executable", return_value="orca"),
                mock.patch.object(
                    mcp_server.subprocess,
                    "run",
                    side_effect=[malformed, command_failed, timeout],
                ),
            ):
                with self.assertRaises(mcp_server.OrcaProtocolError) as malformed_error:
                    mcp_server.run_orca(state, ["status", "--json"])
                with self.assertRaises(mcp_server.OrcaCommandError) as command_error:
                    mcp_server.run_orca(
                        state,
                        ["status", "--json", "--path", "/private/workspace"],
                    )
                with self.assertRaises(mcp_server.OrcaTransportError) as timeout_error:
                    mcp_server.run_orca(state, raw_argv[1:])

            self.assertEqual(
                str(malformed_error.exception), "Orca response was invalid"
            )
            self.assertEqual(str(command_error.exception), "Orca command failed")
            self.assertEqual(
                str(timeout_error.exception), "Orca transport failed; effect is unknown"
            )
            for error in (
                malformed_error.exception,
                command_error.exception,
                timeout_error.exception,
            ):
                for canary in (
                    raw,
                    "secret-stderr",
                    "provider-stderr",
                    "/private/terminal",
                    "raw-prompt",
                    "raw-output /private/workspace",
                    "raw-stderr",
                ):
                    self.assertNotIn(canary, str(error))

    def test_public_tool_entrypoint_runs_worker_done_lifecycle_and_saves_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_path = self.make_state(root)
            calls: list[tuple[str, str]] = []

            def fake_orca(
                _state: dict[str, object],
                args: list[str],
                *,
                timeout_ms: int = 30_000,
            ) -> dict[str, object]:
                del timeout_ms
                calls.append((args[0], args[1]))
                if args[:2] == ["orchestration", "task-create"]:
                    return {"task": {"id": "task_1"}}
                if args[:2] == ["terminal", "create"]:
                    command = shlex.split(args[args.index("--command") + 1])
                    self.assertEqual(
                        command[:5],
                        [
                            "env",
                            f"CODEX_HOME={state_path.parent / 'codex' / 'worker'}",
                            "codex",
                            "-m",
                            "gpt-6-astra",
                        ],
                    )
                    self.assertIn(
                        'developer_instructions="worker instructions"', command
                    )
                    return {"terminal": {"handle": "term_worker"}}
                if args[:2] == ["terminal", "wait"]:
                    return {"wait": {"satisfied": True}}
                if args[:2] == ["terminal", "show"]:
                    return {
                        "terminal": {
                            "preview": (
                                "OpenAI Codex task gpt-6-astra medium · ~/src/dotfiles"
                            )
                        }
                    }
                if args[:2] == ["orchestration", "worker-start"]:
                    return {
                        "dispatchId": "dispatch_1",
                        "taskId": "task_1",
                        "assigneeHandle": "term_worker",
                        "runId": "run_1",
                    }
                if args[:2] == ["orchestration", "check"]:
                    if "--wait" in args:
                        return {
                            "deliveryId": "delivery_1",
                            "messages": [
                                {
                                    "type": "worker_done",
                                    "from": "term_worker",
                                    "payload": json.dumps(
                                        {
                                            "taskId": "task_1",
                                            "dispatchId": "dispatch_1",
                                            "outcome": "succeeded",
                                        }
                                    ),
                                }
                            ],
                        }
                    return {"acknowledged": "delivery_1"}
                if args[:2] == ["orchestration", "worker-read"]:
                    return {
                        "dispatchId": "dispatch_1",
                        "source": "terminal",
                        "sourceIdentity": "fixture-terminal",
                        "terminal": {
                            "handle": "term_worker",
                            "status": "exited",
                            "tail": ["worker output"],
                            "truncated": False,
                            "nextCursor": None,
                            "returnedLineCount": 1,
                        },
                        "cursor": None,
                        "status": {"worker": "completed", "terminal": "exited"},
                        "fallbackReason": None,
                        "warnings": [],
                    }
                if args[:2] == ["orchestration", "worker-release"]:
                    return {
                        "dispatchId": "dispatch_1",
                        "state": "released",
                        "processAction": "closed_agent_terminal",
                    }
                raise AssertionError(args)

            def tool_payload(response: dict[str, object]) -> dict[str, object]:
                self.assertFalse(response["isError"])
                content = response["content"]
                self.assertIsInstance(content, list)
                text = content[0]["text"]
                self.assertIsInstance(text, str)
                payload = json.loads(text)
                self.assertIsInstance(payload, dict)
                return payload

            with (
                mock.patch.dict(os.environ, {"AGENT_TEAM_STATE_PATH": str(state_path)}),
                mock.patch.object(mcp_server, "run_orca", side_effect=fake_orca),
            ):
                assignment = tool_payload(
                    mcp_server.call_tool(
                        "role_prompt", {"role": "worker", "text": "work"}
                    )
                )
                self.assertEqual(assignment["dispatch_id"], "dispatch_1")
                waited = tool_payload(
                    mcp_server.call_tool(
                        "role_wait", {"role": "worker", "timeout_ms": 5_000}
                    )
                )
                self.assertEqual(waited["deliveryId"], "delivery_1")
                read = tool_payload(
                    mcp_server.call_tool("role_read", {"role": "worker", "lines": 20})
                )
                self.assertEqual(read["terminal"]["tail"], ["worker output"])
                repeated_wait = mcp_server.call_tool(
                    "role_wait", {"role": "worker", "timeout_ms": 5_000}
                )
                self.assertTrue(repeated_wait["isError"])
                pending = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(pending["pending_delivery_stage"], "read")
                released = tool_payload(
                    mcp_server.call_tool("role_release", {"role": "worker"})
                )
                self.assertEqual(released["state"], "released")
                acknowledged = tool_payload(
                    mcp_server.call_tool("delivery_ack", {"delivery_id": "delivery_1"})
                )
                self.assertEqual(acknowledged["acknowledged"], "delivery_1")

            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["roles"], {})
            self.assertNotIn("pending_delivery_id", saved)
            self.assertEqual(
                calls,
                [
                    ("orchestration", "task-create"),
                    ("terminal", "create"),
                    ("terminal", "wait"),
                    ("terminal", "show"),
                    ("orchestration", "worker-start"),
                    ("orchestration", "check"),
                    ("orchestration", "worker-read"),
                    ("orchestration", "worker-release"),
                    ("orchestration", "check"),
                ],
            )

    def test_delivery_with_an_extra_completion_cannot_be_acknowledged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = self.make_state(Path(temp_dir))
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["roles"] = {
                "worker": {
                    "task_id": "task_1",
                    "dispatch_id": "dispatch_1",
                    "terminal_handle": "term_worker",
                    "completion_observed": False,
                    "launcher_owned_terminal": True,
                }
            }
            mcp_server.runtime_write_state(state_path, state)
            delivery = {
                "deliveryId": "delivery_1",
                "messages": [
                    {
                        "type": "worker_done",
                        "from": "term_worker",
                        "payload": {
                            "taskId": task_id,
                            "dispatchId": "dispatch_1",
                            "outcome": "succeeded",
                        },
                    }
                    for task_id in ("task_1", "unrelated_task")
                ],
            }
            with (
                mock.patch.dict(os.environ, {"AGENT_TEAM_STATE_PATH": str(state_path)}),
                mock.patch.object(mcp_server, "run_orca", return_value=delivery),
            ):
                waited = mcp_server.call_tool(
                    "role_wait", {"role": "worker", "timeout_ms": 5_000}
                )
                self.assertTrue(waited["isError"])
                acknowledged = mcp_server.call_tool(
                    "delivery_ack", {"delivery_id": "delivery_1"}
                )
                self.assertTrue(acknowledged["isError"])
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertFalse(saved["roles"]["worker"]["completion_observed"])
            self.assertEqual(saved["pending_delivery_id"], "delivery_1")
            self.assertEqual(saved["pending_delivery_kind"], "worker_done")
            self.assertEqual(saved["pending_delivery_stage"], "invalid")

    def test_malformed_delivery_is_pending_and_cannot_be_acknowledged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = self.make_state(Path(temp_dir))
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["roles"] = {
                "worker": {
                    "task_id": "task_1",
                    "dispatch_id": "dispatch_1",
                    "terminal_handle": "term_worker",
                    "completion_observed": False,
                    "launcher_owned_terminal": True,
                }
            }
            mcp_server.runtime_write_state(state_path, state)
            delivery = {
                "deliveryId": "delivery_malformed",
                "messages": [{"type": ["worker_done"]}],
            }
            with (
                mock.patch.dict(os.environ, {"AGENT_TEAM_STATE_PATH": str(state_path)}),
                mock.patch.object(mcp_server, "run_orca", return_value=delivery) as run,
            ):
                waited = mcp_server.call_tool(
                    "role_wait", {"role": "worker", "timeout_ms": 5_000}
                )
                acknowledged = mcp_server.call_tool(
                    "delivery_ack", {"delivery_id": "delivery_malformed"}
                )
            saved = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertTrue(waited["isError"])
        self.assertTrue(acknowledged["isError"])
        self.assertEqual(saved["pending_delivery_id"], "delivery_malformed")
        self.assertEqual(saved["pending_delivery_kind"], "unknown")
        self.assertEqual(saved["pending_delivery_stage"], "invalid")
        run.assert_called_once()

    def test_delivery_with_unknown_message_is_pending_and_cannot_be_acknowledged(
        self,
    ) -> None:
        for extra in ({"type": "future_event"}, None, "future event"):
            with (
                self.subTest(extra=extra),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                state_path = self.make_state(Path(temp_dir))
                state = json.loads(state_path.read_text(encoding="utf-8"))
                state["roles"] = {
                    "worker": {
                        "task_id": "task_1",
                        "dispatch_id": "dispatch_1",
                        "terminal_handle": "term_worker",
                        "completion_observed": False,
                        "launcher_owned_terminal": True,
                    }
                }
                mcp_server.runtime_write_state(state_path, state)
                delivery = {
                    "deliveryId": "delivery_unknown",
                    "messages": [
                        {
                            "type": "worker_done",
                            "from": "term_worker",
                            "payload": {
                                "taskId": "task_1",
                                "dispatchId": "dispatch_1",
                                "outcome": "succeeded",
                            },
                        },
                        extra,
                    ],
                }
                with (
                    mock.patch.dict(
                        os.environ, {"AGENT_TEAM_STATE_PATH": str(state_path)}
                    ),
                    mock.patch.object(
                        mcp_server, "run_orca", return_value=delivery
                    ) as run,
                ):
                    waited = mcp_server.call_tool(
                        "role_wait", {"role": "worker", "timeout_ms": 5_000}
                    )
                    acknowledged = mcp_server.call_tool(
                        "delivery_ack", {"delivery_id": "delivery_unknown"}
                    )
                saved = json.loads(state_path.read_text(encoding="utf-8"))

            self.assertTrue(waited["isError"])
            self.assertTrue(acknowledged["isError"])
            self.assertEqual(saved["pending_delivery_id"], "delivery_unknown")
            self.assertEqual(saved["pending_delivery_kind"], "worker_done")
            self.assertEqual(saved["pending_delivery_stage"], "invalid")
            self.assertFalse(saved["roles"]["worker"]["completion_observed"])
            run.assert_called_once()

    def test_worker_done_ack_requires_read_then_release(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_path = self.make_state(root)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["roles"] = {
                "worker": {
                    "task_id": "task_1",
                    "dispatch_id": "dispatch_1",
                    "terminal_handle": "term_worker",
                    "completion_observed": False,
                    "launcher_owned_terminal": True,
                }
            }
            mcp_server.runtime_write_state(state_path, state)
            calls: list[tuple[str, str]] = []
            fail_read = True
            fail_release = True

            def fake_orca(
                _state: dict[str, object],
                args: list[str],
                *,
                timeout_ms: int = 30_000,
            ) -> dict[str, object]:
                del timeout_ms
                nonlocal fail_read, fail_release
                calls.append((args[0], args[1]))
                if args[:2] == ["orchestration", "check"]:
                    if "--wait" in args:
                        return {
                            "deliveryId": "delivery_1",
                            "messages": [
                                {
                                    "type": "worker_done",
                                    "from": "term_worker",
                                    "payload": json.dumps(
                                        {
                                            "taskId": "task_1",
                                            "dispatchId": "dispatch_1",
                                            "outcome": "succeeded",
                                        }
                                    ),
                                }
                            ],
                        }
                    return {"acknowledged": "delivery_1"}
                if args[:2] == ["orchestration", "worker-read"]:
                    if fail_read:
                        raise RuntimeError("read failed")
                    return {
                        "dispatchId": "dispatch_1",
                        "source": "terminal",
                        "sourceIdentity": "fixture-terminal",
                        "terminal": {
                            "handle": "term_worker",
                            "status": "exited",
                            "tail": ["worker output"],
                            "truncated": False,
                            "nextCursor": None,
                            "returnedLineCount": 1,
                        },
                        "cursor": None,
                        "status": {"worker": "completed", "terminal": "exited"},
                        "fallbackReason": None,
                        "warnings": [],
                    }
                if args[:2] == ["orchestration", "worker-release"]:
                    if fail_release:
                        raise RuntimeError("release failed")
                    return {
                        "dispatchId": "dispatch_1",
                        "state": "released",
                        "processAction": "closed_agent_terminal",
                    }
                if args[:2] == ["terminal", "close"]:
                    return {"state": "closed"}
                raise AssertionError(args)

            with (
                mock.patch.dict(os.environ, {"AGENT_TEAM_STATE_PATH": str(state_path)}),
                mock.patch.object(mcp_server, "run_orca", side_effect=fake_orca),
            ):
                waited = mcp_server.call_tool(
                    "role_wait", {"role": "worker", "timeout_ms": 5_000}
                )
                self.assertFalse(waited["isError"])

                before_read = mcp_server.call_tool(
                    "delivery_ack", {"delivery_id": "delivery_1"}
                )
                self.assertTrue(before_read["isError"])

                failed_read = mcp_server.call_tool(
                    "role_read", {"role": "worker", "lines": 20}
                )
                self.assertTrue(failed_read["isError"])
                after_failed_read = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(after_failed_read["pending_delivery_id"], "delivery_1")
                self.assertEqual(
                    after_failed_read["pending_delivery_stage"], "observed"
                )

                fail_read = False
                read = mcp_server.call_tool(
                    "role_read", {"role": "worker", "lines": 20}
                )
                self.assertFalse(read["isError"])
                after_read = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(after_read["pending_delivery_stage"], "read")

                before_release = mcp_server.call_tool(
                    "delivery_ack", {"delivery_id": "delivery_1"}
                )
                self.assertTrue(before_release["isError"])

                failed_release = mcp_server.call_tool(
                    "role_release", {"role": "worker"}
                )
                self.assertTrue(failed_release["isError"])
                after_failed_release = json.loads(
                    state_path.read_text(encoding="utf-8")
                )
                self.assertEqual(
                    after_failed_release["pending_delivery_id"], "delivery_1"
                )
                self.assertEqual(after_failed_release["pending_delivery_stage"], "read")
                self.assertIn("worker", after_failed_release["roles"])

                fail_release = False
                released = mcp_server.call_tool("role_release", {"role": "worker"})
                self.assertFalse(released["isError"])
                after_release = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(after_release["pending_delivery_stage"], "released")
                acknowledged = mcp_server.call_tool(
                    "delivery_ack", {"delivery_id": "delivery_1"}
                )
                self.assertFalse(acknowledged["isError"])

            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["roles"], {})
            self.assertNotIn("pending_delivery_id", saved)
            self.assertEqual(
                calls,
                [
                    ("orchestration", "check"),
                    ("orchestration", "worker-read"),
                    ("orchestration", "worker-read"),
                    ("orchestration", "worker-release"),
                    ("orchestration", "worker-release"),
                    ("orchestration", "check"),
                ],
            )

    def test_worker_read_requires_matching_dispatch_and_structured_output(self) -> None:
        valid = {
            "dispatchId": "dispatch_1",
            "source": "terminal",
            "sourceIdentity": "fixture-terminal",
            "terminal": {
                "handle": "term_worker",
                "status": "exited",
                "tail": ["worker output"],
                "truncated": False,
                "nextCursor": None,
                "returnedLineCount": 1,
            },
            "cursor": None,
            "status": {"worker": "completed", "terminal": "exited"},
            "fallbackReason": None,
            "warnings": [],
        }
        invalid_results = (
            {},
            {**valid, "dispatchId": "dispatch_other"},
            {key: value for key, value in valid.items() if key != "terminal"},
        )
        for invalid in invalid_results:
            with (
                self.subTest(invalid=invalid),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                state_path = self.make_state(Path(temp_dir))
                state = json.loads(state_path.read_text(encoding="utf-8"))
                state["roles"] = {
                    "worker": {
                        "task_id": "task_1",
                        "dispatch_id": "dispatch_1",
                        "terminal_handle": "term_worker",
                        "completion_observed": True,
                        "launcher_owned_terminal": True,
                    }
                }
                state["pending_delivery_id"] = "delivery_1"
                state["pending_delivery_kind"] = "worker_done"
                state["pending_delivery_stage"] = "observed"
                mcp_server.runtime_write_state(state_path, state)
                with (
                    mock.patch.dict(
                        os.environ, {"AGENT_TEAM_STATE_PATH": str(state_path)}
                    ),
                    mock.patch.object(mcp_server, "run_orca", return_value=invalid),
                ):
                    response = mcp_server.call_tool(
                        "role_read", {"role": "worker", "lines": 20}
                    )
                saved = json.loads(state_path.read_text(encoding="utf-8"))

            self.assertTrue(response["isError"])
            self.assertEqual(saved["pending_delivery_stage"], "observed")
            self.assertIn("worker", saved["roles"])

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = self.make_state(Path(temp_dir))
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["roles"] = {
                "worker": {
                    "task_id": "task_1",
                    "dispatch_id": "dispatch_1",
                    "terminal_handle": "term_worker",
                    "completion_observed": True,
                    "launcher_owned_terminal": True,
                }
            }
            state["pending_delivery_id"] = "delivery_1"
            state["pending_delivery_kind"] = "worker_done"
            state["pending_delivery_stage"] = "observed"
            mcp_server.runtime_write_state(state_path, state)
            with (
                mock.patch.dict(os.environ, {"AGENT_TEAM_STATE_PATH": str(state_path)}),
                mock.patch.object(mcp_server, "run_orca", return_value=valid),
            ):
                response = mcp_server.call_tool(
                    "role_read", {"role": "worker", "lines": 20}
                )
            saved = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertFalse(response["isError"])
        self.assertEqual(saved["pending_delivery_stage"], "read")

    def test_background_release_requires_tab_close_receipt(self) -> None:
        invalid_receipts = (
            {},
            {
                "close": {
                    "handle": "term_other",
                    "tabId": "tab_1",
                    "closeMode": "tab",
                    "ptyKilled": False,
                }
            },
            {
                "close": {
                    "handle": "term_worker",
                    "tabId": "tab_1",
                    "closeMode": "pane",
                    "ptyKilled": True,
                }
            },
        )
        for invalid in invalid_receipts:
            with (
                self.subTest(invalid=invalid),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                state_path = self.make_state(Path(temp_dir))
                state = json.loads(state_path.read_text(encoding="utf-8"))
                state["role_specs"]["worker"]["execution"] = "background"
                state["role_specs"]["worker"]["adapter_id"] = (
                    "github-copilot-direct-readonly-1.0.81"
                )
                state["roles"] = {
                    "worker": {
                        "task_id": "task_1",
                        "dispatch_id": "dispatch_1",
                        "terminal_handle": "term_worker",
                        "completion_observed": True,
                        "launcher_owned_terminal": True,
                        "execution": "background",
                        "adapter_id": "github-copilot-direct-readonly-1.0.81",
                        "launch_nonce": "nonce1234",
                        "prompt_path": str(Path(temp_dir) / "prompt.md"),
                        "provider_private_root": str(Path(temp_dir) / "provider"),
                        "snapshot_root": str(Path(temp_dir) / "snapshot"),
                        "adapter_snapshot": {
                            "adapter_id": "github-copilot-direct-readonly-1.0.81",
                            "revision": "fixture",
                            "executable": "/bin/true",
                            "version": "fixture",
                            "identity": {
                                "device": 0,
                                "inode": 0,
                                "size": 0,
                                "mtime_ns": 0,
                                "sha256": "fixture",
                            },
                        },
                    }
                }
                state["pending_delivery_id"] = "delivery_1"
                state["pending_delivery_kind"] = "worker_done"
                state["pending_delivery_stage"] = "read"
                mcp_server.runtime_write_state(state_path, state)

                def fake_orca(
                    _state: dict[str, object],
                    args: list[str],
                    *,
                    timeout_ms: int = 30_000,
                    invalid_receipt: dict[str, object] = invalid,
                ) -> dict[str, object]:
                    del timeout_ms
                    if args[:2] == ["orchestration", "worker-release"]:
                        return {
                            "dispatchId": "dispatch_1",
                            "state": "released",
                            "processAction": "none",
                        }
                    if args[:2] == ["terminal", "close"]:
                        return invalid_receipt
                    raise AssertionError(args)

                with (
                    mock.patch.dict(
                        os.environ, {"AGENT_TEAM_STATE_PATH": str(state_path)}
                    ),
                    mock.patch.object(mcp_server, "run_orca", side_effect=fake_orca),
                    mock.patch.object(mcp_server, "cleanup_background_resources"),
                ):
                    response = mcp_server.call_tool("role_release", {"role": "worker"})
                saved = json.loads(state_path.read_text(encoding="utf-8"))

            self.assertTrue(response["isError"])
            self.assertIn("worker", saved["roles"])
            self.assertEqual(saved["pending_delivery_stage"], "read")

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = self.make_state(Path(temp_dir))
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["role_specs"]["worker"]["execution"] = "background"
            state["role_specs"]["worker"]["adapter_id"] = (
                "github-copilot-direct-readonly-1.0.81"
            )
            state["roles"] = {
                "worker": {
                    "task_id": "task_1",
                    "dispatch_id": "dispatch_1",
                    "terminal_handle": "term_worker",
                    "completion_observed": True,
                    "launcher_owned_terminal": True,
                }
            }
            state["pending_delivery_id"] = "delivery_1"
            state["pending_delivery_kind"] = "worker_done"
            state["pending_delivery_stage"] = "read"
            state["roles"]["worker"].update(
                {
                    "execution": "background",
                    "adapter_id": "github-copilot-direct-readonly-1.0.81",
                    "launch_nonce": "nonce1234",
                    "prompt_path": str(Path(temp_dir) / "prompt.md"),
                    "provider_private_root": str(Path(temp_dir) / "provider"),
                    "snapshot_root": str(Path(temp_dir) / "snapshot"),
                    "adapter_snapshot": {
                        "adapter_id": "github-copilot-direct-readonly-1.0.81",
                        "revision": "fixture",
                        "executable": "/bin/true",
                        "version": "fixture",
                        "identity": {
                            "device": 0,
                            "inode": 0,
                            "size": 0,
                            "mtime_ns": 0,
                            "sha256": "fixture",
                        },
                    },
                }
            )
            mcp_server.runtime_write_state(state_path, state)
            close = {
                "close": {
                    "handle": "term_worker",
                    "tabId": "tab_1",
                    "closeMode": "tab",
                    "ptyKilled": False,
                }
            }

            def fake_orca(
                _state: dict[str, object],
                args: list[str],
                *,
                timeout_ms: int = 30_000,
            ) -> dict[str, object]:
                del timeout_ms
                if args[:2] == ["orchestration", "worker-release"]:
                    return {
                        "dispatchId": "dispatch_1",
                        "state": "released",
                        "processAction": "none",
                    }
                if args[:2] == ["terminal", "close"]:
                    return close
                raise AssertionError(args)

            with (
                mock.patch.dict(os.environ, {"AGENT_TEAM_STATE_PATH": str(state_path)}),
                mock.patch.object(mcp_server, "run_orca", side_effect=fake_orca),
                mock.patch.object(mcp_server, "cleanup_background_resources"),
            ):
                response = mcp_server.call_tool("role_release", {"role": "worker"})
            saved = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertFalse(response["isError"])
        self.assertEqual(saved["roles"], {})
        self.assertEqual(saved["pending_delivery_stage"], "released")

    def test_ack_requires_wire_receipt_for_the_observed_delivery(self) -> None:
        for wire_ack in (None, "delivery_other"):
            with (
                self.subTest(wire_ack=wire_ack),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                state_path = self.make_state(root)
                state = json.loads(state_path.read_text(encoding="utf-8"))
                state["pending_delivery_id"] = "delivery_1"
                state["pending_delivery_kind"] = "worker_done"
                state["pending_delivery_stage"] = "released"
                mcp_server.runtime_write_state(state_path, state)

                with (
                    mock.patch.dict(
                        os.environ, {"AGENT_TEAM_STATE_PATH": str(state_path)}
                    ),
                    mock.patch.object(
                        mcp_server,
                        "run_orca",
                        return_value={"acknowledged": wire_ack},
                    ) as run_orca,
                ):
                    response = mcp_server.call_tool(
                        "delivery_ack", {"delivery_id": "delivery_1"}
                    )

                self.assertTrue(response["isError"])
                run_orca.assert_called_once()
                saved = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["pending_delivery_id"], "delivery_1")

    def test_question_uses_dispatch_sender_and_wire_reply_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_path = self.make_state(root)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["roles"] = {
                "worker": {
                    "task_id": "task_1",
                    "dispatch_id": "dispatch_1",
                    "terminal_handle": "term_worker",
                    "completion_observed": False,
                    "launcher_owned_terminal": True,
                }
            }
            mcp_server.runtime_write_state(state_path, state)

            def fake_orca(
                _state: dict[str, object],
                args: list[str],
                *,
                timeout_ms: int = 30_000,
            ) -> dict[str, object]:
                del timeout_ms
                if args[:2] == ["orchestration", "check"]:
                    if "--wait" in args:
                        return {
                            "deliveryId": "delivery_question",
                            "messages": [
                                {
                                    "id": "message_observed",
                                    "type": "question",
                                    "from_handle": "dispatch:dispatch_1",
                                    "payload": json.dumps(
                                        {
                                            "taskId": "task_1",
                                            "dispatchId": "dispatch_1",
                                        }
                                    ),
                                }
                            ],
                        }
                    return {"acknowledged": "delivery_question"}
                if args[:2] == ["orchestration", "reply"]:
                    return {
                        "message": {
                            "id": "answer_1",
                            "thread_id": "message_observed",
                            "run_id": "run_1",
                            "body": "answer",
                        },
                        "question": {
                            "message_id": "message_observed",
                            "run_id": "run_1",
                            "dispatch_id": "dispatch_1",
                            "status": "answered",
                            "answer_message_id": "answer_1",
                            "answer_body": "answer",
                        },
                        "duplicate": False,
                    }
                raise AssertionError(args)

            with (
                mock.patch.dict(os.environ, {"AGENT_TEAM_STATE_PATH": str(state_path)}),
                mock.patch.object(mcp_server, "run_orca", side_effect=fake_orca),
            ):
                waited = mcp_server.call_tool(
                    "role_wait", {"role": "worker", "timeout_ms": 5_000}
                )
                self.assertFalse(waited["isError"])
                replied = mcp_server.call_tool(
                    "message_reply",
                    {"message_id": "message_observed", "body": "answer"},
                )
                self.assertFalse(replied["isError"])
                acknowledged = mcp_server.call_tool(
                    "delivery_ack", {"delivery_id": "delivery_question"}
                )
                self.assertFalse(acknowledged["isError"])

            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertNotIn("pending_delivery_id", saved)

    def test_reply_requires_an_answered_wire_question_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_path = self.make_state(root)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["roles"] = {
                "worker": {
                    "task_id": "task_1",
                    "dispatch_id": "dispatch_1",
                    "terminal_handle": "term_worker",
                    "completion_observed": False,
                    "launcher_owned_terminal": True,
                }
            }
            state["pending_delivery_id"] = "delivery_question"
            state["pending_delivery_kind"] = "question"
            state["pending_delivery_stage"] = "observed"
            state["pending_question_ids"] = ["message_observed"]
            state["replied_question_ids"] = []
            mcp_server.runtime_write_state(state_path, state)

            with (
                mock.patch.dict(os.environ, {"AGENT_TEAM_STATE_PATH": str(state_path)}),
                mock.patch.object(mcp_server, "run_orca", return_value={}) as run_orca,
            ):
                response = mcp_server.call_tool(
                    "message_reply",
                    {"message_id": "message_observed", "body": "answer"},
                )

            self.assertTrue(response["isError"])
            run_orca.assert_called_once()
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["replied_question_ids"], [])
            self.assertEqual(saved["pending_delivery_id"], "delivery_question")

    def test_retained_release_keeps_assignment_and_terminal_for_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_path = self.make_state(root)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["roles"] = {
                "worker": {
                    "task_id": "task_1",
                    "dispatch_id": "dispatch_1",
                    "terminal_handle": "term_worker",
                    "completion_observed": True,
                    "launcher_owned_terminal": True,
                }
            }
            state["pending_delivery_id"] = "delivery_1"
            state["pending_delivery_kind"] = "worker_done"
            state["pending_delivery_stage"] = "read"
            mcp_server.runtime_write_state(state_path, state)
            calls: list[tuple[str, str]] = []

            def fake_orca(
                _state: dict[str, object],
                args: list[str],
                *,
                timeout_ms: int = 30_000,
            ) -> dict[str, object]:
                del timeout_ms
                calls.append((args[0], args[1]))
                if args[:2] == ["orchestration", "worker-release"]:
                    return {
                        "dispatchId": "dispatch_1",
                        "state": "retained",
                        "reason": "no_owned_resource",
                        "processAction": "none",
                    }
                raise AssertionError(args)

            with (
                mock.patch.dict(os.environ, {"AGENT_TEAM_STATE_PATH": str(state_path)}),
                mock.patch.object(mcp_server, "run_orca", side_effect=fake_orca),
            ):
                response = mcp_server.call_tool("role_release", {"role": "worker"})

            self.assertTrue(response["isError"])
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIn("worker", saved["roles"])
            self.assertEqual(saved["pending_delivery_stage"], "read")
            self.assertEqual(calls, [("orchestration", "worker-release")])

    def test_release_receipt_must_match_requested_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_path = self.make_state(root)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["roles"] = {
                "worker": {
                    "task_id": "task_1",
                    "dispatch_id": "dispatch_1",
                    "terminal_handle": "term_worker",
                    "completion_observed": True,
                    "launcher_owned_terminal": True,
                }
            }
            state["pending_delivery_id"] = "delivery_1"
            state["pending_delivery_kind"] = "worker_done"
            state["pending_delivery_stage"] = "read"
            mcp_server.runtime_write_state(state_path, state)

            with (
                mock.patch.dict(os.environ, {"AGENT_TEAM_STATE_PATH": str(state_path)}),
                mock.patch.object(
                    mcp_server,
                    "run_orca",
                    return_value={
                        "dispatchId": "dispatch_other",
                        "state": "released",
                        "processAction": "closed_agent_terminal",
                    },
                ),
            ):
                response = mcp_server.call_tool("role_release", {"role": "worker"})

            self.assertTrue(response["isError"])
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIn("worker", saved["roles"])
            self.assertEqual(saved["pending_delivery_stage"], "read")

    def test_question_reply_and_ack_require_observed_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_path = self.make_state(root)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["roles"] = {
                "worker": {
                    "task_id": "task_1",
                    "dispatch_id": "dispatch_1",
                    "terminal_handle": "term_worker",
                    "completion_observed": False,
                    "launcher_owned_terminal": True,
                }
            }
            mcp_server.runtime_write_state(state_path, state)
            calls: list[tuple[str, str]] = []

            def fake_orca(
                _state: dict[str, object],
                args: list[str],
                *,
                timeout_ms: int = 30_000,
            ) -> dict[str, object]:
                del timeout_ms
                calls.append((args[0], args[1]))
                if args[:2] == ["orchestration", "check"]:
                    if "--wait" in args:
                        return {
                            "deliveryId": "delivery_question",
                            "messages": [
                                {
                                    "id": "message_observed",
                                    "type": "question",
                                    "from_handle": "dispatch:dispatch_1",
                                    "payload": json.dumps(
                                        {
                                            "taskId": "task_1",
                                            "dispatchId": "dispatch_1",
                                        }
                                    ),
                                }
                            ],
                        }
                    return {"acknowledged": "delivery_question"}
                if args[:2] == ["orchestration", "reply"]:
                    return {
                        "message": {
                            "id": "answer_1",
                            "thread_id": "message_observed",
                            "run_id": "run_1",
                            "body": "answer",
                        },
                        "question": {
                            "message_id": "message_observed",
                            "run_id": "run_1",
                            "dispatch_id": "dispatch_1",
                            "status": "answered",
                            "answer_message_id": "answer_1",
                            "answer_body": "answer",
                        },
                        "duplicate": False,
                    }
                raise AssertionError(args)

            with (
                mock.patch.dict(os.environ, {"AGENT_TEAM_STATE_PATH": str(state_path)}),
                mock.patch.object(mcp_server, "run_orca", side_effect=fake_orca),
            ):
                waited = mcp_server.call_tool(
                    "role_wait", {"role": "worker", "timeout_ms": 5_000}
                )
                self.assertFalse(waited["isError"])

                unknown = mcp_server.call_tool(
                    "message_reply",
                    {"message_id": "message_unobserved", "body": "answer"},
                )
                self.assertTrue(unknown["isError"])
                before_reply = mcp_server.call_tool(
                    "delivery_ack", {"delivery_id": "delivery_question"}
                )
                self.assertTrue(before_reply["isError"])

                replied = mcp_server.call_tool(
                    "message_reply",
                    {"message_id": "message_observed", "body": "answer"},
                )
                self.assertFalse(replied["isError"])
                acknowledged = mcp_server.call_tool(
                    "delivery_ack", {"delivery_id": "delivery_question"}
                )
                self.assertFalse(acknowledged["isError"])

            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertNotIn("pending_delivery_id", saved)
            self.assertEqual(
                calls,
                [
                    ("orchestration", "check"),
                    ("orchestration", "reply"),
                    ("orchestration", "check"),
                ],
            )

    def test_lost_question_reply_receipt_confirms_the_same_answer_on_retry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_path = self.make_state(root)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["roles"] = {
                "worker": {
                    "task_id": "task_1",
                    "dispatch_id": "dispatch_1",
                    "terminal_handle": "term_worker",
                    "completion_observed": False,
                    "launcher_owned_terminal": True,
                }
            }
            mcp_server.runtime_write_state(state_path, state)
            fail_reply = True

            def fake_orca(
                _state: dict[str, object],
                args: list[str],
                *,
                timeout_ms: int = 30_000,
            ) -> dict[str, object]:
                del timeout_ms
                nonlocal fail_reply
                if args[:2] == ["orchestration", "check"] and "--wait" in args:
                    return {
                        "deliveryId": "delivery_question",
                        "messages": [
                            {
                                "id": "message_observed",
                                "type": "question",
                                "from_handle": "dispatch:dispatch_1",
                                "payload": json.dumps(
                                    {
                                        "taskId": "task_1",
                                        "dispatchId": "dispatch_1",
                                    }
                                ),
                            }
                        ],
                    }
                if args[:2] == ["orchestration", "check"]:
                    return {"acknowledged": "delivery_question"}
                if args[:2] == ["orchestration", "reply"] and fail_reply:
                    raise RuntimeError(
                        "reply response was lost after the answer was applied"
                    )
                if args[:2] == ["orchestration", "reply"]:
                    return {
                        "message": {
                            "id": "answer_1",
                            "thread_id": "message_observed",
                            "run_id": "run_1",
                            "body": "answer",
                        },
                        "question": {
                            "message_id": "message_observed",
                            "run_id": "run_1",
                            "dispatch_id": "dispatch_1",
                            "status": "answered",
                            "answer_message_id": "answer_1",
                            "answer_body": "answer",
                        },
                        "duplicate": True,
                    }
                raise AssertionError(args)

            with (
                mock.patch.dict(os.environ, {"AGENT_TEAM_STATE_PATH": str(state_path)}),
                mock.patch.object(mcp_server, "run_orca", side_effect=fake_orca),
            ):
                waited = mcp_server.call_tool(
                    "role_wait", {"role": "worker", "timeout_ms": 5_000}
                )
                self.assertFalse(waited["isError"])
                failed = mcp_server.call_tool(
                    "message_reply",
                    {"message_id": "message_observed", "body": "answer"},
                )
                self.assertTrue(failed["isError"])
                blocked = mcp_server.call_tool(
                    "delivery_ack", {"delivery_id": "delivery_question"}
                )
                self.assertTrue(blocked["isError"])
                pending = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(pending["pending_delivery_id"], "delivery_question")

                fail_reply = False
                replied = mcp_server.call_tool(
                    "message_reply",
                    {"message_id": "message_observed", "body": "answer"},
                )
                self.assertFalse(replied["isError"])
                acknowledged = mcp_server.call_tool(
                    "delivery_ack", {"delivery_id": "delivery_question"}
                )
                self.assertFalse(acknowledged["isError"])

    def test_escalation_delivery_remains_pending_for_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_path = self.make_state(root)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["roles"] = {
                "worker": {
                    "task_id": "task_1",
                    "dispatch_id": "dispatch_1",
                    "terminal_handle": "term_worker",
                    "completion_observed": False,
                    "launcher_owned_terminal": True,
                }
            }
            mcp_server.runtime_write_state(state_path, state)

            def fake_orca(
                _state: dict[str, object],
                args: list[str],
                *,
                timeout_ms: int = 30_000,
            ) -> dict[str, object]:
                del timeout_ms
                if args[:2] == ["orchestration", "check"] and "--wait" in args:
                    return {
                        "deliveryId": "delivery_escalation",
                        "messages": [
                            {
                                "type": "escalation",
                                "from_handle": "term_worker",
                                "payload": {
                                    "taskId": "task_1",
                                    "dispatchId": "dispatch_1",
                                },
                            }
                        ],
                    }
                raise AssertionError(args)

            with (
                mock.patch.dict(os.environ, {"AGENT_TEAM_STATE_PATH": str(state_path)}),
                mock.patch.object(mcp_server, "run_orca", side_effect=fake_orca),
            ):
                waited = mcp_server.call_tool(
                    "role_wait", {"role": "worker", "timeout_ms": 5_000}
                )
                self.assertFalse(waited["isError"])
                acknowledged = mcp_server.call_tool(
                    "delivery_ack", {"delivery_id": "delivery_escalation"}
                )
                self.assertTrue(acknowledged["isError"])

            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["pending_delivery_id"], "delivery_escalation")


if __name__ == "__main__":
    unittest.main()
