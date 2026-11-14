"""Deterministic races against the fixed capture pipeline.

Every mutation is injected through the synchronization seams in
``recall_collect.site`` (or by monkeypatching its stdlib wrappers), so the
attacker action runs at an exact point inside one capture call.  No threads,
no sleeps and no reliance on repeated ``stat`` shrinking a timing window.
"""
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from recall_collect import site
from recall_collect.site import MAX_SIZE, _READ_CHUNK, capture, crawl
from recall_collect.store import collect

FIX = Path(__file__).resolve().parents[1] / 'fixtures' / 'site'

VALID = ('<html><body><script type="application/json">'
         '{"recall_id": "R1", "published": "2026-01-03", '
         '"product": "Synthetic oat bar", "lots": ["L1"], '
         '"reason": "Undeclared synthetic allergen"}'
         '</script></body></html>')
# A record that must never reach the parser from an out-of-root file.
EVIL = ('<html><body><script type="application/json">'
        '{"recall_id": "EVIL", "published": "2026-02-04", '
        '"product": "Stolen secret", "lots": ["X1"], '
        '"reason": "captured bytes escaped the root"}'
        '</script></body></html>')


class CaptureCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'site'
        shutil.copytree(FIX, self.root)
        # Freeze the original canonical root: later tests deliberately turn
        # self.root itself into an out-of-root symlink, and resolve() would
        # then follow it to the attacker directory.
        self.root_real = self.root.resolve()
        self.db = self.base / 'out.sqlite'

    def minimal_site(self):
        """Replace the fixture with one reachable valid detail page."""
        (self.root / 'index.html').write_text(
            '<a rel="notice" href="a.html">A</a>', encoding='utf-8')
        (self.root / 'a.html').write_text(VALID, encoding='utf-8')
        for name in ('a-copy.html', 'b.html', 'page2.html'):
            (self.root / name).unlink()

    def assert_evil_marker_stays_outside_root(self):
        # The EVIL payload may exist on disk, but only in files whose real
        # location is outside the original site root; the crawl never returned it.
        for path in self.base.rglob('*'):
            if path.is_file() and 'EVIL' in path.read_text(encoding='utf-8'):
                self.assertFalse(
                    path.resolve().is_relative_to(self.root_real),
                    'out-of-root payload was materialized inside the root')


