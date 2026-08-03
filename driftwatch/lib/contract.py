"""The column contract -- the plain-data schema lane E hands to lane C.

WHY THIS MODULE EXISTS
======================
**There is no type sniffing anywhere in this library.**  The type of a cell
comes from ``col.ctype``, always.  Two different columns may legitimately
canonicalize the raw value ``"1"`` differently -- ``s:1``, ``n:1.00``, ``b:1``
-- which is precisely why per-cell inference is wrong and is forbidden.  The
same reasoning applies to decimal separators (``"1.234"`` is 1234 in de-DE and
1.234 in en-US and *no heuristic can tell*), to date formats (``"03/04/2026"``
is genuinely unresolvable) and to booleans (``"maybe"`` is not false).

So every one of those decisions is a **declared** contract option, carried in
this dataclass, authored by an administrator on ``gdrive.mapping.column``
(SPEC §3.9) and serialized here.  Lane C consumes it and never queries Odoo.

``ColumnContract`` is frozen and hashable-by-value so that a contract can be
cached, compared, and fed to ``compute_spec_version`` deterministically.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, fields, replace
from typing import Any, Final, Iterable, Mapping, Sequence

from .spec_version import compute_spec_version
from .tokens import NULL_TOKEN

__all__ = [
    "CTYPES",
    "CTYPE_TEXT",
    "CTYPE_NUMBER",
    "CTYPE_MONEY",
    "CTYPE_BOOL",
    "CTYPE_DATE",
    "CTYPE_DATETIME",
    "CTYPE_SELECTION",
    "CTYPE_M2O",
    "CTYPE_M2M",
    "CTYPE_IGNORE",
    "DEFAULT_TRUTHY",
    "DEFAULT_FALSY",
    "DEFAULT_DATE_FORMATS",
    "CHARLIKE_ODOO_TYPES",
    "ColumnContract",
    "default_text_contract",
    "validate_contract",
    "validate_contracts",
    "contract_from_mapping_dict",
    "contracts_from_mapping_dicts",
    "contract_to_dict",
    "serialize_contracts",
    "spec_version_for_contracts",
    "slugify",
    "slugify_all",
    "SLUG_RE",
]

# --------------------------------------------------------------------------
# ctype vocabulary (SPEC §3.9)
# --------------------------------------------------------------------------

CTYPE_TEXT: Final = "text"
CTYPE_NUMBER: Final = "number"
CTYPE_MONEY: Final = "money"
CTYPE_BOOL: Final = "bool"
CTYPE_DATE: Final = "date"
CTYPE_DATETIME: Final = "datetime"
CTYPE_SELECTION: Final = "selection"
CTYPE_M2O: Final = "many2one"
CTYPE_M2M: Final = "m2m"
CTYPE_IGNORE: Final = "ignore"

CTYPES: Final = (
    CTYPE_TEXT,
    CTYPE_NUMBER,
    CTYPE_MONEY,
    CTYPE_BOOL,
    CTYPE_DATE,
    CTYPE_DATETIME,
    CTYPE_SELECTION,
    CTYPE_M2O,
    CTYPE_M2M,
    CTYPE_IGNORE,
)

#: SPEC §3.9 defaults, verbatim.  ``chr(0x2713)`` is CHECK MARK -- written
#: numerically so a re-encoding of this file cannot quietly change what counts
#: as "true" in every boolean column in the system.
DEFAULT_TRUTHY: Final = ("true", "yes", "y", "1", "x", chr(0x2713))
DEFAULT_FALSY: Final = ("false", "no", "n", "0")
DEFAULT_DATE_FORMATS: Final = ("%Y-%m-%d", "%m/%d/%Y")

#: Odoo field types on which a returned ``False`` means "empty", not "the
#: boolean false".  Branching on the *field type* rather than on truthiness is
#: mandatory: ``False`` on a Boolean field is a real value.
CHARLIKE_ODOO_TYPES: Final = frozenset({"char", "text", "html", "selection", "many2one"})

_SIDES: Final = ("sheet", "odoo")
_TEXT_CASES: Final = ("preserve", "fold")
_PERCENT_MODES: Final = ("none", "divide_100")
_SCALE_MODES: Final = ("currency", "uom", "fixed")
_EMPTY_MEANS: Final = ("false", "null", "error")
_AUTHORITIES: Final = ("sheet", "odoo", "report")
_M2O_COMPARE_BY: Final = ("key", "id")

SLUG_RE: Final = re.compile(r"^[a-z_][a-z0-9_]*$")


# --------------------------------------------------------------------------
# The contract
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ColumnContract:
    """One column's declared canonicalization behaviour.

    Mirrors ``gdrive.mapping.column`` (SPEC §3.9) field for field, plus three
    fields lane E must *resolve* before calling lane C, because lane C is
    forbidden from touching the ORM:

    * ``resolved_scale`` -- the integer scale for ``scale_mode`` ``currency``
      (``res.currency.decimal_places``) or ``uom`` (the UoM decimal precision).
      Lane C never queries Odoo, so an unresolved scale is a contract error.
    * ``sheet_timezone`` -- the IANA name from ``gdrive.dataset``.  A
      ``datetime`` column with no declared timezone is a *validation* error,
      never a runtime guess.
    * ``odoo_field_type`` -- so ``False`` can be interpreted as "empty Char"
      versus "boolean false" without inspecting the value.

    ``key`` is the JCS key this column contributes to the row payload:
    ``odoo_field`` when mapped, else ``slug``.  Keying by ``odoo_field`` rather
    than by the sheet header is what makes the row hash invariant to column
    reordering *and* to cosmetic header renames at the same time.
    """

    # --- identity -------------------------------------------------------
    key: str = ""
    header_canon: str = ""
    odoo_field: str = ""
    slug: str = ""
    ctype: str = CTYPE_TEXT
    odoo_field_type: str = ""
    sequence: int = 10
    col_index: int = 0

    # --- semantics ------------------------------------------------------
    required: bool = False
    is_natural_key: bool = False
    authority: str = "sheet"
    empty_is_null: bool = True

    # --- text -----------------------------------------------------------
    text_trim: bool = True
    text_collapse_ws: bool = True
    text_case: str = "preserve"
    fold_punct: bool = False

    # --- numbers --------------------------------------------------------
    decimal_sep: str = "."
    group_sep: str = ","
    accounting_negatives: bool = True
    percent_mode: str = "none"
    scale_mode: str = "fixed"
    scale: int = 2
    resolved_scale: int | None = None
    currency_code: str = ""
    currency_symbols: tuple[str, ...] = ()
    rel_tol: float = 0.0
    abs_tol: float = 0.0

    # --- dates ----------------------------------------------------------
    date_formats: tuple[str, ...] = DEFAULT_DATE_FORMATS
    datetime_formats: tuple[str, ...] = ()
    sheet_timezone: str = ""

    # --- booleans -------------------------------------------------------
    truthy: tuple[str, ...] = DEFAULT_TRUTHY
    falsy: tuple[str, ...] = DEFAULT_FALSY
    empty_means: str = "false"

    # --- enumerations and relations -------------------------------------
    value_map: Mapping[str, str] = field(default_factory=dict)
    comodel: str = ""
    m2o_match_field: str = "name"
    m2o_create_missing: bool = False
    m2o_compare_by: str = "key"

    # --- identifier discipline ------------------------------------------
    assert_string_value: bool = False
    detect_error_literals: bool = True

    # -- derived helpers -------------------------------------------------

    @property
    def hash_key(self) -> str:
        """The JCS key this column contributes, with the documented fallback."""
        return self.key or self.odoo_field or self.slug

    @property
    def is_numeric(self) -> bool:
        """True for the two numeric families, which share ``NUM_CANON``."""
        return self.ctype in (CTYPE_NUMBER, CTYPE_MONEY)

    @property
    def effective_scale(self) -> int:
        """Resolve the scale from the *domain*, never from the data.

        ``fixed`` uses the declared ``scale``; ``currency`` and ``uom`` use the
        integer lane E resolved from ``res.currency.decimal_places`` / the UoM
        decimal precision.

        Raises:
            ValueError: when a currency/uom column reaches lane C with no
                resolved scale.  Guessing here would silently quantize money to
                two places in a three-place currency, and every ``verified``
                after that would be a lie.
        """
        if self.scale_mode == "fixed":
            return int(self.scale)
        if self.resolved_scale is None:
            raise ValueError(
                "Column %r declares scale_mode=%r but no resolved_scale; lane E "
                "must resolve the domain precision before calling lane C."
                % (self.hash_key or self.header_canon, self.scale_mode)
            )
        return int(self.resolved_scale)

    def with_options(self, **overrides: Any) -> "ColumnContract":
        """Return a copy with ``overrides`` applied (the dataclass is frozen)."""
        return replace(self, **overrides)


def default_text_contract(key: str, *, header_canon: str = "", slug: str = "") -> ColumnContract:
    """The contract used for a column nobody has mapped.

    Every tab is staged whether or not a promotion mapping exists (SPEC §5.4),
    and an unmapped dataset still needs a *stable* hash so that verification
    can say something useful about it.  Treating every unmapped column as
    ``text`` with ``empty_is_null=True`` is the only choice that cannot be
    wrong: it never coerces, never rounds, never guesses a separator.
    """
    return ColumnContract(
        key=key,
        header_canon=header_canon,
        slug=slug or key,
        ctype=CTYPE_TEXT,
        empty_is_null=True,
    )


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def validate_contract(col: ColumnContract) -> None:
    """Raise ``ValueError`` if ``col`` is internally inconsistent.

    This is a *contract* check, not a data check: everything here is something
    lane E got wrong while serializing a mapping, and every one of them would
    otherwise surface as a plausible-looking but meaningless hash.  Per
    CANONICALIZATION §12 invariant 2, contract errors raise; data errors return
    an ``e:`` token.
    """
    if col.ctype not in CTYPES:
        raise ValueError("Unknown ctype %r (expected one of %r)" % (col.ctype, CTYPES))
    if col.ctype == CTYPE_IGNORE:
        return  # excluded from the payload entirely; nothing else matters

    if not col.hash_key:
        raise ValueError(
            "Column with header_canon=%r has no key: a contract column must "
            "carry odoo_field or slug, because that is the JCS key it hashes "
            "under." % (col.header_canon,)
        )
    if not SLUG_RE.match(col.hash_key) and not re.match(
        r"^[A-Za-z_][A-Za-z0-9_]*$", col.hash_key
    ):
        raise ValueError(
            "Column key %r is not a JCS identifier; slugify() it first."
            % (col.hash_key,)
        )
    if col.text_case not in _TEXT_CASES:
        raise ValueError("text_case must be one of %r, got %r" % (_TEXT_CASES, col.text_case))
    if col.authority not in _AUTHORITIES:
        raise ValueError("authority must be one of %r, got %r" % (_AUTHORITIES, col.authority))

    if col.is_numeric:
        if col.percent_mode not in _PERCENT_MODES:
            raise ValueError(
                "percent_mode must be one of %r, got %r" % (_PERCENT_MODES, col.percent_mode)
            )
        if col.scale_mode not in _SCALE_MODES:
            raise ValueError(
                "scale_mode must be one of %r, got %r" % (_SCALE_MODES, col.scale_mode)
            )
        if len(col.decimal_sep) != 1:
            raise ValueError(
                "decimal_sep must be exactly one character (declared, never "
                "guessed), got %r" % (col.decimal_sep,)
            )
        if len(col.group_sep) > 1:
            raise ValueError("group_sep must be at most one character, got %r" % (col.group_sep,))
        if col.group_sep and col.group_sep == col.decimal_sep:
            raise ValueError(
                "group_sep and decimal_sep are both %r; the column is "
                "unparseable as declared." % (col.decimal_sep,)
            )
        # Forces the currency/uom resolution error early, at validation time,
        # rather than in the middle of staging 40 000 rows.
        col.effective_scale

    if col.ctype == CTYPE_BOOL:
        if col.empty_means not in _EMPTY_MEANS:
            raise ValueError(
                "empty_means must be one of %r, got %r" % (_EMPTY_MEANS, col.empty_means)
            )
        if not col.truthy or not col.falsy:
            raise ValueError("A bool column must declare both truthy and falsy tokens.")
        overlap = {t.casefold() for t in col.truthy} & {f.casefold() for f in col.falsy}
        if overlap:
            raise ValueError(
                "Tokens %r appear in both truthy and falsy for column %r."
                % (sorted(overlap), col.hash_key)
            )

    if col.ctype in (CTYPE_DATE, CTYPE_DATETIME) and not col.date_formats and not col.datetime_formats:
        raise ValueError(
            "Column %r declares no date_formats; fuzzy parsing is forbidden, so "
            "there would be no way to read a string date." % (col.hash_key,)
        )

    if col.ctype == CTYPE_DATETIME and not col.sheet_timezone:
        raise ValueError(
            "Column %r is a datetime but the dataset declares no sheet_timezone. "
            "A spreadsheet has no timezone; guessing one silently shifts every "
            "value by hours." % (col.hash_key,)
        )

    if col.ctype == CTYPE_SELECTION and not col.value_map:
        raise ValueError(
            "Selection column %r has an empty value_map; every sheet label "
            "would resolve to e:UNRESOLVED_SELECTION." % (col.hash_key,)
        )

    if col.ctype in (CTYPE_M2O, CTYPE_M2M):
        if col.m2o_compare_by not in _M2O_COMPARE_BY:
            raise ValueError(
                "m2o_compare_by must be one of %r, got %r"
                % (_M2O_COMPARE_BY, col.m2o_compare_by)
            )
        if col.ctype == CTYPE_M2O and col.m2o_compare_by == "key" and not col.m2o_match_field:
            raise ValueError(
                "Column %r compares many2one values by business key but declares "
                "no m2o_match_field." % (col.hash_key,)
            )


def validate_contracts(columns: Sequence[ColumnContract]) -> None:
    """Validate a whole contract, including cross-column uniqueness.

    Duplicate hash keys are fatal: two columns writing the same JCS key means
    one of them silently disappears from every row hash, and the resulting
    digest is stable, plausible, and wrong.
    """
    seen: dict[str, ColumnContract] = {}
    for col in columns:
        validate_contract(col)
        if col.ctype == CTYPE_IGNORE:
            continue
        key = col.hash_key
        if key in seen:
            raise ValueError(
                "Duplicate contract key %r (headers %r and %r): one column would "
                "silently vanish from every row hash."
                % (key, seen[key].header_canon, col.header_canon)
            )
        seen[key] = col


# --------------------------------------------------------------------------
# Construction from lane E's serialized mapping dicts
# --------------------------------------------------------------------------

_FIELD_NAMES: Final = tuple(f.name for f in fields(ColumnContract))
_TUPLE_FIELDS: Final = frozenset(
    {"currency_symbols", "date_formats", "datetime_formats", "truthy", "falsy"}
)
#: Fields whose Odoo counterpart is a comma-separated Char (SPEC §3.9).
_CSV_FIELDS: Final = frozenset({"date_formats", "datetime_formats", "truthy", "falsy", "currency_symbols"})


def _as_tuple(value: Any) -> tuple[str, ...]:
    """Normalize a comma-separated Char or a list into a tuple of strings.

    Empty entries are dropped but interior whitespace is preserved *except* at
    the edges, because ``truthy = "true, yes"`` is what an administrator will
    actually type and the space is not part of the token.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        parts = value.split(",")
    else:
        parts = list(value)
    return tuple(str(p).strip() for p in parts if str(p).strip() != "")


