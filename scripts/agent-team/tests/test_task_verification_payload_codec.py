"""Contract tests for the Issue #80 verification payload codecs.

The #74 authority and #51 Gate fixtures are loaded only when a test needs
them.  The Issue #80 module and each codec entry point are resolved lazily so
an absent implementation is reported as an explicit RED assertion rather
than as a collection-time import error.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import unittest
from collections.abc import Callable, Mapping
from dataclasses import fields, replace
from enum import Enum
from types import ModuleType
from typing import Any, Final, cast

import agent_team.verification_gate as gate
from agent_team.task_policy import TaskLane

LEDGER_MODULE: Final = "agent_team.task_verification_ledger"
PAYLOAD_CODEC_VERSION_NAMES: Final = (
    "APPROVAL_BINDING_CODEC_VERSION",
    "VERIFICATION_REQUEST_CODEC_VERSION",
    "VERIFICATION_RECEIPT_CODEC_VERSION",
)

APPROVAL_SNAPSHOT_FIELDS: Final = (
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
APPROVED_REVIEW_FIELDS: Final = (
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
REQUEST_PROJECTION_FIELDS: Final = (
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
RECEIPT_PROJECTION_FIELDS: Final = (
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

ARGV_CANARY: Final = "request-argv-element-canary-78"
ENVIRONMENT_CANARY: Final = "environment-value-canary-78"
FORBIDDEN_WIRE_FIELDS: Final = (
    '"argv":',
    '"environment_values":',
    '"prompt":',
    '"task_body":',
    '"reviewer_body":',
    '"agent_body":',
    '"stdout":',
    '"stderr":',
    '"pid":',
    '"credential":',
    '"secret":',
    '"token":',
    '"_issuer":',
    '"bound":',
    '"registry":',
    '"connection":',
    '"lock":',
)
CODEC_ERRORS: Final = (TypeError, ValueError, UnicodeError, OverflowError)
APPROVAL_SNAPSHOT_DIGEST_DOMAIN: Final = b"agent-team/approval-binding-snapshot/v1\0"
MAX_VERIFICATION_PAYLOAD_BYTES: Final = 1_048_576
MAX_INT64: Final = 2**63 - 1
MALFORMED_BODY_CANARY: Final = "malformed-payload-body-canary-78"


class MissingLedgerAPI(AssertionError):
    """Make an absent Issue #80 codec API an explicit RED assertion."""


