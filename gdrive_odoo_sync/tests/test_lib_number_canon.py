# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. Licensed under LGPL-3.
"""Lane C — ``NUM_CANON``: decimal canonicalization (``docs/CANONICALIZATION.md`` §5).

WHY these cases
===============
Sheets returns every number as an IEEE-754 double, Odoo ``Float`` columns are
``double precision``, and JSON-RPC hands Python floats to both sides. ``12.30``
is not representable in binary, so two honest paths to "twelve thirty" land on
``12.299999999999999`` and ``12.300000000000001``. A tool that reports those as
a difference is untrustworthy on its first day.

Four decisions in the algorithm carry almost all of the risk, and each has a
test here that fails loudly if it is ever "simplified":

1. ``Decimal(repr(x))`` and never ``Decimal(x)`` — otherwise the move to
   ``Decimal`` buys nothing at all.
2. ``ROUND_HALF_UP`` and never banker's rounding — Odoo's ``float_round`` is
   half-up, Python's ``Decimal`` default is not, and using the default
   guarantees disagreement on every ``.5``.
3. Separators are **declared**, never detected: ``"1.234"`` is 1234 in de-DE and
   1.234 in en-US and no heuristic can tell.
4. Fixed-point output, never scientific — a hash over ``1E+3`` is a hash over a
   display format.
"""

from decimal import Decimal

from odoo.tests.common import BaseCase

from odoo.addons.gdrive_odoo_sync.lib.contract import ColumnContract
from odoo.addons.gdrive_odoo_sync.lib.number_canon import (
    NUM_CANON,
    format_fixed,
    near_boundary,
    quantize,
    raw_decimal,
    step_for_scale,
    to_decimal,
)

NBSP = " "


def _num(**kw):
    """A numeric ColumnContract with the SPEC §3.9 defaults, overridable."""
    kw.setdefault("ctype", "number")
    kw.setdefault("key", "amount")
    return ColumnContract(**kw)


class TestDecimalConstruction(BaseCase):
    """Rule 1 — ``Decimal(repr(x))``, never ``Decimal(x)``."""

    def test_repr_not_binary_expansion(self):
        self.assertEqual(to_decimal(0.1), Decimal("0.1"))
        self.assertNotEqual(to_decimal(0.1), Decimal(0.1))

    def test_binary_noise_never_reaches_the_canonical_form(self):
        # At scale 20 the difference is directly visible: Decimal(0.1) would
        # produce ...0555 in the fraction digits.
        col = _num(scale=20)
        self.assertEqual(NUM_CANON(0.1, col), "n:0." + "1" + "0" * 19)

    def test_twelve_thirty_from_both_directions_agrees(self):
        col = _num(scale=2)
        self.assertEqual(NUM_CANON(12.299999999999999, col), NUM_CANON(12.300000000000001, col))
        self.assertEqual(NUM_CANON(12.3, col), "n:12.30")

    def test_int_and_decimal_inputs_pass_through_losslessly(self):
        col = _num(scale=2)
        self.assertEqual(NUM_CANON(1234, col), "n:1234.00")
        self.assertEqual(NUM_CANON(Decimal("1234.5"), col), "n:1234.50")


class TestRounding(BaseCase):
    """Rule 2 — ``ROUND_HALF_UP`` (away from zero), never banker's rounding."""

    def test_half_rounds_away_from_zero(self):
        col = _num(scale=2)
        # Banker's rounding would give 2.34 and 0.12 respectively.
        self.assertEqual(NUM_CANON(Decimal("2.345"), col), "n:2.35")
        self.assertEqual(NUM_CANON(Decimal("0.125"), col), "n:0.13")

    def test_negative_half_rounds_away_from_zero_too(self):
        col = _num(scale=2)
        self.assertEqual(NUM_CANON(Decimal("-2.345"), col), "n:-2.35")

    def test_quantize_helper_agrees_with_the_documented_step(self):
        self.assertEqual(step_for_scale(2), Decimal("0.01"))
        self.assertEqual(step_for_scale(0), Decimal("1"))
        self.assertEqual(quantize(Decimal("2.345"), 2), Decimal("2.35"))

    def test_minus_zero_collapses(self):
        col = _num(scale=2)
        self.assertEqual(NUM_CANON(-0.001, col), "n:0.00")
        self.assertEqual(NUM_CANON(-0.0, col), "n:0.00")
        self.assertNotIn("-", NUM_CANON(Decimal("-0.004"), col))


