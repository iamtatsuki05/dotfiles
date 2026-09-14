"""Shared native ACP role snapshots and dependency preflight."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from . import native_acp_dependencies
from .contracts import ErrorCode, RoleTarget, RuntimeFailure, role_id, role_kind
from .runtime import RuntimeValidationError
from .scoped_acp import (
    SCOPED_AGENT,
    SCOPED_CLIENT,
    SCOPED_POLICY,
    SCOPED_QUESTIONS,
    checked_digest,
    native_profile,
)


def _runtime_error(
    exc: BaseException,
    context: str,
    *,
    code: ErrorCode = ErrorCode.BACKEND_PROTOCOL_FAILURE,
) -> RuntimeFailure:
    if isinstance(exc, RuntimeFailure):
        return exc
    return RuntimeFailure(code, f"{context}: {type(exc).__name__}"[:240])


def role_spec_snapshot(spec: object, role: RoleTarget) -> dict[str, object]:
    """Normalize one selected role spec before it is persisted."""

    if not hasattr(spec, "provider"):
        raise RuntimeFailure(
            ErrorCode.INVALID_REQUEST,
            f"role spec is invalid: {role_id(role)}",
        )
    # RoleSpec is typed, but the backend still validates the boundary before
    # writing durable state so a caller cannot smuggle arbitrary role data in.
    values = {
        key: getattr(spec, key, None)
        for key in (
            "provider",
            "transport",
            "model",
            "effort",
            "permission",
            "instructions",
            "execution",
        )
    }
    if not all(isinstance(value, str) and value for value in values.values()):
        raise RuntimeFailure(
            ErrorCode.INVALID_REQUEST,
            f"role spec is incomplete: {role_id(role)}",
        )
    result: dict[str, object] = dict(values)
    adapter_id = getattr(spec, "adapter_id", None)
    if adapter_id is not None:
        if not isinstance(adapter_id, str) or not adapter_id:
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST,
                f"role spec adapter is invalid: {role_id(role)}",
            )
        result["adapter_id"] = adapter_id
    raw_executables = getattr(spec, "acp_executables", None)
    if raw_executables is not None:
        if not isinstance(raw_executables, Mapping):
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST,
                f"role spec ACP bindings are invalid: {role_id(role)}",
            )
        result["acp_executables"] = dict(raw_executables)
    wrapper_digest = getattr(spec, "scoped_wrapper_sha256", None)
    if wrapper_digest is not None:
        result["scoped_wrapper_sha256"] = wrapper_digest
    client_digest = getattr(spec, "scoped_client_sha256", None)
    if client_digest is not None:
        result["scoped_client_sha256"] = client_digest
    policy_digest = getattr(spec, "scoped_policy_sha256", None)
    if policy_digest is not None:
        result["scoped_policy_sha256"] = policy_digest
    question_digest = getattr(spec, "scoped_question_client_sha256", None)
    if question_digest is not None:
        result["scoped_question_client_sha256"] = question_digest
    provider_snapshot = getattr(spec, "provider_snapshot", None)
    if provider_snapshot is not None:
        if not isinstance(provider_snapshot, Mapping):
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST, "provider snapshot must be a mapping"
            )
        result["provider_snapshot"] = copy.deepcopy(dict(provider_snapshot))
    return result


def preflight_scoped_role(
    normalized: dict[str, object],
    role: RoleTarget,
    workspace: Path,
    *,
    preflight: bool = True,
) -> (
    native_acp_dependencies.NativeAcpExecutables
    | native_acp_dependencies.CodexAcpExecutables
    | None
):
    """Validate and, optionally, preflight one native ACP role binding."""

    try:
        expected = native_profile(
            cast(str, normalized.get("provider")), role_kind(role).value
        )
    except RuntimeValidationError as exc:
        raise RuntimeFailure(ErrorCode.INVALID_REQUEST, str(exc)) from exc
    if any(normalized.get(key) != value for key, value in expected.items()):
        raise RuntimeFailure(
            ErrorCode.INVALID_REQUEST,
            f"native {role_id(role)} does not match its scoped ACP profile",
        )
    raw = normalized.get("acp_executables")
    if not isinstance(raw, Mapping):
        raise RuntimeFailure(
            ErrorCode.INVALID_REQUEST,
            f"native {role_id(role)} is missing ACP executable bindings",
        )
    if not preflight:
        return None

    try:
        executables: (
            native_acp_dependencies.CodexAcpExecutables
            | native_acp_dependencies.NativeAcpExecutables
        )
        if normalized["provider"] == "codex":
            # Keep provider loading lazy: only the selected Codex profile may
            # import its auth snapshot verifier.
            from . import codex_acp

            executables = native_acp_dependencies.CodexAcpExecutables.from_dict(raw)
            executables.verify()
            provider_snapshot = normalized.get("provider_snapshot")
            if not isinstance(provider_snapshot, Mapping):
                raise RuntimeValidationError("Codex provider snapshot is missing")
            codex_acp.verify_snapshot(provider_snapshot, workspace)
        else:
            executables = native_acp_dependencies.NativeAcpExecutables.from_dict(raw)
            executables.verify()
        # This snapshot is deliberately taken before any state, task, or
        # process creation so a changed selected binding cannot be persisted.
        snapshot: object = (
            native_acp_dependencies.codex_adapter_snapshot(executables)
            if isinstance(executables, native_acp_dependencies.CodexAcpExecutables)
            else native_acp_dependencies.adapter_snapshot(executables)
        )
    except Exception as exc:
        raise _runtime_error(
            exc,
            f"selected {role_id(role)} ACP dependencies are unavailable",
            code=ErrorCode.INVALID_REQUEST,
        ) from exc
    if not isinstance(snapshot, Mapping):
        raise RuntimeFailure(
            ErrorCode.BACKEND_PROTOCOL_FAILURE,
            "ACP adapter snapshot is invalid",
        )
    normalized["acp_executables"] = executables.as_dict()
    if normalized["provider"] == "claude":
        normalized["scoped_wrapper_sha256"] = checked_digest(SCOPED_AGENT)
        normalized["scoped_client_sha256"] = checked_digest(SCOPED_CLIENT)
        normalized["scoped_policy_sha256"] = checked_digest(SCOPED_POLICY)
        normalized["scoped_question_client_sha256"] = checked_digest(SCOPED_QUESTIONS)
    return executables
