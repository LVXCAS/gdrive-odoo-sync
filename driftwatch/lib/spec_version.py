"""``CANON_VERSION`` and ``spec_version`` -- the cache-invalidation keys (lane C).

WHY THIS MODULE EXISTS
======================
Every hash this system stores is an assertion of the form "under normalizer
*N* and contract *C*, these two datasets are identical".  If *N* or *C* changes
and the stored hash does not, the system will happily serve the old digest and
report ``verified`` -- over data it has never actually compared under the new
rules.  That is a **silent false pass**, the single worst failure mode a
verification system can have, and it is invisible: the dashboard is green.

``spec_version`` is the only structural defence.  It binds every hash preimage
to ``H(contract || CANON_VERSION)``, so:

* changing any column option, adding or removing a mapped column, or bumping
  ``CANON_VERSION`` changes ``spec_version``;
* a stored hash whose ``spec_version`` differs from the current one **MUST be
  treated as absent**, never as a cache hit;
* a full recompute is forced weekly and on every module upgrade anyway, because
  the fast path is an optimization built on assumptions and the periodic full
  pass is what catches the day one of them is wrong.

**Bump ``CANON_VERSION`` on ANY behavioural change anywhere in lane C.**  If
you are unsure whether a change is behavioural, it is: bump it.  The cost of an
unnecessary bump is one full recompute; the cost of a missed bump is a green
dashboard over unverified data.
"""

from __future__ import annotations

import hashlib
from typing import Any, Final, Mapping

from .jcs import jcs
from .tokens import SPEC_PREFIX

__all__ = ["CANON_VERSION", "compute_spec_version", "flatten_for_hash"]

#: Bump on ANY behavioural change in lane C.  Format is ``gos-canon-<n>``.
#:
#: ``2`` — ``NUM_CANON`` no longer discards a trailing ``%`` under the default
#: ``percent_mode='none'`` (``"50%"`` and ``"50"`` used to produce the same
#: token), ``header_canon`` re-entered the ``spec_version`` preimage, and
#: contract options are now length-framed instead of comma-joined.  All three
#: change what a cell canonicalizes to or what a contract hashes to, so every
#: stored hash computed under ``gos-canon-1`` must be treated as absent.
CANON_VERSION: Final = "gos-canon-2"


def _stringify(value: Any) -> str:
    """Deterministically stringify one scalar for a spec preimage.

    ``bool`` is checked before ``int`` because ``bool`` is an ``int`` subclass
    and ``True`` must not serialize as ``1`` -- otherwise flipping an option
    from ``True`` to the integer ``1`` would not change ``spec_version`` even
    though it might change behaviour.  ``float`` uses ``repr`` (the shortest
    round-tripping literal) for the same reason ``NUM_CANON`` does.
    """
    if value is None:
        return "z:"
    if isinstance(value, bool):
        return "b:1" if value else "b:0"
    if isinstance(value, float):
        return "n:" + repr(value)
    if isinstance(value, (int,)):
        return "n:" + str(value)
    if isinstance(value, bytes):
        return "x:" + value.hex()
    return "s:" + str(value)


def flatten_for_hash(obj: Any, prefix: str = "c") -> dict[str, str]:
    """Flatten an arbitrary contract structure into a JCS-compatible mapping.

    ``jcs`` accepts only ``{ASCII-identifier: str}``, which is exactly the
    restriction that makes key sorting unambiguous.  Real contracts are nested
    (a list of column dicts, each with a ``value_map``), so they are flattened
    here into deterministic identifier keys such as ``c_0__value_map__draft``.

    Non-identifier characters in a key become ``_``; a key that would start
    with a digit is prefixed with ``_``.  Collisions after that sanitization
    are impossible in practice because the positional prefix is unique per
    container, and if one ever occurred it would raise rather than silently
    drop a value.
    """
    out: dict[str, str] = {}

    def sanitize(part: str) -> str:
        cleaned = "".join(ch if (ch.isalnum() and ch.isascii()) or ch == "_" else "_" for ch in part)
        if not cleaned or cleaned[0].isdigit():
            cleaned = "_" + cleaned
        return cleaned

    def walk(node: Any, path: str) -> None:
        if isinstance(node, Mapping):
            for k in sorted(node, key=lambda x: str(x)):
                walk(node[k], "%s__%s" % (path, sanitize(str(k))))
        elif isinstance(node, (list, tuple)):
            walk(len(node), "%s__len" % path)
            for i, item in enumerate(node):
                walk(item, "%s__%d" % (path, i))
        else:
            if path in out:
                raise ValueError("Key collision while flattening contract: %r" % (path,))
            out[path] = _stringify(node)

    walk(obj, sanitize(prefix))
    return out


def compute_spec_version(contract: Any) -> str:
    """Hex SHA-256 of the serialized contract plus the normalizer version.

    ``H(b"gos1/spec\\x00" + jcs(contract) + CANON_VERSION)``.

    Args:
        contract: either an already-flat ``{identifier: str}`` mapping (the fast
            path, produced by ``contract.serialize_contracts``) or any nested
            structure, which is flattened deterministically first.

    Returns:
        64 hex characters.  Stored on ``gdrive.dataset.spec_version`` and
        ``gdrive.mapping.spec_version`` and mixed into **every** row, bucket and
        dataset hash preimage.
    """
    if isinstance(contract, Mapping) and all(
        isinstance(k, str) and isinstance(v, str) for k, v in contract.items()
    ):
        flat = dict(contract)
    else:
        flat = flatten_for_hash(contract)
    return hashlib.sha256(SPEC_PREFIX + jcs(flat) + CANON_VERSION.encode("utf-8")).hexdigest()
