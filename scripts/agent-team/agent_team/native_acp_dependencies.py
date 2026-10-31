"""Resolve the pinned dependencies used by native ACP clients.

This boundary intentionally knows only about the native Claude, Codex, and
internal Copilot ACP profiles.  It does not resolve ``acpx`` or any other
harness: the Orca resolver owns those dependencies separately.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import sys
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
_COPILOT_PACKAGE = "@github/copilot"
_COPILOT_VERSION = "1.0.91"
_COPILOT_COMMAND = "copilot"
_COPILOT_LOADER = "npm-loader.js"
_COPILOT_HOST = ("darwin", "arm64")
_COPILOT_PLATFORM_PACKAGE = "@github/copilot-darwin-arm64"
_COPILOT_PLATFORM_BINARY = "copilot"
_COPILOT_BINARY_SHA256 = (
    "87f04922933c139cf4af7cb6a80b96161428618a6275fea4b8dfe7e7a69c9518"
)
_COPILOT_SDK_VERSION = "1.4.0"
_COPILOT_COMMANDS = ("node", _COPILOT_COMMAND)
_COPILOT_SERIALIZED_FIELDS = frozenset(
    {
        "node",
        "loader",
        "copilot",
        "sdk",
        "node_sha256",
        "copilot_sha256",
        "sdk_sha256",
        "package_manifest_sha256",
        "platform_manifest_sha256",
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


def _copilot_profile_error(missing: tuple[str, ...]) -> NativeAcpDependencyError:
    names = ", ".join(missing)
    install = (
        f"{_COPILOT_PACKAGE}@{_COPILOT_VERSION} {_SDK_PACKAGE}@{_COPILOT_SDK_VERSION}"
    )
    return NativeAcpDependencyError(
        f"native Copilot ACP profile requires installed commands: {names}; "
        f"install {_COPILOT_PACKAGE}@{_COPILOT_VERSION} and "
        f"{_SDK_PACKAGE}@{_COPILOT_SDK_VERSION} into one npm prefix "
        f"(npm install --prefix DIR {install}), then put DIR/node_modules/.bin "
        "and Node.js 22 or newer first on PATH"
    )


def _copilot_loader_error(path: Path) -> NativeAcpDependencyError:
    return NativeAcpDependencyError(
        f"selected copilot is not the {_COPILOT_PACKAGE}@{_COPILOT_VERSION} "
        f"{_COPILOT_LOADER}: {path}; put DIR/node_modules/.bin first on PATH "
        "(another copilot, such as AWS Copilot, must not shadow it)"
    )


def _copilot_version_error() -> NativeAcpDependencyError:
    return NativeAcpDependencyError(
        f"selected {_COPILOT_PACKAGE}@{_COPILOT_VERSION} is required; install "
        f"exactly {_COPILOT_PACKAGE}@{_COPILOT_VERSION} into the selected npm prefix"
    )


def _copilot_platform_error() -> NativeAcpDependencyError:
    return NativeAcpDependencyError(
        f"selected {_COPILOT_PACKAGE}@{_COPILOT_VERSION} requires "
        f"{_COPILOT_PLATFORM_PACKAGE}@{_COPILOT_VERSION}"
    )


def _copilot_binary_error() -> NativeAcpDependencyError:
    return NativeAcpDependencyError(
        f"selected {_COPILOT_PLATFORM_PACKAGE}@{_COPILOT_VERSION} binary is not "
        f"the verified build; reinstall {_COPILOT_PACKAGE}@{_COPILOT_VERSION} "
        "into the selected npm prefix"
    )


def _copilot_sdk_error() -> NativeAcpDependencyError:
    return NativeAcpDependencyError(
        f"native Copilot ACP client requires {_SDK_PACKAGE}@{_COPILOT_SDK_VERSION} "
        f"in the same npm prefix as {_COPILOT_PACKAGE}@{_COPILOT_VERSION}"
    )


def _copilot_drift_error(label: str) -> NativeAcpDependencyError:
    return NativeAcpDependencyError(f"selected native Copilot ACP {label} changed")


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


def _copilot_host() -> tuple[str, str]:
    return sys.platform, platform.machine()


def _copilot_pinned_sha256() -> str:
    host = _copilot_host()
    if host != _COPILOT_HOST:
        raise NativeAcpDependencyError(
            "native Copilot ACP profile is verified only on "
            f"{'-'.join(_COPILOT_HOST)}; this host is {host[0]}-{host[1]}"
        )
    return _COPILOT_BINARY_SHA256


def _copilot_package(loader: Path) -> tuple[Path, _ManifestRecord]:
    """Return the npm ``node_modules`` root and manifest owning the loader."""

    package_root = loader.parent
    modules = package_root.parent.parent
    if (
        loader.name != _COPILOT_LOADER
        or package_root.name != "copilot"
        or package_root.parent.name != "@github"
        or modules.name != "node_modules"
    ):
        raise _copilot_loader_error(loader)
    _check_file(loader, executable=False, label="copilot loader")
    try:
        record = _read_manifest_record(package_root, _COPILOT_PACKAGE, _COPILOT_VERSION)
    except NativeAcpDependencyError as exc:
        raise _copilot_version_error() from exc
    binaries = record.value.get("bin")
    optional = record.value.get("optionalDependencies")
    if (
        not isinstance(binaries, Mapping)
        or binaries.get(_COPILOT_COMMAND) != _COPILOT_LOADER
        or not isinstance(optional, Mapping)
        or optional.get(_COPILOT_PLATFORM_PACKAGE) != _COPILOT_VERSION
    ):
        raise _copilot_version_error()
    return modules, record


def _copilot_platform(loader: Path, modules: Path) -> tuple[Path, _ManifestRecord]:
    try:
        root = _dependency_root(
            loader,
            _COPILOT_PLATFORM_PACKAGE,
            _COPILOT_VERSION,
            reject_symlink=True,
            owner_package=_COPILOT_PACKAGE,
            owner_version=_COPILOT_VERSION,
        )
    except NativeAcpDependencyError as exc:
        raise _copilot_platform_error() from exc
    if not root.is_relative_to(modules):
        raise _copilot_platform_error()
    try:
        record = _read_manifest_record(
            root, _COPILOT_PLATFORM_PACKAGE, _COPILOT_VERSION
        )
    except NativeAcpDependencyError as exc:
        raise _copilot_platform_error() from exc
    if _export_target(record.value.get("exports")) != f"./{_COPILOT_PLATFORM_BINARY}":
        raise _copilot_platform_error()
    binary = root / _COPILOT_PLATFORM_BINARY
    _check_file(binary, executable=True, label="copilot")
    return _canonical_path(binary, "copilot"), record


def _copilot_sdk(loader: Path, modules: Path) -> tuple[Path, _ManifestRecord]:
    try:
        sdk, record = _sdk_entry_and_manifest(
            loader,
            sdk_version=_COPILOT_SDK_VERSION,
            reject_dependency_symlink=True,
            owner_package=_COPILOT_PACKAGE,
            owner_version=_COPILOT_VERSION,
        )
    except NativeAcpDependencyError as exc:
        raise _copilot_sdk_error() from exc
    if not sdk.is_relative_to(modules):
        raise _copilot_sdk_error()
    return sdk, record


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


@dataclass(frozen=True)
class CopilotAcpExecutables:
    """The exact files selected for one internal Copilot ACP run.

    ``copilot`` is the pinned platform binary, which is itself the ACP server.
    The npm loader only identifies the selected package and is never executed.
    """

    node: Path
    loader: Path
    copilot: Path
    sdk: Path
    node_sha256: str
    copilot_sha256: str
    sdk_sha256: str
    package_manifest_sha256: str
    platform_manifest_sha256: str
    sdk_manifest_sha256: str

    @classmethod
    def resolve(cls, path: str | None = None) -> CopilotAcpExecutables:
        resolved = {name: shutil.which(name, path=path) for name in _COPILOT_COMMANDS}
        missing = tuple(name for name in _COPILOT_COMMANDS if resolved[name] is None)
        if missing:
            raise _copilot_profile_error(missing)
        try:
            node_value = resolved["node"]
            loader_value = resolved[_COPILOT_COMMAND]
            assert node_value is not None
            assert loader_value is not None
            node = Path(node_value).resolve(strict=True)
            loader = Path(loader_value).resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise NativeAcpDependencyError(
                "native Copilot ACP profile selected dependency is unavailable"
            ) from exc
        _canonical_path(node, "Node")
        modules, package_manifest = _copilot_package(loader)
        pinned = _copilot_pinned_sha256()
        copilot, platform_manifest = _copilot_platform(loader, modules)
        # The SDK lookup is cheap, so it precedes fingerprinting the large binary.
        sdk, sdk_manifest = _copilot_sdk(loader, modules)
        copilot_sha256 = _digest(copilot, executable=True, label="copilot")
        if copilot_sha256 != pinned:
            raise _copilot_binary_error()
        selected = cls(
            node,
            loader,
            copilot,
            sdk,
            _digest(node, executable=True, label="Node"),
            copilot_sha256,
            _digest(sdk, executable=False, label="SDK"),
            package_manifest.sha256,
            platform_manifest.sha256,
            sdk_manifest.sha256,
        )
        selected.verify()
        return selected

    def verify(self) -> None:
        node = _canonical_path(self.node, "Node")
        loader = _canonical_path(self.loader, "copilot loader")
        copilot = _canonical_path(self.copilot, "copilot")
        sdk = _canonical_path(self.sdk, "SDK")
        if (
            node != self.node
            or loader != self.loader
            or copilot != self.copilot
            or sdk != self.sdk
        ):
            raise NativeAcpDependencyError(
                "saved native Copilot ACP dependency paths must be canonical"
            )

        pinned = _copilot_pinned_sha256()
        if _digest(node, executable=True, label="Node") != self.node_sha256:
            raise _copilot_drift_error("Node")
        if self.copilot_sha256 != pinned:
            raise _copilot_binary_error()
        if _digest(copilot, executable=True, label="copilot") != self.copilot_sha256:
            raise _copilot_drift_error("copilot")
        if _digest(sdk, executable=False, label="SDK") != self.sdk_sha256:
            raise _copilot_drift_error("SDK")

        modules, package_manifest = _copilot_package(loader)
        if package_manifest.sha256 != self.package_manifest_sha256:
            raise _copilot_drift_error("package manifest")
        expected_copilot, platform_manifest = _copilot_platform(loader, modules)
        if expected_copilot != copilot:
            raise NativeAcpDependencyError(
                "selected native Copilot ACP binary is not the platform package binary"
            )
        if platform_manifest.sha256 != self.platform_manifest_sha256:
            raise _copilot_drift_error("platform manifest")
        expected_sdk, sdk_manifest = _copilot_sdk(loader, modules)
        if expected_sdk != sdk:
            raise NativeAcpDependencyError(
                "selected native Copilot ACP SDK is not the npm prefix dependency"
            )
        if sdk_manifest.sha256 != self.sdk_manifest_sha256:
            raise _copilot_drift_error("SDK manifest")

    def as_dict(self) -> dict[str, object]:
        return {
            "node": str(self.node),
            "loader": str(self.loader),
            "copilot": str(self.copilot),
            "sdk": str(self.sdk),
            "node_sha256": self.node_sha256,
            "copilot_sha256": self.copilot_sha256,
            "sdk_sha256": self.sdk_sha256,
            "package_manifest_sha256": self.package_manifest_sha256,
            "platform_manifest_sha256": self.platform_manifest_sha256,
            "sdk_manifest_sha256": self.sdk_manifest_sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> CopilotAcpExecutables:
        if not isinstance(value, Mapping) or set(value) != _COPILOT_SERIALIZED_FIELDS:
            raise NativeAcpDependencyError(
                "saved native Copilot ACP dependencies have unexpected metadata"
            )
        paths: dict[str, Path] = {}
        for key in ("node", "loader", "copilot", "sdk"):
            raw_path = value.get(key)
            if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
                raise NativeAcpDependencyError(
                    f"saved native Copilot ACP {key} path is invalid"
                )
            paths[key] = Path(raw_path)
        fingerprints: dict[str, str] = {}
        for key in (
            "node",
            "copilot",
            "sdk",
            "package_manifest",
            "platform_manifest",
            "sdk_manifest",
        ):
            raw_digest = value.get(f"{key}_sha256")
            if (
                not isinstance(raw_digest, str)
                or _SHA256_RE.fullmatch(raw_digest) is None
            ):
                raise NativeAcpDependencyError(
                    f"saved native Copilot ACP {key} fingerprint is invalid"
                )
            fingerprints[key] = raw_digest
        return cls(
            paths["node"],
            paths["loader"],
            paths["copilot"],
            paths["sdk"],
            fingerprints["node"],
            fingerprints["copilot"],
            fingerprints["sdk"],
            fingerprints["package_manifest"],
            fingerprints["platform_manifest"],
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


def copilot_adapter_snapshot(executables: CopilotAcpExecutables) -> dict[str, object]:
    identity = executables.copilot.stat()
    return {
        "adapter_id": f"copilot-acp-{_COPILOT_VERSION}",
        "revision": f"{_SDK_PACKAGE}@{_COPILOT_SDK_VERSION}",
        "executable": str(executables.copilot),
        "version": f"{_COPILOT_PACKAGE}@{_COPILOT_VERSION}",
        "identity": {
            "device": identity.st_dev,
            "inode": identity.st_ino,
            "size": identity.st_size,
            "mtime_ns": identity.st_mtime_ns,
            "sha256": executables.copilot_sha256,
        },
    }
