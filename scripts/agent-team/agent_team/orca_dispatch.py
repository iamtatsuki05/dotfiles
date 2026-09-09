"""Named Orca ACP assignment startup.

This module owns only the admission and startup seam for the named, serial
Orca runtime.  The caller owns the lifecycle reservation.  Provider execution
stays in the selected scoped ACP profile and Orca remains the owner of the
Task, Dispatch, and terminal identities.
"""

from __future__ import annotations

import copy
import secrets
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import NoReturn, cast

from . import (
    cli,
    mcp_server,
    native_acp_dependencies,
    role_snapshot,
    scoped_acp,
)
from .adapters import remove_owned_tree
from .contracts import (
    Assignment,
    CompletionIdentity,
    DispatchRef,
    ErrorCode,
    LaunchMode,
    NodeRef,
    Role,
    RolePrompt,
    RunRef,
    RuntimeFailure,
    TaskDispatch,
    TaskRef,
    TerminalRef,
)
from .native_acp_dependencies import CodexAcpExecutables, NativeAcpExecutables
from .runtime import (
    MAX_PROMPT_CHARS,
    RuntimeValidationError,
    create_prompt_file,
    read_state,
    remove_prompt_file,
    write_state,
)
from .scoped_acp import (
    SCOPED_AGENT,
    SCOPED_CLIENT,
    SCOPED_POLICY,
    SCOPED_QUESTIONS,
    checked_digest,
)
from .task_execution import task_json
from .task_spec import TaskSpec

run_orca = mcp_server.run_orca
require_nested_string = mcp_server.require_nested_string
validate_acp_dispatch_response = mcp_server.validate_acp_dispatch_response

_ACP_KINDS = frozenset({Role.PLANNER, Role.WORKER, Role.REVIEWER})
_FROZEN_RUN_FIELDS = (
    "run_id",
    "team_id",
    "workspace",
    "state_path",
    "worktree_id",
    "main_terminal",
    "graph",
    "role_specs",
    "task_specs",
    "max_review_rounds",
)
_NATIVE_ASSIGNMENT_FIELDS = frozenset(
    {"runner_pid", "launcher_owned_runner", "native_result"}
)


def _fail(code: ErrorCode, message: str) -> NoReturn:
    raise RuntimeFailure(code, message)


def _required_string(state: Mapping[str, object], key: str) -> str:
    value = state.get(key)
    if not isinstance(value, str) or not value:
        _fail(ErrorCode.IDENTITY_MISMATCH, f"named Orca state is missing {key}")
    return value


def _request_text(request: RolePrompt | TaskDispatch, text: str) -> None:
    if not isinstance(text, str) or not text.strip():
        _fail(ErrorCode.INVALID_REQUEST, "prepared role request text is required")
    if len(text) > MAX_PROMPT_CHARS:
        _fail(
            ErrorCode.INVALID_REQUEST,
            f"prepared role request text exceeds {MAX_PROMPT_CHARS} characters",
        )
    request_text = request.text if isinstance(request, RolePrompt) else request.message
    if not isinstance(request_text, str) or not request_text.strip():
        _fail(ErrorCode.INVALID_REQUEST, "role request message is required")
    if len(request_text) > MAX_PROMPT_CHARS:
        _fail(
            ErrorCode.INVALID_REQUEST,
            f"role request message exceeds {MAX_PROMPT_CHARS} characters",
        )


def _frozen_run(state: Mapping[str, object]) -> dict[str, object]:
    """Capture the saved run bindings that must stay fixed during startup."""

    return {
        key: copy.deepcopy(state[key]) for key in _FROZEN_RUN_FIELDS if key in state
    }


def _marker_identity(marker: Mapping[str, object]) -> tuple[object, ...]:
    return tuple(
        marker.get(key) for key in ("role", "role_kind", "launch_nonce", "path")
    )


