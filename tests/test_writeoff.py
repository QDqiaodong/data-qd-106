"""Tests for the offline write-off interval analysis.

The expectations here are computed independently of the implementation:

* small scenarios are solved by brute-force enumeration of every integral
  allocation, written directly in this file;
* the exponential-family and billion-unit scenarios assert exact endpoint
  values derived by hand from the stated capacities;
* every witness is re-validated against conservation, capacity and metric
  value by walking the returned assignments.

The pre-existing impact-mart, site-capture, collector and trends tests are
kept unchanged.
"""
import csv
import hashlib
import json
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from recall_collect.writeoff import (
    WriteoffInputError, analyze_writeoff_intervals)
from recall_collect.writeoff.flow import Dinic
from recall_collect.store import collect
from recall_collect.impact import build_impact_mart

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
SITE = ROOT / 'fixtures' / 'site'
FACTORY = ROOT / 'fixtures' / 'factory'
MART_FORMAT = 'recall-impact-mart/1'


# --------------------------------------------------------------------------
# Helpers: build a minimal but schema-compatible mart and a returns CSV.
# --------------------------------------------------------------------------

def build_mart(path, shipments, recalls):
    """``shipments``: [(id, customer, units)]; ``recalls``: {rid: [shipment]}."""
    if Path(path).exists():
        Path(path).unlink()
    con = sqlite3.connect(path)
    con.executescript('''
        CREATE TABLE meta(meta_key TEXT PRIMARY KEY, meta_value TEXT NOT NULL);
        CREATE TABLE recalls(recall_id TEXT PRIMARY KEY);
        CREATE TABLE shipments(
            shipment_id TEXT PRIMARY KEY, customer_id TEXT, units TEXT);
        CREATE TABLE shipment_recall_impact(
            shipment_id TEXT, recall_id TEXT,
            PRIMARY KEY(shipment_id, recall_id));
    ''')
    con.execute("INSERT INTO meta VALUES('format',?)", (MART_FORMAT,))
    con.execute("INSERT INTO meta VALUES('logic_snapshot_sha256','deadbeef')")
    for recall_id in recalls:
        con.execute('INSERT INTO recalls VALUES(?)', (recall_id,))
    for shipment_id, customer_id, units in shipments:
        con.execute('INSERT INTO shipments VALUES(?,?,?)',
                    (shipment_id, customer_id, str(units)))
    for recall_id, shipment_ids in recalls.items():
        for shipment_id in shipment_ids:
            con.execute('INSERT INTO shipment_recall_impact VALUES(?,?)',
                        (shipment_id, recall_id))
    con.commit()
    con.close()


def write_returns(path, rows, newline='\n'):
    """``rows``: [(return_id, customer_id, units, [candidate ids])]."""
    with open(path, 'w', encoding='utf-8', newline=newline) as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ['return_id', 'customer_id', 'units', 'candidate_shipment_ids'])
        for return_id, customer_id, units, candidates in rows:
            writer.writerow([return_id, customer_id, units,
                             json.dumps(candidates)])


def compositions(total, parts):
    """Every ordered split of ``total`` into exactly ``parts`` non-negatives."""
    if parts == 1:
        yield [total]
        return
    for value in range(total + 1):
        for rest in compositions(total - value, parts - 1):
            yield [value] + rest


def brute_allocations(demands, capacities, candidate_indices):
    """Enumerate every feasible integral allocation (small instances only).

    Returns a list of allocations; each is a list over shipments of per-return
    piece lists, i.e. ``allocation[return][shipment]`` is zero when the
    shipment is not a candidate.
    """
    return_count = len(demands)
    shipment_count = len(capacities)
    results = []

    def recurse(index, remaining, assignment):
        if index == return_count:
            results.append([row[:] for row in assignment])
            return
        candidates = candidate_indices[index]
        for split in compositions(demands[index], len(candidates)):
            if all(split[position] <= remaining[candidates[position]]
                   for position in range(len(candidates))):
                updated = remaining[:]
                row = [0] * shipment_count
                for position, shipment in enumerate(candidates):
                    updated[shipment] -= split[position]
                    row[shipment] = split[position]
                assignment.append(row)
                recurse(index + 1, updated, assignment)
                assignment.pop()

    recurse(0, capacities[:], [])
    return results


