"""Tiny shared helpers with no dependencies."""

from __future__ import annotations


def column_letter(index: int) -> str:
    """Spreadsheet column name for a 1-based index: 1 -> A, 27 -> AA."""
    letters = ""
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters
