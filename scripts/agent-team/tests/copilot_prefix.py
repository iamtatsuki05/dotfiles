"""Fake npm prefixes for the internal Copilot ACP tests.

Nothing here runs Copilot or npm.  The fake platform binary is pinned for the
duration of ``pinned_fake_copilot`` so dependency resolution can be exercised
on any host without the real 1.0.91 build.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from agent_team import native_acp_dependencies

PLATFORMS = (
    "linux-x64",
    "linux-arm64",
    "linuxmusl-x64",
    "linuxmusl-arm64",
    "darwin-x64",
    "darwin-arm64",
    "win32-x64",
    "win32-arm64",
)


@dataclass(frozen=True)
class CopilotPrefix:
    path: str
    root: Path
    modules: Path
    node: Path
    loader: Path
    package_root: Path
    platform_root: Path
    binary: Path
    sdk_root: Path
    sdk: Path


def _write(path: Path, contents: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
    path.chmod(mode)


def write_json(path: Path, value: object) -> None:
    _write(path, json.dumps(value, indent=2) + "\n")


def package_manifest(**changes: object) -> dict[str, object]:
    manifest: dict[str, object] = {
        "name": "@github/copilot",
        "version": "1.0.91",
        "type": "module",
        "bin": {"copilot": "npm-loader.js"},
        "dependencies": {"detect-libc": "^2.1.2"},
        "optionalDependencies": {
            f"@github/copilot-{platform}": "1.0.91" for platform in PLATFORMS
        },
    }
    manifest.update(changes)
    return manifest


def platform_manifest(**changes: object) -> dict[str, object]:
    manifest: dict[str, object] = {
        "name": "@github/copilot-darwin-arm64",
        "version": "1.0.91",
        "os": ["darwin"],
        "cpu": ["arm64"],
        "bin": {"copilot-darwin-arm64": "copilot"},
        "exports": {".": "./copilot"},
    }
    manifest.update(changes)
    return manifest


def sdk_manifest(version: str = "1.4.0") -> dict[str, object]:
    return {
        "name": "@agentclientprotocol/sdk",
        "version": version,
        "main": "dist/acp.js",
        "exports": {
            ".": {"import": "./dist/acp.js", "default": "./dist/acp.js"},
        },
    }


def make_copilot_prefix(root: Path, *, hoisted: bool = True) -> CopilotPrefix:
    """Create Node.js and one npm prefix holding Copilot 1.0.91 and SDK 1.4.0.

    ``hoisted`` mirrors ``npm install --prefix``; otherwise the platform
    package is nested below ``@github/copilot`` as the mise layout does.
    """

    node = root / "node-bin" / "node"
    _write(node, "#!/bin/sh\nexit 0\n", 0o755)
    modules = root / "prefix" / "node_modules"
    package_root = modules / "@github" / "copilot"
    write_json(package_root / "package.json", package_manifest())
    loader = package_root / "npm-loader.js"
    _write(loader, "#!/usr/bin/env node\n// fake loader\n", 0o755)
    platform_parent = modules if hoisted else package_root / "node_modules"
    platform_root = platform_parent / "@github" / "copilot-darwin-arm64"
    write_json(platform_root / "package.json", platform_manifest())
    binary = platform_root / "copilot"
    _write(binary, "#!/bin/sh\n# fake copilot-darwin-arm64 1.0.91\nexit 0\n", 0o755)
    sdk_root = modules / "@agentclientprotocol" / "sdk"
    write_json(sdk_root / "package.json", sdk_manifest())
    sdk = sdk_root / "dist" / "acp.js"
    _write(sdk, "export const version = '1.4.0';\n")
    bin_dir = modules / ".bin"
    bin_dir.mkdir()
    (bin_dir / "copilot").symlink_to(
        Path("..") / "@github" / "copilot" / "npm-loader.js"
    )
    return CopilotPrefix(
        path=os.pathsep.join((str(bin_dir), str(node.parent))),
        root=root,
        modules=modules,
        node=node,
        loader=loader,
        package_root=package_root,
        platform_root=platform_root,
        binary=binary,
        sdk_root=sdk_root,
        sdk=sdk,
    )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextmanager
def pinned_fake_copilot(
    binary: Path, *, host: tuple[str, str] = ("darwin", "arm64")
) -> Iterator[None]:
    """Pin the fake binary and report the given host to the resolver."""

    with (
        mock.patch.object(native_acp_dependencies, "_copilot_host", return_value=host),
        mock.patch.object(
            native_acp_dependencies, "_COPILOT_BINARY_SHA256", sha256(binary)
        ),
    ):
        yield
