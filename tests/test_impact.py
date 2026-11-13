import hashlib
import json
import os
import os.path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from recall_collect.store import collect
from recall_collect.impact import (
    ImpactBuildError, build_impact_mart, open_recall_db, read_recall_tables)
from recall_collect.impact.lineage import propagate, validate_dag

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
SITE = ROOT / 'fixtures' / 'site'
FACTORY = ROOT / 'fixtures' / 'factory'

PRODUCTS = [
    ('product_id', 'notice_product'),
    ('OAT', 'Synthetic oat bar'),
    ('SOUP', 'Synthetic soup'),
    ('FIN', 'Synthetic finisher'),
]
LOTS = [
    ('product_id', 'lot_code'),
    ('OAT', 'L1'), ('OAT', 'L2'), ('OAT', 'SAFE'),
    ('SOUP', 'B9'),
    ('FIN', 'D'), ('FIN', 'E'), ('FIN', 'F'),
]
MEMBERS = [
    ('process_id', 'role', 'product_id', 'lot_code'),
    ('P1', 'input', 'OAT', 'L1'),
    ('P1', 'output', 'FIN', 'D'),
    ('P2', 'input', 'OAT', 'L1'),
    ('P2', 'input', 'OAT', 'L2'),
    ('P2', 'output', 'FIN', 'E'),
    ('P3', 'input', 'FIN', 'D'),
    ('P3', 'input', 'FIN', 'E'),
    ('P3', 'input', 'SOUP', 'B9'),
    ('P3', 'output', 'FIN', 'F'),
]
SHIPMENTS = [
    ('shipment_id', 'customer_id', 'product_id', 'lot_code', 'units'),
    ('S1', 'CUST-1', 'FIN', 'F', '10'),
    ('S2', 'CUST-1', 'FIN', 'D', '5'),
    ('S3', 'CUST-1', 'OAT', 'SAFE', '7'),
]


def write_csv(path, rows, newline='\n'):
    # The newline argument lets tests exercise CRLF/LF byte-level differences.
    with open(path, 'w', encoding='utf-8', newline=newline) as fh:
        for row in rows:
            fh.write(','.join(row) + newline)


class MartCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.db = self.dir / 'recalls.sqlite'
        collect(SITE, self.db)
        self.products = self.dir / 'products.csv'
        self.lots = self.dir / 'lots.csv'
        self.members = self.dir / 'process-members.csv'
        self.shipments = self.dir / 'shipments.csv'
        write_csv(self.products, PRODUCTS)
        write_csv(self.lots, LOTS)
        write_csv(self.members, MEMBERS)
        write_csv(self.shipments, SHIPMENTS)
        self.out = self.dir / 'mart.sqlite'

    def build(self, output=None):
        return build_impact_mart(
            self.db, self.products, self.lots, self.members,
            self.shipments, output or self.out)

    def connect(self):
        db = sqlite3.connect(self.out)
        db.row_factory = sqlite3.Row
        return db

    def build_and_connect(self):
        self.build()
        return self.connect()


