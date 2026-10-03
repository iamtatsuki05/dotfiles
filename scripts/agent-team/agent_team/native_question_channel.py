"""Bounded Unix transport for native ACP question batches.

The channel deliberately owns only the ephemeral socket and the wire
validation.  Durable question state, answer persistence, and the lifecycle of
the ACP assignment remain in the caller supplied callbacks.
"""

from __future__ import annotations

import json
import os
import select
import socket
import stat
import threading
import time
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NoReturn, ParamSpec, Self, TypeAlias, TypeVar, cast

MAX_FRAME_BYTES: Final = 512 * 1024
MAX_QUESTIONS: Final = 4
MAX_TEXT_CHARS: Final = 20_000
MAX_ID_CHARS: Final = 256
MAX_BATCHES: Final = 64
MAX_SOCKET_PATH_BYTES: Final = 103
SOCKET_NAME: Final = "q.sock"
_SOCKET_WAIT_SECONDS: Final = 0.1
_FRAME_TIMEOUT_SECONDS: Final = 5.0
_CLOSE_TIMEOUT_SECONDS: Final = 2.0


class QuestionChannelError(RuntimeError):
    """A channel or ownership failure.

    ``cleanup_confirmed`` is true only after the server thread has stopped and
    the exact socket inode owned by this channel has been removed.  Callers
    must retain their durable state when it is false.
    """

    def __init__(self, message: str, *, cleanup_confirmed: bool = False) -> None:
        super().__init__(message)
        self.cleanup_confirmed = cleanup_confirmed


class QuestionCallbackError(QuestionChannelError):
    """A callback raised an ordinary exception at the channel boundary."""


class QuestionValidationError(QuestionChannelError, ValueError):
    """A typed request, answer, receipt, or path failed validation."""


class QuestionProtocolError(QuestionChannelError):
    """A peer sent a malformed or out-of-order frame."""


class QuestionOwnershipError(QuestionChannelError):
    """A private root or endpoint no longer has the expected identity."""


@dataclass(frozen=True, slots=True)
class QuestionField:
    """One custom question in an ACP form batch."""

    field: str
    body: str

    def __post_init__(self) -> None:
        _validate_field(self.field)
        _validate_body(self.body, "question body")

    def as_dict(self) -> dict[str, str]:
        return {"field": self.field, "body": self.body}


@dataclass(frozen=True, slots=True)
class QuestionRequest:
    """Immutable identity and fields for one native ACP question batch."""

    session_id: str
    tool_call_id: str
    questions: tuple[QuestionField, ...]

    def __post_init__(self) -> None:
        _validate_identifier(self.session_id, "session_id")
        _validate_identifier(self.tool_call_id, "tool_call_id")
        if not isinstance(self.questions, tuple):
            raise QuestionValidationError("questions must be a tuple")
        _validate_question_fields(self.questions)

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": "question",
            "session_id": self.session_id,
            "tool_call_id": self.tool_call_id,
            "questions": [question.as_dict() for question in self.questions],
        }


ExchangeCallback: TypeAlias = Callable[
    [QuestionRequest, threading.Event], Mapping[str, str]
]
DeliveredCallback: TypeAlias = Callable[[QuestionRequest], None]
FailedCallback: TypeAlias = Callable[[QuestionRequest | None, Exception], None]
RecordedCallback: TypeAlias = Callable[[QuestionRequest], None]
_CallbackParams = ParamSpec("_CallbackParams")
_CallbackResult = TypeVar("_CallbackResult")


@dataclass(frozen=True, slots=True)
class _PathIdentity:
    device: int
    inode: int
    mode: int
    uid: int
    gid: int


class _PeerDisconnected(Exception):
    """Internal marker for an orderly peer disconnect."""


class _ChannelClosed(Exception):
    """Internal marker for caller initiated channel shutdown."""


def _error(message: str) -> QuestionValidationError:
    return QuestionValidationError(message)


