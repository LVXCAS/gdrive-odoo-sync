"""Row, header and identity hashing (lane C).

WHY SHA-256, AND WHY NOTHING ELSE
=================================
The entire product claim is "these two datasets are identical".  A hash
collision here is therefore not a performance annoyance -- it is a **false
``verified``**, the exact assertion the system exists to make.  MD5, SHA-1,
CRC32 and xxHash all have practical or trivially constructible collisions and
are permanently out of scope.  BLAKE3 would be preferable on speed but is a
third-party dependency, and lane C is dependency-free by contract.

Digests are truncated to 128 bits for row/bucket hashes, giving a ~2^64
birthday bound -- ample for any dataset this system will ever see, and it
halves the storage of the 256 per-dataset bucket digests.  The dataset hash is
kept at the full 256 bits because it is the value a human reads and quotes.

Every preimage begins with a domain-separation prefix (``gos1/row\\x00``,
``gos1/bkt\\x00``, ...) so a bucket digest can never be reinterpreted as a row
digest, and every preimage includes ``spec_version`` so a hash computed under
an older normalizer can never be mistaken for a current one.

Stdlib only: ``hashlib``, ``struct``.
"""

from __future__ import annotations

import hashlib
import struct
from typing import Iterable, Mapping, Sequence

from .jcs import jcs
from .text_canon import cosmetic_fold
from .tokens import (
    BKT_PREFIX,
    EXTRA_PREFIX,
    HDR_PREFIX,
    ROWF_PREFIX,
    ROW_PREFIX,
    TAG_TEXT,
)

__all__ = [
    "H",
    "H128",
    "varint",
    "h_row",
    "h_row_hex",
    "h_row_folded",
    "h_row_folded_hex",
    "h_extra",
    "h_extra_hex",
    "h_header",
    "h_header_hex",
    "fold_canon_map",
    "identity_key_bytes",
    "bucket_of",
    "hash_binary",
]


def H(data: bytes) -> bytes:
    """Full 32-byte SHA-256 digest of ``data``."""
    return hashlib.sha256(data).digest()


def H128(data: bytes) -> bytes:
    """First 16 bytes of the SHA-256 digest of ``data``.

    Truncation is safe here: SHA-256 has no known length-extension-independent
    structure that makes a prefix weaker than a fresh 128-bit hash, and 2^64
    birthday resistance is far beyond the ~10^7 rows this system targets.
    """
    return hashlib.sha256(data).digest()[:16]


def varint(n: int) -> bytes:
    """Unsigned LEB128 encoding of ``n``.

    Used for lengths and counts inside bucket/dataset preimages.  A fixed-width
    encoding would work too; what matters is that the encoding is
    self-delimiting, so a length field can never be confused with the payload
    that follows it.

    Raises:
        ValueError: on a negative input -- LEB128 here is unsigned by
            construction and a negative length is a caller bug.
    """
    if n < 0:
        raise ValueError("varint() is unsigned; got %d" % (n,))
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


# --------------------------------------------------------------------------
# Row hashing
# --------------------------------------------------------------------------


def h_row(canon: Mapping[str, str], spec_version: str) -> bytes:
    """16-byte digest of one row's canonical payload.

    ``canon`` contains **exactly** the contract columns (``ctype != 'ignore'``),
    keyed by ``odoo_field`` when the column is mapped and by the column ``slug``
    otherwise.

    **Keying by ``odoo_field`` rather than by the sheet header is what makes the
    row hash invariant to column reordering *and* to cosmetic header renames at
    the same time.**  A user dragging column D to position B is not a data
    change, and a system that reports 4 000 drifts after a drag will be turned
    off.
    """
    preimage = ROW_PREFIX + spec_version.encode("utf-8") + b"\x00" + jcs(canon)
    return H128(preimage)


def h_row_hex(canon: Mapping[str, str], spec_version: str) -> str:
    """``h_row`` as 32 lowercase hex characters, the storage form."""
    return h_row(canon, spec_version).hex()


def fold_canon_map(canon: Mapping[str, str]) -> dict[str, str]:
    """Rebuild a canonical payload with every ``s:`` token cosmetically folded.

    Numeric, date, boolean, relational and selection tokens pass through
    untouched -- there is no such thing as a cosmetic difference in ``n:12.30``
    -- so folding is confined to text, where smart quotes, case and whitespace
    runs actually occur.
    """
    folded: dict[str, str] = {}
    for key, token in canon.items():
        if isinstance(token, str) and token.startswith(TAG_TEXT):
            folded[key] = TAG_TEXT + cosmetic_fold(token[len(TAG_TEXT) :])
        else:
            folded[key] = token
    return folded


