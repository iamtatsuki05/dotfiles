"""Pure action selection for the validated Mainless program state.

The native runtime owns validation and mutation.  This module only reads the
saved snapshot and returns one bounded action; the caller re-reads the state
under the lifecycle reservation and uses the existing typed requests.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

from .contracts import NodeRef, Role, RuntimeFailure
from .named_graph import GraphSpec
from .parallel_admission import admission_blocker
from .task_execution import program_wave as canonical_program_wave
from .task_execution import task_consultation
from .task_spec import TaskSpec, parse_task_specs

ProgramActionKind = Literal[
    "dispatch",
    "wait",
    "wait_user",
    "acknowledge",
    "verify",
    "seal_wave",
    "reopen_wave",
    "verify_wave",
    "next_wave",
    "complete",
    "pause",
    "reject",
]
ProgramStage = Literal["plan", "implementation", "review", "verification"]
ProgramWavePhase = Literal["writers", "reviewers", "verification"]

_ACTION_KINDS = frozenset(
    {
        "dispatch",
        "wait",
        "wait_user",
        "acknowledge",
        "verify",
        "seal_wave",
        "reopen_wave",
        "verify_wave",
        "next_wave",
        "complete",
        "pause",
        "reject",
    }
)
_STAGES = frozenset({"plan", "implementation", "review", "verification"})
_PHASES = frozenset({"writers", "reviewers", "verification"})


@dataclass(frozen=True, slots=True)
class ProgramAction:
    """One pure coordinator decision."""

    kind: ProgramActionKind
    task_id: str | None = None
    role: NodeRef | None = None
    stage: ProgramStage | None = None
    message: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in _ACTION_KINDS:
            raise ValueError("program action kind is invalid")
        if self.task_id is not None and (
            not isinstance(self.task_id, str) or not self.task_id
        ):
            raise ValueError("program action task_id is invalid")
        if self.role is not None and not isinstance(self.role, NodeRef):
            raise TypeError("program action role must be a NodeRef")
        if self.stage is not None and self.stage not in _STAGES:
            raise ValueError("program action stage is invalid")
        if self.message is not None and (
            not isinstance(self.message, str)
            or not self.message
            or len(self.message) > 512
        ):
            raise ValueError("program action message is invalid")
        if self.kind == "dispatch":
            if self.task_id is None or self.role is None:
                raise ValueError("dispatch requires task_id and role")
            if self.stage not in {"plan", "implementation", "review"}:
                raise ValueError("dispatch requires a dispatch stage")
            if self.message is None:
                raise ValueError("dispatch requires a message")
        elif self.kind == "verify":
            if self.task_id is None or self.stage != "verification":
                raise ValueError("verify requires task_id and verification stage")
            if self.role is not None or self.message is not None:
                raise ValueError("verify cannot target a role or carry a message")
        elif self.kind in {
            "seal_wave",
            "verify_wave",
            "next_wave",
            "complete",
        } and any(
            value is not None
            for value in (self.task_id, self.role, self.stage, self.message)
        ):
            raise ValueError(f"{self.kind} cannot carry task data")
        elif self.kind == "acknowledge" and any(
            value is not None for value in (self.task_id, self.stage, self.message)
        ):
            raise ValueError("acknowledge can only carry an exact role")
        elif self.kind == "reopen_wave" and self.role is not None:
            raise ValueError("reopen_wave cannot target a role")


def _mapping(state: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = state.get(key)
    if not isinstance(value, Mapping):
        raise TypeError(f"{key}_invalid")
    return cast(Mapping[str, object], value)


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("text_invalid")
    return value


def _is_orca(state: Mapping[str, object]) -> bool:
    return state.get("runtime") == "orca"


def _delivery_keys(state: Mapping[str, object]) -> tuple[str, str]:
    """Return the selected runtime's result and question field names."""

    return (
        ("orca_result", "orca_question")
        if _is_orca(state)
        else (
            "native_result",
            "native_question",
        )
    )


def _validate_delivery_namespace(
    state: Mapping[str, object], assignment: Mapping[str, object]
) -> None:
    """Reject a state that projects one runtime's Delivery into the other."""

    result_key, question_key = _delivery_keys(state)
    foreign_result = "native_result" if result_key == "orca_result" else "orca_result"
    foreign_question = (
        "native_question" if question_key == "orca_question" else "orca_question"
    )
    if foreign_result in state or foreign_question in state:
        raise ValueError("delivery_namespace_mismatch")
    if foreign_result in assignment or foreign_question in assignment:
        raise ValueError("delivery_namespace_mismatch")


def _load(
    state: Mapping[str, object],
) -> tuple[GraphSpec, tuple[TaskSpec, ...], Mapping[str, object]]:
    # These are the exact serialized state fields.  Alternate typed inputs and
    # compatibility aliases are intentionally not accepted.
    graph = GraphSpec.from_dict(_mapping(state, "graph"))
    catalog = parse_task_specs(state.get("task_specs"))
    tasks = _mapping(state, "tasks")
    coordination = graph.coordination
    version = state.get("version")
    if version == 4:
        if (
            coordination.mode != "program"
            or coordination.dispatch_mode != "serial"
            or coordination.max_active != 1
        ):
            raise ValueError("program_serial_required")
    elif version == 5:
        if coordination.mode != "program" or coordination.dispatch_mode != "parallel":
            raise ValueError("program_parallel_required")
        if (
            not isinstance(coordination.max_active, int)
            or isinstance(coordination.max_active, bool)
            or coordination.max_active < 1
        ):
            raise ValueError("program_parallel_cap_invalid")
    else:
        raise ValueError("program_state_version_invalid")
    return graph, catalog, tasks


