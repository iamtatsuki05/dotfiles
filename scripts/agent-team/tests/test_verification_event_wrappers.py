"""RED contract tests for Issue #82 workflow verification event wrappers.

The wrappers are pure digest helpers.  These tests deliberately keep the
module lookup lazy so that a missing Issue #82 module or API is reported as a
failing RED assertion rather than a collection error.  The oracle below is
independent of the implementation and freezes the event wire contract:
every value is an exact scalar rendered as UTF-8, framed by a fixed count and
u64 byte lengths, and hashed under a stage-specific domain.
"""

from __future__ import annotations

import hashlib
import importlib
import re
import struct
import unittest
from collections.abc import Callable
from enum import Enum
from types import ModuleType
from typing import Final, cast

VERIFICATION_STORE_MODULE: Final = "agent_team.verification_store"
VERIFICATION_ACTOR: Final = "verification-store-adapter-v1"
REQUEST_DOMAIN: Final = b"agent-team/workflow-verification-request/v1\0"
EVIDENCE_DOMAINS: Final = {
    "PREPARE": b"agent-team/workflow-verification-prepare-evidence/v1\0",
    "RECEIPT": b"agent-team/workflow-verification-receipt-evidence/v1\0",
    "TERMINAL": b"agent-team/workflow-verification-terminal-evidence/v1\0",
    "UNKNOWN": b"agent-team/workflow-verification-unknown-evidence/v1\0",
}
STAGE_NAMES: Final = ("PREPARE", "RECEIPT", "TERMINAL", "UNKNOWN")
STAGE_VALUES: Final = ("prepare", "receipt", "terminal", "unknown")
WRAPPER_PATTERN: Final = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
BARE_DIGEST: Final = "a" * 64
WRAPPED_DIGEST: Final = "sha256:" + BARE_DIGEST
ROOT_KEY: Final = "root-82-東京-e\u0301"
VERIFICATION_REF: Final = "verification-82-🚀"
SEQUENCES: Final = (2, 3, 4, 5)


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


def _stage_type() -> type[Enum]:
    value = _api("VerificationStage")
    if not isinstance(value, type) or not issubclass(value, Enum):
        raise MissingVerificationStoreAPI(
            "VerificationStage must be an exact Enum class"
        )
    return cast(type[Enum], value)


def _stage(name: str) -> Enum:
    value = getattr(_stage_type(), name, None)
    if not isinstance(value, Enum):
        raise MissingVerificationStoreAPI(f"missing VerificationStage member: {name}")
    return value


def _stage_wire_value(stage: Enum) -> str:
    value = stage.value
    if type(value) is not str:
        raise AssertionError("VerificationStage values must be exact strings")
    return value


def _frame(values: tuple[object, ...]) -> bytes:
    """Independently encode the fixed count + u64 UTF-8 event frame."""

    frame = bytearray(struct.pack(">I", len(values)))
    for value in values:
        if type(value) is str:
            encoded = value.encode("utf-8")
        elif type(value) is int:
            encoded = str(value).encode("utf-8")
        else:
            raise AssertionError(f"unsupported oracle value: {type(value)!r}")
        frame.extend(struct.pack(">Q", len(encoded)))
        frame.extend(encoded)
    return bytes(frame)


def _oracle(domain: bytes, values: tuple[object, ...]) -> str:
    return "sha256:" + hashlib.sha256(domain + _frame(values)).hexdigest()


def _request_values(stage: Enum, values: tuple[object, ...]) -> tuple[object, ...]:
    return (_stage_wire_value(stage), *values)


def _request_args(
    stage: object,
    root_key: object = ROOT_KEY,
    verification_ref: object = VERIFICATION_REF,
    workflow_sequence_before: object = SEQUENCES[0],
    workflow_sequence_after: object = SEQUENCES[1],
    task_sequence_before: object = SEQUENCES[2],
    task_sequence_after: object = SEQUENCES[3],
    request_digest: object = BARE_DIGEST,
) -> tuple[object, ...]:
    return (
        stage,
        root_key,
        verification_ref,
        workflow_sequence_before,
        workflow_sequence_after,
        task_sequence_before,
        task_sequence_after,
        request_digest,
    )


def _request(stage: object, **changes: object) -> object:
    values = dict(
        zip(
            (
                "root_key",
                "verification_ref",
                "workflow_sequence_before",
                "workflow_sequence_after",
                "task_sequence_before",
                "task_sequence_after",
                "request_digest",
            ),
            _request_args(stage)[1:],
            strict=True,
        )
    )
    values.update(changes)
    return _call(
        "_verification_request_wrapper",
        *_request_args(stage, **values),
    )