def _recheck_run(
    path: Path,
    expected: Mapping[str, object],
    frozen: Mapping[str, object],
    *,
    expected_marker: Mapping[str, object] | None = None,
) -> None:
    try:
        current = read_state(path)
    except (RuntimeFailure, RuntimeValidationError) as exc:
        raise RuntimeFailure(
            ErrorCode.IDENTITY_MISMATCH,
            "named Orca run disappeared during assignment startup",
        ) from exc
    if (
        _frozen_run(current) != dict(frozen)
        or current.get("run_id") != expected.get("run_id")
        or current.get("runtime") != "orca"
    ):
        _fail(
            ErrorCode.IDENTITY_MISMATCH,
            "named Orca run identity changed during assignment startup",
        )
    current_marker = current.get("pending_role_start")
    if expected_marker is None:
        if current_marker is not None:
            _fail(
                ErrorCode.IDENTITY_MISMATCH,
                "named Orca has an unexpected pending startup marker",
            )
    elif not isinstance(current_marker, Mapping) or dict(current_marker) != dict(
        expected_marker
    ):
        _fail(
            ErrorCode.IDENTITY_MISMATCH,
            "named Orca pending startup marker changed",
        )


def _selected_role(request: RolePrompt | TaskDispatch) -> NodeRef:
    target = request.role
    if not isinstance(target, NodeRef) or target.kind not in _ACP_KINDS:
        _fail(
            ErrorCode.INVALID_REQUEST,
            "named Orca requires a selected dispatchable node",
        )
    return target


def _role_spec(state: Mapping[str, object], target: NodeRef) -> dict[str, object]:
    raw_specs = state.get("role_specs")
    raw_spec = raw_specs.get(target.node_id) if isinstance(raw_specs, Mapping) else None
    if not isinstance(raw_spec, Mapping):
        _fail(ErrorCode.IDENTITY_MISMATCH, "selected named Orca role spec is missing")
    spec = dict(raw_spec)
    if spec.get("kind") != target.kind.value:
        _fail(ErrorCode.IDENTITY_MISMATCH, "named Orca role kind differs from its spec")
    if spec.get("provider") not in {"claude", "codex"}:
        _fail(
            ErrorCode.INVALID_REQUEST,
            "named Orca role has no supported scoped ACP provider",
        )
    return spec


def _frozen_spec(spec: Mapping[str, object]) -> dict[str, object]:
    # ``kind`` belongs to the graph identity.  role_snapshot validates the
    # provider profile fields and must not be allowed to silently rewrite it.
    return {key: copy.deepcopy(value) for key, value in spec.items() if key != "kind"}


def _preflight_role(
    state: Mapping[str, object], target: NodeRef, spec: Mapping[str, object]
) -> tuple[NativeAcpExecutables | CodexAcpExecutables, dict[str, object]]:
    frozen = _frozen_spec(spec)
    normalized = copy.deepcopy(frozen)
    try:
        # The existing snapshot helper validates the selected profile and
        # binding shape.  Loading and fingerprinting is kept here so the
        # selected provider is imported and snapshotted exactly once.
        role_snapshot.preflight_scoped_role(
            normalized,
            target,
            Path(_required_string(state, "workspace")),
            preflight=False,
        )
    except RuntimeFailure:
        raise
    except (RuntimeValidationError, ValueError, OSError) as exc:
        raise RuntimeFailure(
            ErrorCode.INVALID_REQUEST,
            "selected scoped ACP profile is unavailable",
        ) from exc

    provider = cast(str, frozen["provider"])
    raw_executables = normalized.get("acp_executables")
    selected: NativeAcpExecutables | CodexAcpExecutables
    try:
        if provider == "codex":
            from . import codex_acp

            selected = CodexAcpExecutables.from_dict(raw_executables)
            selected.verify()
            provider_snapshot = normalized.get("provider_snapshot")
            if not isinstance(provider_snapshot, Mapping):
                raise RuntimeValidationError("Codex provider snapshot is missing")
            codex_acp.verify_snapshot(
                provider_snapshot, Path(_required_string(state, "workspace"))
            )
            adapter = native_acp_dependencies.codex_adapter_snapshot(selected)
        else:
            selected = NativeAcpExecutables.from_dict(raw_executables)
            selected.verify()
            normalized["scoped_wrapper_sha256"] = checked_digest(SCOPED_AGENT)
            normalized["scoped_client_sha256"] = checked_digest(SCOPED_CLIENT)
            normalized["scoped_policy_sha256"] = checked_digest(SCOPED_POLICY)
            normalized["scoped_question_client_sha256"] = checked_digest(
                SCOPED_QUESTIONS
            )
            adapter = native_acp_dependencies.adapter_snapshot(selected)
        normalized["acp_executables"] = selected.as_dict()
    except (RuntimeValidationError, ValueError, OSError) as exc:
        raise RuntimeFailure(
            ErrorCode.INVALID_REQUEST,
            "selected scoped ACP dependencies are unavailable",
        ) from exc
    if normalized != frozen:
        _fail(
            ErrorCode.IDENTITY_MISMATCH,
            "selected scoped ACP snapshot changed since team start",
        )
    if not isinstance(adapter, Mapping):
        _fail(
            ErrorCode.BACKEND_PROTOCOL_FAILURE, "scoped ACP adapter snapshot is invalid"
        )
    return selected, copy.deepcopy(dict(adapter))


