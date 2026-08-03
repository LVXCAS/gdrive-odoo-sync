# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. Licensed under LGPL-3.
"""Lane C — the bucketed Merkle rollup (``docs/CANONICALIZATION.md`` §10, §11.3).

WHY order-insensitivity is the headline property
================================================
A user sorting a spreadsheet is not a data change. A verification system that
reports 5 000 drifts the morning after somebody clicked "Sort A→Z" is switched
off within a week, and everything it would have caught afterwards is lost.
Order-insensitivity is therefore not a nicety here — it is what makes the
product survivable — and it is achieved structurally, by sorting on the
canonical identity **bytes** rather than by hoping the two sides iterate in the
same order.

The second property is **locality**: one changed cell must perturb exactly one
bucket, so a drill-down materializes ~0.4 % of the rows rather than all of them.
Losing locality does not produce wrong answers; it produces a verification pass
that is too slow to run nightly, which produces no verification at all.

The third is **binding**: the digest commits to the tab it came from
(``tab_uid``) and to the row count, so a truncated read cannot accidentally
match, and a digest can never be reused across the two Drive files that share
the title ``Bettr_Bowl_Data_Request``.
"""

import random

from odoo.tests.common import BaseCase

from odoo.addons.gdrive_odoo_sync.lib.hashing import (
    bucket_of,
    h_row,
    identity_key_bytes,
)
from odoo.addons.gdrive_odoo_sync.lib.merkle import (
    BUCKET_COUNT,
    compute_bucket_hashes,
    dataset_digest,
    diff_buckets,
    empty_bucket_hashes,
    group_entries_by_bucket,
    h_bucket,
    h_dataset,
)

SV = "SPECV1"
TAB_UID = "1abcDEF/0"

# docs/CANONICALIZATION.md §11.3
GV_BUCKET_7_TWO_ENTRIES = "272b8595d58db43eadea1ab472b2b75a"
GV_BUCKET_0_EMPTY = "4680758652c5d246986a923fa30b2fcc"
GV_BUCKET_7_EMPTY = "cae3965449686a04c97d50150cbe08d8"
GV_DATASET = "485a5101901b8f2ccf4f42f02447320f7714a953ccf367197e6afdddc8d308c4"

ROW_ACME = {"amount": "n:1234.50", "name": "s:ACME Foods"}
ROW_BETTR = {"amount": "n:0.00", "name": "s:Bettr Bowl"}


def _entry(name, row):
    """Build one ``(identity_key_bytes, h_row)`` entry from a canonical row."""
    return identity_key_bytes([name]), h_row(row, SV)


def _corpus(n=500):
    """A deterministic synthetic dataset of ``n`` rows."""
    entries = []
    for i in range(n):
        key = "s:CUST-%05d" % i
        row = {"name": "s:Customer %d" % i, "amount": "n:%d.00" % (i * 7 % 991)}
        entries.append((identity_key_bytes([key]), h_row(row, SV)))
    return entries


class TestBucketGoldenVectors(BaseCase):
    """§11.3 — literal digests from the specification."""

    def test_bucket_with_two_entries(self):
        entries = [_entry("s:ACME Foods", ROW_ACME), _entry("s:Bettr Bowl", ROW_BETTR)]
        self.assertEqual(h_bucket(7, entries).hex(), GV_BUCKET_7_TWO_ENTRIES)

    def test_entry_order_within_a_bucket_is_irrelevant(self):
        a = [_entry("s:ACME Foods", ROW_ACME), _entry("s:Bettr Bowl", ROW_BETTR)]
        b = list(reversed(a))
        self.assertEqual(h_bucket(7, a), h_bucket(7, b))
        self.assertEqual(h_bucket(7, b).hex(), GV_BUCKET_7_TWO_ENTRIES)

    def test_empty_buckets_have_distinct_digests(self):
        self.assertEqual(h_bucket(0, []).hex(), GV_BUCKET_0_EMPTY)
        self.assertEqual(h_bucket(7, []).hex(), GV_BUCKET_7_EMPTY)
        self.assertNotEqual(GV_BUCKET_0_EMPTY, GV_BUCKET_7_EMPTY)

    def test_index_is_mixed_into_the_preimage(self):
        # Otherwise shifting every row one bucket along could leave the
        # concatenation of the 256 digests unchanged.
        entries = [_entry("s:ACME Foods", ROW_ACME)]
        self.assertNotEqual(h_bucket(7, entries), h_bucket(8, entries))

    def test_index_out_of_range_is_refused(self):
        for bad in (-1, 256, 1000):
            with self.subTest(index=bad):
                with self.assertRaises(ValueError):
                    h_bucket(bad, [])

    def test_bucket_digest_is_sixteen_bytes(self):
        self.assertEqual(len(h_bucket(0, [])), 16)


