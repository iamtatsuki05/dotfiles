"""Orca implementation of the internal runtime backend.

The public command line surface deliberately remains in :mod:`agent_team.cli`.
This module owns identity checks and lifecycle effects for the four CLI
commands.  The fixed Orca grammar/decoder and per-team reservation live in the
private :mod:`agent_team.orca` module.  MCP and background runner paths keep
their existing implementation until their follow-up slices.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final, cast

from .adapters import remove_owned_tree
from .cleanup import (
    STARTUP_RECOVERY_VERSION,
    StartupCleanup,
    cleanup_assignment_phase,
    cleanup_journal_path,
    journal_assignment,
    load_cleanup_journal,
    load_startup_recovery,
    remove_startup_recovery,
    rollback_startup_recovery,
    startup_recovery_path,
    state_string,
    validated_assignments,
    write_cleanup_journal,
    write_startup_recovery,
)
from .contracts import (
    Attach,
    AttachReceipt,
    BackendPort,
    BackendRequest,
    BackendResult,
    ErrorCode,
    Role,
    RunRef,
    RuntimeFailure,
    StartResult,
    StartSpec,
    Status,
    StatusReceipt,
    StopResult,
    TerminalRef,
)
from .orca import (
    OrcaClient,
    OrcaCommandError,
    OrcaError,
    OrcaProtocolError,
    OrcaTransportError,
    TerminalCloseVerdict,
    TerminalSwitchVerdict,
    WorkerStopAlreadySettledVerdict,
    WorkerStopContextOnlyVerdict,
    WorkerStopOwnedVerdict,
    WorkerStopUnknownVerdict,
    WorkerStopVerdict,
    _LifecycleReservation,
    _required_string,
)
from .runtime import (
    STATE_VERSION,
    RuntimeValidationError,
    StatePublishError,
    read_state,
    remove_state_tree,
    validate_state_tree,
    write_state,
)

MAX_RUNTIME_FAILURE_CHARS: Final = 240
MAX_FOCUS_WARNING_CHARS: Final = 120


def _startup_recovery_payload(
    *,
    phase: str,
    spec: StartSpec,
    workspace: Path,
    worktree_id: str,
    main_terminal: str | None,
    terminal_closed: bool,
    local_tracked: tuple[tuple[str, bool, str], ...],
    state_published: bool = False,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "version": STARTUP_RECOVERY_VERSION,
        "phase": phase,
        "team_id": spec.team_id,
        "workspace": str(workspace),
        "worktree_id": worktree_id,
        "main_terminal": main_terminal,
        "terminal_closed": terminal_closed,
        "local_tracked": [
            {"path": path, "existed": existed, "kind": kind}
            for path, existed, kind in local_tracked
        ],
    }
    if state_published:
        payload["state_published"] = True
    return payload


__all__ = ("OrcaBackend", "OrcaClient")


class OrcaBackend(BackendPort):
    """Bind the typed runtime contract to the existing CLI Orca lifecycle."""

    def __init__(
        self,
        client: OrcaClient,
        *,
        launcher_path: Path | None = None,
        main_command_factory: Callable[[Path], str] | None = None,
        prepare_start: Callable[[], StartupCleanup | Callable[[], None] | None]
        | None = None,
        resume_existing: bool = False,
        user_data_path: Path | None = None,
    ) -> None:
        self._client = client
        self._launcher_path = launcher_path or Path(sys.argv[0]).expanduser().resolve()
        self._main_command_factory = main_command_factory
        self._prepare_start = prepare_start
        self._resume_existing = resume_existing
        self._user_data_path = user_data_path
        self._state: dict[str, object] | None = None
        self.last_start_response: dict[str, object] | None = None
        self.last_status_response: dict[str, object] | None = None
        self.last_attach_response: dict[str, object] | None = None
        self.last_stop_response: dict[str, object] | None = None

    def start(self, spec: StartSpec) -> StartResult:
        self._ensure_supported_platform()
        reservation = _LifecycleReservation(
            spec.state_path, create_parent=not self._resume_existing
        )
        reservation.acquire()
        try:
            if self._resume_existing:
                return self._resume(spec)
            return self._start_new(spec)
        finally:
            if not self._resume_existing and not spec.state_path.exists():
                recovery_path = startup_recovery_path(spec.state_path)
                if not recovery_path.exists() and not recovery_path.is_symlink():
                    try:
                        spec.state_path.parent.rmdir()
                    except OSError:
                        pass
            reservation.release()

    def _start_new(self, spec: StartSpec) -> StartResult:
        workspace = self._validated_workspace(spec)
        recovery_path = startup_recovery_path(spec.state_path)
        recovery = load_startup_recovery(recovery_path)
        if spec.state_path.exists():
            recovery_workspace = (
                recovery.get("workspace") if recovery is not None else None
            )
            if (
                recovery is not None
                and recovery.get("state_published") is True
                and recovery.get("team_id") == spec.team_id
                and isinstance(recovery_workspace, str)
                and self._canonical_path(Path(recovery_workspace)) == workspace
            ):
                try:
                    remove_startup_recovery(recovery_path)
                except (RuntimeFailure, OSError):
                    pass
            raise RuntimeFailure(
                ErrorCode.TEAM_ALREADY_RUNNING,
                "agent-team state already exists; use attach or stop",
            )
        if recovery is not None:
            self._recover_startup(spec, workspace, recovery_path, recovery)
        if spec.state_path.parent.exists() and any(spec.state_path.parent.iterdir()):
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "agent-team startup recovery is pending",
            )
        worktree_id, socket_path = self._ensure_orca_ready(workspace)
        if self._main_command_factory is None:
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST,
                "OrcaBackend start requires a Main command factory",
            )
        prepare_cleanup: Callable[[], None] | None = None
        local_tracked: tuple[tuple[str, bool, str], ...] = ()
        if self._prepare_start is not None:
            try:
                prepared = self._prepare_start()
                if isinstance(prepared, StartupCleanup):
                    prepare_cleanup = prepared
                    local_tracked = prepared.tracked
                elif prepared is not None:
                    prepare_cleanup = prepared
            except Exception as exc:
                raise self._runtime_failure(
                    exc, "team startup preparation failed"
                ) from exc

        main_terminal: str | None = None
        main_terminal_verified = False
        terminal_create_started = False
        state_published = False
        try:
            write_startup_recovery(
                recovery_path,
                _startup_recovery_payload(
                    phase="terminal_create_started",
                    spec=spec,
                    workspace=workspace,
                    worktree_id=worktree_id,
                    main_terminal=None,
                    terminal_closed=False,
                    local_tracked=local_tracked,
                ),
            )
            terminal_create_started = True
            created = self._client.terminal_create(
                worktree_id=worktree_id,
                title=f"{spec.team_id}-main",
                command=self._main_command_factory(socket_path),
                cwd=workspace,
            )
            main_terminal = _required_string(
                created, ("terminal", "handle"), "orca terminal create"
            )
            created_worktree = _required_string(
                created, ("terminal", "worktreeId"), "orca terminal create"
            )
            write_startup_recovery(
                recovery_path,
                _startup_recovery_payload(
                    phase="terminal_created",
                    spec=spec,
                    workspace=workspace,
                    worktree_id=worktree_id,
                    main_terminal=main_terminal,
                    terminal_closed=False,
                    local_tracked=local_tracked,
                ),
            )
            if created_worktree != worktree_id:
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "Orca terminal worktree does not match agent-team state",
                )
            self._verify_terminal(
                terminal_id=main_terminal,
                workspace=workspace,
                worktree_id=worktree_id,
                title=f"{spec.team_id}-main",
            )
            main_terminal_verified = True
            self._client.terminal_wait(terminal_id=main_terminal, cwd=workspace)
            objective = (
                f"{spec.team_id}: Planner / Worker / Reviewer coordination for "
                f"{workspace}"
            )
            run_id = self._client.run_create(
                objective=objective,
                terminal_id=main_terminal,
                cwd=workspace,
            )
            if not run_id:
                raise OrcaProtocolError("run-create returned an empty id")
            self._verify_run(
                run_id=run_id,
                team_id=spec.team_id,
                workspace=workspace,
                coordinator_handle=main_terminal,
                expected_objective=objective,
            )
            state = self._state_for_start(
                spec,
                worktree_id=worktree_id,
                socket_path=socket_path,
                run_id=run_id,
                main_terminal=main_terminal,
            )
            try:
                write_state(spec.state_path, state, reservation_held=True)
            except StatePublishError:
                state_published = True
                raise
            except RuntimeValidationError as exc:
                raise OrcaProtocolError(str(exc)) from exc
        except BaseException as start_error:
            if state_published:
                self._preserve_published_startup_marker(
                    recovery_path=recovery_path,
                    spec=spec,
                    workspace=workspace,
                    worktree_id=worktree_id,
                    main_terminal=main_terminal,
                    local_tracked=local_tracked,
                )
                raise RuntimeFailure(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "agent-team state publication durability is unknown",
                ) from start_error
            cleanup_errors: list[str] = []
            close_succeeded = not terminal_create_started
            local_cleanup_failed = False
            if main_terminal is not None and main_terminal_verified:
                try:
                    self._verify_terminal(
                        terminal_id=main_terminal,
                        workspace=workspace,
                        worktree_id=worktree_id,
                        title=f"{spec.team_id}-main",
                    )
                    close_verdict = self._client.terminal_close(
                        terminal_id=main_terminal,
                        cwd=workspace,
                    )
                    self._require_terminal_close(
                        close_verdict, terminal_id=main_terminal
                    )
                    close_succeeded = True
                except (
                    RuntimeFailure,
                    OrcaError,
                    OSError,
                    RuntimeError,
                    TypeError,
                    ValueError,
                ):
                    cleanup_errors.append("Main terminal cleanup failed")
            if close_succeeded and prepare_cleanup is not None:
                try:
                    prepare_cleanup()
                except (
                    RuntimeFailure,
                    OSError,
                    RuntimeError,
                    TypeError,
                    ValueError,
                ):
                    cleanup_errors.append("startup preparation cleanup failed")
                    local_cleanup_failed = True
            if not close_succeeded or local_cleanup_failed:
                try:
                    write_startup_recovery(
                        recovery_path,
                        _startup_recovery_payload(
                            phase=(
                                "terminal_create_unknown"
                                if main_terminal is None
                                else (
                                    "local_rollback_pending"
                                    if close_succeeded
                                    else "terminal_cleanup_pending"
                                )
                            ),
                            spec=spec,
                            workspace=workspace,
                            worktree_id=worktree_id,
                            main_terminal=main_terminal,
                            terminal_closed=close_succeeded,
                            local_tracked=local_tracked,
                        ),
                    )
                except RuntimeFailure:
                    cleanup_errors.append("startup recovery record failed")
            else:
                try:
                    remove_startup_recovery(recovery_path)
                except (RuntimeFailure, OSError):
                    cleanup_errors.append("startup recovery cleanup failed")
                    try:
                        write_startup_recovery(
                            recovery_path,
                            _startup_recovery_payload(
                                phase="local_rollback_pending",
                                spec=spec,
                                workspace=workspace,
                                worktree_id=worktree_id,
                                main_terminal=main_terminal,
                                terminal_closed=True,
                                local_tracked=local_tracked,
                            ),
                        )
                    except (RuntimeFailure, OSError):
                        cleanup_errors.append("startup recovery record failed")
            if cleanup_errors:
                original = self._runtime_failure(start_error, "team startup failed")
                raise RuntimeFailure(
                    original.code,
                    f"{original}: {'; '.join(cleanup_errors)}"[
                        :MAX_RUNTIME_FAILURE_CHARS
                    ],
                ) from start_error
            raise self._runtime_failure(
                start_error, "team startup failed"
            ) from start_error

        try:
            remove_startup_recovery(recovery_path)
        except (RuntimeFailure, OSError):
            self._preserve_published_startup_marker(
                recovery_path=recovery_path,
                spec=spec,
                workspace=workspace,
                worktree_id=worktree_id,
                main_terminal=main_terminal,
                local_tracked=local_tracked,
            )

        self._state = state
        result = StartResult(
            team_id=spec.team_id,
            run_id=RunRef(run_id),
            main_terminal_id=TerminalRef(main_terminal),
            state_path=spec.state_path,
        )
        focus_warning: str | None = None
        if spec.attach:
            try:
                switch_verdict = self._client.terminal_switch(
                    terminal_id=main_terminal, cwd=workspace
                )
                self._require_terminal_switch(switch_verdict, terminal_id=main_terminal)
            except OrcaCommandError:
                focus_warning = "Orca could not focus Main (command failure)"[
                    :MAX_FOCUS_WARNING_CHARS
                ]
            except (OrcaProtocolError, OrcaTransportError) as exc:
                raise RuntimeFailure(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "agent-team Main focus result is unknown",
                ) from exc
        self.last_start_response = {
            "status": "running",
            "team_id": spec.team_id,
            "workspace": str(workspace),
            "run_id": run_id,
            "main_terminal": main_terminal,
            "state_path": str(spec.state_path),
        }
        if focus_warning is not None:
            self.last_start_response["focus_warning"] = focus_warning
        return result

    def request(self, request: BackendRequest) -> BackendResult:
        if isinstance(request, Status):
            return self._status()
        if isinstance(request, Attach):
            return self._attach(request)
        raise RuntimeFailure(
            ErrorCode.INVALID_REQUEST,
            "Orca CLI backend only supports status and attach requests",
        )

    def _recover_startup(
        self,
        spec: StartSpec,
        workspace: Path,
        recovery_path: Path,
        recovery: Mapping[str, object],
    ) -> None:
        if recovery.get("team_id") != spec.team_id:
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "agent-team startup recovery identity does not match the request",
            )
        recovery_workspace = recovery.get("workspace")
        if (
            not isinstance(recovery_workspace, str)
            or self._canonical_path(Path(recovery_workspace)) != workspace
        ):
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "agent-team startup recovery identity does not match the request",
            )
        worktree_id = recovery.get("worktree_id")
        main_terminal = recovery.get("main_terminal")
        terminal_closed = recovery.get("terminal_closed")
        if not isinstance(worktree_id, str) or not isinstance(terminal_closed, bool):
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "agent-team startup recovery identity is invalid",
            )
        if terminal_closed:
            rollback_startup_recovery(spec.state_path.parent, recovery)
            remove_startup_recovery(recovery_path)
            return
        if main_terminal is None:
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "agent-team startup recovery terminal identity is unknown",
            )
        if not isinstance(main_terminal, str) or not main_terminal:
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "agent-team startup recovery identity is invalid",
            )
        self._verify_worktree(workspace=workspace, worktree_id=worktree_id)
        try:
            shown = self._client.terminal_show(terminal_id=main_terminal, cwd=workspace)
        except OrcaCommandError as exc:
            if not exc.not_found:
                raise RuntimeFailure(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "agent-team startup recovery is pending",
                ) from exc
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "agent-team startup recovery process stop is unconfirmed",
            ) from exc
        except (OrcaProtocolError, OrcaTransportError) as exc:
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "agent-team startup recovery result is unknown",
            ) from exc
        self._verify_terminal(
            terminal_id=main_terminal,
            workspace=workspace,
            worktree_id=worktree_id,
            title=f"{spec.team_id}-main",
            observed=shown,
        )
        raise RuntimeFailure(
            ErrorCode.BACKEND_PROTOCOL_FAILURE,
            "agent-team startup recovery is pending",
        )

    def _retry_published_startup_marker(self, state: Mapping[str, object]) -> None:
        state_path = self._state_path(state)
        recovery_path = startup_recovery_path(state_path)
        recovery = load_startup_recovery(recovery_path)
        if recovery is None or recovery.get("state_published") is not True:
            return
        if (
            recovery.get("team_id") != state.get("team_id")
            or recovery.get("worktree_id") != state.get("worktree_id")
            or recovery.get("main_terminal") != state.get("main_terminal")
        ):
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "agent-team startup recovery identity does not match state",
            )
        recovery_workspace = recovery.get("workspace")
        state_workspace = state.get("workspace")
        if (
            not isinstance(recovery_workspace, str)
            or not isinstance(state_workspace, str)
            or self._canonical_path(Path(recovery_workspace))
            != self._canonical_path(Path(state_workspace))
        ):
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "agent-team startup recovery identity does not match state",
            )
        try:
            remove_startup_recovery(recovery_path)
        except (RuntimeFailure, OSError):
            pass

    @staticmethod
    def _preserve_published_startup_marker(
        *,
        recovery_path: Path,
        spec: StartSpec,
        workspace: Path,
        worktree_id: str,
        main_terminal: str | None,
        local_tracked: tuple[tuple[str, bool, str], ...],
    ) -> None:
        try:
            write_startup_recovery(
                recovery_path,
                _startup_recovery_payload(
                    phase="state_published",
                    spec=spec,
                    workspace=workspace,
                    worktree_id=worktree_id,
                    main_terminal=main_terminal,
                    terminal_closed=False,
                    local_tracked=local_tracked,
                    state_published=True,
                ),
            )
        except (RuntimeFailure, OSError):
            pass

    def stop(self) -> StopResult:
        self._ensure_supported_platform()
        state = self._require_state()
        reservation = _LifecycleReservation(
            self._state_path(state), create_parent=False
        )
        reservation.acquire()
        try:
            return self._stop_locked()
        finally:
            reservation.release()

    def _stop_locked(self) -> StopResult:
        state = self._reload_state_locked()
        self._retry_published_startup_marker(state)
        state_path = self._state_path(state)
        state_file_identity = self._state_file_identity(state_path)
        workspace = self._workspace(state)
        run_id = state_string(state, "run_id")
        try:
            validate_state_tree(state_path, state)
        except RuntimeValidationError as exc:
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "agent-team state tree validation failed: unsupported special file",
            ) from exc

        try:
            journal = load_cleanup_journal(state, state_path)

            def save_journal() -> None:
                write_cleanup_journal(cleanup_journal_path(state_path), journal)

            validated = validated_assignments(state, state_path, journal)

            def mark_absent_remote_stages(*, include_main: bool) -> None:
                if include_main:
                    journal["main"] = "unknown"
                for role_name, _, _, _, _, _ in validated:
                    entry = journal_assignment(journal, role_name)
                    if entry.get("remote") in {"pending", "worker_done"}:
                        entry["remote"] = "unknown"
                save_journal()

            worktree_id = state_string(state, "worktree_id")
            team_id = state_string(state, "team_id")
            pending_remote = any(
                journal_assignment(journal, role).get("remote") != "done"
                for role, _, _, _, _, _ in validated
            )
            main_stage = journal.get("main")
            if pending_remote or main_stage == "pending":
                try:
                    self._verify_worktree(workspace=workspace, worktree_id=worktree_id)
                    self._verify_run(
                        run_id=run_id,
                        team_id=team_id,
                        workspace=workspace,
                        coordinator_handle=state_string(state, "main_terminal"),
                    )
                    self._verify_terminal(
                        terminal_id=state_string(state, "main_terminal"),
                        workspace=workspace,
                        worktree_id=worktree_id,
                        title=f"{team_id}-main",
                    )
                except OrcaCommandError as exc:
                    if not exc.not_found:
                        raise
                    mark_absent_remote_stages(include_main=main_stage == "pending")
                    raise RuntimeFailure(
                        ErrorCode.BACKEND_PROTOCOL_FAILURE,
                        "agent-team lifecycle identity absence is unconfirmed process stop",
                    ) from exc
            for role_name, assignment, _, _, _, _ in validated:
                entry = journal_assignment(journal, role_name)
                remote_stage = entry.get("remote")
                if remote_stage in {"worker_started", "terminal_started", "unknown"}:
                    raise RuntimeFailure(
                        ErrorCode.BACKEND_PROTOCOL_FAILURE,
                        "agent-team remote cleanup effect is unknown; inspect Orca before retry",
                    )
                if remote_stage == "pending" or remote_stage == "worker_done":
                    self._assert_state_current(state_path, state, state_file_identity)
                    try:
                        self._verify_assignment(
                            assignment=assignment,
                            role=role_name,
                            run_id=run_id,
                            worktree_id=worktree_id,
                            workspace=workspace,
                        )
                    except OrcaCommandError as exc:
                        if not exc.not_found:
                            raise
                        entry["remote"] = "unknown"
                        save_journal()
                        raise RuntimeFailure(
                            ErrorCode.BACKEND_PROTOCOL_FAILURE,
                            "agent-team role terminal absence is unconfirmed process stop",
                        ) from exc

            self._assert_state_current(state_path, state, state_file_identity)
            for (
                role_name,
                assignment,
                dispatch_id,
                terminal_id,
                execution,
                transport,
            ) in reversed(validated):
                entry = journal_assignment(journal, role_name)
                remote_stage = entry.get("remote")
                if remote_stage == "pending":
                    entry["remote"] = "worker_started"
                    save_journal()
                    try:
                        stop_verdict = self._client.worker_stop(
                            dispatch_id=dispatch_id, cwd=workspace
                        )
                        self._require_worker_stop(stop_verdict, dispatch_id=dispatch_id)
                        if stop_verdict.state == "stop_unknown":
                            entry["remote"] = "unknown"
                            save_journal()
                            raise RuntimeFailure(
                                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                                "agent-team worker stop effect is unknown",
                            )
                    except OrcaCommandError as exc:
                        if exc.not_found:
                            entry["remote"] = "unknown"
                            save_journal()
                            raise RuntimeFailure(
                                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                                "agent-team worker stop effect is unknown",
                            )
                        entry["remote"] = "pending"
                        save_journal()
                        raise RuntimeFailure(
                            ErrorCode.BACKEND_PROTOCOL_FAILURE,
                            "agent-team worker stop command failed",
                        )
                    except (OrcaProtocolError, OrcaTransportError):
                        entry["remote"] = "unknown"
                        save_journal()
                        raise RuntimeFailure(
                            ErrorCode.BACKEND_PROTOCOL_FAILURE,
                            "agent-team worker stop effect is unknown",
                        )
                    if stop_verdict.closed_agent_terminal:
                        entry["remote"] = "done"
                        save_journal()
                        remote_stage = "done"
                    else:
                        entry["remote"] = "worker_done"
                        save_journal()
                        remote_stage = "worker_done"
                if remote_stage == "worker_done":
                    self._assert_state_current(state_path, state, state_file_identity)
                    try:
                        self._verify_assignment(
                            assignment=assignment,
                            role=role_name,
                            run_id=run_id,
                            worktree_id=worktree_id,
                            workspace=workspace,
                        )
                    except OrcaCommandError as exc:
                        if not exc.not_found:
                            raise
                        entry["remote"] = "unknown"
                        save_journal()
                        raise RuntimeFailure(
                            ErrorCode.BACKEND_PROTOCOL_FAILURE,
                            "agent-team role terminal absence is unconfirmed process stop",
                        ) from exc
                    self._assert_state_current(state_path, state, state_file_identity)
                    entry["remote"] = "terminal_started"
                    save_journal()
                    try:
                        close_verdict = self._client.terminal_close(
                            terminal_id=terminal_id, cwd=workspace
                        )
                        self._require_terminal_close(
                            close_verdict, terminal_id=terminal_id
                        )
                    except OrcaCommandError as exc:
                        if exc.not_found:
                            entry["remote"] = "unknown"
                            save_journal()
                            raise RuntimeFailure(
                                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                                "agent-team terminal is gone; process stop is unconfirmed",
                            )
                        entry["remote"] = "worker_done"
                        save_journal()
                        raise RuntimeFailure(
                            ErrorCode.BACKEND_PROTOCOL_FAILURE,
                            "agent-team terminal close command failed",
                        )
                    except (OrcaProtocolError, OrcaTransportError):
                        entry["remote"] = "unknown"
                        save_journal()
                        raise RuntimeFailure(
                            ErrorCode.BACKEND_PROTOCOL_FAILURE,
                            "agent-team terminal close effect is unknown",
                        )
                    entry["remote"] = "done"
                    save_journal()

            main_stage = journal.get("main")
            if main_stage in {"started", "unknown"}:
                raise RuntimeFailure(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "agent-team Main terminal cleanup effect is unknown; inspect Orca before retry",
                )
            if main_stage == "pending":
                self._assert_state_current(state_path, state, state_file_identity)
                main_terminal = state_string(state, "main_terminal")
                try:
                    self._verify_worktree(workspace=workspace, worktree_id=worktree_id)
                    self._verify_run(
                        run_id=run_id,
                        team_id=team_id,
                        workspace=workspace,
                        coordinator_handle=main_terminal,
                    )
                    self._verify_terminal(
                        terminal_id=main_terminal,
                        workspace=workspace,
                        worktree_id=worktree_id,
                        title=f"{team_id}-main",
                    )
                except OrcaCommandError as exc:
                    if not exc.not_found:
                        raise
                    mark_absent_remote_stages(include_main=True)
                    raise RuntimeFailure(
                        ErrorCode.BACKEND_PROTOCOL_FAILURE,
                        "agent-team lifecycle identity absence is unconfirmed process stop",
                    ) from exc
                self._assert_state_current(state_path, state, state_file_identity)
                journal["main"] = "started"
                save_journal()
                try:
                    close_verdict = self._client.terminal_close(
                        terminal_id=main_terminal,
                        cwd=workspace,
                    )
                    self._require_terminal_close(
                        close_verdict,
                        terminal_id=main_terminal,
                    )
                except OrcaCommandError as exc:
                    if exc.not_found:
                        journal["main"] = "unknown"
                        save_journal()
                        raise RuntimeFailure(
                            ErrorCode.BACKEND_PROTOCOL_FAILURE,
                            "agent-team Main terminal is gone; process stop is unconfirmed",
                        )
                    journal["main"] = "pending"
                    save_journal()
                    raise RuntimeFailure(
                        ErrorCode.BACKEND_PROTOCOL_FAILURE,
                        "agent-team Main terminal close command failed",
                    )
                except (OrcaProtocolError, OrcaTransportError):
                    journal["main"] = "unknown"
                    save_journal()
                    raise RuntimeFailure(
                        ErrorCode.BACKEND_PROTOCOL_FAILURE,
                        "agent-team Main terminal close effect is unknown",
                    )
                journal["main"] = "done"
                save_journal()

            for (
                role_name,
                assignment,
                _,
                _,
                execution,
                transport,
            ) in reversed(validated):
                entry = journal_assignment(journal, role_name)
                local_stage = cast(str, entry["local"])
                if local_stage == "done":
                    continue
                needs_prompt = execution == "background" or transport == "acp"
                if local_stage == "pending" and execution == "background":
                    entry["local"] = cleanup_assignment_phase(
                        role_name,
                        assignment,
                        state_path=state_path,
                        execution=execution,
                        transport=transport,
                        local_stage=local_stage,
                        remove_tree=remove_owned_tree,
                    )
                    save_journal()
                    local_stage = cast(str, entry["local"])
                if local_stage in {"pending", "roots_done"} and needs_prompt:
                    entry["local"] = "prompt_started"
                    save_journal()
                    local_stage = "prompt_started"
                entry["local"] = cleanup_assignment_phase(
                    role_name,
                    assignment,
                    state_path=state_path,
                    execution=execution,
                    transport=transport,
                    local_stage=local_stage,
                    remove_tree=remove_owned_tree,
                )
                save_journal()
            remove_state_tree(state_path, state)
        except RuntimeFailure:
            raise
        except (
            RuntimeValidationError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "team stop cleanup failed",
            ) from exc

        result = StopResult(
            team_id=state_string(state, "team_id"), run_id=RunRef(run_id)
        )
        self.last_stop_response = {
            "status": "stopped",
            "team_id": result.team_id,
            "run_id": run_id,
            "note": "Orca Run is retained as an audit record.",
        }
        self._state = None
        return result

    def _resume(self, spec: StartSpec) -> StartResult:
        try:
            state = read_state(spec.state_path)
        except RuntimeValidationError as exc:
            code = (
                ErrorCode.TEAM_NOT_RUNNING
                if str(exc).startswith("agent-team is not running:")
                else ErrorCode.BACKEND_PROTOCOL_FAILURE
            )
            message = (
                "agent-team is not running"
                if code is ErrorCode.TEAM_NOT_RUNNING
                else "agent-team state is invalid"
            )
            raise RuntimeFailure(code, message) from exc
        self._assert_state_matches_spec(state, spec)
        self._retry_published_startup_marker(state)
        self._verify_worktree(
            workspace=self._validated_workspace(spec),
            worktree_id=state_string(state, "worktree_id"),
        )
        run_id = state_string(state, "run_id")
        main_terminal = state_string(state, "main_terminal")
        result = StartResult(
            team_id=spec.team_id,
            run_id=RunRef(run_id),
            main_terminal_id=TerminalRef(main_terminal),
            state_path=spec.state_path,
        )
        self._state = state
        return result

    def _status(self) -> StatusReceipt:
        self._ensure_supported_platform()
        state = self._require_state()
        reservation = _LifecycleReservation(
            self._state_path(state), create_parent=False
        )
        reservation.acquire()
        try:
            return self._status_locked()
        finally:
            reservation.release()

    def _status_locked(self) -> StatusReceipt:
        state = self._reload_state_locked()
        self._retry_published_startup_marker(state)
        workspace = self._workspace(state)
        run_id = state_string(state, "run_id")
        worktree_id = state_string(state, "worktree_id")
        main_terminal = state_string(state, "main_terminal")
        self._verify_worktree(
            workspace=workspace,
            worktree_id=worktree_id,
        )
        run = self._verify_run(
            run_id=run_id,
            team_id=state_string(state, "team_id"),
            workspace=workspace,
            coordinator_handle=main_terminal,
        )
        try:
            terminal = self._verify_terminal(
                terminal_id=main_terminal,
                workspace=workspace,
                worktree_id=worktree_id,
                title=f"{state_string(state, 'team_id')}-main",
            )
            workers = self._client.worker_list(run_id=run_id, cwd=workspace)
        except Exception as exc:
            raise self._runtime_failure(exc, "team status failed") from exc
        if not isinstance(workers.get("workers"), list):
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "worker-list response has invalid workers",
            )
        team_id = state_string(state, "team_id")
        self.last_status_response = {
            "status": "running",
            "team_id": team_id,
            "run": run,
            "main": terminal,
            "workers": workers,
        }
        return StatusReceipt("running", team_id, RunRef(run_id))

    def _attach(self, request: Attach) -> AttachReceipt:
        self._ensure_supported_platform()
        state = self._require_state()
        reservation = _LifecycleReservation(
            self._state_path(state), create_parent=False
        )
        reservation.acquire()
        try:
            return self._attach_locked(request)
        finally:
            reservation.release()

    def _attach_locked(self, request: Attach) -> AttachReceipt:
        state = self._reload_state_locked()
        self._retry_published_startup_marker(state)
        state_path = self._state_path(state)
        state_file_identity = self._state_file_identity(state_path)
        workspace = self._workspace(state)
        terminal_id = self._terminal_for_role(state, request.role)
        run_id = state_string(state, "run_id")
        worktree_id = state_string(state, "worktree_id")
        self._verify_worktree(
            workspace=workspace,
            worktree_id=worktree_id,
        )
        self._verify_run(
            run_id=run_id,
            team_id=state_string(state, "team_id"),
            workspace=workspace,
            coordinator_handle=state_string(state, "main_terminal"),
        )
        if request.role is Role.MAIN:
            terminal_title = f"{state_string(state, 'team_id')}-main"
        else:
            terminal_title = f"{state_string(state, 'team_id')}-{request.role.value}"
            assignment = self._assignment_for_role(state, request.role)
            self._verify_assignment(
                assignment=assignment,
                role=request.role.value,
                run_id=run_id,
                worktree_id=worktree_id,
                workspace=workspace,
            )
        try:
            self._verify_terminal(
                terminal_id=terminal_id,
                workspace=workspace,
                worktree_id=worktree_id,
                title=terminal_title,
            )
            self._assert_state_current(state_path, state, state_file_identity)
            switch_verdict = self._client.terminal_switch(
                terminal_id=terminal_id, cwd=workspace
            )
            self._require_terminal_switch(switch_verdict, terminal_id=terminal_id)
        except RuntimeFailure:
            raise
        except OrcaCommandError:
            raise
        except OrcaProtocolError as exc:
            raise RuntimeFailure(ErrorCode.BACKEND_PROTOCOL_FAILURE, str(exc)) from exc
        except Exception as exc:
            raise self._runtime_failure(exc, "team attach failed") from exc
        self.last_attach_response = {
            "status": "focused",
            "role": request.role.value,
            "terminal": terminal_id,
        }
        return AttachReceipt(request.role, TerminalRef(terminal_id), RunRef(run_id))

    def _ensure_orca_ready(self, workspace: Path) -> tuple[str, Path]:
        try:
            status = self._client.status(workspace)
            runtime_state = _required_string(
                status, ("runtime", "state"), "orca status"
            )
            graph_state = _required_string(status, ("graph", "state"), "orca status")
            if runtime_state != "ready" or graph_state != "ready":
                raise OrcaProtocolError(
                    f"Orca is not ready: runtime={runtime_state}, graph={graph_state}"
                )
            try:
                current = self._client.worktree_show(workspace)
            except OrcaCommandError as exc:
                raise RuntimeFailure(
                    ErrorCode.INVALID_REQUEST,
                    "workspace is not managed by Orca; register it explicitly with "
                    "`orca repo add --path <workspace>` and retry",
                ) from exc
            worktree_id = _required_string(
                current, ("worktree", "id"), "orca worktree show"
            )
            self._assert_worktree_path(current, workspace)
            return worktree_id, self._current_orca_socket()
        except Exception as exc:
            raise self._runtime_failure(exc, "Orca preflight failed") from exc

    def _current_orca_socket(self) -> Path:
        metadata_path = self._orca_user_data_path() / "orca-runtime.json"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OrcaProtocolError(
                f"Orca runtime metadata is unavailable: {metadata_path}"
            ) from exc
        if not isinstance(metadata, dict):
            raise OrcaProtocolError(
                f"Orca runtime metadata is invalid: {metadata_path}"
            )
        transports = metadata.get("transports")
        if not isinstance(transports, list):
            legacy = metadata.get("transport")
            transports = [legacy] if isinstance(legacy, dict) else []
        for transport in transports:
            if not isinstance(transport, dict) or transport.get("kind") != "unix":
                continue
            endpoint = transport.get("endpoint")
            if isinstance(endpoint, str) and Path(endpoint).is_absolute():
                return Path(endpoint)
        raise OrcaProtocolError(
            "Orca does not expose a Unix runtime socket; Codex role lifecycle "
            "reporting cannot be isolated on this platform"
        )

    def _orca_user_data_path(self) -> Path:
        if self._user_data_path is not None:
            return self._user_data_path.expanduser()
        override = os.environ.get("ORCA_USER_DATA_PATH")
        if override:
            return Path(override).expanduser()
        if sys.platform == "darwin":
            return Path.home() / "Library" / "Application Support" / "orca"
        if sys.platform == "win32":
            raise OrcaProtocolError(
                "agent-team Orca lifecycle requires a POSIX runtime"
            )
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        return config_home / "orca"

    @classmethod
    def _assert_nested_string(
        cls,
        payload: Mapping[str, object],
        keys: tuple[str, ...],
        expected: str,
        context: str,
    ) -> None:
        observed = _required_string(payload, keys, context)
        if observed != expected:
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                f"{context} identity does not match agent-team state",
            )

    @staticmethod
    def _require_worker_stop(verdict: object, *, dispatch_id: str) -> WorkerStopVerdict:
        if not isinstance(
            verdict,
            (
                WorkerStopOwnedVerdict,
                WorkerStopContextOnlyVerdict,
                WorkerStopAlreadySettledVerdict,
                WorkerStopUnknownVerdict,
            ),
        ):
            raise OrcaProtocolError(
                "Orca orchestration worker-stop response was invalid"
            )
        if verdict.dispatch_id != dispatch_id:
            raise OrcaProtocolError(
                "Orca orchestration worker-stop response was invalid"
            )
        if isinstance(verdict, WorkerStopOwnedVerdict):
            valid = (
                verdict.state == "stopped"
                and verdict.process_action == "closed_agent_terminal"
                and verdict.pty_killed
                and isinstance(verdict.already_settled, bool)
            )
        elif isinstance(verdict, WorkerStopContextOnlyVerdict):
            valid = (
                verdict.state == "stopped"
                and verdict.process_action == "none"
                and isinstance(verdict.already_settled, bool)
                and not verdict.pty_killed
            )
        elif isinstance(verdict, WorkerStopAlreadySettledVerdict):
            valid = (
                verdict.state
                in {
                    "succeeded",
                    "failed",
                    "stopped",
                    "abandoned",
                    "completed",
                    "circuit_broken",
                }
                and verdict.process_action == "none"
                and verdict.already_settled is True
                and not verdict.pty_killed
            )
        else:
            valid = (
                verdict.state == "stop_unknown"
                and verdict.process_action
                in {"none", "unknown", "closed_agent_terminal"}
                and verdict.already_settled is False
                and not verdict.pty_killed
            )
        if not valid:
            raise OrcaProtocolError(
                "Orca orchestration worker-stop response was invalid"
            )
        return verdict

    @staticmethod
    def _require_terminal_close(
        verdict: object, *, terminal_id: str
    ) -> TerminalCloseVerdict:
        if not isinstance(verdict, TerminalCloseVerdict):
            raise OrcaProtocolError("Orca terminal close response was invalid")
        if verdict.handle != terminal_id or not verdict.pty_killed:
            raise OrcaProtocolError("Orca terminal close response was invalid")
        if verdict.pty_stop_verdict in {"live", "unverifiable"}:
            raise OrcaProtocolError("Orca terminal close response was invalid")
        return verdict

    @staticmethod
    def _require_terminal_switch(
        verdict: object, *, terminal_id: str
    ) -> TerminalSwitchVerdict:
        if not isinstance(verdict, TerminalSwitchVerdict):
            raise OrcaProtocolError("Orca terminal switch response was invalid")
        if verdict.handle != terminal_id or not verdict.navigated:
            raise OrcaProtocolError("Orca terminal switch response was invalid")
        return verdict

    def _verify_worktree(
        self, *, workspace: Path, worktree_id: str
    ) -> dict[str, object]:
        try:
            current = self._client.worktree_show(workspace)
            self._assert_nested_string(
                current, ("worktree", "id"), worktree_id, "worktree show"
            )
            self._assert_worktree_path(current, workspace)
            return current
        except RuntimeFailure:
            raise
        except OrcaCommandError:
            raise
        except OrcaProtocolError as exc:
            raise RuntimeFailure(ErrorCode.BACKEND_PROTOCOL_FAILURE, str(exc)) from exc
        except Exception as exc:
            raise self._runtime_failure(exc, "worktree verification failed") from exc

    @staticmethod
    def _assert_worktree_path(payload: Mapping[str, object], workspace: Path) -> None:
        observed = _required_string(payload, ("worktree", "path"), "worktree show")
        if Path(observed).expanduser().resolve(strict=False) != workspace.resolve():
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "Orca worktree path does not match agent-team state",
            )

    def _verify_run(
        self,
        *,
        run_id: str,
        team_id: str,
        workspace: Path,
        coordinator_handle: str,
        expected_objective: str | None = None,
    ) -> dict[str, object]:
        try:
            run = self._client.run_show(run_id=run_id, cwd=workspace)
            self._assert_nested_string(run, ("run", "id"), run_id, "run-show")
            objective = _required_string(run, ("run", "objective"), "run-show")
            coordinator = _required_string(
                run, ("run", "coordinator_handle"), "run-show"
            )
        except RuntimeFailure:
            raise
        except OrcaCommandError:
            raise
        except OrcaProtocolError as exc:
            raise RuntimeFailure(ErrorCode.BACKEND_PROTOCOL_FAILURE, str(exc)) from exc
        except Exception as exc:
            raise self._runtime_failure(exc, "Run verification failed") from exc
        expected = expected_objective or (
            f"{team_id}: Planner / Worker / Reviewer coordination for {workspace}"
        )
        if objective != expected or coordinator != coordinator_handle:
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "Orca Run objective does not match agent-team state",
            )
        return run

    def _verify_terminal(
        self,
        *,
        terminal_id: str,
        workspace: Path,
        worktree_id: str,
        title: str,
        observed: dict[str, object] | None = None,
    ) -> dict[str, object]:
        del title
        try:
            terminal = (
                observed
                if observed is not None
                else self._client.terminal_show(terminal_id=terminal_id, cwd=workspace)
            )
            self._assert_nested_string(
                terminal, ("terminal", "handle"), terminal_id, "terminal-show"
            )
            self._assert_nested_string(
                terminal, ("terminal", "worktreeId"), worktree_id, "terminal-show"
            )
            terminal_payload = terminal.get("terminal")
            if not isinstance(terminal_payload, dict):
                raise OrcaProtocolError("terminal-show response is invalid")
            terminal_title = terminal_payload.get("title")
            if not isinstance(terminal_title, str):
                raise OrcaProtocolError("terminal-show response is invalid")
            terminal_path = terminal_payload.get("worktreePath")
            if not isinstance(terminal_path, str):
                raise OrcaProtocolError("terminal-show response is invalid")
            if terminal_path and (
                Path(terminal_path).expanduser().resolve(strict=False)
                != workspace.resolve()
            ):
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "Orca terminal worktree does not match agent-team state",
                )
            return terminal
        except RuntimeFailure:
            raise
        except OrcaCommandError:
            raise
        except OrcaProtocolError as exc:
            raise RuntimeFailure(ErrorCode.BACKEND_PROTOCOL_FAILURE, str(exc)) from exc
        except Exception as exc:
            raise self._runtime_failure(exc, "terminal verification failed") from exc

    def _verify_assignment(
        self,
        *,
        assignment: Mapping[str, object],
        role: str,
        run_id: str,
        worktree_id: str,
        workspace: Path,
    ) -> None:
        dispatch_id = assignment.get("dispatch_id")
        task_id = assignment.get("task_id")
        terminal_id = assignment.get("terminal_handle")
        if not all(
            isinstance(value, str) and value
            for value in (dispatch_id, task_id, terminal_id)
        ):
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "role assignment identity is incomplete",
            )
        dispatch_token = cast(str, dispatch_id)
        task_token = cast(str, task_id)
        terminal_token = cast(str, terminal_id)
        try:
            worker = self._client.worker_show(dispatch_id=dispatch_token, cwd=workspace)
            self._assert_nested_string(
                worker, ("dispatch", "id"), dispatch_token, "worker-show"
            )
            self._assert_nested_string(
                worker, ("dispatch", "task_id"), task_token, "worker-show"
            )
            self._assert_nested_string(
                worker, ("dispatch", "run_id"), run_id, "worker-show"
            )
            self._assert_nested_string(
                worker,
                ("dispatch", "assignee_handle"),
                terminal_token,
                "worker-show",
            )
            self._assert_nested_string(
                worker, ("worker", "dispatch_id"), dispatch_token, "worker-show"
            )
            worker_payload = worker.get("worker")
            if (
                not isinstance(worker_payload, dict)
                or "worktree_id" not in worker_payload
            ):
                raise OrcaProtocolError("worker-show response is invalid")
            observed_worktree = worker_payload["worktree_id"]
            if observed_worktree is not None:
                if not isinstance(observed_worktree, str) or not observed_worktree:
                    raise OrcaProtocolError("worker-show response is invalid")
                if observed_worktree != worktree_id:
                    raise RuntimeFailure(
                        ErrorCode.IDENTITY_MISMATCH,
                        "worker-show identity does not match agent-team state",
                    )
            self._assert_nested_string(
                worker,
                ("worker", "agent_terminal_handle"),
                terminal_token,
                "worker-show",
            )
            self._verify_terminal(
                terminal_id=terminal_token,
                workspace=workspace,
                worktree_id=worktree_id,
                title=f"{state_string(self._require_state(), 'team_id')}-{role}",
            )
        except RuntimeFailure:
            raise
        except OrcaCommandError:
            raise
        except OrcaProtocolError as exc:
            raise RuntimeFailure(ErrorCode.BACKEND_PROTOCOL_FAILURE, str(exc)) from exc
        except Exception as exc:
            raise self._runtime_failure(exc, "Dispatch verification failed") from exc

    def _state_for_start(
        self,
        spec: StartSpec,
        *,
        worktree_id: str,
        socket_path: Path,
        run_id: str,
        main_terminal: str,
    ) -> dict[str, object]:
        role_specs: dict[str, dict[str, object]] = {}
        for role, role_spec in spec.role_specs.items():
            role_specs[role.value] = {
                "provider": role_spec.provider,
                "transport": role_spec.transport,
                "model": role_spec.model,
                "effort": role_spec.effort,
                "permission": role_spec.permission,
                "execution": role_spec.execution,
                "adapter_id": role_spec.adapter_id,
                "instructions": role_spec.instructions,
            }
        return {
            "version": STATE_VERSION,
            "runtime": "orca",
            "team_id": spec.team_id,
            "workspace": str(spec.workspace.resolve()),
            "config_path": str(spec.config_path.resolve()),
            "state_path": str(spec.state_path),
            "launcher_path": str(self._launcher_path),
            "worktree_id": worktree_id,
            "orca_socket": str(socket_path),
            "run_id": run_id,
            "main_terminal": main_terminal,
            "role_specs": role_specs,
            "roles": {},
        }

    @staticmethod
    def _validated_workspace(spec: StartSpec) -> Path:
        workspace = spec.workspace.expanduser().resolve()
        if not workspace.is_dir():
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST,
                "workspace is not a directory",
            )
        return workspace

    def _require_state(self) -> dict[str, object]:
        state = self._state
        if state is None:
            raise RuntimeFailure(
                ErrorCode.TEAM_NOT_RUNNING,
                "agent-team runtime has not been started",
            )
        return state

    def _reload_state_locked(self) -> dict[str, object]:
        previous = self._require_state()
        path = self._state_path(previous)
        try:
            current = read_state(path)
        except RuntimeValidationError as exc:
            code = (
                ErrorCode.TEAM_NOT_RUNNING
                if str(exc).startswith("agent-team is not running:")
                else ErrorCode.BACKEND_PROTOCOL_FAILURE
            )
            message = (
                "agent-team is not running"
                if code is ErrorCode.TEAM_NOT_RUNNING
                else "agent-team state is invalid"
            )
            raise RuntimeFailure(code, message) from exc
        for key in (
            "team_id",
            "workspace",
            "config_path",
            "state_path",
            "run_id",
            "worktree_id",
            "main_terminal",
        ):
            previous_value = previous.get(key)
            current_value = current.get(key)
            if key in {"workspace", "config_path", "state_path"}:
                if not isinstance(previous_value, str) or not isinstance(
                    current_value, str
                ):
                    raise RuntimeFailure(
                        ErrorCode.BACKEND_PROTOCOL_FAILURE,
                        "agent-team state identity is incomplete",
                    )
                if self._canonical_path(Path(previous_value)) != self._canonical_path(
                    Path(current_value)
                ):
                    raise RuntimeFailure(
                        ErrorCode.IDENTITY_MISMATCH,
                        "agent-team state identity changed during operation",
                    )
            elif previous_value != current_value:
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "agent-team state identity changed during operation",
                )
        self._state = current
        return current

    @staticmethod
    def _state_file_identity(path: Path) -> tuple[int, int]:
        try:
            state_stat = path.lstat()
        except OSError as exc:
            raise RuntimeFailure(
                ErrorCode.TEAM_NOT_RUNNING,
                "agent-team is not running",
            ) from exc
        return state_stat.st_dev, state_stat.st_ino

    def _assert_state_current(
        self,
        path: Path,
        expected: Mapping[str, object],
        expected_identity: tuple[int, int],
    ) -> None:
        try:
            before_identity = self._state_file_identity(path)
            current = read_state(path)
            after_identity = self._state_file_identity(path)
        except RuntimeFailure:
            raise
        except RuntimeValidationError as exc:
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "agent-team state is invalid",
            ) from exc
        if (
            before_identity != expected_identity
            or after_identity != expected_identity
            or current != expected
        ):
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "agent-team state changed during operation",
            )

    @classmethod
    def _workspace(cls, state: Mapping[str, object]) -> Path:
        return Path(state_string(state, "workspace"))

    @classmethod
    def _state_path(cls, state: Mapping[str, object]) -> Path:
        return Path(state_string(state, "state_path"))

    @classmethod
    def _terminal_for_role(cls, state: Mapping[str, object], role: Role) -> str:
        if role is Role.MAIN:
            return state_string(state, "main_terminal")
        assignment = cls._assignment_for_role(state, role)
        terminal = assignment.get("terminal_handle")
        if not isinstance(terminal, str) or not terminal:
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "role assignment is missing terminal identity",
            )
        return terminal

    @staticmethod
    def _assignment_for_role(
        state: Mapping[str, object], role: Role
    ) -> dict[str, object]:
        roles = state.get("roles")
        assignment = roles.get(role.value) if isinstance(roles, dict) else None
        if not isinstance(assignment, dict):
            raise RuntimeFailure(
                ErrorCode.TEAM_NOT_RUNNING,
                "role has no active Orca Dispatch",
            )
        return assignment

    @staticmethod
    def _canonical_path(path: Path) -> Path:
        return path.expanduser().resolve(strict=False)

    def _assert_state_matches_spec(
        self, state: Mapping[str, object], spec: StartSpec
    ) -> None:
        expected = {
            "team_id": spec.team_id,
            "workspace": self._canonical_path(spec.workspace),
            "config_path": self._canonical_path(spec.config_path),
            "state_path": self._canonical_path(spec.state_path),
        }
        actual_workspace = self._canonical_path(Path(state_string(state, "workspace")))
        actual_config = self._canonical_path(Path(state_string(state, "config_path")))
        actual_state = self._canonical_path(Path(state_string(state, "state_path")))
        if (
            state.get("team_id") != expected["team_id"]
            or actual_workspace != expected["workspace"]
            or actual_config != expected["config_path"]
            or actual_state != expected["state_path"]
        ):
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "agent-team state does not match the requested team identity",
            )

    @staticmethod
    def _runtime_failure(error: BaseException, context: str) -> RuntimeFailure:
        if isinstance(error, RuntimeFailure):
            return error
        if isinstance(error, OrcaCommandError):
            reason = "Orca command failed"
        elif isinstance(error, OrcaTransportError):
            reason = "Orca transport failed; effect is unknown"
        elif isinstance(error, OrcaProtocolError):
            reason = "Orca response was invalid"
        elif isinstance(error, RuntimeValidationError):
            reason = "runtime artifact validation failed"
        else:
            reason = "runtime operation failed"
        message = f"{context}: {reason}"[:MAX_RUNTIME_FAILURE_CHARS]
        return RuntimeFailure(ErrorCode.BACKEND_PROTOCOL_FAILURE, message)

    @staticmethod
    def _ensure_supported_platform() -> None:
        if sys.platform == "win32":
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST,
                "agent-team Orca lifecycle requires a POSIX runtime",
            )
