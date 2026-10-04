"""Bounded Stop coordination for named Orca parallel runs.

The Run Delivery is a shared FIFO object.  Stop may drain completion owners
individually, but it must leave a question, an invalid/unknown batch, or an
unknown remote effect retained until the whole batch can be acknowledged.
Provider cleanup for an owner that cannot use the ordinary read/release path
is recorded in the existing cleanup journal; state ownership is deliberately
left intact until the backend's normal final cleanup is safe.
"""

from __future__ import annotations

import copy
import time
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, cast

from .adapters import remove_owned_tree
from .cleanup import (
    cleanup_assignment_phase,
    cleanup_journal_path,
    journal_assignment,
    load_cleanup_journal,
    write_cleanup_journal,
)
from .contracts import (
    DeliveryAck,
    DeliveryRef,
    ErrorCode,
    NodeRef,
    Role,
    RoleRead,
    RoleRelease,
    RoleWait,
    RuntimeFailure,
    StopResult,
)
from .mcp_protocol import MAX_READ_LINES, MAX_TIMEOUT_MS, MIN_TIMEOUT_MS
from .runtime import resolve_state_role

if TYPE_CHECKING:
    from .orca_tasks import OrcaTasks


STOP_WAIT_SECONDS = 30.0


def _fail(code: ErrorCode, message: str) -> NoReturn:
    raise RuntimeFailure(code, message)


def _role(node_id: str, assignment: Mapping[str, object]) -> NodeRef:
    kind = assignment.get("role_kind")
    if not isinstance(kind, str):
        _fail(ErrorCode.IDENTITY_MISMATCH, "Orca assignment role kind is invalid")
    try:
        return NodeRef(node_id, Role(kind))
    except ValueError as exc:
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH, "Orca assignment role kind is invalid"
        ) from exc


def _roles(state: Mapping[str, object]) -> dict[str, dict[str, object]]:
    raw = state.get("roles")
    if not isinstance(raw, dict):
        _fail(ErrorCode.IDENTITY_MISMATCH, "Orca parallel roles are invalid")
    result: dict[str, dict[str, object]] = {}
    for node_id, assignment in raw.items():
        if not isinstance(node_id, str) or not isinstance(assignment, dict):
            _fail(ErrorCode.IDENTITY_MISMATCH, "Orca parallel assignment is invalid")
        if assignment.get("role") != node_id:
            _fail(ErrorCode.IDENTITY_MISMATCH, "Orca assignment identity is invalid")
        result[node_id] = assignment
    return result


def _assignment(state: Mapping[str, object], node_id: str) -> dict[str, object]:
    assignment = _roles(state).get(node_id)
    if assignment is None:
        _fail(ErrorCode.IDENTITY_MISMATCH, "Orca assignment is no longer active")
    return assignment


def _batch(state: Mapping[str, object]) -> Mapping[str, object] | None:
    value = state.get("orca_delivery_batch")
    return value if isinstance(value, Mapping) else None


