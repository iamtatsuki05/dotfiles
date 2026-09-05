from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import TYPE_CHECKING
from unittest import mock

if TYPE_CHECKING:
    from agent_team.acp_dependencies import AcpExecutables

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts" / "agent-team"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
agent_team_mcp = importlib.import_module("agent_team.mcp_server")
agent_team_locking = importlib.import_module("agent_team.locking")
MCP_SERVER = REPO_ROOT / "scripts" / "agent-team" / "agent-team"


class AgentTeamMcpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_temp_dirs: dict[Path, str] = {}

    def make_acp_executables(self, root: Path) -> AcpExecutables:
        bin_dir = root / "bin"
        bin_dir.mkdir(exist_ok=True)
        node = bin_dir / "node"
        node.write_text(
            '#!/bin/sh\nif [ "$1" = "--version" ]; then echo v22.23.2; fi\n',
            encoding="utf-8",
        )
        node.chmod(0o700)
        for package_name, version, command in (
            ("acpx", "0.13.2", "acpx"),
            (
                "@agentclientprotocol/claude-agent-acp",
                "0.70.0",
                "claude-agent-acp",
            ),
        ):
            package = root / "node_modules" / package_name
            (package / "dist").mkdir(parents=True)
            (package / "package.json").write_text(
                json.dumps(
                    {
                        "name": package_name,
                        "version": version,
                        "bin": {command: "dist/cli.js"},
                    }
                ),
                encoding="utf-8",
            )
            entry = package / "dist" / "cli.js"
            entry.write_text("#!/usr/bin/env node\n", encoding="utf-8")
            entry.chmod(0o755)
            (bin_dir / command).symlink_to(entry)
        return agent_team_mcp.AcpExecutables.resolve(path=str(bin_dir))

    def make_fixture(self, root: Path) -> tuple[Path, Path, Path]:
        fake_bin = root / "bin"
        fake_bin.mkdir()
        log_path = root / "orca-log.jsonl"
        fake = fake_bin / "orca"
        fake_linux = fake_bin / "orca-ide"
        fake.write_text(
            textwrap.dedent(
                f"""\
                #!{sys.executable}
                import json, os, sys
                from pathlib import Path
                args = sys.argv[1:]
                with Path(os.environ["FAKE_ORCA_LOG"]).open("a", encoding="utf-8") as f:
                    f.write(json.dumps(args) + "\\n")
                if args[:2] == ["orchestration", "task-create"]:
                    print(json.dumps({{"ok": True, "result": {{"task": {{"id": "task_1"}}}}}}))
                elif args[:2] == ["terminal", "create"]:
                    print(json.dumps({{"ok": True, "result": {{"terminal": {{"handle": "term_worker"}}}}}}))
                elif args[:2] == ["terminal", "wait"]:
                    print(json.dumps({{"ok": True, "result": {{"wait": {{"satisfied": True}}}}}}))
                elif args[:2] == ["terminal", "show"]:
                    print(json.dumps({{"ok": True, "result": {{"terminal": {{"preview": "Summarize recent commits gpt-6-astra medium · ~/src/dotfiles"}}}}}}))
                elif args[:2] == ["orchestration", "dispatch"]:
                    if os.environ.get("FAKE_ORCA_BAD_DISPATCH") == "1":
                        dispatch_task = "task_other"
                    else:
                        dispatch_task = "task_1"
                    injected_raw = os.environ.get("FAKE_ORCA_INJECTED", "false")
                    if injected_raw == "missing":
                        injected_field = {{}}
                    elif injected_raw == "true":
                        injected_field = {{"injected": True}}
                    elif injected_raw == "null":
                        injected_field = {{"injected": None}}
                    else:
                        injected_field = {{"injected": False}}
                    print(json.dumps({{"ok": True, "result": {{"dispatch": {{"id": "dispatch_acp", "task_id": dispatch_task, "assignee_handle": os.environ.get("FAKE_ORCA_DISPATCH_ASSIGNEE", "term_worker"), "run_id": "run_1", "state": "ready"}}, **injected_field}}}}))
                elif args[:2] == ["orchestration", "worker-start"]:
                    print(json.dumps({{"ok": True, "result": {{"dispatchId": "dispatch_1", "taskId": "task_1", "assigneeHandle": os.environ.get("FAKE_ORCA_DISPATCH_ASSIGNEE", "term_worker"), "runId": "run_1", "state": "ready"}}}}))
                elif args[:2] == ["orchestration", "worker-show"]:
                    print(json.dumps({{"ok": True, "result": {{"worker": {{"state": "ready"}}}}}}))
                elif args[:2] == ["orchestration", "check"]:
                    if "--ack" in args:
                        acknowledged = args[args.index("--ack") + 1]
                        print(json.dumps({{"ok": True, "result": {{"acknowledged": acknowledged}}}}))
                    else:
                        print(json.dumps({{"ok": True, "result": {{"deliveryId": "delivery_1", "messages": [{{"type": "worker_done", "from": os.environ.get("FAKE_ORCA_MESSAGE_FROM", "term_worker"), "payload": json.dumps({{"taskId": "task_1", "dispatchId": "dispatch_1", "outcome": "succeeded"}})}}]}}}}))
                elif args[:2] == ["orchestration", "worker-read"]:
                    dispatch = args[args.index("--dispatch") + 1]
                    print(json.dumps({{"ok": True, "result": {{"dispatchId": dispatch, "source": "terminal", "sourceIdentity": "fixture-terminal", "terminal": {{"handle": "term_worker", "status": "exited", "tail": ["done"], "truncated": False, "nextCursor": None, "returnedLineCount": 1}}, "cursor": None, "status": {{"worker": "completed", "terminal": "exited"}}, "fallbackReason": None, "warnings": []}}}}))
                elif args[:2] == ["orchestration", "worker-release"]:
                    dispatch = args[args.index("--dispatch") + 1]
                    print(json.dumps({{"ok": True, "result": {{"dispatchId": dispatch, "state": os.environ.get("FAKE_ORCA_RELEASE_STATE", "retained"), "processAction": "none", "reason": "no_owned_resource"}}}}))
                elif args[:2] == ["orchestration", "reply"]:
                    message_id = args[args.index("--id") + 1]
                    body = args[args.index("--body") + 1]
                    run_id = args[args.index("--run") + 1]
                    print(json.dumps({{"ok": True, "result": {{"message": {{"id": "answer_1", "thread_id": message_id, "run_id": run_id, "body": body}}, "question": {{"message_id": message_id, "run_id": run_id, "dispatch_id": "dispatch_1", "status": "answered", "answer_message_id": "answer_1", "answer_body": body}}, "duplicate": False}}}}))
                elif args[:2] == ["terminal", "send"]:
                    if os.environ.get("FAKE_ORCA_FAIL_SEND") == "1":
                        print(json.dumps({{"ok": False, "error": {{"message": "forced send failure"}}}})); raise SystemExit(1)
                    print(json.dumps({{"ok": True, "result": {{}}}}))
                elif args[:2] == ["orchestration", "worker-stop"]:
                    if os.environ.get("FAKE_ORCA_FAIL_WORKER_STOP") == "1":
                        print(json.dumps({{"ok": False, "error": {{"message": "forced worker-stop failure"}}}})); raise SystemExit(1)
                    dispatch = args[args.index("--dispatch") + 1]
                    stop_mode = os.environ.get("FAKE_ORCA_WORKER_STOP_MODE", "unknown")
                    if stop_mode == "owned":
                        print(json.dumps({{"ok": True, "result": {{"dispatchId": dispatch, "state": "stopped", "processAction": "closed_agent_terminal", "alreadySettled": False, "close": {{"handle": "term_worker", "ptyKilled": True}}}}}}))
                    elif stop_mode == "context":
                        print(json.dumps({{"ok": True, "result": {{"dispatchId": dispatch, "state": "stopped", "processAction": "none", "alreadySettled": False}}}}))
                    else:
                        print(json.dumps({{"ok": True, "result": {{}}}}))
                elif args[:2] == ["terminal", "close"]:
                    terminal = args[args.index("--terminal") + 1]
                    close_mode = os.environ.get("FAKE_ORCA_CLOSE_RECEIPT", "valid")
                    if close_mode == "unknown":
                        print(json.dumps({{"ok": True, "result": {{}}}}))
                    elif close_mode == "wrong_handle":
                        print(json.dumps({{"ok": True, "result": {{"close": {{"handle": "term_other", "tabId": "tab_1", "closeMode": "tab", "ptyKilled": False}}}}}}))
                    else:
                        close = {{"handle": terminal, "tabId": "tab_1", "ptyKilled": "--tab" not in args}}
                        if "--tab" in args:
                            close["closeMode"] = "tab"
                        print(json.dumps({{"ok": True, "result": {{"close": close}}}}))
                elif args[:2] == ["orchestration", "task-update"]:
                    print(json.dumps({{"ok": True, "result": {{}}}}))
                else:
                    print(json.dumps({{"ok": False, "error": {{"message": "unsupported"}}}})); raise SystemExit(2)
                """
            ),
            encoding="utf-8",
        )
        fake.chmod(0o755)
        fake_linux.write_text(fake.read_text(encoding="utf-8"), encoding="utf-8")
        fake_linux.chmod(0o755)
        executables = self.make_acp_executables(root)
        state = root / "state.json"
        state.write_text(
            json.dumps(
                {
                    "version": 3,
                    "runtime": "orca",
                    "team_id": "agent-team-project-1",
                    "workspace": str(root / "project"),
                    "config_path": str(root / "config.toml"),
                    "state_path": str(state),
                    "launcher_path": str(MCP_SERVER),
                    "worktree_id": "repo::/project",
                    "orca_socket": str(root / "orca.sock"),
                    "run_id": "run_1",
                    "main_terminal": "term_main",
                    "role_specs": {
                        "planner": {
                            "provider": "claude",
                            "transport": "acp",
                            "model": "fable",
                            "effort": "high",
                            "permission": "read-only",
                            "instructions": "planner snapshot",
                            "execution": "background",
                            "adapter_id": "claude-acp-0.70.0",
                            "acp_executables": executables.as_dict(),
                        },
                        "worker": {
                            "provider": "codex",
                            "transport": "direct",
                            "model": "gpt-6-astra",
                            "effort": "medium",
                            "permission": "workspace-write",
                            "instructions": "worker snapshot",
                            "execution": "tui_direct",
                        },
                        "reviewer": {
                            "provider": "codex",
                            "transport": "direct",
                            "model": "gpt-6-astra",
                            "effort": "high",
                            "permission": "read-only",
                            "instructions": "reviewer snapshot",
                            "execution": "tui_direct",
                        },
                    },
                    "roles": {},
                }
            ),
            encoding="utf-8",
        )
        state.chmod(0o600)
        runtime_temp = tempfile.TemporaryDirectory(prefix="agent-team-test-provider-")
        self.addCleanup(runtime_temp.cleanup)
        self.runtime_temp_dirs[state] = runtime_temp.name
        return fake_bin, log_path, state

    def start_server(
        self,
        fake_bin: Path,
        log_path: Path,
        state: Path,
        *,
        release_state: str = "retained",
        fail_send: bool = False,
        fail_worker_stop: bool = False,
        worker_stop_mode: str = "unknown",
        close_receipt: str = "valid",
        bad_dispatch: bool = False,
        dispatch_assignee: str = "term_worker",
        message_from: str = "term_worker",
        injected: str = "false",
    ) -> subprocess.Popen[str]:
        env = os.environ.copy()
        env.update(
            {
                "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
                "FAKE_ORCA_LOG": str(log_path),
                "AGENT_TEAM_STATE_PATH": str(state),
                "TMPDIR": self.runtime_temp_dirs[state],
                "FAKE_ORCA_RELEASE_STATE": release_state,
                "FAKE_ORCA_FAIL_SEND": "1" if fail_send else "0",
                "FAKE_ORCA_FAIL_WORKER_STOP": "1" if fail_worker_stop else "0",
                "FAKE_ORCA_WORKER_STOP_MODE": worker_stop_mode,
                "FAKE_ORCA_CLOSE_RECEIPT": close_receipt,
                "FAKE_ORCA_BAD_DISPATCH": "1" if bad_dispatch else "0",
                "FAKE_ORCA_DISPATCH_ASSIGNEE": dispatch_assignee,
                "FAKE_ORCA_MESSAGE_FROM": message_from,
                "FAKE_ORCA_INJECTED": injected,
            }
        )
        return subprocess.Popen(
            [str(MCP_SERVER), "_mcp-server"],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def request(
        self, process: subprocess.Popen[str], payload: dict[str, object]
    ) -> dict[str, object]:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(json.dumps(payload) + "\n")
        process.stdin.flush()
        line = process.stdout.readline()
        if not line:
            assert process.stderr is not None
            self.fail(process.stderr.read() or "MCP server closed")
        response = json.loads(line)
        self.assertIsInstance(response, dict)
        return response

    def call(
        self,
        process: subprocess.Popen[str],
        name: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        return self.request(
            process,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )

    def stop(self, process: subprocess.Popen[str]) -> None:
        assert process.stdin is not None
        process.stdin.close()
        process.wait(timeout=2)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()

    def test_lists_fixed_orca_coordination_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_bin, log_path, state = self.make_fixture(Path(temp_dir))
            process = self.start_server(fake_bin, log_path, state)
            try:
                response = self.request(
                    process,
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/list",
                        "params": {},
                    },
                )
            finally:
                self.stop(process)
        names = [tool["name"] for tool in response["result"]["tools"]]
        self.assertEqual(
            names,
            [
                "role_get",
                "role_prompt",
                "role_wait",
                "role_read",
                "role_release",
                "delivery_ack",
                "message_reply",
            ],
        )

    def test_role_prompt_creates_task_terminal_and_supervised_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "project").mkdir()
            fake_bin, log_path, state = self.make_fixture(root)
            injected = f"review; touch {root / 'must-not-exist'}"
            process = self.start_server(fake_bin, log_path, state)
            try:
                response = self.call(
                    process,
                    "role_prompt",
                    {"role": "worker", "text": injected},
                )
            finally:
                self.stop(process)
            log = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]
            saved = json.loads(state.read_text(encoding="utf-8"))

        self.assertFalse(response["result"]["isError"])
        self.assertFalse((root / "must-not-exist").exists())
        self.assertEqual(
            [row[:2] for row in log],
            [
                ["orchestration", "task-create"],
                ["terminal", "create"],
                ["terminal", "wait"],
                ["terminal", "show"],
                ["orchestration", "worker-start"],
            ],
        )
        task = log[0]
        self.assertEqual(task[task.index("--spec") + 1], injected)
        worker = log[4]
        self.assertIn("--terminal", worker)
        self.assertIn("term_worker", worker)
        self.assertIn("--from", worker)
        self.assertIn("term_main", worker)
        self.assertEqual(saved["roles"]["worker"]["dispatch_id"], "dispatch_1")

    def test_acp_role_uses_bare_terminal_dispatch_then_atomic_assignment_and_send(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "project").mkdir()
            fake_bin, log_path, state = self.make_fixture(root)
            process = self.start_server(fake_bin, log_path, state)
            try:
                response = self.call(
                    process,
                    "role_prompt",
                    {"role": "planner", "text": "read-only plan; do not modify files"},
                )
            finally:
                self.stop(process)
            log = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]
            saved = json.loads(state.read_text(encoding="utf-8"))
            assignment = saved["roles"]["planner"]
            prompt_path = Path(assignment["prompt_path"])
            self.assertTrue(prompt_path.is_file())
            self.assertEqual(
                prompt_path.read_text(encoding="utf-8"),
                "read-only plan; do not modify files",
            )
            provider_private_root = Path(assignment["provider_private_root"])
            snapshot_root = Path(assignment["snapshot_root"])
            self.assertTrue(provider_private_root.is_dir())
            self.assertTrue(snapshot_root.is_dir())
            with mock.patch.object(
                tempfile, "gettempdir", return_value=str(provider_private_root.parent)
            ):
                agent_team_mcp.cleanup_background_resources(
                    assignment, state, role="planner"
                )
            self.assertFalse(provider_private_root.exists())
            self.assertFalse(snapshot_root.exists())

        self.assertFalse(response["result"]["isError"])
        self.assertEqual(
            [row[:2] for row in log],
            [
                ["orchestration", "task-create"],
                ["terminal", "create"],
                ["orchestration", "dispatch"],
                ["terminal", "send"],
            ],
        )
        self.assertNotIn("--command", log[1])
        dispatch = log[2]
        self.assertEqual(dispatch[dispatch.index("--task") + 1], "task_1")
        self.assertEqual(dispatch[dispatch.index("--to") + 1], "term_worker")
        self.assertEqual(dispatch[dispatch.index("--from") + 1], "term_main")
        self.assertNotIn("--inject", dispatch)
        send = log[3]
        command = send[send.index("--text") + 1]
        self.assertIn("_acp-run planner", command)
        self.assertNotIn("read-only plan", command)
        self.assertNotIn("transport", assignment)
        self.assertEqual(assignment["dispatch_id"], "dispatch_acp")

    def test_acp_dispatch_requires_explicit_false_injected_flag(self) -> None:
        for injected in ("true", "null", "missing"):
            with (
                self.subTest(injected=injected),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                (root / "project").mkdir()
                fake_bin, log_path, state = self.make_fixture(root)
                process = self.start_server(
                    fake_bin, log_path, state, injected=injected
                )
                try:
                    response = self.call(
                        process,
                        "role_prompt",
                        {"role": "planner", "text": "read-only plan"},
                    )
                finally:
                    self.stop(process)
                saved = json.loads(state.read_text(encoding="utf-8"))
                commands = [
                    json.loads(line)[:2]
                    for line in log_path.read_text(encoding="utf-8").splitlines()
                ]

            self.assertTrue(response["result"]["isError"])
            self.assertNotIn(["terminal", "send"], commands)
            self.assertEqual(saved["roles"], {})

    def test_acp_allocation_failure_cleans_previous_root_and_marks_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "project").mkdir()
            _, _, state_path = self.make_fixture(root)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            provider_root = root / "provider-private"
            provider_root.mkdir()
            calls: list[list[str]] = []

            def fake_orca(
                _state: dict[str, object],
                args: list[str],
                *,
                timeout_ms: int = 30_000,
            ) -> dict[str, object]:
                del timeout_ms
                calls.append(args)
                if args[:2] == ["orchestration", "task-create"]:
                    return {"task": {"id": "task_1"}}
                if args[:2] == ["orchestration", "task-update"]:
                    return {}
                raise AssertionError(args)

            with (
                mock.patch.object(agent_team_mcp, "run_orca", side_effect=fake_orca),
                mock.patch.object(
                    agent_team_mcp.tempfile,
                    "mkdtemp",
                    side_effect=[str(provider_root), OSError("snapshot temp failed")],
                ),
                mock.patch.object(agent_team_mcp, "remove_owned_tree") as remove_tree,
                self.assertRaisesRegex(OSError, "snapshot temp failed"),
            ):
                agent_team_mcp.start_acp_role(state_path, state, "planner", "work")

        remove_tree.assert_called_once_with(provider_root)
        self.assertEqual(
            [args[:2] for args in calls],
            [
                ["orchestration", "task-create"],
                ["orchestration", "task-update"],
            ],
        )
        self.assertEqual(state["roles"], {})

    def test_acp_release_closes_owned_terminal_and_prompt_for_already_released_dispatch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "project").mkdir()
            fake_bin, log_path, state = self.make_fixture(root)
            prompt = root / "prompt-planner-nonce1234.md"
            prompt.write_text("planner", encoding="utf-8")
            prompt.chmod(0o600)
            provider_private_root = Path(
                tempfile.mkdtemp(
                    prefix="agent-team-provider-", dir=self.runtime_temp_dirs[state]
                )
            )
            snapshot_root = Path(
                tempfile.mkdtemp(
                    prefix="agent-team-snapshot-", dir=self.runtime_temp_dirs[state]
                )
            )
            saved = json.loads(state.read_text(encoding="utf-8"))
            saved["roles"] = {
                "planner": {
                    "task_id": "task_1",
                    "dispatch_id": "dispatch_acp",
                    "terminal_handle": "term_worker",
                    "completion_observed": True,
                    "launcher_owned_terminal": True,
                    "prompt_path": str(prompt),
                    "launch_nonce": "nonce1234",
                    "agent_command": "env AGENT_TEAM_ACP_MARKER=agent-team-project-1-planner-nonce1234 npx -y @agentclientprotocol/claude-agent-acp@0.70.0",
                    "execution": "background",
                    "adapter_id": "claude-acp-0.70.0",
                    "provider_private_root": str(provider_private_root),
                    "snapshot_root": str(snapshot_root),
                    "adapter_snapshot": {
                        "adapter_id": "claude-acp-0.70.0",
                        "revision": "acpx@0.13.2",
                        "executable": "npx",
                        "version": "@agentclientprotocol/claude-agent-acp@0.70.0",
                        "identity": {
                            "device": 0,
                            "inode": 0,
                            "size": 0,
                            "mtime_ns": 0,
                            "sha256": "acpx-managed",
                        },
                    },
                }
            }
            saved["pending_delivery_id"] = "delivery_1"
            saved["pending_delivery_kind"] = "worker_done"
            saved["pending_delivery_stage"] = "read"
            state.write_text(json.dumps(saved), encoding="utf-8")
            process = self.start_server(
                fake_bin, log_path, state, release_state="already_released"
            )
            try:
                response = self.call(process, "role_release", {"role": "planner"})
            finally:
                self.stop(process)
            commands = [
                json.loads(line)[:2]
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]
            after = json.loads(state.read_text(encoding="utf-8"))

        self.assertFalse(response["result"]["isError"], response)
        self.assertFalse(prompt.exists())
        self.assertFalse(provider_private_root.exists())
        self.assertFalse(snapshot_root.exists())
        self.assertEqual(after["roles"], {})
        self.assertEqual(
            commands,
            [["orchestration", "worker-release"], ["terminal", "close"]],
        )

    def test_direct_release_skips_close_when_orca_already_closed_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "project").mkdir()
            fake_bin, log_path, state = self.make_fixture(root)
            saved = json.loads(state.read_text(encoding="utf-8"))
            saved["roles"] = {
                "worker": {
                    "task_id": "task_1",
                    "dispatch_id": "dispatch_1",
                    "terminal_handle": "term_worker",
                    "completion_observed": True,
                    "launcher_owned_terminal": True,
                }
            }
            saved["pending_delivery_id"] = "delivery_1"
            saved["pending_delivery_kind"] = "worker_done"
            saved["pending_delivery_stage"] = "read"
            state.write_text(json.dumps(saved), encoding="utf-8")
            state.chmod(0o600)
            process = self.start_server(
                fake_bin, log_path, state, release_state="released"
            )
            try:
                response = self.call(process, "role_release", {"role": "worker"})
            finally:
                self.stop(process)
            commands = [
                json.loads(line)[:2]
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertFalse(response["result"]["isError"])
        self.assertEqual(commands, [["orchestration", "worker-release"]])

    def test_unknown_terminal_ownership_fails_before_release_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "project").mkdir()
            fake_bin, log_path, state = self.make_fixture(root)
            saved = json.loads(state.read_text(encoding="utf-8"))
            saved["roles"] = {
                "worker": {
                    "task_id": "task_1",
                    "dispatch_id": "dispatch_1",
                    "terminal_handle": "term_external",
                    "completion_observed": True,
                    "launcher_owned_terminal": False,
                }
            }
            state.write_text(json.dumps(saved), encoding="utf-8")
            state.chmod(0o600)
            process = self.start_server(fake_bin, log_path, state)
            try:
                response = self.call(process, "role_release", {"role": "worker"})
            finally:
                self.stop(process)
            commands = (
                [
                    json.loads(line)[:2]
                    for line in log_path.read_text(encoding="utf-8").splitlines()
                ]
                if log_path.exists()
                else []
            )

        self.assertTrue(response["result"]["isError"])
        self.assertEqual(commands, [])

    def test_acp_send_failure_preserves_assignment_when_stop_receipt_is_unknown(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "project").mkdir()
            fake_bin, log_path, state = self.make_fixture(root)
            process = self.start_server(fake_bin, log_path, state, fail_send=True)
            try:
                response = self.call(
                    process,
                    "role_prompt",
                    {"role": "planner", "text": "read-only plan"},
                )
            finally:
                self.stop(process)
            commands = [
                json.loads(line)[:2]
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]
            saved = json.loads(state.read_text(encoding="utf-8"))
            prompt_paths = list(root.glob("prompt-*.md"))
            assignment = saved["roles"]["planner"]
            prompt_exists = Path(assignment["prompt_path"]).is_file()
            provider_root_exists = Path(assignment["provider_private_root"]).is_dir()
            snapshot_root_exists = Path(assignment["snapshot_root"]).is_dir()

        self.assertTrue(response["result"]["isError"])
        self.assertIn(
            "ACP role startup failed", response["result"]["content"][0]["text"]
        )
        self.assertNotIn(
            "forced send failure", response["result"]["content"][0]["text"]
        )
        self.assertEqual(
            commands,
            [
                ["orchestration", "task-create"],
                ["terminal", "create"],
                ["orchestration", "dispatch"],
                ["terminal", "send"],
                ["orchestration", "worker-stop"],
                ["orchestration", "task-update"],
            ],
        )
        self.assertTrue(prompt_exists)
        self.assertTrue(provider_root_exists)
        self.assertTrue(snapshot_root_exists)
        self.assertEqual(len(prompt_paths), 1)

    def test_acp_send_failure_cleans_after_verified_process_stop(self) -> None:
        for stop_mode in ("owned", "context"):
            with (
                self.subTest(stop_mode=stop_mode),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                (root / "project").mkdir()
                fake_bin, log_path, state = self.make_fixture(root)
                process = self.start_server(
                    fake_bin,
                    log_path,
                    state,
                    fail_send=True,
                    worker_stop_mode=stop_mode,
                )
                try:
                    response = self.call(
                        process,
                        "role_prompt",
                        {"role": "planner", "text": "read-only plan"},
                    )
                finally:
                    self.stop(process)
                commands = [
                    json.loads(line)[:2]
                    for line in log_path.read_text(encoding="utf-8").splitlines()
                ]
                saved = json.loads(state.read_text(encoding="utf-8"))
                self.assertTrue(response["result"]["isError"])
                expected = [
                    ["orchestration", "task-create"],
                    ["terminal", "create"],
                    ["orchestration", "dispatch"],
                    ["terminal", "send"],
                    ["orchestration", "worker-stop"],
                ]
                if stop_mode == "context":
                    expected.append(["terminal", "close"])
                expected.append(["orchestration", "task-update"])
                self.assertEqual(commands, expected)
                self.assertEqual(saved["roles"], {})
                self.assertEqual(list(root.glob("prompt-*.md")), [])
                self.assertEqual(
                    list(Path(self.runtime_temp_dirs[state]).iterdir()), []
                )

    def test_acp_send_failure_preserves_assignment_when_process_close_receipt_is_unknown(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "project").mkdir()
            fake_bin, log_path, state = self.make_fixture(root)
            process = self.start_server(
                fake_bin,
                log_path,
                state,
                fail_send=True,
                worker_stop_mode="context",
                close_receipt="unknown",
            )
            try:
                response = self.call(
                    process,
                    "role_prompt",
                    {"role": "planner", "text": "read-only plan"},
                )
            finally:
                self.stop(process)
            commands = [
                json.loads(line)[:2]
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]
            saved = json.loads(state.read_text(encoding="utf-8"))
            assignment = saved["roles"]["planner"]
            prompt_exists = Path(assignment["prompt_path"]).is_file()
            provider_root_exists = Path(assignment["provider_private_root"]).is_dir()
            snapshot_root_exists = Path(assignment["snapshot_root"]).is_dir()

        self.assertTrue(response["result"]["isError"])
        self.assertEqual(
            commands,
            [
                ["orchestration", "task-create"],
                ["terminal", "create"],
                ["orchestration", "dispatch"],
                ["terminal", "send"],
                ["orchestration", "worker-stop"],
                ["terminal", "close"],
                ["orchestration", "task-update"],
            ],
        )
        self.assertTrue(prompt_exists)
        self.assertTrue(provider_root_exists)
        self.assertTrue(snapshot_root_exists)

    def test_direct_start_cleanup_attempts_all_resources_and_reports_each_error(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "project").mkdir()
            _, _, state_path = self.make_fixture(root)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            calls: list[list[str]] = []

            def fake_orca(
                state_value: dict[str, object],
                args: list[str],
                *,
                timeout_ms: int = 30_000,
            ) -> dict[str, object]:
                calls.append(args)
                if args[:2] == ["orchestration", "task-create"]:
                    return {"task": {"id": "task_1"}}
                if args[:2] == ["terminal", "create"]:
                    return {"terminal": {"handle": "term_worker"}}
                if args[:2] == ["terminal", "wait"]:
                    return {"wait": {"satisfied": True}}
                if args[:2] == ["terminal", "show"]:
                    return {
                        "terminal": {"preview": "gpt-6-astra medium · ~/src/dotfiles"}
                    }
                if args[:2] == ["orchestration", "worker-start"]:
                    return {
                        "dispatchId": "dispatch_1",
                        "taskId": "task_1",
                        "assigneeHandle": "term_worker",
                        "runId": "run_1",
                    }
                if args[:2] == ["orchestration", "worker-stop"]:
                    raise RuntimeError("worker stop failed")
                if args[:2] == ["terminal", "close"]:
                    raise RuntimeError("terminal close failed")
                if args[:2] == ["orchestration", "task-update"]:
                    raise RuntimeError("task update failed")
                raise AssertionError(args)

            with (
                mock.patch.object(agent_team_mcp, "run_orca", side_effect=fake_orca),
                mock.patch.object(
                    agent_team_mcp,
                    "save_state",
                    side_effect=RuntimeError("state save failed"),
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    "state save failed.*worker stop failed.*terminal close failed.*task update failed",
                ),
            ):
                agent_team_mcp.start_role(state_path, state, "worker", "work")

        self.assertEqual(
            [args[:2] for args in calls],
            [
                ["orchestration", "task-create"],
                ["terminal", "create"],
                ["terminal", "wait"],
                ["terminal", "show"],
                ["orchestration", "worker-start"],
                ["orchestration", "worker-stop"],
                ["terminal", "close"],
                ["orchestration", "task-update"],
            ],
        )

    def test_direct_start_leaves_terminal_cleanup_to_successful_worker_stop(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "project").mkdir()
            _, _, state_path = self.make_fixture(root)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            calls: list[list[str]] = []

            def fake_orca(
                state_value: dict[str, object],
                args: list[str],
                *,
                timeout_ms: int = 30_000,
            ) -> dict[str, object]:
                calls.append(args)
                if args[:2] == ["orchestration", "task-create"]:
                    return {"task": {"id": "task_1"}}
                if args[:2] == ["terminal", "create"]:
                    return {"terminal": {"handle": "term_worker"}}
                if args[:2] == ["terminal", "wait"]:
                    return {"wait": {"satisfied": True}}
                if args[:2] == ["terminal", "show"]:
                    return {
                        "terminal": {"preview": "gpt-6-astra medium · ~/src/dotfiles"}
                    }
                if args[:2] == ["orchestration", "worker-start"]:
                    return {
                        "dispatchId": "dispatch_1",
                        "taskId": "task_1",
                        "assigneeHandle": "term_worker",
                        "runId": "run_1",
                    }
                return {}

            with (
                mock.patch.object(agent_team_mcp, "run_orca", side_effect=fake_orca),
                mock.patch.object(
                    agent_team_mcp,
                    "save_state",
                    side_effect=RuntimeError("state save failed"),
                ),
                self.assertRaisesRegex(RuntimeError, "state save failed"),
            ):
                agent_team_mcp.start_role(state_path, state, "worker", "work")

        self.assertEqual(
            [args[:2] for args in calls],
            [
                ["orchestration", "task-create"],
                ["terminal", "create"],
                ["terminal", "wait"],
                ["terminal", "show"],
                ["orchestration", "worker-start"],
                ["orchestration", "worker-stop"],
                ["orchestration", "task-update"],
            ],
        )

    def test_require_existing_save_does_not_recreate_missing_state_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, _, fixture_state = self.make_fixture(root)
            state = json.loads(fixture_state.read_text(encoding="utf-8"))
            target = root / "missing-parent" / "state.json"
            state["state_path"] = str(target)

            with self.assertRaisesRegex(agent_team_mcp.ToolInputError, "disappeared"):
                agent_team_mcp.save_state(target, state)

            parent_exists = target.parent.exists()

        self.assertFalse(parent_exists)

    def test_raw_mcp_reporter_uses_the_shared_linux_orca_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state = {"workspace": str(root)}
            completed = subprocess.CompletedProcess(
                ["orca-ide", "status", "--json"],
                0,
                '{"ok": true, "result": {}}',
                "",
            )
            with (
                mock.patch.object(
                    agent_team_mcp,
                    "orca_executable",
                    return_value="orca-ide",
                    create=True,
                ),
                mock.patch.object(
                    agent_team_mcp.subprocess,
                    "run",
                    return_value=completed,
                ) as run,
            ):
                agent_team_mcp.run_orca(state, ["status", "--json"])

        self.assertEqual(run.call_args.args[0], ["orca-ide", "status", "--json"])

    def test_stateful_tool_holds_reservation_over_remote_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, _, state_path = self.make_fixture(root)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["pending_delivery_id"] = "delivery_1"
            state["pending_delivery_kind"] = "worker_done"
            state["pending_delivery_stage"] = "released"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            state_path.chmod(0o600)
            observed_lock = False

            def fake_orca(
                state_value: dict[str, object],
                args: list[str],
                *,
                timeout_ms: int = 30_000,
            ) -> dict[str, object]:
                del state_value, args, timeout_ms
                nonlocal observed_lock
                probe = agent_team_locking._LifecycleReservation(
                    state_path, create_parent=False
                )
                try:
                    probe.acquire()
                except agent_team_mcp.RuntimeFailure as exc:
                    observed_lock = (
                        exc.code is agent_team_mcp.ErrorCode.TEAM_ALREADY_RUNNING
                    )
                else:
                    probe.release()
                return {"acknowledged": "delivery_1"}

            with (
                mock.patch.dict(os.environ, {"AGENT_TEAM_STATE_PATH": str(state_path)}),
                mock.patch.object(agent_team_mcp, "run_orca", side_effect=fake_orca),
            ):
                agent_team_mcp.execute_tool(
                    "delivery_ack", {"delivery_id": "delivery_1"}
                )

        self.assertTrue(observed_lock)

    def test_stateful_tool_rejects_generation_change_before_stale_save(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, _, state_path = self.make_fixture(root)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["pending_delivery_id"] = "delivery_1"
            state["pending_delivery_kind"] = "worker_done"
            state["pending_delivery_stage"] = "released"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            state_path.chmod(0o600)

            def mutate_generation(
                state_value: dict[str, object],
                args: list[str],
                *,
                timeout_ms: int = 30_000,
            ) -> dict[str, object]:
                del state_value, args, timeout_ms
                replacement = dict(state)
                replacement["run_id"] = "run_new"
                replacement["main_terminal"] = "term_new"
                state_path.write_text(json.dumps(replacement), encoding="utf-8")
                state_path.chmod(0o600)
                return {"acknowledged": "delivery_1"}

            with (
                mock.patch.dict(os.environ, {"AGENT_TEAM_STATE_PATH": str(state_path)}),
                mock.patch.object(
                    agent_team_mcp, "run_orca", side_effect=mutate_generation
                ),
                self.assertRaises(agent_team_mcp.ToolInputError),
            ):
                agent_team_mcp.execute_tool(
                    "delivery_ack", {"delivery_id": "delivery_1"}
                )
            current = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(current["run_id"], "run_new")
        self.assertEqual(current["main_terminal"], "term_new")

    def test_stateful_remote_reply_rejects_generation_change_after_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, _, state_path = self.make_fixture(root)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["pending_delivery_id"] = "delivery_question"
            state["pending_delivery_kind"] = "question"
            state["pending_delivery_stage"] = "observed"
            state["pending_question_ids"] = ["message_1"]
            state["replied_question_ids"] = []
            state_path.write_text(json.dumps(state), encoding="utf-8")

            def mutate_generation(
                state_value: dict[str, object],
                args: list[str],
                *,
                timeout_ms: int = 30_000,
            ) -> dict[str, object]:
                del state_value, args, timeout_ms
                replacement = dict(state)
                replacement["run_id"] = "run_new"
                replacement["main_terminal"] = "term_new"
                state_path.write_text(json.dumps(replacement), encoding="utf-8")
                state_path.chmod(0o600)
                return {
                    "message": {
                        "id": "answer_1",
                        "thread_id": "message_1",
                        "run_id": "run_1",
                        "body": "answer",
                    },
                    "question": {
                        "message_id": "message_1",
                        "run_id": "run_1",
                        "dispatch_id": "dispatch_1",
                        "status": "answered",
                        "answer_message_id": "answer_1",
                        "answer_body": "answer",
                    },
                    "duplicate": False,
                }

            with (
                mock.patch.dict(os.environ, {"AGENT_TEAM_STATE_PATH": str(state_path)}),
                mock.patch.object(
                    agent_team_mcp, "run_orca", side_effect=mutate_generation
                ),
                self.assertRaises(agent_team_mcp.ToolInputError),
            ):
                agent_team_mcp.execute_tool(
                    "message_reply", {"message_id": "message_1", "body": "answer"}
                )

    def test_stateful_tool_rejects_metadata_generation_change_before_stale_save(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, _, state_path = self.make_fixture(root)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["pending_delivery_id"] = "delivery_1"
            state["pending_delivery_kind"] = "worker_done"
            state["pending_delivery_stage"] = "released"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            state_path.chmod(0o600)

            def mutate_metadata(
                state_value: dict[str, object],
                args: list[str],
                *,
                timeout_ms: int = 30_000,
            ) -> dict[str, object]:
                del state_value, args, timeout_ms
                replacement = dict(state)
                replacement["team_id"] = "agent-team-foreign"
                replacement["workspace"] = str(root / "foreign-project")
                replacement["config_path"] = str(root / "foreign-config.toml")
                state_path.write_text(json.dumps(replacement), encoding="utf-8")
                state_path.chmod(0o600)
                return {"acknowledged": "delivery_1"}

            with (
                mock.patch.dict(os.environ, {"AGENT_TEAM_STATE_PATH": str(state_path)}),
                mock.patch.object(
                    agent_team_mcp, "run_orca", side_effect=mutate_metadata
                ),
                self.assertRaises(agent_team_mcp.ToolInputError),
            ):
                agent_team_mcp.execute_tool(
                    "delivery_ack", {"delivery_id": "delivery_1"}
                )
            current = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(current["team_id"], "agent-team-foreign")
        self.assertEqual(current["workspace"], str(root / "foreign-project"))
        self.assertEqual(current["config_path"], str(root / "foreign-config.toml"))

    def test_dispatch_identity_mismatch_retains_known_resources_without_stopping_foreign_dispatch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "project").mkdir()
            fake_bin, log_path, state = self.make_fixture(root)
            process = self.start_server(fake_bin, log_path, state, bad_dispatch=True)
            try:
                response = self.call(
                    process,
                    "role_prompt",
                    {"role": "planner", "text": "read-only plan"},
                )
                retry = self.call(
                    process,
                    "role_prompt",
                    {"role": "worker", "text": "must remain blocked"},
                )
            finally:
                self.stop(process)
            commands = [
                json.loads(line)[:2]
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]
            saved = json.loads(state.read_text(encoding="utf-8"))
            pending = saved["pending_role_start"]
            self.assertTrue(Path(pending["prompt_path"]).is_file())
            self.assertTrue(Path(pending["provider_private_root"]).is_dir())
            self.assertTrue(Path(pending["snapshot_root"]).is_dir())

        self.assertTrue(response["result"]["isError"])
        self.assertTrue(retry["result"]["isError"])
        self.assertIn("cleanup is pending", retry["result"]["content"][0]["text"])
        self.assertEqual(pending["terminal_handle"], "term_worker")
        self.assertEqual(pending["task_id"], "task_1")
        self.assertNotIn("dispatch_id", pending)
        self.assertNotIn(["terminal", "send"], commands)
        self.assertNotIn(["orchestration", "worker-stop"], commands)
        self.assertNotIn(["terminal", "close"], commands)
        self.assertEqual(commands.count(["orchestration", "task-create"]), 1)

    def test_worker_done_from_other_terminal_is_not_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "project").mkdir()
            fake_bin, log_path, state = self.make_fixture(root)
            saved = json.loads(state.read_text(encoding="utf-8"))
            saved["roles"] = {
                "worker": {
                    "task_id": "task_1",
                    "dispatch_id": "dispatch_1",
                    "terminal_handle": "term_worker",
                    "completion_observed": False,
                    "launcher_owned_terminal": True,
                }
            }
            state.write_text(json.dumps(saved), encoding="utf-8")
            state.chmod(0o600)
            process = self.start_server(
                fake_bin, log_path, state, message_from="term_other"
            )
            try:
                response = self.call(
                    process,
                    "role_wait",
                    {"role": "worker", "timeout_ms": 5000},
                )
            finally:
                self.stop(process)
            after = json.loads(state.read_text(encoding="utf-8"))

        self.assertTrue(response["result"]["isError"])
        self.assertEqual(after["pending_delivery_id"], "delivery_1")
        self.assertEqual(after["pending_delivery_kind"], "worker_done")
        self.assertEqual(after["pending_delivery_stage"], "invalid")
        self.assertFalse(after["roles"]["worker"]["completion_observed"])

    def test_identity_helper_rejects_untrusted_team_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            executables = self.make_acp_executables(Path(temp_dir))
            with self.assertRaisesRegex(agent_team_mcp.ToolInputError, "identity"):
                agent_team_mcp.acp_agent_command(
                    "team with spaces",
                    "planner",
                    "nonce1234",
                    executables=executables,
                )

    def test_invalid_role_never_reaches_orca(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_bin, log_path, state = self.make_fixture(Path(temp_dir))
            process = self.start_server(fake_bin, log_path, state)
            try:
                response = self.call(process, "role_get", {"role": "main"})
            finally:
                self.stop(process)
        self.assertTrue(response["result"]["isError"])
        self.assertFalse(log_path.exists())

    def test_release_removes_only_the_finished_role_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "project").mkdir()
            fake_bin, log_path, state = self.make_fixture(root)
            saved = json.loads(state.read_text(encoding="utf-8"))
            saved["roles"] = {
                "worker": {
                    "task_id": "task_1",
                    "dispatch_id": "dispatch_1",
                    "terminal_handle": "term_worker",
                    "completion_observed": True,
                    "launcher_owned_terminal": True,
                }
            }
            saved["pending_delivery_id"] = "delivery_1"
            saved["pending_delivery_kind"] = "worker_done"
            saved["pending_delivery_stage"] = "read"
            state.write_text(json.dumps(saved), encoding="utf-8")
            process = self.start_server(
                fake_bin, log_path, state, release_state="released"
            )
            try:
                response = self.call(process, "role_release", {"role": "worker"})
            finally:
                self.stop(process)
            after = json.loads(state.read_text(encoding="utf-8"))
            commands = (
                [
                    json.loads(line)[:2]
                    for line in log_path.read_text(encoding="utf-8").splitlines()
                ]
                if log_path.exists()
                else []
            )

        self.assertFalse(response["result"]["isError"])
        self.assertEqual(after["roles"], {})
        self.assertEqual(
            commands,
            [["orchestration", "worker-release"]],
        )

    def test_delivery_ack_requires_observed_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "project").mkdir()
            fake_bin, log_path, state = self.make_fixture(root)
            saved = json.loads(state.read_text(encoding="utf-8"))
            saved["roles"] = {
                "worker": {
                    "task_id": "task_1",
                    "dispatch_id": "dispatch_1",
                    "terminal_handle": "term_worker",
                    "completion_observed": True,
                    "launcher_owned_terminal": True,
                }
            }
            saved["pending_delivery_id"] = "delivery_1"
            state.write_text(json.dumps(saved), encoding="utf-8")
            process = self.start_server(fake_bin, log_path, state)
            try:
                response = self.call(
                    process,
                    "delivery_ack",
                    {"delivery_id": "delivery_1"},
                )
            finally:
                self.stop(process)
            log_exists = log_path.exists()

        self.assertTrue(response["result"]["isError"])
        self.assertFalse(log_exists)

    def test_save_does_not_recreate_state_removed_while_orca_operation_runs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "project").mkdir()
            _, _, state = self.make_fixture(root)
            saved = json.loads(state.read_text(encoding="utf-8"))
            saved["pending_delivery_id"] = "delivery_1"
            saved["pending_delivery_kind"] = "worker_done"
            saved["pending_delivery_stage"] = "released"

            def remove_state(*args: object, **kwargs: object) -> dict[str, object]:
                state.unlink()
                return {"acknowledged": "delivery_1"}

            with (
                mock.patch.dict(os.environ, {"AGENT_TEAM_STATE_PATH": str(state)}),
                mock.patch.object(
                    agent_team_mcp, "load_state", return_value=(state, saved)
                ),
                mock.patch.object(agent_team_mcp, "run_orca", side_effect=remove_state),
                self.assertRaisesRegex(agent_team_mcp.ToolInputError, "disappeared"),
            ):
                agent_team_mcp.execute_tool(
                    "delivery_ack", {"delivery_id": "delivery_1"}
                )

        self.assertFalse(state.exists())

    def test_retained_terminal_not_owned_by_launcher_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "project").mkdir()
            fake_bin, log_path, state = self.make_fixture(root)
            saved = json.loads(state.read_text(encoding="utf-8"))
            saved["roles"] = {
                "worker": {
                    "task_id": "task_1",
                    "dispatch_id": "dispatch_1",
                    "terminal_handle": "term_external",
                    "completion_observed": True,
                    "launcher_owned_terminal": False,
                }
            }
            state.write_text(json.dumps(saved), encoding="utf-8")
            process = self.start_server(fake_bin, log_path, state)
            try:
                response = self.call(process, "role_release", {"role": "worker"})
            finally:
                self.stop(process)
            after = json.loads(state.read_text(encoding="utf-8"))
            commands = (
                [
                    json.loads(line)[:2]
                    for line in log_path.read_text(encoding="utf-8").splitlines()
                ]
                if log_path.exists()
                else []
            )

        self.assertTrue(response["result"]["isError"])
        self.assertIn("worker", after["roles"])
        self.assertEqual(commands, [])

    def test_read_and_release_fail_before_matching_worker_done(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "project").mkdir()
            fake_bin, log_path, state = self.make_fixture(root)
            saved = json.loads(state.read_text(encoding="utf-8"))
            saved["roles"] = {
                "worker": {
                    "task_id": "task_1",
                    "dispatch_id": "dispatch_1",
                    "terminal_handle": "term_worker",
                    "completion_observed": False,
                    "launcher_owned_terminal": True,
                }
            }
            state.write_text(json.dumps(saved), encoding="utf-8")
            process = self.start_server(fake_bin, log_path, state)
            try:
                read = self.call(process, "role_read", {"role": "worker"})
                release = self.call(process, "role_release", {"role": "worker"})
            finally:
                self.stop(process)

        self.assertTrue(read["result"]["isError"])
        self.assertTrue(release["result"]["isError"])
        self.assertFalse(log_path.exists())

    def test_second_role_is_rejected_while_one_dispatch_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "project").mkdir()
            fake_bin, log_path, state = self.make_fixture(root)
            saved = json.loads(state.read_text(encoding="utf-8"))
            saved["roles"] = {
                "worker": {
                    "task_id": "task_1",
                    "dispatch_id": "dispatch_1",
                    "terminal_handle": "term_worker",
                    "completion_observed": False,
                    "launcher_owned_terminal": True,
                }
            }
            state.write_text(json.dumps(saved), encoding="utf-8")
            process = self.start_server(fake_bin, log_path, state)
            try:
                response = self.call(
                    process,
                    "role_prompt",
                    {"role": "reviewer", "text": "review"},
                )
            finally:
                self.stop(process)

        self.assertTrue(response["result"]["isError"])
        self.assertFalse(log_path.exists())

    def test_wait_records_only_matching_worker_done_and_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "project").mkdir()
            fake_bin, log_path, state = self.make_fixture(root)
            saved = json.loads(state.read_text(encoding="utf-8"))
            saved["roles"] = {
                "worker": {
                    "task_id": "task_1",
                    "dispatch_id": "dispatch_1",
                    "terminal_handle": "term_worker",
                    "completion_observed": False,
                    "launcher_owned_terminal": True,
                }
            }
            state.write_text(json.dumps(saved), encoding="utf-8")
            process = self.start_server(fake_bin, log_path, state)
            try:
                response = self.call(
                    process,
                    "role_wait",
                    {"role": "worker", "timeout_ms": 5000},
                )
            finally:
                self.stop(process)
            after = json.loads(state.read_text(encoding="utf-8"))

        self.assertFalse(response["result"]["isError"])
        self.assertEqual(after["pending_delivery_id"], "delivery_1")
        self.assertTrue(after["roles"]["worker"]["completion_observed"])
        self.assertEqual(after["roles"]["worker"]["outcome"], "succeeded")

    def test_agent_ready_rejects_loading_and_requires_configured_model_effort(
        self,
    ) -> None:
        spec = {"provider": "codex", "model": "gpt-6-astra", "effort": "medium"}

        self.assertFalse(
            agent_team_mcp.agent_ready(
                "OpenAI Codex model: loading /model to change", spec
            )
        )
        self.assertFalse(
            agent_team_mcp.agent_ready("OpenAI Codex model: gpt-6-astra high", spec)
        )
        self.assertTrue(
            agent_team_mcp.agent_ready(
                "OpenAI Codex › task gpt-6-astra medium · ~/src/dotfiles", spec
            )
        )
        self.assertFalse(
            agent_team_mcp.agent_ready(
                "please use gpt-6-astra medium for this task", spec
            )
        )

    def test_wait_for_agent_ready_polls_loading_until_tui_footer_is_configured(
        self,
    ) -> None:
        state = {
            "role_specs": {
                "worker": {
                    "provider": "codex",
                    "model": "gpt-6-astra",
                    "effort": "medium",
                }
            }
        }
        loading = {"terminal": {"preview": "model: loading /model to change"}}
        ready = {
            "terminal": {
                "preview": "Summarize recent commits gpt-6-astra medium · ~/src/dotfiles"
            }
        }
        with (
            mock.patch.object(
                agent_team_mcp, "run_orca", side_effect=[loading, ready]
            ) as run,
            mock.patch.object(agent_team_mcp.time, "sleep"),
        ):
            agent_team_mcp.wait_for_agent_ready(
                state, "worker", "term_worker", timeout_ms=5_000
            )

        self.assertEqual(run.call_count, 2)

    def test_wait_for_agent_ready_times_out_while_model_is_loading(self) -> None:
        state = {
            "role_specs": {
                "worker": {
                    "provider": "codex",
                    "model": "gpt-6-astra",
                    "effort": "medium",
                }
            }
        }
        loading = {"terminal": {"preview": "model : loading /model to change"}}
        with (
            mock.patch.object(agent_team_mcp, "run_orca", return_value=loading),
            mock.patch.object(agent_team_mcp.time, "monotonic", side_effect=[0.0, 1.0]),
            mock.patch.object(agent_team_mcp.time, "sleep"),
            self.assertRaisesRegex(RuntimeError, "did not finish loading"),
        ):
            agent_team_mcp.wait_for_agent_ready(
                state, "worker", "term_worker", timeout_ms=500
            )


if __name__ == "__main__":
    unittest.main()