def contract_from_mapping_dict(data: Mapping[str, Any]) -> ColumnContract:
    """Build a ``ColumnContract`` from lane E's ``to_contract_dict()`` output.

    Unknown keys are ignored rather than fatal, so lane E can add diagnostic
    fields to the serialized dict without breaking lane C.  Known keys are
    coerced to the declared type -- in particular the comma-separated Char
    fields (``date_formats``, ``truthy``, ``falsy``) become tuples here, in one
    place, instead of being re-split at every call site.
    """
    kwargs: dict[str, Any] = {}
    for name in _FIELD_NAMES:
        if name not in data:
            continue
        value = data[name]
        if name in _CSV_FIELDS:
            kwargs[name] = _as_tuple(value)
        elif name in _TUPLE_FIELDS:
            kwargs[name] = tuple(value or ())
        elif name == "value_map":
            kwargs[name] = dict(value or {})
        elif name == "resolved_scale":
            kwargs[name] = None if value is None else int(value)
        elif value is None:
            continue  # a Json round trip turns "unset" into null; keep the default
        else:
            kwargs[name] = value

    col = ColumnContract(**kwargs)
    if not col.key:
        col = col.with_options(key=col.odoo_field or col.slug)
    return col


def contracts_from_mapping_dicts(rows: Iterable[Mapping[str, Any]]) -> tuple[ColumnContract, ...]:
    """Build and validate a whole contract from lane E's serialized rows."""
    cols = tuple(contract_from_mapping_dict(r) for r in rows)
    validate_contracts(cols)
    return cols