def metric_extremes(allocations, target):
    """Independent min/max of ``sum of pieces on target shipments``."""
    values = []
    for allocation in allocations:
        total = 0
        for row in allocation:
            for shipment in target:
                total += row[shipment]
        values.append(total)
    return min(values), max(values)


class WriteoffCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.mart = self.dir / 'mart.sqlite'
        self.returns = self.dir / 'returns.csv'
        self.report = self.dir / 'report.json'

    def analyze(self):
        return analyze_writeoff_intervals(self.mart, self.returns)

    def assert_witness_valid(self, witness, rows_by_id, capacities,
                             target, expected):
        """Re-verify one endpoint witness without trusting the solver."""
        self.assertEqual(witness['units'], expected)
        totals = {}
        seen_returns = []
        for row in witness['allocation']:
            return_id = row['return_id']
            seen_returns.append(return_id)
            demanded = rows_by_id[return_id]['units']
            assigned = 0
            for assignment in row['assignments']:
                shipment_id = assignment['shipment_id']
                self.assertIn(shipment_id,
                              rows_by_id[return_id]['candidates'])
                self.assertGreater(assignment['units'], 0)
                assigned += assignment['units']
                totals[shipment_id] = totals.get(shipment_id, 0) \
                    + assignment['units']
            self.assertEqual(assigned, demanded)
        self.assertEqual(sorted(seen_returns), sorted(rows_by_id))
        for shipment_id, value in totals.items():
            self.assertLessEqual(value, capacities[shipment_id])
        for shipment_id in witness['shipment_totals']:
            self.assertEqual(totals.get(shipment_id, 0),
                             witness['shipment_totals'][shipment_id])
        value = sum(totals.get(shipment_id, 0) for shipment_id in target)
        self.assertEqual(value, expected)


# --------------------------------------------------------------------------
# Scenario 1: capacity is sufficient in aggregate but locally insufficient.
# --------------------------------------------------------------------------

class AggregateEnoughButLocallyShortTest(WriteoffCase):
    def test_global_capacity_is_ample_but_subset_is_not(self):
        # Two returns, both only able to use S2/S3.  Total capacity across all
        # candidates is 100, far above the 10 demanded, yet the *shared*
        # candidates only offer 6.
        build_mart(self.mart, [
            ('S1', 'C1', 95),
            ('S2', 'C1', 3),
            ('S3', 'C1', 3),
        ], {'R1': ['S1', 'S2', 'S3']})
        write_returns(self.returns, [
            ('Q1', 'C1', 5, ['S2', 'S3']),
            ('Q2', 'C1', 5, ['S2', 'S3']),
        ])
        report = self.analyze()
        self.assertEqual(report['status'], 'infeasible')
        self.assertIsNone(report['metrics'])
        certificate = report['infeasibility']
        self.assertEqual(certificate['demanded_units'], 10)
        self.assertEqual(certificate['available_units'], 6)
        self.assertEqual(certificate['shortfall_units'], 4)
        self.assertEqual({row['return_id'] for row in certificate['returns']},
                         {'Q1', 'Q2'})
        self.assertEqual(
            {row['shipment_id'] for row in certificate['candidate_shipments']},
            {'S2', 'S3'})
        # Certificate inequality really is strict and independently checkable.
        self.assertGreater(certificate['demanded_units'],
                           certificate['available_units'])

    def test_total_capacity_short_is_also_certified_not_zero(self):
        build_mart(self.mart, [('S1', 'C1', 4)], {'R1': ['S1']})
        write_returns(self.returns, [('Q1', 'C1', 9, ['S1'])])
        report = self.analyze()
        self.assertEqual(report['status'], 'infeasible')
        self.assertEqual(report['infeasibility']['shortfall_units'], 5)


# --------------------------------------------------------------------------
# Scenario 2: each row is feasible on its own, yet they conflict together.
# --------------------------------------------------------------------------

