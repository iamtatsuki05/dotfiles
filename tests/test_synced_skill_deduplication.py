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
                skill = root / 'synced' / bucket / 'docs'
                skill.mkdir(parents=True)
                (skill / 'SKILL.md').write_text('same')
                (skill / 'helper.py').write_text('same helper')
            self.assertEqual(len(module.update(root / 'synced', config)), 1)
            first = config.read_text()
            module.update(root / 'synced', config)
            self.assertEqual(first, config.read_text())
            self.assertEqual(tomllib.loads(first)['model'], 'example')
            self.assertTrue((root / 'synced/b/docs/SKILL.md').exists())
            (root / 'synced/b/docs/helper.py').write_text('different')
            self.assertEqual(module.update(root / 'synced', config), [])
            self.assertNotIn('skills', tomllib.loads(config.read_text()))

    def test_explicit_enabled_copy_is_kept_as_canonical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for bucket in ('a', 'b'):
                skill = root / bucket / 'docs'
                skill.mkdir(parents=True)
                (skill / 'SKILL.md').write_text('same')
            config = root / 'config.toml'
            import json
            selected = str((root / 'b/docs/SKILL.md').resolve())
            config.write_text('[[skills.config]]\npath = ' + json.dumps(selected) + '\nenabled = true\n')
            self.assertEqual(module.update(root, config), [str((root / 'a/docs/SKILL.md').resolve())])
            self.assertTrue(tomllib.loads(config.read_text())['skills']['config'][0]['enabled'])

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
