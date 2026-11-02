#!/usr/bin/env python3
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


SKILL = Path(__file__).resolve().parents[1] / 'dotfiles/.agent/skills/html-plan'


class HtmlPlanTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.historical = 'export const HTML_PLAN_HISTORY_FIXTURE = 11;'
        self.working = 'export const HTML_PLAN_WORKTREE_FIXTURE = 22;'
        (self.repo / 'record.ts').write_text(self.historical + '\n')
        self.git('init', '-q')
        self.git('add', 'record.ts')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit', '-qm', 'fixture')
        (self.repo / 'record.ts').write_text(self.working + '\n')
        (self.repo / 'new.ts').write_text(self.working + '\n')

    def git(self, *args):
        return subprocess.check_output(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                                        '-C', str(self.repo), *args], text=True)

    def pack(self, tag, source='record.ts', ref=None, lint_only=False):
        attribute = f' ref="{ref}"' if ref is not None else ''
        if tag == 'doc-calls':
            block = (f'<doc-calls{attribute}><script type="text/plain">\n'
                     f'+ **readRecord**() @ {source}:1\n</script></doc-calls>')
        else:
            block = f'<{tag} lang="ts" src="{source}"{attribute} lines="1-1"></{tag}>'
        template = (SKILL / 'examples/scheduled-send.html').read_text()
        page, count = re.subn(fr'<{tag}\b[^>]*>.*?</{tag}>', lambda _: block,
                             template, count=1, flags=re.S)
        self.assertEqual(count, 1)
        input_path = self.repo / 'plan.html'
        output_path = self.repo / 'plan.packed.html'
        input_path.write_text(page)
        output_path.unlink(missing_ok=True)
        command = ['node', str(SKILL / 'runtime/pack.mjs'), str(input_path),
                   '--root', str(self.repo), '-o', str(output_path)]
        if lint_only:
            command.append('--lint-only')
        result = subprocess.run(command, capture_output=True, text=True)
        return result, output_path

    def test_missing_requested_revision_or_blob_fails_without_output(self):
        for tag in ('doc-code', 'doc-schema', 'doc-calls'):
            for source, ref in (('record.ts', 'missing-ref'), ('new.ts', 'HEAD')):
                for lint_only in (False, True):
                    with self.subTest(tag=tag, source=source, ref=ref, lint_only=lint_only):
                        result, output = self.pack(tag, source, ref, lint_only)
                        self.assertNotEqual(result.returncode, 0, result.stdout)
                        self.assertIn('unable to read the requested git blob',
                                      result.stdout + result.stderr)
                        self.assertFalse(output.exists())

    def test_requested_revision_and_working_tree_remain_distinct(self):
        for tag in ('doc-code', 'doc-schema', 'doc-calls'):
            for ref in ('HEAD', None):
                with self.subTest(tag=tag, ref=ref):
                    result, output = self.pack(tag, ref=ref)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    page = output.read_text()
                    expected, excluded = ((self.historical, self.working) if ref else
                                          (self.working, self.historical))
                    self.assertIn(expected, page)
                    self.assertNotIn(excluded, page)

    def test_secret_file_and_outside_symlink_are_rejected(self):
        (self.repo / '.env').write_text('FIXTURE_VALUE=not-a-real-secret\n')
        outside = self.root / 'outside.ts'
        outside.write_text('export const OUTSIDE_FIXTURE = 1;\n')
        (self.repo / 'outside-link.ts').symlink_to(outside)
        for source in ('.env', 'outside-link.ts'):
            with self.subTest(source=source):
                result, output = self.pack('doc-code', source)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
