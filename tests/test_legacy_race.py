"""Reproduce the pre-fix TOCTOU defects against the frozen legacy collector.

Each test mutates the site at the exact point where the old code had finished
``resolve``/``stat`` but not yet consumed the bytes.  The legacy module has no
seams of its own, so its single read primitive (``Path.read_text``) is
monkeypatched.  The same scenario is then replayed against the fixed
``recall_collect.site`` to prove the defect is closed.
"""
import importlib.util
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from recall_collect import site
from recall_collect.site import MAX_SIZE

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'fixtures' / 'site'
LEGACY_PATH = Path(__file__).resolve().parent / 'legacy_site.py'

spec = importlib.util.spec_from_file_location('legacy_site_frozen', LEGACY_PATH)
legacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy)

VALID = ('<html><body><script type="application/json">'
         '{"recall_id": "R1", "published": "2026-01-03", '
         '"product": "Synthetic oat bar", "lots": ["L1"], '
         '"reason": "Undeclared synthetic allergen"}'
         '</script></body></html>')
EVIL = ('<html><body><script type="application/json">'
        '{"recall_id": "EVIL", "published": "2026-02-04", '
        '"product": "Stolen secret", "lots": ["X1"], '
        '"reason": "captured bytes escaped the root"}'
        '</script></body></html>')
GROWN = ('<script type="application/json">'
         '{"recall_id": "GROWN", "published": "2026-03-05", '
         '"product": "Oversized page", "lots": ["G1"], '
         '"reason": "grew past the cap after stat"}</script>')


class LegacyRaceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'site'
        shutil.copytree(FIX, self.root)
        self.minimal_site()

    def minimal_site(self):
        (self.root / 'index.html').write_text(
            '<a rel="notice" href="a.html">A</a>', encoding='utf-8')
        (self.root / 'a.html').write_text(VALID, encoding='utf-8')
        for name in ('a-copy.html', 'b.html', 'page2.html'):
            (self.root / name).unlink()

    def patch_legacy_read(self, mutate):
        """Run ``mutate`` inside the legacy read_text of each page, once."""
        fired = set()
        real_read_text = Path.read_text

        def racing_read_text(self, *args, **kwargs):
            if self not in fired:
                fired.add(self)
                mutate(self)
            return real_read_text(self, *args, **kwargs)

        legacy.Path.read_text = racing_read_text
        self.addCleanup(setattr, legacy.Path, 'read_text', real_read_text)


class LegacySymlinkSwapTest(LegacyRaceCase):
    def test_legacy_parses_out_of_root_bytes_fixed_pipeline_rejects(self):
        secret = self.base / 'secret.html'
        secret.write_text(EVIL, encoding='utf-8')
        index = self.root / 'index.html'

        def mutate(path):
            if path == index:
                index.unlink()
                index.symlink_to(secret)

        self.patch_legacy_read(mutate)
        # Old defect: the symlink replaces the page between resolve and read,
        # so out-of-root bytes are parsed as a recall. The swapped index has no
        # outgoing links, so the crawl reaches just that one page.
        records, pages = legacy.crawl(self.root)
        self.assertIn('EVIL', records)
        self.assertEqual(pages, 1)

        # Replay on the fixed pipeline: rebuild a clean site first (the legacy
        # run left index.html as an out-of-root symlink). The seam fires at the
        # equivalent point (post-validation, pre-read) and the replacement is
        # refused.
        shutil.rmtree(self.root)
        shutil.copytree(FIX, self.root)
        self.minimal_site()
        index = self.root / 'index.html'

        def fixed_swap(root, path):
            if path == index.resolve():
                index.unlink()
                index.symlink_to(secret)

        original = site._after_validate
        self.addCleanup(setattr, site, '_after_validate', original)
        site._after_validate = fixed_swap
        with self.assertRaises((OSError, ValueError)):
            site.crawl(self.root)


