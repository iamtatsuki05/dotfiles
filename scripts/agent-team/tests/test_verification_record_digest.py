"""RED contract tests for the Issue #82 verification operation record digest.

The row mapping in this module is only deterministic test input.  It is not an
authority object, an owner capability, or a source from which production code
may hydrate one.  The implementation under test must resolve the fixed
preimage itself and must not treat caller-controlled mapping order or values as
authority.

The target module is intentionally loaded lazily.  This keeps the missing
Issue #82 module/API as assertion failures (rather than collection errors)
while the contract is being implemented.
"""

from __future__ import annotations

import hashlib
import importlib
import re
import sqlite3
import struct
import unittest
from collections.abc import Callable, Mapping
from contextlib import closing
from types import ModuleType
from typing import Final, cast

VERIFICATION_STORE_MODULE: Final = "agent_team.verification_store"
RECORD_DIGEST_DOMAIN: Final = b"agent-team/verification-record/v1\0"
RECORD_DIGEST_PATTERN: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
MAX_INT64: Final = 2**63 - 1
MIN_INT64: Final = -(2**63)

VERIFICATION_RECORD_PREIMAGE_FIELDS: Final = (
    "root_key",
    "verification_ref",
    "record_version",
    "approval_binding_version",
    "approval_binding_bytes",
    "approval_binding_digest",
    "request_schema_version",
    "approval_ref",
    "approval_digest",
    "review_ref",
    "review_digest",
    "completion_ref",
    "completion_digest",
    "request_bytes",
    "request_digest",
    "run_id",
    "main_terminal_id",
    "task_id",
    "dispatch_id",
    "attempt_id",
    "worker_node",
    "reviewer_node",
    "worker_terminal_id",
    "reviewer_terminal_id",
    "team_id",
    "workspace",
    "review_round",
    "task_sequence_before",
    "task_sequence_after",
    "task_digest_before",
    "task_digest_after",
    "workflow_sequence_before",
    "workflow_sequence_after",
    "workflow_digest_before",
    "workflow_digest_after",
    "status",
    "effect_owner",
    "effect_attempt",
    "effect_epoch",
    "effect_fence",
    "effect_nonce",
    "receipt_ref",
    "receipt_digest",
    "terminal_phase",
    "terminal_receipt_ref",
    "terminal_receipt_digest",
    "unknown_code",
    "unknown_evidence_digest",
    "prepare_event_id",
    "prepare_event_digest",
    "receipt_event_id",
    "receipt_event_digest",
    "terminal_event_id",
    "terminal_event_digest",
    "unknown_event_id",
    "unknown_event_digest",
    "created_ns",
    "updated_ns",
)
RECORD_DIGEST_FIELD: Final = "record_digest"
DDL_COLUMNS: Final = (
    *VERIFICATION_RECORD_PREIMAGE_FIELDS[:15],
    RECORD_DIGEST_FIELD,
    *VERIFICATION_RECORD_PREIMAGE_FIELDS[15:],
)

_HEX64: Final = "a" * 64
_DIGEST: Final = "sha256:" + _HEX64
FIXTURE_RECORD_DIGEST: Final = (
    "sha256:588fcaf041a7225f41821c2242eec51d3dc7786bb2cecea902a3d754f20d11ad"
)


class MissingVerificationStoreAPI(AssertionError):
    """Make an absent Issue #82 module/API an explicit RED assertion."""


