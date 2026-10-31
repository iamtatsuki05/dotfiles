from __future__ import annotations

import json
import math
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NoReturn

RESULT_NAME: Final = "client-result.json"
MAX_RESULT_BYTES: Final = 1 * 1024 * 1024
_SUCCESS_KEYS = frozenset(
    {"output", "session_id", "model", "effort", "cleanup_confirmed"}
)
_FAILURE_KEYS = frozenset(
    {"error", "session_id", "model", "effort", "cleanup_confirmed"}
)


class NativeClientResultError(ValueError):
    """A native ACP result failed validation or could not be read safely."""


@dataclass(frozen=True, slots=True)
class NativeClientReceipt:
    output: str | None
    error: str | None
    session_id: str | None
    model: str
    effort: str
    cleanup_confirmed: bool

    @property
    def succeeded(self) -> bool:
        return self.error is None

    def as_dict(self) -> dict[str, object]:
        if self.succeeded:
            return {
                "output": self.output,
                "session_id": self.session_id,
                "model": self.model,
                "effort": self.effort,
                "cleanup_confirmed": self.cleanup_confirmed,
            }
        return {
            "error": self.error,
            "session_id": self.session_id,
            "model": self.model,
            "effort": self.effort,
            "cleanup_confirmed": self.cleanup_confirmed,
        }


def parse_client_receipt(
    value: object, *, model: str, effort: str
) -> NativeClientReceipt:
    _requested_role(model, "model")
    _requested_role(effort, "effort")
    if not isinstance(value, Mapping):
        _fail("native client receipt must be an object")
    try:
        keys = frozenset(value)
    except (TypeError, ValueError):
        _fail("native client receipt fields are invalid")
    if keys == _SUCCESS_KEYS:
        output = _nonblank_text(value.get("output"), "output", 100_000)
        success_session_id = _nonempty_text(value.get("session_id"), "session_id")
        cleanup_confirmed = value.get("cleanup_confirmed")
        if type(cleanup_confirmed) is not bool or cleanup_confirmed is not True:
            _fail("native client success cleanup confirmation is invalid")
        _matching_role(value.get("model"), model, "model")
        _matching_role(value.get("effort"), effort, "effort")
        return NativeClientReceipt(
            output=output,
            error=None,
            session_id=success_session_id,
            model=model,
            effort=effort,
            cleanup_confirmed=True,
        )
    if keys == _FAILURE_KEYS:
        error = _nonblank_text(value.get("error"), "error", 4_000)
        session_id_value = value.get("session_id")
        failure_session_id: str | None
        if session_id_value is not None:
            failure_session_id = _nonempty_text(session_id_value, "session_id")
        else:
            failure_session_id = None
        cleanup_confirmed = value.get("cleanup_confirmed")
        if type(cleanup_confirmed) is not bool:
            _fail("native client failure cleanup confirmation is invalid")
        _matching_role(value.get("model"), model, "model")
        _matching_role(value.get("effort"), effort, "effort")
        return NativeClientReceipt(
            output=None,
            error=error,
            session_id=failure_session_id,
            model=model,
            effort=effort,
            cleanup_confirmed=cleanup_confirmed,
        )
    _fail("native client receipt fields are invalid")


def parse_client_receipt_json(
    raw: str, *, model: str, effort: str
) -> NativeClientReceipt:
    if type(raw) is not str:
        _fail("native client stdout must be text")
    try:
        value = _strict_json(raw.encode("utf-8"))
    except (RecursionError, UnicodeError, ValueError, TypeError):
        _fail("native client stdout JSON is invalid")
    return parse_client_receipt(value, model=model, effort=effort)


def read_client_result(
    private_root: Path, *, launch_nonce: str, model: str, effort: str
) -> NativeClientReceipt:
    if not isinstance(launch_nonce, str):
        _fail("native client launch nonce is invalid")
    try:
        contents = _read_result_file(private_root)
        envelope = _strict_json(contents)
    except NativeClientResultError:
        raise
    except (OSError, RecursionError, UnicodeError, ValueError, TypeError):
        _fail("native client result is invalid")
    if not isinstance(envelope, dict):
        _fail("native client result envelope must be an object")
    if frozenset(envelope) != frozenset({"version", "launch_nonce", "receipt"}):
        _fail("native client result envelope fields are invalid")
    if type(envelope.get("version")) is not int or envelope.get("version") != 1:
        _fail("native client result version is unsupported")
    artifact_nonce = envelope.get("launch_nonce")
    if type(artifact_nonce) is not str or artifact_nonce != launch_nonce:
        _fail("native client result launch nonce does not match")
    return parse_client_receipt(envelope.get("receipt"), model=model, effort=effort)