def contract_to_dict(col: ColumnContract) -> dict[str, Any]:
    """Round-trip a contract back to a plain JSON-safe dict."""
    out: dict[str, Any] = {}
    for name in _FIELD_NAMES:
        value = getattr(col, name)
        if isinstance(value, tuple):
            out[name] = list(value)
        elif isinstance(value, Mapping):
            out[name] = dict(value)
        else:
            out[name] = value
    return out


# --------------------------------------------------------------------------
# spec_version
# --------------------------------------------------------------------------

#: Contract fields that do **not** affect canonical output and are therefore
#: excluded from ``spec_version``.
#:
#: **Only ``col_index`` qualifies, and only because ``header_canon`` is in.**  A
#: pure physical reorder (the user drags column D to position B) changes every
#: ``col_index`` and changes no data, so hashing it would invalidate every cached
#: hash in the database for a no-op -- that is what this exclusion buys.
#:
#: ``header_canon`` was previously excluded here as "cosmetic/UI-only".  It is
#: not: it is the join key that binds a contract entry to a physical column
#: (``gdrive_dataset_column`` matches on it), so re-binding ``partner_vat`` from
#: the header ``VAT`` to the header ``Tax ID`` -- which makes every row read a
#: different column -- left ``spec_version`` byte-identical, and every stored
#: ``h_row`` computed from the old column stayed a valid cache hit.  A dataset
#: would then be reported ``verified`` against digests of a column that is no
#: longer being read.  Re-binding must invalidate; it now does.
_SPEC_IRRELEVANT: Final = frozenset({"col_index"})


