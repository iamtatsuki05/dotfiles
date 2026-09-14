from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from typing import cast

from agent_team.contracts import NodeRef, Role
from agent_team.named_graph import (
    Coordination,
    GraphEdge,
    GraphSpec,
    NamedGraphError,
    TaskRoute,
    validate_graph,
)
from agent_team.task_spec import TaskSpec, VerificationSpec


def _task(task_id: str, *, dependencies: tuple[str, ...] = ()) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        objective=f"Objective for {task_id}.",
        acceptance_criteria=("The task is completed.",),
        allowed_paths=("scripts/agent-team/",),
        forbidden_paths=("upstreams.json",),
        dependencies=dependencies,
        verification=(
            VerificationSpec(
                name="unit",
                argv=("python", "-m", "unittest"),
                timeout_seconds=30,
            ),
        ),
        evidence_requirements=("Record the result.",),
        consultation_conditions=(),
    )


def _node(node_id: str, kind: Role) -> NodeRef:
    return NodeRef(node_id=node_id, kind=kind)


def _agent_graph() -> GraphSpec:
    nodes = (
        _node("main", Role.MAIN),
        _node("plan-a", Role.PLANNER),
        _node("work-a", Role.WORKER),
        _node("work-b", Role.WORKER),
        _node("review-plan", Role.REVIEWER),
        _node("review-a", Role.REVIEWER),
        _node("review-b", Role.REVIEWER),
    )
    edges = (
        GraphEdge("main", "plan-a", "delegates-to"),
        GraphEdge("plan-a", "work-a", "delegates-to"),
        GraphEdge("main", "work-b", "delegates-to"),
        GraphEdge("plan-a", "review-plan", "reviewed-by"),
        GraphEdge("work-a", "review-a", "reviewed-by"),
        GraphEdge("work-b", "review-b", "reviewed-by"),
    )
    routes = (
        TaskRoute(
            task_id="plan-task",
            plan_writer="plan-a",
            plan_reviewer="review-plan",
            implementation_writer="work-a",
            implementation_reviewer="review-a",
        ),
        TaskRoute(
            task_id="second-task",
            plan_writer=None,
            plan_reviewer=None,
            implementation_writer="work-b",
            implementation_reviewer="review-b",
        ),
    )
    return GraphSpec(
        nodes=nodes,
        edges=edges,
        coordination=Coordination(
            mode="agent",
            entry_nodes=("main",),
            dispatch_mode="serial",
            max_active=1,
        ),
        routes=routes,
    )


def _program_graph() -> GraphSpec:
    return GraphSpec(
        nodes=(
            _node("plan-a", Role.PLANNER),
            _node("review-a", Role.REVIEWER),
        ),
        edges=(GraphEdge("plan-a", "review-a", "reviewed-by"),),
        coordination=Coordination(
            mode="program",
            entry_nodes=("plan-a",),
            dispatch_mode="parallel",
            max_active=2,
        ),
        routes=(
            TaskRoute(
                task_id="plan-task",
                plan_writer="plan-a",
                plan_reviewer="review-a",
                implementation_writer=None,
                implementation_reviewer=None,
            ),
        ),
    )