def _prepare_task(
    state: Mapping[str, object],
    request: RolePrompt | TaskDispatch,
    task_record: dict[str, object] | None,
) -> tuple[dict[str, object], dict[str, object] | None]:
    prepared = copy.deepcopy(dict(state))
    if isinstance(request, RolePrompt):
        if task_record is not None:
            _fail(
                ErrorCode.INVALID_REQUEST, "RolePrompt cannot carry a TaskSpec record"
            )
        return prepared, None
    if not isinstance(request.task, TaskSpec):
        _fail(ErrorCode.INVALID_REQUEST, "TaskDispatch requires a TaskSpec")
    if task_record is None:
        _fail(
            ErrorCode.INVALID_REQUEST,
            "TaskDispatch requires a prepared TaskSpec record",
        )
    tasks = prepared.get("tasks")
    saved = tasks.get(request.task.task_id) if isinstance(tasks, Mapping) else None
    if not isinstance(saved, Mapping) or dict(saved) != dict(task_record):
        _fail(
            ErrorCode.IDENTITY_MISMATCH,
            "caller TaskSpec record is not in the prepared state",
        )
    return prepared, copy.deepcopy(dict(task_record))


def _write(path: Path, state: dict[str, object]) -> None:
    # The lifecycle reservation is deliberately held by the caller/root.
    write_state(path, state, require_existing=True, reservation_held=True)


def _marker(
    path: Path,
    target: NodeRef,
    launch_nonce: str,
    *,
    prompt_path: Path,
    private_root: Path,
    snapshot_root: Path,
    phase: str,
    cleanup_confirmed: bool,
    task_id: str | None = None,
    terminal_handle: str | None = None,
    dispatch_id: str | None = None,
    dispatch_id_hint: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "path": str(path),
        "role": target.node_id,
        "role_kind": target.kind.value,
        "launch_nonce": launch_nonce,
        "prompt_path": str(prompt_path),
        "provider_private_root": str(private_root),
        "snapshot_root": str(snapshot_root),
        "reason": "named Orca ACP assignment startup",
        "phase": phase,
        "cleanup_confirmed": cleanup_confirmed,
    }
    for key, value in (
        ("task_id", task_id),
        ("terminal_handle", terminal_handle),
        ("dispatch_id", dispatch_id),
        ("dispatch_id_hint", dispatch_id_hint),
    ):
        if value is not None:
            result[key] = value
    return result


