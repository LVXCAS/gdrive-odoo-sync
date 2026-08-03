# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. Licensed under LGPL-3.
"""Lane C — ``BOOL_CANON`` (``docs/CANONICALIZATION.md`` §7).

WHY this module has more tests than its size suggests
=====================================================
The tempting implementation is ``bool(value)``. It maps ``"no"``, ``"false"``,
``"pending"`` and ``"?"`` all to **true**, because every non-empty string is
truthy in Python. The almost-as-tempting variant — default anything
unrecognised to *false* — is worse in a verification system, because it makes
the tool report ``verified`` over data it actively misread. Reporting "I could
not read this cell" is always the better answer, so unknown tokens become
``e:BAD_BOOL`` and quarantine the row.

The second trap is on the Odoo side: ``False`` on a ``fields.Boolean`` is a
**real value**, not NULL. Branching on truthiness rather than on the field type
turns every unchecked box into a phantom empty.
"""

from odoo.tests.common import BaseCase

from odoo.addons.gdrive_odoo_sync.lib.bool_canon import BOOL_CANON, empty_bool_token
from odoo.addons.gdrive_odoo_sync.lib.contract import (
    DEFAULT_FALSY,
    DEFAULT_TRUTHY,
    ColumnContract,
)

CHECK_MARK = "✓"


def _bool_col(**kw):
    kw.setdefault("ctype", "bool")
    kw.setdefault("key", "is_active")
    return ColumnContract(**kw)


class TestRealBooleans(BaseCase):
    """Step 1 — an actual ``True``/``False`` passes straight through."""

    def test_true_and_false(self):
        col = _bool_col()
        self.assertEqual(BOOL_CANON(True, col), "b:1")
        self.assertEqual(BOOL_CANON(False, col), "b:0")

    def test_odoo_false_is_a_value_not_an_empty(self):
        # This is the whole reason step 1 comes before the empty handling: an
        # unchecked Odoo Boolean must hash as b:0, never as z:.
        col = _bool_col(empty_means="null", odoo_field_type="boolean")
        self.assertEqual(BOOL_CANON(False, col), "b:0")


class TestDeclaredVocabulary(BaseCase):
    """Steps 3-4 — membership against the declared, casefolded lists."""

    def test_default_truthy_tokens(self):
        col = _bool_col()
        for raw in DEFAULT_TRUTHY:
            with self.subTest(raw=raw):
                self.assertEqual(BOOL_CANON(raw, col), "b:1")

    def test_default_falsy_tokens(self):
        col = _bool_col()
        for raw in DEFAULT_FALSY:
            with self.subTest(raw=raw):
                self.assertEqual(BOOL_CANON(raw, col), "b:0")

    def test_check_mark_is_truthy(self):
        # Spreadsheets in this deployment mark rows with a literal check mark.
        self.assertIn(CHECK_MARK, DEFAULT_TRUTHY)
        self.assertEqual(BOOL_CANON(CHECK_MARK, _bool_col()), "b:1")

    def test_case_and_whitespace_are_irrelevant(self):
        col = _bool_col()
        for raw in ("TRUE", " True ", "yEs", "Y", "  NO  ", "False"):
            with self.subTest(raw=repr(raw)):
                self.assertIn(BOOL_CANON(raw, col), ("b:0", "b:1"))
        self.assertEqual(BOOL_CANON("  TRUE ", col), "b:1")
        self.assertEqual(BOOL_CANON("No", col), "b:0")

    def test_numeric_one_and_zero_match_the_default_lists(self):
        col = _bool_col()
        self.assertEqual(BOOL_CANON(1, col), "b:1")
        self.assertEqual(BOOL_CANON(0, col), "b:0")
        self.assertEqual(BOOL_CANON(1.0, col), "b:1")

    def test_custom_vocabulary_is_honoured(self):
        col = _bool_col(truthy=("si", "oui"), falsy=("no", "non"))
        self.assertEqual(BOOL_CANON("SI", col), "b:1")
        self.assertEqual(BOOL_CANON("Non", col), "b:0")
        # And the defaults are then NOT silently still in force.
        self.assertEqual(BOOL_CANON("true", col), "e:BAD_BOOL")


class TestUnknownTokensAreRefused(BaseCase):
    """Step 5 — never default an unrecognized token to false."""

    def test_the_words_that_would_otherwise_be_silently_false(self):
        col = _bool_col()
        for raw in ("pending", "TBD", "maybe", "?", "n/a", "unknown", "2"):
            with self.subTest(raw=raw):
                self.assertEqual(
                    BOOL_CANON(raw, col), "e:BAD_BOOL",
                    "%r must quarantine the row, not become a confident false" % raw,
                )

    def test_a_numeric_two_is_refused(self):
        # No sane contract means 2 as a boolean; reading it as truthy would be
        # a confident wrong answer.
        self.assertEqual(BOOL_CANON(2, _bool_col()), "e:BAD_BOOL")

    def test_error_token_is_never_a_partial_success(self):
        token = BOOL_CANON("pending", _bool_col())
        self.assertTrue(token.startswith("e:"))


class TestEmptyHandling(BaseCase):
    """Step 2 — an empty cell resolves through the declared ``empty_means``."""

    def test_default_empty_means_false(self):
        col = _bool_col()
        for raw in (None, "", "   "):
            with self.subTest(raw=repr(raw)):
                self.assertEqual(BOOL_CANON(raw, col), "b:0")

    def test_empty_means_null(self):
        col = _bool_col(empty_means="null")
        self.assertEqual(BOOL_CANON(None, col), "z:")
        self.assertEqual(BOOL_CANON("   ", col), "z:")

    def test_empty_means_error(self):
        # For a column where a blank really is a defect — a required consent
        # flag, say — rather than a default.
        col = _bool_col(empty_means="error")
        self.assertEqual(BOOL_CANON(None, col), "e:BAD_BOOL")

    def test_helper_matches_the_dispatcher(self):
        for mode, expected in (("false", "b:0"), ("null", "z:"), ("error", "e:BAD_BOOL")):
            with self.subTest(mode=mode):
                col = _bool_col(empty_means=mode)
                self.assertEqual(empty_bool_token(col), expected)
                self.assertEqual(BOOL_CANON(None, col), expected)

    def test_absent_cell_behaves_like_empty(self):
        from odoo.addons.gdrive_odoo_sync.lib.tokens import ABSENT
        self.assertEqual(BOOL_CANON(ABSENT, _bool_col()), "b:0")


class TestDeterminism(BaseCase):
    """A boolean column must produce exactly three possible outcomes, stably."""

    def test_output_alphabet_is_closed(self):
        col = _bool_col()
        allowed = {"b:0", "b:1", "z:", "e:BAD_BOOL"}
        corpus = [True, False, None, "", "yes", "no", "maybe", 1, 0, 2, "✓", "  "]
        for raw in corpus:
            with self.subTest(raw=repr(raw)):
                self.assertIn(BOOL_CANON(raw, col), allowed)

    def test_repeated_calls_agree(self):
        col = _bool_col()
        for raw in (True, "yes", "maybe", None):
            first = BOOL_CANON(raw, col)
            self.assertEqual([BOOL_CANON(raw, col) for _ in range(4)], [first] * 4)
