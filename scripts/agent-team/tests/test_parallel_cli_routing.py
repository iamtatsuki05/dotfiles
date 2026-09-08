from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

import test_config_v5_cli as config_support
import test_native_acp_runner as runner_support
import test_parallel_native_backend as parallel_backend_support

from agent_team import cli, native_backend, native_main, native_mcp
from agent_team.adapters import ProcessResult
from agent_team.config_v5 import load_v5_config_data
from agent_team.contracts import NodeRef, Role, RoleGet, RoleStatusReceipt
from agent_team.named_graph import Coordination, GraphSpec
from agent_team.workflow import WorkflowEngine


def _write_v5_fixture(root: Path, *, agent_parallel: bool = False) -> tuple[Path, Path]:
    workspace = root / "workspace"
    workspace.mkdir()
    prompts = root / "prompts"
    prompts.mkdir()
    for name in (
        "main.md",
        "implementation-a.md",
        "implementation-b.md",
        "review-a.md",
        "review-b.md",
    ):
        (prompts / name).write_text(f"prompt for {name}\n", encoding="utf-8")
    content = config_support._v5_config_text()
    if agent_parallel:
        content = content.replace(
            'dispatch_mode = "serial"', 'dispatch_mode = "parallel"', 1
        )
    config_path = root / "config-v5.toml"
    config_path.write_text(content, encoding="utf-8")
    return workspace, config_path


def _parallel_graph(*, dispatch_mode: str = "parallel") -> GraphSpec:
    return GraphSpec(
        nodes=(
            NodeRef("implementation-a", Role.WORKER),
            NodeRef("implementation-b", Role.WORKER),
        ),
        edges=(),
        coordination=Coordination(
            "program", ("implementation-a", "implementation-b"), dispatch_mode, 2
        ),
        routes=(),
    )


def _native_state(*, version: int, graph: GraphSpec | None = None) -> dict[str, object]:
    if version == 3:
        return {
            "version": 3,
            "runtime": "tmux",
            "run_id": "run-v3",
            "native": {
                "phase": "running",
                "run_nonce": "nonce1234",
                "main_argv": ["/usr/bin/true"],
            },
        }
    assert graph is not None
    return {
        "version": version,
        "runtime": "tmux",
        "run_id": f"run-v{version}",
        "graph": graph.as_dict(),
        "native": {
            "phase": "running",
            "run_nonce": "nonce1234",
            "coordinator_argv": ["/usr/bin/true"],
        },
    }


