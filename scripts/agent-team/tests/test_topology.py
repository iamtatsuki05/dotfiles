from __future__ import annotations

import json
import unittest
from dataclasses import FrozenInstanceError
from typing import cast
from unittest import mock

from agent_team.topology import (
    AgentNode,
    Edge,
    EdgeKind,
    NodeId,
    Permission,
    ProfileRef,
    TeamDefinition,
    TeamId,
    TopologyFormatError,
    TopologyValidationError,
    render_ascii,
    render_json,
    render_mermaid,
    render_topology,
    validate_team,
)


class Resolver:
    def __init__(self) -> None:
        self.calls: list[ProfileRef] = []
        self.profiles: dict[tuple[str, str], frozenset[Permission]] = {
            ("claude", "direct"): frozenset({"orchestrator"}),
            ("codex", "direct"): frozenset({"read-only", "workspace-write"}),
        }

    def resolve(self, profile: ProfileRef) -> frozenset[Permission] | None:
        self.calls.append(profile)
        return self.profiles.get((profile.provider, profile.transport))


def profile(
    provider: str = "codex",
    transport: str = "direct",
    permission: Permission = "workspace-write",
) -> ProfileRef:
    return ProfileRef(provider, transport, permission)


def definition() -> TeamDefinition:
    return TeamDefinition(
        TeamId("build-team"),
        (
            AgentNode(NodeId("reviewer"), "Reviewer", profile(permission="read-only")),
            AgentNode(NodeId("worker"), "Worker", profile()),
            AgentNode(
                NodeId("main"),
                "Main",
                profile("claude", permission="orchestrator"),
                True,
            ),
        ),
        (
            Edge(NodeId("worker"), NodeId("reviewer"), EdgeKind.REVIEWED_BY),
            Edge(NodeId("main"), NodeId("worker"), EdgeKind.DELEGATES_TO),
        ),
    )