def _ledger_module() -> ModuleType:
    try:
        return importlib.import_module(LEDGER_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name == LEDGER_MODULE:
            raise MissingLedgerAPI(f"missing API module: {LEDGER_MODULE}") from exc
        raise


def _api(name: str) -> object:
    module = _ledger_module()
    try:
        return getattr(module, name)
    except AttributeError as exc:
        raise MissingLedgerAPI(f"missing API: {LEDGER_MODULE}.{name}") from exc


def _call(name: str, *args: object) -> object:
    value = _api(name)
    if not callable(value):
        raise MissingLedgerAPI(f"API is not callable: {LEDGER_MODULE}.{name}")
    return cast(Callable[..., object], value)(*args)


def _canonical(mapping: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            mapping,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _redigest_snapshot_mapping(mapping: Mapping[str, object]) -> dict[str, object]:
    """Return a self-redigested snapshot mapping using the independent oracle."""

    payload = dict(mapping)
    payload.pop("binding_digest", None)
    digest = (
        "sha256:"
        + hashlib.sha256(
            APPROVAL_SNAPSHOT_DIGEST_DOMAIN + _canonical(payload)
        ).hexdigest()
    )
    result = dict(mapping)
    result["binding_digest"] = digest
    return result


def _snapshot_payload_with_changes(
    snapshot: object,
    *,
    top_level_changes: Mapping[str, object] | None = None,
    approved_changes: Mapping[str, object] | None = None,
) -> bytes:
    mapping = dict(_snapshot_wire_mapping(snapshot))
    if top_level_changes is not None:
        mapping.update(top_level_changes)
    if approved_changes is not None:
        approved = mapping["approved_review"]
        if type(approved) is not dict:
            raise AssertionError("snapshot approved_review oracle is not a mapping")
        updated_approved = dict(cast(Mapping[str, object], approved))
        updated_approved.update(approved_changes)
        mapping["approved_review"] = updated_approved
    return _canonical(_redigest_snapshot_mapping(mapping))


def _snapshot_payload_with_state_changes(
    snapshot: object, changes: Mapping[str, object]
) -> bytes:
    mapping = dict(_snapshot_wire_mapping(snapshot))
    state_wire = mapping["task_state_bytes"]
    if type(state_wire) is not str:
        raise AssertionError("snapshot task state oracle is not text")
    state_mapping = json.loads(state_wire)
    if type(state_mapping) is not dict:
        raise AssertionError("snapshot task state oracle is not a mapping")
    state_mapping.update(changes)
    state_bytes = _canonical(state_mapping)
    mapping["task_state_bytes"] = state_bytes.decode("utf-8")
    mapping["task_state_digest"] = _call("task_state_digest", state_bytes)
    return _canonical(_redigest_snapshot_mapping(mapping))


def _snapshot_approval_digest_with_changes(
    snapshot: object, changes: Mapping[str, object]
) -> str:
    approval = _approved_projection_mapping(cast(Any, snapshot).approved_review)
    approval.update(changes)
    approval["routing_lane"] = TaskLane(str(approval["routing_lane"]))
    forged = gate._make_approved(**approval)
    return str(forged.authority_digest)


def _receipt_payload_with_changes(
    receipt: gate.VerificationReceipt,
    *,
    top_level_changes: Mapping[str, object] | None = None,
    approval_changes: Mapping[str, object] | None = None,
    before_snapshot_changes: Mapping[str, object] | None = None,
    after_snapshot_changes: Mapping[str, object] | None = None,
) -> bytes:
    mapping = dict(_receipt_wire_mapping(receipt))
    if top_level_changes is not None:
        mapping.update(top_level_changes)
    if approval_changes is not None:
        approval = mapping["approval"]
        if type(approval) is not dict:
            raise AssertionError("receipt approval oracle is not a mapping")
        updated_approval = dict(cast(Mapping[str, object], approval))
        updated_approval.update(approval_changes)
        mapping["approval"] = updated_approval
    if before_snapshot_changes is not None:
        before_snapshot = mapping["before_snapshot"]
        if type(before_snapshot) is not dict:
            raise AssertionError("receipt before snapshot oracle is not a dict")
        updated_before = dict(cast(Mapping[str, object], before_snapshot))
        updated_before.update(before_snapshot_changes)
        mapping["before_snapshot"] = updated_before
    if after_snapshot_changes is not None:
        after_snapshot = mapping["after_snapshot"]
        if type(after_snapshot) is not dict:
            raise AssertionError("receipt after snapshot oracle is not a mapping")
        updated_after = dict(cast(Mapping[str, object], after_snapshot))
        updated_after.update(after_snapshot_changes)
        mapping["after_snapshot"] = updated_after
    return _canonical(mapping)


def _approval_digest_with_changes(
    receipt: gate.VerificationReceipt, changes: Mapping[str, object]
) -> str:
    approval = dict(_approved_mapping(receipt.approval))
    approval.update(changes)
    approval["routing_lane"] = TaskLane(str(approval["routing_lane"]))
    forged = gate._make_approved(**approval)
    return str(forged.authority_digest)


def _sized_unknown_payload(size: int) -> bytes:
    prefix = b'{"unknown":"'
    suffix = b'"}\n'
    filler_size = size - len(prefix) - len(suffix)
    if filler_size < 0:
        raise AssertionError("requested payload size is too small")
    return prefix + b"x" * filler_size + suffix


def _json_value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if type(value) is bytes:
        return value.decode("utf-8")
    if type(value) is tuple:
        return [_json_value(item) for item in value]
    if type(value) is list:
        return [_json_value(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        result: dict[str, object] = {}
        for item in fields(cast(Any, value)):
            if not item.name.startswith("_"):
                result[item.name] = _json_value(getattr(value, item.name))
        return result
    return value


def _approved_mapping(approved: gate.ApprovedReview) -> dict[str, object]:
    result: dict[str, object] = {}
    for name in APPROVED_REVIEW_FIELDS:
        result[name] = _json_value(getattr(approved, name))
    return result


def _snapshot_projection_mapping(
    snapshot: gate.VerificationSnapshot,
) -> dict[str, object]:
    return {
        name: _json_value(getattr(snapshot, name))
        for name in (
            "workspace",
            "canonical_path",
            "device",
            "inode",
            "claim_ref",
            "target_head",
            "allowed_tree_digest",
        )
    }


def _approved_projection_mapping(value: object) -> dict[str, object]:
    if type(value) is not tuple:
        raise AssertionError("snapshot approved_review is not a projection tuple")
    result: dict[str, object] = {}
    for item in value:
        if type(item) is not tuple or len(item) != 2:
            raise AssertionError("snapshot approved_review projection is malformed")
        result[str(item[0])] = _json_value(item[1])
    return result


def _snapshot_wire_mapping(snapshot: object) -> dict[str, object]:
    value = cast(Any, snapshot)
    return {
        "version": value.version,
        "review_ref": value.review_ref,
        "review_digest": value.review_digest,
        "completion_ref": value.completion_ref,
        "completion_digest": value.completion_digest,
        "approval_ref": value.approval_ref,
        "approval_digest": value.approval_digest,
        "approved_review": _approved_projection_mapping(value.approved_review),
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


def _request_wire_mapping(request: gate.VerificationRequest) -> dict[str, object]:
    return {
        "version": 1,
        "approval_ref": request.approval_ref,
        "verification_id": request.verification_id,
        "request_digest": request.request_digest,
        "approval": _approved_mapping(request.approval),
        "profile_ref": request.profile_ref,
        "profile_identity": _json_value(request.profile_identity),
        "profile_binding_digest": request.profile_binding_digest,
        "executable": _json_value(request.executable),
        "argv_digest": request.argv_digest,
        "cwd": request.cwd,
        "environment_names": _json_value(request.environment_names),
        "timeout_ms": request.timeout_ms,
        "output_limit_bytes": request.output_limit_bytes,
        "result_schema": _json_value(request.result_schema),
        "before_snapshot": _snapshot_projection_mapping(request.before_snapshot),
    }


def _receipt_wire_mapping(receipt: gate.VerificationReceipt) -> dict[str, object]:
    return {
        "version": 1,
        "receipt_ref": receipt.receipt_ref,
        "receipt_digest": receipt.receipt_digest,
        "verification_ref": receipt.verification_ref,
        "approval_ref": receipt.approval_ref,
        "request_digest": receipt.request_digest,
        "approval": _approved_mapping(receipt.approval),
        "routing_digest": receipt.routing_digest,
        "reservation_digest": receipt.reservation_digest,
        "profile_ref": receipt.profile_ref,
        "profile_identity": _json_value(receipt.profile_identity),
        "profile_binding_digest": receipt.profile_binding_digest,
        "executable_before": _json_value(receipt.executable_before),
        "executable_after": _json_value(receipt.executable_after),
        "effect_nonce": receipt.effect_nonce,
        "lease_epoch": receipt.lease_epoch,
        "fencing_token": receipt.fencing_token,
        "argv_digest": receipt.argv_digest,
        "cwd": receipt.cwd,
        "environment_names": _json_value(receipt.environment_names),
        "timeout_ms": receipt.timeout_ms,
        "output_limit_bytes": receipt.output_limit_bytes,
        "result_schema": _json_value(receipt.result_schema),
        "before_snapshot": _snapshot_projection_mapping(receipt.before_snapshot),
        "after_snapshot": _snapshot_projection_mapping(receipt.after_snapshot),
        "outcome": _json_value(receipt.outcome),
        "exit_code": receipt.exit_code,
        "stdout_sha256": receipt.stdout_sha256,
        "stderr_sha256": receipt.stderr_sha256,
        "stdout_bytes": receipt.stdout_bytes,
        "stderr_bytes": receipt.stderr_bytes,
        "cleanup": _json_value(receipt.cleanup),
    }


def _snapshot_fixture() -> object:
    """Build a canonical projection from the tracked #74 owner fixture."""

    fixture = importlib.import_module("test_policy_verification_handoff_authority")
    handoff, _owner_store = fixture._new_handoff(unittest.TestCase())
    task = fixture._path_task()
    update, policy = fixture._review_path(task=task)
    review_ref = handoff.save_authority(update, policy)
    completion_ref = handoff.issue_completion_admission(
        **fixture._route_inputs(
            task,
            port=fixture.RecordingReservationPort(),
        )
    )
    approval_ref = handoff.compose(review_ref, completion_ref)
    bound = handoff.resolve(approval_ref)
    approved = bound.approved
    state = update.next_state.task_state
    state_bytes = cast(bytes, _call("encode_task_state", state))
    state_digest = cast(str, _call("task_state_digest", state_bytes))
    mapping: dict[str, object] = {
        "version": 1,
        "review_ref": review_ref.reference,
        "review_digest": review_ref.digest,
        "completion_ref": completion_ref.reference,
        "completion_digest": completion_ref.digest,
        "approval_ref": approval_ref,
        "approval_digest": approved.authority_digest,
        "approved_review": _approved_mapping(approved),
        "task_state_bytes": state_bytes.decode("utf-8"),
        "task_state_digest": state_digest,
        "binding_digest": "",
        "root_key": "root-key-78",
        "run_id": approved.run_id,
        "main_terminal_id": "main-terminal-78",
        "consumer_generation": 0,
        "workflow_sequence": 0,
        "workflow_checkpoint_digest": "sha256:" + "c" * 64,
        "task_sequence": state.sequence,
        "effect_owner": "effect-owner-78",
    }
    mapping["binding_digest"] = (
        "sha256:"
        + hashlib.sha256(
            APPROVAL_SNAPSHOT_DIGEST_DOMAIN
            + _canonical(
                {
                    key: value
                    for key, value in mapping.items()
                    if key != "binding_digest"
                }
            )
        ).hexdigest()
    )
    return _decode("decode_approval_binding_snapshot", _canonical(mapping))


def _request_and_receipt() -> tuple[gate.VerificationRequest, gate.VerificationReceipt]:
    fixture = importlib.import_module("tests.test_verification_gate")
    base_profile = fixture.profile()
    argv_template = (*base_profile.argv_template, ARGV_CANARY)
    profile = replace(
        base_profile,
        argv_template=argv_template,
        argv_template_digest=gate._argv_digest(argv_template),
        environment_values=(ENVIRONMENT_CANARY, "C"),
    )
    verification_gate, state, _, _ = fixture.make_gate(
        resolver=fixture.Resolver(profile)
    )
    handle = verification_gate.start(fixture.APPROVAL_REF)
    if state.record is None:
        raise AssertionError("#51 fixture did not persist a VerificationRequest")
    request = state.record.request
    if type(request) is not gate.VerificationRequest:
        raise AssertionError("#51 fixture request is not exact VerificationRequest")
    verification_gate.resume(handle)
    receipt = state.receipt
    if type(receipt) is not gate.VerificationReceipt:
        raise AssertionError("#51 fixture did not persist an exact receipt")
    return request, receipt


def _decode(name: str, raw: object) -> object:
    return _call(name, raw)


def _assert_rejected(test: unittest.TestCase, name: str, raw: object) -> None:
    try:
        _decode(name, raw)
    except MissingLedgerAPI:
        raise
    except AssertionError:
        raise
    except CODEC_ERRORS:
        return
    raise AssertionError(f"{name} accepted malformed payload")


def _mutated_payloads(mapping: Mapping[str, object]) -> tuple[tuple[str, bytes], ...]:
    canonical = _canonical(mapping)
    unknown = dict(mapping)
    unknown["unknown"] = "unsupported"
    future = dict(mapping)
    future["future_field"] = "v2"
    missing = dict(mapping)
    del missing["version"]
    wrong_version = dict(mapping)
    wrong_version["version"] = 2
    duplicate = canonical[:-2] + b',"version":1}\n'
    return (
        ("duplicate field", duplicate),
        ("unknown field", _canonical(unknown)),
        ("future field", _canonical(future)),
        ("missing field", _canonical(missing)),
        ("wrong version", _canonical(wrong_version)),
    )


class TaskVerificationPayloadCodecTests(unittest.TestCase):
    def test_public_projection_version_rebinding_cannot_change_current_codec(
        self,
    ) -> None:
        module = _ledger_module()
        snapshot = _snapshot_fixture()
        request, receipt = _request_and_receipt()
        names = (
            "APPROVAL_BINDING_CODEC_VERSION",
            "APPROVAL_BINDING_SNAPSHOT_VERSION",
            "VERIFICATION_REQUEST_CODEC_VERSION",
            "VERIFICATION_RECEIPT_CODEC_VERSION",
            "VERIFICATION_RECORD_VERSION",
        )
        originals = {name: getattr(module, name) for name in names}
        try:
            for name in names:
                setattr(module, name, 2)

            snapshot_bytes = cast(
                bytes, _call("encode_approval_binding_snapshot", snapshot)
            )
            self.assertEqual(1, json.loads(snapshot_bytes.decode("utf-8"))["version"])
            self.assertEqual(
                snapshot,
                _call("decode_approval_binding_snapshot", snapshot_bytes),
            )

            request_projection = _call(
                "verification_request_projection_from_request", request
            )
            self.assertEqual(
                1,
                json.loads(
                    cast(
                        bytes,
                        _call(
                            "encode_verification_request_projection",
                            request_projection,
                        ),
                    ).decode("utf-8")
                )["version"],
            )
            receipt_projection = _call(
                "verification_receipt_projection_from_receipt", receipt
            )
            self.assertEqual(
                1,
                json.loads(
                    cast(
                        bytes,
                        _call(
                            "encode_verification_receipt_projection",
                            receipt_projection,
                        ),
                    ).decode("utf-8")
                )["version"],
            )

            _assert_rejected(
                self,
                "decode_approval_binding_snapshot",
                _snapshot_payload_with_changes(
                    snapshot, top_level_changes={"version": 2}
                ),
            )
            request_wire = _request_wire_mapping(request)
            request_wire["version"] = 2
            _assert_rejected(
                self,
                "decode_verification_request_projection",
                _canonical(request_wire),
            )
            receipt_wire = _receipt_wire_mapping(receipt)
            receipt_wire["version"] = 2
            _assert_rejected(
                self,
                "decode_verification_receipt_projection",
                _canonical(receipt_wire),
            )
        finally:
            for name, value in originals.items():
                setattr(module, name, value)

    def test_public_wire_alias_rebinding_cannot_change_projection_contract(
        self,
    ) -> None:
        module = _ledger_module()
        snapshot = _snapshot_fixture()
        request, receipt = _request_and_receipt()
        request_projection = _call(
            "verification_request_projection_from_request", request
        )
        receipt_projection = _call(
            "verification_receipt_projection_from_receipt", receipt
        )
        baseline_snapshot = cast(
            bytes, _call("encode_approval_binding_snapshot", snapshot)
        )
        baseline_request = cast(
            bytes, _call("encode_verification_request_projection", request_projection)
        )
        baseline_receipt = cast(
            bytes, _call("encode_verification_receipt_projection", receipt_projection)
        )
        baseline_digests = (
            _call("approval_binding_snapshot_digest", snapshot),
            _call("verification_request_projection_digest", request_projection),
            _call("verification_receipt_projection_digest", receipt_projection),
        )
        over_payload = _sized_unknown_payload(MAX_VERIFICATION_PAYLOAD_BYTES + 1)
        overflow_payload = _receipt_payload_with_changes(
            receipt,
            top_level_changes={"lease_epoch": MAX_INT64 + 1},
        )
        mutations: tuple[tuple[str, object], ...] = (
            (
                "APPROVAL_BINDING_SNAPSHOT_FIELDS",
                tuple(
                    reversed(
                        cast(
                            tuple[str, ...],
                            module.__dict__["APPROVAL_BINDING_SNAPSHOT_FIELDS"],
                        )
                    )
                ),
            ),
            (
                "VERIFICATION_REQUEST_PROJECTION_FIELDS",
                tuple(
                    reversed(
                        cast(
                            tuple[str, ...],
                            module.__dict__["VERIFICATION_REQUEST_PROJECTION_FIELDS"],
                        )
                    )
                ),
            ),
            (
                "VERIFICATION_RECEIPT_PROJECTION_FIELDS",
                tuple(
                    reversed(
                        cast(
                            tuple[str, ...],
                            module.__dict__["VERIFICATION_RECEIPT_PROJECTION_FIELDS"],
                        )
                    )
                ),
            ),
            ("APPROVAL_BINDING_SNAPSHOT_DIGEST_DOMAIN", b"alternate/snapshot"),
            ("VERIFICATION_REQUEST_PROJECTION_DIGEST_DOMAIN", b"alternate/request"),
            ("VERIFICATION_RECEIPT_PROJECTION_DIGEST_DOMAIN", b"alternate/receipt"),
            ("MAX_VERIFICATION_PAYLOAD_BYTES", 2 * 1_048_576),
            ("MAX_INT64", 2**64),
        )
        originals = {name: module.__dict__[name] for name, _mutation in mutations}
        try:
            for name, mutation in mutations:
                with self.subTest(alias=name):
                    module.__dict__[name] = mutation
                    self.assertEqual(
                        baseline_snapshot,
                        _call("encode_approval_binding_snapshot", snapshot),
                    )
                    self.assertEqual(
                        baseline_request,
                        _call(
                            "encode_verification_request_projection",
                            request_projection,
                        ),
                    )
                    self.assertEqual(
                        baseline_receipt,
                        _call(
                            "encode_verification_receipt_projection",
                            receipt_projection,
                        ),
                    )
                    self.assertEqual(
                        baseline_digests,
                        (
                            _call("approval_binding_snapshot_digest", snapshot),
                            _call(
                                "verification_request_projection_digest",
                                request_projection,
                            ),
                            _call(
                                "verification_receipt_projection_digest",
                                receipt_projection,
                            ),
                        ),
                    )
                    self.assertEqual(
                        snapshot,
                        _call("decode_approval_binding_snapshot", baseline_snapshot),
                    )
                    self.assertEqual(
                        request_projection,
                        _call(
                            "decode_verification_request_projection", baseline_request
                        ),
                    )
                    self.assertEqual(
                        receipt_projection,
                        _call(
                            "decode_verification_receipt_projection", baseline_receipt
                        ),
                    )
                    try:
                        _call("decode_approval_binding_snapshot", over_payload)
                    except CODEC_ERRORS as error:
                        self.assertEqual(getattr(error, "code", None), "payload-size")
                    else:
                        self.fail("oversized snapshot envelope was accepted")
                    try:
                        _call(
                            "decode_verification_receipt_projection",
                            overflow_payload,
                        )
                    except CODEC_ERRORS:
                        pass
                    else:
                        self.fail("out-of-range receipt integer was accepted")
                    module.__dict__[name] = originals[name]
        finally:
            for name, value in originals.items():
                module.__dict__[name] = value

    def test_foundation_codec_does_not_expose_authority_or_adapter_surface(
        self,
    ) -> None:
        module = _ledger_module()
        for name in (
            "VerificationContextSeed",
            "capture_approval_binding",
            "StoreVerificationAdapter",
            "make_store_verification_adapter",
            "_snapshot_from_store",
            "_validate_store_adapter_inputs",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(module, name))

    def test_payload_codec_versions_are_exactly_one(self) -> None:
        for name in PAYLOAD_CODEC_VERSION_NAMES:
            with self.subTest(name=name):
                value = _api(name)
                self.assertIs(type(value), int)
                self.assertEqual(value, 1)

    def test_record_version_is_only_a_foundation_discriminator(self) -> None:
        self.assertEqual(1, _api("VERIFICATION_RECORD_VERSION"))
        for name in (
            "VerificationRecordProjectionV1",
            "encode_verification_record",
            "decode_verification_record",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(_ledger_module(), name))

    def test_approval_snapshot_codec_round_trips_fixed_canonical_body(self) -> None:
        snapshot = _snapshot_fixture()
        mapping = _snapshot_wire_mapping(snapshot)
        encoded = _call("encode_approval_binding_snapshot", snapshot)
        if type(encoded) is not bytes:
            raise AssertionError("snapshot encoder must return exact bytes")
        self.assertEqual(encoded, _canonical(mapping))
        self.assertEqual(
            tuple(json.loads(encoded.decode("utf-8"))), APPROVAL_SNAPSHOT_FIELDS
        )
        decoded = _decode("decode_approval_binding_snapshot", encoded)
        self.assertEqual(type(decoded).__name__, "ApprovalBindingSnapshotV1")
        self.assertEqual(decoded, snapshot)
        self.assertEqual(
            _call("encode_approval_binding_snapshot", decoded),
            encoded,
        )
        digest = _call("approval_binding_snapshot_digest", snapshot)
        self.assertEqual(digest, cast(Any, snapshot).binding_digest)
        self.assertEqual(_call("approval_binding_snapshot_digest", encoded), digest)
        self.assertRegex(cast(str, digest), r"^sha256:[0-9a-f]{64}$")
        self.assertNotEqual(digest, hashlib.sha256(encoded).hexdigest())

    def test_request_projection_is_created_from_exact_request_without_private_values(
        self,
    ) -> None:
        request, _receipt = _request_and_receipt()
        projection = _call(
            "verification_request_projection_from_request",
            request,
        )
        self.assertEqual(type(projection).__name__, "VerificationRequestProjectionV1")
        encoded = _call("encode_verification_request_projection", projection)
        if type(encoded) is not bytes:
            raise AssertionError("request projection encoder must return exact bytes")
        self.assertEqual(encoded, _canonical(_request_wire_mapping(request)))
        wire = json.loads(encoded.decode("utf-8"))
        self.assertEqual(tuple(wire), REQUEST_PROJECTION_FIELDS)
        self.assertEqual(wire["argv_digest"], request.argv_digest)
        self.assertEqual(wire["environment_names"], list(request.environment_names))
        self.assertEqual(wire["profile_ref"], request.profile_ref)
        self.assertEqual(wire["request_digest"], request.request_digest)
        self.assertNotIn(ARGV_CANARY.encode("utf-8"), encoded)
        self.assertNotIn(ENVIRONMENT_CANARY.encode("utf-8"), encoded)
        for field in FORBIDDEN_WIRE_FIELDS:
            with self.subTest(field=field):
                self.assertNotIn(field.encode("utf-8"), encoded)
        self.assertNotIn(ARGV_CANARY, repr(projection))
        self.assertNotIn(ENVIRONMENT_CANARY, repr(projection))
        decoded = _decode("decode_verification_request_projection", encoded)
        self.assertEqual(type(decoded).__name__, "VerificationRequestProjectionV1")
        self.assertEqual(decoded, projection)
        self.assertIsNot(type(decoded), gate.VerificationRequest)
        self.assertEqual(
            _call("encode_verification_request_projection", decoded), encoded
        )

    def test_receipt_projection_round_trips_every_durable_binding_without_private_values(
        self,
    ) -> None:
        _request, receipt = _request_and_receipt()
        projection = _call(
            "verification_receipt_projection_from_receipt",
            receipt,
        )
        self.assertEqual(type(projection).__name__, "VerificationReceiptProjectionV1")
        encoded = _call("encode_verification_receipt_projection", projection)
        if type(encoded) is not bytes:
            raise AssertionError("receipt projection encoder must return exact bytes")
        self.assertEqual(encoded, _canonical(_receipt_wire_mapping(receipt)))
        wire = json.loads(encoded.decode("utf-8"))
        self.assertEqual(tuple(wire), RECEIPT_PROJECTION_FIELDS)
        self.assertEqual(wire["receipt_ref"], receipt.receipt_ref)
        self.assertEqual(wire["request_digest"], receipt.request_digest)
        self.assertEqual(wire["argv_digest"], receipt.argv_digest)
        self.assertEqual(wire["environment_names"], list(receipt.environment_names))
        self.assertEqual(
            wire["profile_identity"], _json_value(receipt.profile_identity)
        )
        self.assertEqual(
            wire["before_snapshot"],
            _snapshot_projection_mapping(receipt.before_snapshot),
        )
        self.assertEqual(
            wire["after_snapshot"], _snapshot_projection_mapping(receipt.after_snapshot)
        )
        self.assertNotIn(ARGV_CANARY.encode("utf-8"), encoded)
        self.assertNotIn(ENVIRONMENT_CANARY.encode("utf-8"), encoded)
        for field in FORBIDDEN_WIRE_FIELDS:
            with self.subTest(field=field):
                self.assertNotIn(field.encode("utf-8"), encoded)
        decoded = _decode("decode_verification_receipt_projection", encoded)
        self.assertEqual(type(decoded).__name__, "VerificationReceiptProjectionV1")
        self.assertEqual(decoded, projection)
        self.assertIsNot(type(decoded), gate.VerificationReceipt)
        self.assertEqual(
            _call("encode_verification_receipt_projection", decoded), encoded
        )

    def test_each_projection_digest_is_dedicated_and_stable_for_bytes_or_value(
        self,
    ) -> None:
        snapshot = _snapshot_fixture()
        request, receipt = _request_and_receipt()
        values = (
            (
                "approval",
                "encode_approval_binding_snapshot",
                "approval_binding_snapshot_digest",
                snapshot,
                cast(Any, snapshot).binding_digest,
                True,
            ),
            (
                "request",
                "encode_verification_request_projection",
                "verification_request_projection_digest",
                _call("verification_request_projection_from_request", request),
                request.request_digest,
                False,
            ),
            (
                "receipt",
                "encode_verification_receipt_projection",
                "verification_receipt_projection_digest",
                _call("verification_receipt_projection_from_receipt", receipt),
                receipt.receipt_digest,
                False,
            ),
        )
        for label, encoder, digester, value, source_digest, preserves_digest in values:
            with self.subTest(codec=label):
                encoded = cast(bytes, _call(encoder, value))
                digest = _call(digester, value)
                self.assertEqual(_call(digester, encoded), digest)
                self.assertRegex(cast(str, digest), r"^sha256:[0-9a-f]{64}$")
                self.assertNotEqual(digest, hashlib.sha256(encoded).hexdigest())
                if preserves_digest:
                    self.assertEqual(digest, source_digest)
                else:
                    self.assertNotEqual(digest, source_digest)

    def test_all_three_codecs_reject_duplicate_unknown_missing_future_and_version_fields(
        self,
    ) -> None:
        snapshot = _snapshot_fixture()
        request, receipt = _request_and_receipt()
        cases = (
            (
                "approval",
                "encode_approval_binding_snapshot",
                "decode_approval_binding_snapshot",
                _snapshot_wire_mapping(snapshot),
                snapshot,
            ),
            (
                "request",
                "encode_verification_request_projection",
                "decode_verification_request_projection",
                _request_wire_mapping(request),
                _call("verification_request_projection_from_request", request),
            ),
            (
                "receipt",
                "encode_verification_receipt_projection",
                "decode_verification_receipt_projection",
                _receipt_wire_mapping(receipt),
                _call("verification_receipt_projection_from_receipt", receipt),
            ),
        )
        for label, encoder, decoder, mapping, value in cases:
            del encoder, value
            for mutation, payload in _mutated_payloads(mapping):
                with self.subTest(codec=label, mutation=mutation):
                    _assert_rejected(self, decoder, payload)

    def test_all_three_decoders_require_exact_canonical_bytes(self) -> None:
        snapshot = _snapshot_fixture()
        request, receipt = _request_and_receipt()
        cases = (
            (
                "approval",
                "decode_approval_binding_snapshot",
                _canonical(_snapshot_wire_mapping(snapshot)),
                _snapshot_wire_mapping(snapshot),
            ),
            (
                "request",
                "decode_verification_request_projection",
                _canonical(_request_wire_mapping(request)),
                _request_wire_mapping(request),
            ),
            (
                "receipt",
                "decode_verification_receipt_projection",
                _canonical(_receipt_wire_mapping(receipt)),
                _receipt_wire_mapping(receipt),
            ),
        )
        for label, decoder, canonical, mapping in cases:
            malformed = (
                (
                    "pretty",
                    json.dumps(mapping, ensure_ascii=False, indent=2).encode() + b"\n",
                ),
                ("reordered", _canonical(dict(reversed(tuple(mapping.items()))))),
                ("bom", b"\xef\xbb\xbf" + canonical),
                ("trailing space", canonical + b" "),
                ("two newlines", canonical + b"\n"),
                ("invalid utf8", canonical.replace(b'"version":1', b"\xff", 1)),
                ("malformed json", b"{\n"),
            )
            for mutation, payload in malformed:
                with self.subTest(codec=label, mutation=mutation):
                    _assert_rejected(self, decoder, payload)
            for value in (
                canonical.decode("utf-8"),
                bytearray(canonical),
                memoryview(canonical),
            ):
                with self.subTest(codec=label, input_type=type(value).__name__):
                    _assert_rejected(self, decoder, value)

    def test_receipt_decoder_rejects_after_snapshot_and_approval_tampering(
        self,
    ) -> None:
        _request, receipt = _request_and_receipt()
        cases = (
            (
                "after claim",
                _receipt_payload_with_changes(
                    receipt,
                    after_snapshot_changes={"claim_ref": "foreign-claim-78"},
                ),
            ),
            (
                "after target",
                _receipt_payload_with_changes(
                    receipt,
                    after_snapshot_changes={"target_head": "c" * 40},
                ),
            ),
            (
                "approval task",
                _receipt_payload_with_changes(
                    receipt,
                    approval_changes={"task_id": "foreign-task-78"},
                ),
            ),
        )
        for label, payload in cases:
            with self.subTest(case=label):
                _assert_rejected(
                    self, "decode_verification_receipt_projection", payload
                )

    def test_receipt_decoder_rejects_workspace_identity_splits(self) -> None:
        _request, receipt = _request_and_receipt()
        cases = (
            (
                "cwd",
                _receipt_payload_with_changes(
                    receipt,
                    top_level_changes={"cwd": "/foreign/cwd-78"},
                ),
            ),
            (
                "approval workspace",
                _receipt_payload_with_changes(
                    receipt,
                    approval_changes={
                        "workspace": "/foreign/approval-workspace-78",
                        "authority_digest": _approval_digest_with_changes(
                            receipt,
                            {"workspace": "/foreign/approval-workspace-78"},
                        ),
                    },
                ),
            ),
            (
                "before snapshot workspace",
                _receipt_payload_with_changes(
                    receipt,
                    before_snapshot_changes={
                        "workspace": "/foreign/before-workspace-78",
                        "canonical_path": "/foreign/before-workspace-78",
                    },
                ),
            ),
            (
                "after snapshot workspace",
                _receipt_payload_with_changes(
                    receipt,
                    after_snapshot_changes={
                        "workspace": "/foreign/after-workspace-78",
                        "canonical_path": "/foreign/after-workspace-78",
                    },
                ),
            ),
        )
        for label, payload in cases:
            with self.subTest(case=label):
                _assert_rejected(
                    self, "decode_verification_receipt_projection", payload
                )

    def test_receipt_rejects_nested_approval_binding_tamper_with_stale_or_recomputed_digest(
        self,
    ) -> None:
        _request, receipt = _request_and_receipt()
        stale_cases = (
            {"routing_digest": "0" * 64},
            {"reservation_digest": "0" * 64},
            {"authority_digest": "0" * 64},
        )
        for changes in stale_cases:
            with self.subTest(kind="stale", fields=tuple(changes)):
                _assert_rejected(
                    self,
                    "decode_verification_receipt_projection",
                    _receipt_payload_with_changes(receipt, approval_changes=changes),
                )

        recomputed_cases = (
            {"routing_digest": "0" * 64},
            {"reservation_digest": "0" * 64},
        )
        for changes in recomputed_cases:
            updated = dict(changes)
            updated["authority_digest"] = _approval_digest_with_changes(
                receipt, changes
            )
            with self.subTest(kind="recomputed", fields=tuple(changes)):
                _assert_rejected(
                    self,
                    "decode_verification_receipt_projection",
                    _receipt_payload_with_changes(receipt, approval_changes=updated),
                )

    def test_receipt_decoder_rejects_invalid_result_and_cleanup_contracts(
        self,
    ) -> None:
        _request, receipt = _request_and_receipt()
        cases = (
            ("passed non-zero exit", {"outcome": "passed", "exit_code": 1}),
            ("failed missing exit", {"outcome": "failed", "exit_code": None}),
            ("failed zero exit", {"outcome": "failed", "exit_code": 0}),
            ("passed not-started cleanup", {"cleanup": "not_started"}),
            ("unknown-effect receipt", {"outcome": "unknown_effect"}),
        )
        for label, changes in cases:
            with self.subTest(case=label):
                _assert_rejected(
                    self,
                    "decode_verification_receipt_projection",
                    _receipt_payload_with_changes(
                        receipt,
                        top_level_changes=changes,
                    ),
                )

    def test_approval_snapshot_decoder_rejects_unsafe_and_self_inconsistent_projection(
        self,
    ) -> None:
        snapshot = _snapshot_fixture()
        cases = (
            (
                "unsafe control",
                _snapshot_payload_with_changes(
                    snapshot,
                    approved_changes={"run_id": "run-\x01"},
                ),
            ),
            (
                "required string null",
                _snapshot_payload_with_changes(
                    snapshot,
                    approved_changes={"run_id": None},
                ),
            ),
        )
        for label, payload in cases:
            with self.subTest(case=label):
                _assert_rejected(self, "decode_approval_binding_snapshot", payload)

    def test_snapshot_decoder_rejects_redigested_split_identity_and_state_bindings(
        self,
    ) -> None:
        snapshot = _snapshot_fixture()
        nested_cases = (
            ("nested approval ref", {"approval_ref": "foreign-approval-80"}),
            ("nested run", {"run_id": "foreign-run-80"}),
            ("nested team", {"team_id": "foreign-team-80"}),
            ("nested workspace", {"workspace": "/foreign/workspace-80"}),
            ("nested task", {"task_id": "foreign-task-80"}),
            ("nested dispatch", {"dispatch_id": "foreign-dispatch-80"}),
            ("nested attempt", {"attempt_id": "foreign-attempt-80"}),
            ("nested worker", {"worker_node": "foreign-worker-80"}),
            ("nested reviewer", {"reviewer_node": "foreign-reviewer-80"}),
            ("nested review round", {"review_round": 99}),
            ("nested target", {"target_head": "c" * 40}),
            ("nested claim", {"claim_ref": "foreign-claim-80"}),
            ("nested sequence", {"approval_sequence": 99}),
        )
        for label, changes in nested_cases:
            with self.subTest(binding=label):
                updated = dict(changes)
                updated["authority_digest"] = _snapshot_approval_digest_with_changes(
                    snapshot, changes
                )
                _assert_rejected(
                    self,
                    "decode_approval_binding_snapshot",
                    _snapshot_payload_with_changes(snapshot, approved_changes=updated),
                )

        top_level_cases = (
            ("top approval ref", {"approval_ref": "foreign-approval-top-80"}),
            ("top approval digest", {"approval_digest": "0" * 64}),
            ("top run", {"run_id": "foreign-run-top-80"}),
            ("top task sequence", {"task_sequence": 99}),
        )
        for label, changes in top_level_cases:
            with self.subTest(binding=label):
                _assert_rejected(
                    self,
                    "decode_approval_binding_snapshot",
                    _snapshot_payload_with_changes(snapshot, top_level_changes=changes),
                )

        state_cases = (
            ("state team", {"team_id": "foreign-state-team-80"}),
            ("state workspace", {"workspace": "/foreign/state-workspace-80"}),
            ("state task", {"task_id": "foreign-state-task-80"}),
            (
                "state dispatch and attempt",
                {
                    "dispatch_id": "foreign-state-dispatch-80",
                    "attempt_id": "foreign-state-attempt-80",
                },
            ),
            ("state worker", {"worker_node": "foreign-state-worker-80"}),
            ("state reviewer", {"reviewer_node": "foreign-state-reviewer-80"}),
            ("state review round", {"review_round": 99}),
            (
                "state target",
                {
                    "target_head": "d" * 40,
                    "target_tree_digest": "e" * 64,
                },
            ),
            ("state claim", {"claim_ref": "foreign-state-claim-80"}),
            ("state receipt", {"receipt_ref": "foreign-receipt-80"}),
            ("state phase", {"phase": "verifying"}),
        )
        for label, changes in state_cases:
            with self.subTest(binding=label):
                _assert_rejected(
                    self,
                    "decode_approval_binding_snapshot",
                    _snapshot_payload_with_state_changes(snapshot, changes),
                )

    def test_verification_payload_limit_is_exactly_one_mib_at_decoder_boundary(
        self,
    ) -> None:
        decoders = (
            "decode_approval_binding_snapshot",
            "decode_verification_request_projection",
            "decode_verification_receipt_projection",
        )
        exact = _sized_unknown_payload(MAX_VERIFICATION_PAYLOAD_BYTES)
        over = _sized_unknown_payload(MAX_VERIFICATION_PAYLOAD_BYTES + 1)
        for decoder in decoders:
            with self.subTest(decoder=decoder, size="exact"):
                try:
                    _decode(decoder, exact)
                except CODEC_ERRORS as error:
                    self.assertNotEqual(
                        getattr(error, "code", None),
                        "payload-size",
                        "exact 1 MiB must reach payload validation",
                    )
                else:
                    self.fail(f"{decoder} accepted an unknown envelope")
            with self.subTest(decoder=decoder, size="over"):
                try:
                    _decode(decoder, over)
                except CODEC_ERRORS as error:
                    self.assertEqual(getattr(error, "code", None), "payload-size")
                else:
                    self.fail(f"{decoder} accepted a payload over 1 MiB")
        self.assertEqual(
            _api("MAX_VERIFICATION_PAYLOAD_BYTES"),
            MAX_VERIFICATION_PAYLOAD_BYTES,
        )

    def test_projection_decoders_reject_integers_above_sqlite_int64(self) -> None:
        _request, receipt = _request_and_receipt()
        for field in ("lease_epoch", "fencing_token"):
            with self.subTest(codec="receipt", field=field):
                _assert_rejected(
                    self,
                    "decode_verification_receipt_projection",
                    _receipt_payload_with_changes(
                        receipt,
                        top_level_changes={field: MAX_INT64 + 1},
                    ),
                )

        snapshot = _snapshot_fixture()
        for field in ("consumer_generation", "workflow_sequence"):
            with self.subTest(codec="snapshot", field=field):
                _assert_rejected(
                    self,
                    "decode_approval_binding_snapshot",
                    _snapshot_payload_with_changes(
                        snapshot,
                        top_level_changes={field: MAX_INT64 + 1},
                    ),
                )

        _request, receipt = _request_and_receipt()
        projection = _call("verification_receipt_projection_from_receipt", receipt)
        for field in ("lease_epoch", "fencing_token"):
            forged = object.__new__(type(projection))
            for item in fields(cast(Any, projection)):
                object.__setattr__(forged, item.name, getattr(projection, item.name))
            object.__setattr__(forged, field, MAX_INT64 + 1)
            with (
                self.subTest(codec="receipt encoder", field=field),
                self.assertRaises(CODEC_ERRORS),
            ):
                _call("encode_verification_receipt_projection", forged)

    def test_typed_snapshot_digest_validates_before_digesting(self) -> None:
        snapshot = _snapshot_fixture()
        forged = object.__new__(type(snapshot))
        for item in fields(cast(Any, snapshot)):
            object.__setattr__(forged, item.name, getattr(snapshot, item.name))
        object.__setattr__(forged, "workflow_sequence", MAX_INT64 + 1)
        with self.assertRaises(CODEC_ERRORS):
            _call("approval_binding_snapshot_digest", forged)

    def test_malformed_payload_errors_do_not_retain_body_canary(self) -> None:
        malformed = ('{"version":1,"secret":"' + MALFORMED_BODY_CANARY + '"\n').encode(
            "utf-8"
        )
        for decoder in (
            "decode_approval_binding_snapshot",
            "decode_verification_request_projection",
            "decode_verification_receipt_projection",
        ):
            with self.subTest(decoder=decoder):
                try:
                    _decode(decoder, malformed)
                except CODEC_ERRORS as error:
                    representations = [str(error), repr(error), repr(error.args)]
                    for attribute in ("__cause__", "__context__"):
                        nested = getattr(error, attribute, None)
                        if nested is None:
                            continue
                        representations.extend((str(nested), repr(nested)))
                        document = getattr(nested, "doc", None)
                        if document is not None:
                            representations.append(str(document))
                    self.assertNotIn(MALFORMED_BODY_CANARY, "\n".join(representations))
                else:
                    self.fail(f"{decoder} accepted malformed JSON")

    def test_invalid_utf8_errors_do_not_retain_body_canary(self) -> None:
        malformed = (
            b'{"version":1,"secret":"'
            + MALFORMED_BODY_CANARY.encode("utf-8")
            + b'"\xff}\n'
        )
        for decoder in (
            "decode_approval_binding_snapshot",
            "decode_verification_request_projection",
            "decode_verification_receipt_projection",
        ):
            with self.subTest(decoder=decoder):
                try:
                    _decode(decoder, malformed)
                except CODEC_ERRORS as error:
                    representations = [str(error), repr(error), repr(error.args)]
                    for attribute in ("__cause__", "__context__"):
                        nested = getattr(error, attribute, None)
                        if nested is None:
                            continue
                        representations.extend((str(nested), repr(nested)))
                        document = getattr(nested, "doc", None)
                        if document is not None:
                            representations.append(str(document))
                        payload = getattr(nested, "object", None)
                        if payload is not None:
                            representations.append(repr(payload))
                    self.assertNotIn(MALFORMED_BODY_CANARY, "\n".join(representations))
                else:
                    self.fail(f"{decoder} accepted invalid UTF-8")

    def test_snapshot_surrogate_errors_do_not_retain_nested_body_canary(self) -> None:
        mapping = dict(_snapshot_wire_mapping(_snapshot_fixture()))
        mapping["task_state_bytes"] = MALFORMED_BODY_CANARY + "\ud800"
        malformed = (
            json.dumps(
                mapping,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("ascii")
            + b"\n"
        )
        try:
            _decode("decode_approval_binding_snapshot", malformed)
        except CODEC_ERRORS as error:
            self.assertEqual(type(error).__name__, "TaskVerificationLedgerError")
            representations = [str(error), repr(error), repr(error.args)]
            for attribute in ("__cause__", "__context__"):
                nested = getattr(error, attribute, None)
                if nested is None:
                    continue
                representations.extend((str(nested), repr(nested)))
                document = getattr(nested, "doc", None)
                if document is not None:
                    representations.append(str(document))
                payload = getattr(nested, "object", None)
                if payload is not None:
                    representations.append(repr(payload))
            self.assertNotIn(MALFORMED_BODY_CANARY, "\n".join(representations))
        else:
            self.fail("decode_approval_binding_snapshot accepted a surrogate")

    def test_invalid_enum_errors_do_not_retain_body_canary(self) -> None:
        snapshot = _snapshot_fixture()
        state_mapping = json.loads(cast(Any, snapshot).task_state_bytes.decode("utf-8"))
        if type(state_mapping) is not dict:
            raise AssertionError("snapshot task state oracle is not a mapping")
        state_mapping["phase"] = MALFORMED_BODY_CANARY
        receipt = _request_and_receipt()[1]
        cases = (
            (
                "task phase",
                ("decode_task_state", _canonical(state_mapping)),
            ),
            (
                "receipt outcome",
                (
                    "decode_verification_receipt_projection",
                    _receipt_payload_with_changes(
                        receipt,
                        top_level_changes={"outcome": MALFORMED_BODY_CANARY},
                    ),
                ),
            ),
            (
                "receipt cleanup",
                (
                    "decode_verification_receipt_projection",
                    _receipt_payload_with_changes(
                        receipt,
                        top_level_changes={"cleanup": MALFORMED_BODY_CANARY},
                    ),
                ),
            ),
        )
        for label, case in cases:
            decoder, payload = cast(tuple[str, object], case)
            with self.subTest(case=label):
                try:
                    _decode(decoder, payload)
                except CODEC_ERRORS as error:
                    representations = [str(error), repr(error), repr(error.args)]
                    for attribute in ("__cause__", "__context__"):
                        nested = getattr(error, attribute, None)
                        if nested is None:
                            continue
                        representations.extend((str(nested), repr(nested)))
                        document = getattr(nested, "doc", None)
                        if document is not None:
                            representations.append(str(document))
                        payload_value = getattr(nested, "object", None)
                        if payload_value is not None:
                            representations.append(repr(payload_value))
                    self.assertNotIn(MALFORMED_BODY_CANARY, "\n".join(representations))
                else:
                    self.fail(f"{decoder} accepted invalid {label}")

    def test_deeply_nested_json_is_rejected_as_a_codec_error(self) -> None:
        depth = 10_000
        malformed = b"[" * depth + b"]" * depth + b"\n"
        for decoder in (
            "decode_approval_binding_snapshot",
            "decode_verification_request_projection",
            "decode_verification_receipt_projection",
        ):
            with self.subTest(decoder=decoder):
                try:
                    _decode(decoder, malformed)
                except RecursionError as error:
                    self.fail(f"{decoder} leaked RecursionError: {error}")
                except CODEC_ERRORS:
                    pass


if __name__ == "__main__":
    unittest.main()
