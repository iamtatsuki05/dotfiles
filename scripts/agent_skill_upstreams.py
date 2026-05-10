#!/usr/bin/env python3
"""Track and update vendored agent skills from upstream Git repositories."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from string import Template
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "dotfiles/.agent/skills/upstreams.json"
DEFAULT_REVIEW_PROMPT = REPO_ROOT / "dotfiles/.agent/skills/review-prompts/skill-upstream-security.md"
SHA_REPR_LENGTH = 12
REVIEW_AGENTS = (
    "codex",
    "claude-code",
    "copilot",
    "cursor-agent",
    "devin",
    "gemini-cli",
    "hermes",
    "opencode",
)
MISE_TOOLS_BY_REVIEW_AGENT = {
    "codex": "codex",
    "claude-code": "claude-code",
    "copilot": "npm:@github/copilot",
    "cursor-agent": "http:cursor-agent",
    "devin": "http:devin",
    "gemini-cli": "gemini-cli",
    "hermes": "pipx:git+https://github.com/NousResearch/hermes-agent.git",
    "opencode": "opencode",
}


class UpstreamError(Exception):
    pass


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise UpstreamError(f"manifest not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise UpstreamError(f"invalid JSON manifest: {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise UpstreamError("manifest root must be an object")
    if data.get("version") != 1:
        raise UpstreamError("manifest version must be 1")
    if not isinstance(data.get("skills"), list):
        raise UpstreamError("manifest skills must be a list")
    return data


def write_manifest(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def short_sha(commit: str) -> str:
    return commit[:SHA_REPR_LENGTH]


def is_full_sha(value: str) -> bool:
    return len(value) == 40 and all(char in "0123456789abcdef" for char in value.lower())


def relative_path(value: str, field_name: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise UpstreamError(f"{field_name} must be a relative path without '..': {value}")
    return path


def require_https_github_url(value: str) -> None:
    if not value.startswith("https://github.com/") or not value.endswith(".git"):
        raise UpstreamError(f"repository must be an https GitHub .git URL: {value}")


def validate_review_agent(review_agent: str) -> None:
    if review_agent not in REVIEW_AGENTS:
        allowed = ", ".join(REVIEW_AGENTS)
        raise UpstreamError(f"review agent must be one of: {allowed}")


def resolve_repo_path(path: str) -> Path:
    rel_path = relative_path(path, "local_path")
    resolved = (REPO_ROOT / rel_path).resolve()
    try:
        resolved.relative_to(REPO_ROOT)
    except ValueError as exc:
        raise UpstreamError(f"local_path must stay inside repo: {path}") from exc
    return resolved


def validate_skill(skill: dict[str, Any], seen_ids: set[str]) -> None:
    skill_id = skill.get("id")
    if not isinstance(skill_id, str) or not skill_id:
        raise UpstreamError("each skill requires a non-empty id")
    if skill_id in seen_ids:
        raise UpstreamError(f"duplicate skill id: {skill_id}")
    seen_ids.add(skill_id)

    repository = skill.get("repository")
    if not isinstance(repository, str):
        raise UpstreamError(f"{skill_id}: repository is required")
    require_https_github_url(repository)

    branch = skill.get("branch")
    if not isinstance(branch, str) or not branch:
        raise UpstreamError(f"{skill_id}: branch is required")

    pinned_commit = skill.get("pinned_commit")
    if not isinstance(pinned_commit, str) or not is_full_sha(pinned_commit):
        raise UpstreamError(f"{skill_id}: pinned_commit must be a full 40-char git SHA")

    mappings = skill.get("mappings")
    if not isinstance(mappings, list) or not mappings:
        raise UpstreamError(f"{skill_id}: mappings must be a non-empty list")

    for mapping in mappings:
        if not isinstance(mapping, dict):
            raise UpstreamError(f"{skill_id}: mapping must be an object")
        source_path = mapping.get("source_path")
        local_path = mapping.get("local_path")
        if not isinstance(source_path, str):
            raise UpstreamError(f"{skill_id}: mapping source_path is required")
        if not isinstance(local_path, str):
            raise UpstreamError(f"{skill_id}: mapping local_path is required")
        relative_path(source_path, "source_path")
        resolved = resolve_repo_path(local_path)
        if not resolved.exists():
            raise UpstreamError(f"{skill_id}: local_path does not exist: {local_path}")


def validate_manifest(data: dict[str, Any]) -> None:
    seen_ids: set[str] = set()
    for skill in data["skills"]:
        if not isinstance(skill, dict):
            raise UpstreamError("each skill entry must be an object")
        validate_skill(skill, seen_ids)


def select_skills(data: dict[str, Any], skill_id: str | None) -> list[dict[str, Any]]:
    if skill_id is None:
        return list(data["skills"])
    selected = [skill for skill in data["skills"] if skill.get("id") == skill_id]
    if not selected:
        raise UpstreamError(f"unknown upstream skill id: {skill_id}")
    return selected


def select_target_skills(data: dict[str, Any], skill_id: str | None, all_skills: bool) -> list[dict[str, Any]]:
    if all_skills:
        if skill_id is not None:
            raise UpstreamError("use either --id or --all, not both")
        return list(data["skills"])
    if skill_id is None:
        raise UpstreamError("either --id or --all is required")
    return select_skills(data, skill_id)


def run_git(args: list[str], cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise UpstreamError("git is not installed") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip()
        raise UpstreamError(f"git {' '.join(args)} failed: {detail}") from exc
    return result.stdout


def run_command(
    command: list[str],
    cwd: Path,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            input=input_text,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise UpstreamError(f"command not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip()
        raise UpstreamError(f"{' '.join(command)} failed: {detail}") from exc
    return result.stdout


def run_direct_or_mise(
    review_agent: str,
    command: list[str],
    cwd: Path,
    env: dict[str, str] | None = None,
) -> str:
    if shutil.which(command[0]) is not None:
        return run_command(command, cwd, env)

    mise = shutil.which("mise")
    if mise is None:
        raise UpstreamError(f"{command[0]} CLI is not on PATH and mise is not available")
    mise_tool = MISE_TOOLS_BY_REVIEW_AGENT[review_agent]
    mise_env = dict(os.environ)
    if env:
        mise_env.update(env)
    mise_env.setdefault("MISE_CONFIG_FILE", str(REPO_ROOT / "config/mise/config.toml"))
    return run_command([mise, "exec", mise_tool, "--", *command], cwd, mise_env)


def parse_ls_remote(output: str, branch: str) -> str:
    target_ref = f"refs/heads/{branch}"
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == target_ref and is_full_sha(parts[0]):
            return parts[0].lower()
    raise UpstreamError(f"could not find {target_ref} in ls-remote output")


def latest_commit(skill: dict[str, Any], fixture_output: str | None = None) -> str:
    branch = skill["branch"]
    output = fixture_output
    if output is None:
        output = run_git(["ls-remote", skill["repository"], f"refs/heads/{branch}"])
    return parse_ls_remote(output, branch)


def parse_latest_commit_overrides(values: list[str] | None) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for value in values or []:
        if "=" not in value:
            raise UpstreamError("--latest-commit must use id=40-char-sha")
        skill_id, commit = value.split("=", 1)
        if not skill_id:
            raise UpstreamError("--latest-commit id cannot be empty")
        if not is_full_sha(commit):
            raise UpstreamError("--latest-commit value must be a full 40-char git SHA")
        overrides[skill_id] = commit.lower()
    return overrides


def candidate_commit_for_skill(
    skill: dict[str, Any],
    explicit_commit: str | None,
    latest_overrides: dict[str, str],
    fixture_output: str | None = None,
) -> str:
    if explicit_commit is not None:
        if not is_full_sha(explicit_commit):
            raise UpstreamError("--commit must be a full 40-char git SHA")
        return explicit_commit.lower()
    skill_id = skill["id"]
    if skill_id in latest_overrides:
        return latest_overrides[skill_id]
    return latest_commit(skill, fixture_output)


def tree_sha256(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for root in sorted(paths):
        if root.is_file():
            files = [root]
            base = root.parent
        else:
            files = sorted(path for path in root.rglob("*") if path.is_file())
            base = root
        for file_path in files:
            rel = file_path.relative_to(base).as_posix()
            digest.update(rel.encode("utf-8"))
            digest.update(b"\0")
            digest.update(hashlib.sha256(file_path.read_bytes()).hexdigest().encode("ascii"))
            digest.update(b"\0")
    return digest.hexdigest()


def read_review_prompt_template(path: Path) -> Template:
    try:
        return Template(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise UpstreamError(f"review prompt not found: {path}") from exc


def default_review_report_dir() -> Path:
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return REPO_ROOT / "dotfiles/.agent/changes/skill-upstream-reviews" / timestamp


def cmd_check(args: argparse.Namespace) -> int:
    data = load_manifest(args.manifest)
    validate_manifest(data)

    print(f"registered upstream skills: {len(data['skills'])}")
    for skill in data["skills"]:
        local_paths = [resolve_repo_path(mapping["local_path"]) for mapping in skill["mappings"]]
        actual_sha = tree_sha256(local_paths)
        recorded_sha = skill.get("local_tree_sha256")
        sha_status = "ok" if actual_sha == recorded_sha else f"changed actual={actual_sha}"
        print(
            f"- {skill['id']}: {skill['repository']} "
            f"{skill['branch']}@{short_sha(skill['pinned_commit'])} local_tree_sha256={sha_status}"
        )
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    data = load_manifest(args.manifest)
    validate_manifest(data)

    for skill in data["skills"]:
        print(f"{skill['id']}\t{skill['repository']}\t{skill['branch']}\t{skill['pinned_commit']}")
    return 0


def cmd_updates(args: argparse.Namespace) -> int:
    data = load_manifest(args.manifest)
    validate_manifest(data)

    for skill in select_skills(data, args.id):
        current = skill["pinned_commit"]
        candidate = latest_commit(skill, args.ls_remote_output)
        status = "up to date" if candidate == current else "update available"
        print(f"{skill['id']}: {status} pinned={current} candidate={candidate}")
    return 0


def security_prompt(
    skill: dict[str, Any],
    candidate_commit: str,
    review_agent: str,
    prompt_template: Template,
) -> str:
    mapping_lines = "\n".join(
        f"- {mapping['source_path']} -> {mapping['local_path']}" for mapping in skill["mappings"]
    )
    return prompt_template.safe_substitute(
        review_agent=review_agent,
        skill_id=skill["id"],
        repository=skill["repository"],
        branch=skill["branch"],
        pinned_commit=skill["pinned_commit"],
        candidate_commit=candidate_commit,
        mappings=mapping_lines,
    )


def cmd_security_prompt(args: argparse.Namespace) -> int:
    data = load_manifest(args.manifest)
    validate_manifest(data)
    validate_review_agent(args.review_agent)
    skills = select_target_skills(data, args.id, args.all)
    if args.all and args.commit is not None:
        raise UpstreamError("--commit can only be used with --id")
    latest_overrides = parse_latest_commit_overrides(args.latest_commit)
    prompt_template = read_review_prompt_template(args.review_prompt)

    for index, skill in enumerate(skills):
        candidate_commit = candidate_commit_for_skill(skill, args.commit, latest_overrides)
        if index > 0:
            print("\n---\n")
        print(security_prompt(skill, candidate_commit, args.review_agent, prompt_template))
    return 0


def run_review_agent(
    review_agent: str,
    prompt: str,
    prompt_file: Path,
    report_file: Path,
    review_command: str | None = None,
) -> None:
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text(prompt, encoding="utf-8")

    env = dict(os.environ)
    env["AGENT_SKILL_REVIEW_PROMPT"] = str(prompt_file)
    env["AGENT_SKILL_REVIEW_REPORT"] = str(report_file)
    env["AGENT_SKILL_REVIEW_REPO_ROOT"] = str(REPO_ROOT)

    if review_command is not None:
        run_command(["/bin/sh", "-c", review_command], REPO_ROOT, env)
        return

    if review_agent == "codex":
        output = run_direct_or_mise(review_agent, ["codex", "exec", "-C", str(REPO_ROOT), prompt], REPO_ROOT, env)
    elif review_agent == "claude-code":
        output = run_direct_or_mise(review_agent, ["claude", "-p", prompt], REPO_ROOT, env)
    elif review_agent == "gemini-cli":
        output = run_direct_or_mise(review_agent, ["gemini", "-p", prompt], REPO_ROOT, env)
    elif review_agent == "copilot":
        output = run_direct_or_mise(
            review_agent,
            [
                "copilot",
                "-C",
                str(REPO_ROOT),
                "--allow-all",
                "--no-remote",
                "--output-format",
                "text",
                "-p",
                prompt,
            ],
            REPO_ROOT,
            env,
        )
    elif review_agent == "devin":
        output = run_direct_or_mise(
            review_agent,
            ["devin", "--permission-mode", "dangerous", "--respect-workspace-trust", "true", "-p", prompt],
            REPO_ROOT,
            env,
        )
    elif review_agent == "cursor-agent":
        output = run_direct_or_mise(
            review_agent,
            ["cursor-agent", "--workspace", str(REPO_ROOT), "--print", "--force", "--trust", prompt],
            REPO_ROOT,
            env,
        )
    elif review_agent == "opencode":
        output = run_direct_or_mise(
            review_agent,
            ["opencode", "run", "--dir", str(REPO_ROOT), "--dangerously-skip-permissions", prompt],
            REPO_ROOT,
            env,
        )
    elif review_agent == "hermes":
        hermes_env = dict(env)
        hermes_env["HERMES_ACCEPT_HOOKS"] = "1"
        output = run_direct_or_mise(
            review_agent,
            ["hermes", "--accept-hooks", "--yolo", "-z", prompt],
            REPO_ROOT,
            hermes_env,
        )
    else:
        raise UpstreamError(f"unsupported review agent: {review_agent}")

    report_file.write_text(output, encoding="utf-8")


def review_report_approved(report_file: Path) -> bool:
    text = report_file.read_text(encoding="utf-8")
    has_approve = (
        re.search(r"(?im)^\s*-?\s*update recommendation:\s*approve(?:\s+with\s+changes)?\.?\s*$", text)
        is not None
    )
    has_blocking_severity = False
    for match in re.finditer(r"(?im)\b(?:critical|high)\b\s*[:,-]\s*([^\n.;]+)", text):
        value = match.group(1).strip().lower()
        if value and not re.match(r"^(none|no|0)\b", value):
            has_blocking_severity = True
            break
    return has_approve and not has_blocking_severity


def build_update_plan(
    skills: list[dict[str, Any]],
    explicit_commit: str | None,
    latest_overrides: dict[str, str],
    fixture_output: str | None = None,
) -> list[tuple[dict[str, Any], str]]:
    plans: list[tuple[dict[str, Any], str]] = []
    for skill in skills:
        candidate_commit = candidate_commit_for_skill(skill, explicit_commit, latest_overrides, fixture_output)
        if candidate_commit == skill["pinned_commit"]:
            print(f"{skill['id']}: already up to date pinned={skill['pinned_commit']}")
            continue
        plans.append((skill, candidate_commit))
    return plans


def cmd_update(args: argparse.Namespace) -> int:
    data = load_manifest(args.manifest)
    validate_manifest(data)
    validate_review_agent(args.review_agent)
    skills = select_skills(data, args.id)
    if args.id is None and args.commit is not None:
        raise UpstreamError("--commit can only be used with --id")

    latest_overrides = parse_latest_commit_overrides(args.latest_commit)
    prompt_template = read_review_prompt_template(args.review_prompt)
    review_dir = Path(args.review_report_dir).resolve() if args.review_report_dir else default_review_report_dir()
    plans = build_update_plan(skills, args.commit, latest_overrides, args.ls_remote_output)
    if not plans:
        print("no upstream skill updates available")
        return 0

    apply_args = argparse.Namespace(
        all=args.id is None,
        id=args.id,
        commit=args.commit,
        latest=True,
        review_report=None if args.id is None else str(review_dir / f"{args.id}.md"),
        review_report_dir=str(review_dir) if args.id is None else None,
        review_agent=args.review_agent,
        security_reviewed=True,
        dry_run=args.dry_run,
        latest_commit=[f"{skill['id']}={candidate_commit}" for skill, candidate_commit in plans],
        ls_remote_output=args.ls_remote_output,
        manifest=args.manifest,
    )

    def run_review(plan: tuple[dict[str, Any], str]) -> tuple[dict[str, Any], str, Path]:
        skill, candidate_commit = plan
        prompt = security_prompt(skill, candidate_commit, args.review_agent, prompt_template)
        prompt_file = review_dir / f"{skill['id']}.prompt.md"
        report_file = review_dir / f"{skill['id']}.md"
        run_review_agent(args.review_agent, prompt, prompt_file, report_file, args.review_command)
        return skill, candidate_commit, report_file

    for skill, candidate_commit in plans:
        print(f"{skill['id']}: running {args.review_agent} review candidate={candidate_commit}")

    review_results: list[tuple[dict[str, Any], str, Path]] = []
    if args.id is None and len(plans) > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(plans)) as executor:
            futures = [executor.submit(run_review, plan) for plan in plans]
            for future in concurrent.futures.as_completed(futures):
                review_results.append(future.result())
    else:
        for plan in plans:
            review_results.append(run_review(plan))

    review_results.sort(key=lambda item: item[0]["id"])
    for skill, _candidate_commit, report_file in review_results:
        if not review_report_approved(report_file):
            raise UpstreamError(
                f"{skill['id']}: review did not approve update; report={os.path.relpath(report_file, REPO_ROOT)}"
            )
        print(f"{skill['id']}: review approved report={os.path.relpath(report_file, REPO_ROOT)}")

    return cmd_apply_update(apply_args)


def clone_at_commit(skill: dict[str, Any], commit: str, workdir: Path) -> Path:
    checkout = workdir / "checkout"
    run_git(["clone", "--filter=blob:none", "--sparse", skill["repository"], str(checkout)])
    run_git(["checkout", commit], cwd=checkout)
    source_paths = [mapping["source_path"] for mapping in skill["mappings"]]
    run_git(["sparse-checkout", "set", "--skip-checks", *source_paths], cwd=checkout)
    return checkout


def replace_path(src: Path, dst: Path) -> None:
    if src.is_dir():
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
    elif src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    else:
        raise UpstreamError(f"upstream source path does not exist: {src}")


def review_report_for_skill(args: argparse.Namespace, skill: dict[str, Any]) -> Path:
    if args.all:
        if args.review_report is not None:
            raise UpstreamError("--review-report can only be used with --id")
        if args.review_report_dir is None:
            raise UpstreamError("--all requires --review-report-dir")
        review_report = Path(args.review_report_dir).resolve() / f"{skill['id']}.md"
    else:
        if args.review_report_dir is not None:
            raise UpstreamError("--review-report-dir can only be used with --all")
        if args.review_report is None:
            raise UpstreamError("--id requires --review-report")
        review_report = Path(args.review_report).resolve()
    if not review_report.is_file():
        raise UpstreamError(f"security review report not found: {review_report}")
    return review_report


def cmd_apply_update(args: argparse.Namespace) -> int:
    data = load_manifest(args.manifest)
    validate_manifest(data)
    validate_review_agent(args.review_agent)
    skills = select_target_skills(data, args.id, args.all)
    if args.all and args.commit is not None:
        raise UpstreamError("--commit can only be used with --id")
    if args.all and not args.latest:
        raise UpstreamError("--all requires --latest")
    if not args.security_reviewed:
        raise UpstreamError("refusing update without --security-reviewed")

    latest_overrides = parse_latest_commit_overrides(args.latest_commit)
    plans: list[tuple[dict[str, Any], str, Path]] = []
    for skill in skills:
        candidate_commit = candidate_commit_for_skill(
            skill,
            args.commit,
            latest_overrides,
            args.ls_remote_output,
        )
        review_report = review_report_for_skill(args, skill)
        plans.append((skill, candidate_commit, review_report))
        print(
            f"{skill['id']}: plan update "
            f"pinned={skill['pinned_commit']} candidate={candidate_commit} "
            f"review_agent={args.review_agent} "
            f"review_report={os.path.relpath(review_report, REPO_ROOT)}"
        )

    if args.dry_run:
        return 0

    for skill, candidate_commit, review_report in plans:
        with tempfile.TemporaryDirectory(prefix="agent-skill-upstream-") as tmp:
            checkout = clone_at_commit(skill, candidate_commit, Path(tmp))
            for mapping in skill["mappings"]:
                src = checkout / mapping["source_path"]
                dst = resolve_repo_path(mapping["local_path"])
                print(f"update {mapping['local_path']} from {mapping['source_path']}")
                replace_path(src, dst)

        local_paths = [resolve_repo_path(mapping["local_path"]) for mapping in skill["mappings"]]
        skill["pinned_commit"] = candidate_commit
        skill["local_tree_sha256"] = tree_sha256(local_paths)
        skill["security_review"] = {
            "status": "reviewed",
            "review_agent": args.review_agent,
            "last_reviewed_commit": candidate_commit,
            "last_reviewed_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
            "review_report": os.path.relpath(review_report, REPO_ROOT),
        }

    write_manifest(args.manifest, data)
    print(f"manifest updated: {args.manifest}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("check", help="Validate manifest and local vendored tree hashes").set_defaults(
        func=cmd_check
    )
    subparsers.add_parser("list", help="List registered upstream skills").set_defaults(func=cmd_list)

    updates = subparsers.add_parser("updates", help="Check whether upstream branches moved")
    updates.add_argument("--id", help="Limit to one upstream skill id")
    updates.add_argument("--ls-remote-output", help=argparse.SUPPRESS)
    updates.set_defaults(func=cmd_updates)

    prompt = subparsers.add_parser("security-prompt", help="Print an Agent security review prompt")
    prompt_target = prompt.add_mutually_exclusive_group(required=True)
    prompt_target.add_argument("--id", help="Upstream skill id")
    prompt_target.add_argument("--all", action="store_true", help="Generate prompts for all upstream skills")
    prompt.add_argument(
        "--commit",
        "--candidate-commit",
        dest="commit",
        help="Commit to review for --id; defaults to latest branch head",
    )
    prompt.add_argument(
        "--review-agent",
        default="codex",
        help="Agent to use for review; one of the managed repo agents",
    )
    prompt.add_argument(
        "--review-prompt",
        type=Path,
        default=DEFAULT_REVIEW_PROMPT,
        help="Review prompt template file",
    )
    prompt.add_argument("--latest-commit", action="append", help=argparse.SUPPRESS)
    prompt.set_defaults(func=cmd_security_prompt)

    apply_update = subparsers.add_parser("apply-update", help="Apply a reviewed upstream skill update")
    apply_target = apply_update.add_mutually_exclusive_group(required=True)
    apply_target.add_argument("--id", help="Upstream skill id")
    apply_target.add_argument("--all", action="store_true", help="Apply latest reviewed updates for all upstream skills")
    apply_update.add_argument(
        "--commit",
        "--candidate-commit",
        dest="commit",
        help="Commit to apply for --id; defaults to latest branch head",
    )
    apply_update.add_argument("--latest", action="store_true", help="Apply the latest branch head")
    apply_update.add_argument("--review-report", help="Path to the Agent security review report for --id")
    apply_update.add_argument("--review-report-dir", help="Directory containing <skill-id>.md reports for --all")
    apply_update.add_argument(
        "--review-agent",
        default="codex",
        help="Agent that performed the review; one of the managed repo agents",
    )
    apply_update.add_argument("--security-reviewed", action="store_true", help="Confirm Agent review completed")
    apply_update.add_argument("--dry-run", action="store_true", help="Show files that would be updated")
    apply_update.add_argument("--ls-remote-output", help=argparse.SUPPRESS)
    apply_update.add_argument("--latest-commit", action="append", help=argparse.SUPPRESS)
    apply_update.set_defaults(func=cmd_apply_update)

    update = subparsers.add_parser(
        "update",
        help="Review and update upstream skills; defaults to all registered skills at latest branch heads",
    )
    update.add_argument("--id", help="Limit to one upstream skill id")
    update.add_argument("--commit", help="Commit to apply for --id; defaults to latest branch head")
    update.add_argument(
        "--review-agent",
        default="codex",
        help="Agent that performs the review; one of the managed repo agents",
    )
    update.add_argument(
        "--review-prompt",
        type=Path,
        default=DEFAULT_REVIEW_PROMPT,
        help="Review prompt template file",
    )
    update.add_argument(
        "--review-report-dir",
        help="Directory for generated <skill-id>.prompt.md and <skill-id>.md review reports",
    )
    update.add_argument("--dry-run", action="store_true", help="Run reviews and show update plan without replacing files")
    update.add_argument("--review-command", help=argparse.SUPPRESS)
    update.add_argument("--ls-remote-output", help=argparse.SUPPRESS)
    update.add_argument("--latest-commit", action="append", help=argparse.SUPPRESS)
    update.set_defaults(func=cmd_update)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except UpstreamError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
