"""Build the self-contained recall-impact SQLite data mart.

The build is a single-writer, offline, full rebuild:

1. validate that the output cannot alias any input (symlink/hard link);
2. read the accepted recalls and their sources in one read-only transaction;
3. capture each CSV exactly once into bytes, then parse those same bytes;
4. validate references, the single-producer rule and the acyclic graph;
5. resolve notice lots and propagate roots;
6. write everything to a temporary database, close it, then atomically
   replace the output.  Any failure removes the temporary file and leaves a
   previously existing mart untouched.
"""
import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path

from .csv_input import capture_csv, parse_bundles
from .lineage import build_roots, propagate, resolve_notice_lots, validate_dag
from .sources import ImpactBuildError, open_recall_db, read_recall_snapshot

MART_FORMAT = 'recall-impact-mart/1'
SNAPSHOT_KIND = 'recall_impact_logic_snapshot/v1'

SCHEMA = '''
PRAGMA foreign_keys=ON;
CREATE TABLE meta(meta_key TEXT PRIMARY KEY, meta_value TEXT NOT NULL);
CREATE TABLE logic_snapshot(
  snapshot_kind TEXT PRIMARY KEY,
  canonical_json TEXT NOT NULL,
  sha256 TEXT NOT NULL);
CREATE TABLE input_raw(
  input_kind TEXT NOT NULL,
  header_text TEXT NOT NULL,
  raw_bytes BLOB NOT NULL,
  sha256 TEXT NOT NULL,
  source_path TEXT NOT NULL,
  PRIMARY KEY(input_kind));
CREATE TABLE input_records(
  input_kind TEXT NOT NULL,
  record_position INTEGER NOT NULL,
  raw_line TEXT NOT NULL,
  PRIMARY KEY(input_kind, record_position),
  FOREIGN KEY(input_kind) REFERENCES input_raw(input_kind));
CREATE TABLE recalls(
  recall_id TEXT PRIMARY KEY,
  published TEXT NOT NULL,
  product TEXT NOT NULL,
  lots_json TEXT NOT NULL,
  reason TEXT NOT NULL);
CREATE TABLE recall_sources(
  recall_id TEXT NOT NULL,
  path TEXT NOT NULL,
  PRIMARY KEY(recall_id, path),
  FOREIGN KEY(recall_id) REFERENCES recalls(recall_id));
CREATE TABLE internal_products(
  product_id TEXT PRIMARY KEY);
CREATE TABLE products(
  product_id TEXT NOT NULL,
  notice_product TEXT NOT NULL,
  record_position INTEGER NOT NULL,
  PRIMARY KEY(notice_product),
  UNIQUE(product_id, notice_product),
  FOREIGN KEY(product_id) REFERENCES internal_products(product_id));
CREATE TABLE internal_lots(
  product_id TEXT NOT NULL,
  lot_code TEXT NOT NULL,
  record_position INTEGER NOT NULL,
  PRIMARY KEY(product_id, lot_code),
  FOREIGN KEY(product_id) REFERENCES internal_products(product_id));
CREATE TABLE processes(
  process_id TEXT PRIMARY KEY);
CREATE TABLE process_members(
  process_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('input','output')),
  product_id TEXT NOT NULL,
  lot_code TEXT NOT NULL,
  record_position INTEGER NOT NULL,
  PRIMARY KEY(process_id, role, product_id, lot_code),
  FOREIGN KEY(process_id) REFERENCES processes(process_id),
  FOREIGN KEY(product_id, lot_code) REFERENCES
    internal_lots(product_id, lot_code));
CREATE TABLE shipments(
  shipment_id TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL,
  product_id TEXT NOT NULL,
  lot_code TEXT NOT NULL,
  units TEXT NOT NULL,
  record_position INTEGER NOT NULL,
  FOREIGN KEY(product_id, lot_code) REFERENCES
    internal_lots(product_id, lot_code));
CREATE TABLE notice_lot_resolutions(
  recall_id TEXT NOT NULL,
  notice_product TEXT NOT NULL,
  notice_lot TEXT NOT NULL,
  product_id TEXT,
  lot_code TEXT NOT NULL,
  status TEXT NOT NULL
    CHECK(status IN ('product_unmapped','lot_unregistered','matched')),
  PRIMARY KEY(recall_id, notice_product, notice_lot),
  FOREIGN KEY(recall_id) REFERENCES recalls(recall_id));
CREATE TABLE recall_roots(
  recall_id TEXT NOT NULL,
  product_id TEXT NOT NULL,
  lot_code TEXT NOT NULL,
  PRIMARY KEY(recall_id, product_id, lot_code),
  FOREIGN KEY(recall_id) REFERENCES recalls(recall_id),
  FOREIGN KEY(product_id, lot_code) REFERENCES
    internal_lots(product_id, lot_code));
CREATE TABLE propagation_facts(
  recall_id TEXT NOT NULL,
  root_product_id TEXT NOT NULL,
  root_lot_code TEXT NOT NULL,
  product_id TEXT NOT NULL,
  lot_code TEXT NOT NULL,
  hops INTEGER NOT NULL CHECK(hops >= 0),
  predecessor_product_id TEXT,
  predecessor_lot_code TEXT,
  via_process TEXT,
  PRIMARY KEY(recall_id, root_product_id, root_lot_code, product_id,
             lot_code),
  FOREIGN KEY(recall_id, root_product_id, root_lot_code)
    REFERENCES recall_roots(recall_id, product_id, lot_code),
  FOREIGN KEY(product_id, lot_code) REFERENCES
    internal_lots(product_id, lot_code));
CREATE TABLE shipment_recall_impact(
  shipment_id TEXT NOT NULL,
  recall_id TEXT NOT NULL,
  units TEXT NOT NULL,
  PRIMARY KEY(shipment_id, recall_id),
  FOREIGN KEY(shipment_id) REFERENCES shipments(shipment_id),
  FOREIGN KEY(recall_id) REFERENCES recalls(recall_id));
CREATE VIEW v_shipment_link_status(
  shipment_id, customer_id, product_id, lot_code, units, link_status) AS
  SELECT s.shipment_id, s.customer_id, s.product_id, s.lot_code, s.units,
         CASE WHEN i.shipment_id IS NULL
              THEN 'not_linked_by_supplied_data'
              ELSE 'linked_to_recall' END
  FROM shipments s
  LEFT JOIN (SELECT DISTINCT shipment_id FROM shipment_recall_impact) i
    ON i.shipment_id = s.shipment_id;
CREATE VIEW v_shipment_recall_roots(
  shipment_id, recall_id, root_product_id, root_lot_code,
  hops, via_process, predecessor_product_id, predecessor_lot_code) AS
  SELECT i.shipment_id, f.recall_id, f.root_product_id, f.root_lot_code,
         f.hops, f.via_process, f.predecessor_product_id,
         f.predecessor_lot_code
  FROM shipment_recall_impact i
  JOIN shipments s ON s.shipment_id = i.shipment_id
  JOIN propagation_facts f
    ON f.recall_id = i.recall_id
   AND f.product_id = s.product_id
   AND f.lot_code = s.lot_code;
CREATE VIEW v_recall_impact(recall_id, shipments_affected, units_total) AS
  SELECT recall_id, count(*),
         printf('%d', sum(CAST(units AS INTEGER)))
  FROM shipment_recall_impact
  GROUP BY recall_id;
CREATE VIEW v_customer_impact(customer_id, shipments_affected, units_total) AS
  SELECT s.customer_id, count(*),
         printf('%d', sum(CAST(s.units AS INTEGER)))
  FROM (SELECT DISTINCT customer_id, shipment_id, units
        FROM shipments
        WHERE shipment_id IN (SELECT shipment_id FROM shipment_recall_impact)
       ) s
  GROUP BY s.customer_id;
'''


