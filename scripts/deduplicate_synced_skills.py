#!/usr/bin/env python3
"""Apply Codex document-skill exclusions and deduplicate synced copies without changing sources."""
import argparse
import hashlib
import json
from pathlib import Path
import tomllib

BEGIN = '# BEGIN dotfiles synced skill deduplication'
END = '# END dotfiles synced skill deduplication'
LOCAL_DOCUMENT_SKILLS = ('docx', 'pdf', 'xlsx', 'powerpoint')
SYNCED_DOCUMENT_SKILLS = {'docs', 'docx', 'pdf', 'xlsx', 'pptx'}


def fingerprint(directory):
    entries = []
    for path in sorted(directory.rglob('*')):
        if '__pycache__' in path.parts:
            continue
        if path.is_symlink():
            raise ValueError(f'Symlink in synced skill: {path}')
        if path.is_file():
            entries.append((str(path.relative_to(directory)), hashlib.sha256(path.read_bytes()).hexdigest()))
    return tuple(entries)


def update(skills_root, config, dry_run=False, local_skills_root=None):
    original = config.read_text()
    text = original
    if BEGIN in text or END in text:
        if text.count(BEGIN) != 1 or text.count(END) != 1:
            raise ValueError('Invalid managed deduplication block')
        start, end = text.index(BEGIN), text.index(END)
        if end < start:
            raise ValueError('Invalid managed deduplication block order')
        text = text[:start] + text[end + len(END):].lstrip('\n')
    parsed = tomllib.loads(text)
    explicit = {str(Path(item['path']).expanduser().resolve()): item.get('enabled', True)
                for item in parsed.get('skills', {}).get('config', [])}
    candidates = sorted(skills_root.glob('*/*/SKILL.md'))
    excluded = {path.resolve() for path in candidates if path.parent.name in SYNCED_DOCUMENT_SKILLS}
    if local_skills_root is not None:
        excluded.update((local_skills_root / 'productivity' / name / 'SKILL.md').resolve()
                        for name in LOCAL_DOCUMENT_SKILLS
                        if (local_skills_root / 'productivity' / name / 'SKILL.md').is_file())
    for path in sorted(excluded):
        if explicit.get(str(path)) is True:
            raise ValueError(f'Document skill is explicitly enabled: {path}')
    candidates = [path for path in candidates if path.resolve() not in excluded]
    seen = {(path.parent.name, fingerprint(path.parent)) for path in candidates
            if explicit.get(str(path.resolve())) is True}
    disabled = [str(path) for path in sorted(excluded) if str(path) not in explicit]
    for skill_file in candidates:
        key = (skill_file.parent.name, fingerprint(skill_file.parent))
        path = str(skill_file.resolve())
        if path in explicit:
            continue
        if key in seen:
            disabled.append(path)
        else:
            seen.add(key)
    block = ''
    if disabled:
        block = BEGIN + '\n'
        for path in disabled:
            block += '\n[[skills.config]]\npath = ' + json.dumps(path) + '\nenabled = false\n'
        block += END + '\n'
    result = text.rstrip() + '\n' + ('\n' + block if block else '')
    tomllib.loads(result)
    if not dry_run and result != original:
        config.write_text(result)
    print(f'Codex: {len(disabled)} skill exclusions configured' + (' (dry-run)' if dry_run else ''))
    return disabled


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skills-root', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--local-skills-root', type=Path)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    update(args.skills_root, args.config, args.dry_run, args.local_skills_root)


if __name__ == '__main__':
    main()
