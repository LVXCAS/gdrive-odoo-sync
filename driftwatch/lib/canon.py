"""``CANON`` -- the ctype dispatcher and the relational canonicalizers (lane C).

WHY THIS MODULE EXISTS
======================
Every other module in this package canonicalizes *one* type family. This one is
the single door they are all reached through, and it owns the three things that
must happen **before** any of them runs (CANONICALIZATION §3.1), in this order:

1. **A spreadsheet error cell is an error, never an empty cell.** Under
   ``valueRenderOption='UNFORMATTED_VALUE'`` -- the bulk read path that stages
   99 % of cells -- ``values.batchGet`` flattens ``#REF!`` to the *literal
   string* ``"#REF!"``. A text column would canonicalize that to ``s:#REF!``,
   which hashes stably, compares equal to itself run after run, and is reported
   ``verified``. That is silent data loss, and ``tokens.SHEET_ERROR_LITERALS``
   exists precisely so it cannot happen. Mapping such a cell to ``z:`` would be
   worse still: it would look like a deliberate blank.
2. **An identifier that arrived as a number is unrecoverable.** ``"007"`` read
   as a number is already ``7`` and ``12345678901234567890`` is already
   ``1.2345678901234567e19``. There is nothing to repair, so the cell is refused
   with ``e:IDENTIFIER_NUMERIC`` and the row is quarantined. Zero-padding it
   back to some guessed width would invent data.
3. **Absent is not empty and empty is not false.** ``ABSENT`` (a ragged row that
   simply stopped) and an Odoo ``False`` on a Char/Text/Html field both mean
   "there is no value here"; ``False`` on a Boolean field is a real value. The
   branch is on the declared field type, never on truthiness.

**There is no type sniffing here.** The type comes from ``col.ctype``, always.

Stdlib only.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any, Final

from .bool_canon import BOOL_CANON
from .contract import (
    CTYPE_BOOL,
    CTYPE_DATE,
    CTYPE_DATETIME,
    CTYPE_IGNORE,
    CTYPE_M2M,
    CTYPE_M2O,
    CTYPE_MONEY,
    CTYPE_NUMBER,
    CTYPE_SELECTION,
    CTYPE_TEXT,
)
from .datetime_canon import DATE_CANON, DATETIME_CANON
from .number_canon import NUM_CANON
from .text_canon import TEXT_CANON, opt, text_prepare
from .tokens import (
    ABSENT,
    ERR_CELL_ERROR,
    ERR_IDENTIFIER_NUMERIC,
    ERR_ORPHAN_REFERENCE,
    ERR_UNRESOLVED_SELECTION,
    NULL_TOKEN,
    SHEET_ERROR_LITERALS,
    SheetErrorValue,
    TAG_REL,
    TAG_SELECTION,
    TAG_TEXT,
    error,
)

__all__ = [
    "CANON",
    "SELECTION_CANON",
    "M2O_CANON",
    "M2M_CANON",
    "is_sheet_error",
    "violates_string_assertion",
]

#: The text options used when a *relational business key* is canonicalized.
#: Fixed rather than taken from the column, because the key is an identifier:
#: the same partner must resolve to the same ``r:`` token whether the column
#: that mentions it declared ``text_case='fold'`` or not.
_KEY_TEXT_OPTS: Final = {
    "text_trim": True,
    "text_collapse_ws": True,
    "text_case": "preserve",
    "empty_is_null": True,
}


# --------------------------------------------------------------------------
# Pre-dispatch guards
# --------------------------------------------------------------------------


def is_sheet_error(v: Any, col: Any = None, side: str = "sheet") -> bool:
    """True when ``v`` is a spreadsheet error cell.

    Two shapes reach lane C for the same underlying condition:

    * :class:`~.tokens.SheetErrorValue` -- the typed ``errorValue`` branch of the
      ``ExtendedValue`` oneof, produced by ``read_effective_values``. Always an
      error, on any side, regardless of contract options.
    * one of :data:`~.tokens.SHEET_ERROR_LITERALS` as a bare string -- what the
      bulk ``values.batchGet`` path flattens the same cell to. Recognised on the
      sheet side only, and only when ``col.detect_error_literals`` (default
      True) is set, because an Odoo Char column is allowed to legitimately
      contain the text ``"#N/A"``.
    """
    if isinstance(v, SheetErrorValue):
        return True
    if side != "sheet" or not isinstance(v, str):
        return False
    if not opt(col, "detect_error_literals", True):
        return False
    return v.strip() in SHEET_ERROR_LITERALS


def violates_string_assertion(v: Any, col: Any = None, side: str = "sheet") -> bool:
    """True when an identifier column received a value Sheets read as a number.

    Only meaningful on the sheet side: ``UNFORMATTED_VALUE`` hands back a Python
    ``float``/``int`` for a cell Sheets decided was numeric, and by the time the
    value reaches this process the leading zeros and the digits past the 15th
    significant one are already gone. ``bool`` is excluded because ``True`` is
    not a number that lost precision.
    """
    if side != "sheet" or not opt(col, "assert_string_value", False):
        return False
    return isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)


def _is_absent(v: Any, col: Any, side: str) -> bool:
    """True when ``v`` means "there is no value in this cell".

    ``None`` and ``ABSENT`` always. On the Odoo side ``False`` as well, for every
    ctype except ``bool`` -- the ORM returns ``False`` for an empty Char, Text,
    Html, Selection and Many2one, and only a Boolean field can mean it. Branching
    on the declared ctype rather than on truthiness is mandatory: ``False`` on a
    Boolean field is a real value, and collapsing it to ``z:`` would make every
    unticked checkbox indistinguishable from an unfilled one.
    """
    if v is None or v is ABSENT:
        return True
    if side == "odoo" and v is False:
        return opt(col, "ctype", CTYPE_TEXT) != CTYPE_BOOL
    return False


# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------


def SELECTION_CANON(v: Any, col: Any = None, side: str = "sheet", warnings: list | None = None) -> str:
    """Canonicalize an Odoo ``Selection`` value to ``k:<technical key>``.

    Odoo stores technical keys (``'draft'``); the sheet almost always holds
    labels (``'Draft'``). **Never compare against labels** -- a translated or
    re-worded label would read as a data change on a record nobody touched.

    Sheet side: the label is ``TEXT_CANON``'d, casefolded, and looked up in
    ``col.value_map``. A miss is ``e:UNRESOLVED_SELECTION``, never a pass-through
    of the raw label: passing it through would hash a label against a key and
    report drift on every row of the column forever.
    """
    if side == "odoo":
        token = TEXT_CANON(v, _KEY_TEXT_OPTS, warnings)
        return NULL_TOKEN if token == NULL_TOKEN else TAG_SELECTION + token[len(TAG_TEXT):]

    token = TEXT_CANON(v, col, warnings)
    if token == NULL_TOKEN:
        return NULL_TOKEN
    label = token[len(TAG_TEXT):]
    value_map = opt(col, "value_map", {}) or {}
    folded = {str(k).casefold(): value_map[k] for k in value_map}
    key = folded.get(label.casefold())
    if key is None:
        return error(ERR_UNRESOLVED_SELECTION)
    return TAG_SELECTION + str(key)


# --------------------------------------------------------------------------
# Relations
# --------------------------------------------------------------------------


def _m2o_id_of(v: Any) -> Any:
    """Extract the database id from the several shapes Odoo hands back."""
    if isinstance(v, (list, tuple)):
        return v[0] if v else None
    return v


def M2O_CANON(v: Any, col: Any = None, side: str = "sheet", warnings: list | None = None) -> str:
    """Canonicalize a ``many2one`` to ``r:<id>`` or ``r:<business key>``.

    **``display_name`` is never hashed.** It is rendered, translated and
    format-dependent, so it drifts when nothing changed. When
    ``col.m2o_compare_by == 'key'`` lane E must therefore resolve the comodel
    record and pass ``record[col.m2o_match_field]``; being handed the raw
    ``[id, display_name]`` pair instead is a plumbing error and raises, because
    silently hashing element 1 of that pair is exactly the bug this rule exists
    to prevent.
    """
    compare_by = opt(col, "m2o_compare_by", "key")

    if compare_by == "id":
        raw_id = _m2o_id_of(v)
        if raw_id is None or raw_id is False or raw_id == "":
            return NULL_TOKEN
        try:
            return TAG_REL + str(int(raw_id))
        except (TypeError, ValueError):
            # A business key arrived at a column declared to compare by id. The
            # reference cannot be resolved here, and inventing one is worse.
            return error(ERR_ORPHAN_REFERENCE)

    if isinstance(v, (list, tuple)):
        raise ValueError(
            "Column %r compares many2one values by business key but received the raw "
            "[id, display_name] pair. Lane E must resolve %r first; display_name is "
            "never hashed."
            % (opt(col, "key", "") or opt(col, "odoo_field", ""), opt(col, "m2o_match_field", "name"))
        )
    token = TEXT_CANON(v, _KEY_TEXT_OPTS, warnings)
    if token == NULL_TOKEN:
        return NULL_TOKEN
    return TAG_REL + token[len(TAG_TEXT):]


def M2M_CANON(v: Any, col: Any = None, side: str = "sheet", warnings: list | None = None) -> str:
    """Canonicalize a ``many2many`` / ``one2many`` to ``r:[a,b,c]``.

    ORM iteration order is not guaranteed stable, so the members are **sorted**
    -- numerically when they are ids, byte-wise on the canonical token when they
    are business keys. Without the sort the same set of tags hashes differently
    on two consecutive reads and every row reports drift.
    """
    if v is None or v is ABSENT or v is False or v == "":
        return NULL_TOKEN
    if isinstance(v, (str, bytes)):
        members: Sequence[Any] = [p for p in text_prepare(v, warnings).split(",") if p.strip()]
    elif isinstance(v, Sequence):
        members = list(v)
    else:
        members = [v]
    if not members:
        return NULL_TOKEN

    resolved = [_m2o_id_of(m) for m in members]
    resolved = [m for m in resolved if m is not None and m is not False and m != ""]
    if not resolved:
        return NULL_TOKEN

    try:
        parts = [str(int(m)) for m in resolved]
        parts.sort(key=int)
    except (TypeError, ValueError):
        tokens = []
        for member in resolved:
            token = TEXT_CANON(member, _KEY_TEXT_OPTS, warnings)
            if token != NULL_TOKEN:
                tokens.append(token[len(TAG_TEXT):])
        if not tokens:
            return NULL_TOKEN
        parts = sorted(tokens)
    return TAG_REL + "[" + ",".join(parts) + "]"


# --------------------------------------------------------------------------
# The dispatcher
# --------------------------------------------------------------------------


def CANON(raw: Any, col: Any = None, side: str = "sheet", warnings: list | None = None) -> str:
    """Canonicalize one cell into exactly one tagged token.

    Args:
        raw: the value from either side, in whatever shape that side produces.
        col: a ``ColumnContract``, an equivalent dict, or ``None`` for text
            defaults.
        side: ``'sheet'`` or ``'odoo'``. It selects the Odoo-side extractors and
            gates the two sheet-only guards; it is **not** a hint that anything
            may be guessed.
        warnings: optional sink for advisory codes (see ``tokens.add_warning``).

    Returns:
        One token from the ``z: s: n: b: d: t: r: k: e:`` families.

    Raises:
        ValueError: only for a *contract* error -- an unknown ctype, an
            ``ignore`` column that should never have been dispatched, a datetime
            with no declared timezone, or a many2one handed a display name. Data
            errors never raise; they return an ``e:`` token (CANONICALIZATION
            §12 invariant 2).
    """
    if side not in ("sheet", "odoo"):
        raise ValueError("side must be 'sheet' or 'odoo', got %r" % (side,))

    ctype = opt(col, "ctype", CTYPE_TEXT)
    if ctype == CTYPE_IGNORE:
        raise ValueError(
            "CANON() was called for an 'ignore' column; such columns are excluded "
            "from the contract payload entirely and must be filtered out before "
            "dispatch, not canonicalized to a placeholder token."
        )

    # --- guard 1: a spreadsheet error cell is never a value and never empty ---
    if is_sheet_error(raw, col, side):
        return error(ERR_CELL_ERROR)

    # --- guard 2: an identifier read as a number is unrecoverable -------------
    if violates_string_assertion(raw, col, side):
        return error(ERR_IDENTIFIER_NUMERIC)

    # --- guard 3: absent -----------------------------------------------------
    if _is_absent(raw, col, side):
        raw = None  # let each canonicalizer apply its own declared empty

    # --- dispatch ------------------------------------------------------------
    if ctype == CTYPE_TEXT:
        return TEXT_CANON(raw, col, warnings)
    if ctype in (CTYPE_NUMBER, CTYPE_MONEY):
        return NUM_CANON(raw, col, warnings)
    if ctype == CTYPE_BOOL:
        return BOOL_CANON(raw, col, warnings)
    if ctype == CTYPE_DATE:
        return DATE_CANON(raw, col, side, warnings)
    if ctype == CTYPE_DATETIME:
        return DATETIME_CANON(raw, col, opt(col, "sheet_timezone", ""), side, warnings)
    if ctype == CTYPE_SELECTION:
        return SELECTION_CANON(raw, col, side, warnings)
    if ctype == CTYPE_M2O:
        return M2O_CANON(raw, col, side, warnings)
    if ctype == CTYPE_M2M:
        return M2M_CANON(raw, col, side, warnings)

    raise ValueError(
        "Unknown ctype %r; lane C refuses to guess a type family, because a wrong "
        "guess produces a stable, plausible and wrong hash." % (ctype,)
    )
