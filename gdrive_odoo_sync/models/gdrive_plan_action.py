# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. See LICENSE (LGPL-3) for full copyright and licensing details.
"""``gdrive.plan.action`` — one executable step of a change plan (SPEC §3.14).

WHY an action is a persisted row rather than a dict in the plan's Json
---------------------------------------------------------------------
Three properties fall out of persistence and none of them are available from a
serialized blob:

* **Per-action outcome.** SPEC §9.7 requires that a failing row fails *alone*:
  the batch rolls back to its savepoint, every row is retried individually, and
  the offender is quarantined with its traceback. ``state`` + ``error`` per row
  is what makes "one bad row must not discard the good ones" auditable after
  the fact.
* **Stable ULIDs.** ``sync_id`` is minted at **plan** time, never at execution
  time. A retried create therefore carries the *same* identifier and collapses
  into a no-op update against the partial unique index on
  ``x_gdrive_sync_id`` — instead of creating a second record with a fresh id.
* **Reviewability.** A human approving a plan is approving these exact rows.
  They are rendered in a list, they are searchable, and they cannot silently
  differ between what was previewed and what is executed.

WHY ``sequence`` encodes the type
---------------------------------
Execution order is fixed by SPEC §9.7 and is not a preference:
``writeback_sync_id`` (10) → ``create`` (20) → ``update`` (30) →
``soft_delete`` (40) → ``quarantine`` (50). Deletes go **last** so that any
earlier failure — evidence that our view of the world is incomplete — can skip
every one of them. Encoding the order in a stored integer rather than in the
executor's control flow means the ordering survives being read back out of the
database, sorted in a view, or re-executed by a different code path.
"""

from __future__ import annotations

import logging

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from ..lib import new_ulid

_logger = logging.getLogger(__name__)

#: Actions per savepoint (SPEC §9.7). Small enough that a rollback-and-retry
#: pass over a failed batch is cheap, large enough that savepoint overhead stays
#: negligible on a 50 000-row promotion.
ACTION_BATCH_SIZE = 200

#: SPEC §9.7 execution order. The *only* definition of it in the addon: the
#: promoter sorts by ``sequence``, so changing this dict changes execution
#: order everywhere, and nothing else needs to be kept in step.
SEQUENCE_BY_TYPE = {
    'writeback_sync_id': 10,
    'create': 20,
    'update': 30,
    'soft_delete': 40,
    'quarantine': 50,
}

#: Deliberately does **not** contain ``unlink`` or ``delete``. SPEC §9.6: hard
#: delete is never available to any automated path, at any threshold, under any
#: configuration. It is unreachable here because it is not expressible.
ACTION_TYPE_SELECTION = [
    ('writeback_sync_id', 'Write Back Sync Id'),
    ('create', 'Create'),
    ('update', 'Update'),
    ('soft_delete', 'Soft Delete'),
    ('quarantine', 'Quarantine'),
]


