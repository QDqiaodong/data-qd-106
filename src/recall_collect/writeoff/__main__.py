"""Command-line entry point: ``python -m recall_collect.writeoff``.

Prints the JSON report to stdout and also writes it to ``--report``.  The
report file is written to a temporary sibling and atomically replaced, so an
existing report survives any failure.  ``--report`` may not alias (by path,
symbolic link or device/inode) the mart or the returns CSV; an aliased
report path is rejected before any analysis runs.
"""
import argparse
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

from .analysis import analyze_writeoff_intervals, report_to_json
from .errors import WriteoffInputError


def _reject_report_aliases(report, inputs):
    report_path = Path(report)
    if report_path.is_symlink():
        raise WriteoffInputError(
            'report must not be a symbolic link: %s' % report_path)
    report_abs = os.path.abspath(report_path)
    for label, other in inputs:
        if report_abs == os.path.abspath(other):
            raise WriteoffInputError(
                'report must not alias the %s input: %s' % (label, other))
    identities = []
    for label, path in [('report', report_path)] + list(inputs):
        try:
            if path.exists():
                stat = os.stat(path)
                identities.append(
                    (label, str(path), (stat.st_dev, stat.st_ino)))
        except OSError as exc:
            raise WriteoffInputError(
                'cannot inspect report/input aliases: %s' % exc) from exc
    seen = {}
    for label, text, identity in identities:
        if identity in seen:
            raise WriteoffInputError(
                '%s %s aliases %s (same device/inode); the report and the '
                'inputs must be distinct files'
                % (label, text, seen[identity]))
        seen[identity] = '%s %s' % (label, text)


def _write_report_atomically(path, text):
    report_path = Path(path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        prefix=report_path.name + '.', suffix='.tmp',
        dir=report_path.parent)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as stream:
            stream.write(text)
        os.replace(temporary, report_path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Offline write-off interval analysis over an impact mart')
    parser.add_argument('--mart', required=True,
                        help='existing recall-impact-mart SQLite file '
                             '(opened read-only)')
    parser.add_argument('--returns', required=True,
                        help='returns CSV: return_id,customer_id,units,'
                             'candidate_shipment_ids')
    parser.add_argument('--report', required=True,
                        help='output JSON report path; must not alias an input')
    arguments = parser.parse_args(argv)
    inputs = [('impact mart', Path(arguments.mart)),
              ('returns CSV', Path(arguments.returns))]
    try:
        _reject_report_aliases(arguments.report, inputs)
        for label, path in inputs:
            if not path.is_file():
                raise WriteoffInputError(
                    'missing %s: %s' % (label, path))
        report = analyze_writeoff_intervals(arguments.mart,
                                            arguments.returns)
        text = report_to_json(report) + '\n'
        _write_report_atomically(arguments.report, text)
    except (WriteoffInputError, OSError, sqlite3.Error) as exc:
        print('write-off analysis failed: %s' % exc, file=sys.stderr)
        return 2
    sys.stdout.write(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