def _validate_text(value: object, context: str, *, max_chars: int) -> str:
    if not isinstance(value, str):
        raise _error(f"{context} must be a string")
    if not value:
        raise _error(f"{context} must be non-empty")
    if len(value) > max_chars:
        raise _error(f"{context} exceeds {max_chars} characters")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise _error(f"{context} must not contain surrogate characters")
    return value


def _validate_identifier(value: object, context: str) -> str:
    text = _validate_text(value, context, max_chars=MAX_ID_CHARS)
    if any(
        character == "\x00"
        or ord(character) < 0x20
        or ord(character) == 0x7F
        or unicodedata.category(character) == "Cc"
        for character in text
    ):
        raise _error(f"{context} contains a control character")
    return text


def _validate_body(value: object, context: str) -> str:
    text = _validate_text(value, context, max_chars=MAX_TEXT_CHARS)
    if not text.strip():
        raise _error(f"{context} must not be whitespace-only")
    if "\x00" in text:
        raise _error(f"{context} must not contain NUL")
    return text


def _validate_field(value: object) -> str:
    if not isinstance(value, str):
        raise _error("question field must be a string")
    if value not in {f"question_{index}_custom" for index in range(MAX_QUESTIONS)}:
        raise _error("question field must be question_N_custom")
    return value


def _validate_question_fields(questions: tuple[QuestionField, ...]) -> None:
    if not 1 <= len(questions) <= MAX_QUESTIONS:
        raise _error(f"questions must contain 1 to {MAX_QUESTIONS} items")
    for index, question in enumerate(questions):
        if not isinstance(question, QuestionField):
            raise _error("questions must contain QuestionField values")
        expected = f"question_{index}_custom"
        if question.field != expected:
            raise _error("question fields must be contiguous question_N_custom names")


def _validate_exact_keys(value: Mapping[object, object], expected: set[str]) -> None:
    keys = set(value)
    if any(not isinstance(key, str) for key in keys):
        raise _error("JSON object keys must be strings")
    string_keys = cast(set[str], keys)
    unknown = string_keys - expected
    missing = expected - string_keys
    if unknown:
        raise _error(f"unknown fields: {', '.join(sorted(unknown))}")
    if missing:
        raise _error(f"missing fields: {', '.join(sorted(missing))}")


def validate_question_request(value: object) -> QuestionRequest:
    """Validate a wire-shaped mapping and return the immutable request type."""

    if isinstance(value, QuestionRequest):
        return QuestionRequest(value.session_id, value.tool_call_id, value.questions)
    if not isinstance(value, Mapping):
        raise _error("question request must be an object")
    mapping = cast(Mapping[object, object], value)
    _validate_exact_keys(mapping, {"kind", "session_id", "tool_call_id", "questions"})
    if mapping.get("kind") != "question":
        raise _error("question request kind must be question")
    session_id = _validate_identifier(mapping.get("session_id"), "session_id")
    tool_call_id = _validate_identifier(mapping.get("tool_call_id"), "tool_call_id")
    raw_questions = mapping.get("questions")
    if not isinstance(raw_questions, (list, tuple)):
        raise _error("questions must be an array")
    fields: list[QuestionField] = []
    for raw_question in raw_questions:
        if not isinstance(raw_question, Mapping):
            raise _error("question entries must be objects")
        question_mapping = cast(Mapping[object, object], raw_question)
        _validate_exact_keys(question_mapping, {"field", "body"})
        field = _validate_field(question_mapping.get("field"))
        body = _validate_body(question_mapping.get("body"), "question body")
        fields.append(QuestionField(field, body))
    return QuestionRequest(session_id, tool_call_id, tuple(fields))


