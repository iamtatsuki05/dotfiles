from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import cast

from agent_team import native_questions
from agent_team.contracts import NodeRef, Role
from agent_team.named_graph import Coordination, GraphEdge, GraphSpec
from agent_team.runtime import (
    RuntimeValidationError,
    _prompt_path,
    build_acp_runner_command,
    build_acp_session_name,
    build_background_runner_command,
    create_prompt_file,
    read_prompt_file,
    remove_prompt_file,
    resolve_state_role,
    validate_state_object,
)


def _graph() -> GraphSpec:
    main = NodeRef("main", Role.MAIN)
    worker_a = NodeRef("worker-a", Role.WORKER)
    worker_b = NodeRef("worker-b", Role.WORKER)
    reviewer_a = NodeRef("reviewer-a", Role.REVIEWER)
    return GraphSpec(
        nodes=(main, worker_a, worker_b, reviewer_a),
        edges=(
            GraphEdge("main", "worker-a", "delegates-to"),
            GraphEdge("main", "worker-b", "delegates-to"),
            GraphEdge("worker-a", "reviewer-a", "reviewed-by"),
        ),
        coordination=Coordination("agent", ("main",), "serial", 1),
        routes=(),
    )


def _spec(
    kind: str, *, permission: str, execution: str = "tui_direct"
) -> dict[str, object]:
    return {
        "kind": kind,
        "provider": "claude",
        "transport": "direct" if kind == "main" else "acp",
        "model": "fable",
        "effort": "high",
        "permission": permission,
        "instructions": kind,
        "execution": execution,
        **(
            {}
            if kind == "main"
            else {
                "adapter_id": (
                    "claude-acp-scoped-0.70.0"
                    if kind == "worker"
                    else "claude-acp-0.70.0"
                )
            }
        ),
    }


def _valid_named_state(root: Path) -> dict[str, object]:
    workspace = root / "workspace"
    workspace.mkdir()
    state_path = root / "state.json"
    graph = _graph()
    return {
        "version": 4,
        "runtime": "zellij",
        "team_id": "named-team",
        "workspace": str(workspace),
        "config_path": str(root / "config.toml"),
        "state_path": str(state_path),
        "launcher_path": str(root / "agent-team"),
        "run_id": "run-1",
        "main_terminal": "main-terminal",
        "native": {
            "phase": "running",
            "run_nonce": "nonce1234",
            "main_argv": ["/usr/bin/true"],
        },
        "task_specs": [],
        "graph": graph.as_dict(),
        "role_specs": {
            "main": _spec("main", permission="orchestrator"),
            "worker-a": _spec(
                "worker", permission="workspace-write", execution="background"
            ),
            "worker-b": _spec(
                "worker", permission="workspace-write", execution="background"
            ),
            "reviewer-a": _spec(
                "reviewer", permission="read-only", execution="background"
            ),
        },
        "roles": {},
    }


