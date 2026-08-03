"""Tagged-token vocabulary for the canonicalization library (lane C).

WHY THIS MODULE EXISTS
======================
The canonical form of a spreadsheet cell is a *tagged* string -- never a bare
string -- and the tag is part of the hash preimage.  Without tags, a column
whose declared type silently changes between two runs produces *equal* hashes
for *unequal* data: the text ``"1"``, the number ``1`` and the boolean ``true``
would all canonicalize to ``"1"`` and collide.  A collision here is a false
``verified``, which is the single worst outcome a verification system can
produce, so the type family is encoded in the first two bytes of every token.

This module owns:

* the nine tag prefixes (``z: s: n: b: d: t: r: k: e:``),
* the ``e:`` error-code vocabulary and the warning-code vocabulary,
* the domain-separation prefixes used by every hash preimage,
* the ``ABSENT`` sentinel and the ``SheetErrorValue`` marker used to represent
  "there is no cell here" and "this cell holds a spreadsheet error" without
  overloading ``None``/``False`` (both of which are legitimate *values* on the
  Odoo side),
* ``equal()`` -- the only sanctioned way to compare two tokens, because ``==``
  gets the error-non-equality rule wrong.

Stdlib only.  No ``odoo`` import, no third-party import, ever.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "TAG_NULL",
    "TAG_TEXT",
    "TAG_NUMBER",
    "TAG_BOOL",
    "TAG_DATE",
    "TAG_DATETIME",
    "TAG_REL",
    "TAG_SELECTION",
    "TAG_ERROR",
    "ALL_TAGS",
    "NULL_TOKEN",
    "TRUE_TOKEN",
    "FALSE_TOKEN",
    "ERROR_CODES",
    "ERR_NOT_A_NUMBER",
    "ERR_NOT_FINITE",
    "ERR_BAD_DATE",
    "ERR_BAD_BOOL",
    "ERR_CELL_ERROR",
    "ERR_IDENTIFIER_NUMERIC",
    "ERR_UNRESOLVED_SELECTION",
    "ERR_ORPHAN_REFERENCE",
    "ERR_NONEXISTENT_LOCAL_TIME",
    "ERR_TIME_COMPONENT_PRESENT",
    "ERR_MULTI_MATCH",
    "ERR_CURRENCY_MISMATCH",
    "WARNING_CODES",
    "WARN_TIME_COMPONENT_PRESENT",
    "WARN_AMBIGUOUS_LOCAL_TIME",
    "WARN_TEXT_COLUMN_RECEIVED_NUMBER",
    "WARN_ROUNDING_BOUNDARY",
    "WARN_GUESSED_SEPARATOR",
    "SHEET_ERROR_LITERALS",
    "SPEC_PREFIX",
    "ROW_PREFIX",
    "ROWF_PREFIX",
    "EXTRA_PREFIX",
    "BKT_PREFIX",
    "DS_PREFIX",
    "HDR_PREFIX",
    "ABSENT",
    "SheetErrorValue",
    "error",
    "is_absent",
    "is_error",
    "is_null",
    "tag_of",
    "payload_of",
    "equal",
    "same_family",
    "add_warning",
]

# --------------------------------------------------------------------------
# Tags
# --------------------------------------------------------------------------

TAG_NULL: Final = "z:"
TAG_TEXT: Final = "s:"
TAG_NUMBER: Final = "n:"
TAG_BOOL: Final = "b:"
TAG_DATE: Final = "d:"
TAG_DATETIME: Final = "t:"
TAG_REL: Final = "r:"
TAG_SELECTION: Final = "k:"
TAG_ERROR: Final = "e:"

#: Every legal tag.  A token whose first two characters are not in this set is
#: a programming error in lane C, not a data error.
ALL_TAGS: Final = (
    TAG_NULL,
    TAG_TEXT,
    TAG_NUMBER,
    TAG_BOOL,
    TAG_DATE,
    TAG_DATETIME,
    TAG_REL,
    TAG_SELECTION,
    TAG_ERROR,
)

NULL_TOKEN: Final = "z:"
TRUE_TOKEN: Final = "b:1"
FALSE_TOKEN: Final = "b:0"

# --------------------------------------------------------------------------
# Error codes (the ``e:`` family) -- CANONICALIZATION.md §2.1
# --------------------------------------------------------------------------

ERR_NOT_A_NUMBER: Final = "NOT_A_NUMBER"
ERR_NOT_FINITE: Final = "NOT_FINITE"
ERR_BAD_DATE: Final = "BAD_DATE"
ERR_BAD_BOOL: Final = "BAD_BOOL"
ERR_CELL_ERROR: Final = "CELL_ERROR"
ERR_IDENTIFIER_NUMERIC: Final = "IDENTIFIER_NUMERIC"
ERR_UNRESOLVED_SELECTION: Final = "UNRESOLVED_SELECTION"
ERR_ORPHAN_REFERENCE: Final = "ORPHAN_REFERENCE"
ERR_NONEXISTENT_LOCAL_TIME: Final = "NONEXISTENT_LOCAL_TIME"
ERR_TIME_COMPONENT_PRESENT: Final = "TIME_COMPONENT_PRESENT"
ERR_MULTI_MATCH: Final = "MULTI_MATCH"
ERR_CURRENCY_MISMATCH: Final = "CURRENCY_MISMATCH"

ERROR_CODES: Final = frozenset(
    {
        ERR_NOT_A_NUMBER,
        ERR_NOT_FINITE,
        ERR_BAD_DATE,
        ERR_BAD_BOOL,
        ERR_CELL_ERROR,
        ERR_IDENTIFIER_NUMERIC,
        ERR_UNRESOLVED_SELECTION,
        ERR_ORPHAN_REFERENCE,
        ERR_NONEXISTENT_LOCAL_TIME,
        ERR_TIME_COMPONENT_PRESENT,
        ERR_MULTI_MATCH,
        ERR_CURRENCY_MISMATCH,
    }
)

# --------------------------------------------------------------------------
# Warning codes.  Lane C is pure and therefore cannot log; it appends these to
# a caller-supplied list so lane E can log them through Odoo's ``_logger``.
# --------------------------------------------------------------------------

WARN_TIME_COMPONENT_PRESENT: Final = "TIME_COMPONENT_PRESENT"
WARN_AMBIGUOUS_LOCAL_TIME: Final = "AMBIGUOUS_LOCAL_TIME"
WARN_TEXT_COLUMN_RECEIVED_NUMBER: Final = "TEXT_COLUMN_RECEIVED_NUMBER"
WARN_ROUNDING_BOUNDARY: Final = "ROUNDING_BOUNDARY"
WARN_GUESSED_SEPARATOR: Final = "GUESSED_SEPARATOR"

WARNING_CODES: Final = frozenset(
    {
        WARN_TIME_COMPONENT_PRESENT,
        WARN_AMBIGUOUS_LOCAL_TIME,
        WARN_TEXT_COLUMN_RECEIVED_NUMBER,
        WARN_ROUNDING_BOUNDARY,
        WARN_GUESSED_SEPARATOR,
    }
)

#: Literal strings Google Sheets returns for error cells under
#: ``valueRenderOption='UNFORMATTED_VALUE'``.  The ``effectiveValue`` oneof is
#: the authoritative signal, but ``values.batchGet`` -- the bulk read path that
#: stages 99 % of cells -- flattens error cells to these strings, so they must
#: also be recognised.  Mapping them to ``z:`` would be a silent data loss.
SHEET_ERROR_LITERALS: Final = frozenset(
    {
        "#N/A",
        "#REF!",
        "#DIV/0!",
        "#VALUE!",
        "#NAME?",
        "#NUM!",
        "#NULL!",
        "#ERROR!",
        "#GETTING_DATA",
        "#SPILL!",
        "#CALC!",
    }
)

# --------------------------------------------------------------------------
# Domain-separation prefixes.  Every hash preimage in this library starts with
# one of these, so a bucket digest can never be mistaken for a row digest even
# if an attacker (or a bug) controls the rest of the preimage.
# --------------------------------------------------------------------------

SPEC_PREFIX: Final = b"gos1/spec\x00"
ROW_PREFIX: Final = b"gos1/row\x00"
ROWF_PREFIX: Final = b"gos1/rowf\x00"
EXTRA_PREFIX: Final = b"gos1/extra\x00"
BKT_PREFIX: Final = b"gos1/bkt\x00"
DS_PREFIX: Final = b"gos1/ds\x00"
HDR_PREFIX: Final = b"gos1/hdr\x00"


# --------------------------------------------------------------------------
# Sentinels
# --------------------------------------------------------------------------


class _Absent:
    """Singleton meaning "the source produced no cell at this position".

    WHY a dedicated sentinel rather than ``None``: on the Odoo side ``None``,
    ``False`` and ``0`` are all *values* with distinct meanings (``False`` is a
    real boolean, ``0`` is a real number), and on the sheet side a ragged row
    simply stops -- there is no cell object at all.  Conflating "absent" with
    "empty string" is how a short row silently overwrites a populated Odoo
    column with NULLs.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "ABSENT"

    def __bool__(self) -> bool:
        return False


