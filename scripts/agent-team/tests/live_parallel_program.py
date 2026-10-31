"""Opt-in live acceptance tests for a mainless parallel native program.

The tests compose the existing ``LiveNativeContractTest`` fixture instead of
inheriting from it, so the v3 single-planner cases are not rerun.

The selected terminal (tmux, Herdr, or Zellij) is real.  The provider and
Node dependency are disposable per-test executables installed below the
fixture's private ``fake-bin``/``node-bin`` directories.  This proves native
terminal, runner, state, and cleanup behavior; it does not prove a real ACP
SDK wire or a real model/provider response.

Run with an explicit runtime and both opt-in gates, for example::

    AGENT_TEAM_RUN_LIVE_NATIVE=1 AGENT_TEAM_RUN_LIVE_PROGRAM=1 \\
      AGENT_TEAM_LIVE_RUNTIME=tmux \\
      uv run --locked --project scripts/agent-team \\
      python -m unittest discover -s scripts/agent-team/tests \\
      -p live_parallel_program.py -v
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import cast

import live_native_contract as _live_native_contract

from agent_team.cli import ACP_TIMEOUT_SECONDS
from agent_team.native_acp_dependencies import NativeAcpExecutables
from agent_team.process_identity import python_process_argv, read_process_argv
from agent_team.scoped_acp import client_argv
from agent_team.workspace_revision import snapshot_revision

_RUN_PROGRAM = os.environ.get("AGENT_TEAM_RUN_LIVE_PROGRAM") == "1"
_RUNTIME = os.environ.get("AGENT_TEAM_LIVE_RUNTIME")
_RUNTIMES = frozenset({"tmux", "herdr", "zellij"})


def _write_parallel_provider(path: Path, root: Path) -> None:
    """Install one private fake Node with barriers and bounded JSON events."""

    # This script is intentionally self-contained.  It receives only the
    # selected ACP client arguments, the safe TMPDIR/HOME/PATH environment,
    # and the marker emitted by the real agent-team launcher.  It never calls
    # an actual model, ACP client, network, or provider CLI.
    source = f"""#!{sys.executable}
import hashlib
import json
import os
import re
import select
import signal
import socket
import sys
import time
from pathlib import Path

ROOT = Path({str(root)!r})
EVENTS = ROOT / "parallel-provider-events.jsonl"
SCENARIO = ROOT / "question-scenario"
ACTIVE_DIR = ROOT / "provider-active"
ARGS = sys.argv[1:]
MARKER = None
ROLE = None
NONCE = None
FINISHED = False
CONNECTION = None
TASK_ID = None
WORKSPACE = None
ACTIVE_MARKER = None


def _argument(name):
    try:
        return ARGS[ARGS.index(name) + 1]
    except (ValueError, IndexError):
        return None


def _task(prompt):
    match = re.search(r'"task_id"\\s*:\\s*"([^"]+)"', prompt)
    return match.group(1) if match else None