def _persist_marker(
    path: Path,
    candidate: Mapping[str, object],
    marker: Mapping[str, object],
) -> None:
    # Marker publication must never persist the caller's prepared TaskSpec
    # clone.  Only the final assignment write may publish that mutable record.
    fresh = read_state(path)
    if _frozen_run(fresh) != _frozen_run(candidate):
        _fail(
            ErrorCode.IDENTITY_MISMATCH,
            "named Orca run changed while retaining ownership",
        )
    existing = fresh.get("pending_role_start")
    if existing is not None and (
        not isinstance(existing, Mapping)
        or _marker_identity(existing) != _marker_identity(marker)
    ):
        _fail(
            ErrorCode.IDENTITY_MISMATCH,
            "named Orca pending startup marker belongs to another assignment",
        )
    fresh["pending_role_start"] = copy.deepcopy(dict(marker))
    _write(path, fresh)


def _clear_marker(state: dict[str, object]) -> None:
    state.pop("pending_role_start", None)


def _cleanup_local(
    prompt_path: Path | None,
    roots: tuple[Path | None, ...],
    *,
    state_dir: Path,
    role: NodeRef,
    launch_nonce: str,
) -> None:
    errors: list[BaseException] = []
    if prompt_path is not None:
        try:
            remove_prompt_file(
                prompt_path,
                state_dir,
                role=role,
                launch_nonce=launch_nonce,
            )
        except (
            OSError,
            RuntimeError,
            RuntimeValidationError,
            TypeError,
            ValueError,
        ) as exc:
            errors.append(exc)
    for root in roots:
        if root is not None:
            try:
                remove_owned_tree(root)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                errors.append(exc)
    if errors:
        raise RuntimeFailure(
            ErrorCode.BACKEND_PROTOCOL_FAILURE,
            "local ACP cleanup is unconfirmed; ownership must be retained",
        ) from errors[0]


def _failure(exc: BaseException, message: str) -> RuntimeFailure:
    if isinstance(exc, RuntimeFailure):
        return exc
    if isinstance(exc, (RuntimeValidationError, ValueError, TypeError)):
        return RuntimeFailure(ErrorCode.INVALID_REQUEST, message)
    return RuntimeFailure(ErrorCode.BACKEND_PROTOCOL_FAILURE, message)


def _assignment_record(
    target: NodeRef,
    spec: Mapping[str, object],
    request: RolePrompt | TaskDispatch,
    record: Mapping[str, object] | None,
    *,
    task_id: str,
    dispatch_id: str,
    terminal_handle: str,
    launch_nonce: str,
    prompt_path: Path,
    agent_command: str,
    session_name: str,
    private_root: Path,
    snapshot_root: Path,
    adapter_snapshot: Mapping[str, object],
    provider_fields: Mapping[str, object],
) -> dict[str, object]:
    assignment: dict[str, object] = {
        "role": target.node_id,
        "role_kind": target.kind.value,
        "task_id": task_id,
        "dispatch_id": dispatch_id,
        "terminal_handle": terminal_handle,
        "completion_observed": False,
        "launcher_owned_terminal": True,
        "prompt_path": str(prompt_path),
        "launch_nonce": launch_nonce,
        "agent_command": agent_command,
        "session_name": session_name,
        "execution": "background",
        "adapter_id": spec.get("adapter_id"),
        "provider_private_root": str(private_root),
        "snapshot_root": str(snapshot_root),
        "adapter_snapshot": copy.deepcopy(dict(adapter_snapshot)),
        **copy.deepcopy(dict(provider_fields)),
    }
    if isinstance(request, TaskDispatch):
        if record is None:
            _fail(
                ErrorCode.IDENTITY_MISMATCH, "TaskSpec assignment has no saved record"
            )
        assignment.update(
            {
                "logical_task_id": request.task.task_id,
                "task_spec": request.task.as_dict(),
                "task_stage": record.get("stage"),
                "task_revision": record.get("revision"),
            }
        )
        if record.get("workspace_revision") is not None:
            assignment["task_workspace_revision"] = record["workspace_revision"]
    if _NATIVE_ASSIGNMENT_FIELDS.intersection(assignment):
        _fail(
            ErrorCode.BACKEND_PROTOCOL_FAILURE,
            "named Orca assignment contains native metadata",
        )
    if assignment.get("role_kind") not in {kind.value for kind in _ACP_KINDS}:
        _fail(ErrorCode.IDENTITY_MISMATCH, "named Orca assignment role kind is invalid")
    return assignment


