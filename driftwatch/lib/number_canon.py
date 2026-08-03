"""``NUM_CANON`` -- exact decimal canonicalization (lane C).

WHY THIS MODULE EXISTS
======================
Google Sheets stores every number as an IEEE-754 double and returns doubles
over the API; Odoo ``Float`` columns are ``double precision`` in Postgres
unless ``digits=`` is set, and JSON-RPC hands you Python floats.  ``12.30`` is
not representable in binary floating point, so two independent paths to "twelve
thirty" land on ``12.299999999999999`` and ``12.300000000000001``.  A tool that
tells a user two cells both reading ``12.30`` differ has destroyed its own
credibility on day one.

Two rules follow, and both are absolute:

* **Never compare floats with ``==``.**  Comparison happens on canonical
  *strings* produced here.
* **Tolerance is baked into the hash by quantizing inside the canonicalizer.**
  That is the only way hashing and tolerance are compatible at all -- hashing
  is exact by nature, so the tolerance has to live in the canonical form and
  the hash inherits it.  There is no global epsilon, ever: a single ``EPS`` is
  guaranteed to be simultaneously too tight for computed margins and too loose
  for cents.  Tolerance is a per-column business property.

Stdlib only: ``decimal``, ``re``.
"""

from __future__ import annotations

import re
from decimal import (
    Decimal,
    InvalidOperation,
    ROUND_FLOOR,
    ROUND_HALF_UP,
    localcontext,
)
from typing import Any, Final

from .text_canon import opt, text_prepare
from .tokens import (
    ABSENT,
    ERR_NOT_A_NUMBER,
    ERR_NOT_FINITE,
    NULL_TOKEN,
    TAG_NUMBER,
    WARN_ROUNDING_BOUNDARY,
    add_warning,
    error,
)

__all__ = [
    "NUM_CANON",
    "to_decimal",
    "raw_decimal",
    "near_boundary",
    "step_for_scale",
    "quantize",
    "format_fixed",
    "DEFAULT_CURRENCY_TOKENS",
]

#: Currency affixes stripped from the head of a string before parsing.  Longest
#: first, so ``USD`` is matched before ``U`` could ever be.  Column-declared
#: extras are prepended by ``_currency_pattern``.
DEFAULT_CURRENCY_TOKENS: Final = (
    "USD",
    "EUR",
    "GBP",
    "CAD",
    "AUD",
    "CHF",
    "JPY",
    "$",
    chr(0x20AC),  # EURO SIGN
    chr(0x00A3),  # POUND SIGN
    chr(0x00A5),  # YEN SIGN
    chr(0x20B9),  # INDIAN RUPEE SIGN
)

#: Working precision for the intermediate ``Decimal`` arithmetic.  Generous
#: enough that no realistic spreadsheet value is rounded by the *context*
#: before the explicit ``quantize`` step -- the only rounding that may ever
#: happen is the declared one.
_WORK_PREC: Final = 80

#: What a cleaned numeric string is allowed to look like.  Anything else is
#: ``e:NOT_A_NUMBER`` rather than something ``Decimal`` might accept with
#: surprising semantics (``Decimal('nan')``, ``Decimal('  1')``, and the
#: locale-free but confusing ``Decimal('1_0')`` in some versions).
_NUMERIC_RE: Final = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")


def _currency_pattern(col: Any) -> re.Pattern:
    """Build the leading-currency-affix regex for ``col``.

    Rebuilt per call rather than cached on the (frozen, but arbitrary) contract
    object: the cost is negligible next to a network round trip, and a cache
    keyed on a mutable dict contract would be a correctness hazard.
    """
    extras = tuple(opt(col, "currency_symbols", ()) or ())
    tokens = sorted(set(extras) | set(DEFAULT_CURRENCY_TOKENS), key=len, reverse=True)
    alternation = "|".join(re.escape(t) for t in tokens)
    # The optional leading group preserves an accounting parenthesis or a sign
    # that precedes the symbol, e.g. "($1,234.50)" and "-$5".
    return re.compile(r"^(?P<pre>[(+-]?\s*)(?:%s)\s*(?P<rest>.*)$" % alternation)


def to_decimal(v: Any) -> Decimal:
    """Convert a *numeric* Python value to ``Decimal`` losslessly.

    **``Decimal(repr(v))``, never ``Decimal(v)``, for floats.**  ``Decimal(0.1)``
    is ``0.1000000000000000055511151231257827021181583404541015625``, which
    defeats the entire point of moving to ``Decimal``.  ``repr`` gives the
    shortest literal that round-trips, ``"0.1"``, which is what the user typed
    and what the other side will also produce.

    ``bool`` is coerced through ``int`` deliberately: a boolean reaching a
    numeric column is a contract smell, but silently hashing ``True`` as a
    string would be worse than hashing it as ``1``.
    """
    if isinstance(v, Decimal):
        return v
    if isinstance(v, bool):
        return Decimal(int(v))
    if isinstance(v, int):
        return Decimal(v)
    if isinstance(v, float):
        return Decimal(repr(v))
    raise TypeError("to_decimal() expects a numeric value, got %r" % (type(v).__name__,))


