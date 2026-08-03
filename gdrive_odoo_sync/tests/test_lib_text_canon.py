# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. Licensed under LGPL-3.
"""Lane C — ``TEXT_CANON``: the ten ordered steps of ``docs/CANONICALIZATION.md`` §4.

WHY these particular cases
==========================
Text canonicalization is where "two cells a human reads as identical" is turned
into "two byte strings that hash identically". Every case below is a real,
observed way that promise gets broken:

* a BOM prepended by a CSV export tool,
* a zero-width space pasted out of a web page,
* an NBSP that Google Sheets emits as a thousands separator,
* a decomposed ``é`` typed on macOS versus a composed one typed on Windows,
* a trailing space nobody can see.

And one case that must **not** be "fixed": NFKC would fold ``㎡`` to ``m2`` and
``①`` to ``1``. That is genuine information loss, so the algorithm specifies
NFC and this module asserts NFKC folding does not happen.

The order of the steps is load-bearing and is asserted directly: stripping
invisibles *before* normalizing is what lets a decomposed sequence separated by
an intervening ZWSP compose correctly.
"""

import unicodedata

from odoo.tests.common import BaseCase

from odoo.addons.gdrive_odoo_sync.lib.contract import ColumnContract
from odoo.addons.gdrive_odoo_sync.lib.text_canon import (
    TEXT_CANON,
    collapse_ws,
    cosmetic_fold,
    fold_punct,
    strip_format_chars,
    text_prepare,
    unify_whitespace,
)

BOM = "\ufeff"
ZWSP = "\u200b"
ZWNJ = "\u200c"
ZWJ = "\u200d"
WORD_JOINER = "\u2060"
SOFT_HYPHEN = "\u00ad"
NBSP = "\u00a0"
NNBSP = "\u202f"
IDEOGRAPHIC_SPACE = "\u3000"
EM_DASH = "\u2014"
RIGHT_SINGLE_QUOTE = "\u2019"


class TestTextCanonInvisibles(BaseCase):
    """Step 3 — invisible and format characters are removed, not preserved."""

    def test_bom_is_stripped(self):
        self.assertEqual(TEXT_CANON(BOM + "ACME Foods"), "s:ACME Foods")

    def test_zero_width_family_is_stripped(self):
        for ch in (ZWSP, ZWNJ, ZWJ, WORD_JOINER):
            with self.subTest(codepoint=hex(ord(ch))):
                self.assertEqual(TEXT_CANON("AC" + ch + "ME"), "s:ACME")

    def test_soft_hyphen_is_stripped(self):
        self.assertEqual(TEXT_CANON("Invoi" + SOFT_HYPHEN + "ce"), "s:Invoice")

    def test_every_cf_category_codepoint_is_stripped(self):
        # U+2066 LEFT-TO-RIGHT ISOLATE is category Cf and is emitted by some
        # editors around bidirectional text. It must not survive into a hash.
        lri = "\u2066"
        self.assertEqual(unicodedata.category(lri), "Cf")
        self.assertEqual(TEXT_CANON(lri + "ACME" + "\u2069"), "s:ACME")

    def test_c0_controls_are_stripped_except_tab_and_newline(self):
        self.assertEqual(strip_format_chars("a\x00b\x01c\x7f"), "abc")
        self.assertEqual(strip_format_chars("a\tb\nc"), "a\tb\nc")

    def test_carriage_return_survives_step3_so_step4_can_normalize_it(self):
        # If step 3 deleted \r, "a\rb" would collapse to "ab" instead of the
        # specified "a\nb". This is exactly why \r is in the keep-set.
        self.assertEqual(TEXT_CANON("a\r\nb"), "s:a\nb")
        self.assertEqual(TEXT_CANON("a\rb"), "s:a\nb")