def serialize_contracts(columns: Sequence[ColumnContract]) -> dict[str, str]:
    """Flatten a contract into the ``{identifier: str}`` shape ``jcs`` accepts.

    Columns are emitted in ``hash_key`` order, not in list order: reordering
    the columns of a mapping is not a behavioural change and must not
    invalidate cached hashes, whereas *renaming* a key or changing any option
    must.
    """
    payload: dict[str, str] = {}
    ordered = sorted(
        (c for c in columns if c.ctype != CTYPE_IGNORE), key=lambda c: c.hash_key
    )
    for position, col in enumerate(ordered):
        for name in _FIELD_NAMES:
            if name in _SPEC_IRRELEVANT:
                continue
            payload.update(_flatten_option("c%d_%s" % (position, name), getattr(col, name)))
    payload["n_columns"] = str(len(ordered))
    return payload


def _flatten_option(prefix: str, value: Any) -> dict[str, str]:
    """Flatten one contract option into one-or-more flat ``{identifier: str}`` entries.

    **Every mapping entry and every sequence element gets its own JCS key**,
    plus an explicit ``__len``, exactly as ``spec_version.flatten_for_hash``
    already does for nested structures.

    WHY not the obvious ``",".join("%s=%s" % ...)``:  that is the delimiter
    injection ``jcs`` exists to prevent, and it reintroduced a
    ``spec_version`` collision between two *behaviourally different* contracts.
    ``{"a": "b,c=d"}`` and ``{"a": "b", "c": "d"}`` both stringified to
    ``"{a=b,c=d}"``; so did ``("%Y-%m-%d,%m/%d/%Y",)`` and
    ``("%Y-%m-%d", "%m/%d/%Y")``, and ``("yes,no",)`` and ``("yes", "no")``.
    Editing a selection column's ``value_map`` -- or a bool column's ``truthy``,
    which flips every cell containing ``no`` -- between two such spellings left
    ``spec_version`` byte-identical, so every hash computed under the old rules
    remained a valid cache hit under the new ones: a green dashboard over data
    that was never compared under the current contract.

    Values are carried as JCS *values*, which ``jcs.escape_json_string``
    escapes, so no amount of punctuation in an administrator-typed option can
    forge a key boundary.
    """
    if isinstance(value, Mapping):
        out = {"%s__len" % prefix: str(len(value))}
        for index, key in enumerate(sorted(value, key=str)):
            out["%s__k%d" % (prefix, index)] = _stringify_option(key)
            out.update(_flatten_option("%s__v%d" % (prefix, index), value[key]))
        return out
    if isinstance(value, (tuple, list)):
        out = {"%s__len" % prefix: str(len(value))}
        for index, item in enumerate(value):
            out.update(_flatten_option("%s__%d" % (prefix, index), item))
        return out
    return {prefix: _stringify_option(value)}


