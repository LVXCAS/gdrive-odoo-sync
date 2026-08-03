# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. Licensed under LGPL-3.
"""``gdrive.promotion.link`` — row state on the Odoo side (SPEC §3.10).

WHY this model exists at all
-----------------------------
``gdrive.staged.row`` remembers what the *sheet* last looked like. Nothing on
that side knows which Odoo record a given identity became, whether that record
is still owned by this sync, or whether writing it keeps flapping between two
values that never converge. This model is that missing half: one row per
``(mapping_id, sync_id)`` that has ever been promoted, carrying the pointer to
the business record and the bookkeeping the reconciler's delete guards and the
plan's convergence assertion both depend on.

WHY there are *two* ``missing_since`` clocks in this addon
------------------------------------------------------------
``gdrive.staged.row.missing_since`` answers "has this row vanished from the
sheet?". ``gdrive.promotion.link.missing_since`` answers a related but
distinct question: "has this *identity* stopped being confirmed by a complete
sheet read, as seen from the promoted business record?". They are kept on
separate models on purpose — a staged row can be deleted and recreated by a
header re-read, a promotion link never is — and only the second clock is what
SPEC §9.6 guard 5 and SPEC §9.8's flap detector actually consult. Both clocks
share the same rule, restated here because it is the one thing this file must
never get backwards: set only on a run that is proven **complete**, cleared the
instant the identity is seen again, and never inferred from a short or failed
read. An incomplete read looks exactly like a mass deletion from the outside,
and the only defence is refusing to let it move this clock at all.

WHY ownership is re-derived here, not just asserted once at promotion time
----------------------------------------------------------------------------
SPEC §3.10's ownership rule — a record is this sync's to delete only while it
carries a matching ``x_gdrive_sync_id`` *and* ``x_gdrive_source_dataset`` *and*
a live link — is checked at plan time by the reconciler, but nothing stops a
human from editing those two technical fields by hand in the Odoo UI between
runs. :meth:`verify_ownership` is how that gets noticed: the moment a linked
record's technical fields stop matching what this link expects, the link is
downgraded to ``unmanaged`` so the record is reported, never silently treated
as synced (or worse, silently deleted) again.

WHY the bookkeeping methods are the only way callers touch these fields
--------------------------------------------------------------------------
``missing_since``, ``missing_run_count`` and ``flap_counters`` are cheap to get
subtly wrong under partial writes — clearing one without the other reintroduces
exactly the ambiguity they exist to remove. Every legitimate transition is
therefore a named method (``mark_promoted``, ``mark_seen``, ``mark_missing``,
``mark_quarantined``, ``mark_soft_deleted``, ``verify_ownership``) that updates
the whole cluster of fields together, rather than the caller poking at
individual columns.
"""

from __future__ import annotations

import logging

from odoo import _, api, fields, models

from .gdrive_mapping import SOURCE_DATASET_FIELD, SYNC_ID_FIELD

_logger = logging.getLogger(__name__)

#: SPEC §3.10. ``linked`` is the only "everything is fine" state; every other
#: value exists to stop something from happening silently:
#: ``missing`` — absent from the sheet, quarantine clock running, not yet
#:   delete-eligible.
#: ``quarantined`` — present in the sheet but its staged row currently fails a
#:   canonicalization or identity check, so nothing is written this run.
#: ``soft_deleted`` — the promoter archived the record; the sync id is
#:   retained so a restore is one flag flip.
#: ``unmanaged`` — the target record's technical fields no longer match this
#:   link (typically a human edit); reported, never touched again.
#: ``non_convergent`` — SPEC §9.8's flap detector tripped for at least one
#:   field on this link; that field has stopped being written.
STATE_SELECTION = [
    ('linked', 'Linked'),
    ('missing', 'Missing'),
    ('quarantined', 'Quarantined'),
    ('soft_deleted', 'Soft Deleted'),
    ('unmanaged', 'Unmanaged'),
    ('non_convergent', 'Non-convergent'),
]

#: States :meth:`mark_promoted` / :meth:`mark_seen` are allowed to clear back
#: to ``linked`` on their own. ``non_convergent`` and ``unmanaged`` are
#: deliberately excluded: both record a problem a human needs to see resolved,
#: and a clean write on an unrelated field must not quietly erase that trail.
_STATES_AUTO_RESOLVED_BY_PRESENCE = ('linked', 'missing', 'quarantined')