class TestTextCanonWhitespace(BaseCase):
    """Steps 4 and 7 — whitespace is unified, then optionally collapsed."""

    def test_nbsp_becomes_a_plain_space(self):
        self.assertEqual(TEXT_CANON("1" + NBSP + "234"), "s:1 234")

    def test_every_zs_separator_becomes_a_plain_space(self):
        for ch in (NBSP, NNBSP, IDEOGRAPHIC_SPACE, "\u2007", "\u205f"):
            with self.subTest(codepoint=hex(ord(ch))):
                self.assertEqual(unicodedata.category(ch), "Zs")
                self.assertEqual(TEXT_CANON("a" + ch + "b"), "s:a b")

    def test_tab_becomes_a_space(self):
        self.assertEqual(unify_whitespace("a\tb"), "a b")
        self.assertEqual(TEXT_CANON("a\tb"), "s:a b")

    def test_collapse_is_on_by_default(self):
        self.assertEqual(TEXT_CANON("ACME    Foods"), "s:ACME Foods")

    def test_collapse_can_be_switched_off_for_notes_columns(self):
        col = ColumnContract(ctype="text", text_collapse_ws=False)
        self.assertEqual(TEXT_CANON("ACME    Foods", col), "s:ACME    Foods")

    def test_collapse_never_crosses_a_newline(self):
        # Collapsing across \n would silently join the lines of an address
        # block, turning a structure-preserving column into a lossy one.
        self.assertEqual(collapse_ws("12  Main St\n\nSuite  4"), "12 Main St\n\nSuite 4")
        self.assertEqual(TEXT_CANON("12  Main St\nSuite  4"), "s:12 Main St\nSuite 4")

    def test_trim_removes_leading_and_trailing_space_and_newline(self):
        self.assertEqual(TEXT_CANON("  ACME Foods \n"), "s:ACME Foods")


class TestTextCanonNormalization(BaseCase):
    """Step 5 — NFC, and emphatically not NFKC."""

    def test_decomposed_and_composed_forms_agree(self):
        decomposed = "e\u0301"  # e + COMBINING ACUTE ACCENT
        composed = "\u00e9"  # é
        self.assertNotEqual(decomposed, composed)
        self.assertEqual(TEXT_CANON(decomposed), TEXT_CANON(composed))
        self.assertEqual(TEXT_CANON(decomposed), "s:\u00e9")

    def test_normalization_happens_after_invisibles_are_stripped(self):
        # A ZWSP wedged between the base letter and its combining accent would
        # block composition if NFC ran first. Step ordering is what fixes it.
        self.assertEqual(TEXT_CANON("e" + ZWSP + "\u0301"), "s:\u00e9")

    def test_nfkc_folding_does_not_occur(self):
        # Each of these is folded by NFKC and must survive NFC untouched:
        # losing them is real information loss that masks genuine differences.
        for raw in ("\u33a1", "\u2460", "\uff21", "\ufb01"):  # ㎡ ① Ａ ﬁ
            with self.subTest(raw=raw):
                self.assertNotEqual(
                    unicodedata.normalize("NFKC", raw), raw,
                    "test input must actually be NFKC-sensitive",
                )
                self.assertEqual(TEXT_CANON(raw), "s:" + raw)


class TestTextCanonCase(BaseCase):
    """Step 8 — ``casefold()``, never ``lower()``."""

    def test_default_is_preserve(self):
        self.assertEqual(TEXT_CANON("ACME Foods"), "s:ACME Foods")

    def test_fold_uses_casefold_not_lower_for_sharp_s(self):
        col = ColumnContract(ctype="text", text_case="fold")
        self.assertEqual("Stra\u00dfe".lower(), "stra\u00dfe")  # lower() is a no-op here
        self.assertEqual(TEXT_CANON("Stra\u00dfe", col), "s:strasse")

    def test_fold_normalizes_greek_final_sigma(self):
        col = ColumnContract(ctype="text", text_case="fold")
        self.assertEqual(TEXT_CANON("\u03c2", col), TEXT_CANON("\u03c3", col))

    def test_fold_makes_email_columns_comparable(self):
        col = ColumnContract(ctype="text", text_case="fold")
        self.assertEqual(
            TEXT_CANON("Lucaso@AvatarNaturalFoods.com", col),
            "s:lucaso@avatarnaturalfoods.com",
        )


