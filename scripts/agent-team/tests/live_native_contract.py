"""Opt-in live contract tests for an explicitly selected native runtime.

Run with ``AGENT_TEAM_RUN_LIVE_NATIVE=1`` from the agent-team project, for
example::

    AGENT_TEAM_RUN_LIVE_NATIVE=1 AGENT_TEAM_LIVE_RUNTIME=tmux \
      uv run --project scripts/agent-team \
      python -m unittest scripts/agent-team/tests/live_native_contract.py -v

The test never uses a real provider.  It creates disposable Claude and native
ACP dependency fixtures, while the selected CLI is the live terminal
implementation.  The fake Node executable models the native client's
one-shot JSON receipt and process cleanup; the public SDK wire contract is
covered separately by ``test_scoped_acp_client.py``.
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
from agent_team.cli import _runtime_engine
from agent_team.native_backend import (
    NativeBackend,
    _remove_socket_root,
    _terminal_stop_is_owned,
    _validated_supervisor_argv,
)
from agent_team.native_terminal import NativeTerminalReceipt, is_native_runtime
from agent_team.process_identity import read_process_argv
from agent_team.runtime import (
    read_state,
    remove_prompt_file,
    remove_state_tree,
)

RUN_LIVE = os.environ.get("AGENT_TEAM_RUN_LIVE_NATIVE") == "1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable)
LIVE_RUNTIME = os.environ.get("AGENT_TEAM_LIVE_RUNTIME", "tmux")


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
        if not is_native_runtime(LIVE_RUNTIME):
            self.fail(f"unsupported explicit native runtime: {LIVE_RUNTIME}")
        self.runtime = LIVE_RUNTIME
        executable = shutil.which(self.runtime)
        if executable is None:
            self.fail(f"live native contract requires an installed {self.runtime}")
        self.terminal_executable = Path(executable).resolve()
        self.root = Path(tempfile.mkdtemp(prefix="agent-team-live-native-"))
        self.root.chmod(0o700)
        self.fake_bin = self.root / "fake-bin"
        self.node_bin = self.root / "node-bin"
        self.agent_bin = self.root / "agent-bin"
        self.python_bin = self.root / "python-bin"
        for directory in (
            self.fake_bin,
            self.node_bin,
            self.agent_bin,
            self.python_bin,
        ):
            directory.mkdir(mode=0o700)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.xdg_state = self.root / "xdg-state"
        self.xdg_state.mkdir(mode=0o700)
        self.acp_log = self.root / "acp.log"
        self.acp_prompt_started = self.root / "acp-prompt-started"
        self.acp_child_marker = self.root / "acp-child.pid"
        self.acp_descendant_marker = self.root / "acp-descendant.pid"
        self.main_marker = self.root / "main"
        self._install_fixtures()
        path_entries = (
            self.fake_bin,
            self.node_bin,
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
        for command in (
            "orca",
            "orca-ide",
            "codex",
            "opencode",
            "tmux",
            "zellij",
            "herdr",
            "acpx",
            "npm",
            "npx",
        ):
            if command == self.runtime:
                self.assertIsNotNone(
                    shutil.which(command, path=self.environment["PATH"])
                )
                continue
            self.assertIsNone(shutil.which(command, path=self.environment["PATH"]))
        self.assertIsNotNone(
            shutil.which("node", path=self.environment["PATH"]),
        )
        self.assertIsNotNone(
            shutil.which("claude-agent-acp", path=self.environment["PATH"]),
        )
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
        (self.fake_bin / self.runtime).symlink_to(self.terminal_executable)
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
while not marker.with_suffix('.exit').exists():
    time.sleep(0.1)
marker.with_suffix('.done').write_text('natural-exit', encoding='utf-8')
marker.with_suffix('.pid').unlink(missing_ok=True)
""",
        )
        _write_executable(
            self.node_bin / "node",
            f"""#!{PYTHON}
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

log = Path({str(self.acp_log)!r})
prompt_started = Path({str(self.acp_prompt_started)!r})
child_marker = Path({str(self.acp_child_marker)!r})
descendant_marker = Path({str(self.acp_descendant_marker)!r})

args = sys.argv[1:]
if args[:1] == ['--fixture-descendant']:
    marker = Path(args[1])
    pending = marker.with_name(marker.name + '.pending')
    pending.write_text(str(os.getpid()), encoding='ascii')
    pending.replace(marker)

    def stop_descendant(_signum, _frame):
        marker.unlink(missing_ok=True)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop_descendant)
    signal.signal(signal.SIGINT, stop_descendant)
    while True:
        time.sleep(0.1)

if args == ['--version']:
    print('v22.13.0')
    raise SystemExit(0)

pending = child_marker.with_name(child_marker.name + '.pending')
pending.write_text(str(os.getpid()), encoding='ascii')
pending.replace(child_marker)
child = None

def record(value):
    with log.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(value) + '\\n')

def stop(_signum, _frame):
    record(dict(event='client-stop'))
    if child is not None:
        try:
            child.terminate()
        except OSError:
            pass
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                child.kill()
            except OSError:
                pass
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
    child_marker.unlink(missing_ok=True)
    descendant_marker.unlink(missing_ok=True)
    raise SystemExit(0)

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
record(dict(event='client-start', argv=args))
prompt = sys.stdin.read()
record(dict(event='prompt', text=prompt))
if 'cancel-live-role' in prompt:
    child = subprocess.Popen(
        [sys.executable, __file__, '--fixture-descendant', str(descendant_marker)],
        start_new_session=False,
    )
    deadline = time.monotonic() + 5
    while not descendant_marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    prompt_started.write_text('started', encoding='utf-8')
    while True:
        time.sleep(0.1)

model = args[args.index('--model') + 1]
effort = args[args.index('--effort') + 1]
record(dict(event='client-complete'))
child_marker.unlink(missing_ok=True)
print(json.dumps(dict(
    output='fake native ACP planner output',
    session_id='fixture-session',
    model=model,
    effort=effort,
    cleanup_confirmed=True,
)))
""",
        )

        packages = self.root / "node_modules" / "@agentclientprotocol"
        agent_root = packages / "claude-agent-acp"
        agent_dist = agent_root / "dist"
        agent_dist.mkdir(mode=0o700, parents=True)
        (agent_root / "package.json").write_text(
            json.dumps(
                {
                    "name": "@agentclientprotocol/claude-agent-acp",
                    "version": "0.70.0",
                    "bin": {"claude-agent-acp": "dist/index.js"},
                    "dependencies": {"@agentclientprotocol/sdk": "1.3.0"},
                    "exports": {".": {"import": "./dist/lib.js"}},
                }
            ),
            encoding="utf-8",
        )
        _write_executable(
            agent_dist / "index.js",
            "#!/usr/bin/env node\nprocess.exit(0);\n",
        )
        (agent_dist / "lib.js").write_text(
            "export const fixtureAgentLibrary = true;\n", encoding="utf-8"
        )

        sdk_root = packages / "sdk"
        sdk_dist = sdk_root / "dist"
        sdk_dist.mkdir(mode=0o700, parents=True)
        (sdk_root / "package.json").write_text(
            json.dumps(
                {
                    "name": "@agentclientprotocol/sdk",
                    "version": "1.3.0",
                    "main": "dist/acp.js",
                    "exports": {
                        ".": {
                            "import": "./dist/acp.js",
                            "default": "./dist/acp.js",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        (sdk_dist / "acp.js").write_text(
            "export const fixtureSdk = true;\n", encoding="utf-8"
        )
        self.agent_root = agent_root
        self.agent_dist = agent_dist
        self.sdk_root = sdk_root
        self.sdk_entry = sdk_dist / "acp.js"
        (self.agent_bin / "claude-agent-acp").symlink_to(agent_dist / "index.js")

    def _config(self, workspace: Path) -> Path:
        (workspace / "main.md").write_text("fake Main", encoding="utf-8")
        (workspace / "planner.md").write_text("fake Planner", encoding="utf-8")
        config = workspace / "team.toml"
        config.write_text(
            f"""version = 3
runtime = "{self.runtime}"
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

    def _acp_events(self) -> list[dict[str, object]]:
        if not self.acp_log.is_file():
            return []
        events: list[dict[str, object]] = []
        for line in self.acp_log.read_text(encoding="utf-8").splitlines():
            value = json.loads(line)
            if not isinstance(value, dict):
                self.fail(f"native client fixture event is not an object: {line!r}")
            events.append(cast(dict[str, object], value))
        return events

    def _assert_native_fixture_binding(self, state: dict[str, object]) -> None:
        role_specs = state.get("role_specs")
        if not isinstance(role_specs, dict):
            self.fail("native state has no role_specs")
        planner = role_specs.get("planner")
        if not isinstance(planner, dict):
            self.fail("native state has no planner role spec")
        executables = planner.get("acp_executables")
        if not isinstance(executables, dict):
            self.fail("native planner has no ACP executable snapshot")
        self.assertEqual(
            set(executables),
            {
                "node",
                "agent",
                "sdk",
                "library",
                "node_sha256",
                "agent_sha256",
                "sdk_sha256",
                "library_sha256",
            },
        )
        self.assertEqual(
            Path(cast(str, executables["node"])), (self.node_bin / "node").resolve()
        )
        self.assertEqual(
            Path(cast(str, executables["agent"])),
            (self.agent_dist / "index.js").resolve(),
        )
        self.assertEqual(Path(cast(str, executables["sdk"])), self.sdk_entry.resolve())
        self.assertEqual(
            Path(cast(str, executables["library"])),
            (self.agent_dist / "lib.js").resolve(),
        )
        for name in (
            "node_sha256",
            "agent_sha256",
            "sdk_sha256",
            "library_sha256",
        ):
            digest = executables.get(name)
            self.assertIsInstance(digest, str)
            self.assertRegex(cast(str, digest), r"^[0-9a-f]{64}$")

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
            self._owned_state_paths.extend(self.xdg_state.rglob("state.json"))
            if not self._owned_state_paths:
                self._last_failure_details += " startup-failed-without-state"
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
        self._assert_native_fixture_binding(read_state(state_path))
        return workspace, state_path, cast(dict[str, object], payload)

    def _safe_cleanup(self, state_path: Path) -> None:
        self._safe_cleanup_acp_child()
        self._safe_cleanup_acp_descendant()
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
        try:
            backend = self._terminal_backend(state)
            receipt = backend._receipt_from_state(native)
            driver = backend._driver_from_receipt(receipt)
        except (RuntimeError, OSError, TypeError, ValueError) as exc:
            self._last_failure_details += f" terminal-receipt={exc!r}"
            return
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
                process = native.get("main_process")
                if isinstance(process, dict) and _terminal_stop_is_owned(
                    receipt, inspected, process
                ):
                    main_proven = (
                        isinstance(process, dict)
                        and process.get("phase") == "exited"
                        and process.get("group_stopped") is True
                    )
                    if isinstance(process, dict) and process.get("phase") == "running":
                        supervisor = process.get("supervisor_pid")
                        if supervisor == receipt.pane_pid:
                            if read_process_argv(
                                supervisor
                            ) != _validated_supervisor_argv(state):
                                self._last_failure_details += (
                                    " supervisor-identity-unproven"
                                )
                                return
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
                        self._last_failure_details += f" terminal-close={closed!r}"
                        return
                    _remove_socket_root(backend._socket_root(receipt))
                else:
                    self._last_failure_details += " terminal-ownership-unproven"
                    return
            except (RuntimeError, OSError, TypeError, ValueError) as exc:
                self._last_failure_details += f" terminal-cleanup={exc!r}"
                return
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

    def _safe_cleanup_acp_descendant(self) -> None:
        if not self.acp_descendant_marker.is_file():
            return
        try:
            pid = int(self.acp_descendant_marker.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            self._last_failure_details += " acp-descendant-marker-invalid"
            return
        argv = read_process_argv(pid)
        if argv is None or not any(str(self.root) in item for item in argv):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                self.acp_descendant_marker.unlink(missing_ok=True)
                return
            except OSError:
                return
            self._last_failure_details += f" acp-descendant-identity-unproven pid={pid}"
            return
        try:
            os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    self.acp_descendant_marker.unlink(missing_ok=True)
                    return
                time.sleep(0.05)
            os.kill(pid, signal.SIGKILL)
        except (OSError, ValueError) as exc:
            self._last_failure_details += f" acp-descendant-cleanup={exc!r}"

    def _terminal_backend(
        self, state: dict[str, object]
    ) -> NativeBackend[NativeTerminalReceipt]:
        if state.get("runtime") != self.runtime:
            raise ValueError("fixture state runtime differs from the selected runtime")
        _, backend = _runtime_engine(state, resume_existing=True)
        if not isinstance(backend, NativeBackend):
            raise TypeError("fixture runtime did not select a native backend")
        return cast(NativeBackend[NativeTerminalReceipt], backend)

    def _assert_owned_resources_gone(self, state: dict[str, object]) -> None:
        native = cast(dict[str, object], state["native"])
        backend = self._terminal_backend(state)
        receipt = backend._receipt_from_state(native)
        process = cast(dict[str, object], native["main_process"])
        for pid in (receipt.server_pid, receipt.pane_pid, process["agent_pid"]):
            self._wait_pid_gone(cast(int, pid))
        receipt_fields = receipt.as_dict()
        for path in (
            Path(cast(str, receipt_fields["socket_path"])),
            Path(cast(str, receipt_fields["config_path"])),
            backend._socket_root(receipt),
        ):
            self.assertFalse(path.exists(), str(path))
        for assignment in cast(dict[str, dict[str, object]], state["roles"]).values():
            for key in ("prompt_path", "provider_private_root", "snapshot_root"):
                self.assertFalse(Path(cast(str, assignment[key])).exists(), key)
        self.assertFalse(self.acp_child_marker.exists())
        self.assertFalse(self.acp_descendant_marker.exists())

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
                    "task_get",
                    "task_dispatch",
                    "task_verify",
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
            assigned_state = read_state(state_path)
            saved_assignment = cast(
                dict[str, object], assigned_state["roles"]["planner"]
            )
            assigned_paths = tuple(
                Path(cast(str, saved_assignment[key]))
                for key in ("prompt_path", "provider_private_root", "snapshot_root")
            )
            waited = mcp.tool("role_wait", {"role": "planner", "timeout_ms": 15_000})
            self.assertEqual(len(waited["events"]), 1)
            event = waited["events"][0]
            self.assertEqual(event["kind"], "worker_done")
            self.assertEqual(event["outcome"], "succeeded")
            read = mcp.tool("role_read", {"role": "planner", "lines": 20})
            self.assertIn("fake native ACP planner output", read["output"])
            released = mcp.tool("role_release", {"role": "planner"})
            self.assertEqual(released["state"], "released")
            for path in assigned_paths:
                self.assertFalse(path.exists(), str(path))
            delivery_id = cast(str, waited["delivery_id"])
            acknowledged = mcp.tool("delivery_ack", {"delivery_id": delivery_id})
            self.assertTrue(acknowledged["acknowledged"])
        finally:
            mcp.close()

        final_state = read_state(state_path)
        self.assertEqual(final_state["roles"], {})
        self.assertNotIn("native_result", final_state)
        events = self._acp_events()
        self.assertEqual(
            [entry.get("event") for entry in events],
            ["client-start", "prompt", "client-complete"],
        )
        client_argv = events[0].get("argv")
        self.assertIsInstance(client_argv, list)
        client_argv = cast(list[str], client_argv)
        self.assertEqual(client_argv.count("--sdk-entry"), 1)
        sdk_index = client_argv.index("--sdk-entry")
        self.assertEqual(client_argv[sdk_index + 1], str(self.sdk_entry.resolve()))
        self.assertNotIn("sessions", client_argv)
        self.assertNotIn("acpx", " ".join(client_argv))
        self.assertIn("normal live turn", cast(str, events[1]["text"]))
        stop = self._run_cli(
            ["stop", "--state", str(state_path), "--cwd", str(workspace)]
        )
        self.assertEqual(stop.returncode, 0, stop.stderr)
        self.assertFalse(state_path.exists())
        self._assert_owned_resources_gone(final_state)

    def test_natural_main_exit_can_be_stopped_without_original_config(self) -> None:
        workspace, state_path, _ = self._start("natural-exit")
        (workspace / "team.toml").unlink()
        (workspace / "main.md").unlink()
        (workspace / "planner.md").unlink()
        self.main_marker.with_suffix(".exit").write_text("exit", encoding="utf-8")
        final_state = self._wait_state(
            state_path,
            lambda state: (
                isinstance(state.get("native"), dict)
                and isinstance(state["native"].get("main_process"), dict)
                and state["native"]["main_process"].get("phase") == "exited"
                and state["native"]["main_process"].get("group_stopped") is True
            ),
        )
        process = cast(dict[str, object], final_state["native"]["main_process"])
        self._wait_pid_gone(cast(int, process["supervisor_pid"]))
        status = self._run_cli(
            ["status", "--state", str(state_path), "--cwd", str(workspace)]
        )
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(
            self.main_marker.with_suffix(".done").read_text(encoding="utf-8"),
            "natural-exit",
        )
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
            self.assertTrue(self.acp_child_marker.exists())
            self.assertTrue(self.acp_descendant_marker.exists())
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
            self.assertFalse(self.acp_child_marker.exists())
            self.assertFalse(self.acp_descendant_marker.exists())
        finally:
            mcp.close()
        if stop_succeeded and runner_pid is not None:
            self._wait_pid_gone(runner_pid)
            self._assert_owned_resources_gone(state)
            events = self._acp_events()
            self.assertIn("client-stop", [entry.get("event") for entry in events])
        if stop_succeeded and self.acp_child_marker.is_file():
            self._wait_pid_gone(
                int(self.acp_child_marker.read_text(encoding="ascii").strip())
            )


if __name__ == "__main__":
    unittest.main()
