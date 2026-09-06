"""Opt-in live contract tests for the native tmux runtime.

Run with ``AGENT_TEAM_RUN_LIVE_NATIVE=1`` from the agent-team project, for
example::

    AGENT_TEAM_RUN_LIVE_NATIVE=1 uv run --project scripts/agent-team \
      python -m unittest scripts/agent-team/tests/live_native_contract.py -v

The test never uses a real provider.  It creates disposable Claude, Node,
ACPX, and ACP adapter fixtures, while tmux itself is the selected live
terminal implementation.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import cast

from agent_team.adapters import remove_owned_tree
from agent_team.process_identity import read_process_argv
from agent_team.runtime import (
    read_state,
    remove_prompt_file,
    remove_state_tree,
)
from agent_team.tmux import (
    TmuxDriver,
    TmuxReceipt,
)

RUN_LIVE = os.environ.get("AGENT_TEAM_RUN_LIVE_NATIVE") == "1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable)
REAL_TMUX = shutil.which("tmux")


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o700)


def _json_state(path: Path) -> dict[str, object]:
    return cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))


def _pid_group(pid: int) -> int | str:
    try:
        return os.getpgid(pid)
    except OSError:
        return "unknown"


class _McpProcess:
    def __init__(self, *, env: dict[str, str], state_path: Path) -> None:
        process_env = dict(env)
        process_env["AGENT_TEAM_STATE_PATH"] = str(state_path)
        self.process = subprocess.Popen(
            [str(PYTHON), "-m", "agent_team", "_mcp-server"],
            cwd=PROJECT_ROOT,
            env=process_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._request_id = 0

    def request(
        self, method: str, params: dict[str, object] | None = None
    ) -> dict[str, object]:
        self._request_id += 1
        if self.process.stdin is None or self.process.stdout is None:
            raise AssertionError("MCP stdio pipes are unavailable")
        request: dict[str, object] = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
        }
        if params is not None:
            request["params"] = params
        self.process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            ready, _, _ = select.select([self.process.stdout], [], [], remaining)
            if not ready:
                continue
            line = self.process.stdout.readline()
            if not line:
                break
            response = json.loads(line)
            if isinstance(response, dict):
                return cast(dict[str, object], response)
        stderr = ""
        if self.process.stderr is not None:
            stderr = self.process.stderr.read(4_000)
        raise AssertionError(f"MCP response timed out; stderr={stderr!r}")

    def tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        response = self.request(
            "tools/call",
            {"name": name, "arguments": arguments},
        )
        result = response.get("result")
        if not isinstance(result, dict):
            raise TypeError(f"MCP tool response has no result: {response!r}")
        content = result.get("content")
        if (
            not isinstance(content, list)
            or not content
            or not isinstance(content[0], dict)
        ):
            raise AssertionError(f"MCP tool response has no content: {response!r}")
        text = content[0].get("text")
        if not isinstance(text, str):
            raise TypeError(f"MCP tool response text is invalid: {response!r}")
        if result.get("isError"):
            raise AssertionError(f"MCP tool returned an error: {text}")
        value = json.loads(text)
        if not isinstance(value, dict):
            raise TypeError(f"MCP tool payload is invalid: {response!r}")
        return cast(dict[str, object], value)

    def close(self) -> None:
        if self.process.stdin is not None:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=5.0)
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()


class LiveNativeContractTest(unittest.TestCase):
    def setUp(self) -> None:
        if not RUN_LIVE:
            self.fail("set AGENT_TEAM_RUN_LIVE_NATIVE=1 for the explicit live test")
        if REAL_TMUX is None:
            self.fail("live native contract requires an installed tmux")
        self.root = Path(tempfile.mkdtemp(prefix="agent-team-live-native-"))
        self.root.chmod(0o700)
        self.fake_bin = self.root / "fake-bin"
        self.node_bin = self.root / "node-bin"
        self.acpx_bin = self.root / "acpx-bin"
        self.agent_bin = self.root / "agent-bin"
        self.python_bin = self.root / "python-bin"
        for directory in (
            self.fake_bin,
            self.node_bin,
            self.acpx_bin,
            self.agent_bin,
            self.python_bin,
        ):
            directory.mkdir(mode=0o700)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.xdg_state = self.root / "xdg-state"
        self.xdg_state.mkdir(mode=0o700)
        self.acp_log = self.root / "acp.log"
        self.acp_sessions = self.root / "acp-sessions"
        self.acp_sessions.mkdir(mode=0o700)
        self.acp_prompt_started = self.root / "acp-prompt-started"
        self.acp_child_marker = self.root / "acp-child.pid"
        self.main_marker = self.root / "main"
        self._install_fixtures()
        path_entries = (
            self.fake_bin,
            self.node_bin,
            self.acpx_bin,
            self.agent_bin,
            self.python_bin,
            Path("/usr/bin"),
            Path("/bin"),
        )
        self.environment = {
            "PATH": os.pathsep.join(str(directory) for directory in path_entries),
            "HOME": str(self.home),
            "XDG_STATE_HOME": str(self.xdg_state),
            "LANG": "C",
            "TMPDIR": str(self.root),
        }
        for command in ("orca", "orca-ide", "codex", "opencode", "zellij", "herdr"):
            self.assertIsNone(shutil.which(command, path=self.environment["PATH"]))
        self._owned_state_paths: list[Path] = []
        self._last_failure_details = ""

    def tearDown(self) -> None:
        for state_path in self._owned_state_paths:
            self._safe_cleanup(state_path)
        remaining = [path for path in self._owned_state_paths if path.exists()]
        if remaining or self._last_failure_details:
            print(
                "LIVE_NATIVE_EVIDENCE_PRESERVED "
                f"root={self.root} remaining={remaining} "
                f"details={self._last_failure_details}",
                file=sys.stderr,
            )
            return
        shutil.rmtree(self.root)

    def _install_fixtures(self) -> None:
        if REAL_TMUX is None:
            raise AssertionError("tmux was checked in setUp")
        (self.fake_bin / "tmux").symlink_to(Path(REAL_TMUX).resolve())
        self.python_bin.joinpath("python3").symlink_to(PYTHON)
        _write_executable(
            self.fake_bin / "claude",
            f"""#!{PYTHON}
