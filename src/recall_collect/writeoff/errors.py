"""Error type shared by the offline write-off interval analysis."""


class WriteoffInputError(ValueError):
    """Raised for any invalid return CSV, mart or analysis argument.

    Every check that can reject a run happens before a report is returned;
    the source mart is only ever opened read-only, so a rejection never
    modifies it.
    """
