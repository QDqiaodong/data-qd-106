"""Capture and validate the four factory CSV exports.

Each file is read once as raw bytes; the very same bytes feed CSV parsing,
the stored original content and the SHA-256 digest.  Identifiers and lot
codes are compared byte-for-byte after UTF-8 decoding (no trimming, no case
folding, no zero padding).  Empty or all-whitespace values are rejected.
"""
import csv
import hashlib

from .sources import ImpactBuildError

HEADERS = {
    'products': ('product_id', 'notice_product'),
    'lots': ('product_id', 'lot_code'),
    'process-members': ('process_id', 'role', 'product_id', 'lot_code'),
    'shipments': ('shipment_id', 'customer_id', 'product_id', 'lot_code',
                  'units'),
}


def _nonempty(value):
    return isinstance(value, str) and value.strip() != ''


def capture_csv(path, kind):
    """Read one CSV file once and return its captured bundle.

    The bundle contains the raw bytes, the SHA-256 hex digest over those
    exact bytes and parsed data records.  Record positions are 1-based data
    record numbers; the header line is not counted.
    """
    try:
        with open(path, 'rb') as handle:
            raw = handle.read()
    except OSError as exc:
        raise ImpactBuildError('cannot read %s CSV: %s' % (kind, exc)) from exc
    digest = hashlib.sha256(raw).digest().hex()
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ImpactBuildError('%s CSV is not valid UTF-8: %s' % (kind, exc)) \
            from exc

    # Parse physical lines individually so the stored raw_line is the exact
    # source text (quoting preserved, line terminator stripped) for each data
    # record.  Blank physical separator lines are ignored; their absence does
    # not renumber the 1-based data-record positions.
    physical = text.splitlines()
    if not physical:
        raise ImpactBuildError('%s CSV is empty (header required)' % kind)
    # The factory contract uses one record per physical line; quoted fields
    # with embedded line breaks are rejected explicitly instead of being
    # silently split.  With an embedded newline, csv.reader merges lines into
    # one record, so its non-empty record count falls below the physical
    # non-empty line count.
    try:
        whole = [row for row in csv.reader(physical) if row != []]
    except csv.Error as exc:
        raise ImpactBuildError(
            '%s CSV is malformed: %s' % (kind, exc)) from exc
    if len(whole) != sum(1 for line in physical if line != ''):
        raise ImpactBuildError(
            '%s CSV contains an embedded line break inside a quoted field; '
            'one physical line per record is required' % kind)
    try:
        header = tuple(next(iter(csv.reader([physical[0]]))))
    except csv.Error as exc:
        raise ImpactBuildError('%s CSV header is malformed: %s'
                               % (kind, exc)) from exc
    expected = HEADERS[kind]
    if header != expected:
        raise ImpactBuildError(
            '%s CSV header %r does not match ordered header %r'
            % (kind, header, expected))

    records = []
    position = 0
    for line_no, line in enumerate(physical[1:], start=2):
        if line == '':
            continue
        try:
            parsed = next(iter(csv.reader([line])))
        except csv.Error as exc:
            raise ImpactBuildError(
                '%s CSV data line %d is malformed: %s'
                % (kind, line_no, exc)) from exc
        position += 1
        # An all-whitespace field is a real value and is rejected below like
        # any blank value; only truly empty separator lines were skipped.
        if len(parsed) != len(expected):
            raise ImpactBuildError(
                '%s CSV data record %d (line %d) has %d fields, expected %d'
                % (kind, position, line_no, len(parsed), len(expected)))
        if not all(_nonempty(v) for v in parsed):
            raise ImpactBuildError(
                '%s CSV data record %d (line %d) contains an empty or '
                'all-whitespace value' % (kind, position, line_no))
        records.append({'position': position, 'values': tuple(parsed),
                        'raw_line': line})
    return {'kind': kind, 'path': str(path), 'raw': raw,
            'sha256': digest, 'header': header, 'records': records}


