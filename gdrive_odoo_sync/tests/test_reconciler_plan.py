# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. Licensed under LGPL-3.
"""Lane E — the pure planner (SPEC §3.17, §5.5, §9.3, §9.4).

WHY purity is the property under test
=====================================
Dry-run and apply call the **same** function; the only difference is whether the
returned plan is executed. If they had separate code paths the preview would be
a lie, and a preview that is a lie is worse than no preview — the operator
approves what they were shown and something else happens.

So ``gdrive.reconciler.plan()`` must:

* perform **no ORM writes** (nothing is created, nothing is modified),
* make **no network calls**,
* read **no ambient clock** — ``now`` is injected, so a plan computed twice from
  the same inputs is byte-identical,

and it must classify differences honestly: a ``cosmetic`` or ``rounding`` delta
is *reported* and never auto-written, because writing one makes the value flap
between runs forever without ever converging.

The snapshot schema below is the plain-dict contract between the verifier and
the planner. It is deliberately free of recordsets: a planner that can touch the
ORM will eventually touch the ORM.
"""

from odoo.tests.common import TransactionCase

from odoo.addons.gdrive_odoo_sync.lib.hashing import h_row, h_row_folded

SV = "SPECV1"


def sheet_row(sync_id="", natural_key="", canon=None, a1_ref="'S'!A2",
              identity_source="sync_id", row_number=2):
    """One row of the sheet-side snapshot."""
    canon = canon or {}
    return {
        "sync_id": sync_id,
        "natural_key": natural_key,
        "identity_source": identity_source,
        "canon": dict(canon),
        "h_row": h_row(canon, SV).hex(),
        "h_row_folded": h_row_folded(canon, SV).hex(),
        "a1_ref": a1_ref,
        "row_number": row_number,
    }


def odoo_row(res_id, sync_id="", natural_key="", canon=None, owned=True):
    """One row of the Odoo-side snapshot, as ``read_odoo_snapshot`` returns it."""
    canon = canon or {}
    return {
        "res_id": res_id,
        "res_model": "res.partner",
        "sync_id": sync_id,
        "natural_key": natural_key,
        "canon": dict(canon),
        "h_row": h_row(canon, SV).hex(),
        "source_dataset": "1abcDEF/0" if owned else "",
        "write_date": "2026-07-01 00:00:00",
    }


def snapshot(rows, read_complete=True, blocking=False, tab_uid="1abcDEF/0"):
    return {
        "rows": list(rows),
        "row_count": len(rows),
        "read_complete": read_complete,
        "blocking": blocking,
        "tab_uid": tab_uid,
    }


def odoo_snapshot(rows, max_write_date="2026-07-01 00:00:00"):
    return {"rows": list(rows), "count": len(rows), "max_write_date": max_write_date}


def contract(columns=("name", "amount"), spec_version=SV):
    return {
        "spec_version": spec_version,
        "tab_uid": "1abcDEF/0",
        "target_model": "res.partner",
        "columns": [
            {"key": key, "ctype": "text" if key == "name" else "number",
             "authority": "sheet", "scale": 2, "rel_tol": 0.0, "abs_tol": 0.0}
            for key in columns
        ],
    }


def policy(**kw):
    """The mapping's execution policy, with SPEC §3.8 defaults."""
    base = {
        "identity_strategy": "sync_id_then_key",
        "create_allowed": True,
        "update_allowed": True,
        "delete_policy": "report",
        "soft_delete_field": "active",
        "auto_heal": False,
        "dry_run": True,
        "create_threshold_abs": 50,
        "create_threshold_pct": 20.0,
        "delete_threshold_abs": 20,
        "delete_threshold_pct": 5.0,
        "quarantine_runs": 2,
        "quarantine_hours": 24,
        "flap_limit": 3,
    }
    base.update(kw)
    return base


NOW = "2026-07-28 12:00:00"


class ReconcilerCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.reconciler = cls.env["gdrive.reconciler"]

    def plan(self, sheet, odoo, ctr=None, pol=None, now=NOW):
        return self.reconciler.plan(sheet, odoo, ctr or contract(), pol or policy(), now)


