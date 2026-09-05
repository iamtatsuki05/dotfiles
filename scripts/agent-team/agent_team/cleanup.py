"""Private cleanup journal and local-resource checks for the Orca CLI backend.

The journal is an internal recovery record.  It is kept next to state v3 and
is removed with that state tree; it is not part of the public state schema.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from .contracts import ErrorCode, RuntimeFailure
from .runtime import (
    RuntimeValidationError,
    remove_prompt_file,
    validate_prompt_file,
)

CLEANUP_JOURNAL_VERSION: Final = 1
MAX_CLEANUP_JOURNAL_BYTES: Final = 256_000
STARTUP_RECOVERY_VERSION: Final = 1
MAX_STARTUP_RECOVERY_BYTES: Final = 64_000


@dataclass(frozen=True)
class StartupCleanup:
    tracked: tuple[tuple[str, bool, str], ...]
    callback: Callable[[], None]

    def __call__(self) -> None:
        self.callback()


def startup_recovery_path(state_path: Path) -> Path:
    return state_path.parent / ".startup-recovery.json"


def write_startup_recovery(path: Path, payload: Mapping[str, object]) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(encoded) > MAX_STARTUP_RECOVERY_BYTES:
        raise RuntimeFailure(
            ErrorCode.BACKEND_PROTOCOL_FAILURE,
            "agent-team startup recovery record exceeds its size limit",
        )
    temporary = path.with_name(f".{path.name}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd: int | None = None
    try:
        fd = os.open(temporary, flags, 0o600)
        with os.fdopen(fd, "wb") as recovery_file:
            fd = None
            recovery_file.write(encoded)
            recovery_file.flush()
            os.fsync(recovery_file.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        if fd is not None:
            os.close(fd)
        try:
            temporary.unlink()
        except OSError:
            pass
        raise RuntimeFailure(
            ErrorCode.BACKEND_PROTOCOL_FAILURE,
            "agent-team startup recovery record could not be saved",
        ) from exc


def load_startup_recovery(path: Path) -> dict[str, object] | None:
    if not path.exists() and not path.is_symlink():
        return None
    try:
        file_stat = path.lstat()
        if (
            stat.S_ISLNK(file_stat.st_mode)
            or not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_uid != os.getuid()
            or stat.S_IMODE(file_stat.st_mode) != 0o600
        ):
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "agent-team startup recovery record is not private",
            )
        raw = path.read_bytes()
        if len(raw) > MAX_STARTUP_RECOVERY_BYTES:
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "agent-team startup recovery record exceeds its size limit",
            )
        payload = json.loads(raw.decode("utf-8"))
    except RuntimeFailure:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeFailure(
            ErrorCode.BACKEND_PROTOCOL_FAILURE,
            "agent-team startup recovery record is invalid",
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("version") != STARTUP_RECOVERY_VERSION
    ):
        raise RuntimeFailure(
            ErrorCode.BACKEND_PROTOCOL_FAILURE,
            "agent-team startup recovery record is invalid",
        )
    return payload


def remove_startup_recovery(path: Path) -> None:
    try:
        path.unlink()
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeFailure(
            ErrorCode.BACKEND_PROTOCOL_FAILURE,
            "agent-team startup recovery record could not be removed",
        ) from exc


def rollback_startup_recovery(state_root: Path, payload: Mapping[str, object]) -> None:
    raw_tracked = payload.get("local_tracked")
    if not isinstance(raw_tracked, list):
        raise RuntimeFailure(
            ErrorCode.BACKEND_PROTOCOL_FAILURE,
            "agent-team startup recovery local identity is invalid",
        )
    tracked: list[tuple[Path, bool, str]] = []
    canonical_root = state_root.resolve(strict=False)
    for raw in raw_tracked:
        if not isinstance(raw, dict):
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "agent-team startup recovery local identity is invalid",
            )
        raw_path = raw.get("path")
        existed = raw.get("existed")
        kind = raw.get("kind")
        if (
            not isinstance(raw_path, str)
            or not isinstance(existed, bool)
            or kind not in {"dir", "link"}
        ):
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "agent-team startup recovery local identity is invalid",
            )
        candidate = Path(raw_path).expanduser().absolute()
        try:
            candidate.parent.resolve(strict=False).relative_to(canonical_root)
        except ValueError as exc:
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "agent-team startup recovery path is outside state",
            ) from exc
        tracked.append((candidate, existed, cast(str, kind)))
    for path, existed, kind in sorted(
        tracked, key=lambda item: len(item[0].parts), reverse=True
    ):
        if existed or not (path.exists() or path.is_symlink()):
            continue
        if kind == "link":
            if not path.is_symlink():
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "agent-team startup recovery link identity is unproven",
                )
            path.unlink()
        else:
            if not path.is_dir() or path.is_symlink():
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "agent-team startup recovery directory identity is unproven",
                )
            path.rmdir()


def state_string(state: Mapping[str, object], key: str) -> str:
    value = state.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeFailure(
            ErrorCode.BACKEND_PROTOCOL_FAILURE,
            f"agent-team state is missing {key}",
        )
    return value


def cleanup_journal_path(state_path: Path) -> Path:
    return state_path.parent / ".cleanup.json"


def cleanup_records(state: Mapping[str, object]) -> list[dict[str, object]]:
    roles = state.get("roles")
    role_specs = state.get("role_specs")
    if not isinstance(roles, dict) or not isinstance(role_specs, dict):
        raise RuntimeFailure(
            ErrorCode.BACKEND_PROTOCOL_FAILURE,
            "agent-team state has invalid cleanup metadata",
        )
    records: list[dict[str, object]] = []
    for role, raw_assignment in roles.items():
        spec = role_specs.get(role)
        if not isinstance(role, str) or not isinstance(raw_assignment, dict):
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "agent-team cleanup assignment is invalid",
            )
        if not isinstance(spec, dict):
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "agent-team cleanup role spec is missing",
            )
        values = {
            key: raw_assignment.get(key)
            for key in ("task_id", "dispatch_id", "terminal_handle")
        }
        transport = spec.get("transport")
        execution = spec.get("execution")
        if not all(
            isinstance(value, str) and value
            for value in (*values.values(), transport, execution)
        ):
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "agent-team cleanup assignment identity is incomplete",
            )
        record: dict[str, object] = {
            "role": role,
            **values,
            "transport": transport,
            "execution": execution,
        }
        for key in (
            "prompt_path",
            "launch_nonce",
            "provider_private_root",
            "snapshot_root",
        ):
            value = raw_assignment.get(key)
            if value is not None:
                if not isinstance(value, str) or not value:
                    raise RuntimeFailure(
                        ErrorCode.BACKEND_PROTOCOL_FAILURE,
                        "agent-team cleanup resource identity is invalid",
                    )
                record[key] = value
        records.append(record)
    return records


def load_cleanup_journal(
    state: Mapping[str, object], state_path: Path
) -> dict[str, object]:
    expected_records = cleanup_records(state)
    path = cleanup_journal_path(state_path)
    if path.exists() or path.is_symlink():
        try:
            file_stat = path.lstat()
            if (
                stat.S_ISLNK(file_stat.st_mode)
                or not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_uid != os.getuid()
                or stat.S_IMODE(file_stat.st_mode) != 0o600
            ):
                raise RuntimeFailure(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "agent-team cleanup journal is not private",
                )
            raw = path.read_bytes()
            if len(raw) > MAX_CLEANUP_JOURNAL_BYTES:
                raise RuntimeFailure(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "agent-team cleanup journal exceeds its size limit",
                )
            payload = json.loads(raw.decode("utf-8"))
        except RuntimeFailure:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "agent-team cleanup journal is invalid",
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "agent-team cleanup journal is invalid",
            )
        validate_cleanup_journal(payload, state, expected_records)
        return payload

    journal: dict[str, object] = {
        "version": CLEANUP_JOURNAL_VERSION,
        "team_id": state_string(state, "team_id"),
        "run_id": state_string(state, "run_id"),
        "worktree_id": state_string(state, "worktree_id"),
        "main_terminal": state_string(state, "main_terminal"),
        "main": "pending",
        "assignments": [
            {**record, "remote": "pending", "local": "pending"}
            for record in expected_records
        ],
    }
    write_cleanup_journal(path, journal)
    return journal


def validate_cleanup_journal(
    journal: Mapping[str, object],
    state: Mapping[str, object],
    expected_records: list[dict[str, object]],
) -> None:
    if journal.get("version") != CLEANUP_JOURNAL_VERSION:
        raise RuntimeFailure(
            ErrorCode.BACKEND_PROTOCOL_FAILURE,
            "agent-team cleanup journal version is unsupported",
        )
    for key in ("team_id", "run_id", "worktree_id", "main_terminal"):
        if journal.get(key) != state_string(state, key):
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "agent-team cleanup journal identity does not match state",
            )
    if journal.get("main") not in {"pending", "started", "done", "unknown"}:
        raise RuntimeFailure(
            ErrorCode.BACKEND_PROTOCOL_FAILURE,
            "agent-team cleanup journal has an invalid Main stage",
        )
    raw_assignments = journal.get("assignments")
    if not isinstance(raw_assignments, list) or len(raw_assignments) != len(
        expected_records
    ):
        raise RuntimeFailure(
            ErrorCode.BACKEND_PROTOCOL_FAILURE,
            "agent-team cleanup journal assignments are invalid",
        )
    for expected, raw in zip(expected_records, raw_assignments, strict=True):
        if not isinstance(raw, dict):
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "agent-team cleanup journal assignment is invalid",
            )
        for key, value in expected.items():
            if raw.get(key) != value:
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "agent-team cleanup journal assignment identity changed",
                )
        if raw.get("remote") not in {
            "pending",
            "worker_started",
            "worker_done",
            "terminal_started",
            "done",
            "unknown",
        } or raw.get("local") not in {
            "pending",
            "roots_done",
            "prompt_started",
            "done",
        }:
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "agent-team cleanup journal stage is invalid",
            )


def write_cleanup_journal(path: Path, journal: Mapping[str, object]) -> None:
    payload = (json.dumps(journal, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(payload) > MAX_CLEANUP_JOURNAL_BYTES:
        raise RuntimeFailure(
            ErrorCode.BACKEND_PROTOCOL_FAILURE,
            "agent-team cleanup journal exceeds its size limit",
        )
    temporary = path.with_name(f".{path.name}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd: int | None = None
    try:
        fd = os.open(temporary, flags, 0o600)
        with os.fdopen(fd, "wb") as journal_file:
            fd = None
            journal_file.write(payload)
            journal_file.flush()
            os.fsync(journal_file.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        if fd is not None:
            os.close(fd)
        try:
            temporary.unlink()
        except OSError:
            pass
        raise RuntimeFailure(
            ErrorCode.BACKEND_PROTOCOL_FAILURE,
            "agent-team cleanup journal could not be saved",
        ) from exc


def journal_assignment(journal: Mapping[str, object], role: str) -> dict[str, object]:
    assignments = journal.get("assignments")
    if not isinstance(assignments, list):
        raise RuntimeFailure(
            ErrorCode.BACKEND_PROTOCOL_FAILURE,
            "agent-team cleanup journal assignments are invalid",
        )
    for assignment in assignments:
        if isinstance(assignment, dict) and assignment.get("role") == role:
            return assignment
    raise RuntimeFailure(
        ErrorCode.BACKEND_PROTOCOL_FAILURE,
        "agent-team cleanup journal assignment is missing",
    )


def validated_assignments(
    state: Mapping[str, object], state_path: Path, journal: Mapping[str, object]
) -> list[tuple[str, dict[str, object], str, str, str, str]]:
    roles = state.get("roles")
    role_specs = state.get("role_specs")
    if not isinstance(roles, dict) or not isinstance(role_specs, dict):
        raise RuntimeFailure(
            ErrorCode.BACKEND_PROTOCOL_FAILURE,
            "agent-team state has invalid role metadata",
        )
    validated: list[tuple[str, dict[str, object], str, str, str, str]] = []
    state_root = state_path.parent.resolve(strict=False)
    for role_name, raw_assignment in roles.items():
        if not isinstance(role_name, str) or not isinstance(raw_assignment, dict):
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "agent-team state has an invalid role assignment",
            )
        dispatch_id = raw_assignment.get("dispatch_id")
        terminal_id = raw_assignment.get("terminal_handle")
        if not isinstance(dispatch_id, str) or not isinstance(terminal_id, str):
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "agent-team state is missing a Dispatch id",
            )
        if raw_assignment.get("launcher_owned_terminal") is not True:
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "agent-team refuses to stop a terminal with unknown ownership",
            )
        role_spec = role_specs.get(role_name)
        if not isinstance(role_spec, dict):
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "agent-team state is missing a role spec",
            )
        transport = role_spec.get("transport")
        execution = role_spec.get("execution")
        if execution not in {"background", "tui_direct"}:
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "role assignment has an unsupported execution",
            )
        if execution == "background":
            local_stage = "pending"
            journal_assignment_value = journal_assignment(journal, role_name)
            raw_stage = journal_assignment_value.get("local")
            if isinstance(raw_stage, str):
                local_stage = raw_stage
            validate_background_assignment(
                role_name,
                raw_assignment,
                state_path=state_path,
                state_root=state_root,
                local_stage=local_stage,
            )
        elif transport not in {"direct", "acp"}:
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "role assignment has an unsupported transport",
            )
        validated.append(
            (
                role_name,
                raw_assignment,
                dispatch_id,
                terminal_id,
                str(execution),
                str(transport),
            )
        )
    return validated


def validate_background_assignment(
    role_name: str,
    assignment: Mapping[str, object],
    *,
    state_path: Path,
    state_root: Path,
    local_stage: str = "pending",
) -> None:
    raw_prompt = assignment.get("prompt_path")
    nonce = assignment.get("launch_nonce")
    if not isinstance(raw_prompt, str) or not isinstance(nonce, str):
        raise RuntimeFailure(
            ErrorCode.BACKEND_PROTOCOL_FAILURE,
            "background assignment is missing prompt cleanup identity",
        )
    if local_stage not in {"done", "prompt_started"}:
        try:
            validate_prompt_file(
                Path(raw_prompt),
                state_path.parent,
                role=role_name,
                launch_nonce=nonce,
            )
        except RuntimeValidationError as exc:
            prompt = Path(raw_prompt).expanduser().absolute()
            state_root = state_path.parent.resolve(strict=False)
            expected = state_root / f"prompt-{role_name}-{nonce}.md"
            if (
                role_name in {"planner", "reviewer"}
                and re.fullmatch(r"[a-z0-9]{8,64}", nonce) is not None
                and not prompt.exists()
                and not prompt.is_symlink()
                and prompt.resolve(strict=False) == expected.resolve(strict=False)
            ):
                pass
            else:
                raise RuntimeFailure(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "agent-team cleanup prompt is invalid",
                ) from exc
    if local_stage == "done":
        return
    if local_stage == "prompt_started":
        return
    for root_key in ("provider_private_root", "snapshot_root"):
        raw_root = assignment.get(root_key)
        if not isinstance(raw_root, str):
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "background assignment is missing cleanup root",
            )
        raw_root_path = Path(raw_root).expanduser().absolute()
        root = raw_root_path.resolve(strict=False)
        try:
            root.relative_to(state_root)
        except ValueError:
            pass
        else:
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "background cleanup root must stay outside agent-team state",
            )
        if local_stage == "roots_done":
            continue
        try:
            root_stat = raw_root_path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "background cleanup root is unavailable",
            ) from exc
        if (
            not raw_root_path.is_dir()
            or raw_root_path.is_symlink()
            or root_stat.st_uid != os.getuid()
        ):
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "background cleanup root ownership is unproven",
            )


def cleanup_assignment_phase(
    role_name: str,
    assignment: Mapping[str, object],
    *,
    state_path: Path,
    execution: str,
    transport: str,
    local_stage: str,
    remove_tree: Callable[[Path], None],
) -> str:
    if local_stage == "done":
        return "done"
    if execution == "background" and local_stage == "pending":
        for root_key in ("snapshot_root", "provider_private_root"):
            raw_root = assignment.get(root_key)
            if isinstance(raw_root, str):
                remove_tree(Path(raw_root).expanduser().absolute())
        return "roots_done"
    if execution == "background" or transport == "acp":
        raw_prompt = assignment.get("prompt_path")
        nonce = assignment.get("launch_nonce")
        if not isinstance(raw_prompt, str) or not isinstance(nonce, str):
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "agent-team cleanup prompt identity is incomplete",
            )
        prompt = Path(raw_prompt).expanduser().absolute()
        if not prompt.exists() and not prompt.is_symlink():
            return "done"
        try:
            remove_prompt_file(
                prompt,
                state_path.parent,
                role=role_name,
                launch_nonce=nonce,
            )
        except RuntimeValidationError as exc:
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "agent-team cleanup prompt failed",
            ) from exc
    return "done"
