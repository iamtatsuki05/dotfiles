"""Read-only background adapters for provider CLIs.

The module intentionally has no Orca knowledge.  It provides the small seam
used by the outer runner: a provider is preflighted, then executed in a
turn-scoped read snapshot with a bounded process runner.
"""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

MAX_PROCESS_OUTPUT_BYTES = 100_000
MAX_PROMPT_BYTES = 400_000
MAX_SNAPSHOT_FILES = 5_000
MAX_SNAPSHOT_FILE_BYTES = 10_000_000
MAX_SNAPSHOT_TOTAL_BYTES = 100_000_000
SNAPSHOT_PATH_INSTRUCTION = (
    "Repository files are available only in a private read snapshot. "
    "Resolve every repository path relative to the current working directory; "
    "do not use an absolute path from the original workspace.\n\n"
)

_STDIN_EAGAIN_RETRY_SECONDS = 0.01

SAFE_ENV_KEYS = frozenset(
    {"PATH", "HOME", "TMPDIR", "SHELL", "USER", "LOGNAME", "LANG", "TERM"}
)
_SECRET_NAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        "credentials",
        "credentials.json",
        "credentials.yaml",
        "token",
        "tokens",
        "secret",
        "secrets",
        "private_key",
        "id_rsa",
        "id_ed25519",
    }
)
_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".secret", ".token")
_CONFIG_DIRS = frozenset(
    {".agent", ".codex", ".claude", ".cursor", ".gemini", ".opencode", ".github"}
)
_CONFIG_FILES = frozenset(
    {
        "opencode.json",
        "AGENTS.md",
        "CLAUDE.md",
        "MCP.json",
        "mcp.json",
    }
)


class AdapterError(RuntimeError):
    """Base class for deterministic adapter failures."""


class ExecutionError(AdapterError):
    """A provider process failed, timed out, or exceeded an output limit."""


class SnapshotError(AdapterError):
    """A read snapshot could not be made safe and complete."""


class BackgroundAdapter(Protocol):
    adapter_id: str

    def preflight(self, context: AdapterContext) -> AdapterSnapshot: ...

    def execute(
        self,
        context: AdapterContext,
        snapshot: AdapterSnapshot,
        prompt: str,
        runner: ProcessRunner,
    ) -> ExecutionResult: ...


@dataclass(frozen=True)
class AdapterContext:
    provider: str
    role: str
    model: str
    effort: str
    workspace: Path
    private_root: Path


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int
    sha256: str


@dataclass(frozen=True)
class AdapterSnapshot:
    adapter_id: str
    revision: str
    executable: Path
    version: str
    identity: FileIdentity


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass(frozen=True)
class ExecutionResult:
    output: str
    stderr: str
    returncode: int
    timed_out: bool = False


@dataclass(frozen=True)
class ReadSnapshot:
    root: Path
    manifest: tuple[dict[str, object], ...]

    def cleanup(self) -> None:
        _remove_owned_tree(self.root)


class ProcessRunner:
    """Run fixed argv in a new process group with bounded output."""

    def __init__(self, *, max_output_bytes: int = MAX_PROCESS_OUTPUT_BYTES) -> None:
        if max_output_bytes < 1:
            raise ValueError("max_output_bytes must be positive")
        self.max_output_bytes = max_output_bytes

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        input_text: str | None = None,
        timeout_seconds: float = 900.0,
    ) -> ProcessResult:
        if not argv or any(not isinstance(item, str) or not item for item in argv):
            raise ExecutionError("provider argv must be non-empty strings")
        if os.name == "nt":
            raise ExecutionError("provider process runner requires a POSIX runtime")
        if timeout_seconds <= 0 or not math.isfinite(timeout_seconds):
            raise ExecutionError("provider timeout must be finite and positive")
        try:
            input_bytes = None if input_text is None else input_text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ExecutionError("provider input is not valid UTF-8") from exc
        process: subprocess.Popen[bytes] | None = None
        process_group_id: int | None = None
        try:
            process = subprocess.Popen(
                tuple(argv),
                cwd=cwd,
                env=dict(env),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=True,
            )
            if os.name != "nt":
                try:
                    process_group_id = os.getpgid(process.pid)
                except OSError:
                    process_group_id = process.pid
            stdout_data, stderr_data = _bounded_communicate(
                process,
                input_bytes,
                timeout_seconds=timeout_seconds,
                max_output_bytes=self.max_output_bytes,
            )
        except _ProcessTimeout as exc:
            assert process is not None
            _terminate_process_group(process, process_group_id)
            raise ExecutionError(
                f"provider process timed out after {timeout_seconds:g}s"
            ) from exc
        except _ProcessOutputLimit as exc:
            assert process is not None
            _terminate_process_group(process, process_group_id)
            raise ExecutionError(
                "provider output exceeds the configured limit"
            ) from exc
        except (OSError, UnicodeEncodeError, ValueError, RuntimeError) as exc:
            if process is not None:
                _terminate_process_group(process, process_group_id)
            raise ExecutionError(f"provider process could not start: {exc}") from exc
        assert process is not None
        if process_group_id is not None and not _process_group_exited(process_group_id):
            _terminate_process_group(process, process_group_id)
        try:
            decoded_stdout = bytes(stdout_data).decode("utf-8")
            decoded_stderr = bytes(stderr_data).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ExecutionError("provider output is not valid UTF-8") from exc
        return ProcessResult(process.returncode, decoded_stdout, decoded_stderr)