class TestPurity(ReconcilerCase):
    """§3.17 — no ORM writes, no network, no ambient clock."""

    def test_planning_creates_no_records(self):
        Plan = self.env["gdrive.plan"]
        Action = self.env["gdrive.plan.action"]
        Partner = self.env["res.partner"]
        before = (Plan.search_count([]), Action.search_count([]), Partner.search_count([]))

        sheet = snapshot([sheet_row("01A", canon={"name": "s:ACME", "amount": "n:1.00"})])
        self.plan(sheet, odoo_snapshot([]))

        after = (Plan.search_count([]), Action.search_count([]), Partner.search_count([]))
        self.assertEqual(before, after)

    def test_the_result_is_plain_data(self):
        sheet = snapshot([sheet_row("01A", canon={"name": "s:ACME"})])
        result = self.plan(sheet, odoo_snapshot([]))
        self.assertIsInstance(result, dict)
        self.assertIsInstance(result["actions"], list)
        for action in result["actions"]:
            self.assertIsInstance(action, dict)

    def test_the_same_inputs_produce_the_same_plan_twice(self):
        sheet = snapshot([
            sheet_row("01A", canon={"name": "s:ACME", "amount": "n:1.00"}),
            sheet_row("01B", canon={"name": "s:Bettr", "amount": "n:2.00"}, a1_ref="'S'!A3"),
        ])
        odoo = odoo_snapshot([odoo_row(1, "01A", canon={"name": "s:ACME", "amount": "n:9.00"})])
        first = self.plan(sheet, odoo)
        second = self.plan(sheet, odoo)
        self.assertEqual(first["actions"], second["actions"])

    def test_now_is_injected_not_read(self):
        # A planner that reads the clock cannot be replayed, and a plan that
        # cannot be replayed cannot be verified before being applied.
        sheet = snapshot([sheet_row("01A", canon={"name": "s:ACME"})])
        early = self.plan(sheet, odoo_snapshot([]), now="2026-01-01 00:00:00")
        late = self.plan(sheet, odoo_snapshot([]), now="2026-12-31 23:59:59")
        self.assertEqual(
            [a["action_type"] for a in early["actions"]],
            [a["action_type"] for a in late["actions"]],
        )


