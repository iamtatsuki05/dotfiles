from __future__ import annotations

import copy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest import mock

from test_named_orca_state import _graph, _role_spec, _task
from test_orca_backend import FakeOrcaClient, start_spec

from agent_team import backend as backend_module
from agent_team.backend import OrcaBackend, OrcaClient
from agent_team.contracts import (
    Attach,
    ErrorCode,
    NodeRef,
    Role,
    RoleSpec,
    RuntimeFailure,
    Status,
)
from agent_team.locking import _LifecycleReservation
from agent_team.named_graph import GraphEdge, TaskRoute
from agent_team.runtime import read_state, write_state


class NamedOrcaBackendTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        initial = start_spec(self.root)
        initial.workspace.mkdir()
        self.lead = NodeRef("lead", Role.MAIN)
        graph = replace(
            _graph(),
            nodes=(
                self.lead,
                NodeRef("worker-a", Role.WORKER),
                NodeRef("reviewer-a", Role.REVIEWER),
            ),
            edges=(
                GraphEdge("lead", "worker-a", "delegates-to"),
                GraphEdge("worker-a", "reviewer-a", "reviewed-by"),
            ),
            coordination=replace(_graph().coordination, entry_nodes=("lead",)),
            routes=(TaskRoute("task-1", None, None, "worker-a", "reviewer-a"),),
        )
        specs = {}
        for node in graph.nodes:
            raw = _role_spec(node.kind.value)
            raw.pop("kind")
            if node.kind is not Role.MAIN:
                raw["acp_executables"] = {"node": "selected-node"}
            specs[node] = RoleSpec(**raw)
        self.spec = replace(
            initial,
            graph=graph,
            role_specs=specs,
            task_specs=(_task(),),
            max_review_rounds=2,
        )
        self.client = FakeOrcaClient()
        self.client.run_create_result = "run_1"
        original_worker_show = self.client.worker_show

        def context_worker_show(**kwargs):
            response = original_worker_show(**kwargs)
            response["worker"].update(
                {"state": "unsupervised", "stage": "context_only", "worktree_id": None}
            )
            response["terminalResource"] = None
            response["observation"] = {"status": "live", "exactWorker": True}
            return response

        self.client.worker_show = context_worker_show
        self.backend = OrcaBackend(
            cast(OrcaClient, self.client),
            main_command_factory=lambda _: "selected-main",
        )
        ready = mock.patch.object(
            OrcaBackend,
            "_ensure_orca_ready",
            return_value=("repo::project", self.root / "orca.sock"),
        )
        ready.start()
        self.addCleanup(ready.stop)

    @staticmethod
    def scoped_preflight(normalized, _role, _workspace, **_kwargs):
        for key in (
            "scoped_wrapper_sha256",
            "scoped_client_sha256",
            "scoped_policy_sha256",
            "scoped_question_client_sha256",
        ):
            normalized[key] = "a" * 64

    def start(self) -> dict[str, object]:
        with mock.patch.object(
            backend_module, "preflight_scoped_role", side_effect=self.scoped_preflight
        ) as preflight:
            self.backend.start(self.spec)
        self.assertEqual(preflight.call_count, 2)
        return read_state(self.spec.state_path)

    def test_start_saves_graph_catalog_and_scoped_bindings_then_attaches_named_main(
        self,
    ):
        state = self.start()
        self.assertEqual(state["version"], 4)
        self.assertEqual(state["runtime"], "orca")
        self.assertEqual(state["graph"], self.spec.graph.as_dict())
        self.assertEqual(state["task_specs"], [_task().as_dict()])
        self.assertEqual(state["tasks"], {})
        self.assertEqual(
            state["role_specs"]["worker-a"]["scoped_question_client_sha256"], "a" * 64
        )
        self.assertNotIn("native", state)
        receipt = self.backend.request(Attach(self.lead))
        self.assertEqual(receipt.role, self.lead)
        with self.assertRaises(RuntimeFailure) as raised:
            self.backend.request(Attach(NodeRef("lead", Role.WORKER)))
        self.assertIs(raised.exception.code, ErrorCode.IDENTITY_MISMATCH)

    def test_missing_selected_binding_fails_before_orca_or_state_effect(self):
        with (
            mock.patch.object(
                backend_module,
                "preflight_scoped_role",
                side_effect=RuntimeFailure(
                    ErrorCode.INVALID_REQUEST, "selected SDK unavailable"
                ),
            ),
            self.assertRaisesRegex(RuntimeFailure, "selected SDK unavailable"),
        ):
            self.backend.start(self.spec)
        self.assertEqual(self.client.calls, [])
        self.assertFalse(self.spec.state_path.parent.exists())

    def test_attach_rejects_pending_main_before_probe_and_before_focus(self):
        self.start()
        original_run_show = self.client.run_show

        def publish_pending():
            state = read_state(self.spec.state_path)
            state["pending_role_start"] = {
                "role": "main",
                "phase": "main_sent",
                "run_id": state["run_id"],
                "main_terminal": state["main_terminal"],
                "command_sha256": "a" * 64,
            }
            write_state(self.spec.state_path, state, require_existing=True)

        def run_show(**kwargs):
            publish_pending()
            return original_run_show(**kwargs)

        self.client.run_show = run_show
        for already_pending in (False, True):
            with self.subTest(already_pending=already_pending):
                before = list(self.client.calls)
                with self.assertRaises(RuntimeFailure) as raised:
                    self.backend.request(Attach(self.lead))
                self.assertIs(raised.exception.code, ErrorCode.BUSY)
                effects = self.client.calls[len(before) :]
                self.assertFalse(any(call[0] == "terminal-switch" for call in effects))
                if already_pending:
                    self.assertEqual(effects, [])

    def test_mutated_snapshot_is_rejected_before_external_status_effect(self):
        state = self.start()
        damaged = copy.deepcopy(state)
        damaged["role_specs"]["worker-a"]["model"] = "changed-model"
        write_state(self.spec.state_path, damaged, require_existing=True)
        before = list(self.client.calls)
        with self.assertRaises(RuntimeFailure) as raised:
            self.backend.request(Attach(self.lead))
        self.assertIs(raised.exception.code, ErrorCode.IDENTITY_MISMATCH)
        self.assertEqual(self.client.calls, before)

    def test_status_and_attach_allow_publication_during_remote_checks(self):
        self.start()
        original = self.client.run_show

        def run_show(**kwargs):
            reservation = _LifecycleReservation(self.spec.state_path)
            reservation.acquire()
            reservation.release()
            state = read_state(self.spec.state_path)
            write_state(self.spec.state_path, state, require_existing=True)
            return original(**kwargs)

        self.client.run_show = run_show
        self.assertEqual(self.backend.request(Status()).status, "running")
        self.assertEqual(self.backend.request(Attach(self.lead)).role, self.lead)

    def test_status_retains_stopping_identity_without_remote_probe(self):
        state = self.start()
        state["orca_stop_requested"] = True
        write_state(self.spec.state_path, state, require_existing=True)
        before = list(self.client.calls)
        self.assertEqual(self.backend.request(Status()).status, "stopping")
        self.assertEqual(self.client.calls, before)
        with self.assertRaises(RuntimeFailure):
            self.backend.request(Attach(self.lead))
        self.assertEqual(self.client.calls, before)

    def test_attach_focus_effect_is_fenced_against_stop(self):
        self.start()
        original = self.client.terminal_switch

        def terminal_switch(**kwargs):
            reservation = _LifecycleReservation(self.spec.state_path)
            with self.assertRaises(RuntimeFailure) as raised:
                reservation.acquire()
            self.assertIs(raised.exception.code, ErrorCode.TEAM_ALREADY_RUNNING)
            return original(**kwargs)

        self.client.terminal_switch = terminal_switch
        self.assertEqual(self.backend.request(Attach(self.lead)).role, self.lead)

    def test_parallel_graph_is_rejected_before_dependency_probe(self):
        graph = replace(
            self.spec.graph,
            coordination=replace(
                self.spec.graph.coordination, dispatch_mode="parallel", max_active=2
            ),
        )
        with (
            mock.patch.object(backend_module, "preflight_scoped_role") as preflight,
            self.assertRaises(RuntimeFailure) as raised,
        ):
            self.backend.start(replace(self.spec, graph=graph))
        self.assertIs(raised.exception.code, ErrorCode.INVALID_REQUEST)
        preflight.assert_not_called()
        self.assertEqual(self.client.calls, [])
        self.assertFalse(self.spec.state_path.parent.exists())