class NamedRuntimeStateTest(unittest.TestCase):
    def test_resolve_state_role_requires_exact_versioned_identity(self) -> None:
        v3 = {"version": 3, "role_specs": {"worker": {}, "reviewer": {}}}
        self.assertEqual(resolve_state_role(v3, "worker"), Role.WORKER)
        with self.assertRaises(RuntimeValidationError):
            resolve_state_role(v3, "worker-a")
        with self.assertRaises(RuntimeValidationError):
            resolve_state_role({**v3, "graph": _graph().as_dict()}, "worker")
        with self.assertRaises(RuntimeValidationError):
            resolve_state_role(
                {"version": 4, "role_specs": {"worker-a": {}}}, "worker-a"
            )
        with self.assertRaises(RuntimeValidationError):
            resolve_state_role({"version": 99}, "worker")

    def test_resolve_v4_returns_the_graph_node_ref(self) -> None:
        graph = _graph()
        state = {
            "version": 4,
            "runtime": "zellij",
            "task_specs": [],
            "graph": graph.as_dict(),
            "role_specs": {
                "main": _spec("main", permission="orchestrator"),
                "worker-a": _spec(
                    "worker", permission="workspace-write", execution="background"
                ),
                "worker-b": _spec(
                    "worker", permission="workspace-write", execution="background"
                ),
                "reviewer-a": _spec(
                    "reviewer", permission="read-only", execution="background"
                ),
            },
        }
        self.assertEqual(
            resolve_state_role(state, "worker-a"),
            NodeRef("worker-a", Role.WORKER),
        )

    def test_v4_state_rejects_orca_and_mixed_or_invalid_node_identity(self) -> None:
        graph = _graph()
        state: dict[str, object] = {
            "version": 4,
            "runtime": "zellij",
            "team_id": "named-team",
            "workspace": "/tmp/workspace",
            "config_path": "/tmp/config.toml",
            "state_path": "/tmp/state.json",
            "launcher_path": "/tmp/agent-team",
            "run_id": "run-1",
            "main_terminal": "main-terminal",
            "graph": graph.as_dict(),
            "role_specs": {
                "main": _spec("main", permission="orchestrator"),
                "worker-a": _spec(
                    "worker", permission="workspace-write", execution="background"
                ),
                "worker-b": _spec(
                    "worker", permission="workspace-write", execution="background"
                ),
                "reviewer-a": _spec(
                    "reviewer", permission="read-only", execution="background"
                ),
            },
            "roles": {},
        }
        with self.assertRaises(RuntimeValidationError):
            validate_state_object(Path("/tmp/state.json"), {**state, "runtime": "orca"})
        with self.assertRaises(RuntimeValidationError):
            validate_state_object(
                Path("/tmp/state.json"),
                {
                    **state,
                    "role_specs": {
                        **cast(dict[str, object], state["role_specs"]),
                        "worker-a": _spec("reviewer", permission="read-only"),
                    },
                },
            )
        with self.assertRaises(RuntimeValidationError):
            validate_state_object(
                Path("/tmp/state.json"),
                {**state, "graph": {**graph.as_dict(), "unexpected": True}},
            )

    def test_v4_native_result_is_checked_at_read_and_write_boundaries(self) -> None:
        from agent_team.runtime import read_state, write_state

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "state.json"
            state = _valid_named_state(root)
            state["native_result"] = {
                "role": "worker-a",
                "role_kind": "worker",
                "run_id": "run-1",
                "task_id": "task-1",
                "dispatch_id": "dispatch-1",
                "terminal_handle": "terminal-1",
                "launch_nonce": "nonce1234",
                "outcome": "succeeded",
                "body": "done",
                "cleanup_confirmed": True,
                "delivery_id": "delivery-1",
            }
            write_state(state_path, state)
            original = state_path.read_bytes()
            with self.assertRaises(RuntimeValidationError):
                write_state(
                    state_path,
                    {
                        **state,
                        "pending_delivery_id": "pending-1",
                        "pending_delivery_kind": "worker_done",
                        "pending_delivery_stage": "observed",
                    },
                    require_existing=True,
                )
            with self.assertRaises(RuntimeValidationError):
                write_state(
                    state_path,
                    {**state, "pending_delivery_kind": "worker_done"},
                    require_existing=True,
                )

            result = cast(dict[str, object], state["native_result"])
            mutations: tuple[object, ...] = (
                None,
                [],
                {**result, "outcome": []},
                {**result, "delivery_id": ""},
                {key: value for key, value in result.items() if key != "outcome"},
                {**result, "cleanup_confirmed": "yes"},
                {**result, "role_kind": "reviewer"},
                {**result, "run_id": "other-run"},
            )
            for mutation in mutations:
                state_path.write_bytes(original)
                with self.subTest(mutation=mutation):
                    with self.assertRaises(RuntimeValidationError):
                        write_state(
                            state_path,
                            {**state, "native_result": mutation},
                            require_existing=True,
                        )
                    self.assertEqual(state_path.read_bytes(), original)
                    try:
                        state_path.write_text(
                            json.dumps({**state, "native_result": mutation})
                        )
                        with self.assertRaises(RuntimeValidationError):
                            read_state(state_path)
                    finally:
                        state_path.write_bytes(original)

            for field in ("pending_delivery_kind", "pending_delivery_stage"):
                changed = {
                    **state,
                    "pending_delivery_id": "delivery-1",
                    "pending_delivery_kind": "worker_done",
                    "pending_delivery_stage": "observed",
                    field: [],
                }
                with self.subTest(field=field):
                    with self.assertRaises(RuntimeValidationError):
                        write_state(state_path, changed, require_existing=True)
                    self.assertEqual(state_path.read_bytes(), original)

            encoded = json.loads(original.decode("utf-8"))
            encoded["native_result"] = {
                key: value for key, value in result.items() if key != "delivery_id"
            }
            state_path.write_text(
                json.dumps(encoded, ensure_ascii=False), encoding="utf-8"
            )
            with self.assertRaises(RuntimeValidationError):
                read_state(state_path)

    def test_v4_background_assignment_keeps_adapter_and_snapshot_gates(self) -> None:
        graph = _graph()
        state: dict[str, object] = {
            "version": 4,
            "runtime": "zellij",
            "team_id": "named-team",
            "workspace": "/tmp/workspace",
            "config_path": "/tmp/config.toml",
            "state_path": "/tmp/state.json",
            "launcher_path": "/tmp/agent-team",
            "run_id": "run-1",
            "main_terminal": "main-terminal",
            "task_specs": [],
            "native": {
                "phase": "running",
                "run_nonce": "nonce1234",
                "main_argv": ["/usr/bin/true"],
            },
            "graph": graph.as_dict(),
            "role_specs": {
                "main": _spec("main", permission="orchestrator"),
                "worker-a": _spec(
                    "worker", permission="workspace-write", execution="background"
                ),
                "worker-b": _spec(
                    "worker", permission="workspace-write", execution="background"
                ),
                "reviewer-a": _spec(
                    "reviewer", permission="read-only", execution="background"
                ),
            },
            "roles": {
                "worker-a": {
                    "role": "worker-a",
                    "role_kind": "worker",
                    "task_id": "task-1",
                    "dispatch_id": "dispatch-1",
                    "terminal_handle": "terminal-1",
                    "completion_observed": False,
                    "launcher_owned_runner": True,
                    "execution": "background",
                    "adapter_id": "claude-acp-scoped-0.70.0",
                    "launch_nonce": "nonce1234",
                    "prompt_path": "/tmp/prompt.md",
                    "provider_private_root": "/tmp/private",
                    "snapshot_root": "/tmp/snapshot",
                    "adapter_snapshot": {
                        "adapter_id": "claude-acp-scoped-0.70.0",
                        "revision": "sdk-1",
                        "executable": "/bin/claude",
                        "version": "claude 1",
                        "identity": {
                            "device": 1,
                            "inode": 2,
                            "size": 3,
                            "mtime_ns": 4,
                            "sha256": "a" * 64,
                        },
                    },
                }
            },
        }
        validate_state_object(Path("/tmp/state.json"), state)
        assignment = cast(
            dict[str, object], cast(dict[str, object], state["roles"])["worker-a"]
        )
        state["native_result"] = {
            "role": "worker-a",
            "role_kind": "worker",
            "run_id": "run-1",
            "task_id": assignment["task_id"],
            "dispatch_id": assignment["dispatch_id"],
            "terminal_handle": assignment["terminal_handle"],
            "launch_nonce": assignment["launch_nonce"],
            "outcome": "succeeded",
            "body": "done",
            "cleanup_confirmed": True,
            "delivery_id": "delivery-1",
        }
        validate_state_object(Path("/tmp/state.json"), state)
        with self.assertRaises(RuntimeValidationError):
            validate_state_object(
                Path("/tmp/state.json"),
                {
                    **state,
                    "roles": {"worker-b": {**assignment, "role": "worker-b"}},
                },
            )
        validate_state_object(Path("/tmp/state.json"), {**state, "roles": {}})
        with self.assertRaises(RuntimeValidationError):
            validate_state_object(
                Path("/tmp/state.json"),
                {
                    **state,
                    "native_result": {
                        **cast(dict[str, object], state["native_result"]),
                        "dispatch_id": "other-dispatch",
                    },
                },
            )
        damaged = {
            **state,
            "roles": {"worker-a": {**assignment, "adapter_id": "wrong"}},
        }
        with self.assertRaises(RuntimeValidationError):
            validate_state_object(Path("/tmp/state.json"), damaged)
        damaged_snapshot = {
            **state,
            "roles": {
                "worker-a": {
                    **assignment,
                    "adapter_snapshot": {
                        **cast(dict[str, object], assignment["adapter_snapshot"]),
                        "revision": "",
                    },
                }
            },
        }
        with self.assertRaises(RuntimeValidationError):
            validate_state_object(Path("/tmp/state.json"), damaged_snapshot)

    def test_named_prompt_files_are_separate_and_cross_node_access_is_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker_a = NodeRef("worker-a", Role.WORKER)
            worker_b = NodeRef("worker-b", Role.WORKER)
            prompt_a = create_prompt_file(root, worker_a, "nonce1234", "A")
            prompt_b = create_prompt_file(root, worker_b, "nonce1234", "B")

            self.assertEqual(
                prompt_a, _prompt_path(root.resolve(), worker_a, "nonce1234")
            )
            self.assertEqual(
                prompt_b, _prompt_path(root.resolve(), worker_b, "nonce1234")
            )
            self.assertNotEqual(prompt_a, prompt_b)
            self.assertEqual(
                build_acp_session_name(worker_a, "nonce1234"),
                "agent-team-worker-a-nonce1234",
            )
            runner_state: dict[str, object] = {
                "launcher_path": "/tmp/agent-team",
                "state_path": "/tmp/team/state.json",
            }
            runner = build_acp_runner_command(
                runner_state,
                worker_a,
                task_id="task-1",
                dispatch_id="dispatch-1",
                terminal_handle="terminal-1",
                prompt_path=prompt_a,
                launch_nonce="nonce1234",
            )
            background = build_background_runner_command(
                runner_state,
                worker_b,
                task_id="task-2",
                dispatch_id="dispatch-2",
                terminal_handle="terminal-2",
                prompt_path=prompt_b,
                launch_nonce="nonce1234",
            )
            self.assertIn("_acp-run worker-a", runner)
            self.assertIn("_background-run worker-b", background)
            self.assertEqual(
                read_prompt_file(
                    prompt_a, root, role=worker_a, launch_nonce="nonce1234"
                ),
                "A",
            )
            with self.assertRaises(RuntimeValidationError):
                read_prompt_file(
                    prompt_a, root, role=worker_b, launch_nonce="nonce1234"
                )
            with self.assertRaises(RuntimeValidationError):
                remove_prompt_file(
                    prompt_a, root, role=worker_b, launch_nonce="nonce1234"
                )
            self.assertTrue(prompt_a.exists())
            remove_prompt_file(prompt_a, root, role=worker_a, launch_nonce="nonce1234")
            remove_prompt_file(prompt_b, root, role=worker_b, launch_nonce="nonce1234")

    def test_named_question_receipts_bind_id_and_kind_but_v3_stays_legacy(self) -> None:
        identity = {
            "role": "worker-a",
            "role_kind": "worker",
            "run_id": "run-1",
            "task_id": "task-1",
            "dispatch_id": "dispatch-1",
            "terminal_handle": "terminal-1",
            "launch_nonce": "nonce1234",
        }
        question = {
            **identity,
            "request": {
                "kind": "question",
                "session_id": "session-1",
                "tool_call_id": "tool-1",
                "questions": [{"field": "question_0_custom", "body": "A or B?"}],
            },
            "message_ids": ["message-1"],
            "answers": {"message-1": "A"},
            "delivery_id": "delivery-1",
        }
        item = native_questions.receipt(question, expected_identity=identity)
        native_questions.validate_receipts([item], identity)
        with self.assertRaises(ValueError):
            native_questions.validate_receipts([item], {**identity, "role": "worker-b"})
        with self.assertRaises(ValueError):
            native_questions.validate_receipts(
                [item], {**identity, "role_kind": "reviewer"}
            )
        legacy = {key: value for key, value in question.items() if key != "role_kind"}
        legacy_item = native_questions.receipt(legacy, expected_identity=legacy)
        native_questions.validate_receipts(
            [legacy_item],
            {key: value for key, value in identity.items() if key != "role_kind"},
        )
        with self.assertRaises(ValueError):
            native_questions.validate_receipts(
                [item],
                {key: value for key, value in identity.items() if key != "role_kind"},
            )

    def test_v3_question_state_rejects_named_identity_fields(self) -> None:
        with self.assertRaises(ValueError):
            native_questions.validate_state(
                {
                    "version": 3,
                    "role_specs": {"worker": {"kind": "worker"}},
                    "roles": {},
                }
            )


if __name__ == "__main__":
    unittest.main()
