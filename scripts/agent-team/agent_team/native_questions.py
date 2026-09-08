"""Validate native question outboxes and their consumed, content-free receipts."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Final, cast

from .native_question_channel import (
    MAX_BATCHES,
    MAX_TEXT_CHARS,
    validate_question_request,
)

IDENTITY_FIELDS: Final = (
    "role",
    "run_id",
    "task_id",
    "dispatch_id",
    "terminal_handle",
    "launch_nonce",
)
NAMED_IDENTITY_FIELDS: Final = (*IDENTITY_FIELDS, "role_kind")
_ACP_ROLE_KINDS: Final = frozenset({"planner", "worker", "reviewer"})
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_OUTBOX_FIELDS = frozenset(
    {
        *IDENTITY_FIELDS,
        "phase",
        "request",
        "delivery_id",
        "message_ids",
        "answers",
        "error",
    }
)
_PENDING_FIELDS = (
    "pending_delivery_id",
    "pending_delivery_kind",
    "pending_delivery_stage",
    "pending_question_ids",
    "replied_question_ids",
)


def text(value: object, field: str, *, maximum: int = MAX_TEXT_CHARS) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or "\0" in value
        or any(0xD800 <= ord(character) <= 0xDFFF for character in value)
    ):
        raise ValueError(f"native question {field} is invalid")
    return value


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"native question {field} must be an object")
    return cast(dict[str, object], value)


def _identity_fields(value: Mapping[str, object], *, named: bool) -> tuple[str, ...]:
    fields = NAMED_IDENTITY_FIELDS if named else IDENTITY_FIELDS
    if not named and "role_kind" in value:
        raise ValueError("native question v3 identity has an unexpected role_kind")
    return fields


def _identity(
    value: Mapping[str, object], *, named: bool | None = None
) -> dict[str, str]:
    if named is None:
        named = "role_kind" in value
    fields = _identity_fields(value, named=named)
    result = {field: text(value.get(field), field, maximum=256) for field in fields}
    if named and result["role_kind"] not in _ACP_ROLE_KINDS:
        raise ValueError("native question role_kind is invalid")
    return result


def _named_state(state: Mapping[str, object]) -> bool:
    version = state.get("version")
    if version == 3:
        return False
    if version == 4:
        return True
    raise ValueError("native question state version is invalid")


def _trusted_identity(
    question: Mapping[str, object], expected_identity: Mapping[str, object] | None
) -> tuple[dict[str, str], bool]:
    if expected_identity is None:
        named = "role_kind" in question
        return _identity(question, named=named), named
    named = "role_kind" in expected_identity
    expected = _identity(expected_identity, named=named)
    observed = _identity(question, named=named)
    if observed != expected:
        raise ValueError("native question assignment identity changed")
    return expected, named


def _profile_matches(spec: Mapping[str, object], role_kind: str) -> bool:
    if role_kind not in _ACP_ROLE_KINDS:
        return False
    try:
        from .scoped_acp import native_profile

        expected = native_profile(str(spec.get("provider")), role_kind)
    except (RuntimeError, ValueError):
        return False
    return all(spec.get(key) == value for key, value in expected.items())


def _strings(value: object, field: str, count: int) -> list[str]:
    if not isinstance(value, list) or len(value) != count:
        raise ValueError(f"native question {field} has invalid length")
    result = [text(item, field, maximum=256) for item in value]
    if len(result) != len(set(result)):
        raise ValueError(f"native question {field} has duplicate identities")
    return result


def validate_receipts(value: object, identity: Mapping[str, object]) -> None:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_BATCHES:
        raise ValueError("native question receipts have invalid length")
    named = "role_kind" in identity
    expected = _identity(identity, named=named)
    seen_calls: set[tuple[str, str]] = set()
    seen_deliveries: set[str] = set()
    seen_messages: set[str] = set()
    session: str | None = None
    for item in value:
        receipt = _object(item, "receipt")
        if set(receipt) != {
            *_identity_fields(receipt, named=named),
            "session_id",
            "tool_call_id",
            "delivery_id",
            "messages",
        }:
            raise ValueError("native question receipt fields are invalid")
        if _identity(receipt, named=named) != expected:
            raise ValueError("native question receipt assignment identity changed")
        session_id = text(receipt["session_id"], "session_id", maximum=256)
        tool_call_id = text(receipt["tool_call_id"], "tool_call_id", maximum=256)
        delivery_id = text(receipt["delivery_id"], "delivery_id", maximum=256)
        if session is not None and session_id != session:
            raise ValueError("native question receipt session changed")
        session = session_id
        if (session_id, tool_call_id) in seen_calls or delivery_id in seen_deliveries:
            raise ValueError("native question receipt identity was reused")
        seen_calls.add((session_id, tool_call_id))
        seen_deliveries.add(delivery_id)
        messages = receipt["messages"]
        if not isinstance(messages, list) or not 1 <= len(messages) <= 4:
            raise ValueError("native question receipt messages are invalid")
        for message in messages:
            fields = _object(message, "receipt message")
            if set(fields) != {"message_id", "question_sha256", "answer_sha256"}:
                raise ValueError("native question receipt message fields are invalid")
            message_id = text(fields["message_id"], "message_id", maximum=256)
            if message_id in seen_messages:
                raise ValueError("native question receipt message identity was reused")
            seen_messages.add(message_id)
            for key in ("question_sha256", "answer_sha256"):
                digest = fields[key]
                if not isinstance(digest, str) or not _HASH.fullmatch(digest):
                    raise ValueError("native question receipt digest is invalid")


def outbox(state: Mapping[str, object]) -> dict[str, object] | None:
    named = _named_state(state)
    if "native_question" not in state:
        if state.get("pending_delivery_kind") == "question":
            raise ValueError("native question Delivery has no outbox")
        return None
    question = _object(state["native_question"], "outbox")
    outbox_fields = _OUTBOX_FIELDS | ({"role_kind"} if named else set())
    if set(question) != outbox_fields:
        raise ValueError("native question outbox fields are invalid")
    identity = _identity(question, named=named)
    role = identity["role"]
    specs = _object(state.get("role_specs"), "role specs")
    spec = _object(specs.get(role), "role spec")
    roles = _object(state.get("roles"), "assignments")
    assignment = _object(roles.get(role), "assignment")
    if named:
        if (
            spec.get("kind") != identity["role_kind"]
            or assignment.get("role") != role
            or assignment.get("role_kind") != identity["role_kind"]
        ):
            raise ValueError("native question named assignment identity is invalid")
        profile_kind = identity["role_kind"]
    else:
        if "kind" in spec or "role_kind" in assignment:
            raise ValueError("native question v3 assignment is mixed with named state")
        profile_kind = role
    if (
        spec.get("provider") != "claude"
        or spec.get("transport") != "acp"
        or not _profile_matches(spec, profile_kind)
        or len(roles) != 1
        or identity["run_id"] != state.get("run_id")
        or any(identity[key] != assignment.get(key) for key in IDENTITY_FIELDS[2:])
        or assignment.get("completion_observed") is not False
    ):
        raise ValueError("native question assignment identity is invalid")
    request = validate_question_request(question["request"])
    ids = _strings(question["message_ids"], "message_ids", len(request.questions))
    delivery = text(question["delivery_id"], "delivery_id", maximum=256)
    answers = _object(question["answers"], "answers")
    if not set(answers).issubset(ids):
        raise ValueError("native question answers contain an unknown message")
    for message_id, answer in answers.items():
        text(answer, f"answer for {message_id}")
    phase = question["phase"]
    if not isinstance(phase, str) or phase not in {
        "published",
        "observed",
        "acknowledged",
        "received",
        "recorded",
        "failed",
        "cancelling",
    }:
        raise ValueError("native question phase is invalid")
    if phase == "failed":
        text(question["error"], "error", maximum=2_000)
    elif question["error"] is not None:
        raise ValueError("native question contains an unexpected error")
    native = _object(state.get("native"), "native lifecycle")
    if phase == "cancelling" and native.get("phase") != "stopping":
        raise ValueError("native question cancellation requires team stop")
    pending = state.get("pending_delivery_id")
    if pending is not None:
        if (
            pending != delivery
            or state.get("pending_delivery_kind") != "question"
            or state.get("pending_delivery_stage") != "observed"
            or state.get("pending_question_ids") != ids
            or state.get("replied_question_ids")
            != [message_id for message_id in ids if message_id in answers]
            or phase not in {"observed", "failed", "cancelling"}
        ):
            raise ValueError("native question pending Delivery does not match outbox")
    elif any(field in state for field in _PENDING_FIELDS):
        raise ValueError("native question pending Delivery fields are incomplete")
    if phase == "observed" and pending is None:
        raise ValueError("native question was not observed as a Delivery")
    if phase == "published" and answers:
        raise ValueError("unobserved native question cannot have answers")
    if phase in {"acknowledged", "received", "recorded"} and set(answers) != set(ids):
        raise ValueError("native question requires every answer before acknowledgment")
    result = state.get("native_result")
    if result is not None:
        completion = _object(result, "pending completion")
        if _identity(completion, named=named) != identity:
            raise ValueError("native question completion identity is invalid")
        if completion.get("outcome") != "failed" or phase not in {
            "failed",
            "cancelling",
        }:
            raise ValueError(
                "native question cannot coexist with successful completion"
            )
    receipts = assignment.get("question_receipts")
    if receipts is not None:
        validate_receipts(receipts, identity)
        items = cast(list[dict[str, object]], receipts)
        already_received = (
            items[-1] == receipt(question, expected_identity=identity)
            if set(answers) == set(ids)
            else False
        )
        if (
            any(item["session_id"] != request.session_id for item in items)
            or (
                already_received
                and phase not in {"received", "recorded", "failed", "cancelling"}
            )
            or (
                not already_received
                and (
                    len(items) >= MAX_BATCHES
                    or any(
                        item["tool_call_id"] == request.tool_call_id for item in items
                    )
                )
            )
            or (phase in {"received", "recorded"} and not already_received)
        ):
            raise ValueError(
                "native question session changed, call repeated, or limit reached"
            )
    elif phase in {"received", "recorded"}:
        raise ValueError("native question received phase requires its receipt")
    return question


def receipt(
    question: Mapping[str, object],
    expected_identity: Mapping[str, object] | None = None,
) -> dict[str, object]:
    identity, _named = _trusted_identity(question, expected_identity)
    request = validate_question_request(question["request"])
    ids = _strings(question["message_ids"], "message_ids", len(request.questions))
    answers = _object(question["answers"], "answers")
    return {
        **identity,
        "session_id": request.session_id,
        "tool_call_id": request.tool_call_id,
        "delivery_id": question["delivery_id"],
        "messages": [
            {
                "message_id": message_id,
                "question_sha256": hashlib.sha256(
                    field.body.encode("utf-8")
                ).hexdigest(),
                "answer_sha256": hashlib.sha256(
                    text(answers.get(message_id), "answer").encode("utf-8")
                ).hexdigest(),
            }
            for message_id, field in zip(ids, request.questions, strict=True)
        ],
    }


def _result_identity(
    state: Mapping[str, object],
    result: Mapping[str, object],
    roles: Mapping[str, object],
    specs: Mapping[str, object],
    *,
    named: bool,
) -> dict[str, str]:
    role = result.get("role")
    if not isinstance(role, str):
        raise TypeError("native question result role is invalid")
    spec = _object(specs.get(role), "role spec")
    if named:
        role_kind = text(spec.get("kind"), "role_kind", maximum=64)
        if result.get("role_kind") != role_kind:
            raise ValueError("native question result role kind changed")
    elif "kind" in spec or "role_kind" in result:
        raise ValueError("native question v3 result is mixed with named state")
    assignment = roles.get(role)
    if isinstance(assignment, Mapping):
        identity = {
            **assignment,
            "role": role,
            "run_id": state.get("run_id"),
            **({"role_kind": spec.get("kind")} if named else {}),
        }
        trusted = _identity(identity, named=named)
        if _identity(result, named=named) != trusted:
            raise ValueError("native question result assignment identity changed")
        return trusted
    return _identity(result, named=named)


def _reject_v3_named_fields(state: Mapping[str, object]) -> None:
    specs = state.get("role_specs")
    if isinstance(specs, Mapping) and any(
        isinstance(spec, Mapping) and "kind" in spec for spec in specs.values()
    ):
        raise ValueError("native question v3 role spec has an unexpected kind")
    roles = state.get("roles")
    if isinstance(roles, Mapping):
        for assignment in roles.values():
            if isinstance(assignment, Mapping) and "role_kind" in assignment:
                raise ValueError(
                    "native question v3 assignment has an unexpected role_kind"
                )
    for field in ("native_result", "native_question"):
        value = state.get(field)
        if isinstance(value, Mapping) and "role_kind" in value:
            raise ValueError(f"native question v3 {field} has an unexpected role_kind")


def validate_state(state: Mapping[str, object]) -> None:
    named = _named_state(state)
    if not named:
        _reject_v3_named_fields(state)
    outbox(state)
    roles = _object(state.get("roles"), "assignments")
    specs = _object(state.get("role_specs"), "role specs")
    for role, raw_assignment in roles.items():
        assignment = _object(raw_assignment, "assignment")
        spec = _object(specs.get(role), "role spec")
        if named:
            role_kind = text(spec.get("kind"), "role_kind", maximum=64)
            if (
                assignment.get("role") != role
                or assignment.get("role_kind") != role_kind
            ):
                raise ValueError("native question assignment node identity is invalid")
            trusted_identity = {
                **assignment,
                "role": role,
                "role_kind": role_kind,
                "run_id": state.get("run_id"),
            }
        else:
            if "role_kind" in assignment or "kind" in spec:
                raise ValueError(
                    "native question v3 assignment is mixed with named state"
                )
            trusted_identity = {
                **assignment,
                "role": role,
                "run_id": state.get("run_id"),
            }
        if "question_socket" in assignment:
            root = Path(text(assignment.get("provider_private_root"), "private root"))
            if (
                not root.is_absolute()
                or assignment["question_socket"] != str(root / "q.sock")
                or spec.get("provider") != "claude"
                or spec.get("transport") != "acp"
            ):
                raise ValueError("native question socket does not match its assignment")
        elif "scoped_question_client_sha256" in spec:
            raise ValueError("native question socket is missing from assignment")
        if "question_receipts" in assignment:
            validate_receipts(assignment["question_receipts"], trusted_identity)
    result = state.get("native_result")
    if isinstance(result, Mapping) and "question_receipts" in result:
        identity = _result_identity(state, result, roles, specs, named=named)
        validate_receipts(result["question_receipts"], identity)
    tasks = state.get("tasks")
    if isinstance(tasks, Mapping):
        for record in tasks.values():
            if not isinstance(record, Mapping):
                continue
            for field in ("result", "writer_result", "review_result"):
                result = record.get(field)
                if isinstance(result, Mapping) and "question_receipts" in result:
                    identity = _result_identity(
                        state, result, roles, specs, named=named
                    )
                    validate_receipts(result["question_receipts"], identity)
