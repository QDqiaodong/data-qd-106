import json
import unittest

from recall_collect.site import notice
from recall_collect.trends import TrendInputError, summarize_monthly


def row(recall_id, published, product, lots, reason):
    """Factory for synthetic accepted-announcement records (no DB, no CSV)."""
    return {
        'recall_id': recall_id,
        'published': published,
        'product': product,
        'lots': list(lots),
        'reason': reason,
    }


# Hand-built dataset. Every expectation below is computed from these literals,
# never produced by the function under test.
A = row('R1', '2025-12-31', 'Pudding', ['a', 'b', 'b'], 'contamination')
B = row('R2', '2026-01-02', 'Jam', ['x'], 'Contamination')
C = row('R3', '2026-01-15', 'Pudding', ['a', 'c'], 'contamination')
D = row('R4', '2026-02-28', 'Jam', ['a', 'a', 'd'], 'labelling')
E = row('R5', '2025-11-30', 'Tea', ['z'], 'contamination')   # before range
F = row('R6', '2026-03-01', 'Tea', ['q'], 'labelling')       # after range


class MonthlyTrendTest(unittest.TestCase):
    def test_cross_year_boundaries_empty_month_and_full_shape(self):
        result = summarize_monthly(
            [A, B, C, D, E, F], '2025-12', '2026-02')
        self.assertEqual([m['month'] for m in result],
                         ['2025-12', '2026-01', '2026-02'])
        self.assertEqual(result, [
            {'month': '2025-12', 'announcement_count': 1,
             'announcement_lot_pairs': 2, 'distinct_products': 1,
             'reason_counts': {'contamination': 1}},
            {'month': '2026-01', 'announcement_count': 2,
             'announcement_lot_pairs': 3, 'distinct_products': 2,
             'reason_counts': {'Contamination': 1, 'contamination': 1}},
            {'month': '2026-02', 'announcement_count': 1,
             'announcement_lot_pairs': 2, 'distinct_products': 1,
             'reason_counts': {'labelling': 1}},
        ])

    def test_month_edges_use_published_calendar_month(self):
        records = [
            row('R1', '2025-12-31', 'P', ['l1'], 'r'),
            row('R2', '2026-01-01', 'P', ['l1'], 'r'),
            row('R3', '2026-01-31', 'Q', ['l1', 'l2'], 'r'),
            row('R4', '2026-02-01', 'P', ['l1'], 'r'),
        ]
        result = summarize_monthly(records, '2026-01', '2026-01')
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], {
            'month': '2026-01', 'announcement_count': 2,
            'announcement_lot_pairs': 3, 'distinct_products': 2,
            'reason_counts': {'r': 2}})

    def test_empty_month_emits_zeroes_and_empty_rows(self):
        records = [
            row('R1', '2026-01-10', 'P', ['l1'], 'r'),
            row('R2', '2026-03-05', 'P', ['l2'], 'r'),
        ]
        result = summarize_monthly(records, '2026-01', '2026-03')
        self.assertEqual([m['month'] for m in result],
                         ['2026-01', '2026-02', '2026-03'])
        self.assertEqual(result[1], {
            'month': '2026-02', 'announcement_count': 0,
            'announcement_lot_pairs': 0, 'distinct_products': 0,
            'reason_counts': {}})

        zero = summarize_monthly([], '2026-01', '2026-02')
        self.assertEqual([m['month'] for m in zero],
                         ['2026-01', '2026-02'])
        self.assertTrue(all(m['announcement_count'] == 0
                            and m['announcement_lot_pairs'] == 0
                            and m['distinct_products'] == 0
                            and m['reason_counts'] == {} for m in zero))

    def test_records_outside_range_are_ignored(self):
        result = summarize_monthly([A, B, C, D, E, F], '2026-02', '2026-02')
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['announcement_count'], 1)
        self.assertEqual(result[0]['reason_counts'], {'labelling': 1})

        same_start_end = summarize_monthly([B], '2026-01', '2026-01')
        self.assertEqual(same_start_end[0]['month'], '2026-01')
        self.assertEqual(same_start_end[0]['announcement_count'], 1)

    def test_reason_grouping_is_exact_and_stably_sorted(self):
        records = [
            row('R1', '2026-01-01', 'P1', ['l1'], 'Stop Sale'),
            row('R2', '2026-01-02', 'P2', ['l2'], 'stop sale'),
            row('R3', '2026-01-03', 'P3', ['l3'], 'Stop Sale'),
            row('R4', '2026-01-04', 'P4', ['l4'], 'Stop  Sale'),
        ]
        result = summarize_monthly(records, '2026-01', '2026-01')[0]
        # Exact text: case and internal whitespace are preserved.
        self.assertEqual(result['reason_counts'], {
            'Stop  Sale': 1, 'Stop Sale': 2, 'stop sale': 1})
        # Unicode code-point ordering, independent of row order.
        self.assertEqual(list(result['reason_counts']),
                         ['Stop  Sale', 'Stop Sale', 'stop sale'])

    def test_lot_dedup_within_announcement_not_across_announcements(self):
        records = [
            row('R1', '2026-01-01', 'P', ['L1', 'L1', 'L2', 'L2', 'L3'], 'r'),
            row('R2', '2026-01-02', 'Q', ['L1'], 'r'),
        ]
        result = summarize_monthly(records, '2026-01', '2026-01')[0]
        self.assertEqual(result['announcement_count'], 2)
        self.assertEqual(result['announcement_lot_pairs'], 4)  # 3 + 1
        self.assertEqual(result['distinct_products'], 2)

        shared = [
            row('R1', '2026-01-01', 'P', ['L9', 'L9'], 'r'),
            row('R2', '2026-01-02', 'Q', ['L9'], 'r'),
        ]
        shared_result = summarize_monthly(shared, '2026-01', '2026-01')[0]
        self.assertEqual(shared_result['announcement_lot_pairs'], 2)

    def test_totals_reconcile_across_months(self):
        result = summarize_monthly(
            [A, B, C, D, E, F], '2025-12', '2026-02')
        self.assertEqual(sum(m['announcement_count'] for m in result), 4)
        self.assertEqual(sum(m['announcement_lot_pairs'] for m in result), 7)
        # Every announcement is attributed to exactly one exact reason group.
        for month in result:
            self.assertEqual(sum(month['reason_counts'].values()),
                             month['announcement_count'])
        all_reasons = {}
        for month in result:
            for reason, count in month['reason_counts'].items():
                all_reasons[reason] = all_reasons.get(reason, 0) + count
        self.assertEqual(all_reasons,
                         {'contamination': 2, 'Contamination': 1,
                          'labelling': 1})
        self.assertEqual(sum(all_reasons.values()), 4)

    def test_stable_output_under_input_permutation(self):
        ordered = [A, B, C, D, E, F]
        baseline = summarize_monthly(ordered, '2025-12', '2026-02')
        shuffled = [F, D, B, E, C, A]
        self.assertEqual(
            summarize_monthly(shuffled, '2025-12', '2026-02'), baseline)
        reversed_rows = list(reversed(ordered))
        self.assertEqual(
            summarize_monthly(reversed_rows, '2025-12', '2026-02'), baseline)

    def test_input_rows_are_not_mutated(self):
        records = [
            row('R1', '2026-01-01', 'P', ['L1', 'L1', 'L2'], 'r'),
            row('R2', '2026-01-02', 'Q', ['L2'], 'r'),
        ]
        snapshot = json.loads(json.dumps(records))
        summarize_monthly(records, '2026-01', '2026-02')
        self.assertEqual(records, snapshot)

    def test_duplicate_recall_id_fails_even_when_identical_or_out_of_range(self):
        dup = [A, row('R1', '2026-01-01', 'Other', ['z'], 'other')]
        with self.assertRaises(TrendInputError):
            summarize_monthly(dup, '2025-12', '2026-02')
        identical = [A, json.loads(json.dumps(A))]
        with self.assertRaises(TrendInputError):
            summarize_monthly(identical, '2025-12', '2026-02')
        # A duplicate hidden in an out-of-range row still fails the whole call.
        hidden = [B, row('R2', '2030-01-01', 'X', ['z'], 'x')]
        with self.assertRaises(TrendInputError):
            summarize_monthly(hidden, '2026-01', '2026-01')

    def test_illegal_months_and_reversed_range_fail(self):
        for bad in ('2026-13', '2026-00', '2026-1', '202601', '',
                    '2026-01-01', None, 202601):
            with self.assertRaises(TrendInputError):
                summarize_monthly([], bad, '2026-02')
            with self.assertRaises(TrendInputError):
                summarize_monthly([], '2026-01', bad)
        with self.assertRaises(TrendInputError):
            summarize_monthly([], '2026-02', '2026-01')

    def test_invalid_records_fail_without_partial_results(self):
        valid = row('R1', '2026-01-01', 'P', ['l1'], 'r')
        bad_cases = [
            'not-a-mapping',
            {k: v for k, v in valid.items() if k != 'reason'},          # missing
            dict(valid, extra='x'),                                     # extra
            row('', '2026-01-01', 'P', ['l1'], 'r'),                    # blank id
            row('R2', '2026-01-01', '  ', ['l1'], 'r'),                 # blank product
            row('R2', '2026-02-30', 'P', ['l1'], 'r'),                  # bad date
            row('R2', '2026-1-1', 'P', ['l1'], 'r'),                    # non-ISO date
            row('R2', '20260101', 'P', ['l1'], 'r'),                    # compact date
            row('R2', 20260101, 'P', ['l1'], 'r'),                      # non-string
            row('R2', '2026-01-01', 'P', [], 'r'),                      # empty lots
            {**row('R2', '2026-01-01', 'P', [], 'r'),
             'lots': ('l1',)},                                          # lots not list
            row('R2', '2026-01-01', 'P', ['l1', ''], 'r'),              # blank lot
            row('R2', '2026-01-01', 'P', ['l1', 9], 'r'),              # non-string lot
            row('R2', '2026-01-01', 'P', ['l1'], ''),                   # blank reason
        ]
        for bad in bad_cases:
            with self.assertRaises(TrendInputError):
                summarize_monthly([valid, bad], '2026-01', '2026-02')
        with self.assertRaises(TrendInputError):
            summarize_monthly(123, '2026-01', '2026-02')
        with self.assertRaises(TrendInputError):
            summarize_monthly('R1', '2026-01', '2026-02')

    def test_accepts_parser_and_snapshot_record_shapes(self):
        # Same five-key mapping produced by site.notice and by
        # impact.sources.read_recall_tables (where lots is a parsed list).
        notices = [
            notice(json.dumps({
                'recall_id': 'R001', 'published': '2026-01-03',
                'product': 'Synthetic oat bar',
                'lots': ['L2', 'L1', 'L1'],
                'reason': 'Undeclared synthetic allergen'})),
            notice(json.dumps({
                'recall_id': 'R002', 'published': '2026-01-04',
                'product': 'Synthetic soup', 'lots': ['B9'],
                'reason': 'Simulated packaging fault'})),
        ]
        result = summarize_monthly(notices, '2026-01', '2026-01')
        self.assertEqual(result, [{
            'month': '2026-01', 'announcement_count': 2,
            'announcement_lot_pairs': 3, 'distinct_products': 2,
            'reason_counts': {
                'Simulated packaging fault': 1,
                'Undeclared synthetic allergen': 1}}])


if __name__ == '__main__':
    unittest.main()