class TestIdentityCascade(ReconcilerCase):
    """§5.5 — sync_id, then natural key, then create; never row position."""

    def test_matched_sync_ids_are_paired(self):
        canon = {"name": "s:ACME", "amount": "n:1.00"}
        sheet = snapshot([sheet_row("01A", canon=canon)])
        odoo = odoo_snapshot([odoo_row(1, "01A", canon=canon)])
        result = self.plan(sheet, odoo)
        self.assertEqual([a for a in result["actions"] if a["action_type"] != "quarantine"], [])

    def test_sheet_only_row_becomes_a_create(self):
        sheet = snapshot([sheet_row("01A", canon={"name": "s:ACME"})])
        result = self.plan(sheet, odoo_snapshot([]))
        types = [a["action_type"] for a in result["actions"]]
        self.assertIn("create", types)

    def test_natural_key_match_backfills_the_sync_id(self):
        # This is how sync #1 bootstraps against pre-existing Odoo data that has
        # no ids anywhere: match on the declared key, then stamp the ULID.
        canon = {"name": "s:ACME", "amount": "n:1.00"}
        sheet = snapshot([sheet_row("", natural_key="KEY-ACME", canon=canon,
                                    identity_source="natural_key")])
        odoo = odoo_snapshot([odoo_row(1, "", natural_key="KEY-ACME", canon=canon)])
        result = self.plan(sheet, odoo)
        types = [a["action_type"] for a in result["actions"]]
        self.assertNotIn("create", types)
        self.assertTrue(result.get("backfills") or any(
            a.get("sync_id") for a in result["actions"]))

    def test_one_to_many_natural_key_match_is_a_multi_match(self):
        canon = {"name": "s:ACME"}
        sheet = snapshot([sheet_row("", natural_key="KEY-ACME", canon=canon,
                                    identity_source="natural_key")])
        odoo = odoo_snapshot([
            odoo_row(1, "", natural_key="KEY-ACME", canon=canon),
            odoo_row(2, "", natural_key="KEY-ACME", canon=canon),
        ])
        result = self.plan(sheet, odoo)
        drifts = {d["drift_type"] for d in result["drifts"]}
        self.assertIn("multi_match", drifts)
        self.assertNotIn("create", [a["action_type"] for a in result["actions"]])

    def test_a_multi_match_is_data_quality_not_drift(self):
        canon = {"name": "s:ACME"}
        sheet = snapshot([sheet_row("", natural_key="K", canon=canon,
                                    identity_source="natural_key")])
        odoo = odoo_snapshot([odoo_row(1, "", natural_key="K", canon=canon),
                              odoo_row(2, "", natural_key="K", canon=canon)])
        result = self.plan(sheet, odoo)
        multi = [d for d in result["drifts"] if d["drift_type"] == "multi_match"]
        self.assertTrue(multi)
        self.assertEqual(multi[0]["category"], "data_quality")

    def test_row_position_is_never_an_identity(self):
        # Reversing the sheet must not turn two updates into two creates plus
        # two deletes. This is the failure that a single user sort would cause.
        rows = [
            sheet_row("01A", canon={"name": "s:ACME"}, a1_ref="'S'!A2", row_number=2),
            sheet_row("01B", canon={"name": "s:Bettr"}, a1_ref="'S'!A3", row_number=3),
        ]
        odoo = odoo_snapshot([
            odoo_row(1, "01A", canon={"name": "s:ACME"}),
            odoo_row(2, "01B", canon={"name": "s:Bettr"}),
        ])
        forward = self.plan(snapshot(rows), odoo)
        backward = self.plan(snapshot(list(reversed(rows))), odoo)
        self.assertEqual(
            sorted(a["action_type"] for a in forward["actions"]),
            sorted(a["action_type"] for a in backward["actions"]),
        )
        self.assertNotIn("create", [a["action_type"] for a in backward["actions"]])


class TestFieldMismatchClassification(ReconcilerCase):
    """§9.4 — cosmetic, rounding, substantive."""

    def test_substantive_difference_yields_an_update(self):
        sheet = snapshot([sheet_row("01A", canon={"name": "s:ACME Foods", "amount": "n:1234.50"})])
        odoo = odoo_snapshot([odoo_row(1, "01A", canon={"name": "s:ACME Foods", "amount": "n:99.00"})])
        result = self.plan(sheet, odoo)
        updates = [a for a in result["actions"] if a["action_type"] == "update"]
        self.assertEqual(len(updates), 1)
        self.assertEqual([d["field"] for d in updates[0]["deltas"]], ["amount"])

    def test_only_differing_fields_appear_in_the_delta(self):
        # A full-record write stomps unmanaged fields and bumps write_date on
        # everything, poisoning the Odoo-side fast path.
        sheet = snapshot([sheet_row("01A", canon={"name": "s:ACME", "amount": "n:2.00"})])
        odoo = odoo_snapshot([odoo_row(1, "01A", canon={"name": "s:ACME", "amount": "n:1.00"})])
        updates = [a for a in self.plan(sheet, odoo)["actions"] if a["action_type"] == "update"]
        self.assertEqual({d["field"] for d in updates[0]["deltas"]}, {"amount"})

    def test_cosmetic_difference_is_reported_not_written(self):
        # Smart quotes and case. Writing these makes the value flap forever.
        sheet = snapshot([sheet_row("01A", canon={"name": "s:Bob’s Data"})])
        odoo = odoo_snapshot([odoo_row(1, "01A", canon={"name": "s:BOB'S DATA"})])
        result = self.plan(sheet, odoo)
        self.assertEqual([a for a in result["actions"] if a["action_type"] == "update"], [])
        mismatches = [d for d in result["drifts"] if d["drift_type"] == "field_mismatch"]
        self.assertTrue(mismatches)
        self.assertEqual(mismatches[0]["delta_class"], "cosmetic")

    def test_canonical_forms_are_reported_verbatim(self):
        # Debuggability depends on these two strings being exactly what was
        # hashed — not re-rendered, not trimmed, not prettified.
        sheet = snapshot([sheet_row("01A", canon={"name": "s:ACME Foods"})])
        odoo = odoo_snapshot([odoo_row(1, "01A", canon={"name": "s:ACME  Foods"})])
        drifts = [d for d in self.plan(sheet, odoo)["drifts"]
                  if d["drift_type"] == "field_mismatch"]
        self.assertEqual(drifts[0]["canon_sheet"], "s:ACME Foods")
        self.assertEqual(drifts[0]["canon_odoo"], "s:ACME  Foods")

    def test_every_drift_cites_its_cell(self):
        sheet = snapshot([sheet_row("01A", canon={"name": "s:ACME"}, a1_ref="'Leads'!A412")])
        odoo = odoo_snapshot([odoo_row(1, "01A", canon={"name": "s:Other"})])
        drifts = self.plan(sheet, odoo)["drifts"]
        self.assertTrue(all(d.get("source_ref") for d in drifts if d["category"] == "drift"))
        self.assertIn("A412", drifts[0]["source_ref"])