class TopologyValidationTest(unittest.TestCase):
    def test_valid_graph_is_frozen_and_uses_read_only_resolver(self) -> None:
        team = definition()
        with self.assertRaises(FrozenInstanceError):
            team.team_id = TeamId("changed")  # type: ignore[misc]

        resolver = Resolver()
        result = validate_team(team, resolver)

        self.assertTrue(result.valid)
        self.assertEqual(result.errors, ())
        self.assertEqual(
            set(resolver.calls),
            {
                profile("claude", permission="orchestrator"),
                profile(),
                profile(permission="read-only"),
            },
        )

    def test_validation_result_is_stable_when_input_order_changes(self) -> None:
        team = definition()
        reordered = TeamDefinition(
            team.team_id, tuple(reversed(team.nodes)), tuple(reversed(team.edges))
        )

        self.assertEqual(
            validate_team(team, Resolver()), validate_team(reordered, Resolver())
        )

    def test_duplicate_id_label_and_main_are_rejected(self) -> None:
        team = TeamDefinition(
            TeamId("team"),
            (
                AgentNode(NodeId("main"), "Main", profile(), True),
                AgentNode(NodeId("MAIN"), "main", profile(), True),
            ),
            (),
        )

        codes = {issue.code for issue in validate_team(team, Resolver()).errors}

        self.assertTrue({"duplicate-node-id", "duplicate-label"} <= codes)
        self.assertIn("main-cardinality", codes)

    def test_unknown_profile_and_permission_mismatch_are_rejected(self) -> None:
        unknown = TeamDefinition(
            TeamId("team"),
            (AgentNode(NodeId("main"), "Main", profile("missing"), True),),
            (),
        )
        mismatch = TeamDefinition(
            TeamId("team"),
            (
                AgentNode(
                    NodeId("main"),
                    "Main",
                    profile("claude", permission="read-only"),
                    True,
                ),
            ),
            (),
        )

        self.assertIn(
            "unknown-profile",
            {issue.code for issue in validate_team(unknown, Resolver()).errors},
        )
        self.assertIn(
            "permission-mismatch",
            {issue.code for issue in validate_team(mismatch, Resolver()).errors},
        )

    def test_main_and_non_main_permissions_are_role_specific(self) -> None:
        wrong_main = TeamDefinition(
            TeamId("team"),
            (AgentNode(NodeId("main"), "Main", profile(), True),),
            (),
        )
        wrong_worker = TeamDefinition(
            TeamId("team"),
            (
                AgentNode(
                    NodeId("main"),
                    "Main",
                    profile("claude", permission="orchestrator"),
                    True,
                ),
                AgentNode(
                    NodeId("worker"),
                    "Worker",
                    profile("claude", permission="orchestrator"),
                ),
            ),
            (Edge(NodeId("main"), NodeId("worker"), EdgeKind.DELEGATES_TO),),
        )

        self.assertIn(
            "main-permission",
            {issue.code for issue in validate_team(wrong_main, Resolver()).errors},
        )
        self.assertIn(
            "non-main-permission",
            {issue.code for issue in validate_team(wrong_worker, Resolver()).errors},
        )

    def test_mixed_relationship_return_to_main_is_not_a_cycle(self) -> None:
        team = definition()
        with_escalation = TeamDefinition(
            team.team_id,
            team.nodes,
            team.edges
            + (Edge(NodeId("reviewer"), NodeId("main"), EdgeKind.ESCALATES_TO),),
        )

        result = validate_team(with_escalation, Resolver())

        self.assertTrue(result.valid, result.errors)

    def test_cycle_within_one_relationship_kind_is_rejected(self) -> None:
        team = definition()
        delegation_cycle = TeamDefinition(
            team.team_id,
            team.nodes,
            team.edges
            + (Edge(NodeId("worker"), NodeId("main"), EdgeKind.DELEGATES_TO),),
        )

        self.assertIn(
            "cycle",
            {
                issue.code
                for issue in validate_team(delegation_cycle, Resolver()).errors
            },
        )

    def test_cardinality_unknown_endpoint_self_review_and_cycles_are_rejected(
        self,
    ) -> None:
        nodes = (
            AgentNode(NodeId("main"), "Main", profile(), True),
            AgentNode(NodeId("worker"), "Worker", profile()),
            AgentNode(NodeId("reviewer"), "Reviewer", profile(permission="read-only")),
            AgentNode(NodeId("orphan"), "Orphan", profile()),
        )
        edges = (
            Edge(NodeId("main"), NodeId("worker"), EdgeKind.DELEGATES_TO),
            Edge(NodeId("reviewer"), NodeId("worker"), EdgeKind.DELEGATES_TO),
            Edge(NodeId("worker"), NodeId("worker"), EdgeKind.REVIEWED_BY),
            Edge(NodeId("worker"), NodeId("reviewer"), EdgeKind.REVIEWED_BY),
            Edge(NodeId("reviewer"), NodeId("main"), EdgeKind.ESCALATES_TO),
            Edge(NodeId("worker"), NodeId("missing"), EdgeKind.DELEGATES_TO),
        )

        result = validate_team(TeamDefinition(TeamId("team"), nodes, edges), Resolver())
        codes = {issue.code for issue in result.errors}

        self.assertTrue(
            {
                "delegation-cardinality",
                "self-review",
                "unknown-node",
            }
            <= codes
        )
        self.assertIn("unreachable-node", codes)

    def test_invalid_text_permission_and_edge_kind_are_rejected(self) -> None:
        invalid_permission = TeamDefinition(
            TeamId("team"),
            (
                AgentNode(
                    NodeId("main"),
                    "Main",
                    ProfileRef("codex", "direct", cast(Permission, "admin")),
                    True,
                ),
            ),
            (),
        )
        invalid_edge_kind = Edge(
            NodeId("main"),
            NodeId("main"),
            cast(EdgeKind, "unknown-kind"),
        )
        invalid_text = TeamDefinition(
            TeamId("team"),
            (AgentNode(NodeId("main\n"), "   ", profile(), True),),
            (invalid_edge_kind,),
        )

        self.assertIn(
            "invalid-permission",
            {
                issue.code
                for issue in validate_team(invalid_permission, Resolver()).errors
            },
        )
        text_codes = {
            issue.code for issue in validate_team(invalid_text, Resolver()).errors
        }
        self.assertTrue(
            {"invalid-node-id", "invalid-label", "invalid-edge-kind"} <= text_codes
        )

    def test_control_characters_and_invalid_unicode_are_rejected(self) -> None:
        for unsafe in (
            "label\x01",
            "label\x1b",
            "label\x7f",
            "la\u0085bel",
            "label\ud800",
        ):
            with self.subTest(unsafe=ascii(unsafe)):
                team = TeamDefinition(
                    TeamId("team"),
                    (
                        AgentNode(
                            NodeId("main"),
                            unsafe,
                            profile("claude", permission="orchestrator"),
                            True,
                        ),
                    ),
                    (),
                )
                result = validate_team(team, Resolver())
                self.assertIn("invalid-label", {issue.code for issue in result.errors})
                with self.assertRaises(TopologyValidationError):
                    render_mermaid(team, Resolver())

    def test_resolver_exceptions_and_invalid_contract_fail_closed(self) -> None:
        class BrokenResolver:
            def __init__(self, error: Exception) -> None:
                self.error = error

            def resolve(self, _profile: ProfileRef) -> frozenset[Permission] | None:
                raise self.error

        for error in (OSError("unavailable"), RuntimeError("unexpected")):
            with self.subTest(error=type(error).__name__):
                result = validate_team(definition(), BrokenResolver(error))
                self.assertIn("resolver-error", {issue.code for issue in result.errors})

        resolver = Resolver()
        resolver.profiles[("codex", "direct")] = cast(
            frozenset[Permission], {"read-only"}
        )
        result = validate_team(definition(), resolver)
        self.assertIn("resolver-contract", {issue.code for issue in result.errors})

    def test_main_flag_must_be_a_boolean(self) -> None:
        team = TeamDefinition(
            TeamId("team"),
            (
                AgentNode(
                    NodeId("main"),
                    "Main",
                    profile("claude", permission="orchestrator"),
                    cast(bool, "false"),
                ),
            ),
            (),
        )

        result = validate_team(team, Resolver())

        self.assertIn("invalid-main-flag", {issue.code for issue in result.errors})

    def test_edge_endpoints_must_use_the_canonical_node_id(self) -> None:
        team = TeamDefinition(
            TeamId("team"),
            (
                AgentNode(
                    NodeId("Main"),
                    "Main",
                    profile("claude", permission="orchestrator"),
                    True,
                ),
                AgentNode(NodeId("Worker"), "Worker", profile()),
            ),
            (Edge(NodeId("main"), NodeId("WORKER"), EdgeKind.DELEGATES_TO),),
        )

        result = validate_team(team, Resolver())

        self.assertIn("noncanonical-endpoint", {issue.code for issue in result.errors})

    def test_each_relationship_kind_rejects_cycles_and_cardinality_overflow(
        self,
    ) -> None:
        base = definition()
        for kind in EdgeKind:
            with self.subTest(kind=kind.value):
                cycle = TeamDefinition(
                    base.team_id,
                    base.nodes,
                    (
                        Edge(NodeId("main"), NodeId("worker"), kind),
                        Edge(NodeId("worker"), NodeId("main"), kind),
                        Edge(NodeId("main"), NodeId("reviewer"), kind),
                    ),
                )
                codes = {
                    issue.code for issue in validate_team(cycle, Resolver()).errors
                }
                self.assertIn("cycle", codes)
                if kind is not EdgeKind.DELEGATES_TO:
                    self.assertIn(
                        "review-cardinality"
                        if kind is EdgeKind.REVIEWED_BY
                        else "escalation-cardinality",
                        codes,
                    )

    def test_empty_graph_and_invalid_definition_fail_before_rendering(self) -> None:
        empty = TeamDefinition(TeamId("team"), (), ())

        result = validate_team(empty, Resolver())
        self.assertTrue(
            {"empty-graph", "main-cardinality"} <= {i.code for i in result.errors}
        )
        for renderer in (render_json, render_ascii, render_mermaid):
            with self.subTest(renderer=renderer.__name__):
                with self.assertRaises(TopologyValidationError) as context:
                    renderer(empty, Resolver())
                self.assertEqual(
                    [issue.code for issue in context.exception.issues],
                    ["empty-graph", "main-cardinality"],
                )


