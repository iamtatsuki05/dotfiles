"""Address assignment-scoped Orca Delivery records.

Orca serial state keeps its Delivery journal at the state root.  Orca named
parallel state keeps one journal per exact role assignment.  This module only
selects the owning mapping; lifecycle and wire validation remain in the
runtime and question/task modules.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, cast

DELIVERY_FIELDS: Final = frozenset(
    {
        "orca_result",
        "orca_release",
        "pending_orca_effect",
        "pending_delivery_id",
        "pending_delivery_kind",
        "pending_delivery_stage",
        "pending_question_ids",
        "replied_question_ids",
    }
)


def _roles(state: Mapping[str, object]) -> Mapping[str, object]:
    roles = state.get("roles")
    if not isinstance(roles, Mapping):
        raise TypeError("Orca delivery assignments must be an object")
    for node_id, assignment in roles.items():
        if (
            not isinstance(node_id, str)
            or not node_id
            or not isinstance(assignment, Mapping)
            or assignment.get("role") != node_id
        ):
            raise ValueError("Orca delivery assignment identity is invalid")
    return roles


def containers(
    state: Mapping[str, object],
) -> tuple[tuple[str | None, Mapping[str, object]], ...]:
    """Return the exact state mappings that own Orca Delivery fields.

    Only named Orca v4 serial state has a root container.  Named Orca v5
    parallel state has one container for every exact role key and must not
    retain any legacy root Delivery field.  Returned mappings are the objects
    supplied by the caller; this helper never copies or aliases them.
    """

    if not isinstance(state, Mapping):
        raise TypeError("Orca delivery state must be a mapping")
    version = state.get("version")
    runtime = state.get("runtime")
    if version == 4 and runtime == "orca":
        _roles(state)
        return ((None, state),)
    if version != 5 or runtime != "orca":
        raise ValueError("Orca delivery state version or runtime is unsupported")
    if DELIVERY_FIELDS.intersection(state):
        raise ValueError("Orca parallel state contains a legacy root Delivery")
    roles = _roles(state)
    return tuple(
        (node_id, cast(Mapping[str, object], assignment))
        for node_id, assignment in roles.items()
    )


def container(
    state: Mapping[str, object], node_id: str | None = None
) -> Mapping[str, object]:
    """Return one exact Delivery owner, rejecting missing or unknown IDs."""

    records = containers(state)
    if state.get("version") == 4:
        return state
    if not isinstance(node_id, str) or not node_id:
        raise ValueError("Orca parallel delivery requires an exact node identity")
    for selected, assignment in records:
        if selected == node_id:
            return assignment
    raise ValueError("Orca parallel delivery assignment is missing")
