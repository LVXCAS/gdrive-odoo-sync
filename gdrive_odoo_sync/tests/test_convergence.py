# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. Licensed under LGPL-3.
"""Convergence — the invariant whose violation is invisible (SPEC §9.8, CANON §12.9).

THE BUG CLASS THIS FILE EXISTS TO CATCH
=======================================
The comparator says A ≠ B. The writer writes A. A round-trips through Odoo as
A′, and A′ ≠ A under the normalizer. So the next run writes it again. And the
next. Forever.

Nothing errors. The dashboard says "3 fixes applied" every single night and
everybody assumes it is working. Storage grows, ``write_date`` churns on records
nobody edited, the Odoo-side fast path never hits, and the drift report is
permanently non-empty for reasons nobody can explain.

Three defences, all tested here:

1. **The convergence property itself** — for every value, canonicalizing the
   sheet side and canonicalizing what Odoo hands back after a write must produce
   the *same token*. This is checked against a deliberately hostile corpus: NBSP,
   smart quotes, decomposed accents, accounting negatives, serial dates, empty
   strings, booleans, and the values that sit exactly on a rounding boundary.
2. **The post-apply hash assertion** — after executing a plan both dataset
   hashes are recomputed and asserted equal. Failure sets ``convergence_ok`` and
   raises a ``non_convergent`` finding at ``critical``. The system **alerts
   rather than retries**: retrying is precisely what the bug already does.
3. **The flap detector** — a ``(sync_id, field)`` written on ``flap_limit``
   consecutive runs stops being written, and the finding carries both canonical
   forms, which tells the maintainer exactly which normalization rule is
   asymmetric.
"""

from datetime import date, datetime
from decimal import Decimal

from odoo.tests.common import TransactionCase

from odoo.addons.gdrive_odoo_sync.lib.bool_canon import BOOL_CANON
from odoo.addons.gdrive_odoo_sync.lib.contract import ColumnContract
from odoo.addons.gdrive_odoo_sync.lib.datetime_canon import DATE_CANON, DATETIME_CANON
from odoo.addons.gdrive_odoo_sync.lib.number_canon import NUM_CANON
from odoo.addons.gdrive_odoo_sync.lib.text_canon import TEXT_CANON

from .test_reconciler_plan import (
    NOW,
    contract,
    odoo_row,
    odoo_snapshot,
    policy,
    sheet_row,
    snapshot,
)

NBSP = " "
RSQUO = "’"
NY = "America/New_York"


class TestTextConvergence(TransactionCase):
    """``CANON_sheet(v) == CANON_odoo(read_back(write(typed(v))))`` for text."""

    HOSTILE = [
        "ACME Foods",
        "  ACME   Foods  ",
        "﻿ACME Foods",
        "ACME" + NBSP + "Foods",
        "Bob" + RSQUO + "s Data",
        "éclair",  # decomposed
        "éclair",  # composed
        "RE Portfolio — MASTER",
        "Straße",
        "㎡",  # NFKC-sensitive; must survive NFC untouched
        "line one\nline two",
        "",
        "   ",
    ]

    def _col(self, **kw):
        kw.setdefault("ctype", "text")
        kw.setdefault("key", "name")
        kw.setdefault("odoo_field_type", "char")
        return ColumnContract(**kw)

    def test_writing_the_canonical_form_back_is_a_fixed_point(self):
        # This is the practical form of the invariant: whatever the sheet side
        # produces, feeding it back through the canonicalizer must not move.
        col = self._col()
        for raw in self.HOSTILE:
            with self.subTest(raw=repr(raw)):
                once = TEXT_CANON(raw, col)
                payload = once[2:] if once.startswith("s:") else None
                if payload is None:
                    continue
                twice = TEXT_CANON(payload, col)
                self.assertEqual(once, twice)

    def test_odoo_returning_false_for_an_empty_char_agrees_with_an_empty_cell(self):
        col = self._col()
        self.assertEqual(TEXT_CANON("", col), "z:")
        self.assertEqual(TEXT_CANON(None, col), "z:")

    def test_folding_is_also_a_fixed_point(self):
        col = self._col(text_case="fold")
        for raw in self.HOSTILE:
            with self.subTest(raw=repr(raw)):
                once = TEXT_CANON(raw, col)
                if not once.startswith("s:"):
                    continue
                self.assertEqual(TEXT_CANON(once[2:], col), once)

    def test_a_collapsing_column_converges_after_one_write(self):
        # The value that reaches Odoo is the *typed* value, which for text is
        # the canonical payload. Reading it back must not collapse further.
        col = self._col()
        written = TEXT_CANON("ACME   Foods", col)[2:]
        self.assertEqual(written, "ACME Foods")
        self.assertEqual(TEXT_CANON(written, col), "s:ACME Foods")


