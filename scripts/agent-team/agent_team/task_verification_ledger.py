"""Pure schema-4 task/snapshot/request/receipt projection codecs.

This module owns only bounded, canonical wire representations for task state,
approval-binding data, and safe request/receipt projections.  It does not own
authority, Store provenance, lifecycle mutation, or Gate hydration.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final, NewType, NoReturn, cast

from . import verification_gate as _gate
from .task_policy import (
    _CURRENT_STATE_POLICY_VERSION,
    TaskLane,
    TaskPhase,
    TaskPolicyStateV4,
    TaskPolicyValidationError,
    parse_task_state,
    task_state_to_dict,
)

_CURRENT_TASK_STATE_CODEC_VERSION: Final = 1
_CURRENT_APPROVAL_BINDING_CODEC_VERSION: Final = 1
_CURRENT_VERIFICATION_REQUEST_CODEC_VERSION: Final = 1
_CURRENT_VERIFICATION_RECEIPT_CODEC_VERSION: Final = 1
# The operation row uses this discriminator; it has no duplicate record payload.
_CURRENT_VERIFICATION_RECORD_VERSION: Final = 1
TASK_STATE_CODEC_VERSION: Final = _CURRENT_TASK_STATE_CODEC_VERSION
APPROVAL_BINDING_CODEC_VERSION: Final = _CURRENT_APPROVAL_BINDING_CODEC_VERSION
VERIFICATION_REQUEST_CODEC_VERSION: Final = _CURRENT_VERIFICATION_REQUEST_CODEC_VERSION
VERIFICATION_RECEIPT_CODEC_VERSION: Final = _CURRENT_VERIFICATION_RECEIPT_CODEC_VERSION
VERIFICATION_RECORD_VERSION: Final = _CURRENT_VERIFICATION_RECORD_VERSION

_CURRENT_TASK_STATE_FIELDS: Final = (
    "version",
    "team_id",
    "workspace",
    "sequence",
    "task_id",
    "attempt_id",
    "dispatch_id",
    "worker_node",
    "reviewer_node",
    "review_round",
    "target_head",
    "target_tree_digest",
    "claim_ref",
    "receipt_ref",
    "phase",
)
TASK_STATE_FIELDS: Final = _CURRENT_TASK_STATE_FIELDS

_CURRENT_TASK_STATE_DIGEST_DOMAIN: Final = b"agent-team/task-policy-state/v4\0"
_CURRENT_MAX_TASK_STATE_BYTES: Final = 1_048_576
_CURRENT_MAX_INT64: Final = 2**63 - 1
TASK_STATE_DIGEST_DOMAIN: Final = _CURRENT_TASK_STATE_DIGEST_DOMAIN
MAX_TASK_STATE_BYTES: Final = _CURRENT_MAX_TASK_STATE_BYTES
MAX_INT64: Final = _CURRENT_MAX_INT64

StateDigest = NewType("StateDigest", str)

_CURRENT_APPROVAL_BINDING_SNAPSHOT_VERSION: Final = 1
APPROVAL_BINDING_SNAPSHOT_VERSION: Final = _CURRENT_APPROVAL_BINDING_SNAPSHOT_VERSION
_CURRENT_APPROVAL_BINDING_SNAPSHOT_DIGEST_DOMAIN: Final = (
    b"agent-team/approval-binding-snapshot/v1\0"
)
APPROVAL_BINDING_SNAPSHOT_DIGEST_DOMAIN: Final = (
    _CURRENT_APPROVAL_BINDING_SNAPSHOT_DIGEST_DOMAIN
)
_BARE_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_DOMAIN_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


class TaskStateCodecError(ValueError):
    """Raised when a task-state codec boundary is not admissible."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _error(code: str, message: str) -> TaskStateCodecError:
    return TaskStateCodecError(code, message)


def _reject_float(value: str) -> NoReturn:
    del value
    raise _error("non-integer-number", "floating-point values are not supported")


def _reject_constant(value: str) -> NoReturn:
    del value
    raise _error("non-finite-number", "non-finite numbers are not supported")


def _strict_integer(value: str) -> int:
    try:
        parsed = int(value, 10)
    except (TypeError, ValueError) as exc:
        raise _error("invalid-integer", "integer value is invalid") from exc
    if str(parsed) != value and not (
        value.startswith("-") and str(parsed) == value[1:]
    ):
        raise _error("invalid-integer", "integer value is not canonical")
    if parsed < -_CURRENT_MAX_INT64 - 1 or parsed > _CURRENT_MAX_INT64:
        raise _error("integer-range", "integer value is outside signed int64")
    return parsed


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _error("duplicate-field", "task-state fields must be unique")
        result[key] = value
    return result


def _parse_json(raw: bytes) -> dict[str, object]:
    if type(raw) is not bytes:
        raise _error("invalid-bytes", "task-state payload must be bytes")
    if not raw or len(raw) > _CURRENT_MAX_TASK_STATE_BYTES:
        raise _error("payload-size", "task-state payload size is outside the limit")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise _error("utf8-bom", "task-state payload must not contain a BOM")
    if raw[-1:] != b"\n" or raw.count(b"\n") != 1:
        raise _error("trailing-data", "task-state payload must end with one newline")

    decode_failed = False
    text = ""
    try:
        text = raw[:-1].decode("utf-8")
    except UnicodeDecodeError:
        decode_failed = True
    if decode_failed:
        raise _error("invalid-utf8", "task-state payload is not valid UTF-8")

    parse_failed = False
    parsed: object = None
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_unique_pairs,
            parse_int=_strict_integer,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except TaskStateCodecError:
        raise
    except (RecursionError, TypeError, ValueError):
        # Raise after leaving the json parser's exception context.  JSON
        # decode errors retain the complete document in ``.doc``.
        parse_failed = True
    if parse_failed:
        raise _error("invalid-json", "task-state payload is not valid JSON")

    if type(parsed) is not dict:
        raise _error("invalid-envelope", "task-state payload must be a JSON object")

    fields = tuple(parsed)
    if fields != _CURRENT_TASK_STATE_FIELDS:
        if set(fields) == set(_CURRENT_TASK_STATE_FIELDS):
            raise _error("field-order", "task-state fields are not in canonical order")
        raise _error("field-set", "task-state fields are missing or unsupported")
    return parsed


_STATE_REQUIRED_STRING_FIELDS: Final = ("team_id", "workspace", "task_id")
_STATE_OPTIONAL_STRING_FIELDS: Final = (
    "attempt_id",
    "dispatch_id",
    "worker_node",
    "reviewer_node",
    "target_head",
    "target_tree_digest",
    "claim_ref",
    "receipt_ref",
)


def _validate_typed_state_scalars(state: TaskPolicyStateV4) -> None:
    try:
        values = {name: getattr(state, name) for name in _CURRENT_TASK_STATE_FIELDS}
    except AttributeError:
        raise _error("invalid-state", "task-state fields are incomplete") from None
    version = values["version"]
    if type(version) is not int or version != _CURRENT_STATE_POLICY_VERSION:
        raise _error("invalid-state", "task-state version is invalid")
    for name in ("sequence", "review_round"):
        value = values[name]
        if type(value) is not int or value < 0 or value > _CURRENT_MAX_INT64:
            raise _error("invalid-state", f"task-state {name} is invalid")
    for name in _STATE_REQUIRED_STRING_FIELDS:
        if type(values[name]) is not str:
            raise _error("invalid-state", f"task-state {name} is invalid")
    for name in _STATE_OPTIONAL_STRING_FIELDS:
        value = values[name]
        if value is not None and type(value) is not str:
            raise _error("invalid-state", f"task-state {name} is invalid")
    if type(values["phase"]) is not TaskPhase:
        raise _error("invalid-state", "task-state phase is invalid")


def _validated_state(state: object) -> TaskPolicyStateV4:
    if type(state) is not TaskPolicyStateV4:
        raise _error("invalid-state", "value is not a TaskPolicyStateV4")
    _validate_typed_state_scalars(state)
    try:
        TaskPolicyStateV4.__post_init__(state)
        task_state_to_dict(state)
    except (AttributeError, TypeError, ValueError) as exc:
        if isinstance(exc, TaskStateCodecError):
            raise
        raise _error("invalid-state", "task-state values are invalid") from exc
    return state


