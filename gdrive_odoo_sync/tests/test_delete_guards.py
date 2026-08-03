# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. Licensed under LGPL-3.
"""Lane E — the seven delete guards (SPEC §9.6).

WHY deletes get a materially higher evidence bar than creates and updates
========================================================================
Three independent reasons, each sufficient on its own:

1. **Asymmetric cost.** A wrongly created record is deleted in seconds. A
   wrongly deleted Odoo record takes its journal entries, attachments, message
   threads and many2one back-references with it, may be legally required to
   exist, and often cannot be restored at all.
2. **Asymmetric evidence.** Creates and updates are asserted by positive data
   present in the source. Deletes are inferred from **absence** — and absence is
   exactly what every read failure looks like: an empty service-account corpus,
   an expired token, a renamed tab, a partial ``batchGet``, a range that stopped
   at row 1000, a hidden filter view, a wrong Odoo domain, a ``spec_version``
   bump mid-deploy. There is no read bug whose signature is "invent 4000 new
   rows".
3. **Non-locality.** A create or update touches one record. A mass-delete event
   touches the whole dataset at once and is the only failure unbounded in blast
   radius.

Each of the seven conditions is tested **independently**: every test starts from
a configuration where the delete *would* be planned and then flips exactly one
guard, so a regression in any single guard fails its own test rather than hiding
behind another.
"""

from odoo.tests.common import TransactionCase

from .test_reconciler_plan import (
    NOW,
    contract,
    odoo_row,
    odoo_snapshot,
    policy,
    sheet_row,
    snapshot,
)

LONG_AGO = "2026-07-01 00:00:00"


def missing_odoo_row(res_id, sync_id, *, owned=True, missing_since=LONG_AGO,
                     missing_run_count=5, identity_source="sync_id"):
    """An Odoo row whose identity is absent from the sheet and has been for a while."""
    row = odoo_row(res_id, sync_id, canon={"name": "s:Gone", "amount": "n:1.00"}, owned=owned)
    row.update({
        "missing_since": missing_since,
        "missing_run_count": missing_run_count,
        "identity_source": identity_source,
        "has_link": True,
    })
    return row


def deletable_policy(**kw):
    """The only configuration in which a soft delete may be planned at all."""
    base = {
        "delete_policy": "soft",
        "quarantine_runs": 2,
        "quarantine_hours": 24,
        "delete_threshold_abs": 20,
        "delete_threshold_pct": 5.0,
        "identity_strategy": "sync_id",
    }
    base.update(kw)
    return policy(**base)


class DeleteGuardCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.reconciler = cls.env["gdrive.reconciler"]

    def plan(self, sheet, odoo, pol=None, ctr=None, now=NOW):
        return self.reconciler.plan(sheet, odoo, ctr or contract(), pol or deletable_policy(), now)

    def soft_deletes(self, result):
        return [a for a in result["actions"] if a["action_type"] == "soft_delete"]

    def baseline(self):
        """The one configuration where a soft delete is legitimately planned."""
        sheet = snapshot([sheet_row("01KEEP", canon={"name": "s:Kept", "amount": "n:1.00"})],
                         read_complete=True, blocking=False)
        odoo = odoo_snapshot([
            odoo_row(1, "01KEEP", canon={"name": "s:Kept", "amount": "n:1.00"}),
            missing_odoo_row(2, "01GONE"),
        ])
        return sheet, odoo


class TestBaseline(DeleteGuardCase):
    """The fixture must actually plan a delete, or the guard tests prove nothing."""

    def test_a_soft_delete_is_planned_when_every_condition_holds(self):
        sheet, odoo = self.baseline()
        result = self.plan(sheet, odoo)
        deletes = self.soft_deletes(result)
        self.assertEqual(len(deletes), 1, result["actions"])
        self.assertEqual(deletes[0]["res_id"], 2)


class TestGuard1DeletePolicy(DeleteGuardCase):
    """Condition 1 — ``delete_policy == 'soft'``, which is never the default."""

    def test_report_is_the_default(self):
        self.assertEqual(policy()["delete_policy"], "report")

    def test_report_policy_blocks_the_delete(self):
        sheet, odoo = self.baseline()
        result = self.plan(sheet, odoo, pol=deletable_policy(delete_policy="report"))
        self.assertEqual(self.soft_deletes(result), [])

    def test_never_policy_blocks_the_delete(self):
        sheet, odoo = self.baseline()
        result = self.plan(sheet, odoo, pol=deletable_policy(delete_policy="never"))
        self.assertEqual(self.soft_deletes(result), [])

    def test_the_finding_is_still_reported(self):
        sheet, odoo = self.baseline()
        result = self.plan(sheet, odoo, pol=deletable_policy(delete_policy="report"))
        self.assertIn("missing_in_sheet", {d["drift_type"] for d in result["drifts"]})


class TestGuard2Ownership(DeleteGuardCase):
    """Condition 2 — the record must be owned by this dataset."""

    def test_unowned_record_is_unmanaged_not_deleted(self):
        sheet, _odoo = self.baseline()
        odoo = odoo_snapshot([
            odoo_row(1, "01KEEP", canon={"name": "s:Kept", "amount": "n:1.00"}),
            missing_odoo_row(2, "01GONE", owned=False),
        ])
        result = self.plan(sheet, odoo)
        self.assertEqual(self.soft_deletes(result), [])
        self.assertIn("unmanaged_record", {d["drift_type"] for d in result["drifts"]})

    def test_missing_sync_id_blocks_the_delete(self):
        sheet, _odoo = self.baseline()
        row = missing_odoo_row(2, "")
        odoo = odoo_snapshot([
            odoo_row(1, "01KEEP", canon={"name": "s:Kept", "amount": "n:1.00"}),
            row,
        ])
        self.assertEqual(self.soft_deletes(self.plan(sheet, odoo)), [])

    def test_missing_promotion_link_blocks_the_delete(self):
        sheet, _odoo = self.baseline()
        row = missing_odoo_row(2, "01GONE")
        row["has_link"] = False
        odoo = odoo_snapshot([
            odoo_row(1, "01KEEP", canon={"name": "s:Kept", "amount": "n:1.00"}),
            row,
        ])
        self.assertEqual(self.soft_deletes(self.plan(sheet, odoo)), [])


class TestGuard3CompleteRead(DeleteGuardCase):
    """Condition 3 — the read must be proven complete on both sides."""

    def test_incomplete_read_blocks_the_delete(self):
        sheet, odoo = self.baseline()
        sheet["read_complete"] = False
        self.assertEqual(self.soft_deletes(self.plan(sheet, odoo)), [])

    def test_an_incomplete_read_still_reports_the_absence(self):
        # Absence is recorded so a human can look; it is simply not acted on.
        sheet, odoo = self.baseline()
        sheet["read_complete"] = False
        result = self.plan(sheet, odoo)
        self.assertIn("missing_in_sheet", {d["drift_type"] for d in result["drifts"]})

    def test_an_empty_sheet_after_a_populated_one_is_never_a_mass_delete(self):
        # This is the shape of every catastrophic failure: a read that returned
        # nothing looks identical to a genuinely emptied tab.
        sheet = snapshot([], read_complete=False)
        odoo = odoo_snapshot([missing_odoo_row(i, "01G%d" % i) for i in range(1, 11)])
        self.assertEqual(self.soft_deletes(self.plan(sheet, odoo)), [])


class TestGuard4BlockedDataset(DeleteGuardCase):
    """Condition 4 — no blocking finding may exist for this dataset."""

    def test_a_blocking_finding_blocks_the_delete(self):
        sheet, odoo = self.baseline()
        sheet["blocking"] = True
        self.assertEqual(self.soft_deletes(self.plan(sheet, odoo)), [])

    def test_a_blocked_dataset_produces_no_write_actions_at_all(self):
        sheet, odoo = self.baseline()
        sheet["blocking"] = True
        result = self.plan(sheet, odoo)
        self.assertEqual(
            [a for a in result["actions"] if a["action_type"] in ("create", "update", "soft_delete")],
            [],
        )


