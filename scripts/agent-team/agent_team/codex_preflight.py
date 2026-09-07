"""Configuration and authentication gates before the selected Codex app-server.

Cloud requirements need a separate authentication check. An app-server RPC is
too late for either check: managed feature pins can start plugins at launch.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import stat
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .runtime import RuntimeValidationError

_SYSTEM_PATHS = (
    Path("/etc/codex/config.toml"),
    Path("/etc/codex/requirements.toml"),
    Path("/etc/codex/managed_config.toml"),
)
_MANAGED_KEYS = ("config_toml_base64", "requirements_toml_base64")
_PERSONAL_PLANS = frozenset({"free", "go", "plus", "pro", "prolite"})
_MAX_AUTH_BYTES = 262_144
_PROJECT_KEYS = frozenset(
    {
        "model",
        "model_reasoning_effort",
        "model_reasoning_summary",
        "model_verbosity",
        "mcp_servers",
    }
)


@dataclass(frozen=True)
class FileAuthBinding:
    path: Path
    sha256: str
    device: int
    inode: int
    plan_type: str
    expires_at: int


def _file_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _read_owned_file(
    path: Path, *, private: bool, limit: int
) -> tuple[bytes, os.stat_result]:
    try:
        before = path.lstat()
        if (
            not path.is_absolute()
            or not stat.S_ISREG(before.st_mode)
            or before.st_uid not in ({os.getuid()} if private else {0, os.getuid()})
            or (
                stat.S_IMODE(before.st_mode) != 0o600
                if private
                else before.st_mode & 0o022
            )
            or before.st_nlink != 1
            or before.st_size > limit
            or path.resolve(strict=True) != path
        ):
            raise RuntimeValidationError(
                "Codex artifact requires a safely owned canonical regular file"
            )
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            opened = os.fstat(fd)
            if _file_identity(before) != _file_identity(opened):
                raise RuntimeValidationError("Codex artifact changed while opening")
            with os.fdopen(fd, "rb", closefd=False) as source:
                contents = source.read(limit + 1)
            if len(contents) > limit or any(
                _file_identity(value) != _file_identity(opened)
                for value in (os.fstat(fd), path.lstat())
            ):
                raise RuntimeValidationError("Codex artifact changed while reading")
            return contents, opened
        finally:
            os.close(fd)
    except OSError:
        raise RuntimeValidationError("cannot read bound Codex artifact") from None


def _jwt_claims(token: object) -> dict[str, object]:
    if (
        not isinstance(token, str)
        or len(token.split(".")) != 3
        or not all(token.split("."))
    ):
        raise RuntimeValidationError(
            "Codex authentication token claims are unavailable"
        )
    try:
        payload = token.split(".")[1]
        decoded = base64.b64decode(
            payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True
        )
        if base64.urlsafe_b64encode(decoded).decode().rstrip("=") != payload:
            raise ValueError("noncanonical JWT payload")
        claims = _strict_json(decoded)
    except (ValueError, UnicodeError, binascii.Error, RecursionError):
        raise RuntimeValidationError(
            "Codex authentication token claims are invalid"
        ) from None
    if not isinstance(claims, dict):
        raise RuntimeValidationError(
            "Codex authentication token claims must be an object"
        )
    return claims


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON field")
        value[key] = item
    return value


def _strict_json(payload: bytes) -> object:
    value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            item.encode("utf-8")
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError("non-finite JSON number")
        elif type(item) is int and not -(2**63) <= item <= 2**64 - 1:
            raise ValueError("JSON integer out of range")
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return value


def inspect_file_auth(path: Path, *, now: float | None = None) -> FileAuthBinding:
    """Inspect startup eligibility without returning or modifying credentials.

    Codex 0.153.4 refreshes access tokens within five minutes of expiry before
    checking cloud requirements. The extra minute bounds prelaunch handoff.
    This gate does not authenticate the token or suppress later OAuth refresh.
    """
    contents, info = _read_owned_file(path, private=True, limit=_MAX_AUTH_BYTES)
    try:
        value = _strict_json(contents)
    except (ValueError, UnicodeError, RecursionError):
        raise RuntimeValidationError(
            "Codex file authentication is invalid JSON"
        ) from None
    if (
        not isinstance(value, dict)
        or value.get("auth_mode") not in (None, "chatgpt")
        or any(
            value.get(key) is not None
            for key in (
                "OPENAI_API_KEY",
                "personal_access_token",
                "bedrock_api_key",
                "bedrock_access_keys",
                "agent_identity",
            )
        )
        or not isinstance(value.get("tokens"), dict)
    ):
        raise RuntimeValidationError(
            "scoped Codex ACP requires ChatGPT file authentication"
        )
    tokens = value["tokens"]
    if any(
        not isinstance(tokens.get(key), str) or not tokens[key]
        for key in ("id_token", "access_token", "refresh_token", "account_id")
    ):
        raise RuntimeValidationError("Codex file authentication is incomplete")
    auth_claims = _jwt_claims(tokens["id_token"]).get("https://api.openai.com/auth")
    plan = (
        auth_claims.get("chatgpt_plan_type") if isinstance(auth_claims, dict) else None
    )
    if not isinstance(plan, str) or plan not in _PERSONAL_PLANS:
        raise RuntimeValidationError(
            "scoped Codex ACP requires a known personal ChatGPT plan"
        )
    expiry = _jwt_claims(tokens["access_token"]).get("exp")
    current_time = time.time() if now is None else now
    if (
        not math.isfinite(current_time)
        or type(expiry) is not int
        or expiry <= current_time + 360
        or expiry > 253_402_300_799
    ):
        raise RuntimeValidationError(
            "Codex access token is not fresh enough for scoped startup"
        )
    return FileAuthBinding(
        path=path,
        sha256=hashlib.sha256(contents).hexdigest(),
        device=info.st_dev,
        inode=info.st_ino,
        plan_type=plan,
        expires_at=expiry,
    )


def file_auth_path(home: Path) -> Path:
    if not home.is_absolute():
        raise RuntimeValidationError("normal CODEX_HOME must be absolute")
    try:
        home = home.resolve(strict=True)
        source = home / "config.toml"
        try:
            source.lstat()
        except FileNotFoundError:
            config: dict[str, object] = {}
        else:
            contents, _ = _read_owned_file(
                source.resolve(strict=True), private=False, limit=1_048_576
            )
            config = tomllib.loads(contents.decode("utf-8"))
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise RuntimeValidationError(
            "normal Codex authentication configuration is unavailable"
        ) from None
    if config.get("cli_auth_credentials_store", "file") != "file":
        raise RuntimeValidationError(
            "scoped Codex ACP requires normal file-backed ChatGPT authentication"
        )
    return home / "auth.json"


def project_configuration_snapshot(workspace: Path) -> dict[str, str | None]:
    """Bind the project layers discovered with Codex's fixed .git root marker."""
    workspace = workspace.resolve(strict=True)
    if not workspace.is_dir():
        raise RuntimeValidationError("Codex workspace must be a directory")
    ancestors = [workspace, *workspace.parents]
    project_root = workspace
    for ancestor in ancestors:
        marker = ancestor / ".git"
        try:
            info = marker.stat()
            if stat.S_ISDIR(info.st_mode):
                (marker / "HEAD").stat()
        except FileNotFoundError:
            continue
        except OSError:
            raise RuntimeValidationError("cannot inspect Codex project root") from None
        project_root = ancestor
        break
    layers = ancestors[: ancestors.index(project_root) + 1]
    snapshot: dict[str, str | None] = {}
    for directory in reversed(layers):
        folder = directory / ".codex"
        source = folder / "config.toml"
        snapshot[str(source)] = None
        try:
            folder_info = folder.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise RuntimeValidationError(
                "cannot inspect project Codex configuration"
            ) from None
        if (
            not stat.S_ISDIR(folder_info.st_mode)
            or folder.resolve(strict=True) != folder
        ):
            raise RuntimeValidationError(
                "project Codex configuration directory is not canonical"
            )
        try:
            source.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise RuntimeValidationError(
                "cannot inspect project Codex configuration"
            ) from None
        contents, _ = _read_owned_file(source, private=False, limit=1_048_576)
        try:
            config = tomllib.loads(contents.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            raise RuntimeValidationError(
                "project Codex configuration is invalid TOML"
            ) from None
        unknown = set(config) - _PROJECT_KEYS
        if unknown:
            raise RuntimeValidationError(
                "unsupported project Codex configuration keys: "
                + ", ".join(sorted(unknown))
            )
        snapshot[str(source)] = hashlib.sha256(contents).hexdigest()
    return snapshot


def _macos_managed_preferences() -> tuple[str, ...]:
    import ctypes

    core = ctypes.CDLL(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    )
    core.CFStringCreateWithCString.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_uint32,
    ]
    core.CFStringCreateWithCString.restype = ctypes.c_void_p
    core.CFPreferencesCopyAppValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    core.CFPreferencesCopyAppValue.restype = ctypes.c_void_p
    core.CFRelease.argtypes = [ctypes.c_void_p]
    core.CFRelease.restype = None

    domain = core.CFStringCreateWithCString(None, b"com.openai.codex", 0x08000100)
    if not domain:
        raise OSError("managed preferences domain could not be allocated")
    present: list[str] = []
    try:
        for name in _MANAGED_KEYS:
            key = core.CFStringCreateWithCString(None, name.encode("ascii"), 0x08000100)
            if not key:
                raise OSError("managed preferences key could not be allocated")
            try:
                value = core.CFPreferencesCopyAppValue(key, domain)
                if value:
                    present.append(name)
                    core.CFRelease(value)
            finally:
                core.CFRelease(key)
    finally:
        core.CFRelease(domain)
    return tuple(present)


def assert_no_system_configuration() -> None:
    if sys.platform not in {"darwin", "linux"}:
        raise RuntimeValidationError("scoped Codex ACP requires macOS or Linux")
    for source in _SYSTEM_PATHS:
        try:
            source.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RuntimeValidationError(
                "cannot establish absence of Codex system or managed configuration"
            ) from exc
        raise RuntimeValidationError(
            f"scoped Codex ACP does not support system or managed configuration: {source}"
        )
    if sys.platform == "darwin":
        try:
            present = _macos_managed_preferences()
        except (OSError, AttributeError) as exc:
            raise RuntimeValidationError(
                "cannot establish absence of Codex managed preferences"
            ) from exc
        if present:
            raise RuntimeValidationError(
                "scoped Codex ACP does not support com.openai.codex managed preferences"
            )