class _ProcessTimeout(Exception):
    pass


class _ProcessOutputLimit(Exception):
    pass


def _bounded_communicate(
    process: subprocess.Popen[bytes],
    input_bytes: bytes | None,
    *,
    timeout_seconds: float,
    max_output_bytes: int,
) -> tuple[bytearray, bytearray]:
    """Drain both pipes without retaining more than the configured bound."""

    assert process.stdout is not None and process.stderr is not None
    stdout_data = bytearray()
    stderr_data = bytearray()
    input_offset = 0
    selector: selectors.BaseSelector | None = None
    stdin_fd: int | None = None
    stdin_payload: bytes | None = None
    stdin_retry_deadline: float | None = None
    try:
        # Poll reports pipe EOF reliably on macOS; kqueue can leave a completed
        # child with an empty readiness set while its descriptors remain mapped.
        selector_type = getattr(selectors, "PollSelector", None)
        selector = (selector_type or selectors.DefaultSelector)()
        stdout_fd = process.stdout.fileno()
        stderr_fd = process.stderr.fileno()
        os.set_blocking(stdout_fd, False)
        os.set_blocking(stderr_fd, False)
        selector.register(stdout_fd, selectors.EVENT_READ, "stdout")
        selector.register(stderr_fd, selectors.EVENT_READ, "stderr")
        if process.stdin is not None:
            stdin_fd = process.stdin.fileno()
            os.set_blocking(stdin_fd, False)
            if input_bytes:
                stdin_payload = input_bytes
                selector.register(stdin_fd, selectors.EVENT_WRITE, stdin_payload)
            else:
                process.stdin.close()
        deadline = time.monotonic() + timeout_seconds

        def drain_output(key: selectors.SelectorKey) -> None:
            while True:
                try:
                    chunk = os.read(key.fd, 65_536)
                except BlockingIOError:
                    break
                if not chunk:
                    selector.unregister(key.fd)
                    break
                target = stdout_data if key.data == "stdout" else stderr_data
                target.extend(chunk)
                if len(target) > max_output_bytes:
                    raise _ProcessOutputLimit()

        while selector.get_map() or process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _ProcessTimeout()
            parent_exited = process.poll() is not None
            select_timeout = 0 if parent_exited else min(remaining, 0.25)
            if stdin_retry_deadline is not None and not parent_exited:
                select_timeout = min(
                    select_timeout,
                    max(0.0, stdin_retry_deadline - time.monotonic()),
                )
            events_ready = selector.select(select_timeout)
            if parent_exited:
                # The parent is the only process whose output belongs to this
                # result.  A descendant may retain inherited pipes forever;
                # drain bytes already buffered, then let the group fence kill
                # that descendant instead of waiting for EOF.
                for key in list(selector.get_map().values()):
                    if key.data in {"stdout", "stderr"}:
                        drain_output(key)
                    else:
                        selector.unregister(key.fd)
                        if process.stdin is not None and not process.stdin.closed:
                            process.stdin.close()
                break
            if (
                stdin_retry_deadline is not None
                and time.monotonic() >= stdin_retry_deadline
            ):
                assert stdin_fd is not None and stdin_payload is not None
                selector.register(stdin_fd, selectors.EVENT_WRITE, stdin_payload)
                stdin_retry_deadline = None
            for key, events in events_ready:
                if key.data in {"stdout", "stderr"}:
                    drain_output(key)
                    continue
                if events & selectors.EVENT_WRITE:
                    payload = key.data
                    assert isinstance(payload, bytes)
                    try:
                        written = os.write(key.fd, payload[input_offset:])
                    except BlockingIOError as exc:
                        if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                            raise
                        selector.unregister(key.fd)
                        stdin_retry_deadline = min(
                            deadline,
                            time.monotonic() + _STDIN_EAGAIN_RETRY_SECONDS,
                        )
                        continue
                    except BrokenPipeError:
                        selector.unregister(key.fd)
                        if process.stdin is not None and not process.stdin.closed:
                            process.stdin.close()
                        continue
                    input_offset += written
                    if input_offset == len(payload):
                        selector.unregister(key.fd)
                        assert process.stdin is not None
                        process.stdin.close()
    finally:
        if selector is not None:
            try:
                selector.close()
            except (OSError, RuntimeError, ValueError):
                pass
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except (OSError, RuntimeError, ValueError):
                    pass
    process.wait()
    return stdout_data, stderr_data