def _node(graph: GraphSpec, raw: Mapping[str, object], field: str) -> NodeRef:
    node_id = _text(raw.get(field))
    kind_field = "writer_kind" if field == "writer_role" else "role_kind"
    target = NodeRef(node_id, Role(_text(raw.get(kind_field))))
    if graph.node(node_id) != target:
        raise ValueError("node_identity_mismatch")
    return target


def _active(
    state: Mapping[str, object], graph: GraphSpec
) -> tuple[NodeRef, Mapping[str, object]] | None:
    roles = _mapping(state, "roles")
    if len(roles) > 1:
        raise ValueError("serial_active_assignment")
    if not roles:
        return None
    node_id, raw = next(iter(roles.items()))
    if not isinstance(node_id, str) or not isinstance(raw, Mapping):
        raise TypeError("assignment_invalid")
    assignment = cast(Mapping[str, object], raw)
    _validate_delivery_namespace(state, assignment)
    if assignment.get("role") != node_id:
        raise ValueError("assignment_identity_mismatch")
    return _node(graph, assignment, "role"), assignment


def _parallel_active(
    state: Mapping[str, object],
    graph: GraphSpec,
    catalog: tuple[TaskSpec, ...],
) -> tuple[tuple[NodeRef, Mapping[str, object], TaskSpec], ...]:
    """Validate and return every v5 assignment with its logical TaskSpec."""

    roles = _mapping(state, "roles")
    by_id = {task.task_id: task for task in catalog}
    active: list[tuple[NodeRef, Mapping[str, object], TaskSpec]] = []
    for node_id, raw in roles.items():
        if not isinstance(node_id, str) or not isinstance(raw, Mapping):
            raise TypeError("assignment_invalid")
        assignment = cast(Mapping[str, object], raw)
        _validate_delivery_namespace(state, assignment)
        if assignment.get("role") != node_id:
            raise ValueError("assignment_identity_mismatch")
        target = _node(graph, assignment, "role")
        raw_spec = assignment.get("task_spec")
        task = TaskSpec.from_dict(raw_spec)
        declared = by_id.get(task.task_id)
        if declared is None or declared != task:
            raise ValueError("assignment_task_spec_mismatch")
        active.append((target, assignment, task))
    if len(active) > graph.coordination.max_active:
        raise ValueError("parallel_active_cap_exceeded")
    return tuple(active)


def _native_parallel_delivery_actions(
    tasks: Mapping[str, object],
    active: tuple[tuple[NodeRef, Mapping[str, object], TaskSpec], ...],
) -> tuple[tuple[ProgramAction, ...], tuple[ProgramAction, ...]]:
    """Return drain actions first and unanswered-question waits second."""

    completions: list[ProgramAction] = []
    answered_questions: list[ProgramAction] = []
    unanswered_questions: list[ProgramAction] = []
    for role, assignment, task in active:
        pending_id = assignment.get("pending_delivery_id")
        if pending_id is None:
            result = assignment.get("native_result")
            question = assignment.get("native_question")
            if isinstance(question, Mapping) and question.get("phase") in {
                "failed",
                "cancelling",
            }:
                completions.append(
                    ProgramAction(
                        "reject",
                        task_id=task.task_id,
                        role=role,
                        message="question_delivery_failed",
                    )
                )
            elif result is not None or (
                isinstance(question, Mapping) and question.get("phase") == "published"
            ):
                completions.append(
                    ProgramAction(
                        "wait",
                        task_id=task.task_id,
                        role=role,
                        stage=_stage_for(role, _record(tasks, task.task_id)),
                        message="delivery_observation_required",
                    )
                )
            continue
        if not isinstance(pending_id, str) or not pending_id:
            raise ValueError("pending_delivery_invalid")
        kind = assignment.get("pending_delivery_kind")
        stage = assignment.get("pending_delivery_stage")
        record = _record(tasks, task.task_id)
        action_stage = _stage_for(role, record)
        if kind == "worker_done":
            if stage in {"observed", "read"}:
                completions.append(
                    ProgramAction(
                        "wait",
                        task_id=task.task_id,
                        role=role,
                        stage=action_stage,
                        message="completion_drain_required",
                    )
                )
            elif stage == "released":
                completions.append(ProgramAction("acknowledge", role=role))
            else:
                raise ValueError("pending_completion_stage_invalid")
            continue
        if kind != "question" or stage != "observed":
            raise ValueError("pending_delivery_unknown")
        question = assignment.get("native_question")
        if not isinstance(question, Mapping):
            raise TypeError("question_outbox_missing")
        if question.get("phase") in {"failed", "cancelling"}:
            completions.append(
                ProgramAction(
                    "reject",
                    task_id=task.task_id,
                    role=role,
                    message="question_delivery_failed",
                )
            )
            continue
        if question.get("delivery_id") != pending_id:
            raise ValueError("question_delivery_mismatch")
        ids = question.get("message_ids")
        answers = question.get("answers")
        if not isinstance(ids, (list, tuple)) or not isinstance(answers, Mapping):
            raise TypeError("question_outbox_invalid")
        if set(cast(Mapping[str, object], answers)) == set(ids):
            answered_questions.append(ProgramAction("acknowledge", role=role))
        else:
            unanswered_questions.append(
                ProgramAction(
                    "wait_user",
                    task_id=task.task_id,
                    role=role,
                    stage=action_stage,
                    message="question_answer_required",
                )
            )
    return tuple(completions + answered_questions), tuple(unanswered_questions)