def validate_answers(request: QuestionRequest, value: object) -> dict[str, str]:
    """Validate the exact answer mapping for ``request``."""

    request = validate_question_request(request)
    if not isinstance(value, Mapping):
        raise _error("answers must be an object")
    mapping = cast(Mapping[object, object], value)
    expected = {question.field for question in request.questions}
    _validate_exact_keys(mapping, expected)
    answers: dict[str, str] = {}
    for question in request.questions:
        answer = _validate_body(mapping.get(question.field), "answer")
        answers[question.field] = answer
    return answers


def _reject_duplicate_keys(pairs: list[tuple[object, object]]) -> dict[object, object]:
    result: dict[object, object] = {}
    for key, value in pairs:
        if key in result:
            raise QuestionProtocolError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> NoReturn:
    raise QuestionProtocolError(f"non-finite JSON value {value} is not allowed")


def _decode_frame(raw: bytes) -> object:
    if len(raw) > MAX_FRAME_BYTES:
        raise QuestionProtocolError("JSON frame exceeds 512 KiB")
    if not raw.endswith(b"\n"):
        raise QuestionProtocolError("JSON frame must end with newline")
    payload = raw[:-1]
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise QuestionProtocolError("JSON frame is not valid UTF-8") from exc
    if not text or text != text.strip(" \t\r"):
        raise QuestionProtocolError("JSON frame has invalid surrounding whitespace")
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except QuestionProtocolError:
        raise
    except (TypeError, ValueError) as exc:
        raise QuestionProtocolError("JSON frame is invalid") from exc


def _encode_frame(value: Mapping[str, object]) -> bytes:
    try:
        encoded = (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, UnicodeError, ValueError) as exc:
        raise QuestionProtocolError("JSON frame cannot be encoded") from exc
    if len(encoded) > MAX_FRAME_BYTES:
        raise QuestionProtocolError("JSON frame exceeds 512 KiB")
    return encoded


def _path_identity(path: Path, *, expected_kind: int) -> _PathIdentity:
    try:
        info = path.lstat()
    except (OSError, ValueError) as exc:
        raise QuestionOwnershipError(f"cannot inspect owned path {path}") from exc
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_IFMT(info.st_mode) != expected_kind:
        raise QuestionOwnershipError(f"owned path {path} has the wrong kind")
    if info.st_uid != os.getuid():
        raise QuestionOwnershipError(f"owned path {path} has the wrong owner")
    return _PathIdentity(info.st_dev, info.st_ino, mode, info.st_uid, info.st_gid)


def _same_identity(first: _PathIdentity, second: _PathIdentity) -> bool:
    return first == second


def _as_exception(error: BaseException, fallback: str) -> Exception:
    if isinstance(error, Exception):
        return error
    return QuestionChannelError(fallback)


def _invoke_callback(
    failure_message: str,
    callback: Callable[_CallbackParams, _CallbackResult],
    *args: _CallbackParams.args,
    **kwargs: _CallbackParams.kwargs,
) -> _CallbackResult:
    try:
        return callback(*args, **kwargs)
    except Exception as exc:
        raise QuestionCallbackError(failure_message) from exc


def _read_line(
    connection: socket.socket,
    stop: threading.Event,
    *,
    timeout_seconds: float | None = None,
) -> bytes:
    data = bytearray()
    timeout = _FRAME_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    deadline = time.monotonic() + timeout
    while True:
        if stop.is_set():
            raise _ChannelClosed
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise QuestionChannelError("question frame read timed out")
        try:
            readable, _, _ = select.select(
                [connection], [], [], min(_SOCKET_WAIT_SECONDS, remaining)
            )
        except (OSError, ValueError) as exc:
            if stop.is_set():
                raise _ChannelClosed from exc
            raise QuestionChannelError("question peer became unavailable") from exc
        if not readable:
            continue
        try:
            chunk = connection.recv(min(16 * 1024, MAX_FRAME_BYTES + 1 - len(data)))
        except BlockingIOError:
            continue
        except OSError as exc:
            if stop.is_set():
                raise _ChannelClosed from exc
            raise QuestionChannelError("question peer read failed") from exc
        if not chunk:
            if not data:
                raise _PeerDisconnected
            raise QuestionProtocolError("peer closed a partial JSON frame")
        data.extend(chunk)
        if len(data) > MAX_FRAME_BYTES:
            raise QuestionProtocolError("JSON frame exceeds 512 KiB")
        newline = data.find(b"\n")
        if newline < 0:
            continue
        if newline != len(data) - 1:
            raise QuestionProtocolError("multiple JSON frames are not allowed")
        return bytes(data)