def _stringify_option(value: Any) -> str:
    """Deterministically stringify one **scalar** contract option.

    Containers never reach here -- ``_flatten_option`` decomposes them into
    separate keys first, which is what makes the encoding unambiguous.  ``bool``
    is handled before ``int`` because ``bool`` is an ``int`` subclass and
    ``str(True)`` must not become ``"1"``.
    """
    if value is None:
        return NULL_TOKEN
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        return repr(value)
    return str(value)


def spec_version_for_contracts(columns: Sequence[ColumnContract]) -> str:
    """Hex ``spec_version`` for a whole column contract.

    A stored hash whose ``spec_version`` differs from the current one MUST be
    treated as absent, never as a cache hit.  Serving a stale hash computed by
    an older normalizer as ``verified`` is a silent false pass -- the single
    worst failure mode of a verification system -- and this value is the only
    structural defence against it.
    """
    return compute_spec_version(serialize_contracts(columns))


# --------------------------------------------------------------------------
# slugify
# --------------------------------------------------------------------------

_COMBINING: Final = "Mn"


def slugify(header_canon: str) -> str:
    """Turn a canonical header into a JCS-safe payload key.

    Guarantees the result matches ``^[a-z_][a-z0-9_]*$``:

    1. drop the ``s:`` tag if present,
    2. NFKD-decompose and drop combining marks, so ``Café`` becomes ``cafe``
       (this is the *one* place NFKD is acceptable -- a slug is an internal
       identifier, not a compared value, so information loss is fine here and
       fatal in ``TEXT_CANON``),
    3. casefold,
    4. replace every run of non ``[a-z0-9]`` with a single ``_``,
    5. trim ``_`` from both ends,
    6. prefix ``_`` when the result is empty or starts with a digit.

    Deterministic and pure: the same header always produces the same slug, on
    any machine, which matters because the slug is a persisted payload key.
    """
    s = header_canon
    if s.startswith("s:"):
        s = s[2:]
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != _COMBINING)
    s = s.casefold()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = s.strip("_")
    if not s or s[0].isdigit():
        s = "_" + s
    return s


def slugify_all(header_canons: Sequence[str]) -> list[str]:
    """Slugify a header row, deduplicating with ``_2``, ``_3``, ...

    Deduplication is **positional and deterministic**: the first occurrence
    keeps the bare slug and later collisions take the next free suffix.  Two
    columns literally named ``Amount`` and ``amount`` collapse to the same slug
    otherwise, and one of them would silently overwrite the other in
    ``payload``.
    """
    used: dict[str, int] = {}
    out: list[str] = []
    for header in header_canons:
        base = slugify(header)
        if base not in used:
            used[base] = 1
            out.append(base)
            continue
        n = used[base] + 1
        candidate = "%s_%d" % (base, n)
        while candidate in used:
            n += 1
            candidate = "%s_%d" % (base, n)
        used[base] = n
        used[candidate] = 1
        out.append(candidate)
    return out