import json
import signal
import sys
import time
from pathlib import Path

marker = Path({str(self.main_marker)!r})
marker.parent.mkdir(mode=0o700, exist_ok=True)
marker.with_suffix('.argv').write_text(json.dumps(sys.argv), encoding='utf-8')
try:
    config_index = sys.argv.index('--mcp-config')
    config = json.loads(sys.argv[config_index + 1])
    state_path = config['mcpServers']['agent_team']['env']['AGENT_TEAM_STATE_PATH']
    marker.with_suffix('.state').write_text(state_path, encoding='utf-8')
except (ValueError, KeyError, IndexError, json.JSONDecodeError):
    marker.with_suffix('.state-error').write_text('missing mcp state', encoding='utf-8')
marker.with_suffix('.pid').write_text(str(__import__('os').getpid()), encoding='utf-8')

def stop(_signum, _frame):
    marker.with_suffix('.done').write_text('stopped', encoding='utf-8')
    marker.with_suffix('.pid').unlink(missing_ok=True)
    raise SystemExit(0)

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
while True:
    time.sleep(0.1)
""",
        )
        _write_executable(
            self.node_bin / "node",
            f"""#!{PYTHON}
import json
import sys
import time
from pathlib import Path

log = Path({str(self.acp_log)!r})
sessions = Path({str(self.acp_sessions)!r})
prompt_started = Path({str(self.acp_prompt_started)!r})
child_marker = Path({str(self.acp_child_marker)!r})
sessions.mkdir(mode=0o700, exist_ok=True)
child_marker.write_text(str(__import__('os').getpid()), encoding='utf-8')

def record(value):
    with log.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(value) + '\\n')

args = sys.argv[1:]
if args == ['--version']:
    print('v22.13.0')
    raise SystemExit(0)
record(args)
if 'sessions' in args:
    index = args.index('sessions')
    operation = args[index + 1] if index + 1 < len(args) else ''
    if operation == 'new':
        name = args[args.index('--name') + 1]
        (sessions / name).write_text('open', encoding='utf-8')
    elif operation == 'close':
        name = args[index + 2]
        (sessions / name).unlink(missing_ok=True)
    elif operation == 'prune':
        for entry in sessions.iterdir():
            entry.unlink()
