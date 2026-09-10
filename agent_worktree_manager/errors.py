"""Errors safe to present at the command-line boundary."""


class AWMError(Exception):
    """An operation could not safely complete."""