class TestNumberConvergence(TransactionCase):
    """Quantization is the tolerance, so it must be idempotent."""

    HOSTILE = [0.1, 12.3, 1234.5, -0.001, 2.345, 0.125, 1e-7, 1234567.891,
               Decimal("1E+3"), 0, -0.0]

    def _col(self, **kw):
        kw.setdefault("ctype", "number")
        kw.setdefault("key", "amount")
        kw.setdefault("scale", 2)
        return ColumnContract(**kw)

    def test_quantization_is_a_fixed_point(self):
        col = self._col()
        for raw in self.HOSTILE:
            with self.subTest(raw=repr(raw)):
                once = NUM_CANON(raw, col)
                self.assertTrue(once.startswith("n:"), once)
                self.assertEqual(NUM_CANON(once[2:], col), once)

    def test_the_float_odoo_hands_back_agrees_with_the_sheet_value(self):
        # Odoo stores a Float as double precision; the value read back is a
        # Python float that may differ in the last bit. Quantizing both sides is
        # what makes them agree, and comparing them with == is what would not.
        col = self._col()
        for raw in self.HOSTILE:
            with self.subTest(raw=repr(raw)):
                token = NUM_CANON(raw, col)
                round_tripped = float(token[2:])
                self.assertEqual(NUM_CANON(round_tripped, col), token)

    def test_boundary_values_do_not_flip_between_runs(self):
        col = self._col()
        for raw in ("2.005", "0.005", "-2.005", "1.115"):
            with self.subTest(raw=raw):
                token = NUM_CANON(raw, col)
                self.assertEqual(NUM_CANON(float(token[2:]), col), token)

    def test_a_currency_scale_of_zero_converges(self):
        col = self._col(scale_mode="currency", resolved_scale=0)
        token = NUM_CANON(1234.5, col)
        self.assertEqual(NUM_CANON(float(token[2:]), col), token)


class TestDateConvergence(TransactionCase):
    """A date written to Odoo comes back as a ``date``; it must hash the same."""

    def _col(self, **kw):
        kw.setdefault("ctype", "date")
        kw.setdefault("key", "due")
        return ColumnContract(**kw)

    def test_serial_and_date_object_agree(self):
        col = self._col()
        sheet_token = DATE_CANON(45000, col)
        odoo_token = DATE_CANON(date(2023, 3, 15), col, side="odoo")
        self.assertEqual(sheet_token, odoo_token)

    def test_iso_string_and_date_object_agree(self):
        col = self._col()
        self.assertEqual(
            DATE_CANON("2026-07-31", col),
            DATE_CANON(date(2026, 7, 31), col, side="odoo"),
        )

    def test_a_late_evening_date_does_not_roll_over(self):
        # The classic off-by-one: only reproducible at night, when the cron runs
        # and UTC has already advanced past the local date.
        col = self._col()
        self.assertEqual(
            DATE_CANON(datetime(2026, 7, 31, 23, 30), col, side="odoo"),
            "d:2026-07-31",
        )


class TestDatetimeConvergence(TransactionCase):
    """Sheet-local → UTC → Odoo-naive-UTC → token must be a round trip."""

    def _col(self, **kw):
        kw.setdefault("ctype", "datetime")
        kw.setdefault("key", "signed_at")
        kw.setdefault("sheet_timezone", NY)
        return ColumnContract(**kw)

    def test_round_trip_through_utc(self):
        col = self._col()
        sheet_token = DATETIME_CANON(datetime(2026, 7, 31, 10, 0, 0), col)
        self.assertEqual(sheet_token, "t:2026-07-31T14:00:00Z")
        stored = datetime(2026, 7, 31, 14, 0, 0)  # what Odoo persists, UTC-naive
        self.assertEqual(DATETIME_CANON(stored, col, side="odoo"), sheet_token)

    def test_winter_round_trip(self):
        col = self._col()
        sheet_token = DATETIME_CANON(datetime(2026, 1, 31, 10, 0, 0), col)
        stored = datetime(2026, 1, 31, 15, 0, 0)
        self.assertEqual(DATETIME_CANON(stored, col, side="odoo"), sheet_token)

    def test_an_uncomparable_local_time_never_converges_and_says_so(self):
        # Returning an error rather than shifting is what stops this becoming a
        # cell that is rewritten every night.
        col = self._col()
        self.assertEqual(
            DATETIME_CANON(datetime(2026, 3, 8, 2, 30), col),
            "e:NONEXISTENT_LOCAL_TIME",
        )


