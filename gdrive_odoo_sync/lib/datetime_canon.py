"""``serial_to_naive``, ``DATE_CANON``, ``DATETIME_CANON`` (lane C).

WHY THIS MODULE EXISTS
======================
Three independent bugs live in this area, and all three are the kind that
produce a *full-column* false drift:

1. **Serial truncation.**  Google Sheets returns dates as Lotus-style serials
   (days since 1899-12-30).  ``45000.5`` arrives over the wire as
   ``45000.499999999996``, and truncating the fractional part gives ``11:59:59``
   instead of ``12:00:00``.  Round, never truncate.
2. **Timezone-converting a pure date.**  Pushing a date through a timezone is
   the classic off-by-one-day bug, and characteristically it only shows up at
   night -- when the cron runs and UTC has already rolled over.  Dates never
   see a timezone, on either side, ever.
3. **Fuzzy date parsing.**  ``"03/04/2026"`` is genuinely unresolvable; a fuzzy
   parser picks one interpretation silently and corrupts a year of data.  Only
   strict ``strptime`` against the administrator's declared format list.

A spreadsheet has no timezone, so ``datetime`` columns require an explicitly
declared IANA ``sheet_timezone``.  DST is handled by fiat where it is
ambiguous, and refused where it is impossible -- never silently shifted.

Stdlib only: ``datetime``, ``decimal``, ``zoneinfo``.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .text_canon import opt, text_prepare
from .tokens import (
    ABSENT,
    ERR_BAD_DATE,
    ERR_NONEXISTENT_LOCAL_TIME,
    NULL_TOKEN,
    TAG_DATE,
    TAG_DATETIME,
    WARN_AMBIGUOUS_LOCAL_TIME,
    WARN_TIME_COMPONENT_PRESENT,
    add_warning,
    error,
)

__all__ = [
    "SERIAL_EPOCH",
    "serial_to_naive",
    "naive_to_serial",
    "DATE_CANON",
    "DATETIME_CANON",
    "parse_strict",
]

#: The Lotus 1-2-3 epoch Google Sheets and Excel both inherited.  1899-12-30,
#: not 1899-12-31, because of the deliberate Lotus leap-year bug that treats
#: 1900 as a leap year; using 12-31 puts every date one day out.
SERIAL_EPOCH: Final = date(1899, 12, 30)

_SECONDS_PER_DAY: Final = 86400
_WORK_PREC: Final = 40


def serial_to_naive(serial: Any) -> datetime:
    """Convert a spreadsheet serial number to a naive ``datetime``.

    Days since 1899-12-30; the fractional part is a fraction of a day, **rounded
    to whole seconds**.

    Microseconds are deliberately not representable: one microsecond is
    ``1.157e-11`` of a day, which is below double precision at the serial
    magnitudes in use (around 45 000).  Pretending to sub-second accuracy would
    manufacture differences out of float noise.  Second precision is the
    contract.

    Raises:
        TypeError: if ``serial`` is not numeric (a contract/plumbing error).
    """
    if isinstance(serial, bool) or not isinstance(serial, (int, float, Decimal)):
        raise TypeError("serial_to_naive() expects a number, got %r" % (type(serial).__name__,))

    with localcontext() as ctx:
        ctx.prec = _WORK_PREC
        d = serial if isinstance(serial, Decimal) else Decimal(repr(serial))
        days = int(d)  # truncates toward zero
        frac = d - days
        seconds = int((frac * _SECONDS_PER_DAY).quantize(Decimal(1), rounding=ROUND_HALF_UP))

    if seconds == _SECONDS_PER_DAY:
        # 23:59:59.6 rounds up to a whole day.
        days += 1
        seconds = 0
    elif seconds < 0:
        # Only reachable for negative serials (pre-1899 dates), which do not
        # occur in practice; normalizing here keeps the function total instead
        # of returning a datetime with a negative time-of-day.
        days -= 1
        seconds += _SECONDS_PER_DAY

    return datetime.combine(SERIAL_EPOCH + timedelta(days=days), time()) + timedelta(
        seconds=seconds
    )


def naive_to_serial(dt: datetime) -> Decimal:
    """Inverse of ``serial_to_naive``, exact to the second.

    Used by lane E when it needs to express an Odoo-side value in sheet terms
    for a human-readable report.  Never used inside a hash preimage -- the
    canonical form is always the ISO string, never the serial.
    """
    delta = dt - datetime.combine(SERIAL_EPOCH, time())
    with localcontext() as ctx:
        ctx.prec = _WORK_PREC
        return Decimal(delta.days) + (
            Decimal(delta.seconds) / Decimal(_SECONDS_PER_DAY)
        )


def parse_strict(s: str, formats) -> datetime | None:
    """Try each ``strptime`` format **in declared order**; first success wins.

    Strict only.  ``dateutil``, ``pandas`` inference and every other fuzzy
    parser are forbidden: they resolve genuinely ambiguous input silently, and
    "silently wrong" is the only failure mode this system exists to prevent.

    Returns ``None`` when nothing matched, which the callers turn into
    ``e:BAD_DATE``.
    """
    for fmt in formats or ():
        try:
            return datetime.strptime(s, fmt)
        except (ValueError, TypeError):
            continue
    return None


def _formats_for_date(col: Any):
    return tuple(opt(col, "date_formats", ()) or ())


def _formats_for_datetime(col: Any):
    """Datetime columns try their own formats first, then the date formats.

    A date-only format applied to a datetime column yields midnight local time,
    which is the correct reading of a date typed into a timestamp column.
    """
    return tuple(opt(col, "datetime_formats", ()) or ()) + _formats_for_date(col)


def DATE_CANON(v: Any, col: Any = None, side: str = "sheet", warnings: list | None = None) -> str:
    """Canonicalize ``v`` as a calendar date.  **Never timezone-converted.**

    * absent / ``None`` / Odoo ``False`` -> ``z:``
    * Odoo side: ``v`` is already a ``datetime.date``; emit it and stop
    * sheet side, numeric: ``serial_to_naive``; a non-zero time component emits
      ``TIME_COMPONENT_PRESENT`` at warning and is **dropped**
    * sheet side, string: ``TEXT_CANON`` steps 1-6 then strict ``strptime``
    * no match -> ``e:BAD_DATE``

    Returns ``"d:YYYY-MM-DD"``.
    """
    if v is None or v is ABSENT or v is False:
        return NULL_TOKEN

    if isinstance(v, datetime):
        # datetime is a subclass of date; take the date part on either side.
        if v.time() != time():
            add_warning(warnings, WARN_TIME_COMPONENT_PRESENT, v.isoformat())
        return TAG_DATE + v.date().isoformat()
    if isinstance(v, date):
        return TAG_DATE + v.isoformat()

    if isinstance(v, bool):
        # bool is an int subclass and would otherwise be read as serial 0 or 1,
        # i.e. 1899-12-30. Refuse instead.
        return error(ERR_BAD_DATE)

    if isinstance(v, (int, float, Decimal)):
        naive = serial_to_naive(v)
        if naive.time() != time():
            add_warning(warnings, WARN_TIME_COMPONENT_PRESENT, naive.isoformat())
        return TAG_DATE + naive.date().isoformat()

    s = text_prepare(v, warnings)
    if s == "":
        return NULL_TOKEN
    parsed = parse_strict(s, _formats_for_date(col))
    if parsed is None:
        return error(ERR_BAD_DATE)
    return TAG_DATE + parsed.date().isoformat()


def DATETIME_CANON(
    v: Any,
    col: Any = None,
    sheet_timezone: str = "",
    side: str = "sheet",
    warnings: list | None = None,
) -> str:
    """Canonicalize ``v`` as an instant, always rendered UTC with a ``Z``.

    Odoo side
        ``fields.Datetime`` is stored and returned **UTC-naive**.  Treat it as
        UTC directly and emit it.  **Never apply the user's timezone** -- the
        value in the database is already UTC, and applying a display timezone
        to it shifts every timestamp by the user's offset.

    Sheet side
        A spreadsheet has no timezone, so ``sheet_timezone`` (from
        ``gdrive.dataset.sheet_timezone``, default ``America/New_York``) is
        **required**.  The naive local time is localized with ``fold=0`` and
        converted to UTC.

        * **Ambiguous** local times (the repeated hour at DST fall-back) resolve
          to the *first*, pre-transition occurrence -- deterministic by fiat --
          and emit ``AMBIGUOUS_LOCAL_TIME`` at info.
        * **Nonexistent** local times (the skipped hour at spring-forward) are
          detected by round-tripping the wall clock and return
          ``e:NONEXISTENT_LOCAL_TIME``.  Silently shifting them would invent an
          instant the user never wrote.

    Returns ``"t:YYYY-MM-DDTHH:MM:SSZ"``, always second precision.

    Raises:
        ValueError: when a sheet-side value needs a timezone and none was
            declared, or the declared IANA name is unknown.  That is a contract
            error, not a data error (CANONICALIZATION §12 invariant 2).
    """
    if v is None or v is ABSENT or v is False:
        return NULL_TOKEN

    if side == "odoo":
        if isinstance(v, datetime):
            naive = v.replace(tzinfo=None) if v.tzinfo is None else v.astimezone(timezone.utc).replace(tzinfo=None)
            return TAG_DATETIME + naive.strftime("%Y-%m-%dT%H:%M:%SZ")
        if isinstance(v, date):
            return TAG_DATETIME + datetime.combine(v, time()).strftime("%Y-%m-%dT%H:%M:%SZ")
        if isinstance(v, str):
            s = text_prepare(v, warnings)
            if s == "":
                return NULL_TOKEN
            parsed = parse_strict(s, ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")) or parse_strict(
                s, _formats_for_datetime(col)
            )
            if parsed is None:
                return error(ERR_BAD_DATE)
            return TAG_DATETIME + parsed.strftime("%Y-%m-%dT%H:%M:%SZ")
        return error(ERR_BAD_DATE)

    # ---- sheet side ----
    tz_name = sheet_timezone or opt(col, "sheet_timezone", "")
    if not tz_name:
        raise ValueError(
            "A datetime column requires a declared sheet_timezone (IANA name). "
            "A spreadsheet carries no timezone and guessing one shifts every "
            "value by hours."
        )
    try:
        zone = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Unknown IANA timezone %r on the dataset: %s" % (tz_name, exc)) from exc

    if isinstance(v, bool):
        return error(ERR_BAD_DATE)
    if isinstance(v, datetime):
        naive = v.replace(tzinfo=None) if v.tzinfo is None else v.astimezone(timezone.utc).replace(tzinfo=None)
        if v.tzinfo is not None:
            # Already an instant; no localization needed or wanted.
            return TAG_DATETIME + naive.strftime("%Y-%m-%dT%H:%M:%SZ")
    elif isinstance(v, date):
        naive = datetime.combine(v, time())
    elif isinstance(v, (int, float, Decimal)):
        naive = serial_to_naive(v)
    else:
        s = text_prepare(v, warnings)
        if s == "":
            return NULL_TOKEN
        parsed = parse_strict(s, _formats_for_datetime(col))
        if parsed is None:
            return error(ERR_BAD_DATE)
        naive = parsed

    aware = naive.replace(tzinfo=zone, fold=0)

    # Nonexistent local time: round-trip the *wall clock*, not the instant.
    # Comparing the two aware datetimes directly would compare instants and be
    # trivially equal, which is why this is done on the naive projections.
    roundtrip = aware.astimezone(timezone.utc).astimezone(zone)
    if roundtrip.replace(tzinfo=None) != naive:
        return error(ERR_NONEXISTENT_LOCAL_TIME)

    if aware.utcoffset() != aware.replace(fold=1).utcoffset():
        add_warning(warnings, WARN_AMBIGUOUS_LOCAL_TIME, naive.isoformat())

    utc = aware.astimezone(timezone.utc)
    return TAG_DATETIME + utc.strftime("%Y-%m-%dT%H:%M:%SZ")
