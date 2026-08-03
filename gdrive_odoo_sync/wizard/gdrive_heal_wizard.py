# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. Licensed under LGPL-3.
"""``gdrive.heal.wizard`` — the approval surface (SPEC §3.16 line 671, §9).

This is the only place in the whole addon where a human turns a report into a
write against a live Odoo business record. Every other lane in this module —
discovery, staging, verification — only ever reads Drive and writes to this
addon's own tables. The moment a soft delete or a create lands on
``res.partner`` or wherever a mapping points, it happened because a plan
passed through :meth:`GdriveHealWizard.action_apply` on *this* file.

WHY preview and apply are not two code paths
=============================================
:meth:`action_preview` never plans anything itself. It calls
``gdrive.mapping._promote_once(dry_run=True)``, which reads both snapshots,
calls ``gdrive.reconciler.plan()`` — the pure planner, no ORM writes, no
network, no ambient clock — and persists the result as a fresh ``gdrive.plan``
+ ``gdrive.plan.action`` rows. :meth:`action_apply` executes that *exact* same
plan through ``gdrive.promoter.execute()``. If preview recomputed one thing and
apply executed another, the preview would be a lie, and a preview that lies is
worse than no preview: the operator approves what they were shown and
something else happens. So every single Preview click recomputes from scratch
against the *current* state of the world — it never reuses a stale plan.

WHY the wizard re-implements the four refusal checks instead of calling
``gdrive.plan.action_apply()``
=================================================================
``gdrive.plan`` already has an ``action_apply()`` that is admin-gated, refuses
on a stale fingerprint and refuses on expiry — but it executes *every* pending
action on the plan unconditionally. This wizard adds one more degree of
freedom on top: a human may untick individual lines (``gdrive.heal.wizard.line
.selected``) after reading them, e.g. because they are not sure about one
particular soft delete. That per-line decision has to change what actually
executes, and it can only do that by marking the corresponding
``gdrive.plan.action`` rows ``skipped`` *before* the promoter runs — which
means this method, not ``gdrive.plan.action_apply()``, has to be the one
driving execution. So the four safety checks are re-asserted here, explicitly,
in Python, immediately before the one call that is allowed to write:
``self.env['gdrive.promoter'].execute(plan)``. This wizard itself never calls
``create``/``write``/``unlink`` on any business model.

WHY the per-line ``selected`` toggle lives on its own transient model
=======================================================================
``gdrive_odoo_sync/security/gdrive_security.xml`` (top of file) explains this
at length: a manager who could write ``gdrive.plan.action`` directly could
retarget an admin-approved plan to a different record before the admin
executes it, and the apply-time fingerprints describe the *data*, not the
plan's own action rows, so they would not catch it. Putting the toggle on
``gdrive.heal.wizard.line`` — a model managers own outright — means the worst
a manager can do is mis-configure a wizard nobody has approved yet; the
persistent ``gdrive.plan.action`` rows an admin is about to execute are never
exposed to manager write access at all.

WHY breaker warnings and the delete banner are as loud as they are
=====================================================================
SPEC §9.6: a wrongly created record is deleted in seconds; a wrongly deleted
one takes its journal entries, attachments, message threads and many2one
back-references with it, and often cannot be restored at all. Deletes are
inferred from *absence*, and absence is exactly what every read failure looks
like. So :meth:`_build_breaker_warning` renders the tripped reason in plain
English before a single checkbox can be unticked, and every fragment of
sheet/Drive-derived text that reaches the ``Html`` field is escaped — this
file is rendered in a browser, and canonical forms are attacker-influenced
strings straight out of a spreadsheet cell.
"""

import logging

from odoo import _, fields, models
from odoo.exceptions import UserError
from odoo.tools import html_escape

from ..models.gdrive_plan_action import ACTION_TYPE_SELECTION

_logger = logging.getLogger(__name__)