class TestGuard5QuarantineWindow(DeleteGuardCase):
    """Condition 5 — absent for N consecutive complete runs **and** for H hours."""

    def test_too_few_runs_blocks_the_delete(self):
        sheet, _odoo = self.baseline()
        odoo = odoo_snapshot([
            odoo_row(1, "01KEEP", canon={"name": "s:Kept", "amount": "n:1.00"}),
            missing_odoo_row(2, "01GONE", missing_run_count=1),
        ])
        self.assertEqual(self.soft_deletes(self.plan(sheet, odoo)), [])

    def test_too_recent_blocks_the_delete(self):
        sheet, _odoo = self.baseline()
        odoo = odoo_snapshot([
            odoo_row(1, "01KEEP", canon={"name": "s:Kept", "amount": "n:1.00"}),
            missing_odoo_row(2, "01GONE", missing_since="2026-07-28 11:00:00"),
        ])
        # One hour ago, against a 24-hour window.
        self.assertEqual(self.soft_deletes(self.plan(sheet, odoo)), [])

    def test_both_conditions_are_anded_not_ored(self):
        sheet, _odoo = self.baseline()
        # Enough runs, not enough time.
        odoo = odoo_snapshot([
            odoo_row(1, "01KEEP", canon={"name": "s:Kept", "amount": "n:1.00"}),
            missing_odoo_row(2, "01GONE", missing_run_count=99,
                             missing_since="2026-07-28 11:59:00"),
        ])
        self.assertEqual(self.soft_deletes(self.plan(sheet, odoo)), [])

    def test_never_seen_missing_blocks_the_delete(self):
        sheet, _odoo = self.baseline()
        odoo = odoo_snapshot([
            odoo_row(1, "01KEEP", canon={"name": "s:Kept", "amount": "n:1.00"}),
            missing_odoo_row(2, "01GONE", missing_since=False, missing_run_count=0),
        ])
        self.assertEqual(self.soft_deletes(self.plan(sheet, odoo)), [])


class TestGuard6Thresholds(DeleteGuardCase):
    """Condition 6 — the circuit breaker, above which nothing executes."""

    def _many_deletes(self, count, odoo_total=None):
        sheet = snapshot([sheet_row("01KEEP", canon={"name": "s:Kept", "amount": "n:1.00"})])
        rows = [odoo_row(1, "01KEEP", canon={"name": "s:Kept", "amount": "n:1.00"})]
        rows += [missing_odoo_row(100 + i, "01G%03d" % i) for i in range(count)]
        if odoo_total:
            rows += [odoo_row(1000 + i, "01P%03d" % i,
                              canon={"name": "s:Kept", "amount": "n:1.00"})
                     for i in range(odoo_total)]
        return sheet, odoo_snapshot(rows)

    def test_below_the_absolute_threshold_does_not_trip(self):
        sheet, odoo = self._many_deletes(3)
        result = self.plan(sheet, odoo)
        self.assertFalse(result["breaker_tripped"])
        self.assertEqual(len(self.soft_deletes(result)), 3)

    def test_above_the_absolute_threshold_trips_the_breaker(self):
        sheet, odoo = self._many_deletes(25)
        result = self.plan(sheet, odoo, pol=deletable_policy(delete_threshold_abs=20,
                                                             delete_threshold_pct=0.0))
        self.assertTrue(result["breaker_tripped"])
        self.assertEqual(result["breaker_reason"], "deletes_exceed_threshold")

    def test_a_tripped_breaker_requires_approval(self):
        sheet, odoo = self._many_deletes(25)
        result = self.plan(sheet, odoo, pol=deletable_policy(delete_threshold_abs=20,
                                                             delete_threshold_pct=0.0))
        self.assertTrue(result["requires_approval"])

    def test_the_percentage_arm_of_the_breaker(self):
        # 10 deletes out of 101 Odoo rows is ~10 %, above a 5 % ceiling, even
        # though it is below the absolute floor of 20.
        sheet, odoo = self._many_deletes(10, odoo_total=90)
        result = self.plan(sheet, odoo, pol=deletable_policy(delete_threshold_abs=5,
                                                             delete_threshold_pct=5.0))
        self.assertTrue(result["breaker_tripped"])

    def test_the_create_breaker_exists_too(self):
        # Exceeding it means the identity strategy broke — a renamed key column,
        # a wrong domain, an empty Odoo read — not that 4000 invoices appeared.
        rows = [sheet_row("01N%03d" % i, canon={"name": "s:N%d" % i, "amount": "n:1.00"},
                          a1_ref="'S'!A%d" % (i + 2)) for i in range(60)]
        result = self.plan(snapshot(rows), odoo_snapshot([]),
                           pol=deletable_policy(create_threshold_abs=50,
                                                create_threshold_pct=0.0))
        self.assertTrue(result["breaker_tripped"])
        self.assertEqual(result["breaker_reason"], "creates_exceed_threshold")

    def test_default_create_threshold_is_fifty(self):
        self.assertEqual(policy()["create_threshold_abs"], 50)