class RowsIndividuallyFeasibleButJointlyConflictingTest(WriteoffCase):
    def test_two_rows_that_are_fine_alone_but_not_together(self):
        build_mart(self.mart, [('S1', 'C1', 5)], {'R1': ['S1']})
        write_returns(self.returns, [
            ('Q1', 'C1', 4, ['S1']),
            ('Q2', 'C1', 4, ['S1']),
        ])
        # Each row alone fits (4 <= 5), but 8 > 5 jointly.
        for alone in (['Q1'], ['Q2']):
            write_returns(self.returns, [
                row for row in [
                    ('Q1', 'C1', 4, ['S1']), ('Q2', 'C1', 4, ['S1'])]
                if row[0] in alone])
            single = self.analyze()
            self.assertEqual(single['status'], 'feasible')
        write_returns(self.returns, [
            ('Q1', 'C1', 4, ['S1']), ('Q2', 'C1', 4, ['S1'])])
        report = self.analyze()
        self.assertEqual(report['status'], 'infeasible')
        self.assertEqual(report['infeasibility']['shortfall_units'], 3)


# --------------------------------------------------------------------------
# Scenario 3: greedy per row fails but an exact optimum exists.
# --------------------------------------------------------------------------

class GreedyFailsTest(WriteoffCase):
    def test_row_by_row_greedy_would_blow_the_optimum(self):
        # Q1 may use S1 or S2; Q2 is S1-only.  A greedy that fills Q1 into S1
        # first exhausts S1 and then fails Q2, even though a feasible split
        # exists (Q1 -> S2).  The exact endpoints are integer values, not the
        # greedy outcome.
        build_mart(self.mart, [
            ('S1', 'C1', 5),
            ('S2', 'C1', 10),
        ], {'R1': ['S1']})
        write_returns(self.returns, [
            ('Q1', 'C1', 7, ['S1', 'S2']),
            ('Q2', 'C1', 3, ['S1']),
        ])
        report = self.analyze()
        self.assertEqual(report['status'], 'feasible')
        rows = {
            'Q1': {'units': 7, 'candidates': ['S1', 'S2']},
            'Q2': {'units': 3, 'candidates': ['S1']},
        }
        capacities = {'S1': 5, 'S2': 10}
        union = report['metrics']['affected_union']
        # Q2 forces 3 onto S1; Q1 adds 0..2 (S1 cap 5, S2 must hold the rest).
        self.assertEqual(union['min']['units'], 3)
        self.assertEqual(union['max']['units'], 5)
        self.assert_witness_valid(union['min'], rows, capacities,
                                  {'S1'}, 3)
        self.assert_witness_valid(union['max'], rows, capacities,
                                  {'S1'}, 5)

    def test_shared_capacity_forces_a_non_greedy_split(self):
        # Q1 can use S1(2) or S2(10); Q2 only S2.  S2 is tight (total demand
        # equals its capacity), so the minimum uses S1 not at all while the
        # maximum lends it the full two pieces.
        build_mart(self.mart, [
            ('S1', 'C1', 2),
            ('S2', 'C1', 10),
        ], {'R1': ['S1']})
        write_returns(self.returns, [
            ('Q1', 'C1', 7, ['S1', 'S2']),
            ('Q2', 'C1', 3, ['S2']),
        ])
        report = self.analyze()
        self.assertEqual(report['status'], 'feasible')
        union = report['metrics']['affected_union']
        self.assertEqual(union['min']['units'], 0)
        self.assertEqual(union['max']['units'], 2)


# --------------------------------------------------------------------------
# Scenario 4: overlapping recalls must not be added together.
# --------------------------------------------------------------------------