class TestSeparators(BaseCase):
    """Rule 3 — separators are declared contract fields, never guessed."""

    def test_same_string_two_locales_two_answers(self):
        en = _num(scale=3, decimal_sep=".", group_sep=",")
        de = _num(scale=3, decimal_sep=",", group_sep=".")
        self.assertEqual(NUM_CANON("1.234", en), "n:1.234")
        self.assertEqual(NUM_CANON("1.234", de), "n:1234.000")

    def test_grouped_thousands_are_stripped(self):
        self.assertEqual(NUM_CANON("1,234.50", _num(scale=2)), "n:1234.50")

    def test_de_de_grouping_and_decimal(self):
        de = _num(scale=2, decimal_sep=",", group_sep=".")
        self.assertEqual(NUM_CANON("1.234.567,89", de), "n:1234567.89")

    def test_nbsp_grouping_survives_because_text_prepare_runs_first(self):
        # Exported sheets routinely emit NBSP as a thousands separator; step 4
        # of TEXT_CANON turns it into a plain space, which NUM_CANON removes.
        self.assertEqual(NUM_CANON("1" + NBSP + "234.50", _num(scale=2)), "n:1234.50")

    def test_leading_currency_symbol_is_stripped(self):
        col = _num(scale=2)
        self.assertEqual(NUM_CANON("$1,234.50", col), "n:1234.50")
        self.assertEqual(NUM_CANON("USD 1,234.50", col), "n:1234.50")


class TestAccountingNegatives(BaseCase):
    """Parenthesised and trailing-minus negatives are an accounting convention."""

    def test_parentheses_mean_negative(self):
        self.assertEqual(NUM_CANON("(1,234.50)", _num(scale=2)), "n:-1234.50")

    def test_parenthesised_currency_means_negative(self):
        self.assertEqual(NUM_CANON("($1,234.50)", _num(scale=2)), "n:-1234.50")

    def test_trailing_minus_means_negative(self):
        self.assertEqual(NUM_CANON("1234-", _num(scale=2)), "n:-1234.00")

    def test_leading_plus_is_dropped(self):
        self.assertEqual(NUM_CANON("+1234", _num(scale=2)), "n:1234.00")

    def test_can_be_switched_off(self):
        col = _num(scale=2, accounting_negatives=False)
        self.assertEqual(NUM_CANON("(1234)", col), "e:NOT_A_NUMBER")


class TestPercent(BaseCase):
    """A trailing ``%`` only divides when the contract says so."""

    def test_divide_100_when_declared(self):
        col = _num(scale=4, percent_mode="divide_100")
        self.assertEqual(NUM_CANON("12.5%", col), "n:0.1250")

    def test_percent_is_refused_when_the_contract_does_not_declare_one(self):
        # Silently dropping the sign made "12.5%" and "12.5" the same token, so
        # editing a cell from 5 to 5% produced a byte-identical row hash, an
        # unchanged dataset digest and a `verified` result over a hundredfold
        # change in meaning. Refusing quarantines the row and tells a human the
        # contract does not say how to read a percent.
        col = _num(scale=2, percent_mode="none")
        self.assertEqual(NUM_CANON("12.5%", col), "e:NOT_A_NUMBER")

    def test_percent_and_bare_number_never_share_a_token(self):
        for mode in ("none", "divide_100"):
            with self.subTest(percent_mode=mode):
                col = _num(scale=4, percent_mode=mode)
                self.assertNotEqual(NUM_CANON("50%", col), NUM_CANON("50", col))


class TestOutputFormat(BaseCase):
    """Rule 4 — fixed point, exactly ``scale`` fraction digits, never an exponent."""

    def test_large_magnitude_is_not_scientific(self):
        col = _num(scale=0)
        self.assertEqual(NUM_CANON(Decimal("1E+3"), col), "n:1000")
        self.assertNotIn("E", NUM_CANON(Decimal("1E+18"), col))

    def test_tiny_magnitude_is_not_scientific(self):
        col = _num(scale=2)
        self.assertEqual(NUM_CANON(Decimal("1E-7"), col), "n:0.00")

    def test_scale_zero_omits_the_fraction(self):
        self.assertEqual(NUM_CANON(1, _num(scale=0)), "n:1")

    def test_never_emits_a_plus_sign(self):
        self.assertEqual(format_fixed(Decimal("1234.50")), "1234.50")
        self.assertNotIn("+", NUM_CANON(1234.5, _num(scale=2)))


