from __future__ import annotations

import copy
import json
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest import TestCase, mock

from agent_team import (
    cli,
    codex_acp,
    native_acp_dependencies,
    orca_dispatch,
    role_snapshot,
    scoped_acp,
)
from agent_team.contracts import (
    ErrorCode,
    NodeRef,
    Role,
    RolePrompt,
    RuntimeFailure,
    TaskDispatch,
)
from agent_team.named_graph import Coordination, GraphEdge, GraphSpec, TaskRoute
from agent_team.runtime import read_state as runtime_read_state
from agent_team.runtime import write_state as runtime_write_state
from agent_team.scoped_acp import native_profile
from agent_team.task_execution import task_digest, task_json
from agent_team.task_spec import TaskSpec, VerificationSpec


class _FakeExecutables:
    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.agent = Path(f"/private/{provider}-agent")
        self.node = Path(f"/private/{provider}-node")
        self.sdk = Path(f"/private/{provider}-sdk")
        self.library = Path(f"/private/{provider}-library")
        self.codex = Path(f"/private/{provider}-codex")

    def verify(self) -> None:
        return None

    def as_dict(self) -> dict[str, object]:
        return {"saved": self.provider}


class OrcaNamedDispatchTest(TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(
            self.stack.enter_context(__import__("tempfile").TemporaryDirectory())
        )
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.state_path = self.root / "state.json"
        self.state = self._state("claude")
        self.saved: list[dict[str, object]] = []
        self.effects: list[tuple[str, list[str]]] = []
        self.preflight_calls: list[str] = []
        self.adapter_calls: list[str] = []
        self.temp_counter = 0
        self._install_common_fakes()

    def _state(self, provider: str) -> dict[str, object]:
        snapshot: dict[str, object] = {}
        if provider == "codex":
            snapshot = {
                "runtime_sha256": {},
                "auth_path": "/private/auth.json",
                "auth_sha256": "a" * 64,
                "auth_expires_at": 2_000_000_000,
                "project_configs": {},
            }
        return {
            "version": 4,
            "runtime": "orca",
            "team_id": "team-1",
            "run_id": "run-1",
            "workspace": str(self.workspace),
            "state_path": str(self.state_path),
            "worktree_id": "wt-1",
            "main_terminal": "main-terminal",
            "launcher_path": "/private/agent-team",
            "graph": {"coordination": {"mode": "agent", "dispatch_mode": "serial"}},
            "roles": {},
            "tasks": {},
            "role_specs": {
                "worker-a": {
                    "kind": "worker",
                    "provider": provider,
                    "transport": "acp",
                    "model": "model-1",
                    "effort": "high",
                    "permission": "workspace-write",
                    "instructions": "worker instructions",
                    "execution": "background",
                    "adapter_id": (
                        "claude-acp-scoped-0.70.0"
                        if provider == "claude"
                        else "codex-acp-scoped-1.10.0"
                    ),
                    "acp_executables": {"saved": provider},
                    **(
                        {
                            "scoped_wrapper_sha256": "w" * 64,
                            "scoped_client_sha256": "c" * 64,
                            "scoped_policy_sha256": "p" * 64,
                            "scoped_question_client_sha256": "q" * 64,
                        }
                        if provider == "claude"
                        else {}
                    ),
                    **({"provider_snapshot": snapshot} if provider == "codex" else {}),
                }
            },
        }

    def _task(self) -> TaskSpec:
        return TaskSpec(
            task_id="task-a",
            objective="implement the task",
            acceptance_criteria=("the task is complete",),
            allowed_paths=("src",),
            forbidden_paths=("secrets",),
            dependencies=(),
            verification=(VerificationSpec("unit", ("python", "-m", "unittest"), 30),),
            evidence_requirements=("test output",),
            consultation_conditions=(),
        )

    def _install_common_fakes(self) -> None:
        self.stack.enter_context(
            mock.patch.object(
                orca_dispatch,
                "read_state",
                side_effect=lambda _path: copy.deepcopy(self.state),
            )
        )
        self.stack.enter_context(
            mock.patch.object(
                orca_dispatch, "write_state", side_effect=self._save_state
            )
        )
        self.stack.enter_context(
            mock.patch.object(
                orca_dispatch, "remove_owned_tree", side_effect=lambda _path: None
            )
        )
        self.stack.enter_context(
            mock.patch.object(
                orca_dispatch.tempfile, "mkdtemp", side_effect=self._mkdtemp
            )
        )
        self.stack.enter_context(
            mock.patch.object(
                orca_dispatch,
                "create_prompt_file",
                side_effect=self._create_prompt,
            )
        )
        self.stack.enter_context(
            mock.patch.object(
                role_snapshot, "preflight_scoped_role", side_effect=self._preflight
            )
        )
        self.stack.enter_context(
            mock.patch.object(
                native_acp_dependencies.NativeAcpExecutables,
                "from_dict",
                return_value=_FakeExecutables("claude"),
            )
        )
        self.stack.enter_context(
            mock.patch.object(
                native_acp_dependencies.CodexAcpExecutables,
                "from_dict",
                return_value=_FakeExecutables("codex"),
            )
        )
        self.stack.enter_context(
            mock.patch.object(
                orca_dispatch,
                "checked_digest",
                side_effect=self._scoped_digest,
            )
        )
        self.stack.enter_context(
            mock.patch.object(codex_acp, "verify_snapshot", return_value=None)
        )
        self.stack.enter_context(
            mock.patch.object(
                native_acp_dependencies,
                "adapter_snapshot",
                side_effect=lambda _exe: self._adapter("claude"),
            )
        )
        self.stack.enter_context(
            mock.patch.object(
                native_acp_dependencies,
                "codex_adapter_snapshot",
                side_effect=lambda _exe: self._adapter("codex"),
            )
        )
        self.stack.enter_context(
            mock.patch.object(
                cli, "acp_agent_command", return_value="claude-agent --policy"
            )
        )
        self.stack.enter_context(
            mock.patch.object(cli, "acp_session_name", return_value="session-worker-a")
        )
        self.stack.enter_context(
            mock.patch.object(
                cli, "acp_runner_command", return_value="agent-team _acp-run worker-a"
            )
        )
        self.stack.enter_context(
            mock.patch.object(
                scoped_acp,
                "create_write_policy",
                side_effect=self._create_write_policy,
            )
        )
        self.stack.enter_context(
            mock.patch.object(
                codex_acp,
                "prepare_assignment",
                side_effect=self._prepare_codex,
            )
        )
        self.stack.enter_context(
            mock.patch.object(
                codex_acp, "agent_command", return_value="codex-acp-agent"
            )
        )
        self.stack.enter_context(
            mock.patch.object(orca_dispatch.secrets, "token_hex", return_value="n" * 32)
        )
        self.stack.enter_context(
            mock.patch.object(orca_dispatch, "run_orca", side_effect=self._run_orca)
        )

    def _mkdtemp(
        self,
        *args: object,
        prefix: str | None = None,
        dir: str | None = None,
    ) -> str:
        if args:
            prefix = args[1] if len(args) > 1 else prefix
            dir = args[2] if len(args) > 2 else dir
        del dir
        prefix_text = prefix if isinstance(prefix, str) else "tmp-"
        path = self.root / f"{prefix_text}fixture-{self.temp_counter}"
        self.temp_counter += 1
        path.mkdir(mode=0o700)
        return str(path)

    def _create_prompt(
        self, state_dir: Path, role: NodeRef, launch_nonce: str, text: str
    ) -> Path:
        path = state_dir / f"prompt-{role.node_id}-{launch_nonce}.md"
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)
        return path

    def _create_write_policy(
        self,
        private_root: Path,
        _workspace: Path,
        _state_path: Path,
        _task: TaskSpec | None,
        _agent_entry: Path,
        *,
        permission: str,
    ) -> tuple[Path, str]:
        del permission
        return private_root / "write-policy.json", "p" * 64

    def _prepare_codex(
        self, *, private_root: Path, **_kwargs: object
    ) -> dict[str, object]:
        return {
            "write_policy_path": str(private_root / "write-policy.json"),
            "write_policy_sha256": "p" * 64,
            "codex_launch_path": str(private_root / "codex-launch.json"),
            "codex_launch_sha256": "l" * 64,
            "codex_proxy_path": str(private_root / "codex-proxy"),
            "codex_proxy_sha256": "x" * 64,
            "codex_config_snapshot": {},
        }

    def _save_state(
        self, _path: Path, state: dict[str, object], **_kwargs: object
    ) -> None:
        self.saved.append(copy.deepcopy(state))
        self.state = copy.deepcopy(state)
        self.effects.append(("save", []))

    def _adapter(self, provider: str) -> dict[str, object]:
        self.adapter_calls.append(provider)
        return {
            "adapter_id": f"{provider}-adapter",
            "revision": "fixture",
            "executable": f"/private/{provider}-sdk",
            "version": f"{provider}-version",
            "identity": {
                "device": 1,
                "inode": 2,
                "size": 3,
                "mtime_ns": 4,
                "sha256": "a" * 64,
            },
        }

    def _scoped_digest(self, path: Path) -> str:
        return {
            "claude_scoped_agent.mjs": "w" * 64,
            "scoped_acp_client.mjs": "c" * 64,
            "scoped_policy.mjs": "p" * 64,
            "scoped_question_client.mjs": "q" * 64,
        }[path.name]

    def _preflight(
        self,
        normalized: dict[str, object],
        role: object,
        workspace: Path,
        **_kwargs: object,
    ) -> None:
        del role, workspace
        self.preflight_calls.append(str(normalized["provider"]))

    def _prepare_dispatch(
        self, prepared: dict[str, object], request: TaskDispatch
    ) -> tuple[dict[str, object], str]:
        record: dict[str, object] = {
            "spec": request.task.as_dict(),
            "digest": "d" * 64,
            "dispatch_id": None,
            "status": "running",
            "stage": "implementation",
            "revision": None,
            "role": request.role.node_id,
            "role_kind": request.role.kind.value,
            "writer_role": request.role.node_id,
            "writer_kind": request.role.kind.value,
        }
        tasks = prepared.setdefault("tasks", {})
        assert isinstance(tasks, dict)
        tasks[request.task.task_id] = record
        return record, "prepared task prompt"

    def _run_orca(
        self, _state: dict[str, object], args: list[str], **_kwargs: object
    ) -> dict[str, object]:
        self.effects.append(("orca", list(args)))
        if args[:2] == ["orchestration", "task-create"]:
            return {"task": {"id": "remote-task"}}
        if args[:2] == ["terminal", "create"]:
            return {"terminal": {"handle": "terminal-1"}}
        if args[:2] == ["orchestration", "dispatch"]:
            return {
                "injected": False,
                "dispatch": {
                    "id": "remote-dispatch",
                    "task_id": "remote-task",
                    "assignee_handle": "terminal-1",
                    "run_id": "run-1",
                },
            }
        if args[:2] == ["terminal", "send"]:
            return {}
        raise AssertionError(args)

    def _request(self, provider: str = "claude") -> TaskDispatch:
        if provider != self.state["role_specs"]["worker-a"]["provider"]:
            self.state = self._state(provider)
        task = self._task()
        return TaskDispatch(NodeRef("worker-a", Role.WORKER), task, "work")

    def _prepared_request(
        self, provider: str = "claude"
    ) -> tuple[dict[str, object], TaskDispatch, dict[str, object]]:
        request = self._request(provider)
        prepared = copy.deepcopy(self.state)
        record, _prepared_text = self._prepare_dispatch(prepared, request)
        return prepared, request, record

    def test_selected_dependency_failure_has_no_orca_or_state_effect(self) -> None:
        failure = RuntimeFailure(
            ErrorCode.INVALID_REQUEST, "selected binding is missing"
        )
        prepared, request, record = self._prepared_request()
        with (
            mock.patch.object(
                role_snapshot, "preflight_scoped_role", side_effect=failure
            ),
            self.assertRaises(RuntimeFailure),
        ):
            orca_dispatch.start_assignment(
                self.state_path,
                prepared,
                request,
                task_record=record,
                text="prepared work",
            )

        self.assertEqual(self.saved, [])
        self.assertEqual([kind for kind, _args in self.effects], [])

    def test_task_dispatch_without_prepared_record_fails_before_effects(self) -> None:
        with self.assertRaisesRegex(RuntimeFailure, "prepared TaskSpec record"):
            orca_dispatch.start_assignment(
                self.state_path,
                self.state,
                self._request(),
                task_record=None,
                text="prepared work",
            )

        self.assertEqual(self.saved, [])
        self.assertEqual([kind for kind, _args in self.effects], [])

    def test_read_only_planner_role_prompt_has_no_task_spec(self) -> None:
        planner_spec = dict(self.state["role_specs"]["worker-a"])
        planner_spec.update(
            {
                "kind": "planner",
                "permission": "read-only",
                "adapter_id": "claude-acp-0.70.0",
            }
        )
        self.state["role_specs"] = {"planner-a": planner_spec}
        planner = NodeRef("planner-a", Role.PLANNER)
        result = orca_dispatch.start_assignment(
            self.state_path,
            self.state,
            RolePrompt(planner, "inspect"),
            task_record=None,
            text="prepared read-only prompt",
        )

        assignment = self.state["roles"]["planner-a"]
        self.assertEqual(result.role, planner)
        self.assertNotIn("task_spec", assignment)
        self.assertNotIn("logical_task_id", assignment)
        task_create = next(
            args
            for kind, args in self.effects
            if kind == "orca" and args[:2] == ["orchestration", "task-create"]
        )
        self.assertEqual(
            task_create[task_create.index("--spec") + 1], "prepared read-only prompt"
        )

    def test_preflight_binding_drift_is_rejected_without_refreshing_snapshot(
        self,
    ) -> None:
        def drift(
            normalized: dict[str, object],
            _role: object,
            _workspace: Path,
            **_kwargs: object,
        ) -> None:
            normalized["instructions"] = "drifted instructions"

        prepared, request, record = self._prepared_request()
        with (
            mock.patch.object(
                role_snapshot, "preflight_scoped_role", side_effect=drift
            ),
            self.assertRaisesRegex(RuntimeFailure, "changed"),
        ):
            orca_dispatch.start_assignment(
                self.state_path,
                prepared,
                request,
                task_record=record,
                text="prepared work",
            )

        self.assertEqual(self.saved, [])
        self.assertEqual([kind for kind, _args in self.effects], [])

    def test_explicit_claude_and_codex_profiles_use_the_selected_path(self) -> None:
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                self.state = self._state(provider)
                self.saved.clear()
                self.effects.clear()
                self.preflight_calls.clear()
                self.adapter_calls.clear()
                prepared, request, record = self._prepared_request(provider)
                result = orca_dispatch.start_assignment(
                    self.state_path,
                    prepared,
                    request,
                    task_record=record,
                    text="prepared work",
                )
                self.assertEqual(result.role, NodeRef("worker-a", Role.WORKER))
                self.assertEqual(self.preflight_calls, [provider])
                self.assertEqual(self.adapter_calls, [provider])
                assignment = self.state["roles"]["worker-a"]
                self.assertEqual(
                    assignment["adapter_id"],
                    self.state["role_specs"]["worker-a"]["adapter_id"],
                )
                if provider == "claude":
                    self.assertEqual(
                        assignment["question_socket"],
                        str(Path(assignment["provider_private_root"]) / "q.sock"),
                    )
                    self.assertEqual(
                        assignment["agent_command"], "claude-agent --policy"
                    )
                else:
                    self.assertEqual(assignment["agent_command"], "codex-acp-agent")
                    self.assertIn("codex_launch_path", assignment)

    def test_assignment_is_saved_before_runner_send(self) -> None:
        prepared, request, record = self._prepared_request()
        result = orca_dispatch.start_assignment(
            self.state_path,
            prepared,
            request,
            task_record=record,
            text="prepared work",
        )

        send_index = next(
            index
            for index, (kind, args) in enumerate(self.effects)
            if kind == "orca" and args[:2] == ["terminal", "send"]
        )
        self.assertTrue(
            any(kind == "save" for kind, _args in self.effects[:send_index])
        )
        self.assertEqual(result.task_id._value, "remote-task")
        self.assertNotIn("pending_role_start", self.state)
        self.assertTrue(self.state["roles"]["worker-a"]["launcher_owned_terminal"])

    def test_task_spec_uses_canonical_logical_id_and_remote_dispatch_id(self) -> None:
        prepared, request, record = self._prepared_request()
        result = orca_dispatch.start_assignment(
            self.state_path,
            prepared,
            request,
            task_record=record,
            text="prepared task prompt",
        )
        task_create = next(
            args
            for kind, args in self.effects
            if kind == "orca" and args[:2] == ["orchestration", "task-create"]
        )
        spec = task_create[task_create.index("--spec") + 1]
        self.assertEqual(spec, task_json(request.task))
        self.assertEqual(json.loads(spec)["task_id"], "task-a")
        assignment = self.state["roles"]["worker-a"]
        self.assertEqual(assignment["task_id"], "remote-task")
        self.assertEqual(assignment["logical_task_id"], "task-a")
        self.assertEqual(
            self.state["tasks"]["task-a"]["dispatch_id"], "remote-dispatch"
        )
        self.assertEqual(result.dispatch_id._value, "remote-dispatch")

    def test_prepared_task_record_is_used_without_repreparing(self) -> None:
        prepared, request, record = self._prepared_request()
        result = orca_dispatch.start_assignment(
            self.state_path,
            prepared,
            request,
            task_record=record,
            text="prepared task prompt",
        )

        self.assertEqual(result.task_id._value, "remote-task")
        self.assertEqual(prepared["tasks"]["task-a"]["dispatch_id"], "remote-dispatch")

    def test_task_namespaces_allow_equal_text_without_aliasing_fields(self):
        prepared, request, record = self._prepared_request()

        def same_text(state, args, **kwargs):
            result = self._run_orca(state, args, **kwargs)
            if args[:2] == ["orchestration", "task-create"]:
                result["task"]["id"] = request.task.task_id
            elif args[:2] == ["orchestration", "dispatch"]:
                result["dispatch"]["task_id"] = request.task.task_id
            return result

        with mock.patch.object(orca_dispatch, "run_orca", side_effect=same_text):
            result = orca_dispatch.start_assignment(
                self.state_path,
                prepared,
                request,
                task_record=record,
                text="prepared task prompt",
            )
        self.assertEqual(result.task_id._value, request.task.task_id)
        assignment = self.state["roles"]["worker-a"]
        self.assertEqual(assignment["task_id"], request.task.task_id)
        self.assertEqual(assignment["logical_task_id"], request.task.task_id)
        self.assertEqual(
            self.state["tasks"][request.task.task_id]["dispatch_id"], "remote-dispatch"
        )

    def test_real_runtime_io_binds_arbitrary_reviewer_and_old_dispatch(self) -> None:
        # The other cases isolate boundaries with fakes; this case deliberately
        # restores the module aliases so runtime prompt/state I/O is real.
        self.stack.close()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            state_dir = root / "state"
            state_dir.mkdir(mode=0o700)
            state_path = state_dir / "state.json"
            task = self._task()
            graph = GraphSpec(
                nodes=(
                    NodeRef("main", Role.MAIN),
                    NodeRef("worker-a", Role.WORKER),
                    NodeRef("reviewer-b", Role.REVIEWER),
                ),
                edges=(
                    GraphEdge("main", "worker-a", "delegates-to"),
                    GraphEdge("worker-a", "reviewer-b", "reviewed-by"),
                ),
                coordination=Coordination("agent", ("main",), "serial", 1),
                routes=(TaskRoute(task.task_id, None, None, "worker-a", "reviewer-b"),),
            )

            def role_spec(kind: str) -> dict[str, object]:
                if kind == "main":
                    return {
                        "kind": "main",
                        "provider": "claude",
                        "transport": "direct",
                        "model": "main-model",
                        "effort": "high",
                        "permission": "orchestrator",
                        "instructions": "main",
                        "execution": "tui_direct",
                    }
                profile = native_profile("claude", kind)
                return {
                    "kind": kind,
                    **profile,
                    "model": "role-model",
                    "effort": "high",
                    "instructions": kind,
                    "acp_executables": {"saved": "claude"},
                    "scoped_wrapper_sha256": "w" * 64,
                    "scoped_client_sha256": "c" * 64,
                    "scoped_policy_sha256": "p" * 64,
                    "scoped_question_client_sha256": "q" * 64,
                }

            state: dict[str, object] = {
                "version": 4,
                "runtime": "orca",
                "team_id": "integration-team",
                "workspace": str(workspace),
                "config_path": str(root / "config.toml"),
                "state_path": str(state_path),
                "launcher_path": str(root / "agent-team"),
                "worktree_id": "repo::workspace",
                "orca_socket": str(root / "orca.sock"),
                "run_id": "integration-run",
                "main_terminal": "main-terminal",
                "graph": graph.as_dict(),
                "task_specs": [task.as_dict()],
                "max_review_rounds": 2,
                "tasks": {},
                "role_specs": {
                    "main": role_spec("main"),
                    "worker-a": role_spec("worker"),
                    "reviewer-b": role_spec("reviewer"),
                },
                "roles": {},
            }
            runtime_write_state(state_path, state)
            saved = runtime_read_state(state_path)
            old_dispatch = "old-worker-dispatch"
            record: dict[str, object] = {
                "spec": task.as_dict(),
                "digest": task_digest(task),
                "dispatch_id": old_dispatch,
                "status": "reviewing_implementation",
                "stage": "implementation",
                "revision": "r" * 64,
                "review_rounds": {"plan": 0, "implementation": 1},
                "role": "reviewer-b",
                "role_kind": "reviewer",
                "writer_role": "worker-a",
                "writer_kind": "worker",
                "review_source_dispatch_id": old_dispatch,
                "writer_result": {
                    "role": "worker-a",
                    "role_kind": "worker",
                    "dispatch_id": old_dispatch,
                    "body": "writer result",
                    "outcome": "succeeded",
                },
                "result": {
                    "role": "worker-a",
                    "role_kind": "worker",
                    "dispatch_id": old_dispatch,
                    "body": "writer result",
                    "outcome": "succeeded",
                },
            }
            prepared = copy.deepcopy(saved)
            prepared["tasks"] = {task.task_id: record}
            reviewer = NodeRef("reviewer-b", Role.REVIEWER)
            temp_counter = 0

            def fake_mkdtemp(*, prefix: str, dir: str | None = None) -> str:
                del dir
                nonlocal temp_counter
                path = root / f"{prefix}{temp_counter}"
                temp_counter += 1
                path.mkdir(mode=0o700)
                return str(path)

            def fake_policy(
                private_root: Path,
                _workspace: Path,
                _state_path: Path,
                _task: TaskSpec | None,
                _agent_entry: Path,
                *,
                permission: str,
            ) -> tuple[Path, str]:
                del permission
                return private_root / "write-policy.json", "p" * 64

            def fake_orca(
                _state: dict[str, object], args: list[str], **_kwargs: object
            ) -> dict[str, object]:
                if args[:2] == ["orchestration", "task-create"]:
                    return {"task": {"id": "remote-review-task"}}
                if args[:2] == ["terminal", "create"]:
                    return {"terminal": {"handle": "review-terminal"}}
                if args[:2] == ["orchestration", "dispatch"]:
                    return {
                        "injected": False,
                        "dispatch": {
                            "id": "new-review-dispatch",
                            "task_id": "remote-review-task",
                            "assignee_handle": "review-terminal",
                            "run_id": "integration-run",
                        },
                    }
                if args[:2] == ["terminal", "send"]:
                    return {}
                raise AssertionError(args)

            with (
                mock.patch.object(
                    orca_dispatch.tempfile, "mkdtemp", side_effect=fake_mkdtemp
                ),
                mock.patch.object(
                    role_snapshot, "preflight_scoped_role", return_value=None
                ),
                mock.patch.object(
                    native_acp_dependencies.NativeAcpExecutables,
                    "from_dict",
                    return_value=_FakeExecutables("claude"),
                ),
                mock.patch.object(
                    orca_dispatch, "checked_digest", side_effect=self._scoped_digest
                ),
                mock.patch.object(
                    native_acp_dependencies,
                    "adapter_snapshot",
                    return_value=self._adapter("claude"),
                ),
                mock.patch.object(
                    scoped_acp, "create_write_policy", side_effect=fake_policy
                ),
                mock.patch.object(
                    cli, "acp_agent_command", return_value="claude-agent --policy"
                ) as agent_command,
                mock.patch.object(
                    cli, "acp_session_name", return_value="review-session"
                ) as session_name,
                mock.patch.object(
                    cli, "acp_runner_command", return_value="runner"
                ) as runner_command,
                mock.patch.object(orca_dispatch, "run_orca", side_effect=fake_orca),
            ):
                result = orca_dispatch.start_assignment(
                    state_path,
                    prepared,
                    TaskDispatch(reviewer, task, "review"),
                    task_record=record,
                    text="prepared reviewer prompt",
                )

            final = runtime_read_state(state_path)
            final_record = final["tasks"][task.task_id]
            assignment = final["roles"][reviewer.node_id]
            self.assertEqual(result.role, reviewer)
            self.assertEqual(final_record["dispatch_id"], "new-review-dispatch")
            self.assertEqual(final_record["review_source_dispatch_id"], old_dispatch)
            self.assertEqual(assignment["dispatch_id"], "new-review-dispatch")
            self.assertTrue(Path(assignment["prompt_path"]).is_file())
            self.assertEqual(agent_command.call_args.args[1], reviewer)
            self.assertEqual(session_name.call_args.args[0], reviewer)
            self.assertEqual(runner_command.call_args.args[1], reviewer)

    def test_terminal_failure_retains_pending_ownership_without_rollback(self) -> None:
        def fail_terminal(
            _state: dict[str, object], args: list[str], **_kwargs: object
        ) -> dict[str, object]:
            self.effects.append(("orca", list(args)))
            if args[:2] == ["orchestration", "task-create"]:
                return {"task": {"id": "remote-task"}}
            if args[:2] == ["terminal", "create"]:
                raise RuntimeError("terminal create outcome is unknown")
            raise AssertionError(args)

        prepared, request, record = self._prepared_request()
        with (
            mock.patch.object(orca_dispatch, "run_orca", side_effect=fail_terminal),
            self.assertRaises(RuntimeFailure),
        ):
            orca_dispatch.start_assignment(
                self.state_path,
                prepared,
                request,
                task_record=record,
                text="prepared work",
            )

        self.assertTrue(self.saved)
        marker = self.saved[-1]["pending_role_start"]
        self.assertEqual(marker["role"], "worker-a")
        self.assertEqual(marker["role_kind"], "worker")
        self.assertEqual(marker["launch_nonce"], "n" * 32)
        self.assertEqual(marker["task_id"], "remote-task")
        self.assertEqual(marker["phase"], "terminal-create")
        self.assertFalse(marker["cleanup_confirmed"])
        self.assertEqual(self.saved[-1]["roles"], {})
        self.assertFalse(
            any(
                args[:2] == ["terminal", "send"]
                for kind, args in self.effects
                if kind == "orca"
            )
        )
        self.assertFalse(
            any(
                tuple(args[:2])
                in {("orchestration", "worker-stop"), ("terminal", "close")}
                for kind, args in self.effects
                if kind == "orca"
            )
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