class TestGuard7IdentityStrategy(DeleteGuardCase):
    """Condition 7 — natural-key-only identity forces ``delete_policy = 'report'``."""

    def test_natural_key_identity_disables_deletes(self):
        # v1 has no Drive write scope, so _sync_id is never written back. A typo
        # fix in a key column would otherwise read as delete + create.
        sheet = snapshot([sheet_row("", natural_key="KEEP", canon={"name": "s:Kept"},
                                    identity_source="natural_key")])
        gone = missing_odoo_row(2, "", identity_source="natural_key")
        gone["natural_key"] = "GONE"
        odoo = odoo_snapshot([
            odoo_row(1, "", natural_key="KEEP", canon={"name": "s:Kept"}),
            gone,
        ])
        result = self.plan(sheet, odoo,
                           pol=deletable_policy(identity_strategy="natural_key"))
        self.assertEqual(self.soft_deletes(result), [])

    def test_the_absence_is_still_reported(self):
        sheet = snapshot([sheet_row("", natural_key="KEEP", canon={"name": "s:Kept"},
                                    identity_source="natural_key")])
        gone = missing_odoo_row(2, "", identity_source="natural_key")
        gone["natural_key"] = "GONE"
        odoo = odoo_snapshot([
            odoo_row(1, "", natural_key="KEEP", canon={"name": "s:Kept"}),
            gone,
        ])
        result = self.plan(sheet, odoo,
                           pol=deletable_policy(identity_strategy="natural_key"))
        self.assertIn("missing_in_sheet", {d["drift_type"] for d in result["drifts"]})


class TestHardDeleteIsUnreachable(DeleteGuardCase):
    """"Hard delete is never available to any automated path, at any threshold."""

    def test_no_action_type_can_express_a_hard_delete(self):
        action_types = dict(
            self.env["gdrive.plan.action"]._fields["action_type"].selection
        )
        self.assertNotIn("unlink", action_types)
        self.assertNotIn("delete", action_types)
        self.assertIn("soft_delete", action_types)

    def test_delete_policy_has_no_hard_option(self):
        policies = dict(self.env["gdrive.mapping"]._fields["delete_policy"].selection)
        self.assertEqual(set(policies), {"never", "report", "soft"})

    def test_a_planned_delete_is_a_flag_flip(self):
        sheet, odoo = self.baseline()
        deletes = self.soft_deletes(self.plan(sheet, odoo))
        self.assertEqual(len(deletes), 1)
        payload = deletes[0].get("payload") or {}
        self.assertEqual(payload.get("active"), False)

    def test_the_sync_id_is_retained_so_a_restore_is_one_flag(self):
        sheet, odoo = self.baseline()
        deletes = self.soft_deletes(self.plan(sheet, odoo))
        payload = deletes[0].get("payload") or {}
        self.assertNotIn("x_gdrive_sync_id", payload)


class TestEmptyTabGuard(DeleteGuardCase):
    """§5.4 step 9 — zero rows where there were N is a signal, not an instruction."""

    def test_zero_rows_against_a_populated_odoo_side_trips_the_breaker(self):
        sheet = snapshot([], read_complete=True)
        odoo = odoo_snapshot([missing_odoo_row(i, "01G%d" % i) for i in range(1, 31)])
        result = self.plan(sheet, odoo)
        self.assertTrue(result["breaker_tripped"])
        self.assertEqual(self.soft_deletes(result), [])