class MainCounterexampleTest(MartCase):
    def test_expected_roots_resolutions_and_facts(self):
        summary = self.build()
        self.assertEqual(summary, {
            'format': 'recall-impact-mart/1',
            'output': str(self.out),
            'recalls': 2,
            'notice_lot_rows': 3,
            'matched_roots': 3,
            'propagation_facts': 9,
            'shipments_total': 3,
            'shipments_linked': 2,
            'shipments_not_linked': 1,
        })
        db = self.connect()
        # Independent expected relation set, not re-derived via the views.
        expected_resolutions = {
            ('R001', 'Synthetic oat bar', 'L1', 'OAT', 'L1', 'matched'),
            ('R001', 'Synthetic oat bar', 'L2', 'OAT', 'L2', 'matched'),
            ('R002', 'Synthetic soup', 'B9', 'SOUP', 'B9', 'matched'),
        }
        actual = {tuple(r) for r in db.execute(
            'SELECT recall_id,notice_product,notice_lot,product_id,lot_code,'
            'status FROM notice_lot_resolutions')}
        self.assertEqual(actual, expected_resolutions)

        expected_facts = {
            # R001 / OAT-L1
            ('R001', 'OAT', 'L1', 'OAT', 'L1', 0),
            ('R001', 'OAT', 'L1', 'FIN', 'D', 1),
            ('R001', 'OAT', 'L1', 'FIN', 'E', 1),
            ('R001', 'OAT', 'L1', 'FIN', 'F', 2),
            # R001 / OAT-L2
            ('R001', 'OAT', 'L2', 'OAT', 'L2', 0),
            ('R001', 'OAT', 'L2', 'FIN', 'E', 1),
            ('R001', 'OAT', 'L2', 'FIN', 'F', 2),
            # R002 / SOUP-B9
            ('R002', 'SOUP', 'B9', 'SOUP', 'B9', 0),
            ('R002', 'SOUP', 'B9', 'FIN', 'F', 1),
        }
        actual_facts = {tuple(r) for r in db.execute(
            'SELECT recall_id,root_product_id,root_lot_code,product_id,'
            'lot_code,hops FROM propagation_facts')}
        self.assertEqual(actual_facts, expected_facts)
        self.assertEqual(len(actual_facts), 9)

    def test_r001_l1_to_fin_f_uses_p1_then_p3(self):
        db = self.build_and_connect()
        row = db.execute(
            "SELECT * FROM propagation_facts WHERE recall_id='R001' "
            "AND root_lot_code='L1' AND product_id='FIN' AND lot_code='F'"
        ).fetchone()
        self.assertEqual(row['hops'], 2)
        self.assertEqual(row['via_process'], 'P3')
        self.assertEqual((row['predecessor_product_id'],
                          row['predecessor_lot_code']), ('FIN', 'D'))
        # Reconstruct the complete canonical path with a recursive CTE that
        # follows the stored predecessor chain of this one root.
        path = db.execute(
            "WITH RECURSIVE chain(hop, p, l, via, pred_p, pred_l) AS ("
            "  SELECT hops, product_id, lot_code, via_process, "
            "         predecessor_product_id, predecessor_lot_code "
            "  FROM propagation_facts "
            "  WHERE recall_id='R001' AND root_product_id='OAT' "
            "    AND root_lot_code='L1' AND product_id='FIN' AND lot_code='F' "
            "  UNION ALL "
            "  SELECT f.hops, f.product_id, f.lot_code, f.via_process, "
            "         f.predecessor_product_id, f.predecessor_lot_code "
            "  FROM propagation_facts f JOIN chain c "
            "    ON f.recall_id='R001' AND f.root_product_id='OAT' "
            "   AND f.root_lot_code='L1' "
            "   AND f.product_id=c.pred_p AND f.lot_code=c.pred_l "
            "   AND f.hops=c.hop-1 "
            ") SELECT hop, p, l, via FROM chain ORDER BY hop").fetchall()
        self.assertEqual([tuple(r) for r in path], [
            (0, 'OAT', 'L1', None),
            (1, 'FIN', 'D', 'P1'),
            (2, 'FIN', 'F', 'P3')])
        steps = db.execute(
            "SELECT json_extract(step.value,'$[0]'),"
            "       json_extract(step.value,'$[1]'),"
            "       json_extract(step.value,'$[2]') "
            "FROM logic_snapshot, "
            "json_each(json_extract(canonical_json,'$.propagation_facts')) f,"
            "json_each(json_extract(f.value,'$.canonical_steps')) step "
            "WHERE json_extract(f.value,'$.recall_id')='R001' "
            "AND json_extract(f.value,'$.root')=JSON_ARRAY('OAT','L1') "
            "AND json_extract(f.value,'$.target')=JSON_ARRAY('FIN','F')"
        ).fetchall()
        self.assertEqual([tuple(r) for r in steps],
                         [('P1', 'FIN', 'D'), ('P3', 'FIN', 'F')])

    def test_fin_f_keeps_three_distinct_root_bases(self):
        db = self.build_and_connect()
        rows = db.execute(
            "SELECT recall_id,root_product_id,root_lot_code,hops "
            "FROM propagation_facts WHERE product_id='FIN' AND lot_code='F' "
            "ORDER BY recall_id,root_lot_code").fetchall()
        self.assertEqual([tuple(r) for r in rows], [
            ('R001', 'OAT', 'L1', 2),
            ('R001', 'OAT', 'L2', 2),
            ('R002', 'SOUP', 'B9', 1),
        ])

    def test_shipment_granularity_and_two_summaries(self):
        db = self.build_and_connect()
        # shipment_id/recall_id grain: S1 once per recall even though R001
        # reaches it through two roots and several paths.
        impact = {tuple(r) for r in db.execute(
            'SELECT shipment_id,recall_id,units '
            'FROM shipment_recall_impact')}
        self.assertEqual(impact, {
            ('S1', 'R001', '10'), ('S1', 'R002', '10'),
            ('S2', 'R001', '5')})
        recall_view = {tuple(r) for r in db.execute(
            'SELECT recall_id,shipments_affected,units_total '
            'FROM v_recall_impact')}
        self.assertEqual(recall_view, {('R001', 2, '15'),
                                       ('R002', 1, '10')})
        customer_view = {tuple(r) for r in db.execute(
            'SELECT customer_id,shipments_affected,units_total '
            'FROM v_customer_impact')}
        # Union of shipment_id: 2 shipments, 15 units; never 25.
        self.assertEqual(customer_view, {('CUST-1', 2, '15')})
        status = {r['shipment_id']: r['link_status'] for r in db.execute(
            'SELECT shipment_id,link_status FROM v_shipment_link_status')}
        self.assertEqual(status, {
            'S1': 'linked_to_recall', 'S2': 'linked_to_recall',
            'S3': 'not_linked_by_supplied_data'})

    def test_roots_view_lists_every_basis_for_one_shipment(self):
        db = self.build_and_connect()
        rows = db.execute(
            "SELECT recall_id,root_product_id,root_lot_code,hops "
            "FROM v_shipment_recall_roots WHERE shipment_id='S1' "
            "ORDER BY recall_id,root_lot_code").fetchall()
        self.assertEqual([tuple(r) for r in rows], [
            ('R001', 'OAT', 'L1', 2), ('R001', 'OAT', 'L2', 2),
            ('R002', 'SOUP', 'B9', 1)])