ABSENT: Final = _Absent()


@dataclass(frozen=True)
class SheetErrorValue:
    """A Google Sheets ``errorValue`` cell (``#N/A``, ``#REF!``, ...).

    WHY: lane B's ``read_effective_values`` sees the ``errorValue`` branch of
    the ``ExtendedValue`` oneof and must be able to hand lane C something that
    can never be confused with a legitimate string cell whose text happens to
    be ``"#N/A"``.  Canonicalizes to ``e:CELL_ERROR``, never to ``z:``.
    """

    code: str = "ERROR"
    message: str = ""


# --------------------------------------------------------------------------
# Token helpers
# --------------------------------------------------------------------------


def error(code: str) -> str:
    """Build an ``e:`` token, rejecting codes outside the frozen vocabulary.

    WHY validate: a typo'd error code would flow all the way into a drift
    record and a report artefact, where nobody would ever notice it is not one
    of the twelve documented codes.  Fail at the point of construction.
    """
    if code not in ERROR_CODES:
        raise ValueError("Unknown canonicalization error code: %r" % (code,))
    return TAG_ERROR + code


def is_absent(value: Any) -> bool:
    """True when ``value`` is the ABSENT sentinel (identity comparison)."""
    return value is ABSENT


def is_error(token: str) -> bool:
    """True when ``token`` belongs to the ``e:`` family.

    Callers use this to decide row quarantine: a single ``e:`` token
    quarantines the *whole* row, because a half-written row is worse than an
    unwritten one.
    """
    return isinstance(token, str) and token.startswith(TAG_ERROR)