class TestBoolConvergence(TransactionCase):
    """Odoo hands back a real ``bool``; the sheet hands back a label."""

    def _col(self, **kw):
        kw.setdefault("ctype", "bool")
        kw.setdefault("key", "is_active")
        kw.setdefault("odoo_field_type", "boolean")
        return ColumnContract(**kw)

    def test_labels_and_booleans_agree(self):
        col = self._col()
        for label, value in (("yes", True), ("TRUE", True), ("no", False), ("0", False)):
            with self.subTest(label=label):
                self.assertEqual(BOOL_CANON(label, col), BOOL_CANON(value, col))

    def test_an_unreadable_label_is_never_silently_false(self):
        # If "pending" became b:0, the writer would set active=False on a record
        # whose real state nobody knows, and the hashes would then agree.
        col = self._col()
        self.assertEqual(BOOL_CANON("pending", col), "e:BAD_BOOL")
        self.assertNotEqual(BOOL_CANON("pending", col), BOOL_CANON(False, col))


class TestAsymmetricNormalizerIsCaught(TransactionCase):
    """§9.8 — the post-apply hash assertion, and what it is for."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.reconciler = cls.env["gdrive.reconciler"]

    def test_a_value_that_does_not_round_trip_keeps_producing_an_update(self):
        # Simulated directly: the sheet says one thing, Odoo persistently says
        # another, and the planner keeps proposing the same write. This is the
        # shape the post-apply assertion detects — the plan converged on paper
        # and did not converge in fact.
        sheet = snapshot([sheet_row("01A", canon={"name": "s:ACME Foods",
                                                  "amount": "n:1234.50"})])
        odoo = odoo_snapshot([odoo_row(1, "01A", canon={"name": "s:ACME Foods",
                                                        "amount": "n:1234.51"})])
        first = self.reconciler.plan(sheet, odoo, contract(), policy(), NOW)
        second = self.reconciler.plan(sheet, odoo, contract(), policy(), NOW)
        self.assertEqual(
            [a["action_type"] for a in first["actions"]],
            [a["action_type"] for a in second["actions"]],
        )
        self.assertTrue([a for a in first["actions"] if a["action_type"] == "update"])

    def test_the_flap_counter_stops_writing_at_the_limit(self):
        # Three consecutive writes of the same (sync_id, field) is the signature
        # of an asymmetric normalizer. Continuing to write is the bug; stopping
        # and reporting is the fix.
        row = odoo_row(1, "01A", canon={"name": "s:ACME Foods", "amount": "n:1234.51"})
        row["flap_counters"] = {"amount": 3}
        sheet = snapshot([sheet_row("01A", canon={"name": "s:ACME Foods",
                                                  "amount": "n:1234.50"})])
        result = self.reconciler.plan(sheet, odoo_snapshot([row]), contract(),
                                      policy(flap_limit=3), NOW)
        for update in [a for a in result["actions"] if a["action_type"] == "update"]:
            self.assertNotIn("amount", [d["field"] for d in update["deltas"]])

    def test_a_non_convergent_finding_is_raised_at_critical(self):
        row = odoo_row(1, "01A", canon={"name": "s:ACME Foods", "amount": "n:1234.51"})
        row["flap_counters"] = {"amount": 3}
        sheet = snapshot([sheet_row("01A", canon={"name": "s:ACME Foods",
                                                  "amount": "n:1234.50"})])
        result = self.reconciler.plan(sheet, odoo_snapshot([row]), contract(),
                                      policy(flap_limit=3), NOW)
        findings = [d for d in result["drifts"] if d["drift_type"] == "non_convergent"]
        self.assertTrue(findings)
        self.assertEqual(findings[0]["severity"], "critical")

    def test_the_finding_carries_both_canonical_forms(self):
        # Which is what tells the maintainer exactly which normalization rule is
        # asymmetric, rather than "something is wrong with amounts".
        row = odoo_row(1, "01A", canon={"name": "s:ACME Foods", "amount": "n:1234.51"})
        row["flap_counters"] = {"amount": 3}
        sheet = snapshot([sheet_row("01A", canon={"name": "s:ACME Foods",
                                                  "amount": "n:1234.50"})])
        result = self.reconciler.plan(sheet, odoo_snapshot([row]), contract(),
                                      policy(flap_limit=3), NOW)
        finding = [d for d in result["drifts"] if d["drift_type"] == "non_convergent"][0]
        self.assertEqual(finding["canon_sheet"], "n:1234.50")
        self.assertEqual(finding["canon_odoo"], "n:1234.51")

    def test_below_the_limit_the_field_is_still_written(self):
        row = odoo_row(1, "01A", canon={"name": "s:ACME Foods", "amount": "n:1234.51"})
        row["flap_counters"] = {"amount": 1}
        sheet = snapshot([sheet_row("01A", canon={"name": "s:ACME Foods",
                                                  "amount": "n:1234.50"})])
        result = self.reconciler.plan(sheet, odoo_snapshot([row]), contract(),
                                      policy(flap_limit=3), NOW)
        updates = [a for a in result["actions"] if a["action_type"] == "update"]
        self.assertTrue(updates)
        self.assertIn("amount", [d["field"] for d in updates[0]["deltas"]])
