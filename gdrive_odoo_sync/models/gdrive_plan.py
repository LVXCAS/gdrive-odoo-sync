# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. See LICENSE (LGPL-3) for full copyright and licensing details.
"""``gdrive.plan`` — the fingerprint-guarded change plan (SPEC §3.13, §9.6–§9.8).

WHY a plan exists at all
------------------------
Every write this module performs to a business record goes through one of these
records first. Dry-run and apply call the **same** planner
(``gdrive.reconciler.plan()``); the only difference is whether the resulting
actions are executed. If preview and apply had separate code paths the preview
would be a lie, and a preview that is a lie is worse than no preview — the
operator approves what they were shown and something else happens.

WHY the fingerprints
--------------------
A plan is a statement about a world that may have moved. Between the moment it
was computed and the moment a human clicks Apply, somebody can edit the sheet,
somebody can edit the Odoo records, or a deploy can bump ``spec_version`` and
invalidate every cached hash. All seven fingerprints
(``drive_version``, ``drive_modified``, ``odoo_count``, ``odoo_max_write_date``,
``h_sheet``, ``h_odoo``, ``spec_version``) are captured at plan time and
re-read immediately before execution. Any movement turns "someone edited the
sheet between preview and approval" from a **corruption** into a **retry**.

WHY the circuit breaker leans so hard on deletes
------------------------------------------------
SPEC §9.6. Creates and updates are asserted by positive data present in the
source. Deletes are inferred from **absence** — and absence is precisely what
every read failure looks like: an expired token, a renamed tab, a partial
``batchGet``, a range that stopped at row 1000, a wrong Odoo domain. Every one
of those maps exactly onto "delete everything". There is no read bug whose
signature is "invent 4000 new rows". So an incomplete read or an empty tab
**trips the breaker** rather than planning deletions, and no configuration of
this model can express a hard delete.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from .gdrive_plan_action import SEQUENCE_BY_TYPE

_logger = logging.getLogger(__name__)

#: Fallback plan lifetime, in hours, when ``gdrive_odoo_sync.plan_expiry_hours``
#: is missing or unparseable. SPEC §3.13: plans expire 24 h after creation.
DEFAULT_EXPIRY_HOURS = 24

#: The complete breaker vocabulary of SPEC §3.13. Ordered by *evidence class*,
#: not by severity: the four structural reasons are evaluated before either
#: threshold, because a structural failure means the counts themselves are not
#: trustworthy — tripping on "25 deletes" when the real problem is "the tab was
#: empty" would send the operator hunting for 25 missing rows that never left.
BREAKER_REASONS = (
    'read_incomplete',
    'empty_tab',
    'header_blocked',
    'duplicate_identity',
    'deletes_exceed_threshold',
    'creates_exceed_threshold',
)

#: ``gdrive.dataset.block_reason`` values that mean the *shape* of the data can
#: no longer be trusted, as opposed to its contents.
STRUCTURAL_BLOCK_REASONS = (
    'mapped_column_missing',
    'tab_missing',
    'header_changed',
    'spec_mismatch',
    'access_lost',
    'file_trashed',
)


class GdrivePlan(models.Model):
    """A serialized, fingerprinted set of changes awaiting a human decision."""

    _name = 'gdrive.plan'
    _description = 'Google Drive Sync Change Plan'
    _order = 'create_date desc'
    _rec_name = 'name'

    name = fields.Char(
        string='Reference', compute='_compute_name', store=True, readonly=True, index=True)

    verification_id = fields.Many2one(
        'gdrive.verification', string='Verification', index=True, ondelete='cascade',
        help='The verification this plan was derived from. Optional only so that '
             'a promotion plan built directly from a staged dataset — the first '
             'load, which has nothing to verify against yet — is expressible '
             'through exactly the same machinery.',
    )
    dataset_id = fields.Many2one(
        'gdrive.dataset', string='Dataset', index=True, ondelete='cascade',
        help='Denormalized from the verification at create time rather than '
             'declared `related`: a promotion plan is built straight from a '
             'dataset with no verification behind it, and the scope must stay '
             'writable for that path.',
    )
    mapping_id = fields.Many2one(
        'gdrive.mapping', string='Mapping', index=True, ondelete='cascade')
    run_id = fields.Many2one(
        'gdrive.sync.run', string='Run', index=True, ondelete='set null')

    state = fields.Selection(
        [
            ('preview', 'Preview'),
            ('approved', 'Approved'),
            ('applied', 'Applied'),
            ('refused_stale', 'Refused (stale)'),
            ('aborted', 'Aborted'),
            ('expired', 'Expired'),
        ],
        string='Status', default='preview', required=True, index=True, copy=False,
    )
    dry_run = fields.Boolean(
        string='Dry Run', default=True,
        help='Ships True. A dry-run plan is computed and displayed in full but '
             'can never be executed — action_apply() refuses while this is set.',
    )

    # -- Fingerprints captured at plan time (SPEC §9.7) -------------------- #
    fp_drive_version = fields.Char(string='Drive Version', readonly=True)
    fp_drive_modified = fields.Datetime(string='Drive Modified', readonly=True)
    fp_odoo_count = fields.Integer(string='Odoo Row Count', readonly=True)
    fp_odoo_max_write_date = fields.Datetime(string='Odoo Max Write Date', readonly=True)
    fp_h_sheet = fields.Char(string='Sheet Dataset Hash', size=64, readonly=True)
    fp_h_odoo = fields.Char(string='Odoo Dataset Hash', size=64, readonly=True)
    fp_spec_version = fields.Char(string='Spec Version', readonly=True)

    # -- Counts ----------------------------------------------------------- #
    create_count = fields.Integer(
        string='Creates', compute='_compute_counts', store=True, aggregator='sum')
    update_count = fields.Integer(
        string='Updates', compute='_compute_counts', store=True, aggregator='sum')
    soft_delete_count = fields.Integer(
        string='Soft Deletes', compute='_compute_counts', store=True, aggregator='sum')
    quarantine_count = fields.Integer(
        string='Quarantines', compute='_compute_counts', store=True, aggregator='sum')

    # -- Circuit breaker -------------------------------------------------- #
    breaker_tripped = fields.Boolean(string='Breaker Tripped', index=True, copy=False)
    breaker_reason = fields.Char(
        string='Breaker Reason', index=True, copy=False,
        help='One of: %s.' % ', '.join(BREAKER_REASONS),
    )
    requires_approval = fields.Boolean(
        string='Needs Approval', compute='_compute_requires_approval',
        store=True, index=True,
    )

    approved_by_id = fields.Many2one('res.users', string='Approved By', readonly=True, copy=False)
    approved_date = fields.Datetime(string='Approved On', readonly=True, copy=False)
    applied_by_id = fields.Many2one('res.users', string='Applied By', readonly=True, copy=False)
    applied_date = fields.Datetime(string='Applied On', readonly=True, copy=False)
    apply_result = fields.Selection(
        [
            ('success', 'Success'),
            ('partial', 'Partial'),
            ('failed', 'Failed'),
            ('refused', 'Refused'),
        ],
        string='Apply Result', readonly=True, index=True, copy=False,
    )
    convergence_ok = fields.Boolean(
        string='Converged', readonly=True, copy=False,
        help='SPEC §9.8. False means the writer wrote what the comparator asked '
             'for and the two sides still disagree — an asymmetric normalization '
             'rule. The system alerts rather than retries, because retrying '
             'rewrites the same field every night forever.',
    )
    expiry_date = fields.Datetime(
        string='Expires', default=lambda self: self._default_expiry_date(), copy=False,
        help='A plan is a statement about a state of the world that was true '
             'when it was computed. Past this, it can never be applied.',
    )

    action_ids = fields.One2many('gdrive.plan.action', 'plan_id', string='Actions')

    # ------------------------------------------------------------------
    # Defaults and computes
    # ------------------------------------------------------------------
    @api.model
    def _default_expiry_date(self) -> datetime:
        """Now + ``gdrive_odoo_sync.plan_expiry_hours`` (default 24 h)."""
        hours = DEFAULT_EXPIRY_HOURS
        raw = self.env['ir.config_parameter'].sudo().get_param(
            'gdrive_odoo_sync.plan_expiry_hours', DEFAULT_EXPIRY_HOURS)
        try:
            hours = max(int(raw), 1)
        except (TypeError, ValueError):
            _logger.warning(
                'gdrive_odoo_sync.plan_expiry_hours is not an integer (%r); '
                'falling back to %d hours.', raw, DEFAULT_EXPIRY_HOURS)
        return fields.Datetime.add(fields.Datetime.now(), hours=hours)

    @api.depends('verification_id', 'verification_id.dataset_id', 'verification_id.mapping_id')
    def _compute_scope(self) -> None:
        """Denormalize dataset/mapping from the verification when it is known.

        ``readonly=False`` on both: a promotion plan is built straight from a
        dataset with no verification behind it, and the caller must be able to
        state the scope directly rather than inventing an empty verification.
        """
        for plan in self:
            verification = plan.verification_id
            if verification:
                plan.dataset_id = verification.dataset_id
                plan.mapping_id = verification.mapping_id
            else:
                # Preserve whatever the caller supplied; a compute must always
                # assign, or the ORM raises on the unset field.
                plan.dataset_id = plan.dataset_id
                plan.mapping_id = plan.mapping_id

    @api.depends('dataset_id', 'mapping_id', 'create_date')
    def _compute_name(self) -> None:
        """A stable, quotable reference: ``PLAN/<dataset>/<timestamp>``."""
        for plan in self:
            scope = plan.dataset_id.display_name or plan.mapping_id.display_name or _('unscoped')
            stamp = fields.Datetime.to_string(plan.create_date or fields.Datetime.now())
            plan.name = 'PLAN/%s/%s' % (scope, stamp)

    @api.depends('action_ids', 'action_ids.action_type')
    def _compute_counts(self) -> None:
        """Counts are derived from the actions, never asserted independently.

        A count that can disagree with the action list is a count that will
        eventually be used to approve a plan that does something else.
        """
        for plan in self:
            types = plan.action_ids.mapped('action_type')
            plan.create_count = types.count('create')
            plan.update_count = types.count('update')
            plan.soft_delete_count = types.count('soft_delete')
            plan.quarantine_count = types.count('quarantine')

    @api.depends('breaker_tripped', 'soft_delete_count', 'mapping_id.auto_heal')
    def _compute_requires_approval(self) -> None:
        """True when a human must look at this before anything executes.

        Any tripped breaker, any soft delete at all, or a mapping that has not
        opted in to ``auto_heal``. Deletes are in the list unconditionally and
        not merely above a threshold: the first wrongly archived record is
        already the expensive one.
        """
        for plan in self:
            plan.requires_approval = bool(
                plan.breaker_tripped
                or plan.soft_delete_count
                or not plan.mapping_id.auto_heal
            )

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    @api.model
    def _create_from_result(self, result: dict, dataset=None, mapping=None,
                            verification=None, run=None, dry_run: bool | None = None):
        """Materialize a ``gdrive.reconciler.plan()`` result into records.

        ``result`` is the planner's plain-dict output — no recordsets, no ORM
        writes — so this method is the *only* place the pure plan becomes
        persistent state. Fingerprints are captured here, at plan time, from the
        dataset the plan was computed against.

        Args:
            result: the reconciler's dict, with at least ``actions``; the
                breaker verdict is taken from it when present and recomputed
                here otherwise.
            dataset: ``gdrive.dataset`` the plan was computed against.
            mapping: ``gdrive.mapping`` in force.
            verification: the ``gdrive.verification`` that produced it, if any.
            run: the ``gdrive.sync.run`` this happened inside, if any.
            dry_run: overrides ``mapping.dry_run_default``.

        Returns:
            The created ``gdrive.plan`` record.
        """
        verification = verification or self.env['gdrive.verification']
        dataset = dataset or (verification.dataset_id if verification else None)
        mapping = mapping or (verification.mapping_id if verification else None)
        if dry_run is None:
            dry_run = bool(mapping.dry_run_default) if mapping else True

        vals = {
            'verification_id': verification.id if verification else False,
            'dataset_id': dataset.id if dataset else False,
            'mapping_id': mapping.id if mapping else False,
            'run_id': run.id if run else False,
            'state': 'preview',
            'dry_run': bool(dry_run),
        }
        vals.update(self._fingerprint_vals(dataset))
        plan = self.create(vals)

        actions = result.get('actions') or []
        if actions:
            self.env['gdrive.plan.action'].create([
                plan._action_vals(action) for action in actions
            ])
            plan.invalidate_recordset(['action_ids'])

        # The planner's verdict wins when it supplied one — it saw the raw
        # snapshots. Otherwise it is derived from what actually landed in the
        # plan, so a plan is never stored without a breaker verdict at all.
        if result.get('breaker_reason'):
            plan.write({
                'breaker_tripped': bool(result.get('breaker_tripped', True)),
                'breaker_reason': result['breaker_reason'],
            })
        else:
            tripped, reason = plan._evaluate_breaker()
            plan.write({'breaker_tripped': tripped, 'breaker_reason': reason or False})

        _logger.info(
            'Planned %s: %d create / %d update / %d soft-delete / %d quarantine '
            '(dry_run=%s, breaker=%s).',
            plan.name, plan.create_count, plan.update_count,
            plan.soft_delete_count, plan.quarantine_count,
            plan.dry_run, plan.breaker_reason or 'clear',
        )
        return plan

    def _action_vals(self, action: dict) -> dict:
        """Translate one planner action dict into ``gdrive.plan.action`` values."""
        self.ensure_one()
        action_type = action.get('action_type')
        return {
            'plan_id': self.id,
            'sequence': action.get('sequence') or SEQUENCE_BY_TYPE.get(action_type, 20),
            'action_type': action_type,
            'sync_id': action.get('sync_id') or False,
            'staged_row_id': action.get('staged_row_id') or False,
            'res_model': action.get('res_model') or False,
            'res_id': action.get('res_id') or 0,
            'payload': action.get('payload') or None,
            'deltas': action.get('deltas') or None,
            'source_ref': action.get('source_ref') or False,
            'state': 'pending',
        }

    # ------------------------------------------------------------------
    # Fingerprints
    # ------------------------------------------------------------------
    @api.model
    def _fingerprint_vals(self, dataset) -> dict:
        """The seven fingerprints, read off ``dataset`` as it stands right now."""
        if not dataset:
            return {}
        return {
            'fp_drive_version': dataset.last_drive_version or False,
            'fp_drive_modified': dataset.last_drive_modified or False,
            'fp_odoo_count': dataset.last_odoo_count or 0,
            'fp_odoo_max_write_date': dataset.last_odoo_max_write_date or False,
            'fp_h_sheet': dataset.h_dataset_sheet or False,
            'fp_h_odoo': dataset.h_dataset_odoo or False,
            'fp_spec_version': dataset.spec_version or False,
        }

    def _fingerprints_moved(self) -> list[str]:
        """Names of the fingerprints that differ from the dataset's current state.

        A fingerprint that was never captured is **not** compared. Comparing an
        unset value against a live one would make every plan built before a
        given field was populated permanently unappliable, and would report
        "stale" for a difference that is really "unknown" — an alarm nobody can
        act on is an alarm everybody learns to ignore. Plans built through
        :meth:`_create_from_result` always capture all seven.
        """
        self.ensure_one()
        dataset = self.dataset_id
        if not dataset:
            return []
        moved = []
        pairs = (
            ('fp_drive_version', dataset.last_drive_version),
            ('fp_drive_modified', dataset.last_drive_modified),
            ('fp_odoo_count', dataset.last_odoo_count),
            ('fp_odoo_max_write_date', dataset.last_odoo_max_write_date),
            ('fp_h_sheet', dataset.h_dataset_sheet),
            ('fp_h_odoo', dataset.h_dataset_odoo),
            ('fp_spec_version', dataset.spec_version),
        )
        for field_name, current in pairs:
            captured = self[field_name]
            if not captured:
                continue
            if (current or (0 if isinstance(captured, int) else False)) != captured:
                moved.append(field_name)
        return moved

    # ------------------------------------------------------------------
    # Circuit breaker
    # ------------------------------------------------------------------
    def _evaluate_breaker(self) -> tuple[bool, str]:
        """Re-derive the breaker verdict from the plan's own state.

        Structural reasons are checked first and unconditionally; the two
        threshold arms only afterwards. The ordering is the point: if the tab
        came back empty, "0 rows" is not a delete instruction and the delete
        *count* is a symptom, not the finding.

        Returns:
            ``(tripped, reason)`` where ``reason`` is '' when nothing tripped.
        """
        self.ensure_one()
        dataset = self.dataset_id
        mapping = self.mapping_id
        deletes = self.soft_delete_count
        creates = self.create_count

        if dataset and dataset.block_reason == 'duplicate_identity':
            return True, 'duplicate_identity'
        if dataset and dataset.block_reason in STRUCTURAL_BLOCK_REASONS:
            return True, 'header_blocked'

        if deletes:
            # Guard 3 of SPEC §9.6, at both levels. A delete inferred from a
            # read that was not proven complete is indistinguishable from a
            # delete inferred from an expired token.
            if self.run_id and not self.run_id.complete_read:
                return True, 'read_incomplete'
            if dataset and not dataset.last_read_complete:
                return True, 'read_incomplete'
            # Zero rows where there were N is a signal, not an instruction.
            if dataset and not dataset.row_count:
                return True, 'empty_tab'
            if dataset and dataset.block_reason == 'empty_tab':
                return True, 'empty_tab'

        if mapping:
            if deletes:
                rows_odoo = self.fp_odoo_count or (dataset.last_odoo_count if dataset else 0)
                ceiling = max(
                    mapping.delete_threshold_abs or 0,
                    int((mapping.delete_threshold_pct or 0.0) * rows_odoo / 100.0),
                )
                if deletes > ceiling:
                    return True, 'deletes_exceed_threshold'
            if creates:
                rows_sheet = dataset.row_count if dataset else 0
                ceiling = max(
                    mapping.create_threshold_abs or 0,
                    int((mapping.create_threshold_pct or 0.0) * rows_sheet / 100.0),
                )
                if creates > ceiling:
                    return True, 'creates_exceed_threshold'
        return False, ''

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------
    def _ensure_admin(self, what: str) -> None:
        """Group check in Python, not only in the view or the ACL.

        View ``groups=`` is a usability affordance and an ACL is per model; the
        boundary that matters here is "may execute a plan that writes to live
        business records", and it is enforced at the entry point itself.
        """
        if self.env.su or self.env.user.has_group('gdrive_odoo_sync.group_gdrive_admin'):
            return
        raise AccessError(_(
            'Only a Google Drive Sync Administrator may %s. Previewing a plan is '
            'non-mutating; %s writes to live business records.'
        ) % (what, what))

    def _is_expired(self) -> bool:
        self.ensure_one()
        return bool(self.expiry_date and self.expiry_date < fields.Datetime.now())

    def action_approve(self):
        """Record that a human accepted this change set. Does **not** execute it."""
        self.ensure_one()
        self._ensure_admin(_('approve a change plan'))
        if self.state != 'preview':
            raise UserError(_('Only a plan awaiting a decision can be approved.'))
        if self._is_expired():
            self.sudo().write({'state': 'expired'})
            raise UserError(_(
                'This plan expired on %s. The world it was computed against no '
                'longer exists; re-preview and decide again.'
            ) % self.expiry_date)
        self.sudo().write({
            'state': 'approved',
            'approved_by_id': self.env.user.id,
            'approved_date': fields.Datetime.now(),
        })
        _logger.info('%s approved by %s (breaker=%s, %d soft delete(s)).',
                     self.name, self.env.user.login, self.breaker_reason or 'clear',
                     self.soft_delete_count)
        return True

    def action_apply(self):
        """Execute the plan, after re-checking everything that could have moved.

        The refusal ladder, in order, and the reason for the order:

        1. **Group** — before anything is read or written.
        2. **State** — an unapproved plan has no human behind it.
        3. **Dry run** — the operator explicitly asked for a preview.
        4. **Expiry** — the outer bound; recorded as a state, not an exception,
           because it is an ordinary outcome of a plan sitting in a queue.
        5. **Fingerprints** — the fine-grained guard; likewise a state.
        6. **Breaker re-evaluation** — new structural evidence that appeared
           *after* approval was granted invalidates the approval, because the
           human approved a different situation.

        Steps 1–3 raise: they are operator errors, and silently doing nothing
        would be indistinguishable from success. Steps 4–6 record a state and
        return: they are the system correctly declining, which is a result, not
        a mistake.
        """
        self.ensure_one()
        self._ensure_admin(_('apply a change plan'))

        if self.state != 'approved':
            raise UserError(_(
                'A plan must be approved before it can be applied (this one is %s).'
            ) % self.state)
        if self.dry_run:
            raise UserError(_(
                'This plan is a dry run. Clear the dry-run flag deliberately '
                'before applying it — a preview that silently executes is the '
                'exact failure this flag exists to prevent.'
            ))

        if self._is_expired():
            _logger.warning('%s refused: expired at %s.', self.name, self.expiry_date)
            self.sudo().write({'state': 'expired', 'apply_result': 'refused'})
            return False

        moved = self._fingerprints_moved()
        if moved:
            _logger.warning(
                '%s refused as stale: %s moved between preview and apply.',
                self.name, ', '.join(moved))
            self.sudo().write({'state': 'refused_stale', 'apply_result': 'refused'})
            return False

        tripped, reason = self._evaluate_breaker()
        if tripped and reason != (self.breaker_reason or ''):
            # The approval covered the situation as it stood at approval time.
            # A *different* breaker reason is new evidence nobody has seen.
            _logger.warning(
                '%s refused: the circuit breaker now reports %r, which is not '
                'what was approved (%r).', self.name, reason, self.breaker_reason or '')
            self.sudo().write({
                'state': 'refused_stale',
                'apply_result': 'refused',
                'breaker_tripped': True,
                'breaker_reason': reason,
            })
            return False

        _logger.info('%s applying: %d action(s) by %s.',
                     self.name, len(self.action_ids), self.env.user.login)
        self.env['gdrive.promoter'].execute(self)

        vals = {
            'state': 'applied',
            'applied_by_id': self.env.user.id,
            'applied_date': fields.Datetime.now(),
        }
        if not self.apply_result:
            failed = self.action_ids.filtered(lambda a: a.state == 'failed')
            applied = self.action_ids.filtered(lambda a: a.state == 'applied')
            if failed and applied:
                vals['apply_result'] = 'partial'
            elif failed:
                vals['apply_result'] = 'failed'
            else:
                vals['apply_result'] = 'success'
        self.sudo().write(vals)

        self._check_convergence()
        return True

    # ------------------------------------------------------------------
    # Convergence (SPEC §9.8)
    # ------------------------------------------------------------------
    def _check_convergence(self) -> bool:
        """Read back everything that was written and assert it stuck.

        The bug class this catches is the killer: the comparator says A ≠ B, the
        writer writes A, but A round-trips through Odoo as A′, and A′ ≠ A under
        the normalizer — so the next run writes it again, forever. Without this
        assertion the symptom is invisible: the dashboard reports "3 fixes
        applied" every single night and everyone assumes it is working.

        On failure the system **alerts and stops writing that field** — it never
        retries. Retrying is what produces the nightly rewrite loop.
        """
        self.ensure_one()
        divergent: list[tuple[str, str]] = []
        applied = self.action_ids.filtered(
            lambda a: a.state == 'applied' and a.action_type in ('create', 'update'))

        for action in applied:
            expected = self._expected_values(action)
            if not expected:
                continue
            record = self._resolve_record(action)
            if not record:
                divergent.append((action.sync_id or '', '<record>'))
                continue
            for field_name, wanted in expected.items():
                if field_name not in record._fields:
                    continue
                if not self._values_agree(record[field_name], wanted):
                    divergent.append((action.sync_id or '', field_name))

        converged = not divergent
        self.sudo().write({'convergence_ok': converged})

        if converged:
            _logger.info('%s converged: %d written record(s) read back identical.',
                         self.name, len(applied))
        else:
            _logger.error(
                '%s did NOT converge. The writer wrote what the comparator asked '
                'for and the two sides still disagree, which means a '
                'normalization rule is asymmetric. Divergent (sync_id, field): %s',
                self.name, divergent[:20])
            self._raise_non_convergent_drift(divergent)

        self._update_flap_counters(applied, divergent)
        return converged

    def _expected_values(self, action) -> dict:
        """What this action asked Odoo to end up holding."""
        if action.action_type == 'create':
            return dict(action.payload or {})
        return {
            delta['field']: delta.get('to_typed')
            for delta in (action.deltas or [])
            if delta.get('field')
        }

    def _resolve_record(self, action):
        """The business record an applied action produced or touched."""
        if not action.res_model or action.res_model not in self.env:
            return None
        model = self.env[action.res_model].sudo().with_context(active_test=False)
        if action.res_id:
            record = model.browse(action.res_id).exists()
            if record:
                return record
        if action.sync_id and 'x_gdrive_sync_id' in model._fields:
            return model.search([('x_gdrive_sync_id', '=', action.sync_id)], limit=1) or None
        return None

    @api.model
    def _values_agree(self, actual, expected) -> bool:
        """Compare a stored ORM value with the value the plan asked for.

        Deliberately tolerant about *representation* and strict about *content*:
        a many2one reads back as a recordset and a date as a ``date`` object,
        neither of which is what went into the Json payload, and calling those
        divergences would make every plan non-convergent. A genuinely different
        value still fails.
        """
        if isinstance(actual, models.BaseModel):
            actual = actual.id or False
        if isinstance(actual, datetime):
            actual = fields.Datetime.to_string(actual)
        elif isinstance(actual, date):
            actual = fields.Date.to_string(actual)
        if actual == expected:
            return True
        if isinstance(actual, (int, float)) and isinstance(expected, (int, float)) \
                and not isinstance(actual, bool) and not isinstance(expected, bool):
            return abs(float(actual) - float(expected)) <= 1e-9
        if actual in (False, None, '') and expected in (False, None, ''):
            return True
        return str(actual) == str(expected)

    def _raise_non_convergent_drift(self, divergent: list[tuple[str, str]]) -> None:
        """Record a ``non_convergent`` drift at ``critical`` severity.

        Best-effort by design: a plan that executed correctly must not be rolled
        back because the *reporting* of a convergence failure failed. The
        exception is logged in full — never swallowed — and the
        ``convergence_ok`` flag has already been persisted.
        """
        self.ensure_one()
        if not self.verification_id:
            _logger.error(
                '%s is non-convergent but has no verification to hang a drift '
                'record on; the failure is recorded on the plan only.', self.name)
            return
        try:
            self.env['gdrive.drift'].sudo().create([{
                'verification_id': self.verification_id.id,
                'category': 'drift',
                'drift_type': 'non_convergent',
                'severity': 'critical',
                'sync_id': sync_id or False,
                'field_name': field_name or False,
                'resolution': 'open',
                'message': _(
                    'Applied but did not converge: %(field)s on %(sync)s reads '
                    'back differently from what was written. A normalization '
                    'rule is asymmetric; this field will not be rewritten.'
                ) % {'field': field_name, 'sync': sync_id or '-'},
            } for sync_id, field_name in divergent])
        except Exception:
            _logger.exception(
                'Could not record the non-convergence drift for %s. The '
                'convergence_ok flag on the plan is authoritative.', self.name)

    def _update_flap_counters(self, applied_actions, divergent: list[tuple[str, str]]) -> None:
        """Count consecutive runs in which a given ``(sync_id, field)`` was written.

        SPEC §9.8. At ``mapping.flap_limit`` (default 3) the link is marked
        ``non_convergent`` so the planner stops proposing that field at all.
        Counters for fields *not* written this run are reset, because the metric
        is consecutive writes: a field that settles must not carry its history
        forward and trip weeks later.

        Best-effort, for the same reason as the drift record: bookkeeping must
        not undo a successful apply.
        """
        self.ensure_one()
        mapping = self.mapping_id
        if not mapping:
            return
        written: dict[str, set[str]] = {}
        for action in applied_actions:
            if not action.sync_id:
                continue
            fields_written = set(self._expected_values(action).keys())
            if fields_written:
                written.setdefault(action.sync_id, set()).update(fields_written)
        if not written:
            return

        limit = mapping.flap_limit or 3
        divergent_ids = {sync_id for sync_id, _field in divergent}
        try:
            links = self.env['gdrive.promotion.link'].sudo().search([
                ('mapping_id', '=', mapping.id),
                ('sync_id', 'in', list(written)),
            ])
            for link in links:
                counters = dict(link.flap_counters or {})
                touched = written.get(link.sync_id, set())
                for field_name in list(counters):
                    if field_name not in touched:
                        counters.pop(field_name)
                for field_name in touched:
                    counters[field_name] = counters.get(field_name, 0) + 1
                vals = {'flap_counters': counters}
                flapping = sorted(f for f, n in counters.items() if n >= limit)
                if flapping and link.sync_id in divergent_ids:
                    vals['state'] = 'non_convergent'
                    vals['state_detail'] = _(
                        'Written on %(n)d consecutive runs without converging: '
                        '%(fields)s. These fields are no longer written.'
                    ) % {'n': limit, 'fields': ', '.join(flapping)}
                    _logger.error(
                        'Promotion link %s (%s) hit the flap limit on %s; the '
                        'planner will stop writing those fields.',
                        link.id, link.sync_id, ', '.join(flapping))
                link.write(vals)
        except Exception:
            _logger.exception(
                'Could not update flap counters for %s; convergence state on '
                'the plan is unaffected.', self.name)

    # ------------------------------------------------------------------
    # UI helpers
    # ------------------------------------------------------------------
    def action_abort(self):
        """Retire a plan nobody intends to apply."""
        self.ensure_one()
        self._ensure_admin(_('abort a change plan'))
        if self.state in ('applied',):
            raise UserError(_('An applied plan cannot be aborted; it already ran.'))
        self.sudo().write({'state': 'aborted'})
        _logger.info('%s aborted by %s.', self.name, self.env.user.login)
        return True

    def action_open_actions(self):
        """Open this plan's actions in their own list."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Planned Actions'),
            'res_model': 'gdrive.plan.action',
            'view_mode': 'list',
            'domain': [('plan_id', '=', self.id)],
            'context': {'default_plan_id': self.id},
        }