class LineageEdgeTest(MartCase):
    def test_zero_hop_when_root_batch_is_shipped_directly(self):
        write_csv(self.shipments, [
            SHIPMENTS[0],
            ('SX', 'CUST-2', 'OAT', 'L1', '3'),
        ])
        db = self.build_and_connect()
        row = db.execute(
            "SELECT * FROM propagation_facts WHERE recall_id='R001' "
            "AND root_lot_code='L1' AND product_id='OAT' AND lot_code='L1'"
        ).fetchone()
        self.assertEqual(row['hops'], 0)
        self.assertIsNone(row['via_process'])
        self.assertIsNone(row['predecessor_product_id'])
        self.assertIn(('SX', 'R001', '3'), {tuple(r) for r in db.execute(
            'SELECT shipment_id,recall_id,units FROM shipment_recall_impact')})

    def test_two_recalls_hitting_one_root_batch_keep_separate_roots(self):
        # A second collector-style database with two recalls on the exact
        # same product and lot.
        db2 = self.dir / 'two.sqlite'
        with sqlite3.connect(db2) as c:
            c.execute('CREATE TABLE recalls(recall_id TEXT PRIMARY KEY,'
                      'published TEXT,product TEXT,lots_json TEXT,reason TEXT)')
            c.execute('CREATE TABLE sources(recall_id TEXT,path TEXT,'
                      'PRIMARY KEY(recall_id,path))')
            c.executemany('INSERT INTO recalls VALUES(?,?,?,?,?)', [
                ('RA', '2026-02-01', 'Synthetic oat bar',
                 json.dumps(['L1']), 'r-a'),
                ('RB', '2026-02-02', 'Synthetic oat bar',
                 json.dumps(['L1']), 'r-b')])
            c.executemany('INSERT INTO sources VALUES(?,?)',
                          [('RA', 'a.html'), ('RB', 'b.html')])
        out = self.dir / 'two-mart.sqlite'
        build_impact_mart(db2, self.products, self.lots, self.members,
                          self.shipments, out)
        db = sqlite3.connect(out)
        roots = {tuple(r) for r in db.execute(
            'SELECT recall_id,product_id,lot_code FROM recall_roots')}
        self.assertEqual(roots, {('RA', 'OAT', 'L1'),
                                 ('RB', 'OAT', 'L1')})
        # Every target appears once per root; S2 (FIN/D) is hit by both.
        rows = {tuple(r) for r in db.execute(
            "SELECT shipment_id,recall_id FROM shipment_recall_impact "
            "ORDER BY 1,2")}
        self.assertEqual(rows, {('S1', 'RA'), ('S1', 'RB'),
                                ('S2', 'RA'), ('S2', 'RB')})

    def test_same_lot_code_on_different_products_does_not_join(self):
        write_csv(self.lots, [LOTS[0], ('OAT', 'L1'), ('SOUP', 'L1'),
                              ('FIN', 'D')])
        write_csv(self.members, [
            MEMBERS[0],
            ('PX', 'input', 'OAT', 'L1'),
            ('PX', 'output', 'FIN', 'D'),
        ])
        write_csv(self.shipments, [
            SHIPMENTS[0],
            ('SZ', 'CUST-9', 'SOUP', 'L1', '2'),
        ])
        db = self.build_and_connect()
        # SOUP/L1 is never a root and receives no propagation facts.
        self.assertEqual(db.execute(
            "SELECT count(*) FROM propagation_facts "
            "WHERE product_id='SOUP' AND lot_code='L1'").fetchone()[0], 0)
        self.assertEqual({tuple(r) for r in db.execute(
            "SELECT shipment_id,link_status FROM v_shipment_link_status "
            "WHERE shipment_id='SZ'")}, {('SZ', 'not_linked_by_supplied_data')})

    def test_unmapped_product_and_unregistered_lot_are_kept_gaps(self):
        db2 = self.dir / 'gaps.sqlite'
        with sqlite3.connect(db2) as c:
            c.execute('CREATE TABLE recalls(recall_id TEXT PRIMARY KEY,'
                      'published TEXT,product TEXT,lots_json TEXT,reason TEXT)')
            c.execute('CREATE TABLE sources(recall_id TEXT,path TEXT,'
                      'PRIMARY KEY(recall_id,path))')
            c.executemany('INSERT INTO recalls VALUES(?,?,?,?,?)', [
                ('RG', '2026-03-01', 'Mystery product',
                 json.dumps(['X9']), 'g'),
                ('RH', '2026-03-02', 'Synthetic oat bar',
                 json.dumps(['GHOST']), 'h')])
            c.executemany('INSERT INTO sources VALUES(?,?)',
                          [('RG', 'g.html'), ('RH', 'h.html')])
        out = self.dir / 'gap-mart.sqlite'
        build_impact_mart(db2, self.products, self.lots, self.members,
                          self.shipments, out)
        db = sqlite3.connect(out)
        rows = {tuple(r) for r in db.execute(
            'SELECT recall_id,product_id,lot_code,status '
            'FROM notice_lot_resolutions')}
        self.assertEqual(rows, {
            ('RG', None, 'X9', 'product_unmapped'),
            ('RH', 'OAT', 'GHOST', 'lot_unregistered')})
        self.assertEqual(db.execute(
            'SELECT count(*) FROM recall_roots').fetchone()[0], 0)
        self.assertEqual(db.execute(
            'SELECT count(*) FROM propagation_facts').fetchone()[0], 0)
        # Gap notices are not "safe": every shipment stays not-linked.
        self.assertEqual(db.execute(
            "SELECT count(*) FROM v_shipment_link_status "
            "WHERE link_status='linked_to_recall'").fetchone()[0], 0)