def _receipt(
    target: NodeRef,
    run_id: str,
    task_id: str,
    dispatch_id: str,
    terminal_handle: str,
) -> Assignment:
    task_ref = TaskRef(task_id)
    dispatch_ref = DispatchRef(dispatch_id)
    terminal_ref = TerminalRef(terminal_handle)
    return Assignment(
        role=target,
        launch_mode=LaunchMode.BARE_BACKGROUND,
        task_id=task_ref,
        dispatch_id=dispatch_ref,
        terminal_id=terminal_ref,
        completion_identity=CompletionIdentity(
            run_id=RunRef(run_id),
            task_id=task_ref,
            dispatch_id=dispatch_ref,
            sender_terminal_id=terminal_ref,
        ),
    )


def start_assignment(
    path: Path,
    state: dict[str, object],
    request: RolePrompt | TaskDispatch,
    *,
    task_record: dict[str, object] | None,
    text: str,
) -> Assignment:
    """Start one selected named ACP role under Orca ownership.

    The caller supplies an admitted run and prepared TaskSpec clone.  Selected
    profile checks happen before the first Orca command.  Once an Orca mutation
    is attempted, failures retain an exact ``pending_role_start`` marker and
    never claim rollback success.
    """

    if not isinstance(path, Path) or not isinstance(state, dict):
        _fail(ErrorCode.INVALID_REQUEST, "named Orca assignment boundary is invalid")
    if not isinstance(request, (RolePrompt, TaskDispatch)):
        _fail(ErrorCode.INVALID_REQUEST, "named Orca assignment request is invalid")
    _request_text(request, text)
    target = _selected_role(request)
    frozen_run = _frozen_run(state)
    run_id = _required_string(state, "run_id")
    team_id = _required_string(state, "team_id")
    worktree_id = _required_string(state, "worktree_id")
    main_terminal = _required_string(state, "main_terminal")
    spec = _role_spec(state, target)
    prepared_state, prepared_record = _prepare_task(state, request, task_record)
    selected, adapter = _preflight_role(state, target, spec)
    provider = cast(str, spec["provider"])
    launch_nonce = secrets.token_hex(16)
    private_root: Path | None = None
    snapshot_root: Path | None = None
    prompt_path: Path | None = None
    marker_published = False
    live_marker: dict[str, object] | None = None
    remote_attempted = False
    final_saved = False
    task_id: str | None = None
    terminal_handle: str | None = None
    dispatch_id: str | None = None
    dispatch_id_hint: str | None = None
    phase = "local-preparation"

    try:
        private_root = Path(
            tempfile.mkdtemp(
                prefix="agent-team-provider-",
                dir="/tmp" if provider == "claude" else None,
            )
        ).resolve(strict=True)
        snapshot_root = Path(tempfile.mkdtemp(prefix="agent-team-snapshot-")).resolve(
            strict=True
        )
        prompt_path = create_prompt_file(path.parent, target, launch_nonce, text)

        provider_fields: dict[str, object] = {}
        if provider == "codex":
            from . import codex_acp

            codex_selected = cast(CodexAcpExecutables, selected)
            phase = "codex-inspection"
            inspection_marker = _marker(
                path,
                target,
                launch_nonce,
                prompt_path=prompt_path,
                private_root=private_root,
                snapshot_root=snapshot_root,
                phase=phase,
                cleanup_confirmed=False,
            )
            _recheck_run(path, state, frozen_run, expected_marker=None)
            _persist_marker(path, prepared_state, inspection_marker)
            marker_published = True
            live_marker = inspection_marker
            try:
                provider_fields = codex_acp.prepare_assignment(
                    private_root=private_root,
                    workspace=Path(_required_string(state, "workspace")),
                    state_path=path,
                    task=request.task if isinstance(request, TaskDispatch) else None,
                    permission=cast(str, spec["permission"]),
                    executables=codex_selected,
                    model=cast(str, spec["model"]),
                    effort=cast(str, spec["effort"]),
                    instructions=cast(str, spec["instructions"]),
                    provider_snapshot=cast(
                        Mapping[str, object], spec.get("provider_snapshot", {})
                    ),
                )
            except BaseException as exc:
                cleanup_confirmed = isinstance(exc, RuntimeValidationError) or bool(
                    getattr(exc, "cleanup_confirmed", False)
                )
                failed_marker = _marker(
                    path,
                    target,
                    launch_nonce,
                    prompt_path=prompt_path,
                    private_root=private_root,
                    snapshot_root=snapshot_root,
                    phase=phase,
                    cleanup_confirmed=cleanup_confirmed,
                )
                if cleanup_confirmed:
                    try:
                        _cleanup_local(
                            prompt_path,
                            (private_root, snapshot_root),
                            state_dir=path.parent,
                            role=target,
                            launch_nonce=launch_nonce,
                        )
                    except RuntimeFailure as cleanup_error:
                        failed_marker["cleanup_confirmed"] = False
                        _persist_marker(path, prepared_state, failed_marker)
                        raise RuntimeFailure(
                            ErrorCode.BACKEND_PROTOCOL_FAILURE,
                            "Codex inspection cleanup is unconfirmed; ownership retained",
                        ) from cleanup_error
                    _recheck_run(
                        path,
                        state,
                        frozen_run,
                        expected_marker=live_marker,
                    )
                    state_without_marker = read_state(path)
                    _clear_marker(state_without_marker)
                    _write(path, state_without_marker)
                    marker_published = False
                else:
                    _persist_marker(path, prepared_state, failed_marker)
                raise _failure(
                    exc,
                    "Codex scoped ACP preparation failed",
                ) from exc
            agent_command = codex_acp.agent_command(codex_selected)
            phase = "pre-task"
            live_marker = _marker(
                path,
                target,
                launch_nonce,
                prompt_path=prompt_path,
                private_root=private_root,
                snapshot_root=snapshot_root,
                phase=phase,
                cleanup_confirmed=True,
            )
            _persist_marker(path, prepared_state, live_marker)
            raw_policy_path = provider_fields.get("write_policy_path")
            if not isinstance(raw_policy_path, str):
                _fail(
                    ErrorCode.IDENTITY_MISMATCH,
                    "Codex scoped policy path is missing",
                )
            codex_policy_path = Path(raw_policy_path).resolve(strict=False)
            if codex_policy_path.parent != private_root:
                _fail(
                    ErrorCode.IDENTITY_MISMATCH,
                    "Codex scoped policy is outside its private root",
                )
            provider_fields["write_policy_path"] = str(codex_policy_path)
        else:
            claude_selected = cast(NativeAcpExecutables, selected)
            phase = "local-policy"
            policy_path, policy_digest = scoped_acp.create_write_policy(
                private_root,
                Path(_required_string(state, "workspace")),
                path,
                request.task if isinstance(request, TaskDispatch) else None,
                claude_selected.agent,
                permission=cast(str, spec["permission"]),
            )
            policy_path = policy_path.resolve(strict=False)
            if policy_path.parent != private_root:
                _fail(
                    ErrorCode.IDENTITY_MISMATCH,
                    "Claude scoped policy is outside its private root",
                )
            provider_fields = {
                "write_policy_path": str(policy_path),
                "write_policy_sha256": policy_digest,
                "question_socket": str(private_root / "q.sock"),
            }
            agent_command = cli.acp_agent_command(
                team_id,
                target,
                launch_nonce,
                executables=claude_selected,
                write_policy=policy_path,
                questions=True,
            )

        _recheck_run(path, state, frozen_run, expected_marker=live_marker)
        live_marker = _marker(
            path,
            target,
            launch_nonce,
            prompt_path=prompt_path,
            private_root=private_root,
            snapshot_root=snapshot_root,
            phase="pre-task",
            cleanup_confirmed=True,
        )
        _persist_marker(path, prepared_state, live_marker)
        marker_published = True

        phase = "task-create"
        remote_attempted = True
        _recheck_run(path, state, frozen_run, expected_marker=live_marker)
        task_payload = (
            task_json(request.task) if isinstance(request, TaskDispatch) else text
        )
        task_response = run_orca(
            prepared_state,
            [
                "orchestration",
                "task-create",
                "--spec",
                task_payload,
                "--task-title",
                f"{team_id}-{target.node_id}",
                "--display-name",
                f"team-{target.node_id}",
                "--run",
                run_id,
                "--from",
                main_terminal,
                "--json",
            ],
        )
        task_id = require_nested_string(task_response, ("task", "id"), "task-create")
        live_marker = _marker(
            path,
            target,
            launch_nonce,
            prompt_path=prompt_path,
            private_root=private_root,
            snapshot_root=snapshot_root,
            phase=phase,
            cleanup_confirmed=False,
            task_id=task_id,
        )
        _persist_marker(path, prepared_state, live_marker)
        _recheck_run(path, state, frozen_run, expected_marker=live_marker)

        phase = "terminal-create"
        _recheck_run(path, state, frozen_run, expected_marker=live_marker)
        terminal_response = run_orca(
            prepared_state,
            [
                "terminal",
                "create",
                "--worktree",
                f"id:{worktree_id}",
                "--title",
                f"{team_id}-{target.node_id}",
                "--json",
            ],
        )
        terminal_handle = require_nested_string(
            terminal_response, ("terminal", "handle"), "terminal create"
        )
        live_marker = _marker(
            path,
            target,
            launch_nonce,
            prompt_path=prompt_path,
            private_root=private_root,
            snapshot_root=snapshot_root,
            phase=phase,
            cleanup_confirmed=False,
            task_id=task_id,
            terminal_handle=terminal_handle,
        )
        _persist_marker(path, prepared_state, live_marker)
        _recheck_run(path, state, frozen_run, expected_marker=live_marker)

        phase = "dispatch"
        _recheck_run(path, state, frozen_run, expected_marker=live_marker)
        dispatch_response = run_orca(
            prepared_state,
            [
                "orchestration",
                "dispatch",
                "--task",
                task_id,
                "--to",
                terminal_handle,
                "--run",
                run_id,
                "--from",
                main_terminal,
                "--json",
            ],
        )
        try:
            dispatch_id = validate_acp_dispatch_response(
                dispatch_response,
                task_id=task_id,
                terminal_handle=terminal_handle,
                run_id=run_id,
                context="dispatch",
            )
        except BaseException:
            dispatch = dispatch_response.get("dispatch")
            hint = dispatch.get("id") if isinstance(dispatch, Mapping) else None
            if isinstance(hint, str):
                dispatch_id_hint = hint
            live_marker = _marker(
                path,
                target,
                launch_nonce,
                prompt_path=prompt_path,
                private_root=private_root,
                snapshot_root=snapshot_root,
                phase=phase,
                cleanup_confirmed=False,
                task_id=task_id,
                terminal_handle=terminal_handle,
                dispatch_id_hint=dispatch_id_hint,
            )
            _persist_marker(path, prepared_state, live_marker)
            raise

        live_marker = _marker(
            path,
            target,
            launch_nonce,
            prompt_path=prompt_path,
            private_root=private_root,
            snapshot_root=snapshot_root,
            phase=phase,
            cleanup_confirmed=False,
            task_id=task_id,
            terminal_handle=terminal_handle,
            dispatch_id=dispatch_id,
        )
        _persist_marker(path, prepared_state, live_marker)
        _recheck_run(path, state, frozen_run, expected_marker=live_marker)

        assignment = _assignment_record(
            target,
            spec,
            request,
            prepared_record,
            task_id=task_id,
            dispatch_id=dispatch_id,
            terminal_handle=terminal_handle,
            launch_nonce=launch_nonce,
            prompt_path=prompt_path,
            agent_command=agent_command,
            session_name=cli.acp_session_name(target, launch_nonce),
            private_root=private_root,
            snapshot_root=snapshot_root,
            adapter_snapshot=adapter,
            provider_fields=provider_fields,
        )
        roles = prepared_state.get("roles")
        tasks = prepared_state.get("tasks")
        if not isinstance(roles, dict) or not isinstance(tasks, dict):
            _fail(
                ErrorCode.IDENTITY_MISMATCH,
                "named Orca state has no mutable assignment containers",
            )
        roles[target.node_id] = assignment
        if isinstance(request, TaskDispatch):
            record = tasks.get(request.task.task_id)
            if not isinstance(record, dict):
                _fail(
                    ErrorCode.IDENTITY_MISMATCH,
                    "TaskSpec record disappeared before assignment save",
                )
            record["dispatch_id"] = dispatch_id
        _clear_marker(prepared_state)
        _recheck_run(path, state, frozen_run, expected_marker=live_marker)
        _write(path, prepared_state)
        final_saved = True
        state.clear()
        state.update(copy.deepcopy(prepared_state))

        phase = "terminal-send"
        remote_attempted = True
        run_orca(
            prepared_state,
            [
                "terminal",
                "send",
                "--terminal",
                terminal_handle,
                "--text",
                cli.acp_runner_command(
                    prepared_state,
                    target,
                    task_id=task_id,
                    dispatch_id=dispatch_id,
                    terminal_handle=terminal_handle,
                    prompt_path=prompt_path,
                    launch_nonce=launch_nonce,
                ),
                "--enter",
                "--json",
            ],
        )
        return _receipt(target, run_id, task_id, dispatch_id, terminal_handle)
    except BaseException as exc:
        if final_saved:
            # The assignment is durable.  A lost terminal-send response must
            # leave it available for the existing recovery path.
            raise _failure(exc, "Orca terminal runner delivery is unknown") from exc
        if marker_published and remote_attempted:
            retained = _marker(
                path,
                target,
                launch_nonce,
                prompt_path=cast(Path, prompt_path),
                private_root=cast(Path, private_root),
                snapshot_root=cast(Path, snapshot_root),
                phase=phase,
                cleanup_confirmed=False,
                task_id=task_id,
                terminal_handle=terminal_handle,
                dispatch_id=dispatch_id,
                dispatch_id_hint=dispatch_id_hint,
            )
            try:
                _persist_marker(path, prepared_state, retained)
            except BaseException as retention_error:
                raise RuntimeFailure(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "Orca assignment ownership is unknown and could not be retained",
                ) from retention_error
        elif marker_published:
            retained = _marker(
                path,
                target,
                launch_nonce,
                prompt_path=cast(Path, prompt_path),
                private_root=cast(Path, private_root),
                snapshot_root=cast(Path, snapshot_root),
                phase=phase,
                cleanup_confirmed=False,
                task_id=task_id,
                terminal_handle=terminal_handle,
                dispatch_id=dispatch_id,
                dispatch_id_hint=dispatch_id_hint,
            )
            _persist_marker(path, prepared_state, retained)
        else:
            try:
                _cleanup_local(
                    prompt_path,
                    (private_root, snapshot_root),
                    state_dir=path.parent,
                    role=target,
                    launch_nonce=launch_nonce,
                )
            except RuntimeFailure as cleanup_error:
                if (
                    prompt_path is not None
                    and private_root is not None
                    and snapshot_root is not None
                ):
                    retained = _marker(
                        path,
                        target,
                        launch_nonce,
                        prompt_path=prompt_path,
                        private_root=private_root,
                        snapshot_root=snapshot_root,
                        phase=phase,
                        cleanup_confirmed=False,
                        task_id=task_id,
                        terminal_handle=terminal_handle,
                        dispatch_id=dispatch_id,
                    )
                    _persist_marker(path, prepared_state, retained)
                raise RuntimeFailure(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "local ACP cleanup is unconfirmed; ownership retained",
                ) from cleanup_error
        if isinstance(exc, RuntimeFailure):
            raise
        raise _failure(exc, "named Orca assignment startup failed") from exc
