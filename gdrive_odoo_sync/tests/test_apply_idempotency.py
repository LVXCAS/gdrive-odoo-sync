# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. Licensed under LGPL-3.
"""Lane E — plan execution: idempotency, staleness and batch failure (SPEC §9.7).

WHY every one of these properties is load-bearing
=================================================
* **Idempotency.** Plan execution is at-least-once by construction: a cron can
  be killed by ``limit_time_real``, a worker can be recycled mid-batch, an
  operator can double-click. Every ``create`` is therefore an upsert keyed by
  the partial unique index on ``x_gdrive_sync_id``, and the ULID is generated at
  **plan** time so a retry reuses the same id. Without both, a retried apply
  silently doubles the dataset.
* **Staleness.** ``apply()`` re-reads every fingerprint immediately before
  executing and refuses if any moved. That turns "someone edited the sheet
  between preview and approval" from a corruption into a retry.
* **Minimal writes.** An ``update`` writes only the differing fields. A
  full-record ``write()`` stomps fields the sync does not manage and bumps
  ``write_date`` on everything, which poisons the Odoo-side fast path and makes
  the next run do full work forever.
* **Batch atomicity.** One savepoint per 200 actions; on failure the batch rolls
  back and the rows are retried individually so one bad row cannot discard 199
  good ones — and cannot leave a half-applied batch either.
"""

from datetime import timedelta

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase

from odoo.addons.gdrive_odoo_sync.lib.ulid import new_ulid

GSHEET = "application/vnd.google-apps.spreadsheet"


class ApplyCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.connection = cls.env["gdrive.connection"].create({
            "name": "Test — lucaso@",
            "subject_email": "lucaso@avatarnaturalfoods.com",
        })
        cls.node = cls.env["gdrive.node"].create({
            "connection_id": cls.connection.id,
            "google_id": "1abcDEF",
            "name": "Investor Directory",
            "mime_type": GSHEET,
            "node_type": "spreadsheet",
        })
        cls.dataset = cls.env["gdrive.dataset"].create({
            "node_id": cls.node.id,
            "source_kind": "gsheet",
            "sheet_gid": 0,
            "tab_title": "Investors",
            "last_read_complete": True,
        })
        cls.partner_model = cls.env["ir.model"]._get("res.partner")
        cls.mapping = cls.env["gdrive.mapping"].create({
            "name": "Investors → Partners",
            "dataset_id": cls.dataset.id,
            "target_model_id": cls.partner_model.id,
            "identity_strategy": "sync_id",
        })
        cls.env["gdrive.mapping.column"].create([
            {
                "mapping_id": cls.mapping.id,
                "header_canon": "s:Name",
                "odoo_field_id": cls.env["ir.model.fields"]._get("res.partner", "name").id,
                "ctype": "text",
                "is_natural_key": True,
                "sequence": 10,
            },
            {
                "mapping_id": cls.mapping.id,
                "header_canon": "s:Reference",
                "odoo_field_id": cls.env["ir.model.fields"]._get("res.partner", "ref").id,
                "ctype": "text",
                "assert_string_value": True,
                "sequence": 20,
            },
        ])
        # Creates x_gdrive_sync_id / x_gdrive_source_dataset and the partial
        # unique index that makes every create an idempotent upsert.
        cls.mapping.action_validate()

    # -- helpers ---------------------------------------------------------- #

    def _plan(self, actions, **kw):
        vals = {
            "dataset_id": self.dataset.id,
            "mapping_id": self.mapping.id,
            "dry_run": False,
            "state": "approved",
            "expiry_date": fields.Datetime.now() + timedelta(hours=12),
        }
        vals.update(kw)
        plan = self.env["gdrive.plan"].create(vals)
        self.env["gdrive.plan.action"].create([
            dict(action, plan_id=plan.id) for action in actions
        ])
        return plan

    def _create_action(self, sync_id, name, ref, sequence=20):
        return {
            "sequence": sequence,
            "action_type": "create",
            "sync_id": sync_id,
            "res_model": "res.partner",
            "payload": {
                "name": name,
                "ref": ref,
                "x_gdrive_sync_id": sync_id,
                "x_gdrive_source_dataset": "1abcDEF/0",
            },
            "source_ref": "'Investors'!A2",
        }

    def _partners(self):
        return self.env["res.partner"].search([("x_gdrive_source_dataset", "=", "1abcDEF/0")])


