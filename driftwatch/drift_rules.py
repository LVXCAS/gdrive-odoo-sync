# -*- coding: utf-8 -*-
"""Drift rules that more than one phase has to agree on.

A rule applied in one phase and not another is worse than no rule at all: the
same condition reaches the store twice under two severities, and whichever
surface a reader happens to be looking at decides what they believe. The
empty-tab guard shipped exactly that way -- ``verifier`` graded it, ``stager``
hardcoded ``warning`` -- so a corpus of ordinary blank tabs produced hundreds
of warnings from one phase and hundreds of ``info`` rows from the next, for
the same tabs, in the same cycle.

The rule lives here now and both phases import it. Nothing in this module
touches the database or the network, so either phase can apply it wherever it
already has the numbers.
"""
from __future__ import annotations

#: A tab that staged zero rows when a previous complete read had some. This is
#: the mass-delete signal: never acted on, always reported loudly.
EMPTY_TAB_LOST_ROWS = 'error'

#: A tab that has always been empty. A fact about the corpus, not an event.
EMPTY_TAB_ALWAYS_EMPTY = 'info'


def coerce_rows(value) -> int:
    """A row count read from an untrusted column: NULL and junk both mean 0."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def empty_tab_severity(previous_rows: int) -> str:
    """Grade a zero-row tab by whether it ever had rows (SPEC 9.6).

    The guard's *behaviour* does not vary with this answer -- zero rows are
    never evidence of deletion either way. Only how loudly it is reported.
    """
    return EMPTY_TAB_LOST_ROWS if previous_rows > 0 else EMPTY_TAB_ALWAYS_EMPTY


def carried_baseline(previous_dataset) -> int:
    """The baseline that ``prev_row_count`` will hold once this read lands.

    Mirrors the ``ON CONFLICT`` arithmetic in ``Store.upsert_dataset``: a
    complete previous read becomes the new baseline; an incomplete one carries
    the older baseline forward untouched, so a truncated read cannot erase the
    very number the guard fires on.

    The stager needs this value *before* its own upsert commits, which is the
    only reason the arithmetic exists in two places. The SQL is the authority;
    this is the copy that has to follow it.
    """
    if previous_dataset is None:
        return 0
    try:
        complete = bool(previous_dataset['read_complete'])
        return coerce_rows(
            previous_dataset['row_count' if complete else 'prev_row_count'])
    except (KeyError, IndexError, TypeError):
        return 0
