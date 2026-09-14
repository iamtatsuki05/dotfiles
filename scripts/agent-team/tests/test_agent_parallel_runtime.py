from __future__ import annotations

import copy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import test_named_runtime_state as named

from agent_team.contracts import NodeRef, Role
from agent_team.named_graph import Coordination, GraphSpec
from agent_team.native_controller import controller_keys
from agent_team.runtime import (
    RuntimeValidationError,
    resolve_state_role,
    validate_state_object,
)


class AgentParallelRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = named._valid_named_state(self.root)
        graph = GraphSpec.from_dict(self.state["graph"])
        graph = replace(
            graph, coordination=Coordination("agent", ("main",), "parallel", 2)
        )
        self.state.update(version=5, graph=graph.as_dict())

    def test_agent_parallel_uses_main_identity_and_exact_nodes(self):
        validate_state_object(self.root / "state.json", self.state)
        self.assertEqual(controller_keys(self.state).pid, "agent_pid")
        self.assertEqual(controller_keys(self.state).terminal, "main_terminal")
        self.assertEqual(
            resolve_state_role(self.state, "worker-b"), NodeRef("worker-b", Role.WORKER)
        )
        self.assertNotIn("program_wave", self.state)
        self.assertNotIn("agent_batch", self.state)

    def test_parallel_main_does_not_accept_coordinator_aliases_or_serial_version(self):
        for mutate in (
            lambda state: state.update(version=4),
            lambda state: state.update(coordinator_terminal=state.pop("main_terminal")),
            lambda state: state["native"].update(
                coordinator_argv=state["native"].pop("main_argv")
            ),
        ):
            state = copy.deepcopy(self.state)
            mutate(state)
            with (
                self.subTest(mutation=mutate),
                self.assertRaises(RuntimeValidationError),
            ):
                validate_state_object(self.root / "state.json", state)