class OverlappingRecallsTest(WriteoffCase):
    def test_union_is_deduplicated_and_recalls_tracked_separately(self):
        # S1 is shared by R1 and R2; S2 only R1.  The union metric counts the
        # shared shipment once, so it can never be R1 + R2.
        build_mart(self.mart, [
            ('S1', 'C1', 4),
            ('S2', 'C1', 4),
            ('S3', 'C1', 20),
        ], {'R1': ['S1', 'S2'], 'R2': ['S1']})
        write_returns(self.returns, [
            ('Q1', 'C1', 6, ['S1', 'S2', 'S3']),
        ])
        report = self.analyze()
        self.assertEqual(report['status'], 'feasible')
        union = report['metrics']['affected_union']
        self.assertEqual(union['target_shipment_ids'], ['S1', 'S2'])
        self.assertEqual(union['min']['units'], 0)
        self.assertEqual(union['max']['units'], 6)
        self.assertEqual(report['metrics']['recalls']['R1']['max']['units'], 6)
        self.assertEqual(report['metrics']['recalls']['R2']['max']['units'], 4)
        # The union max is not the sum of the two per-recall maxima (10).
        self.assertNotEqual(union['max']['units'],
                            report['metrics']['recalls']['R1']['max']['units']
                            + report['metrics']['recalls']['R2']['max']['units'])

    def test_overlapping_recall_units_are_not_additive(self):
        build_mart(self.mart, [('S1', 'C1', 5)], {'R1': ['S1'], 'R2': ['S1']})
        write_returns(self.returns, [('Q1', 'C1', 5, ['S1'])])
        report = self.analyze()
        union = report['metrics']['affected_union']['max']['units']
        self.assertEqual(union, 5)
        self.assertEqual(report['metrics']['recalls']['R1']['max']['units'], 5)
        self.assertEqual(report['metrics']['recalls']['R2']['max']['units'], 5)
        self.assertEqual(union, 5)  # not 10


# --------------------------------------------------------------------------
# Scenario 5: interval is non-unique (min != max) and both are witnessed.
# --------------------------------------------------------------------------

class NonUniqueIntervalTest(WriteoffCase):
    def test_min_and_max_differ_and_each_has_its_own_witness(self):
        build_mart(self.mart, [('S1', 'C1', 3), ('S2', 'C1', 7)],
                   {'R1': ['S1']})
        write_returns(self.returns, [('Q1', 'C1', 7, ['S1', 'S2'])])
        report = self.analyze()
        union = report['metrics']['affected_union']
        self.assertEqual(union['min']['units'], 0)
        self.assertEqual(union['max']['units'], 3)
        self.assertNotEqual(union['min']['allocation'],
                            union['max']['allocation'])
        rows = {'Q1': {'units': 7, 'candidates': ['S1', 'S2']}}
        self.assert_witness_valid(union['min'], rows,
                                  {'S1': 3, 'S2': 7}, {'S1'}, 0)
        self.assert_witness_valid(union['max'], rows,
                                  {'S1': 3, 'S2': 7}, {'S1'}, 3)


# --------------------------------------------------------------------------
# Scenario 6: brute-force differential over random small instances.
# --------------------------------------------------------------------------

