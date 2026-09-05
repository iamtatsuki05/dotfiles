from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_team import mcp_server


class McpLifecycleTest(unittest.TestCase):
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
                    "model": "gpt-5.6-sol",
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
                    return {"terminal": {"handle": "term_worker"}}
                if args[:2] == ["terminal", "wait"]:
                    return {"wait": {"satisfied": True}}
                if args[:2] == ["terminal", "show"]:
                    return {
                        "terminal": {
                            "preview": (
                                "OpenAI Codex task gpt-5.6-sol medium · ~/src/dotfiles"
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
                    return {"acknowledged": True}
                if args[:2] == ["orchestration", "worker-read"]:
                    return {"output": "worker output"}
                if args[:2] == ["orchestration", "worker-release"]:
                    return {"state": "released"}
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
                self.assertEqual(read["output"], "worker output")
                released = tool_payload(
                    mcp_server.call_tool("role_release", {"role": "worker"})
                )
                self.assertEqual(released["state"], "released")
                acknowledged = tool_payload(
                    mcp_server.call_tool("delivery_ack", {"delivery_id": "delivery_1"})
                )
                self.assertTrue(acknowledged["acknowledged"])

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


if __name__ == "__main__":
    unittest.main()