def _verification_store_module() -> ModuleType:
    try:
        return importlib.import_module(VERIFICATION_STORE_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name == VERIFICATION_STORE_MODULE:
            raise MissingVerificationStoreAPI(
                f"missing API module: {VERIFICATION_STORE_MODULE}"
            ) from exc
        raise


def _api(name: str) -> object:
    module = _verification_store_module()
    try:
        return getattr(module, name)
    except AttributeError as exc:
        raise MissingVerificationStoreAPI(
            f"missing API: {VERIFICATION_STORE_MODULE}.{name}"
        ) from exc


def _call(name: str, *args: object) -> object:
    value = _api(name)
    if not callable(value):
        raise MissingVerificationStoreAPI(
            f"API is not callable: {VERIFICATION_STORE_MODULE}.{name}"
        )
    return cast(Callable[..., object], value)(*args)


def _verification_digest(mapping: Mapping[str, object]) -> str:
    value = _call("_verification_record_digest", mapping)
    if type(value) is not str:
        raise AssertionError("record digest API must return an exact str")
    return value


def _verification_ddl_columns() -> tuple[str, ...]:
    """Read the schema DDL in memory without opening or mutating a Store."""

    from agent_team import store as store_module

    sql = getattr(store_module, "_VERIFICATION_OPERATIONS_SQL", None)
    if type(sql) is not str:
        raise AssertionError("verification_operations DDL is unavailable")
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.executescript(sql)
        rows = connection.execute(
            "PRAGMA table_info(verification_operations)"
        ).fetchall()
    return tuple(cast(str, row[1]) for row in rows)


def _record_fixture() -> dict[str, object]:
    """Return a full DDL-ordered PREPARED row used only as pure test input."""

    values: dict[str, object] = {
        "root_key": "root-82",
        "verification_ref": "verification-82",
        "record_version": 1,
        "approval_binding_version": 1,
        "approval_binding_bytes": b"canonical-approval-binding-v1",
        "approval_binding_digest": _DIGEST,
        "request_schema_version": 1,
        "approval_ref": "approval-82",
        "approval_digest": _HEX64,
        "review_ref": "review-82",
        "review_digest": _HEX64,
        "completion_ref": "completion-82",
        "completion_digest": _HEX64,
        "request_bytes": b"canonical-verification-request-v1",
        "request_digest": _HEX64,
        "record_digest": _DIGEST,
        "run_id": "run-82",
        "main_terminal_id": "main-terminal-82",
        "task_id": "task-82",
        "dispatch_id": "dispatch-82",
        "attempt_id": "attempt-82",
        "worker_node": "worker-82",
        "reviewer_node": "reviewer-82",
        "worker_terminal_id": "worker-terminal-82",
        "reviewer_terminal_id": "reviewer-terminal-82",
        "team_id": "team-82",
        "workspace": "workspace-82",
        "review_round": 0,
        "task_sequence_before": 2,
        "task_sequence_after": 3,
        "task_digest_before": _DIGEST,
        "task_digest_after": _DIGEST,
        "workflow_sequence_before": 2,
        "workflow_sequence_after": 3,
        "workflow_digest_before": _DIGEST,
        "workflow_digest_after": _DIGEST,
        "status": "PREPARED",
        "effect_owner": None,
        "effect_attempt": None,
        "effect_epoch": None,
        "effect_fence": None,
        "effect_nonce": None,
        "receipt_ref": None,
        "receipt_digest": None,
        "terminal_phase": None,
        "terminal_receipt_ref": None,
        "terminal_receipt_digest": None,
        "unknown_code": None,
        "unknown_evidence_digest": None,
        "prepare_event_id": 1,
        "prepare_event_digest": _DIGEST,
        "receipt_event_id": None,
        "receipt_event_digest": None,
        "terminal_event_id": None,
        "terminal_event_digest": None,
        "unknown_event_id": None,
        "unknown_event_digest": None,
        "created_ns": 100,
        "updated_ns": 101,
    }
    if tuple(values) != DDL_COLUMNS:
        raise AssertionError("record fixture is not in DDL column order")
    return values


def _oracle_frame(mapping: Mapping[str, object]) -> bytes:
    """Independently encode the contract's fixed record-digest frame."""

    if type(mapping) is not dict or tuple(mapping) != DDL_COLUMNS:
        raise ValueError("record mapping is not in canonical DDL order")

    frame = bytearray(struct.pack(">I", len(VERIFICATION_RECORD_PREIMAGE_FIELDS)))
    for field in VERIFICATION_RECORD_PREIMAGE_FIELDS:
        field_bytes = field.encode("utf-8")
        frame.extend(struct.pack(">I", len(field_bytes)))
        frame.extend(field_bytes)
        value = mapping[field]
        if value is None:
            frame.extend(b"N")
        elif type(value) is int:
            frame.extend(b"I")
            frame.extend(struct.pack(">q", value))
        elif type(value) is str:
            value_bytes = value.encode("utf-8")
            frame.extend(b"T")
            frame.extend(struct.pack(">Q", len(value_bytes)))
            frame.extend(value_bytes)
        elif type(value) is bytes:
            frame.extend(b"B")
            frame.extend(struct.pack(">Q", len(value)))
            frame.extend(value)
        else:
            raise TypeError(f"unsupported test value for {field}: {type(value)!r}")
    return bytes(frame)


def _oracle_digest(mapping: Mapping[str, object]) -> str:
    return (
        "sha256:"
        + hashlib.sha256(RECORD_DIGEST_DOMAIN + _oracle_frame(mapping)).hexdigest()
    )


def _assert_rejected(test: unittest.TestCase, mapping: Mapping[str, object]) -> None:
    try:
        _verification_digest(mapping)
    except MissingVerificationStoreAPI:
        raise
    except (KeyError, TypeError, UnicodeError, ValueError, OverflowError):
        return
    test.fail("record digest helper accepted an invalid mapping")


class VerificationRecordDigestRedTest(unittest.TestCase):
    def test_fixed_preimage_fields_are_exact_58_tuple(self) -> None:
        value = _api("VERIFICATION_RECORD_PREIMAGE_FIELDS")
        self.assertIs(type(value), tuple)
        self.assertEqual(value, VERIFICATION_RECORD_PREIMAGE_FIELDS)
        self.assertEqual(len(cast(tuple[str, ...], value)), 58)

    def test_preimage_fields_match_ddl_in_exact_order_without_record_digest(
        self,
    ) -> None:
        ddl_columns = _verification_ddl_columns()
        self.assertEqual(len(ddl_columns), 59)
        self.assertEqual(ddl_columns, DDL_COLUMNS)
        self.assertEqual(
            tuple(column for column in ddl_columns if column != RECORD_DIGEST_FIELD),
            VERIFICATION_RECORD_PREIMAGE_FIELDS,
        )
        self.assertEqual(ddl_columns.count(RECORD_DIGEST_FIELD), 1)

    def test_oracle_frame_has_fixed_count_and_explicit_field_names(self) -> None:
        mapping = _record_fixture()
        frame = _oracle_frame(mapping)
        self.assertEqual(struct.unpack_from(">I", frame)[0], 58)
        offset = 4
        for field in VERIFICATION_RECORD_PREIMAGE_FIELDS:
            field_size = struct.unpack_from(">I", frame, offset)[0]
            offset += 4
            field_bytes = frame[offset : offset + field_size]
            offset += field_size
            self.assertEqual(field_bytes, field.encode("utf-8"))
            tag = frame[offset : offset + 1]
            offset += 1
            if tag == b"N":
                continue
            if tag == b"I":
                offset += 8
                continue
            self.assertIn(tag, (b"T", b"B"))
            value_size = struct.unpack_from(">Q", frame, offset)[0]
            offset += 8 + value_size
        self.assertEqual(offset, len(frame))

    def test_digest_matches_independent_domain_and_framing_oracle(self) -> None:
        mapping = _record_fixture()
        expected = _oracle_digest(mapping)
        self.assertEqual(expected, FIXTURE_RECORD_DIGEST)
        self.assertRegex(expected, RECORD_DIGEST_PATTERN)
        self.assertEqual(_verification_digest(mapping), expected)
        self.assertNotEqual(
            expected,
            hashlib.sha256(_oracle_frame(mapping)).hexdigest(),
        )

    def test_digest_is_deterministic_and_does_not_mutate_caller_input(self) -> None:
        mapping = _record_fixture()
        before = dict(mapping)
        first = _verification_digest(mapping)
        second = _verification_digest(dict(mapping))
        self.assertEqual(first, second)
        self.assertEqual(mapping, before)

    def test_record_digest_is_self_excluding(self) -> None:
        mapping = _record_fixture()
        changed = dict(mapping)
        changed[RECORD_DIGEST_FIELD] = "sha256:" + "f" * 64
        self.assertEqual(_verification_digest(mapping), _verification_digest(changed))
        self.assertEqual(_oracle_digest(mapping), _oracle_digest(changed))

    def test_all_four_type_tags_match_the_independent_oracle(self) -> None:
        mapping = _record_fixture()
        variants = (
            ("null", "effect_owner", None),
            ("text", "effect_owner", "effect-owner-82"),
            ("int", "effect_attempt", 1),
            ("blob", "request_bytes", b"request-bytes-82"),
        )
        for tag, field, value in variants:
            with self.subTest(tag=tag):
                variant = dict(mapping)
                variant[field] = value
                self.assertEqual(_verification_digest(variant), _oracle_digest(variant))

    def test_each_preimage_field_changes_the_digest(self) -> None:
        mapping = _record_fixture()
        original = _verification_digest(mapping)
        for field in VERIFICATION_RECORD_PREIMAGE_FIELDS:
            variant = dict(mapping)
            value = variant[field]
            if value is None:
                variant[field] = ""
            elif type(value) is int:
                variant[field] = value + 1
            elif type(value) is str:
                variant[field] = value + "-changed"
            elif type(value) is bytes:
                variant[field] = value + b"-changed"
            else:
                raise AssertionError(f"unhandled fixture type for {field}")
            with self.subTest(field=field):
                self.assertEqual(_verification_digest(variant), _oracle_digest(variant))
                self.assertNotEqual(_verification_digest(variant), original)

    def test_null_and_empty_text_are_distinct(self) -> None:
        mapping = _record_fixture()
        empty = dict(mapping)
        empty["effect_owner"] = ""
        self.assertNotEqual(_verification_digest(mapping), _verification_digest(empty))
        self.assertEqual(_verification_digest(empty), _oracle_digest(empty))

    def test_utf8_lengths_are_bytes_and_text_is_not_normalized(self) -> None:
        mapping = _record_fixture()
        composed = dict(mapping)
        composed["workspace"] = "東京é"
        decomposed = dict(mapping)
        decomposed["workspace"] = "東京e\u0301"
        self.assertEqual(_verification_digest(composed), _oracle_digest(composed))
        self.assertEqual(_verification_digest(decomposed), _oracle_digest(decomposed))
        self.assertNotEqual(
            _verification_digest(composed), _verification_digest(decomposed)
        )

    def test_signed_int64_boundaries_are_accepted_and_framed(self) -> None:
        mapping = _record_fixture()
        for value in (MIN_INT64, MAX_INT64):
            with self.subTest(value=value):
                variant = dict(mapping)
                variant["task_sequence_before"] = value
                self.assertEqual(_verification_digest(variant), _oracle_digest(variant))

    def test_bool_float_and_non_exact_binary_values_are_rejected(self) -> None:
        mapping = _record_fixture()
        for field, value in (
            ("review_round", True),
            ("review_round", 1.0),
            ("request_bytes", bytearray(b"request")),
            ("request_bytes", memoryview(b"request")),
        ):
            with self.subTest(field=field, value_type=type(value).__name__):
                variant = dict(mapping)
                variant[field] = value
                _assert_rejected(self, variant)

    def test_int64_overflow_is_rejected(self) -> None:
        mapping = _record_fixture()
        for value in (MIN_INT64 - 1, MAX_INT64 + 1):
            with self.subTest(value=value):
                variant = dict(mapping)
                variant["updated_ns"] = value
                _assert_rejected(self, variant)

    def test_surrogate_text_is_rejected_without_normalization(self) -> None:
        mapping = _record_fixture()
        variant = dict(mapping)
        variant["workspace"] = "invalid-\ud800"
        _assert_rejected(self, variant)

    def test_missing_unknown_and_reordered_fields_are_rejected(self) -> None:
        mapping = _record_fixture()
        missing = dict(mapping)
        del missing["workspace"]
        unknown = dict(mapping)
        unknown["future_field"] = "must-not-be-ignored"
        reordered = dict(reversed(tuple(mapping.items())))
        for label, variant in (
            ("missing", missing),
            ("unknown", unknown),
            ("reordered", reordered),
        ):
            with self.subTest(label=label):
                _assert_rejected(self, variant)


if __name__ == "__main__":
    unittest.main()