def _batch_members(batch: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    members = batch.get("members")
    if not isinstance(members, list) or not members:
        return ()
    if any(not isinstance(member, Mapping) for member in members):
        return ()
    return tuple(cast(Mapping[str, object], member) for member in members)


def _question_acked(assignment: Mapping[str, object]) -> bool:
    question = assignment.get("orca_question")
    return (
        isinstance(question, Mapping)
        and question.get("phase") == "acked"
        and assignment.get("pending_delivery_id") is None
    )


def _deadline_timeout(deadline: float) -> int:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return 0
    return max(MIN_TIMEOUT_MS, min(MAX_TIMEOUT_MS, int(remaining * 1_000)))


def _stopping_tasks(tasks: OrcaTasks) -> OrcaTasks:
    if tasks.stopping:
        return tasks
    return type(tasks)(tasks.backend, stopping=True)


def _snapshot(tasks: OrcaTasks) -> dict[str, object]:
    with tasks.transaction(progress=False) as state:
        return copy.deepcopy(state)


def _stop_fence(tasks: OrcaTasks) -> None:
    with tasks.transaction(progress=False) as state:
        if "pending_role_start" in state or "pending_coordinator_start" in state:
            _fail(ErrorCode.BUSY, "Orca startup cleanup is pending")
        records = state.get("tasks")
        if isinstance(records, Mapping) and any(
            isinstance(record, Mapping) and record.get("status") == "verifying"
            for record in records.values()
        ):
            _fail(ErrorCode.BUSY, "verification cleanup is unconfirmed")
        state["orca_stop_requested"] = True
        tasks.save(state)


def _journal_for(tasks: OrcaTasks, state: Mapping[str, object]) -> dict[str, object]:
    return load_cleanup_journal(state, tasks.path)


def _write_journal(tasks: OrcaTasks, journal: Mapping[str, object]) -> None:
    write_cleanup_journal(cleanup_journal_path(tasks.path), journal)


def _mark_released(tasks: OrcaTasks, node_id: str) -> None:
    """Record proof already produced by the public read/release path."""

    with tasks.transaction(progress=False) as state:
        _assignment(state, node_id)
        journal = _journal_for(tasks, state)
        entry = journal_assignment(journal, node_id)
        if entry.get("remote") == "unknown":
            _fail(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "Orca remote cleanup effect is unknown; inspect before retry",
            )
        entry["remote"] = "done"
        entry["local"] = "done"
        _write_journal(tasks, journal)


def _prune_journal(tasks: OrcaTasks, journal: Mapping[str, object]) -> None:
    with tasks.transaction(progress=False) as state:
        active = set(_roles(state))
        entries = journal.get("assignments")
        if not isinstance(entries, list):
            _fail(ErrorCode.BACKEND_PROTOCOL_FAILURE, "Orca cleanup journal is invalid")
        proposed = copy.deepcopy(dict(journal))
        proposed["assignments"] = [
            entry
            for entry in entries
            if isinstance(entry, Mapping) and entry.get("role") in active
        ]
        _write_journal(tasks, proposed)


def _provider_error(message: str, *, unknown: bool = True) -> RuntimeFailure:
    suffix = "; inspect Orca before retry" if unknown else ""
    return RuntimeFailure(ErrorCode.BACKEND_PROTOCOL_FAILURE, f"{message}{suffix}")


def _mark_journal_unknown(
    tasks: OrcaTasks, state: Mapping[str, object], node_id: str
) -> None:
    journal = _journal_for(tasks, state)
    entry = journal_assignment(journal, node_id)
    entry["remote"] = "unknown"
    _write_journal(tasks, journal)


def _scoped_cleanup(
    tasks: OrcaTasks, node_id: str, expected: Mapping[str, object]
) -> None:
    """Perform backend cleanup for one assignment, retaining its state owner."""

    backend = tasks.backend
    with tasks.transaction(progress=False) as state:
        assignment = _assignment(state, node_id)
        result = assignment.get("orca_result")
        if (
            not isinstance(result, Mapping)
            or result.get("cleanup_confirmed") is not True
        ):
            _fail(ErrorCode.BUSY, "ACP process cleanup is unconfirmed")
        for field in (
            "role",
            "role_kind",
            "task_id",
            "dispatch_id",
            "terminal_handle",
            "launch_nonce",
            "prompt_path",
            "provider_private_root",
            "snapshot_root",
            "task_spec",
            "task_stage",
            "task_revision",
            "task_workspace_revision",
        ):
            if assignment.get(field) != expected.get(field):
                _fail(
                    ErrorCode.IDENTITY_MISMATCH, "Orca assignment changed during Stop"
                )
        journal = _journal_for(tasks, state)
        entry = journal_assignment(journal, node_id)
        remote_stage = entry.get("remote")
        local_stage = entry.get("local")
        if remote_stage == "unknown":
            raise _provider_error("Orca remote cleanup effect is unknown")
        if remote_stage == "done" and local_stage == "done":
            return
        workspace = Path(str(state["workspace"]))
        worktree_id = str(state["worktree_id"])
        run_id = str(state["run_id"])
        try:
            backend._verify_assignment(
                assignment=assignment,
                role=node_id,
                run_id=run_id,
                worktree_id=worktree_id,
                workspace=workspace,
            )
        except Exception as exc:
            _mark_journal_unknown(tasks, state, node_id)
            raise _provider_error(
                "Orca assignment cleanup identity is unconfirmed"
            ) from exc

        dispatch_id = assignment.get("dispatch_id")
        terminal_id = assignment.get("terminal_handle")
        if not isinstance(dispatch_id, str) or not isinstance(terminal_id, str):
            _mark_journal_unknown(tasks, state, node_id)
            raise _provider_error("Orca assignment cleanup identity is incomplete")
        if remote_stage == "pending":
            entry["remote"] = "worker_started"
            _write_journal(tasks, journal)
            try:
                verdict = backend._client.worker_stop(
                    dispatch_id=dispatch_id, cwd=workspace
                )
                backend._require_worker_stop(verdict, dispatch_id=dispatch_id)
                if state.get("version") in {4, 5}:
                    from .orca import WorkerStopContextOnlyVerdict

                    if not isinstance(verdict, WorkerStopContextOnlyVerdict):
                        raise TypeError(
                            "named Orca stop requires a context-only receipt"
                        )
            except Exception as exc:
                _mark_journal_unknown(tasks, state, node_id)
                raise _provider_error("Orca worker stop effect is unknown") from exc
            if verdict.closed_agent_terminal:
                entry["remote"] = "done"
            else:
                entry["remote"] = "worker_done"
            _write_journal(tasks, journal)
            remote_stage = entry["remote"]
        if remote_stage == "worker_done":
            try:
                backend._verify_assignment(
                    assignment=assignment,
                    role=node_id,
                    run_id=run_id,
                    worktree_id=worktree_id,
                    workspace=workspace,
                )
            except Exception as exc:
                _mark_journal_unknown(tasks, state, node_id)
                raise _provider_error(
                    "Orca terminal cleanup identity is unconfirmed"
                ) from exc
            entry["remote"] = "terminal_started"
            _write_journal(tasks, journal)
            try:
                closed = backend._client.terminal_close(
                    terminal_id=terminal_id, cwd=workspace
                )
                backend._require_terminal_close(closed, terminal_id=terminal_id)
            except Exception as exc:
                _mark_journal_unknown(tasks, state, node_id)
                raise _provider_error("Orca terminal close effect is unknown") from exc
            entry["remote"] = "done"
            _write_journal(tasks, journal)
            remote_stage = "done"
        if remote_stage != "done":
            _fail(
                ErrorCode.BACKEND_PROTOCOL_FAILURE, "Orca remote cleanup is incomplete"
            )

        if local_stage == "done":
            return
        if not isinstance(local_stage, str):
            _fail(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "Orca local cleanup stage is invalid",
            )
        execution = str(entry.get("execution"))
        transport = str(entry.get("transport"))
        cleanup_role = resolve_state_role(state, node_id)
        if local_stage == "pending" and execution == "background":
            local_stage = cleanup_assignment_phase(
                cleanup_role,
                assignment,
                state_path=tasks.path,
                execution=execution,
                transport=transport,
                local_stage=local_stage,
                remove_tree=remove_owned_tree,
            )
            entry["local"] = local_stage
            _write_journal(tasks, journal)
        if local_stage in {"pending", "roots_done"} and (
            execution == "background" or transport == "acp"
        ):
            entry["local"] = "prompt_started"
            _write_journal(tasks, journal)
            local_stage = "prompt_started"
        entry["local"] = cleanup_assignment_phase(
            cleanup_role,
            assignment,
            state_path=tasks.path,
            execution=execution,
            transport=transport,
            local_stage=local_stage,
            remove_tree=remove_owned_tree,
        )
        _write_journal(tasks, journal)


def _process_batch_member(
    tasks: OrcaTasks,
    member: Mapping[str, object],
) -> tuple[bool, RuntimeFailure | None]:
    node_id = member.get("role")
    kind = member.get("kind")
    if not isinstance(node_id, str) or not node_id:
        return False, _provider_error("Orca batch owner is unknown", unknown=False)
    with tasks.transaction(progress=False) as state:
        assignment = _assignment(state, node_id)
        role = _role(node_id, assignment)
        batch = state.get("orca_delivery_batch")
        delivery_id = batch.get("delivery_id") if isinstance(batch, Mapping) else None
        if not isinstance(delivery_id, str):
            return False, _provider_error(
                "Orca batch Delivery ID is invalid", unknown=False
            )
        if assignment.get("pending_orca_effect") is not None:
            return False, _provider_error("Orca batch effect is unconfirmed")
        stage = assignment.get("pending_delivery_stage")
    if kind == "worker_done":
        try:
            if stage == "observed":
                tasks.read(RoleRead(role, MAX_READ_LINES))
            with tasks.transaction(progress=False) as state:
                assignment = _assignment(state, node_id)
                stage = assignment.get("pending_delivery_stage")
            if stage == "read":
                tasks.release(RoleRelease(role))
                _mark_released(tasks, node_id)
            elif stage == "released":
                _mark_released(tasks, node_id)
            elif stage != "released":
                return False, RuntimeFailure(
                    ErrorCode.ORDER_VIOLATION,
                    "read and release every completion before Stop ACK",
                )
            return True, None
        except RuntimeFailure as exc:
            return False, exc
        except (OSError, RuntimeError, TypeError, ValueError):
            return False, _provider_error("Orca completion cleanup is unknown")
    if kind == "question":
        with tasks.transaction(progress=False) as state:
            assignment = _assignment(state, node_id)
            expected = copy.deepcopy(assignment)
            question = assignment.get("orca_question")
            message_id = (
                question.get("message_id") if isinstance(question, Mapping) else None
            )
            if (
                isinstance(question, Mapping)
                and question.get("phase") == "replied"
                and isinstance(message_id, str)
                and assignment.get("pending_question_ids") == [message_id]
                and assignment.get("replied_question_ids") == [message_id]
            ):
                return True, None
            result = assignment.get("orca_result")
            can_cleanup = (
                isinstance(result, Mapping) and result.get("cleanup_confirmed") is True
            )
        if can_cleanup:
            _scoped_cleanup(tasks, node_id, expected)
        return False, RuntimeFailure(
            ErrorCode.ORDER_VIOLATION,
            "Stop cannot fabricate an answer for an unanswered Orca question",
        )
    with tasks.transaction(progress=False) as state:
        assignment = _assignment(state, node_id)
        expected = copy.deepcopy(assignment)
        result = assignment.get("orca_result")
        can_cleanup = (
            isinstance(result, Mapping) and result.get("cleanup_confirmed") is True
        )
    if can_cleanup:
        _scoped_cleanup(tasks, node_id, expected)
    return False, _provider_error("Orca batch requires inspection", unknown=False)


def _batch_ready(
    tasks: OrcaTasks, state: Mapping[str, object], batch: Mapping[str, object]
) -> bool:
    delivery_id = batch.get("delivery_id")
    if not isinstance(delivery_id, str):
        return False
    try:
        tasks._ready_batch(copy.deepcopy(dict(state)), delivery_id)
    except (RuntimeFailure, OSError, RuntimeError, TypeError, ValueError, KeyError):
        return False
    return True


def _process_unobserved(
    tasks: OrcaTasks,
    node_id: str,
    deadline: float,
) -> tuple[bool, bool, RuntimeFailure | None]:
    cleanup = delivery_unknown = should_wait = False
    role: NodeRef | None = None
    wait_timeout = 0
    with tasks.transaction(progress=False) as state:
        assignment = _assignment(state, node_id)
        expected = copy.deepcopy(assignment)
        raw_result = assignment.get("orca_result")
        result = raw_result if isinstance(raw_result, Mapping) else None
        pending_id = assignment.get("pending_delivery_id")
        batch = _batch(state)
        if assignment.get("pending_orca_effect") is not None:
            return False, True, _provider_error("Orca assignment effect is unconfirmed")
        if pending_id is not None:
            return False, True, None
        if result is None:
            return (
                False,
                True,
                (
                    RuntimeFailure(
                        ErrorCode.BUSY,
                        "ACP process cleanup is unconfirmed after Stop request",
                    )
                    if time.monotonic() >= deadline
                    else None
                ),
            )
        if result is not None and result.get("cleanup_confirmed") is not True:
            return (
                False,
                True,
                RuntimeFailure(ErrorCode.BUSY, "ACP process cleanup is unconfirmed"),
            )
        safe_result = result is not None and result.get("cleanup_confirmed") is True
        if (
            result is not None
            and result.get("cleanup_confirmed") is True
            and result.get("notification_expected") is False
        ):
            cleanup = True
        elif _question_acked(assignment):
            return (
                False,
                True,
                RuntimeFailure(
                    ErrorCode.ORDER_VIOLATION,
                    "question owner cleanup requires a trusted cleanup result",
                ),
            )
        elif isinstance(batch, Mapping) and batch.get("phase") == "observed":
            return False, True, None
        elif isinstance(batch, Mapping) and batch.get("phase") in {
            "invalid",
            "acknowledging",
        }:
            if not safe_result:
                return (
                    False,
                    True,
                    _provider_error("Orca shared Delivery state is unconfirmed"),
                )
            cleanup = True
        elif result is not None or time.monotonic() < deadline:
            wait_timeout = _deadline_timeout(deadline)
            role = _role(node_id, assignment)
            should_wait = True
        else:
            return (
                False,
                True,
                RuntimeFailure(
                    ErrorCode.BUSY,
                    "ACP process cleanup is unconfirmed after Stop request",
                ),
            )
    if should_wait:
        if wait_timeout <= 0:
            if not safe_result:
                return (
                    False,
                    True,
                    RuntimeFailure(
                        ErrorCode.BUSY, "Orca completion Delivery is unconfirmed"
                    ),
                )
            cleanup = delivery_unknown = True
        else:
            try:
                assert role is not None
                receipt = tasks.wait(RoleWait(role, wait_timeout))
            except RuntimeFailure as exc:
                return False, True, exc
            if receipt.delivery_id is not None:
                return True, True, None
            if time.monotonic() < deadline:
                return False, True, None
            if not safe_result:
                return (
                    False,
                    True,
                    RuntimeFailure(
                        ErrorCode.BUSY, "Orca completion Delivery is unconfirmed"
                    ),
                )
            cleanup = delivery_unknown = True
    if not cleanup:
        return False, True, _provider_error("Orca safe cleanup did not progress")
    try:
        _scoped_cleanup(tasks, node_id, expected)
    except RuntimeFailure as exc:
        return False, True, exc
    if delivery_unknown:
        return (
            True,
            True,
            RuntimeFailure(ErrorCode.BUSY, "Orca completion Delivery is unconfirmed"),
        )
    return True, False, None


def stop_parallel(tasks: OrcaTasks) -> StopResult:
    """Stop a named Orca parallel Run with a bounded whole-batch fence."""

    _stop_fence(tasks)
    draining = _stopping_tasks(tasks)
    deadline = time.monotonic() + max(0.0, float(STOP_WAIT_SECONDS))
    first_error: RuntimeFailure | None = None
    evidence_remains = False
    while True:
        evidence_remains = False
        state = _snapshot(draining)
        batch = _batch(state)
        if batch is not None and batch.get("phase") in {"invalid", "acknowledging"}:
            evidence_remains = True
            if first_error is None:
                first_error = _provider_error(
                    "Orca shared Delivery state is unconfirmed"
                )
        progress = False
        if batch is not None and batch.get("phase") == "observed":
            members = _batch_members(batch)
            if not members:
                evidence_remains = True
                first_error = first_error or _provider_error(
                    "Orca shared Delivery members are invalid", unknown=False
                )
            else:
                for member in members:
                    try:
                        changed, error = _process_batch_member(draining, member)
                        progress = progress or changed
                        if error is not None and first_error is None:
                            first_error = error
                        if member.get("kind") != "worker_done" or error is not None:
                            evidence_remains = True
                    except RuntimeFailure as exc:
                        evidence_remains = True
                        first_error = first_error or exc
                state = _snapshot(draining)
                current_batch = _batch(state)
                try:
                    ready = current_batch is not None and _batch_ready(
                        draining, state, current_batch
                    )
                except RuntimeFailure as exc:
                    ready = False
                    first_error = first_error or exc
                if ready and current_batch is not None:
                    try:
                        journal = _journal_for(draining, state)
                        delivery_id = str(current_batch["delivery_id"])
                        draining.ack(DeliveryAck(DeliveryRef(delivery_id)))
                        _prune_journal(draining, journal)
                        progress = True
                        evidence_remains = False
                    except RuntimeFailure as exc:
                        evidence_remains = True
                        first_error = first_error or exc
                else:
                    evidence_remains = True
        state = _snapshot(draining)
        for node_id, assignment in tuple(_roles(state).items()):
            if _batch(state) is not None and any(
                member.get("role") == node_id
                for member in _batch_members(_batch(state) or {})
            ):
                continue
            try:
                changed, remains, error = _process_unobserved(
                    draining, node_id, deadline
                )
                progress = progress or changed
                evidence_remains = evidence_remains or remains
                if error is not None and first_error is None:
                    first_error = error
            except RuntimeFailure as exc:
                evidence_remains = True
                first_error = first_error or exc
        state = _snapshot(draining)
        if (
            first_error is not None
            and _batch(state) is not None
            and first_error.code
            in {ErrorCode.ORDER_VIOLATION, ErrorCode.BACKEND_PROTOCOL_FAILURE}
            and not any(
                assignment.get("orca_result") is None
                for assignment in _roles(state).values()
            )
        ):
            raise first_error
        if _batch(state) is None and not evidence_remains:
            try:
                draining.backend._await_program_exit()
                with draining.transaction(progress=False):
                    return draining.backend._stop_locked()
            except RuntimeFailure as exc:
                first_error = first_error or exc
                evidence_remains = True
        if not _roles(state):
            if first_error is None:
                first_error = _provider_error(
                    "Orca Stop retained no owner but did not finish"
                )
            raise first_error
        if time.monotonic() >= deadline or (first_error is not None and not progress):
            raise first_error or RuntimeFailure(
                ErrorCode.BUSY, "ACP process cleanup is unconfirmed after Stop request"
            )
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


__all__ = ("STOP_WAIT_SECONDS", "stop_parallel")