def encode_task_state(state: TaskPolicyStateV4) -> bytes:
    """Encode one validated state using the fixed schema-4 wire order."""

    validated = _validated_state(state)
    values = task_state_to_dict(validated)
    mapping = {field: values[field] for field in _CURRENT_TASK_STATE_FIELDS}
    try:
        encoded = (
            json.dumps(
                mapping,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
    except (UnicodeError, TypeError, ValueError) as exc:
        raise _error("encode-failed", "task-state values cannot be encoded") from exc
    if len(encoded) > _CURRENT_MAX_TASK_STATE_BYTES:
        raise _error("payload-size", "task-state payload size is outside the limit")
    return encoded


def decode_task_state(raw: bytes) -> TaskPolicyStateV4:
    """Decode and byte-validate one canonical schema-4 task-state payload."""

    mapping = _parse_json(raw)
    state: TaskPolicyStateV4 | None = None
    try:
        state = parse_task_state(mapping)
    except TaskPolicyValidationError:
        pass
    if state is None:
        raise _error("invalid-state", "task-state values are invalid") from None
    if encode_task_state(state) != raw:
        raise _error("noncanonical", "task-state payload is not canonical")
    return state


def task_state_digest(value: bytes | TaskPolicyStateV4) -> StateDigest:
    """Return the domain-separated digest of canonical task-state bytes."""

    if type(value) is TaskPolicyStateV4:
        payload = encode_task_state(value)
    elif type(value) is bytes:
        decode_task_state(value)
        payload = value
    else:
        raise _error("invalid-digest-input", "task-state digest input is invalid")
    return StateDigest(
        "sha256:"
        + hashlib.sha256(_CURRENT_TASK_STATE_DIGEST_DOMAIN + payload).hexdigest()
    )


class TaskVerificationLedgerError(ValueError):
    """Raised when a task-verification codec boundary is invalid."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _ledger_error(code: str, message: str) -> TaskVerificationLedgerError:
    return TaskVerificationLedgerError(code, message)


def _bounded_text(value: object, name: str) -> str:
    if type(value) is not str or not value:
        raise _ledger_error("invalid-text", f"{name} is invalid")
    try:
        byte_length = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise _ledger_error("unsafe-text", f"{name} is invalid") from None
    if byte_length > 4096:
        raise _ledger_error("invalid-text", f"{name} is invalid")
    if value != value.strip() or any(
        ord(character) < 0x20
        or 0x7F <= ord(character) <= 0x9F
        or 0xD800 <= ord(character) <= 0xDFFF
        or ord(character) in {0x2028, 0x2029}
        for character in value
    ):
        raise _ledger_error("unsafe-text", f"{name} is invalid")
    return value


def _bare_digest(value: object, name: str) -> str:
    if type(value) is not str or _BARE_DIGEST.fullmatch(value) is None:
        raise _ledger_error("invalid-digest", f"{name} is invalid")
    return value


def _domain_digest(value: object, name: str) -> str:
    if type(value) is not str or _DOMAIN_DIGEST.fullmatch(value) is None:
        raise _ledger_error("invalid-digest", f"{name} is invalid")
    return value


def _nonnegative(value: object, name: str) -> int:
    if type(value) is not int or value < 0 or value > _CURRENT_MAX_INT64:
        raise _ledger_error("invalid-sequence", f"{name} is invalid")
    return value


ApprovedReviewProjection = tuple[tuple[str, object], ...]

_APPROVED_FIELDS: Final = (
    "run_id",
    "team_id",
    "workspace",
    "task_id",
    "dispatch_id",
    "attempt_id",
    "worker_node",
    "reviewer_node",
    "worker_terminal_id",
    "reviewer_terminal_id",
    "review_round",
    "target_head",
    "target_tree_digest",
    "claim_ref",
    "policy_fingerprint",
    "routing_lane",
    "approval_ref",
    "approval_sequence",
    "profile_ref",
    "verification_id",
    "routing_digest",
    "reservation_digest",
    "authority_digest",
)


def _projection_json(projection: ApprovedReviewProjection) -> dict[str, object]:
    return {name: value for name, value in projection}


def _snapshot_payload(
    *,
    version: int,
    review_ref: str,
    review_digest: str,
    completion_ref: str,
    completion_digest: str,
    approval_ref: str,
    approval_digest: str,
    approved_review: ApprovedReviewProjection,
    task_state_bytes: bytes,
    task_state_digest: StateDigest,
    root_key: str,
    run_id: str,
    main_terminal_id: str,
    consumer_generation: int,
    workflow_sequence: int,
    workflow_checkpoint_digest: str,
    task_sequence: int,
    effect_owner: str,
) -> dict[str, object]:
    return {
        "version": version,
        "review_ref": review_ref,
        "review_digest": review_digest,
        "completion_ref": completion_ref,
        "completion_digest": completion_digest,
        "approval_ref": approval_ref,
        "approval_digest": approval_digest,
        "approved_review": _projection_json(approved_review),
        "task_state_bytes": task_state_bytes.decode("utf-8"),
        "task_state_digest": str(task_state_digest),
        "root_key": root_key,
        "run_id": run_id,
        "main_terminal_id": main_terminal_id,
        "consumer_generation": consumer_generation,
        "workflow_sequence": workflow_sequence,
        "workflow_checkpoint_digest": workflow_checkpoint_digest,
        "task_sequence": task_sequence,
        "effect_owner": effect_owner,
    }


def _snapshot_digest(payload: Mapping[str, object]) -> str:
    try:
        encoded = (
            json.dumps(
                dict(payload),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, UnicodeError, ValueError) as exc:
        raise _ledger_error(
            "snapshot-encode-failed", "snapshot is not encodable"
        ) from exc
    return (
        "sha256:"
        + hashlib.sha256(
            _CURRENT_APPROVAL_BINDING_SNAPSHOT_DIGEST_DOMAIN + encoded
        ).hexdigest()
    )


def _validate_projection(value: object) -> ApprovedReviewProjection:
    if type(value) is not tuple:
        raise _ledger_error("approval-projection", "approval projection is invalid")
    if len(value) != len(_APPROVED_FIELDS):
        raise _ledger_error("approval-projection", "approval projection is invalid")
    result: list[tuple[str, object]] = []
    for item in value:
        if type(item) is not tuple or len(item) != 2 or type(item[0]) is not str:
            raise _ledger_error("approval-projection", "approval projection is invalid")
        name, field_value = item
        if not (
            type(field_value) is str or type(field_value) is int or field_value is None
        ):
            raise _ledger_error(
                "approval-projection", "approval projection is not primitive"
            )
        result.append((name, field_value))
    projection = tuple(result)
    if tuple(name for name, _ in projection) != _APPROVED_FIELDS:
        raise _ledger_error("approval-projection", "approval projection fields differ")
    text_fields = {
        "run_id",
        "team_id",
        "workspace",
        "task_id",
        "dispatch_id",
        "attempt_id",
        "worker_node",
        "reviewer_node",
        "worker_terminal_id",
        "reviewer_terminal_id",
        "claim_ref",
        "approval_ref",
        "profile_ref",
        "verification_id",
    }
    digest_fields = {
        "policy_fingerprint",
        "routing_digest",
        "authority_digest",
        "target_tree_digest",
    }
    for name, field_value in projection:
        if name in text_fields:
            _bounded_text(field_value, f"approval.{name}")
        elif name == "review_round":
            if type(field_value) is not int or field_value <= 0:
                raise _ledger_error(
                    "approval-range", "approval review round is invalid"
                )
            _nonnegative(field_value, "approval.review_round")
        elif name == "approval_sequence":
            _nonnegative(field_value, "approval.approval_sequence")
        elif name == "target_head":
            if type(field_value) is not str or not re.fullmatch(
                r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z", field_value
            ):
                raise _ledger_error(
                    "approval-target", "approval target head is invalid"
                )
        elif name in digest_fields:
            _bare_digest(field_value, f"approval.{name}")
        elif name == "reservation_digest":
            if field_value is not None:
                _bare_digest(field_value, "approval.reservation_digest")
        elif name == "routing_lane":
            if field_value not in {"normal", "express"}:
                raise _ledger_error("approval-lane", "approval routing lane is invalid")
        else:
            raise _ledger_error("approval-projection", "approval field is unsupported")
    return projection


def _projection_from_approved(
    approved: _gate.ApprovedReview,
) -> ApprovedReviewProjection:
    if type(approved) is not _gate.ApprovedReview:
        raise _ledger_error("approval-invalid", "approval type is not exact")
    result: list[tuple[str, object]] = []
    try:
        for name in _APPROVED_FIELDS:
            field_value = getattr(approved, name)
            if isinstance(field_value, Enum):
                field_value = field_value.value
            result.append((name, field_value))
    except AttributeError:
        raise _ledger_error("approval-invalid", "approval is incomplete") from None
    return _validate_projection(tuple(result))


@dataclass(frozen=True, slots=True)
class ApprovalBindingSnapshotV1:
    """Canonical, non-authority projection of approval-binding data."""

    version: int
    review_ref: str
    review_digest: str
    completion_ref: str
    completion_digest: str
    approval_ref: str
    approval_digest: str
    approved_review: ApprovedReviewProjection
    task_state_bytes: bytes
    task_state_digest: StateDigest
    binding_digest: str
    root_key: str
    run_id: str
    main_terminal_id: str
    consumer_generation: int
    workflow_sequence: int
    workflow_checkpoint_digest: str
    task_sequence: int
    effect_owner: str

    def __post_init__(self) -> None:
        _require_dataclass_attributes(
            self, _CURRENT_APPROVAL_BINDING_SNAPSHOT_FIELDS, "snapshot"
        )
        if (
            type(self.version) is not int
            or self.version != _CURRENT_APPROVAL_BINDING_SNAPSHOT_VERSION
        ):
            raise _ledger_error("snapshot-version", "snapshot version is invalid")
        _bounded_text(self.review_ref, "snapshot.review_ref")
        _bare_digest(self.review_digest, "snapshot.review_digest")
        _bounded_text(self.completion_ref, "snapshot.completion_ref")
        _bare_digest(self.completion_digest, "snapshot.completion_digest")
        _bounded_text(self.approval_ref, "snapshot.approval_ref")
        _bare_digest(self.approval_digest, "snapshot.approval_digest")
        projection = _validate_projection(self.approved_review)
        if type(self.task_state_bytes) is not bytes:
            raise _ledger_error(
                "snapshot-state", "snapshot task state bytes are invalid"
            )
        state = decode_task_state(self.task_state_bytes)
        expected_state_digest = task_state_digest(self.task_state_bytes)
        if self.task_state_digest != expected_state_digest:
            raise _ledger_error(
                "state-digest-mismatch", "snapshot state digest differs"
            )
        _bounded_text(self.root_key, "snapshot.root_key")
        _bounded_text(self.run_id, "snapshot.run_id")
        _bounded_text(self.main_terminal_id, "snapshot.main_terminal_id")
        _nonnegative(self.consumer_generation, "snapshot.consumer_generation")
        _nonnegative(self.workflow_sequence, "snapshot.workflow_sequence")
        _domain_digest(
            self.workflow_checkpoint_digest, "snapshot.workflow_checkpoint_digest"
        )
        _nonnegative(self.task_sequence, "snapshot.task_sequence")
        if self.task_sequence != state.sequence:
            raise _ledger_error("sequence-mismatch", "snapshot task sequence differs")
        _bounded_text(self.effect_owner, "snapshot.effect_owner")
        _validate_projection_approval(projection)
        _validate_snapshot_overlap(self, projection, state)
        payload = _snapshot_payload(
            version=self.version,
            review_ref=self.review_ref,
            review_digest=self.review_digest,
            completion_ref=self.completion_ref,
            completion_digest=self.completion_digest,
            approval_ref=self.approval_ref,
            approval_digest=self.approval_digest,
            approved_review=projection,
            task_state_bytes=self.task_state_bytes,
            task_state_digest=self.task_state_digest,
            root_key=self.root_key,
            run_id=self.run_id,
            main_terminal_id=self.main_terminal_id,
            consumer_generation=self.consumer_generation,
            workflow_sequence=self.workflow_sequence,
            workflow_checkpoint_digest=self.workflow_checkpoint_digest,
            task_sequence=self.task_sequence,
            effect_owner=self.effect_owner,
        )
        _domain_digest(self.binding_digest, "snapshot.binding_digest")
        if self.binding_digest != _snapshot_digest(payload):
            raise _ledger_error("snapshot-digest", "snapshot digest differs")


def _validate_snapshot_overlap(
    snapshot: ApprovalBindingSnapshotV1,
    projection: ApprovedReviewProjection,
    state: TaskPolicyStateV4,
) -> None:
    """Validate duplicated values without making the projection authoritative."""

    values = dict(projection)
    duplicate_values = (
        (snapshot.approval_ref, values["approval_ref"]),
        (snapshot.approval_digest, values["authority_digest"]),
        (snapshot.run_id, values["run_id"]),
        (snapshot.task_sequence, values["approval_sequence"]),
        (state.sequence, values["approval_sequence"]),
        (state.team_id, values["team_id"]),
        (state.workspace, values["workspace"]),
        (state.task_id, values["task_id"]),
        (state.dispatch_id, values["dispatch_id"]),
        (state.attempt_id, values["attempt_id"]),
        (state.worker_node, values["worker_node"]),
        (state.reviewer_node, values["reviewer_node"]),
        (state.review_round, values["review_round"]),
        (state.target_head, values["target_head"]),
        (state.target_tree_digest, values["target_tree_digest"]),
        (state.claim_ref, values["claim_ref"]),
    )
    if any(left != right for left, right in duplicate_values):
        raise _ledger_error(
            "snapshot-correlation", "snapshot duplicated values do not agree"
        )
    if state.phase is not TaskPhase.APPROVED:
        raise _ledger_error("snapshot-phase", "snapshot task state must be approved")
    if state.receipt_ref is not None:
        raise _ledger_error(
            "snapshot-phase", "approved snapshot must not contain a receipt"
        )


# The #51 request and receipt objects contain runner-only values (argv and
# environment values) as well as process-local issuer state.  These codecs
# deliberately project only the fields that are safe and necessary for a
# durable Store row.  The projection classes below are not Gate authority and
# cannot be passed to the Gate's state port.
_CURRENT_APPROVAL_BINDING_SNAPSHOT_FIELDS: Final = (
    "version",
    "review_ref",
    "review_digest",
    "completion_ref",
    "completion_digest",
    "approval_ref",
    "approval_digest",
    "approved_review",
    "task_state_bytes",
    "task_state_digest",
    "binding_digest",
    "root_key",
    "run_id",
    "main_terminal_id",
    "consumer_generation",
    "workflow_sequence",
    "workflow_checkpoint_digest",
    "task_sequence",
    "effect_owner",
)
_CURRENT_VERIFICATION_REQUEST_PROJECTION_FIELDS: Final = (
    "version",
    "approval_ref",
    "verification_id",
    "request_digest",
    "approval",
    "profile_ref",
    "profile_identity",
    "profile_binding_digest",
    "executable",
    "argv_digest",
    "cwd",
    "environment_names",
    "timeout_ms",
    "output_limit_bytes",
    "result_schema",
    "before_snapshot",
)
_CURRENT_VERIFICATION_RECEIPT_PROJECTION_FIELDS: Final = (
    "version",
    "receipt_ref",
    "receipt_digest",
    "verification_ref",
    "approval_ref",
    "request_digest",
    "approval",
    "routing_digest",
    "reservation_digest",
    "profile_ref",
    "profile_identity",
    "profile_binding_digest",
    "executable_before",
    "executable_after",
    "effect_nonce",
    "lease_epoch",
    "fencing_token",
    "argv_digest",
    "cwd",
    "environment_names",
    "timeout_ms",
    "output_limit_bytes",
    "result_schema",
    "before_snapshot",
    "after_snapshot",
    "outcome",
    "exit_code",
    "stdout_sha256",
    "stderr_sha256",
    "stdout_bytes",
    "stderr_bytes",
    "cleanup",
)
APPROVAL_BINDING_SNAPSHOT_FIELDS: Final = _CURRENT_APPROVAL_BINDING_SNAPSHOT_FIELDS
VERIFICATION_REQUEST_PROJECTION_FIELDS: Final = (
    _CURRENT_VERIFICATION_REQUEST_PROJECTION_FIELDS
)
VERIFICATION_RECEIPT_PROJECTION_FIELDS: Final = (
    _CURRENT_VERIFICATION_RECEIPT_PROJECTION_FIELDS
)

_CURRENT_VERIFICATION_REQUEST_PROJECTION_DIGEST_DOMAIN: Final = (
    b"agent-team/verification-request-projection/v1\0"
)
_CURRENT_VERIFICATION_RECEIPT_PROJECTION_DIGEST_DOMAIN: Final = (
    b"agent-team/verification-receipt-projection/v1\0"
)
VERIFICATION_REQUEST_PROJECTION_DIGEST_DOMAIN: Final = (
    _CURRENT_VERIFICATION_REQUEST_PROJECTION_DIGEST_DOMAIN
)
VERIFICATION_RECEIPT_PROJECTION_DIGEST_DOMAIN: Final = (
    _CURRENT_VERIFICATION_RECEIPT_PROJECTION_DIGEST_DOMAIN
)
_CURRENT_MAX_VERIFICATION_PAYLOAD_BYTES: Final = 1_048_576
MAX_VERIFICATION_PAYLOAD_BYTES: Final = _CURRENT_MAX_VERIFICATION_PAYLOAD_BYTES


def _encode_payload(
    mapping: Mapping[str, object], fields_order: tuple[str, ...], label: str
) -> bytes:
    if type(mapping) is not dict or tuple(mapping) != fields_order:
        raise _ledger_error("field-order", f"{label} fields are not canonical")
    try:
        encoded = (
            json.dumps(
                mapping,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, UnicodeError, ValueError) as exc:
        raise _ledger_error("encode-failed", f"{label} cannot be encoded") from exc
    if len(encoded) > _CURRENT_MAX_VERIFICATION_PAYLOAD_BYTES:
        raise _ledger_error("payload-size", f"{label} exceeds its byte limit")
    return encoded


def _parse_payload(
    raw: object, fields_order: tuple[str, ...], label: str
) -> dict[str, object]:
    if type(raw) is not bytes:
        raise _ledger_error("invalid-bytes", f"{label} payload must be bytes")
    if not raw or len(raw) > _CURRENT_MAX_VERIFICATION_PAYLOAD_BYTES:
        raise _ledger_error("payload-size", f"{label} payload size is invalid")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise _ledger_error("utf8-bom", f"{label} payload must not contain a BOM")
    if raw[-1:] != b"\n" or raw.count(b"\n") != 1:
        raise _ledger_error(
            "trailing-data", f"{label} payload must end with one newline"
        )
    decode_failed = False
    text = ""
    try:
        text = raw[:-1].decode("utf-8")
    except UnicodeDecodeError:
        decode_failed = True
    if decode_failed:
        raise _ledger_error("invalid-utf8", f"{label} payload is not UTF-8")
    parse_failed = False
    parsed: object = None
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_unique_pairs,
            parse_int=_strict_integer,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except TaskVerificationLedgerError:
        raise
    except (RecursionError, TypeError, ValueError):
        # Raise after leaving the json parser's exception context.  JSON
        # decode errors retain the complete document in ``.doc``.
        parse_failed = True
    if parse_failed:
        raise _ledger_error("invalid-json", f"{label} payload is not valid JSON")
    if type(parsed) is not dict:
        raise _ledger_error("invalid-envelope", f"{label} payload must be an object")
    actual = tuple(parsed)
    if actual != fields_order:
        if set(actual) == set(fields_order):
            raise _ledger_error("field-order", f"{label} fields are not canonical")
        raise _ledger_error("field-set", f"{label} fields are missing or unsupported")
    return parsed


def _strict_mapping(
    value: object, fields_order: tuple[str, ...], label: str
) -> dict[str, object]:
    if type(value) is not dict:
        raise _ledger_error("invalid-object", f"{label} must be an object")
    actual = tuple(value)
    if actual != fields_order:
        if set(actual) == set(fields_order):
            raise _ledger_error("field-order", f"{label} fields are not canonical")
        raise _ledger_error("field-set", f"{label} fields are missing or unsupported")
    return value


def _snapshot_wire_projection(value: _gate.VerificationSnapshot) -> dict[str, object]:
    if type(value) is not _gate.VerificationSnapshot:
        raise _ledger_error("snapshot-invalid", "snapshot type is not exact")
    _gate.VerificationSnapshot.__post_init__(value)
    return {
        "workspace": value.workspace,
        "canonical_path": value.canonical_path,
        "device": value.device,
        "inode": value.inode,
        "claim_ref": value.claim_ref,
        "target_head": value.target_head,
        "allowed_tree_digest": value.allowed_tree_digest,
    }


def _profile_identity_wire(
    value: _gate.VerificationProfileIdentity,
) -> dict[str, object]:
    if type(value) is not _gate.VerificationProfileIdentity:
        raise _ledger_error("profile-invalid", "profile identity type is not exact")
    _gate.VerificationProfileIdentity.__post_init__(value)
    return {
        "harness_id": value.harness_id,
        "permission": value.permission,
        "operating_system": value.operating_system,
        "architecture": value.architecture,
        "probe_revision": value.probe_revision,
        "sandbox_policy_id": value.sandbox_policy_id,
    }


def _executable_wire(
    value: _gate.VerificationExecutableIdentity,
) -> dict[str, object]:
    if type(value) is not _gate.VerificationExecutableIdentity:
        raise _ledger_error("executable-invalid", "executable type is not exact")
    _gate.VerificationExecutableIdentity.__post_init__(value)
    return {"path": value.path, "version": value.version, "sha256": value.sha256}


def _result_schema_wire(value: _gate.ResultSchema) -> dict[str, object]:
    if type(value) is not _gate.ResultSchema:
        raise _ledger_error("schema-invalid", "result schema type is not exact")
    _gate.ResultSchema.__post_init__(value)
    return {
        "schema_id": value.schema_id,
        "version": value.version,
        "digest": value.digest,
    }


def _approved_projection_wire(
    value: ApprovedReviewProjection,
) -> dict[str, object]:
    projection = _validate_projection(value)
    return {name: field_value for name, field_value in projection}


def _approved_projection_from_wire(value: object) -> ApprovedReviewProjection:
    mapping = _strict_mapping(value, _APPROVED_FIELDS, "approved_review")
    projection = tuple((name, mapping[name]) for name in _APPROVED_FIELDS)
    return _validate_projection(projection)


def _snapshot_projection_from_wire(value: object) -> _gate.VerificationSnapshot:
    mapping = _strict_mapping(
        value,
        (
            "workspace",
            "canonical_path",
            "device",
            "inode",
            "claim_ref",
            "target_head",
            "allowed_tree_digest",
        ),
        "snapshot",
    )
    try:
        result = _gate.VerificationSnapshot(**cast(Any, mapping))
        _gate.VerificationSnapshot.__post_init__(result)
        return result
    except (AttributeError, TypeError, ValueError) as exc:
        raise _ledger_error("snapshot-invalid", "snapshot values are invalid") from exc


def _profile_identity_from_wire(
    value: object,
) -> _gate.VerificationProfileIdentity:
    mapping = _strict_mapping(
        value,
        (
            "harness_id",
            "permission",
            "operating_system",
            "architecture",
            "probe_revision",
            "sandbox_policy_id",
        ),
        "profile_identity",
    )
    try:
        result = _gate.VerificationProfileIdentity(**cast(Any, mapping))
        _gate.VerificationProfileIdentity.__post_init__(result)
        return result
    except (AttributeError, TypeError, ValueError) as exc:
        raise _ledger_error(
            "profile-invalid", "profile identity values are invalid"
        ) from exc


def _executable_from_wire(value: object) -> _gate.VerificationExecutableIdentity:
    mapping = _strict_mapping(value, ("path", "version", "sha256"), "executable")
    try:
        result = _gate.VerificationExecutableIdentity(**cast(Any, mapping))
        _gate.VerificationExecutableIdentity.__post_init__(result)
        return result
    except (AttributeError, TypeError, ValueError) as exc:
        raise _ledger_error(
            "executable-invalid", "executable values are invalid"
        ) from exc


def _result_schema_from_wire(value: object) -> _gate.ResultSchema:
    mapping = _strict_mapping(
        value, ("schema_id", "version", "digest"), "result_schema"
    )
    try:
        result = _gate.ResultSchema(**cast(Any, mapping))
        _gate.ResultSchema.__post_init__(result)
        return result
    except (AttributeError, TypeError, ValueError) as exc:
        raise _ledger_error(
            "schema-invalid", "result schema values are invalid"
        ) from exc


def _validate_projection_approval(
    projection: ApprovedReviewProjection,
) -> None:
    """Validate an approval projection without returning Gate authority."""

    validated = _validate_projection(projection)
    values = dict(validated)
    try:
        values["routing_lane"] = TaskLane(str(values["routing_lane"]))
        approved = _gate._make_approved(**values)
    except (AttributeError, KeyError, TypeError, ValueError):
        raise _ledger_error(
            "approval-invalid", "approval projection values are invalid"
        ) from None
    if approved.authority_digest != values["authority_digest"]:
        raise _ledger_error(
            "approval-invalid", "approval digest does not bind its fields"
        )


def _validate_projection_digest(value: object, label: str) -> str:
    return _bare_digest(value, label)


def _require_dataclass_attributes(
    value: object, names: tuple[str, ...], label: str
) -> None:
    for name in names:
        try:
            getattr(value, name)
        except AttributeError:
            raise _ledger_error("invalid-object", f"{label} is incomplete") from None


def _validate_gate_profile_identity(value: object, label: str) -> None:
    if type(value) is not _gate.VerificationProfileIdentity:
        raise _ledger_error("profile-invalid", f"{label} type is not exact")
    try:
        _gate.VerificationProfileIdentity.__post_init__(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise _ledger_error("profile-invalid", f"{label} values are invalid") from exc


def _validate_gate_executable(value: object, label: str) -> None:
    if type(value) is not _gate.VerificationExecutableIdentity:
        raise _ledger_error("executable-invalid", f"{label} type is not exact")
    try:
        _gate.VerificationExecutableIdentity.__post_init__(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise _ledger_error(
            "executable-invalid", f"{label} values are invalid"
        ) from exc


def _validate_gate_result_schema(value: object, label: str) -> None:
    if type(value) is not _gate.ResultSchema:
        raise _ledger_error("schema-invalid", f"{label} type is not exact")
    try:
        _gate.ResultSchema.__post_init__(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise _ledger_error("schema-invalid", f"{label} values are invalid") from exc


def _validate_gate_snapshot(value: object, label: str) -> None:
    if type(value) is not _gate.VerificationSnapshot:
        raise _ledger_error("snapshot-invalid", f"{label} type is not exact")
    try:
        _gate.VerificationSnapshot.__post_init__(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise _ledger_error("snapshot-invalid", f"{label} values are invalid") from exc


def _validate_snapshot_approval_projection(
    snapshot: _gate.VerificationSnapshot,
    approval: Mapping[str, object],
) -> None:
    if (
        snapshot.workspace != approval["workspace"]
        or snapshot.claim_ref != approval["claim_ref"]
        or snapshot.target_head != approval["target_head"]
        or snapshot.allowed_tree_digest != approval["target_tree_digest"]
    ):
        raise _ledger_error(
            "snapshot-invalid", "snapshot does not match approval projection"
        )


def _framed_digest(parts: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    digest.update(len(parts).to_bytes(8, "big"))
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _snapshot_parts(value: _gate.VerificationSnapshot) -> tuple[str, ...]:
    return (
        str(value.workspace),
        value.canonical_path,
        str(value.device),
        str(value.inode),
        str(value.claim_ref),
        str(value.target_head),
        str(value.allowed_tree_digest),
    )


def _receipt_gate_digest(
    value: VerificationReceiptProjectionV1,
    approval: Mapping[str, object],
) -> str:
    after = value.executable_after
    parts = (
        "verification-receipt-v3",
        str(value.receipt_ref),
        str(value.verification_ref),
        str(value.approval_ref),
        str(value.request_digest),
        str(approval["run_id"]),
        str(approval["team_id"]),
        str(approval["workspace"]),
        str(approval["task_id"]),
        str(approval["dispatch_id"]),
        str(approval["attempt_id"]),
        str(approval["worker_node"]),
        str(approval["reviewer_node"]),
        str(approval["worker_terminal_id"]),
        str(approval["reviewer_terminal_id"]),
        str(approval["review_round"]),
        str(approval["target_head"]),
        str(approval["target_tree_digest"]),
        str(approval["claim_ref"]),
        str(approval["policy_fingerprint"]),
        str(approval["routing_lane"]),
        str(approval["approval_ref"]),
        str(approval["approval_sequence"]),
        str(approval["profile_ref"]),
        str(approval["verification_id"]),
        str(value.routing_digest),
        "" if value.reservation_digest is None else str(value.reservation_digest),
        str(value.profile_ref),
        value.profile_identity.harness_id,
        value.profile_identity.permission,
        value.profile_identity.operating_system,
        value.profile_identity.architecture,
        value.profile_identity.probe_revision,
        value.profile_identity.sandbox_policy_id,
        str(value.profile_binding_digest),
        value.executable_before.path,
        value.executable_before.version,
        value.executable_before.sha256,
        "" if after is None else after.path,
        "" if after is None else after.version,
        "" if after is None else after.sha256,
        str(value.effect_nonce),
        str(value.lease_epoch),
        str(value.fencing_token),
        str(value.argv_digest),
        value.cwd,
        *(str(item) for item in value.environment_names),
        str(value.timeout_ms),
        str(value.output_limit_bytes),
        str(value.result_schema.schema_id),
        str(value.result_schema.version),
        str(value.result_schema.digest),
        *_snapshot_parts(value.before_snapshot),
        *_snapshot_parts(value.after_snapshot),
        value.outcome.value,
        "" if value.exit_code is None else str(value.exit_code),
        "" if value.stdout_sha256 is None else str(value.stdout_sha256),
        "" if value.stderr_sha256 is None else str(value.stderr_sha256),
        str(value.stdout_bytes),
        str(value.stderr_bytes),
        value.cleanup.value,
    )
    return _framed_digest(parts)


def _validate_request_projection(value: VerificationRequestProjectionV1) -> None:
    if type(value) is not VerificationRequestProjectionV1:
        raise _ledger_error(
            "request-projection-invalid", "projection type is not exact"
        )
    _require_dataclass_attributes(
        value,
        _CURRENT_VERIFICATION_REQUEST_PROJECTION_FIELDS,
        "request projection",
    )
    if (
        type(value.version) is not int
        or value.version != _CURRENT_VERIFICATION_REQUEST_CODEC_VERSION
    ):
        raise _ledger_error(
            "projection-version", "request projection version is invalid"
        )
    approval_projection = _validate_projection(value.approval)
    _validate_projection_approval(approval_projection)
    approval = dict(approval_projection)
    _bounded_text(value.approval_ref, "request.approval_ref")
    _bounded_text(value.verification_id, "request.verification_id")
    if approval["approval_ref"] != value.approval_ref:
        raise _ledger_error("approval-invalid", "request approval ref differs")
    if approval["verification_id"] != value.verification_id:
        raise _ledger_error("verification-invalid", "request verification id differs")
    _validate_projection_digest(value.request_digest, "request.request_digest")
    _bounded_text(value.profile_ref, "request.profile_ref")
    if approval["profile_ref"] != value.profile_ref:
        raise _ledger_error("profile-mismatch", "request profile ref differs")
    _validate_gate_profile_identity(value.profile_identity, "request.profile_identity")
    _validate_projection_digest(
        value.profile_binding_digest, "request.profile_binding_digest"
    )
    _validate_gate_executable(value.executable, "request.executable")
    _validate_projection_digest(value.argv_digest, "request.argv_digest")
    _gate._canonical_path(value.cwd, "request.cwd")
    names = _gate._validate_environment_names(
        value.environment_names, "request.environment_names"
    )
    if names != value.environment_names:
        raise _ledger_error("environment-mismatch", "request environment names differ")
    _gate._positive(value.timeout_ms, "request.timeout_ms", _gate.MAX_TIMEOUT_MS)
    _gate._positive(
        value.output_limit_bytes,
        "request.output_limit_bytes",
        _gate.MAX_OUTPUT_LIMIT_BYTES,
    )
    _validate_gate_result_schema(value.result_schema, "request.result_schema")
    _validate_gate_snapshot(value.before_snapshot, "request.before_snapshot")
    try:
        _validate_snapshot_approval_projection(value.before_snapshot, approval)
    except (AttributeError, TypeError, ValueError) as exc:
        raise _ledger_error(
            "snapshot-invalid", "request snapshot does not match approval"
        ) from exc
    if (
        value.cwd != approval["workspace"]
        or value.before_snapshot.workspace != value.cwd
    ):
        raise _ledger_error("workspace-mismatch", "request cwd differs from approval")


def _validate_receipt_projection(value: VerificationReceiptProjectionV1) -> None:
    if type(value) is not VerificationReceiptProjectionV1:
        raise _ledger_error(
            "receipt-projection-invalid", "projection type is not exact"
        )
    if (
        type(value.version) is not int
        or value.version != _CURRENT_VERIFICATION_RECEIPT_CODEC_VERSION
    ):
        raise _ledger_error(
            "projection-version", "receipt projection version is invalid"
        )
    _require_dataclass_attributes(
        value,
        _CURRENT_VERIFICATION_RECEIPT_PROJECTION_FIELDS,
        "receipt projection",
    )
    approval_projection = _validate_projection(value.approval)
    _validate_projection_approval(approval_projection)
    approval = dict(approval_projection)
    _bounded_text(value.receipt_ref, "receipt.receipt_ref")
    _validate_projection_digest(value.receipt_digest, "receipt.receipt_digest")
    _bounded_text(value.verification_ref, "receipt.verification_ref")
    _bounded_text(value.approval_ref, "receipt.approval_ref")
    if approval["approval_ref"] != value.approval_ref:
        raise _ledger_error("approval-invalid", "receipt approval ref differs")
    if approval["verification_id"] != value.verification_ref:
        raise _ledger_error("verification-invalid", "receipt verification ref differs")
    _validate_projection_digest(value.request_digest, "receipt.request_digest")
    _validate_projection_digest(value.routing_digest, "receipt.routing_digest")
    if value.reservation_digest is not None:
        _validate_projection_digest(
            value.reservation_digest, "receipt.reservation_digest"
        )
    if approval["routing_digest"] != value.routing_digest:
        raise _ledger_error("routing-mismatch", "receipt nested routing digest differs")
    if approval["reservation_digest"] != value.reservation_digest:
        raise _ledger_error(
            "reservation-mismatch", "receipt nested reservation digest differs"
        )
    _bounded_text(value.profile_ref, "receipt.profile_ref")
    if approval["profile_ref"] != value.profile_ref:
        raise _ledger_error("profile-mismatch", "receipt profile ref differs")
    _validate_gate_profile_identity(value.profile_identity, "receipt.profile_identity")
    _validate_projection_digest(
        value.profile_binding_digest, "receipt.profile_binding_digest"
    )
    _validate_gate_executable(value.executable_before, "receipt.executable_before")
    if value.executable_after is not None:
        _validate_gate_executable(value.executable_after, "receipt.executable_after")
    _bounded_text(value.effect_nonce, "receipt.effect_nonce")
    if type(value.lease_epoch) is not int or not (
        0 <= value.lease_epoch <= _CURRENT_MAX_INT64
    ):
        raise _ledger_error("invalid-sequence", "receipt lease epoch is invalid")
    if type(value.fencing_token) is not int or not (
        0 < value.fencing_token <= _CURRENT_MAX_INT64
    ):
        raise _ledger_error("invalid-sequence", "receipt fencing token is invalid")
    _validate_projection_digest(value.argv_digest, "receipt.argv_digest")
    _gate._canonical_path(value.cwd, "receipt.cwd")
    names = _gate._validate_environment_names(
        value.environment_names, "receipt.environment_names"
    )
    if names != value.environment_names:
        raise _ledger_error("environment-mismatch", "receipt environment names differ")
    _gate._positive(value.timeout_ms, "receipt.timeout_ms", _gate.MAX_TIMEOUT_MS)
    _gate._positive(
        value.output_limit_bytes,
        "receipt.output_limit_bytes",
        _gate.MAX_OUTPUT_LIMIT_BYTES,
    )
    _validate_gate_result_schema(value.result_schema, "receipt.result_schema")
    _validate_gate_snapshot(value.before_snapshot, "receipt.before_snapshot")
    _validate_gate_snapshot(value.after_snapshot, "receipt.after_snapshot")
    try:
        _validate_snapshot_approval_projection(value.before_snapshot, approval)
    except (AttributeError, TypeError, ValueError) as exc:
        raise _ledger_error(
            "snapshot-invalid", "receipt before snapshot does not match approval"
        ) from exc
    try:
        _validate_snapshot_approval_projection(value.after_snapshot, approval)
    except (AttributeError, TypeError, ValueError) as exc:
        raise _ledger_error(
            "snapshot-invalid", "receipt after snapshot does not match approval"
        ) from exc
    if not _gate._same_snapshot(value.after_snapshot, value.before_snapshot):
        raise _ledger_error(
            "snapshot-drift", "receipt before and after snapshots differ"
        )
    if (
        value.cwd != approval["workspace"]
        or value.before_snapshot.workspace != value.cwd
        or value.after_snapshot.workspace != value.cwd
    ):
        raise _ledger_error(
            "workspace-mismatch", "receipt workspace identities do not agree"
        )
    if type(value.outcome) is not _gate.VerificationOutcome:
        raise _ledger_error("outcome-invalid", "receipt outcome is invalid")
    if value.exit_code is not None and (
        type(value.exit_code) is not int
        or not (-_CURRENT_MAX_INT64 - 1 <= value.exit_code <= _CURRENT_MAX_INT64)
    ):
        raise _ledger_error("outcome-invalid", "receipt exit code is invalid")
    for label, digest in (
        ("receipt.stdout_sha256", value.stdout_sha256),
        ("receipt.stderr_sha256", value.stderr_sha256),
    ):
        if digest is not None:
            _validate_projection_digest(digest, label)
    _gate._nonnegative(
        value.stdout_bytes, "receipt.stdout_bytes", _gate.MAX_OUTPUT_BYTES
    )
    _gate._nonnegative(
        value.stderr_bytes, "receipt.stderr_bytes", _gate.MAX_OUTPUT_BYTES
    )
    if value.stdout_bytes and value.stdout_sha256 is None:
        raise _ledger_error("receipt-contract", "stdout bytes require a digest")
    if value.stderr_bytes and value.stderr_sha256 is None:
        raise _ledger_error("receipt-contract", "stderr bytes require a digest")
    if type(value.cleanup) is not _gate.CleanupStatus:
        raise _ledger_error("cleanup-invalid", "receipt cleanup is invalid")
    if value.outcome is _gate.VerificationOutcome.UNKNOWN_EFFECT:
        raise _ledger_error("unknown-effect", "receipt cannot persist unknown effect")
    if value.outcome is _gate.VerificationOutcome.RUNNER_UNAVAILABLE:
        if value.executable_after is not None:
            raise _ledger_error(
                "runner-unavailable-contract",
                "runner unavailable receipt must not contain an after executable",
            )
        if value.cleanup is not _gate.CleanupStatus.NOT_STARTED:
            raise _ledger_error(
                "runner-unavailable-contract",
                "runner unavailable receipt must remain not started",
            )
        if (
            value.exit_code is not None
            or value.stdout_bytes
            or value.stderr_bytes
            or value.stdout_sha256 is not None
            or value.stderr_sha256 is not None
        ):
            raise _ledger_error(
                "receipt-contract",
                "runner unavailable receipt must prove no process or output",
            )
    else:
        if value.executable_after is None or not _gate._same_executable(
            value.executable_after, value.executable_before
        ):
            raise _ledger_error(
                "executable-identity-after-run-unavailable",
                "receipt executable identity after run is missing or differs",
            )
        if value.cleanup is not _gate.CleanupStatus.REAPED:
            raise _ledger_error(
                "cleanup-unknown", "known receipt outcome is not reaped"
            )
        if value.outcome is _gate.VerificationOutcome.PASSED:
            if value.exit_code != 0:
                raise _ledger_error(
                    "receipt-contract", "passed receipt requires exit code zero"
                )
            if value.stdout_bytes + value.stderr_bytes > value.output_limit_bytes:
                raise _ledger_error(
                    "output-limit", "passed receipt exceeds output limit"
                )
        elif value.outcome is _gate.VerificationOutcome.FAILED and (
            value.exit_code is None or value.exit_code == 0
        ):
            raise _ledger_error(
                "receipt-contract", "failed receipt requires non-zero exit code"
            )
    if value.receipt_digest != _receipt_gate_digest(value, approval):
        raise _ledger_error(
            "receipt-digest", "receipt digest does not bind all receipt fields"
        )


@dataclass(frozen=True, slots=True)
class VerificationRequestProjectionV1:
    version: int
    approval_ref: str
    verification_id: str
    request_digest: str
    approval: ApprovedReviewProjection
    profile_ref: str
    profile_identity: _gate.VerificationProfileIdentity
    profile_binding_digest: str
    executable: _gate.VerificationExecutableIdentity
    argv_digest: str
    cwd: str
    environment_names: tuple[str, ...]
    timeout_ms: int
    output_limit_bytes: int
    result_schema: _gate.ResultSchema
    before_snapshot: _gate.VerificationSnapshot

    def __post_init__(self) -> None:
        _validate_request_projection(self)


@dataclass(frozen=True, slots=True)
class VerificationReceiptProjectionV1:
    version: int
    receipt_ref: str
    receipt_digest: str
    verification_ref: str
    approval_ref: str
    request_digest: str
    approval: ApprovedReviewProjection
    routing_digest: str
    reservation_digest: str | None
    profile_ref: str
    profile_identity: _gate.VerificationProfileIdentity
    profile_binding_digest: str
    executable_before: _gate.VerificationExecutableIdentity
    executable_after: _gate.VerificationExecutableIdentity | None
    effect_nonce: str
    lease_epoch: int
    fencing_token: int
    argv_digest: str
    cwd: str
    environment_names: tuple[str, ...]
    timeout_ms: int
    output_limit_bytes: int
    result_schema: _gate.ResultSchema
    before_snapshot: _gate.VerificationSnapshot
    after_snapshot: _gate.VerificationSnapshot
    outcome: _gate.VerificationOutcome
    exit_code: int | None
    stdout_sha256: str | None
    stderr_sha256: str | None
    stdout_bytes: int
    stderr_bytes: int
    cleanup: _gate.CleanupStatus

    def __post_init__(self) -> None:
        _validate_receipt_projection(self)


def _snapshot_wire_mapping(value: ApprovalBindingSnapshotV1) -> dict[str, object]:
    try:
        value.__post_init__()
    except AttributeError:
        raise _ledger_error("snapshot-invalid", "snapshot is incomplete") from None
    return {
        "version": value.version,
        "review_ref": value.review_ref,
        "review_digest": value.review_digest,
        "completion_ref": value.completion_ref,
        "completion_digest": value.completion_digest,
        "approval_ref": value.approval_ref,
        "approval_digest": value.approval_digest,
        "approved_review": _approved_projection_wire(value.approved_review),
        "task_state_bytes": value.task_state_bytes.decode("utf-8"),
        "task_state_digest": value.task_state_digest,
        "binding_digest": value.binding_digest,
        "root_key": value.root_key,
        "run_id": value.run_id,
        "main_terminal_id": value.main_terminal_id,
        "consumer_generation": value.consumer_generation,
        "workflow_sequence": value.workflow_sequence,
        "workflow_checkpoint_digest": value.workflow_checkpoint_digest,
        "task_sequence": value.task_sequence,
        "effect_owner": value.effect_owner,
    }


def encode_approval_binding_snapshot(value: ApprovalBindingSnapshotV1) -> bytes:
    if type(value) is not ApprovalBindingSnapshotV1:
        raise _ledger_error("snapshot-invalid", "snapshot type is not exact")
    return _encode_payload(
        _snapshot_wire_mapping(value),
        _CURRENT_APPROVAL_BINDING_SNAPSHOT_FIELDS,
        "snapshot",
    )


def decode_approval_binding_snapshot(raw: bytes) -> ApprovalBindingSnapshotV1:
    mapping = _parse_payload(raw, _CURRENT_APPROVAL_BINDING_SNAPSHOT_FIELDS, "snapshot")
    task_state_wire = mapping["task_state_bytes"]
    if type(task_state_wire) is not str:
        raise _ledger_error("snapshot-state", "snapshot task state is not text")
    state_encoding_failed = False
    task_state_bytes = b""
    try:
        task_state_bytes = task_state_wire.encode("utf-8")
    except UnicodeEncodeError:
        state_encoding_failed = True
    if state_encoding_failed:
        raise _ledger_error("invalid-utf8", "snapshot state is not UTF-8") from None
    snapshot = ApprovalBindingSnapshotV1(
        version=cast(int, mapping["version"]),
        review_ref=cast(str, mapping["review_ref"]),
        review_digest=cast(str, mapping["review_digest"]),
        completion_ref=cast(str, mapping["completion_ref"]),
        completion_digest=cast(str, mapping["completion_digest"]),
        approval_ref=cast(str, mapping["approval_ref"]),
        approval_digest=cast(str, mapping["approval_digest"]),
        approved_review=_approved_projection_from_wire(mapping["approved_review"]),
        task_state_bytes=task_state_bytes,
        task_state_digest=StateDigest(cast(str, mapping["task_state_digest"])),
        binding_digest=cast(str, mapping["binding_digest"]),
        root_key=cast(str, mapping["root_key"]),
        run_id=cast(str, mapping["run_id"]),
        main_terminal_id=cast(str, mapping["main_terminal_id"]),
        consumer_generation=cast(int, mapping["consumer_generation"]),
        workflow_sequence=cast(int, mapping["workflow_sequence"]),
        workflow_checkpoint_digest=cast(str, mapping["workflow_checkpoint_digest"]),
        task_sequence=cast(int, mapping["task_sequence"]),
        effect_owner=cast(str, mapping["effect_owner"]),
    )
    snapshot.__post_init__()
    if encode_approval_binding_snapshot(snapshot) != raw:
        raise _ledger_error("noncanonical", "snapshot payload is not canonical")
    return snapshot


def _snapshot_payload_for_digest(
    value: ApprovalBindingSnapshotV1,
) -> dict[str, object]:
    return _snapshot_payload(
        version=value.version,
        review_ref=value.review_ref,
        review_digest=value.review_digest,
        completion_ref=value.completion_ref,
        completion_digest=value.completion_digest,
        approval_ref=value.approval_ref,
        approval_digest=value.approval_digest,
        approved_review=value.approved_review,
        task_state_bytes=value.task_state_bytes,
        task_state_digest=value.task_state_digest,
        root_key=value.root_key,
        run_id=value.run_id,
        main_terminal_id=value.main_terminal_id,
        consumer_generation=value.consumer_generation,
        workflow_sequence=value.workflow_sequence,
        workflow_checkpoint_digest=value.workflow_checkpoint_digest,
        task_sequence=value.task_sequence,
        effect_owner=value.effect_owner,
    )


def approval_binding_snapshot_digest(
    value: ApprovalBindingSnapshotV1 | bytes,
) -> str:
    if type(value) is ApprovalBindingSnapshotV1:
        snapshot = value
        try:
            snapshot.__post_init__()
        except AttributeError:
            raise _ledger_error("snapshot-invalid", "snapshot is incomplete") from None
    elif type(value) is bytes:
        snapshot = decode_approval_binding_snapshot(value)
    else:
        raise _ledger_error("invalid-digest-input", "snapshot digest input is invalid")
    return _snapshot_digest(_snapshot_payload_for_digest(snapshot))


def verification_request_projection_from_request(
    value: _gate.VerificationRequest,
) -> VerificationRequestProjectionV1:
    if type(value) is not _gate.VerificationRequest:
        raise _ledger_error("request-invalid", "request type is not exact")
    try:
        _gate._validate_request(value, verify_digest=True)
        return VerificationRequestProjectionV1(
            version=_CURRENT_VERIFICATION_REQUEST_CODEC_VERSION,
            approval_ref=value.approval_ref,
            verification_id=value.verification_id,
            request_digest=value.request_digest,
            approval=_projection_from_approved(value.approval),
            profile_ref=value.profile_ref,
            profile_identity=value.profile_identity,
            profile_binding_digest=value.profile_binding_digest,
            executable=value.executable,
            argv_digest=value.argv_digest,
            cwd=value.cwd,
            environment_names=tuple(value.environment_names),
            timeout_ms=value.timeout_ms,
            output_limit_bytes=value.output_limit_bytes,
            result_schema=value.result_schema,
            before_snapshot=value.before_snapshot,
        )
    except TaskVerificationLedgerError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise _ledger_error("request-invalid", "request is not admissible") from exc


def _request_projection_wire_mapping(
    value: VerificationRequestProjectionV1,
) -> dict[str, object]:
    _validate_request_projection(value)
    return {
        "version": value.version,
        "approval_ref": value.approval_ref,
        "verification_id": value.verification_id,
        "request_digest": value.request_digest,
        "approval": _approved_projection_wire(value.approval),
        "profile_ref": value.profile_ref,
        "profile_identity": _profile_identity_wire(value.profile_identity),
        "profile_binding_digest": value.profile_binding_digest,
        "executable": _executable_wire(value.executable),
        "argv_digest": value.argv_digest,
        "cwd": value.cwd,
        "environment_names": list(value.environment_names),
        "timeout_ms": value.timeout_ms,
        "output_limit_bytes": value.output_limit_bytes,
        "result_schema": _result_schema_wire(value.result_schema),
        "before_snapshot": _snapshot_wire_projection(value.before_snapshot),
    }


def encode_verification_request_projection(
    value: VerificationRequestProjectionV1,
) -> bytes:
    if type(value) is not VerificationRequestProjectionV1:
        raise _ledger_error(
            "request-projection-invalid", "projection type is not exact"
        )
    return _encode_payload(
        _request_projection_wire_mapping(value),
        _CURRENT_VERIFICATION_REQUEST_PROJECTION_FIELDS,
        "request projection",
    )


def decode_verification_request_projection(
    raw: bytes,
) -> VerificationRequestProjectionV1:
    mapping = _parse_payload(
        raw,
        _CURRENT_VERIFICATION_REQUEST_PROJECTION_FIELDS,
        "request projection",
    )
    names = mapping["environment_names"]
    if type(names) is not list:
        raise _ledger_error("environment-mismatch", "request environment names invalid")
    projection = VerificationRequestProjectionV1(
        version=cast(int, mapping["version"]),
        approval_ref=cast(str, mapping["approval_ref"]),
        verification_id=cast(str, mapping["verification_id"]),
        request_digest=cast(str, mapping["request_digest"]),
        approval=_approved_projection_from_wire(mapping["approval"]),
        profile_ref=cast(str, mapping["profile_ref"]),
        profile_identity=_profile_identity_from_wire(mapping["profile_identity"]),
        profile_binding_digest=cast(str, mapping["profile_binding_digest"]),
        executable=_executable_from_wire(mapping["executable"]),
        argv_digest=cast(str, mapping["argv_digest"]),
        cwd=cast(str, mapping["cwd"]),
        environment_names=tuple(cast(str, item) for item in names),
        timeout_ms=cast(int, mapping["timeout_ms"]),
        output_limit_bytes=cast(int, mapping["output_limit_bytes"]),
        result_schema=_result_schema_from_wire(mapping["result_schema"]),
        before_snapshot=_snapshot_projection_from_wire(mapping["before_snapshot"]),
    )
    if encode_verification_request_projection(projection) != raw:
        raise _ledger_error(
            "noncanonical", "request projection payload is not canonical"
        )
    return projection


def verification_request_projection_digest(
    value: VerificationRequestProjectionV1 | bytes,
) -> str:
    if type(value) is VerificationRequestProjectionV1:
        payload = encode_verification_request_projection(value)
    elif type(value) is bytes:
        decode_verification_request_projection(value)
        payload = value
    else:
        raise _ledger_error(
            "invalid-digest-input", "request projection digest input is invalid"
        )
    return (
        "sha256:"
        + hashlib.sha256(
            _CURRENT_VERIFICATION_REQUEST_PROJECTION_DIGEST_DOMAIN + payload
        ).hexdigest()
    )


def verification_receipt_projection_from_receipt(
    value: _gate.VerificationReceipt,
) -> VerificationReceiptProjectionV1:
    if type(value) is not _gate.VerificationReceipt:
        raise _ledger_error("receipt-invalid", "receipt type is not exact")
    try:
        _gate._validate_receipt(value, verify_digest=True)
        return VerificationReceiptProjectionV1(
            version=_CURRENT_VERIFICATION_RECEIPT_CODEC_VERSION,
            receipt_ref=value.receipt_ref,
            receipt_digest=value.receipt_digest,
            verification_ref=value.verification_ref,
            approval_ref=value.approval_ref,
            request_digest=value.request_digest,
            approval=_projection_from_approved(value.approval),
            routing_digest=value.routing_digest,
            reservation_digest=value.reservation_digest,
            profile_ref=value.profile_ref,
            profile_identity=value.profile_identity,
            profile_binding_digest=value.profile_binding_digest,
            executable_before=value.executable_before,
            executable_after=value.executable_after,
            effect_nonce=value.effect_nonce,
            lease_epoch=value.lease_epoch,
            fencing_token=value.fencing_token,
            argv_digest=value.argv_digest,
            cwd=value.cwd,
            environment_names=tuple(value.environment_names),
            timeout_ms=value.timeout_ms,
            output_limit_bytes=value.output_limit_bytes,
            result_schema=value.result_schema,
            before_snapshot=value.before_snapshot,
            after_snapshot=value.after_snapshot,
            outcome=value.outcome,
            exit_code=value.exit_code,
            stdout_sha256=value.stdout_sha256,
            stderr_sha256=value.stderr_sha256,
            stdout_bytes=value.stdout_bytes,
            stderr_bytes=value.stderr_bytes,
            cleanup=value.cleanup,
        )
    except TaskVerificationLedgerError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise _ledger_error("receipt-invalid", "receipt is not admissible") from exc


def _receipt_projection_wire_mapping(
    value: VerificationReceiptProjectionV1,
) -> dict[str, object]:
    _validate_receipt_projection(value)
    return {
        "version": value.version,
        "receipt_ref": value.receipt_ref,
        "receipt_digest": value.receipt_digest,
        "verification_ref": value.verification_ref,
        "approval_ref": value.approval_ref,
        "request_digest": value.request_digest,
        "approval": _approved_projection_wire(value.approval),
        "routing_digest": value.routing_digest,
        "reservation_digest": value.reservation_digest,
        "profile_ref": value.profile_ref,
        "profile_identity": _profile_identity_wire(value.profile_identity),
        "profile_binding_digest": value.profile_binding_digest,
        "executable_before": _executable_wire(value.executable_before),
        "executable_after": (
            None
            if value.executable_after is None
            else _executable_wire(value.executable_after)
        ),
        "effect_nonce": value.effect_nonce,
        "lease_epoch": value.lease_epoch,
        "fencing_token": value.fencing_token,
        "argv_digest": value.argv_digest,
        "cwd": value.cwd,
        "environment_names": list(value.environment_names),
        "timeout_ms": value.timeout_ms,
        "output_limit_bytes": value.output_limit_bytes,
        "result_schema": _result_schema_wire(value.result_schema),
        "before_snapshot": _snapshot_wire_projection(value.before_snapshot),
        "after_snapshot": _snapshot_wire_projection(value.after_snapshot),
        "outcome": value.outcome.value,
        "exit_code": value.exit_code,
        "stdout_sha256": value.stdout_sha256,
        "stderr_sha256": value.stderr_sha256,
        "stdout_bytes": value.stdout_bytes,
        "stderr_bytes": value.stderr_bytes,
        "cleanup": value.cleanup.value,
    }


def encode_verification_receipt_projection(
    value: VerificationReceiptProjectionV1,
) -> bytes:
    if type(value) is not VerificationReceiptProjectionV1:
        raise _ledger_error(
            "receipt-projection-invalid", "projection type is not exact"
        )
    return _encode_payload(
        _receipt_projection_wire_mapping(value),
        _CURRENT_VERIFICATION_RECEIPT_PROJECTION_FIELDS,
        "receipt projection",
    )


def decode_verification_receipt_projection(
    raw: bytes,
) -> VerificationReceiptProjectionV1:
    mapping = _parse_payload(
        raw,
        _CURRENT_VERIFICATION_RECEIPT_PROJECTION_FIELDS,
        "receipt projection",
    )
    names = mapping["environment_names"]
    if type(names) is not list:
        raise _ledger_error("environment-mismatch", "receipt environment names invalid")
    outcome: _gate.VerificationOutcome | None = None
    cleanup: _gate.CleanupStatus | None = None
    try:
        outcome = _gate.VerificationOutcome(cast(str, mapping["outcome"]))
        cleanup = _gate.CleanupStatus(cast(str, mapping["cleanup"]))
    except (TypeError, ValueError):
        pass
    if outcome is None or cleanup is None:
        raise _ledger_error("outcome-invalid", "receipt status is invalid") from None
    executable_after = mapping["executable_after"]
    projection = VerificationReceiptProjectionV1(
        version=cast(int, mapping["version"]),
        receipt_ref=cast(str, mapping["receipt_ref"]),
        receipt_digest=cast(str, mapping["receipt_digest"]),
        verification_ref=cast(str, mapping["verification_ref"]),
        approval_ref=cast(str, mapping["approval_ref"]),
        request_digest=cast(str, mapping["request_digest"]),
        approval=_approved_projection_from_wire(mapping["approval"]),
        routing_digest=cast(str, mapping["routing_digest"]),
        reservation_digest=cast(str | None, mapping["reservation_digest"]),
        profile_ref=cast(str, mapping["profile_ref"]),
        profile_identity=_profile_identity_from_wire(mapping["profile_identity"]),
        profile_binding_digest=cast(str, mapping["profile_binding_digest"]),
        executable_before=_executable_from_wire(mapping["executable_before"]),
        executable_after=(
            None
            if executable_after is None
            else _executable_from_wire(executable_after)
        ),
        effect_nonce=cast(str, mapping["effect_nonce"]),
        lease_epoch=cast(int, mapping["lease_epoch"]),
        fencing_token=cast(int, mapping["fencing_token"]),
        argv_digest=cast(str, mapping["argv_digest"]),
        cwd=cast(str, mapping["cwd"]),
        environment_names=tuple(cast(str, item) for item in names),
        timeout_ms=cast(int, mapping["timeout_ms"]),
        output_limit_bytes=cast(int, mapping["output_limit_bytes"]),
        result_schema=_result_schema_from_wire(mapping["result_schema"]),
        before_snapshot=_snapshot_projection_from_wire(mapping["before_snapshot"]),
        after_snapshot=_snapshot_projection_from_wire(mapping["after_snapshot"]),
        outcome=outcome,
        exit_code=cast(int | None, mapping["exit_code"]),
        stdout_sha256=cast(str | None, mapping["stdout_sha256"]),
        stderr_sha256=cast(str | None, mapping["stderr_sha256"]),
        stdout_bytes=cast(int, mapping["stdout_bytes"]),
        stderr_bytes=cast(int, mapping["stderr_bytes"]),
        cleanup=cleanup,
    )
    if encode_verification_receipt_projection(projection) != raw:
        raise _ledger_error(
            "noncanonical", "receipt projection payload is not canonical"
        )
    return projection


def verification_receipt_projection_digest(
    value: VerificationReceiptProjectionV1 | bytes,
) -> str:
    if type(value) is VerificationReceiptProjectionV1:
        payload = encode_verification_receipt_projection(value)
    elif type(value) is bytes:
        decode_verification_receipt_projection(value)
        payload = value
    else:
        raise _ledger_error(
            "invalid-digest-input", "receipt projection digest input is invalid"
        )
    return (
        "sha256:"
        + hashlib.sha256(
            _CURRENT_VERIFICATION_RECEIPT_PROJECTION_DIGEST_DOMAIN + payload
        ).hexdigest()
    )