def _orca_batch_actions(
    state: Mapping[str, object],
    graph: GraphSpec,
    tasks: Mapping[str, object],
    active: tuple[tuple[NodeRef, Mapping[str, object], TaskSpec], ...],
) -> tuple[tuple[ProgramAction, ...], tuple[ProgramAction, ...]]:
    """Drain one Run-level FIFO batch without acknowledging individual roles."""

    if state.get("pending_orca_effect") is not None:
        return (
            (ProgramAction("pause", message="orca_delivery_batch_effect_unconfirmed"),),
            (),
        )
    batch_value = state.get("orca_delivery_batch")
    if batch_value is None:
        for role, assignment, task in active:
            if assignment.get("pending_orca_effect") is not None:
                return (
                    (
                        ProgramAction(
                            "pause",
                            task_id=task.task_id,
                            role=role,
                            message="orca_delivery_batch_effect_unconfirmed",
                        ),
                    ),
                    (),
                )
            if assignment.get("pending_delivery_id") is not None:
                return (
                    (
                        ProgramAction(
                            "pause",
                            task_id=task.task_id,
                            role=role,
                            message="orca_delivery_batch_missing",
                        ),
                    ),
                    (),
                )
            question = assignment.get("orca_question")
            if assignment.get("orca_result") is not None or (
                isinstance(question, Mapping)
                and question.get("phase") in {"asking", "recorded"}
            ):
                return (
                    (
                        ProgramAction(
                            "wait",
                            task_id=task.task_id,
                            role=role,
                            stage=_stage_for(role, _record(tasks, task.task_id)),
                            message="delivery_observation_required",
                        ),
                    ),
                    (),
                )
        return (), ()
    if not isinstance(batch_value, Mapping):
        return (ProgramAction("pause", message="orca_delivery_batch_invalid"),), ()
    batch = cast(Mapping[str, object], batch_value)
    phase = batch.get("phase")
    if phase != "observed":
        return (
            (
                ProgramAction(
                    "pause",
                    message=(
                        "orca_delivery_batch_effect_unconfirmed"
                        if phase == "acknowledging"
                        else "orca_delivery_batch_invalid"
                    ),
                ),
            ),
            (),
        )
    delivery_id = batch.get("delivery_id")
    members_value = batch.get("members")
    if (
        not isinstance(delivery_id, str)
        or not delivery_id
        or not isinstance(members_value, list)
        or not members_value
    ):
        return (ProgramAction("pause", message="orca_delivery_batch_invalid"),), ()
    if state.get("pending_orca_effect") is not None:
        return (
            (ProgramAction("pause", message="orca_delivery_batch_effect_unconfirmed"),),
            (),
        )
    for role, assignment, task in active:
        if assignment.get("pending_orca_effect") is not None:
            return (
                (
                    ProgramAction(
                        "pause",
                        task_id=task.task_id,
                        role=role,
                        message="orca_delivery_batch_effect_unconfirmed",
                    ),
                ),
                (),
            )
    active_by_role = {
        role.node_id: (role, assignment, task) for role, assignment, task in active
    }
    member_roles: set[str] = set()
    drains: list[ProgramAction] = []
    unanswered: list[ProgramAction] = []
    ready_members = 0
    for raw_member in members_value:
        if not isinstance(raw_member, Mapping):
            return (ProgramAction("pause", message="orca_delivery_batch_invalid"),), ()
        member = cast(Mapping[str, object], raw_member)
        node_id = member.get("role")
        role_kind = member.get("role_kind")
        if not isinstance(node_id, str) or not isinstance(role_kind, str):
            return (ProgramAction("pause", message="orca_delivery_batch_invalid"),), ()
        if node_id in member_roles:
            return (ProgramAction("pause", message="orca_delivery_batch_invalid"),), ()
        member_roles.add(node_id)
        try:
            role = NodeRef(node_id, Role(role_kind))
            if graph.node(node_id) != role:
                raise ValueError("batch_node_identity_mismatch")
        except (TypeError, ValueError, KeyError):
            return (ProgramAction("pause", message="orca_delivery_batch_invalid"),), ()
        current = active_by_role.get(node_id)
        if current is None:
            return (
                ProgramAction(
                    "pause",
                    role=role,
                    message="orca_delivery_batch_assignment_unknown",
                ),
            ), ()
        active_role, assignment, task = current
        if active_role != role or assignment.get("pending_delivery_id") != delivery_id:
            return (
                ProgramAction(
                    "pause",
                    task_id=task.task_id,
                    role=role,
                    message="orca_delivery_batch_identity_mismatch",
                ),
            ), ()
        for field in (
            "role_kind",
            "task_id",
            "dispatch_id",
            "terminal_handle",
            "launch_nonce",
        ):
            if member.get(field) != assignment.get(field):
                return (
                    (
                        ProgramAction(
                            "pause",
                            task_id=task.task_id,
                            role=role,
                            message="orca_delivery_batch_identity_mismatch",
                        ),
                    ),
                    (),
                )
        if assignment.get("pending_orca_effect") is not None:
            return (
                (
                    ProgramAction(
                        "pause",
                        task_id=task.task_id,
                        role=role,
                        message="orca_delivery_batch_effect_unconfirmed",
                    ),
                ),
                (),
            )
        kind = member.get("kind")
        stage = assignment.get("pending_delivery_stage")
        action_stage = _stage_for(role, _record(tasks, task.task_id))
        if kind == "worker_done":
            if assignment.get("pending_delivery_kind") != "worker_done":
                return (
                    ProgramAction("pause", message="orca_delivery_batch_invalid"),
                ), ()
            if stage in {"observed", "read"}:
                drains.append(
                    ProgramAction(
                        "wait",
                        task_id=task.task_id,
                        role=role,
                        stage=action_stage,
                        message="completion_drain_required",
                    )
                )
            elif stage == "released":
                ready_members += 1
            else:
                return (
                    ProgramAction("pause", message="orca_delivery_batch_invalid"),
                ), ()
            continue
        if kind != "question" or assignment.get("pending_delivery_kind") != "question":
            return (ProgramAction("pause", message="orca_delivery_batch_invalid"),), ()
        if stage != "observed":
            return (ProgramAction("pause", message="orca_delivery_batch_invalid"),), ()
        question = assignment.get("orca_question")
        if not isinstance(question, Mapping):
            return (ProgramAction("pause", message="orca_delivery_batch_invalid"),), ()
        if member.get("message_id") != question.get("message_id"):
            return (
                (
                    ProgramAction(
                        "pause",
                        task_id=task.task_id,
                        role=role,
                        message="orca_delivery_batch_identity_mismatch",
                    ),
                ),
                (),
            )
        if question.get("phase") == "replied":
            ready_members += 1
        elif question.get("phase") == "observed":
            unanswered.append(
                ProgramAction(
                    "wait_user",
                    task_id=task.task_id,
                    role=role,
                    stage=action_stage,
                    message="question_answer_required",
                )
            )
        else:
            return (
                ProgramAction(
                    "pause",
                    task_id=task.task_id,
                    role=role,
                    message="orca_delivery_batch_invalid",
                ),
            ), ()
    if drains:
        return tuple(drains), tuple(unanswered)
    if unanswered:
        return (), tuple(unanswered)
    if ready_members == len(members_value):
        return (ProgramAction("acknowledge"),), ()
    return (ProgramAction("pause", message="orca_delivery_batch_invalid"),), ()


