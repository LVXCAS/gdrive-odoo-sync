# `gdrive_odoo_sync` — Canonicalization & Hashing Specification

**Applies to:** `gdrive_odoo_sync/lib/*` (lane C, the implementer) and `gdrive_odoo_sync/models/gdrive_reconciler.py` + `gdrive_verification.py` (lane E, the consumer).
**Status:** normative. Every MUST here is a test case in `tests/test_lib_*.py`.
**Companion:** `docs/SPEC.md` §3.9 defines the `ColumnContract` options referenced throughout; `docs/FILE_MANIFEST.md` assigns the files.

Lane C is **stdlib-only**: `hashlib`, `decimal`, `unicodedata`, `datetime`, `zoneinfo`, `json`, `re`, `struct`, `secrets`, `time`. No `odoo`, no third-party imports, ever. Every function in this document is **pure**: same inputs → same bytes, on any machine, in any locale, in any timezone, forever.

---

## 1. Versioning and cache invalidation

```python
CANON_VERSION = "gos-canon-2"          # bump on ANY behavioural change in lane C
```

```python
def compute_spec_version(contract: dict) -> str:
    """Hex sha256 of the serialized contract plus the normalizer version."""
    return sha256(b"gos1/spec\x00" + jcs(contract) + CANON_VERSION.encode()).hexdigest()
```

`spec_version` is stored on `gdrive.dataset` and `gdrive.mapping`, and is part of **every** hash preimage below. Consequences, all mandatory:

- Changing any column option, adding/removing a mapped column, or bumping `CANON_VERSION` changes `spec_version`.
- A stored hash whose `spec_version` differs from the current one **MUST be treated as absent**, never as a cache hit.
- Serving a stale hash computed by an older normalizer as `verified` is a silent **false pass**. That is the single worst failure mode of a verification system, and `spec_version` is the only structural defence against it.
- A full recompute is additionally forced weekly and on every module upgrade, because the fast path is an optimization built on assumptions and the periodic full pass is what catches the day one of them is wrong.

---

## 2. The tagged-token vocabulary

**The canonical form of a cell is a tagged string, never a bare string.** The tag is part of the hash preimage.

| Token | Type family | Payload grammar |
|---|---|---|
| `z:` | NULL / empty | (nothing follows the colon) |
| `s:` | text | arbitrary UTF-8, NFC-normalized |
| `n:` | number / money | `-?[0-9]+\.[0-9]{scale}` (fixed point; `\.` and the fraction are omitted only when `scale == 0`) |
| `b:0` / `b:1` | boolean | exactly these two strings |
| `d:` | date | `YYYY-MM-DD` |
| `t:` | datetime | `YYYY-MM-DDTHH:MM:SSZ` — always UTC, always `Z`, always second precision |
| `r:` | relational reference | `<natural key>` or `<id>`; for m2m, `[1,5,9]` (ascending ids, no spaces) |
| `k:` | selection | the Odoo **technical** key, e.g. `k:draft` |
| `e:` | error / uncomparable | one of the error codes in §2.1 |

**Why tags exist.** Without them, a column whose declared type silently changes between runs produces equal hashes for unequal data. A text `"1"`, a numeric `1`, and a boolean `true` must never collide. The golden vectors in §11 prove they do not.

### 2.1 Error codes (`e:` family)

`NOT_A_NUMBER`, `NOT_FINITE`, `BAD_DATE`, `BAD_BOOL`, `CELL_ERROR`, `IDENTIFIER_NUMERIC`, `UNRESOLVED_SELECTION`, `ORPHAN_REFERENCE`, `NONEXISTENT_LOCAL_TIME`, `TIME_COMPONENT_PRESENT`, `MULTI_MATCH`, `CURRENCY_MISMATCH`.

**Equality rule (mandatory):** an `e:` token is **never equal to anything, including an identical `e:` token**. `equal(a, b)` MUST return False if either side starts with `e:`. Errors do not compare; they quarantine.

**Propagation rule:** any `e:` token in a row quarantines the **whole row** (`gdrive.staged.row.state='quarantined'`). Never write a partially valid row — half-written rows are worse than unwritten ones. Quarantined rows are counted in `data_quality_count`, never in `drift_count`, so "12 drifts" can never silently mean "12 cells I could not read".

**Sheets `errorValue`** (`#N/A`, `#REF!`, `#DIV/0!`, `#VALUE!`, `#NAME?`, `#NUM!`, `#NULL!`) maps to `e:CELL_ERROR`. It MUST NOT map to `z:`.

---

## 3. `CANON` — the dispatcher

