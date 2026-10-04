from __future__ import annotations

import unittest
from types import MappingProxyType

from agent_team.native_delivery import container, containers


class NativeDeliveryContainerTest(unittest.TestCase):
    def test_serial_container_is_the_original_state(self):
        for version in (3, 4):
            state = {"version": version, "roles": {}, "pending_delivery_id": "old"}
            with self.subTest(version=version):
                self.assertIs(container(state, "worker"), state)
                self.assertEqual(containers(state), ((None, state),))

    def test_parallel_returns_each_original_assignment_without_projection(self):
        first = {"role": "writer-a", "native_result": {"delivery_id": "a"}}
        second = {"role": "writer-b", "native_question": {"delivery_id": "b"}}
        state = {"version": 5, "roles": {"writer-a": first, "writer-b": second}}
        self.assertIs(container(state, "writer-a"), first)
        self.assertIs(container(state, "writer-b"), second)
        self.assertEqual(containers(state), (("writer-a", first), ("writer-b", second)))
        container(state, "writer-a")["pending_delivery_stage"] = "read"
        self.assertNotIn("pending_delivery_stage", second)
        self.assertNotIn("pending_delivery_stage", state)

    def test_parallel_never_guesses_or_fabricates_an_assignment(self):
        state = {"version": 5, "roles": {"writer-a": {"role": "writer-a"}}}
        before = repr(state)
        for node in (None, "", "writer-b"):
            with self.subTest(node=node), self.assertRaises(ValueError):
                container(state, node)
        self.assertEqual(repr(state), before)

    def test_unknown_versions_and_malformed_assignments_are_rejected(self):
        for state in (
            {},
            {"version": 2},
            {"version": 6},
            {"version": True},
            {"version": 5},
            {"version": 5, "roles": []},
            {"version": 5, "roles": {1: {}}},
            {"version": 5, "roles": {"": {}}},
            {"version": 5, "roles": {"writer": None}},
            {"version": 5, "roles": {"writer": {"role": "other"}}},
        ):
            with self.subTest(state=state), self.assertRaises((TypeError, ValueError)):
                containers(state)

    def test_parallel_rejects_legacy_root_delivery_fields(self):
        for field in (
            "native_result",
            "native_question",
            "pending_delivery_id",
            "pending_delivery_kind",
            "pending_delivery_stage",
            "pending_question_ids",
            "replied_question_ids",
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                containers({"version": 5, "roles": {}, field: None})

    def test_read_only_mappings_keep_the_same_read_boundary(self):
        entry = MappingProxyType({"role": "worker"})
        state = MappingProxyType(
            {"version": 5, "roles": MappingProxyType({"worker": entry})}
        )
        self.assertIs(container(state, "worker"), entry)
        serial = MappingProxyType({"version": 4})
        self.assertIs(container(serial), serial)