def _parallel_delivery_actions(
    state: Mapping[str, object],
    graph: GraphSpec,
    tasks: Mapping[str, object],
    active: tuple[tuple[NodeRef, Mapping[str, object], TaskSpec], ...],
) -> tuple[tuple[ProgramAction, ...], tuple[ProgramAction, ...]]:
    if _is_orca(state):
        return _orca_batch_actions(state, graph, tasks, active)
    return _native_parallel_delivery_actions(tasks, active)


def _parallel_admit(
    graph: GraphSpec,
    catalog: tuple[TaskSpec, ...],
    active: tuple[tuple[NodeRef, Mapping[str, object], TaskSpec], ...],
    target: NodeRef,
    task: TaskSpec,
) -> bool:
    assignments = {role.node_id: dict(assignment) for role, assignment, _ in active}
    return admission_blocker(graph, catalog, assignments, target, task) is None


def _parallel_wait(
    active: tuple[tuple[NodeRef, Mapping[str, object], TaskSpec], ...],
    *,
    message: str = "active_assignment_drain_required",
) -> ProgramAction | None:
    if not active:
        return None
    role, _assignment, task = active[0]
    return ProgramAction("wait", task_id=task.task_id, role=role, message=message)


def _wave(
    state: Mapping[str, object],
) -> tuple[tuple[str, ...], ProgramWavePhase, str | None] | None:
    if "program_wave" not in state:
        return None
    try:
        raw = canonical_program_wave(state)
    except RuntimeFailure as exc:
        raise ValueError("program_wave_invalid") from exc
    values = raw["task_ids"]
    phase = raw["phase"]
    revision = raw["revision"]
    if not isinstance(values, list) or not isinstance(phase, str):
        raise TypeError("program_wave_invalid")
    if phase not in _PHASES:
        raise ValueError("program_wave_invalid")
    if revision is not None and not isinstance(revision, str):
        raise TypeError("program_wave_invalid")
    return (
        tuple(_text(value) for value in values),
        cast(ProgramWavePhase, phase),
        revision,
    )


def _record(tasks: Mapping[str, object], task_id: str) -> Mapping[str, object] | None:
    value = tasks.get(task_id)
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("task_record_invalid")
    return cast(Mapping[str, object], value)


def _status(record: Mapping[str, object] | None) -> str | None:
    if record is None:
        return None
    value = record.get("status")
    return _text(value)


def _ready(task: TaskSpec, tasks: Mapping[str, object]) -> bool:
    return all(
        _status(_record(tasks, dependency)) == "completed"
        for dependency in task.dependencies
    )


def _route_node(graph: GraphSpec, task: TaskSpec, field: str) -> NodeRef | None:
    node_id = getattr(graph.route(task.task_id), field)
    return None if node_id is None else graph.node(node_id)


def _dispatch(
    task: TaskSpec,
    role: NodeRef,
    stage: Literal["plan", "implementation", "review"],
) -> ProgramAction:
    message = {
        "plan": "宣言されたTaskSpecに沿って計画を作成してください。",
        "implementation": "宣言されたTaskSpecに沿って実装してください。",
        "review": "宣言されたTaskSpecに沿ってレビューしてください。",
    }[stage]
    return ProgramAction(
        "dispatch",
        task_id=task.task_id,
        role=role,
        stage=stage,
        message=message,
    )


def _stage_for(
    role: NodeRef, record: Mapping[str, object] | None
) -> ProgramStage | None:
    if role.kind is Role.REVIEWER:
        return "review"
    if record is None:
        return None
    value = record.get("stage")
    return value if value in {"plan", "implementation"} else None