class ParallelCliRoutingTest(unittest.TestCase):
    def _parallel_plan(
        self,
        root: Path,
        *,
        team: str = "program",
        agent_parallel: bool = False,
    ) -> dict[str, object]:
        workspace, config_path = _write_v5_fixture(root, agent_parallel=agent_parallel)
        config = load_v5_config_data(
            config_path, tomllib.loads(config_path.read_text(encoding="utf-8"))
        )
        with mock.patch.dict(
            os.environ, {"XDG_STATE_HOME": str(root / "state")}, clear=False
        ):
            return cli._v5_runtime_plan(config, workspace, team)

    def test_program_parallel_plan_is_accepted_before_runtime_effect(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-parallel-cli-"
        ) as directory:
            plan = self._parallel_plan(Path(directory))

            # The plan gate is the side-effect boundary.  A valid program graph
            # must pass it before prerequisite probing or backend construction.
            cli._require_named_runtime_plan(plan)
            backend = config_support._RecordingBackend()
            engine = WorkflowEngine(backend)
            with (
                mock.patch.object(cli, "_start_prerequisites") as prerequisites,
                mock.patch.object(
                    cli, "_runtime_engine", return_value=(engine, backend)
                ),
            ):
                response = cli.start_team(plan, attach=False)

            prerequisites.assert_called_once_with(plan)
            self.assertEqual(response["status"], "started")
            self.assertIsNotNone(backend.start_spec)
            assert backend.start_spec is not None
            self.assertEqual(
                backend.start_spec.graph, GraphSpec.from_dict(plan["graph"])
            )
            self.assertEqual(
                backend.start_spec.graph.coordination.dispatch_mode, "parallel"
            )
            self.assertEqual(backend.start_spec.graph.coordination.max_active, 2)
            self.assertIsNone(backend.start_spec.graph.main_node)

    def test_agent_parallel_plan_reaches_selected_runtime_with_main_contract(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-agent-parallel-"
        ) as directory:
            plan = self._parallel_plan(
                Path(directory), team="build", agent_parallel=True
            )
            graph = GraphSpec.from_dict(plan["graph"])
            backend = config_support._RecordingBackend()
            with (
                mock.patch.object(cli, "_start_prerequisites") as prerequisites,
                mock.patch.object(
                    cli,
                    "_runtime_engine",
                    return_value=(WorkflowEngine(backend), backend),
                ),
            ):
                response = cli.start_team(plan, attach=False)
            prerequisites.assert_called_once_with(plan)
            self.assertEqual(response["status"], "started")
            self.assertEqual(backend.start_spec.graph, graph)
            self.assertIsNotNone(graph.main_node)
            self.assertEqual(graph.coordination.dispatch_mode, "parallel")
            instructions = plan["roles"][graph.main_node.node_id]["instructions"]
            self.assertIn("task_batch_open", instructions)
            main_argv = plan["roles"][graph.main_node.node_id]["argv"]
            batch_tool = "mcp__agent_team__task_batch_open"
            self.assertIn(
                batch_tool, main_argv[main_argv.index("--tools") + 1].split(",")
            )
            self.assertIn(
                batch_tool,
                main_argv[
                    main_argv.index("--allowedTools") + 1 : main_argv.index(
                        "--permission-mode"
                    )
                ],
            )
            self.assertIn("全作成担当", instructions)
            self.assertIn("質問中でも", instructions)
            self.assertIn("同じ集合", instructions)

    def test_empty_agent_parallel_catalog_is_rejected_before_prerequisites(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-empty-parallel-"
        ) as directory:
            plan = self._parallel_plan(
                Path(directory), team="build", agent_parallel=True
            )
            plan["task_specs"] = []
            plan["graph"]["routes"] = []
            with (
                mock.patch.object(cli, "_start_prerequisites") as prerequisites,
                mock.patch.object(cli, "_runtime_engine") as runtime,
                self.assertRaisesRegex(cli.ConfigError, "parallel.*TaskSpecs"),
            ):
                cli.start_team(plan, attach=False)
            prerequisites.assert_not_called()
            runtime.assert_not_called()

    def test_v5_saved_plan_reload_preserves_graph_node_kind_cap_and_no_main(
        self,
    ) -> None:
        fixture = parallel_backend_support.ParallelNativeBackendTest(
            methodName="runTest"
        )
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.start(max_active=2)
        state = native_backend.runtime_read_state(fixture.path)
        self.assertEqual(state["version"], 5)

        restored = cli._management_plan_from_state(state)
        spec = cli._start_spec(restored, attach=False)
        assert spec.graph is not None
        self.assertEqual(spec.graph.coordination.dispatch_mode, "parallel")
        self.assertEqual(spec.graph.coordination.max_active, 2)
        self.assertEqual(
            {(node.node_id, node.kind.value) for node in spec.graph.nodes},
            {
                ("implementation-a", "worker"),
                ("implementation-b", "worker"),
                ("review-a", "reviewer"),
                ("review-b", "reviewer"),
            },
        )
        self.assertIsNone(spec.graph.main_node)
        self.assertNotIn("main", restored["roles"])

    def test_runner_and_native_mcp_resolve_exact_v5_node_refs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-v5-routing-") as directory:
            root = Path(directory)
            plan = self._parallel_plan(root)
            state = {
                "version": 5,
                "runtime": "tmux",
                "run_id": "run-v5",
                "graph": plan["graph"],
                "task_specs": plan["task_specs"],
                "role_specs": plan["roles"],
            }
            expected = NodeRef("implementation-a", Role.WORKER)

            self.assertEqual(
                cli._runner_role_target(state, "implementation-a"), expected
            )
            with self.assertRaises(cli.ConfigError):
                cli._runner_role_target(
                    state, NodeRef("implementation-a", Role.REVIEWER)
                )

            class RecordingBackend:
                def __init__(self) -> None:
                    self.requests: list[object] = []

                def request(self, request: object) -> RoleStatusReceipt:
                    self.requests.append(request)
                    assert isinstance(request, RoleGet)
                    return RoleStatusReceipt(request.role, "running")

            backend = RecordingBackend()
            session = native_mcp.NativeMcpSession.__new__(native_mcp.NativeMcpSession)
            session.path = root / "state.json"
            session.run_id = "run-v5"
            session.runtime = "tmux"
            session.backend = backend
            with mock.patch.object(native_mcp, "read_state", return_value=state):
                response = session.execute("role_get", {"role": "implementation-a"})

            self.assertEqual(
                response,
                {
                    "role": {"node_id": "implementation-a", "kind": "worker"},
                    "status": "running",
                },
            )
            self.assertEqual(backend.requests, [RoleGet(expected)])

    def test_native_main_accepts_v3_v4_and_v5_controller_routing(self) -> None:
        states = (
            _native_state(version=3),
            _native_state(
                version=4,
                graph=GraphSpec(
                    nodes=(NodeRef("worker", Role.WORKER),),
                    edges=(),
                    coordination=Coordination("program", ("worker",), "serial", 1),
                    routes=(),
                ),
            ),
            _native_state(version=5, graph=_parallel_graph()),
        )
        for state in states:
            with self.subTest(version=state["version"]):
                self.assertIsNone(native_main._run_id(state, state["run_id"]))

    def test_v5_runner_rejects_assignment_question_session_mismatch(self) -> None:
        # Reuse the native ACP fixture so the regression reaches the final
        # receipt/question reconciliation after a real result artifact read.
        fixture = runner_support.NativeAcpRunnerTest(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        state, _executables, prompt_path = fixture._state()
        state_path = fixture.state_dir / "state.json"
        graph = GraphSpec(
            nodes=(NodeRef("planner", Role.PLANNER),),
            edges=(),
            coordination=Coordination("program", ("planner",), "parallel", 1),
            routes=(),
        )
        state["version"] = 5
        state["graph"] = graph.as_dict()
        role_specs = state["role_specs"]
        assert isinstance(role_specs, dict)
        role_specs.pop("main")
        role_specs["planner"]["kind"] = "planner"
        assignment = state["roles"]["planner"]
        assignment["role"] = "planner"
        assignment["role_kind"] = "planner"
        assignment["native_question"] = {
            "phase": "recorded",
            "request": {"session_id": "stale-question-session"},
        }
        receipt = {
            "output": "finished",
            "session_id": "final-session",
            "model": "fable",
            "effort": "high",
            "cleanup_confirmed": True,
        }
        fixture.write_client_result(receipt)

        with (
            mock.patch.object(cli, "client_argv", return_value=["/bin/true"]),
            mock.patch.object(
                cli,
                "_state_role_target",
                return_value=NodeRef("planner", Role.PLANNER),
            ),
            mock.patch.object(
                cli._NativeAcpClientRunner,
                "run",
                return_value=ProcessResult(0, json.dumps(receipt), ""),
            ),
            mock.patch.object(cli, "read_state", return_value=state),
            mock.patch.object(native_backend, "_assert_publisher"),
            mock.patch.object(
                native_backend,
                "publish_completion",
                side_effect=lambda *_args, **kwargs: kwargs["outcome"],
            ) as publish,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = cli._acp_run_turn(
                state=state,
                role="planner",
                state_path=state_path,
                task_id="task-1",
                dispatch_id="dispatch-1",
                terminal_handle="terminal-1",
                prompt_path=prompt_path,
                launch_nonce="planner1234",
                cancellation=fixture.cancellation,
            )

        self.assertEqual(result, 1)
        publish.assert_called_once()
        self.assertEqual(publish.call_args.kwargs["outcome"], "failed")
        self.assertIn("question session", publish.call_args.kwargs["body"])


if __name__ == "__main__":
    unittest.main()