class TestCreateIdempotency(ApplyCase):
    """Applying the same plan twice must produce identical state."""

    def test_first_apply_creates(self):
        sync_id = new_ulid()
        plan = self._plan([self._create_action(sync_id, "ACME Foods", "ACME-1")])
        self.env["gdrive.promoter"].execute(plan)
        partners = self._partners()
        self.assertEqual(len(partners), 1)
        self.assertEqual(partners.name, "ACME Foods")
        self.assertEqual(partners.x_gdrive_sync_id, sync_id)

    def test_second_apply_of_the_same_plan_creates_no_duplicate(self):
        sync_id = new_ulid()
        actions = [self._create_action(sync_id, "ACME Foods", "ACME-1")]
        self.env["gdrive.promoter"].execute(self._plan(actions))
        self.env["gdrive.promoter"].execute(self._plan(actions))
        self.assertEqual(len(self._partners()), 1)

    def test_a_retried_create_collapses_into_a_no_op_update(self):
        # The partial unique index on x_gdrive_sync_id is what makes this true;
        # without it the retry is a second partner with the same ULID.
        sync_id = new_ulid()
        self.env["gdrive.promoter"].execute(
            self._plan([self._create_action(sync_id, "ACME Foods", "ACME-1")]))
        first_id = self._partners().id
        self.env["gdrive.promoter"].execute(
            self._plan([self._create_action(sync_id, "ACME Foods", "ACME-1")]))
        self.assertEqual(self._partners().id, first_id)

    def test_ulids_are_stable_across_retries(self):
        # Generated at plan time, so the retry carries the same id rather than
        # minting a new one and creating a second record.
        sync_id = new_ulid()
        plan = self._plan([self._create_action(sync_id, "ACME Foods", "ACME-1")])
        self.env["gdrive.promoter"].execute(plan)
        self.assertEqual(plan.action_ids.sync_id, sync_id)
        self.env["gdrive.promoter"].execute(plan)
        self.assertEqual(plan.action_ids.sync_id, sync_id)

    def test_ownership_markers_are_written(self):
        # Without these, the record is UNMANAGED and can never be soft-deleted
        # by the sync — which is the correct default for anything it did not
        # create.
        sync_id = new_ulid()
        self.env["gdrive.promoter"].execute(
            self._plan([self._create_action(sync_id, "ACME Foods", "ACME-1")]))
        partner = self._partners()
        self.assertEqual(partner.x_gdrive_source_dataset, "1abcDEF/0")
        self.assertTrue(partner.x_gdrive_sync_id)

    def test_actions_record_their_outcome(self):
        plan = self._plan([self._create_action(new_ulid(), "ACME Foods", "ACME-1")])
        self.env["gdrive.promoter"].execute(plan)
        self.assertEqual(plan.action_ids.state, "applied")
        self.assertFalse(plan.action_ids.error)


class TestMinimalUpdates(ApplyCase):
    """An update writes only the fields that actually differ."""

    def _existing(self, sync_id, **kw):
        vals = {
            "name": "ACME Foods",
            "ref": "ACME-1",
            "comment": "hand-written note nobody should touch",
            "x_gdrive_sync_id": sync_id,
            "x_gdrive_source_dataset": "1abcDEF/0",
        }
        vals.update(kw)
        return self.env["res.partner"].create(vals)

    def test_only_the_differing_field_is_written(self):
        sync_id = new_ulid()
        partner = self._existing(sync_id)
        plan = self._plan([{
            "sequence": 30,
            "action_type": "update",
            "sync_id": sync_id,
            "res_model": "res.partner",
            "res_id": partner.id,
            "deltas": [{"field": "ref", "from": "s:ACME-1", "to": "s:ACME-2",
                        "to_typed": "ACME-2"}],
            "source_ref": "'Investors'!B2",
        }])
        self.env["gdrive.promoter"].execute(plan)
        self.assertEqual(partner.ref, "ACME-2")
        self.assertEqual(partner.name, "ACME Foods")
        self.assertEqual(partner.comment, "hand-written note nobody should touch")

    def test_unmanaged_fields_are_never_stomped(self):
        sync_id = new_ulid()
        partner = self._existing(sync_id, function="CFO")
        plan = self._plan([{
            "sequence": 30,
            "action_type": "update",
            "sync_id": sync_id,
            "res_model": "res.partner",
            "res_id": partner.id,
            "deltas": [{"field": "name", "from": "s:ACME Foods", "to": "s:ACME Holdings",
                        "to_typed": "ACME Holdings"}],
        }])
        self.env["gdrive.promoter"].execute(plan)
        self.assertEqual(partner.function, "CFO")

    def test_an_empty_delta_list_writes_nothing(self):
        sync_id = new_ulid()
        partner = self._existing(sync_id)
        before = partner.write_date
        plan = self._plan([{
            "sequence": 30,
            "action_type": "update",
            "sync_id": sync_id,
            "res_model": "res.partner",
            "res_id": partner.id,
            "deltas": [],
        }])
        self.env["gdrive.promoter"].execute(plan)
        partner.invalidate_recordset()
        self.assertEqual(partner.write_date, before,
                         "a no-op write would poison the Odoo-side fast path")


