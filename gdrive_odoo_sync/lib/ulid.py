"""``new_ulid`` / ``is_ulid`` -- the injected identity generator (lane C).

WHY A ULID AND NOT A UUID4
==========================
The ``_sync_id`` column is written back into a *spreadsheet*, where a human
sorts, filters and eyeballs it. Three properties matter, and only a ULID has
all three:

* **Lexicographically sortable.** The first 48 bits are a millisecond
  timestamp, so sorting the column as text sorts it by creation time. A UUID4
  column sorts as noise, which makes "which rows did last night's run add?"
  unanswerable from inside the sheet.
* **No dashes, fixed 26 characters.** Google Sheets does not reformat it, and
  the column width is stable. A dashed UUID invites a spreadsheet autoformat
  and a "helpful" find-and-replace.
* **Crockford base32.** The alphabet excludes ``I``, ``L``, ``O`` and ``U``, so
  the two transcription errors a human actually makes -- ``1``/``I`` and
  ``0``/``O`` -- cannot produce a *different valid* id.

**Generated at plan time, never at apply time.** A retried apply must reuse the
id it already planned, otherwise a network timeout between "wrote the row" and
"recorded the link" produces a second record on the retry -- the duplicate this
whole identity mechanism exists to prevent.

Stdlib only: ``secrets``, ``time``.
"""

from __future__ import annotations

import secrets
import time
from typing import Any, Final

__all__ = ["ULID_ALPHABET", "ULID_LENGTH", "new_ulid", "is_ulid", "ulid_timestamp_ms"]

#: Crockford base32. ``I``, ``L``, ``O`` and ``U`` are deliberately absent.
ULID_ALPHABET: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

ULID_LENGTH: Final = 26

#: 10 characters of timestamp (48 bits) + 16 characters of randomness (80 bits).
_TIME_CHARS: Final = 10
_RANDOM_CHARS: Final = 16
_TIME_MAX: Final = (1 << 48) - 1

#: Decode table. Built once; includes the lowercase forms because a human
#: retyping an id into a sheet will not hold shift, and rejecting a value that
#: is unambiguously the right id would start a phantom delete plus create.
_DECODE: Final = {ch: i for i, ch in enumerate(ULID_ALPHABET)}
_DECODE.update({ch.lower(): i for i, ch in enumerate(ULID_ALPHABET)})


def _encode(value: int, length: int) -> str:
    """Encode ``value`` as exactly ``length`` Crockford base32 characters."""
    out = [""] * length
    for position in range(length - 1, -1, -1):
        out[position] = ULID_ALPHABET[value & 0x1F]
        value >>= 5
    return "".join(out)


def new_ulid(timestamp_ms: int | None = None) -> str:
    """Return a fresh 26-character uppercase ULID.

    Args:
        timestamp_ms: milliseconds since the Unix epoch. Injected only by tests;
            production always uses the wall clock.

    The 80 random bits come from :func:`secrets.token_bytes`, not from
    ``random``: the id is a durable business identifier written into a document
    other people can see, and a predictable id in a shared sheet is an id
    somebody can guess and collide with. ``random`` is seeded from the clock and
    is shared process-wide, which in a forked Odoo worker pool means two workers
    can emit the same sequence.
    """
    ms = int(time.time() * 1000) if timestamp_ms is None else int(timestamp_ms)
    if not 0 <= ms <= _TIME_MAX:
        raise ValueError("ULID timestamp %d is outside the representable 48-bit range" % (ms,))
    randomness = int.from_bytes(secrets.token_bytes(10), "big")  # 80 bits
    return _encode(ms, _TIME_CHARS) + _encode(randomness, _RANDOM_CHARS)


def is_ulid(value: Any) -> bool:
    """True when ``value`` is a syntactically valid ULID.

    Checks the length, the alphabet (case-insensitively) and the 48-bit
    timestamp ceiling. It deliberately does **not** check that the timestamp is
    plausible: a clock-skewed worker still produced a real, unique id, and
    rejecting it here would be a data-loss decision made on a heuristic.
    """
    if not isinstance(value, str) or len(value) != ULID_LENGTH:
        return False
    if any(ch not in _DECODE for ch in value):
        return False
    # The first character encodes the top 5 bits of a 48-bit value, so anything
    # above '7' (decimal 7) overflows and is not a ULID at all.
    return _DECODE[value[0]] <= 7


def ulid_timestamp_ms(value: str) -> int:
    """Return the millisecond timestamp embedded in ``value``.

    Raises:
        ValueError: when ``value`` is not a valid ULID. Returning a plausible
            number for an invalid id would put a fabricated creation time into a
            report.
    """
    if not is_ulid(value):
        raise ValueError("Not a ULID: %r" % (value,))
    ms = 0
    for ch in value[:_TIME_CHARS]:
        ms = (ms << 5) | _DECODE[ch]
    return ms
