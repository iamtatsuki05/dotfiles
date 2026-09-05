from __future__ import annotations

import hashlib
import importlib
import json
import unittest
from collections.abc import Callable
from dataclasses import fields, replace
from types import ModuleType
from typing import Final, cast

from agent_team import task_policy
from agent_team.task_policy import (
    AttemptId,
    ClaimRef,
    DispatchId,
    GitObjectId,
    ReceiptRef,
    TaskId,
    TaskPhase,
    TaskPolicyStateV4,
    TreeDigest,
    WorkspaceIdentity,
    task_state_to_dict,
)
from agent_team.topology import NodeId, TeamId

LEDGER_MODULE: Final = "agent_team.task_verification_ledger"
EXPECTED_TASK_STATE_FIELDS: Final = (
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
CODEC_VERSION_NAMES: Final = (
    "TASK_STATE_CODEC_VERSION",
    "APPROVAL_BINDING_CODEC_VERSION",
    "VERIFICATION_REQUEST_CODEC_VERSION",
    "VERIFICATION_RECEIPT_CODEC_VERSION",
    "VERIFICATION_RECORD_VERSION",
)
CODEC_ERRORS: Final = (TypeError, ValueError, UnicodeError, OverflowError)
SUBCLASS_CANARY: Final = "task-state-subclass-canary-80"


class MissingLedgerAPI(AssertionError):
    """Make an absent Issue #80 module an explicit RED assertion."""


def _ledger_module() -> ModuleType:
    try:
        return importlib.import_module(LEDGER_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name == LEDGER_MODULE:
            raise MissingLedgerAPI(f"missing API module: {LEDGER_MODULE}") from exc
        raise


def _required_api(name: str) -> object:
    module = _ledger_module()
    try:
        return getattr(module, name)
    except AttributeError as exc:
        raise MissingLedgerAPI(f"missing API: {LEDGER_MODULE}.{name}") from exc


def _call_api(name: str, *args: object) -> object:
    value = _required_api(name)
    if not callable(value):
        raise MissingLedgerAPI(f"API is not callable: {LEDGER_MODULE}.{name}")
    function = cast(Callable[..., object], value)
    return function(*args)


def _encode(state: object) -> bytes:
    value = _call_api("encode_task_state", state)
    if type(value) is not bytes:
        raise AssertionError("encode_task_state must return exact bytes")
    return value


def _decode(raw: object) -> TaskPolicyStateV4:
    value = _call_api("decode_task_state", raw)
    if type(value) is not TaskPolicyStateV4:
        raise AssertionError("decode_task_state must return TaskPolicyStateV4")
    return value


def _digest(value: object) -> str:
    result = _call_api("task_state_digest", value)
    if type(result) is not str:
        raise AssertionError("task_state_digest must return a string digest")
    return result


def _full_state() -> TaskPolicyStateV4:
    return TaskPolicyStateV4(
        version=4,
        team_id=TeamId("チーム-é"),
        workspace=WorkspaceIdentity("/workspace/東京/e\u0301"),
        sequence=7,
        task_id=TaskId("task-課題-🚀"),
        attempt_id=AttemptId("attempt-一"),
        dispatch_id=DispatchId("dispatch-二"),
        worker_node=NodeId("worker-三"),
        reviewer_node=NodeId("reviewer-四"),
        review_round=3,
        target_head=GitObjectId("a" * 40),
        target_tree_digest=TreeDigest("b" * 64),
        claim_ref=ClaimRef("claim-審査"),
        receipt_ref=ReceiptRef("receipt-結果"),
        phase=TaskPhase.VERIFYING,
    )


def _null_state() -> TaskPolicyStateV4:
    return TaskPolicyStateV4(
        version=4,
        team_id=TeamId("チーム"),
        workspace=WorkspaceIdentity("/workspace/東京"),
        sequence=0,
        task_id=TaskId("課題"),
        attempt_id=None,
        dispatch_id=None,
        worker_node=None,
        reviewer_node=None,
        review_round=0,
        target_head=None,
        target_tree_digest=None,
        claim_ref=None,
        receipt_ref=None,
        phase=TaskPhase.PENDING,
    )


def _wire_mapping(state: TaskPolicyStateV4) -> dict[str, object]:
    current = task_state_to_dict(state)
    return {field: current[field] for field in EXPECTED_TASK_STATE_FIELDS}


def _compact_bytes(mapping: dict[str, object]) -> bytes:
    return (
        json.dumps(
            mapping,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _assert_decode_rejects(raw: object) -> None:
    try:
        _decode(raw)
    except MissingLedgerAPI:
        raise
    except AssertionError:
        raise
    except CODEC_ERRORS:
        return
    raise AssertionError("decode_task_state accepted a noncanonical payload")


def _assert_encode_rejects(value: object) -> None:
    try:
        _encode(value)
    except MissingLedgerAPI:
        raise
    except AssertionError:
        raise
    except CODEC_ERRORS:
        return
    raise AssertionError("encode_task_state accepted a non-state/private mapping")


class TaskVerificationLedgerCodecTests(unittest.TestCase):
    def test_public_version_rebinding_cannot_change_current_state_codec(self) -> None:
        module = _ledger_module()
        state = _full_state()
        baseline_encoded = _encode(state)
        baseline_digest = _digest(state)
        originals = {
            "TASK_STATE_CODEC_VERSION": module.__dict__["TASK_STATE_CODEC_VERSION"],
            "TASK_STATE_FIELDS": module.__dict__["TASK_STATE_FIELDS"],
            "TASK_STATE_DIGEST_DOMAIN": module.__dict__["TASK_STATE_DIGEST_DOMAIN"],
            "MAX_TASK_STATE_BYTES": module.__dict__["MAX_TASK_STATE_BYTES"],
            "MAX_INT64": module.__dict__["MAX_INT64"],
        }
        original_policy_version = task_policy.__dict__["STATE_POLICY_VERSION"]
        try:
            module.__dict__["TASK_STATE_CODEC_VERSION"] = 2
            module.__dict__["TASK_STATE_FIELDS"] = tuple(
                reversed(module.__dict__["TASK_STATE_FIELDS"])
            )
            module.__dict__["TASK_STATE_DIGEST_DOMAIN"] = b"alternate/task-domain"
            module.__dict__["MAX_TASK_STATE_BYTES"] = 2 * 1024 * 1024
            module.__dict__["MAX_INT64"] = 2**64
            task_policy.__dict__["STATE_POLICY_VERSION"] = 3

            encoded = _encode(state)
            wire = json.loads(encoded.decode("utf-8"))
            self.assertEqual(4, wire["version"])
            self.assertEqual(baseline_encoded, encoded)
            self.assertEqual(baseline_digest, _digest(state))
            self.assertEqual(state, _decode(encoded))

            with self.assertRaises(CODEC_ERRORS):
                replace(state, version=3)
            legacy_wire = _wire_mapping(state)
            legacy_wire["version"] = 3
            with self.assertRaises(CODEC_ERRORS):
                _decode(_compact_bytes(legacy_wire))
        finally:
            for name, value in originals.items():
                module.__dict__[name] = value
            task_policy.__dict__["STATE_POLICY_VERSION"] = original_policy_version

    def test_encoder_rejects_scalar_subclasses_before_owner_normalization(self) -> None:
        state = _full_state()

        class AliasInt(int):
            def __lt__(self, _other: object) -> bool:
                return False

            def __gt__(self, _other: object) -> bool:
                return False

            def __le__(self, _other: object) -> bool:
                return True

            def __ge__(self, _other: object) -> bool:
                return True

        class AliasStr(str):
            def __str__(self) -> str:
                return SUBCLASS_CANARY

        def forged(**changes: object) -> object:
            value = object.__new__(type(state))
            for item in fields(state):
                object.__setattr__(value, item.name, getattr(state, item.name))
            for name, replacement in changes.items():
                object.__setattr__(value, name, replacement)
            return value

        with self.assertRaises(CODEC_ERRORS):
            _encode(forged(sequence=AliasInt(2**63)))

        with self.assertRaises(CODEC_ERRORS) as error_context:
            _encode(forged(team_id=AliasStr("team-safe")))
        error = error_context.exception
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
        self.assertNotIn(SUBCLASS_CANARY, "\n".join(representations))

        for name in ("version", "sequence", "review_round"):
            with self.subTest(field=name), self.assertRaises(CODEC_ERRORS):
                _encode(forged(**{name: True}))

    def test_all_five_codec_versions_are_exactly_one(self) -> None:
        for name in CODEC_VERSION_NAMES:
            with self.subTest(name=name):
                value = _required_api(name)
                self.assertIs(type(value), int)
                self.assertEqual(value, 1)

    def test_task_state_fields_are_the_exact_fifteen_wire_fields(self) -> None:
        fields = cast(tuple[str, ...], _required_api("TASK_STATE_FIELDS"))

        self.assertIs(type(fields), tuple)
        self.assertEqual(fields, EXPECTED_TASK_STATE_FIELDS)
        self.assertEqual(len(fields), 15)
        self.assertNotIn("root_key", fields)
        self.assertNotIn("run_id", fields)
        self.assertNotIn("state_digest", fields)

    def test_full_unicode_state_round_trips_as_compact_canonical_utf8_bytes(
        self,
    ) -> None:
        state = _full_state()
        encoded = _encode(state)

        self.assertEqual(encoded, _compact_bytes(_wire_mapping(state)))
        self.assertEqual(encoded.decode("utf-8").encode("utf-8"), encoded)
        self.assertTrue(encoded.endswith(b"\n"))
        self.assertFalse(encoded.endswith(b"\n\n"))
        self.assertNotIn(b": ", encoded)
        self.assertNotIn(b", ", encoded)
        self.assertEqual(_decode(encoded), state)
        self.assertEqual(json.loads(encoded.decode("utf-8")), _wire_mapping(state))
        self.assertIn("e\u0301", state.workspace)
        self.assertIn("é", state.team_id)

    def test_nullable_state_round_trips_with_explicit_nulls_and_unicode(self) -> None:
        state = _null_state()
        encoded = _encode(state)
        wire = json.loads(encoded.decode("utf-8"))

        for field in (
            "attempt_id",
            "dispatch_id",
            "worker_node",
            "reviewer_node",
            "target_head",
            "target_tree_digest",
            "claim_ref",
            "receipt_ref",
        ):
            with self.subTest(field=field):
                self.assertIsNone(wire[field])
        self.assertEqual(_decode(encoded), state)
        self.assertEqual(wire["team_id"], "チーム")
        self.assertEqual(wire["task_id"], "課題")

    def test_decode_requires_exact_bytes_and_rejects_all_noncanonical_forms(
        self,
    ) -> None:
        state = _full_state()
        canonical = _encode(state)
        mapping = _wire_mapping(state)
        cases: list[tuple[str, object]] = [
            ("duplicate field", canonical[:-2] + b',"version":4}\n'),
            ("unknown field", _compact_bytes({**mapping, "unknown": "x"})),
            ("future field", _compact_bytes({**mapping, "future_field": "x"})),
            ("root correlation field", _compact_bytes({**mapping, "root_key": "x"})),
            ("run correlation field", _compact_bytes({**mapping, "run_id": "x"})),
            (
                "missing field",
                _compact_bytes(
                    {key: value for key, value in mapping.items() if key != "phase"}
                ),
            ),
            ("version three", _compact_bytes({**mapping, "version": 3})),
            ("version five", _compact_bytes({**mapping, "version": 5})),
            ("version float", _compact_bytes({**mapping, "version": 4.0})),
            ("version string", _compact_bytes({**mapping, "version": "4"})),
            ("version boolean", _compact_bytes({**mapping, "version": True})),
            ("sequence float", _compact_bytes({**mapping, "sequence": 7.0})),
            ("sequence string", _compact_bytes({**mapping, "sequence": "7"})),
            ("sequence boolean", _compact_bytes({**mapping, "sequence": True})),
            ("review round float", _compact_bytes({**mapping, "review_round": 3.0})),
            ("negative sequence", _compact_bytes({**mapping, "sequence": -1})),
            ("nan", canonical.replace(b'"sequence":7', b'"sequence":NaN', 1)),
            (
                "infinity",
                canonical.replace(b'"sequence":7', b'"sequence":Infinity', 1),
            ),
            (
                "malformed utf8",
                canonical.replace("チーム".encode(), b"\xff", 1),
            ),
            ("utf8 bom", b"\xef\xbb\xbf" + canonical),
            (
                "reordered fields",
                _compact_bytes(dict(reversed(tuple(mapping.items())))),
            ),
            (
                "pretty printed",
                (
                    json.dumps(mapping, ensure_ascii=False, allow_nan=False, indent=2)
                    + "\n"
                ).encode("utf-8"),
            ),
            (
                "escaped unicode",
                (
                    json.dumps(
                        mapping,
                        ensure_ascii=True,
                        allow_nan=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode("utf-8"),
            ),
            ("missing trailing newline", canonical[:-1]),
            ("two trailing newlines", canonical + b"\n"),
            ("trailing space", canonical + b" "),
            ("trailing json", canonical + b"{}"),
            ("top-level array", b"[]\n"),
            ("top-level null", b"null\n"),
        ]

        for name, payload in cases:
            with self.subTest(case=name):
                _assert_decode_rejects(payload)

        for field in EXPECTED_TASK_STATE_FIELDS:
            missing = {key: value for key, value in mapping.items() if key != field}
            with self.subTest(case=f"missing {field}"):
                _assert_decode_rejects(_compact_bytes(missing))

    def test_decode_does_not_accept_text_or_bytes_like_inputs(self) -> None:
        encoded = _encode(_full_state())

        for value in (encoded.decode("utf-8"), bytearray(encoded), memoryview(encoded)):
            with self.subTest(input_type=type(value).__name__):
                _assert_decode_rejects(value)

    def test_state_digest_is_domain_separated_and_supports_state_or_bytes(self) -> None:
        state = _full_state()
        encoded = _encode(state)
        from_state = _digest(state)
        from_bytes = _digest(encoded)

        self.assertEqual(from_state, from_bytes)
        self.assertRegex(from_state, r"^sha256:[0-9a-f]{64}$")
        self.assertNotEqual(from_state, "sha256:" + hashlib.sha256(encoded).hexdigest())
        self.assertNotEqual(from_state, hashlib.sha256(encoded).hexdigest())

        pretty = (
            json.dumps(_wire_mapping(state), ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        with self.assertRaises(CODEC_ERRORS):
            _digest(pretty)

    def test_every_mutable_state_field_changes_the_digest(self) -> None:
        state = _full_state()
        baseline = _digest(state)
        changes = (
            ("team_id", replace(state, team_id=TeamId("チーム2"))),
            (
                "workspace",
                replace(state, workspace=WorkspaceIdentity("/workspace/東京/別")),
            ),
            ("sequence", replace(state, sequence=8)),
            ("task_id", replace(state, task_id=TaskId("task-別"))),
            ("attempt_id", replace(state, attempt_id=AttemptId("attempt-別"))),
            ("dispatch_id", replace(state, dispatch_id=DispatchId("dispatch-別"))),
            ("worker_node", replace(state, worker_node=NodeId("worker-別"))),
            ("reviewer_node", replace(state, reviewer_node=NodeId("reviewer-別"))),
            ("review_round", replace(state, review_round=4)),
            ("target_head", replace(state, target_head=GitObjectId("c" * 40))),
            (
                "target_tree_digest",
                replace(state, target_tree_digest=TreeDigest("d" * 64)),
            ),
            ("claim_ref", replace(state, claim_ref=ClaimRef("claim-変更"))),
            ("receipt_ref", replace(state, receipt_ref=ReceiptRef("receipt-変更"))),
            ("phase", replace(state, phase=TaskPhase.COMPLETED)),
        )

        for field, changed in changes:
            with self.subTest(field=field):
                self.assertNotEqual(_digest(changed), baseline)
                self.assertNotEqual(_encode(changed), _encode(state))

        invalid_version = {**_wire_mapping(state), "version": 5}
        with self.assertRaises(CODEC_ERRORS):
            _digest(_compact_bytes(invalid_version))

    def test_wire_excludes_body_private_and_sql_correlation_fields(self) -> None:
        state = _full_state()
        encoded = _encode(state)
        wire = json.loads(encoded.decode("utf-8"))

        self.assertEqual(tuple(wire), EXPECTED_TASK_STATE_FIELDS)
        self.assertEqual(set(wire), set(EXPECTED_TASK_STATE_FIELDS))
        for forbidden in (
            "root_key",
            "run_id",
            "argv",
            "prompt",
            "task_body",
            "reviewer_body",
            "agent_body",
            "stdout",
            "stderr",
            "environment",
            "credential",
            "token",
            "secret",
            "_issuer",
            "bound",
            "record",
            "sentinel",
        ):
            with self.subTest(field=forbidden):
                self.assertNotIn(forbidden, wire)
                self.assertNotIn(forbidden.encode("utf-8"), encoded)

        for private_field in (
            "argv",
            "prompt",
            "stdout",
            "stderr",
            "_issuer",
            "secret",
        ):
            with self.subTest(private_field=private_field):
                mapping_candidate = {
                    **_wire_mapping(state),
                    private_field: "private-body",
                }
                _assert_decode_rejects(_compact_bytes(mapping_candidate))

        candidates: tuple[object, ...] = (
            _wire_mapping(state),
            {"state": _wire_mapping(state), "private": "private-body"},
            None,
            object(),
        )
        for candidate in candidates:
            _assert_encode_rejects(candidate)


if __name__ == "__main__":
    unittest.main()