def step_for_scale(scale: int) -> Decimal:
    """The quantization step for ``scale``: ``Decimal(1).scaleb(-scale)``."""
    return Decimal(1).scaleb(-int(scale))


def quantize(d: Decimal, scale: int) -> Decimal:
    """Quantize ``d`` to ``scale`` fraction digits with ``ROUND_HALF_UP``.

    **``ROUND_HALF_UP`` (away from zero), never ``ROUND_HALF_EVEN``.**  Odoo's
    ``float_round`` default is half-up; Python's ``Decimal`` default is
    banker's rounding.  Using the default would guarantee disagreement with
    Odoo on every ``.5`` case -- a whole class of drifts that exist only
    because the two sides round differently.

    ``Decimal('-0.00')`` is collapsed to ``Decimal('0.00')`` so that a value
    that rounds to zero from below hashes identically to one that rounds to
    zero from above.
    """
    with localcontext() as ctx:
        ctx.prec = _WORK_PREC
        ctx.rounding = ROUND_HALF_UP
        q = d.quantize(step_for_scale(scale), rounding=ROUND_HALF_UP)
        if q == 0:
            q = abs(q)
        return q


def format_fixed(q: Decimal) -> str:
    """Format ``q`` in fixed point -- **never** scientific notation.

    ``format(q, 'f')`` keeps a leading ``-`` for negatives and never emits
    ``+`` or an exponent.  ``str(Decimal('1E+3'))`` would emit ``1E+3``, and a
    hash over that string is a hash over a display format, not a value.
    """
    return format(q, "f")


def NUM_CANON(v: Any, col: Any = None, warnings: list | None = None) -> str:
    """Canonicalize ``v`` as a number or money amount.

    Steps (CANONICALIZATION §5):

    1. numeric input -> ``Decimal`` via ``to_decimal`` (``repr`` for floats);
    2. string input -> ``TEXT_CANON`` steps 1-6, then leading currency affix,
       trailing ``%``, accounting negatives, group separator removal and
       decimal separator replacement -- **all declared, never guessed**;
    3. NaN/Inf -> ``e:NOT_FINITE``;
    4. resolve the scale from the *domain* (``currency`` / ``uom`` / ``fixed``);
    5. ``quantize(..., ROUND_HALF_UP)``;
    6. collapse ``-0``;
    7. fixed-point format;
    8. return ``"n:" + s``.

    Returns:
        ``z:`` for an empty cell (when ``empty_is_null``), ``n:<fixed point>``,
        or an ``e:`` token.  Never raises for a data reason; raises only when
        the *contract* is unusable (an unresolved currency/uom scale).
    """
    if v is None or v is ABSENT:
        return NULL_TOKEN if opt(col, "empty_is_null", True) else _zero_token(col)

    scale = _scale_of(col)
    percent_divide = False

    if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
        d = to_decimal(v)
    elif isinstance(v, bool):
        d = to_decimal(v)
    else:
        s = text_prepare(v, warnings)
        if s == "":
            return NULL_TOKEN if opt(col, "empty_is_null", True) else _zero_token(col)

        # (a) leading currency symbol or ISO code, keeping any sign/paren.
        m = _currency_pattern(col).match(s)
        if m:
            s = (m.group("pre") or "") + (m.group("rest") or "")
            s = s.strip()

        # (b) trailing percent sign.  Stripped **only** when the contract
        # declares how to read a percent.  Under the default
        # ``percent_mode="none"`` the sign used to be discarded silently, so
        # ``"50%"`` and ``"50"`` both produced ``n:50.00`` -- identical row
        # hashes, identical bucket digests, a ``verified`` result, and a user
        # who edited a cell from ``5`` to ``5%`` (a hundredfold change in
        # meaning) got zero drift.  Refusing is correct; dropping the sign is
        # the "different values hash the same" case the module docstring puts
        # explicitly out of scope.
        if s.endswith("%"):
            if opt(col, "percent_mode", "none") != "divide_100":
                return error(ERR_NOT_A_NUMBER)
            s = s[:-1].rstrip()
            percent_divide = True

        # (c) accounting negatives.
        negative = False
        if opt(col, "accounting_negatives", True):
            if s.startswith("(") and s.endswith(")"):
                s = s[1:-1].strip()
                negative = True
            elif s.endswith("-"):
                s = s[:-1].strip()
                negative = True
            if s.startswith("+"):
                s = s[1:].strip()

        # (d) declared separators.  Order matters: strip the group separator
        # first, because in de-DE the group separator is "." and would
        # otherwise be mistaken for a decimal point by the replacement below.
        group_sep = opt(col, "group_sep", ",")
        decimal_sep = opt(col, "decimal_sep", ".")
        if group_sep:
            s = s.replace(group_sep, "")
        if decimal_sep and decimal_sep != ".":
            s = s.replace(decimal_sep, ".")
        s = s.replace(" ", "")

        if negative and s and not s.startswith("-"):
            s = "-" + s

        # (e) parse.
        if not _NUMERIC_RE.match(s):
            return error(ERR_NOT_A_NUMBER)
        try:
            d = Decimal(s)
        except InvalidOperation:
            return error(ERR_NOT_A_NUMBER)

    if d.is_nan() or d.is_infinite():
        return error(ERR_NOT_FINITE)

    if percent_divide:
        # scaleb is an exact exponent shift -- no division, no precision loss.
        with localcontext() as ctx:
            ctx.prec = _WORK_PREC
            d = d.scaleb(-2)

    if near_boundary(d, step_for_scale(scale)):
        add_warning(warnings, WARN_ROUNDING_BOUNDARY, format_fixed(d))

    try:
        q = quantize(d, scale)
    except InvalidOperation:
        # Only reachable for absurd magnitudes (>1e80); refusing is correct,
        # inventing a rounded value is not.
        return error(ERR_NOT_FINITE)

    return TAG_NUMBER + format_fixed(q)


