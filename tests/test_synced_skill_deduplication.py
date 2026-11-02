import importlib.util
from pathlib import Path
import tempfile
import tomllib
import unittest

spec = importlib.util.spec_from_file_location('dedup', Path(__file__).resolve().parents[1] / 'scripts/deduplicate_synced_skills.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DeduplicationTest(unittest.TestCase):
    def test_preserves_sources_and_reconciles_changed_support_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / 'config.toml'
            config.write_text('model = "example"\n')
            for bucket in ('a', 'b'):
                skill = root / 'synced' / bucket / 'morning'
                skill.mkdir(parents=True)
                (skill / 'SKILL.md').write_text('same')
                (skill / 'helper.py').write_text('same helper')
            self.assertEqual(len(module.update(root / 'synced', config)), 1)
            first = config.read_text()
            module.update(root / 'synced', config)
            self.assertEqual(first, config.read_text())
            self.assertEqual(tomllib.loads(first)['model'], 'example')
            self.assertTrue((root / 'synced/b/morning/SKILL.md').exists())
            (root / 'synced/b/morning/helper.py').write_text('different')
            self.assertEqual(module.update(root / 'synced', config), [])
            self.assertNotIn('skills', tomllib.loads(config.read_text()))

    def test_explicit_enabled_copy_is_kept_as_canonical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for bucket in ('a', 'b'):
                skill = root / bucket / 'morning'
                skill.mkdir(parents=True)
                (skill / 'SKILL.md').write_text('same')
            config = root / 'config.toml'
            import json
            selected = str((root / 'b/morning/SKILL.md').resolve())
            config.write_text('[[skills.config]]\npath = ' + json.dumps(selected) + '\nenabled = true\n')
            self.assertEqual(module.update(root, config), [str((root / 'a/morning/SKILL.md').resolve())])
            self.assertTrue(tomllib.loads(config.read_text())['skills']['config'][0]['enabled'])

    def test_document_policy_without_synced_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skills = root / 'skills'
            config = root / 'config.toml'
            config.write_text('model = "example"\n')
            for name in ('docx', 'pdf', 'xlsx', 'powerpoint'):
                folder = skills / 'productivity' / name
                folder.mkdir(parents=True)
                (folder / 'SKILL.md').write_text(name)
            disabled = module.update(skills / 'synced', config, local_skills_root=skills)
            self.assertEqual(len(disabled), 4)
            self.assertEqual(len(list(skills.rglob('SKILL.md'))), 4)
            first = config.read_text()
            module.update(skills / 'synced', config, local_skills_root=skills)
            self.assertEqual(config.read_text(), first)

    def test_all_synced_document_copies_are_disabled_even_when_different(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / 'config.toml'
            config.write_text('model = "example"\n')
            for bucket in ('a', 'b'):
                for name in ('docs', 'docx', 'pdf', 'xlsx', 'pptx'):
                    folder = root / 'synced' / bucket / name
                    folder.mkdir(parents=True)
                    (folder / 'SKILL.md').write_text(bucket + name)
            module.update(root / 'synced', config, True)
            self.assertEqual(config.read_text(), 'model = "example"\n')
            self.assertEqual(len(module.update(root / 'synced', config)), 10)
            self.assertEqual(len(list((root / 'synced').rglob('SKILL.md'))), 10)

    def test_document_enabled_override_fails_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / 'synced/a/docs'
            folder.mkdir(parents=True)
            (folder / 'SKILL.md').write_text('docs')
            config = root / 'config.toml'
            import json
            original = '[[skills.config]]\npath = ' + json.dumps(str((folder / 'SKILL.md').resolve())) + '\nenabled = true\n'
            config.write_text(original)
            with self.assertRaisesRegex(ValueError, 'explicitly enabled'):
                module.update(root / 'synced', config)
            self.assertEqual(config.read_text(), original)

    def test_existing_disabled_override_and_old_managed_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / 'synced/a/docs'
            folder.mkdir(parents=True)
            (folder / 'SKILL.md').write_text('docs')
            config = root / 'config.toml'
            import json
            path = str((folder / 'SKILL.md').resolve())
            manual = '[[skills.config]]\npath = ' + json.dumps(path) + '\nenabled = false\n'
            config.write_text(manual + module.BEGIN + '\n[[skills.config]]\npath = "/old/copy/SKILL.md"\nenabled = false\n' + module.END + '\n')
            self.assertEqual(module.update(root / 'synced', config), [])
            self.assertEqual(tomllib.loads(config.read_text())['skills']['config'], [{'path': path, 'enabled': False}])

    def test_dry_run_and_user_configuration_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / 'config.toml'
            original = 'model = "example"\n'
            config.write_text(original)
            module.update(root, config, True)
            self.assertEqual(config.read_text(), original)


if __name__ == '__main__':
    unittest.main()