class BruteForceDifferentialTest(WriteoffCase):
    def test_random_small_instances_match_enumeration(self):
        rng = random.Random(20261114)
        feasible_seen = 0
        infeasible_seen = 0
        for trial in range(250):
            shipment_count = rng.randint(1, 4)
            return_count = rng.randint(1, 4)
            shipment_ids = ['S%d' % index for index in range(shipment_count)]
            capacities = {sid: rng.randint(1, 4) for sid in shipment_ids}
            customers = {sid: 'C%d' % rng.randint(1, 2)
                         for sid in shipment_ids}
            recall_ids = ['R1', 'R2'][:rng.randint(0, 2)]
            recall_members = {}
            for recall_id in recall_ids:
                members = [sid for sid in shipment_ids
                           if rng.random() < 0.5]
                recall_members[recall_id] = members or [shipment_ids[0]]
            build_mart(self.mart,
                       [(sid, customers[sid], capacities[sid])
                        for sid in shipment_ids],
                       recall_members)

            rows = []
            for index in range(return_count):
                customer = rng.choice(sorted(set(customers.values())))
                pool = [sid for sid in shipment_ids
                        if customers[sid] == customer]
                if not pool:
                    continue
                chosen = sorted(rng.sample(
                    pool, rng.randint(1, len(pool))))
                rows.append(('Q%d' % index, customer,
                             rng.randint(1, 4), chosen))
            if not rows:
                continue
            write_returns(self.returns, rows)

            candidate_ids = sorted({sid for _, _, _, chosen in rows
                                    for sid in chosen})
            index_of = {sid: index for index, sid in enumerate(candidate_ids)}
            capacities_list = [capacities[sid] for sid in candidate_ids]
            candidate_indices = [sorted(index_of[sid] for sid in chosen)
                                 for _, _, _, chosen in rows]
            demands = [units for _, _, units, _ in rows]
            allocations = brute_allocations(
                demands, capacities_list, candidate_indices)
            rows_by_id = {return_id: {'units': units, 'candidates': chosen}
                          for return_id, _, units, chosen in rows}

            report = self.analyze()
            if not allocations:
                infeasible_seen += 1
                self.assertEqual(report['status'], 'infeasible',
                                 'trial %d' % trial)
                certificate = report['infeasibility']
                demanded = certificate['demanded_units']
                available = certificate['available_units']
                self.assertGreater(demanded, available)
                self.assertEqual(
                    demanded - available, certificate['shortfall_units'])
                continue

            feasible_seen += 1
            self.assertEqual(report['status'], 'feasible',
                             'trial %d' % trial)
            affected = {
                recall_id: {index_of[sid] for sid in members
                            if sid in index_of}
                for recall_id, members in recall_members.items()}
            union = set()
            for members in affected.values():
                union |= members

            def check(target, metric_report):
                low, high = metric_extremes(allocations, target)
                self.assertEqual(metric_report['min']['units'], low)
                self.assertEqual(metric_report['max']['units'], high)
                target_ids = {candidate_ids[index] for index in target}
                self.assert_witness_valid(
                    metric_report['min'], rows_by_id, capacities,
                    target_ids, low)
                self.assert_witness_valid(
                    metric_report['max'], rows_by_id, capacities,
                    target_ids, high)

            check(union, report['metrics']['affected_union'])
            for recall_id, members in affected.items():
                check(members, report['metrics']['recalls'][recall_id])
        # Both branches of the algorithm really were exercised.
        self.assertGreater(feasible_seen, 0)
        self.assertGreater(infeasible_seen, 0)


# --------------------------------------------------------------------------
# Scenario 7: 40 binary returns (2**40 schemes) and billion-unit capacity.
# --------------------------------------------------------------------------

class ExponentialAndBillionUnitTest(WriteoffCase):
    def test_forty_binary_returns_with_a_billion_units(self):
        billion = 1_000_000_000
        # Two shipments, each holding exactly one billion pieces; 40 returns
        # of 10 million each (400 million total) may each go to either.
        build_mart(self.mart, [('A', 'C', billion), ('B', 'C', billion)],
                   {'R1': ['A']})
        rows = [('T%02d' % index, 'C', 10_000_000, ['A', 'B'])
                for index in range(40)]
        write_returns(self.returns, rows)
        report = self.analyze()
        self.assertEqual(report['status'], 'feasible')
        union = report['metrics']['affected_union']
        # Every return can be routed entirely to B (min) or entirely to A
        # (max); 40 * 10,000,000 = 400,000,000.
        self.assertEqual(union['min']['units'], 0)
        self.assertEqual(union['max']['units'], 400_000_000)
        self.assertEqual(report['metrics']['recalls']['R1']['max']['units'],
                         400_000_000)
        rows_by_id = {return_id: {'units': units, 'candidates': ['A', 'B']}
                      for return_id, _, units, _ in rows}
        self.assert_witness_valid(union['min'], rows_by_id,
                                  {'A': billion, 'B': billion},
                                  {'A'}, 0)
        self.assert_witness_valid(union['max'], rows_by_id,
                                  {'A': billion, 'B': billion},
                                  {'A'}, 400_000_000)

    def test_billion_unit_endpoints_are_exact(self):
        billion = 1_000_000_000
        build_mart(self.mart, [('A', 'C', billion), ('B', 'C', billion)],
                   {'R1': ['A']})
        # A single return of 600 million pieces split across two billion-unit
        # shipments: the endpoint arithmetic stays exact and integral.
        write_returns(self.returns, [('Q1', 'C', 600_000_000, ['A', 'B'])])
        report = self.analyze()
        union = report['metrics']['affected_union']
        self.assertEqual(union['min']['units'], 0)
        self.assertEqual(union['max']['units'], 600_000_000)