def is_null(token: str) -> bool:
    """True when ``token`` is the NULL/empty token ``z:``."""
    return token == NULL_TOKEN


def tag_of(token: str) -> str:
    """Return the two-character tag of ``token``.

    Raises ``ValueError`` for anything that is not a tagged token -- that is a
    lane-C bug, not a data condition, and must not be papered over.
    """
    if not isinstance(token, str) or len(token) < 2 or token[:2] not in ALL_TAGS:
        raise ValueError("Not a tagged canonical token: %r" % (token,))
    return token[:2]


def payload_of(token: str) -> str:
    """Return everything after the tag of ``token``."""
    return token[len(tag_of(token)) :]


def same_family(a: str, b: str) -> bool:
    """True when both tokens carry the same tag.

    Type-family disjointness is invariant 3 of CANONICALIZATION §12: two tokens
    with different tags are never equal, no matter how similar their payloads.
    """
    return tag_of(a) == tag_of(b)


def equal(a: str, b: str) -> bool:
    """The only sanctioned equality test for two canonical tokens.

    WHY not ``a == b``: an ``e:`` token is **never** equal to anything,
    *including a byte-identical ``e:`` token*.  Two cells that both failed to
    parse are not "the same value" -- they are two unknowns, and asserting
    equality on unknowns is exactly how a verification system reports
    ``verified`` over data it could not read.  Errors do not compare; they
    quarantine.
    """
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    if a.startswith(TAG_ERROR) or b.startswith(TAG_ERROR):
        return False
    return a == b


def add_warning(sink: list | None, code: str, detail: str = "") -> None:
    """Append a warning code to a caller-supplied sink, if one was supplied.

    WHY a sink instead of ``logging``: lane C must be a pure function of its
    inputs so that its output is byte-reproducible on any machine.  A module
    that logs is a module that behaves differently under different handler
    configurations, and a module that keeps module-level state is a module that
    is not thread-safe inside an Odoo worker.  The caller owns the sink.
    """
    if sink is None:
        return
    if code not in WARNING_CODES:
        raise ValueError("Unknown canonicalization warning code: %r" % (code,))
    sink.append("%s:%s" % (code, detail) if detail else code)