class TestSoftDelete(ApplyCase):
    """Execution is a flag flip; the identity is retained."""

    def test_soft_delete_archives_rather_than_unlinks(self):
        sync_id = new_ulid()
        partner = self.env["res.partner"].create({
            "name": "Gone Corp",
            "x_gdrive_sync_id": sync_id,
            "x_gdrive_source_dataset": "1abcDEF/0",
        })
        plan = self._plan([{
            "sequence": 40,
            "action_type": "soft_delete",
            "sync_id": sync_id,
            "res_model": "res.partner",
            "res_id": partner.id,
            "payload": {"active": False},
        }])
        self.env["gdrive.promoter"].execute(plan)
        partner.invalidate_recordset()
        self.assertTrue(partner.exists())
        self.assertFalse(partner.active)
        self.assertEqual(partner.x_gdrive_sync_id, sync_id,
                         "the identity must survive so a restore is one flag flip")

    def test_deletes_are_skipped_when_an_earlier_action_failed(self):
        # An error earlier in the run is evidence that our view of the world is
        # incomplete, and deletes are the one action whose blast radius is
        # unbounded.
        doomed = new_ulid()
        victim_id = self.env["res.partner"].create({
            "name": "Gone Corp",
            "x_gdrive_sync_id": new_ulid(),
            "x_gdrive_source_dataset": "1abcDEF/0",
        })
        plan = self._plan([
            {
                "sequence": 30,
                "action_type": "update",
                "sync_id": doomed,
                "res_model": "res.partner",
                "res_id": 0,  # deliberately unresolvable
                "deltas": [{"field": "name", "from": "s:a", "to": "s:b", "to_typed": "b"}],
            },
            {
                "sequence": 40,
                "action_type": "soft_delete",
                "sync_id": victim_id.x_gdrive_sync_id,
                "res_model": "res.partner",
                "res_id": victim_id.id,
                "payload": {"active": False},
            },
        ])
        self.env["gdrive.promoter"].execute(plan)
        victim_id.invalidate_recordset()
        self.assertTrue(victim_id.active, "a soft delete must not run after an earlier failure")
        delete_action = plan.action_ids.filtered(lambda a: a.action_type == "soft_delete")
        self.assertEqual(delete_action.state, "skipped")


class TestStaleness(ApplyCase):
    """``apply()`` refuses if the world moved between preview and approval."""

    def _fingerprinted_plan(self):
        self.dataset.sudo().write({
            "last_drive_version": "41",
            "h_dataset_sheet": "a" * 64,
            "h_dataset_odoo": "b" * 64,
            "spec_version": "SPECV1",
            "last_odoo_count": 1,
        })
        return self._plan(
            [self._create_action(new_ulid(), "ACME Foods", "ACME-1")],
            state="approved",
            fp_drive_version="41",
            fp_h_sheet="a" * 64,
            fp_h_odoo="b" * 64,
            fp_spec_version="SPECV1",
            fp_odoo_count=1,
        )

    def test_a_matching_fingerprint_applies(self):
        plan = self._fingerprinted_plan()
        plan.action_apply()
        self.assertEqual(plan.state, "applied")

    def test_a_moved_drive_version_is_refused_as_stale(self):
        plan = self._fingerprinted_plan()
        self.dataset.sudo().write({"last_drive_version": "42"})
        plan.action_apply()
        self.assertEqual(plan.state, "refused_stale")
        self.assertEqual(len(self._partners()), 0)

    def test_a_moved_sheet_hash_is_refused_as_stale(self):
        plan = self._fingerprinted_plan()
        self.dataset.sudo().write({"h_dataset_sheet": "c" * 64})
        plan.action_apply()
        self.assertEqual(plan.state, "refused_stale")

    def test_a_moved_spec_version_is_refused_as_stale(self):
        # A spec_version bump mid-deploy invalidates every cached hash, so the
        # plan was computed against a different normalizer entirely.
        plan = self._fingerprinted_plan()
        self.dataset.sudo().write({"spec_version": "SPECV2"})
        plan.action_apply()
        self.assertEqual(plan.state, "refused_stale")

    def test_an_expired_plan_refuses(self):
        plan = self._fingerprinted_plan()
        plan.sudo().write({"expiry_date": fields.Datetime.now() - timedelta(hours=1)})
        plan.action_apply()
        self.assertIn(plan.state, ("expired", "refused_stale"))
        self.assertEqual(len(self._partners()), 0)

    def test_a_dry_run_plan_cannot_be_applied(self):
        plan = self._fingerprinted_plan()
        plan.sudo().write({"dry_run": True})
        with self.assertRaises(UserError):
            plan.action_apply()
        self.assertEqual(len(self._partners()), 0)

    def test_an_unapproved_plan_cannot_be_applied(self):
        plan = self._fingerprinted_plan()
        plan.sudo().write({"state": "preview"})
        with self.assertRaises(UserError):
            plan.action_apply()