def _terminate_process_group(
    process: subprocess.Popen[bytes], process_group_id: int | None = None
) -> None:
    if os.name == "nt":
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=2.0)
            return
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired as exc:
                raise ExecutionError("provider process could not be reaped") from exc
        return

    group_id = process_group_id or process.pid
    try:
        os.killpg(group_id, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError:
        if process.poll() is None:
            process.terminate()
    if _wait_for_process_group_exit(group_id, timeout_seconds=2.0, process=process):
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired as exc:
            raise ExecutionError("provider process could not be reaped") from exc
        return
    try:
        os.killpg(group_id, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        if process.poll() is None:
            process.kill()
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired as exc:
        raise ExecutionError("provider process group could not be reaped") from exc
    if not _wait_for_process_group_exit(group_id, timeout_seconds=2.0, process=process):
        raise ExecutionError("provider process group could not be reaped")


def _wait_for_process_group_exit(
    group_id: int,
    *,
    timeout_seconds: float,
    process: subprocess.Popen[bytes] | None = None,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while _process_group_exited(group_id) is False:
        if process is not None and process.poll() is not None:
            try:
                process.wait(timeout=0)
            except subprocess.TimeoutExpired:
                pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(remaining, 0.05))
    return True


def _process_group_exited(group_id: int) -> bool:
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    if sys.platform.startswith("linux"):
        live_member = _linux_process_group_has_live_member(group_id)
        if live_member is False:
            return True
    return False


def _linux_process_group_has_live_member(group_id: int) -> bool | None:
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return None
    try:
        entries = tuple(proc_root.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            stat_line = (entry / "stat").read_text(encoding="ascii")
            closing_paren = stat_line.rfind(")")
            fields = stat_line[closing_paren + 2 :].split()
            if len(fields) < 3 or int(fields[2]) != group_id:
                continue
            if fields[0] != "Z":
                return True
        except (OSError, UnicodeDecodeError, ValueError):
            continue
    return False


def safe_environment(
    provider: str,
    *,
    home: Path,
    private_root: Path,
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a closed environment without inheriting credentials or overrides."""

    source_values = os.environ if source is None else source
    result = {
        key: value
        for key, value in source_values.items()
        if key in SAFE_ENV_KEYS or key.startswith("LC_")
    }
    result["HOME"] = str(home)
    if provider == "copilot":
        result["COPILOT_HOME"] = str(private_root / "copilot-home")
    elif provider == "opencode":
        for name in (
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "XDG_CACHE_HOME",
        ):
            result[name] = str(private_root / name.lower())
        api_key = source_values.get("OPENCODE_API_KEY")
        if api_key:
            result["OPENCODE_API_KEY"] = api_key
    else:
        raise AdapterError(f"unsupported background provider: {provider}")
    return result


def _version_probe(
    executable: Path, *, provider: str, private_root: Path, runner: ProcessRunner
) -> str:
    environment = safe_environment(
        provider,
        home=Path.home(),
        private_root=private_root,
    )
    result = runner.run(
        (str(executable), "--version"),
        cwd=private_root,
        env=environment,
        timeout_seconds=15,
    )
    if result.returncode != 0:
        raise AdapterError(f"{provider} version probe failed")
    return result.stdout.strip()


def _identity(path: Path) -> FileIdentity:
    try:
        resolved = path.resolve(strict=True)
        file_stat = resolved.stat()
        if not stat.S_ISREG(file_stat.st_mode) or not os.access(resolved, os.X_OK):
            raise AdapterError(f"executable is not a regular executable: {resolved}")
        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    except OSError as exc:
        raise AdapterError(f"executable is unavailable: {path}") from exc
    return FileIdentity(
        device=file_stat.st_dev,
        inode=file_stat.st_ino,
        size=file_stat.st_size,
        mtime_ns=file_stat.st_mtime_ns,
        sha256=digest,
    )


def _resolve_mise(
    package: str, *, private_root: Path, runner: ProcessRunner
) -> Path | None:
    mise = shutil.which("mise")
    if mise is None:
        return None
    result = runner.run(
        (mise, "where", package),
        cwd=private_root,
        env=safe_environment("copilot", home=Path.home(), private_root=private_root),
        timeout_seconds=15,
    )
    if result.returncode != 0:
        return None
    directory = Path(result.stdout.strip())
    if not directory.is_absolute() or not directory.is_dir():
        return None
    return directory


def _preflight(
    context: AdapterContext,
    *,
    adapter_id: str,
    version: str,
    marker: str,
    package: str,
    executable_name: str,
    runner: ProcessRunner,
) -> AdapterSnapshot:
    copilot_adapter = adapter_id == CopilotReadOnlyAdapter.adapter_id
    candidates: list[Path] = []
    if not copilot_adapter:
        path_candidate = shutil.which(executable_name)
        if path_candidate:
            candidates.append(Path(path_candidate))
    mise_root = _resolve_mise(package, private_root=context.private_root, runner=runner)
    if mise_root is not None:
        if copilot_adapter:
            package_platforms: tuple[str, ...] = ()
            if sys.platform == "darwin":
                package_platforms = ("darwin",)
            elif sys.platform.startswith("linux"):
                package_platforms = ("linux", "linuxmusl")
            package_arch = None
            if package_platforms:
                package_arch = {
                    "aarch64": "arm64",
                    "arm64": "arm64",
                    "x86_64": "x64",
                }.get(os.uname().machine.lower())
            if package_arch is not None:
                candidates.extend(
                    mise_root
                    / "lib/node_modules/@github/copilot/node_modules"
                    / f"@github/copilot-{package_platform}-{package_arch}/copilot"
                    for package_platform in package_platforms
                )
        else:
            candidates.append(mise_root / "bin" / executable_name)
            candidates.append(mise_root / executable_name)
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
            identity = _identity(resolved)
            observed = _version_probe(
                resolved,
                provider=context.provider,
                private_root=context.private_root,
                runner=runner,
            )
        except (AdapterError, OSError):
            continue
        if (
            not _exact_version_present(observed, version)
            or marker.lower() not in observed.lower()
        ):
            continue
        return AdapterSnapshot(adapter_id, version, resolved, observed, identity)
    raise AdapterError(
        f"{adapter_id} requires exact {version}; no unambiguous executable was found"
    )


def _validate_snapshot(
    snapshot: AdapterSnapshot, *, context: AdapterContext, runner: ProcessRunner
) -> None:
    current = _identity(snapshot.executable)
    if current != snapshot.identity:
        raise AdapterError("provider executable identity changed after preflight")
    observed = _version_probe(
        snapshot.executable,
        provider=context.provider,
        private_root=context.private_root,
        runner=runner,
    )
    if not _exact_version_present(observed, snapshot.version):
        raise AdapterError("provider executable version changed after preflight")


def _exact_version_present(output: str, version: str) -> bool:
    return re.search(rf"(?<![0-9]){re.escape(version)}(?![0-9])", output) is not None


def _validate_prompt(prompt: str) -> None:
    if not prompt.strip():
        raise AdapterError("provider prompt must not be empty")
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise AdapterError("provider prompt exceeds byte limit")


class CopilotReadOnlyAdapter:
    adapter_id = "github-copilot-direct-readonly-1.0.81"

    def _write_settings(self, context: AdapterContext) -> None:
        home = context.private_root / "copilot-home"
        home.mkdir(parents=True, mode=0o700, exist_ok=True)
        denied_paths = (
            context.private_root,
            context.workspace / ".git",
        )
        settings = {
            "disableAllHooks": True,
            "remote": "off",
            "remoteExport": False,
            "sandbox": {
                "enabled": True,
                "allowBypass": False,
                "auth": {"git": False, "gh": False},
                "userPolicy": {
                    "deniedPaths": [
                        denied
                        for path in denied_paths
                        for denied in (str(path), str(path / "**"))
                    ],
                    "network": {
                        "allowOutbound": False,
                        "allowLocalNetwork": False,
                    },
                    "seatbelt": {"keychainAccess": False},
                },
            },
        }
        target = home / "settings.json"
        target.write_text(json.dumps(settings), encoding="utf-8")
        target.chmod(0o400)

    def preflight(self, context: AdapterContext) -> AdapterSnapshot:
        return _preflight(
            context,
            adapter_id=self.adapter_id,
            version="1.0.81",
            marker="GitHub Copilot CLI",
            package="npm:@github/copilot@1.0.81",
            executable_name="copilot",
            runner=ProcessRunner(),
        )

    def build_argv(
        self, context: AdapterContext, prompt: str, executable: Path | None = None
    ) -> tuple[str, ...]:
        prompt = SNAPSHOT_PATH_INSTRUCTION + prompt
        _validate_prompt(prompt)
        if context.model == "auto" and context.effort != "none":
            raise AdapterError("Copilot model=auto requires effort=none")
        binary = str(executable or Path("copilot"))
        argv = [
            binary,
            "--no-auto-update",
            "--no-custom-instructions",
            "--disable-builtin-mcps",
            "--no-remote",
            "--no-remote-export",
            "--disallow-temp-dir",
            "--mode",
            "plan",
            "--available-tools",
            "view,grep,glob",
            "--allow-tool",
            "read",
            "--deny-tool",
            "shell",
            "--deny-tool",
            "write",
            "--deny-tool",
            "url",
            "--no-ask-user",
            "--output-format",
            "text",
            "--silent",
            "--model",
            context.model,
            "-p",
            prompt,
        ]
        if context.model != "auto" and context.effort != "none":
            argv[argv.index("-p") : argv.index("-p")] = ["--effort", context.effort]
        return tuple(argv)

    def execute(
        self,
        context: AdapterContext,
        snapshot: AdapterSnapshot,
        prompt: str,
        runner: ProcessRunner,
    ) -> ExecutionResult:
        _validate_snapshot(snapshot, context=context, runner=runner)
        self._write_settings(context)
        argv = self.build_argv(context, prompt, snapshot.executable)
        environment = safe_environment(
            "copilot",
            home=Path.home(),
            private_root=context.private_root,
        )
        result = runner.run(argv, cwd=context.workspace, env=environment)
        if result.returncode != 0 or not result.stdout.strip():
            detail = result.stderr.strip()[-1_000:] or "no stderr output"
            raise ExecutionError(
                "Copilot returned no successful final output "
                f"(exit {result.returncode}): {detail}"
            )
        return ExecutionResult(
            result.stdout.strip(), result.stderr[-10_000:], result.returncode
        )


class OpenCodeReadOnlyAdapter:
    adapter_id = "opencode-direct-readonly-1.18.25"

    def preflight(self, context: AdapterContext) -> AdapterSnapshot:
        return _preflight(
            context,
            adapter_id=self.adapter_id,
            version="1.18.25",
            marker="opencode",
            package="opencode@1.18.25",
            executable_name="opencode",
            runner=ProcessRunner(),
        )

    def _write_config(self, context: AdapterContext) -> None:
        config_dir = context.private_root / "xdg_config_home" / "opencode"
        config_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        config = {
            "$schema": "https://opencode.ai/config.json",
            "autoupdate": False,
            "instructions": [],
            "plugin": [],
            "mcp": {},
            "permission": {
                "*": "deny",
                "read": {"*": "deny", str(context.workspace / "**"): "allow"},
                "list": {"*": "deny", str(context.workspace / "**"): "allow"},
                "glob": {"*": "deny", str(context.workspace / "**"): "allow"},
                "grep": {"*": "deny", str(context.workspace / "**"): "allow"},
                "edit": "deny",
                "write": "deny",
                "bash": "deny",
                "webfetch": "deny",
                "task": "deny",
                "external_directory": "deny",
                "lsp": "deny",
                "skill": "deny",
            },
        }
        target = config_dir / "opencode.json"
        target.write_text(json.dumps(config), encoding="utf-8")
        target.chmod(0o400)

    def build_argv(
        self, context: AdapterContext, prompt: str, executable: Path | None = None
    ) -> tuple[str, ...]:
        _validate_prompt(prompt)
        binary = str(executable or Path("opencode"))
        argv = [
            binary,
            "--pure",
            "run",
            prompt,
            "--format",
            "json",
            "--model",
            context.model,
            "--dir",
            str(context.workspace),
        ]
        if context.effort != "none":
            argv.extend(("--variant", context.effort))
        return tuple(argv)

    def execute(
        self,
        context: AdapterContext,
        snapshot: AdapterSnapshot,
        prompt: str,
        runner: ProcessRunner,
    ) -> ExecutionResult:
        _validate_snapshot(snapshot, context=context, runner=runner)
        self._write_config(context)
        environment = safe_environment(
            "opencode",
            home=Path.home(),
            private_root=context.private_root,
        )
        result = runner.run(
            self.build_argv(context, prompt, snapshot.executable),
            cwd=context.workspace,
            env=environment,
        )
        if result.returncode != 0:
            raise ExecutionError("OpenCode returned a non-zero exit status")
        output = _extract_opencode_final(result.stdout)
        if not output:
            raise ExecutionError("OpenCode returned an empty final output")
        return ExecutionResult(output, result.stderr[-10_000:], result.returncode)


def background_adapter(adapter_id: str) -> BackgroundAdapter:
    """Resolve only the two verified background profiles; never fall through."""

    if adapter_id == CopilotReadOnlyAdapter.adapter_id:
        return CopilotReadOnlyAdapter()
    if adapter_id == OpenCodeReadOnlyAdapter.adapter_id:
        return OpenCodeReadOnlyAdapter()
    raise AdapterError(f"background adapter is not registered: {adapter_id}")


def _extract_opencode_final(raw: str) -> str:
    texts: list[str] = []
    lines = [line for line in raw.splitlines() if line.strip()]
    if not lines:
        return ""
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ExecutionError("OpenCode output is not valid JSON events") from exc
        if not isinstance(event, dict):
            continue
        part = event.get("part")
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            texts.append(part["text"])
        elif event.get("type") == "text" and isinstance(event.get("text"), str):
            texts.append(event["text"])
    return "".join(texts).strip()


def create_read_snapshot(
    workspace: Path, *, state_root: Path | None = None
) -> ReadSnapshot:
    try:
        workspace_stat = workspace.lstat()
    except OSError as exc:
        raise SnapshotError("workspace is unavailable") from exc
    if stat.S_ISLNK(workspace_stat.st_mode):
        raise SnapshotError("workspace must not be a symlink")
    source = workspace.resolve(strict=True)
    if not source.is_dir():
        raise SnapshotError("workspace must be a real directory")
    if state_root is not None:
        state = state_root.resolve(strict=False)
        try:
            source.relative_to(state)
        except ValueError:
            pass
        else:
            raise SnapshotError("workspace and state root must be separate")
    temporary = Path(tempfile.mkdtemp(prefix="agent-team-snapshot-"))
    try:
        temporary.chmod(0o700)
        entries = _git_entries(source)
        manifest: list[dict[str, object]] = []
        total = 0
        for relative in entries:
            if len(manifest) >= MAX_SNAPSHOT_FILES:
                raise SnapshotError("snapshot file limit exceeded")
            reason = _exclude_reason(relative)
            if reason is not None:
                manifest.append({"path": relative, "excluded": reason})
                continue
            try:
                source_path = _safe_source_path(source, relative)
            except SnapshotError:
                if _has_symlink_component(source, relative):
                    manifest.append({"path": relative, "excluded": "symlink"})
                    continue
                raise
            try:
                file_stat = source_path.lstat()
            except OSError as exc:
                raise SnapshotError(f"snapshot source disappeared: {relative}") from exc
            if stat.S_ISLNK(file_stat.st_mode):
                manifest.append({"path": relative, "excluded": "symlink"})
                continue
            if not stat.S_ISREG(file_stat.st_mode):
                manifest.append({"path": relative, "excluded": "special-file"})
                continue
            if file_stat.st_size > MAX_SNAPSHOT_FILE_BYTES:
                raise SnapshotError(f"snapshot file is too large: {relative}")
            data = _read_source_bytes(source, relative, file_stat)
            total += len(data)
            if total > MAX_SNAPSHOT_TOTAL_BYTES:
                raise SnapshotError("snapshot total size limit exceeded")
            destination = temporary / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            destination.chmod(0o444)
            manifest.append(
                {
                    "path": relative,
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
        _make_read_only(temporary)
        final = temporary.with_name(temporary.name + "-ready")
        temporary.rename(final)
        return ReadSnapshot(final, tuple(manifest))
    except BaseException:
        _remove_owned_tree(temporary)
        raise


def _git_entries(workspace: Path) -> list[str]:
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(workspace),
                "ls-files",
                "-co",
                "--exclude-standard",
                "-z",
            ],
            check=False,
            capture_output=True,
            cwd=workspace,
            shell=False,
        )
    except OSError as exc:
        raise SnapshotError(f"could not enumerate workspace files: {exc}") from exc
    if result.returncode != 0:
        raise SnapshotError("git file enumeration failed")
    try:
        text = result.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SnapshotError("workspace filename is not valid UTF-8") from exc
    return [item for item in text.split("\0") if item]


def _exclude_reason(relative: str) -> str | None:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise SnapshotError(f"workspace path escapes root: {relative}")
    if any(part in _CONFIG_DIRS for part in path.parts):
        return "provider-or-agent-config"
    if path.name in _CONFIG_FILES:
        return "provider-or-agent-instructions"
    lower_name = path.name.lower()
    if lower_name in _SECRET_NAMES or lower_name.startswith(".env."):
        return "secret-like-name"
    if lower_name.endswith(_SECRET_SUFFIXES):
        return "secret-like-extension"
    return None


def _safe_source_path(root: Path, relative: str) -> Path:
    current = root
    for component in Path(relative).parts:
        current = current / component
        try:
            entry_stat = current.lstat()
        except OSError as exc:
            raise SnapshotError(f"workspace path disappeared: {relative}") from exc
        if stat.S_ISLNK(entry_stat.st_mode):
            raise SnapshotError(f"workspace parent is a symlink: {relative}")
        if (
            current != root
            and not current.is_dir()
            and component != Path(relative).name
        ):
            raise SnapshotError(f"workspace parent is not a directory: {relative}")
    return current


def _has_symlink_component(root: Path, relative: str) -> bool:
    current = root
    for component in Path(relative).parts:
        current = current / component
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                return True
        except OSError:
            return False
    return False


def _read_source_bytes(root: Path, relative: str, before: os.stat_result) -> bytes:
    components = Path(relative).parts
    if not components:
        raise SnapshotError("snapshot source path is empty")
    directory_flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    directory_fds: list[int] = []
    file_fd: int | None = None
    try:
        directory_fds.append(os.open(root, directory_flags))
        for component in components[:-1]:
            directory_fds.append(
                os.open(
                    component,
                    directory_flags,
                    dir_fd=directory_fds[-1],
                )
            )
        file_fd = os.open(
            components[-1],
            file_flags,
            dir_fd=directory_fds[-1],
        )
        try:
            opened = os.fstat(file_fd)
            if (
                opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
                or opened.st_mode != before.st_mode
                or opened.st_size != before.st_size
            ):
                raise SnapshotError(f"workspace file changed while copying: {relative}")
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = os.read(file_fd, 65_536)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_SNAPSHOT_FILE_BYTES:
                    raise SnapshotError(
                        f"workspace file grew too large while copying: {relative}"
                    )
            after = os.fstat(file_fd)
            if (
                after.st_size != opened.st_size
                or after.st_mtime_ns != opened.st_mtime_ns
            ):
                raise SnapshotError(f"workspace file changed while copying: {relative}")
            return b"".join(chunks)
        finally:
            os.close(file_fd)
            file_fd = None
    except OSError as exc:
        raise SnapshotError(f"could not read workspace file: {relative}") from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)


def _make_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_dir() and not path.is_symlink():
            path.chmod(0o555)
        elif path.is_file() and not path.is_symlink():
            path.chmod(0o444)
    root.chmod(0o555)


def _remove_owned_tree(root: Path) -> None:
    allowed_prefixes = ("agent-team-provider-", "agent-team-snapshot-")
    temporary_parent = Path(tempfile.gettempdir()).resolve()
    if root.name.endswith("-ready"):
        identity_name = root.name.removesuffix("-ready")
    else:
        identity_name = root.name
    if root.parent.resolve(
        strict=False
    ) != temporary_parent or not identity_name.startswith(allowed_prefixes):
        raise SnapshotError(f"snapshot cleanup target is not launcher-owned: {root}")
    try:
        root_stat = root.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise SnapshotError(f"snapshot cleanup could not inspect {root}") from exc
    if (
        not stat.S_ISDIR(root_stat.st_mode)
        or root.is_symlink()
        or root_stat.st_uid != os.getuid()
    ):
        raise SnapshotError(
            f"snapshot cleanup target is not an owned directory: {root}"
        )
    directory_flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    parent_fd: int | None = None
    root_fd: int | None = None
    try:
        parent_fd = os.open(temporary_parent, directory_flags)
        root_fd = os.open(root.name, directory_flags, dir_fd=parent_fd)
        opened = os.fstat(root_fd)
        if (
            opened.st_dev != root_stat.st_dev
            or opened.st_ino != root_stat.st_ino
            or opened.st_uid != os.getuid()
        ):
            raise SnapshotError(f"snapshot cleanup target changed: {root}")
        os.fchmod(root_fd, 0o700)
        _remove_owned_tree_fd(root_fd)
        os.rmdir(root.name, dir_fd=parent_fd)
    except OSError as exc:
        raise SnapshotError(f"snapshot cleanup target is unavailable: {root}") from exc
    finally:
        if root_fd is not None:
            os.close(root_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def _remove_owned_tree_fd(directory_fd: int) -> None:
    directory_flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    with os.scandir(os.dup(directory_fd)) as entries:
        entry_list = list(entries)
    for entry in entry_list:
        entry_stat = entry.stat(follow_symlinks=False)
        if entry_stat.st_uid != os.getuid():
            raise SnapshotError(
                f"snapshot cleanup entry has unexpected owner: {entry.name}"
            )
        if stat.S_ISLNK(entry_stat.st_mode):
            os.unlink(entry.name, dir_fd=directory_fd)
            continue
        if stat.S_ISDIR(entry_stat.st_mode):
            child_fd = os.open(entry.name, directory_flags, dir_fd=directory_fd)
            try:
                opened = os.fstat(child_fd)
                if (
                    opened.st_dev != entry_stat.st_dev
                    or opened.st_ino != entry_stat.st_ino
                    or opened.st_uid != os.getuid()
                ):
                    raise SnapshotError(
                        f"snapshot cleanup directory changed: {entry.name}"
                    )
                os.fchmod(child_fd, 0o700)
                _remove_owned_tree_fd(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(entry.name, dir_fd=directory_fd)
            continue
        if not stat.S_ISREG(entry_stat.st_mode):
            raise SnapshotError(f"snapshot cleanup refuses special file: {entry.name}")
        file_fd = os.open(entry.name, file_flags, dir_fd=directory_fd)
        try:
            opened = os.fstat(file_fd)
            if (
                opened.st_dev != entry_stat.st_dev
                or opened.st_ino != entry_stat.st_ino
                or opened.st_uid != os.getuid()
            ):
                raise SnapshotError(f"snapshot cleanup file changed: {entry.name}")
            os.fchmod(file_fd, 0o600)
        finally:
            os.close(file_fd)
        os.unlink(entry.name, dir_fd=directory_fd)


def remove_owned_tree(root: Path) -> None:
    """Remove one launcher-owned temporary directory after identity checks."""

    _remove_owned_tree(root)