# --------------------------------------------------------------------------
# Input rejection and read-only guarantees.
# --------------------------------------------------------------------------

class InputRejectionTest(WriteoffCase):
    def _base(self):
        build_mart(self.mart, [('S1', 'C1', 5)], {'R1': ['S1']})
        write_returns(self.returns, [('Q1', 'C1', 4, ['S1'])])

    def _expect(self, fragment):
        with self.assertRaises(WriteoffInputError) as caught:
            self.analyze()
        self.assertIn(fragment, str(caught.exception))

    def test_bad_units_and_header(self):
        self._base()
        for bad in ('0', '1000000001', '-1', '1.5', ' 5', '0x1', '１２'):
            write_returns(self.returns, [('Q1', 'C1', bad, ['S1'])])
            with self.subTest(bad=bad):
                self._expect('units')
        self.returns.write_text('wrong,header\n1,2\n', encoding='utf-8')
        self._expect('header')

    def test_non_json_and_empty_candidates(self):
        self._base()
        self.returns.write_text(
            'return_id,customer_id,units,candidate_shipment_ids\n'
            'Q1,C1,4,not-json\n', encoding='utf-8')
        self._expect('JSON')
        write_returns(self.returns, [('Q1', 'C1', 4, [])])
        self._expect('non-empty')

    def test_duplicate_return_and_duplicate_candidate(self):
        self._base()
        write_returns(self.returns, [
            ('Q1', 'C1', 4, ['S1']), ('Q1', 'C1', 3, ['S1'])])
        self._expect('duplicates return_id')
        write_returns(self.returns, [('Q1', 'C1', 4, ['S1', 'S1'])])
        self._expect('repeats a candidate')

    def test_unknown_candidate_and_cross_customer(self):
        self._base()
        write_returns(self.returns, [('Q1', 'C1', 4, ['S9'])])
        self._expect('does not exist')
        build_mart(self.mart, [('S1', 'C1', 5)], {'R1': ['S1']})
        write_returns(self.returns, [('Q1', 'C2', 4, ['S1'])])
        self._expect('belongs to customer')

    def test_embedded_newline_in_quoted_field_rejected(self):
        self._base()
        self.returns.write_bytes(
            b'return_id,customer_id,units,candidate_shipment_ids\n'
            b'Q1,C1,4,"[\n"S1"]"\n')
        self._expect('embedded line break')

    def test_incompatible_or_missing_mart(self):
        build_mart(self.mart, [('S1', 'C1', 5)], {'R1': ['S1']})
        write_returns(self.returns, [('Q1', 'C1', 4, ['S1'])])
        with sqlite3.connect(self.mart) as con:
            con.execute("UPDATE meta SET meta_value='wrong' "
                        "WHERE meta_key='format'")
        self._expect('format')
        missing = self.dir / 'missing.sqlite'
        with self.assertRaises(WriteoffInputError):
            analyze_writeoff_intervals(missing, self.returns)

    def test_mart_is_not_modified_by_success_or_failure(self):
        self._base()
        before = hashlib.sha256(self.mart.read_bytes()).hexdigest()
        self.analyze()
        self.assertEqual(hashlib.sha256(self.mart.read_bytes()).hexdigest(),
                         before)
        write_returns(self.returns, [('Q1', 'C1', 999, ['S1'])])
        report = self.analyze()
        self.assertEqual(report['status'], 'infeasible')
        self.assertEqual(hashlib.sha256(self.mart.read_bytes()).hexdigest(),
                         before)

    def test_invalid_input_never_returns_partial_result(self):
        self._base()
        write_returns(self.returns, [
            ('Q1', 'C1', 4, ['S1']),
            ('Q2', 'C1', 0, ['S1']),  # invalid second row
        ])
        with self.assertRaises(WriteoffInputError):
            self.analyze()


