"""``BOOL_CANON`` -- boolean coercion that refuses to guess (lane C).

WHY THIS MODULE EXISTS
======================
The tempting implementation is ``bool(value)``.  It is also the single most
dangerous line of code in a sync: it maps ``"maybe"``, ``"pending"``, ``"TBD"``
and ``"?"`` to **true**, and ``"no"``, ``"n"`` and ``"false"`` to **true** as
well, because every non-empty string is truthy in Python.  The
almost-as-tempting variant -- defaulting anything unrecognised to *false* --
means the system reports ``verified`` over data it actively misread.

So this module recognises exactly the tokens the administrator declared, and
everything else becomes ``e:BAD_BOOL``, which quarantines the row.  Reporting
"I could not read this cell" is always better than reporting a confident wrong
answer.

One more trap, on the Odoo side: ``False`` on a ``fields.Boolean`` is a **real
value**, not NULL.  Branch on the *field type*, never on truthiness.

Stdlib only.
"""

from __future__ import annotations

from typing import Any, Final

from .text_canon import TEXT_CANON, opt
from .tokens import (
    ABSENT,
    ERR_BAD_BOOL,
    FALSE_TOKEN,
    NULL_TOKEN,
    TRUE_TOKEN,
    error,
)

__all__ = ["BOOL_CANON", "BOOL_TEXT_OPTS", "empty_bool_token"]

#: The fixed text options ``BOOL_CANON`` uses for string input, regardless of
#: the column's own text options: trim, collapse, and **casefold**.  A boolean
#: literal has no meaningful case or surrounding whitespace, and letting a
#: column's ``text_case='preserve'`` make ``"TRUE"`` unrecognisable would be a
#: pure footgun.
BOOL_TEXT_OPTS: Final = {
    "text_trim": True,
    "text_collapse_ws": True,
    "text_case": "fold",
    "empty_is_null": True,
}


def empty_bool_token(col: Any) -> str:
    """Resolve an empty boolean cell through the declared ``empty_means``.

    ``false`` (default) -> ``b:0``; ``null`` -> ``z:``; ``error`` -> the
    ``e:BAD_BOOL`` token, for columns where a blank is genuinely a data defect
    (a required consent flag, say) rather than a default.
    """
    mode = opt(col, "empty_means", "false")
    if mode == "null":
        return NULL_TOKEN
    if mode == "error":
        return error(ERR_BAD_BOOL)
    return FALSE_TOKEN


def BOOL_CANON(v: Any, col: Any = None, warnings: list | None = None) -> str:
    """Canonicalize ``v`` as a boolean, returning ``b:0``, ``b:1``, ``z:`` or ``e:``.

    Steps (CANONICALIZATION §7):

    1. a real ``True``/``False`` (Sheets ``boolValue``, Odoo ``Boolean``) passes
       straight through;
    2. ``None`` / absent resolves through ``col.empty_means``;
    3. anything else is normalized as text with ``case='fold'``; an empty
       result falls back to step 2;
    4. membership test against the casefolded ``truthy`` / ``falsy`` lists;
    5. anything else -> ``e:BAD_BOOL``, and the row is quarantined.

    Numbers are handled by the membership test too, so a numeric ``1``/``0``
    matches the default ``truthy``/``falsy`` lists, while a numeric ``2`` --
    which no sane contract means as a boolean -- is refused rather than
    silently truthy.
    """
    if v is True:
        return TRUE_TOKEN
    if v is False:
        return FALSE_TOKEN
    if v is None or v is ABSENT:
        return empty_bool_token(col)

    token = TEXT_CANON(v, BOOL_TEXT_OPTS, warnings)
    if token == NULL_TOKEN:
        return empty_bool_token(col)

    s = token[2:]  # strip the "s:" tag

    truthy = {t.casefold() for t in (opt(col, "truthy", ()) or ())}
    falsy = {f.casefold() for f in (opt(col, "falsy", ()) or ())}
    if not truthy and not falsy:
        from .contract import DEFAULT_FALSY, DEFAULT_TRUTHY  # local: avoids a cycle

        truthy = {t.casefold() for t in DEFAULT_TRUTHY}
        falsy = {f.casefold() for f in DEFAULT_FALSY}

    if s in truthy:
        return TRUE_TOKEN
    if s in falsy:
        return FALSE_TOKEN

    # A number that survived TEXT_CANON as "1.0"/"0.0" still means 1/0.
    numeric = s.rstrip("0").rstrip(".") if "." in s else s
    if numeric in truthy:
        return TRUE_TOKEN
    if numeric in falsy:
        return FALSE_TOKEN

    return error(ERR_BAD_BOOL)