```python
def CANON(raw, col: ColumnContract, side: str) -> str:
    """side is 'sheet' or 'odoo'. Returns exactly one tagged token."""
```

**There is no type sniffing anywhere in this library.** The type comes from `col.ctype`, always. Two different columns may legitimately canonicalize the raw value `"1"` differently — `s:1`, `n:1.00`, `b:1` — which is precisely why per-cell inference is wrong and is forbidden.

Dispatch table:

| `col.ctype` | Function |
|---|---|
| `text` | `TEXT_CANON` (§4) |
| `number`, `money` | `NUM_CANON` (§5) |
| `bool` | `BOOL_CANON` (§7) |
| `date` | `DATE_CANON` (§6.2) |
| `datetime` | `DATETIME_CANON` (§6.3) |
| `selection` | `SELECTION_CANON` (§8.1) |
| `many2one` | `M2O_CANON` (§8.2) |
| `m2m` | `M2M_CANON` (§8.3) |
| `ignore` | excluded from the contract payload entirely |

### 3.1 Pre-dispatch guards, applied in this order

1. **Sheets `errorValue`** (side `sheet`, from `read_effective_values`) → `e:CELL_ERROR`. Stop.
2. **`col.assert_string_value` violated** — the `effectiveValue` oneof branch is `numberValue` for a column declared as an identifier → `e:IDENTIFIER_NUMERIC`. Stop. Do not attempt to recover: leading zeros are already gone (`"007"` → `7`) and anything past ~15 significant digits is already mangled (`12345678901234567890` → `1.2345678901234567e19`). Set `assert_string_value = True` on every SKU, barcode, invoice number, phone number, account number, and postal code column.
3. **Absent cell** (ragged row, index past the row's length after padding, or side `odoo` returning `False` on a Char/Text/Html field) → `z:` if `col.empty_is_null` else the type's declared empty.
4. Otherwise dispatch.

---

## 4. `TEXT_CANON` — exact algorithm

Ten ordered steps. **The order is load-bearing.**

```python
def TEXT_CANON(v, col) -> str:
```

1. If `v is None`, or (`side == 'odoo'` and `v is False` and the Odoo field type is Char/Text/Html), or the cell is absent → return `"z:"` (subject to step 9's `empty_is_null`).
2. `s = str(v)`. If `v` is a `Decimal`/`int`/`float` reaching a text column, use `format(v, 'f')` for Decimal and `repr` semantics for float — but note this is a contract smell and lane E logs `TEXT_COLUMN_RECEIVED_NUMBER` at `warning`.
3. **Strip invisible/format characters.** Remove: `U+FEFF` (BOM), `U+200B`–`U+200D` (ZWSP/ZWNJ/ZWJ), `U+2060` (word joiner), `U+00AD` (soft hyphen), and **every** code point in Unicode general category `Cf`. Also remove C0 controls (`U+0000`–`U+001F`) **except** `\t` and `\n`, and remove `U+007F`.
4. **Unify whitespace.** Map every code point in category `Zs` (`U+00A0` NBSP, `U+1680`, `U+2000`–`U+200A`, `U+202F`, `U+205F`, `U+3000`) and `U+0009` to `U+0020`. Normalize `\r\n` → `\n` and bare `\r` → `\n`.
5. **`unicodedata.normalize('NFC', s)`.** This MUST come after steps 3–4, so decomposed sequences separated by an intervening invisible character compose correctly.
   **NFC, never NFKC.** NFKC folds `㎡` → `m2`, `①` → `1`, and full-width → half-width. That is real information loss, and it silently masks genuine differences.
6. **Trim** leading and trailing `U+0020` and `\n`.
7. If `col.text_collapse_ws` (default **True**): replace every run of `U+0020` with a single `U+0020`, **per line** (do not collapse across `\n`). Set this **False** for description/notes/code/address-block columns; leave True for names and labels.
8. If `col.text_case == 'fold'`: `s = s.casefold()`. **`casefold()`, not `lower()`** — casefold handles `ß`→`ss`, Turkish dotless `ı`, and Greek final sigma. Default is `preserve`; use `fold` for emails, country codes, and SKUs.
9. If `s == ""` and `col.empty_is_null` (default True) → return `"z:"`. This makes `""`, `"   "`, an NBSP-only cell, and a missing cell all collapse to the same token.
10. Return `"s:" + s`.

### 4.1 Smart punctuation — a deliberate design decision

`TEXT_CANON` does **NOT** fold `’ ‘ “ ” – — … ` into ASCII in the primary canonical form. Folding them would hide real edits, and the write-back would then never converge.

Instead, `fold_punct(s)` is applied only when computing the **second**, cosmetic hash (§9.3). The mapping is exactly:

| From | To |
|---|---|
| `U+2018` `U+2019` `U+201A` `U+201B` `U+2032` | `'` |
| `U+201C` `U+201D` `U+201E` `U+201F` `U+2033` | `"` |
| `U+2010`–`U+2015` `U+2212` | `-` |
| `U+2026` | `...` |
| `U+00A0` | already handled in step 4 |

Additionally the cosmetic variant applies `casefold()` and `collapse_ws` unconditionally, regardless of the column options.

**Classification rule:** strict hashes differ **and** folded hashes match ⇒ `COSMETIC_DRIFT`. Reported. Not auto-written by default.

Real-world relevance here: the dataset titles `Food CPG Master — Investor Directory (79)` and `RE Portfolio — MASTER (4M / Michael)` contain `U+2014` EM DASH. Any cell in those sheets typed on a Mac will carry smart quotes. Cosmetic classification is what stops that from generating permanent noise.

---

## 5. `NUM_CANON` — exact algorithm

```python
def NUM_CANON(v, col) -> str:
```

1. **Float input** (Sheets `UNFORMATTED_VALUE`, Odoo JSON-RPC, `openpyxl`):
   `d = Decimal(repr(v))`.
   **`Decimal(repr(v))`, never `Decimal(v)`.** `Decimal(0.1)` is `0.1000000000000000055511151231257827021181583404541015625`, which defeats the entire point of moving to `Decimal`. `repr` gives the shortest round-tripping literal, `"0.1"`.
   Integer input: `d = Decimal(v)`.
   `Decimal` input: use as-is.
2. **String input:** apply `TEXT_CANON` steps 1–6 first (NBSP thousands separators are extremely common in exported sheets), then, in order:
   a. Strip a leading currency symbol or ISO code (`$ € £ ¥ USD EUR GBP` and `col`-declared extras) and surrounding spaces.
   b. Strip a trailing `%`; if present and `col.percent_mode == 'divide_100'`, divide by 100 after parsing.
   c. Accounting negatives (when `col.accounting_negatives`, default True): `(1,234.50)` → `-1234.50`; trailing minus `1234-` → `-1234`; strip a leading `+`.
   d. Remove every occurrence of `col.group_sep`. Replace `col.decimal_sep` with `.`.
   e. `d = Decimal(cleaned)`; on `InvalidOperation` → return `"e:NOT_A_NUMBER"`.
3. If `d.is_nan()` or `d.is_infinite()` → `"e:NOT_FINITE"`.
4. **Resolve the scale from the domain, never from the data:**

   | `col.scale_mode` | scale |
   |---|---|
   | `currency` | `res.currency.decimal_places` of the resolved currency (equivalently `-log10(rounding)`), passed in by lane E |
   | `uom` | `decimal.precision` for `Product Unit of Measure` (typically 3), passed in by lane E |
   | `fixed` | `col.scale` |

   Lane C never queries Odoo; lane E resolves the integer and puts it on the contract dict before calling.
5. `q = d.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP)`.
   **`ROUND_HALF_UP` (away from zero), never `ROUND_HALF_EVEN`.** Odoo's `float_round` default is half-up; Python's `Decimal` default is banker's rounding. Using the default guarantees disagreement with Odoo on every `.5` case.
6. `if q == 0: q = abs(q)` — collapses `Decimal('-0.00')` to `Decimal('0.00')`.
7. `s = format(q, 'f')` — fixed point, **never** scientific notation, with exactly `scale` fraction digits (`quantize` guarantees this). Keep a leading `-` for negatives; never emit `+`.
8. Return `"n:" + s`.

### 5.1 The tolerance policy

**Tolerance is baked into the hash by quantizing inside the canonicalizer.** That is the *only* way hashing and tolerance are compatible at all: hashing is exact by nature, so tolerance must be expressed in the canonical form, and the hash inherits it.

**There is no global epsilon, ever.** A single `EPS` constant is guaranteed to be simultaneously too tight for computed margins and too loose for cents. Tolerance is a per-column business property.

| Column class | `ctype` / `scale_mode` | Comparison |
|---|---|---|
| **Money** | `money` / `currency` | Quantize both sides to `currency.decimal_places`, `ROUND_HALF_UP`, compare canonical strings exactly. Lane E cross-checks with `odoo.tools.float_compare(a, b, precision_rounding=currency.rounding) == 0`. |
| **Quantities / UoM** | `number` / `uom` | Same, at the UoM precision (typically 3). |
| **Derived floats** (percentages, weights, computed margins) | `number` / `fixed` with `rel_tol`/`abs_tol` set | Hash a value quantized to the declared display scale so the hash stays usable; at the **L3 drill-down only**, apply `math.isclose(a, b, rel_tol=col.rel_tol, abs_tol=col.abs_tol)` (defaults `1e-9` / `1e-12` when the column sets them non-zero). |
| **Identifiers that look numeric** | `text` with `assert_string_value=True` | Never numeric. Full stop. |

**Never compare floats with `==`.** Sheets stores every number as an IEEE-754 double and returns doubles over the API; Odoo `Float` columns are `double precision` in Postgres unless `digits=` is set, and JSON-RPC hands you Python floats. `12.30` is not representable; two paths to "twelve thirty" land on `12.299999999999999` and `12.300000000000001`. The user then sees a tool claiming two cells that both read `12.30` differ — which destroys trust in the entire product on the first day.

**`ROUNDING_DRIFT` classification (L3 only, downgrade-only).** A numeric field mismatch where the pre-quantization `abs(a - b) < 0.51 * step` (with `step = Decimal(1).scaleb(-scale)`) is classified `rounding`: **reported, not auto-written**. Writing it makes the value flap between runs. Tolerance at L3 may only *downgrade* severity (`substantive` → `rounding`); it may never *upgrade* an inequality into equality.

**Boundary warning.** `near_boundary(d, step)` returns True when `abs(d - nearest_half_step) < 1e-9 * step`. Lane E logs `ROUNDING_BOUNDARY` at `warning` for those cells. They are the values that flip classification between runs and generate the mystery "drift appeared and vanished" tickets.

### 5.2 Separators are declared, never detected

`"1.234"` is `1234` in de-DE and `1.234` in en-US, and **no heuristic can tell**. `col.decimal_sep` and `col.group_sep` are required contract fields.

If a future mode ever guesses (it is not implemented in v1): if both `,` and `.` appear, the **rightmost** is the decimal separator; if only one appears and it is followed by exactly three digits with no other separator, treat it as a group separator; and emit `GUESSED_SEPARATOR` at `warning` **every single time**.

### 5.3 The independent raw-total control

`raw_decimal(v)` returns `Decimal(repr(v))` (float) or `Decimal(str(v).strip())` (string, minimal cleaning only, no quantization), or `None` if unparseable. Lane E sums these **per numeric column, on both sides**, and stores them in `gdrive.verification.column_totals`.

This is deliberately **not** derived from the canonical form. If both sides are canonicalized wrongly in the same way, the hashes agree *and* canonical totals agree — and only a raw total disagrees. It is the one control that catches a symmetric normalizer bug.

---

## 6. Dates and datetimes

### 6.1 Serial → naive datetime

Google Sheets returns dates as Lotus-style serials under `dateTimeRenderOption='SERIAL_NUMBER'`: days since **1899-12-30**, fractional part = fraction of day.

```python
BASE = date(1899, 12, 30)

def serial_to_naive(serial) -> datetime:
    d = Decimal(repr(serial))
    days = int(d)                        # truncation toward zero; serials are non-negative in practice
    frac = d - days
    seconds = int((frac * 86400).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if seconds == 86400:                 # 23:59:59.6 rounds up to a whole day
        days += 1
        seconds = 0
    return datetime.combine(BASE + timedelta(days=days), time()) + timedelta(seconds=seconds)
```

`round`, not truncate: `45000.5` arrives over the wire as `45000.499999999996`, and truncating gives `11:59:59` instead of `12:00:00`.

Microseconds are **not** representable: one microsecond is `1.157e-11` of a day, below double precision at serial magnitudes around 45 000. Second precision is the contract.

### 6.2 `DATE_CANON` — dates never see a timezone

```python
def DATE_CANON(v, col) -> str:
```

1. `v is None` / absent / Odoo `False` → `"z:"`.
2. Odoo side: `v` is a `datetime.date`. → `"d:" + v.isoformat()`. **Stop. Do not convert.**
3. Sheet side, numeric `v`: `naive = serial_to_naive(v)`. If the time component is non-zero, emit `TIME_COMPONENT_PRESENT` at `warning` and **drop it**. → `"d:" + naive.date().isoformat()`.
4. Sheet side, string `v`: apply `TEXT_CANON` steps 1–6, then try `datetime.strptime(s, fmt).date()` for each `fmt` in `col.date_formats`, **in declared order**, strictly. First success wins. No match → `"e:BAD_DATE"`.
5. Return `"d:YYYY-MM-DD"`.

**Never timezone-convert a date, on either side.** Pushing a pure date through a timezone is the classic off-by-one-day bug: it produces a full-column false drift, and characteristically only at night, when the cron runs and UTC has already rolled over.

**Never use `dateutil`, `pandas` inference, or any fuzzy parser.** `"03/04/2026"` is genuinely unresolvable; a fuzzy parser picks one silently and corrupts a year of data. Only strict `strptime` against the declared list.

### 6.3 `DATETIME_CANON`

```python
def DATETIME_CANON(v, col, sheet_timezone: str) -> str:
```

1. Absent / None / False → `"z:"`.
2. **Odoo side:** `fields.Datetime` is stored and returned **UTC-naive**. Treat it as UTC directly. → `"t:" + v.strftime("%Y-%m-%dT%H:%M:%SZ")`. **Never apply the user's timezone to it.**
3. **Sheet side:** a spreadsheet has no timezone, so `sheet_timezone` (an IANA name; `gdrive.dataset.sheet_timezone`, default `America/New_York`) is **required**. A `datetime` column on a dataset with no declared timezone is a contract validation error, not a runtime guess.
   a. `naive = serial_to_naive(v)` (or strict `strptime` for strings, per §6.2 step 4, returning `e:BAD_DATE` on failure).
   b. `aware = naive.replace(tzinfo=ZoneInfo(sheet_timezone), fold=0)`.
      - **Ambiguous local time** (the repeated hour at DST fall-back): `fold=0` selects the **first** (pre-transition, DST) occurrence. Deterministic by fiat. Emit `AMBIGUOUS_LOCAL_TIME` at `info`.
      - **Nonexistent local time** (the skipped hour at DST spring-forward): detect by round-tripping — `aware.astimezone(UTC).astimezone(ZoneInfo(tz)) != aware` — and return `"e:NONEXISTENT_LOCAL_TIME"`. Do **not** silently shift.
   c. `utc = aware.astimezone(timezone.utc)`.
4. Return `"t:" + utc.strftime("%Y-%m-%dT%H:%M:%SZ")`.

---

## 7. `BOOL_CANON`

```python
def BOOL_CANON(v, col) -> str:
```

1. `v is True` → `"b:1"`. `v is False` → `"b:0"`. (Sheets `boolValue`, Odoo `Boolean`. On the Odoo side, `False` is a **real value**, not NULL — branch on the *field type*, never on truthiness.)
2. `v is None` / absent → `col.empty_means`: `false` → `"b:0"`, `null` → `"z:"`, `error` → `"e:BAD_BOOL"`. Default `false`.
3. Otherwise: `t = TEXT_CANON(v, opts=text_trim+collapse_ws+case='fold')`. If `t == "z:"`, go to step 2.
4. Strip the `s:` tag. Membership test against `col.truthy` and `col.falsy`, both `casefold()`ed, both split on `,`.
   Defaults: `truthy = true,yes,y,1,x,✓` — `falsy = false,no,n,0`.
5. Anything else → `"e:BAD_BOOL"` and quarantine the row.

**Never default an unrecognized token to false.** Silently mapping `"maybe"`, `"pending"`, `"TBD"`, or `"?"` to `false` means the system reports `verified` on data it actively misread. That is worse than reporting drift.

---

## 8. Relational and enumerated types

### 8.1 `SELECTION_CANON`

Odoo stores **technical keys** (`'draft'`); the sheet almost always holds **labels** (`'Draft'`).

- **Odoo side:** `v` is already the technical key → `"k:" + v`. `False` → `"z:"`.
- **Sheet side:** `t = TEXT_CANON(v, col)`; strip `s:`; look up `col.value_map[label]`. Hit → `"k:" + key`. Miss → `"e:UNRESOLVED_SELECTION"`.
- Lookup is done on the `TEXT_CANON`'d label with `case='fold'` applied to both sides of the map, so `"draft"`, `"Draft"`, and `"DRAFT "` all resolve.

**Never compare against labels.** Lane E validates `set(col.value_map.values()) ⊆ set(fields_get(field)['selection'] keys)` at mapping-validation time, so a new Odoo state fails **loudly** at validation rather than drifting silently at run time.

### 8.2 `M2O_CANON`

Odoo returns `[id, display_name]` or `False`.

- `False` → `"z:"`.
- If the sheet holds **ids**: `"r:" + str(id)`.
- If the sheet holds **business keys** (the normal case): resolve the comodel record and emit `"r:" + TEXT_CANON(record[col.m2o_match_field]).lstrip('s:')`. On the sheet side, resolve `value → comodel record` via `col.m2o_match_field`; unresolvable → `"e:ORPHAN_REFERENCE"` (and `col.m2o_create_missing` defaults **False**, so nothing is invented).

**Never hash `display_name`.** It is rendered, translated, and format-dependent — it drifts when nothing changed.

### 8.3 `M2M_CANON` (and one2many)

Sort ids **ascending numerically**, then `"r:[" + ",".join(str(i) for i in ids) + "]"`. Empty → `"z:"`.

ORM iteration order is not guaranteed stable; sorting is what makes this hashable.

### 8.4 Monetary companion currency

A `money` column MUST resolve a currency (`col.currency_field_id` on the record, else `col.default_currency_id`). If the two sides resolve to **different** currencies, lane E emits a `currency_mismatch` drift at `critical` and the field is **never auto-written**. Comparing bare amounts across differing currencies is meaningless.

### 8.5 Binary / image fields

Hashed **separately** (sha256 of the raw bytes, stored on the drift record), never inlined into the row-hash preimage.

---

## 9. Row-level hashing

### 9.1 JCS — the canonical serialization

A restricted, deterministic subset of RFC 8785 JSON Canonicalization Scheme.

```python
def jcs(payload: dict[str, str]) -> bytes:
```

Rules:
1. Every **key** MUST match `^[A-Za-z_][A-Za-z0-9_]*$`. Lane C raises `ValueError` otherwise. This is a deliberate restriction: with an ASCII-identifier key charset, byte order and UTF-16 code-unit order are identical, so key sorting is unambiguous with zero UTF-16 machinery. Odoo field names already satisfy it; unmapped column slugs are forced into it by `contract.slugify()`.
2. Every **value** MUST be a `str` (a tagged token). Floats are forbidden in the preimage — that is the whole point of §5.
3. Keys are emitted in ascending byte order.
4. Output is `{"k1":"v1","k2":"v2"}` — no whitespace anywhere, UTF-8 encoded, `ensure_ascii=False` (non-ASCII characters are emitted literally as UTF-8).
5. String escaping is exactly: `"` → `\"`, `\` → `\\`, `U+0008` → `\b`, `U+0009` → `\t`, `U+000A` → `\n`, `U+000C` → `\f`, `U+000D` → `\r`, every other code point below `U+0020` → `\u00xx` with **lowercase** hex. Nothing else is escaped — not `/`, not non-ASCII.

**Why not an ad-hoc `k=v|k=v` join:** it is delimiter-injectable. A cell containing `|amount=0` can forge another field's value, and `{a:"1",b:""}` collides with a different record. Use JCS. (Sorted netstrings — `len(k):k,len(v):v,` per key — are the only acceptable alternative; a bare join never is.)

### 9.2 `h_row`

```python
ROW_PREFIX = b"gos1/row\x00"

def h_row(canon: dict[str, str], spec_version: str) -> bytes:   # 16 bytes
    preimage = ROW_PREFIX + spec_version.encode() + b"\x00" + jcs(canon)
    return sha256(preimage).digest()[:16]
```

- `canon` contains **exactly** the contract columns (`ctype != 'ignore'`), keyed by **`odoo_field`** when the column is mapped, else by the column **`slug`**.
- **Keying by `odoo_field`, not by the sheet header, is what makes the row hash invariant to column reordering *and* to cosmetic header renames simultaneously.**
- 128 bits gives a ~2⁶⁴ birthday bound — ample for any dataset this system will see.

**Hash choice: SHA-256, from `hashlib`. Never MD5, SHA-1, CRC32, or xxHash.** The entire product claim is "these two datasets are identical", so a collision is a false `verified` — the exact assertion the system exists to make. BLAKE3 would be preferable on speed but is a third-party dependency, and lane C is dependency-free by contract.

### 9.3 `h_row_folded`

Identical to `h_row`, but every `s:` token is recomputed with `fold_punct` + unconditional `casefold()` + unconditional `collapse_ws`, and every `n:` token is left unchanged. Prefix `b"gos1/rowf\x00"`.

Drives the `COSMETIC` classification (§4.1).

### 9.4 `h_extra`

```python
EXTRA_PREFIX = b"gos1/extra\x00"

def h_extra(extra: dict[str, str], spec_version: str) -> bytes:   # 16 bytes
```

Covers every sheet column **not** in the contract, keyed by slug, each canonicalized with a default text contract. Schema growth is thereby detected (drift type `schema_growth`, severity `info`) without polluting the compared hash.

---

## 10. Identity and dataset rollup

### 10.1 `identity_key_bytes`

```python
def identity_key_bytes(parts: list[str]) -> bytes:
    out = b""
    for p in parts:
        pb = p.encode('utf-8')
        out += struct.pack('>I', len(pb)) + pb     # 4-byte big-endian length prefix
    return out
```

Length-prefixed, therefore injection-proof. A naive `"|".join()` makes `("a|b","c")` collide with `("a","b|c")`; the golden vectors in §11.2 prove the prefixed form does not.

`parts` is:
- `[sync_id]` when `identity_source == 'sync_id'` — the raw 26-char ULID, **not** tagged.
- The **canonical tokens** of the `is_natural_key` columns, ordered by `sequence`, when `identity_source == 'natural_key'`.

Identity strategy ranking is fixed by SPEC §5.5 and restated here for the implementer's benefit: injected opaque `_sync_id` first (survives edits to every business field, so "invoice 1001 renamed to 1002" is an update, not delete+create), declared natural key second (fails when the key itself is edited), **row position never** (one user sort produces a full-dataset false drift), and **a hash of mutable content never** (a typo fix becomes a phantom delete + phantom create).

### 10.2 Bucket assignment

```python
BKT_PREFIX = b"gos1/bkt\x00"

def bucket_of(key_bytes: bytes) -> int:
    return int.from_bytes(sha256(BKT_PREFIX + key_bytes).digest()[:2], 'big') % 256
```

256 buckets is the deliberate complexity/benefit choice. A dataset mismatch is localized to typically 1–2 buckets, so only ~0.4 % of rows need materializing for the row-level diff — one extra round trip, no tree-walk code. A full binary Merkle buys `log n` round trips instead of 2 and is not worth the complexity below ~10⁷ rows.

### 10.3 `h_bucket`

```python
def varint(n: int) -> bytes:      # unsigned LEB128
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | 0x80]) if n else bytes([b])
        if not n:
            return out