class NamedGraphValuesTest(unittest.TestCase):
    def test_valid_agent_graph_allows_multiple_workers_and_exact_queries(self) -> None:
        graph = _agent_graph()

        validate_graph(graph, (_task("plan-task"), _task("second-task")))

        self.assertEqual(graph.node("work-a"), _node("work-a", Role.WORKER))
        self.assertEqual(graph.route("second-task").implementation_writer, "work-b")
        self.assertEqual(graph.main_node, _node("main", Role.MAIN))
        with self.assertRaises(KeyError):
            graph.node("WORK-A")
        with self.assertRaises(KeyError):
            graph.route("unknown-task")

    def test_program_graph_is_mainless_and_does_not_fabricate_main(self) -> None:
        graph = _program_graph()

        validate_graph(graph, (_task("plan-task"),))

        self.assertIsNone(graph.main_node)
        self.assertEqual(graph.coordination.entry_nodes, ("plan-a",))

    def test_round_trip_is_exact_and_values_are_immutable(self) -> None:
        graph = _agent_graph()

        encoded = graph.as_dict()
        decoded = GraphSpec.from_dict(encoded)

        self.assertEqual(decoded, graph)
        self.assertEqual(set(encoded), {"nodes", "edges", "coordination", "routes"})
        self.assertEqual(encoded["nodes"][0], {"node_id": "main", "kind": "main"})  # type: ignore[index]
        with self.assertRaises(FrozenInstanceError):
            graph.coordination.max_active = 3  # type: ignore[misc]

    def test_unknown_serialized_keys_and_version_are_rejected(self) -> None:
        encoded = _agent_graph().as_dict()

        for invalid in (
            {**encoded, "version": 5},
            {**encoded, "unexpected": True},
            {key: value for key, value in encoded.items() if key != "routes"},
        ):
            with self.subTest(keys=tuple(invalid)), self.assertRaises(ValueError):
                GraphSpec.from_dict(invalid)

        coordination = cast(dict[str, object], encoded["coordination"])
        with self.assertRaises(ValueError):
            GraphSpec.from_dict(
                {
                    **encoded,
                    "coordination": {**coordination, "unexpected": True},
                }
            )

    def test_graph_rejects_duplicate_or_bad_references(self) -> None:
        graph = _agent_graph()

        cases = (
            GraphSpec(
                nodes=graph.nodes + (_node("work-a", Role.WORKER),),
                edges=graph.edges,
                coordination=graph.coordination,
                routes=graph.routes,
            ),
            GraphSpec(
                nodes=graph.nodes,
                edges=graph.edges + (GraphEdge("main", "missing", "delegates-to"),),
                coordination=graph.coordination,
                routes=graph.routes,
            ),
            GraphSpec(
                nodes=graph.nodes,
                edges=graph.edges,
                coordination=Coordination("agent", ("missing",), "serial", 1),
                routes=graph.routes,
            ),
        )
        for invalid in cases:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_graph(invalid, (_task("plan-task"), _task("second-task")))

    def test_graph_rejects_automatic_cycle_and_unreachable_nodes(self) -> None:
        graph = _agent_graph()

        cycle = GraphSpec(
            nodes=graph.nodes,
            edges=graph.edges + (GraphEdge("review-a", "plan-a", "delegates-to"),),
            coordination=graph.coordination,
            routes=graph.routes,
        )
        unreachable = GraphSpec(
            nodes=graph.nodes + (_node("orphan", Role.WORKER),),
            edges=graph.edges,
            coordination=graph.coordination,
            routes=graph.routes,
        )
        for invalid in (cycle, unreachable):
            with self.subTest(invalid=invalid), self.assertRaises(NamedGraphError):
                validate_graph(invalid, (_task("plan-task"), _task("second-task")))

    def test_consultation_is_one_hop_and_is_not_a_user_gate(self) -> None:
        valid = GraphSpec(
            nodes=(
                _node("main", Role.MAIN),
                _node("work", Role.WORKER),
                _node("review", Role.REVIEWER),
                _node("consult", Role.REVIEWER),
            ),
            edges=(
                GraphEdge("main", "work", "delegates-to"),
                GraphEdge("work", "review", "reviewed-by"),
                GraphEdge("work", "consult", "consults-to"),
            ),
            coordination=Coordination("agent", ("main",), "serial", 1),
            routes=(TaskRoute("work-task", None, None, "work", "review"),),
        )
        validate_graph(valid, (_task("work-task"),))

        self.assertEqual(valid.main_node, _node("main", Role.MAIN))
        invalid = GraphSpec(
            nodes=valid.nodes + (_node("after-consult", Role.WORKER),),
            edges=valid.edges
            + (GraphEdge("consult", "after-consult", "delegates-to"),),
            coordination=valid.coordination,
            routes=valid.routes,
        )
        with self.assertRaises(NamedGraphError):
            validate_graph(invalid, (_task("work-task"),))

    def test_consultation_and_automatic_edges_are_order_independent(self) -> None:
        nodes = (
            _node("main", Role.MAIN),
            _node("work", Role.WORKER),
            _node("review", Role.REVIEWER),
        )
        automatic = GraphEdge("main", "work", "delegates-to")
        consultation = GraphEdge("main", "work", "consults-to")
        review = GraphEdge("work", "review", "reviewed-by")
        coordination = Coordination("agent", ("main",), "serial", 1)
        route = (TaskRoute("work-task", None, None, "work", "review"),)

        for edges in (
            (consultation, automatic, review),
            (automatic, consultation, review),
        ):
            graph = GraphSpec(
                nodes=nodes,
                edges=edges,
                coordination=coordination,
                routes=route,
            )
            with self.subTest(edges=edges):
                validate_graph(graph, (_task("work-task"),))

    def test_edge_kind_and_main_target_rules_are_strict(self) -> None:
        graph = _agent_graph()

        bad_edges = (
            GraphEdge("main", "review-plan", "reviewed-by"),
            GraphEdge("review-plan", "work-a", "reviewed-by"),
            GraphEdge("plan-a", "main", "delegates-to"),
            GraphEdge("work-a", "work-a", "consults-to"),
        )
        for bad_edge in bad_edges:
            invalid = GraphSpec(
                nodes=graph.nodes,
                edges=graph.edges + (bad_edge,),
                coordination=graph.coordination,
                routes=graph.routes,
            )
            with self.subTest(edge=bad_edge), self.assertRaises(ValueError):
                validate_graph(invalid, (_task("plan-task"), _task("second-task")))

    def test_route_requires_exact_catalog_and_stage_pairs(self) -> None:
        graph = _agent_graph()
        cases = (
            (
                TaskRoute("missing", None, None, "work-a", "review-a"),
                (_task("plan-task"),),
            ),
            (
                TaskRoute("plan-task", "plan-a", None, None, None),
                (_task("plan-task"),),
            ),
            (
                TaskRoute("plan-task", None, None, None, None),
                (_task("plan-task"),),
            ),
            (
                TaskRoute("plan-task", "work-a", "review-a", None, None),
                (_task("plan-task"),),
            ),
            (
                TaskRoute("plan-task", "plan-a", "review-plan", "work-a", None),
                (_task("plan-task"),),
            ),
        )
        for route, catalog in cases:
            invalid = GraphSpec(
                nodes=graph.nodes,
                edges=graph.edges,
                coordination=graph.coordination,
                routes=(route,),
            )
            with self.subTest(route=route), self.assertRaises(ValueError):
                validate_graph(invalid, catalog)

    def test_route_requires_review_edges_and_plan_to_implementation_edge(self) -> None:
        graph = _agent_graph()
        missing_review = GraphSpec(
            nodes=graph.nodes,
            edges=tuple(edge for edge in graph.edges if edge.target != "review-a"),
            coordination=graph.coordination,
            routes=graph.routes,
        )
        missing_plan_to_implementation = GraphSpec(
            nodes=graph.nodes,
            edges=tuple(
                edge
                for edge in graph.edges
                if not (edge.source == "plan-a" and edge.target == "work-a")
            ),
            coordination=graph.coordination,
            routes=graph.routes,
        )
        for invalid in (missing_review, missing_plan_to_implementation):
            with self.subTest(invalid=invalid), self.assertRaises(NamedGraphError):
                validate_graph(invalid, (_task("plan-task"), _task("second-task")))

    def test_task_catalog_rejects_unknown_dependency_and_cycle(self) -> None:
        graph = _program_graph()
        unknown = _task("plan-task", dependencies=("missing",))
        first = _task("plan-task", dependencies=("second-task",))
        second = _task("second-task", dependencies=("plan-task",))

        with self.assertRaises(ValueError):
            validate_graph(graph, (unknown,))
        with self.assertRaises(ValueError):
            validate_graph(graph, (first, second))

    def test_coordination_mode_and_dispatch_bounds_are_strict(self) -> None:
        graph = _program_graph()
        invalids = (
            ("agent", ("plan-a",), "parallel", 1),
            ("program", (), "serial", 1),
            ("program", ("plan-a",), "serial", 2),
        )
        for values in invalids:
            coordination = Coordination(*values)
            invalid = GraphSpec(
                nodes=graph.nodes,
                edges=graph.edges,
                coordination=coordination,
                routes=graph.routes,
            )
            with self.subTest(coordination=coordination), self.assertRaises(ValueError):
                validate_graph(invalid, (_task("plan-task"),))

        with self.assertRaises(ValueError):
            Coordination("program", ("plan-a",), "parallel", 0)

    def test_node_and_edge_limits_are_bounded(self) -> None:
        nodes = tuple(_node(f"node-{index}", Role.WORKER) for index in range(129))
        oversized_nodes = GraphSpec(
            nodes=nodes,
            edges=(),
            coordination=Coordination("program", ("node-0",), "serial", 1),
            routes=(),
        )
        with self.assertRaises(NamedGraphError):
            validate_graph(oversized_nodes, ())

        edge = GraphEdge("main", "worker", "delegates-to")
        oversized_edges = GraphSpec(
            nodes=(_node("main", Role.MAIN), _node("worker", Role.WORKER)),
            edges=(edge,) * 257,
            coordination=Coordination("agent", ("main",), "serial", 1),
            routes=(),
        )
        with self.assertRaises(NamedGraphError):
            validate_graph(oversized_edges, ())


if __name__ == "__main__":
    unittest.main()
