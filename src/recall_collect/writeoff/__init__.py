"""Offline write-off interval analysis for accepted recall impact marts.

Read-only reuse of a ``recall-impact-mart/1`` SQLite file plus one returns
CSV.  The library API, the ``python -m recall_collect.writeoff`` CLI and the
JSON report share one implementation; no input is ever modified.
"""
from .analysis import (
    REPORT_FORMAT, WriteoffResult, analyze_writeoff_intervals, report_to_json)
from .errors import WriteoffInputError

__all__ = [
    'REPORT_FORMAT',
    'WriteoffInputError',
    'WriteoffResult',
    'analyze_writeoff_intervals',
    'report_to_json',
]