def _pending(
    state: Mapping[str, object],
    graph: GraphSpec,
    tasks: Mapping[str, object],
    active: tuple[NodeRef, Mapping[str, object]] | None,
) -> ProgramAction | None:
    result_key, question_key = _delivery_keys(state)
    if _is_orca(state) and state.get("pending_orca_effect") is not None:
        return ProgramAction("pause", message="orca_delivery_effect_unconfirmed")
    pending_id = state.get("pending_delivery_id")
    if pending_id is None:
        if active is not None:
            role, assignment = active
            task_id = assignment.get("task_id")
            record = _record(tasks, task_id) if isinstance(task_id, str) else None
            return ProgramAction(
                "wait",
                task_id=task_id if isinstance(task_id, str) else None,
                role=role,
                stage=_stage_for(role, record),
                message="active_assignment_drain_required",
            )
        if state.get(result_key) is not None:
            return ProgramAction("pause", message="completion_delivery_required")
        return None
    if not isinstance(pending_id, str) or not pending_id:
        return ProgramAction("reject", message="pending_delivery_invalid")
    kind = state.get("pending_delivery_kind")
    stage = state.get("pending_delivery_stage")
    if kind == "worker_done":
        if stage == "released" and active is None:
            return ProgramAction("acknowledge")
        if active is None:
            return ProgramAction("pause", message="completion_assignment_unknown")
        role, assignment = active
        task_id = assignment.get("task_id")
        record = _record(tasks, task_id) if isinstance(task_id, str) else None
        return ProgramAction(
            "wait",
            task_id=task_id if isinstance(task_id, str) else None,
            role=role,
            stage=_stage_for(role, record),
            message="completion_drain_required",
        )
    if kind != "question" or stage != "observed":
        return ProgramAction("pause", message="pending_delivery_unknown")
    raw_question: object
    if _is_orca(state) and active is not None:
        raw_question = active[1].get(question_key)
    else:
        raw_question = state.get(question_key)
    if not isinstance(raw_question, Mapping):
        return ProgramAction("reject", message="question_outbox_missing")
    question = cast(Mapping[str, object], raw_question)
    if not _is_orca(state) and question.get("delivery_id") != pending_id:
        return ProgramAction("reject", message="question_delivery_mismatch")
    if _is_orca(state):
        question_phase = question.get("phase")
        orca_question_role: NodeRef | None = (
            active[0]
            if active is not None
            else (
                _node(graph, question, "role")
                if question.get("role") is not None
                else None
            )
        )
        task_id = question.get("task_id")
        if not isinstance(task_id, str) and active is not None:
            assignment_task = active[1].get("task_spec")
            task_id = TaskSpec.from_dict(assignment_task).task_id
        if question_phase == "replied":
            return ProgramAction("acknowledge")
        if question_phase == "observed":
            return ProgramAction(
                "wait_user",
                task_id=task_id if isinstance(task_id, str) else None,
                role=orca_question_role,
                message="question_answer_required",
            )
        return ProgramAction(
            "pause",
            task_id=task_id if isinstance(task_id, str) else None,
            role=orca_question_role,
            message="question_delivery_invalid",
        )
    ids = question.get("message_ids")
    answers = question.get("answers")
    if not isinstance(ids, (list, tuple)) or not isinstance(answers, Mapping):
        return ProgramAction("reject", message="question_outbox_invalid")
    question_role: NodeRef | None = (
        _node(graph, question, "role") if question.get("role") is not None else None
    )
    task_id = question.get("task_id")
    if set(cast(Mapping[str, object], answers)) != set(ids):
        return ProgramAction(
            "wait_user",
            task_id=task_id if isinstance(task_id, str) else None,
            role=question_role,
            message="question_answer_required",
        )
    return ProgramAction("acknowledge")


def _rounds(record: Mapping[str, object], stage: str) -> int:
    rounds = record.get("review_rounds")
    if not isinstance(rounds, Mapping):
        raise TypeError("review_rounds_invalid")
    value = rounds.get(stage)
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("review_rounds_invalid")
    return value


def _review(
    graph: GraphSpec,
    task: TaskSpec,
    record: Mapping[str, object],
    max_rounds: int,
    revision: str | None,
) -> ProgramAction:
    status = _status(record)
    stage = "plan" if status == "awaiting_plan_review" else "implementation"
    field = "plan_reviewer" if stage == "plan" else "implementation_reviewer"
    reviewer = _route_node(graph, task, field)
    if reviewer is None:
        return ProgramAction("reject", task_id=task.task_id, message="reviewer_missing")
    if _rounds(record, stage) >= max_rounds:
        return ProgramAction(
            "wait_user",
            task_id=task.task_id,
            role=reviewer,
            stage="review",
            message="review_round_limit",
        )
    if stage == "implementation" and revision is None:
        return ProgramAction(
            "pause", task_id=task.task_id, message="wave_revision_required"
        )
    return _dispatch(task, reviewer, "review")