class TestDatasetGoldenVector(BaseCase):
    """§11.3 — the two-row dataset digest."""

    def _two_row_buckets(self):
        buckets = empty_bucket_hashes()
        entries = [_entry("s:ACME Foods", ROW_ACME), _entry("s:Bettr Bowl", ROW_BETTR)]
        buckets[7] = h_bucket(7, entries)
        return buckets

    def test_dataset_digest(self):
        self.assertEqual(
            h_dataset(self._two_row_buckets(), SV, TAB_UID, 2).hex(), GV_DATASET
        )

    def test_dataset_digest_is_thirty_two_bytes(self):
        self.assertEqual(len(h_dataset(empty_bucket_hashes(), SV, TAB_UID, 0)), 32)

    def test_tab_uid_binds_the_digest_to_one_tab_of_one_file(self):
        buckets = self._two_row_buckets()
        self.assertNotEqual(
            h_dataset(buckets, SV, "1abcDEF/0", 2),
            h_dataset(buckets, SV, "1abcDEF/1", 2),
        )
        self.assertNotEqual(
            h_dataset(buckets, SV, "1abcDEF/0", 2),
            h_dataset(buckets, SV, "9zzzXYZ/0", 2),
        )

    def test_row_count_is_committed_to_independently(self):
        # A truncated read that happens to leave the same bucket digests must
        # still not produce a matching dataset hash.
        buckets = self._two_row_buckets()
        self.assertNotEqual(
            h_dataset(buckets, SV, TAB_UID, 2), h_dataset(buckets, SV, TAB_UID, 1)
        )

    def test_spec_version_binds_the_digest(self):
        buckets = self._two_row_buckets()
        self.assertNotEqual(
            h_dataset(buckets, SV, TAB_UID, 2),
            h_dataset(buckets, "SPECV2", TAB_UID, 2),
        )

    def test_short_bucket_list_is_refused(self):
        with self.assertRaises(ValueError):
            h_dataset(empty_bucket_hashes()[:255], SV, TAB_UID, 0)

    def test_negative_row_count_is_refused(self):
        with self.assertRaises(ValueError):
            h_dataset(empty_bucket_hashes(), SV, TAB_UID, -1)


class TestOrderInsensitivity(BaseCase):
    """§12 invariant 5 — sorting the sheet must be a no-op."""

    def test_shuffled_and_sorted_datasets_agree(self):
        entries = _corpus(500)
        shuffled = list(entries)
        random.Random(20260728).shuffle(shuffled)
        self.assertNotEqual(entries, shuffled, "the shuffle must actually reorder")

        digest_a, buckets_a = dataset_digest(entries, SV, TAB_UID)
        digest_b, buckets_b = dataset_digest(shuffled, SV, TAB_UID)
        self.assertEqual(digest_a, digest_b)
        self.assertEqual(buckets_a, buckets_b)

    def test_reversal_is_also_a_no_op(self):
        entries = _corpus(120)
        self.assertEqual(
            dataset_digest(entries, SV, TAB_UID)[0],
            dataset_digest(list(reversed(entries)), SV, TAB_UID)[0],
        )

    def test_many_independent_shuffles_all_agree(self):
        entries = _corpus(200)
        reference = dataset_digest(entries, SV, TAB_UID)[0]
        rng = random.Random(7)
        for _ in range(10):
            shuffled = list(entries)
            rng.shuffle(shuffled)
            self.assertEqual(dataset_digest(shuffled, SV, TAB_UID)[0], reference)