class TestDriftAccounting(ReconcilerCase):
    """§9.3 — the three categories stay disjoint."""

    def test_counts_are_reported_separately(self):
        sheet = snapshot([sheet_row("01A", canon={"name": "s:ACME"})])
        odoo = odoo_snapshot([odoo_row(1, "01A", canon={"name": "s:Other"})])
        result = self.plan(sheet, odoo)
        for key in ("drift_count", "data_quality_count", "structural_count"):
            with self.subTest(key=key):
                self.assertIn(key, result)

    def test_data_quality_items_are_excluded_from_drift_count(self):
        # "12 drifts" must never silently mean "12 cells I could not read".
        canon = {"name": "s:ACME"}
        sheet = snapshot([sheet_row("", natural_key="K", canon=canon,
                                    identity_source="natural_key")])
        odoo = odoo_snapshot([odoo_row(1, "", natural_key="K", canon=canon),
                              odoo_row(2, "", natural_key="K", canon=canon)])
        result = self.plan(sheet, odoo)
        self.assertGreater(result["data_quality_count"], 0)
        self.assertEqual(
            result["drift_count"],
            len([d for d in result["drifts"] if d["category"] == "drift"]),
        )

    def test_missing_in_odoo_is_a_warning_not_a_blocker(self):
        sheet = snapshot([sheet_row("01A", canon={"name": "s:ACME"})])
        result = self.plan(sheet, odoo_snapshot([]))
        drifts = [d for d in result["drifts"] if d["drift_type"] == "missing_in_odoo"]
        self.assertTrue(drifts)
        self.assertEqual(drifts[0]["severity"], "warning")

    def test_missing_in_sheet_never_produces_an_immediate_delete(self):
        sheet = snapshot([])
        odoo = odoo_snapshot([odoo_row(1, "01A", canon={"name": "s:ACME"})])
        result = self.plan(sheet, odoo, pol=policy(delete_policy="report"))
        self.assertEqual([a for a in result["actions"] if a["action_type"] == "soft_delete"], [])
        self.assertIn("missing_in_sheet", {d["drift_type"] for d in result["drifts"]})

    def test_unmanaged_records_are_reported_and_never_touched(self):
        # Records a human created directly in Odoo are not the sync's to delete.
        sheet = snapshot([])
        odoo = odoo_snapshot([odoo_row(9, "", canon={"name": "s:Hand made"}, owned=False)])
        result = self.plan(sheet, odoo, pol=policy(delete_policy="soft"))
        self.assertEqual([a for a in result["actions"] if a["action_type"] == "soft_delete"], [])
        self.assertIn("unmanaged_record", {d["drift_type"] for d in result["drifts"]})