class PathOrderingTest(MartCase):
    def _tie_graph(self):
        write_csv(self.lots, [
            LOTS[0], ('OAT', 'L1'),
            ('FIN', 'M1'), ('FIN', 'M2'), ('FIN', 'Z'),
        ])
        # Two equal-length paths differ in their FIRST step and converge at
        # Z through one merge process (Z keeps a single producer).
        write_csv(self.members, [
            MEMBERS[0],
            ('PX', 'input', 'OAT', 'L1'), ('PX', 'output', 'FIN', 'M1'),
            ('PY', 'input', 'OAT', 'L1'), ('PY', 'output', 'FIN', 'M2'),
            ('PZ1', 'input', 'FIN', 'M1'), ('PZ1', 'input', 'FIN', 'M2'),
            ('PZ1', 'output', 'FIN', 'Z'),
        ])
        write_csv(self.shipments, [
            SHIPMENTS[0], ('ST', 'CUST-1', 'FIN', 'Z', '1')])

    def test_full_prefix_unicode_tiebreak(self):
        self._tie_graph()
        db = self.build_and_connect()
        row = db.execute(
            "SELECT * FROM propagation_facts WHERE recall_id='R001' "
            "AND root_lot_code='L1' AND product_id='FIN' AND lot_code='Z'"
        ).fetchone()
        self.assertEqual(row['hops'], 2)
        # The two paths are PX,PZ1 vs PY,PZ2 with a single producer for Z;
        # the first step (PX < PY) decides, never the last step alone.
        self.assertEqual(row['via_process'], 'PZ1')
        self.assertEqual((row['predecessor_product_id'],
                          row['predecessor_lot_code']), ('FIN', 'M1'))
        snap = json.loads(db.execute(
            'SELECT canonical_json FROM logic_snapshot').fetchone()[0])
        steps = next(f['canonical_steps'] for f in snap['propagation_facts']
                     if f['recall_id'] == 'R001' and f['target'] ==
                     ['FIN', 'Z'])
        self.assertEqual(steps, [['PX', 'FIN', 'M1'],
                                 ['PZ1', 'FIN', 'Z']])

    def test_diamond_converges_without_duplicate_facts(self):
        # Layered diamond: L1 -> M1,M2 -> N -> Z, two paths merge twice.
        write_csv(self.lots, [
            LOTS[0], ('OAT', 'L1'),
            ('FIN', 'M1'), ('FIN', 'M2'), ('FIN', 'N'), ('FIN', 'Z'),
        ])
        write_csv(self.members, [
            MEMBERS[0],
            ('PA', 'input', 'OAT', 'L1'), ('PA', 'output', 'FIN', 'M1'),
            ('PB', 'input', 'OAT', 'L1'), ('PB', 'output', 'FIN', 'M2'),
            ('PC', 'input', 'FIN', 'M1'), ('PC', 'input', 'FIN', 'M2'),
            ('PC', 'output', 'FIN', 'N'),
            ('PD', 'input', 'FIN', 'N'), ('PD', 'output', 'FIN', 'Z'),
        ])
        write_csv(self.shipments, [
            SHIPMENTS[0], ('SD', 'CUST-1', 'FIN', 'Z', '4')])
        db = self.build_and_connect()
        # One fact per reachable target regardless of how many paths merge:
        # L1(0), M1, M2 (1), N (2), Z (3) => five rows, not an exponential set.
        rows = db.execute(
            "SELECT product_id,lot_code,hops FROM propagation_facts "
            "WHERE recall_id='R001' AND root_lot_code='L1' "
            "ORDER BY product_id,lot_code").fetchall()
        self.assertEqual([tuple(r) for r in rows], [
            ('FIN', 'M1', 1), ('FIN', 'M2', 1), ('FIN', 'N', 2),
            ('FIN', 'Z', 3), ('OAT', 'L1', 0)])
        # Algorithm bookkeeping: each target node carries a single label,
        # so the converging node N is stored exactly once.
        self.assertEqual(db.execute(
            "SELECT count(*) FROM propagation_facts "
            "WHERE product_id='FIN' AND lot_code='N'").fetchone()[0], 1)

    def test_propagate_never_enumerates_paths_on_wide_diamond(self):
        # 10 parallel branches that repeatedly re-converge: an enumerating
        # algorithm would carry 2**10 path strings; the label algorithm stays
        # linear in nodes.
        lots = [LOTS[0], ('OAT', 'L1')]
        members = [MEMBERS[0]]
        for i in range(10):
            lots.append(('FIN', 'B%d' % i))
            members.append(('Q%d' % i, 'input', 'OAT', 'L1'))
            members.append(('Q%d' % i, 'output', 'FIN', 'B%d' % i))
        lots.append(('FIN', 'MERGE'))
        for i in range(10):
            members.append(('RM', 'input', 'FIN', 'B%d' % i))
        members.append(('RM', 'output', 'FIN', 'MERGE'))
        write_csv(self.lots, lots)
        write_csv(self.members, members)
        write_csv(self.shipments, [
            SHIPMENTS[0], ('SM', 'CUST-1', 'FIN', 'MERGE', '6')])
        db = self.build_and_connect()
        self.assertEqual(db.execute(
            "SELECT count(*) FROM propagation_facts WHERE recall_id='R001' "
            "AND root_lot_code='L1'").fetchone()[0], 12)
        merge = db.execute(
            "SELECT hops,via_process,predecessor_lot_code FROM "
            "propagation_facts WHERE product_id='FIN' AND lot_code='MERGE'"
        ).fetchone()
        self.assertEqual(tuple(merge), (2, 'RM', 'B0'))