#: Plain-English explanation of each SPEC §3.13 breaker reason, rendered into
#: ``breaker_warning``. Not wrapped in ``_()`` at module scope — Odoo's
#: translation function is meant to be called where a request/environment is
#: live, not at import time — the same convention this addon already uses for
#: other module-level vocabulary strings (see ``gdrive.plan.BREAKER_REASONS``).
_BREAKER_EXPLANATIONS = {
    'creates_exceed_threshold': (
        'The number of records this plan would CREATE exceeds the mapping\'s '
        'threshold. This almost always means the identity strategy broke — a '
        'renamed key column, a wrong domain, an empty Odoo read — not that '
        'thousands of genuinely new rows appeared overnight.'
    ),
    'deletes_exceed_threshold': (
        'The number of records this plan would SOFT DELETE exceeds the '
        'mapping\'s threshold. Deletes are inferred from absence, and absence '
        'is exactly what a partial read, an expired token, a renamed tab and a '
        'wrong Odoo domain all look like too.'
    ),
    'read_incomplete': (
        'The most recent read of this dataset — or of its Odoo counterpart — '
        'did not complete. An incomplete read is byte-identical to "every row '
        'was deleted", so no delete may be planned from it.'
    ),
    'empty_tab': (
        'The sheet tab read back as zero rows where a previous complete read '
        'saw rows that exist as owned Odoo records. This is treated as a '
        'failed read, never as "every row was deleted".'
    ),
    'header_blocked': (
        'This dataset is structurally blocked — a mapped column is missing, '
        'the tab itself is gone, a header was renamed, or access was lost. Its '
        'shape cannot currently be trusted, so nothing about its content can be '
        'trusted either.'
    ),
    'duplicate_identity': (
        'The same identity (sync id or natural key) is claimed by more than '
        'one row, on the sheet side, the Odoo side, or both. Writing either '
        'claimant would be a coin flip, so neither is written.'
    ),
}

_UNKNOWN_BREAKER_EXPLANATION = (
    'The circuit breaker tripped for a reason this wizard does not recognize. '
    'Treat this as blocking until a human has read the plan\'s action list in '
    'full.'
)


def _describe_action(action) -> str:
    """A one-line, human-readable summary of one ``gdrive.plan.action``.

    Built strictly from the fields already on the action record — never
    re-derived from the sheet, never re-canonicalized — so this description can
    never claim a different change than the one that will actually execute.
    Kept as a free function, not a method, because it is a pure function of one
    action record and has no business reading wizard state.
    """
    target = (
        '%s #%d' % (action.res_model, action.res_id) if action.res_id
        else (action.res_model or _('(new record)'))
    )

    if action.action_type == 'create':
        payload = action.payload or {}
        shown = sorted(k for k in payload
                        if k not in ('x_gdrive_sync_id', 'x_gdrive_source_dataset'))
        return _('Create %(model)s with %(fields)s.') % {
            'model': action.res_model or '?',
            'fields': ', '.join(shown) or _('no writable fields'),
        }

    if action.action_type == 'update':
        deltas = action.deltas or []
        shown = ['%s: %s → %s' % (d.get('field'), d.get('from'), d.get('to'))
                 for d in deltas[:3]]
        more = _(' (+%d more)') % (len(deltas) - 3) if len(deltas) > 3 else ''
        return _('Update %(target)s — %(deltas)s%(more)s.') % {
            'target': target,
            'deltas': '; '.join(shown) or _('no field changes'),
            'more': more,
        }

    if action.action_type == 'soft_delete':
        flag = next(iter(action.payload or {}), 'active')
        return _(
            'Soft delete %(target)s (sets %(flag)s = False; x_gdrive_sync_id is '
            'retained, so a restore is one flag flip).'
        ) % {'target': target, 'flag': flag}

    if action.action_type == 'quarantine':
        payload = action.payload or {}
        return _('Quarantine (%(reason)s): %(detail)s') % {
            'reason': payload.get('quarantine_reason') or '?',
            'detail': payload.get('quarantine_detail') or '',
        }

    if action.action_type == 'writeback_sync_id':
        return _('Stamp sync id %(sync_id)s onto %(target)s (first-sync ownership bootstrap).') % {
            'sync_id': action.sync_id or '?', 'target': target,
        }

    return action.action_type or _('(unrecognized action)')