class FixedPipelineTest(CaptureCase):
    def test_in_root_symlink_alias_is_one_page(self):
        # Legitimate in-root symlink: still allowed, still one canonical page.
        link = self.root / 'alias.html'
        link.symlink_to('a.html')
        (self.root / 'index.html').write_text(
            '<a rel="notice" href="alias.html">alias</a>', encoding='utf-8')
        records, pages = crawl(self.root)
        self.assertEqual(pages, 2)
        self.assertEqual(records['R001']['sources'],
                         ['a.html'])  # canonical location, not the alias name

    def test_in_root_absolute_href_keeps_source_path_semantics(self):
        target = (self.root / 'a.html').resolve()
        (self.root / 'index.html').write_text(
            '<a rel="notice" href="%s">abs</a>' % target, encoding='utf-8')
        records, pages = crawl(self.root)
        self.assertEqual(pages, 2)
        self.assertEqual(records['R001']['sources'], ['a.html'])

    def test_page_replaced_by_out_of_root_symlink_after_check_is_rejected(self):
        self.minimal_site()
        secret = self.base / 'secret.html'
        secret.write_text(EVIL, encoding='utf-8')
        index = self.root / 'index.html'

        def swap(root, path):
            if path == index.resolve():
                index.unlink()
                index.symlink_to(secret)

        original = site._after_validate
        self.addCleanup(setattr, site, '_after_validate', original)
        site._after_validate = swap
        # O_NOFOLLOW raises at open, or the post-read samefile check rejects
        # the renamed target; either way no external byte is parsed.
        with self.assertRaises((OSError, ValueError)):
            crawl(self.root)
        self.assert_evil_marker_stays_outside_root()

    def test_parent_directory_replaced_by_out_of_root_link_is_rejected(self):
        self.minimal_site()
        evil_site = self.base / 'evil-site'
        evil_site.mkdir()
        (evil_site / 'index.html').write_text(EVIL, encoding='utf-8')
        moved_root = self.base / 'site-real'
        index = self.root / 'index.html'

        def swap(root, path):
            if path == index.resolve():
                os.replace(self.root, moved_root)
                self.root.symlink_to(evil_site, target_is_directory=True)

        original = site._after_validate
        self.addCleanup(setattr, site, '_after_validate', original)
        site._after_validate = swap
        with self.assertRaises(ValueError):
            crawl(self.root)
        self.assert_evil_marker_stays_outside_root()

    def test_growth_past_cap_during_read_is_rejected_without_parsing(self):
        self.minimal_site()
        index = self.root / 'index.html'
        index.write_bytes(b'<a rel="next" href="a.html">x</a>' + b' ' * (
            MAX_SIZE - 40))
        real_read = os.read
        grown = {'done': False}

        def growing_read(fd, n):
            chunk = real_read(fd, n)
            if (not grown['done'] and chunk
                    and os.fstat(fd).st_size >= MAX_SIZE):
                # First chunk returned: make the file exceed the cap while the
                # descriptor is still open.  No sleep: the next read observes
                # the growth deterministically.
                grown['done'] = True
                with open(index, 'ab') as fh:
                    fh.write(b'<script type="application/json">'
                             + EVIL.encode('utf-8') + b'</script>')
            return real_read(fd, n)

        original_read = site.os.read
        self.addCleanup(setattr, site.os, 'read', original_read)
        site.os.read = growing_read
        with self.assertRaises(ValueError):
            crawl(self.root)
        self.assert_evil_marker_stays_outside_root()

    def test_static_cap_boundary_accepts_exactly_one_megabyte(self):
        page = self.root / 'solo.html'
        # Pad inside an HTML comment so the parser ignores every padding byte.
        frame = b'<html><!--' + b'--></html>'
        body = b'<html><!--' + b'p' * (MAX_SIZE - len(frame)) + b'--></html>'
        self.assertEqual(len(body), MAX_SIZE)
        page.write_bytes(body)
        self.assertEqual(len(capture(self.root.resolve(), page.resolve())),
                         MAX_SIZE)
        page.write_bytes(body + b'x')
        with self.assertRaises(ValueError):
            capture(self.root.resolve(), page.resolve())

    def test_utf8_multibyte_character_split_across_read_chunks(self):
        # 64 KiB read chunks: place a 3-byte CJK character so its bytes straddle
        # a chunk boundary; capture must reassemble and decode it whole.
        marker = '中'  # 3 UTF-8 bytes
        prefix = 'x' * (_READ_CHUNK - 1)
        text = ('<html>' + prefix + marker + '尾</html>')
        self.assertEqual(len(text.encode('utf-8')), len('<html>') +
                         _READ_CHUNK - 1 + 3 + len('尾'.encode('utf-8'))
                         + len('</html>'))
        page = self.root / 'cjk.html'
        page.write_text(text, encoding='utf-8')
        decoded = capture(self.root.resolve(), page.resolve())
        self.assertEqual(decoded, text)
        self.assertIn(marker + '尾', decoded)
        # Drop exactly the last byte of the boundary-spanning character so the
        # reassembled stream ends on an incomplete multibyte sequence.
        raw = text.encode('utf-8')
        cut = len('<html>') + _READ_CHUNK - 1 + 2  # keep only 2 of 3 bytes
        self.assertEqual(raw[cut - 2:cut + 1], marker.encode('utf-8'))
        page.write_bytes(raw[:cut])
        with self.assertRaises(UnicodeDecodeError):
            capture(self.root.resolve(), page.resolve())

    def test_each_canonical_page_is_opened_and_read_exactly_once(self):
        # Fixture graph: index -> page2 -> index (cycle), two aliases of a.html.
        opens = []
        real_open = site._open_candidate

        def counting_open(path):
            opens.append(path)
            return real_open(path)

        self.addCleanup(setattr, site, '_open_candidate', real_open)
        site._open_candidate = counting_open
        reads = {'n': 0}
        real_read = site.os.read

        def counting_read(fd, n):
            reads['n'] += 1
            return real_read(fd, n)

        self.addCleanup(setattr, site.os, 'read', real_read)
        site.os.read = counting_read
        records, pages = crawl(self.root)
        self.assertEqual(pages, 5)
        self.assertEqual(len(opens), 5)  # one open per canonical page...
        self.assertEqual(len(set(opens)), 5)  # ...no page opened twice
        # Detail non-empty check reuses captured bytes: the two detail pages
        # were opened once, not re-read in the final verification loop.
        self.assertEqual(sorted(records['R001']['sources']),
                         ['a-copy.html', 'a.html'])

    def test_alias_and_cycle_still_count_as_one_page(self):
        records, pages = crawl(self.root)
        self.assertEqual(pages, 5)
        self.assertEqual(set(records), {'R001', 'R002'})

    def test_failed_crawl_leaves_existing_database_unchanged(self):
        self.assertEqual(collect(self.root, self.db)['stored'], 2)
        # A capture-time rejection aborts before the database is opened.
        index = self.root / 'index.html'
        secret = self.base / 'secret.html'
        secret.write_text(EVIL, encoding='utf-8')

        def swap(root, path):
            if path == index.resolve():
                index.unlink()
                index.symlink_to(secret)

        original = site._after_validate
        self.addCleanup(setattr, site, '_after_validate', original)
        site._after_validate = swap
        with self.assertRaises((OSError, ValueError)):
            collect(self.root, self.db)
        with sqlite3.connect(self.db) as db:
            self.assertEqual(
                db.execute('SELECT count(*) FROM recalls').fetchone()[0], 2)
            self.assertEqual(
                db.execute('SELECT count(*) FROM sources').fetchone()[0], 3)

    def test_original_site_rerun_is_byte_stable(self):
        first, pages = crawl(self.root)
        second, pages2 = crawl(self.root)
        self.assertEqual((pages, pages2), (5, 5))
        self.assertEqual(first, second)
        self.assertEqual(collect(self.root, self.db)['inserted'], 2)
        self.assertEqual(collect(self.root, self.db)['inserted'], 0)


if __name__ == '__main__':
    unittest.main()
