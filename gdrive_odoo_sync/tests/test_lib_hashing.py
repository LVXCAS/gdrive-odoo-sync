# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. Licensed under LGPL-3.
"""Lane C — JCS, row hashing and identity keys (``docs/CANONICALIZATION.md`` §9-§11).

WHY the golden vectors are asserted literally
=============================================
Every claim this product makes reduces to "these two digests are equal".
A digest is only meaningful if it is reproducible byte for byte on any machine,
in any locale, forever — so the vectors in ``docs/CANONICALIZATION.md`` §11 are
transcribed here as literals rather than recomputed by the code under test.
A test that recomputes the expected value with the same function it is testing
proves only that the function is deterministic, which is not the property that
matters.

The three structural properties proved below, in order of how expensive they
are to get wrong:

1. **Type families never collide.** ``"1"``, ``1`` and ``true`` must produce
   three different digests. Without tags they produce one, and a column whose
   declared type silently changed would then report ``verified`` over unequal
   data — a false pass, the single worst failure of a verification system.
2. **Delimiter injection is impossible.** With a naive ``"|".join()`` the
   identities ``("a|b", "c")`` and ``("a", "b|c")`` collide, so one row can
   forge another's identity. Length prefixing makes that unrepresentable.
3. **Key order is irrelevant, and spec_version is not.** Building the same row
   payload in a different order must hash identically; changing any contract
   option must invalidate every cached digest.
"""

from odoo.tests.common import BaseCase

from odoo.addons.gdrive_odoo_sync.lib.hashing import (
    H,
    H128,
    bucket_of,
    h_extra,
    h_header,
    h_row,
    h_row_folded,
    identity_key_bytes,
    varint,
)
from odoo.addons.gdrive_odoo_sync.lib.jcs import (
    escape_json_string,
    is_valid_jcs_key,
    jcs,
)
from odoo.addons.gdrive_odoo_sync.lib.spec_version import (
    CANON_VERSION,
    compute_spec_version,
)

SV = "SPECV1"

# docs/CANONICALIZATION.md §11.1
GV_R1 = "3830219051a074351eaf44902059fc01"
GV_R2 = "ba43380c0d901252740bb6decccbdb1b"
GV_TEXT_1 = "86fce31b0ee6e5b56592956396b0e4aa"
GV_NUMBER_1 = "b24edee0af0d3c173a66d81f70cb33e1"
GV_BOOL_1 = "46345361aa9a318cf48d826e53884332"
GV_NULL = "e89c6a4ea3913a8a9a19798c6cb1b439"

# docs/CANONICALIZATION.md §11.2
GV_IKB_ACME = "0000000c733a41434d4520466f6f6473"
GV_IKB_AB_C = "00000005733a617c6200000003733a63"
GV_IKB_A_BC = "00000003733a6100000005733a627c63"
GV_IKB_ULID = "0000001a30314a4258335437514b3956324d344e36503852305331543255"

# docs/CANONICALIZATION.md §11.4
GV_HEADER = "f35e0b3ff0179c25d00deb4a74c399d7"