class GdriveHealWizard(models.TransientModel):
    """The approval surface: preview a plan, review it, decide, apply it."""

    _name = 'gdrive.heal.wizard'
    _description = 'Google Drive Sync Heal Wizard'

    plan_id = fields.Many2one(
        'gdrive.plan', string='Plan',
        help='An existing plan to review and apply. Preview still recomputes '
             'it from scratch through the reconciler — this field only says '
             'which dataset/mapping to recompute for.',
    )
    dataset_ids = fields.Many2many(
        'gdrive.dataset', string='Datasets',
        help='Datasets to plan a heal for, when there is no plan yet. Exactly '
             'one at a time: one Apply click must correspond to one set of '
             'fingerprints an admin can unambiguously approve, never a blend '
             'of several datasets\' worth of deletes into one decision.',
    )
    dry_run = fields.Boolean(
        string='Dry Run', default=True,
        help='Ships True on every new wizard. While this is on, Apply is '
             'hidden by the view AND refused in Python if it is ever reached '
             'by another route — untying it is a deliberate, separate act from '
             'clicking Apply.',
    )
    state = fields.Selection(
        [
            ('setup', 'Setup'),
            ('preview', 'Preview'),
            ('applied', 'Applied'),
        ],
        string='Status', default='setup', required=True,
    )
    summary = fields.Text(string='Summary', readonly=True)
    breaker_warning = fields.Html(
        string='Breaker Warning', readonly=True, sanitize=False,
        help='Hand-built and hand-escaped in Python (never sanitized twice) '
             'because this wizard is the sole author of its own content.',
    )
    line_ids = fields.One2many(
        'gdrive.heal.wizard.line', 'wizard_id', string='Planned Actions')

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------
    def _ensure_admin(self, what: str) -> None:
        """Group check in Python. The view's ``groups=`` is a UI convenience,
        not a security boundary — it hides a button, it does not stop a call.
        """
        self.ensure_one()
        if not self.env.user.has_group('gdrive_odoo_sync.group_gdrive_admin'):
            raise UserError(_(
                'Only a Google Drive Sync Administrator may %(what)s. '
                'Previewing a plan is non-mutating; %(what)s writes to live '
                'business records.'
            ) % {'what': what})

    def _resolve_mapping(self):
        """The single ``gdrive.mapping`` this preview recomputes against.

        ``dataset_ids`` wins over ``plan_id`` when both are set, because a
        dataset selection is an explicit request to plan fresh scope, whereas
        ``plan_id`` is what a previous preview on *this same wizard* leaves
        behind. Multiple datasets are refused outright rather than merged: one
        Apply must correspond to one plan's fingerprints, never a blend.
        """
        self.ensure_one()
        if len(self.dataset_ids) > 1:
            raise UserError(_(
                'Select one dataset at a time. A single Apply must correspond '
                'to one plan\'s fingerprints, so its staleness check stays '
                'unambiguous; healing several datasets means opening this '
                'wizard once per dataset.'
            ))
        if self.dataset_ids:
            dataset = self.dataset_ids
            mapping = dataset.mapping_id
        elif self.plan_id:
            mapping = self.plan_id.mapping_id
            dataset = mapping.dataset_id if mapping else self.plan_id.dataset_id
        else:
            raise UserError(_(
                'Select a dataset, or an existing plan, before previewing.'))

        if not mapping:
            raise UserError(_(
                '%s has no promotion mapping yet. Build and validate one '
                'before healing it.'
            ) % (dataset.display_name if dataset else _('This dataset')))
        if mapping.state != 'active':
            raise UserError(_(
                'Mapping %s is not validated and enabled (state=%s). Validate '
                'it on the mapping form before healing it here.'
            ) % (mapping.display_name, mapping.state))
        return mapping

    # ------------------------------------------------------------------
    # Preview
    # ------------------------------------------------------------------
    def action_preview(self):
        """Recompute the plan through ``gdrive.reconciler`` and re-open self.

        Non-mutating on this addon's business-facing promise: it performs no
        ORM write on any model outside ``gdrive_odoo_sync`` itself. It reads
        Drive/Sheets data already staged, reads the current Odoo snapshot via
        ``gdrive.promoter.read_odoo_snapshot()``, and persists a fresh
        ``gdrive.plan`` — the same bookkeeping ``gdrive.mapping.action_preview``
        does, called through the same ``_promote_once(dry_run=True)`` so there
        is exactly one code path that turns a reconciled result into a plan.
        """
        self.ensure_one()
        if self.state == 'applied':
            raise UserError(_(
                'This heal has already been applied. Open a new wizard to plan '
                'another one.'
            ))
        mapping = self._resolve_mapping()
        plan = mapping._promote_once(dry_run=True)
        if not plan:
            raise UserError(_(
                '%s has no complete read yet to plan from. Stage it first.'
            ) % mapping.dataset_id.display_name)
        self._sync_from_plan(plan)
        _logger.info(
            '%s previewed by %s: %d create / %d update / %d soft-delete / '
            '%d quarantine (breaker=%s).',
            plan.name, self.env.user.login, plan.create_count, plan.update_count,
            plan.soft_delete_count, plan.quarantine_count, plan.breaker_reason or 'clear',
        )
        return self._reopen()

    def _sync_from_plan(self, plan) -> None:
        """Replace this wizard's lines and summary with ``plan``'s content.

        Every previous line is dropped and rebuilt rather than diffed against
        the new plan: the new plan is a different ``gdrive.plan`` record with
        different ``gdrive.plan.action`` rows (a fresh preview never reuses a
        stale plan), so there is nothing meaningful to diff against, and a
        partial merge could quietly carry a stale ``selected=False`` onto an
        action it was never actually about.
        """
        self.ensure_one()
        self.line_ids.unlink()
        line_vals = [{
            'action_id': action.id,
            'sequence': action.sequence,
            'selected': True,
            'action_type': action.action_type,
            'source_ref': action.source_ref or '',
            'description': _describe_action(action),
        } for action in plan.action_ids]

        self.write({
            'plan_id': plan.id,
            'dataset_ids': [(6, 0, [plan.dataset_id.id])] if plan.dataset_id else [(5, 0, 0)],
            'state': 'preview',
            'summary': self._build_summary(plan),
            'breaker_warning': self._build_breaker_warning(plan),
            'line_ids': [(0, 0, vals) for vals in line_vals],
        })

    def _build_summary(self, plan) -> str:
        """Plain-text recap of ``plan`` for the read-only ``summary`` box."""
        self.ensure_one()
        lines = [
            _('%(name)s — dataset %(dataset)s.') % {
                'name': plan.name, 'dataset': plan.dataset_id.display_name or '-'},
            _('%(c)d create(s), %(u)d update(s), %(d)d soft delete(s), %(q)d quarantine(s).') % {
                'c': plan.create_count, 'u': plan.update_count,
                'd': plan.soft_delete_count, 'q': plan.quarantine_count},
        ]
        if plan.breaker_tripped:
            lines.append(_('Circuit breaker tripped: %s. Read the warning above.')
                         % (plan.breaker_reason or '?'))
        if plan.requires_approval:
            lines.append(_('This plan requires human approval before it can execute.'))
        if plan.expiry_date:
            lines.append(_('Expires %s — stale after that, whatever is decided.')
                         % plan.expiry_date)

        if plan.state == 'applied':
            lines.append(_('Applied on %(date)s by %(user)s — result: %(result)s.') % {
                'date': plan.applied_date, 'user': plan.applied_by_id.name or '-',
                'result': plan.apply_result or '-'})
            lines.append(
                _('Converged: both dataset hashes were recomputed after applying and matched.')
                if plan.convergence_ok else
                _('DID NOT CONVERGE — a normalization rule is asymmetric. See the '
                  'non_convergent drift record; the affected field(s) will not be '
                  'rewritten again automatically.')
            )
            skipped = plan.action_ids.filtered(lambda a: a.state == 'skipped')
            if skipped:
                lines.append(_('%d action(s) were skipped (deselected here, or an '
                               'earlier action in the batch failed).') % len(skipped))
        return '\n'.join(lines)

    def _build_breaker_warning(self, plan) -> str:
        """Hand-built, hand-escaped HTML fragment for the ``breaker_warning`` alert.

        Empty (falsy) when the breaker did not trip, which is what keeps the
        view's ``invisible="not breaker_warning"`` alert box hidden on a clean
        plan. Every dynamic value is escaped with ``html_escape`` before being
        interpolated into a tag: ``breaker_reason`` is a closed, safe
        vocabulary, but the explanation text and this whole page are rendered
        in a browser, so nothing here is trusted by default.
        """
        self.ensure_one()
        if not plan.breaker_tripped:
            return ''
        esc = html_escape
        reason = plan.breaker_reason or ''
        explanation = _BREAKER_EXPLANATIONS.get(reason, _UNKNOWN_BREAKER_EXPLANATION)

        parts = [
            '<p><strong>%s</strong></p>' % esc(_('Circuit breaker tripped: %s') % reason),
            '<p>%s</p>' % esc(explanation),
        ]
        if plan.soft_delete_count:
            parts.append('<p><strong>%s</strong></p>' % esc(_(
                'This plan proposes %d soft delete(s). Untick any line below you '
                'are not certain about — a wrongly archived record takes its '
                'journal entries, attachments and message threads with it, and '
                'hard delete is never available here at any threshold.'
            ) % plan.soft_delete_count))
        return ''.join(parts)

    def _reopen(self):
        """Re-open this exact wizard record in its own dialog.

        Returning an ``ir.actions.act_window`` pinned to ``res_id=self.id``
        (rather than closing the dialog) is what lets ``action_preview`` show
        its freshly computed lines, and what lets ``action_apply`` show the
        applied state and result, without the operator losing whatever they
        had open.
        """
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Heal from Google Drive'),
            'res_model': self._name,
            'view_mode': 'form',
            'view_id': self.env.ref('gdrive_odoo_sync.gdrive_heal_wizard_view_form').id,
            'res_id': self.id,
            'target': 'new',
        }

    # ------------------------------------------------------------------
    # Apply
    # ------------------------------------------------------------------
    def action_apply(self):
        """Execute the previewed plan through ``gdrive.promoter``.

        Four independent refusals, each a distinct, actionable ``UserError``,
        checked in this order because each one is cheaper to explain than the
        next and none of them may be skipped by reaching this method some
        other way than the (hidden-while-inapplicable) Apply button:

        1. The caller is not a Google Drive Sync Administrator.
        2. ``dry_run`` is still True on this wizard.
        3. The plan's fingerprints moved since it was last previewed.
        4. The plan expired.

        Only after all four pass does anything touch ``gdrive.plan`` (to
        record approval/skips) or the promoter (to actually write). This
        wizard itself never calls ``create``/``write``/``unlink`` on a business
        model — that authority belongs to ``gdrive.promoter`` alone.
        """
        self.ensure_one()

        # Refusal 1 — group membership, before anything is read or written.
        self._ensure_admin(_('apply a heal plan'))

        if self.state != 'preview':
            raise UserError(_(
                'Preview the plan before applying it (current status: %s).'
            ) % self.state)
        plan = self.plan_id
        if not plan:
            raise UserError(_('There is no plan to apply. Preview first.'))

        # Refusal 2 — the operator must deliberately untick "dry run". A
        # preview that silently executes is the exact failure this flag exists
        # to prevent.
        if self.dry_run:
            raise UserError(_(
                'This wizard is still set to dry run. Untick "Dry Run" '
                'deliberately before applying — what you previewed is '
                'exactly what will run, and this flag is the one thing that '
                'stands between the two.'
            ))

        # Refusal 3 — someone edited the sheet or the Odoo records between
        # preview and this click. Re-read every fingerprint captured at plan
        # time and compare against the dataset's live state.
        moved = plan._fingerprints_moved()
        if moved:
            plan.sudo().write({'state': 'refused_stale'})
            raise UserError(_(
                'The world moved since this plan was previewed (%s changed). '
                'Preview again to see the current state before deciding.'
            ) % ', '.join(moved))

        # Refusal 4 — the plan is a statement about a state of the world that
        # was true when it was computed; past its expiry it never is again.
        if plan._is_expired():
            plan.sudo().write({'state': 'expired'})
            raise UserError(_(
                'This plan expired on %s. Preview again to compute a current one.'
            ) % plan.expiry_date)

        # Every safety gate passed. Deselected lines become skipped ACTIONS on
        # the persistent plan — not deleted, not hidden — so the promoter (and
        # anyone auditing the plan afterwards) sees exactly why they did not run.
        deselected = self.line_ids.filtered(lambda line: not line.selected and line.action_id)
        if deselected:
            deselected.mapped('action_id')._mark_skipped(
                _('Deselected by %s in the heal wizard before applying.') % self.env.user.login)

        plan.sudo().write({
            'state': 'approved',
            'approved_by_id': self.env.user.id,
            'approved_date': fields.Datetime.now(),
            'dry_run': False,
        })

        _logger.info('%s applying via heal wizard: %d action(s) by %s (%d deselected).',
                     plan.name, len(plan.action_ids), self.env.user.login, len(deselected))
        self.env['gdrive.promoter'].execute(plan)

        failed = plan.action_ids.filtered(lambda a: a.state == 'failed')
        applied_actions = plan.action_ids.filtered(lambda a: a.state == 'applied')
        if failed and applied_actions:
            apply_result = 'partial'
        elif failed:
            apply_result = 'failed'
        else:
            apply_result = 'success'

        plan.sudo().write({
            'state': 'applied',
            'applied_by_id': self.env.user.id,
            'applied_date': fields.Datetime.now(),
            'apply_result': apply_result,
        })
        plan._check_convergence()

        self.write({
            'state': 'applied',
            'summary': self._build_summary(plan),
        })
        return self._reopen()