def h_row_folded(canon: Mapping[str, str], spec_version: str) -> bytes:
    """16-byte digest of the cosmetically folded row.

    Strict hashes differing while folded hashes match is exactly the
    ``COSMETIC`` drift classification: reported, **not** auto-written by
    default.  Auto-writing cosmetic differences is how a sync ends up rewriting
    the same 200 cells every night forever without ever converging.
    """
    preimage = ROWF_PREFIX + spec_version.encode("utf-8") + b"\x00" + jcs(fold_canon_map(canon))
    return H128(preimage)


def h_row_folded_hex(canon: Mapping[str, str], spec_version: str) -> str:
    """``h_row_folded`` as 32 lowercase hex characters."""
    return h_row_folded(canon, spec_version).hex()


def h_extra(extra: Mapping[str, str], spec_version: str) -> bytes:
    """16-byte digest of the columns that are **not** in the contract.

    Keyed by slug, each canonicalized with a default text contract.  Schema
    growth is thereby detectable (drift type ``schema_growth``, severity
    ``info``) without polluting the compared hash: adding an unmapped notes
    column to a sheet must not make every row look changed.
    """
    preimage = EXTRA_PREFIX + spec_version.encode("utf-8") + b"\x00" + jcs(extra)
    return H128(preimage)


def h_extra_hex(extra: Mapping[str, str], spec_version: str) -> str:
    """``h_extra`` as 32 lowercase hex characters."""
    return h_extra(extra, spec_version).hex()


# --------------------------------------------------------------------------
# Header fingerprint
# --------------------------------------------------------------------------


def h_header(header_canons: Iterable[str]) -> bytes:
    """16-byte fingerprint of a tab's header labels.

    Sorted before hashing, so column **reordering** does not change the
    fingerprint -- reordering is a genuine no-op by construction, because
    columns resolve by ``header_canon`` and rows hash by ``odoo_field``.

    A change in this value therefore means a column was **added, removed or
    renamed**, which lane E classifies as ``schema_growth`` (unmapped column
    added: info, non-blocking) or ``header_change`` (mapped column missing or
    renamed: **blocking**, zero rows staged).  Treating an absent mapped column
    as empty cells would write NULL over an entire Odoo column, which is the
    single most destructive failure mode in sheet sync and must be structurally
    impossible rather than merely warned about.
    """
    joined = b"\x00".join(s.encode("utf-8") for s in sorted(header_canons))
    return H128(HDR_PREFIX + joined)


def h_header_hex(header_canons: Iterable[str]) -> str:
    """``h_header`` as 32 lowercase hex characters, the storage form."""
    return h_header(header_canons).hex()


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


def identity_key_bytes(parts: Sequence[str]) -> bytes:
    """Length-prefixed, injection-proof encoding of an identity key.

    Each part is UTF-8 encoded and preceded by its 4-byte big-endian length.

    WHY not ``"|".join(parts)``: ``("a|b", "c")`` and ``("a", "b|c")`` both
    produce ``a|b|c`` and collide -- two different records with one identity,
    which in a delete-capable system means one of them is eventually deleted as
    a duplicate.  Length prefixing makes the encoding unambiguous, and the
    golden vectors in CANONICALIZATION §11.2 prove the two cases land in
    different buckets.

    ``parts`` is ``[sync_id]`` (the raw 26-character ULID, untagged) when
    identity comes from an injected id column, or the **canonical tokens** of
    the ``is_natural_key`` columns ordered by ``sequence`` when it comes from a
    declared natural key.  Row position is never an identity: one user sort
    would produce a full-dataset false drift.  A hash of mutable content is
    never an identity either: a typo fix would become a phantom delete plus a
    phantom create.
    """
    out = bytearray()
    for part in parts:
        pb = part.encode("utf-8")
        out += struct.pack(">I", len(pb))
        out += pb
    return bytes(out)


def bucket_of(key_bytes: bytes) -> int:
    """Map an identity key to one of 256 Merkle buckets.

    256 buckets is the deliberate complexity/benefit choice: a dataset mismatch
    localizes to typically one or two buckets, so only ~0.4 % of rows need
    materializing for the row-level diff -- one extra round trip, no tree-walk
    code.  A full binary Merkle tree buys ``log n`` round trips instead of two
    and is not worth the complexity below ~10^7 rows.
    """
    return int.from_bytes(H(BKT_PREFIX + key_bytes)[:2], "big") % 256


def hash_binary(data: bytes) -> str:
    """Hex SHA-256 of raw bytes, for binary/image fields.

    Binary fields are hashed **separately** and stored on the drift record;
    they are never inlined into a row-hash preimage.  A 4 MB image in a row
    preimage would make every row hash cost a megabyte of hashing and would put
    the image bytes into a JSON string, which is neither.
    """
    return hashlib.sha256(data).hexdigest()