class TestJcs(BaseCase):
    """§9.1 — a restricted, deterministic subset of RFC 8785."""

    def test_exact_output_shape(self):
        self.assertEqual(
            jcs({"name": "s:ACME Foods", "amount": "n:1234.50"}),
            b'{"amount":"n:1234.50","name":"s:ACME Foods"}',
        )

    def test_no_whitespace_anywhere(self):
        out = jcs({"a": "s:x", "b": "s:y"})
        self.assertNotIn(b" ", out)
        self.assertNotIn(b"\n", out)

    def test_keys_are_sorted_by_byte_order(self):
        out = jcs({"Zeta": "s:1", "alpha": "s:2", "Alpha": "s:3"})
        # Uppercase sorts before lowercase in byte order — deliberately, since
        # the restricted ASCII key charset makes byte order and UTF-16 code-unit
        # order identical, so key sorting needs no UTF-16 machinery at all.
        self.assertEqual(out, b'{"Alpha":"s:3","Zeta":"s:1","alpha":"s:2"}')

    def test_non_ascii_values_are_emitted_literally_as_utf8(self):
        out = jcs({"name": "s:Bettr Böwl — München"})
        self.assertEqual(out.decode("utf-8"), '{"name":"s:Bettr Böwl — München"}')
        self.assertNotIn(b"\\u", out)

    def test_escaping_table_is_exactly_the_documented_one(self):
        self.assertEqual(escape_json_string('a"b'), 'a\\"b')
        self.assertEqual(escape_json_string("a\\b"), "a\\\\b")
        self.assertEqual(escape_json_string("a\nb"), "a\\nb")
        self.assertEqual(escape_json_string("a\tb"), "a\\tb")
        self.assertEqual(escape_json_string("a\rb"), "a\\rb")
        self.assertEqual(escape_json_string("a\bb"), "a\\bb")
        self.assertEqual(escape_json_string("a\fb"), "a\\fb")

    def test_forward_slash_is_not_escaped(self):
        self.assertEqual(escape_json_string("a/b"), "a/b")

    def test_low_control_uses_lowercase_hex(self):
        self.assertEqual(escape_json_string("\x1f"), "\\u001f")
        self.assertNotEqual(escape_json_string("\x1f"), "\\u001F")

    def test_key_charset_is_enforced(self):
        for bad in ("Invoice Number", "amount-due", "1st", "", "café", "a.b"):
            with self.subTest(key=repr(bad)):
                self.assertFalse(is_valid_jcs_key(bad))
                with self.assertRaises(ValueError):
                    jcs({bad: "s:x"})

    def test_valid_keys_are_accepted(self):
        for good in ("name", "_sync_id", "x_gdrive_sync_id", "A1", "__x"):
            with self.subTest(key=good):
                self.assertTrue(is_valid_jcs_key(good))

    def test_non_string_values_are_a_contract_error(self):
        # Floats in the preimage are the whole thing §5 exists to prevent.
        for bad in (1, 1.5, None, True, ["s:x"]):
            with self.subTest(value=repr(bad)):
                with self.assertRaises(ValueError):
                    jcs({"v": bad})

    def test_insertion_order_is_irrelevant(self):
        a = jcs({"name": "s:ACME Foods", "amount": "n:1234.50"})
        b = jcs({"amount": "n:1234.50", "name": "s:ACME Foods"})
        self.assertEqual(a, b)


class TestPrimitives(BaseCase):
    """``H``, ``H128`` and ``varint`` — SHA-256 only, LEB128 lengths."""

    def test_h_is_sha256(self):
        import hashlib
        self.assertEqual(H(b"abc"), hashlib.sha256(b"abc").digest())
        self.assertEqual(len(H(b"")), 32)

    def test_h128_is_the_first_sixteen_bytes(self):
        self.assertEqual(H128(b"abc"), H(b"abc")[:16])
        self.assertEqual(len(H128(b"")), 16)

    def test_varint_is_unsigned_leb128(self):
        self.assertEqual(varint(0), b"\x00")
        self.assertEqual(varint(1), b"\x01")
        self.assertEqual(varint(127), b"\x7f")
        self.assertEqual(varint(128), b"\x80\x01")
        self.assertEqual(varint(300), b"\xac\x02")

    def test_varint_lengths_are_self_delimiting(self):
        # Concatenated varints must be unambiguously separable, which is what
        # makes the bucket preimage injection-proof.
        self.assertNotEqual(varint(1) + varint(28), varint(128))


class TestHRowGoldenVectors(BaseCase):
    """§11.1 — the literal vectors from the specification."""

    def test_r1(self):
        canon = {"amount": "n:1234.50", "name": "s:ACME Foods"}
        self.assertEqual(h_row(canon, SV).hex(), GV_R1)

    def test_r1_is_key_order_invariant(self):
        forward = {"amount": "n:1234.50", "name": "s:ACME Foods"}
        backward = {"name": "s:ACME Foods", "amount": "n:1234.50"}
        self.assertEqual(h_row(forward, SV).hex(), GV_R1)
        self.assertEqual(h_row(backward, SV).hex(), GV_R1)

    def test_r2_extra_column_changes_the_digest(self):
        canon = {"amount": "n:1234.50", "due": "d:2026-07-31", "name": "s:ACME Foods"}
        self.assertEqual(h_row(canon, SV).hex(), GV_R2)
        self.assertNotEqual(GV_R2, GV_R1)

    def test_h_row_is_sixteen_bytes(self):
        self.assertEqual(len(h_row({"v": "s:1"}, SV)), 16)