class GdrivePromotionLink(models.Model):
    """One promoted identity: the pointer from a sync id to a business record."""

    _name = 'gdrive.promotion.link'
    _description = 'Google Drive Promotion Link'
    _order = 'mapping_id, id'

    mapping_id = fields.Many2one(
        'gdrive.mapping', string='Mapping',
        required=True, index=True, ondelete='cascade',
    )
    dataset_id = fields.Many2one(
        'gdrive.dataset', string='Dataset',
        related='mapping_id.dataset_id', store=True, index=True,
    )
    staged_row_id = fields.Many2one(
        'gdrive.staged.row', string='Staged Row', index=True, ondelete='set null',
    )
    sync_id = fields.Char(
        string='Sync Id', required=True, index=True,
        help='ULID. Together with mapping_id, the upsert key for this link '
             'and the partial unique index on the target model.',
    )
    natural_key = fields.Char(
        string='Natural Key', index=True,
        help='Snapshot at last successful promotion. Not a live join key.',
    )
    res_model = fields.Char(string='Target Model', required=True, index=True)
    res_id = fields.Integer(string='Target Record Id', required=True, index=True)

    last_h_row = fields.Char(string='Last Row Hash', size=32)
    last_promoted_date = fields.Datetime(string='Last Promoted')
    last_seen_in_sheet_date = fields.Datetime(string='Last Seen In Sheet', index=True)

    missing_since = fields.Datetime(
        string='Missing Since', index=True,
        help='Set on the first COMPLETE run in which the identity is absent '
             'from the sheet; cleared the moment it reappears. Drives SPEC '
             '§9.6 delete guard 5 together with missing_run_count.',
    )
    missing_run_count = fields.Integer(
        string='Missing Run Count', default=0,
        help='Consecutive COMPLETE runs the identity has been absent. ANDed '
             'with mapping.quarantine_hours against missing_since, never ORed.',
    )
    flap_counters = fields.Json(
        string='Flap Counters',
        help='{field_name: consecutive_write_count}. SPEC §9.8: at '
             'mapping.flap_limit the offending field stops being written and '
             'this link is marked non_convergent.',
    )

    state = fields.Selection(
        STATE_SELECTION, string='Status', default='linked', required=True, index=True,
    )
    state_detail = fields.Text(string='Status Detail')

    _sql_constraints = [
        ('link_sync_uniq', 'unique(mapping_id, sync_id)', 'One link per sync id per mapping.'),
    ]

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------
    @api.depends('res_model', 'res_id', 'sync_id', 'state')
    def _compute_display_name(self) -> None:
        for link in self:
            target = '%s#%s' % (link.res_model or '?', link.res_id or '?')
            link.display_name = ('%s · %s' % (target, link.sync_id or '')).strip(' ·')

    # ------------------------------------------------------------------
    # Bookkeeping — the only supported way to move these fields
    # ------------------------------------------------------------------
    @api.model
    def mark_promoted(self, mapping, sync_id, res_model, res_id, *,
                       natural_key=False, staged_row_id=False, h_row=False, now=False):
        """Upsert the link for one identity after a successful create/update.

        Keyed on ``(mapping_id, sync_id)`` — the same pair the SQL unique
        constraint protects — so a retried apply of the same ULID collapses
        into an update instead of a duplicate link, mirroring the
        upsert-by-partial-index behaviour SPEC §3.10 requires on the target
        model itself.

        Presence this run is positive evidence, so the quarantine clock is
        cleared unconditionally. ``state`` only auto-resolves to ``linked``
        from :data:`_STATES_AUTO_RESOLVED_BY_PRESENCE`; ``non_convergent`` and
        ``unmanaged`` are left for a human (or :meth:`verify_ownership`) to
        clear explicitly.

        Returns the upserted record.
        """
        now = now or fields.Datetime.now()
        link = self.sudo().search([
            ('mapping_id', '=', mapping.id), ('sync_id', '=', sync_id),
        ], limit=1)
        vals = {
            'staged_row_id': staged_row_id or False,
            'res_model': res_model,
            'res_id': res_id,
            'last_h_row': h_row or False,
            'last_promoted_date': now,
            'last_seen_in_sheet_date': now,
            'missing_since': False,
            'missing_run_count': 0,
        }
        if natural_key:
            vals['natural_key'] = natural_key
        if link:
            if link.state in _STATES_AUTO_RESOLVED_BY_PRESENCE:
                vals['state'] = 'linked'
                vals['state_detail'] = False
            link.write(vals)
            return link
        vals.update({
            'mapping_id': mapping.id,
            'sync_id': sync_id,
            'natural_key': natural_key or False,
            'state': 'linked',
        })
        return self.sudo().create(vals)

    def mark_seen(self, now=False):
        """Clear the quarantine clock for links whose identity reappeared.

        Called for every link matched by identity in a COMPLETE sheet read,
        whether or not its row was actually written this run — a row that is
        currently quarantined for data quality still proves the identity is
        present, it is simply not writable yet (see :meth:`mark_quarantined`).
        """
        now = now or fields.Datetime.now()
        for link in self:
            vals = {
                'last_seen_in_sheet_date': now,
                'missing_since': False,
                'missing_run_count': 0,
            }
            if link.state == 'missing':
                vals['state'] = 'linked'
                vals['state_detail'] = False
            link.write(vals)

    def mark_missing(self, now=False):
        """Advance the quarantine clock for links absent from a COMPLETE read.

        The caller is responsible for only invoking this against a proven
        COMPLETE run and only for links whose identity was not present — this
        method has no way to verify either condition from the recordset alone,
        exactly like ``gdrive.staged.row``'s equivalent clock. ``soft_deleted``
        links are skipped: the record is already archived and ticking a clock
        nothing reads any more would just be noise.
        """
        now = now or fields.Datetime.now()
        for link in self:
            if link.state == 'soft_deleted':
                continue
            vals = {
                'missing_since': link.missing_since or now,
                'missing_run_count': link.missing_run_count + 1,
            }
            if link.state == 'linked':
                vals['state'] = 'missing'
            link.write(vals)

    def mark_quarantined(self, detail=False, now=False):
        """Flag links whose current staged row cannot be written this run.

        Distinct from ``missing``: the identity is present, so the quarantine
        clock is cleared, but the row failed a canonicalization or identity
        check upstream, so nothing is written to the linked record.
        """
        now = now or fields.Datetime.now()
        self.write({
            'state': 'quarantined',
            'state_detail': detail or False,
            'last_seen_in_sheet_date': now,
            'missing_since': False,
            'missing_run_count': 0,
        })

    def mark_soft_deleted(self, detail=False):
        """Record that the promoter executed a ``soft_delete`` action here.

        ``missing_since`` / ``missing_run_count`` are left untouched: they are
        the evidence trail that justified the delete and stay useful for audit
        after the fact. ``flap_counters`` is cleared — a soft-deleted record is
        never written again, so there is nothing left that can flap.
        """
        self.write({
            'state': 'soft_deleted',
            'state_detail': detail or False,
            'flap_counters': {},
        })

    def verify_ownership(self):
        """Re-derive ``state='unmanaged'`` for links whose record disowned itself.

        SPEC §3.10's ownership rule is load-bearing: a business record is a
        delete/update candidate for this sync only while it carries a matching
        ``x_gdrive_sync_id`` *and* ``x_gdrive_source_dataset`` *and* a live
        promotion link. Nothing in this addon can stop a human from editing
        those two technical fields directly in the Odoo UI, so instead this
        notices it: the moment a linked record's technical fields stop
        matching what this link expects, the link is downgraded to
        ``unmanaged`` and reported, never acted on again. A link whose record
        has since had ownership restored is promoted back to ``linked``.

        Grouped into one ``search_read`` per ``res_model`` so verifying a few
        thousand links costs a handful of queries, never one read per record.

        Best-effort per group and never raises: a model that no longer exists,
        or one the caller cannot read, must not abort verification for every
        other model, and this may run from a cron via the promotion cycle.
        """
        by_model: dict[str, models.BaseModel] = {}
        for link in self:
            by_model[link.res_model] = by_model.get(link.res_model, self.browse()) | link
        for res_model, links in by_model.items():
            try:
                if not res_model or res_model not in self.env:
                    links.write({
                        'state': 'unmanaged',
                        'state_detail': _('Target model %s no longer exists.') % (res_model or '?'),
                    })
                    continue
                target = self.env[res_model].sudo().with_context(active_test=False)
                if SYNC_ID_FIELD not in target._fields or SOURCE_DATASET_FIELD not in target._fields:
                    links.write({
                        'state': 'unmanaged',
                        'state_detail': _(
                            '%s no longer carries the sync technical fields.') % res_model,
                    })
                    continue
                rows = target.search_read(
                    [('id', 'in', links.mapped('res_id'))],
                    ['id', SYNC_ID_FIELD, SOURCE_DATASET_FIELD],
                )
                by_id = {row['id']: row for row in rows}
                for link in links:
                    row = by_id.get(link.res_id)
                    if not row:
                        link.write({
                            'state': 'unmanaged',
                            'state_detail': _('The target record no longer exists.'),
                        })
                        continue
                    expected_tag = link.mapping_id.tab_uid() if link.mapping_id else False
                    if row.get(SYNC_ID_FIELD) != link.sync_id or row.get(SOURCE_DATASET_FIELD) != expected_tag:
                        link.write({
                            'state': 'unmanaged',
                            'state_detail': _(
                                "The target record's technical fields no longer match this "
                                "link; it was likely edited by hand. Reported, never touched."
                            ),
                        })
                    elif link.state == 'unmanaged':
                        link.write({'state': 'linked', 'state_detail': False})
            except Exception:
                _logger.exception(
                    "Could not verify ownership for %d promotion link(s) on %s; "
                    "their state is unchanged.", len(links), res_model)
