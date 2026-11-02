#!/usr/bin/env python3
import json
from pathlib import Path
import subprocess
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
SKILLS = Path('dotfiles/.agent/skills')


def managed_directories():
    paths = subprocess.check_output(
        ['git', '-C', str(ROOT), 'ls-files', '--cached', '--others',
         '--exclude-standard', '--', str(SKILLS / '**/SKILL.md')], text=True
    ).splitlines()
    return {Path(path).relative_to(SKILLS).parent.as_posix() for path in paths
            if not any(part.startswith('.') for part in Path(path).relative_to(SKILLS).parts)
            and Path(path).relative_to(SKILLS).parts[0] != 'synced'}


class SkillDistributionTest(unittest.TestCase):
    def test_antigravity_declares_all_managed_nested_skills(self):
        config = json.loads((ROOT / 'dotfiles/.agent/apps/antigravity-cli/plugins/dotfiles-agent/plugin.json').read_text())
        expected = {'./skills/' + path for path in managed_directories() if '/' in path}
        actual = config.get('skills', [])
        self.assertEqual(set(actual), expected)
        self.assertEqual(len(actual), len(set(actual)))

    def test_hermes_only_and_archived_skills_are_outside_shared_discovery(self):
        root = ROOT / 'dotfiles/.agent'
        for name in ('codex', 'claude-code', 'plan'):
            self.assertTrue((root / 'apps/hermes-agent/skills' / name / 'SKILL.md').is_file())
        for path in ('autonomous-ai-agents/codex', 'autonomous-ai-agents/claude-code',
                     'software-development/plan', 'software-development/systematic-debugging'):
            self.assertFalse((ROOT / SKILLS / path).exists())
        self.assertTrue((root / 'skill-archives/systematic-debugging/SKILL.md').is_file())
        config = (root / 'apps/hermes-agent/config.yaml').read_text()
        self.assertIn('    - ~/.hermes/dotfiles-hermes-skills', config)

    def test_grok_explicit_paths_preserve_bundled_docx(self):
        config = tomllib.loads((ROOT / 'dotfiles/.agent/apps/grok/config.toml').read_text())
        # Explicit user paths override bundled skills; keep Grok's native docx.
        expected = {'~/.grok/dotfiles-skills/' + path for path in managed_directories()
                    if path != 'productivity/docx'}
        actual = config.get('skills', {}).get('paths', [])
        self.assertEqual(set(actual), expected)
        self.assertEqual(len(actual), len(set(actual)))
        self.assertNotIn('ignore', config.get('skills', {}))


if __name__ == '__main__':
    unittest.main()
