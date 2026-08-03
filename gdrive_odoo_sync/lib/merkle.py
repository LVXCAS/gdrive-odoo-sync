"""Bucketed-Merkle dataset rollup (lane C).

WHY THIS MODULE EXISTS
======================
Comparing two datasets row by row costs one comparison per row and a full
materialization of both sides.  Comparing one hash per side costs nothing but
tells you only *that* they differ.  The bucketed rollup gives both: 256 bucket
digests localize a mismatch to typically one or two buckets, so ~0.4 % of rows
are materialized for the row-level diff.

**The rollup is order-insensitive by construction.**  Entries are sorted by
their identity key *bytes* before hashing, so a user sorting the sheet -- which
is not a data change -- produces the identical dataset hash.  A system that
reports 5 000 drifts after somebody clicks "Sort A-Z" gets switched off within
a week.

Sorting is byte-wise on the canonical key bytes.  Never ``locale.strcoll``,
never a decoded-string comparison under an ICU collator: those are machine- and
locale-dependent, which would make the hash non-reproducible across an Odoo.sh
worker and a developer laptop.

Stdlib only.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Sequence

from .hashing import H, H128, bucket_of, varint
from .tokens import BKT_PREFIX, DS_PREFIX

__all__ = [
    "BUCKET_COUNT",
    "h_bucket",
    "h_bucket_hex",
    "empty_bucket_hashes",
    "group_entries_by_bucket",
    "compute_bucket_hashes",
    "h_dataset",
    "h_dataset_hex",
    "dataset_digest",
    "diff_buckets",
]

#: Fixed at 256 forever: it is part of the dataset hash preimage (all 256
#: digests are concatenated), so changing it is a normalizer change requiring a
#: ``CANON_VERSION`` bump and a full recompute of every stored hash.
BUCKET_COUNT = 256


def h_bucket(index: int, entries: Iterable[tuple[bytes, bytes]]) -> bytes:
    """16-byte digest of one bucket.

    Args:
        index: the bucket number, 0-255.  It is mixed into the preimage so that
            an empty bucket 7 and an empty bucket 8 have different digests --
            otherwise a shift of every row into a neighbouring bucket could
            leave the concatenation unchanged.
        entries: ``[(identity_key_bytes, h_row)]`` in any order.

    Entries are sorted **byte-wise on the identity key** and each is emitted as
    ``varint(len(key)) + key + h_row``.  The self-delimiting length prefix is
    what stops two adjacent entries from being re-parsed as one, which is the
    same injection problem ``identity_key_bytes`` solves one level down.

    An empty bucket still has a hash: ``h_bucket(i, [])``.  Omitting empty
    buckets would make the dataset preimage variable-length in a way that
    depends on the data, and "no rows in bucket 7" is itself information worth
    committing to.
    """
    if not 0 <= index < BUCKET_COUNT:
        raise ValueError("Bucket index %r outside 0..%d" % (index, BUCKET_COUNT - 1))
    ordered = sorted(entries, key=lambda e: e[0])
    body = b"".join(varint(len(key)) + key + digest for key, digest in ordered)
    return H128(BKT_PREFIX + varint(index) + varint(len(ordered)) + body)


def h_bucket_hex(index: int, entries: Iterable[tuple[bytes, bytes]]) -> str:
    """``h_bucket`` as 32 lowercase hex characters, the storage form."""
    return h_bucket(index, entries).hex()


def group_entries_by_bucket(
    entries: Iterable[tuple[bytes, bytes]]
) -> dict[int, list[tuple[bytes, bytes]]]:
    """Group ``(identity_key_bytes, h_row)`` pairs by ``bucket_of(key)``."""
    grouped: dict[int, list[tuple[bytes, bytes]]] = {}
    for key, digest in entries:
        grouped.setdefault(bucket_of(key), []).append((key, digest))
    return grouped


def empty_bucket_hashes() -> list[bytes]:
    """The 256 digests of a dataset with no rows at all.

    Useful as a constant-time reference: a dataset whose bucket hashes equal
    this list read as empty, which the ``EMPTY_TAB`` guard treats as a
    mass-delete *signal*, never as "all rows were deleted".
    """
    return [h_bucket(i, []) for i in range(BUCKET_COUNT)]


def compute_bucket_hashes(entries: Iterable[tuple[bytes, bytes]]) -> list[bytes]:
    """Compute all 256 bucket digests from a flat entry list.

    Every bucket is present in the result, empty ones included, in index order
    -- which is exactly the order ``h_dataset`` concatenates them in.
    """
    grouped = group_entries_by_bucket(entries)
    return [h_bucket(i, grouped.get(i, ())) for i in range(BUCKET_COUNT)]


def h_dataset(
    bucket_hashes: Sequence[bytes], spec_version: str, tab_uid: str, total_rows: int
) -> bytes:
    """32-byte digest of a whole dataset (one spreadsheet tab).

    Args:
        bucket_hashes: exactly 256 digests, buckets 0..255 **in index order**.
        spec_version: binds the digest to the normalizer and the contract.
        tab_uid: exactly ``"%s/%d" % (node.google_id, dataset.sheet_gid)``, e.g.
            ``1abcDEF/0``.  This ties the hash to a specific tab of a specific
            Drive file, so a digest can never be accidentally reused across the
            two files both titled ``Bettr_Bowl_Data_Request`` -- titles are
            display strings, file ids are identity.
        total_rows: the row count, committed to independently of the buckets so
            that a truncated read cannot produce a matching hash.

    Raises:
        ValueError: if ``bucket_hashes`` is not exactly 256 entries.  A short
            list is a lane-E bug and hashing it anyway would produce a
            plausible-looking digest over an incomplete dataset.
    """
    if len(bucket_hashes) != BUCKET_COUNT:
        raise ValueError(
            "h_dataset() expects exactly %d bucket hashes, got %d"
            % (BUCKET_COUNT, len(bucket_hashes))
        )
    if total_rows < 0:
        raise ValueError("total_rows must be non-negative, got %d" % (total_rows,))
    preimage = (
        DS_PREFIX
        + spec_version.encode("utf-8")
        + b"\x00"
        + tab_uid.encode("utf-8")
        + b"\x00"
        + varint(total_rows)
        + b"".join(bucket_hashes)
    )
    return H(preimage)


def h_dataset_hex(
    bucket_hashes: Sequence[bytes], spec_version: str, tab_uid: str, total_rows: int
) -> str:
    """``h_dataset`` as 64 lowercase hex characters.

    This is the storage form for ``gdrive.dataset.h_dataset_sheet`` and
    ``h_dataset_odoo``.
    """
    return h_dataset(bucket_hashes, spec_version, tab_uid, total_rows).hex()


def dataset_digest(
    entries: Iterable[tuple[bytes, bytes]], spec_version: str, tab_uid: str
) -> tuple[str, list[str]]:
    """One-call rollup: ``(h_dataset_hex, [256 bucket hex strings])``.

    ``total_rows`` is taken from the entry list itself rather than passed in,
    so the count committed to in the hash can never disagree with the rows
    actually hashed.  Lane E stores the bucket list in
    ``gdrive.dataset.bucket_hashes`` (a Json field -- ~4 KB, never queried,
    which is exactly what Json is for).
    """
    materialized = list(entries)
    buckets = compute_bucket_hashes(materialized)
    digest = h_dataset_hex(buckets, spec_version, tab_uid, len(materialized))
    return digest, [b.hex() for b in buckets]


def diff_buckets(a: Sequence, b: Sequence) -> list[int]:
    """Indices where two bucket-hash lists disagree.

    Accepts either ``bytes`` digests or their hex strings on either side, since
    one list typically comes from a live computation and the other from the
    Json column.  Comparison is on the normalized hex form, so a ``bytes`` and
    a ``str`` representation of the same digest correctly compare equal.

    Raises:
        ValueError: if the two lists differ in length -- comparing a 256-bucket
            list against a truncated one would silently report only the
            overlapping prefix as differing.
    """
    if len(a) != len(b):
        raise ValueError(
            "diff_buckets() needs equal-length lists, got %d and %d" % (len(a), len(b))
        )

    def norm(value) -> str:
        return value.hex() if isinstance(value, (bytes, bytearray)) else str(value).lower()

    return [i for i in range(len(a)) if norm(a[i]) != norm(b[i])]