class RejectionTest(MartCase):
    def _expect_failure(self):
        with self.assertRaises(ImpactBuildError):
            self.build()
        self.assertFalse(self.out.exists())

    def test_full_graph_cycle_rejected(self):
        write_csv(self.members, MEMBERS + [
            ('P4', 'input', 'FIN', 'F'), ('P4', 'output', 'OAT', 'L1')])
        self._expect_failure()

    def test_self_loop_is_a_cycle(self):
        write_csv(self.members, MEMBERS + [
            ('P4', 'input', 'FIN', 'D'), ('P4', 'output', 'FIN', 'D')])
        self._expect_failure()

    def test_cycle_in_unrelated_component_rejected(self):
        write_csv(self.lots, [LOTS[0], ('OAT', 'L1'),
                              ('FIN', 'U1'), ('FIN', 'U2')])
        write_csv(self.members, [
            MEMBERS[0],
            ('U1', 'input', 'FIN', 'U2'), ('U1', 'output', 'FIN', 'U1'),
            ('U2', 'input', 'FIN', 'U1'), ('U2', 'output', 'FIN', 'U2'),
        ])
        self._expect_failure()

    def test_multiple_producers_rejected(self):
        write_csv(self.members, MEMBERS + [
            ('P4', 'input', 'OAT', 'L2'), ('P4', 'output', 'FIN', 'D')])
        self._expect_failure()

    def test_unknown_reference_in_member_rejected(self):
        write_csv(self.members, MEMBERS + [
            ('P4', 'input', 'OAT', 'NOPE'), ('P4', 'output', 'FIN', 'D')])
        self._expect_failure()

    def test_unknown_reference_in_shipment_rejected(self):
        write_csv(self.shipments, [
            SHIPMENTS[0], ('S9', 'C', 'OAT', 'NOPE', '1')])
        self._expect_failure()

    def test_duplicate_member_rejected_even_if_identical(self):
        write_csv(self.members, MEMBERS + [MEMBERS[1]])
        self._expect_failure()

    def test_duplicate_shipment_rejected(self):
        write_csv(self.shipments, SHIPMENTS + [
            ('S1', 'CUST-2', 'FIN', 'D', '1')])
        self._expect_failure()

    def test_illegal_units_rejected(self):
        for bad in ('0', '1000000001', '-1', '1.5', '', ' 5', '0x1', '１２'):
            write_csv(self.shipments, [
                SHIPMENTS[0], ('SB', 'C', 'FIN', 'D', bad)])
            with self.subTest(bad=bad):
                self._expect_failure()

    def test_blank_and_whitespace_values_rejected(self):
        write_csv(self.lots, [LOTS[0], ('OAT', '   ')])
        self._expect_failure()
        write_csv(self.lots, [LOTS[0], ('', 'L9')])
        self._expect_failure()

    def test_embedded_line_break_in_quoted_field_rejected(self):
        self.lots.write_bytes(
            b'product_id,lot_code\n"O\nAT",L1\n')
        self._expect_failure()

    def test_bad_role_and_bad_header_rejected(self):
        write_csv(self.members, [
            MEMBERS[0], ('PX', 'feed', 'OAT', 'L1'),
            ('PX', 'output', 'FIN', 'D')])
        self._expect_failure()
        write_csv(self.members, [('wrong', 'header')])
        self._expect_failure()

    def test_duplicate_notice_product_rejected(self):
        write_csv(self.products, [
            PRODUCTS[0], ('OAT', 'Synthetic oat bar'),
            ('FIN', 'Synthetic oat bar')])
        self._expect_failure()

    def test_process_without_input_or_output_rejected(self):
        write_csv(self.members, [
            MEMBERS[0], ('PX', 'output', 'FIN', 'D')])
        self._expect_failure()

    def test_missing_database_rejected(self):
        with self.assertRaises(ImpactBuildError):
            build_impact_mart(self.dir / 'nope.sqlite', self.products,
                              self.lots, self.members, self.shipments,
                              self.out)

    def test_incompatible_database_rejected_readonly(self):
        weird = self.dir / 'weird.sqlite'
        with sqlite3.connect(weird) as c:
            c.execute('CREATE TABLE recalls(x TEXT)')
            c.execute('CREATE TABLE sources(y TEXT)')
        with self.assertRaises(ImpactBuildError):
            build_impact_mart(weird, self.products, self.lots, self.members,
                              self.shipments, self.out)
        # The source must remain untouched: read-only open cannot DDL.
        with sqlite3.connect(weird) as c:
            self.assertEqual(
                [r[1] for r in c.execute('PRAGMA table_info(recalls)')],
                ['x'])


class EmptyInputTest(MartCase):
    def test_header_only_lineage_and_shipments(self):
        write_csv(self.lots, [LOTS[0]])
        write_csv(self.members, [MEMBERS[0]])
        write_csv(self.shipments, [SHIPMENTS[0]])
        summary = self.build()
        self.assertEqual(summary['matched_roots'], 0)
        self.assertEqual(summary['propagation_facts'], 0)
        db = self.connect()
        self.assertEqual({r['status'] for r in db.execute(
            'SELECT status FROM notice_lot_resolutions')},
            {'lot_unregistered'})
        self.assertEqual(db.execute(
            'SELECT count(*) FROM internal_lots').fetchone()[0], 0)

    def test_products_requires_a_mapping(self):
        write_csv(self.products, [PRODUCTS[0]])
        with self.assertRaises(ImpactBuildError):
            self.build()