class TestErrorTokens(BaseCase):
    """Unparseable data returns an ``e:`` token; it never raises and never guesses."""

    def test_non_numeric_text(self):
        for raw in ("abc", "N/A", "twelve", "1,2,3.4.5", "--5"):
            with self.subTest(raw=raw):
                self.assertEqual(NUM_CANON(raw, _num(scale=2)), "e:NOT_A_NUMBER")

    def test_nan_and_infinity(self):
        col = _num(scale=2)
        self.assertEqual(NUM_CANON(float("nan"), col), "e:NOT_FINITE")
        self.assertEqual(NUM_CANON(float("inf"), col), "e:NOT_FINITE")
        self.assertEqual(NUM_CANON(Decimal("Infinity"), col), "e:NOT_FINITE")

    def test_empty_is_null_by_default(self):
        col = _num(scale=2)
        for raw in (None, "", "   ", NBSP):
            with self.subTest(raw=repr(raw)):
                self.assertEqual(NUM_CANON(raw, col), "z:")

    def test_empty_can_be_declared_to_mean_zero(self):
        col = _num(scale=2, empty_is_null=False)
        self.assertEqual(NUM_CANON("", col), "n:0.00")


class TestScaleResolution(BaseCase):
    """Scale comes from the domain, never from the data — and never from Odoo here."""

    def test_fixed_scale_uses_the_declared_value(self):
        self.assertEqual(NUM_CANON(1.5, _num(scale=3)), "n:1.500")

    def test_currency_scale_must_be_resolved_by_lane_e(self):
        col = _num(scale_mode="currency")
        with self.assertRaises(ValueError):
            NUM_CANON(1.5, col)

    def test_currency_scale_honours_resolved_scale(self):
        col = _num(scale_mode="currency", resolved_scale=0)  # e.g. JPY
        self.assertEqual(NUM_CANON(1234.5, col), "n:1235")

    def test_uom_scale_honours_resolved_scale(self):
        col = _num(scale_mode="uom", resolved_scale=3)
        self.assertEqual(NUM_CANON(1.23456, col), "n:1.235")


class TestRawDecimalControl(BaseCase):
    """§5.3 — the independent control shares no code with the normalizer.

    That is the entire point: if both sides are canonicalized wrongly in the
    same way, the hashes agree *and* the canonical totals agree. Only a total
    computed by a different code path can catch a symmetric normalizer bug — so
    ``raw_decimal`` must NOT learn about group separators, currency symbols or
    accounting parentheses, however tempting that is.
    """

    def test_floats_use_repr_semantics(self):
        self.assertEqual(raw_decimal(0.1), Decimal("0.1"))

    def test_no_quantization(self):
        self.assertEqual(raw_decimal(1.23456789), Decimal("1.23456789"))

    def test_plain_numeric_string_parses(self):
        self.assertEqual(raw_decimal(" 1234.50 "), Decimal("1234.50"))

    def test_grouped_string_is_refused_rather_than_cleaned(self):
        self.assertIsNone(raw_decimal("1,234.50"))
        self.assertIsNone(raw_decimal("$1234.50"))

    def test_non_finite_is_excluded_from_totals(self):
        self.assertIsNone(raw_decimal(float("nan")))
        self.assertIsNone(raw_decimal(float("inf")))

    def test_empty_is_none_not_zero(self):
        # Summing a None-free column of zeros would hide missing cells.
        self.assertIsNone(raw_decimal(None))
        self.assertIsNone(raw_decimal(""))


class TestRoundingBoundary(BaseCase):
    """Values sitting on a half-step are the source of "drift appeared and vanished"."""

    def test_exact_half_step_is_flagged(self):
        self.assertTrue(near_boundary(Decimal("2.005"), Decimal("0.01")))
        self.assertTrue(near_boundary(Decimal("-2.005"), Decimal("0.01")))

    def test_comfortably_inside_a_step_is_not_flagged(self):
        self.assertFalse(near_boundary(Decimal("2.001"), Decimal("0.01")))
        self.assertFalse(near_boundary(Decimal("2.0"), Decimal("0.01")))

    def test_canonicalizing_a_boundary_value_pushes_a_warning(self):
        sink = []
        NUM_CANON(Decimal("2.005"), _num(scale=2), warnings=sink)
        self.assertTrue(sink, "a half-step value must be reported to lane E")

    def test_ordinary_value_pushes_no_warning(self):
        sink = []
        NUM_CANON(Decimal("2.001"), _num(scale=2), warnings=sink)
        self.assertEqual(sink, [])