class TestAuthority(ReconcilerCase):
    """§9.5 — exactly one authority per column, and only ``sheet`` is written."""

    def test_odoo_authority_columns_are_reported_not_written(self):
        ctr = contract()
        ctr["columns"][1]["authority"] = "odoo"
        sheet = snapshot([sheet_row("01A", canon={"name": "s:ACME", "amount": "n:5.00"})])
        odoo = odoo_snapshot([odoo_row(1, "01A", canon={"name": "s:ACME", "amount": "n:9.00"})])
        result = self.plan(sheet, odoo, ctr=ctr)
        updates = [a for a in result["actions"] if a["action_type"] == "update"]
        for update in updates:
            self.assertNotIn("amount", [d["field"] for d in update["deltas"]])

    def test_report_authority_columns_are_never_written(self):
        ctr = contract()
        ctr["columns"][0]["authority"] = "report"
        sheet = snapshot([sheet_row("01A", canon={"name": "s:ACME"})])
        odoo = odoo_snapshot([odoo_row(1, "01A", canon={"name": "s:Other"})])
        result = self.plan(sheet, odoo, ctr=ctr)
        for update in [a for a in result["actions"] if a["action_type"] == "update"]:
            self.assertNotIn("name", [d["field"] for d in update["deltas"]])


class TestActionOrdering(ReconcilerCase):
    """§9.7 — the execution order is encoded in the plan, not in the executor."""

    def test_sequences_follow_the_documented_order(self):
        sheet = snapshot([
            sheet_row("01A", canon={"name": "s:ACME", "amount": "n:2.00"}),
            sheet_row("01NEW", canon={"name": "s:New", "amount": "n:1.00"}, a1_ref="'S'!A3"),
        ])
        odoo = odoo_snapshot([
            odoo_row(1, "01A", canon={"name": "s:ACME", "amount": "n:1.00"}),
            odoo_row(2, "01OLD", canon={"name": "s:Old", "amount": "n:1.00"}),
        ])
        result = self.plan(sheet, odoo, pol=policy(delete_policy="soft", quarantine_runs=0,
                                                   quarantine_hours=0))
        expected = {"writeback_sync_id": 10, "create": 20, "update": 30,
                    "soft_delete": 40, "quarantine": 50}
        for action in result["actions"]:
            with self.subTest(action_type=action["action_type"]):
                self.assertEqual(action["sequence"], expected[action["action_type"]])

    def test_ulids_are_generated_at_plan_time(self):
        # So a retried apply reuses the same id and the partial unique index
        # turns the retry into a no-op update instead of a duplicate.
        from odoo.addons.gdrive_odoo_sync.lib.ulid import is_ulid
        sheet = snapshot([sheet_row("", natural_key="K", canon={"name": "s:New"},
                                    identity_source="natural_key")])
        result = self.plan(sheet, odoo_snapshot([]))
        creates = [a for a in result["actions"] if a["action_type"] == "create"]
        self.assertTrue(creates)
        self.assertTrue(is_ulid(creates[0]["sync_id"]), creates[0]["sync_id"])


class TestBlockedDataset(ReconcilerCase):
    """A blocking structural finding stops the dataset; it does not degrade it."""

    def test_a_blocking_flag_produces_no_write_actions(self):
        sheet = snapshot([sheet_row("01A", canon={"name": "s:ACME"})], blocking=True)
        result = self.plan(sheet, odoo_snapshot([]))
        write_types = {"create", "update", "soft_delete"}
        self.assertEqual([a for a in result["actions"] if a["action_type"] in write_types], [])

    def test_an_incomplete_read_produces_no_deletes(self):
        sheet = snapshot([], read_complete=False)
        odoo = odoo_snapshot([odoo_row(1, "01A", canon={"name": "s:ACME"})])
        result = self.plan(sheet, odoo, pol=policy(delete_policy="soft"))
        self.assertEqual([a for a in result["actions"] if a["action_type"] == "soft_delete"], [])
