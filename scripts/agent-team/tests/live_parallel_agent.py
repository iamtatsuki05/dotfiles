"""Opt-in live acceptance tests for native ``agent/parallel`` Main delegation.

The fixture composes :class:`live_native_contract.LiveNativeContractTest` for
the real terminal lifecycle and reuses the bounded fake ACP provider from
``live_parallel_program``.  The ``claude`` executable installed here is a
small fake Main: it parses the launch-provided MCP config, starts that config's
``_mcp-server`` command as a child, and sends every orchestration request over
the child's JSON-RPC stdio connection.  The host test only observes state,
provider events, and the Main/MCP transcript; it never sends a tool request on
Main's behalf.

The provider and model remain disposable fakes.  A selected terminal is live
only when both explicit gates and ``AGENT_TEAM_LIVE_RUNTIME`` are set::

    AGENT_TEAM_RUN_LIVE_NATIVE=1 AGENT_TEAM_RUN_LIVE_AGENT_PARALLEL=1 \\
      AGENT_TEAM_LIVE_RUNTIME=tmux \\
      uv run --locked --project scripts/agent-team \\
      python -m unittest discover -s scripts/agent-team/tests \\
      -p live_parallel_agent.py -v
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import cast

import live_native_contract as _live_native_contract
from live_parallel_program import _program_config, _write_parallel_provider

from agent_team.cli import ACP_TIMEOUT_SECONDS
from agent_team.native_acp_dependencies import NativeAcpExecutables
from agent_team.native_backend import _validated_supervisor_argv
from agent_team.process_identity import python_process_argv, read_process_argv
from agent_team.scoped_acp import client_argv
from agent_team.workspace_revision import snapshot_revision

_RUN_NATIVE = os.environ.get("AGENT_TEAM_RUN_LIVE_NATIVE") == "1"
_RUN_AGENT_PARALLEL = os.environ.get("AGENT_TEAM_RUN_LIVE_AGENT_PARALLEL") == "1"
_RUNTIME = os.environ.get("AGENT_TEAM_LIVE_RUNTIME")
_RUNTIMES = frozenset({"tmux", "herdr", "zellij"})


def _write_agent_main(path: Path, root: Path) -> None:
    """Install a fake Main that uses only the launch-provided MCP endpoint."""

    source = (
        textwrap.dedent(
            """
        #!__PYTHON__
        import json
        import os
        import signal
        import subprocess
        import sys
        import time
        from pathlib import Path

        ROOT = Path(__ROOT__)
        EVENTS = ROOT / "main-events.jsonl"
        TRANSCRIPT = ROOT / "main-mcp-transcript.jsonl"
        MAIN_MARKER = ROOT / "main"
        STOP_REQUESTED = False
        SERVER = None
        REQUEST_ID = 0
        TASKS = {}


        def _append(path, value):
            pending = path.with_name(path.name + ".pending")
            with pending.open("w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, sort_keys=True)
                stream.write("\\n")
                stream.flush()
                os.fsync(stream.fileno())
            with path.open("a", encoding="utf-8") as stream:
                stream.write(pending.read_text(encoding="utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            pending.unlink(missing_ok=True)


        def _identity():
            try:
                pgid = os.getpgid(0)
            except OSError:
                pgid = None
            mcp_pid = SERVER.pid if SERVER is not None else None
            mcp_pgid = None
            if SERVER is not None and SERVER.poll() is None:
                try:
                    mcp_pgid = os.getpgid(SERVER.pid)
                except OSError:
                    mcp_pgid = None
            return {
                "main_pid": os.getpid(),
                "main_ppid": os.getppid(),
                "main_pgid": pgid,
                "mcp_pid": mcp_pid,
                "mcp_ppid": os.getpid() if SERVER is not None else None,
                "mcp_pgid": mcp_pgid,
            }


        def _event(event, **fields):
            value = {"event": event, **_identity(), **fields}
            _append(EVENTS, value)


        def _transcript(kind, **fields):
            value = {"kind": kind, **_identity(), **fields}
            _append(TRANSCRIPT, value)


        def _stop(signum, _frame):
            global STOP_REQUESTED
            STOP_REQUESTED = True
            try:
                _event("main-stop", signal=signum)
            except OSError:
                pass


        def _argument(name):
            try:
                return sys.argv[sys.argv.index(name) + 1]
            except (ValueError, IndexError):
                return None


        def _read_response(request_id):
            if SERVER is None or SERVER.stdout is None:
                raise RuntimeError("fake Main MCP stdout is unavailable")
            while True:
                line = SERVER.stdout.readline()
                if not line:
                    raise RuntimeError("fake Main MCP server closed stdout")
                response = json.loads(line)
                if not isinstance(response, dict):
                    raise RuntimeError("fake Main MCP response is not an object")
                _transcript(
                    "response",
                    request_id=request_id,
                    response=response,
                )
                if response.get("id") == request_id:
                    return response


        def _rpc(method, params=None, *, tool_name=None, arguments=None):
            global REQUEST_ID
            if STOP_REQUESTED:
                raise RuntimeError("fake Main received stop before tool call")
            if SERVER is None or SERVER.stdin is None:
                raise RuntimeError("fake Main MCP stdin is unavailable")
            REQUEST_ID += 1
            request = {
                "jsonrpc": "2.0",
                "id": REQUEST_ID,
                "method": method,
            }
            if params is not None:
                request["params"] = params
            SERVER.stdin.write(json.dumps(request, ensure_ascii=False) + "\\n")
            SERVER.stdin.flush()
            fields = {
                "request_id": REQUEST_ID,
                "method": method,
            }
            if tool_name is not None:
                fields["tool_name"] = tool_name
            if arguments is not None:
                fields["arguments"] = arguments
            _transcript("request", **fields)
            response = _read_response(REQUEST_ID)
            return response


        def _notification(method, params=None):
            if SERVER is None or SERVER.stdin is None:
                raise RuntimeError("fake Main MCP stdin is unavailable")
            request = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                request["params"] = params
            SERVER.stdin.write(json.dumps(request, ensure_ascii=False) + "\\n")
            SERVER.stdin.flush()
            _transcript(
                "request",
                request_id=None,
                method=method,
                notification=True,
            )


        def _tool(name, arguments):
            response = _rpc(
                "tools/call",
                {"name": name, "arguments": arguments},
                tool_name=name,
                arguments=arguments,
            )
            result = response.get("result")
            if not isinstance(result, dict):
                raise RuntimeError("fake Main MCP response has no result")
            content = result.get("content")
            if (
                not isinstance(content, list)
                or not content
                or not isinstance(content[0], dict)
                or not isinstance(content[0].get("text"), str)
            ):
                raise RuntimeError("fake Main MCP result has no text")
            text = content[0]["text"]
            if result.get("isError"):
                raise RuntimeError(text)
            value = json.loads(text)
            if not isinstance(value, dict):
                raise RuntimeError("fake Main tool result is not an object")
            return value


        def _drain(role):
            waited = _tool("role_wait", {"role": role, "timeout_ms": 900000})
            delivery_id = waited.get("delivery_id")
            if not isinstance(delivery_id, str):
                raise RuntimeError("role_wait did not return a delivery_id")
            read = _tool("role_read", {"role": role, "lines": 400})
            released = _tool("role_release", {"role": role})
            acknowledged = _tool("delivery_ack", {"delivery_id": delivery_id})
            _event(
                "role-drained",
                role=role,
                delivery_id=delivery_id,
                read_receipt=read,
                release_receipt=released,
                ack_receipt=acknowledged,
            )
            return waited


        def _close_server():
            if SERVER is None:
                return True
            graceful = True
            if SERVER.stdin is not None:
                try:
                    SERVER.stdin.close()
                except OSError:
                    pass
            try:
                SERVER.wait(timeout=5)
            except subprocess.TimeoutExpired:
                graceful = False
                _event("mcp-graceful-timeout")
                try:
                    SERVER.terminate()
                except OSError:
                    pass
                try:
                    SERVER.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        SERVER.kill()
                    except OSError:
                        pass
                    SERVER.wait(timeout=5)
            _event(
                "mcp-exit",
                returncode=SERVER.returncode,
                graceful=graceful,
            )
            MAIN_MARKER.with_suffix(".mcp-pid").unlink(missing_ok=True)
            return graceful and SERVER.returncode == 0


        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        MAIN_MARKER.with_suffix(".argv").write_text(
            json.dumps(sys.argv), encoding="utf-8"
        )
        MAIN_MARKER.with_suffix(".pid").write_text(str(os.getpid()), encoding="ascii")
        config_text = _argument("--mcp-config")
        if config_text is None:
            raise RuntimeError("fake Main launch omitted --mcp-config")
        config = json.loads(config_text)
        server_config = config["mcpServers"]["agent_team"]
        command = server_config["command"]
        arguments = server_config["args"]
        environment = os.environ.copy()
        environment.update(server_config.get("env", {}))
        state_path = environment.get("AGENT_TEAM_STATE_PATH")
        if not isinstance(state_path, str) or not state_path:
            raise RuntimeError("fake Main MCP config omitted AGENT_TEAM_STATE_PATH")
        MAIN_MARKER.with_suffix(".state").write_text(state_path, encoding="utf-8")
        MAIN_MARKER.with_suffix(".mcp-config.json").write_text(
            json.dumps(config, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        state = json.loads(Path(state_path).read_text(encoding="utf-8"))
        TASKS = {
            task["task_id"]: task
            for task in state["task_specs"]
            if isinstance(task, dict) and isinstance(task.get("task_id"), str)
        }
        _event(
            "main-ready-gate",
            command=command,
            arguments=arguments,
            state_path=state_path,
        )
        while not (ROOT / "release-main").is_file():
            if STOP_REQUESTED:
                raise RuntimeError("fake Main stopped before MCP ready gate")
            time.sleep(0.02)
        SERVER = subprocess.Popen(
            [command, *arguments],
            cwd=Path.cwd(),
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        MAIN_MARKER.with_suffix(".mcp-pid").write_text(
            str(SERVER.pid), encoding="ascii"
        )
        _event(
            "mcp-start",
            command=command,
            arguments=arguments,
            state_path=state_path,
        )

        exit_code = 0
        try:
            tools_arg = _argument("--tools")
            if not isinstance(tools_arg, str):
                raise RuntimeError("fake Main launch omitted --tools")
            allowed_index = sys.argv.index("--allowedTools")
            permission_index = sys.argv.index("--permission-mode", allowed_index)
            allowed_tools = sys.argv[allowed_index + 1 : permission_index]
            required_tool = "mcp__agent_team__task_batch_open"
            if required_tool not in tools_arg.split(",") or required_tool not in allowed_tools:
                raise RuntimeError(
                    "fake Main launch did not allow task_batch_open in both tool lists"
                )
            initialize_response = _rpc(
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "agent-team-live-fixture-main",
                        "version": "1",
                    },
                },
            )
            initialize_result = initialize_response.get("result")
            if not isinstance(initialize_result, dict) or not isinstance(
                initialize_result.get("protocolVersion"), str
            ):
                raise RuntimeError("fake Main MCP initialize response is invalid")
            _notification("notifications/initialized")
            tools_list_response = _rpc("tools/list")
            tools_result = tools_list_response.get("result")
            if not isinstance(tools_result, dict):
                raise RuntimeError("fake Main tools/list response has no result")
            advertised = tools_result.get("tools")
            if not isinstance(advertised, list) or not any(
                isinstance(tool, dict) and tool.get("name") == "task_batch_open"
                for tool in advertised
            ):
                raise RuntimeError("fake Main MCP catalog omitted task_batch_open")
            batch = _tool("task_batch_open", {"task_ids": ["task-b", "task-a"]})
            _event("batch-opened", receipt=batch)
            for role, task_id in (("worker-a", "task-a"), ("worker-b", "task-b")):
                _tool(
                    "task_dispatch",
                    {
                        "role": role,
                        "task": TASKS[task_id],
                        "message": "Work only within the declared TaskSpec scope.",
                    },
                )
                _event("writer-dispatched", role=role, task_id=task_id)

            scenario_question = (ROOT / "question-scenario").is_file()
            if scenario_question:
                question_wait = _tool(
                    "role_wait", {"role": "worker-a", "timeout_ms": 900000}
                )
                question_events = question_wait.get("events")
                if (
                    not isinstance(question_events, list)
                    or len(question_events) != 1
                    or not isinstance(question_events[0], dict)
                    or question_events[0].get("kind") != "question"
                ):
                    raise RuntimeError(
                        "fake Main worker-a wait did not return exactly one question"
                    )
                question_delivery = question_wait.get("delivery_id")
                question_message = question_events[0].get("message_id")
                if not isinstance(question_delivery, str) or not isinstance(
                    question_message, str
                ):
                    raise RuntimeError("fake Main question delivery identity is invalid")
                _event(
                    "question-observed",
                    role="worker-a",
                    delivery_id=question_delivery,
                    message_id=question_message,
                )
                _drain("worker-b")
                _event("question-peer-drained", role="worker-b", task_id="task-b")
                marker = ROOT / "question-ready"
                marker.write_text("worker-b read-release-ack complete\\n", encoding="utf-8")
                while not STOP_REQUESTED:
                    time.sleep(0.05)
                _event("question-stop-preserved")
            else:
                _drain("worker-a")
                _drain("worker-b")
                _event("writers-drained")
                for role, task_id in (("reviewer-a", "task-a"), ("reviewer-b", "task-b")):
                    _tool(
                        "task_dispatch",
                        {
                            "role": role,
                            "task": TASKS[task_id],
                            "message": "Review the exact writer output at the sealed revision.",
                        },
                    )
                    _event("reviewer-dispatched", role=role, task_id=task_id)
                _drain("reviewer-a")
                _drain("reviewer-b")
                _event("reviewers-drained")
                for task_id in ("task-a", "task-b"):
                    verified = _tool("task_verify", {"task_id": task_id})
                    _event("task-verified", task_id=task_id, receipt=verified)
                _event("main-complete", task_ids=["task-a", "task-b"])
        except BaseException as exc:
            exit_code = 1
            _event("main-error", error=repr(exc))
        finally:
            try:
                mcp_closed = _close_server()
                if exit_code == 0 and not STOP_REQUESTED and not mcp_closed:
                    exit_code = 1
                    _event("main-error", error="MCP child did not exit cleanly")
            except BaseException as exc:
                exit_code = 1
                _event("main-error", error=repr(exc))
            _event("main-exit", returncode=exit_code)
            MAIN_MARKER.with_suffix(".pid").unlink(missing_ok=True)
        raise SystemExit(exit_code)
        """
        )
        .lstrip("\n")
        .replace("__PYTHON__", str(sys.executable))
        .replace("__ROOT__", repr(str(root)))
    )
    if not source.startswith("#!"):
        raise RuntimeError("fake Main executable must start with a kernel shebang")
    path.write_bytes(source.encode("utf-8"))
    path.chmod(0o700)


def _agent_config(root: Path, runtime: str) -> tuple[Path, Path, dict[str, list[str]]]:
    """Build the fixed named agent/parallel graph and disposable workspace."""

    workspace, config, verification = _program_config(root, runtime)
    (root / "main.md").write_text("fixture Main instructions\n", encoding="utf-8")

    task_blocks: list[str] = []
    for task_id, allowed in (("task-a", "src/a"), ("task-b", "src/b")):
        argv = ", ".join(json.dumps(item) for item in verification[task_id])
        task_blocks.append(
            "\n".join(
                (
                    "[[teams.agent.tasks]]",
                    f'task_id = "{task_id}"',
                    f'objective = "Complete {task_id} in its declared scope."',
                    'acceptance_criteria = ["the fixture task reaches completed"]',
                    f'allowed_paths = ["{allowed}"]',
                    'forbidden_paths = [".secrets"]',
                    "dependencies = []",
                    'evidence_requirements = ["record the fixed verification command"]',
                    'consultation_conditions = ["ask before leaving the declared scope"]',
                    "",
                    "[[teams.agent.tasks.verification]]",
                    'name = "fixed-success"',
                    f"argv = [{argv}]",
                    "timeout_seconds = 10",
                )
            )
        )

    node_blocks = [
        '''[[teams.agent.nodes]]
id = "main"
label = "main"
kind = "main"

[teams.agent.nodes.role_spec]
provider = "claude"
transport = "direct"
model = "fixture-main"
effort = "high"
prompt = "main.md"
permission = "orchestrator"'''
    ]
    for node_id, kind, model, effort, permission in (
        ("worker-a", "worker", "fixture-worker-a", "high", "workspace-write"),
        ("worker-b", "worker", "fixture-worker-b", "medium", "workspace-write"),
        ("reviewer-a", "reviewer", "fixture-reviewer-a", "low", "read-only"),
        ("reviewer-b", "reviewer", "fixture-reviewer-b", "high", "read-only"),
    ):
        node_blocks.append(
            "\n".join(
                (
                    "[[teams.agent.nodes]]",
                    f'id = "{node_id}"',
                    f'label = "{node_id}"',
                    f'kind = "{kind}"',
                    "",
                    "[teams.agent.nodes.role_spec]",
                    'provider = "claude"',
                    'transport = "acp"',
                    f'model = "{model}"',
                    f'effort = "{effort}"',
                    f'prompt = "program-prompts/{node_id}.md"',
                    f'permission = "{permission}"',
                )
            )
        )

    config.write_text(
        "\n".join(
            (
                "version = 5",
                f'runtime = "{runtime}"',
                "",
                "[teams.agent]",
                'name = "Fixture Agent Parallel"',
                "max_review_rounds = 1",
                "",
                *node_blocks,
                "",
                "[[teams.agent.edges]]",
                'source = "main"',
                'target = "worker-a"',
                'kind = "delegates-to"',
                "",
                "[[teams.agent.edges]]",
                'source = "main"',
                'target = "worker-b"',
                'kind = "delegates-to"',
                "",
                "[[teams.agent.edges]]",
                'source = "worker-a"',
                'target = "reviewer-a"',
                'kind = "reviewed-by"',
                "",
                "[[teams.agent.edges]]",
                'source = "worker-b"',
                'target = "reviewer-b"',
                'kind = "reviewed-by"',
                "",
                "[teams.agent.coordination]",
                'mode = "agent"',
                'entry_nodes = ["main"]',
                'dispatch_mode = "parallel"',
                "max_active = 2",
                "",
                *task_blocks,
                "",
                "[[teams.agent.routes]]",
                'task_id = "task-a"',
                'implementation_writer = "worker-a"',
                'implementation_reviewer = "reviewer-a"',
                "",
                "[[teams.agent.routes]]",
                'task_id = "task-b"',
                'implementation_writer = "worker-b"',
                'implementation_reviewer = "reviewer-b"',
                "",
            )
        ),
        encoding="utf-8",
    )
    return workspace, config, verification


class LiveParallelAgentContractTest(unittest.TestCase):
    """Run the bounded real-terminal agent/parallel acceptance contract."""

    maxDiff = None

    def setUp(self) -> None:
        if not _RUN_NATIVE:
            self.fail("set AGENT_TEAM_RUN_LIVE_NATIVE=1 for the explicit live test")
        if not _RUN_AGENT_PARALLEL:
            self.fail(
                "set AGENT_TEAM_RUN_LIVE_AGENT_PARALLEL=1 for the explicit live test"
            )
        if _RUNTIME not in _RUNTIMES:
            self.fail(
                "set AGENT_TEAM_LIVE_RUNTIME explicitly to tmux, herdr, or zellij"
            )
        fixture = _live_native_contract.LiveNativeContractTest(methodName="runTest")
        fixture.setUp()
        self.fixture = fixture
        self._fixture_ready = True
        self._state_paths: dict[Path, Path] = {}
        self._verified_clean_runs: set[Path] = set()
        self._preserve_fixture_root = False
        self.evidence: list[dict[str, object]] = []
        self.addCleanup(self._tear_down_agent)
        _write_parallel_provider(fixture.node_bin / "node", fixture.root)
        _write_agent_main(fixture.fake_bin / "claude", fixture.root)
        git = shutil.which("git")
        if git is None:
            self.fail("agent parallel fixture requires git for workspace revisions")
        (fixture.fake_bin / "git").symlink_to(git)

    def _events(self) -> list[dict[str, object]]:
        return self._read_jsonl(
            self.fixture.root / "parallel-provider-events.jsonl", 128
        )

    def _main_events(self) -> list[dict[str, object]]:
        return self._read_jsonl(self.fixture.root / "main-events.jsonl", 128)

    def _transcript(self) -> list[dict[str, object]]:
        return self._read_jsonl(self.fixture.root / "main-mcp-transcript.jsonl", 256)

    def _read_jsonl(self, path: Path, maximum: int) -> list[dict[str, object]]:
        if not path.is_file():
            return []
        raw = path.read_text(encoding="utf-8")
        records = raw.splitlines(keepends=True)
        if records and not records[-1].endswith("\n"):
            active = (
                self._provider_writer_active()
                if path.name == "parallel-provider-events.jsonl"
                else self.fixture.main_marker.with_suffix(".pid").exists()
            )
            if active:
                records.pop()
            else:
                self.fail(f"unterminated fixture evidence record: {path}")
        if len(records) > maximum:
            self.fail(f"bounded fixture evidence exceeded {maximum} records: {path}")
        values: list[dict[str, object]] = []
        for record in records:
            if not record.endswith("\n"):
                self.fail(f"fixture evidence record is not newline terminated: {path}")
            value = json.loads(record[:-1])
            if not isinstance(value, dict):
                self.fail(f"fixture evidence record is not an object: {path}")
            values.append(cast(dict[str, object], value))
        return values

    def _provider_writer_active(self) -> bool:
        active_dir = self.fixture.root / "provider-active"
        for marker in active_dir.glob("*.active"):
            try:
                pid = int(marker.read_text(encoding="ascii"))
                os.kill(pid, 0)
            except (OSError, ValueError):
                continue
            return True
        return False

    def _wait_events(
        self,
        predicate: Callable[[list[dict[str, object]]], bool],
        *,
        timeout: float = 60.0,
    ) -> list[dict[str, object]]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            events = self._events()
            if predicate(events):
                return events
            time.sleep(0.05)
        self.fail(f"timed out waiting for provider evidence: {self._events()!r}")

    def _wait_main(
        self,
        predicate: Callable[[list[dict[str, object]]], bool],
        *,
        timeout: float = 60.0,
    ) -> list[dict[str, object]]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            events = self._main_events()
            if predicate(events):
                return events
            time.sleep(0.05)
        self.fail(f"timed out waiting for Main evidence: {self._main_events()!r}")

    def _wait_transcript(
        self,
        predicate: Callable[[list[dict[str, object]]], bool],
        *,
        timeout: float = 60.0,
    ) -> list[dict[str, object]]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            records = self._transcript()
            if predicate(records):
                return records
            time.sleep(0.05)
        self.fail(f"timed out waiting for Main MCP transcript: {self._transcript()!r}")

    @staticmethod
    def _workspace_fingerprint(workspace: Path) -> str:
        digest = hashlib.sha256()
        for path in sorted(item for item in workspace.rglob("*") if item.is_file()):
            digest.update(str(path.relative_to(workspace)).encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
        return digest.hexdigest()

    def _snapshot(
        self, state_path: Path, label: str, workspace: Path
    ) -> dict[str, object]:
        state = cast(
            dict[str, object], json.loads(state_path.read_text(encoding="utf-8"))
        )
        raw_roles = state.get("roles")
        roles: dict[str, object] = {}
        if isinstance(raw_roles, dict):
            for role, raw in raw_roles.items():
                if not isinstance(role, str) or not isinstance(raw, dict):
                    continue
                spec = raw.get("task_spec")
                roles[role] = {
                    "task_id": spec.get("task_id") if isinstance(spec, dict) else None,
                    "dispatch_id": raw.get("dispatch_id"),
                    "launch_nonce": raw.get("launch_nonce"),
                    "runner_pid": raw.get("runner_pid"),
                    "runner_process_group_id": raw.get("runner_process_group_id"),
                    "runner_argv": raw.get("runner_argv"),
                    "task_revision": raw.get("task_revision"),
                    "native_question": raw.get("native_question"),
                    "native_result": raw.get("native_result"),
                    "pending_delivery_id": raw.get("pending_delivery_id"),
                    "pending_delivery_kind": raw.get("pending_delivery_kind"),
                    "pending_delivery_stage": raw.get("pending_delivery_stage"),
                    "provider_private_root": raw.get("provider_private_root"),
                    "snapshot_root": raw.get("snapshot_root"),
                    "prompt_path": raw.get("prompt_path"),
                    "question_socket": raw.get("question_socket"),
                }
        tasks: dict[str, object] = {}
        raw_tasks = state.get("tasks")
        if isinstance(raw_tasks, dict):
            for task_id, raw in raw_tasks.items():
                if isinstance(task_id, str) and isinstance(raw, dict):
                    tasks[task_id] = {
                        "status": raw.get("status"),
                        "revision": raw.get("revision"),
                        "dispatch_id": raw.get("dispatch_id"),
                        "task_evidence": raw.get("task_evidence"),
                        "review_result": raw.get("review_result"),
                        "verification": raw.get("verification"),
                    }
        native = state.get("native")
        native_summary = native if isinstance(native, dict) else {}
        evidence = {
            "label": label,
            "fixture_root": str(self.fixture.root),
            "state_path": str(state_path),
            "workspace": str(workspace),
            "workspace_fingerprint": self._workspace_fingerprint(workspace),
            "workspace_revision": snapshot_revision(workspace),
            "team_id": state.get("team_id"),
            "run_id": state.get("run_id"),
            "graph": state.get("graph"),
            "agent_batch": state.get("agent_batch"),
            "native": native_summary,
            "roles": roles,
            "tasks": tasks,
            "main_events": self._main_events(),
            "main_transcript": self._transcript(),
            "provider_events": self._events(),
        }
        if len(self.evidence) >= 64:
            self.fail("agent parallel evidence exceeded its 64-snapshot bound")
        self.evidence.append(evidence)
        self._write_evidence()
        return state

    def _write_evidence(self) -> None:
        payload = json.dumps(self.evidence, ensure_ascii=False, indent=2)
        (self.fixture.root / "agent-parallel-evidence.json").write_text(
            payload, encoding="utf-8"
        )
        configured = os.environ.get("AGENT_TEAM_LIVE_EVIDENCE_DIR")
        if configured:
            output_dir = Path(configured).expanduser().resolve()
            output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            (output_dir / f"{self._testMethodName}.json").write_text(
                payload, encoding="utf-8"
            )

    def _public_stop(
        self, state_path: Path, workspace: Path
    ) -> subprocess.CompletedProcess[str]:
        result = self.fixture._run_cli(
            ["stop", "--state", str(state_path), "--cwd", str(workspace)],
            timeout=30.0,
        )
        if state_path.exists():
            self._snapshot(state_path, "retained-after-public-stop", workspace)
        if self.evidence:
            attempts = cast(
                list[dict[str, object]],
                self.evidence[-1].setdefault("public_stop_attempts", []),
            )
            attempts.append(
                {
                    "returncode": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "state_absent": not state_path.exists(),
                }
            )
            self._write_evidence()
        return result

    def _start_agent(
        self, name: str
    ) -> tuple[Path, Path, dict[str, object], dict[str, list[str]]]:
        workspace, config, verification = _agent_config(
            self.fixture.root / name, cast(str, _RUNTIME)
        )
        try:
            result = self.fixture._run_cli(
                [
                    "start",
                    "--config",
                    str(config),
                    "--cwd",
                    str(workspace),
                    "--team",
                    "agent",
                    "--no-attach",
                ],
                timeout=30.0,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._register_partial_states(workspace)
            self._preserve_fixture_root = True
            self.fail(f"agent parallel start did not return: {exc!r}")
        if result.returncode != 0:
            self._register_partial_states(workspace)
            self._preserve_fixture_root = True
            self.fail(
                f"agent parallel start failed: stdout={result.stdout!r} "
                f"stderr={result.stderr!r}"
            )
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict) or not isinstance(
            payload.get("state_path"), str
        ):
            self._register_partial_states(workspace)
            self._preserve_fixture_root = True
            self.fail(f"agent parallel start returned invalid JSON: {result.stdout!r}")
        state_path = Path(cast(str, payload["state_path"])).resolve()
        self._state_paths[state_path] = workspace
        self.fixture._owned_state_paths.append(state_path)
        state = self.fixture._wait_state(
            state_path,
            lambda item: (
                item.get("version") == 5
                and isinstance(item.get("native"), dict)
                and item["native"].get("phase") == "running"
                and isinstance(item["native"].get("main_process"), dict)
                and item["native"]["main_process"].get("phase") == "running"
                and "agent_batch" not in item
                and isinstance(item.get("graph"), dict)
                and item["graph"].get("coordination", {}).get("mode") == "agent"
                and item["graph"].get("coordination", {}).get("dispatch_mode")
                == "parallel"
            ),
            timeout=30.0,
        )
        state = self._wait_main_controller_ready(state_path, state)
        self._snapshot(state_path, "started", workspace)
        self.assertTrue(
            any(
                event.get("event") == "main-ready-gate" for event in self._main_events()
            )
        )
        self.assertFalse(
            any(event.get("event") == "mcp-start" for event in self._main_events())
        )
        self.assertFalse(self.fixture.main_marker.with_suffix(".mcp-pid").exists())
        self.assertEqual(self._transcript(), [])
        (self.fixture.root / "release-main").write_text(
            "main ownership observed\n", encoding="utf-8"
        )
        self.assertEqual(payload.get("status"), "running")
        return workspace, state_path, state, verification

    def _register_partial_states(self, workspace: Path) -> None:
        for state_path in self.fixture.xdg_state.rglob("state.json"):
            if state_path.is_file():
                resolved = state_path.resolve()
                self._state_paths[resolved] = workspace
                if resolved not in self.fixture._owned_state_paths:
                    self.fixture._owned_state_paths.append(resolved)

    def _wait_main_controller_ready(
        self, state_path: Path, state: dict[str, object]
    ) -> dict[str, object]:
        deadline = time.monotonic() + 10.0
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                current = cast(
                    dict[str, object],
                    json.loads(state_path.read_text(encoding="utf-8")),
                )
                self._assert_main_controller_ready(current)
                return current
            except (
                AssertionError,
                OSError,
                RuntimeError,
                subprocess.SubprocessError,
                TypeError,
                ValueError,
            ) as exc:
                last_error = exc
                time.sleep(0.05)
        self.fail(f"native Main ownership was not observable: {last_error!r}")
        return state

    @staticmethod
    def _parent_pid(pid: int) -> int:
        return int(
            subprocess.check_output(
                ["ps", "-o", "ppid=", "-p", str(pid)],
                text=True,
                timeout=5.0,
            ).strip()
        )

    def _assert_main_controller_ready(self, state: dict[str, object]) -> None:
        native = cast(dict[str, object], state["native"])
        process = cast(dict[str, object], native["main_process"])
        main_pid = cast(int, process["agent_pid"])
        supervisor_pid = cast(int, process["supervisor_pid"])
        self.assertEqual(process["process_group_id"], main_pid)
        self.assertEqual(os.getpgid(main_pid), main_pid)
        self.assertEqual(self._parent_pid(main_pid), supervisor_pid)
        main_argv = cast(list[str], native["main_argv"])
        self.assertEqual(
            read_process_argv(main_pid),
            python_process_argv([sys.executable, *main_argv]),
        )
        self.assertEqual(
            read_process_argv(supervisor_pid), _validated_supervisor_argv(state)
        )
        self.assertEqual(
            self.fixture.main_marker.with_suffix(".pid").read_text(encoding="ascii"),
            str(main_pid),
        )
        self.assertNotIn("agent_batch", state)

    def _assert_main_identity(self, state: dict[str, object]) -> None:
        self._wait_transcript(
            lambda records: any(
                record.get("kind") == "response"
                and record.get("request_id") == 1
                and isinstance(record.get("response"), dict)
                and isinstance(record["response"].get("result"), dict)
                and isinstance(record["response"]["result"].get("protocolVersion"), str)
                for record in records
            )
        )
        native = state.get("native")
        self.assertIsInstance(native, dict)
        process = cast(dict[str, object], native)["main_process"]
        self.assertIsInstance(process, dict)
        process_map = cast(dict[str, object], process)
        main_pid = process_map.get("agent_pid")
        supervisor_pid = process_map.get("supervisor_pid")
        self.assertIsInstance(main_pid, int)
        self.assertIsInstance(supervisor_pid, int)
        start_events = [
            event for event in self._main_events() if event.get("event") == "mcp-start"
        ]
        self.assertTrue(start_events)
        event = start_events[-1]
        self.assertEqual(event.get("main_pid"), main_pid)
        self.assertEqual(event.get("main_ppid"), supervisor_pid)
        self.assertEqual(event.get("main_pgid"), main_pid)
        self.assertEqual(self._parent_pid(cast(int, main_pid)), supervisor_pid)
        self.assertEqual(os.getpgid(cast(int, main_pid)), main_pid)
        mcp_pid = event.get("mcp_pid")
        self.assertIsInstance(mcp_pid, int)
        self.assertEqual(event.get("mcp_ppid"), main_pid)
        self.assertEqual(event.get("mcp_pgid"), main_pid)
        main_argv = read_process_argv(cast(int, main_pid))
        self.assertIsNotNone(main_argv)
        saved_argv = cast(list[str], cast(dict[str, object], native)["main_argv"])
        self.assertEqual(
            main_argv,
            python_process_argv([sys.executable, *saved_argv]),
        )
        self.assertEqual(
            read_process_argv(cast(int, supervisor_pid)),
            _validated_supervisor_argv(state),
        )
        config_index = saved_argv.index("--mcp-config")
        launch_config = json.loads(saved_argv[config_index + 1])
        recorded_config = json.loads(
            self.fixture.main_marker.with_suffix(".mcp-config.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(launch_config, recorded_config)
        server_config = launch_config["mcpServers"]["agent_team"]
        self.assertEqual(event.get("command"), server_config["command"])
        self.assertEqual(event.get("arguments"), server_config["args"])
        self.assertEqual(
            event.get("state_path"), server_config["env"]["AGENT_TEAM_STATE_PATH"]
        )
        mcp_argv = read_process_argv(cast(int, mcp_pid))
        self.assertIsNotNone(mcp_argv)
        command_path = Path(cast(str, server_config["command"])).resolve()
        launcher_path = Path(cast(str, state["launcher_path"])).resolve()
        self.assertEqual(command_path, launcher_path)
        self.assertEqual(command_path.read_bytes().splitlines()[0], b"#!/bin/sh")
        self.assertEqual(
            command_path.read_text(encoding="utf-8").splitlines()[-1],
            'exec python3 -m agent_team "$@"',
        )
        observed_mcp_argv = cast(tuple[str, ...], mcp_argv)
        self.assertEqual(
            observed_mcp_argv[1:], ("-m", "agent_team", *server_config["args"])
        )
        mcp_interpreter = shutil.which(
            observed_mcp_argv[0], path=self.fixture.environment["PATH"]
        )
        self.assertIsNotNone(mcp_interpreter)
        self.assertEqual(
            Path(cast(str, mcp_interpreter)).resolve(),
            Path(python_process_argv([sys.executable])[0]).resolve(),
        )
        self.assertEqual(self._parent_pid(cast(int, mcp_pid)), main_pid)
        self.assertEqual(os.getpgid(cast(int, mcp_pid)), main_pid)
        self.evidence[-1]["main_mcp_os_identity"] = {
            "main_pid": main_pid,
            "main_ppid": supervisor_pid,
            "main_pgid": main_pid,
            "main_argv": list(cast(tuple[str, ...], main_argv)),
            "mcp_pid": mcp_pid,
            "mcp_ppid": main_pid,
            "mcp_pgid": main_pid,
            "mcp_argv": list(observed_mcp_argv),
            "mcp_interpreter": str(Path(cast(str, mcp_interpreter)).resolve()),
        }
        self._write_evidence()
        state_path = event.get("state_path")
        self.assertEqual(state_path, state["state_path"])
        self.assertEqual(
            Path(
                self.fixture.main_marker.with_suffix(".state").read_text(
                    encoding="utf-8"
                )
            ),
            Path(cast(str, state["state_path"])),
        )

    def _assert_assignment_bindings(
        self,
        state: dict[str, object],
        events: list[dict[str, object]],
        event_name: str,
    ) -> None:
        roles = state.get("roles")
        self.assertIsInstance(roles, dict)
        selected = [event for event in events if event.get("event") == event_name]
        self.assertTrue(selected)
        observations: list[dict[str, object]] = []
        role_map = cast(dict[str, dict[str, object]], roles)
        for event in selected:
            role = event.get("role")
            self.assertIsInstance(role, str)
            assignment = role_map[cast(str, role)]
            task_spec = assignment.get("task_spec")
            task_id = task_spec.get("task_id") if isinstance(task_spec, dict) else None
            self.assertEqual(event.get("task_id"), task_id)
            nonce = assignment.get("launch_nonce")
            self.assertEqual(event.get("launch_nonce"), nonce)
            self.assertEqual(
                event.get("marker"),
                f"agent-team/{state['team_id']}/{role}/{nonce}",
            )
            runner_pid = assignment.get("runner_pid")
            self.assertIsInstance(runner_pid, int)
            runner_argv = read_process_argv(cast(int, runner_pid))
            self.assertEqual(
                runner_argv, tuple(cast(list[str], assignment["runner_argv"]))
            )
            self.assertEqual(
                os.getpgid(cast(int, runner_pid)),
                assignment.get("runner_process_group_id"),
            )
            provider_pid = event.get("pid")
            self.assertIsInstance(provider_pid, int)
            self.assertNotEqual(provider_pid, runner_pid)
            provider_argv = read_process_argv(cast(int, provider_pid))
            self.assertIsNotNone(provider_argv)
            self.assertIn(
                str((self.fixture.node_bin / "node").resolve()),
                cast(tuple[str, ...], provider_argv),
            )
            self.assertIn(cast(str, nonce), cast(tuple[str, ...], provider_argv))
            self.assertEqual(event.get("pgid"), provider_pid)
            parent_pid = int(
                subprocess.check_output(
                    ["ps", "-o", "ppid=", "-p", str(provider_pid)],
                    text=True,
                    timeout=5.0,
                ).strip()
            )
            self.assertEqual(parent_pid, runner_pid)
            spec = cast(dict[str, dict[str, object]], state["role_specs"])[
                cast(str, role)
            ]
            expected_client = client_argv(
                NativeAcpExecutables.from_dict(spec["acp_executables"]),
                cast(str, assignment["agent_command"]),
                harness=cast(str, spec["provider"]),
                workspace=Path(cast(str, state["workspace"])),
                permission=cast(str, spec["permission"]),
                model=cast(str, spec["model"]),
                effort=cast(str, spec["effort"]),
                instructions=cast(str, spec["instructions"]),
                timeout_seconds=ACP_TIMEOUT_SECONDS,
                question_socket=Path(cast(str, assignment["question_socket"])),
                result_file=Path(cast(str, assignment["provider_private_root"]))
                / "client-result.json",
                launch_nonce=cast(str, nonce),
            )
            self.assertEqual(
                provider_argv,
                python_process_argv([sys.executable, *expected_client]),
            )
            observations.append(
                {
                    "role": role,
                    "task_id": task_id,
                    "dispatch_id": assignment.get("dispatch_id"),
                    "launch_nonce": nonce,
                    "marker": event.get("marker"),
                    "provider_pid": provider_pid,
                    "provider_pgid": event.get("pgid"),
                    "runner_pid": runner_pid,
                    "runner_pgid": assignment.get("runner_process_group_id"),
                    "parent_pid": parent_pid,
                    "provider_argv": list(cast(tuple[str, ...], provider_argv)),
                }
            )
        self.evidence[-1]["assignment_event_bindings"] = observations
        self._write_evidence()

    def _assert_outputs(
        self, workspace: Path, roles: set[str], events: list[dict[str, object]]
    ) -> None:
        expected = {
            "worker-a": ("src/a/output.txt", "fixture output task-a\n"),
            "worker-b": ("src/b/output.txt", "fixture output task-b\n"),
        }
        completions = [
            event for event in events if event.get("event") == "writer-complete"
        ]
        self.assertEqual({event.get("role") for event in completions}, roles)
        for event in completions:
            role = cast(str, event["role"])
            relative, content = expected[role]
            output = workspace / relative
            self.assertEqual(event.get("output_path"), relative)
            self.assertEqual(output.read_text(encoding="utf-8"), content)
            self.assertEqual(
                event.get("output_sha256"),
                hashlib.sha256(output.read_bytes()).hexdigest(),
            )

    def _assert_resources_gone(
        self,
        final_state: dict[str, object],
        stop_result: subprocess.CompletedProcess[str],
    ) -> None:
        native = cast(dict[str, object], final_state["native"])
        process = cast(dict[str, object], native["main_process"])
        backend = self.fixture._terminal_backend(final_state)
        receipt = backend._receipt_from_state(native)
        receipt_fields = receipt.as_dict()
        terminal_paths = {
            key: Path(cast(str, receipt_fields[key]))
            for key in ("socket_path", "config_path")
        }
        socket_root = backend._socket_root(receipt)
        terminal_resources = {
            "receipt": receipt_fields,
            "server_pid": receipt.server_pid,
            "pane_pid": receipt.pane_pid,
            "socket_root": str(socket_root),
            "paths": {key: str(value) for key, value in terminal_paths.items()},
        }
        pids = {process.get("agent_pid"), process.get("supervisor_pid")}
        pids.update({receipt.server_pid, receipt.pane_pid})
        for event in self._main_events():
            for key in ("main_pid", "mcp_pid"):
                if isinstance(event.get(key), int):
                    pids.add(event[key])
        groups: set[int] = set()
        for event in self._main_events() + self._events():
            if isinstance(event.get("main_pgid"), int):
                groups.add(cast(int, event["main_pgid"]))
            if isinstance(event.get("mcp_pgid"), int):
                groups.add(cast(int, event["mcp_pgid"]))
            if isinstance(event.get("pgid"), int):
                groups.add(cast(int, event["pgid"]))
        for evidence in self.evidence:
            roles = evidence.get("roles")
            if not isinstance(roles, dict):
                continue
            for raw in roles.values():
                if not isinstance(raw, dict):
                    continue
                for key in ("runner_pid",):
                    if isinstance(raw.get(key), int):
                        pids.add(raw[key])
                if isinstance(raw.get("runner_process_group_id"), int):
                    groups.add(cast(int, raw["runner_process_group_id"]))
                for key in (
                    "provider_private_root",
                    "snapshot_root",
                    "prompt_path",
                    "question_socket",
                ):
                    path = raw.get(key)
                    if isinstance(path, str):
                        self.assertFalse(
                            Path(path).exists() or Path(path).is_symlink(), path
                        )
        provider_pids = {
            cast(int, event["pid"])
            for event in self._events()
            if isinstance(event.get("pid"), int)
        }
        pids.update(provider_pids)
        for pid in sorted(
            cast(set[int], {item for item in pids if isinstance(item, int)})
        ):
            if pid > 1:
                self.fixture._wait_pid_gone(pid)
        for group in sorted(groups):
            if group <= 1:
                continue
            deadline = time.monotonic() + 5.0
            while True:
                try:
                    os.killpg(group, 0)
                except ProcessLookupError:
                    break
                if time.monotonic() >= deadline:
                    self.fail(f"owned fixture process group remains: pgid={group}")
                time.sleep(0.05)
        self.assertFalse(self.fixture.main_marker.with_suffix(".pid").exists())
        self.assertFalse(self.fixture.main_marker.with_suffix(".mcp-pid").exists())
        self.assertEqual(
            list((self.fixture.root / "provider-active").glob("*.active")), []
        )
        for path in (*terminal_paths.values(), socket_root):
            self.assertFalse(path.exists() or path.is_symlink(), str(path))
        terminal_resources["paths_gone"] = True
        self.evidence[-1]["terminal_resources"] = terminal_resources
        state_path = Path(cast(str, final_state["state_path"]))
        self.assertFalse(state_path.exists())
        try:
            stop_receipt: object = json.loads(stop_result.stdout)
        except json.JSONDecodeError:
            stop_receipt = {"raw": stop_result.stdout}
        self.evidence[-1]["post_stop"] = {
            "stop_returncode": stop_result.returncode,
            "stop_receipt": stop_receipt,
            "state_absent": not state_path.exists(),
            "main_events": self._main_events(),
            "provider_events": self._events(),
            "transcript": self._transcript(),
            "pids_gone": True,
            "process_groups_gone": True,
            "owned_paths_gone": True,
            "terminal_paths_gone": True,
        }
        self._write_evidence()

    def _assert_tool_sequence(self, *, question: bool = False) -> None:
        requests = [
            record for record in self._transcript() if record.get("kind") == "request"
        ]
        self.assertTrue(requests)
        self.assertEqual(requests[0].get("method"), "initialize")
        self.assertEqual(requests[1].get("method"), "notifications/initialized")
        self.assertEqual(requests[2].get("method"), "tools/list")
        names = [
            record["tool_name"]
            for record in requests
            if isinstance(record.get("tool_name"), str)
        ]
        if question:
            expected = [
                "task_batch_open",
                "task_dispatch",
                "task_dispatch",
                "role_wait",
                "role_wait",
                "role_read",
                "role_release",
                "delivery_ack",
            ]
            self.assertEqual(names, expected)
            waits = [
                record
                for record in requests
                if record.get("tool_name") == "role_wait"
                and isinstance(record.get("arguments"), dict)
            ]
            self.assertEqual(
                [
                    cast(dict[str, object], record["arguments"]).get("role")
                    for record in waits
                ],
                ["worker-a", "worker-b"],
            )
            responses = {
                record.get("request_id"): record
                for record in self._transcript()
                if record.get("kind") == "response"
            }

            def receipt_for(request: dict[str, object]) -> dict[str, object]:
                response_record = responses.get(request.get("request_id"))
                self.assertIsInstance(response_record, dict)
                response = cast(dict[str, object], response_record)["response"]
                result = cast(dict[str, object], response)["result"]
                content = cast(list[dict[str, object]], result["content"])[0]
                return cast(dict[str, object], json.loads(cast(str, content["text"])))

            worker_a_delivery = receipt_for(waits[0]).get("delivery_id")
            worker_b_delivery = receipt_for(waits[1]).get("delivery_id")
            self.assertIsInstance(worker_a_delivery, str)
            self.assertIsInstance(worker_b_delivery, str)
            consumed_roles = [
                cast(dict[str, object], record["arguments"]).get("role")
                for record in requests
                if record.get("tool_name") in {"role_read", "role_release"}
                and isinstance(record.get("arguments"), dict)
            ]
            self.assertEqual(consumed_roles, ["worker-b", "worker-b"])
            acknowledgements = [
                cast(dict[str, object], record["arguments"]).get("delivery_id")
                for record in requests
                if record.get("tool_name") == "delivery_ack"
                and isinstance(record.get("arguments"), dict)
            ]
            self.assertEqual(acknowledgements, [worker_b_delivery])
            self.assertNotEqual(worker_a_delivery, worker_b_delivery)
            self.assertNotIn("message_reply", names)
            return
        expected_prefix = [
            "task_batch_open",
            "task_dispatch",
            "task_dispatch",
            "role_wait",
            "role_read",
            "role_release",
            "delivery_ack",
            "role_wait",
            "role_read",
            "role_release",
            "delivery_ack",
            "task_dispatch",
            "task_dispatch",
            "role_wait",
            "role_read",
            "role_release",
            "delivery_ack",
            "role_wait",
            "role_read",
            "role_release",
            "delivery_ack",
            "task_verify",
            "task_verify",
        ]
        self.assertEqual(names, expected_prefix)

    def _assert_main_exit(self, *, normal: bool) -> None:
        events = self._main_events()
        self.assertFalse(
            any(event.get("event") == "main-error" for event in events), events
        )
        exits = [event for event in events if event.get("event") == "main-exit"]
        self.assertTrue(exits)
        self.assertEqual(exits[-1].get("returncode"), 0)
        if normal:
            self.assertTrue(
                any(event.get("event") == "main-complete" for event in events)
            )
            mcp_exits = [event for event in events if event.get("event") == "mcp-exit"]
            self.assertTrue(mcp_exits)
            self.assertEqual(mcp_exits[-1].get("returncode"), 0)
            self.assertTrue(mcp_exits[-1].get("graceful"))
        else:
            self.assertTrue(
                any(event.get("event") == "question-stop-preserved" for event in events)
            )

    def _tear_down_agent(self) -> None:
        fixture = getattr(self, "fixture", None)
        if fixture is None or not getattr(self, "_fixture_ready", False):
            return
        cleanup_issues: list[str] = []
        for state_path, workspace in self._state_paths.items():
            if not state_path.exists():
                if state_path not in self._verified_clean_runs:
                    cleanup_issues.append(
                        f"state={state_path} reason=post-stop-proof-unconfirmed"
                    )
                continue
            try:
                result = self._public_stop(state_path, workspace)
            except (OSError, subprocess.TimeoutExpired) as exc:
                cleanup_issues.append(
                    f"state={state_path} reason=stop-exception:{exc!r}"
                )
                continue
            if result.returncode != 0 or state_path.exists():
                cleanup_issues.append(
                    f"state={state_path} returncode={result.returncode} "
                    f"stderr={result.stderr!r}"
                )
                continue
            cleanup_issues.append(
                f"state={state_path} reason=failure-cleanup-requires-independent-proof"
            )
        if self._preserve_fixture_root:
            cleanup_issues.append("reason=identity-or-cleanup-uncertain")
        if cleanup_issues:
            print(
                f"LIVE_AGENT_PARALLEL_EVIDENCE_PRESERVED root={fixture.root} "
                f"details={cleanup_issues}",
                file=sys.stderr,
            )
            return
        fixture.tearDown()

    def test_agent_parallel_main_overlap_review_verify_and_public_stop(self) -> None:
        workspace, state_path, initial, verification = self._start_agent("normal")
        self._wait_main(
            lambda events: any(event.get("event") == "mcp-start" for event in events)
        )
        self._assert_main_identity(initial)
        graph = cast(dict[str, object], initial["graph"])
        self.assertEqual(
            graph["coordination"],
            {
                "mode": "agent",
                "entry_nodes": ["main"],
                "dispatch_mode": "parallel",
                "max_active": 2,
            },
        )
        self.assertEqual(
            {node["kind"] for node in cast(list[dict[str, object]], graph["nodes"])},
            {"main", "worker", "reviewer"},
        )
        self._wait_transcript(
            lambda records: any(
                record.get("kind") == "response"
                and isinstance(record.get("response"), dict)
                and isinstance(
                    cast(dict[str, object], record["response"]).get("result"),
                    dict,
                )
                for record in records
            )
        )
        writer_events = self._wait_events(
            lambda events: (
                {
                    event.get("role")
                    for event in events
                    if event.get("event") == "writer-start"
                }
                == {"worker-a", "worker-b"}
            )
        )
        running = self._snapshot(state_path, "both-writers-running", workspace)
        self._assert_assignment_bindings(running, writer_events, "writer-start")
        writer_pids = {
            cast(int, event["pid"])
            for event in writer_events
            if event.get("event") == "writer-start"
        }
        self.assertEqual(len(writer_pids), 2)
        self.assertEqual(
            len(
                {
                    event.get("pgid")
                    for event in writer_events
                    if event.get("event") == "writer-start"
                }
            ),
            2,
        )
        (self.fixture.root / "release-writers").write_text(
            "release\n", encoding="utf-8"
        )
        writer_done = self._wait_events(
            lambda events: (
                {
                    event.get("role")
                    for event in events
                    if event.get("event") == "writer-complete"
                }
                == {"worker-a", "worker-b"}
            )
        )
        self._assert_outputs(workspace, {"worker-a", "worker-b"}, writer_done)
        self._wait_main(
            lambda events: any(
                event.get("event") == "writers-drained" for event in events
            )
        )
        sealed = self.fixture._wait_state(
            state_path,
            lambda item: (
                isinstance(item.get("agent_batch"), dict)
                and item["agent_batch"].get("phase") == "reviewers"
                and isinstance(item["agent_batch"].get("revision"), str)
                and len(item["agent_batch"]["revision"]) == 64
            ),
            timeout=60.0,
        )
        self._snapshot(state_path, "writers-drained-sealed-review", workspace)
        revision = cast(str, cast(dict[str, object], sealed["agent_batch"])["revision"])
        self.assertEqual(snapshot_revision(workspace), revision)
        reviewer_events = self._wait_events(
            lambda events: (
                {
                    event.get("role")
                    for event in events
                    if event.get("event") == "reviewer-start"
                }
                == {"reviewer-a", "reviewer-b"}
            )
        )
        reviewer_pids = {
            cast(int, event["pid"])
            for event in reviewer_events
            if event.get("event") == "reviewer-start"
        }
        reviewer_pgids = {
            cast(int, event["pgid"])
            for event in reviewer_events
            if event.get("event") == "reviewer-start"
        }
        self.assertEqual(len(reviewer_pids), 2)
        self.assertEqual(len(reviewer_pgids), 2)
        reviewing = self._snapshot(state_path, "both-reviewers-running", workspace)
        self._assert_assignment_bindings(reviewing, reviewer_events, "reviewer-start")
        self.assertEqual(
            {
                event.get("revision")
                for event in reviewer_events
                if event.get("event") == "reviewer-start"
            },
            {revision},
        )
        for role in ("reviewer-a", "reviewer-b"):
            assignment = cast(
                dict[str, object], cast(dict[str, object], reviewing["roles"])[role]
            )
            self.assertEqual(assignment.get("task_revision"), revision)
        (self.fixture.root / "release-reviewers").write_text(
            "release\n", encoding="utf-8"
        )
        reviewer_done = self._wait_events(
            lambda events: (
                {
                    event.get("role")
                    for event in events
                    if event.get("event") == "reviewer-complete"
                }
                == {"reviewer-a", "reviewer-b"}
            )
        )
        self.assertEqual(
            {
                tuple(cast(list[str], event["verified_outputs"]))
                for event in reviewer_done
                if event.get("event") == "reviewer-complete"
            },
            {("src/a/output.txt", "src/b/output.txt")},
        )
        final_state = self.fixture._wait_state(
            state_path,
            lambda item: (
                isinstance(item.get("tasks"), dict)
                and all(
                    isinstance(item["tasks"].get(task_id), dict)
                    and item["tasks"][task_id].get("status") == "completed"
                    for task_id in ("task-a", "task-b")
                )
                and item.get("roles") == {}
                and isinstance(item.get("native"), dict)
                and item["native"]["main_process"].get("phase") == "exited"
                and item["native"]["main_process"].get("group_stopped") is True
            ),
            timeout=90.0,
        )
        self._snapshot(state_path, "completed-before-public-stop", workspace)
        tasks = cast(dict[str, dict[str, object]], final_state["tasks"])
        for task_id, expected_argv in verification.items():
            record = tasks[task_id]
            self.assertEqual(record.get("revision"), revision)
            evidence = cast(dict[str, object], record["verification"])
            self.assertEqual(evidence.get("revision"), revision)
            self.assertTrue(evidence.get("passed"))
            self.assertTrue(evidence.get("cleanup_confirmed"))
            command = cast(list[dict[str, object]], evidence["commands"])[0]
            self.assertEqual(command.get("argv"), expected_argv)
            self.assertEqual(command.get("returncode"), 0)
        self.assertEqual(
            cast(dict[str, object], final_state["agent_batch"])["revision"], revision
        )
        self.assertEqual(
            cast(dict[str, object], final_state["agent_batch"])["phase"], "verification"
        )
        self._assert_main_exit(normal=True)
        self._assert_tool_sequence()
        stop = self._public_stop(state_path, workspace)
        self.assertEqual(stop.returncode, 0, stop.stderr)
        self.assertFalse(state_path.exists())
        self._assert_resources_gone(final_state, stop)
        self._verified_clean_runs.add(state_path)

    def test_agent_parallel_question_peer_progress_then_public_stop(self) -> None:
        (self.fixture.root / "question-scenario").write_text(
            "enabled\n", encoding="utf-8"
        )
        workspace, state_path, initial, _verification = self._start_agent("question")
        self._assert_main_identity(initial)
        started = self._wait_events(
            lambda events: (
                {
                    event.get("role")
                    for event in events
                    if event.get("event") == "writer-start"
                }
                == {"worker-a", "worker-b"}
                and any(event.get("event") == "question-published" for event in events)
            )
        )
        self._snapshot(state_path, "question-pending-both-workers", workspace)
        (self.fixture.root / "release-question-peer").write_text(
            "release\n", encoding="utf-8"
        )
        events = self._wait_events(
            lambda current: (
                any(
                    event.get("event") == "writer-complete"
                    and event.get("role") == "worker-b"
                    for event in current
                )
                and (self.fixture.root / "question-ready").is_file()
            )
        )
        pending = self.fixture._wait_state(
            state_path,
            lambda item: (
                isinstance(item.get("roles"), dict)
                and isinstance(item["roles"].get("worker-a"), dict)
                and isinstance(item["roles"]["worker-a"].get("native_question"), dict)
                and item["roles"]["worker-a"]["native_question"].get("phase")
                == "observed"
                and item["roles"]["worker-a"]["native_question"].get("answers") == {}
                and isinstance(item.get("tasks"), dict)
                and item["tasks"]["task-b"].get("status")
                == "awaiting_implementation_review"
                and "worker-b" not in item["roles"]
            ),
            timeout=60.0,
        )
        pending_snapshot = self._snapshot(
            state_path, "question-observed-peer-drained", workspace
        )
        self._assert_assignment_bindings(pending, started, "question-published")
        self._assert_outputs(workspace, {"worker-b"}, events)
        self.assertFalse((workspace / "src/a/output.txt").exists())
        pending_roles = cast(dict[str, dict[str, object]], pending["roles"])
        question = cast(dict[str, object], pending_roles["worker-a"]["native_question"])
        self.assertEqual(question.get("answers"), {})
        observed = self._wait_main(
            lambda current: any(
                event.get("event") == "question-observed" for event in current
            )
        )
        observed_event = [
            event for event in observed if event.get("event") == "question-observed"
        ][-1]
        self.assertEqual(observed_event.get("delivery_id"), question.get("delivery_id"))
        self.assertIn(
            observed_event.get("message_id"),
            cast(list[object], question["message_ids"]),
        )
        self.assertEqual(
            [
                event.get("role")
                for event in events
                if event.get("event") == "question-answered"
            ],
            [],
        )
        self.assertEqual(
            [
                event.get("role")
                for event in events
                if event.get("event") == "question-recorded"
            ],
            [],
        )
        self._assert_tool_sequence(question=True)
        self.assertIn("worker-b", str(pending_snapshot["tasks"]))
        stop = self._public_stop(state_path, workspace)
        self.assertEqual(stop.returncode, 0, stop.stderr)
        self.assertFalse(state_path.exists())
        self._assert_resources_gone(pending, stop)
        self._assert_main_exit(normal=False)
        after = self._events()
        self.assertTrue(
            any(
                event.get("event") == "provider-stop"
                and event.get("role") == "worker-a"
                for event in after
            )
        )
        self.assertFalse(
            any(event.get("event") == "question-answered" for event in after)
        )
        self.assertFalse(
            any(event.get("event") == "question-recorded" for event in after)
        )
        self._verified_clean_runs.add(state_path)


if __name__ == "__main__":
    unittest.main()