class TestBatchFailure(ApplyCase):
    """One bad row must not discard the good ones, nor leave a half-applied batch."""

    def test_a_failing_action_does_not_prevent_the_others(self):
        good = new_ulid()
        plan = self._plan([
            self._create_action(good, "Good Corp", "G-1"),
            {
                "sequence": 20,
                "action_type": "create",
                "sync_id": new_ulid(),
                "res_model": "res.partner",
                # `nonexistent_field` cannot be written; this row must fail
                # alone rather than taking the batch with it.
                "payload": {"name": "Bad Corp", "nonexistent_field": 1,
                            "x_gdrive_source_dataset": "1abcDEF/0"},
            },
        ])
        self.env["gdrive.promoter"].execute(plan)
        names = self._partners().mapped("name")
        self.assertIn("Good Corp", names)
        self.assertNotIn("Bad Corp", names)

    def test_the_failing_action_records_its_error(self):
        plan = self._plan([{
            "sequence": 20,
            "action_type": "create",
            "sync_id": new_ulid(),
            "res_model": "res.partner",
            "payload": {"name": "Bad Corp", "nonexistent_field": 1},
        }])
        self.env["gdrive.promoter"].execute(plan)
        action = plan.action_ids
        self.assertEqual(action.state, "failed")
        self.assertTrue(action.error, "a failure must never be swallowed")

    def test_the_plan_result_reports_partial_success(self):
        plan = self._plan([
            self._create_action(new_ulid(), "Good Corp", "G-1"),
            {
                "sequence": 20,
                "action_type": "create",
                "sync_id": new_ulid(),
                "res_model": "res.partner",
                "payload": {"name": "Bad Corp", "nonexistent_field": 1},
            },
        ])
        self.env["gdrive.promoter"].execute(plan)
        self.assertEqual(plan.apply_result, "partial")


class TestApplyIsAdminGated(ApplyCase):
    """ACL alone is not the guard; ``action_apply()`` checks the group in Python."""

    def test_a_manager_cannot_apply(self):
        manager = self.env["res.users"].create({
            "name": "Mapping Manager",
            "login": "gdrive_manager_test",
            "groups_id": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref("gdrive_odoo_sync.group_gdrive_manager").id,
            ])],
        })
        plan = self._plan([self._create_action(new_ulid(), "ACME Foods", "ACME-1")])
        with self.assertRaises((AccessError, UserError)):
            plan.with_user(manager).action_apply()

    def test_an_admin_can_apply(self):
        admin = self.env["res.users"].create({
            "name": "GDrive Admin",
            "login": "gdrive_admin_test",
            "groups_id": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref("gdrive_odoo_sync.group_gdrive_admin").id,
            ])],
        })
        plan = self._plan([self._create_action(new_ulid(), "ACME Foods", "ACME-1")])
        plan.with_user(admin).action_apply()
        self.assertEqual(plan.state, "applied")


class TestMappingIsOptIn(ApplyCase):
    """Nothing is promoted until a human deliberately enables the mapping."""

    def test_enabled_ships_false(self):
        fresh = self.env["gdrive.mapping"].create({
            "name": "Another",
            "dataset_id": self.dataset.id,
            "target_model_id": self.partner_model.id,
        })
        self.assertFalse(fresh.enabled)

    def test_auto_heal_ships_false(self):
        self.assertFalse(self.mapping.auto_heal)

    def test_dry_run_default_ships_true(self):
        self.assertTrue(self.mapping.dry_run_default)

    def test_delete_policy_defaults_to_report(self):
        self.assertEqual(self.mapping.delete_policy, "report")

    def test_writeback_is_permanently_unavailable_in_v1(self):
        # There is no Drive write scope, structurally.
        self.assertFalse(self.mapping.writeback_sync_id)