# --------------------------------------------------------------------------
# Determinism and the standard-library CLI.
# --------------------------------------------------------------------------

class DeterminismTest(WriteoffCase):
    @staticmethod
    def _business(report):
        # The raw-byte digest tracks the physical file and may legitimately
        # change with row order; the analytical content must not.
        return json.dumps(
            {key: value for key, value in report.items()
             if key != 'inputs'},
            ensure_ascii=False, sort_keys=True, separators=(',', ':'))

    def test_row_order_does_not_change_the_report(self):
        build_mart(self.mart, [('S1', 'C1', 3), ('S2', 'C1', 5)],
                   {'R1': ['S1'], 'R2': ['S2']})
        first = [('Q1', 'C1', 4, ['S1', 'S2']),
                 ('Q2', 'C1', 3, ['S2'])]
        write_returns(self.returns, first)
        report_one = self._business(self.analyze())
        write_returns(self.returns, list(reversed(first)))
        report_two = self._business(self.analyze())
        self.assertEqual(report_one, report_two)

    def test_candidate_array_order_does_not_change_the_report(self):
        build_mart(self.mart, [('S1', 'C1', 3), ('S2', 'C1', 5)],
                   {'R1': ['S1']})
        write_returns(self.returns, [('Q1', 'C1', 4, ['S1', 'S2'])])
        one = self._business(self.analyze())
        write_returns(self.returns, [('Q1', 'C1', 4, ['S2', 'S1'])])
        two = self._business(self.analyze())
        self.assertEqual(one, two)


