"""Address native delivery records without projecting one state version as another."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, cast

DELIVERY_FIELDS: Final = frozenset(
    {
        "native_result",
        "native_question",
        "pending_delivery_id",
        "pending_delivery_kind",
        "pending_delivery_stage",
        "pending_question_ids",
        "replied_question_ids",
    }
)


def containers(
    state: Mapping[str, object],
) -> tuple[tuple[str | None, Mapping[str, object]], ...]:
    version = state.get("version")
    if version in {3, 4}:
        return ((None, state),)
    if version != 5:
        raise ValueError("native delivery state version is unsupported")
    if DELIVERY_FIELDS.intersection(state):
        raise ValueError("parallel state contains a legacy root delivery")
    roles = state.get("roles")
    if not isinstance(roles, Mapping):
        raise TypeError("parallel delivery assignments must be an object")
    result: list[tuple[str | None, Mapping[str, object]]] = []
    for node_id, assignment in roles.items():
        if (
            not isinstance(node_id, str)
            or not node_id
            or not isinstance(assignment, Mapping)
            or assignment.get("role") != node_id
        ):
            raise ValueError("parallel delivery assignment identity is invalid")
        result.append((node_id, cast(Mapping[str, object], assignment)))
    return tuple(result)


def container(
    state: Mapping[str, object], node_id: str | None = None
) -> Mapping[str, object]:
    records = containers(state)
    if state.get("version") in {3, 4}:
        return state
    if not isinstance(node_id, str) or not node_id:
        raise ValueError("parallel delivery requires an exact node identity")
    for selected, assignment in records:
        if selected == node_id:
            return assignment
    raise ValueError("parallel delivery assignment is missing")
