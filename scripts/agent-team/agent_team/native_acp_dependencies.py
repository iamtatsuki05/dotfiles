"""Resolve the pinned dependencies used by native ACP clients.

This boundary intentionally knows only about the native Claude and Codex ACP
profiles.  It does not resolve ``acpx`` or any other harness: the Orca resolver
owns those dependencies separately.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class NativeAcpDependencyError(ValueError):
    """Raised when pinned native ACP dependencies are unavailable."""


@dataclass(frozen=True)
class _ManifestRecord:
    path: Path
    value: dict[str, object]
    sha256: str


_AGENT_PACKAGE = "@agentclientprotocol/claude-agent-acp"
_AGENT_VERSION = "0.70.0"
_AGENT_COMMAND = "claude-agent-acp"
_AGENT_ENTRY = "dist/index.js"
_AGENT_LIBRARY = "dist/lib.js"
_SDK_PACKAGE = "@agentclientprotocol/sdk"
_SDK_VERSION = "1.3.0"
_SDK_ENTRY = "dist/acp.js"
_COMMANDS = ("node", _AGENT_COMMAND)
_CODEX_AGENT_PACKAGE = "@agentclientprotocol/codex-acp"
_CODEX_AGENT_VERSION = "1.10.0"
_CODEX_AGENT_COMMAND = "codex-acp"
_CODEX_AGENT_ENTRY = "dist/index.js"
_CODEX_SDK_VERSION = "1.4.0"
_CODEX_PACKAGE = "@openai/codex"
_CODEX_VERSION = "0.153.4"
_CODEX_COMMANDS = ("node", _CODEX_AGENT_COMMAND, "codex")
_SERIALIZED_FIELDS = frozenset(
    {
        "node",
        "agent",
        "sdk",
        "library",
        "node_sha256",
        "agent_sha256",
        "sdk_sha256",
        "library_sha256",
    }
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_FILE_IDENTITY_FIELDS = (
    "st_dev",
    "st_ino",
    "st_mode",
    "st_uid",
    "st_gid",
    "st_size",
    "st_mtime_ns",
    "st_ctime_ns",
)
_CODEX_SERIALIZED_FIELDS = frozenset(
    {
        "node",
        "agent",
        "sdk",
        "codex",
        "node_sha256",
        "agent_sha256",
        "sdk_sha256",
        "codex_sha256",
        "agent_manifest_sha256",
        "sdk_manifest_sha256",
    }
)


def _profile_error(missing: tuple[str, ...]) -> NativeAcpDependencyError:
    names = ", ".join(missing)
    message = (
        "native Claude ACP profile requires installed commands: "
        f"{names}; install @agentclientprotocol/claude-agent-acp@0.70.0 "
        "and add its Node and Claude ACP bins to PATH"
    )
    return NativeAcpDependencyError(message)


def _codex_profile_error(missing: tuple[str, ...]) -> NativeAcpDependencyError:
    names = ", ".join(missing)
    message = (
        "native Codex ACP profile requires installed commands: "
        f"{names}; install Node.js >=22, @agentclientprotocol/codex-acp@"
        f"{_CODEX_AGENT_VERSION} (with {_SDK_PACKAGE}@{_CODEX_SDK_VERSION} and "
        f"{_CODEX_PACKAGE}@^0.153.3) and {_CODEX_PACKAGE}@{_CODEX_VERSION}, "
        "then add node, codex-acp, and codex to PATH"
    )
    return NativeAcpDependencyError(message)


def _same_file_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return all(
        getattr(first, field) == getattr(second, field)
        for field in _FILE_IDENTITY_FIELDS
    )


def _read_manifest_record(root: Path, package: str, version: str) -> _ManifestRecord:
    label = f"{package} package manifest"
    manifest = root / "package.json"
    before = _check_file(manifest, executable=False, label=label)
    manifest = _canonical_path(manifest, label)
    fd: int | None = None
    try:
        fd = os.open(manifest, os.O_RDONLY | os.O_NOFOLLOW)
        opened = os.fstat(fd)
        if not _same_file_identity(before, opened):
            raise NativeAcpDependencyError(
                f"selected {package}@{version} package manifest changed while opening"
            )
        with os.fdopen(fd, "rb", closefd=False) as source:
            payload = source.read()
        for after in (os.fstat(fd), manifest.lstat()):
            if not _same_file_identity(opened, after):
                raise NativeAcpDependencyError(
                    f"selected {package}@{version} package manifest changed while reading"
                )
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise NativeAcpDependencyError(
                f"selected {package}@{version} package manifest is unavailable"
            ) from exc
    except NativeAcpDependencyError:
        raise
    except (OSError, ValueError) as exc:
        raise NativeAcpDependencyError(
            f"selected {package}@{version} package manifest is unavailable"
        ) from exc
    finally:
        if fd is not None:
            os.close(fd)
    if not isinstance(value, dict):
        raise NativeAcpDependencyError(
            f"selected {package}@{version} package manifest is invalid"
        )
    result: dict[str, object] = value
    if result.get("name") != package or result.get("version") != version:
        raise NativeAcpDependencyError(f"selected {package}@{version} is required")
    return _ManifestRecord(manifest, result, hashlib.sha256(payload).hexdigest())


def _read_manifest(root: Path, package: str, version: str) -> dict[str, object]:
    return _read_manifest_record(root, package, version).value


def _canonical_path(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise NativeAcpDependencyError(f"saved native ACP {label} path is invalid")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise NativeAcpDependencyError(
            f"selected native ACP {label} is unavailable"
        ) from exc
    if resolved != path:
        raise NativeAcpDependencyError(
            f"selected native ACP {label} path must be canonical"
        )
    return resolved


def _check_file(path: Path, *, executable: bool, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except (OSError, ValueError) as exc:
        raise NativeAcpDependencyError(
            f"selected native ACP {label} is unavailable"
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise NativeAcpDependencyError(
            f"selected native ACP {label} must be a regular file"
        )
    if info.st_uid not in {0, os.getuid()}:
        raise NativeAcpDependencyError(
            f"selected native ACP {label} has an unsafe owner"
        )
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise NativeAcpDependencyError(
            f"selected native ACP {label} is group/other writable"
        )
    if not os.access(path, os.R_OK):
        raise NativeAcpDependencyError(f"selected native ACP {label} is not readable")
    if executable and not os.access(path, os.X_OK):
        raise NativeAcpDependencyError(f"selected native ACP {label} is not executable")
    return info


def _digest(path: Path, *, executable: bool, label: str) -> str:
    before = _check_file(path, executable=executable, label=label)
    fd: int | None = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        opened = os.fstat(fd)
        if not _same_file_identity(before, opened):
            raise NativeAcpDependencyError(
                f"selected native ACP {label} changed while opening"
            )
        with os.fdopen(fd, "rb", closefd=False) as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        for after in (os.fstat(fd), path.lstat()):
            if not _same_file_identity(opened, after):
                raise NativeAcpDependencyError(
                    f"selected native ACP {label} changed while reading"
                )
        return digest
    except NativeAcpDependencyError:
        raise
    except (OSError, ValueError) as exc:
        raise NativeAcpDependencyError(
            f"selected native ACP {label} cannot be fingerprinted"
        ) from exc
    finally:
        if fd is not None:
            os.close(fd)


def _package_root(
    entry: Path,
    *,
    package: str = _AGENT_PACKAGE,
    version: str = _AGENT_VERSION,
    canonical_entry: str = _AGENT_ENTRY,
) -> Path:
    root = entry.parent.parent
    if entry != root / canonical_entry:
        raise NativeAcpDependencyError(
            f"selected {package}@{version} must use {canonical_entry}"
        )
    return root


def _agent_manifest(entry: Path) -> tuple[Path, dict[str, object]]:
    root = _package_root(entry)
    manifest = _read_manifest(root, _AGENT_PACKAGE, _AGENT_VERSION)
    binaries = manifest.get("bin")
    if (
        not isinstance(binaries, Mapping)
        or binaries.get(_AGENT_COMMAND) != _AGENT_ENTRY
    ):
        raise NativeAcpDependencyError(
            f"selected {_AGENT_PACKAGE}@{_AGENT_VERSION} has no canonical entrypoint"
        )
    dependencies = manifest.get("dependencies")
    if (
        not isinstance(dependencies, Mapping)
        or dependencies.get(_SDK_PACKAGE) != _SDK_VERSION
    ):
        raise NativeAcpDependencyError(
            f"selected {_AGENT_PACKAGE}@{_AGENT_VERSION} requires {_SDK_PACKAGE}@{_SDK_VERSION}"
        )
    return root, manifest


def _agent_library(entry: Path) -> Path:
    root, _ = _agent_manifest(entry)
    library = entry.parent / "lib.js"
    if library != root / _AGENT_LIBRARY or not library.is_relative_to(root):
        raise NativeAcpDependencyError(
            f"selected {_AGENT_PACKAGE}@{_AGENT_VERSION} library is outside its package"
        )
    _check_file(library, executable=False, label="agent library")
    _canonical_path(library, "agent library")
    return library


def _codex_manifest(entry: Path) -> tuple[Path, _ManifestRecord]:
    root = _package_root(
        entry,
        package=_CODEX_AGENT_PACKAGE,
        version=_CODEX_AGENT_VERSION,
        canonical_entry=_CODEX_AGENT_ENTRY,
    )
    record = _read_manifest_record(root, _CODEX_AGENT_PACKAGE, _CODEX_AGENT_VERSION)
    manifest = record.value
    if manifest.get("main") != _CODEX_AGENT_ENTRY:
        raise NativeAcpDependencyError(
            f"selected {_CODEX_AGENT_PACKAGE}@{_CODEX_AGENT_VERSION} must use "
            f"{_CODEX_AGENT_ENTRY} as main"
        )
    binaries = manifest.get("bin")
    if (
        not isinstance(binaries, Mapping)
        or binaries.get(_CODEX_AGENT_COMMAND) != _CODEX_AGENT_ENTRY
    ):
        raise NativeAcpDependencyError(
            f"selected {_CODEX_AGENT_PACKAGE}@{_CODEX_AGENT_VERSION} has no "
            "canonical entrypoint"
        )
    dependencies = manifest.get("dependencies")
    if (
        not isinstance(dependencies, Mapping)
        or dependencies.get(_SDK_PACKAGE) != f"^{_CODEX_SDK_VERSION}"
        or dependencies.get(_CODEX_PACKAGE) != "^0.153.3"
    ):
        raise NativeAcpDependencyError(
            f"selected {_CODEX_AGENT_PACKAGE}@{_CODEX_AGENT_VERSION} requires "
            f"{_SDK_PACKAGE}@^{_CODEX_SDK_VERSION} and {_CODEX_PACKAGE}@^0.153.3"
        )
    return root, record


def _dependency_root(
    entry: Path,
    package: str,
    version: str,
    *,
    reject_symlink: bool = False,
    owner_package: str = _AGENT_PACKAGE,
    owner_version: str = _AGENT_VERSION,
) -> Path:
    """Perform Node's upward node_modules lookup without running Node."""

    for ancestor in (entry.parent, *entry.parents):
        candidate = ancestor / "node_modules" / package
        try:
            candidate_info = candidate.lstat()
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ENOTDIR}:
                continue
            raise NativeAcpDependencyError(
                f"selected {package}@{version} dependency is unavailable"
            ) from exc
        except ValueError as exc:
            raise NativeAcpDependencyError(
                f"selected {package}@{version} dependency is unavailable"
            ) from exc
        if stat.S_ISLNK(candidate_info.st_mode):
            if reject_symlink:
                raise NativeAcpDependencyError(
                    f"selected {package}@{version} dependency path is a symlink"
                )
            try:
                return candidate.resolve(strict=True)
            except (OSError, RuntimeError, ValueError) as exc:
                raise NativeAcpDependencyError(
                    f"selected {package}@{version} is unavailable"
                ) from exc
        if stat.S_ISDIR(candidate_info.st_mode):
            return candidate
        raise NativeAcpDependencyError(f"selected {package}@{version} is unavailable")
    raise NativeAcpDependencyError(
        f"selected {owner_package}@{owner_version} requires {package}@{version}"
    )


