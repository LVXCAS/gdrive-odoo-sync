"""Restricted RFC 8785 JSON Canonicalization Scheme (lane C).

WHY THIS MODULE EXISTS
======================
Row hashing needs a *canonical serialization*: two dicts with the same keys and
values must produce the same bytes regardless of insertion order, Python
version, or ``json`` module defaults.  The obvious shortcut -- joining
``"%s=%s" % (k, v)`` with a delimiter -- is **delimiter-injectable**: a cell
containing ``"|amount=0"`` can forge another field's value, and ``{"a": "1",
"b": ""}`` collides with ``{"a": "1|b=", ...}``.  In a system whose entire
product claim is "these two datasets are identical", a forgeable preimage is a
forgeable ``verified``.

This is a deliberately *restricted* subset of RFC 8785:

* every key must match ``^[A-Za-z_][A-Za-z0-9_]*$`` -- with an ASCII-identifier
  charset, byte order and UTF-16 code-unit order are identical, so key sorting
  is unambiguous with zero UTF-16 machinery (full RFC 8785 sorting is defined
  over UTF-16 code units and is a notorious source of cross-implementation
  disagreement);
* every value must be a ``str`` -- a tagged token.  Floats are forbidden in the
  preimage; that is the entire point of the ``Decimal`` canonicalization in
  ``number_canon``.

Stdlib only.
"""

from __future__ import annotations

import re
from typing import Final, Mapping

__all__ = ["JCS_KEY_RE", "is_valid_jcs_key", "jcs", "escape_json_string"]

#: The only key charset this canonicalization accepts.  Odoo field names
#: already satisfy it; unmapped sheet columns are forced into it by
#: ``contract.slugify``.
JCS_KEY_RE: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Two-character escapes mandated by CANONICALIZATION §9.1 rule 5.  Note that
#: ``/`` is deliberately absent: escaping it is legal JSON but changes the
#: bytes, and this table must be exhaustive and frozen.
_SHORT_ESCAPES: Final = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def is_valid_jcs_key(key: str) -> bool:
    """True when ``key`` may be used as a JCS object key."""
    return isinstance(key, str) and bool(JCS_KEY_RE.match(key))


def escape_json_string(s: str) -> str:
    """Escape ``s`` for inclusion in a canonical JSON string literal.

    Exactly seven two-character escapes, then ``\\u00xx`` with **lowercase**
    hex for every remaining code point below ``U+0020``.  Nothing else is
    escaped -- not ``/``, not non-ASCII: the output is UTF-8 and non-ASCII code
    points are emitted literally (``ensure_ascii=False`` semantics).  Escaping
    non-ASCII would be legal JSON but would make the preimage depend on which
    JSON writer produced it, which defeats canonicalization.
    """
    out = []
    for ch in s:
        esc = _SHORT_ESCAPES.get(ch)
        if esc is not None:
            out.append(esc)
        elif ch < " ":
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    return "".join(out)


def jcs(payload: Mapping[str, str]) -> bytes:
    """Serialize a flat ``{identifier: tagged-token}`` mapping to canonical bytes.

    Output shape is ``{"k1":"v1","k2":"v2"}`` -- no whitespace anywhere, keys in
    ascending byte order, UTF-8 encoded.

    Raises:
        TypeError: if ``payload`` is not a mapping.
        ValueError: if any key violates ``JCS_KEY_RE`` or any value is not a
            ``str``.  Both are *contract* errors (lane E built the payload
            wrong), never data errors, so they raise rather than returning an
            ``e:`` token -- silently hashing a malformed payload would produce a
            stable-looking digest over garbage.
    """
    if not isinstance(payload, Mapping):
        raise TypeError("jcs() expects a mapping, got %r" % (type(payload).__name__,))

    parts = []
    for key in sorted(payload):
        if not is_valid_jcs_key(key):
            raise ValueError(
                "Invalid JCS key %r: keys must match ^[A-Za-z_][A-Za-z0-9_]*$ "
                "so that byte order and UTF-16 code-unit order coincide." % (key,)
            )
        value = payload[key]
        if not isinstance(value, str):
            raise ValueError(
                "Invalid JCS value for key %r: expected a tagged token (str), got %r. "
                "Floats and other scalars are forbidden in a hash preimage."
                % (key, type(value).__name__)
            )
        parts.append('"%s":"%s"' % (escape_json_string(key), escape_json_string(value)))

    return ("{" + ",".join(parts) + "}").encode("utf-8")