def _send_frame(
    connection: socket.socket,
    frame: bytes,
    stop: threading.Event,
    *,
    timeout_seconds: float | None = None,
) -> None:
    offset = 0
    timeout = _FRAME_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    deadline = time.monotonic() + timeout
    while offset < len(frame):
        if stop.is_set():
            raise _ChannelClosed
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise QuestionChannelError("question frame write timed out")
        try:
            _, writable, _ = select.select(
                [], [connection], [], min(_SOCKET_WAIT_SECONDS, remaining)
            )
        except (OSError, ValueError) as exc:
            if stop.is_set():
                raise _ChannelClosed from exc
            raise QuestionChannelError("question peer became unavailable") from exc
        if not writable:
            continue
        try:
            sent = connection.send(frame[offset:])
        except BlockingIOError:
            continue
        except OSError as exc:
            if stop.is_set():
                raise _ChannelClosed from exc
            raise QuestionChannelError("question peer write failed") from exc
        if sent <= 0:
            raise _PeerDisconnected
        offset += sent


def _peer_has_data_or_closed(connection: socket.socket) -> bool:
    """Return true when the peer closed or sent an unexpected frame."""

    try:
        readable, _, _ = select.select([connection], [], [], _SOCKET_WAIT_SECONDS)
    except (OSError, ValueError) as exc:
        raise QuestionChannelError("question peer became unavailable") from exc
    if not readable:
        return False
    try:
        data = connection.recv(1, socket.MSG_PEEK)
    except BlockingIOError:
        return False
    except OSError as exc:
        raise QuestionChannelError("question peer read failed") from exc
    if not data:
        raise _PeerDisconnected
    raise QuestionProtocolError("unexpected data before answer")