def _parallel_select(
    state: Mapping[str, object],
    graph: GraphSpec,
    catalog: tuple[TaskSpec, ...],
    tasks: Mapping[str, object],
    wave: tuple[tuple[str, ...], ProgramWavePhase, str | None],
    active: tuple[tuple[NodeRef, Mapping[str, object], TaskSpec], ...],
) -> ProgramAction:
    """Select one v5 action while allowing independent assignments to overlap."""

    max_rounds = state.get("max_review_rounds")
    if (
        not isinstance(max_rounds, int)
        or isinstance(max_rounds, bool)
        or max_rounds < 1
    ):
        return ProgramAction("reject", message="max_review_rounds_invalid")
    task_ids, phase, revision = wave
    by_id = {task.task_id: task for task in catalog}
    try:
        members = tuple(by_id[task_id] for task_id in task_ids)
    except KeyError as exc:
        raise ValueError("program_wave_task_unknown") from exc

    if all(_status(_record(tasks, task.task_id)) == "completed" for task in members):
        waiting = _parallel_wait(active)
        if waiting is not None:
            return waiting
        if all(
            _status(_record(tasks, task.task_id)) == "completed" for task in catalog
        ):
            return ProgramAction("complete")
        return ProgramAction("next_wave")

    active_ids = {task.task_id for _role, _assignment, task in active}
    writer_candidates: list[
        tuple[TaskSpec, NodeRef, Literal["plan", "implementation"]]
    ] = []
    review_candidates: list[tuple[TaskSpec, Mapping[str, object]]] = []
    verify_candidates: list[tuple[TaskSpec, Mapping[str, object]]] = []
    unanswered_consultations: list[ProgramAction] = []
    transition_wait: ProgramAction | None = None
    blocked_task: tuple[str, str] | None = None
    reopen_task_id: str | None = None

    for task in members:
        record = _record(tasks, task.task_id)
        status = _status(record)
        if status == "failed":
            return ProgramAction(
                "pause", task_id=task.task_id, message="terminal_task_failure"
            )
        if status == "consultation_required":
            if record is None:
                return ProgramAction("reject", message="task_record_invalid")
            pending = task_consultation(state, record)
            if pending is None:
                return ProgramAction("reject", message="consultation_state_invalid")
            if pending["answered"] is not True:
                unanswered_consultations.append(
                    ProgramAction(
                        "wait_user",
                        task_id=task.task_id,
                        stage="review",
                        message="reviewer_consultation_required",
                    )
                )
                continue
            if phase != "writers":
                if reopen_task_id is None:
                    reopen_task_id = task.task_id
                continue
            stage = _text(record.get("stage"))
            if _rounds(record, stage) >= max_rounds:
                unanswered_consultations.append(
                    ProgramAction(
                        "wait_user", task_id=task.task_id, message="review_round_limit"
                    )
                )
                continue

        if status in {
            "running",
            "reviewing_plan",
            "reviewing_implementation",
            "verifying",
        }:
            if task.task_id not in active_ids and transition_wait is None:
                transition_wait = ProgramAction(
                    "wait", task_id=task.task_id, message="state_transition_pending"
                )
            continue

        plan_node = _route_node(graph, task, "plan_writer")
        implementation_node = _route_node(graph, task, "implementation_writer")
        candidate: tuple[NodeRef, Literal["plan", "implementation"]] | None = None
        if phase == "writers":
            if status is None and _ready(task, tasks):
                if plan_node is not None:
                    candidate = (plan_node, "plan")
                elif implementation_node is not None:
                    candidate = (implementation_node, "implementation")
            elif plan_node is not None and (
                status == "plan_changes_requested"
                or (status == "verification_failed" and implementation_node is None)
            ):
                candidate = (plan_node, "plan")
            elif (
                status
                in {
                    "plan_approved",
                    "implementation_changes_requested",
                    "verification_failed",
                }
                and implementation_node is not None
            ):
                candidate = (implementation_node, "implementation")
            elif status == "consultation_required" and record is not None:
                stage = _text(record.get("stage"))
                writer = plan_node if stage == "plan" else implementation_node
                if writer is not None:
                    candidate = (
                        writer,
                        cast(Literal["plan", "implementation"], stage),
                    )
            if candidate is not None and task.task_id not in active_ids:
                writer_candidates.append((task, candidate[0], candidate[1]))
            if status == "awaiting_plan_review" and implementation_node is not None:
                review_candidates.append((task, cast(Mapping[str, object], record)))
        elif phase == "reviewers":
            if status in {"awaiting_plan_review", "awaiting_implementation_review"}:
                review_candidates.append((task, cast(Mapping[str, object], record)))
            elif status in {"plan_approved", "implementation_approved"}:
                verify_candidates.append((task, cast(Mapping[str, object], record)))
            elif (
                status
                in {
                    "plan_changes_requested",
                    "implementation_changes_requested",
                    "verification_failed",
                }
                and reopen_task_id is None
            ):
                reopen_task_id = task.task_id
        else:
            if status in {"plan_approved", "implementation_approved"}:
                verify_candidates.append((task, cast(Mapping[str, object], record)))
            elif (
                status
                in {
                    "implementation_changes_requested",
                    "plan_changes_requested",
                    "verification_failed",
                }
                and reopen_task_id is None
            ):
                reopen_task_id = task.task_id

    def admitted(task: TaskSpec, target: NodeRef, *, reason: str) -> bool:
        if _parallel_admit(graph, catalog, active, target, task):
            return True
        nonlocal blocked_task
        if blocked_task is None:
            blocked_task = (task.task_id, reason)
        return False

    if phase == "writers":
        for task, writer, stage in writer_candidates:
            if admitted(task, writer, reason="parallel_admission_blocked"):
                return _dispatch(task, writer, stage)
        for task, record in review_candidates:
            reviewer = _route_node(
                graph,
                task,
                "plan_reviewer"
                if _status(record) == "awaiting_plan_review"
                else "implementation_reviewer",
            )
            if reviewer is not None and admitted(
                task, reviewer, reason="review_admission_blocked"
            ):
                return _review(graph, task, record, max_rounds, revision)
        final_stages = [
            "plan"
            if _route_node(graph, task, "implementation_writer") is None
            else "implementation"
            for task in members
        ]
        if all(
            _status(_record(tasks, task.task_id)) == f"awaiting_{stage}_review"
            for task, stage in zip(members, final_stages, strict=True)
        ):
            waiting = _parallel_wait(active)
            if waiting is not None:
                return waiting
            return ProgramAction("seal_wave")
    elif phase == "reviewers":
        for task, record in review_candidates:
            reviewer = _route_node(
                graph,
                task,
                "plan_reviewer"
                if _status(record) == "awaiting_plan_review"
                else "implementation_reviewer",
            )
            if reviewer is not None and admitted(
                task, reviewer, reason="review_admission_blocked"
            ):
                return _review(graph, task, record, max_rounds, revision)
        if all(
            _status(_record(tasks, task.task_id))
            == (
                "plan_approved"
                if _route_node(graph, task, "implementation_writer") is None
                else "implementation_approved"
            )
            for task in members
        ):
            waiting = _parallel_wait(active)
            if waiting is not None:
                return waiting
            return ProgramAction("verify_wave")
    else:
        if verify_candidates:
            waiting = _parallel_wait(active)
            if waiting is not None:
                return waiting
            task, _record_value = verify_candidates[0]
            return ProgramAction("verify", task_id=task.task_id, stage="verification")

    if reopen_task_id is not None:
        if unanswered_consultations:
            return unanswered_consultations[0]
        waiting = _parallel_wait(active)
        if waiting is not None:
            return waiting
        return ProgramAction("reopen_wave", task_id=reopen_task_id)
    if transition_wait is not None:
        return transition_wait
    if blocked_task is not None and active:
        waiting = _parallel_wait(active, message=blocked_task[1])
        if waiting is not None:
            return waiting
    if blocked_task is not None:
        return ProgramAction("pause", task_id=blocked_task[0], message=blocked_task[1])
    if unanswered_consultations:
        return unanswered_consultations[0]
    waiting = _parallel_wait(active)
    if waiting is not None:
        return waiting
    blocked = next(
        (
            task
            for task in members
            if _record(tasks, task.task_id) is None and not _ready(task, tasks)
        ),
        None,
    )
    if blocked is not None:
        return ProgramAction(
            "pause", task_id=blocked.task_id, message="dependency_not_completed"
        )
    if phase == "verification" and all(
        _status(_record(tasks, task.task_id)) == "completed" for task in members
    ):
        return ProgramAction("next_wave")
    return ProgramAction("pause", message="wave_transition_required")