def _reject_output_aliases(output, input_paths):
    out = Path(output)
    if out.is_symlink():
        raise ImpactBuildError(
            'output must not be a symbolic link: %s' % out)
    # Lexical absolute-path check: catches an output naming an input directly
    # even before the output file exists.
    out_abs = os.path.abspath(out)
    for label, other in input_paths:
        if out_abs == os.path.abspath(other):
            raise ImpactBuildError(
                'output must not alias the %s input: %s' % (label, other))
    # Identity check over every supplied path (output plus the five inputs):
    # hard links and symlink aliases on either side are recognized by device
    # and inode, so two arguments can never quietly name one physical file.
    candidates = [('output', out)] + input_paths
    identities = []
    for label, path in candidates:
        try:
            if path.exists():
                stat = os.stat(path)
                identities.append((label, str(path),
                                   (stat.st_dev, stat.st_ino)))
        except OSError as exc:
            raise ImpactBuildError(
                'cannot inspect input aliases: %s' % exc) from exc
    seen = {}
    for label, text, identity in identities:
        if identity in seen:
            raise ImpactBuildError(
                '%s %s aliases %s (same device/inode); inputs and output '
                'must be distinct files' % (label, text, seen[identity]))
        seen[identity] = '%s %s' % (label, text)


def _canonical_snapshot(recalls, sources, model, resolutions, roots, facts,
                        shipments_ordered):
    snapshot = {
        'format': MART_FORMAT,
        'recalls': [
            {'recall_id': rid,
             'published': recalls[rid]['published'],
             'product': recalls[rid]['product'],
             'lots': sorted(set(recalls[rid]['lots'])),
             'reason': recalls[rid]['reason'],
             'sources': sorted(sources[rid])}
            for rid in sorted(recalls)],
        'products': sorted(
            ({'product_id': pid, 'notice_product': name}
             for name, pid in model['name_to_product'].items()),
            key=lambda d: (d['product_id'], d['notice_product'])),
        'lots': sorted(model['lots'].keys()),
        'processes': sorted(
            ({'process_id': pid,
              'inputs': sorted(proc['inputs']),
              'outputs': sorted(proc['outputs'])}
             for pid, proc in model['processes'].items()),
            key=lambda d: d['process_id']),
        'shipments': sorted(
            ({'shipment_id': sid,
              'customer_id': row['customer_id'],
              'product_id': row['lot'][0],
              'lot_code': row['lot'][1],
              'units': row['units']}
             for sid, row in model['shipments'].items()),
            key=lambda d: d['shipment_id']),
        'notice_lot_resolutions': sorted(
            ({'recall_id': r['recall_id'],
              'notice_product': r['notice_product'],
              'notice_lot': r['notice_lot'],
              'product_id': r['product_id'],
              'lot_code': r['lot_code'],
              'status': r['status']}
             for r in resolutions),
            key=lambda d: (d['recall_id'], d['notice_product'],
                           d['notice_lot'])),
        'roots': sorted(roots.keys()),
        'propagation_facts': sorted(
            ({'recall_id': f['recall_id'],
              'root': [f['root_product_id'], f['root_lot_code']],
              'target': [f['product_id'], f['lot_code']],
              'hops': f['hops'],
              'predecessor': (
                  [f['predecessor_product_id'], f['predecessor_lot_code']]
                  if f['predecessor_product_id'] is not None else None),
              'via_process': f['via_process'],
              'canonical_steps': [list(step) for step in f['canonical_steps']]}
             for f in facts),
            key=lambda d: (d['recall_id'], tuple(d['root']),
                           tuple(d['target']))),
        'shipment_recall_impact': sorted(
            (row['shipment_id'], row['recall_id'])
            for row in shipments_ordered),
    }
    return json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'))


