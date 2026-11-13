"""Offline recall-impact data mart.

Read-only reuse of an accepted-recall collector database plus four factory
CSV exports.  Everything is derived deterministically; the collector database
and CSV inputs are never modified.
"""
from .sources import (
    ImpactBuildError, open_recall_db, read_recall_snapshot, read_recall_tables)
from .mart import build_impact_mart

__all__ = [
    'ImpactBuildError',
    'build_impact_mart',
    'read_recall_snapshot',
    'read_recall_tables',
    'open_recall_db',
]
