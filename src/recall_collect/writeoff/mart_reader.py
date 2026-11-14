"""Read the affected-shipment relations from an existing impact mart.

The mart is opened strictly read-only with a ``mode=ro`` URI: the analysis
never creates, upgrades or writes a table, so an accepted mart is bit-for-bit
unchanged by a successful or a failed run.  Only the relations the analysis
needs are read, inside one read transaction, so a concurrent writer cannot
interleave them.

Required relations (the impact mart already provides all of them):

* ``meta`` with ``format = recall-impact-mart/1``;
* ``recalls(recall_id)``;
* ``shipments(shipment_id, customer_id, units)``;
* ``shipment_recall_impact(shipment_id, recall_id)``.
"""
import sqlite3

from .errors import WriteoffInputError

MART_FORMAT = 'recall-impact-mart/1'
UNITS_MIN = 1
UNITS_MAX = 1_000_000_000


def _columns(db, table):
    try:
        return tuple(row[1] for row in db.execute(
            'PRAGMA table_info(%s)' % table))
    except sqlite3.Error as exc:
        raise WriteoffInputError(
            'incompatible impact mart: %s' % exc) from exc


def _require_table(db, table, required):
    columns = _columns(db, table)
    if not columns:
        raise WriteoffInputError(
            'incompatible impact mart: missing table %r' % table)
    missing = [name for name in required if name not in columns]
    if missing:
        raise WriteoffInputError(
            'incompatible impact mart: table %r lacks columns %r'
            % (table, missing))


def load_mart_snapshot(mart_path):
    """Read shipments, recalls and affected relations from the mart.

    Returns a dict::

        {
          'format': 'recall-impact-mart/1',
          'logic_snapshot_sha256': str or None,
          'shipments': {shipment_id: {'customer_id', 'units_text', 'units'}},
          'recall_shipments': {recall_id: set(shipment_id)},
          'recall_ids': sorted list of every recall_id,
        }

    Any structural or value error raises :class:`WriteoffInputError` before
    any analysis result exists.  The mart is never modified.
    """
    uri = 'file:' + str(mart_path) + '?mode=ro'
    try:
        db = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise WriteoffInputError(
            'cannot open impact mart read-only: %s' % exc) from exc
    try:
        db.execute('BEGIN')
        try:
            return _read_snapshot(db)
        finally:
            db.execute('COMMIT')
    except sqlite3.Error as exc:
        raise WriteoffInputError(
            'incompatible impact mart: %s' % exc) from exc
    finally:
        db.close()


def _read_snapshot(db):
    _require_table(db, 'meta', ('meta_key', 'meta_value'))
    _require_table(db, 'recalls', ('recall_id',))
    _require_table(db, 'shipments',
                   ('shipment_id', 'customer_id', 'units'))
    _require_table(db, 'shipment_recall_impact',
                   ('shipment_id', 'recall_id'))

    try:
        format_value = db.execute(
            "SELECT meta_value FROM meta WHERE meta_key='format'").fetchone()
    except sqlite3.Error as exc:
        raise WriteoffInputError(
            'incompatible impact mart: %s' % exc) from exc
    if format_value is None or format_value[0] != MART_FORMAT:
        raise WriteoffInputError(
            'incompatible impact mart: meta format must be %r'
            % MART_FORMAT)

    digest_row = db.execute(
        "SELECT meta_value FROM meta "
        "WHERE meta_key='logic_snapshot_sha256'").fetchone()
    digest = digest_row[0] if digest_row is not None else None

    recall_ids = []
    for (recall_id,) in db.execute(
            'SELECT recall_id FROM recalls ORDER BY recall_id'):
        if not isinstance(recall_id, str) or not recall_id.strip():
            raise WriteoffInputError(
                'incompatible impact mart: blank recall_id')
        recall_ids.append(recall_id)

    shipments = {}
    for shipment_id, customer_id, units_text in db.execute(
            'SELECT shipment_id,customer_id,units FROM shipments '
            'ORDER BY shipment_id'):
        if (not isinstance(shipment_id, str) or not shipment_id.strip()
                or not isinstance(customer_id, str)
                or not customer_id.strip()):
            raise WriteoffInputError(
                'incompatible impact mart: blank shipment/customer id')
        if shipment_id in shipments:
            raise WriteoffInputError(
                'incompatible impact mart: duplicate shipment_id %r'
                % shipment_id)
        if not isinstance(units_text, str) or not units_text.isascii() \
                or not units_text.isdigit():
            raise WriteoffInputError(
                'incompatible impact mart: shipment %r has non-integer '
                'units %r' % (shipment_id, units_text))
        units = int(units_text)
        if not UNITS_MIN <= units <= UNITS_MAX:
            raise WriteoffInputError(
                'incompatible impact mart: shipment %r units %s outside '
                '%d..%d' % (shipment_id, units_text, UNITS_MIN, UNITS_MAX))
        shipments[shipment_id] = {
            'customer_id': customer_id,
            'units_text': units_text,
            'units': units,
        }

    known_recalls = set(recall_ids)
    recall_shipments = {recall_id: set() for recall_id in recall_ids}
    for shipment_id, recall_id in db.execute(
            'SELECT shipment_id,recall_id FROM shipment_recall_impact '
            'ORDER BY shipment_id,recall_id'):
        if shipment_id not in shipments:
            raise WriteoffInputError(
                'incompatible impact mart: impact row references unknown '
                'shipment %r' % shipment_id)
        if recall_id not in known_recalls:
            raise WriteoffInputError(
                'incompatible impact mart: impact row references unknown '
                'recall %r' % recall_id)
        recall_shipments[recall_id].add(shipment_id)

    return {
        'format': MART_FORMAT,
        'logic_snapshot_sha256': digest,
        'shipments': shipments,
        'recall_shipments': recall_shipments,
        'recall_ids': recall_ids,
    }