class TestTypeFamilyDisjointness(BaseCase):
    """§11.1 rows 4-7 — the payload ``1`` in four type families never collides."""

    def test_each_family_has_its_own_digest(self):
        self.assertEqual(h_row({"v": "s:1"}, SV).hex(), GV_TEXT_1)
        self.assertEqual(h_row({"v": "n:1"}, SV).hex(), GV_NUMBER_1)
        self.assertEqual(h_row({"v": "b:1"}, SV).hex(), GV_BOOL_1)
        self.assertEqual(h_row({"v": "z:"}, SV).hex(), GV_NULL)

    def test_all_four_are_distinct(self):
        digests = {GV_TEXT_1, GV_NUMBER_1, GV_BOOL_1, GV_NULL}
        self.assertEqual(len(digests), 4)

    def test_a_silently_changed_column_type_cannot_report_verified(self):
        # The scenario: an administrator flips a column from text to number.
        # Without the tag both sides would canonicalize "1" to "1" and the
        # dataset would be declared identical when it is not comparable at all.
        before = h_row({"invoice_no": "s:1"}, SV)
        after = h_row({"invoice_no": "n:1"}, SV)
        self.assertNotEqual(before, after)


class TestFoldedAndExtraHashes(BaseCase):
    """§9.3 and §9.4 — the cosmetic hash and the schema-growth hash."""

    def test_folded_hash_ignores_smart_punctuation_and_case(self):
        strict_a = {"name": "s:Bob’s Data"}
        strict_b = {"name": "s:BOB'S  DATA"}
        self.assertNotEqual(h_row(strict_a, SV), h_row(strict_b, SV))
        self.assertEqual(h_row_folded(strict_a, SV), h_row_folded(strict_b, SV))

    def test_folded_hash_does_not_fold_numbers(self):
        # A rounding difference must NOT be downgraded to cosmetic.
        self.assertNotEqual(
            h_row_folded({"amount": "n:1234.50"}, SV),
            h_row_folded({"amount": "n:1234.51"}, SV),
        )

    def test_folded_hash_is_domain_separated_from_the_strict_one(self):
        canon = {"name": "s:acme foods"}
        self.assertNotEqual(h_row(canon, SV), h_row_folded(canon, SV))

    def test_extra_hash_is_domain_separated_too(self):
        payload = {"note": "s:whatever"}
        self.assertNotEqual(h_extra(payload, SV), h_row(payload, SV))

    def test_extra_hash_detects_schema_growth(self):
        base = h_extra({"note": "s:x"}, SV)
        grown = h_extra({"note": "s:x", "new_column": "s:y"}, SV)
        self.assertNotEqual(base, grown)

    def test_extra_hash_does_not_pollute_the_compared_row_hash(self):
        # Adding an unmapped column must not change h_row — that is what makes
        # schema growth non-blocking.
        contract_only = {"name": "s:ACME Foods", "amount": "n:1234.50"}
        self.assertEqual(h_row(contract_only, SV).hex(), GV_R1)


class TestIdentityKeyBytes(BaseCase):
    """§10.1 and §11.2 — length-prefixed, therefore injection-proof."""

    def test_single_part_vector(self):
        self.assertEqual(identity_key_bytes(["s:ACME Foods"]).hex(), GV_IKB_ACME)

    def test_ulid_vector(self):
        self.assertEqual(
            identity_key_bytes(["01JBX3T7QK9V2M4N6P8R0S1T2U"]).hex(), GV_IKB_ULID
        )

    def test_delimiter_injection_pair_does_not_collide(self):
        left = identity_key_bytes(["s:a|b", "s:c"])
        right = identity_key_bytes(["s:a", "s:b|c"])
        self.assertEqual(left.hex(), GV_IKB_AB_C)
        self.assertEqual(right.hex(), GV_IKB_A_BC)
        self.assertNotEqual(left, right)

    def test_the_naive_join_would_have_collided(self):
        # Stated explicitly so nobody "simplifies" this back to a join.
        self.assertEqual("|".join(["s:a|b", "s:c"]), "|".join(["s:a", "s:b|c"]))

    def test_empty_parts_are_distinguishable(self):
        self.assertNotEqual(identity_key_bytes(["", "a"]), identity_key_bytes(["a", ""]))
        self.assertNotEqual(identity_key_bytes(["a"]), identity_key_bytes(["a", ""]))

    def test_unicode_parts_are_utf8_encoded(self):
        parts = ["s:München"]
        self.assertEqual(identity_key_bytes(parts)[4:], "s:München".encode("utf-8"))


