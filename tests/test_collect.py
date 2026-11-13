import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from recall_collect.site import crawl, notice
from recall_collect.store import collect

FIX=Path(__file__).resolve().parents[1]/'fixtures/site'
class CollectorTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.site=self.root/'site'; shutil.copytree(FIX,self.site)
        self.db=self.root/'out.sqlite'
    def test_business_and_repeat(self):
        self.assertEqual(collect(self.site,self.db),dict(pages=5,source_unique_recalls=2,inserted=2,stored=2))
        self.assertEqual(collect(self.site,self.db)['inserted'],0)
        with sqlite3.connect(self.db) as db:
            self.assertEqual(json.loads(db.execute("SELECT lots_json FROM recalls WHERE recall_id='R001'").fetchone()[0]),['L1','L2'])
            self.assertEqual(db.execute('SELECT count(*) FROM sources').fetchone()[0],3)
    def test_conflict_preserves_database(self):
        collect(self.site,self.db)
        p=self.site/'a-copy.html';p.write_text(p.read_text().replace('Synthetic oat bar','Different'))
        with self.assertRaises(ValueError): collect(self.site,self.db)
        with sqlite3.connect(self.db) as db:self.assertEqual(db.execute('SELECT count(*) FROM recalls').fetchone()[0],2)
    def test_escape(self):
        (self.site/'index.html').write_text('<a rel="next" href="../outside.html">bad</a>')
        (self.root/'outside.html').write_text('outside')
        with self.assertRaises(ValueError):crawl(self.site)
    def test_external_link(self):
        (self.site/'index.html').write_text('<a rel="notice" href="https://example.invalid/x">bad</a>')
        with self.assertRaises(ValueError):crawl(self.site)
    def test_missing_detail(self):
        (self.site/'b.html').write_text('<html>no data</html>')
        with self.assertRaises(ValueError):crawl(self.site)
    def test_symlink_escape(self):
        (self.root/'secret.html').write_text('private')
        (self.site/'escape.html').symlink_to(self.root/'secret.html')
        (self.site/'index.html').write_text('<a rel="next" href="escape.html">bad</a>')
        with self.assertRaises(ValueError):crawl(self.site)
    def test_invalid_date(self):
        d={'recall_id':'x','published':'2026-02-30','product':'x','lots':['a'],'reason':'x'}
        with self.assertRaises(ValueError): notice(json.dumps(d))
    def test_stored_conflict_rolls_back_batch(self):
        collect(self.site,self.db)
        with sqlite3.connect(self.db) as db:db.execute("UPDATE recalls SET product='Other' WHERE recall_id='R002'")
        with self.assertRaises(ValueError):collect(self.site,self.db)
        with sqlite3.connect(self.db) as db:self.assertEqual(db.execute('SELECT count(*) FROM recalls').fetchone()[0],2)
