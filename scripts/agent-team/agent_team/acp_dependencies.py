"""Resolve the selected ACP programs without invoking a package manager."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class AcpDependencyError(RuntimeError):
    pass


def _digest(path: Path) -> str:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK):
            raise AcpDependencyError(f"ACP program is not executable: {path}")
        if info.st_uid not in {0, os.getuid()} or info.st_mode & 0o022:
            raise AcpDependencyError(
                f"ACP program has an unsafe owner or is writable: {path}"
            )
        with path.open("rb") as source:
            return hashlib.file_digest(source, "sha256").hexdigest()
    except (OSError, ValueError) as exc:
        raise AcpDependencyError(f"ACP program is unavailable: {str(path)!r}") from exc


def _package(entry: Path, name: str, version: str, command: str) -> None:
    for root in entry.parents:
        manifest = root / "package.json"
        if not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AcpDependencyError(f"invalid package manifest: {manifest}") from exc
        if (
            not isinstance(data, dict)
            or data.get("name") != name
            or data.get("version") != version
        ):
            raise AcpDependencyError(f"selected ACP program requires {name}@{version}")
        bins = data.get("bin")
        relative = bins.get(command) if isinstance(bins, dict) else bins
        if not isinstance(relative, str) or not relative:
            raise AcpDependencyError(f"{name}@{version} has no {command} entrypoint")
        try:
            expected = (root / relative).resolve(strict=True)
        except (OSError, ValueError) as exc:
            raise AcpDependencyError(
                f"{name}@{version} entrypoint is unavailable"
            ) from exc
        if expected != entry or not entry.is_relative_to(root):
            raise AcpDependencyError(
                f"selected {command} does not match its package entrypoint"
            )
        return
    raise AcpDependencyError(f"cannot identify {name}@{version} for {entry}")


@dataclass(frozen=True)
class AcpExecutables:
    node: Path
    client: Path
    agent: Path
    node_sha256: str
    client_sha256: str
    agent_sha256: str

    @classmethod
    def resolve(cls, *, path: str | None = None) -> AcpExecutables:
        commands = ("node", "acpx", "claude-agent-acp")
        resolved = {name: shutil.which(name, path=path) for name in commands}
        missing = [name for name in commands if resolved[name] is None]
        if missing:
            raise AcpDependencyError(
                "selected Claude ACP profile requires installed commands: "
                + ", ".join(missing)
                + "; install acpx@0.13.2 and "
                "@agentclientprotocol/claude-agent-acp@0.70.0 and add their bins to PATH"
            )
        paths: list[Path] = []
        for name in commands:
            value = resolved[name]
            assert value is not None
            paths.append(Path(value).resolve(strict=True))
        node, client, agent = paths
        selected = cls(node, client, agent, *(_digest(item) for item in paths))
        selected.verify()
        return selected

    def verify(self) -> None:
        for path, expected in (
            (self.node, self.node_sha256),
            (self.client, self.client_sha256),
            (self.agent, self.agent_sha256),
        ):
            if not path.is_absolute() or _digest(path) != expected:
                raise AcpDependencyError(f"selected ACP executable changed: {path}")
        _package(self.client, "acpx", "0.13.2", "acpx")
        _package(
            self.agent,
            "@agentclientprotocol/claude-agent-acp",
            "0.70.0",
            "claude-agent-acp",
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "node": str(self.node),
            "client": str(self.client),
            "agent": str(self.agent),
            "node_sha256": self.node_sha256,
            "client_sha256": self.client_sha256,
            "agent_sha256": self.agent_sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> AcpExecutables:
        if not isinstance(value, Mapping):
            raise AcpDependencyError(
                "running team has no resolved ACP executables; restart it after setup"
            )
        paths: list[Path] = []
        digests: list[str] = []
        for key in ("node", "client", "agent"):
            raw = value.get(key)
            digest = value.get(f"{key}_sha256")
            if not isinstance(raw, str) or not Path(raw).is_absolute():
                raise AcpDependencyError(f"invalid saved ACP path: {key}")
            if (
                not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            ):
                raise AcpDependencyError(f"invalid saved ACP fingerprint: {key}")
            paths.append(Path(raw))
            digests.append(digest)
        return cls(paths[0], paths[1], paths[2], digests[0], digests[1], digests[2])