class DeterminismTest(MartCase):
    def _snapshot_digest(self, path):
        with sqlite3.connect(path) as db:
            kind, cj, digest = db.execute(
                'SELECT * FROM logic_snapshot').fetchone()
            self.assertEqual(
                digest, hashlib.sha256(cj.encode('utf-8')).hexdigest())
            return kind, digest, cj
    def test_shuffled_rows_keep_identical_logic(self):
        import random
        first = self.dir / 'a.sqlite'
        second = self.dir / 'b.sqlite'
        self.build(first)
        rng = random.Random(20261113)
        for path, rows in ((self.products, PRODUCTS), (self.lots, LOTS),
                           (self.members, MEMBERS),
                           (self.shipments, SHIPMENTS)):
            header, body = rows[0], rows[1:]
            rng.shuffle(body)
            write_csv(path, [header] + body)
        self.build(second)
        kind_a, digest_a, cj_a = self._snapshot_digest(first)
        kind_b, digest_b, cj_b = self._snapshot_digest(second)
        self.assertEqual(kind_a, kind_b)
        self.assertEqual(digest_a, digest_b)
        self.assertEqual(json.loads(cj_a), json.loads(cj_b))
        # Stable-key business rows must match row for row.  Record positions
        # deliberately differ because they are source-location provenance,
        # not logic, and are excluded from this comparison.
        business_queries = {
            'propagation_facts':
                'SELECT recall_id,root_product_id,root_lot_code,product_id,'
                'lot_code,hops,predecessor_product_id,predecessor_lot_code,'
                'via_process FROM propagation_facts',
            'shipment_recall_impact':
                'SELECT shipment_id,recall_id,units FROM shipment_recall_impact',
            'notice_lot_resolutions':
                'SELECT recall_id,notice_product,notice_lot,product_id,'
                'lot_code,status FROM notice_lot_resolutions',
            'recall_roots':
                'SELECT recall_id,product_id,lot_code FROM recall_roots',
            'process_members':
                'SELECT process_id,role,product_id,lot_code '
                'FROM process_members',
        }
        with sqlite3.connect(first) as d1, sqlite3.connect(second) as d2:
            for table, q in business_queries.items():
                self.assertEqual(sorted(d1.execute(q).fetchall()),
                                 sorted(d2.execute(q).fetchall()), table)
            # Positions do reflect the real row reordering.
            self.assertNotEqual(
                d1.execute('SELECT record_position FROM process_members '
                           'WHERE process_id="P1" AND role="input"').fetchone(),
                d2.execute('SELECT record_position FROM process_members '
                           'WHERE process_id="P1" AND role="input"').fetchone())

    def test_crlf_keeps_logic_but_changes_raw_digest(self):
        lf = self.dir / 'lf.sqlite'
        self.build(lf)
        crlf = self.dir / 'crlf.sqlite'
        for path, rows in ((self.products, PRODUCTS), (self.lots, LOTS),
                           (self.members, MEMBERS),
                           (self.shipments, SHIPMENTS)):
            write_csv(path, rows, newline='\r\n')
        self.build(crlf)
        _, digest_lf, cj_lf = self._snapshot_digest(lf)
        _, digest_crlf, cj_crlf = self._snapshot_digest(crlf)
        self.assertEqual(cj_lf, cj_crlf)  # business facts unchanged
        with sqlite3.connect(lf) as d1, sqlite3.connect(crlf) as d2:
            raw_lf = dict(d1.execute(
                'SELECT input_kind,sha256 FROM input_raw').fetchall())
            raw_crlf = dict(d2.execute(
                'SELECT input_kind,sha256 FROM input_raw').fetchall())
            self.assertNotEqual(raw_lf, raw_crlf)
            for kind, digest in raw_crlf.items():
                self.assertEqual(len(digest), 64)
            # Positions are data-record ordinal numbers and stay 1-based.
            self.assertEqual(d2.execute(
                'SELECT min(record_position),max(record_position) '
                'FROM input_records WHERE input_kind="shipments"').fetchone(),
                (1, 3))

    def test_rebuild_is_repeatable(self):
        self.build()
        digest_before = self.connect().execute(
            'SELECT sha256 FROM logic_snapshot').fetchone()[0]
        self.build()
        digest_after = self.connect().execute(
            'SELECT sha256 FROM logic_snapshot').fetchone()[0]
        self.assertEqual(digest_before, digest_after)

    def test_raw_line_keeps_source_text_and_blank_lines_do_not_number(self):
        # Quoted fields survive verbatim; a blank separator line must not
        # consume a 1-based data-record position.
        self.lots.write_text(
            'product_id,lot_code\n'
            '\n'
            '"OAT","L1"\n'
            'OAT,L2\n'
            'OAT,SAFE\n'
            'SOUP,B9\n'
            'FIN,D\n'
            'FIN,E\n'
            'FIN,F\n', encoding='utf-8')
        db = self.build_and_connect()
        rows = db.execute(
            'SELECT record_position,raw_line FROM input_records '
            "WHERE input_kind='lots' ORDER BY record_position").fetchall()
        self.assertEqual([tuple(r) for r in rows][:2],
                         [(1, '"OAT","L1"'), (2, 'OAT,L2')])
        self.assertEqual(db.execute(
            "SELECT count(*) FROM input_records WHERE input_kind='lots'"
        ).fetchone()[0], 7)
        # Parsed business values are identical to the unquoted fixture.
        self.assertEqual({tuple(r) for r in db.execute(
            'SELECT product_id,lot_code FROM internal_lots')},
            {('OAT', 'L1'), ('OAT', 'L2'), ('OAT', 'SAFE'),
             ('SOUP', 'B9'), ('FIN', 'D'), ('FIN', 'E'), ('FIN', 'F')})


