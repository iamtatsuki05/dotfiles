"""Durable TaskSpec, review, and verification-admission transitions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import NoReturn, cast

from .contracts import ErrorCode, Role, RuntimeFailure, TaskDispatch
from .task_spec import TaskSpec, parse_task_specs

_WRITER_ROLES = frozenset({Role.PLANNER, Role.WORKER})
_REVIEW_STAGES = frozenset({"plan", "implementation"})
_REVIEW_DECISIONS = frozenset({"approve", "request_changes", "consult"})
_VERIFICATION_KEYS = frozenset(
    {"revision", "passed", "commands", "error", "cleanup_confirmed"}
)
_VERIFICATION_COMMAND_KEYS = frozenset(
    {
        "name",
        "argv",
        "timeout_seconds",
        "returncode",
        "stdout_sha256",
        "stderr_sha256",
        "error",
    }
)
_MAX_VERIFICATION_ERROR_CHARS = 512
_TASK_STATUSES = frozenset(
    {
        "running",
        "awaiting_plan_review",
        "reviewing_plan",
        "plan_approved",
        "plan_changes_requested",
        "awaiting_implementation_review",
        "reviewing_implementation",
        "implementation_approved",
        "implementation_changes_requested",
        "consultation_required",
        "verifying",
        "completed",
        "verification_failed",
        "failed",
    }
)


def _fail(code: ErrorCode, message: str) -> NoReturn:
    raise RuntimeFailure(code, message)


def task_json(task: TaskSpec) -> str:
    return json.dumps(task.as_dict(), ensure_ascii=False, sort_keys=True)


def task_digest(task: TaskSpec) -> str:
    return hashlib.sha256(task_json(task).encode("utf-8")).hexdigest()


def _max_review_rounds(state: Mapping[str, object]) -> int:
    value = state.get("max_review_rounds")
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        _fail(ErrorCode.INVALID_REQUEST, "max_review_rounds must be a positive integer")
    return value


def _mutable_tasks(state: dict[str, object]) -> dict[str, object]:
    tasks = state.get("tasks")
    if not isinstance(tasks, dict):
        _fail(ErrorCode.INVALID_REQUEST, "saved tasks are invalid")
    return tasks


def _require_message(request: TaskDispatch) -> str:
    if not isinstance(request.message, str) or not request.message.strip():
        _fail(ErrorCode.INVALID_REQUEST, "task dispatch message must be non-empty")
    return request.message


def _require_record_task(record: Mapping[str, object], task: TaskSpec) -> None:
    if record.get("spec") != task.as_dict():
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved TaskSpec cannot be replaced")
    if record.get("digest") != task_digest(task):
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved TaskSpec digest does not match")


def _review_rounds(record: Mapping[str, object]) -> dict[str, int]:
    raw = record.get("review_rounds")
    if not isinstance(raw, Mapping) or set(raw) != {"plan", "implementation"}:
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved review rounds are invalid")
    rounds: dict[str, int] = {}
    for stage in ("plan", "implementation"):
        value = raw.get(stage)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            _fail(ErrorCode.IDENTITY_MISMATCH, "saved review rounds are invalid")
        rounds[stage] = value
    return rounds


def _result_body(record: Mapping[str, object]) -> str:
    result = record.get("result")
    if not isinstance(result, Mapping) or not isinstance(result.get("body"), str):
        _fail(ErrorCode.ORDER_VIOLATION, "review requires the previous result body")
    return cast(str, result["body"])


def _task_prompt(task: TaskSpec, message: str) -> str:
    return (
        "次のTaskSpecを遵守してください。完了条件・変更範囲・相談条件はこの仕様に従います。\n"
        + task_json(task)
        + "\n\n追加の依頼:\n"
        + message
    )


def review_prompt(
    task: TaskSpec,
    *,
    stage: str,
    revision: str,
    result_body: str,
    message: str = "",
) -> str:
    """Render a reviewer prompt with one exact JSON output contract."""

    if stage not in _REVIEW_STAGES:
        _fail(ErrorCode.INVALID_REQUEST, "review stage is invalid")
    if not isinstance(revision, str) or not revision:
        _fail(ErrorCode.INVALID_REQUEST, "review revision must be non-empty")
    if not isinstance(result_body, str):
        _fail(ErrorCode.INVALID_REQUEST, "review result body must be a string")
    if not isinstance(message, str):
        _fail(ErrorCode.INVALID_REQUEST, "review message must be a string")
    template = {
        "task_id": task.task_id,
        "stage": stage,
        "revision": revision,
        "decision": "approve",
        "findings": [],
    }
    supplement = f"\n\n追加の依頼:\n{message}" if message else ""
    return (
        "前段resultbody:\n"
        + result_body
        + "\n\nTaskSpec:\n"
        + task_json(task)
        + supplement
        + "\n\nレビュー結果は次のキーを持つJSON objectだけを出力してください。"
        "説明文やMarkdownを追加してはいけません。decisionはapprove、"
        "request_changes、consultのいずれかです。request_changesとconsultでは"
        "findingsを1件以上記載してください。\n"
        + json.dumps(template, ensure_ascii=False, separators=(",", ":"))
    )


def _decode_review(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _fail(ErrorCode.INVALID_REQUEST, "review verdict must be an object")
    keys = tuple(value)
    expected = {"task_id", "stage", "revision", "decision", "findings"}
    if any(not isinstance(key, str) for key in keys):
        _fail(ErrorCode.INVALID_REQUEST, "review verdict keys must be strings")
    if set(keys) != expected or len(keys) != len(set(keys)):
        _fail(ErrorCode.INVALID_REQUEST, "review verdict keys are not exact")
    return {cast(str, key): item for key, item in value.items()}


def parse_review(
    output: str, *, task: TaskSpec, stage: str, revision: str
) -> dict[str, object]:
    """Parse and bind the only accepted reviewer output."""

    if stage not in _REVIEW_STAGES:
        _fail(ErrorCode.INVALID_REQUEST, "review stage is invalid")
    if not isinstance(revision, str) or not revision:
        _fail(ErrorCode.INVALID_REQUEST, "review revision must be non-empty")

    def pairs(pairs_value: list[tuple[str, object]]) -> dict[str, object]:
        parsed: dict[str, object] = {}
        for key, value in pairs_value:
            if key in parsed:
                _fail(
                    ErrorCode.INVALID_REQUEST,
                    "review verdict contains duplicate keys",
                )
            parsed[key] = value
        return parsed

    try:
        decoded = json.loads(output, object_pairs_hook=pairs)
    except (TypeError, json.JSONDecodeError, RuntimeFailure) as exc:
        if isinstance(exc, RuntimeFailure):
            raise
        _fail(ErrorCode.INVALID_REQUEST, "review output must be one JSON object")
    verdict = _decode_review(decoded)
    if verdict.get("task_id") != task.task_id:
        _fail(ErrorCode.IDENTITY_MISMATCH, "review task_id does not match TaskSpec")
    if verdict.get("stage") != stage:
        _fail(ErrorCode.IDENTITY_MISMATCH, "review stage does not match assignment")
    if verdict.get("revision") != revision:
        _fail(ErrorCode.IDENTITY_MISMATCH, "review revision does not match assignment")
    decision = verdict.get("decision")
    if not isinstance(decision, str) or decision not in _REVIEW_DECISIONS:
        _fail(ErrorCode.INVALID_REQUEST, "review decision is invalid")
    findings = verdict.get("findings")
    if not isinstance(findings, list) or any(
        not isinstance(item, str) or not item.strip() for item in findings
    ):
        _fail(ErrorCode.INVALID_REQUEST, "review findings must be a string array")
    if decision != "approve" and not findings:
        _fail(
            ErrorCode.INVALID_REQUEST,
            "review findings are required for this decision",
        )
    return {
        "task_id": task.task_id,
        "stage": stage,
        "revision": revision,
        "decision": decision,
        "findings": list(findings),
    }


def _parse_saved_task(task_id: object, record: object, max_rounds: int) -> TaskSpec:
    if not isinstance(task_id, str) or not task_id or not isinstance(record, Mapping):
        _fail(ErrorCode.INVALID_REQUEST, "saved TaskSpec is invalid")
    task = TaskSpec.from_dict(record.get("spec"))
    if task.task_id != task_id or record.get("digest") != task_digest(task):
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved TaskSpec identity is invalid")
    dispatch_id = record.get("dispatch_id")
    if not isinstance(dispatch_id, str) or not dispatch_id:
        _fail(
            ErrorCode.IDENTITY_MISMATCH,
            "saved TaskSpec dispatch identity is invalid",
        )
    status = record.get("status")
    if status not in _TASK_STATUSES:
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved TaskSpec status is invalid")
    role = record.get("role")
    if role not in {item.value for item in Role}:
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved TaskSpec role is invalid")
    rounds = _review_rounds(record)
    if any(value > max_rounds for value in rounds.values()):
        _fail(
            ErrorCode.IDENTITY_MISMATCH,
            "saved review rounds exceed the configured limit",
        )
    stage = record.get("stage")
    if stage is not None and stage not in _REVIEW_STAGES:
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved TaskSpec stage is invalid")
    revision = record.get("revision")
    if revision is not None and (not isinstance(revision, str) or not revision):
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved TaskSpec revision is invalid")
    result = record.get("result")
    if result is not None and not isinstance(result, Mapping):
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved TaskSpec result is invalid")
    writer_role = record.get("writer_role")
    if writer_role is not None and writer_role not in {
        Role.PLANNER.value,
        Role.WORKER.value,
    }:
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved TaskSpec writer role is invalid")
    expected_role: str | None = None
    expected_stage: str | None = None
    if status == "running":
        expected_role = cast(str, writer_role) if writer_role is not None else role
        expected_stage = (
            "plan" if expected_role == Role.PLANNER.value else "implementation"
        )
    elif status == "awaiting_plan_review":
        expected_role, expected_stage = Role.PLANNER.value, "plan"
    elif status == "awaiting_implementation_review":
        expected_role, expected_stage = Role.WORKER.value, "implementation"
    elif status in {"reviewing_plan", "plan_approved", "plan_changes_requested"}:
        expected_role, expected_stage = Role.REVIEWER.value, "plan"
    elif status in {
        "reviewing_implementation",
        "implementation_approved",
        "implementation_changes_requested",
    }:
        expected_role, expected_stage = Role.REVIEWER.value, "implementation"
    elif status == "consultation_required":
        expected_role = Role.REVIEWER.value
    if expected_role is not None and role != expected_role:
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved TaskSpec role does not match status")
    if expected_stage is not None and stage != expected_stage:
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved TaskSpec stage does not match status")
    if status in {
        "awaiting_plan_review",
        "reviewing_plan",
        "plan_approved",
        "plan_changes_requested",
        "reviewing_implementation",
        "implementation_approved",
        "implementation_changes_requested",
        "consultation_required",
    } and not isinstance(revision, str):
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved TaskSpec review revision is missing")
    if status == "awaiting_implementation_review" and revision is not None:
        _fail(
            ErrorCode.IDENTITY_MISMATCH, "implementation review revision is premature"
        )
    if status in {
        "awaiting_plan_review",
        "awaiting_implementation_review",
        "reviewing_plan",
        "reviewing_implementation",
        "plan_approved",
        "plan_changes_requested",
        "implementation_approved",
        "implementation_changes_requested",
        "consultation_required",
    } and not isinstance(result, Mapping):
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved TaskSpec result is missing")
    if status in {"completed", "verification_failed"}:
        _validate_verification(
            task,
            record,
            status=cast(str, status),
            require_complete=status == "completed",
        )
    return task


def _writer_status(status: object) -> Role | None:
    if status == "plan_approved":
        return Role.WORKER
    if status == "plan_changes_requested":
        return Role.PLANNER
    if status == "implementation_changes_requested":
        return Role.WORKER
    if status == "verification_failed":
        return Role.WORKER
    return None


def _require_sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        _fail(ErrorCode.IDENTITY_MISMATCH, f"{field} is not a SHA-256 digest")
    try:
        int(value, 16)
    except ValueError:
        _fail(ErrorCode.IDENTITY_MISMATCH, f"{field} is not a SHA-256 digest")
    return value


def _require_bounded_error(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_VERIFICATION_ERROR_CHARS
        or not value.isprintable()
    ):
        _fail(ErrorCode.IDENTITY_MISMATCH, f"{field} is not a bounded error")
    return value


def _validate_verification(
    task: TaskSpec,
    record: Mapping[str, object],
    *,
    status: str,
    require_complete: bool = True,
) -> Mapping[str, object]:
    revision = record.get("revision")
    if not isinstance(revision, str) or not revision:
        _fail(ErrorCode.IDENTITY_MISMATCH, "verified task revision is missing")
    task_evidence = record.get("task_evidence")
    if not isinstance(task_evidence, Mapping):
        _fail(
            ErrorCode.IDENTITY_MISMATCH, "implementation approval evidence is missing"
        )
    try:
        verdict = parse_review(
            json.dumps(dict(task_evidence), ensure_ascii=False),
            task=task,
            stage="implementation",
            revision=revision,
        )
    except RuntimeFailure as exc:
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH,
            "implementation approval evidence is invalid",
        ) from exc
    if verdict["decision"] != "approve":
        _fail(
            ErrorCode.IDENTITY_MISMATCH,
            "implementation approval evidence is not approved",
        )
    review_result = record.get("review_result")
    if not isinstance(review_result, Mapping) or review_result.get(
        "task_evidence"
    ) != dict(task_evidence):
        _fail(ErrorCode.IDENTITY_MISMATCH, "implementation review result is missing")
    writer_result = record.get("writer_result")
    if not isinstance(writer_result, Mapping) or not isinstance(
        writer_result.get("body"), str
    ):
        _fail(ErrorCode.IDENTITY_MISMATCH, "implementation writer result is missing")

    verification = record.get("verification")
    if not isinstance(verification, Mapping) or set(verification) != _VERIFICATION_KEYS:
        _fail(ErrorCode.IDENTITY_MISMATCH, "verification evidence is incomplete")
    if verification.get("revision") != revision:
        _fail(
            ErrorCode.IDENTITY_MISMATCH, "verification revision does not match approval"
        )
    passed = verification.get("passed")
    if not isinstance(passed, bool):
        _fail(ErrorCode.IDENTITY_MISMATCH, "verification passed field is invalid")
    cleanup_confirmed = verification.get("cleanup_confirmed")
    if not isinstance(cleanup_confirmed, bool):
        _fail(ErrorCode.IDENTITY_MISMATCH, "verification cleanup evidence is invalid")
    top_error = verification.get("error")
    if passed:
        if top_error is not None or cleanup_confirmed is not True:
            _fail(
                ErrorCode.IDENTITY_MISMATCH,
                "successful verification evidence is invalid",
            )
    elif not isinstance(top_error, str) or not top_error:
        _fail(ErrorCode.IDENTITY_MISMATCH, "failed verification evidence is incomplete")
    else:
        _require_bounded_error(top_error, "verification error")
    commands = verification.get("commands")
    if not isinstance(commands, list) or (
        len(commands) > len(task.verification)
        or require_complete
        and len(commands) != len(task.verification)
        or passed
        and (not require_complete or len(commands) != len(task.verification))
    ):
        _fail(
            ErrorCode.IDENTITY_MISMATCH, "verification command evidence is incomplete"
        )
    for declared, observed in zip(task.verification, commands):
        if (
            not isinstance(observed, Mapping)
            or set(observed) != _VERIFICATION_COMMAND_KEYS
        ):
            _fail(
                ErrorCode.IDENTITY_MISMATCH, "verification command evidence is invalid"
            )
        if (
            observed.get("name") != declared.name
            or observed.get("argv") != list(declared.argv)
            or observed.get("timeout_seconds") != declared.timeout_seconds
        ):
            _fail(
                ErrorCode.IDENTITY_MISMATCH, "verification command binding is invalid"
            )
        returncode = observed.get("returncode")
        if returncode is not None and (
            not isinstance(returncode, int) or isinstance(returncode, bool)
        ):
            _fail(ErrorCode.IDENTITY_MISMATCH, "verification returncode is invalid")
        _require_sha256(observed.get("stdout_sha256"), "verification stdout")
        _require_sha256(observed.get("stderr_sha256"), "verification stderr")
        command_error = observed.get("error")
        if passed:
            if returncode != 0 or command_error is not None:
                _fail(
                    ErrorCode.IDENTITY_MISMATCH,
                    "successful verification has a failed command",
                )
        elif returncode == 0:
            if command_error is not None:
                _fail(ErrorCode.IDENTITY_MISMATCH, "successful command has an error")
        elif not isinstance(command_error, str) or not command_error:
            _fail(ErrorCode.IDENTITY_MISMATCH, "failed command evidence is incomplete")
        else:
            _require_bounded_error(command_error, "verification command error")
    if status == "completed" and passed is not True:
        _fail(ErrorCode.IDENTITY_MISMATCH, "completed task has failed verification")
    if status == "verification_failed" and passed is not False:
        _fail(
            ErrorCode.IDENTITY_MISMATCH,
            "verification_failed task has no failed verification",
        )
    return verification


def _require_verification_retry(
    task: TaskSpec, record: Mapping[str, object], max_rounds: int
) -> None:
    _validate_verification(
        task,
        record,
        status="verification_failed",
        require_complete=False,
    )
    verification = cast(Mapping[str, object], record["verification"])
    if verification.get("cleanup_confirmed") is not True:
        _fail(
            ErrorCode.ORDER_VIOLATION,
            "verification cleanup is unconfirmed; user consultation required",
        )
    rounds = _review_rounds(record)
    if rounds["implementation"] >= max_rounds:
        _fail(
            ErrorCode.ORDER_VIOLATION,
            "maximum review rounds reached; user consultation required",
        )


def _require_declared_task(state: Mapping[str, object], task: TaskSpec) -> None:
    declared = parse_task_specs(state.get("task_specs", []))
    match = next((item for item in declared if item.task_id == task.task_id), None)
    if match is None:
        _fail(
            ErrorCode.INVALID_REQUEST,
            "TaskSpec must be declared by the user at startup",
        )
    if match != task:
        _fail(
            ErrorCode.IDENTITY_MISMATCH,
            "TaskSpec differs from the declared startup specification",
        )


def prepare_dispatch(
    state: dict[str, object], request: TaskDispatch, *, revision: str | None = None
) -> tuple[dict[str, object], str]:
    """Validate and durably prepare one writer or reviewer assignment."""

    max_rounds = _max_review_rounds(state)
    if not isinstance(request, TaskDispatch) or not isinstance(request.task, TaskSpec):
        _fail(ErrorCode.INVALID_REQUEST, "TaskSpec is required")
    _require_declared_task(state, request.task)
    message = _require_message(request)
    tasks = _mutable_tasks(state)
    task = request.task
    current = tasks.get(task.task_id)

    if current is None:
        if request.role not in _WRITER_ROLES:
            _fail(
                ErrorCode.ORDER_VIOLATION,
                "a new TaskSpec may be assigned only to Planner or Worker",
            )
        for dependency in task.dependencies:
            prior = tasks.get(dependency)
            if not isinstance(prior, Mapping):
                _fail(
                    ErrorCode.ORDER_VIOLATION,
                    f"task dependency {dependency!r} is not completed",
                )
            _parse_saved_task(dependency, prior, max_rounds)
            if prior.get("status") != "completed":
                _fail(
                    ErrorCode.ORDER_VIOLATION,
                    f"task dependency {dependency!r} is not completed",
                )
        stage = "plan" if request.role is Role.PLANNER else "implementation"
        record: dict[str, object] = {
            "spec": task.as_dict(),
            "digest": task_digest(task),
            "dispatch_id": None,
            "status": "running",
            "role": request.role.value,
            "writer_role": request.role.value,
            "stage": stage,
            "revision": None,
            "review_rounds": {"plan": 0, "implementation": 0},
        }
        prompt = _task_prompt(task, message)
        tasks[task.task_id] = record
        return record, prompt

    if not isinstance(current, Mapping):
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved TaskSpec is invalid")
    _require_record_task(current, task)
    status = current.get("status")
    rounds = _review_rounds(current)

    if request.role is Role.REVIEWER:
        if status not in {"awaiting_plan_review", "awaiting_implementation_review"}:
            if status == "consultation_required":
                _fail(
                    ErrorCode.ORDER_VIOLATION,
                    "user consultation is required before another dispatch",
                )
            _fail(
                ErrorCode.ORDER_VIOLATION,
                "review requires an acknowledged plan or implementation result",
            )
        stage = "plan" if status == "awaiting_plan_review" else "implementation"
        if rounds[stage] >= max_rounds:
            _fail(
                ErrorCode.ORDER_VIOLATION,
                "maximum review rounds reached; user consultation required",
            )
        saved_revision = current.get("revision")
        if stage == "implementation":
            if not isinstance(revision, str) or not revision:
                _fail(
                    ErrorCode.INVALID_REQUEST,
                    "workspace revision is required for implementation review",
                )
            resolved_revision = revision
        else:
            if not isinstance(saved_revision, str) or not saved_revision:
                _fail(ErrorCode.IDENTITY_MISMATCH, "plan revision is missing")
            if revision is not None and revision != saved_revision:
                _fail(
                    ErrorCode.IDENTITY_MISMATCH, "plan revision does not match result"
                )
            resolved_revision = saved_revision
        previous_body = _result_body(current)
        prompt = review_prompt(
            task,
            stage=stage,
            revision=resolved_revision,
            result_body=previous_body,
            message=message,
        )
        updated = dict(current)
        updated_rounds = dict(rounds)
        updated_rounds[stage] += 1
        updated["review_rounds"] = updated_rounds
        updated["status"] = f"reviewing_{stage}"
        updated["role"] = Role.REVIEWER.value
        updated["stage"] = stage
        updated["review_source_dispatch_id"] = current.get("dispatch_id")
        updated["revision"] = resolved_revision
        tasks[task.task_id] = updated
        return updated, prompt

    expected_writer = _writer_status(status)
    if status == "verification_failed":
        _require_verification_retry(task, current, max_rounds)
    if expected_writer is None or request.role is not expected_writer:
        if status == "consultation_required":
            _fail(
                ErrorCode.ORDER_VIOLATION,
                "user consultation is required before another dispatch",
            )
        _fail(ErrorCode.ORDER_VIOLATION, "writer does not match the task stage")
    updated = dict(current)
    updated["status"] = "running"
    updated["role"] = request.role.value
    updated["writer_role"] = request.role.value
    updated["stage"] = "implementation" if request.role is Role.WORKER else "plan"
    updated["revision"] = None
    prompt = _task_prompt(task, message)
    writer_result = current.get("writer_result")
    if isinstance(writer_result, Mapping) and isinstance(
        writer_result.get("body"), str
    ):
        prompt += "\n\n前段の作成結果（参照資料）:\n" + writer_result["body"]
    if isinstance(current.get("task_evidence"), Mapping):
        prompt += "\n\n同じTaskSpecに対するレビュー判定:\n" + json.dumps(
            dict(current["task_evidence"]), ensure_ascii=False
        )
    tasks[task.task_id] = updated
    return updated, prompt


def validate_task_assignment(
    state: Mapping[str, object], assignment: Mapping[str, object]
) -> TaskSpec | None:
    tasks = state.get("tasks")
    if "task_spec" not in assignment:
        if isinstance(tasks, Mapping) and any(
            isinstance(record, Mapping)
            and record.get("dispatch_id") == assignment.get("dispatch_id")
            for record in tasks.values()
        ):
            _fail(ErrorCode.IDENTITY_MISMATCH, "TaskSpec is missing")
        return None
    task = TaskSpec.from_dict(assignment["task_spec"])
    record = tasks.get(task.task_id) if isinstance(tasks, Mapping) else None
    if (
        not isinstance(record, Mapping)
        or record.get("spec") != task.as_dict()
        or record.get("digest") != task_digest(task)
        or record.get("dispatch_id") != assignment.get("dispatch_id")
        or record.get("status") not in _TASK_STATUSES
    ):
        _fail(ErrorCode.IDENTITY_MISMATCH, "TaskSpec does not match its saved dispatch")
    return task


def validate_saved_tasks(state: Mapping[str, object]) -> None:
    if "task_specs" in state:
        parse_task_specs(state["task_specs"])
    if "tasks" not in state:
        return
    max_rounds = _max_review_rounds(state)
    tasks = state.get("tasks")
    if not isinstance(tasks, Mapping):
        _fail(ErrorCode.INVALID_REQUEST, "saved TaskSpecs are invalid")
    for key, record in tasks.items():
        task = _parse_saved_task(key, record, max_rounds)
        _require_declared_task(state, task)


def _review_verdict(
    record: Mapping[str, object], result: Mapping[str, object]
) -> dict[str, object]:
    task = TaskSpec.from_dict(record.get("spec"))
    stage = record.get("stage")
    revision = record.get("revision")
    if stage not in _REVIEW_STAGES or not isinstance(revision, str) or not revision:
        _fail(ErrorCode.IDENTITY_MISMATCH, "review binding is incomplete")
    evidence = result.get("task_evidence")
    if not isinstance(evidence, Mapping):
        _fail(ErrorCode.BACKEND_PROTOCOL_FAILURE, "trusted review verdict is missing")
    try:
        encoded = json.dumps(dict(evidence), ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise RuntimeFailure(
            ErrorCode.BACKEND_PROTOCOL_FAILURE, "trusted review verdict is invalid"
        ) from exc
    return parse_review(
        encoded,
        task=task,
        stage=stage,
        revision=revision,
    )


def acknowledge_task(state: dict[str, object], result: Mapping[str, object]) -> None:
    """Consume one provider result without granting completion authority."""

    if "tasks" not in state:
        return
    _max_review_rounds(state)
    tasks = _mutable_tasks(state)
    dispatch_id = result.get("dispatch_id")
    if not isinstance(dispatch_id, str) or not dispatch_id:
        _fail(ErrorCode.IDENTITY_MISMATCH, "task result dispatch identity is missing")
    matches = [
        (task_id, record)
        for task_id, record in tasks.items()
        if isinstance(record, Mapping) and record.get("dispatch_id") == dispatch_id
    ]
    if len(matches) != 1:
        _fail(ErrorCode.IDENTITY_MISMATCH, "task result dispatch identity is unknown")
    task_id, current = matches[0]
    if not isinstance(current, Mapping):
        _fail(ErrorCode.IDENTITY_MISMATCH, "saved TaskSpec is invalid")
    record = dict(current)
    status = record.get("status")
    role = result.get("role")
    outcome = result.get("outcome")
    body = result.get("body")
    if (
        not isinstance(role, str)
        or not isinstance(outcome, str)
        or not isinstance(body, str)
    ):
        _fail(ErrorCode.BACKEND_PROTOCOL_FAILURE, "task result fields are invalid")
    expected_role = record.get("role")
    if role != expected_role:
        _fail(ErrorCode.IDENTITY_MISMATCH, "task result role does not match assignment")
    if outcome not in {"succeeded", "failed"}:
        _fail(ErrorCode.BACKEND_PROTOCOL_FAILURE, "task result outcome is invalid")
    if status not in {"running", "reviewing_plan", "reviewing_implementation"}:
        _fail(ErrorCode.ORDER_VIOLATION, "task result was already consumed")

    if outcome == "failed":
        record["status"] = "failed"
        record["result"] = dict(result)
        tasks[task_id] = record
        return

    if status == "running":
        if role not in {Role.PLANNER.value, Role.WORKER.value}:
            _fail(ErrorCode.IDENTITY_MISMATCH, "writer result role is invalid")
        record["status"] = (
            "awaiting_plan_review"
            if role == Role.PLANNER.value
            else "awaiting_implementation_review"
        )
        record["stage"] = "plan" if role == Role.PLANNER.value else "implementation"
        record["revision"] = (
            hashlib.sha256(body.encode("utf-8")).hexdigest()
            if role == Role.PLANNER.value
            else None
        )
        record["result"] = dict(result)
        record["writer_result"] = dict(result)
        tasks[task_id] = record
        return

    if role != Role.REVIEWER.value:
        _fail(ErrorCode.IDENTITY_MISMATCH, "review result role is invalid")
    verdict = _review_verdict(record, result)
    decision = verdict["decision"]
    stage = cast(str, record["stage"])
    record["task_evidence"] = verdict
    record["review_result"] = dict(result)
    record["result"] = dict(result)
    if decision == "consult":
        record["status"] = "consultation_required"
    elif stage == "plan":
        record["status"] = (
            "plan_approved" if decision == "approve" else "plan_changes_requested"
        )
    else:
        record["status"] = (
            "implementation_approved"
            if decision == "approve"
            else "implementation_changes_requested"
        )
    tasks[task_id] = record
