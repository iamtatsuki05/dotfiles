#!/usr/bin/env python3
"""Project repository-owned skills without replacing personal skill collections."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

RECEIPT = '.dotfiles-managed-skills.json'
SKILLS = Path('dotfiles/.agent/skills')


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True, stderr=subprocess.PIPE).strip()


def common_dir(repo: Path) -> Path:
    return Path(git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def no_symlink_parents(path: Path, stop: Path) -> None:
    for parent in path.parents:
        if parent == stop:
            break
        require(not parent.is_symlink(), f'Symlink parent is not allowed: {parent}')
        require(not exists(parent) or parent.is_dir(), f'Parent is not a directory: {parent}')


def source_root(path: Path, common: Path) -> Path:
    require(not path.is_symlink(), f'Skill source must not be a symlink: {path}')
    path = path.resolve()
    require(path.is_dir() and not path.is_symlink(), f'Invalid skill source: {path}')
    repo = path.parents[2]
    require(repo / SKILLS == path, f'Expected repository skills root: {path}')
    require(Path(git(repo, 'rev-parse', '--show-toplevel')).resolve() == repo, f'Expected repository root: {repo}')
    require(common_dir(repo) == common, f'Migration source belongs to another repository: {path}')
    return path


def managed_skills(repo: Path) -> list[Path]:
    output = git(repo, 'ls-files', '-z', '--cached', '--others', '--exclude-standard', '--', str(SKILLS))
    result = set()
    for name in output.split('\0'):
        if not name:
            continue
        relative = Path(name).relative_to(SKILLS)
        if relative.name != 'SKILL.md' or relative.parts[0] == 'synced' or any(p.startswith('.') for p in relative.parts):
            continue
        skill = relative.parent
        require(skill != Path('.'), 'The skills root cannot itself be a skill')
        require((repo / SKILLS / relative).is_file(), f'Missing managed skill: {relative}')
        for parent in (repo / SKILLS / relative).parents:
            if parent == repo:
                break
            require(not parent.is_symlink(), f'Managed source has symlink parent: {parent}')
        result.add(skill)
    ordered = sorted(result)
    for skill in ordered:
        require(not any(parent in result for parent in skill.parents), f'Nested skill inside another skill: {skill}')
    return ordered


def link_matches(path: Path, expected: Path) -> bool:
    if not path.is_symlink():
        return False
    actual = Path(os.path.abspath(path.parent / os.readlink(path)))
    return actual.parent.resolve() / actual.name == expected.parent.resolve() / expected.name


def preserve_tree(source: Path, destination: Path, branches: set[Path], relative: Path = Path('.')) -> None:
    destination.mkdir(exist_ok=True)
    for entry in source.iterdir():
        child = relative / entry.name
        if child in branches:
            require(entry.is_dir() and not entry.is_symlink(), f'Cannot materialize symlink parent: {entry}')
            preserve_tree(entry, destination / entry.name, branches, child)
        else:
            (destination / entry.name).symlink_to(entry)


def sync(repo: Path, agent: str, dry_run: bool, migrate_from: Path | None) -> None:
    repo = repo.resolve()
    common = common_dir(repo)
    source = source_root(repo / SKILLS, common)
    old = source_root(migrate_from, common) if migrate_from else None
    old_tracked = set()
    if old is not None and old != source:
        indexed = git(old.parents[2], 'ls-files', '-z', '--cached', '--', str(SKILLS))
        old_tracked = {Path(name).relative_to(SKILLS).parent for name in indexed.split('\0') if name.endswith('/SKILL.md')}
    home = Path.home().absolute()
    target = home / ('.pi/agent/skills' if agent == 'pi' else '.dsh/skills')
    no_symlink_parents(target, home)
    skills = managed_skills(repo)
    links = {}
    for skill in skills:
        name = str(skill) if agent == 'pi' else 'dotfiles-' + str(skill).replace('/', '--')
        require(name not in links, f'Flattened skill name collision: {name}')
        links[name] = str(source / skill)
    conversion = None
    if target.is_symlink():
        require(agent == 'pi', f'Skill root must be a real directory: {target}')
        conversion = (target.parent / os.readlink(target)).resolve()
        require(conversion == source or conversion == old, f'Explicit --migrate-from required for skill root: {conversion}')
        source_root(conversion, common)
    else:
        require(not exists(target) or target.is_dir(), f'Skill root is not a directory: {target}')
    receipt = target / RECEIPT
    previous = {}
    if conversion is None and exists(receipt):
        require(receipt.is_file() and not receipt.is_symlink(), f'Invalid ownership receipt: {receipt}')
        data = json.loads(receipt.read_text())
        require(isinstance(data, dict) and data.get('version') == 1 and data.get('agent') == agent, f'Invalid ownership receipt: {receipt}')
        recorded_source = Path(data['source'])
        require(recorded_source == source or recorded_source == old, f'Explicit --migrate-from required for owned source: {recorded_source}')
        previous = data['links']
        require(isinstance(previous, dict), f'Invalid ownership links: {receipt}')
        for name, expected in previous.items():
            relative = Path(name)
            require(not relative.is_absolute() and '..' not in relative.parts and relative != Path('.') and name != RECEIPT, f'Invalid owned path: {name}')
            source_relative = Path(expected).relative_to(recorded_source)
            require(source_relative != Path('.') and '..' not in source_relative.parts, f'Invalid owned source: {expected}')
            expected_name = str(source_relative) if agent == 'pi' else 'dotfiles-' + str(source_relative).replace('/', '--')
            require(name == expected_name, f'Ownership name does not match source: {name}')
            no_symlink_parents(target / name, target)
            require(link_matches(target / name, Path(expected)), f'Modified managed skill: {target / name}')
    elif conversion is None and agent == 'pi' and target.exists():
        require(not any(target.iterdir()), f'Unowned nonempty Pi skills directory: {target}')
    branches = {parent for skill in skills for parent in skill.parents if parent != Path('.')}
    for name, expected in links.items():
        path = target / name
        if conversion is not None:
            relative = Path(name)
            for parent in relative.parents:
                if parent != Path('.'):
                    candidate = conversion / parent
                    require(not candidate.is_symlink() and (not exists(candidate) or candidate.is_dir()), f'Conflicting source parent: {candidate}')
            candidate = conversion / relative
            require(not exists(candidate) or conversion == source or relative in old_tracked, f'Unowned source skill collision: {candidate}')
            require(not exists(candidate) or (candidate.is_dir() and not candidate.is_symlink() and (candidate / 'SKILL.md').is_file()), f'Conflicting source skill: {candidate}')
        else:
            no_symlink_parents(path, target)
            if exists(path) and name not in previous:
                allowed = [Path(expected)]
                source_relative = Path(expected).relative_to(source)
                if old is not None and source_relative in old_tracked:
                    allowed.append(old / source_relative)
                require(agent == 'deepseek' and any(link_matches(path, p) for p in allowed), f'Conflicting unmanaged skill: {path}')
    if conversion is not None:
        require(not exists(conversion / RECEIPT), f'Source contains reserved receipt: {conversion / RECEIPT}')
    if dry_run:
        print(f'{agent}: validated {len(links)} managed skills (dry-run)')
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = None
    work = target
    try:
        if conversion is not None:
            staged = Path(tempfile.mkdtemp(prefix='.skills-stage-', dir=target.parent))
            preserve_tree(conversion, staged, branches)
            work = staged
        else:
            work.mkdir(exist_ok=True)
        for name in previous.keys() - links.keys():
            (work / name).unlink()
        for name, expected in links.items():
            path = work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if exists(path):
                if link_matches(path, Path(expected)):
                    continue
                path.unlink()
            path.symlink_to(expected)
        data = {'version': 1, 'agent': agent, 'source': str(source), 'links': links}
        (work / RECEIPT).write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')
        if conversion is not None:
            require(target.is_symlink() and target.resolve() == conversion, f'Pi skill root changed during staging: {target}')
            original_link = os.readlink(target)
            target.unlink()
            try:
                staged.rename(target)
            except BaseException:
                target.symlink_to(original_link)
                raise
            staged = None
    finally:
        if staged is not None:
            shutil.rmtree(staged)
    print(f'{agent}: synchronized {len(links)} managed skills')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo-root', type=Path, required=True)
    parser.add_argument('--agent', choices=('pi', 'deepseek'), required=True)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--migrate-from', type=Path)
    args = parser.parse_args()
    try:
        sync(args.repo_root, args.agent, args.dry_run, args.migrate_from)
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