def parse_bundles(bundles):
    """Validate cross-file rules and return the structured factory model."""
    by_kind = {b['kind']: b for b in bundles}
    products_b = by_kind['products']
    lots_b = by_kind['lots']
    members_b = by_kind['process-members']
    shipments_b = by_kind['shipments']

    # products: each notice_product may appear once; internal products may
    # carry several notice names.
    name_to_product = {}
    product_names = {}
    for rec in products_b['records']:
        product_id, notice_product = rec['values']
        if notice_product in name_to_product:
            raise ImpactBuildError(
                'products CSV record %d duplicates notice_product %r'
                % (rec['position'], notice_product))
        name_to_product[notice_product] = product_id
        product_names.setdefault(product_id, []).append(notice_product)
    if not products_b['records']:
        raise ImpactBuildError('products CSV needs at least one mapping')

    # lots: identity is the (product_id, lot_code) pair.
    lots = {}
    for rec in lots_b['records']:
        product_id, lot_code = rec['values']
        if product_id not in product_names:
            raise ImpactBuildError(
                'lots CSV record %d references undeclared product_id %r'
                % (rec['position'], product_id))
        key = (product_id, lot_code)
        if key in lots:
            raise ImpactBuildError(
                'lots CSV record %d duplicates batch %r / %r'
                % (rec['position'], product_id, lot_code))
        lots[key] = {'product_id': product_id, 'lot_code': lot_code,
                     'position': rec['position']}

    # process members.
    processes = {}
    member_seen = set()
    for rec in members_b['records']:
        process_id, role, product_id, lot_code = rec['values']
        if role not in ('input', 'output'):
            raise ImpactBuildError(
                'process-members CSV record %d has role %r; only input/'
                'output are accepted' % (rec['position'], role))
        lot_key = (product_id, lot_code)
        if lot_key not in lots:
            raise ImpactBuildError(
                'process-members CSV record %d references unregistered '
                'batch %r / %r' % (rec['position'], product_id, lot_code))
        member = (process_id, role, product_id, lot_code)
        if member in member_seen:
            raise ImpactBuildError(
                'process-members CSV record %d duplicates member %r'
                % (rec['position'], member))
        member_seen.add(member)
        proc = processes.setdefault(process_id, {'inputs': [], 'outputs': []})
        proc[role + 's'].append(lot_key)

    for process_id, proc in processes.items():
        if not proc['inputs'] or not proc['outputs']:
            raise ImpactBuildError(
                'process %r needs at least one input and one output'
                % process_id)

    # A batch may be produced by at most one process.
    producer = {}
    for process_id, proc in processes.items():
        for lot_key in proc['outputs']:
            if lot_key in producer:
                raise ImpactBuildError(
                    'batch %r / %r is an output of both process %r and %r'
                    % (lot_key[0], lot_key[1], producer[lot_key], process_id))
            producer[lot_key] = process_id

    # shipments.
    shipments = {}
    for rec in shipments_b['records']:
        shipment_id, customer_id, product_id, lot_code, units = rec['values']
        if shipment_id in shipments:
            raise ImpactBuildError(
                'shipments CSV record %d duplicates shipment_id %r'
                % (rec['position'], shipment_id))
        lot_key = (product_id, lot_code)
        if lot_key not in lots:
            raise ImpactBuildError(
                'shipments CSV record %d references unregistered batch '
                '%r / %r' % (rec['position'], product_id, lot_code))
        # Decimal integer string: ASCII digits only, value within the bound.
        if not units.isascii() or not units.isdigit():
            raise ImpactBuildError(
                'shipments CSV record %d has non-integer units %r'
                % (rec['position'], units))
        value = int(units)
        if not 1 <= value <= 1_000_000_000:
            raise ImpactBuildError(
                'shipments CSV record %d units %s outside 1..1000000000'
                % (rec['position'], units))
        shipments[shipment_id] = {
            'shipment_id': shipment_id, 'customer_id': customer_id,
            'lot': lot_key, 'units_text': units, 'units': value,
            'position': rec['position']}

    return {
        'name_to_product': name_to_product,
        'product_names': product_names,
        'lots': lots,
        'processes': processes,
        'producer': producer,
        'shipments': shipments,
    }