def _select(
    state: Mapping[str, object],
    graph: GraphSpec,
    catalog: tuple[TaskSpec, ...],
    tasks: Mapping[str, object],
    wave: tuple[tuple[str, ...], ProgramWavePhase, str | None],
) -> ProgramAction:
    max_rounds = state.get("max_review_rounds")
    if (
        not isinstance(max_rounds, int)
        or isinstance(max_rounds, bool)
        or max_rounds < 1
    ):
        return ProgramAction("reject", message="max_review_rounds_invalid")
    task_ids, phase, revision = wave
    by_id = {task.task_id: task for task in catalog}
    members = tuple(by_id[task_id] for task_id in task_ids)

    for task in members:
        record = _record(tasks, task.task_id)
        if record is not None and record.get("status") == "consultation_required":
            pending = task_consultation(state, record)
            assert pending is not None
            if pending["answered"] is not True:
                return ProgramAction(
                    "wait_user",
                    task_id=task.task_id,
                    stage="review",
                    message="reviewer_consultation_required",
                )

    if all(_status(_record(tasks, task.task_id)) == "completed" for task in members):
        if all(
            _status(_record(tasks, task.task_id)) == "completed" for task in catalog
        ):
            return ProgramAction("complete")
        return ProgramAction("next_wave")

    writer_candidate: (
        tuple[TaskSpec, NodeRef, Literal["plan", "implementation"]] | None
    ) = None
    plan_review: tuple[TaskSpec, Mapping[str, object]] | None = None
    final_review: tuple[TaskSpec, Mapping[str, object]] | None = None
    verify: tuple[TaskSpec, Mapping[str, object]] | None = None

    for task in members:
        record = _record(tasks, task.task_id)
        status = _status(record)
        if status == "failed":
            return ProgramAction(
                "pause", task_id=task.task_id, message="terminal_task_failure"
            )
        if status == "consultation_required":
            if record is None:
                return ProgramAction("reject", message="task_record_invalid")
            stage = _text(record.get("stage"))
            if _rounds(record, stage) >= max_rounds:
                return ProgramAction(
                    "wait_user", task_id=task.task_id, message="review_round_limit"
                )
            if phase != "writers":
                return ProgramAction("reopen_wave", task_id=task.task_id)
            if writer_candidate is None:
                writer = _route_node(
                    graph,
                    task,
                    "plan_writer" if stage == "plan" else "implementation_writer",
                )
                if writer is None:
                    return ProgramAction(
                        "reject", task_id=task.task_id, message="writer_missing"
                    )
                writer_candidate = (
                    task,
                    writer,
                    cast(Literal["plan", "implementation"], stage),
                )
        if status in {
            "running",
            "reviewing_plan",
            "reviewing_implementation",
            "verifying",
        }:
            return ProgramAction(
                "wait", task_id=task.task_id, message="state_transition_pending"
            )
        plan_node = _route_node(graph, task, "plan_writer")
        implementation_node = _route_node(graph, task, "implementation_writer")
        if plan_node is not None:
            if status is None and _ready(task, tasks) and writer_candidate is None:
                writer_candidate = (task, plan_node, "plan")
            elif status == "plan_changes_requested" or (
                status == "verification_failed" and implementation_node is None
            ):
                if phase != "writers":
                    return ProgramAction("reopen_wave", task_id=task.task_id)
                if writer_candidate is None:
                    writer_candidate = (task, plan_node, "plan")
            elif status == "awaiting_plan_review":
                assert record is not None
                if implementation_node is None:
                    if final_review is None:
                        final_review = (task, record)
                elif plan_review is None:
                    plan_review = (task, record)
            elif (
                status == "plan_approved"
                and implementation_node is None
                and verify is None
            ):
                assert record is not None
                verify = (task, record)
        if implementation_node is not None:
            if status is None and plan_node is None:
                if _ready(task, tasks) and writer_candidate is None:
                    writer_candidate = (task, implementation_node, "implementation")
            elif status == "plan_approved":
                if phase == "writers" and writer_candidate is None:
                    writer_candidate = (task, implementation_node, "implementation")
            elif status in {"implementation_changes_requested", "verification_failed"}:
                if phase == "writers":
                    if writer_candidate is None:
                        writer_candidate = (
                            task,
                            implementation_node,
                            "implementation",
                        )
                else:
                    return ProgramAction("reopen_wave", task_id=task.task_id)
            elif status == "awaiting_implementation_review" and final_review is None:
                assert record is not None
                final_review = (task, record)
            elif status == "implementation_approved" and verify is None:
                assert record is not None
                verify = (task, record)

    # Writers always precede implementation review.  Plan review can occur in
    # this phase to unblock the task's implementation writer.
    if phase == "writers":
        if writer_candidate is not None:
            return _dispatch(
                writer_candidate[0], writer_candidate[1], writer_candidate[2]
            )
        if plan_review is not None:
            return _review(graph, plan_review[0], plan_review[1], max_rounds, None)
        if final_review is not None:
            all_awaiting_review = all(
                _status(_record(tasks, task.task_id))
                == (
                    "awaiting_plan_review"
                    if _route_node(graph, task, "implementation_writer") is None
                    else "awaiting_implementation_review"
                )
                for task in members
            )
            if not all_awaiting_review:
                return ProgramAction("pause", message="writer_barrier_incomplete")
            if revision is None:
                return ProgramAction("seal_wave")
            return ProgramAction("pause", message="reviewer_phase_required")
        if verify is not None:
            all_awaiting_review = all(
                _status(_record(tasks, task.task_id))
                == "awaiting_implementation_review"
                for task in members
                if _route_node(graph, task, "implementation_writer") is not None
            )
            if not all_awaiting_review:
                return ProgramAction("pause", message="writer_barrier_incomplete")
            return (
                ProgramAction("seal_wave")
                if revision is None
                else ProgramAction("verify_wave")
            )
    elif phase == "reviewers":
        if plan_review is not None:
            return _review(graph, plan_review[0], plan_review[1], max_rounds, None)
        if writer_candidate is not None:
            return ProgramAction("pause", message="writers_forbidden_in_reviewers")
        if final_review is not None:
            return _review(
                graph,
                final_review[0],
                final_review[1],
                max_rounds,
                revision,
            )
        if verify is not None:
            return ProgramAction("verify_wave")
    else:
        if final_review is not None:
            return ProgramAction("pause", message="reviewers_required")
        if verify is not None:
            saved_revision = verify[1].get(
                "workspace_revision"
                if _route_node(graph, verify[0], "implementation_writer") is None
                else "revision"
            )
            if revision is None or saved_revision != revision:
                return ProgramAction("reopen_wave", task_id=verify[0].task_id)
            return ProgramAction(
                "verify", task_id=verify[0].task_id, stage="verification"
            )

    blocked = next(
        (
            task
            for task in members
            if _record(tasks, task.task_id) is None and not _ready(task, tasks)
        ),
        None,
    )
    if blocked is not None:
        return ProgramAction(
            "pause", task_id=blocked.task_id, message="dependency_not_completed"
        )
    if phase == "writers" and final_review is not None:
        return ProgramAction("seal_wave")
    if phase == "reviewers" and all(
        _status(_record(tasks, task.task_id))
        == (
            "plan_approved"
            if _route_node(graph, task, "implementation_writer") is None
            else "implementation_approved"
        )
        for task in members
    ):
        return ProgramAction("verify_wave")
    if phase == "verification" and all(
        _status(_record(tasks, task.task_id)) == "completed" for task in members
    ):
        return ProgramAction("next_wave")
    return ProgramAction("pause", message="wave_transition_required")