elif 'prompt' in args:
    prompt = sys.stdin.read()
    record(['prompt-input', prompt])
    if 'cancel-live-role' in prompt:
        prompt_started.write_text('started', encoding='utf-8')
        time.sleep(60)
    print('fake ACP planner output')
""",
        )
        acpx_root = self.root / "acpx-package"
        acpx_root.mkdir(mode=0o700)
        (acpx_root / "bin").mkdir(mode=0o700)
        (acpx_root / "package.json").write_text(
            json.dumps(
                {
                    "name": "acpx",
                    "version": "0.13.2",
                    "bin": {"acpx": "bin/acpx"},
                }
            ),
            encoding="utf-8",
        )
        _write_executable(self.acpx_bin / "acpx", "#!/bin/sh\nexit 0\n")
        # The dependency resolver requires a regular file under the package
        # root; the executable selected in PATH is that exact file.
        (acpx_root / "bin" / "acpx").write_text(
            (self.acpx_bin / "acpx").read_text(encoding="utf-8"), encoding="utf-8"
        )
        (acpx_root / "bin" / "acpx").chmod(0o700)
        (self.acpx_bin / "acpx").unlink()
        (self.acpx_bin / "acpx").symlink_to(acpx_root / "bin" / "acpx")

        agent_root = self.root / "agent-package"
        agent_root.mkdir(mode=0o700)
        (agent_root / "bin").mkdir(mode=0o700)
        (agent_root / "package.json").write_text(
            json.dumps(
                {
                    "name": "@agentclientprotocol/claude-agent-acp",
                    "version": "0.70.0",
                    "bin": {"claude-agent-acp": "bin/claude-agent-acp"},
                }
            ),
            encoding="utf-8",
        )
        _write_executable(
            agent_root / "bin" / "claude-agent-acp", "#!/bin/sh\nexit 0\n"
        )
        (self.agent_bin / "claude-agent-acp").symlink_to(
            agent_root / "bin" / "claude-agent-acp"
        )

    def _config(self, workspace: Path) -> Path:
        (workspace / "main.md").write_text("fake Main", encoding="utf-8")
        (workspace / "planner.md").write_text("fake Planner", encoding="utf-8")
        config = workspace / "team.toml"
        config.write_text(
            """version = 3
runtime = "tmux"
team_prefix = "live"
max_review_rounds = 1

[main]
provider = "claude"
transport = "direct"
model = "fixture-model"
effort = "high"
permission = "orchestrator"
prompt = "main.md"