class TestTextCanonEmpty(BaseCase):
    """Steps 1 and 9 — everything that means "nothing" collapses to one token."""

    def test_all_flavours_of_empty_produce_the_same_token(self):
        for raw in (None, "", "   ", NBSP, NBSP + " " + NBSP, ZWSP, BOM, "\n\n"):
            with self.subTest(raw=repr(raw)):
                self.assertEqual(TEXT_CANON(raw), "z:")

    def test_empty_is_null_false_keeps_the_empty_string(self):
        col = ColumnContract(ctype="text", empty_is_null=False)
        self.assertEqual(TEXT_CANON("   ", col), "s:")

    def test_absent_sentinel_is_empty(self):
        # A ragged row shorter than the header produces ABSENT, not '' — the two
        # are different facts ("no cell" vs "an empty cell") and only the
        # dispatcher's pre-dispatch guard is allowed to conflate them.
        from odoo.addons.gdrive_odoo_sync.lib.tokens import ABSENT
        self.assertEqual(TEXT_CANON(ABSENT), "z:")


class TestSmartPunctuation(BaseCase):
    """§4.1 — smart punctuation is *not* folded in the primary canonical form."""

    def test_strict_form_keeps_smart_punctuation(self):
        raw = "RE Portfolio " + EM_DASH + " MASTER"
        self.assertEqual(TEXT_CANON(raw), "s:" + raw)
        self.assertIn(EM_DASH, TEXT_CANON(raw))

    def test_apostrophe_is_not_folded_in_the_strict_form(self):
        self.assertEqual(TEXT_CANON("Bob" + RIGHT_SINGLE_QUOTE + "s Data"),
                         "s:Bob" + RIGHT_SINGLE_QUOTE + "s Data")

    def test_fold_punct_maps_exactly_the_documented_table(self):
        self.assertEqual(fold_punct("\u2018a\u2019"), "'a'")
        self.assertEqual(fold_punct("\u201ca\u201d"), '"a"')
        self.assertEqual(fold_punct("\u2010\u2013\u2014\u2212"), "----")
        self.assertEqual(fold_punct("\u2026"), "...")

    def test_cosmetic_fold_is_unconditional(self):
        # The folded hash exists to answer "is this difference presentational?",
        # so it folds maximally regardless of the column's declared options.
        self.assertEqual(
            cosmetic_fold("ACME" + RIGHT_SINGLE_QUOTE + "S   FOODS"),
            "acme's foods",
        )

    def test_strict_and_folded_differ_exactly_when_the_edit_is_cosmetic(self):
        strict_a = TEXT_CANON("Bob" + RIGHT_SINGLE_QUOTE + "s Data")
        strict_b = TEXT_CANON("Bob's  data")
        self.assertNotEqual(strict_a, strict_b)
        self.assertEqual(cosmetic_fold(strict_a[2:]), cosmetic_fold(strict_b[2:]))


class TestTextPrepareSharing(BaseCase):
    """``text_prepare`` is steps 1-6 only, and is what the other canonicalizers reuse."""

    def test_returns_an_untagged_string(self):
        self.assertEqual(text_prepare("  1" + NBSP + "234,50  "), "1 234,50")

    def test_does_not_collapse_or_casefold(self):
        # Collapsing here would corrupt NUM_CANON's view of a value whose
        # separators are being stripped a step later.
        self.assertEqual(text_prepare("A   B"), "A   B")
        self.assertEqual(text_prepare("ABC"), "ABC")


class TestTextCanonWarnings(BaseCase):
    """A number arriving at a text column is a contract smell and must be visible."""

    def test_number_in_a_text_column_warns(self):
        sink = []
        token = TEXT_CANON(1234, warnings=sink)
        self.assertEqual(token, "s:1234")
        self.assertTrue(sink, "a number reaching a text column must push a warning")

    def test_no_warning_for_a_plain_string(self):
        sink = []
        TEXT_CANON("1234", warnings=sink)
        self.assertEqual(sink, [])