def _fail(message: str) -> NoReturn:
    raise NativeClientResultError(message)


def _requested_role(value: object, field: str) -> str:
    if type(value) is not str or _has_surrogate(value):
        _fail(f"native client {field} is invalid")
    return value


def _matching_role(value: object, expected: str, field: str) -> None:
    if type(value) is not str or _has_surrogate(value) or value != expected:
        _fail(f"native client {field} does not match requested role")


def _nonempty_text(value: object, field: str) -> str:
    if type(value) is not str or not value or _has_surrogate(value):
        _fail(f"native client {field} is invalid")
    return value


def _nonblank_text(value: object, field: str, maximum: int) -> str:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > maximum
        or _has_surrogate(value)
    ):
        _fail(f"native client {field} is invalid")
    return value


def _has_surrogate(value: str) -> bool:
    return any(0xD800 <= ord(character) <= 0xDFFF for character in value)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON number: {value}")


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite JSON number")
    return result


def _strict_json(contents: bytes) -> object:
    text = contents.decode("utf-8")
    value = json.loads(
        text,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_constant,
        parse_float=_finite_float,
    )
    _validate_json_strings(value)
    return value


def _validate_json_strings(value: object) -> None:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, str):
            if _has_surrogate(current):
                raise ValueError("JSON contains an invalid Unicode surrogate")
        elif isinstance(current, dict):
            pending.extend(current.keys())
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)


def _identity(info: os.stat_result) -> tuple[int, ...]:
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


def _read_result_file(private_root: Path) -> bytes:
    if not isinstance(private_root, Path) or not private_root.is_absolute():
        _fail("native client private root must be an absolute path")
    try:
        if private_root.resolve(strict=True) != private_root:
            _fail("native client private root must be canonical")
        root_before = os.stat(private_root, follow_symlinks=False)
        _check_private_root(root_before)
    except NativeClientResultError:
        raise
    except (OSError, RuntimeError, ValueError):
        _fail("native client private root is unsafe")

    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        _fail("native client result requires no-follow file access")
    flags = os.O_RDONLY | nofollow
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    if directory_flag:
        flags |= directory_flag
    try:
        root_fd = os.open(private_root, flags)
    except OSError:
        _fail("native client private root is unavailable")
    try:
        root_opened = os.fstat(root_fd)
        if _identity(root_opened) != _identity(root_before):
            _fail("native client private root changed while opening")
        _check_private_root(root_opened)
        result_before = os.stat(RESULT_NAME, dir_fd=root_fd, follow_symlinks=False)
        _check_result_file(result_before)
        file_flags = os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0)
        try:
            result_fd = os.open(RESULT_NAME, file_flags, dir_fd=root_fd)
        except OSError:
            _fail("native client result is unavailable")
        try:
            result_opened = os.fstat(result_fd)
            if _identity(result_opened) != _identity(result_before):
                _fail("native client result changed while opening")
            contents = _read_bounded(result_fd)
            root_after = os.fstat(root_fd)
            root_path_after = os.stat(private_root, follow_symlinks=False)
            result_after_fd = os.fstat(result_fd)
            result_after_path = os.stat(
                RESULT_NAME, dir_fd=root_fd, follow_symlinks=False
            )
            if (
                _identity(root_after) != _identity(root_opened)
                or _identity(root_path_after) != _identity(root_opened)
                or _identity(result_after_fd) != _identity(result_opened)
                or _identity(result_after_path) != _identity(result_opened)
            ):
                _fail("native client result changed while reading")
            return contents
        finally:
            os.close(result_fd)
    except NativeClientResultError:
        raise
    except (OSError, ValueError):
        _fail("native client result could not be read safely")
    finally:
        os.close(root_fd)


def _check_private_root(info: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        _fail("native client private root has unsafe ownership or mode")


def _check_result_file(info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
        or info.st_size > MAX_RESULT_BYTES
    ):
        _fail("native client result has unsafe ownership, mode, or type")


def _read_bounded(fd: int) -> bytes:
    data = bytearray()
    while len(data) <= MAX_RESULT_BYTES:
        chunk = os.read(fd, min(65_536, MAX_RESULT_BYTES + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
    if len(data) > MAX_RESULT_BYTES:
        _fail("native client result exceeds the size limit")
    return bytes(data)