class TopologyRenderingTest(unittest.TestCase):
    def test_json_ascii_and_mermaid_are_canonical_and_safe(self) -> None:
        team = definition()
        resolver = Resolver()
        reordered = TeamDefinition(
            team.team_id, tuple(reversed(team.nodes)), tuple(reversed(team.edges))
        )

        self.assertEqual(render_json(team, resolver), render_json(reordered, resolver))
        self.assertEqual(
            render_ascii(team, resolver), render_ascii(reordered, resolver)
        )
        self.assertEqual(
            render_mermaid(team, resolver), render_mermaid(reordered, resolver)
        )
        payload = json.loads(render_json(team, resolver))
        self.assertEqual(payload["team_id"], "build-team")
        self.assertNotIn("orca", render_json(team, resolver))

    def test_renderer_fixtures_are_stable(self) -> None:
        self.assertEqual(
            render_ascii(definition(), Resolver()),
            """TEAM \"build-team\"
NODES
NODE id=\"main\" label=\"Main\" profile=\"claude/direct/orchestrator\" main=true
NODE id=\"reviewer\" label=\"Reviewer\" profile=\"codex/direct/read-only\" main=false
NODE id=\"worker\" label=\"Worker\" profile=\"codex/direct/workspace-write\" main=false
EDGES
EDGE source=\"main\" kind=\"delegates-to\" target=\"worker\"
EDGE source=\"worker\" kind=\"reviewed-by\" target=\"reviewer\"
""",
        )
        self.assertEqual(
            render_mermaid(definition(), Resolver()),
            """flowchart TD
    n_0d6e4079e36703eb[\"Main &#40;id: main&#41;\"]
    n_2d70999ae1805e4b[\"Reviewer &#40;id: reviewer&#41;\"]
    n_87eba76e7f316453[\"Worker &#40;id: worker&#41;\"]
    n_0d6e4079e36703eb -->|delegates-to| n_87eba76e7f316453
    n_87eba76e7f316453 -->|reviewed-by| n_2d70999ae1805e4b
""",
        )

    def test_special_text_is_escaped_and_not_executable(self) -> None:
        team = TeamDefinition(
            TeamId("team;echo unsafe"),
            (
                AgentNode(
                    NodeId("main;echo unsafe"),
                    'Main "quoted"',
                    profile("claude", permission="orchestrator"),
                    True,
                ),
                AgentNode(
                    NodeId("reviewer--x"),
                    "Reviewer [link] *bold* `code`",
                    profile(permission="read-only"),
                ),
            ),
            (
                Edge(
                    NodeId("main;echo unsafe"),
                    NodeId("reviewer--x"),
                    EdgeKind.ESCALATES_TO,
                ),
            ),
        )

        ascii_output = render_ascii(team, Resolver())
        mermaid_output = render_mermaid(team, Resolver())
        self.assertIn('"main;echo unsafe"', ascii_output)
        self.assertIn("&#91;link&#93;", mermaid_output)
        self.assertNotIn("```", ascii_output + mermaid_output)
        self.assertNotIn("sh -c", ascii_output + mermaid_output)
        self.assertNotIn("main;echo unsafe -->", mermaid_output)

    def test_mermaid_hash_collision_is_rejected_before_output(self) -> None:
        digest = mock.Mock()
        digest.hexdigest.return_value = "0" * 64
        with (
            mock.patch("agent_team.topology.hashlib.sha256", return_value=digest),
            self.assertRaises(TopologyValidationError) as context,
        ):
            render_mermaid(definition(), Resolver())
        self.assertEqual(context.exception.issues[0].code, "renderer-collision")

    def test_unknown_output_format_fails_fast(self) -> None:
        with self.assertRaises(TopologyFormatError):
            render_topology(definition(), "yaml", Resolver())
        with self.assertRaises(TopologyFormatError):
            render_topology(definition(), "JSON", Resolver())


if __name__ == "__main__":
    unittest.main()