[roles.planner]
provider = "claude"
transport = "acp"
model = "fixture-model"
effort = "high"
permission = "read-only"
prompt = "planner.md"
""",
            encoding="utf-8",
        )
        return config

    def _run_cli(
        self,
        arguments: list[str],
        *,
        timeout: float = 20.0,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(PYTHON), "-m", "agent_team", *arguments],
            cwd=PROJECT_ROOT,
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )

    def _wait_state(
        self, path: Path, predicate: object, *, timeout: float = 15.0
    ) -> dict[str, object]:
        if not callable(predicate):
            raise TypeError("state predicate must be callable")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.is_file():
                state = _json_state(path)
                if predicate(state):
                    return state
            time.sleep(0.05)
        self.fail(f"timed out waiting for state: {path}")

    def _wait_pid_gone(self, pid: int, *, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        self.fail(f"owned fixture process remains after cleanup: pid={pid}")

    def _start(self, name: str) -> tuple[Path, Path, dict[str, object]]:
        workspace = self.root / name
        workspace.mkdir(mode=0o700)
        config = self._config(workspace)
        result = self._run_cli(
            [
                "start",
                "--config",
                str(config),
                "--cwd",
                str(workspace),
                "--no-attach",
            ]
        )
        if result.returncode != 0:
            self.fail(
                "native public start failed: "
                f"stdout={result.stdout!r} stderr={result.stderr!r}"
            )
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            self.fail(f"native public start returned invalid JSON: {result.stdout!r}")
        state_path = Path(cast(str, payload["state_path"]))
        self._owned_state_paths.append(state_path)
        self._wait_state(
            state_path,
            lambda state: (
                isinstance(state.get("native"), dict)
                and isinstance(state["native"].get("main_process"), dict)
                and state["native"]["main_process"].get("phase") == "running"
            ),
        )
        return workspace, state_path, cast(dict[str, object], payload)

    def _safe_cleanup(self, state_path: Path) -> None:
        self._safe_cleanup_acp_child()
        if not state_path.is_file():
            return
        try:
            state = read_state(state_path)
        except (RuntimeError, OSError, TypeError, ValueError) as exc:
            self._last_failure_details += f" state-read={exc!r}"
            return
        native = state.get("native")
        if not isinstance(native, dict):
            self._last_failure_details += " native-state-missing"
            return
        raw_receipt = native.get("tmux_receipt")
        receipt: TmuxReceipt | None = None
        driver: TmuxDriver | None = None
        if raw_receipt is not None:
            try:
                receipt = TmuxReceipt.from_dict(raw_receipt)
                driver = TmuxDriver.from_receipt(receipt)
            except (RuntimeError, OSError, TypeError, ValueError) as exc:
                self._last_failure_details += f" tmux-receipt={exc!r}"
        roles = state.get("roles")
        if isinstance(roles, dict):
            for raw_assignment in roles.values():
                if not isinstance(raw_assignment, dict):
                    continue
                runner_stopped = True
                pid = raw_assignment.get("runner_pid")
                pgid = raw_assignment.get("runner_process_group_id")
                argv = raw_assignment.get("runner_argv")
                runner_identity = (
                    read_process_argv(pid) if isinstance(pid, int) else None
                )
                if isinstance(pid, int):
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        pass
                    else:
                        runner_stopped = False
                if (
                    isinstance(pid, int)
                    and isinstance(pgid, int)
                    and pid == pgid
                    and isinstance(argv, list)
                    and runner_identity == tuple(argv)
                ):
                    try:
                        os.killpg(pgid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    deadline = time.monotonic() + 3.0
                    while time.monotonic() < deadline:
                        try:
                            os.killpg(pgid, 0)
                        except ProcessLookupError:
                            break
                        time.sleep(0.05)
                    else:
                        try:
                            os.killpg(pgid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        runner_stopped = True
                for key in ("provider_private_root", "snapshot_root"):
                    raw_root = raw_assignment.get(key)
                    if runner_stopped and isinstance(raw_root, str):
                        root = Path(raw_root)
                        try:
                            remove_owned_tree(root)
                        except (RuntimeError, OSError, TypeError, ValueError):
                            pass
                raw_prompt = raw_assignment.get("prompt_path")
                role = next(
                    (name for name, value in roles.items() if value is raw_assignment),
                    None,
                )
                nonce = raw_assignment.get("launch_nonce")
                if (
                    runner_stopped
                    and isinstance(raw_prompt, str)
                    and isinstance(role, str)
                    and isinstance(nonce, str)
                    and Path(raw_prompt).exists()
                ):
                    try:
                        remove_prompt_file(
                            Path(raw_prompt),
                            state_path.parent,
                            role=role,
                            launch_nonce=nonce,
                        )
                    except (RuntimeError, OSError, TypeError, ValueError):
                        pass
        if receipt is not None and driver is not None:
            try:
                inspected = driver.inspect(receipt)
                if inspected.identity_verified:
                    process = native.get("main_process")
                    main_proven = (
                        isinstance(process, dict)
                        and process.get("phase") == "exited"
                        and process.get("group_stopped") is True
                    )
                    if isinstance(process, dict) and process.get("phase") == "running":
                        supervisor = process.get("supervisor_pid")
                        if supervisor == receipt.pane_pid:
                            try:
                                os.kill(supervisor, signal.SIGTERM)
                            except ProcessLookupError:
                                pass
                            deadline = time.monotonic() + 8.0
                            while time.monotonic() < deadline:
                                current = _json_state(state_path)
                                current_native = current.get("native")
                                current_process = (
                                    current_native.get("main_process")
                                    if isinstance(current_native, dict)
                                    else None
                                )
                                if (
                                    isinstance(current_process, dict)
                                    and current_process.get("phase") == "exited"
                                    and current_process.get("group_stopped") is True
                                ):
                                    main_proven = True
                                    break
                                time.sleep(0.05)
                    if not main_proven:
                        self._last_failure_details += " main-cleanup-proof-unavailable"
                        return
                    closed = driver.close(receipt)
                    if not (
                        closed.ownership_verified
                        and closed.server_terminated
                        and closed.socket_removed
                    ):
                        self._last_failure_details += f" tmux-close={closed!r}"
            except (RuntimeError, OSError, TypeError, ValueError) as exc:
                self._last_failure_details += f" tmux-cleanup={exc!r}"
        try:
            remove_state_tree(state_path, state)
        except (RuntimeError, OSError, TypeError, ValueError) as exc:
            self._last_failure_details += f" state-cleanup={exc!r}"

    def _safe_cleanup_acp_child(self) -> None:
        if not self.acp_child_marker.is_file():
            return
        try:
            pid = int(self.acp_child_marker.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            self._last_failure_details += " acp-child-marker-invalid"
            return
        argv = read_process_argv(pid)
        if argv is None or not any(str(self.root) in item for item in argv):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            except OSError:
                return
            self._last_failure_details += f" acp-child-identity-unproven pid={pid}"
            return
        try:
            pgid = os.getpgid(pid)
            if pgid != pid:
                self._last_failure_details += (
                    f" acp-child-pgid-unexpected pid={pid} pgid={pgid}"
                )
                return
            os.killpg(pgid, signal.SIGTERM)
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                try:
                    os.killpg(pgid, 0)
                except ProcessLookupError:
                    return
                time.sleep(0.05)
            os.killpg(pgid, signal.SIGKILL)
        except (OSError, ValueError) as exc:
            self._last_failure_details += f" acp-child-cleanup={exc!r}"

    def _assert_owned_resources_gone(self, state: dict[str, object]) -> None:
        native = cast(dict[str, object], state["native"])
        receipt = TmuxReceipt.from_dict(native["tmux_receipt"])
        process = cast(dict[str, object], native["main_process"])
        for pid in (receipt.server_pid, receipt.pane_pid, process["agent_pid"]):
            self._wait_pid_gone(cast(int, pid))
        for path in (
            receipt.socket_path,
            receipt.config_path,
            receipt.socket_path.parent,
        ):
            self.assertFalse(path.exists(), str(path))
        for assignment in cast(dict[str, dict[str, object]], state["roles"]).values():
            for key in ("prompt_path", "provider_private_root", "snapshot_root"):
                self.assertFalse(Path(cast(str, assignment[key])).exists(), key)
        self.assertEqual(list(self.acp_sessions.iterdir()), [])

    def test_public_start_mcp_lifecycle_and_cross_process_stop(self) -> None:
        workspace, state_path, start_payload = self._start("normal")
        self.assertEqual(start_payload["status"], "running")
        status = self._run_cli(
            ["status", "--state", str(state_path), "--cwd", str(workspace)]
        )
        self.assertEqual(status.returncode, 0, status.stderr)
        status_payload = json.loads(status.stdout)
        self.assertEqual(status_payload["status"], "running")
        self.assertNotIn("orca_socket", status_payload)
        self.assertNotIn("worktree_id", status_payload)

        mcp = _McpProcess(env=self.environment, state_path=state_path)
        try:
            initialized = mcp.request("initialize", {"protocolVersion": "2025-06-18"})
            self.assertEqual(initialized["result"]["serverInfo"]["name"], "agent-team")
            listed = mcp.request("tools/list")
            tools = listed["result"]["tools"]
            self.assertEqual(
                {item["name"] for item in tools},
                {
                    "role_get",
                    "role_prompt",
                    "role_wait",
                    "role_read",
                    "role_release",
                    "delivery_ack",
                    "message_reply",
                },
            )
            assignment = mcp.tool(
                "role_prompt", {"role": "planner", "text": "normal live turn"}
            )
            self.assertEqual(assignment["role"], "planner")
            waited = mcp.tool("role_wait", {"role": "planner", "timeout_ms": 15_000})
            self.assertEqual(len(waited["events"]), 1)
            event = waited["events"][0]
            self.assertEqual(event["kind"], "worker_done")
            self.assertEqual(event["outcome"], "succeeded")
            read = mcp.tool("role_read", {"role": "planner", "lines": 20})
            self.assertIn("fake ACP planner output", read["output"])
            released = mcp.tool("role_release", {"role": "planner"})
            self.assertEqual(released["state"], "released")
            delivery_id = cast(str, waited["delivery_id"])
            acknowledged = mcp.tool("delivery_ack", {"delivery_id": delivery_id})
            self.assertTrue(acknowledged["acknowledged"])
        finally:
            mcp.close()

        final_state = read_state(state_path)
        self.assertEqual(final_state["roles"], {})
        self.assertNotIn("native_result", final_state)
        self.assertEqual(list(self.acp_sessions.iterdir()), [])
        acp_log = self.acp_log.read_text(encoding="utf-8")
        self.assertIn("sessions", acp_log)
        self.assertIn("prompt-input", acp_log)
        self.assertIn("fake ACP", read["output"])
        stop = self._run_cli(
            ["stop", "--state", str(state_path), "--cwd", str(workspace)]
        )
        self.assertEqual(stop.returncode, 0, stop.stderr)
        self.assertFalse(state_path.exists())
        self._assert_owned_resources_gone(final_state)

    def test_active_role_stop_from_different_cli_process(self) -> None:
        workspace, state_path, _ = self._start("cancel")
        mcp = _McpProcess(env=self.environment, state_path=state_path)
        stop_succeeded = False
        runner_pid: int | None = None
        try:
            mcp.tool("role_prompt", {"role": "planner", "text": "cancel-live-role"})
            state = self._wait_state(
                state_path,
                lambda item: (
                    isinstance(item.get("roles"), dict)
                    and isinstance(item["roles"].get("planner"), dict)
                    and isinstance(item["roles"]["planner"].get("runner_pid"), int)
                ),
            )
            assignment = cast(dict[str, object], state["roles"]["planner"])
            runner_pid = cast(int, assignment["runner_pid"])
            runner_pgid = cast(int, assignment["runner_process_group_id"])
            self._wait_state(
                state_path,
                lambda _item: self.acp_prompt_started.exists(),
            )
            stop = self._run_cli(
                ["stop", "--state", str(state_path), "--cwd", str(workspace)],
                timeout=20.0,
            )
            if stop.returncode != 0:
                current = _json_state(state_path)
                native_state = current.get("native")
                process = (
                    native_state.get("main_process")
                    if isinstance(native_state, dict)
                    else None
                )
                child_pid = (
                    self.acp_child_marker.read_text(encoding="ascii").strip()
                    if self.acp_child_marker.is_file()
                    else "missing"
                )
                child_argv = (
                    read_process_argv(int(child_pid)) if child_pid.isdigit() else None
                )
                self._last_failure_details = (
                    f"cancel-stop failed state={state_path} "
                    f"runner_pid={runner_pid} runner_pgid={runner_pgid} "
                    f"runner_argv={assignment.get('runner_argv')!r} "
                    f"main_process={process!r} "
                    f"acp_child_pid={child_pid} acp_child_pgid="
                    f"{_pid_group(int(child_pid)) if child_pid.isdigit() else 'unknown'} "
                    f"acp_child_argv={child_argv!r} stderr={stop.stderr!r}"
                )
                self.fail(self._last_failure_details)
            stop_succeeded = True
            self.assertFalse(state_path.exists())
            self.assertFalse(self.main_marker.with_suffix(".pid").exists())
        finally:
            mcp.close()
        if stop_succeeded and runner_pid is not None:
            self._wait_pid_gone(runner_pid)
            self._assert_owned_resources_gone(state)
        if stop_succeeded and self.acp_child_marker.is_file():
            self._wait_pid_gone(
                int(self.acp_child_marker.read_text(encoding="ascii").strip())
            )


if __name__ == "__main__":
    unittest.main()
