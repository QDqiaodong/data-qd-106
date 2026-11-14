"""Strict reader for the returns CSV used by the write-off analysis.

Fixed header ``return_id,customer_id,units,candidate_shipment_ids``.  Every
file is read exactly once as raw bytes and those same bytes feed both parsing
and the SHA-256 digest recorded in the report.  The candidate shipment
column is a JSON array of shipment-id strings.

Validation is deliberately strict and total: an invalid row rejects the
whole analysis before any result is produced, and the source mart is never
touched.  Identifiers are compared byte-for-byte after UTF-8 decoding (no
trimming, case folding or zero padding).
"""
import csv
import hashlib
import json

from .errors import WriteoffInputError

HEADER = ('return_id', 'customer_id', 'units', 'candidate_shipment_ids')

UNITS_MIN = 1
UNITS_MAX = 1_000_000_000


def _nonempty(value):
    return isinstance(value, str) and value.strip() != ''


def read_returns_csv(path):
    """Read and fully validate one returns CSV.

    Returns a dict with the raw bytes, SHA-256 and an ordered list of
    normalized return records.  Raises :class:`WriteoffInputError` on any
    malformed input; nothing is returned partially.
    """
    try:
        with open(path, 'rb') as handle:
            raw = handle.read()
    except OSError as exc:
        raise WriteoffInputError(
            'cannot read returns CSV: %s' % exc) from exc
    digest = hashlib.sha256(raw).digest().hex()
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise WriteoffInputError(
            'returns CSV is not valid UTF-8: %s' % exc) from exc

    physical = text.splitlines()
    if not physical:
        raise WriteoffInputError('returns CSV is empty (header required)')
    # One record per physical line: reject embedded line breaks explicitly.
    try:
        header = tuple(next(iter(csv.reader([physical[0]]))))
    except csv.Error as exc:
        raise WriteoffInputError(
            'returns CSV header is malformed: %s' % exc) from exc
    if header != HEADER:
        raise WriteoffInputError(
            'returns CSV header %r does not match ordered header %r'
            % (header, HEADER))
    try:
        whole = [row for row in csv.reader(physical) if row != []]
    except csv.Error as exc:
        raise WriteoffInputError(
            'returns CSV is malformed: %s' % exc) from exc
    if len(whole) != sum(1 for line in physical if line != ''):
        raise WriteoffInputError(
            'returns CSV contains an embedded line break inside a quoted '
            'field; one physical line per record is required')

    records = []
    seen_ids = {}
    position = 0
    for line_no, line in enumerate(physical[1:], start=2):
        if line == '':
            continue
        try:
            parsed = next(iter(csv.reader([line], strict=True)))
        except csv.Error as exc:
            raise WriteoffInputError(
                'returns CSV line %d is malformed: %s' % (line_no, exc)) \
                from exc
        position += 1
        if len(parsed) != len(HEADER):
            raise WriteoffInputError(
                'returns CSV data record %d (line %d) has %d fields, '
                'expected %d' % (position, line_no, len(parsed),
                                 len(HEADER)))
        if not all(_nonempty(value) for value in parsed):
            raise WriteoffInputError(
                'returns CSV data record %d (line %d) contains an empty '
                'or all-whitespace value' % (position, line_no))
        return_id, customer_id, units_text, candidates_text = parsed

        if not units_text.isascii() or not units_text.isdigit():
            raise WriteoffInputError(
                'returns CSV data record %d has non-integer units %r'
                % (position, units_text))
        units = int(units_text)
        if not UNITS_MIN <= units <= UNITS_MAX:
            raise WriteoffInputError(
                'returns CSV data record %d units %s outside %d..%d'
                % (position, units_text, UNITS_MIN, UNITS_MAX))

        try:
            candidates = json.loads(candidates_text)
        except ValueError as exc:
            raise WriteoffInputError(
                'returns CSV data record %d candidate_shipment_ids is not '
                'valid JSON: %s' % (position, exc)) from exc
        if type(candidates) is not list or not candidates:
            raise WriteoffInputError(
                'returns CSV data record %d candidate_shipment_ids must be '
                'a non-empty JSON array of shipment-id strings'
                % position)
        for value in candidates:
            if not isinstance(value, str) or not value.strip():
                raise WriteoffInputError(
                    'returns CSV data record %d candidate shipment ids '
                    'must be non-empty strings' % position)
        if len(set(candidates)) != len(candidates):
            raise WriteoffInputError(
                'returns CSV data record %d repeats a candidate shipment id'
                % position)

        if return_id in seen_ids:
            raise WriteoffInputError(
                'returns CSV data record %d duplicates return_id %r '
                '(first seen at data record %d)'
                % (position, return_id, seen_ids[return_id]))
        seen_ids[return_id] = position

        records.append({
            'position': position,
            'return_id': return_id,
            'customer_id': customer_id,
            'units': units,
            'units_text': units_text,
            'candidates': tuple(candidates),
            'raw_line': line,
        })

    return {
        'path': str(path),
        'raw': raw,
        'sha256': digest,
        'header': header,
        'records': records,
    }
