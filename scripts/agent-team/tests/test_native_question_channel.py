from __future__ import annotations

import json
import socket
import stat
import tempfile
import threading
import time
import unittest
from collections.abc import Callable, Mapping
from pathlib import Path
from unittest import mock

from agent_team import native_question_channel
from agent_team.native_question_channel import (
    MAX_FRAME_BYTES,
    QuestionCallbackError,
    QuestionChannel,
    QuestionChannelError,
    QuestionRequest,
    validate_question_request,
)


class NativeQuestionChannelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="agent-team-question-")
        self.root = Path(self.directory.name).resolve()
        self.private = self.root / "private"
        self.private.mkdir(mode=0o700)
        self.private.chmod(0o700)
        self.socket_path = self.private / "q.sock"
        self.channel: QuestionChannel | None = None
        self.failed: list[tuple[QuestionRequest | None, Exception]] = []
        self.delivered: list[QuestionRequest] = []
        self.recorded: list[QuestionRequest] = []
        self._failed_event = threading.Event()

    def tearDown(self) -> None:
        if self.channel is not None:
            try:
                self.channel.close()
            except QuestionChannelError:
                pass
        self.directory.cleanup()

    def _record_failure(
        self, request: QuestionRequest | None, error: Exception
    ) -> None:
        self.failed.append((request, error))
        self._failed_event.set()

    def _record_recorded(self, request: QuestionRequest) -> None:
        self.recorded.append(request)

    def _new_channel(
        self,
        exchange: Callable[[QuestionRequest, threading.Event], Mapping[str, str]],
    ) -> QuestionChannel:
        self.channel = QuestionChannel(
            self.socket_path,
            exchange,
            self.delivered.append,
            self._record_failure,
            recorded=self._record_recorded,
        )
        self.channel.start()
        return self.channel

    @staticmethod
    def _request(
        session_id: str, tool_call_id: str, body: str = "Need approval"
    ) -> dict[str, object]:
        return {
            "kind": "question",
            "session_id": session_id,
            "tool_call_id": tool_call_id,
            "questions": [{"field": "question_0_custom", "body": body}],
        }

    @staticmethod
    def _frame(value: object) -> bytes:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )

    @staticmethod
    def _recv_line(connection: socket.socket) -> bytes:
        connection.settimeout(3.0)
        data = bytearray()
        while not data.endswith(b"\n"):
            chunk = connection.recv(64 * 1024)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_FRAME_BYTES:
                raise AssertionError("test peer received an oversized frame")
        return bytes(data)

    def _send_request(
        self,
        value: object,
        *,
        receipt: Mapping[str, object] | None = None,
        expect_answer: bool = True,
    ) -> bytes:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(3.0)
            connection.connect(str(self.socket_path))
            if isinstance(value, bytes):
                connection.sendall(value)
            else:
                connection.sendall(self._frame(value))
            answer = self._recv_line(connection)
            if expect_answer:
                self.assertTrue(answer, "channel closed before answer")
                decoded = json.loads(answer)
                self.assertEqual(decoded.get("kind"), "answer")
                if receipt is None:
                    request = cast_request(value)
                    receipt = {
                        "kind": "received",
                        "session_id": request.session_id,
                        "tool_call_id": request.tool_call_id,
                    }
                connection.sendall(self._frame(receipt))
                recorded = json.loads(self._recv_line(connection))
                self.assertEqual(
                    recorded,
                    {
                        "kind": "recorded",
                        "session_id": receipt["session_id"],
                        "tool_call_id": receipt["tool_call_id"],
                    },
                )
            return answer

    def _wait_for(self, predicate: Callable[[], bool], timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("timed out waiting for channel state")
            time.sleep(0.01)

    def _close_with_capture(self, errors: list[Exception]) -> None:
        try:
            assert self.channel is not None
            self.channel.close()
        except (AssertionError, OSError, QuestionChannelError, RuntimeError) as error:
            errors.append(error)

    def test_real_request_response_receipt_and_owned_cleanup(self) -> None:
        calls: list[QuestionRequest] = []

        def exchange(
            request: QuestionRequest, stopped: threading.Event
        ) -> Mapping[str, str]:
            self.assertFalse(stopped.is_set())
            calls.append(request)
            return {request.questions[0].field: "approved"}

        channel = self._new_channel(exchange)
        answer = self._send_request(self._request("session-1", "tool-1"))

        self.assertEqual(
            json.loads(answer),
            {"kind": "answer", "answers": {"question_0_custom": "approved"}},
        )
        self.assertEqual([request.tool_call_id for request in calls], ["tool-1"])
        self.assertEqual(
            [request.tool_call_id for request in self.delivered], ["tool-1"]
        )
        self._wait_for(lambda: len(self.recorded) == 1)
        self.assertEqual(
            [request.tool_call_id for request in self.recorded], ["tool-1"]
        )
        self.assertEqual(self.failed, [])
        self.assertEqual(stat.S_IMODE(self.socket_path.stat().st_mode), 0o600)
        self.assertTrue(channel.is_running)

        channel.close()
        self.assertFalse(self.socket_path.exists())

    def test_queued_exchanges_are_serial_and_accept_only_one_pending_batch(
        self,
    ) -> None:
        first_started = threading.Event()
        release_first = threading.Event()
        calls: list[str] = []
        answers: dict[str, bytes] = {}

        def exchange(
            request: QuestionRequest, stopped: threading.Event
        ) -> Mapping[str, str]:
            calls.append(request.tool_call_id)
            if request.tool_call_id == "one":
                first_started.set()
                while not release_first.wait(0.01):
                    if stopped.is_set():
                        raise QuestionChannelError("first exchange stopped")
            return {request.questions[0].field: request.tool_call_id}

        self._new_channel(exchange)

        def run_client(tool_call_id: str) -> None:
            answers[tool_call_id] = self._send_request(
                self._request("session-queue", tool_call_id)
            )

        first = threading.Thread(target=run_client, args=("one",))
        second = threading.Thread(target=run_client, args=("two",))
        first.start()
        self.assertTrue(first_started.wait(3.0))
        second.start()
        time.sleep(0.15)
        self.assertEqual(calls, ["one"])
        release_first.set()
        first.join(3.0)
        second.join(3.0)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(calls, ["one", "two"])
        self.assertEqual(
            json.loads(answers["two"])["answers"], {"question_0_custom": "two"}
        )

    def test_strict_malformed_frames_are_rejected_before_a_valid_batch(self) -> None:
        def exchange(
            request: QuestionRequest, stopped: threading.Event
        ) -> Mapping[str, str]:
            return {request.questions[0].field: "ok"}

        self._new_channel(exchange)
        malformed = [
            b'{"kind":"question","kind":"question","session_id":"s","tool_call_id":"t","questions":[{"field":"question_0_custom","body":"x"}]}\n',
            self._frame(
                {
                    **self._request("s", "unknown"),
                    "extra": "reject",
                }
            ),
            b"\xff\n",
            b'{"kind":"question","session_id":"s","tool_call_id":"t-nan","questions":[{"field":"question_0_custom","body":NaN}]}\n',
            self._frame(self._request("s\n", "control")),
            self._frame(self._request("s\u0085", "c1-control")),
            self._frame(
                {
                    "kind": "question",
                    "session_id": "s",
                    "tool_call_id": "noncontiguous",
                    "questions": [{"field": "question_1_custom", "body": "x"}],
                }
            ),
            self._frame(self._request("s", "blank", " \t")),
            b'{"kind":"question","session_id":"s","tool_call_id":"missing","questions":}\n',
            b"{" + b"x" * MAX_FRAME_BYTES + b"\n",
        ]
        for frame in malformed:
            previous_channel = self.channel
            if previous_channel is not None:
                previous_channel.close()
            self.failed.clear()
            malformed_channel = self._new_channel(exchange)
            self._send_request(frame, expect_answer=False)
            self._wait_for(lambda: bool(self.failed))
            malformed_channel.close()
        self.failed.clear()
        valid_channel = self._new_channel(exchange)
        answer = self._send_request(self._request("s", "valid"))
        self.assertEqual(json.loads(answer)["answers"], {"question_0_custom": "ok"})
        self._wait_for(lambda: len(self.delivered) == 1)
        self.assertEqual(
            [request.tool_call_id for request in self.delivered], ["valid"]
        )
        valid_channel.close()

    def test_peer_disconnect_during_exchange_sets_request_stop_and_fails_channel(
        self,
    ) -> None:
        exchange_started = threading.Event()
        stopped_seen = threading.Event()

        def exchange(
            _request: QuestionRequest, stopped: threading.Event
        ) -> Mapping[str, str]:
            exchange_started.set()
            while not stopped.wait(0.01):
                pass
            stopped_seen.set()
            raise QuestionChannelError("peer disconnected")

        channel = self._new_channel(exchange)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(3.0)
            connection.connect(str(self.socket_path))
            connection.sendall(self._frame(self._request("session-disconnect", "tool")))
            self.assertTrue(exchange_started.wait(3.0))
        self.assertTrue(stopped_seen.wait(3.0))
        self._wait_for(lambda: bool(self.failed))
        self.assertEqual(self.delivered, [])
        self.assertIsNotNone(self.failed[0][0])
        self.assertFalse(channel.is_running)

    def test_mismatched_receipt_is_rejected_without_delivery(self) -> None:
        def exchange(
            request: QuestionRequest, stopped: threading.Event
        ) -> Mapping[str, str]:
            return {request.questions[0].field: "answer"}

        self._new_channel(exchange)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(3.0)
            connection.connect(str(self.socket_path))
            connection.sendall(self._frame(self._request("session-receipt", "tool")))
            answer = json.loads(self._recv_line(connection))
            self.assertEqual(answer["kind"], "answer")
            connection.sendall(
                self._frame(
                    {
                        "kind": "received",
                        "session_id": "session-receipt",
                        "tool_call_id": "other-tool",
                    }
                )
            )
        self._wait_for(lambda: bool(self.failed))
        self.assertEqual(self.delivered, [])
        self.assertEqual(
            self.failed[0][0].tool_call_id if self.failed[0][0] else None, "tool"
        )

    def test_recorded_send_failure_is_reported_after_durable_delivery(self) -> None:
        delivered_started = threading.Event()
        release_delivery = threading.Event()

        def exchange(
            request: QuestionRequest, stopped: threading.Event
        ) -> Mapping[str, str]:
            return {request.questions[0].field: "answer"}

        def delivered(request: QuestionRequest) -> None:
            self.delivered.append(request)
            delivered_started.set()
            release_delivery.wait(3.0)

        self.channel = QuestionChannel(
            self.socket_path,
            exchange,
            delivered,
            self._record_failure,
            recorded=self._record_recorded,
        )
        self.channel.start()
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(3.0)
        connection.connect(str(self.socket_path))
        connection.sendall(self._frame(self._request("session-recorded", "tool")))
        self.assertEqual(json.loads(self._recv_line(connection))["kind"], "answer")
        connection.sendall(
            self._frame(
                {
                    "kind": "received",
                    "session_id": "session-recorded",
                    "tool_call_id": "tool",
                }
            )
        )
        self.assertTrue(delivered_started.wait(3.0))
        connection.shutdown(socket.SHUT_RDWR)
        connection.close()
        release_delivery.set()
        self._wait_for(lambda: bool(self.failed))
        self.assertEqual([request.tool_call_id for request in self.delivered], ["tool"])
        self.assertEqual(self.recorded, [])
        self.assertEqual(
            self.failed[0][0].tool_call_id if self.failed[0][0] else None, "tool"
        )

    def test_recorded_callback_runs_only_after_recorded_frame_send(self) -> None:
        events: list[str] = []

        def delivered(_request: QuestionRequest) -> None:
            events.append("delivered")

        def recorded(_request: QuestionRequest) -> None:
            events.append("recorded-callback")

        original_send_frame = native_question_channel._send_frame

        def send_frame(
            connection: socket.socket,
            frame: bytes,
            stop: threading.Event,
        ) -> None:
            original_send_frame(connection, frame, stop)
            if b'"kind":"recorded"' in frame:
                events.append("recorded-send")

        channel = QuestionChannel(
            self.socket_path,
            lambda request, stopped: {request.questions[0].field: "answer"},
            delivered,
            self._record_failure,
            recorded=recorded,
        )
        self.channel = channel
        with (
            mock.patch.object(native_question_channel, "_send_frame", send_frame),
            socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection,
        ):
            connection.settimeout(3.0)
            channel.start()
            connection.connect(str(self.socket_path))
            connection.sendall(self._frame(self._request("session-order", "tool")))
            self.assertEqual(json.loads(self._recv_line(connection))["kind"], "answer")
            events.append("received")
            connection.sendall(
                self._frame(
                    {
                        "kind": "received",
                        "session_id": "session-order",
                        "tool_call_id": "tool",
                    }
                )
            )
            self.assertEqual(
                json.loads(self._recv_line(connection))["kind"], "recorded"
            )
        self._wait_for(
            lambda: (
                events
                == ["received", "delivered", "recorded-send", "recorded-callback"]
            )
        )
        self.assertEqual(self.failed, [])

    def test_delivery_callback_failure_stops_before_recorded_frame(self) -> None:
        delivered_calls = 0
        recorded_calls = 0

        def delivered(_request: QuestionRequest) -> None:
            nonlocal delivered_calls
            delivered_calls += 1
            raise KeyError("durable delivery failed")

        def recorded(_request: QuestionRequest) -> None:
            nonlocal recorded_calls
            recorded_calls += 1

        channel = QuestionChannel(
            self.socket_path,
            lambda request, stopped: {request.questions[0].field: "answer"},
            delivered,
            self._record_failure,
            recorded=recorded,
        )
        self.channel = channel
        channel.start()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(3.0)
            connection.connect(str(self.socket_path))
            connection.sendall(
                self._frame(self._request("session-delivery-error", "tool"))
            )
            self.assertEqual(json.loads(self._recv_line(connection))["kind"], "answer")
            connection.sendall(
                self._frame(
                    {
                        "kind": "received",
                        "session_id": "session-delivery-error",
                        "tool_call_id": "tool",
                    }
                )
            )
            self.assertEqual(self._recv_line(connection), b"")
        self._wait_for(lambda: bool(self.failed))
        error = self.failed[0][1]
        self.assertIsInstance(error, QuestionCallbackError)
        self.assertEqual(str(error), "question delivery callback failed")
        self.assertIsInstance(error.__cause__, KeyError)
        self.assertEqual(str(error.__cause__), "'durable delivery failed'")
        self.assertEqual(delivered_calls, 1)
        self.assertEqual(recorded_calls, 0)
        self.assertFalse(channel.is_running)

    def test_recorded_callback_failure_is_fatal_after_packet_send(self) -> None:
        recorded_called = threading.Event()
        recorded_calls = 0

        def recorded(_request: QuestionRequest) -> None:
            nonlocal recorded_calls
            recorded_calls += 1
            recorded_called.set()
            raise AssertionError("recorded state update failed")

        channel = QuestionChannel(
            self.socket_path,
            lambda request, stopped: {request.questions[0].field: "answer"},
            self.delivered.append,
            self._record_failure,
            recorded=recorded,
        )
        self.channel = channel
        channel.start()
        answer = self._send_request(self._request("session-recorded-error", "tool"))
        self.assertEqual(json.loads(answer)["kind"], "answer")
        self.assertTrue(recorded_called.wait(3.0))
        self._wait_for(lambda: bool(self.failed))
        self.assertEqual(recorded_calls, 1)
        error = self.failed[0][1]
        self.assertIsInstance(error, QuestionCallbackError)
        self.assertEqual(str(error), "question recorded callback failed")
        self.assertIsInstance(error.__cause__, AssertionError)
        self.assertEqual(str(error.__cause__), "recorded state update failed")
        self.assertFalse(channel.is_running)

    def test_exchange_callback_failure_does_not_send_answer(self) -> None:
        exchange_calls = 0

        def exchange(
            _request: QuestionRequest, _stopped: threading.Event
        ) -> Mapping[str, str]:
            nonlocal exchange_calls
            exchange_calls += 1
            raise KeyError("durable state failed")

        self._new_channel(exchange)
        answer = self._send_request(
            self._request("session-failure", "tool"), expect_answer=False
        )
        self.assertEqual(answer, b"")
        self._wait_for(lambda: bool(self.failed))
        self.assertEqual(exchange_calls, 1)
        error = self.failed[0][1]
        self.assertIsInstance(error, QuestionCallbackError)
        self.assertEqual(str(error), "question exchange callback failed")
        self.assertIsInstance(error.__cause__, KeyError)
        self.assertEqual(str(error.__cause__), "'durable state failed'")
        self.assertEqual(self.delivered, [])

    def test_close_cancels_active_exchange_and_removes_owned_endpoint(self) -> None:
        started = threading.Event()
        stopped = threading.Event()

        def exchange(
            request: QuestionRequest, request_stopped: threading.Event
        ) -> Mapping[str, str]:
            started.set()
            request_stopped.wait(3.0)
            stopped.set()
            return {request.questions[0].field: "should-not-send"}

        channel = self._new_channel(exchange)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(3.0)
        client.connect(str(self.socket_path))
        client.sendall(self._frame(self._request("session-close", "tool")))
        self.assertTrue(started.wait(3.0))
        channel.close()
        self.assertTrue(stopped.wait(3.0))
        self.assertEqual(self._recv_line(client), b"")
        client.close()
        self.assertFalse(self.socket_path.exists())

    def test_close_serializes_accept_registration_and_does_not_leave_peer(self) -> None:
        self._new_channel(lambda request, stopped: {request.questions[0].field: "ok"})
        close_entered = threading.Event()
        release_close = threading.Event()
        close_errors: list[Exception] = []
        original_close_listener = self.channel._close_listener  # type: ignore[union-attr]

        def delayed_close_listener() -> None:
            close_entered.set()
            self.assertTrue(release_close.wait(3.0))
            original_close_listener()

        with mock.patch.object(
            self.channel, "_close_listener", side_effect=delayed_close_listener
        ):
            close_thread = threading.Thread(
                target=lambda: self._close_with_capture(close_errors), daemon=True
            )
            close_thread.start()
            self.assertTrue(close_entered.wait(3.0))
            peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            peer.settimeout(3.0)
            peer.connect(str(self.socket_path))
            peer.close()
            release_close.set()
            close_thread.join(3.0)

        self.assertFalse(close_thread.is_alive())
        self.assertEqual(close_errors, [])
        self.assertFalse(self.socket_path.exists())

    def test_server_thread_start_failure_rolls_back_socket_and_close_is_safe(
        self,
    ) -> None:
        channel = QuestionChannel(
            self.socket_path,
            lambda request, stopped: {request.questions[0].field: "ok"},
            self.delivered.append,
            self._record_failure,
            recorded=self._record_recorded,
        )
        self.channel = channel
        with (
            mock.patch.object(
                threading.Thread, "start", side_effect=RuntimeError("start failed")
            ),
            self.assertRaisesRegex(RuntimeError, "start failed"),
        ):
            channel.start()
        channel.close()
        self.assertFalse(self.socket_path.exists())

    def test_exchange_worker_start_failure_fails_and_closes_channel(self) -> None:
        channel = self._new_channel(
            lambda request, stopped: {request.questions[0].field: "ok"}
        )
        original_start = threading.Thread.start

        def fail_exchange_worker(thread: threading.Thread) -> None:
            if thread.name == "agent-team-native-question-exchange":
                raise RuntimeError("worker start failed")
            original_start(thread)

        with mock.patch.object(threading.Thread, "start", fail_exchange_worker):
            answer = self._send_request(
                self._request("session-worker-start", "tool"), expect_answer=False
            )
        self.assertEqual(answer, b"")
        self._wait_for(lambda: bool(self.failed))
        self.assertIn("worker start failed", str(self.failed[0][1]))
        channel.close()
        self.assertFalse(self.socket_path.exists())

    def test_partial_frame_deadline_fails_channel(self) -> None:
        channel = self._new_channel(
            lambda request, stopped: {request.questions[0].field: "ok"}
        )
        with (
            mock.patch.object(native_question_channel, "_FRAME_TIMEOUT_SECONDS", 0.05),
            socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection,
        ):
            connection.settimeout(2.0)
            connection.connect(str(self.socket_path))
            connection.sendall(b"{")
            self.assertEqual(self._recv_line(connection), b"")
        self._wait_for(lambda: bool(self.failed))
        self.assertFalse(channel.is_running)

    def test_partial_receipt_deadline_fails_channel(self) -> None:
        channel = self._new_channel(
            lambda request, stopped: {request.questions[0].field: "ok"}
        )
        with mock.patch.object(native_question_channel, "_FRAME_TIMEOUT_SECONDS", 0.05):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(2.0)
                connection.connect(str(self.socket_path))
                connection.sendall(
                    self._frame(self._request("session-receipt-timeout", "tool"))
                )
                self.assertEqual(
                    json.loads(self._recv_line(connection))["kind"], "answer"
                )
                connection.sendall(b'{"kind":"received"')
                self.assertEqual(self._recv_line(connection), b"")
            self._wait_for(lambda: bool(self.failed))
        self.assertFalse(channel.is_running)

    def test_64th_success_is_allowed_and_65th_request_fails_explicitly(self) -> None:
        channel = self._new_channel(
            lambda request, stopped: {request.questions[0].field: "ok"}
        )
        for index in range(64):
            self._send_request(self._request("session-limit", f"tool-{index}"))
        self.assertTrue(channel.is_running)
        answer = self._send_request(
            self._request("session-limit", "tool-64"), expect_answer=False
        )
        self.assertEqual(answer, b"")
        self._wait_for(
            lambda: any(
                request is not None and request.tool_call_id == "tool-64"
                for request, _error in self.failed
            )
        )
        self.assertFalse(channel.is_running)
        channel.close()
        self.assertFalse(self.socket_path.exists())

    def test_duplicate_identity_is_rejected(self) -> None:
        def exchange(
            request: QuestionRequest, stopped: threading.Event
        ) -> Mapping[str, str]:
            return {request.questions[0].field: "ok"}

        self._new_channel(exchange)
        self._send_request(self._request("session-duplicate", "tool"))
        duplicate = self._send_request(
            self._request("session-duplicate", "tool"), expect_answer=False
        )
        self.assertEqual(duplicate, b"")
        self._wait_for(lambda: bool(self.failed))
        self.assertEqual(len(self.delivered), 1)

    def test_replaced_endpoint_is_never_removed(self) -> None:
        self._new_channel(lambda request, stopped: {request.questions[0].field: "ok"})
        self.socket_path.unlink()
        self.socket_path.write_text("replacement", encoding="utf-8")
        with self.assertRaises(QuestionChannelError) as context:
            self.channel.close()  # type: ignore[union-attr]
        self.assertFalse(context.exception.cleanup_confirmed)
        self.assertEqual(self.socket_path.read_text(encoding="utf-8"), "replacement")

    def test_replaced_parent_is_never_cleaned(self) -> None:
        self._new_channel(lambda request, stopped: {request.questions[0].field: "ok"})
        moved = self.root / "moved-private"
        self.private.rename(moved)
        self.private.mkdir(mode=0o700)
        self.private.chmod(0o700)
        with self.assertRaises(QuestionChannelError) as context:
            self.channel.close()  # type: ignore[union-attr]
        self.assertFalse(context.exception.cleanup_confirmed)
        self.assertTrue((moved / "q.sock").exists())
        self.assertFalse(self.socket_path.exists())

    def test_path_and_root_constraints_fail_before_start(self) -> None:
        self.socket_path.touch()
        with self.assertRaises(QuestionChannelError):
            QuestionChannel(
                self.socket_path,
                lambda request, stopped: {},
                self.delivered.append,
                self._record_failure,
                recorded=self._record_recorded,
            )
        self.socket_path.unlink()
        with self.assertRaises(QuestionChannelError):
            QuestionChannel(
                self.private / ("x" * 110) / "q.sock",
                lambda request, stopped: {},
                self.delivered.append,
                self._record_failure,
                recorded=self._record_recorded,
            )


def cast_request(value: object) -> QuestionRequest:
    return validate_question_request(value)


if __name__ == "__main__":
    unittest.main()