class TestLocality(BaseCase):
    """§12 invariant 7 — one changed cell perturbs exactly one bucket."""

    def test_single_cell_edit_moves_exactly_one_bucket(self):
        entries = _corpus(500)
        before = compute_bucket_hashes(entries)

        # Edit row 42's amount. Its identity is unchanged, so it stays in the
        # same bucket and only that bucket's digest may move.
        key = identity_key_bytes(["s:CUST-00042"])
        edited = [
            (k, h_row({"name": "s:Customer 42", "amount": "n:999.00"}, SV) if k == key else d)
            for k, d in entries
        ]
        after = compute_bucket_hashes(edited)

        differing = diff_buckets(before, after)
        self.assertEqual(len(differing), 1)
        self.assertEqual(differing[0], bucket_of(key))

    def test_two_edits_in_different_buckets_move_two_buckets(self):
        entries = _corpus(500)
        before = compute_bucket_hashes(entries)
        # Pick two rows that genuinely land in different buckets rather than
        # assuming it; the mapping is a digest and is not ours to predict.
        k1 = entries[0][0]
        k2 = next(k for k, _ in entries[1:] if bucket_of(k) != bucket_of(k1))

        edited = []
        for k, d in entries:
            if k in (k1, k2):
                edited.append((k, h_row({"name": "s:changed", "amount": "n:1.00"}, SV)))
            else:
                edited.append((k, d))
        after = compute_bucket_hashes(edited)
        self.assertEqual(sorted(diff_buckets(before, after)), sorted([bucket_of(k1), bucket_of(k2)]))

    def test_deleting_a_row_moves_only_its_bucket(self):
        entries = _corpus(300)
        key = identity_key_bytes(["s:CUST-00100"])
        before = compute_bucket_hashes(entries)
        after = compute_bucket_hashes([e for e in entries if e[0] != key])
        self.assertEqual(diff_buckets(before, after), [bucket_of(key)])

    def test_identical_datasets_differ_nowhere(self):
        entries = _corpus(300)
        self.assertEqual(
            diff_buckets(compute_bucket_hashes(entries), compute_bucket_hashes(entries)), []
        )

    def test_drill_down_cost_is_a_small_fraction_of_the_dataset(self):
        # The design claim being protected: a single-row difference should make
        # roughly 1/256 of the rows worth materializing, not all of them.
        entries = _corpus(2560)
        key = entries[1234][0]
        after = [(k, h_row({"name": "s:x"}, SV) if k == key else d) for k, d in entries]
        differing = set(diff_buckets(compute_bucket_hashes(entries), compute_bucket_hashes(after)))
        grouped = group_entries_by_bucket(entries)
        to_materialize = sum(len(grouped.get(i, ())) for i in differing)
        self.assertLess(to_materialize, len(entries) // 10)


class TestBucketHashesShape(BaseCase):
    """The bucket list is always fully populated, in index order."""

    def test_always_256_entries(self):
        self.assertEqual(len(empty_bucket_hashes()), BUCKET_COUNT)
        self.assertEqual(len(compute_bucket_hashes(_corpus(10))), BUCKET_COUNT)

    def test_empty_dataset_matches_the_empty_reference(self):
        self.assertEqual(compute_bucket_hashes([]), empty_bucket_hashes())

    def test_grouping_agrees_with_bucket_of(self):
        entries = _corpus(200)
        grouped = group_entries_by_bucket(entries)
        for index, bucket_entries in grouped.items():
            for key, _digest in bucket_entries:
                self.assertEqual(bucket_of(key), index)

    def test_dataset_digest_round_trips_through_hex(self):
        entries = _corpus(50)
        digest, buckets = dataset_digest(entries, SV, TAB_UID)
        self.assertEqual(len(digest), 64)
        self.assertEqual(len(buckets), BUCKET_COUNT)
        self.assertTrue(all(len(b) == 32 for b in buckets))
        self.assertEqual(
            h_dataset([bytes.fromhex(b) for b in buckets], SV, TAB_UID, len(entries)).hex(),
            digest,
        )


class TestDiffBuckets(BaseCase):
    """The drill-down localizer must never silently compare unequal lists."""

    def test_accepts_bytes_and_hex_on_either_side(self):
        a = compute_bucket_hashes(_corpus(30))
        b_hex = [x.hex() for x in a]
        self.assertEqual(diff_buckets(a, b_hex), [])
        self.assertEqual(diff_buckets(b_hex, a), [])

    def test_case_insensitive_hex_comparison(self):
        a = compute_bucket_hashes(_corpus(5))
        self.assertEqual(diff_buckets([x.hex().upper() for x in a], a), [])

    def test_length_mismatch_is_refused(self):
        a = empty_bucket_hashes()
        with self.assertRaises(ValueError):
            diff_buckets(a, a[:100])


class TestIndependentControls(BaseCase):
    """§9.2 — row counts behave independently of the hash."""

    def test_row_count_disagreement_is_visible_without_the_hash(self):
        entries = _corpus(100)
        fewer = entries[:99]
        # Bucket hashes localize *where*; the counts alone already say *that*.
        self.assertNotEqual(len(entries), len(fewer))
        self.assertNotEqual(
            dataset_digest(entries, SV, TAB_UID)[0], dataset_digest(fewer, SV, TAB_UID)[0]
        )

    def test_equal_counts_do_not_imply_equal_hashes(self):
        entries = _corpus(100)
        mutated = [(k, h_row({"name": "s:mutated"}, SV)) for k, _ in entries]
        self.assertEqual(len(entries), len(mutated))
        self.assertNotEqual(
            dataset_digest(entries, SV, TAB_UID)[0], dataset_digest(mutated, SV, TAB_UID)[0]
        )
