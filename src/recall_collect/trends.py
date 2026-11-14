"""Pure analytical summaries over already accepted recall announcements.

This module never opens a database, never reads the factory CSV exports and
never fetches site pages.  The caller passes accepted announcement records
and receives deterministic monthly statistics; all I/O and notice acceptance
remain the responsibility of the collection and impact layers.

Record shape
------------

Each record is a mapping with exactly these five fields, the same shape
produced by the site parser (:func:`recall_collect.site.notice`) and by the
read-only impact snapshot
(:func:`recall_collect.impact.sources.read_recall_tables`):

    {
        'recall_id': str,           # unique announcement identity
        'published': 'YYYY-MM-DD',  # ISO publication date
        'product':   str,           # notice product text, compared exactly
        'lots':      [str, ...],    # one or more lot code strings
        'reason':    str,           # original reason text, compared exactly
    }

Rows read directly from the ``recalls`` SQLite table store lots in
``lots_json``; callers must parse that column with ``json.loads`` into
``lots`` first (which is what ``read_recall_tables`` already does).

Statistical granularity and date basis
--------------------------------------

* One result entry per calendar month between ``start_month`` and
  ``end_month``, both ends included and formatted ``YYYY-MM``.  Months with
  no announcements are still emitted with zero values.
* Announcements are assigned to a month solely by the calendar month of
  their ``published`` date.  Records outside the inclusive range are ignored;
  invalid records still make the whole call fail.
* ``announcement_lot_pairs`` counts distinct ``(announcement, lot)`` pairs:
  a lot repeated inside one announcement counts once, while the same lot in
  two different announcements counts once per announcement.
* ``distinct_products`` counts distinct exact ``product`` strings.
* ``reason_counts`` groups announcements by the original, exact ``reason``
  string (no trimming, case folding or normalization); groups are emitted
  sorted by Unicode code point so output never depends on input row order.

Failure contract
----------------

Every input is validated before any result is built, so a failure never
returns partial statistics.  Illegal month strings, reversed ranges,
duplicate ``recall_id`` values and malformed records all raise
:class:`TrendInputError` (a ``ValueError`` subclass).
"""
import re
from collections.abc import Mapping
from datetime import date

RECORD_KEYS = ('recall_id', 'published', 'product', 'lots', 'reason')
_MONTH_RE = re.compile(r'^([0-9]{4})-([0-9]{2})$')


class TrendInputError(ValueError):
    """Raised for any invalid month range or announcement record."""


def _parse_month(value, label):
    if not isinstance(value, str):
        raise TrendInputError('%s must be a YYYY-MM string' % label)
    match = _MONTH_RE.match(value)
    if match is None:
        raise TrendInputError(
            '%s must be a YYYY-MM string, got %r' % (label, value))
    year, month = int(match.group(1)), int(match.group(2))
    try:
        date(year, month, 1)
    except ValueError as exc:
        raise TrendInputError(
            '%s is not a calendar month: %r' % (label, value)) from exc
    return year, month


def _iter_months(start, end):
    year, month = start
    end_year, end_month = end
    while (year, month) <= (end_year, end_month):
        yield '%04d-%02d' % (year, month)
        month += 1
        if month == 13:
            month = 1
            year += 1


def _require_text(row, index, field):
    value = row[field]
    if not isinstance(value, str) or not value.strip():
        raise TrendInputError(
            'row %d: %s must be a non-empty string' % (index, field))
    return value


def _validate_rows(rows):
    """Validate every record up front; never emit partial aggregates."""
    if isinstance(rows, (str, bytes)) or not hasattr(rows, '__iter__'):
        raise TrendInputError(
            'rows must be an iterable of announcement record mappings')
    normalized = []
    seen_ids = set()
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise TrendInputError(
                'row %d: record must be a mapping' % index)
        if frozenset(row) != frozenset(RECORD_KEYS):
            raise TrendInputError(
                'row %d: record fields must be exactly %r'
                % (index, list(RECORD_KEYS)))
        recall_id = _require_text(row, index, 'recall_id')
        published = _require_text(row, index, 'published')
        product = _require_text(row, index, 'product')
        reason = _require_text(row, index, 'reason')
        # Same strict ISO-date rule as the site parser: fromisoformat alone
        # accepts compact forms, so require the canonical round trip.
        try:
            parsed = date.fromisoformat(published)
        except ValueError as exc:
            raise TrendInputError(
                'row %d: published must be a valid YYYY-MM-DD date, got %r'
                % (index, published)) from exc
        if parsed.isoformat() != published:
            raise TrendInputError(
                'row %d: published must be canonical YYYY-MM-DD, got %r'
                % (index, published))
        lots = row['lots']
        if type(lots) is not list or not lots:
            raise TrendInputError(
                'row %d: lots must be a non-empty list of strings' % index)
        for lot in lots:
            if not isinstance(lot, str) or not lot.strip():
                raise TrendInputError(
                    'row %d: lot codes must be non-empty strings' % index)
        if recall_id in seen_ids:
            raise TrendInputError('duplicate recall_id: %r' % recall_id)
        seen_ids.add(recall_id)
        normalized.append((recall_id, published, product, list(lots), reason))
    return normalized


def summarize_monthly(rows, start_month, end_month):
    """Summarize accepted announcement records by publication month.

    ``rows`` is any iterable of accepted records in the documented mapping
    shape.  ``start_month`` and ``end_month`` are inclusive ``YYYY-MM``
    bounds.  Returns a chronologically ordered list with one mapping per
    calendar month (empty months included) containing the keys
    ``month``, ``announcement_count``, ``announcement_lot_pairs``,
    ``distinct_products`` and ``reason_counts``.

    Raises :class:`TrendInputError` for any invalid input; no partial
    result is ever returned.
    """
    start = _parse_month(start_month, 'start_month')
    end = _parse_month(end_month, 'end_month')
    if start > end:
        raise TrendInputError(
            'start_month %r is after end_month %r'
            % (start_month, end_month))
    # Full validation happens before aggregation, so an invalid stream can
    # never leave behind partially computed months.
    records = _validate_rows(rows)

    months = list(_iter_months(start, end))
    buckets = {
        month: {
            'announcement_count': 0,
            'announcement_lot_pairs': 0,
            'products': set(),
            'reason_counts': {},
        }
        for month in months
    }

    for _recall_id, published, product, lots, reason in records:
        bucket = buckets.get(published[:7])
        if bucket is None:
            continue  # outside the inclusive requested month range
        bucket['announcement_count'] += 1
        # Set semantics inside one announcement: repeated lots count once.
        bucket['announcement_lot_pairs'] += len(set(lots))
        bucket['products'].add(product)
        bucket['reason_counts'][reason] = \
            bucket['reason_counts'].get(reason, 0) + 1

    result = []
    for month in months:
        bucket = buckets[month]
        result.append({
            'month': month,
            'announcement_count': bucket['announcement_count'],
            'announcement_lot_pairs': bucket['announcement_lot_pairs'],
            'distinct_products': len(bucket['products']),
            'reason_counts': {
                reason: bucket['reason_counts'][reason]
                for reason in sorted(bucket['reason_counts'])
            },
        })
    return result
