"""Assignment-scoped question state for named Orca ACP workers.

This draft owns local question state, the existing QuestionChannel transport,
and the selected Orca ask CLI. Orca reply and Delivery ACK RPCs are supplied
by the typed backend through receipts; this module never sends them.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Final, NoReturn, TypeAlias, cast

from .adapters import (
    ExecutionError,
    ProcessCancellationRequested,
    ProcessRunner,
)
from .contracts import (
    CompletionIdentity,
    DeliveryRef,
    DispatchRef,
    MessageRef,
    NormalizedEvent,
    RunRef,
    TaskRef,
    TerminalRef,
)
from .locking import _LifecycleReservation
from .named_graph import GraphSpec
from .native_question_channel import (
    MAX_FRAME_BYTES,
    MAX_TEXT_CHARS,
    QuestionChannel,
    QuestionChannelError,
    QuestionRequest,
    validate_answers,
    validate_question_request,
)
from .orca import orca_executable
from .orca_delivery import container as select_delivery_container
from .runtime import read_state as _runtime_read_state
from .runtime import write_state as _runtime_write_state

read_state = _runtime_read_state
write_state = _runtime_write_state

ASK_TIMEOUT_MS: Final = 500
MAX_ERROR_CHARS: Final = 2_000
QUESTION_KEY: Final = "orca_question"
SESSION_KEY: Final = "acp_session_id"
PHASES: Final = frozenset(
    {
        "asking",
        "observed",
        "replied",
        "acked",
        "received",
        "recorded",
        "failed",
        "cancelling",
    }
)
ACTIVE_PHASES: Final = frozenset({"asking", "observed", "replied", "acked", "received"})
QUESTION_FIELDS: Final = frozenset(
    {
        "session_id",
        "tool_call_id",
        "request",
        "message_id",
        "thread_id",
        "answer_message_id",
        "phase",
        "answers",
        "answer_sha256",
        "error",
    }
)
IDENTITY_FIELDS: Final = (
    "role",
    "role_kind",
    "run_id",
    "task_id",
    "dispatch_id",
    "terminal_handle",
    "launch_nonce",
)
AskCallback: TypeAlias = Callable[
    [str | None, str | None, int, threading.Event], Mapping[str, object]
]
StopCheck: TypeAlias = Callable[[], bool]


class OrcaQuestionError(ValueError):
    """A named Orca question state or wire contract failed validation."""


def _text(value: object, field: str, *, maximum: int = MAX_TEXT_CHARS) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or "\x00" in value
        or any(0xD800 <= ord(character) <= 0xDFFF for character in value)
    ):
        raise OrcaQuestionError(f"question {field} is invalid")
    return value


def _json_constant(value: str) -> NoReturn:
    raise OrcaQuestionError(f"non-finite JSON value {value} is not allowed")


def _duplicate_pairs(pairs: list[tuple[object, object]]) -> dict[object, object]:
    result: dict[object, object] = {}
    for key, value in pairs:
        if key in result:
            raise OrcaQuestionError("duplicate JSON object key")
        result[key] = value
    return result


def _canonical(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, UnicodeError) as exc:
        raise OrcaQuestionError("question JSON is not serializable") from exc
    if len(encoded.encode("utf-8")) + 1 > MAX_FRAME_BYTES:
        raise OrcaQuestionError("question JSON exceeds the frame limit")
    return encoded


def _strict_json(body: str) -> object:
    body = _text(body, "answer body", maximum=MAX_FRAME_BYTES)
    if len(body.encode("utf-8")) + 1 > MAX_FRAME_BYTES:
        raise OrcaQuestionError("answer body exceeds the frame limit")
    try:
        return json.loads(
            body,
            object_pairs_hook=_duplicate_pairs,
            parse_constant=_json_constant,
        )
    except OrcaQuestionError:
        raise
    except (TypeError, ValueError, UnicodeError) as exc:
        raise OrcaQuestionError("answer body is not valid JSON") from exc


def _canonical_answer(request: QuestionRequest, value: object) -> dict[str, str]:
    answers = validate_answers(request, value)
    _canonical(answers)
    return answers


def validate_answer_mapping(request: QuestionRequest, value: object) -> dict[str, str]:
    """Validate a complete form answer and its UTF-8 frame size."""

    return _canonical_answer(validate_question_request(request), value)


def _request(request: object) -> QuestionRequest:
    try:
        return validate_question_request(request)
    except (TypeError, ValueError) as exc:
        raise OrcaQuestionError(str(exc)) from exc


def question_json(request: QuestionRequest) -> str:
    """Return the one canonical JSON body sent as the Orca question."""

    return _canonical(_request(request).as_dict())


def _answer_json(request: QuestionRequest, body: str) -> dict[str, str]:
    parsed = _strict_json(body)
    return _canonical_answer(request, parsed)


def _digest_answers(answers: Mapping[str, str]) -> str:
    return hashlib.sha256(_canonical(dict(answers)).encode("utf-8")).hexdigest()


def _required_identity(identity: Mapping[str, object]) -> dict[str, str]:
    result: dict[str, str] = {}
    for field in IDENTITY_FIELDS:
        result[field] = _text(identity.get(field), field, maximum=256)
    if result["role_kind"] not in {"planner", "worker", "reviewer"}:
        raise OrcaQuestionError("question role_kind is invalid")
    return result


def _assignment(
    state: Mapping[str, object], identity: Mapping[str, object]
) -> dict[str, object]:
    checked = _required_identity(identity)
    if state.get("runtime") != "orca":
        raise OrcaQuestionError("question state runtime is not Orca")
    version = state.get("version")
    if version not in {4, 5}:
        raise OrcaQuestionError("question state version is not named Orca")
    if state.get("run_id") != checked["run_id"]:
        raise OrcaQuestionError("question Run identity changed")
    roles = state.get("roles")
    assignment = roles.get(checked["role"]) if isinstance(roles, Mapping) else None
    if not isinstance(assignment, dict):
        raise OrcaQuestionError("question assignment is missing")
    for field in (
        "role",
        "role_kind",
        "task_id",
        "dispatch_id",
        "terminal_handle",
        "launch_nonce",
    ):
        if assignment.get(field) != checked[field]:
            raise OrcaQuestionError(f"question assignment {field} changed")
    if assignment.get("completion_observed") is not False:
        raise OrcaQuestionError("question assignment is already completed")
    if assignment.get("launcher_owned_terminal") is not True:
        raise OrcaQuestionError("question terminal ownership is unproven")
    _validate_named_profile(state, checked, assignment)
    _delivery_container(state, assignment)
    return cast(dict[str, object], assignment)


def _validate_named_profile(
    state: Mapping[str, object],
    identity: Mapping[str, str],
    assignment: Mapping[str, object],
) -> None:
    graph_value = state.get("graph")
    try:
        graph = GraphSpec.from_dict(graph_value)
        node = graph.node(identity["role"])
    except (KeyError, TypeError, ValueError) as exc:
        raise OrcaQuestionError("question graph identity is invalid") from exc
    expected_dispatch = "serial" if state.get("version") == 4 else "parallel"
    if (
        graph.coordination.mode != "agent"
        or graph.coordination.dispatch_mode != expected_dispatch
    ):
        raise OrcaQuestionError("question graph coordination is not an Orca graph")
    if node.kind.value != identity["role_kind"]:
        raise OrcaQuestionError("question node kind changed")
    role_specs = state.get("role_specs")
    spec = role_specs.get(identity["role"]) if isinstance(role_specs, Mapping) else None
    if not isinstance(spec, Mapping):
        raise OrcaQuestionError("question role spec is missing")
    required = (
        "provider",
        "transport",
        "model",
        "effort",
        "permission",
        "instructions",
        "execution",
    )
    if any(
        not isinstance(spec.get(field), str) or not spec.get(field)
        for field in required
    ):
        raise OrcaQuestionError("question role spec is incomplete")
    from .scoped_acp import native_profile

    try:
        expected = native_profile("claude", identity["role_kind"])
    except (RuntimeError, ValueError) as exc:
        raise OrcaQuestionError("question role kind is invalid") from exc
    if any(spec.get(key) != value for key, value in expected.items()):
        raise OrcaQuestionError(
            "question role spec is not the selected Claude ACP profile"
        )
    if (
        assignment.get("role") != identity["role"]
        or assignment.get("role_kind") != identity["role_kind"]
    ):
        raise OrcaQuestionError("question assignment node identity changed")


def _assignment_from_state(
    state: Mapping[str, object], assignment: Mapping[str, object]
) -> dict[str, object]:
    identity = {field: assignment.get(field) for field in IDENTITY_FIELDS}
    if identity["run_id"] is None:
        identity["run_id"] = state.get("run_id")
    return _assignment(state, identity)


def _question_value(assignment: Mapping[str, object]) -> dict[str, object] | None:
    value = assignment.get(QUESTION_KEY)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise OrcaQuestionError("orca_question must be an object")
    if set(value) != QUESTION_FIELDS:
        raise OrcaQuestionError("orca_question fields are invalid")
    return cast(dict[str, object], value)


def _delivery_container(
    state: Mapping[str, object], assignment: Mapping[str, object]
) -> dict[str, object]:
    node_id = assignment.get("role")
    if node_id is not None and not isinstance(node_id, str):
        raise OrcaQuestionError("question assignment role is invalid")
    try:
        selected = select_delivery_container(state, node_id)
    except (TypeError, ValueError) as exc:
        raise OrcaQuestionError(str(exc)) from exc
    if not isinstance(selected, dict):
        raise OrcaQuestionError("question Delivery container is invalid")
    return selected


def _validate_message_id(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field, maximum=256)


def _validate_outbox_value(
    state: Mapping[str, object], assignment: Mapping[str, object]
) -> dict[str, object] | None:
    question = _question_value(assignment)
    if question is None:
        return None
    request = _request(question.get("request"))
    session_id = _text(question.get("session_id"), "session_id", maximum=256)
    tool_call_id = _text(question.get("tool_call_id"), "tool_call_id", maximum=256)
    if session_id != request.session_id or tool_call_id != request.tool_call_id:
        raise OrcaQuestionError("question request identity changed")
    saved_session = assignment.get(SESSION_KEY)
    if saved_session is not None and saved_session != session_id:
        raise OrcaQuestionError("ACP question session changed")
    phase = question.get("phase")
    if phase not in PHASES:
        raise OrcaQuestionError("question phase is invalid")
    message_id = _validate_message_id(question.get("message_id"), "message_id")
    thread_id = _validate_message_id(question.get("thread_id"), "thread_id")
    answer_message_id = _validate_message_id(
        question.get("answer_message_id"), "answer_message_id"
    )
    if (
        phase in {"observed", "replied", "acked", "received", "recorded"}
        and message_id is None
    ):
        raise OrcaQuestionError("question phase is missing message_id")
    if message_id is not None and thread_id is not None and thread_id != message_id:
        raise OrcaQuestionError("question thread identity changed")
    answers = question.get("answers")
    if answers is not None:
        answers = _canonical_answer(request, answers)
    digest = question.get("answer_sha256")
    if digest is not None:
        digest = _text(digest, "answer_sha256", maximum=64)
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise OrcaQuestionError("answer_sha256 is invalid")
        if answers is None or digest != _digest_answers(answers):
            raise OrcaQuestionError("answer_sha256 does not match answers")
    if phase in {"replied", "acked", "received", "recorded"} and answers is None:
        raise OrcaQuestionError("answered question is missing answers")
    if (
        phase in {"replied", "acked", "received", "recorded"}
        and answer_message_id is None
    ):
        raise OrcaQuestionError("answered question is missing answer_message_id")
    error = question.get("error")
    if error is not None:
        _text(error, "error", maximum=MAX_ERROR_CHARS)
    delivery = _delivery_container(state, assignment)
    pending_id = delivery.get("pending_delivery_id")
    pending_kind = delivery.get("pending_delivery_kind")
    pending_stage = delivery.get("pending_delivery_stage")
    pending_questions = delivery.get("pending_question_ids")
    replied_questions = delivery.get("replied_question_ids")
    if pending_id is not None:
        if pending_kind != "question" or pending_stage != "observed":
            raise OrcaQuestionError("question outbox does not match pending Delivery")
        if phase in {"observed", "replied"}:
            if pending_questions != [message_id] or not isinstance(
                replied_questions, list
            ):
                raise OrcaQuestionError(
                    "question outbox does not match pending Delivery"
                )
            if any(not isinstance(item, str) for item in replied_questions):
                raise OrcaQuestionError("pending question replies are invalid")
            if any(item != message_id for item in replied_questions):
                raise OrcaQuestionError("question outbox reply identity changed")
        elif phase in {"failed", "cancelling"}:
            if message_id is None or pending_questions != [message_id]:
                raise OrcaQuestionError("failed question Delivery identity changed")
        else:
            raise OrcaQuestionError("question outbox does not match pending Delivery")
    elif phase in {"observed", "replied"}:
        raise OrcaQuestionError("observed question has no pending Delivery")
    return question


def validate_outbox(
    state: Mapping[str, object], assignment: Mapping[str, object]
) -> dict[str, object] | None:
    """Validate and return one assignment-scoped Orca question."""

    return _validate_outbox_value(state, _assignment_from_state(state, assignment))


def _save_question(
    path: Path,
    identity: Mapping[str, object],
    mutate: Callable[[dict[str, object], dict[str, object]], object],
) -> object:
    reservation = _LifecycleReservation(path, create_parent=False)
    reservation.acquire_for_publication()
    try:
        state = read_state(path)
        assignment = _assignment(state, identity)
        value = mutate(state, assignment)
        write_state(path, state, require_existing=True, reservation_held=True)
        return copy.deepcopy(value)
    finally:
        reservation.release()


def _begin_in_state(
    state: dict[str, object],
    assignment: dict[str, object],
    request: QuestionRequest,
) -> dict[str, object]:
    existing = _question_value(assignment)
    if existing is not None:
        if (
            existing.get("phase") == "asking"
            and existing.get("request") == request.as_dict()
        ):
            return existing
        if existing.get("phase") == "recorded":
            assignment.pop(QUESTION_KEY, None)
        else:
            raise OrcaQuestionError("another Orca question is pending")
    delivery = _delivery_container(state, assignment)
    if delivery.get("pending_delivery_id") is not None:
        raise OrcaQuestionError("a Delivery is already pending")
    session_id = request.session_id
    saved_session = assignment.get(SESSION_KEY)
    if saved_session is not None and saved_session != session_id:
        raise OrcaQuestionError("ACP question session changed")
    assignment[SESSION_KEY] = session_id
    assignment[QUESTION_KEY] = {
        "session_id": session_id,
        "tool_call_id": request.tool_call_id,
        "request": request.as_dict(),
        "message_id": None,
        "thread_id": None,
        "answer_message_id": None,
        "phase": "asking",
        "answers": None,
        "answer_sha256": None,
        "error": None,
    }
    _validate_outbox_value(state, assignment)
    return cast(dict[str, object], assignment[QUESTION_KEY])


def begin_question(
    path: Path, request: QuestionRequest, **identity: str
) -> dict[str, object]:
    request = _request(request)
    return cast(
        dict[str, object],
        _save_question(
            path,
            identity,
            lambda state, assignment: _begin_in_state(state, assignment, request),
        ),
    )


def _save_remote_identity(
    path: Path,
    identity: Mapping[str, object],
    *,
    message_id: str,
    thread_id: str,
    answer_message_id: str | None = None,
) -> None:
    def mutate(state: dict[str, object], assignment: dict[str, object]) -> None:
        question = _question_value(assignment)
        if question is None:
            raise OrcaQuestionError("question placeholder is missing")
        current = question.get("message_id")
        if current is not None and current != message_id:
            raise OrcaQuestionError("question message identity changed")
        current_thread = question.get("thread_id")
        if current_thread is not None and current_thread != thread_id:
            raise OrcaQuestionError("question thread identity changed")
        current_answer = question.get("answer_message_id")
        if (
            answer_message_id is not None
            and current_answer is not None
            and current_answer != answer_message_id
        ):
            raise OrcaQuestionError("question answer message identity changed")
        question["message_id"] = message_id
        question["thread_id"] = thread_id
        if answer_message_id is not None:
            question["answer_message_id"] = answer_message_id
        _validate_outbox_value(state, assignment)

    _save_question(Path(cast(str, identity["state_path"])), identity, mutate)


class _AskProcessRunner(ProcessRunner):
    """Run only the selected Orca process and observe the ACP stop event."""

    def __init__(self, stopped: threading.Event) -> None:
        super().__init__(max_output_bytes=MAX_FRAME_BYTES * 2)
        self._stopped = stopped

    def _check_cancelled(self) -> None:
        if self._stopped.is_set():
            raise ProcessCancellationRequested()
        super()._check_cancelled()


def _parse_bare_ask_result(payload: object, returncode: int) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise OrcaQuestionError("Orca ask response is not an object")
    base_fields = {
        "answer",
        "messageId",
        "threadId",
        "timedOut",
        "cancelled",
        "connectionLost",
        "timeoutMs",
    }
    fields = set(payload)
    success_fields = {*base_fields, "answerMessageId"}
    if fields != base_fields and fields != success_fields:
        raise OrcaQuestionError("Orca ask response fields are invalid")
    answer = payload.get("answer")
    if answer is not None:
        _text(answer, "answer", maximum=MAX_FRAME_BYTES)
    message_id = _text(payload.get("messageId"), "messageId", maximum=256)
    thread_id = _text(payload.get("threadId"), "threadId", maximum=256)
    if thread_id != message_id:
        raise OrcaQuestionError("Orca ask threadId does not match messageId")
    for field in ("timedOut", "cancelled", "connectionLost"):
        if type(payload.get(field)) is not bool:
            raise OrcaQuestionError(f"Orca ask {field} is invalid")
    timeout_value = payload.get("timeoutMs")
    if timeout_value is not None and (
        type(timeout_value) is not int or timeout_value < 0
    ):
        raise OrcaQuestionError("Orca ask timeoutMs is invalid")
    pending = any(
        cast(bool, payload[field])
        for field in ("timedOut", "cancelled", "connectionLost")
    )
    if returncode != 0:
        if fields != base_fields or answer is not None or not pending:
            raise OrcaQuestionError("Orca ask failed without a pending result")
    elif fields != success_fields or answer is None or pending:
        raise OrcaQuestionError("Orca ask success result is incomplete")
    if returncode == 0:
        _text(payload.get("answerMessageId"), "answerMessageId", maximum=256)
    return cast(dict[str, object], payload)


def _ask_orca(
    path: Path,
    question: str | None,
    resume_message_id: str | None,
    timeout_ms: int,
    stopped: threading.Event,
    **identity: str,
) -> Mapping[str, object]:
    if type(timeout_ms) is not int or not 1 <= timeout_ms <= 1_800_000:
        raise OrcaQuestionError("Orca ask timeout is invalid")
    if (question is None) == (resume_message_id is None):
        raise OrcaQuestionError("Orca ask requires exactly question or resume")
    state = read_state(path)
    assignment = _assignment(state, identity)
    workspace = _text(state.get("workspace"), "workspace", maximum=4_096)
    run_id = _text(state.get("run_id"), "run_id", maximum=256)
    terminal = _text(assignment.get("terminal_handle"), "terminal_handle", maximum=256)
    argv = [orca_executable(), "orchestration", "ask"]
    if question is not None:
        argv.extend(("--question", question))
    else:
        argv.extend(("--resume", cast(str, resume_message_id)))
    argv.extend(
        (
            "--run",
            run_id,
            "--from",
            terminal,
            "--timeout-ms",
            str(timeout_ms),
            "--json",
        )
    )
    runner = _AskProcessRunner(stopped)
    result = runner.run(
        argv,
        cwd=Path(workspace),
        env=os.environ.copy(),
        timeout_seconds=timeout_ms / 1_000 + 5.0,
    )
    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise OrcaQuestionError("Orca ask response is not valid JSON") from exc
    return _parse_bare_ask_result(payload, result.returncode)


def _ask_result(
    result: Mapping[str, object],
) -> tuple[str | None, str, str, str | None, bool, bool, bool]:
    base_fields = {
        "answer",
        "messageId",
        "threadId",
        "timedOut",
        "cancelled",
        "connectionLost",
        "timeoutMs",
    }
    fields = set(result)
    success_fields = {*base_fields, "answerMessageId"}
    if fields != base_fields and fields != success_fields:
        raise OrcaQuestionError("Orca ask result fields are invalid")
    message_id = _text(result.get("messageId"), "messageId", maximum=256)
    thread_id = _text(result.get("threadId"), "threadId", maximum=256)
    if thread_id != message_id:
        raise OrcaQuestionError("Orca ask threadId does not match messageId")
    if (
        type(result.get("timedOut")) is not bool
        or type(result.get("cancelled")) is not bool
        or type(result.get("connectionLost")) is not bool
    ):
        raise OrcaQuestionError("Orca ask result flags are invalid")
    timeout_value = result.get("timeoutMs")
    if timeout_value is not None and (
        type(timeout_value) is not int or timeout_value < 0
    ):
        raise OrcaQuestionError("Orca ask timeoutMs is invalid")
    answer = result.get("answer")
    if answer is not None:
        answer = _text(answer, "answer", maximum=MAX_FRAME_BYTES)
    answer_message_id = result.get("answerMessageId")
    if answer is not None:
        if fields != success_fields:
            raise OrcaQuestionError(
                "answered Orca ask result is missing answerMessageId"
            )
        answer_message_id = _text(answer_message_id, "answerMessageId", maximum=256)
    elif fields != base_fields or answer_message_id is not None:
        raise OrcaQuestionError("pending Orca ask result has an answerMessageId")
    return (
        answer,
        message_id,
        thread_id,
        answer_message_id,
        cast(bool, result["timedOut"]),
        cast(bool, result["cancelled"]),
        cast(bool, result["connectionLost"]),
    )


def exchange(
    path: Path,
    request: QuestionRequest,
    stopped: threading.Event,
    *,
    ask: AskCallback | None = None,
    timeout_ms: int = ASK_TIMEOUT_MS,
    **identity: str,
) -> dict[str, str]:
    """Ask/resume without holding the lifecycle lock and wait for local ACK."""

    request = _request(request)
    state = read_state(path)
    assignment = _assignment(state, identity)
    if _question_value(assignment) is None:
        begin_question(path, request, **identity)
        state = read_state(path)
        assignment = _assignment(state, identity)
    question = validate_outbox(state, assignment)
    if question is None or question["request"] != request.as_dict():
        raise OrcaQuestionError("question placeholder does not match request")
    remote_answers: dict[str, str] | None = None
    remote_answer_message_id: str | None = None
    remote_message_id: str | None = cast(str | None, question.get("message_id"))
    remote_thread_id: str | None = cast(str | None, question.get("thread_id"))
    while not stopped.is_set():
        current = read_state(path)
        current_assignment = _assignment(current, identity)
        current_question = validate_outbox(current, current_assignment)
        if current_question is None:
            raise OrcaQuestionError("question state disappeared")
        phase = cast(str, current_question["phase"])
        if phase in {"failed", "cancelling"}:
            raise OrcaQuestionError("question exchange was cancelled")
        if remote_answers is not None:
            saved_answers = current_question.get("answers")
            if phase in {"acked", "received", "recorded"}:
                if (
                    current_question.get("answer_message_id")
                    != remote_answer_message_id
                ):
                    raise OrcaQuestionError(
                        "Orca answer message does not match Main reply receipt"
                    )
                if saved_answers != remote_answers:
                    raise OrcaQuestionError("Orca answer does not match local ACK")
                return remote_answers
            stopped.wait(0.05)
            continue
        remote_message_id = cast(str | None, current_question.get("message_id"))
        remote_thread_id = cast(str | None, current_question.get("thread_id"))
        result = (
            ask(
                question_json(request) if remote_message_id is None else None,
                remote_message_id,
                timeout_ms,
                stopped,
            )
            if ask is not None
            else _ask_orca(
                path,
                question_json(request) if remote_message_id is None else None,
                remote_message_id,
                timeout_ms,
                stopped,
                **identity,
            )
        )
        (
            answer,
            message_id,
            thread_id,
            answer_message_id,
            timed_out,
            cancelled,
            connection_lost,
        ) = _ask_result(result)
        if remote_message_id is not None and message_id != remote_message_id:
            raise OrcaQuestionError("Orca ask message ID changed during resume")
        if remote_thread_id is not None and thread_id != remote_thread_id:
            raise OrcaQuestionError("Orca ask thread ID changed during resume")
        if remote_message_id is None or remote_thread_id != thread_id:
            _save_remote_identity(
                path,
                {**identity, "state_path": str(path)},
                message_id=message_id,
                thread_id=thread_id,
                answer_message_id=answer_message_id,
            )
        if answer is not None:
            remote_answers = _answer_json(request, answer)
            remote_answer_message_id = answer_message_id
            continue
        if not (timed_out or cancelled or connection_lost):
            raise OrcaQuestionError("Orca ask returned no answer or pending flag")
        stopped.wait(0.05)
    raise OrcaQuestionError("question exchange was stopped")


def _message_payload(message: Mapping[str, object]) -> dict[str, object]:
    payload = message.get("payload")
    if isinstance(payload, Mapping):
        return dict(payload)
    if isinstance(payload, str):
        parsed = _strict_json(payload)
        if isinstance(parsed, dict):
            return cast(dict[str, object], parsed)
    raise OrcaQuestionError("question message payload is invalid")


def _normalized_body(request: QuestionRequest) -> str:
    template = {question.field: "<answer>" for question in request.questions}
    return (
        question_json(request)
        + "\n\nReply with JSON containing exactly these answer fields:\n"
        + _canonical(template)
    )


def _event_identity(
    state: Mapping[str, object], assignment: Mapping[str, object]
) -> CompletionIdentity:
    return CompletionIdentity(
        RunRef(_text(state.get("run_id"), "run_id", maximum=256)),
        TaskRef(_text(assignment.get("task_id"), "task_id", maximum=256)),
        DispatchRef(_text(assignment.get("dispatch_id"), "dispatch_id", maximum=256)),
        TerminalRef(
            _text(assignment.get("terminal_handle"), "terminal_handle", maximum=256)
        ),
    )


def observe_question(
    state: dict[str, object],
    assignment: dict[str, object],
    message: Mapping[str, object],
    delivery_id: str,
) -> NormalizedEvent:
    _assignment_from_state(state, assignment)
    delivery_id = _text(delivery_id, "delivery_id", maximum=256)
    if message.get("type") != "question":
        raise OrcaQuestionError("Delivery message is not a question")
    message_id = _text(message.get("id"), "message_id", maximum=256)
    expected_sender = (
        f"dispatch:{_text(assignment.get('dispatch_id'), 'dispatch_id', maximum=256)}"
    )
    if message.get("from_handle") != expected_sender:
        raise OrcaQuestionError("question sender does not match its Dispatch")
    payload = _message_payload(message)
    if payload.get("taskId") != assignment.get("task_id") or payload.get(
        "dispatchId"
    ) != assignment.get("dispatch_id"):
        raise OrcaQuestionError("question payload does not match its assignment")
    question = _question_value(assignment)
    if question is None:
        raise OrcaQuestionError("question placeholder is missing")
    request = _request(question["request"])
    remote_body = message.get("body")
    if remote_body != question_json(request):
        raise OrcaQuestionError("question body does not match its request")
    if question.get("message_id") not in {None, message_id}:
        raise OrcaQuestionError("question message identity changed")
    phase = question.get("phase")
    if phase not in {"asking", "observed", "replied"}:
        raise OrcaQuestionError("question is not observable")
    delivery = _delivery_container(state, assignment)
    pending = delivery.get("pending_delivery_id")
    if pending is not None and (
        pending != delivery_id
        or delivery.get("pending_delivery_kind") != "question"
        or delivery.get("pending_delivery_stage") != "observed"
    ):
        raise OrcaQuestionError("question Delivery identity changed")
    question["message_id"] = message_id
    question["thread_id"] = message_id
    question["phase"] = "observed" if phase == "asking" else phase
    delivery["pending_delivery_id"] = delivery_id
    delivery["pending_delivery_kind"] = "question"
    delivery["pending_delivery_stage"] = "observed"
    delivery["pending_question_ids"] = [message_id]
    delivery["replied_question_ids"] = [message_id] if phase == "replied" else []
    _validate_outbox_value(state, assignment)
    return NormalizedEvent.question(
        identity=_event_identity(state, assignment),
        message_id=MessageRef(message_id),
        delivery_id=DeliveryRef(delivery_id),
        body=_normalized_body(request),
    )


def prepare_reply(
    state: Mapping[str, object],
    assignment: Mapping[str, object],
    message_id: str,
    body: str,
) -> dict[str, str]:
    message_id = _text(message_id, "message_id", maximum=256)
    question = validate_outbox(state, assignment)
    if question is None or question.get("message_id") != message_id:
        raise OrcaQuestionError("reply message does not match the question")
    if question.get("phase") not in {"observed", "replied"}:
        raise OrcaQuestionError("question is not awaiting a reply")
    request = _request(question["request"])
    parsed = _answer_json(request, body)
    saved = question.get("answers")
    if saved is not None and saved != parsed:
        raise OrcaQuestionError("reply body changed during retry")
    return parsed


def accept_reply(
    state: dict[str, object],
    assignment: dict[str, object],
    response: Mapping[str, object],
    message_id: str,
    body: str,
) -> dict[str, str]:
    parsed = prepare_reply(state, assignment, message_id, body)
    from .mcp_server import _validate_question_reply

    _validate_question_reply(
        dict(response),
        message_id=message_id,
        body=body,
        run_id=_text(state.get("run_id"), "run_id", maximum=256),
        dispatch_id=_text(assignment.get("dispatch_id"), "dispatch_id", maximum=256),
    )
    question = _question_value(assignment)
    if question is None:
        raise OrcaQuestionError("question placeholder is missing")
    response_message = response.get("message")
    if not isinstance(response_message, Mapping):
        raise OrcaQuestionError("question reply message receipt is missing")
    answer_message_id = _text(
        response_message.get("id"), "answer_message_id", maximum=256
    )
    if question.get("answer_message_id") not in {None, answer_message_id}:
        raise OrcaQuestionError("question reply receipt identity changed")
    question["phase"] = "replied"
    question["answers"] = parsed
    question["answer_sha256"] = _digest_answers(parsed)
    question["answer_message_id"] = answer_message_id
    delivery = _delivery_container(state, assignment)
    replied = delivery.get("replied_question_ids")
    if not isinstance(replied, list):
        raise OrcaQuestionError("pending question replies are invalid")
    if message_id not in replied:
        replied.append(message_id)
    _validate_outbox_value(state, assignment)
    return parsed


def acknowledge_question(
    state: dict[str, object], assignment: dict[str, object], delivery_id: str
) -> dict[str, str]:
    delivery_id = _text(delivery_id, "delivery_id", maximum=256)
    question = validate_outbox(state, assignment)
    delivery = _delivery_container(state, assignment)
    if (
        question is None
        or question.get("phase") != "replied"
        or delivery.get("pending_delivery_id") != delivery_id
        or delivery.get("pending_delivery_kind") != "question"
        or delivery.get("pending_delivery_stage") != "observed"
        or delivery.get("pending_question_ids") != [question.get("message_id")]
        or delivery.get("replied_question_ids") != [question.get("message_id")]
    ):
        raise OrcaQuestionError("question Delivery is not ready for acknowledgment")
    answers = cast(dict[str, str], question["answers"])
    question["phase"] = "acked"
    for key in (
        "pending_delivery_id",
        "pending_delivery_kind",
        "pending_delivery_stage",
        "pending_question_ids",
        "replied_question_ids",
    ):
        delivery.pop(key, None)
    _validate_outbox_value(state, assignment)
    return answers


def _question_mutation(
    path: Path,
    request: Mapping[str, object] | None,
    identity: Mapping[str, object],
    *,
    phase: str,
    error: str | None = None,
) -> dict[str, object] | None:
    def mutate(
        state: dict[str, object], assignment: dict[str, object]
    ) -> dict[str, object] | None:
        question = _question_value(assignment)
        if question is None:
            return None
        if (
            request is not None
            and question.get("request") != _request(request).as_dict()
        ):
            raise OrcaQuestionError("question request identity changed")
        if question.get("phase") == "recorded" and phase == "recorded":
            return question
        question["phase"] = phase
        question["error"] = error
        _validate_outbox_value(state, assignment)
        return question

    return cast(dict[str, object] | None, _save_question(path, identity, mutate))


def confirm_question(
    path: Path, request: QuestionRequest, **identity: str
) -> dict[str, object] | None:
    def mutate(
        state: dict[str, object], assignment: dict[str, object]
    ) -> dict[str, object]:
        question = _question_value(assignment)
        if question is None or question.get("request") != _request(request).as_dict():
            raise OrcaQuestionError("question request does not match the outbox")
        if question.get("phase") != "acked":
            raise OrcaQuestionError("question was not locally acknowledged")
        question["phase"] = "received"
        _validate_outbox_value(state, assignment)
        return question

    return cast(dict[str, object], _save_question(path, identity, mutate))


def record_question_sent(
    path: Path, request: QuestionRequest, **identity: str
) -> dict[str, object] | None:
    def mutate(
        state: dict[str, object], assignment: dict[str, object]
    ) -> dict[str, object]:
        question = _question_value(assignment)
        if question is None or question.get("request") != _request(request).as_dict():
            raise OrcaQuestionError("question request does not match the outbox")
        if question.get("phase") != "received":
            raise OrcaQuestionError("question received receipt is missing")
        question["phase"] = "recorded"
        _validate_outbox_value(state, assignment)
        return question

    return cast(dict[str, object], _save_question(path, identity, mutate))


def fail_question(
    path: Path,
    request: QuestionRequest | None,
    *,
    cancelling: bool = False,
    error: str | None = None,
    **identity: str,
) -> dict[str, object] | None:
    phase = "cancelling" if cancelling else "failed"
    bounded_error = _text(
        error or "Orca question channel failed; unanswered Delivery is retained",
        "error",
        maximum=MAX_ERROR_CHARS,
    )
    return _question_mutation(
        path,
        request.as_dict() if request is not None else None,
        identity,
        phase=phase,
        error=bounded_error,
    )


@contextmanager
def question_context(
    path: Path,
    socket_path: Path,
    identity: Mapping[str, str],
    *,
    ask: AskCallback | None = None,
    timeout_ms: int = ASK_TIMEOUT_MS,
    stop_requested: StopCheck | None = None,
) -> Iterator[None]:
    def exchange_callback(
        request: QuestionRequest, stopped: threading.Event
    ) -> Mapping[str, str]:
        begin_question(path, request, **identity)
        return exchange(
            path,
            request,
            stopped,
            ask=ask,
            timeout_ms=timeout_ms,
            **identity,
        )

    def delivered(request: QuestionRequest) -> None:
        confirm_question(path, request, **identity)

    def recorded(request: QuestionRequest) -> None:
        record_question_sent(path, request, **identity)

    def saved_stop_requested() -> bool:
        state = read_state(path)
        _assignment(state, identity)
        return state.get("orca_stop_requested") is True

    def failed(request: QuestionRequest | None, error: Exception) -> None:
        fail_question(
            path,
            request,
            cancelling=(
                stop_requested()
                if stop_requested is not None
                else saved_stop_requested()
            ),
            error=str(error),
            **identity,
        )

    channel = QuestionChannel(
        socket_path,
        exchange_callback,
        delivered,
        failed,
        recorded=recorded,
    )
    try:
        with channel:
            yield
    except QuestionChannelError as exc:
        raise ExecutionError(str(exc), cleanup_confirmed=exc.cleanup_confirmed) from exc
    if channel.failure is not None:
        raise ExecutionError(
            "Orca question channel failed; pending question state is retained",
            cleanup_confirmed=True,
        )