def _emit(event, **fields):
    payload = {{
        "event": event,
        "pid": os.getpid(),
        "pgid": os.getpgid(0),
        "role": ROLE,
        "launch_nonce": NONCE,
        "marker": MARKER,
        "task_id": TASK_ID,
        "time_ns": time.time_ns(),
        **fields,
    }}
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\\n"
    ).encode("utf-8")
    fd = os.open(EVENTS, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        if os.write(fd, encoded) != len(encoded):
            raise RuntimeError("fixture event append was partial")
    finally:
        os.close(fd)


def _finish(receipt, code):
    global FINISHED
    if FINISHED:
        raise SystemExit(code)
    FINISHED = True
    result_path = _argument("--result-file")
    nonce = _argument("--launch-nonce")
    if result_path is not None and nonce is not None:
        result = Path(result_path)
        pending = result.with_name("client-result.pending")
        with pending.open("x", encoding="utf-8") as target:
            os.fchmod(target.fileno(), 0o600)
            json.dump(
                {{"version": 1, "launch_nonce": nonce, "receipt": receipt}},
                target,
                ensure_ascii=False,
            )
            target.flush()
            os.fsync(target.fileno())
        os.link(pending, result)
        pending.unlink()
        directory = os.open(result.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    if ACTIVE_MARKER is not None:
        ACTIVE_MARKER.unlink(missing_ok=True)
    print(json.dumps(receipt, ensure_ascii=False), flush=True)
    raise SystemExit(code)


def _stop(_signum, _frame):
    _emit("provider-stop")
    if CONNECTION is not None:
        try:
            CONNECTION.close()
        except OSError:
            pass
    _finish(
        {{
            "error": "fixture provider cancelled by native stop",
            "session_id": "fixture-session-" + ROLE,
            "model": _argument("--model"),
            "effort": _argument("--effort"),
            "cleanup_confirmed": True,
        }},
        1,
    )


signal.signal(signal.SIGTERM, _stop)
signal.signal(signal.SIGINT, _stop)

if "--version" in ARGS:
    print("v22.13.0")
    raise SystemExit(0)

agent_argv = json.loads(_argument("--agent-argv"))
if (
    not isinstance(agent_argv, list)
    or len(agent_argv) < 3
    or agent_argv[0] != "env"
    or not isinstance(agent_argv[1], str)
    or not agent_argv[1].startswith("AGENT_TEAM_ACP_MARKER=")
):
    raise RuntimeError("fixture client requires the nested ACP marker")
MARKER = agent_argv[1].removeprefix("AGENT_TEAM_ACP_MARKER=")
marker_match = re.fullmatch(
    r"agent-team/([a-z][a-z0-9-]{{0,63}})/(worker-[ab]|reviewer-[ab])/([a-z0-9]{{8,64}})",
    MARKER,
)
if marker_match is None or marker_match[3] != _argument("--launch-nonce"):
    raise RuntimeError("fixture client marker identity is invalid")
ROLE = marker_match[2]
NONCE = marker_match[3]
ACTIVE_DIR.mkdir(mode=0o700, exist_ok=True)
ACTIVE_MARKER = ACTIVE_DIR / (ROLE + "-" + NONCE + ".active")
ACTIVE_MARKER.write_text(str(os.getpid()), encoding="ascii")
prompt = sys.stdin.read()
TASK_ID = _task(prompt)
workspace_arg = _argument("--cwd")
if not workspace_arg or not Path(workspace_arg).is_absolute():
    raise RuntimeError("fixture client requires an absolute --cwd")
WORKSPACE = Path(workspace_arg)
is_review = "レビュー結果は次のキー" in prompt
revision_match = re.search(r'"revision"\\s*:\\s*"([0-9a-f]{{64}})"', prompt)
revision = revision_match.group(1) if revision_match else None
scenario_question = SCENARIO.is_file() and ROLE == "worker-a"

if is_review:
    _emit("reviewer-start", revision=revision)
    while not (ROOT / "release-reviewers").is_file():
        time.sleep(0.05)
    if revision is None or TASK_ID is None:
        _finish(
            {{"error": "fixture reviewer could not bind TaskSpec revision", "cleanup_confirmed": True}},
            1,
        )
    expected_outputs = {{
        "src/a/output.txt": "fixture output task-a\\n",
        "src/b/output.txt": "fixture output task-b\\n",
    }}
    output_hashes = {{}}
    for relative, expected in expected_outputs.items():
        output = WORKSPACE / relative
        if not output.is_file() or output.read_text(encoding="utf-8") != expected:
            _finish(
                {{
                    "error": "fixture reviewer output assertion failed: " + relative,
                    "cleanup_confirmed": True,
                }},
                1,
            )
        output_hashes[relative] = hashlib.sha256(output.read_bytes()).hexdigest()
    _emit(
        "reviewer-complete",
        revision=revision,
        verified_outputs=sorted(expected_outputs),
        output_hashes=output_hashes,
    )
    _finish(
        {{
            "output": json.dumps(
                {{
                    "task_id": TASK_ID,
                    "stage": "implementation",
                    "revision": revision,
                    "decision": "approve",
                    "findings": [],
                }},
                separators=(",", ":"),
            ),
            "session_id": "fixture-session-" + ROLE,
            "model": _argument("--model"),
            "effort": _argument("--effort"),
            "cleanup_confirmed": True,
        }},
        0,
    )

_emit("writer-start", scenario="question" if scenario_question else "normal")
if scenario_question:
    socket_path = _argument("--question-socket")
    if socket_path is None:
        _finish({{"error": "fixture question socket is missing", "cleanup_confirmed": True}}, 1)
    CONNECTION = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    deadline = time.monotonic() + 5.0
    while True:
        try:
            CONNECTION.connect(socket_path)
            break
        except (FileNotFoundError, ConnectionRefusedError):
            if time.monotonic() >= deadline:
                _finish({{"error": "fixture question socket did not open", "cleanup_confirmed": True}}, 1)
            time.sleep(0.01)
    CONNECTION.sendall((json.dumps({{
        "kind": "question",
        "session_id": "fixture-session-" + ROLE,
        "tool_call_id": "fixture-question-" + ROLE,
        "questions": [{{"field": "question_0_custom", "body": "Which source should I inspect?"}}],
    }}) + "\\n").encode("utf-8"))
    _emit("question-published")
    reader = CONNECTION.makefile("rwb", buffering=0)
    while True:
        ready, _, _ = select.select([reader], [], [], 0.2)
        if not ready:
            continue
        answer_line = reader.readline()
        if not answer_line:
            _stop(0, None)
        answer = json.loads(answer_line)
        if answer.get("kind") != "answer":
            _finish({{"error": "fixture received an invalid question frame", "cleanup_confirmed": True}}, 1)
        _emit("question-answered")
        reader.write((json.dumps({{
            "kind": "received",
            "session_id": "fixture-session-" + ROLE,
            "tool_call_id": "fixture-question-" + ROLE,
        }}) + "\\n").encode("utf-8"))
        recorded_line = reader.readline()
        if not recorded_line:
            _stop(0, None)
        recorded = json.loads(recorded_line)
        if recorded.get("kind") != "recorded":
            _finish({{"error": "fixture missing recorded question frame", "cleanup_confirmed": True}}, 1)
        _emit("question-recorded")
        _finish(
            {{
                "output": "fixture question completed",
                "session_id": "fixture-session-" + ROLE,
                "model": _argument("--model"),
                "effort": _argument("--effort"),
                "cleanup_confirmed": True,
            }},
            0,
        )

if ROLE.startswith("worker-"):
    if not SCENARIO.is_file():
        while not (ROOT / "release-writers").is_file():
            time.sleep(0.05)
    else:
        while not (ROOT / "release-question-peer").is_file():
            time.sleep(0.05)
    relative = "src/a/output.txt" if ROLE == "worker-a" else "src/b/output.txt"
    output = WORKSPACE / relative
    expected = "fixture output task-a\\n" if ROLE == "worker-a" else "fixture output task-b\\n"
    output.write_text(expected, encoding="utf-8")
    _emit(
        "writer-complete",
        output_path=relative,
        output_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
    )
    _finish(
        {{
            "output": "fixture writer result for " + ROLE,
            "session_id": "fixture-session-" + ROLE,
            "model": _argument("--model"),
            "effort": _argument("--effort"),
            "cleanup_confirmed": True,
        }},
        0,
    )

_finish({{"error": "fixture role is unsupported", "cleanup_confirmed": True}}, 1)
"""
    path.write_text(source, encoding="utf-8")
    path.chmod(0o700)


def _program_config(
    root: Path, runtime: str
) -> tuple[Path, Path, dict[str, list[str]]]:
    workspace = root / "program-workspace"
    (workspace / "src/a").mkdir(mode=0o700, parents=True)
    (workspace / "src/b").mkdir(mode=0o700, parents=True)
    (workspace / "src/a/.keep").write_text("a\n", encoding="utf-8")
    (workspace / "src/b/.keep").write_text("b\n", encoding="utf-8")
    git_init = subprocess.run(
        ["git", "init", "--quiet"],
        cwd=workspace,
        capture_output=True,
        check=False,
        timeout=10.0,
    )
    if git_init.returncode != 0:
        raise AssertionError(
            "fixture workspace git init failed: "
            + git_init.stderr.decode(errors="replace")
        )
    git_add = subprocess.run(
        ["git", "add", "--all"],
        cwd=workspace,
        capture_output=True,
        check=False,
        timeout=10.0,
    )
    if git_add.returncode != 0:
        raise AssertionError(
            "fixture workspace git add failed: "
            + git_add.stderr.decode(errors="replace")
        )
    prompts = root / "program-prompts"
    prompts.mkdir(mode=0o700)
    for name in ("worker-a", "worker-b", "reviewer-a", "reviewer-b"):
        (prompts / f"{name}.md").write_text(
            f"fixture instructions for {name}\n", encoding="utf-8"
        )

    verify_code = (
        "from pathlib import Path; "
        "assert Path('src/a/output.txt').read_text(encoding='utf-8') == "
        "'fixture output task-a\\n'; "
        "assert Path('src/b/output.txt').read_text(encoding='utf-8') == "
        "'fixture output task-b\\n'"
    )
    verification = {
        "task-a": ["python3", "-c", verify_code],
        "task-b": ["python3", "-c", verify_code],
    }
    task_blocks = []
    for task_id, allowed in (("task-a", "src/a"), ("task-b", "src/b")):
        argv = ", ".join(json.dumps(item) for item in verification[task_id])
        task_blocks.append(
            "\n".join(
                (
                    "[[teams.program.tasks]]",
                    f'task_id = "{task_id}"',
                    f'objective = "Complete {task_id} in its declared scope."',
                    'acceptance_criteria = ["the fixture task reaches completed"]',
                    f'allowed_paths = ["{allowed}"]',
                    'forbidden_paths = [".secrets"]',
                    "dependencies = []",
                    'evidence_requirements = ["record the fixed verification command"]',
                    'consultation_conditions = ["ask before leaving the declared scope"]',
                    "",
                    "[[teams.program.tasks.verification]]",
                    'name = "fixed-success"',
                    f"argv = [{argv}]",
                    "timeout_seconds = 10",
                )
            )
        )

    node_blocks = []
    for node_id, kind, model, effort, permission in (
        ("worker-a", "worker", "fixture-worker-a", "high", "workspace-write"),
        ("worker-b", "worker", "fixture-worker-b", "medium", "workspace-write"),
        ("reviewer-a", "reviewer", "fixture-reviewer-a", "low", "read-only"),
        ("reviewer-b", "reviewer", "fixture-reviewer-b", "high", "read-only"),
    ):
        node_blocks.append(
            "\n".join(
                (
                    "[[teams.program.nodes]]",
                    f'id = "{node_id}"',
                    f'label = "{node_id}"',
                    f'kind = "{kind}"',
                    "",
                    "[teams.program.nodes.role_spec]",
                    'provider = "claude"',
                    'transport = "acp"',
                    f'model = "{model}"',
                    f'effort = "{effort}"',
                    f'prompt = "program-prompts/{node_id}.md"',
                    f'permission = "{permission}"',
                )
            )
        )

    config = root / "program-v5.toml"
    config.write_text(
        "\n".join(
            (
                "version = 5",
                f'runtime = "{runtime}"',
                "",
                "[teams.program]",
                'name = "Fixture Parallel Program"',
                "max_review_rounds = 1",
                "",
                *node_blocks,
                "",
                "[[teams.program.edges]]",
                'source = "worker-a"',
                'target = "reviewer-a"',
                'kind = "reviewed-by"',
                "",
                "[[teams.program.edges]]",
                'source = "worker-b"',
                'target = "reviewer-b"',
                'kind = "reviewed-by"',
                "",
                "[teams.program.coordination]",
                'mode = "program"',
                'entry_nodes = ["worker-a", "worker-b"]',
                'dispatch_mode = "parallel"',
                "max_active = 2",
                "",
                *task_blocks,
                "",
                "[[teams.program.routes]]",
                'task_id = "task-a"',
                'implementation_writer = "worker-a"',
                'implementation_reviewer = "reviewer-a"',
                "",
                "[[teams.program.routes]]",
                'task_id = "task-b"',
                'implementation_writer = "worker-b"',
                'implementation_reviewer = "reviewer-b"',
                "",
            )
        ),
        encoding="utf-8",
    )
    return workspace, config, verification


class LiveParallelProgramContractTest(unittest.TestCase):
    """Run the bounded real-terminal acceptance contract when explicitly gated."""

    maxDiff = None

    def setUp(self) -> None:
        if not _RUN_PROGRAM:
            self.fail("set AGENT_TEAM_RUN_LIVE_PROGRAM=1 for the explicit live test")
        if _RUNTIME not in _RUNTIMES:
            self.fail(
                "set AGENT_TEAM_LIVE_RUNTIME explicitly to tmux, herdr, or zellij"
            )
        # LiveNativeContractTest performs the native gate itself and fails
        # closed when AGENT_TEAM_RUN_LIVE_NATIVE is absent.
        fixture = _live_native_contract.LiveNativeContractTest(methodName="runTest")
        fixture.setUp()
        self.fixture = fixture
        self._program_workspaces: dict[Path, Path] = {}
        self._verified_clean_runs: set[Path] = set()
        self._preserve_fixture_root = False
        self.addCleanup(self._tear_down_program)
        _write_parallel_provider(fixture.node_bin / "node", fixture.root)
        git = shutil.which("git")
        if git is None:
            self.fail("parallel program fixture requires git for workspace revisions")
        (fixture.fake_bin / "git").symlink_to(git)
        self.evidence: list[dict[str, object]] = []

    def _events(self) -> list[dict[str, object]]:
        path = self.fixture.root / "parallel-provider-events.jsonl"
        if not path.is_file():
            return []
        try:
            raw = path.read_bytes()
            decoded = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            self.fail(f"fake provider event log cannot be read: {exc}")
        records = decoded.splitlines(keepends=True)
        if records and not records[-1].endswith("\n"):
            if not self._provider_writer_active():
                self.fail("unterminated provider event without an active writer")
            records.pop()
        events: list[dict[str, object]] = []
        for record in records:
            if not record.endswith("\n"):
                self.fail("provider event record is not newline terminated")
            line = record[:-1]
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                self.fail(f"malformed provider event record: {exc}")
            if not isinstance(value, dict):
                self.fail("provider event record is not an object")
            events.append(cast(dict[str, object], value))
        if len(events) > 128:
            self.fail("provider event log exceeded the bounded 128-record contract")
        return events

    def _provider_writer_active(self) -> bool:
        active_dir = self.fixture.root / "provider-active"
        for marker in active_dir.glob("*.active"):
            try:
                pid = int(marker.read_text(encoding="ascii").strip())
            except (OSError, ValueError):
                continue
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            except OSError:
                return True
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
        self.fail(f"timed out waiting for fake provider events: {self._events()!r}")

    @staticmethod
    def _workspace_fingerprint(workspace: Path) -> str:
        digest = hashlib.sha256()
        for path in sorted(item for item in workspace.rglob("*") if item.is_file()):
            digest.update(str(path.relative_to(workspace)).encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
        return digest.hexdigest()

    def _snapshot(
        self, state_path: Path, label: str, *, workspace: Path | None = None
    ) -> dict[str, object]:
        state = cast(
            dict[str, object], json.loads(state_path.read_text(encoding="utf-8"))
        )
        roles: dict[str, object] = {}
        raw_roles = state.get("roles")
        if isinstance(raw_roles, dict):
            for node_id, raw in raw_roles.items():
                if not isinstance(node_id, str) or not isinstance(raw, dict):
                    continue
                task_spec = raw.get("task_spec")
                task_id = (
                    task_spec.get("task_id") if isinstance(task_spec, dict) else None
                )
                roles[node_id] = {
                    "task_id": task_id,
                    "provider_task_id": raw.get("task_id"),
                    "role": raw.get("role"),
                    "role_kind": raw.get("role_kind"),
                    "dispatch_id": raw.get("dispatch_id"),
                    "launch_nonce": raw.get("launch_nonce"),
                    "runner_pid": raw.get("runner_pid"),
                    "runner_process_group_id": raw.get("runner_process_group_id"),
                    "runner_argv": raw.get("runner_argv"),
                    "adapter_id": raw.get("adapter_id"),
                    "task_revision": raw.get("task_revision"),
                    "pending_delivery_id": raw.get("pending_delivery_id"),
                    "pending_delivery_kind": raw.get("pending_delivery_kind"),
                    "pending_delivery_stage": raw.get("pending_delivery_stage"),
                    "native_question": raw.get("native_question"),
                    "native_result": raw.get("native_result"),
                    "provider_private_root": raw.get("provider_private_root"),
                    "snapshot_root": raw.get("snapshot_root"),
                    "prompt_path": raw.get("prompt_path"),
                    "question_socket": raw.get("question_socket"),
                }
        tasks: dict[str, object] = {}
        raw_tasks = state.get("tasks")
        if isinstance(raw_tasks, dict):
            for task_id, raw in raw_tasks.items():
                if not isinstance(task_id, str) or not isinstance(raw, dict):
                    continue
                tasks[task_id] = {
                    "status": raw.get("status"),
                    "revision": raw.get("revision"),
                    "task_evidence": raw.get("task_evidence"),
                    "review_result": raw.get("review_result"),
                    "writer_result": raw.get("writer_result"),
                    "verification": raw.get("verification"),
                    "dispatch_id": raw.get("dispatch_id"),
                }
        native = state.get("native")
        native_summary: dict[str, object] = {}
        if isinstance(native, dict):
            native_summary = {
                "phase": native.get("phase"),
                "coordinator_process": native.get("coordinator_process"),
                "coordinator_argv": native.get("coordinator_argv"),
                "supervisor_argv": native.get("supervisor_argv"),
                "run_nonce": native.get("run_nonce"),
                "startup_socket_path": native.get("startup_socket_path"),
            }
        event_bindings: list[dict[str, object]] = []
        for event in self._events():
            role = event.get("role")
            assignment = raw_roles.get(role) if isinstance(raw_roles, dict) else None
            if not isinstance(role, str) or not isinstance(assignment, dict):
                continue
            task_spec = assignment.get("task_spec")
            task_id = task_spec.get("task_id") if isinstance(task_spec, dict) else None
            event_bindings.append(
                {
                    "run_id": state.get("run_id"),
                    "node_id": role,
                    "task_id": task_id,
                    "dispatch_id": assignment.get("dispatch_id"),
                    "provider_pid": event.get("pid"),
                    "event": event.get("event"),
                    "launch_nonce": event.get("launch_nonce"),
                    "marker": event.get("marker"),
                }
            )
        evidence = {
            "label": label,
            "fixture_root": str(self.fixture.root),
            "state_path": str(state_path),
            "workspace": state.get("workspace"),
            "runtime": state.get("runtime"),
            "run_id": state.get("run_id"),
            "version": state.get("version"),
            "graph": state.get("graph"),
            "program_wave": state.get("program_wave"),
            "native": native_summary,
            "roles": roles,
            "tasks": tasks,
            "event_bindings": event_bindings,
            "events": self._events(),
        }
        backend = self.fixture._terminal_backend(state)
        if isinstance(native, dict) and backend._receipt_key in native:
            receipt = backend._receipt_from_state(native)
            evidence["terminal_receipt"] = receipt.as_dict()
            evidence["terminal_socket_root"] = str(backend._socket_root(receipt))
        if workspace is not None:
            evidence["workspace_fingerprint"] = self._workspace_fingerprint(workspace)
        if len(self.evidence) >= 64:
            self.fail("parallel state evidence exceeded its 64-snapshot bound")
        self.evidence.append(evidence)
        self._write_evidence()
        return state

    def _write_evidence(self) -> None:
        payload = json.dumps(self.evidence, ensure_ascii=False, indent=2)
        (self.fixture.root / "parallel-evidence.json").write_text(
            payload, encoding="utf-8"
        )
        configured = os.environ.get("AGENT_TEAM_LIVE_EVIDENCE_DIR")
        if configured:
            output_dir = Path(configured).expanduser().resolve()
            output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            output_path = output_dir / f"{self._testMethodName}.json"
            output_path.write_text(payload, encoding="utf-8")

    def _start_program(
        self, name: str
    ) -> tuple[Path, Path, dict[str, object], dict[str, list[str]]]:
        workspace, config, verification = _program_config(
            self.fixture.root / name, _RUNTIME or ""
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
                    "program",
                    "--no-attach",
                ],
                timeout=30.0,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._register_partial_states(workspace)
            if not self._program_workspaces:
                self._preserve_fixture_root = True
            self.fail(f"program start did not return: {exc!r}")
        if result.returncode != 0:
            self._register_partial_states(workspace)
            if not self._program_workspaces:
                self._preserve_fixture_root = True
            self.fail(
                f"program start failed: stdout={result.stdout!r} stderr={result.stderr!r}"
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            self._register_partial_states(workspace)
            if not self._program_workspaces:
                self._preserve_fixture_root = True
            self.fail(f"program start returned invalid JSON: {exc}")
        if not isinstance(payload, dict):
            self._register_partial_states(workspace)
            if not self._program_workspaces:
                self._preserve_fixture_root = True
            self.fail(f"program start returned invalid JSON: {result.stdout!r}")
        raw_state_path = payload.get("state_path")
        if not isinstance(raw_state_path, str) or not raw_state_path:
            self._register_partial_states(workspace)
            if not self._program_workspaces:
                self._preserve_fixture_root = True
            self.fail(f"program start response omitted state_path: {payload!r}")
        state_path = Path(raw_state_path)
        self._register_owned_state(state_path, workspace)
        state = self.fixture._wait_state(
            state_path,
            lambda item: (
                item.get("version") == 5
                and item.get("run_id")
                and isinstance(item.get("native"), dict)
                and item["native"].get("phase") == "running"
                and isinstance(item["native"].get("coordinator_process"), dict)
                and item["native"]["coordinator_process"].get("phase") == "running"
                and isinstance(
                    item["native"]["coordinator_process"].get("coordinator_pid"), int
                )
                and isinstance(item.get("graph"), dict)
                and item["graph"].get("coordination", {}).get("mode") == "program"
                and item["graph"].get("coordination", {}).get("dispatch_mode")
                == "parallel"
            ),
            timeout=30.0,
        )
        self._snapshot(state_path, "started", workspace=workspace)
        self.assertEqual(payload.get("status"), "running")
        return workspace, state_path, state, verification

    def _register_owned_state(self, state_path: Path, workspace: Path) -> None:
        resolved = state_path.expanduser().resolve(strict=False)
        if resolved not in self._program_workspaces:
            self.fixture._owned_state_paths.append(resolved)
        self._program_workspaces[resolved] = workspace

    def _register_partial_states(self, workspace: Path) -> None:
        for state_path in self.fixture.xdg_state.rglob("state.json"):
            if state_path.is_file():
                self._register_owned_state(state_path, workspace)

    def _tear_down_program(self) -> None:
        """Stop owned program runs before base cleanup, preserving uncertainty."""

        if self._preserve_fixture_root:
            print(
                f"LIVE_PARALLEL_EVIDENCE_PRESERVED root={self.fixture.root} "
                "reason=program-start-identity-unresolved",
                file=sys.stderr,
            )
            return
        for state_path, workspace in self._program_workspaces.items():
            if not state_path.exists():
                if state_path not in self._verified_clean_runs:
                    print(
                        f"LIVE_PARALLEL_EVIDENCE_PRESERVED root={self.fixture.root} "
                        f"state={state_path} reason=post-stop-proof-unconfirmed",
                        file=sys.stderr,
                    )
                    return
                continue
            try:
                self._snapshot(
                    state_path, "failure-before-cleanup", workspace=workspace
                )
                result = self.fixture._run_cli(
                    [
                        "stop",
                        "--state",
                        str(state_path),
                        "--cwd",
                        str(workspace),
                    ],
                    timeout=30.0,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                self.fixture._last_failure_details += f" program-stop={exc!r}"
                print(
                    f"LIVE_PARALLEL_EVIDENCE_PRESERVED state={state_path} "
                    f"reason=program-stop-exception:{exc!r}",
                    file=sys.stderr,
                )
                return
            if result.returncode != 0 or state_path.exists():
                self.fixture._last_failure_details += (
                    f" program-stop-returncode={result.returncode}"
                )
                print(
                    f"LIVE_PARALLEL_EVIDENCE_PRESERVED state={state_path} "
                    f"returncode={result.returncode} stderr={result.stderr!r}",
                    file=sys.stderr,
                )
                return
            print(
                f"LIVE_PARALLEL_EVIDENCE_PRESERVED root={self.fixture.root} "
                f"state={state_path} reason=failure-cleanup-requires-independent-proof",
                file=sys.stderr,
            )
            return
        self.fixture.tearDown()

    def _assert_program_resources_gone(self, state: dict[str, object]) -> None:
        native = state.get("native")
        if not isinstance(native, dict):
            self.fail("program state has no native mapping")
        process = native.get("coordinator_process")
        if not isinstance(process, dict):
            self.fail("program state has no coordinator_process")
        for key in ("supervisor_pid", "coordinator_pid"):
            pid = process.get(key)
            self.assertIsInstance(pid, int)
            self.fixture._wait_pid_gone(cast(int, pid))
        backend = self.fixture._terminal_backend(state)
        receipt = backend._receipt_from_state(native)
        self.fixture._wait_pid_gone(receipt.server_pid)
        self.fixture._wait_pid_gone(receipt.pane_pid)
        receipt_fields = receipt.as_dict()
        for key in ("socket_path", "config_path"):
            raw_path = receipt_fields.get(key)
            self.assertIsInstance(raw_path, str)
            self.assertFalse(Path(cast(str, raw_path)).exists(), key)
        self.assertFalse(backend._socket_root(receipt).exists())

    def _assert_evidence_paths_gone(self) -> None:
        runner_pids: set[int] = set()
        group_ids: set[int] = set()
        removed_paths: set[str] = set()
        for evidence in self.evidence:
            native = cast(dict[str, object], evidence["native"])
            process = native.get("coordinator_process")
            if (
                isinstance(process, dict)
                and type(process.get("process_group_id")) is int
            ):
                group_ids.add(cast(int, process["process_group_id"]))
            for role in cast(dict[str, object], evidence["roles"]).values():
                if not isinstance(role, dict):
                    continue
                if type(role.get("runner_pid")) is int:
                    runner_pids.add(cast(int, role["runner_pid"]))
                if type(role.get("runner_process_group_id")) is int:
                    group_ids.add(cast(int, role["runner_process_group_id"]))
                for key in (
                    "provider_private_root",
                    "snapshot_root",
                    "prompt_path",
                    "question_socket",
                ):
                    path = role.get(key)
                    if isinstance(path, str):
                        self.assertFalse(
                            Path(path).exists() or Path(path).is_symlink(),
                            f"{key}: {path}",
                        )
                        removed_paths.add(path)
        events = self._events()
        provider_pids = {cast(int, event["pid"]) for event in events}
        group_ids.update(cast(int, event["pgid"]) for event in events)
        for pid in sorted(runner_pids | provider_pids):
            self.assertGreater(pid, 1)
            self.fixture._wait_pid_gone(pid)
        for pgid in sorted(group_ids):
            self.assertGreater(pgid, 1)
            deadline = time.monotonic() + 5.0
            while True:
                try:
                    os.killpg(pgid, 0)
                except ProcessLookupError:
                    break
                if time.monotonic() >= deadline:
                    self.fail(f"owned fixture process group remains: pgid={pgid}")
                time.sleep(0.05)
        self.evidence[-1]["post_stop"] = {
            "runner_pids_gone": sorted(runner_pids),
            "provider_pids_gone": sorted(provider_pids),
            "process_groups_gone": sorted(group_ids),
            "paths_gone": sorted(removed_paths),
            "events": events,
        }
        self._write_evidence()

    def _assert_assignment_event_bindings(
        self,
        state: dict[str, object],
        events: list[dict[str, object]],
        event_name: str,
    ) -> None:
        roles = state.get("roles")
        if not isinstance(roles, dict):
            self.fail("parallel state has no roles mapping")
        selected = [event for event in events if event.get("event") == event_name]
        self.assertTrue(selected, f"missing provider events {event_name!r}")
        observations = []
        for event in selected:
            role = event.get("role")
            if not isinstance(role, str) or not isinstance(roles.get(role), dict):
                self.fail(f"provider event is not bound to a live node: {event!r}")
            assignment = cast(dict[str, object], roles[role])
            task_spec = assignment.get("task_spec")
            task_id = task_spec.get("task_id") if isinstance(task_spec, dict) else None
            self.assertEqual(event.get("task_id"), task_id)
            nonce = assignment.get("launch_nonce")
            self.assertEqual(event.get("launch_nonce"), nonce)
            self.assertIsInstance(nonce, str)
            self.assertEqual(
                event.get("marker"),
                f"agent-team/{state['team_id']}/{role}/{nonce}",
            )
            self.assertIsInstance(assignment.get("dispatch_id"), str)
            self.assertTrue(cast(str, assignment["dispatch_id"]))
            expected_adapter = (
                "claude-acp-scoped-0.70.0"
                if assignment.get("role_kind") == "worker"
                else "claude-acp-0.70.0"
            )
            self.assertEqual(assignment.get("adapter_id"), expected_adapter)
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
            self.assertNotEqual(provider_pid, assignment.get("runner_pid"))
            provider_argv = read_process_argv(cast(int, provider_pid))
            self.assertIsNotNone(provider_argv)
            self.assertIn(
                str((self.fixture.node_bin / "node").resolve()), provider_argv
            )
            self.assertIn("--launch-nonce", provider_argv)
            self.assertIn(cast(str, nonce), provider_argv)
            self.assertEqual(event.get("pgid"), os.getpgid(cast(int, provider_pid)))
            self.assertEqual(event["pgid"], provider_pid)
            self.assertNotEqual(event["pgid"], assignment["runner_process_group_id"])
            parent_pid = int(
                subprocess.check_output(
                    ["ps", "-o", "ppid=", "-p", str(provider_pid)],
                    text=True,
                    timeout=5.0,
                ).strip()
            )
            self.assertEqual(parent_pid, runner_pid)
            spec = cast(dict[str, dict[str, object]], state["role_specs"])[role]
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
                provider_argv, python_process_argv([sys.executable, *expected_client])
            )
            observations.append(
                {
                    "role": role,
                    "pid": provider_pid,
                    "pgid": event["pgid"],
                    "argv": list(provider_argv),
                    "launch_nonce": nonce,
                    "runner_pid": runner_pid,
                    "runner_argv": list(cast(tuple[str, ...], runner_argv)),
                    "runner_process_group_id": assignment["runner_process_group_id"],
                    "parent_pid": parent_pid,
                }
            )
        self.evidence[-1]["live_provider_processes"] = observations
        self._write_evidence()

    def _assert_writer_outputs(
        self,
        workspace: Path,
        events: list[dict[str, object]],
        expected_roles: set[str],
    ) -> None:
        expected = {
            "worker-a": ("src/a/output.txt", "fixture output task-a\n"),
            "worker-b": ("src/b/output.txt", "fixture output task-b\n"),
        }
        completions = [event for event in events if event["event"] == "writer-complete"]
        self.assertEqual(len(completions), len(expected_roles))
        self.assertEqual({event["role"] for event in completions}, expected_roles)
        for event in completions:
            relative, content = expected[cast(str, event["role"])]
            self.assertEqual(event["output_path"], relative)
            output = workspace / relative
            self.assertEqual(output.read_text(encoding="utf-8"), content)
            self.assertEqual(
                event["output_sha256"], hashlib.sha256(output.read_bytes()).hexdigest()
            )

    def test_parallel_writers_review_verify_and_cross_process_stop(self) -> None:
        workspace, state_path, initial, verification = self._start_program("normal")
        native = cast(dict[str, object], initial["native"])
        graph = cast(dict[str, object], initial["graph"])
        nodes = cast(list[dict[str, object]], graph["nodes"])
        self.assertEqual(initial["version"], 5)
        self.assertEqual({node["kind"] for node in nodes}, {"worker", "reviewer"})
        self.assertNotIn("main", {node["kind"] for node in nodes})
        role_specs = cast(dict[str, dict[str, object]], initial["role_specs"])
        for node_id, model, effort, permission in (
            ("worker-a", "fixture-worker-a", "high", "workspace-write"),
            ("worker-b", "fixture-worker-b", "medium", "workspace-write"),
            ("reviewer-a", "fixture-reviewer-a", "low", "read-only"),
            ("reviewer-b", "fixture-reviewer-b", "high", "read-only"),
        ):
            self.assertEqual(role_specs[node_id]["model"], model)
            self.assertEqual(role_specs[node_id]["effort"], effort)
            self.assertEqual(role_specs[node_id]["permission"], permission)
            self.assertEqual(role_specs[node_id]["transport"], "acp")
        coordinator_process = native.get("coordinator_process")
        self.assertIsInstance(coordinator_process, dict)
        self.assertIsInstance(
            cast(dict[str, object], coordinator_process).get("coordinator_pid"), int
        )
        coordinator_argv = native.get("coordinator_argv")
        self.assertIsInstance(coordinator_argv, list)
        self.assertIn("_program-run", cast(list[object], coordinator_argv))
        self.assertIn(
            cast(str, initial["run_id"]), cast(list[object], coordinator_argv)
        )
        before_fingerprint = self._workspace_fingerprint(workspace)
        before_revision = snapshot_revision(workspace)
        self.assertFalse((workspace / "src/a/output.txt").exists())
        self.assertFalse((workspace / "src/b/output.txt").exists())

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
        running = self._snapshot(
            state_path, "both-writers-running", workspace=workspace
        )
        self.assertEqual(
            set(cast(dict[str, object], running["roles"])), {"worker-a", "worker-b"}
        )
        self._assert_assignment_event_bindings(running, writer_events, "writer-start")
        pids = {
            cast(int, event["pid"])
            for event in writer_events
            if event.get("event") == "writer-start"
            and isinstance(event.get("pid"), int)
        }
        self.assertEqual(len(pids), 2, "writer provider PIDs must prove overlap")
        self.assertEqual(
            len(
                {
                    cast(int, event["pgid"])
                    for event in writer_events
                    if event.get("event") == "writer-start"
                }
            ),
            2,
            "writer provider process groups must prove overlap",
        )
        (self.fixture.root / "release-writers").write_text(
            "release\n", encoding="utf-8"
        )
        finished_writers = self._wait_events(
            lambda events: (
                {
                    event.get("role")
                    for event in events
                    if event.get("event") == "writer-complete"
                }
                == {"worker-a", "worker-b"}
            )
        )
        after_writer_fingerprint = self._workspace_fingerprint(workspace)
        after_writer_revision = snapshot_revision(workspace)
        self.assertNotEqual(
            before_fingerprint,
            after_writer_fingerprint,
            "writers must change the declared workspace revision",
        )
        self.assertNotEqual(before_revision, after_writer_revision)
        self._assert_writer_outputs(
            workspace, finished_writers, {"worker-a", "worker-b"}
        )

        sealed = self.fixture._wait_state(
            state_path,
            lambda item: (
                isinstance(item.get("program_wave"), dict)
                and item["program_wave"].get("phase") == "reviewers"
                and isinstance(item["program_wave"].get("revision"), str)
                and len(item["program_wave"]["revision"]) == 64
            ),
            timeout=60.0,
        )
        self._snapshot(state_path, "writer-wave-sealed", workspace=workspace)
        wave = cast(dict[str, object], sealed["program_wave"])
        revision = cast(str, wave["revision"])
        self.assertEqual(revision, after_writer_revision)
        tasks = cast(dict[str, object], sealed["tasks"])
        for task_id in ("task-a", "task-b"):
            record = cast(dict[str, object], tasks[task_id])
            self.assertIn(
                record["status"],
                {
                    "awaiting_implementation_review",
                    "reviewing_implementation",
                    "implementation_approved",
                    "completed",
                },
            )
            writer_result = cast(dict[str, object], record["writer_result"])
            self.assertIsInstance(writer_result.get("body"), str)
            self.assertIsInstance(writer_result.get("dispatch_id"), str)

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
        reviewing = self._snapshot(
            state_path, "both-reviewers-running", workspace=workspace
        )
        self.assertEqual(
            set(cast(dict[str, object], reviewing["roles"])),
            {"reviewer-a", "reviewer-b"},
        )
        self._assert_assignment_event_bindings(
            reviewing, reviewer_events, "reviewer-start"
        )
        for event in reviewer_events:
            if event.get("event") == "reviewer-start":
                self.assertEqual(event.get("revision"), revision)
            if event.get("event") == "reviewer-start":
                self.assertIsNone(event.get("verified_outputs"))
        for assignment in cast(
            dict[str, dict[str, object]], reviewing["roles"]
        ).values():
            self.assertEqual(assignment.get("task_revision"), revision)
            self.assertEqual(assignment.get("adapter_id"), "claude-acp-0.70.0")
        (self.fixture.root / "release-reviewers").write_text(
            "release\n", encoding="utf-8"
        )
        self._wait_events(
            lambda events: (
                {
                    event.get("role")
                    for event in events
                    if event.get("event") == "reviewer-complete"
                }
                == {"reviewer-a", "reviewer-b"}
            )
        )
        after_review_fingerprint = self._workspace_fingerprint(workspace)
        self.assertEqual(after_review_fingerprint, after_writer_fingerprint)
        self.assertEqual(snapshot_revision(workspace), after_writer_revision)
        reviewer_completions = [
            event
            for event in self._events()
            if event.get("event") == "reviewer-complete"
        ]
        self.assertEqual(
            {
                tuple(cast(list[str], event["verified_outputs"]))
                for event in reviewer_completions
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
                and isinstance(item["native"].get("coordinator_process"), dict)
                and item["native"]["coordinator_process"].get("phase") == "exited"
                and item["native"]["coordinator_process"].get("group_stopped") is True
            ),
            timeout=90.0,
        )
        self._snapshot(state_path, "completed-before-public-stop", workspace=workspace)
        final_tasks = cast(dict[str, dict[str, object]], final_state["tasks"])
        for task_id, expected_argv in verification.items():
            record = final_tasks[task_id]
            self.assertEqual(record.get("revision"), revision)
            evidence = cast(dict[str, object], record["task_evidence"])
            self.assertEqual(evidence.get("decision"), "approve")
            self.assertEqual(evidence.get("revision"), revision)
            review = cast(dict[str, object], record["review_result"])
            self.assertEqual(review.get("task_evidence"), evidence)
            evidence = cast(dict[str, object], record["verification"])
            self.assertEqual(evidence.get("revision"), revision)
            self.assertTrue(evidence.get("passed"))
            self.assertTrue(evidence.get("cleanup_confirmed"))
            commands = cast(list[dict[str, object]], evidence["commands"])
            self.assertEqual(commands[0].get("argv"), expected_argv)
            self.assertEqual(commands[0].get("returncode"), 0)
        self.assertEqual(
            cast(dict[str, object], final_state["program_wave"])["revision"], revision
        )
        self.assertEqual(
            cast(dict[str, object], final_state["program_wave"])["phase"],
            "verification",
        )

        stop = self.fixture._run_cli(
            ["stop", "--state", str(state_path), "--cwd", str(workspace)],
            timeout=30.0,
        )
        self.assertEqual(stop.returncode, 0, stop.stderr)
        self.assertFalse(state_path.exists())
        self._assert_program_resources_gone(final_state)
        self._assert_evidence_paths_gone()
        self.assertEqual(self._events()[-1].get("event"), "reviewer-complete")
        self._verified_clean_runs.add(state_path.resolve(strict=False))

    def test_question_peer_progress_then_public_stop_preserves_no_answer_or_ack(
        self,
    ) -> None:
        (self.fixture.root / "question-scenario").write_text(
            "enabled\n", encoding="utf-8"
        )
        workspace, state_path, _initial, _verification = self._start_program("question")
        started = self._wait_events(
            lambda current: (
                any(event.get("event") == "question-published" for event in current)
                and any(
                    event.get("event") == "writer-start"
                    and event.get("role") == "worker-b"
                    for event in current
                )
            )
        )
        both_active = self._snapshot(
            state_path, "question-pending-both-runners-active", workspace=workspace
        )
        self.assertEqual(
            set(cast(dict[str, object], both_active["roles"])), {"worker-a", "worker-b"}
        )
        self._assert_assignment_event_bindings(both_active, started, "writer-start")
        (self.fixture.root / "release-question-peer").touch()
        events = self._wait_events(
            lambda current: (
                any(
                    event.get("event") == "question-published"
                    and event.get("role") == "worker-a"
                    for event in current
                )
                and any(
                    event.get("event") == "writer-complete"
                    and event.get("role") == "worker-b"
                    for event in current
                )
            ),
            timeout=60.0,
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
                and isinstance(item["tasks"].get("task-b"), dict)
                and item["tasks"]["task-b"].get("status")
                == "awaiting_implementation_review"
                and "worker-b" not in item["roles"]
            ),
            timeout=60.0,
        )
        self._snapshot(
            state_path, "question-observed-peer-completed", workspace=workspace
        )
        self._assert_assignment_event_bindings(pending, events, "question-published")
        self._assert_writer_outputs(workspace, events, {"worker-b"})
        self.assertFalse((workspace / "src/a/output.txt").exists())
        pending_roles = cast(dict[str, object], pending["roles"])
        worker_a = cast(dict[str, object], pending_roles["worker-a"])
        question = cast(dict[str, object], worker_a["native_question"])
        self.assertEqual(question.get("answers"), {})
        self.assertFalse(
            any(event.get("event") == "question-answered" for event in events)
        )
        self.assertFalse(
            any(event.get("event") == "question-recorded" for event in events)
        )
        self.assertTrue(
            any(
                event.get("event") == "writer-complete"
                and event.get("role") == "worker-b"
                for event in events
            )
        )

        stop = self.fixture._run_cli(
            ["stop", "--state", str(state_path), "--cwd", str(workspace)],
            timeout=30.0,
        )
        self.assertEqual(stop.returncode, 0, stop.stderr)
        self.assertFalse(state_path.exists())
        self._assert_program_resources_gone(pending)
        self._assert_evidence_paths_gone()
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
        self._verified_clean_runs.add(state_path.resolve(strict=False))


if __name__ == "__main__":
    unittest.main()