class SnapshotConcurrencyTest(unittest.TestCase):
    def test_recalls_and_sources_come_from_one_snapshot(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        site = d / 'site'
        shutil.copytree(SITE, site)
        db_path = d / 'recalls.sqlite'
        # Pre-populate one committed recall; a second commit happens while the
        # mart holds an open read transaction.
        with sqlite3.connect(db_path) as c:
            c.execute('CREATE TABLE recalls(recall_id TEXT PRIMARY KEY,'
                      'published TEXT,product TEXT,lots_json TEXT,reason TEXT)')
            c.execute('CREATE TABLE sources(recall_id TEXT,path TEXT,'
                      'PRIMARY KEY(recall_id,path))')
            c.execute('INSERT INTO recalls VALUES(?,?,?,?,?)',
                      ('OLD', '2020-01-01', 'old',
                       json.dumps(['OLD-LOT']), 'old'))
            c.execute('INSERT INTO sources VALUES(?,?)', ('OLD', 'old.html'))

        barrier = threading.Event()
        errors = []

        def writer():
            # Block until the reader has opened its transaction; then commit
            # a full re-collection. With the default rollback journal a
            # writer takes the reserved lock and can commit only once the
            # reader ends its read transaction.
            barrier.wait()
            try:
                collect(site, db_path)
            except BaseException as exc:  # pragma: no cover - failure reporter
                errors.append(exc)

        thread = threading.Thread(target=writer)
        thread.start()
        with open_recall_db(db_path) as reader:
            reader.execute('BEGIN')
            try:
                barrier.set()
                recalls, sources = read_recall_tables(reader)
                # Inside this snapshot the concurrent collector commit is not
                # visible yet.
                self.assertEqual(sorted(recalls), ['OLD'])
                self.assertEqual(sources, {'OLD': ['old.html']})
            finally:
                reader.execute('COMMIT')
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        # After the transaction ends the committed snapshot is fully visible
        # (the pre-existing OLD row is retained, plus both collected recalls).
        with open_recall_db(db_path) as reader:
            recalls_after, sources_after = read_recall_tables(reader)
        self.assertEqual(sorted(recalls_after), ['OLD', 'R001', 'R002'])
        self.assertEqual(
            sum(len(v) for v in sources_after.values()), 4)


class FailureAndAliasTest(MartCase):
    def test_existing_mart_survives_a_later_failed_build(self):
        self.build()
        good_digest = self.connect().execute(
            'SELECT sha256 FROM logic_snapshot').fetchone()[0]
        # Add an edge that closes a cycle over the real lineage.
        write_csv(self.members, MEMBERS + [
            ('P4', 'input', 'FIN', 'F'), ('P4', 'output', 'OAT', 'L1')])
        with self.assertRaises(ImpactBuildError):
            self.build()
        db = self.connect()
        self.assertEqual(db.execute(
            'SELECT sha256 FROM logic_snapshot').fetchone()[0],
            good_digest)
        self.assertEqual(db.execute(
            'SELECT count(*) FROM propagation_facts').fetchone()[0], 9)
        leftovers = list(self.dir.glob(self.out.name + '.*.tmp'))
        self.assertEqual(leftovers, [])

    def test_replace_failure_keeps_old_mart_and_clears_temp(self):
        self.build()
        from recall_collect.impact import mart as mart_module
        real_replace = os.replace

        def boom(src, dst):
            raise OSError('injected replace failure')

        self.addCleanup(lambda: setattr(mart_module.os, 'replace',
                                        real_replace))
        mart_module.os.replace = boom
        with self.assertRaises(OSError):
            self.build()
        db = self.connect()
        self.assertEqual(db.execute(
            'SELECT count(*) FROM recall_roots').fetchone()[0], 3)
        self.assertEqual(list(self.dir.glob(self.out.name + '.*.tmp')), [])

    def test_output_cannot_alias_inputs(self):
        # Same path.
        with self.assertRaises(ImpactBuildError):
            self.build(self.db)
        # Symlink output pointing at the source database.
        link = self.dir / 'link.sqlite'
        link.symlink_to(self.db)
        with self.assertRaises(ImpactBuildError):
            self.build(link)
        # Hard-link alias of the source database.
        hard = self.dir / 'hard.sqlite'
        os.link(self.db, hard)
        with self.assertRaises(ImpactBuildError):
            self.build(hard)
        # Symlink output pointing at one of the CSV inputs.
        csv_out = self.dir / 'csv-out.csv'
        csv_out.symlink_to(self.products)
        with self.assertRaises(ImpactBuildError):
            build_impact_mart(self.db, self.products, self.lots, self.members,
                              self.shipments, csv_out)

    def test_mart_is_queryable_after_inputs_leave(self):
        self.build()
        vault = self.dir / 'vault'
        vault.mkdir()
        moved = vault / 'mart.sqlite'
        shutil.move(self.out, moved)
        db = sqlite3.connect(moved)
        db.row_factory = sqlite3.Row
        # Facts, reconstructed basis and provenance all survive.
        self.assertEqual(db.execute(
            'SELECT count(*) FROM propagation_facts').fetchone()[0], 9)
        row = db.execute(
            "SELECT via_process,predecessor_lot_code FROM propagation_facts "
            "WHERE recall_id='R001' AND root_lot_code='L1' "
            "AND product_id='FIN' AND lot_code='F'").fetchone()
        self.assertEqual(tuple(row), ('P3', 'D'))
        # Re-hash the stored raw bytes and compare with the stored digest.
        for kind, raw, digest in db.execute(
                'SELECT input_kind,raw_bytes,sha256 FROM input_raw'):
            self.assertEqual(
                hashlib.sha256(raw).hexdigest(), digest)
        snap = db.execute(
            'SELECT canonical_json,sha256 FROM logic_snapshot').fetchone()
        self.assertEqual(hashlib.sha256(snap[0].encode()).hexdigest(),
                         snap[1])
        # No machine-absolute path or wall-clock metadata leaked in.
        meta = {r[0] for r in db.execute('SELECT meta_key FROM meta')}
        self.assertEqual(meta, {'format', 'logic_snapshot_kind',
                                'logic_snapshot_sha256'})
        self.assertNotIn(str(self.dir),
                         db.execute('SELECT canonical_json FROM logic_snapshot')
                         .fetchone()[0])

    def test_no_absolute_paths_or_timestamps_in_snapshot(self):
        db = self.build_and_connect()
        cj = db.execute(
            'SELECT canonical_json FROM logic_snapshot').fetchone()[0]
        self.assertNotIn(self.tmp.name, cj)
        snap = json.loads(cj)
        self.assertEqual(snap['format'], 'recall-impact-mart/1')


class CliEndToEndTest(unittest.TestCase):
    def test_full_cli_pipeline_with_independent_expectations(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        db = d / 'recalls.sqlite'
        env = dict(os.environ, PYTHONPATH=str(SRC))
        env.pop('PYTHONHOME', None)
        # 1. Real collection CLI prepares the source database.
        first = subprocess.run(
            [sys.executable, '-m', 'recall_collect.cli',
             '--source', str(SITE), '--database', str(db)],
            cwd=str(ROOT), env=env, text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(first.stdout), {
            'pages': 5, 'source_unique_recalls': 2,
            'inserted': 2, 'stored': 2})
        second = subprocess.run(
            [sys.executable, '-m', 'recall_collect.cli',
             '--source', str(SITE), '--database', str(db)],
            cwd=str(ROOT), env=env, text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(second.stdout)['inserted'], 0)

        out = d / 'mart.sqlite'
        # 2. New mart CLI reads the four CSVs.
        run = subprocess.run(
            [sys.executable, '-m', 'recall_collect.impact',
             '--recalls-db', str(db),
             '--products', str(FACTORY / 'products.csv'),
             '--lots', str(FACTORY / 'lots.csv'),
             '--process-members', str(FACTORY / 'process-members.csv'),
             '--shipments', str(FACTORY / 'shipments.csv'),
             '--output', str(out)],
            cwd=str(ROOT), env=env, text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(run.stdout)['propagation_facts'], 9)

        # 3. Independent expected relations checked directly in SQL.
        con = sqlite3.connect(out)
        self.assertEqual(con.execute(
            "SELECT lots_json FROM recalls WHERE recall_id='R001'"
        ).fetchone()[0], json.dumps(['L1', 'L2']))
        self.assertEqual(con.execute(
            'SELECT count(*) FROM recall_sources').fetchone()[0], 3)
        expected = {
            ('R001', 'OAT', 'L1', 'OAT', 'L1', 0),
            ('R001', 'OAT', 'L1', 'FIN', 'D', 1),
            ('R001', 'OAT', 'L1', 'FIN', 'E', 1),
            ('R001', 'OAT', 'L1', 'FIN', 'F', 2),
            ('R001', 'OAT', 'L2', 'OAT', 'L2', 0),
            ('R001', 'OAT', 'L2', 'FIN', 'E', 1),
            ('R001', 'OAT', 'L2', 'FIN', 'F', 2),
            ('R002', 'SOUP', 'B9', 'SOUP', 'B9', 0),
            ('R002', 'SOUP', 'B9', 'FIN', 'F', 1),
        }
        self.assertEqual({tuple(r) for r in con.execute(
            'SELECT recall_id,root_product_id,root_lot_code,product_id,'
            'lot_code,hops FROM propagation_facts')}, expected)
        self.assertEqual({tuple(r) for r in con.execute(
            'SELECT shipment_id,recall_id,units '
            'FROM shipment_recall_impact ORDER BY 1,2')}, {
            ('S1', 'R001', '10'), ('S1', 'R002', '10'),
            ('S2', 'R001', '5')})
        self.assertEqual({tuple(r) for r in con.execute(
            'SELECT recall_id,shipments_affected,units_total '
            'FROM v_recall_impact')},
            {('R001', 2, '15'), ('R002', 1, '10')})
        self.assertEqual({tuple(r) for r in con.execute(
            'SELECT customer_id,shipments_affected,units_total '
            'FROM v_customer_impact')}, {('CUST-1', 2, '15')})
        self.assertEqual(con.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_cli_failure_is_nonzero_without_touching_output(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        env = dict(os.environ, PYTHONPATH=str(SRC))
        run = subprocess.run(
            [sys.executable, '-m', 'recall_collect.impact',
             '--recalls-db', str(d / 'missing.sqlite'),
             '--products', str(FACTORY / 'products.csv'),
             '--lots', str(FACTORY / 'lots.csv'),
             '--process-members', str(FACTORY / 'process-members.csv'),
             '--shipments', str(FACTORY / 'shipments.csv'),
             '--output', str(d / 'mart.sqlite')],
            cwd=str(ROOT), env=env, text=True, capture_output=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('impact build failed', run.stderr)
        self.assertFalse((d / 'mart.sqlite').exists())


if __name__ == '__main__':
    unittest.main()