def _export_target(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if not isinstance(value, Mapping):
        return None
    if "." in value:
        return _export_target(value["."])
    for condition in ("import", "default"):
        if condition in value:
            target = _export_target(value[condition])
            if target is not None:
                return target
    return None


def _sdk_entry_and_manifest(
    entry: Path,
    *,
    sdk_version: str = _SDK_VERSION,
    reject_dependency_symlink: bool = False,
    owner_package: str = _AGENT_PACKAGE,
    owner_version: str = _AGENT_VERSION,
) -> tuple[Path, _ManifestRecord]:
    sdk_root = _dependency_root(
        entry,
        _SDK_PACKAGE,
        sdk_version,
        reject_symlink=reject_dependency_symlink,
        owner_package=owner_package,
        owner_version=owner_version,
    )
    record = _read_manifest_record(sdk_root, _SDK_PACKAGE, sdk_version)
    manifest = record.value
    if manifest.get("main") != _SDK_ENTRY:
        raise NativeAcpDependencyError(
            f"selected {_SDK_PACKAGE}@{sdk_version} has no canonical main entrypoint"
        )
    exports = manifest.get("exports")
    target = _export_target(exports)
    if target != f"./{_SDK_ENTRY}":
        raise NativeAcpDependencyError(
            f"selected {_SDK_PACKAGE}@{sdk_version} has no canonical export"
        )
    try:
        resolved = (sdk_root / target).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise NativeAcpDependencyError(
            f"selected {_SDK_PACKAGE}@{sdk_version} entrypoint is unavailable"
        ) from exc
    expected = sdk_root / _SDK_ENTRY
    if resolved != expected or not resolved.is_relative_to(sdk_root):
        raise NativeAcpDependencyError(
            f"selected {_SDK_PACKAGE}@{sdk_version} entrypoint is invalid"
        )
    _check_file(resolved, executable=False, label="SDK")
    return resolved, record


def _sdk_entry(
    entry: Path,
    *,
    sdk_version: str = _SDK_VERSION,
    reject_dependency_symlink: bool = False,
    owner_package: str = _AGENT_PACKAGE,
    owner_version: str = _AGENT_VERSION,
) -> Path:
    return _sdk_entry_and_manifest(
        entry,
        sdk_version=sdk_version,
        reject_dependency_symlink=reject_dependency_symlink,
        owner_package=owner_package,
        owner_version=owner_version,
    )[0]


@dataclass(frozen=True)
class NativeAcpExecutables:
    """The exact files selected for one native Claude ACP run."""

    node: Path
    agent: Path
    sdk: Path
    library: Path
    node_sha256: str
    agent_sha256: str
    sdk_sha256: str
    library_sha256: str

    @classmethod
    def resolve(cls, path: str | None = None) -> NativeAcpExecutables:
        resolved = {name: shutil.which(name, path=path) for name in _COMMANDS}
        missing = tuple(name for name in _COMMANDS if resolved[name] is None)
        if missing:
            raise _profile_error(missing)
        try:
            node_value = resolved["node"]
            agent_value = resolved[_AGENT_COMMAND]
            assert node_value is not None
            assert agent_value is not None
            node = Path(node_value).resolve(strict=True)
            agent = Path(agent_value).resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise NativeAcpDependencyError(
                "native Claude ACP profile selected dependency is unavailable"
            ) from exc
        _canonical_path(node, "Node")
        _canonical_path(agent, "agent")
        _agent_manifest(agent)
        sdk = _sdk_entry(agent)
        library = _agent_library(agent)
        selected = cls(
            node,
            agent,
            sdk,
            library,
            _digest(node, executable=True, label="Node"),
            _digest(agent, executable=True, label="agent"),
            _digest(sdk, executable=False, label="SDK"),
            _digest(library, executable=False, label="agent library"),
        )
        selected.verify()
        return selected

    def verify(self) -> None:
        node = _canonical_path(self.node, "Node")
        agent = _canonical_path(self.agent, "agent")
        sdk = _canonical_path(self.sdk, "SDK")
        library = _canonical_path(self.library, "agent library")
        if (
            node != self.node
            or agent != self.agent
            or sdk != self.sdk
            or library != self.library
        ):
            raise NativeAcpDependencyError(
                "saved native ACP dependency paths must be canonical"
            )

        if _digest(node, executable=True, label="Node") != self.node_sha256:
            raise NativeAcpDependencyError("selected native ACP Node changed")
        if _digest(agent, executable=True, label="agent") != self.agent_sha256:
            raise NativeAcpDependencyError("selected native ACP agent changed")
        if _digest(sdk, executable=False, label="SDK") != self.sdk_sha256:
            raise NativeAcpDependencyError("selected native ACP SDK changed")
        if (
            _digest(library, executable=False, label="agent library")
            != self.library_sha256
        ):
            raise NativeAcpDependencyError("selected native ACP library changed")

        _agent_manifest(agent)
        expected_sdk = _sdk_entry(agent)
        if expected_sdk != sdk:
            raise NativeAcpDependencyError(
                "selected native ACP SDK is not the agent dependency"
            )
        expected_library = _agent_library(agent)
        if expected_library != library:
            raise NativeAcpDependencyError(
                "selected native ACP library is not the agent sibling library"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "node": str(self.node),
            "agent": str(self.agent),
            "sdk": str(self.sdk),
            "library": str(self.library),
            "node_sha256": self.node_sha256,
            "agent_sha256": self.agent_sha256,
            "sdk_sha256": self.sdk_sha256,
            "library_sha256": self.library_sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> NativeAcpExecutables:
        if not isinstance(value, Mapping) or set(value) != _SERIALIZED_FIELDS:
            raise NativeAcpDependencyError(
                "saved native ACP dependencies have unexpected metadata"
            )
        paths: dict[str, Path] = {}
        fingerprints: dict[str, str] = {}
        for key in ("node", "agent", "sdk", "library"):
            raw_path = value.get(key)
            raw_digest = value.get(f"{key}_sha256")
            if not isinstance(raw_path, str):
                raise NativeAcpDependencyError(
                    f"saved native ACP {key} path is invalid"
                )
            try:
                path = Path(raw_path)
            except (OSError, TypeError, ValueError) as exc:
                raise NativeAcpDependencyError(
                    f"saved native ACP {key} path is invalid"
                ) from exc
            if not path.is_absolute():
                raise NativeAcpDependencyError(
                    f"saved native ACP {key} path is invalid"
                )
            if (
                not isinstance(raw_digest, str)
                or _SHA256_RE.fullmatch(raw_digest) is None
            ):
                raise NativeAcpDependencyError(
                    f"saved native ACP {key} fingerprint is invalid"
                )
            paths[key] = path
            fingerprints[key] = raw_digest
        return cls(
            paths["node"],
            paths["agent"],
            paths["sdk"],
            paths["library"],
            fingerprints["node"],
            fingerprints["agent"],
            fingerprints["sdk"],
            fingerprints["library"],
        )


@dataclass(frozen=True)
class CodexAcpExecutables:
    """The exact files selected for one native Codex ACP run."""

    node: Path
    agent: Path
    sdk: Path
    codex: Path
    node_sha256: str
    agent_sha256: str
    sdk_sha256: str
    codex_sha256: str
    agent_manifest_sha256: str
    sdk_manifest_sha256: str

    @classmethod
    def resolve(cls, path: str | None = None) -> CodexAcpExecutables:
        resolved = {name: shutil.which(name, path=path) for name in _CODEX_COMMANDS}
        missing = tuple(name for name in _CODEX_COMMANDS if resolved[name] is None)
        if missing:
            raise _codex_profile_error(missing)
        try:
            node_value = resolved["node"]
            agent_value = resolved[_CODEX_AGENT_COMMAND]
            codex_value = resolved["codex"]
            assert node_value is not None
            assert agent_value is not None
            assert codex_value is not None
            node = Path(node_value).resolve(strict=True)
            agent = Path(agent_value).resolve(strict=True)
            codex = Path(codex_value).resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise NativeAcpDependencyError(
                "native Codex ACP profile selected dependency is unavailable"
            ) from exc
        _canonical_path(node, "Node")
        _canonical_path(agent, "agent")
        _canonical_path(codex, "Codex")
        _, agent_manifest = _codex_manifest(agent)
        sdk, sdk_manifest = _sdk_entry_and_manifest(
            agent,
            sdk_version=_CODEX_SDK_VERSION,
            reject_dependency_symlink=True,
            owner_package=_CODEX_AGENT_PACKAGE,
            owner_version=_CODEX_AGENT_VERSION,
        )
        selected = cls(
            node,
            agent,
            sdk,
            codex,
            _digest(node, executable=True, label="Node"),
            _digest(agent, executable=True, label="agent"),
            _digest(sdk, executable=False, label="SDK"),
            _digest(codex, executable=True, label="Codex"),
            agent_manifest.sha256,
            sdk_manifest.sha256,
        )
        selected.verify()
        return selected

    def verify(self) -> None:
        node = _canonical_path(self.node, "Node")
        agent = _canonical_path(self.agent, "agent")
        sdk = _canonical_path(self.sdk, "SDK")
        codex = _canonical_path(self.codex, "Codex")
        if (
            node != self.node
            or agent != self.agent
            or sdk != self.sdk
            or codex != self.codex
        ):
            raise NativeAcpDependencyError(
                "saved native Codex ACP dependency paths must be canonical"
            )

        if _digest(node, executable=True, label="Node") != self.node_sha256:
            raise NativeAcpDependencyError("selected native Codex ACP Node changed")
        if _digest(agent, executable=True, label="agent") != self.agent_sha256:
            raise NativeAcpDependencyError("selected native Codex ACP agent changed")
        if _digest(sdk, executable=False, label="SDK") != self.sdk_sha256:
            raise NativeAcpDependencyError("selected native Codex ACP SDK changed")
        if _digest(codex, executable=True, label="Codex") != self.codex_sha256:
            raise NativeAcpDependencyError("selected native Codex ACP codex changed")

        _, agent_manifest = _codex_manifest(agent)
        agent_manifest_path = agent.parent.parent / "package.json"
        if agent_manifest.path != agent_manifest_path:
            raise NativeAcpDependencyError(
                "selected native Codex ACP agent manifest path is not canonical"
            )
        if agent_manifest.sha256 != self.agent_manifest_sha256:
            raise NativeAcpDependencyError(
                "selected native Codex ACP agent manifest changed"
            )

        expected_sdk, sdk_manifest = _sdk_entry_and_manifest(
            agent,
            sdk_version=_CODEX_SDK_VERSION,
            reject_dependency_symlink=True,
            owner_package=_CODEX_AGENT_PACKAGE,
            owner_version=_CODEX_AGENT_VERSION,
        )
        if expected_sdk != sdk:
            raise NativeAcpDependencyError(
                "selected native Codex ACP SDK is not the agent dependency"
            )
        sdk_manifest_path = sdk.parent.parent / "package.json"
        if sdk_manifest.path != sdk_manifest_path:
            raise NativeAcpDependencyError(
                "selected native Codex ACP SDK manifest path is not canonical"
            )
        if sdk_manifest.sha256 != self.sdk_manifest_sha256:
            raise NativeAcpDependencyError(
                "selected native Codex ACP SDK manifest changed"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "node": str(self.node),
            "agent": str(self.agent),
            "sdk": str(self.sdk),
            "codex": str(self.codex),
            "node_sha256": self.node_sha256,
            "agent_sha256": self.agent_sha256,
            "sdk_sha256": self.sdk_sha256,
            "codex_sha256": self.codex_sha256,
            "agent_manifest_sha256": self.agent_manifest_sha256,
            "sdk_manifest_sha256": self.sdk_manifest_sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> CodexAcpExecutables:
        if not isinstance(value, Mapping) or set(value) != _CODEX_SERIALIZED_FIELDS:
            raise NativeAcpDependencyError(
                "saved native Codex ACP dependencies have unexpected metadata"
            )
        paths: dict[str, Path] = {}
        fingerprints: dict[str, str] = {}
        for key in ("node", "agent", "sdk", "codex"):
            raw_path = value.get(key)
            raw_digest = value.get(f"{key}_sha256")
            if not isinstance(raw_path, str):
                raise NativeAcpDependencyError(
                    f"saved native Codex ACP {key} path is invalid"
                )
            try:
                path = Path(raw_path)
            except (OSError, TypeError, ValueError) as exc:
                raise NativeAcpDependencyError(
                    f"saved native Codex ACP {key} path is invalid"
                ) from exc
            if not path.is_absolute():
                raise NativeAcpDependencyError(
                    f"saved native Codex ACP {key} path is invalid"
                )
            if (
                not isinstance(raw_digest, str)
                or _SHA256_RE.fullmatch(raw_digest) is None
            ):
                raise NativeAcpDependencyError(
                    f"saved native Codex ACP {key} fingerprint is invalid"
                )
            paths[key] = path
            fingerprints[key] = raw_digest
        for key in ("agent_manifest", "sdk_manifest"):
            raw_digest = value.get(f"{key}_sha256")
            if (
                not isinstance(raw_digest, str)
                or _SHA256_RE.fullmatch(raw_digest) is None
            ):
                raise NativeAcpDependencyError(
                    f"saved native Codex ACP {key} fingerprint is invalid"
                )
            fingerprints[key] = raw_digest
        return cls(
            paths["node"],
            paths["agent"],
            paths["sdk"],
            paths["codex"],
            fingerprints["node"],
            fingerprints["agent"],
            fingerprints["sdk"],
            fingerprints["codex"],
            fingerprints["agent_manifest"],
            fingerprints["sdk_manifest"],
        )


def adapter_snapshot(executables: NativeAcpExecutables) -> dict[str, object]:
    identity = executables.sdk.stat()
    return {
        "adapter_id": "claude-acp-0.70.0",
        "revision": "@agentclientprotocol/sdk@1.3.0",
        "executable": str(executables.sdk),
        "version": "@agentclientprotocol/claude-agent-acp@0.70.0",
        "identity": {
            "device": identity.st_dev,
            "inode": identity.st_ino,
            "size": identity.st_size,
            "mtime_ns": identity.st_mtime_ns,
            "sha256": executables.sdk_sha256,
        },
    }


def codex_adapter_snapshot(executables: CodexAcpExecutables) -> dict[str, object]:
    identity = executables.sdk.stat()
    return {
        "adapter_id": "codex-acp-1.10.0",
        "revision": "@agentclientprotocol/sdk@1.4.0",
        "executable": str(executables.sdk),
        "version": "@agentclientprotocol/codex-acp@1.10.0",
        "identity": {
            "device": identity.st_dev,
            "inode": identity.st_ino,
            "size": identity.st_size,
            "mtime_ns": identity.st_mtime_ns,
            "sha256": executables.sdk_sha256,
        },
    }
