#!/usr/bin/env python3
import json
import os
import runpy
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/sync_agent_skills.py'


class ManagedSkillSyncTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.home = self.root / 'home'
        self.home.mkdir()
        self.git('init', '-q')
        self.source = self.repo / 'dotfiles/.agent/skills'
        self.skill('alpha')
        self.skill('group/beta')
        self.skill('synced/private')
        self.skill('.system/private')
        (self.source / 'group/beta/resource.txt').write_text('resource')
        self.git('add', '.')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True)

    def skill(self, name):
        path = self.source / name
        path.mkdir(parents=True, exist_ok=True)
        (path / 'SKILL.md').write_text(name)

    def target(self, agent):
        return self.home / ('.pi/agent/skills' if agent == 'pi' else '.dsh/skills')

    def run_sync(self, agent, *args, ok=True):
        result = subprocess.run([sys.executable, str(SCRIPT), '--repo-root', str(self.repo), '--agent', agent, *map(str, args)], env={**os.environ, 'HOME': str(self.home)}, capture_output=True, text=True)
        self.assertEqual(result.returncode == 0, ok, result.stderr)
        return result

    def snapshot(self, root=None):
        def walk(path):
            if path.is_symlink():
                return ('link', os.readlink(path))
            if path.is_dir():
                return {p.name: walk(p) for p in sorted(path.iterdir())}
            return path.read_bytes()
        return walk(root or self.home)

    def test_fresh_repeat_and_only_authored_skills(self):
        self.skill('untracked')
        (self.repo / '.gitignore').write_text('dotfiles/.agent/skills/cache/\n')
        self.skill('cache/ignored')
        for agent in ('pi', 'deepseek'):
            self.run_sync(agent, '--dry-run')
            self.assertFalse(self.target(agent).exists())
            self.run_sync(agent)
            target = self.target(agent)
            beta = target / ('group/beta' if agent == 'pi' else 'dotfiles-group--beta')
            self.assertEqual((beta / 'resource.txt').read_text(), 'resource')
            self.assertFalse((target / 'synced').exists())
            self.assertFalse((target / 'dotfiles-synced--private').exists())
            before = self.snapshot()
            self.run_sync(agent)
            self.assertEqual(before, self.snapshot())

    def test_root_migration_preserves_foreign_and_old_worktree(self):
        old = self.root / 'old'
        self.git('worktree', 'add', '--detach', str(old))
        old_source = old / 'dotfiles/.agent/skills'
        (old_source / 'group/custom').mkdir()
        (old_source / 'group/custom/file').write_text('custom')
        old_state = self.snapshot(old_source)
        target = self.target('pi')
        target.parent.mkdir(parents=True)
        target.symlink_to(old_source)
        before = self.snapshot()
        self.run_sync('pi', ok=False)
        self.assertEqual(before, self.snapshot())
        self.skill('new')
        self.run_sync('pi', '--migrate-from', old_source, '--dry-run')
        self.assertEqual(before, self.snapshot())
        self.run_sync('pi', '--migrate-from', old_source)
        self.assertFalse(target.is_symlink())
        self.assertEqual((target / 'alpha').resolve(), self.source / 'alpha')
        self.assertEqual((target / 'group/beta').resolve(), self.source / 'group/beta')
        self.assertEqual((target / 'group/custom').resolve(), old_source / 'group/custom')
        self.assertEqual((target / 'synced').resolve(), old_source / 'synced')
        self.assertEqual((old_source / 'group/beta/SKILL.md').read_text(), 'group/beta')
        self.assertFalse((old_source / 'new').exists())
        self.assertEqual(old_state, self.snapshot(old_source))
        self.run_sync('pi')

    def test_old_untracked_personal_collision_is_preserved_for_both_agents(self):
        old = self.root / 'old'
        self.git('worktree', 'add', '--detach', str(old))
        old_source = old / 'dotfiles/.agent/skills'
        personal = old_source / 'newskill'
        personal.mkdir()
        (personal / 'SKILL.md').write_text('PERSONAL')
        self.skill('newskill')
        for agent in ('pi', 'deepseek'):
            target = self.target(agent)
            target.parent.mkdir(parents=True, exist_ok=True)
            if agent == 'pi':
                target.symlink_to(old_source)
            else:
                target.mkdir()
                (target / 'dotfiles-newskill').symlink_to(personal)
            before_home = self.snapshot()
            before_old = self.snapshot(old_source)
            for options in (('--dry-run',), ()):
                with self.subTest(agent=agent, options=options):
                    self.run_sync(agent, '--migrate-from', old_source, *options, ok=False)
                    self.assertEqual(before_home, self.snapshot())
                    self.assertEqual(before_old, self.snapshot(old_source))

    def test_deepseek_migration_and_stale_owned_cleanup(self):
        old = self.root / 'old'
        self.git('worktree', 'add', '--detach', str(old))
        old_source = old / 'dotfiles/.agent/skills'
        target = self.target('deepseek')
        target.mkdir(parents=True)
        (target / 'dotfiles-alpha').symlink_to(old_source / 'alpha')
        (target / 'personal').mkdir()
        self.run_sync('deepseek', ok=False)
        self.run_sync('deepseek', '--migrate-from', old_source)
        self.git('rm', '-r', '-q', 'dotfiles/.agent/skills/alpha')
        self.run_sync('deepseek')
        self.assertFalse((target / 'dotfiles-alpha').is_symlink())
        self.assertTrue((target / 'personal').is_dir())

    def test_collision_and_modified_link_are_unchanged(self):
        for agent in ('pi', 'deepseek'):
            self.run_sync(agent)
            target = self.target(agent)
            leaf = target / ('alpha' if agent == 'pi' else 'dotfiles-alpha')
            leaf.unlink()
            leaf.write_text('personal')
            before = self.snapshot()
            self.run_sync(agent, ok=False)
            self.assertEqual(before, self.snapshot())

    def test_normal_runtimecache_and_nested_synced_names_are_managed(self):
        self.skill('runtimecache/authored')
        self.skill('group/synced/authored')
        self.run_sync('pi')
        target = self.target('pi')
        self.assertEqual((target / 'runtimecache/authored').resolve(), self.source / 'runtimecache/authored')
        self.assertEqual((target / 'group/synced/authored').resolve(), self.source / 'group/synced/authored')

    def test_conversion_refuses_root_changed_during_staging(self):
        target = self.target('pi')
        target.parent.mkdir(parents=True)
        target.symlink_to(self.source)
        replacement = self.root / 'replacement'
        replacement.mkdir()
        (replacement / 'personal').write_text('keep')
        original_write = Path.write_text

        def write_and_replace(path, text, *args, **kwargs):
            result = original_write(path, text, *args, **kwargs)
            if path.name == '.dotfiles-managed-skills.json':
                target.unlink()
                target.symlink_to(replacement)
            return result

        sync = runpy.run_path(str(SCRIPT))['sync']
        with patch.dict(os.environ, HOME=str(self.home)), patch.object(Path, 'write_text', write_and_replace):
            with self.assertRaisesRegex(ValueError, 'changed during staging'):
                sync(self.repo, 'pi', False, None)
        self.assertTrue(target.is_symlink())
        self.assertEqual(target.resolve(), replacement)
        self.assertEqual((replacement / 'personal').read_text(), 'keep')
        self.assertEqual(list(target.parent.glob('.skills-stage-*')), [])

    def test_receipt_cannot_claim_foreign_link(self):
        self.run_sync('deepseek')
        target = self.target('deepseek')
        personal = target / 'personal'
        personal.symlink_to(self.home / 'private')
        receipt = target / '.dotfiles-managed-skills.json'
        data = json.loads(receipt.read_text())
        data['links']['personal'] = str(self.home / 'private')
        receipt.write_text(json.dumps(data))
        before = self.snapshot()
        self.run_sync('deepseek', ok=False)
        self.assertEqual(before, self.snapshot())

    def test_flattened_name_collision_is_preflight_only(self):
        self.skill('group--beta')
        before = self.snapshot()
        self.run_sync('deepseek', ok=False)
        self.assertEqual(before, self.snapshot())

    def test_modified_symlink_and_foreign_root_rejected(self):
        self.run_sync('pi')
        leaf = self.target('pi') / 'alpha'
        leaf.unlink()
        leaf.symlink_to(self.source / 'group/beta')
        before = self.snapshot()
        self.run_sync('pi', ok=False)
        self.assertEqual(before, self.snapshot())

    def test_current_root_conversion_and_stale_pi_cleanup(self):
        target = self.target('pi')
        target.parent.mkdir(parents=True)
        target.symlink_to(self.source)
        self.run_sync('pi')
        (target / 'personal').mkdir()
        self.git('rm', '-r', '-q', 'dotfiles/.agent/skills/alpha')
        self.run_sync('pi')
        self.assertFalse((target / 'alpha').is_symlink())
        self.assertTrue((target / 'personal').is_dir())
        self.assertEqual((target / 'synced').resolve(), self.source / 'synced')

    def test_foreign_git_source_rejected_even_with_migration_flag(self):
        foreign = self.root / 'foreign'
        foreign.mkdir()
        subprocess.check_call(['git', '-C', str(foreign), 'init', '-q'])
        source = foreign / 'dotfiles/.agent/skills'
        source.mkdir(parents=True)
        target = self.target('pi')
        target.parent.mkdir(parents=True)
        target.symlink_to(source)
        before = self.snapshot()
        self.run_sync('pi', '--migrate-from', source, ok=False)
        self.assertEqual(before, self.snapshot())

    def test_unowned_pi_and_symlink_parent_rejected(self):
        target = self.target('pi')
        target.mkdir(parents=True)
        (target / 'personal').mkdir()
        before = self.snapshot()
        self.run_sync('pi', ok=False)
        self.assertEqual(before, self.snapshot())
        dsh = self.home / '.dsh'
        dsh.symlink_to(target)
        before = self.snapshot()
        self.run_sync('deepseek', ok=False)
        self.assertEqual(before, self.snapshot())


if __name__ == '__main__':
    unittest.main()