def h_bucket(index: int, entries: list[tuple[bytes, bytes]]) -> bytes:   # 16 bytes
    entries = sorted(entries, key=lambda e: e[0])          # by identity_key_bytes, BYTE-WISE
    body = b"".join(varint(len(k)) + k + h for k, h in entries)
    return sha256(BKT_PREFIX + varint(index) + varint(len(entries)) + body).digest()[:16]
```

`entries` is `[(identity_key_bytes, h_row)]`.

**Sort byte-wise on the canonical key bytes.** Never `locale.strcoll`, never `str` comparison on decoded text with an ICU collator — those are machine- and locale-dependent and would make the hash non-reproducible.

An empty bucket still has a hash: `h_bucket(i, [])`.

**This is what makes the dataset hash order-insensitive.** A user sorting the sheet is not a data change, and a system that reports 5 000 drifts after a sort will be switched off within a week.

### 10.4 `h_dataset`

```python
DS_PREFIX = b"gos1/ds\x00"

def h_dataset(bucket_hashes: list[bytes], spec_version: str, tab_uid: str, total_rows: int) -> bytes:  # 32 bytes
    assert len(bucket_hashes) == 256
    return sha256(
        DS_PREFIX + spec_version.encode() + b"\x00"
        + tab_uid.encode() + b"\x00"
        + varint(total_rows)
        + b"".join(bucket_hashes)          # buckets 0..255 in index order
    ).digest()
