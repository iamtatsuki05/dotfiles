from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_team.native_client_result import (
    MAX_RESULT_BYTES,
    RESULT_NAME,
    NativeClientReceipt,
    NativeClientResultError,
    parse_client_receipt,
    parse_client_receipt_json,
    read_client_result,
)

MODEL = "claude-sonnet"
EFFORT = "high"
NONCE = "planner1234"


def success_payload(
    *, output: str = "done", session_id: str = "session-1", **extra: object
) -> dict[str, object]:
    payload: dict[str, object] = {
        "output": output,
        "session_id": session_id,
        "model": MODEL,
        "effort": EFFORT,
        "cleanup_confirmed": True,
    }
    payload.update(extra)
    return payload


def failure_payload(
    *, error: str = "failed", session_id: str | None = None, **extra: object
) -> dict[str, object]:
    payload: dict[str, object] = {
        "error": error,
        "session_id": session_id,
        "model": MODEL,
        "effort": EFFORT,
        "cleanup_confirmed": False,
    }
    payload.update(extra)
    return payload


class NativeClientResultTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name).resolve() / "private"
        self.root.mkdir(mode=0o700)
        os.chmod(self.root, 0o700)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write_artifact(
        self,
        receipt: dict[str, object],
        *,
        nonce: str = NONCE,
        raw: bytes | None = None,
        mode: int = 0o600,
    ) -> Path:
        path = self.root / RESULT_NAME
        if raw is None:
            raw = json.dumps(
                {"version": 1, "launch_nonce": nonce, "receipt": receipt},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        path.write_bytes(raw)
        os.chmod(path, mode)
        return path

    def assert_invalid(
        self, callable_obj: object, *args: object, **kwargs: object
    ) -> None:
        with self.assertRaises(NativeClientResultError):
            callable_obj(*args, **kwargs)  # type: ignore[operator]

    def test_parse_success_receipt_returns_frozen_exact_shape(self) -> None:
        result = parse_client_receipt(success_payload(), model=MODEL, effort=EFFORT)

        self.assertIsInstance(result, NativeClientReceipt)
        self.assertTrue(result.succeeded)
        self.assertEqual(result.output, "done")
        self.assertIsNone(result.error)
        self.assertEqual(
            result.as_dict(),
            {
                "output": "done",
                "session_id": "session-1",
                "model": MODEL,
                "effort": EFFORT,
                "cleanup_confirmed": True,
            },
        )
        with self.assertRaises(AttributeError):
            result.output = "changed"  # type: ignore[misc]

    def test_parse_failure_receipt_accepts_null_session_and_false_cleanup(self) -> None:
        result = parse_client_receipt(
            failure_payload(error="cancelled"), model=MODEL, effort=EFFORT
        )

        self.assertFalse(result.succeeded)
        self.assertIsNone(result.output)
        self.assertEqual(result.error, "cancelled")
        self.assertEqual(
            result.as_dict(),
            {
                "error": "cancelled",
                "session_id": None,
                "model": MODEL,
                "effort": EFFORT,
                "cleanup_confirmed": False,
            },
        )
        confirmed = parse_client_receipt(
            failure_payload(session_id="session-2", cleanup_confirmed=True),
            model=MODEL,
            effort=EFFORT,
        )
        self.assertTrue(confirmed.cleanup_confirmed)

    def test_parse_client_receipt_json_validates_stdout_strictly(self) -> None:
        success = parse_client_receipt_json(
            json.dumps(success_payload()), model=MODEL, effort=EFFORT
        )
        self.assertTrue(success.succeeded)
        self.assertEqual(success.output, "done")

        failure = parse_client_receipt_json(
            json.dumps(failure_payload(error="cancelled")),
            model=MODEL,
            effort=EFFORT,
        )
        self.assertFalse(failure.succeeded)
        self.assertEqual(failure.error, "cancelled")

        invalid_json = (
            "\ufeff{}",
            (
                '{"output":"done","output":"other","session_id":"s",'
                '"model":"claude-sonnet","effort":"high",'
                '"cleanup_confirmed":true}'
            ),
            (
                '{"output":"done","session_id":"s","model":"claude-sonnet",'
                '"effort":"high","cleanup_confirmed":NaN}'
            ),
            (
                '{"output":"\\ud800","session_id":"s","model":"claude-sonnet",'
                '"effort":"high","cleanup_confirmed":true}'
            ),
        )
        for raw in invalid_json:
            with self.subTest(raw=raw):
                self.assert_invalid(
                    parse_client_receipt_json,
                    raw,
                    model=MODEL,
                    effort=EFFORT,
                )

    def test_read_success_and_failure_artifacts(self) -> None:
        self.write_artifact(success_payload())
        result = read_client_result(
            self.root, launch_nonce=NONCE, model=MODEL, effort=EFFORT
        )
        self.assertTrue(result.succeeded)

        self.write_artifact(failure_payload(session_id="session-2"))
        result = read_client_result(
            self.root, launch_nonce=NONCE, model=MODEL, effort=EFFORT
        )
        self.assertFalse(result.succeeded)
        self.assertEqual(result.session_id, "session-2")

    def test_envelope_and_receipt_require_exact_fields_and_matching_identity(
        self,
    ) -> None:
        receipt = success_payload()
        cases = (
            {"version": True, "launch_nonce": NONCE, "receipt": receipt},
            {"version": 1, "launch_nonce": "wrong", "receipt": receipt},
            {"version": 1, "launch_nonce": NONCE, "receipt": receipt, "extra": 1},
            {"version": 1, "launch_nonce": NONCE},
        )
        for envelope in cases:
            with self.subTest(envelope=envelope):
                self.write_artifact(receipt, raw=json.dumps(envelope).encode())
                self.assert_invalid(
                    read_client_result,
                    self.root,
                    launch_nonce=NONCE,
                    model=MODEL,
                    effort=EFFORT,
                )

        for invalid_receipt in (
            {**receipt, "extra": 1},
            {key: value for key, value in receipt.items() if key != "output"},
            {**receipt, "model": "other"},
            {**receipt, "effort": "low"},
        ):
            with self.subTest(receipt=invalid_receipt):
                self.assert_invalid(
                    parse_client_receipt,
                    invalid_receipt,
                    model=MODEL,
                    effort=EFFORT,
                )

    def test_output_and_error_boundaries_are_enforced(self) -> None:
        parse_client_receipt(
            success_payload(output="x" * 100_000), model=MODEL, effort=EFFORT
        )
        parse_client_receipt(
            failure_payload(error="x" * 4_000), model=MODEL, effort=EFFORT
        )
        for receipt in (
            success_payload(output="x" * 100_001),
            failure_payload(error="x" * 4_001),
        ):
            with self.subTest(receipt=receipt):
                self.assert_invalid(
                    parse_client_receipt, receipt, model=MODEL, effort=EFFORT
                )

        oversized = b"{" + b"x" * (MAX_RESULT_BYTES + 1)
        self.write_artifact(success_payload(), raw=oversized)
        self.assert_invalid(
            read_client_result,
            self.root,
            launch_nonce=NONCE,
            model=MODEL,
            effort=EFFORT,
        )

    def test_strict_json_rejects_unsafe_encoding_numbers_and_duplicate_keys(
        self,
    ) -> None:
        raw_cases = (
            b"\xef\xbb\xbf{}",
            b'{"version":1,"launch_nonce":"planner1234","receipt":{"output":\xff}}',
            b'{"version":1,"launch_nonce":"planner1234","receipt":{"output":"\\ud800","session_id":"s","model":"claude-sonnet","effort":"high","cleanup_confirmed":true}}',
            b'{"version":1,"launch_nonce":"planner1234","receipt":{"output":"done","session_id":"s","model":"claude-sonnet","effort":"high","cleanup_confirmed":true},"version":1}',
            b'{"version":1,"launch_nonce":"planner1234","receipt":{"output":"done","session_id":"s","model":"claude-sonnet","effort":"high","cleanup_confirmed":NaN}}',
            b'{"version":1,"launch_nonce":"planner1234","receipt":{"output":"done","session_id":"s","model":"claude-sonnet","effort":"high","cleanup_confirmed":Infinity}}',
        )
        for raw in raw_cases:
            with self.subTest(raw=raw):
                self.write_artifact(success_payload(), raw=raw)
                self.assert_invalid(
                    read_client_result,
                    self.root,
                    launch_nonce=NONCE,
                    model=MODEL,
                    effort=EFFORT,
                )

    def test_receipt_scalar_and_cleanup_contract_is_strict(self) -> None:
        invalid = (
            {**success_payload(), "output": "   "},
            {**success_payload(), "output": ""},
            {**success_payload(), "session_id": ""},
            {**success_payload(), "cleanup_confirmed": False},
            {**failure_payload(), "error": ""},
            {**failure_payload(), "error": "   "},
            {**failure_payload(), "session_id": ""},
            {**failure_payload(), "cleanup_confirmed": "false"},
            {**failure_payload(), "output": "wrong-kind"},
        )
        for receipt in invalid:
            with self.subTest(receipt=receipt):
                self.assert_invalid(
                    parse_client_receipt, receipt, model=MODEL, effort=EFFORT
                )

    def test_unsafe_root_and_result_types_are_rejected(self) -> None:
        self.write_artifact(success_payload())
        os.chmod(self.root, 0o755)
        self.assert_invalid(
            read_client_result,
            self.root,
            launch_nonce=NONCE,
            model=MODEL,
            effort=EFFORT,
        )
        os.chmod(self.root, 0o700)

        path = self.root / RESULT_NAME
        path.unlink()
        path.symlink_to(self.root / "outside")
        self.assert_invalid(
            read_client_result,
            self.root,
            launch_nonce=NONCE,
            model=MODEL,
            effort=EFFORT,
        )

        path.unlink()
        external = Path(self.tempdir.name) / "external"
        external.write_bytes(b"x")
        path.hardlink_to(external)
        os.chmod(path, 0o600)
        self.assert_invalid(
            read_client_result,
            self.root,
            launch_nonce=NONCE,
            model=MODEL,
            effort=EFFORT,
        )

        path.unlink()
        if hasattr(os, "mkfifo"):
            os.mkfifo(path, 0o600)
            self.assert_invalid(
                read_client_result,
                self.root,
                launch_nonce=NONCE,
                model=MODEL,
                effort=EFFORT,
            )

    def test_symlinked_root_is_rejected(self) -> None:
        real_root = Path(self.tempdir.name) / "real"
        real_root.mkdir(mode=0o700)
        os.chmod(real_root, 0o700)
        (real_root / RESULT_NAME).write_bytes(
            json.dumps(
                {"version": 1, "launch_nonce": NONCE, "receipt": success_payload()}
            ).encode()
        )
        os.chmod(real_root / RESULT_NAME, 0o600)
        link = Path(self.tempdir.name) / "link"
        link.symlink_to(real_root, target_is_directory=True)
        self.assert_invalid(
            read_client_result,
            link,
            launch_nonce=NONCE,
            model=MODEL,
            effort=EFFORT,
        )

    def test_result_replacement_during_read_is_rejected(self) -> None:
        self.write_artifact(success_payload())
        replacement = self.root.with_name("replacement-root")
        replacement.mkdir(mode=0o700)
        os.chmod(replacement, 0o700)
        (replacement / RESULT_NAME).write_bytes(
            json.dumps(
                {"version": 1, "launch_nonce": NONCE, "receipt": success_payload()}
            ).encode()
        )
        os.chmod(replacement / RESULT_NAME, 0o600)
        original_read = os.read
        replaced = False

        def replacing_read(fd: int, size: int) -> bytes:
            nonlocal replaced
            if not replaced:
                replaced = True
                os.replace(replacement / RESULT_NAME, self.root / RESULT_NAME)
                self.write_artifact(success_payload(output="new"))
            return original_read(fd, size)

        with patch("agent_team.native_client_result.os.read", replacing_read):
            self.assert_invalid(
                read_client_result,
                self.root,
                launch_nonce=NONCE,
                model=MODEL,
                effort=EFFORT,
            )

    def test_parent_replacement_during_read_is_rejected(self) -> None:
        self.write_artifact(success_payload())
        original_root = self.root
        old_root = self.root.with_name("old-root")
        new_root = self.root.with_name("new-root")
        original_read = os.read
        replaced = False

        def replacing_read(fd: int, size: int) -> bytes:
            nonlocal replaced
            if not replaced:
                replaced = True
                original_root.rename(old_root)
                new_root.mkdir(mode=0o700)
                os.chmod(new_root, 0o700)
                (new_root / RESULT_NAME).write_bytes(
                    json.dumps(
                        {
                            "version": 1,
                            "launch_nonce": NONCE,
                            "receipt": success_payload(output="new"),
                        }
                    ).encode()
                )
                os.chmod(new_root / RESULT_NAME, 0o600)
                new_root.rename(original_root)
            return original_read(fd, size)

        try:
            with patch("agent_team.native_client_result.os.read", replacing_read):
                self.assert_invalid(
                    read_client_result,
                    original_root,
                    launch_nonce=NONCE,
                    model=MODEL,
                    effort=EFFORT,
                )
        finally:
            self.root = original_root


if __name__ == "__main__":
    unittest.main()