def _evidence(stage: object, *source: object) -> object:
    return _evidence_with_values(
        stage,
        (ROOT_KEY, VERIFICATION_REF, *SEQUENCES, *source),
    )


def _evidence_with_values(stage: object, values: tuple[object, ...]) -> object:
    return _call(
        "_verification_evidence_wrapper",
        stage,
        *values,
    )


def _assert_rejects(call: Callable[[], object]) -> None:
    try:
        call()
    except MissingVerificationStoreAPI:
        raise
    except (TypeError, ValueError, UnicodeError, OverflowError):
        return
    raise AssertionError("invalid event-wrapper input was accepted")


class VerificationEventWrapperContractTests(unittest.TestCase):
    def test_verification_actor_is_the_exact_closed_adapter_identity(self) -> None:
        actor = _api("VERIFICATION_ACTOR")
        self.assertIs(type(actor), str)
        self.assertEqual(VERIFICATION_ACTOR, actor)

    def test_verification_stage_is_closed_with_exact_wire_values(self) -> None:
        stage_type = _stage_type()
        self.assertEqual(STAGE_NAMES, tuple(stage_type.__members__))
        self.assertEqual(
            STAGE_VALUES,
            tuple(member.value for member in stage_type),
        )
        for member in stage_type:
            with self.subTest(stage=member.name):
                self.assertIs(type(member.value), str)

    def test_request_wrapper_matches_the_independent_eight_field_oracle(self) -> None:
        stage = _stage("PREPARE")
        actual = _request(stage)
        expected = _oracle(
            REQUEST_DOMAIN,
            _request_values(
                stage, (ROOT_KEY, VERIFICATION_REF, *SEQUENCES, BARE_DIGEST)
            ),
        )
        self.assertIs(type(actual), str)
        self.assertRegex(actual, WRAPPER_PATTERN)
        self.assertEqual(expected, actual)

    def test_request_wrapper_has_no_record_or_event_pointer_extension(self) -> None:
        stage = _stage("PREPARE")
        _assert_rejects(
            lambda: _call(
                "_verification_request_wrapper",
                *_request_args(stage),
                "record-digest-must-not-be-here",
            )
        )
        _assert_rejects(
            lambda: _call(
                "_verification_request_wrapper",
                *_request_args(stage),
                "event-pointer-must-not-be-here",
            )
        )

    def test_request_wrapper_is_deterministic_and_sensitive_to_every_field(
        self,
    ) -> None:
        stage = _stage("PREPARE")
        baseline = _request(stage)
        self.assertEqual(baseline, _request(stage))
        changes: tuple[tuple[str, object], ...] = (
            ("root_key", "root-82-other"),
            ("verification_ref", "verification-82-other"),
            ("workflow_sequence_before", 3),
            ("workflow_sequence_after", 4),
            ("task_sequence_before", 5),
            ("task_sequence_after", 6),
            ("request_digest", "b" * 64),
        )
        for field, replacement in changes:
            with self.subTest(field=field):
                self.assertNotEqual(baseline, _request(stage, **{field: replacement}))

    def test_request_wrapper_accepts_zero_and_signed_int64_sequence_bounds(
        self,
    ) -> None:
        stage = _stage("PREPARE")
        actual = _request(
            stage,
            workflow_sequence_before=0,
            workflow_sequence_after=2**63 - 1,
            task_sequence_before=0,
            task_sequence_after=2**63 - 1,
        )
        self.assertRegex(actual, WRAPPER_PATTERN)

    def test_request_wrapper_rejects_invalid_sequence_scalars_and_bounds(self) -> None:
        stage = _stage("PREPARE")
        for field in (
            "workflow_sequence_before",
            "workflow_sequence_after",
            "task_sequence_before",
            "task_sequence_after",
        ):
            for invalid in (-1, 2**63, True, 1.0, "1", None):
                with self.subTest(field=field, invalid=repr(invalid)):
                    _assert_rejects(
                        lambda field=field, invalid=invalid: _request(
                            stage, **{field: invalid}
                        )
                    )

    def test_request_wrapper_requires_exact_identifiers_and_bare_digest(self) -> None:
        stage = _stage("PREPARE")
        invalid_identifier_values = (
            b"root-82",
            None,
            "",
            " root-82",
        )
        invalid_digest_values = (
            WRAPPED_DIGEST,
            "A" * 64,
            "a" * 63,
            "a" * 65,
        )
        for field in ("root_key", "verification_ref"):
            for invalid in invalid_identifier_values:
                with self.subTest(field=field, invalid=repr(invalid)):
                    _assert_rejects(
                        lambda field=field, invalid=invalid: _request(
                            stage, **{field: invalid}
                        )
                    )
        for invalid in invalid_digest_values:
            with self.subTest(field="request_digest", invalid=repr(invalid)):
                _assert_rejects(
                    lambda invalid=invalid: _request(stage, request_digest=invalid)
                )

    def test_request_wrapper_rejects_unsafe_text_and_stage_values(self) -> None:
        stage = _stage("PREPARE")
        for field, invalid in (
            ("root_key", "root-82-\ud800"),
            ("verification_ref", "verification-82-\udfff"),
            ("stage", "prepare"),
            ("stage", "PREPARE"),
            ("stage", object()),
        ):
            with self.subTest(field=field, invalid=repr(invalid)):
                if field == "stage":
                    _assert_rejects(lambda invalid=invalid: _request(invalid))
                else:
                    _assert_rejects(
                        lambda field=field, invalid=invalid: _request(
                            stage, **{field: invalid}
                        )
                    )

    def test_evidence_wrappers_match_each_stage_specific_oracle(self) -> None:
        cases: tuple[tuple[str, tuple[object, ...]], ...] = (
            ("PREPARE", (WRAPPED_DIGEST,)),
            ("RECEIPT", (BARE_DIGEST,)),
            ("TERMINAL", ("completed", "receipt-82", BARE_DIGEST)),
            ("UNKNOWN", ("runner-response-loss", WRAPPED_DIGEST, 7)),
        )
        for name, source in cases:
            with self.subTest(stage=name):
                stage = _stage(name)
                actual = _evidence(stage, *source)
                expected = _oracle(
                    EVIDENCE_DOMAINS[name],
                    _request_values(
                        stage,
                        (ROOT_KEY, VERIFICATION_REF, *SEQUENCES, *source),
                    ),
                )
                self.assertIs(type(actual), str)
                self.assertRegex(actual, WRAPPER_PATTERN)
                self.assertEqual(expected, actual)

    def test_evidence_domains_are_separate_from_request_and_each_other(self) -> None:
        stage = _stage("PREPARE")
        actual = _evidence(stage, WRAPPED_DIGEST)
        request = _request(stage)
        self.assertNotEqual(actual, request)
        self.assertNotEqual(
            actual,
            _oracle(
                b"agent-team/workflow-verification-receipt-evidence/v1\0",
                _request_values(
                    stage,
                    (ROOT_KEY, VERIFICATION_REF, *SEQUENCES, WRAPPED_DIGEST),
                ),
            ),
        )
        evidence_values = (
            _evidence(_stage("RECEIPT"), BARE_DIGEST),
            _evidence(_stage("TERMINAL"), "completed", "receipt-82", BARE_DIGEST),
            _evidence(_stage("UNKNOWN"), "runner-response-loss", WRAPPED_DIGEST, 7),
        )
        self.assertEqual(3, len(set(evidence_values)))

    def test_evidence_wrappers_are_deterministic_and_sensitive_to_identity_and_source(
        self,
    ) -> None:
        stage = _stage("TERMINAL")
        source = ("completed", "receipt-82", BARE_DIGEST)
        baseline = _evidence(stage, *source)
        self.assertEqual(baseline, _evidence(stage, *source))
        cases: tuple[tuple[str, tuple[object, ...]], ...] = (
            ("root", ("root-82-other", VERIFICATION_REF, *SEQUENCES, *source)),
            (
                "verification",
                (ROOT_KEY, "verification-82-other", *SEQUENCES, *source),
            ),
            ("workflow-before", (ROOT_KEY, VERIFICATION_REF, 3, 3, 4, 5, *source)),
            ("workflow-after", (ROOT_KEY, VERIFICATION_REF, 2, 4, 4, 5, *source)),
            ("task-before", (ROOT_KEY, VERIFICATION_REF, 2, 3, 5, 5, *source)),
            ("task-after", (ROOT_KEY, VERIFICATION_REF, 2, 3, 4, 6, *source)),
            (
                "phase",
                (
                    ROOT_KEY,
                    VERIFICATION_REF,
                    *SEQUENCES,
                    "verification_failed",
                    *source[1:],
                ),
            ),
            (
                "receipt-ref",
                (
                    ROOT_KEY,
                    VERIFICATION_REF,
                    *SEQUENCES,
                    source[0],
                    "receipt-other",
                    source[2],
                ),
            ),
            (
                "receipt-digest",
                (
                    ROOT_KEY,
                    VERIFICATION_REF,
                    *SEQUENCES,
                    source[0],
                    source[1],
                    "b" * 64,
                ),
            ),
        )
        for name, values in cases:
            with self.subTest(field=name):
                actual = _evidence_with_values(stage, values)
                self.assertNotEqual(
                    baseline,
                    actual,
                )

    def test_evidence_wrapper_enforces_closed_stage_source_arity(self) -> None:
        cases: tuple[str, tuple[object, ...]] = (
            ("PREPARE", (WRAPPED_DIGEST,)),
            ("RECEIPT", (BARE_DIGEST,)),
            ("TERMINAL", ("completed", "receipt-82", BARE_DIGEST)),
            ("UNKNOWN", ("runner-response-loss", WRAPPED_DIGEST, 7)),
        )
        for name, source in cases:
            stage = _stage(name)
            for invalid_source in (source[:-1], (*source, "extra")):
                with self.subTest(stage=name, source=invalid_source):
                    _assert_rejects(
                        lambda stage=stage, invalid_source=invalid_source: _evidence(
                            stage, *invalid_source
                        )
                    )

    def test_evidence_wrapper_rejects_wrong_stage_and_request_cross_use(self) -> None:
        _assert_rejects(lambda: _evidence("prepare", WRAPPED_DIGEST))
        _assert_rejects(lambda: _evidence(_stage("PREPARE"), BARE_DIGEST))
        _assert_rejects(lambda: _evidence(_stage("RECEIPT"), WRAPPED_DIGEST))
        _assert_rejects(lambda: _evidence(_stage("TERMINAL"), BARE_DIGEST))
        _assert_rejects(
            lambda: _evidence(_stage("UNKNOWN"), "runner-response-loss", BARE_DIGEST, 7)
        )
        _assert_rejects(
            lambda: _request(_stage("PREPARE"), request_digest=WRAPPED_DIGEST)
        )
        _assert_rejects(
            lambda: _request(
                _stage("PREPARE"),
                request_digest=_evidence(_stage("RECEIPT"), BARE_DIGEST),
            )
        )

    def test_evidence_wrapper_requires_exact_source_types_and_digest_namespaces(
        self,
    ) -> None:
        prepare = _stage("PREPARE")
        receipt = _stage("RECEIPT")
        terminal = _stage("TERMINAL")
        unknown = _stage("UNKNOWN")
        for invalid in (BARE_DIGEST, b"x" * 64, None, "A" * 64):
            with self.subTest(stage="prepare", invalid=repr(invalid)):
                _assert_rejects(lambda invalid=invalid: _evidence(prepare, invalid))
        for invalid in (WRAPPED_DIGEST, b"x" * 64, None, "A" * 64):
            with self.subTest(stage="receipt", invalid=repr(invalid)):
                _assert_rejects(lambda invalid=invalid: _evidence(receipt, invalid))
        for invalid in (WRAPPED_DIGEST, b"x" * 64, None, "A" * 64):
            with self.subTest(stage="terminal", invalid=repr(invalid)):
                _assert_rejects(
                    lambda invalid=invalid: _evidence(
                        terminal, "completed", "receipt-82", invalid
                    )
                )
        for invalid in (BARE_DIGEST, b"x" * 64, None, "sha256:" + "A" * 64):
            with self.subTest(stage="unknown", invalid=repr(invalid)):
                _assert_rejects(
                    lambda invalid=invalid: _evidence(
                        unknown, "runner-response-loss", invalid, 7
                    )
                )
        for invalid in (True, 0.5, "7", -1, 0, 2**63, None):
            with self.subTest(stage="unknown", effect_fence=repr(invalid)):
                _assert_rejects(
                    lambda invalid=invalid: _evidence(
                        unknown, "runner-response-loss", WRAPPED_DIGEST, invalid
                    )
                )

    def test_evidence_wrapper_rejects_unsafe_text_and_scalar_subclasses(self) -> None:
        class AliasStr(str):
            pass

        class AliasInt(int):
            pass

        _assert_rejects(
            lambda: _evidence(
                _stage("TERMINAL"), "completed", "receipt-\ud800", BARE_DIGEST
            )
        )
        _assert_rejects(
            lambda: _evidence(_stage("UNKNOWN"), "runner-\udfff", WRAPPED_DIGEST, 7)
        )
        _assert_rejects(
            lambda: _evidence(
                _stage("UNKNOWN"),
                "runner-response-loss",
                WRAPPED_DIGEST,
                AliasInt(7),
            )
        )
        _assert_rejects(
            lambda: _evidence(
                _stage("TERMINAL"), AliasStr("completed"), "receipt-82", BARE_DIGEST
            )
        )
        _assert_rejects(
            lambda: _evidence(
                _stage("TERMINAL"), "completed", AliasStr("receipt-82"), BARE_DIGEST
            )
        )
        _assert_rejects(
            lambda: _request(_stage("PREPARE"), root_key=AliasStr(ROOT_KEY))
        )
        _assert_rejects(
            lambda: _request(_stage("PREPARE"), request_digest=AliasStr(BARE_DIGEST))
        )


if __name__ == "__main__":
    unittest.main()