def build_impact_mart(recalls_db, products_csv, lots_csv, process_members_csv,
                      shipments_csv, output):
    """Build the impact mart at ``output`` and return a small summary dict."""
    inputs = [
        ('recalls database', Path(recalls_db)),
        ('products CSV', Path(products_csv)),
        ('lots CSV', Path(lots_csv)),
        ('process-members CSV', Path(process_members_csv)),
        ('shipments CSV', Path(shipments_csv)),
    ]
    output_path = Path(output)
    _reject_output_aliases(output_path, inputs)
    # Inputs must exist before any expensive work starts.
    for label, path in inputs:
        if not path.is_file():
            raise ImpactBuildError('missing %s: %s' % (label, path))

    with open_recall_db(recalls_db) as db:
        recalls, sources = read_recall_snapshot(db)

    bundles = [
        capture_csv(products_csv, 'products'),
        capture_csv(lots_csv, 'lots'),
        capture_csv(process_members_csv, 'process-members'),
        capture_csv(shipments_csv, 'shipments'),
    ]
    model = parse_bundles(bundles)
    downstream = validate_dag(model['lots'], model['processes'])

    resolutions = resolve_notice_lots(
        recalls, model['name_to_product'], model['lots'])
    roots = build_roots(resolutions)
    facts = propagate(roots, downstream)

    # Shipment impact at shipment_id/recall_id granularity: several roots or
    # several converging paths still hit a shipment once per recall.
    fact_index = {
        (f['recall_id'], f['product_id'], f['lot_code']): f for f in facts}
    impact_rows = {}
    for shipment_id in sorted(model['shipments']):
        row = model['shipments'][shipment_id]
        for recall_id in sorted(recalls):
            if (recall_id, row['lot'][0], row['lot'][1]) in fact_index:
                impact_rows[(shipment_id, recall_id)] = {
                    'shipment_id': shipment_id, 'recall_id': recall_id,
                    'units': row['units_text']}
    impact_ordered = [impact_rows[k] for k in sorted(impact_rows)]

    canonical_json = _canonical_snapshot(
        recalls, sources, model, resolutions, roots, facts, impact_ordered)
    snapshot_digest = hashlib.sha256(
        canonical_json.encode('utf-8')).digest().hex()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=output_path.name + '.', suffix='.tmp',
        dir=output_path.parent)
    os.close(tmp_fd)
    try:
        _write_mart(tmp_name, bundles, recalls, sources, model, resolutions,
                    roots, facts, impact_ordered, canonical_json,
                    snapshot_digest)
        os.replace(tmp_name, output_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    linked = {row['shipment_id'] for row in impact_ordered}
    return {
        'format': MART_FORMAT,
        'output': str(output_path),
        'recalls': len(recalls),
        'notice_lot_rows': len(resolutions),
        'matched_roots': len(roots),
        'propagation_facts': len(facts),
        'shipments_total': len(model['shipments']),
        'shipments_linked': len(linked),
        'shipments_not_linked': len(model['shipments']) - len(linked),
    }


def _write_mart(path, bundles, recalls, sources, model, resolutions, roots,
                facts, impact_ordered, canonical_json, snapshot_digest):
    db = sqlite3.connect(path)
    try:
        db.execute('PRAGMA foreign_keys=ON')
        db.executescript(SCHEMA)
        db.executemany('INSERT INTO meta(meta_key,meta_value) VALUES(?,?)', [
            ('format', MART_FORMAT),
            ('logic_snapshot_kind', SNAPSHOT_KIND),
            ('logic_snapshot_sha256', snapshot_digest),
        ])
        db.execute('INSERT INTO logic_snapshot VALUES(?,?,?)',
                   (SNAPSHOT_KIND, canonical_json, snapshot_digest))

        for bundle in bundles:
            db.execute('INSERT INTO input_raw VALUES(?,?,?,?,?)', (
                bundle['kind'], ','.join(bundle['header']),
                sqlite3.Binary(bundle['raw']), bundle['sha256'],
                bundle['path']))
            db.executemany(
                'INSERT INTO input_records(input_kind,record_position,'
                'raw_line) VALUES(?,?,?)',
                [(bundle['kind'], rec['position'], rec['raw_line'])
                 for rec in bundle['records']])

        db.executemany('INSERT INTO recalls VALUES(?,?,?,?,?)', [
            (rid, recalls[rid]['published'], recalls[rid]['product'],
             json.dumps(sorted(set(recalls[rid]['lots'])),
                        ensure_ascii=False),
             recalls[rid]['reason']) for rid in sorted(recalls)])
        db.executemany('INSERT INTO recall_sources VALUES(?,?)', [
            (rid, p) for rid in sorted(sources) for p in sources[rid]])

        db.executemany('INSERT INTO internal_products(product_id) VALUES(?)',
                       [(pid,) for pid in sorted(model['product_names'])])
        db.executemany(
            'INSERT INTO products(product_id,notice_product,record_position)'
            ' VALUES(?,?,?)',
            sorted((pid, name, _product_position(bundles, name))
                   for name, pid in model['name_to_product'].items()))
        db.executemany(
            'INSERT INTO internal_lots VALUES(?,?,?)',
            sorted((key[0], key[1], row['position'])
                   for key, row in model['lots'].items()))
        db.executemany('INSERT INTO processes(process_id) VALUES(?)',
                       [(pid,) for pid in sorted(model['processes'])])
        member_rows = []
        for process_id in sorted(model['processes']):
            proc = model['processes'][process_id]
            for role, lots in (('input', proc['inputs']),
                               ('output', proc['outputs'])):
                for lot in lots:
                    pos = _member_position(bundles, process_id, role, lot)
                    member_rows.append((process_id, role, lot[0], lot[1],
                                        pos))
        db.executemany('INSERT INTO process_members VALUES(?,?,?,?,?)',
                       sorted(member_rows))
        db.executemany(
            'INSERT INTO shipments VALUES(?,?,?,?,?,?)',
            sorted((sid, row['customer_id'], row['lot'][0], row['lot'][1],
                    row['units_text'], row['position'])
                   for sid, row in model['shipments'].items()))
        db.executemany(
            'INSERT INTO notice_lot_resolutions VALUES(?,?,?,?,?,?)',
            sorted((r['recall_id'], r['notice_product'], r['notice_lot'],
                    r['product_id'], r['lot_code'], r['status'])
                   for r in resolutions))
        db.executemany('INSERT INTO recall_roots VALUES(?,?,?)',
                       sorted(roots.keys()))
        db.executemany(
            'INSERT INTO propagation_facts VALUES(?,?,?,?,?,?,?,?,?)',
            sorted((f['recall_id'], f['root_product_id'], f['root_lot_code'],
                    f['product_id'], f['lot_code'], f['hops'],
                    f['predecessor_product_id'], f['predecessor_lot_code'],
                    f['via_process']) for f in facts))
        db.executemany('INSERT INTO shipment_recall_impact VALUES(?,?,?)',
                       sorted((row['shipment_id'], row['recall_id'],
                               row['units']) for row in impact_ordered))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def _product_position(bundles, notice_product):
    for rec in next(b for b in bundles if b['kind'] == 'products')['records']:
        if rec['values'][1] == notice_product:
            return rec['position']
    raise ImpactBuildError('internal error: product position not found')


def _member_position(bundles, process_id, role, lot):
    for rec in next(b for b in bundles if b['kind'] == 'process-members')[
            'records']:
        vals = rec['values']
        if (vals[0], vals[1], vals[2], vals[3]) == \
                (process_id, role, lot[0], lot[1]):
            return rec['position']
    raise ImpactBuildError('internal error: member position not found')