class LegacyGrowthTest(LegacyRaceCase):
    def test_legacy_bypasses_byte_cap_fixed_pipeline_rejects(self):
        page = self.root / 'a.html'
        page.write_bytes(VALID.encode('utf-8') + b' ' * (
            MAX_SIZE - len(VALID.encode('utf-8'))))
        self.assertEqual(page.stat().st_size, MAX_SIZE)

        def mutate(path):
            if path == page:
                # Grows strictly beyond the cap after the stat check passed.
                with open(page, 'ab') as fh:
                    fh.write(GROWN.encode('utf-8'))

        self.patch_legacy_read(mutate)
        # Old defect: read_text reads the grown file; the appended notice is
        # parsed even though the page now exceeds 1 MB.
        records, _ = legacy.crawl(self.root)
        self.assertIn('GROWN', records)
        self.assertGreater(page.stat().st_size, MAX_SIZE)

        # Fixed pipeline: inject the same growth between its fstat check and
        # its bounded read; the cap is enforced on the bytes actually read.
        def fixed_grow(root, path):
            if path == page.resolve():
                with open(page, 'ab') as fh:
                    fh.write(GROWN.encode('utf-8'))

        original = site._after_validate
        self.addCleanup(setattr, site, '_after_validate', original)
        site._after_validate = fixed_grow
        with self.assertRaises(ValueError):
            site.crawl(self.root)


class LegacyParentSwapTest(LegacyRaceCase):
    def test_legacy_follows_swapped_parent_fixed_pipeline_rejects(self):
        evil_site = self.base / 'evil-site'
        evil_site.mkdir()
        (evil_site / 'index.html').write_text(EVIL, encoding='utf-8')
        (evil_site / 'a.html').write_text(EVIL, encoding='utf-8')

        def mutate(path):
            if path == self.root / 'index.html':
                moved = self.base / 'site-real'
                os.replace(self.root, moved)
                self.root.symlink_to(evil_site, target_is_directory=True)

        self.patch_legacy_read(mutate)
        # Old defect: swapping the root directory to an out-of-root link after
        # validation makes every subsequent read parse external bytes.
        records, _ = legacy.crawl(self.root)
        self.assertIn('EVIL', records)

        # Rebuild a clean site for the fixed replay (the root was moved and a
        # directory symlink with the same name left behind).
        if self.root.is_symlink():
            self.root.unlink()
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.copytree(FIX, self.root)
        self.minimal_site()
        index = self.root / 'index.html'

        def fixed_swap(root, path):
            if path == index.resolve():
                moved = self.base / 'site-real-2'
                os.replace(self.root, moved)
                self.root.symlink_to(evil_site, target_is_directory=True)

        original = site._after_validate
        self.addCleanup(setattr, site, '_after_validate', original)
        site._after_validate = fixed_swap
        with self.assertRaises(ValueError):
            site.crawl(self.root)


class LegacyDivergingReadsTest(LegacyRaceCase):
    def test_legacy_second_read_decides_detail_verdict_fixed_reads_once(self):
        # First read carries a valid notice; the second read (the detail
        # verification loop) returns a page with no JSON block.
        page = self.root / 'a.html'
        valid_bytes = VALID.encode('utf-8')
        empty = b'<html>no data</html>'
        reads = {'n': 0}
        real_read_text = Path.read_text

        def diverging_read_text(self, *args, **kwargs):
            if self == page:
                reads['n'] += 1
                if reads['n'] == 2:
                    return empty.decode('utf-8')
            return real_read_text(self, *args, **kwargs)

        legacy.Path.read_text = diverging_read_text
        self.addCleanup(setattr, legacy.Path, 'read_text', real_read_text)
        # Old defect: two reads of one path disagree and the crawl fails on
        # content that was never part of the parsed record set.
        with self.assertRaises(ValueError):
            legacy.crawl(self.root)
        self.assertEqual(reads['n'], 2)

        # Fixed pipeline: one capture per canonical page; no second read exists.
        records, pages = site.crawl(self.root)
        self.assertEqual(pages, 2)
        self.assertEqual(set(records), {'R1'})


if __name__ == '__main__':
    unittest.main()
