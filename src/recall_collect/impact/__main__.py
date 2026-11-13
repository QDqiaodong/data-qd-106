"""Command-line entry point: ``python -m recall_collect.impact``."""
import argparse
import json
import sqlite3
import sys

from .mart import build_impact_mart
from .sources import ImpactBuildError


def main(argv=None):
    p = argparse.ArgumentParser(
        description='Build the offline recall-impact SQLite data mart')
    p.add_argument('--recalls-db', required=True,
                   help='existing collector SQLite database (opened read-only)')
    p.add_argument('--products', required=True)
    p.add_argument('--lots', required=True)
    p.add_argument('--process-members', required=True)
    p.add_argument('--shipments', required=True)
    p.add_argument('--output', required=True,
                   help='output SQLite file; must not alias any input')
    args = p.parse_args(argv)
    try:
        summary = build_impact_mart(
            args.recalls_db, args.products, args.lots,
            args.process_members, args.shipments, args.output)
    except (ImpactBuildError, OSError, sqlite3.Error) as exc:
        print('impact build failed: %s' % exc, file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
