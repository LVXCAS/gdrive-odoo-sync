"""``TEXT_CANON`` -- the ten-step text normalization (lane C).

WHY THIS MODULE EXISTS
======================
Text is where false drift is born.  A cell typed on a Mac carries ``U+2019``
instead of ``'``; a cell pasted from a web page carries a BOM, a zero-width
space and a non-breaking space; a cell exported from Excel carries a trailing
NBSP that no human can see.  None of those are data changes, and a system that
reports 5 000 drifts because somebody re-typed a name will be switched off
within a week.

At the same time, over-normalizing is worse: NFKC folds ``U+33A1`` (square
metre) to ``m2`` and ``U+2460`` (circled one) to ``1``, which is real
information loss that *masks* genuine differences.  So the primary canonical
form normalizes only what is provably invisible, and the visible-but-cosmetic
transformations (smart quotes, case, whitespace runs) live in ``fold_punct`` +
the folded hash, which drives the ``COSMETIC`` drift classification instead of
hiding the change.

**The order of the ten steps is load-bearing.**  In particular NFC must run
*after* invisible-character stripping, so that a decomposed sequence separated
by an intervening ZWSP still composes.

Every code point this module *operates on* is written numerically (``chr(cp)``
or an integer ``translate`` key) rather than as a literal character.  That is
deliberate: this file must survive being opened, saved, re-encoded and diffed
by tools with imperfect Unicode handling without silently changing what the
normalizer strips.  A literal soft hyphen in the source is invisible in every
code review that would have caught its loss.

Stdlib only: ``re``, ``unicodedata``.
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal
from typing import Any, Final

from .tokens import (
    ABSENT,
    NULL_TOKEN,
    TAG_TEXT,
    WARN_TEXT_COLUMN_RECEIVED_NUMBER,
    add_warning,
)

__all__ = [
    "TEXT_CANON",
    "text_prepare",
    "fold_punct",
    "collapse_ws",
    "strip_format_chars",
    "unify_whitespace",
    "cosmetic_fold",
    "stringify_scalar",
]

# --------------------------------------------------------------------------
# Step 3 -- invisible / format characters
# --------------------------------------------------------------------------

#: Explicitly named invisibles.  Most are Unicode category ``Cf`` and would be
#: caught by the category sweep anyway; they are listed so the intent survives
#: any future Unicode recategorization (``U+200B`` in particular has moved
#: between categories across Unicode versions).
_EXPLICIT_INVISIBLES: Final = frozenset(
    chr(cp)
    for cp in (
        0xFEFF,  # ZERO WIDTH NO-BREAK SPACE / byte-order mark
        0x200B,  # ZERO WIDTH SPACE
        0x200C,  # ZERO WIDTH NON-JOINER
        0x200D,  # ZERO WIDTH JOINER
        0x2060,  # WORD JOINER
        0x00AD,  # SOFT HYPHEN
        0x007F,  # DELETE
    )
)

#: C0 controls that survive step 3.  ``\r`` is kept here *only* so that step 4
#: can normalize ``\r\n`` and bare ``\r`` to ``\n``; if step 3 deleted it,
#: ``"a\rb"`` would become ``"ab"`` instead of the specified ``"a\nb"``.
_KEEP_CONTROLS: Final = frozenset({"\t", "\n", "\r"})


def strip_format_chars(s: str) -> str:
    """Step 3: remove invisible and format characters.

    Removes every explicitly named invisible, every code point in Unicode
    general category ``Cf``, and every C0 control except ``\\t``, ``\\n`` and
    ``\\r``.

    WHY: these characters are literally unrenderable.  Two cells that a human
    reads as identical must hash identically, or every copy-paste from a
    browser generates a permanent phantom drift.
    """
    out = []
    for ch in s:
        if ch in _KEEP_CONTROLS:
            out.append(ch)
            continue
        if ch in _EXPLICIT_INVISIBLES:
            continue
        if ch < " ":
            continue
        if unicodedata.category(ch) == "Cf":
            continue
        out.append(ch)
    return "".join(out)


# --------------------------------------------------------------------------
# Step 4 -- whitespace unification
# --------------------------------------------------------------------------


def unify_whitespace(s: str) -> str:
    """Step 4: fold every space separator to ``U+0020`` and every newline to ``\\n``.

    Every code point in category ``Zs`` (NBSP ``U+00A0``, ``U+1680``,
    ``U+2000``-``U+200A``, ``U+202F``, ``U+205F``, ``U+3000``) and ``U+0009``
    becomes a plain space; ``\\r\\n`` and bare ``\\r`` become ``\\n``.

    WHY: NBSP is the single most common invisible difference in exported
    spreadsheets -- it is what Excel and Google Sheets emit as a thousands
    separator and what a French keyboard emits before ``?`` and ``!``.
    """
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    out = []
    for ch in s:
        if ch == "\t" or (ch != "\n" and unicodedata.category(ch) == "Zs"):
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


# --------------------------------------------------------------------------
# Step 7 -- whitespace collapsing
# --------------------------------------------------------------------------

_SPACE_RUN: Final = re.compile(" +")


def collapse_ws(s: str) -> str:
    """Step 7: collapse runs of ``U+0020`` to one space, **per line**.

    WHY per line: collapsing across ``\\n`` would silently join the lines of a
    multi-line address block, turning a formatting-preserving column into a
    lossy one.  Newlines are structure; spaces within a line are not.
    """
    return "\n".join(_SPACE_RUN.sub(" ", line) for line in s.split("\n"))


# --------------------------------------------------------------------------
# §4.1 -- cosmetic punctuation folding (folded hash only)
# --------------------------------------------------------------------------

#: Exactly the table in CANONICALIZATION §4.1.  Applied **only** when computing
#: ``h_row_folded``; never in the primary canonical form.
_PUNCT_MAP: Final = {
    0x2018: "'",  # LEFT SINGLE QUOTATION MARK
    0x2019: "'",  # RIGHT SINGLE QUOTATION MARK
    0x201A: "'",  # SINGLE LOW-9 QUOTATION MARK
    0x201B: "'",  # SINGLE HIGH-REVERSED-9 QUOTATION MARK
    0x2032: "'",  # PRIME
    0x201C: '"',  # LEFT DOUBLE QUOTATION MARK
    0x201D: '"',  # RIGHT DOUBLE QUOTATION MARK
    0x201E: '"',  # DOUBLE LOW-9 QUOTATION MARK
    0x201F: '"',  # DOUBLE HIGH-REVERSED-9 QUOTATION MARK
    0x2033: '"',  # DOUBLE PRIME
    0x2010: "-",  # HYPHEN
    0x2011: "-",  # NON-BREAKING HYPHEN
    0x2012: "-",  # FIGURE DASH
    0x2013: "-",  # EN DASH
    0x2014: "-",  # EM DASH
    0x2015: "-",  # HORIZONTAL BAR
    0x2212: "-",  # MINUS SIGN
    0x2026: "...",  # HORIZONTAL ELLIPSIS
}


def fold_punct(s: str) -> str:
    """Fold smart punctuation to ASCII -- **cosmetic hash only**.

    WHY this is not in the primary canonical form: folding ``U+2019`` to ``'``
    everywhere would hide a real edit, and a write-back that normalizes what it
    compares can never converge -- it would rewrite the same cell every night
    forever.  Instead the strict hash keeps the difference and the folded hash
    proves it is only cosmetic, which is what downgrades the finding to
    ``COSMETIC_DRIFT``: reported, not auto-written.

    The dataset titles in this deployment (``Food CPG Master - Investor
    Directory (79)``, ``RE Portfolio - MASTER``) contain ``U+2014``, so this
    path is exercised daily.
    """
    return s.translate(_PUNCT_MAP)


def cosmetic_fold(s: str) -> str:
    """Full cosmetic normalization of a text payload for the folded hash.

    ``fold_punct`` + unconditional ``casefold()`` + unconditional
    ``collapse_ws`` + trim, regardless of the column's declared options.  The
    folded hash exists to answer one question -- "is this difference merely
    presentational?" -- so it must fold maximally, independent of the contract.
    """
    return collapse_ws(fold_punct(s).casefold()).strip(" \n")


# --------------------------------------------------------------------------
# Scalar coercion (step 2)
# --------------------------------------------------------------------------


def stringify_scalar(v: Any, warnings: list | None = None) -> str:
    """Step 2: turn a non-``str`` scalar into a string for a text column.

    ``Decimal`` uses ``format(v, 'f')`` -- fixed point, never scientific, so a
    large identifier does not become ``1.2345678901234567e+19``.  ``float``
    uses ``repr`` semantics (the shortest round-tripping literal).  ``bool`` is
    emitted as ``True``/``False`` (Python's own repr) so it is at least
    unambiguous.

    A number arriving at a text column is a *contract smell* -- the column
    should probably be ``number``, or ``assert_string_value`` should be set --
    so ``TEXT_COLUMN_RECEIVED_NUMBER`` is pushed to the warning sink for lane E
    to log.
    """
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "True" if v else "False"
    if isinstance(v, Decimal):
        add_warning(warnings, WARN_TEXT_COLUMN_RECEIVED_NUMBER)
        return format(v, "f")
    if isinstance(v, float):
        add_warning(warnings, WARN_TEXT_COLUMN_RECEIVED_NUMBER)
        return repr(v)
    if isinstance(v, int):
        add_warning(warnings, WARN_TEXT_COLUMN_RECEIVED_NUMBER)
        return str(v)
    return str(v)


# --------------------------------------------------------------------------
# Contract option access -- duck-typed so a dataclass *or* a plain dict works
# --------------------------------------------------------------------------


def opt(col: Any, name: str, default: Any) -> Any:
    """Read option ``name`` from a ColumnContract, a plain dict, or ``None``.

    WHY duck-typed: lane E may hand lane C either a ``ColumnContract`` instance
    or the plain dict it serialized into a Json field.  Requiring one shape
    would force a conversion at every call site, and a conversion is a place
    for a default to be silently dropped.  ``None`` values are treated as
    "unset" and fall back to ``default``, because a Json round trip turns an
    absent option into an explicit ``null``.
    """
    if col is None:
        return default
    if isinstance(col, dict):
        value = col.get(name, default)
    else:
        value = getattr(col, name, default)
    return default if value is None else value


# --------------------------------------------------------------------------
# Steps 1-6, reusable
# --------------------------------------------------------------------------


def text_prepare(v: Any, warnings: list | None = None) -> str:
    """Steps 1-6 of ``TEXT_CANON``, returned as a bare (untagged) string.

    ``NUM_CANON``, ``DATE_CANON``, ``DATETIME_CANON``, ``BOOL_CANON`` and
    ``SELECTION_CANON`` all begin by running their string input through these
    steps: NBSP thousands separators, BOMs and stray trailing spaces are
    extremely common in exported sheets, and every one of them would otherwise
    turn a perfectly good number into ``e:NOT_A_NUMBER``.

    Trimming is unconditional here (unlike step 6 of ``TEXT_CANON``, which
    honours ``col.text_trim``): leading/trailing whitespace is never meaningful
    inside a number, a date or a boolean literal.
    """
    if v is None or v is ABSENT:
        return ""
    s = stringify_scalar(v, warnings)
    s = strip_format_chars(s)  # step 3
    s = unify_whitespace(s)  # step 4
    s = unicodedata.normalize("NFC", s)  # step 5 -- NFC, never NFKC
    return s.strip(" \n")  # step 6


def TEXT_CANON(v: Any, col: Any = None, warnings: list | None = None) -> str:
    """Canonicalize ``v`` as text, returning exactly one tagged token.

    The ten ordered steps of CANONICALIZATION §4:

    1. absent / ``None`` -> ``z:``
    2. coerce scalars to ``str``
    3. strip invisible and format characters
    4. unify whitespace and newlines
    5. ``NFC`` (**never** ``NFKC``)
    6. trim, if ``col.text_trim`` (default True)
    7. collapse space runs per line, if ``col.text_collapse_ws`` (default True)
    8. ``casefold()``, if ``col.text_case == 'fold'`` (**never** ``lower()`` --
       ``casefold`` is what handles the German sharp s, Turkish dotless i and
       Greek final sigma)
    9. empty -> ``z:`` when ``col.empty_is_null`` (default True), which is what
       makes ``""``, ``"   "``, an NBSP-only cell and a missing cell collapse to
       one token
    10. return ``"s:" + s``

    Args:
        v: the raw cell value from either side.
        col: a ``ColumnContract``, an equivalent dict, or ``None`` for defaults.
        warnings: optional sink for advisory codes (see ``tokens.add_warning``).

    Returns:
        ``"z:"`` or ``"s:<normalized text>"``.  Never raises for a data reason.
    """
    if v is None or v is ABSENT:
        return NULL_TOKEN  # step 1

    s = stringify_scalar(v, warnings)  # step 2
    s = strip_format_chars(s)  # step 3
    s = unify_whitespace(s)  # step 4
    s = unicodedata.normalize("NFC", s)  # step 5

    if opt(col, "text_trim", True):  # step 6
        s = s.strip(" \n")

    if opt(col, "text_collapse_ws", True):  # step 7
        s = collapse_ws(s)

    if opt(col, "text_case", "preserve") == "fold":  # step 8
        s = s.casefold()

    if s == "" and opt(col, "empty_is_null", True):  # step 9
        return NULL_TOKEN

    return TAG_TEXT + s  # step 10