class CliEndToEndTest(WriteoffCase):
    def test_cli_writes_report_and_prints_it(self):
        build_mart(self.mart, [('S1', 'C1', 3), ('S2', 'C1', 7)],
                   {'R1': ['S1']})
        write_returns(self.returns, [('Q1', 'C1', 7, ['S1', 'S2'])])
        env = dict(os.environ, PYTHONPATH=str(SRC))
        run = subprocess.run(
            [sys.executable, '-m', 'recall_collect.writeoff',
             '--mart', str(self.mart),
             '--returns', str(self.returns),
             '--report', str(self.report)],
            cwd=str(ROOT), env=env, text=True, capture_output=True, check=True)
        printed = json.loads(run.stdout)
        written = json.loads(self.report.read_text(encoding='utf-8'))
        self.assertEqual(printed, written)
        self.assertEqual(printed['status'], 'feasible')
        self.assertEqual(
            printed['metrics']['affected_union']['max']['units'], 3)

    def test_cli_rejects_report_aliasing_the_mart(self):
        build_mart(self.mart, [('S1', 'C1', 3)], {'R1': ['S1']})
        write_returns(self.returns, [('Q1', 'C1', 3, ['S1'])])
        env = dict(os.environ, PYTHONPATH=str(SRC))
        before = hashlib.sha256(self.mart.read_bytes()).hexdigest()
        run = subprocess.run(
            [sys.executable, '-m', 'recall_collect.writeoff',
             '--mart', str(self.mart),
             '--returns', str(self.returns),
             '--report', str(self.mart)],
            cwd=str(ROOT), env=env, text=True, capture_output=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('write-off analysis failed', run.stderr)
        self.assertEqual(hashlib.sha256(self.mart.read_bytes()).hexdigest(),
                         before)

    def test_cli_failure_is_nonzero_and_leaves_existing_report(self):
        build_mart(self.mart, [('S1', 'C1', 3)], {'R1': ['S1']})
        write_returns(self.returns, [('Q1', 'C1', 3, ['S1'])])
        self.report.write_text('previous', encoding='utf-8')
        write_returns(self.returns, [('Q1', 'C1', 0, ['S1'])])
        env = dict(os.environ, PYTHONPATH=str(SRC))
        run = subprocess.run(
            [sys.executable, '-m', 'recall_collect.writeoff',
             '--mart', str(self.mart),
             '--returns', str(self.returns),
             '--report', str(self.report)],
            cwd=str(ROOT), env=env, text=True, capture_output=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(self.report.read_text(encoding='utf-8'), 'previous')
        leftovers = list(self.dir.glob(self.report.name + '.*.tmp'))
        self.assertEqual(leftovers, [])

    def test_cli_missing_input_is_nonzero(self):
        env = dict(os.environ, PYTHONPATH=str(SRC))
        run = subprocess.run(
            [sys.executable, '-m', 'recall_collect.writeoff',
             '--mart', str(self.dir / 'nope.sqlite'),
             '--returns', str(self.returns),
             '--report', str(self.report)],
            cwd=str(ROOT), env=env, text=True, capture_output=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertFalse(self.report.exists())


class RealMartEndToEndTest(unittest.TestCase):
    """Exercise the real ``recall-impact-mart/1`` schema end to end."""

    def test_reads_a_mart_built_from_the_fixtures(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            recalls_db = base / 'recalls.sqlite'
            collect(SITE, recalls_db)
            mart = base / 'impact.sqlite'
            build_impact_mart(
                recalls_db, FACTORY / 'products.csv', FACTORY / 'lots.csv',
                FACTORY / 'process-members.csv', FACTORY / 'shipments.csv',
                mart)
            env = dict(os.environ, PYTHONPATH=str(SRC))
            report = base / 'report.json'
            run = subprocess.run(
                [sys.executable, '-m', 'recall_collect.writeoff',
                 '--mart', str(mart),
                 '--returns', str(FACTORY / 'returns.csv'),
                 '--report', str(report)],
                cwd=str(ROOT), env=env, text=True, capture_output=True,
                check=True)
            parsed = json.loads(run.stdout)
            self.assertEqual(parsed, json.loads(
                report.read_text(encoding='utf-8')))
            # Fixture expectations derived by hand from the shipments CSV:
            # S1=FIN/F (10, linked to both recalls), S2=FIN/D (5, R001),
            # S3=OAT/SAFE (7, not linked).  Q1=6 over {S1,S2,S3}, Q2=4 over
            # {S2,S3}; union = {S1,S2}.  Q2 caps S3 usage at 4, so S1+S2 must
            # carry at least 6; and S2 cap 5 means the union max is 10.
            self.assertEqual(parsed['status'], 'feasible')
            union = parsed['metrics']['affected_union']
            self.assertEqual(union['target_shipment_ids'], ['S1', 'S2'])
            self.assertEqual(union['min']['units'], 3)
            self.assertEqual(union['max']['units'], 10)
            self.assertEqual(
                sorted(parsed['metrics']['recalls']), ['R001', 'R002'])
            self.assertEqual(
                parsed['metrics']['recalls']['R002']['max']['units'], 6)
            # The mart stays byte-identical after a read-only analysis.
            before = hashlib.sha256(mart.read_bytes()).hexdigest()
            analyze_writeoff_intervals(mart, FACTORY / 'returns.csv')
            self.assertEqual(
                hashlib.sha256(mart.read_bytes()).hexdigest(), before)


# --------------------------------------------------------------------------
# Flow kernel sanity (independent of the analysis layer).
# --------------------------------------------------------------------------

class FlowKernelTest(unittest.TestCase):
    def test_billion_unit_flow_is_one_augmentation(self):
        network = Dinic(4)
        network.add_edge(0, 1, 1_000_000_000)
        network.add_edge(1, 3, 1_000_000_000)
        self.assertEqual(network.max_flow(0, 3), 1_000_000_000)

    def test_residual_reachable_reports_a_tight_cut(self):
        network = Dinic(4)
        network.add_edge(0, 1, 5)
        handle = network.add_edge(1, 3, 3)
        network.max_flow(0, 3)
        reachable = network.residual_reachable(0)
        self.assertIn(0, reachable)
        self.assertIn(1, reachable)
        self.assertNotIn(3, reachable)
        self.assertEqual(network.edge_flow(handle), 3)


if __name__ == '__main__':
    unittest.main()
