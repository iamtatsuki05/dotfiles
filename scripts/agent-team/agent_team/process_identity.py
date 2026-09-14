"""Read exact argument vectors for local processes without display parsing."""

from __future__ import annotations

import ctypes
import os
import sys
import sysconfig
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

_CTL_KERN: Final = 1
_KERN_PROCARGS2: Final = 49
_MAX_ARG_BYTES: Final = 4 * 1024 * 1024


def _decode_args(values: Sequence[bytes]) -> tuple[str, ...] | None:
    try:
        return tuple(value.decode("utf-8") for value in values)
    except UnicodeDecodeError:
        return None


def _linux_process_argv(pid: int) -> tuple[str, ...] | None:
    path = Path("/proc") / str(pid) / "cmdline"
    try:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_ARG_BYTES + 1)
    except (OSError, ValueError):
        return None
    if not raw or len(raw) > _MAX_ARG_BYTES:
        return None
    if raw.endswith(b"\0"):
        raw = raw[:-1]
    if not raw:
        return None
    return _decode_args(raw.split(b"\0"))


def _sysctl_function() -> Callable[..., int]:
    libc = ctypes.CDLL(None, use_errno=True)
    function = libc.sysctl
    function.argtypes = [
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.c_void_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    return function


def _find_nul(buffer: ctypes.Array[ctypes.c_char], start: int, end: int) -> int | None:
    for offset in range(start, end):
        if buffer[offset] == b"\0":
            return offset
    return None


def _darwin_process_argv(pid: int) -> tuple[str, ...] | None:
    try:
        sysctl = _sysctl_function()
        mib = (ctypes.c_int * 3)(_CTL_KERN, _KERN_PROCARGS2, pid)
        size = ctypes.c_size_t(0)
        if sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0:
            return None
        if size.value < ctypes.sizeof(ctypes.c_int) or size.value > _MAX_ARG_BYTES:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        capacity = size.value
        size = ctypes.c_size_t(capacity)
        if (
            sysctl(
                mib,
                3,
                ctypes.cast(buffer, ctypes.c_void_p),
                ctypes.byref(size),
                None,
                0,
            )
            != 0
        ):
            return None
        result_size = size.value
        if result_size < ctypes.sizeof(ctypes.c_int) or result_size > capacity:
            return None
        argc = ctypes.c_int.from_buffer(buffer).value
        if argc < 1 or argc > _MAX_ARG_BYTES or argc > result_size:
            return None
        offset = ctypes.sizeof(ctypes.c_int)
        path_end = _find_nul(buffer, offset, result_size)
        if path_end is None or path_end == offset:
            return None
        offset = path_end
        while offset < result_size and buffer[offset] == b"\0":
            offset += 1
        if offset >= result_size:
            return None

        values: list[bytes] = []
        base = ctypes.addressof(buffer)
        for _ in range(argc):
            end = _find_nul(buffer, offset, result_size)
            if end is None:
                return None
            values.append(ctypes.string_at(base + offset, end - offset))
            offset = end + 1
        return _decode_args(values)
    except (AttributeError, OSError, OverflowError, TypeError, ValueError):
        return None


def read_process_argv(pid: int) -> tuple[str, ...] | None:
    """Return an exact argv tuple, or ``None`` when identity is unproven."""

    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if sys.platform.startswith("linux"):
        return _linux_process_argv(pid)
    if sys.platform == "darwin":
        return _darwin_process_argv(pid)
    return None


def python_process_argv(launch_argv: Sequence[str]) -> tuple[str, ...]:
    """Describe the kernel argv for a child of this exact Python interpreter."""

    if not launch_argv or launch_argv[0] != sys.executable:
        raise ValueError("Python launch argv must use the current interpreter")
    if sys.platform == "darwin" and sysconfig.get_config_var("PYTHONFRAMEWORK"):
        # CPython's Mac/Tools/pythonw.c replaces argv[0] with its application
        # executable while preserving the launcher/venv in __PYVENV_LAUNCHER__.
        current = read_process_argv(os.getpid())
        if not current or not Path(current[0]).is_absolute():
            raise RuntimeError("Python framework process argv identity is unavailable")
        return (current[0], *launch_argv[1:])
    return tuple(launch_argv)
