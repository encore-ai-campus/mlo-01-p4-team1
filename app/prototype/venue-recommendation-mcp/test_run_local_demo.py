"""Checks for selecting local-only demo data and isolating its state."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import run_local_demo


class DemoCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix='venue-demo-catalog-')
        self.addCleanup(self.temporary.cleanup)
        self.catalog = Path(self.temporary.name) / 'research_preview.json'
        self.catalog.write_text('{}', encoding='utf-8')
        self.patch_catalog = patch.object(run_local_demo, 'RESEARCH_CATALOG', self.catalog)
        self.patch_catalog.start()
        self.addCleanup(self.patch_catalog.stop)

    def test_bundled_research_requires_selection_and_separate_state(self):
        path, state = run_local_demo.demo_catalog(None, True, None)
        self.assertEqual(path, run_local_demo.RESEARCH_CATALOG.resolve())
        self.assertEqual(state, (run_local_demo.APP / '.local' / 'venue-research-demo').resolve())

    def test_default_preserves_sample(self):
        path, state = run_local_demo.demo_catalog(None, False, None)
        self.assertIsNone(path)
        self.assertEqual(state, (run_local_demo.APP / '.local' / 'venue-mcp').resolve())

    def test_missing_bundle_fails_instead_of_silent_sample(self):
        with patch.object(run_local_demo, 'RESEARCH_CATALOG', Path('nonexistent-research-catalog.json')):
            with self.assertRaisesRegex(ValueError, 'missing'):
                run_local_demo.demo_catalog(None, True, None)

    def test_custom_catalog_requires_explicit_isolated_state(self):
        with self.assertRaisesRegex(ValueError, 'separate --state-dir'):
            run_local_demo.demo_catalog(run_local_demo.RESEARCH_CATALOG, False, None)
        with self.assertRaisesRegex(ValueError, 'either'):
            run_local_demo.demo_catalog(run_local_demo.RESEARCH_CATALOG, True, Path('state'))
        catalog, state = run_local_demo.demo_catalog(run_local_demo.RESEARCH_CATALOG, False, Path('custom-state'))
        self.assertEqual(catalog, run_local_demo.RESEARCH_CATALOG.resolve())
        self.assertEqual(state, Path('custom-state').resolve())


if __name__ == '__main__':
    unittest.main()