def select_action(state: Mapping[str, object]) -> ProgramAction:
    """Select one bounded program action without mutating ``state``."""

    try:
        graph, catalog, tasks = _load(state)
        if state.get("version") == 5:
            active_parallel = _parallel_active(state, graph, catalog)
            drain, unanswered = _parallel_delivery_actions(
                state, graph, tasks, active_parallel
            )
            if drain:
                return drain[0]
            if _is_orca(state) and "orca_delivery_batch" in state:
                if unanswered:
                    return unanswered[0]
                return ProgramAction("pause", message="orca_delivery_batch_invalid")
            wave = _wave(state)
            if wave is None:
                return ProgramAction("reject", message="program_wave_required")
            action = _parallel_select(
                state, graph, catalog, tasks, wave, active_parallel
            )
            if unanswered and action.kind == "wait":
                return unanswered[0]
            if action.kind in {
                "dispatch",
                "wait",
                "verify",
                "seal_wave",
                "reopen_wave",
                "verify_wave",
                "next_wave",
                "complete",
            }:
                return action
            if action.kind in {"pause", "reject"}:
                if action.message == "wave_transition_required" and unanswered:
                    return unanswered[0]
                return action
            if action.kind == "wait_user":
                return action
            if unanswered:
                return unanswered[0]
            return action
        active = _active(state, graph)
        pending = _pending(state, graph, tasks, active)
        if pending is not None:
            return pending
        wave = _wave(state)
        if wave is None:
            return ProgramAction("reject", message="program_wave_required")
        return _select(state, graph, catalog, tasks, wave)
    except (KeyError, TypeError, ValueError, RuntimeFailure):
        return ProgramAction("reject", message="invalid_program_state")