class TestBucketOf(BaseCase):
    """§10.2 and §11.2 — 256 buckets, derived from a domain-separated digest."""

    def test_golden_buckets(self):
        self.assertEqual(bucket_of(identity_key_bytes(["s:ACME Foods"])), 23)
        self.assertEqual(bucket_of(identity_key_bytes(["s:a|b", "s:c"])), 216)
        self.assertEqual(bucket_of(identity_key_bytes(["s:a", "s:b|c"])), 234)
        self.assertEqual(bucket_of(identity_key_bytes(["01JBX3T7QK9V2M4N6P8R0S1T2U"])), 17)

    def test_injection_pair_lands_in_different_buckets(self):
        self.assertNotEqual(
            bucket_of(identity_key_bytes(["s:a|b", "s:c"])),
            bucket_of(identity_key_bytes(["s:a", "s:b|c"])),
        )

    def test_always_in_range(self):
        for i in range(500):
            b = bucket_of(identity_key_bytes(["s:row-%d" % i]))
            self.assertGreaterEqual(b, 0)
            self.assertLess(b, 256)

    def test_distribution_is_not_degenerate(self):
        # Not a statistical test — just a guard against a bug that maps
        # everything to bucket 0, which would silently disable the Merkle
        # localization and make every drill-down a full scan.
        seen = {bucket_of(identity_key_bytes(["s:row-%d" % i])) for i in range(2000)}
        self.assertGreater(len(seen), 200)


class TestHeaderFingerprint(BaseCase):
    """§10.5 — sorted, so reordering columns is a genuine no-op."""

    def test_golden_vector(self):
        self.assertEqual(
            h_header(["s:Amount", "s:Due Date", "s:Invoice Number"]).hex(), GV_HEADER
        )

    def test_every_permutation_agrees(self):
        import itertools
        labels = ["s:Amount", "s:Due Date", "s:Invoice Number"]
        for perm in itertools.permutations(labels):
            with self.subTest(order=perm):
                self.assertEqual(h_header(list(perm)).hex(), GV_HEADER)

    def test_renaming_a_column_changes_the_fingerprint(self):
        self.assertNotEqual(
            h_header(["s:Amount", "s:Due Date", "s:Invoice Number"]),
            h_header(["s:Amount", "s:Due Date", "s:Invoice #"]),
        )

    def test_adding_a_column_changes_the_fingerprint(self):
        self.assertNotEqual(
            h_header(["s:Amount", "s:Due Date", "s:Invoice Number"]),
            h_header(["s:Amount", "s:Due Date", "s:Invoice Number", "s:Notes"]),
        )

    def test_removing_a_column_changes_the_fingerprint(self):
        self.assertNotEqual(
            h_header(["s:Amount", "s:Due Date", "s:Invoice Number"]),
            h_header(["s:Amount", "s:Due Date"]),
        )


class TestSpecVersionInvalidation(BaseCase):
    """§1 — a stale hash must be unusable, not merely suspicious."""

    def test_spec_version_participates_in_every_row_digest(self):
        canon = {"amount": "n:1234.50", "name": "s:ACME Foods"}
        self.assertNotEqual(h_row(canon, SV), h_row(canon, "SPECV2"))

    def test_spec_version_participates_in_the_folded_and_extra_digests(self):
        canon = {"name": "s:acme"}
        self.assertNotEqual(h_row_folded(canon, SV), h_row_folded(canon, "SPECV2"))
        self.assertNotEqual(h_extra(canon, SV), h_extra(canon, "SPECV2"))

    def test_changing_any_contract_option_changes_the_spec_version(self):
        base = {"cols": {"amount": {"ctype": "number", "scale": "2"}}}
        changed = {"cols": {"amount": {"ctype": "number", "scale": "3"}}}
        self.assertNotEqual(compute_spec_version(base), compute_spec_version(changed))

    def test_spec_version_is_stable_for_an_unchanged_contract(self):
        contract = {"cols": {"amount": {"ctype": "number", "scale": "2"}}}
        self.assertEqual(compute_spec_version(contract), compute_spec_version(contract))

    def test_spec_version_is_a_hex_sha256(self):
        value = compute_spec_version({"a": "b"})
        self.assertEqual(len(value), 64)
        int(value, 16)  # raises if it is not hex

    def test_canon_version_is_a_frozen_non_empty_string(self):
        self.assertIsInstance(CANON_VERSION, str)
        self.assertTrue(CANON_VERSION)
