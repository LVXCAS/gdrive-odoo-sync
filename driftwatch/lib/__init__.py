# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. Licensed under LGPL-3.
"""Canonicalization and hashing library — the module's *pure* core.

This package is the single implementation of ``docs/CANONICALIZATION.md``.

WHY it is quarantined from the rest of the addon
------------------------------------------------
Everything in here is deliberately dependency-free: standard library only, no
``import odoo``, no ``googleapiclient``, no reference to the ``services``
package. Three concrete payoffs justify the discipline:

* **Byte-reproducibility.** A verification system that says "these two datasets
  are identical" is only trustworthy if the same inputs always produce the same
  digest, on any machine, in any Odoo version, forever. Pure functions over
  stdlib primitives are the only way to promise that. The moment a hash depends
  on an ORM default, a server timezone, or a third-party library's version, a
  library upgrade silently reclassifies every dataset as drifted.
* **Testability outside Odoo.** ``python -m pytest gdrive_odoo_sync/tests`` on
  the lane C tests needs no database, no registry, and no network, so the
  hostile-corpus and property-based tests can run in a fraction of a second and
  therefore actually get run.
* **Auditability.** The golden vectors in ``docs/CANONICALIZATION.md`` §11 can
  be reproduced by hand against this package alone.

WHY this file re-exports at all
-------------------------------
Callers in lanes D and E should import from ``..lib`` and not from
``..lib.hashing`` / ``..lib.text_canon`` / …. That keeps the internal file
layout free to change (splitting ``canon.py``, for instance) without touching
twenty call sites, and it gives one obvious place to read what the public
surface actually *is*.

Anything not named in ``__all__`` is internal. Do not import it from outside
this package.
"""

from .spec_version import CANON_VERSION, compute_spec_version
from .tokens import is_error, tag_of
from .contract import (
    ColumnContract,
    contract_from_mapping_dict,
    slugify,
    validate_contract,
)
from .text_canon import TEXT_CANON, fold_punct
from .number_canon import NUM_CANON, near_boundary, raw_decimal
from .datetime_canon import DATE_CANON, DATETIME_CANON, serial_to_naive
from .bool_canon import BOOL_CANON
from .canon import CANON
from .jcs import jcs
from .hashing import (
    H,
    H128,
    bucket_of,
    h_extra,
    h_header,
    h_row,
    h_row_folded,
    identity_key_bytes,
)
from .merkle import diff_buckets, h_bucket, h_dataset
from .ulid import is_ulid, new_ulid

__all__ = [
    # Versioning / cache invalidation
    'CANON_VERSION',
    'compute_spec_version',
    # Token vocabulary
    'is_error',
    'tag_of',
    # Column contract
    'ColumnContract',
    'contract_from_mapping_dict',
    'slugify',
    'validate_contract',
    # Per-type canonicalizers
    'CANON',
    'TEXT_CANON',
    'NUM_CANON',
    'DATE_CANON',
    'DATETIME_CANON',
    'BOOL_CANON',
    'fold_punct',
    'near_boundary',
    'raw_decimal',
    'serial_to_naive',
    # Serialization + hashing
    'jcs',
    'H',
    'H128',
    'h_row',
    'h_row_folded',
    'h_extra',
    'h_header',
    'identity_key_bytes',
    'bucket_of',
    # Merkle rollup
    'h_bucket',
    'h_dataset',
    'diff_buckets',
    # Identity
    'new_ulid',
    'is_ulid',
]
