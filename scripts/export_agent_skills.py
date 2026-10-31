#!/usr/bin/env python3

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys


SKILLS_RELATIVE_PATH = Path("dotfiles/.agent/skills")
PUBLISH_CONFIG_RELATIVE_PATH = Path("config/agent-skills-publish.json")
SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
EXPECTED_REPOSITORY = "iamtatsuki05/skills"
SOURCE_REPOSITORY = "iamtatsuki05/dotfiles"


class ExportError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export allowlisted agent skills without changing the source tree."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Source repository root.",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def load_manifest(manifest_path: Path) -> dict:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ExportError(f"cannot read {manifest_path}: {error}") from error

    if not isinstance(manifest, dict):
        raise ExportError("publish configuration must be an object")
    if manifest.get("version") != 1:
        raise ExportError("publish manifest version must be 1")
    if manifest.get("repository") != EXPECTED_REPOSITORY:
        raise ExportError(f"repository must be {EXPECTED_REPOSITORY}")

    plugin = manifest.get("plugin")
    if not isinstance(plugin, dict):
        raise ExportError("plugin metadata must be an object")
    for key in ("name", "marketplace_name", "description", "author"):
        if not plugin.get(key):
            raise ExportError(f"plugin.{key} is required")
    if not isinstance(plugin["author"], dict) or not plugin["author"].get("name"):
        raise ExportError("plugin.author.name is required")

    skills = manifest.get("skills")
    if not isinstance(skills, list) or not skills:
        raise ExportError("skills must be a non-empty list")
    for name in skills:
        if not isinstance(name, str) or not SKILL_NAME_PATTERN.fullmatch(name):
            raise ExportError(f"invalid skill name: {name!r}")
    if skills != sorted(set(skills)):
        raise ExportError("skills must be unique and sorted")
    return manifest


def parse_skill_frontmatter(skill_file: Path) -> tuple[str, str]:
    text = skill_file.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---(?:\n|$)", text, re.DOTALL)
    if not match:
        raise ExportError(f"missing YAML frontmatter: {skill_file}")
    frontmatter = match.group(1)

    def scalar(key: str) -> str:
        field = re.search(rf"^{re.escape(key)}:\s*(.+?)\s*$", frontmatter, re.MULTILINE)
        if not field:
            raise ExportError(f"missing {key} in {skill_file}")
        value = field.group(1).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not value or value in {">", "|"}:
            raise ExportError(f"{key} must be a single-line scalar in {skill_file}")
        return value

    return scalar("name"), scalar("description")


def tracked_files(repo_root: Path, relative_skill: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z", "--", relative_skill.as_posix()],
        check=True,
        capture_output=True,
    )
    paths = [Path(value.decode()) for value in result.stdout.split(b"\0") if value]
    if not paths:
        raise ExportError(f"no tracked files found under {relative_skill}")
    return paths


def copy_skill(repo_root: Path, output: Path, name: str) -> str:
    relative_skill = SKILLS_RELATIVE_PATH / name
    source_skill = repo_root / relative_skill
    skill_file = source_skill / "SKILL.md"
    if not skill_file.is_file() or skill_file.is_symlink():
        raise ExportError(f"missing regular SKILL.md for {name}")

    declared_name, description = parse_skill_frontmatter(skill_file)
    if declared_name != name:
        raise ExportError(
            f"skill directory {name} does not match frontmatter name {declared_name}"
        )

    for relative_file in tracked_files(repo_root, relative_skill):
        source = repo_root / relative_file
        if source.is_symlink():
            raise ExportError(f"refusing tracked symlink: {relative_file}")
        if not source.is_file():
            raise ExportError(f"tracked path is not a regular file: {relative_file}")
        destination = output / "skills" / name / source.relative_to(source_skill)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return description


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def skill_list(skills: list[str]) -> str:
    return "\n".join(f"- [`{name}`](skills/{name}/SKILL.md)" for name in skills)


def write_readmes(output: Path, manifest: dict) -> None:
    repository = manifest["repository"]
    plugin = manifest["plugin"]
    skills = manifest["skills"]
    listed_skills = skill_list(skills)

    readme = f"""# Tatsuki's Agent Skills

This repository is generated from the reviewed skill sources in
[`iamtatsuki05/dotfiles`](https://github.com/iamtatsuki05/dotfiles).
Do not edit generated files directly.

## Install

### Claude Code

```bash
claude plugin marketplace add {repository}
claude plugin install {plugin['name']}@{plugin['marketplace_name']}
```

### Codex and other compatible agents

```bash
npx skills@latest add {repository}
```

## Published skills

{listed_skills}

## Copyright

Copyright (c) Tatsuki Okada. All rights reserved. No license is granted.
Individual files may carry their own license notices; those notices continue to apply.
"""
    readme_ja = f"""# Tatsuki's Agent Skills

このリポジトリは、
[`iamtatsuki05/dotfiles`](https://github.com/iamtatsuki05/dotfiles)
でレビュー済みのskillを公開用に生成したものです。
生成されたファイルは直接編集しないでください。

## インストール

### Claude Code

```bash
claude plugin marketplace add {repository}
claude plugin install {plugin['name']}@{plugin['marketplace_name']}
```

### Codexなどの対応agent

```bash
npx skills@latest add {repository}
```

## 公開するskill

{listed_skills}

## 著作権

Copyright (c) Tatsuki Okada. All rights reserved. No license is granted.
個別ファイルにライセンス表記がある場合は、その条件が引き続き適用されます。
"""
    (output / "README.md").write_text(readme, encoding="utf-8")
    (output / "README_JA.md").write_text(readme_ja, encoding="utf-8")


def export(repo_root: Path, output: Path) -> None:
    repo_root = repo_root.resolve()
    output = output.resolve()
    skills_root = repo_root / SKILLS_RELATIVE_PATH
    if not skills_root.is_dir():
        raise ExportError(f"skills root not found: {skills_root}")
    try:
        output.relative_to(repo_root)
    except ValueError:
        pass
    else:
        raise ExportError("output directory must be outside the source repository")
    if output.exists() and any(output.iterdir()):
        raise ExportError(f"output directory must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)

    manifest = load_manifest(repo_root / PUBLISH_CONFIG_RELATIVE_PATH)
    for name in manifest["skills"]:
        copy_skill(repo_root, output, name)

    plugin = manifest["plugin"]
    skill_paths = [f"./skills/{name}" for name in manifest["skills"]]
    write_json(
        output / ".agent-skills-mirror.json",
        {
            "schema_version": 1,
            "source_repository": SOURCE_REPOSITORY,
            "destination_repository": EXPECTED_REPOSITORY,
        },
    )
    write_json(
        output / ".claude-plugin" / "plugin.json",
        {
            "name": plugin["name"],
            "description": plugin["description"],
            "author": plugin["author"],
            "repository": f"https://github.com/{manifest['repository']}",
            "skills": skill_paths,
        },
    )
    write_json(
        output / ".claude-plugin" / "marketplace.json",
        {
            "name": plugin["marketplace_name"],
            "owner": plugin["author"],
            "description": plugin["description"],
            "plugins": [
                {
                    "name": plugin["name"],
                    "source": "./",
                    "description": plugin["description"],
                    "category": "development",
                }
            ],
        },
    )
    write_readmes(output, manifest)


def main() -> int:
    args = parse_args()
    try:
        export(args.repo_root, args.output)
    except (ExportError, OSError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"Exported agent skills to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
