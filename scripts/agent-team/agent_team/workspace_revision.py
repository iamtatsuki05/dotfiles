"""Bounded, content-based revisions for Git workspaces."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from .adapters import (
    MAX_SNAPSHOT_FILE_BYTES,
    MAX_SNAPSHOT_FILES,
    MAX_SNAPSHOT_TOTAL_BYTES,
)

_GIT_TIMEOUT_SECONDS = 5.0
_READ_CHUNK_BYTES = 65_536
_REVISION_FORMAT = b"agent-team-workspace-revision-v1\0"


class WorkspaceRevisionError(RuntimeError):
    """The workspace could not be safely fingerprinted."""


@dataclass(frozen=True, slots=True)
class WorkspaceEntry:
    path: str
    kind: str
    executable: bool
    content_sha256: str | None
    executable_bits: int = 0


@dataclass(frozen=True, slots=True)
class WorkspaceManifest:
    entries: tuple[WorkspaceEntry, ...]
    head: str
    index_sha256: str

    @property
    def revision(self) -> str:
        return _manifest_revision(self)

    def entry(self, path: str) -> WorkspaceEntry:
        for entry in self.entries:
            if entry.path == path:
                return entry
        raise KeyError(path)


def snapshot_manifest(workspace: Path) -> WorkspaceManifest:
    """Read a bounded, race-checked manifest from a Git worktree."""

    root = _workspace_root(workspace)
    _require_git_worktree(root)
    head = _git_head(root)
    head_paths = (
        set()
        if head.startswith("unborn:")
        else _parse_git_paths(
            _git_output(root, "ls-tree", "-r", "--name-only", "-z", "HEAD"),
            staged=False,
        )
    )
    index_output = _git_output(root, "ls-files", "-c", "--stage", "-z")
    tracked_paths = _parse_git_paths(index_output, staged=True)
    tracked_paths.update(head_paths)
    listed_output = _git_output(root, "ls-files", "-co", "--exclude-standard", "-z")
    paths = _parse_git_paths(listed_output, staged=False)
    paths.update(head_paths)
    paths.update(tracked_paths)
    if len(paths) > MAX_SNAPSHOT_FILES:
        raise WorkspaceRevisionError("workspace file limit exceeded")

    entries: list[WorkspaceEntry] = []
    total_bytes = 0
    for relative in sorted(set(paths)):
        components = _safe_components(relative)
        try:
            entry_stat = _lstat_components(root, components)
        except FileNotFoundError:
            if relative not in tracked_paths:
                raise WorkspaceRevisionError(
                    f"untracked workspace path disappeared: {relative}"
                ) from None
            entries.append(WorkspaceEntry(relative, "deleted", False, None))
            continue
        if stat.S_ISLNK(entry_stat.st_mode):
            raise WorkspaceRevisionError(
                f"workspace symlink is not supported: {relative}"
            )
        if not stat.S_ISREG(entry_stat.st_mode):
            raise WorkspaceRevisionError(
                f"workspace special file is not supported: {relative}"
            )
        if entry_stat.st_size > MAX_SNAPSHOT_FILE_BYTES:
            raise WorkspaceRevisionError(f"workspace file is too large: {relative}")
        content_sha256, size = _read_regular_file(root, components, entry_stat)
        total_bytes += size
        if total_bytes > MAX_SNAPSHOT_TOTAL_BYTES:
            raise WorkspaceRevisionError("workspace total size limit exceeded")
        executable_bits = entry_stat.st_mode & 0o111
        entries.append(
            WorkspaceEntry(
                relative,
                "regular",
                bool(executable_bits),
                content_sha256,
                executable_bits,
            )
        )
    return WorkspaceManifest(
        tuple(entries),
        head,
        hashlib.sha256(index_output).hexdigest(),
    )


def snapshot_revision(workspace: Path) -> str:
    """Return the SHA-256 revision of the current Git worktree content."""

    return snapshot_manifest(workspace).revision


def changed_paths(
    before: WorkspaceManifest, after: WorkspaceManifest
) -> tuple[str, ...]:
    """Return paths whose manifest entries differ between two snapshots."""

    before_by_path = {entry.path: entry for entry in before.entries}
    after_by_path = {entry.path: entry for entry in after.entries}
    return tuple(
        path
        for path in sorted(before_by_path.keys() | after_by_path.keys())
        if before_by_path.get(path) != after_by_path.get(path)
    )


def _workspace_root(workspace: Path) -> Path:
    try:
        workspace_stat = workspace.lstat()
    except (OSError, TypeError) as exc:
        raise WorkspaceRevisionError("workspace is unavailable") from exc
    if stat.S_ISLNK(workspace_stat.st_mode):
        raise WorkspaceRevisionError("workspace must not be a symlink")
    try:
        root = workspace.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceRevisionError("workspace is unavailable") from exc
    if not root.is_dir():
        raise WorkspaceRevisionError("workspace must be a directory")
    return root


def _require_git_worktree(root: Path) -> None:
    result = _git_run(root, "rev-parse", "--is-inside-work-tree")
    if result.returncode != 0 or result.stdout.strip() != b"true":
        raise WorkspaceRevisionError("workspace must be a Git worktree")


def _git_head(root: Path) -> str:
    result = _git_run(root, "rev-parse", "--verify", "HEAD")
    if result.returncode == 0:
        try:
            head = result.stdout.decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise WorkspaceRevisionError("Git HEAD is not valid ASCII") from exc
        if head:
            return head
        raise WorkspaceRevisionError("Git HEAD is empty")

    symbolic = _git_run(root, "symbolic-ref", "--quiet", "HEAD")
    if symbolic.returncode != 0:
        raise WorkspaceRevisionError("could not resolve Git HEAD")
    try:
        branch = symbolic.stdout.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise WorkspaceRevisionError("Git HEAD name is not valid UTF-8") from exc
    if not branch:
        raise WorkspaceRevisionError("Git HEAD name is empty")
    return f"unborn:{branch}"


def _git_output(root: Path, *args: str) -> bytes:
    result = _git_run(root, *args)
    if result.returncode != 0:
        raise WorkspaceRevisionError("Git workspace enumeration failed")
    return result.stdout


def _git_run(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    argv = ("git", "-C", str(root), *args)
    try:
        result = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            cwd=root,
            shell=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceRevisionError("Git command timed out") from exc
    except OSError as exc:
        raise WorkspaceRevisionError("Git command could not start") from exc
    if len(result.stdout) > MAX_SNAPSHOT_TOTAL_BYTES:
        raise WorkspaceRevisionError("Git command output exceeds the configured limit")
    if len(result.stderr) > MAX_SNAPSHOT_TOTAL_BYTES:
        raise WorkspaceRevisionError("Git command error exceeds the configured limit")
    return result


def _parse_git_paths(raw: bytes, *, staged: bool) -> set[str]:
    paths: set[str] = set()
    records = raw.split(b"\0")
    for record in records:
        if not record:
            continue
        path_bytes = record.split(b"\t", 1)[1] if staged else record
        try:
            path = path_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceRevisionError("Git filename is not valid UTF-8") from exc
        _safe_components(path)
        paths.add(path)
    return paths


def _safe_components(relative: str) -> tuple[str, ...]:
    if not relative or "\0" in relative:
        raise WorkspaceRevisionError("Git returned an invalid workspace path")
    posix = PurePosixPath(relative)
    windows = PureWindowsPath(relative)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise WorkspaceRevisionError(f"workspace path is absolute: {relative}")
    components = posix.parts
    if not components or any(component in {"", ".", ".."} for component in components):
        raise WorkspaceRevisionError(f"workspace path escapes root: {relative}")
    return components


def _lstat_components(root: Path, components: tuple[str, ...]) -> os.stat_result:
    current = root
    for index, component in enumerate(components):
        current = current / component
        entry_stat = current.lstat()
        if stat.S_ISLNK(entry_stat.st_mode):
            raise WorkspaceRevisionError(
                f"workspace symlink is not supported: {'/'.join(components)}"
            )
        if index < len(components) - 1 and not stat.S_ISDIR(entry_stat.st_mode):
            raise WorkspaceRevisionError(
                f"workspace parent is not a directory: {'/'.join(components)}"
            )
    return entry_stat


def _read_regular_file(
    root: Path,
    components: tuple[str, ...],
    before: os.stat_result,
) -> tuple[str, int]:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise WorkspaceRevisionError("workspace revision requires nofollow support")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow
    file_flags = os.O_RDONLY | nofollow
    directory_fds: list[int] = []
    file_fd: int | None = None
    relative = "/".join(components)
    try:
        directory_fds.append(os.open(root, directory_flags))
        for component in components[:-1]:
            directory_fds.append(
                os.open(component, directory_flags, dir_fd=directory_fds[-1])
            )
        file_fd = os.open(components[-1], file_flags, dir_fd=directory_fds[-1])
        opened = os.fstat(file_fd)
        _require_same_stat(before, opened, relative)
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = _read_chunk(file_fd, _READ_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            if size > MAX_SNAPSHOT_FILE_BYTES:
                raise WorkspaceRevisionError(
                    f"workspace file grew too large: {relative}"
                )
        after = os.fstat(file_fd)
        _require_same_stat(before, after, relative)
        after_path = _lstat_components(root, components)
        _require_same_stat(before, after_path, relative)
        return digest.hexdigest(), size
    except WorkspaceRevisionError:
        raise
    except FileNotFoundError as exc:
        raise WorkspaceRevisionError(
            f"workspace path changed while reading: {relative}"
        ) from exc
    except OSError as exc:
        raise WorkspaceRevisionError(
            f"workspace path could not be read: {relative}"
        ) from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)


def _require_same_stat(
    before: os.stat_result, after: os.stat_result, relative: str
) -> None:
    fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in fields):
        raise WorkspaceRevisionError(
            f"workspace file changed while reading: {relative}"
        )


def _read_chunk(file_fd: int, count: int) -> bytes:
    return os.read(file_fd, count)


def _manifest_revision(manifest: WorkspaceManifest) -> str:
    digest = hashlib.sha256()
    digest.update(_REVISION_FORMAT)
    _hash_field(digest, manifest.head.encode("utf-8"))
    _hash_field(digest, manifest.index_sha256.encode("ascii"))
    for entry in manifest.entries:
        _hash_field(digest, entry.path.encode("utf-8"))
        _hash_field(digest, entry.kind.encode("ascii"))
        _hash_field(digest, entry.executable_bits.to_bytes(2, "big"))
        _hash_field(
            digest,
            b"<deleted>"
            if entry.content_sha256 is None
            else entry.content_sha256.encode("ascii"),
        )
    return digest.hexdigest()


def _hash_field(digest: hashlib._Hash, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)
