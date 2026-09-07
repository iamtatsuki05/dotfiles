"""Frozen policy artifacts and the native ACP profile contract."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import stat
from collections.abc import Mapping
from pathlib import Path

from .native_acp_dependencies import CodexAcpExecutables, NativeAcpExecutables
from .runtime import RuntimeValidationError
from .task_spec import TaskSpec

SCOPED_ADAPTER_ID = "claude-acp-scoped-0.70.0"
CODEX_SCOPED_ADAPTER_ID = "codex-acp-scoped-1.10.0"
SCOPED_AGENT = Path(__file__).resolve().with_name("claude_scoped_agent.mjs")
SCOPED_CLIENT = Path(__file__).resolve().with_name("scoped_acp_client.mjs")
SCOPED_POLICY = Path(__file__).resolve().with_name("scoped_policy.mjs")
SCOPED_QUESTIONS = Path(__file__).resolve().with_name("scoped_question_client.mjs")


def native_profile(provider: str, role: str) -> dict[str, str]:
    if (
        not isinstance(provider, str)
        or not isinstance(role, str)
        or provider not in {"claude", "codex"}
        or role not in {"planner", "worker", "reviewer"}
    ):
        raise RuntimeValidationError("unsupported native ACP provider or role")
    adapter = (
        CODEX_SCOPED_ADAPTER_ID
        if provider == "codex"
        else (SCOPED_ADAPTER_ID if role == "worker" else "claude-acp-0.70.0")
    )
    return {
        "provider": provider,
        "transport": "acp",
        "permission": "workspace-write" if role == "worker" else "read-only",
        "execution": "background",
        "adapter_id": adapter,
    }


def client_argv(
    executables: NativeAcpExecutables | CodexAcpExecutables,
    agent_command: str,
    *,
    harness: str,
    workspace: Path,
    permission: str,
    model: str,
    effort: str,
    instructions: str,
    timeout_seconds: int,
    question_socket: Path | None = None,
    result_file: Path | None = None,
    launch_nonce: str | None = None,
) -> list[str]:
    if not (
        harness == "claude"
        and isinstance(executables, NativeAcpExecutables)
        or harness == "codex"
        and isinstance(executables, CodexAcpExecutables)
    ):
        raise RuntimeValidationError(
            "native ACP harness does not match its dependencies"
        )
    if question_socket is not None and (
        harness != "claude" or not question_socket.is_absolute()
    ):
        raise RuntimeValidationError(
            "native question socket requires selected Claude and an absolute path"
        )
    if (result_file is None) != (launch_nonce is None) or (
        result_file is not None
        and (not result_file.is_absolute() or result_file.name != "client-result.json")
    ):
        raise RuntimeValidationError(
            "native ACP result requires an absolute client-result.json and launch nonce"
        )
    executables.verify()
    return [
        str(executables.node),
        str(SCOPED_CLIENT),
        "--harness",
        harness,
        "--sdk-entry",
        str(executables.sdk),
        "--agent-argv",
        json.dumps(shlex.split(agent_command)),
        "--cwd",
        str(workspace),
        "--permission",
        permission,
        "--model",
        model,
        "--effort",
        effort,
        "--instructions",
        instructions,
        "--timeout-ms",
        str(timeout_seconds * 1_000),
        *(
            ["--question-socket", str(question_socket)]
            if question_socket is not None
            else []
        ),
        *(
            ["--result-file", str(result_file), "--launch-nonce", str(launch_nonce)]
            if result_file is not None
            else []
        ),
    ]


def checked_digest(path: Path, *, private: bool = False) -> str:
    def identity(value: os.stat_result) -> tuple[int, ...]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
            value.st_nlink,
        )

    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid not in ({os.getuid()} if private else {0, os.getuid()})
        or (stat.S_IMODE(info.st_mode) != 0o600 if private else info.st_mode & 0o022)
        or info.st_nlink != 1
    ):
        raise RuntimeValidationError(
            "scoped ACP artifact has unsafe file ownership or mode"
        )
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(fd)
        if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
            raise RuntimeValidationError("scoped ACP artifact changed during open")
        with os.fdopen(fd, "rb", closefd=False) as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        if identity(os.fstat(fd)) != identity(opened) or identity(
            path.lstat()
        ) != identity(opened):
            raise RuntimeValidationError("scoped ACP artifact changed while reading")
        return digest
    finally:
        os.close(fd)


def create_write_policy(
    private_root: Path,
    workspace: Path,
    state_path: Path,
    task: TaskSpec | None,
    agent_entry: Path,
    *,
    permission: str,
) -> tuple[Path, str]:
    path = private_root / "write-policy.json"
    payload = policy_payload(
        private_root, workspace, state_path, task, agent_entry, permission=permission
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as target:
        json.dump(payload, target, ensure_ascii=False, sort_keys=True)
    return path, checked_digest(path, private=True)


def policy_payload(
    private_root: Path,
    workspace: Path,
    state_path: Path,
    task: TaskSpec | None,
    agent_entry: Path,
    *,
    permission: str,
) -> dict[str, object]:
    if permission not in {"read-only", "workspace-write"} or (
        task is None and permission != "read-only"
    ):
        raise RuntimeValidationError("scoped ACP permission requires a matching task")
    protected = [
        state_path.parent.resolve(strict=True),
        private_root.resolve(strict=True),
        SCOPED_AGENT.parent,
        agent_entry.parent.parent,
    ]
    normal_home = Path.home()
    for relative in (
        ".claude",
        ".claude.json",
        ".claude/settings.json",
        ".claude/settings.local.json",
        ".claude/.credentials.json",
        ".acpx",
        "Library/Keychains",
    ):
        source = normal_home / relative
        if source.exists():
            protected.append(source.resolve(strict=True))
    return {
        "permission": permission,
        "workspace": str(workspace.resolve(strict=True)),
        "allowed_paths": list(task.allowed_paths) if task is not None else [],
        "forbidden_paths": list(task.forbidden_paths) if task is not None else [],
        "protected_paths": list(dict.fromkeys(str(item) for item in protected)),
    }


def validate_write_policy(
    state: Mapping[str, object],
    assignment: Mapping[str, object],
    spec: Mapping[str, object],
) -> Path:
    task = (
        TaskSpec.from_dict(assignment["task_spec"])
        if "task_spec" in assignment
        else None
    )
    private_root = Path(str(assignment.get("provider_private_root")))
    policy_path = Path(str(assignment.get("write_policy_path")))
    if (
        not private_root.is_absolute()
        or policy_path != private_root / "write-policy.json"
    ):
        raise RuntimeValidationError(
            "scoped ACP policy path does not match its assignment"
        )
    if checked_digest(SCOPED_AGENT) != spec.get("scoped_wrapper_sha256"):
        raise RuntimeValidationError("scoped ACP wrapper changed since team start")
    if checked_digest(SCOPED_CLIENT) != spec.get("scoped_client_sha256"):
        raise RuntimeValidationError("scoped ACP client changed since team start")
    if checked_digest(SCOPED_POLICY) != spec.get("scoped_policy_sha256"):
        raise RuntimeValidationError(
            "scoped ACP shared policy changed since team start"
        )
    if checked_digest(SCOPED_QUESTIONS) != spec.get("scoped_question_client_sha256"):
        raise RuntimeValidationError(
            "scoped ACP question client changed since team start"
        )
    if assignment.get("question_socket") != str(private_root / "q.sock"):
        raise RuntimeValidationError(
            "scoped ACP question socket does not match its assignment"
        )
    if checked_digest(policy_path, private=True) != assignment.get(
        "write_policy_sha256"
    ):
        raise RuntimeValidationError("scoped ACP write policy changed since dispatch")
    executables = spec.get("acp_executables")
    if not isinstance(executables, Mapping) or not isinstance(
        executables.get("agent"), str
    ):
        raise RuntimeValidationError("scoped ACP agent binding is missing")
    expected = policy_payload(
        permission=str(spec["permission"]),
        private_root=private_root,
        workspace=Path(str(state["workspace"])),
        state_path=Path(str(state["state_path"])),
        task=task,
        agent_entry=Path(executables["agent"]),
    )
    if json.loads(policy_path.read_text(encoding="utf-8")) != expected:
        raise RuntimeValidationError(
            "scoped ACP policy does not match the saved TaskSpec"
        )
    return policy_path