class QuestionChannel:
    """Own one bounded private Unix question endpoint."""

    def __init__(
        self,
        socket_path: Path,
        exchange: ExchangeCallback,
        delivered: DeliveredCallback,
        failed: FailedCallback,
        *,
        recorded: RecordedCallback,
    ) -> None:
        if os.name != "posix" or not hasattr(socket, "AF_UNIX"):
            raise QuestionChannelError("native question channels require POSIX AF_UNIX")
        if not isinstance(socket_path, Path):
            raise QuestionValidationError("question socket path must be a Path")
        self._socket_path = socket_path
        self._validate_socket_layout()
        if not callable(exchange) or not callable(delivered) or not callable(failed):
            raise QuestionValidationError("question channel callbacks must be callable")
        if not callable(recorded):
            raise QuestionValidationError("question recorded callback must be callable")
        self._exchange = exchange
        self._delivered = delivered
        self._failed = failed
        self._recorded = recorded
        self._root_identity = _path_identity(
            self._socket_path.parent, expected_kind=stat.S_IFDIR
        )
        if self._root_identity.mode != 0o700:
            raise QuestionOwnershipError(
                "question socket parent must be an owned mode-0700 directory"
            )
        self._listener: socket.socket | None = None
        self._endpoint_identity: _PathIdentity | None = None
        self._server_thread: threading.Thread | None = None
        self._server_thread_started = False
        self._active_worker: threading.Thread | None = None
        self._active_connection: socket.socket | None = None
        self._active_stop: threading.Event | None = None
        self._lifecycle_lock = threading.RLock()
        self._stop = threading.Event()
        self._started = False
        self._closed = False
        self._failure: Exception | None = None
        self._seen: set[tuple[str, str]] = set()
        self._batch_count = 0

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    @property
    def failure(self) -> Exception | None:
        with self._lifecycle_lock:
            return self._failure

    @property
    def is_running(self) -> bool:
        thread = self._server_thread
        return thread is not None and thread.is_alive() and not self._stop.is_set()

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def _validate_socket_layout(self) -> None:
        path = self._socket_path
        if not path.is_absolute() or path.name != SOCKET_NAME:
            raise QuestionValidationError(
                "question socket path must be an absolute canonical q.sock"
            )
        try:
            encoded = str(path).encode("utf-8")
        except (UnicodeError, ValueError) as exc:
            raise QuestionValidationError(
                "question socket path is not valid UTF-8"
            ) from exc
        if len(encoded) > MAX_SOCKET_PATH_BYTES:
            raise QuestionValidationError(
                "question socket path exceeds 103 UTF-8 bytes"
            )
        try:
            canonical = path.resolve(strict=False)
            parent = path.parent.resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise QuestionValidationError(
                "question socket path is not canonical"
            ) from exc
        if canonical != path or parent != path.parent:
            raise QuestionValidationError("question socket path must be canonical")
        try:
            path.lstat()
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            raise QuestionOwnershipError(
                "question socket path cannot be inspected"
            ) from exc
        raise QuestionOwnershipError(
            "question socket path already exists; refusing to take it over"
        )

    def _verify_parent(self) -> None:
        current = _path_identity(self._socket_path.parent, expected_kind=stat.S_IFDIR)
        if not _same_identity(current, self._root_identity) or current.mode != 0o700:
            raise QuestionOwnershipError("question socket parent identity changed")

    def _verify_endpoint(self) -> None:
        expected = self._endpoint_identity
        if expected is None:
            raise QuestionOwnershipError(
                "question socket endpoint identity is unavailable"
            )
        current = _path_identity(self._socket_path, expected_kind=stat.S_IFSOCK)
        if current.mode != 0o600 or not _same_identity(current, expected):
            raise QuestionOwnershipError("question socket endpoint identity changed")

    def _verify_owned_paths(self) -> None:
        self._verify_parent()
        self._verify_endpoint()

    def _assert_absent(self) -> None:
        try:
            self._socket_path.lstat()
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            raise QuestionOwnershipError(
                "question socket path cannot be inspected"
            ) from exc
        raise QuestionOwnershipError(
            "question socket path already exists; refusing to take it over"
        )

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._started:
                raise QuestionChannelError("question channel has already started")
            if self._closed:
                raise QuestionChannelError("question channel is closed")
            self._verify_parent()
            self._assert_absent()
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                listener.bind(str(self._socket_path))
                self._endpoint_identity = _path_identity(
                    self._socket_path, expected_kind=stat.S_IFSOCK
                )
                os.chmod(self._socket_path, 0o600)
                self._endpoint_identity = _path_identity(
                    self._socket_path, expected_kind=stat.S_IFSOCK
                )
                if self._endpoint_identity.mode != 0o600:
                    raise QuestionOwnershipError("question socket is not mode 0600")
                listener.listen(1)
                listener.setblocking(False)
            except (OSError, ValueError, QuestionChannelError):
                listener.close()
                if self._endpoint_identity is not None:
                    try:
                        self._remove_owned_endpoint()
                    except QuestionChannelError as cleanup_error:
                        raise QuestionChannelError(
                            "question channel setup cleanup is unconfirmed",
                            cleanup_confirmed=False,
                        ) from cleanup_error
                raise
            self._listener = listener
            self._started = True
            self._server_thread = threading.Thread(
                target=self._serve,
                name="agent-team-native-question-channel",
                daemon=True,
            )
            self._server_thread_started = True
            try:
                self._server_thread.start()
            except (OSError, RuntimeError) as start_error:
                self._server_thread_started = False
                self._stop.set()
                self._close_listener()
                if self._server_thread.is_alive():
                    self._server_thread.join(_CLOSE_TIMEOUT_SECONDS)
                if self._server_thread.is_alive():
                    error = QuestionChannelError(
                        "question channel server thread did not stop",
                        cleanup_confirmed=False,
                    )
                    self._failure = error
                    raise error from start_error
                try:
                    self._remove_owned_endpoint()
                except QuestionChannelError as cleanup_error:
                    self._failure = cleanup_error
                    raise QuestionChannelError(
                        "question channel startup cleanup is unconfirmed",
                        cleanup_confirmed=False,
                    ) from start_error
                self._started = False
                self._closed = True
                self._server_thread = None
                self._endpoint_identity = None
                raise

    def _close_connection(self, connection: socket.socket | None) -> None:
        if connection is None:
            return
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            connection.close()
        except OSError:
            pass

    def _close_listener(self) -> None:
        with self._lifecycle_lock:
            listener = self._listener
            self._listener = None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass

    def _notify_failed(self, request: QuestionRequest | None, error: Exception) -> None:
        with self._lifecycle_lock:
            self._failure = error
        try:
            _invoke_callback(
                "question failure callback failed",
                self._failed,
                request,
                error,
            )
        except QuestionCallbackError as callback_error:
            with self._lifecycle_lock:
                self._failure = callback_error
            self._stop.set()

    def _fail_channel(
        self, error: Exception, request: QuestionRequest | None = None
    ) -> None:
        self._notify_failed(request, error)
        self._stop.set()
        active_stop = self._active_stop
        if active_stop is not None:
            active_stop.set()
        self._close_connection(self._active_connection)
        self._close_listener()

    def _serve(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self._verify_owned_paths()
                except QuestionChannelError as exc:
                    self._fail_channel(exc)
                    break
                listener = self._listener
                if listener is None:
                    break
                try:
                    readable, _, _ = select.select(
                        [listener], [], [], _SOCKET_WAIT_SECONDS
                    )
                except (OSError, ValueError):
                    if self._stop.is_set():
                        break
                    self._fail_channel(
                        QuestionChannelError("question listener became unavailable")
                    )
                    break
                if not readable:
                    continue
                try:
                    connection, _ = listener.accept()
                    connection.setblocking(False)
                except BlockingIOError:
                    continue
                except OSError:
                    if self._stop.is_set():
                        break
                    self._fail_channel(
                        QuestionChannelError("question listener accept failed")
                    )
                    break
                with self._lifecycle_lock:
                    reject_connection = (
                        self._stop.is_set() or self._listener is not listener
                    )
                    if not reject_connection:
                        self._active_connection = connection
                if reject_connection:
                    self._close_connection(connection)
                    break
                try:
                    self._verify_owned_paths()
                    self._handle_connection(connection)
                except QuestionChannelError as exc:
                    if not self._stop.is_set():
                        self._fail_channel(exc)
                except (OSError, ValueError, RuntimeError) as exc:
                    if not self._stop.is_set():
                        self._fail_channel(
                            _as_exception(exc, "question channel failed")
                        )
                finally:
                    self._close_connection(connection)
                    with self._lifecycle_lock:
                        self._active_connection = None
                        self._active_stop = None
                        if (
                            self._active_worker is None
                            or not self._active_worker.is_alive()
                        ):
                            self._active_worker = None
        finally:
            self._close_connection(self._active_connection)
            self._close_listener()
            with self._lifecycle_lock:
                self._server_thread_started = False

    def _handle_connection(self, connection: socket.socket) -> None:
        request: QuestionRequest | None = None
        request_stop = threading.Event()
        with self._lifecycle_lock:
            if self._stop.is_set():
                request_stop.set()
                return
            self._active_stop = request_stop
        try:
            frame = _read_line(connection, request_stop)
            decoded = _decode_frame(frame)
            request = validate_question_request(decoded)
            key = (request.session_id, request.tool_call_id)
            if key in self._seen:
                error = QuestionProtocolError(
                    "duplicate session_id/tool_call_id question is not allowed"
                )
                self._fail_channel(error, request)
                return
            if self._batch_count >= MAX_BATCHES:
                self._fail_channel(
                    QuestionProtocolError("question batch limit exceeded"), request
                )
                return
            self._seen.add(key)
            self._batch_count += 1
        except _PeerDisconnected:
            if not self._stop.is_set():
                self._fail_channel(
                    QuestionProtocolError("peer disconnected before question frame")
                )
            return
        except _ChannelClosed:
            return
        except (
            QuestionChannelError,
            TypeError,
            ValueError,
            OSError,
            UnicodeError,
        ) as exc:
            self._fail_channel(_as_exception(exc, "question frame failed"))
            return

        result: list[tuple[Mapping[str, str] | None, Exception | None]] = []

        valid_request = request

        def invoke_exchange() -> None:
            try:
                result.append(
                    (
                        _invoke_callback(
                            "question exchange callback failed",
                            self._exchange,
                            valid_request,
                            request_stop,
                        ),
                        None,
                    )
                )
            except QuestionCallbackError as callback_error:
                result.append((None, callback_error))

        try:
            worker = threading.Thread(
                target=invoke_exchange,
                name="agent-team-native-question-exchange",
                daemon=True,
            )
        except (OSError, RuntimeError) as exc:
            self._fail_channel(
                _as_exception(exc, "question exchange worker creation failed"), request
            )
            return
        worker_start_error: Exception | None = None
        with self._lifecycle_lock:
            if self._stop.is_set():
                request_stop.set()
                return
            self._active_worker = worker
            try:
                worker.start()
            except (OSError, RuntimeError) as exc:
                self._active_worker = None
                request_stop.set()
                worker_start_error = _as_exception(
                    exc, "question exchange worker start failed"
                )
        if worker_start_error is not None:
            self._fail_channel(worker_start_error, request)
            return
        request_failed = False
        failure_error: Exception | None = None
        while worker.is_alive():
            if self._stop.is_set():
                request_stop.set()
                request_failed = True
                failure_error = QuestionChannelError("question channel stopped")
                break
            try:
                if _peer_has_data_or_closed(connection):
                    request_stop.set()
                    request_failed = True
                    failure_error = QuestionProtocolError(
                        "peer disconnected or sent data before answer"
                    )
                    break
            except (
                _PeerDisconnected,
                QuestionChannelError,
                OSError,
                ValueError,
            ) as exc:
                request_stop.set()
                request_failed = True
                failure_error = _as_exception(exc, "question peer monitoring failed")
                break
        if request_failed:
            assert request is not None
            self._fail_channel(
                failure_error or QuestionChannelError("question failed"), request
            )
            worker.join(_CLOSE_TIMEOUT_SECONDS)
            return
        worker.join()
        if not result:
            exchange_missing_error = QuestionChannelError(
                "question exchange did not return"
            )
            self._fail_channel(exchange_missing_error, request)
            return
        answers_value, exchange_error = result[0]
        if exchange_error is not None:
            self._fail_channel(exchange_error, request)
            return
        if request_stop.is_set() or self._stop.is_set():
            self._fail_channel(QuestionChannelError("question was stopped"), request)
            return
        try:
            answers = validate_answers(request, answers_value)
            _send_frame(
                connection,
                _encode_frame({"kind": "answer", "answers": answers}),
                request_stop,
            )
            receipt_frame = _read_line(connection, request_stop)
            receipt = _decode_frame(receipt_frame)
            if not isinstance(receipt, Mapping):
                raise QuestionProtocolError("received receipt must be an object")
            receipt_mapping = cast(Mapping[object, object], receipt)
            _validate_exact_keys(
                receipt_mapping, {"kind", "session_id", "tool_call_id"}
            )
            if (
                receipt_mapping.get("kind") != "received"
                or receipt_mapping.get("session_id") != request.session_id
                or receipt_mapping.get("tool_call_id") != request.tool_call_id
            ):
                raise QuestionProtocolError("received receipt identity does not match")
            _validate_identifier(receipt_mapping.get("session_id"), "session_id")
            _validate_identifier(receipt_mapping.get("tool_call_id"), "tool_call_id")
        except (_PeerDisconnected, _ChannelClosed):
            self._fail_channel(
                QuestionChannelError("peer disconnected before receipt"), request
            )
            return
        except (
            QuestionChannelError,
            TypeError,
            ValueError,
            OSError,
            UnicodeError,
        ) as exc:
            self._fail_channel(_as_exception(exc, "question receipt failed"), request)
            return
        try:
            _invoke_callback(
                "question delivery callback failed",
                self._delivered,
                request,
            )
        except QuestionCallbackError as callback_error:
            self._fail_channel(callback_error, request)
            return
        try:
            _send_frame(
                connection,
                _encode_frame(
                    {
                        "kind": "recorded",
                        "session_id": request.session_id,
                        "tool_call_id": request.tool_call_id,
                    }
                ),
                request_stop,
            )
        except (_PeerDisconnected, _ChannelClosed):
            self._fail_channel(QuestionChannelError("recorded receipt failed"), request)
            return
        except (QuestionChannelError, OSError, ValueError, UnicodeError) as exc:
            self._fail_channel(_as_exception(exc, "recorded receipt failed"), request)
            return
        try:
            _invoke_callback(
                "question recorded callback failed",
                self._recorded,
                request,
            )
        except QuestionCallbackError as callback_error:
            self._fail_channel(callback_error, request)

    def _remove_owned_endpoint(self) -> None:
        try:
            self._verify_parent()
            self._verify_endpoint()
        except QuestionChannelError as exc:
            raise QuestionChannelError(
                f"question channel cleanup is unverified: {exc}",
                cleanup_confirmed=False,
            ) from exc
        try:
            self._socket_path.unlink()
        except (OSError, ValueError) as exc:
            raise QuestionChannelError(
                "question socket could not be removed", cleanup_confirmed=False
            ) from exc
        try:
            self._socket_path.lstat()
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            raise QuestionChannelError(
                "question socket removal could not be verified", cleanup_confirmed=False
            ) from exc
        raise QuestionChannelError(
            "question socket remained after removal", cleanup_confirmed=False
        )

    def close(self) -> None:
        deadline = time.monotonic() + _CLOSE_TIMEOUT_SECONDS
        with self._lifecycle_lock:
            if self._closed:
                return
            self._stop.set()
            active_stop = self._active_stop
            connection = self._active_connection
            server_thread = self._server_thread
            worker = self._active_worker
        if active_stop is not None:
            active_stop.set()
        self._close_connection(connection)
        self._close_listener()
        if server_thread is not None and self._server_thread_started:
            if server_thread is threading.current_thread():
                raise QuestionChannelError(
                    "question channel cannot close from its server thread",
                    cleanup_confirmed=False,
                )
            server_thread.join(max(0.0, deadline - time.monotonic()))
            if server_thread.is_alive():
                raise QuestionChannelError(
                    "question channel server thread did not stop",
                    cleanup_confirmed=False,
                )
        if worker is not None and worker.is_alive():
            worker.join(max(0.0, deadline - time.monotonic()))
            if worker.is_alive():
                raise QuestionChannelError(
                    "question exchange callback did not stop",
                    cleanup_confirmed=False,
                )
        if self._started:
            self._remove_owned_endpoint()
        with self._lifecycle_lock:
            self._closed = True