def _scale_of(col: Any) -> int:
    """Resolve the declared scale, honouring ``ColumnContract.effective_scale``."""
    if col is None:
        return 2
    effective = getattr(col, "effective_scale", None)
    if effective is not None and not isinstance(col, dict):
        return int(effective)
    scale_mode = opt(col, "scale_mode", "fixed")
    if scale_mode == "fixed":
        return int(opt(col, "scale", 2))
    resolved = opt(col, "resolved_scale", None)
    if resolved is None:
        raise ValueError(
            "scale_mode=%r requires resolved_scale on the contract; lane C never "
            "queries Odoo for currency or UoM precision." % (scale_mode,)
        )
    return int(resolved)


def _zero_token(col: Any) -> str:
    """The declared non-null empty for a numeric column: a quantized zero.

    Only reached when ``empty_is_null`` is explicitly False, i.e. the
    administrator declared that a blank cell in this column means zero.
    """
    return TAG_NUMBER + format_fixed(quantize(Decimal(0), _scale_of(col)))


def raw_decimal(v: Any) -> Decimal | None:
    """Parse ``v`` with **minimal** cleaning and **no quantization**.

    Lane E sums these per numeric column on both sides and stores the totals in
    ``gdrive.verification.column_totals``.

    WHY this is deliberately *not* derived from the canonical form: if both
    sides are canonicalized wrongly in the same way, the hashes agree *and* the
    canonical totals agree -- and only an independent raw total disagrees.  It
    is the one control that can catch a symmetric normalizer bug, and it only
    works because it shares no code with the normalizer.

    Returns:
        ``Decimal`` or ``None`` when the value is empty or unparseable.  A NaN
        or infinity returns ``None``: including it in a sum would poison the
        whole column total.
    """
    if v is None or v is ABSENT:
        return None
    if isinstance(v, bool):
        return Decimal(int(v))
    if isinstance(v, Decimal):
        return None if (v.is_nan() or v.is_infinite()) else v
    if isinstance(v, int):
        return Decimal(v)
    if isinstance(v, float):
        d = Decimal(repr(v))
        return None if (d.is_nan() or d.is_infinite()) else d
    s = str(v).strip()
    if not s:
        return None
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return None if (d.is_nan() or d.is_infinite()) else d


def near_boundary(d: Decimal, step: Decimal) -> bool:
    """True when ``d`` sits within ``1e-9 * step`` of a half-step boundary.

    These are the values that flip classification between runs -- ``2.005`` at
    scale 2 rounds to ``2.01`` from one side and ``2.00`` from the other
    depending on the last bit of a float that no user can see -- and they
    generate the mystery "drift appeared and vanished" tickets.  Lane E logs
    ``ROUNDING_BOUNDARY`` at ``warning`` for them so the pattern is visible
    before someone spends a day on it.
    """
    if d.is_nan() or d.is_infinite() or step == 0:
        return False
    with localcontext() as ctx:
        ctx.prec = _WORK_PREC
        ratio = d / step
        floor = ratio.to_integral_value(rounding=ROUND_FLOOR)
        nearest_half = (floor + Decimal("0.5")) * step
        alt_half = (floor - Decimal("0.5")) * step
        distance = min(abs(d - nearest_half), abs(d - alt_half))
        return distance < (Decimal("1e-9") * abs(step))
