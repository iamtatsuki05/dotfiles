from __future__ import annotations

import unittest

from agent_team import orca_delivery


class OrcaParallelDeliveryAddressTest(unittest.TestCase):
    def test_serial_v4_uses_the_root_object_without_copying(self) -> None:
        state: dict[str, object] = {"version": 4, "runtime": "orca", "roles": {}}
        self.assertIs(orca_delivery.containers(state)[0][1], state)
        self.assertIs(orca_delivery.container(state), state)

    def test_parallel_v5_returns_exact_role_objects_without_copying(self) -> None:
        first: dict[str, object] = {"role": "worker-a"}
        second: dict[str, object] = {"role": "worker-b"}
        state: dict[str, object] = {
            "version": 5,
            "runtime": "orca",
            "roles": {"worker-a": first, "worker-b": second},
        }
        records = orca_delivery.containers(state)
        self.assertEqual(tuple(item[0] for item in records), ("worker-a", "worker-b"))
        self.assertIs(records[0][1], first)
        self.assertIs(records[1][1], second)
        self.assertIs(orca_delivery.container(state, "worker-b"), second)

    def test_root_legacy_fields_and_invalid_identity_are_rejected(self) -> None:
        base: dict[str, object] = {
            "version": 5,
            "runtime": "orca",
            "roles": {"worker-a": {"role": "worker-a"}},
        }
        for field in orca_delivery.DELIVERY_FIELDS:
            with self.subTest(field=field), self.assertRaises(ValueError):
                orca_delivery.containers({**base, field: object()})

        with self.assertRaises(ValueError):
            orca_delivery.containers(
                {**base, "roles": {"worker-a": {"role": "worker-b"}}}
            )
        with self.assertRaises(ValueError):
            orca_delivery.container(base)
        with self.assertRaises(ValueError):
            orca_delivery.container(base, "unknown")

    def test_no_runtime_or_version_alias_is_accepted(self) -> None:
        for state in (
            {"version": 3, "runtime": "orca", "roles": {}},
            {"version": 4, "runtime": "zellij", "roles": {}},
            {"version": 5, "runtime": "zellij", "roles": {}},
            {"version": 6, "runtime": "orca", "roles": {}},
        ):
            with self.subTest(state=state), self.assertRaises(ValueError):
                orca_delivery.containers(state)


if __name__ == "__main__":
    unittest.main()