class GdrivePlanAction(models.Model):
    """A single typed, serializable write that a plan proposes to perform."""

    _name = 'gdrive.plan.action'
    _description = 'Google Drive Sync Plan Action'
    _order = 'plan_id, sequence, id'

    plan_id = fields.Many2one(
        'gdrive.plan', string='Plan',
        required=True, index=True, ondelete='cascade',
    )
    sequence = fields.Integer(
        string='Sequence', default=lambda self: SEQUENCE_BY_TYPE['create'], index=True,
        help='Execution order (SPEC §9.7): 10 write-back, 20 create, 30 update, '
             '40 soft delete, 50 quarantine. Deletes are last so that an earlier '
             'failure can skip them all.',
    )
    batch_index = fields.Integer(
        string='Batch', default=0, index=True,
        help='Savepoint group. One savepoint per %d actions; on batch failure '
             'the batch is rolled back and its rows retried individually.'
             % ACTION_BATCH_SIZE,
    )
    action_type = fields.Selection(
        ACTION_TYPE_SELECTION, string='Action', required=True, index=True,
    )

    sync_id = fields.Char(
        string='Sync Id', index=True,
        help='ULID minted at PLAN time, never at execution time, so a retry of '
             'this action reuses the same identity and upserts instead of '
             'creating a duplicate.',
    )
    staged_row_id = fields.Many2one(
        'gdrive.staged.row', string='Staged Row', index=True, ondelete='set null',
    )
    res_model = fields.Char(string='Model', index=True)
    res_id = fields.Integer(
        string='Record Id', index=True,
        help='Empty for a create: the record does not exist yet.',
    )

    payload = fields.Json(
        string='Payload',
        help='Odoo-native typed values for a create (or the single flag flip of '
             'a soft delete), serialized to JSON.',
    )
    deltas = fields.Json(
        string='Deltas',
        help='[{"field": …, "from": "<canon>", "to": "<canon>", "to_typed": …}] '
             'for an update. Only the differing fields: a full-record write '
             'stomps unmanaged fields and bumps write_date on everything, '
             'poisoning the L0b Odoo fast path.',
    )
    source_ref = fields.Char(
        string='Source', help='A1 reference of the sheet row this action came from.')

    state = fields.Selection(
        [
            ('pending', 'Pending'),
            ('applied', 'Applied'),
            ('skipped', 'Skipped'),
            ('failed', 'Failed'),
        ],
        string='Status', default='pending', required=True, index=True,
    )
    error = fields.Text(
        string='Error',
        help='Verbatim failure detail. Never blank on a failed action: a '
             'swallowed exception here is a write that silently did not happen.',
    )

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list: list[dict]) -> 'GdrivePlanAction':
        """Fill in the derived, order-critical fields the caller may omit.

        Three things are normalized here rather than at every call site:

        * ``sequence`` is derived from ``action_type`` unless it was given
          explicitly, so the SPEC §9.7 ordering cannot be forgotten.
        * ``sync_id`` is minted for creates that lack one. This is *plan* time
          by definition — the row is being written now — which is exactly the
          guarantee SPEC §9.7 asks for: a retried create reuses the ULID it was
          planned with.
        * ``batch_index`` is assigned by counting the actions already on the
          plan, so savepoint groups stay contiguous even when a plan is
          materialized in several ``create()`` calls.
        """
        counters: dict[int, int] = {}
        for vals in vals_list:
            action_type = vals.get('action_type')
            if action_type and not vals.get('sequence'):
                vals['sequence'] = SEQUENCE_BY_TYPE.get(action_type, 20)
            if action_type == 'create' and not vals.get('sync_id'):
                vals['sync_id'] = new_ulid()
            plan_id = vals.get('plan_id')
            if plan_id and 'batch_index' not in vals:
                if plan_id not in counters:
                    counters[plan_id] = self.search_count([('plan_id', '=', plan_id)])
                vals['batch_index'] = counters[plan_id] // ACTION_BATCH_SIZE
                counters[plan_id] += 1
        return super().create(vals_list)

    # ------------------------------------------------------------------
    # Integrity
    # ------------------------------------------------------------------
    @api.constrains('action_type', 'payload', 'res_id')
    def _check_soft_delete_is_a_flag_flip(self) -> None:
        """A soft delete may only turn a flag off, and must keep the sync id.

        SPEC §9.6: execution is ``active = False`` (or the mapping's declared
        ``soft_delete_field``) with ``x_gdrive_sync_id`` **retained**, so that a
        restore is one flag flip. An action that also cleared the sync id would
        make the record unrecoverable *and* invisible to the next run's
        ownership check — it would look like an unmanaged record forever after.
        """
        for action in self:
            if action.action_type != 'soft_delete':
                continue
            payload = action.payload or {}
            if not payload:
                raise ValidationError(_(
                    'A soft-delete action must name the flag it clears. An empty '
                    'payload is not a delete, it is an unexecutable instruction.'
                ))
            if 'x_gdrive_sync_id' in payload or 'x_gdrive_source_dataset' in payload:
                raise ValidationError(_(
                    'A soft delete must retain x_gdrive_sync_id and '
                    'x_gdrive_source_dataset so the record can be restored with '
                    'one flag flip and stays recognisable as ours.'
                ))
            if any(value for value in payload.values()):
                raise ValidationError(_(
                    'A soft delete may only clear a boolean flag. Setting a value '
                    'here would be a disguised update.'
                ))

    @api.constrains('action_type', 'res_id')
    def _check_target_is_resolvable(self) -> None:
        """Non-create actions must name the record they act on."""
        for action in self:
            if action.action_type in ('update', 'soft_delete', 'writeback_sync_id'):
                if not action.res_model:
                    raise ValidationError(_(
                        'A %s action must name its target model.'
                    ) % action.action_type)

    @api.depends('action_type', 'sync_id', 'res_model', 'res_id')
    def _compute_display_name(self) -> None:
        """Odoo 18 removed ``name_get()``; it is never called if defined."""
        for action in self:
            label = dict(ACTION_TYPE_SELECTION).get(action.action_type, action.action_type or '')
            target = action.res_id and '%s,%s' % (action.res_model or '?', action.res_id)
            action.display_name = '%s %s' % (label, target or action.sync_id or '')

    # ------------------------------------------------------------------
    # Outcome recording — used by ``gdrive.promoter`` during execution
    # ------------------------------------------------------------------
    def _mark_applied(self, res_id: int | None = None) -> None:
        """Record that this action executed, optionally binding the new record."""
        vals: dict = {'state': 'applied', 'error': False}
        if res_id:
            vals['res_id'] = res_id
        self.sudo().write(vals)

    def _mark_failed(self, error: str) -> None:
        """Record a failure, verbatim, and mirror it to the server log.

        Mirrored because a row inside an aborted savepoint may never be visible
        to anyone tailing the database, and "the write did not happen and
        nothing said so" is the failure mode this whole module exists to avoid.
        """
        message = (error or '').strip() or _('Unknown error.')
        self.sudo().write({'state': 'failed', 'error': message})
        for action in self:
            _logger.error(
                'Plan action %s (%s, %s) failed: %s',
                action.id, action.action_type, action.sync_id or '-', message,
            )

    def _mark_skipped(self, reason: str) -> None:
        """Record that this action was deliberately not attempted.

        The canonical use is SPEC §9.7's ``earlier_errors``: if anything before
        sequence 40 failed, every ``soft_delete`` is skipped, because a failure
        earlier in the run is evidence that our view of the world is incomplete
        and absence is exactly what an incomplete view looks like.
        """
        self.sudo().write({'state': 'skipped', 'error': reason or False})
        _logger.info('Skipped %d plan action(s): %s', len(self), reason)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        """Plain-data form, for the plan artefact and the run log.

        Kept symmetrical with the dicts ``gdrive.reconciler.plan()`` returns so
        that a stored action and a freshly planned one are comparable field by
        field — which is how "the preview is not a lie" is actually tested.
        """
        self.ensure_one()
        return {
            'sequence': self.sequence,
            'batch_index': self.batch_index,
            'action_type': self.action_type,
            'sync_id': self.sync_id or '',
            'res_model': self.res_model or '',
            'res_id': self.res_id or 0,
            'payload': self.payload or {},
            'deltas': self.deltas or [],
            'source_ref': self.source_ref or '',
            'state': self.state,
            'error': self.error or '',
        }