```

`tab_uid` is exactly `"%s/%d" % (node.google_id, dataset.sheet_gid)` — e.g. `1abcDEF/0`. It ties the hash to a specific tab of a specific Drive file, so a hash can never be accidentally reused across the two `Bettr_Bowl_Data_Request` files.

Stored as 64 hex chars in `gdrive.dataset.h_dataset_sheet` / `h_dataset_odoo`.

### 10.5 Header fingerprint

```python
HDR_PREFIX = b"gos1/hdr\x00"

def h_header(header_canons: list[str]) -> bytes:   # 16 bytes
    return sha256(HDR_PREFIX + b"\x00".join(s.encode() for s in sorted(header_canons))).digest()[:16]
```

Sorted, so column **reordering** does not change the fingerprint — reordering is a genuine no-op by construction, because columns resolve by `header_canon` and rows hash by `odoo_field`.

Lane E separately keeps the `header_canon → col_index` map. A change in the fingerprint means a column was **added, removed, or renamed**, which is then classified:
- unmapped column added → `schema_growth`, `info`, non-blocking, absorbed into `h_extra`;
- **mapped column missing or renamed → `header_change`, `blocking`, hard stop, zero rows staged.** Treating an absent mapped column as empty cells would write NULL over an entire Odoo column. That is the single most destructive failure mode in sheet sync and it must be structurally impossible, not merely warned about.

---

## 11. Golden test vectors

These are computed values, not illustrations. Lane C MUST reproduce them byte for byte; lane F MUST assert them in `tests/test_lib_hashing.py` and `tests/test_lib_merkle.py`. All computed with `spec_version = "SPECV1"`.

### 11.1 `h_row` (first 16 bytes, hex)

| Case | `jcs(canon)` | `h_row` |
|---|---|---|
| R1 | `{"amount":"n:1234.50","name":"s:ACME Foods"}` | `3830219051a074351eaf44902059fc01` |
| R1 with the dict built in the opposite key order | `{"amount":"n:1234.50","name":"s:ACME Foods"}` | `3830219051a074351eaf44902059fc01` |
| R2 (extra column) | `{"amount":"n:1234.50","due":"d:2026-07-31","name":"s:ACME Foods"}` | `ba43380c0d901252740bb6decccbdb1b` |
| text `"1"` | `{"v":"s:1"}` | `86fce31b0ee6e5b56592956396b0e4aa` |
| number `1` | `{"v":"n:1"}` | `b24edee0af0d3c173a66d81f70cb33e1` |
| boolean true | `{"v":"b:1"}` | `46345361aa9a318cf48d826e53884332` |
| NULL | `{"v":"z:"}` | `e89c6a4ea3913a8a9a19798c6cb1b439` |

Rows 1–2 prove **key-order invariance**. Rows 4–7 prove the four type families do **not** collide on the payload `1`.

### 11.2 `identity_key_bytes` and `bucket_of`

| `parts` | `identity_key_bytes` (hex) | bucket |
|---|---|---|
| `["s:ACME Foods"]` | `0000000c733a41434d4520466f6f6473` | `23` |
| `["s:a\|b", "s:c"]` | `00000005733a617c6200000003733a63` | `216` |
| `["s:a", "s:b\|c"]` | `00000003733a6100000005733a627c63` | `234` |
| `["01JBX3T7QK9V2M4N6P8R0S1T2U"]` | `0000001a30314a4258335437514b3956324d344e36503852305331543255` | `17` |

Rows 2 and 3 are the delimiter-injection test: with a naive `"|".join()` both produce `s:a|b|s:c` and collide. Length-prefixed, they differ, and land in different buckets.

### 11.3 `h_bucket` and `h_dataset`

Bucket 7 containing exactly two entries — `("s:ACME Foods", h_row({"amount":"n:1234.50","name":"s:ACME Foods"}))` and `("s:Bettr Bowl", h_row({"amount":"n:0.00","name":"s:Bettr Bowl"}))`, sorted byte-wise:

```
h_bucket(7, [...])      = 272b8595d58db43eadea1ab472b2b75a
h_bucket(0, [])         = 4680758652c5d246986a923fa30b2fcc
h_bucket(7, [])         = cae3965449686a04c97d50150cbe08d8
```

Dataset with those 2 rows, `tab_uid = "1abcDEF/0"`, `total_rows = 2`, buckets 0–255 where only bucket 7 is non-empty:

```
h_dataset = 485a5101901b8f2ccf4f42f02447320f7714a953ccf367197e6afdddc8d308c4
```

### 11.4 `h_header`

```
h_header(["s:Amount", "s:Due Date", "s:Invoice Number"]) = f35e0b3ff0179c25d00deb4a74c399d7
```

(Any input permutation MUST yield the same value.)

---

## 12. Invariants lane E may rely on, and MUST test

1. **Determinism.** `CANON` is a pure function of `(raw, contract_dict)`. No clock, no locale, no timezone environment, no ORM, no network.
2. **Total function.** `CANON` never raises for a data reason; it returns an `e:` token. It raises only for a *contract* error (unknown `ctype`, invalid JCS key, missing `sheet_timezone` for a `datetime` column).
3. **Type-family disjointness.** For any two tokens with different tag prefixes, `equal()` is False.
4. **Error non-equality.** `equal(e_token, anything)` is False, including against an identical error token.
5. **Order invariance.** Permuting the rows of a dataset, or the keys of a row payload, does not change `h_dataset`.
6. **Reorder invariance.** Permuting the physical columns of a sheet does not change `h_row`, `h_dataset`, or `h_header`.
7. **Locality.** Changing exactly one cell changes exactly one `h_row` and therefore exactly one bucket hash.
8. **Version binding.** Changing `CANON_VERSION` or any contract option changes `spec_version` and therefore every `h_row`, `h_bucket`, and `h_dataset`.
9. **Convergence.** For every value `v`, `CANON_sheet(v) == CANON_odoo(read_back(write_to_odoo(typed(v))))`. This is the property-based test in `tests/test_convergence.py`, and it is the invariant whose violation produces the "3 fixes applied every night forever" bug that SPEC §9.8's post-apply hash assertion and flap counter exist to catch.