class GdriveHealWizardLine(models.TransientModel):
    """One reviewable, selectable mirror of a ``gdrive.plan.action`` row.

    Deliberately its own model rather than a field on ``gdrive.plan.action``
    itself (SPEC/security note in ``security/gdrive_security.xml``): a manager
    may toggle ``selected`` here freely, because the worst that can do is
    misconfigure a wizard nobody has approved yet. The persistent
    ``gdrive.plan.action`` rows an admin is about to execute are never exposed
    to that write access at all.
    """

    _name = 'gdrive.heal.wizard.line'
    _description = 'Google Drive Sync Heal Wizard Line'
    _order = 'sequence, id'

    wizard_id = fields.Many2one(
        'gdrive.heal.wizard', string='Wizard', required=True, ondelete='cascade', index=True)
    action_id = fields.Many2one(
        'gdrive.plan.action', string='Plan Action', ondelete='cascade', index=True,
        help='The persistent plan action this line mirrors. Read-only mirror: '
             'nothing on this record is ever written back onto it except, at '
             'apply time, being marked skipped when unselected.',
    )
    sequence = fields.Integer(string='Sequence', default=20)
    selected = fields.Boolean(
        string='Apply', default=True,
        help='Unticked lines are marked skipped on the plan before the '
             'promoter runs; they never execute, and the plan keeps a record '
             'of exactly why.',
    )
    action_type = fields.Selection(ACTION_TYPE_SELECTION, string='Action', readonly=True)
    source_ref = fields.Char(string='Source', readonly=True)
    description = fields.Char(string='Description', readonly=True)
