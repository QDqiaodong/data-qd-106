"""Read access to an existing collector database.

The collector database is opened strictly read-only: no CREATE TABLE, no
schema upgrade, no write transaction.  Recalls and their sources are read in
one single SQLite read transaction, so a concurrent collector commit cannot
interleave between the two tables.
"""
import json
import sqlite3
from contextlib import contextmanager

RECALL_COLUMNS = ('recall_id', 'published', 'product', 'lots_json', 'reason')
SOURCE_COLUMNS = ('recall_id', 'path')


class ImpactBuildError(ValueError):
    """Raised for any invalid input or incompatible source database."""


@contextmanager
def open_recall_db(path):
    """Open a collector database in immutable read-only mode.

    ``mode=ro`` rejects missing files and prevents any DDL/DML from running
    against the source database.
    """
    uri = 'file:' + str(path) + '?mode=ro'
    try:
        db = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise ImpactBuildError('cannot open recalls database: %s' % exc) from exc
    try:
        yield db
    finally:
        db.close()


def _check_columns(db, table, expected):
    try:
        cols = tuple(row[1] for row in db.execute(
            'PRAGMA table_info(%s)' % table))
    except sqlite3.Error as exc:
        raise ImpactBuildError(
            'incompatible recalls database: %s' % exc) from exc
    if cols != expected:
        raise ImpactBuildError(
            'incompatible recalls database: table %r columns %r, expected %r'
            % (table, cols, expected))


def read_recall_tables(db):
    """Read recalls and sources through the caller's current transaction.

    Both SELECTs see one snapshot when invoked inside a single transaction;
    :func:`read_recall_snapshot` opens that transaction itself.
    """
    _check_columns(db, 'recalls', RECALL_COLUMNS)
    _check_columns(db, 'sources', SOURCE_COLUMNS)
    try:
        recall_rows = db.execute(
            'SELECT recall_id,published,product,lots_json,reason '
            'FROM recalls ORDER BY recall_id').fetchall()
        source_rows = db.execute(
            'SELECT recall_id,path FROM sources '
            'ORDER BY recall_id,path').fetchall()
    except sqlite3.Error as exc:
        raise ImpactBuildError(
            'incompatible recalls database: %s' % exc) from exc

    recalls = {}
    for recall_id, published, product, lots_json, reason in recall_rows:
        try:
            lots = json.loads(lots_json)
        except (TypeError, ValueError) as exc:
            raise ImpactBuildError(
                'recall %r has invalid lots_json' % recall_id) from exc
        if (not isinstance(lots, list) or not lots
                or any(not isinstance(x, str) or not x.strip() for x in lots)):
            raise ImpactBuildError(
                'recall %r has invalid stored lots' % recall_id)
        recalls[recall_id] = {
            'recall_id': recall_id,
            'published': published,
            'product': product,
            'lots': lots,
            'reason': reason,
        }
    if not recalls:
        raise ImpactBuildError('incompatible recalls database: no recalls')

    sources = {recall_id: [] for recall_id in recalls}
    for recall_id, path in source_rows:
        if recall_id not in sources:
            raise ImpactBuildError(
                'incompatible recalls database: source row for unknown '
                'recall %r' % recall_id)
        sources[recall_id].append(path)
    return recalls, sources


def read_recall_snapshot(db):
    """Return ``(recalls, sources)`` from one read transaction.

    Both tables are read inside the same deferred transaction so they share
    one snapshot and a concurrent collector commit cannot interleave.
    """
    try:
        db.execute('BEGIN')
        try:
            return read_recall_tables(db)
        finally:
            db.execute('COMMIT')
    except sqlite3.Error as exc:
        raise ImpactBuildError(
            'incompatible recalls database: %s' % exc) from exc
