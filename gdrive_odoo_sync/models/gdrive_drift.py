# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. See LICENSE (LGPL-3) for full copyright and licensing details.
"""``gdrive.drift`` — one finding, and the taxonomy that keeps findings honest.

WHY a record per finding rather than a blob on the verification
---------------------------------------------------------------
A finding has a *lifecycle*: it is discovered, it may be planned into a
``gdrive.plan.action``, applied, ignored by a human, or resolved by somebody
editing the sheet. That lifecycle needs a primary key, an audit trail and a
searchable index — a JSON array on the parent has none of those.

WHY the three categories are disjoint and enforced here
-------------------------------------------------------
SPEC §9.3 splits every finding into exactly one of ``drift`` (the two sides
genuinely disagree), ``data_quality`` (a cell could not be read or an identity
could not be resolved) and ``structural`` (the shape of the source moved).
The counts are kept separate everywhere because conflating them produces the
single most damaging report a verification system can emit: "12 drifts" when
what actually happened is "12 cells I could not parse". The first sentence
invites a bulk heal; the second forbids it.

The mapping from ``drift_type`` to category is therefore **not** a caller
argument. It is a module-level table applied on create, so no call site can
mis-file a finding into a category that inflates ``drift_count``.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


#: Category vocabulary (SPEC §3.12). Order matters only for the UI.
CATEGORY_SELECTION = [
    ('drift', 'Real Difference'),
    ('data_quality', 'Data Quality'),
    ('structural', 'Structural'),
]

#: The full drift taxonomy of SPEC §3.12 — all sixteen types, in the order the
#: spec lists them. Adding a type here without adding it to
#: :data:`DRIFT_TYPE_CATEGORY` and :data:`DRIFT_TYPE_SEVERITY` raises at create
#: time rather than silently defaulting; see :meth:`_normalize_finding`.
DRIFT_TYPE_SELECTION = [
    ('missing_in_odoo', 'Missing in Odoo'),
    ('missing_in_sheet', 'Missing in Sheet'),
    ('field_mismatch', 'Field Mismatch'),
    ('duplicate_identity', 'Duplicate Identity'),
    ('header_change', 'Header Change'),
    ('tab_missing', 'Tab Missing'),
    ('type_coercion', 'Type Coercion Failure'),
    ('currency_mismatch', 'Currency Mismatch'),
    ('multi_match', 'Multiple Identity Matches'),
    ('orphan_reference', 'Orphan Reference'),
    ('empty_tab', 'Empty Tab'),
    ('access_lost', 'Access Lost'),
    ('unmanaged_record', 'Unmanaged Record'),
    ('non_convergent', 'Non-convergent Field'),
    ('identifier_numeric', 'Identifier Read as Number'),
    ('schema_growth', 'Schema Growth'),
]

SEVERITY_SELECTION = [
    ('info', 'Info'),
    ('warning', 'Warning'),
    ('critical', 'Critical'),
    ('blocking', 'Blocking'),
]

DELTA_CLASS_SELECTION = [
    ('cosmetic', 'Cosmetic'),
    ('rounding', 'Rounding'),
    ('substantive', 'Substantive'),
]

RESOLUTION_SELECTION = [
    ('open', 'Open'),
    ('planned', 'Planned'),
    ('applied', 'Applied'),
    ('ignored', 'Ignored'),
    ('resolved_externally', 'Resolved Externally'),
]

#: ``drift_type`` → category. Authoritative; SPEC §9.3.
DRIFT_TYPE_CATEGORY: dict[str, str] = {
    # Category `drift` — counted in drift_count.
    'missing_in_odoo': 'drift',
    'missing_in_sheet': 'drift',
    'field_mismatch': 'drift',
    'currency_mismatch': 'drift',
    'unmanaged_record': 'drift',
    'non_convergent': 'drift',
    # Category `data_quality` — never counted in drift_count.
    'type_coercion': 'data_quality',
    'identifier_numeric': 'data_quality',
    'orphan_reference': 'data_quality',
    'multi_match': 'data_quality',
    'duplicate_identity': 'data_quality',
    # Category `structural` — the shape of the source moved.
    'header_change': 'structural',
    'schema_growth': 'structural',
    'tab_missing': 'structural',
    'empty_tab': 'structural',
    'access_lost': 'structural',
}

#: ``drift_type`` → default severity. SPEC §9.3.
#:
#: Every structural type is ``blocking`` *except* ``schema_growth``: an extra,
#: unmapped column is additive information and must never halt a dataset, while
#: a *missing* mapped column must, because reading it as empty cells would write
#: NULL over an entire Odoo column.
DRIFT_TYPE_SEVERITY: dict[str, str] = {
    'missing_in_odoo': 'warning',
    'missing_in_sheet': 'warning',
    'field_mismatch': 'warning',
    'currency_mismatch': 'critical',
    'unmanaged_record': 'info',
    'non_convergent': 'critical',
    'type_coercion': 'warning',
    'identifier_numeric': 'warning',
    'orphan_reference': 'warning',
    'multi_match': 'warning',
    'duplicate_identity': 'critical',
    'header_change': 'blocking',
    'schema_growth': 'info',
    'tab_missing': 'blocking',
    'empty_tab': 'blocking',
    'access_lost': 'blocking',
}

#: Ranking used when the caller asks "is anything here blocking?".
SEVERITY_RANK: dict[str, int] = {'info': 0, 'warning': 1, 'critical': 2, 'blocking': 3}


class GdriveDrift(models.Model):
    """A single difference, unreadable cell, or structural change."""

    _name = 'gdrive.drift'
    _description = 'Google Drive Sync Drift Finding'
    # SPEC §3.12. Findings are read parent-first; within a verification the
    # loudest ones come first so the triage screen opens on the fire.
    _order = 'verification_id, severity desc, id'

    verification_id = fields.Many2one(
        'gdrive.verification', string='Verification',
        required=True, index=True, ondelete='cascade',
    )
    dataset_id = fields.Many2one(
        'gdrive.dataset', string='Dataset',
        related='verification_id.dataset_id', store=True, index=True,
    )
    mapping_id = fields.Many2one(
        'gdrive.mapping', string='Mapping',
        related='verification_id.mapping_id', store=True, index=True,
    )

    category = fields.Selection(
        CATEGORY_SELECTION, string='Category', required=True, index=True,
        help='Derived from drift_type, never supplied by the caller. Keeps the '
             'three counts on the verification disjoint by construction.',
    )
    drift_type = fields.Selection(
        DRIFT_TYPE_SELECTION, string='Drift Type', required=True, index=True,
    )
    severity = fields.Selection(
        SEVERITY_SELECTION, string='Severity', required=True, index=True,
        default='warning',
        help='`blocking` halts the dataset: nothing is staged or promoted from '
             'that tab until the underlying problem is fixed.',
    )
    delta_class = fields.Selection(
        DELTA_CLASS_SELECTION, string='Delta Class',
        help='For field_mismatch only. Cosmetic and rounding differences are '
             'reported but never auto-written — writing them makes the value '
             'flap between runs forever without converging.',
    )

    sync_id = fields.Char(string='Sync Id', index=True)
    natural_key = fields.Char(string='Natural Key', index=True)
    staged_row_id = fields.Many2one(
        'gdrive.staged.row', string='Staged Row', ondelete='set null', index=True,
    )
    res_model = fields.Char(string='Target Model')
    res_id = fields.Integer(string='Target Id')
    field_name = fields.Char(string='Field', index=True)

    canon_sheet = fields.Char(
        string='Sheet Canonical Form',
        help='Tagged canonical token, verbatim and exactly as hashed. Never '
             're-rendered or prettified: the whole debuggability of the system '
             'rests on these two strings being byte-identical to the hash input.',
    )
    canon_odoo = fields.Char(string='Odoo Canonical Form')
    source_ref = fields.Char(
        string='Source Reference', index=True,
        help="A1 reference, e.g. \"'Wholesale — Leads'!A412\".",
    )
    drive_link = fields.Char(
        string='Drive Link', compute='_compute_drive_link',
        help='Deep link to the originating tab. Computed, never stored: the '
             'link is derived from the node and would go stale on a re-share.',
    )
    message = fields.Text(string='Message')

    resolution = fields.Selection(
        RESOLUTION_SELECTION, string='Resolution',
        default='open', required=True, index=True,
    )
    plan_action_id = fields.Many2one(
        'gdrive.plan.action', string='Plan Action', ondelete='set null', index=True,
    )

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------
    @api.depends('drift_type', 'field_name', 'source_ref', 'sync_id')
    def _compute_display_name(self) -> None:
        """Human label: type, then whatever locates the finding most precisely."""
        labels = dict(DRIFT_TYPE_SELECTION)
        for drift in self:
            locator = drift.source_ref or drift.sync_id or drift.natural_key or ''
            field_part = ' %s' % drift.field_name if drift.field_name else ''
            base = labels.get(drift.drift_type, drift.drift_type or _('Drift'))
            drift.display_name = ('%s%s %s' % (base, field_part, locator)).strip()

    @api.depends('dataset_id')
    def _compute_drive_link(self) -> None:
        """Build the Drive deep link, anchored on the tab when we know its gid.

        Google honours ``#gid=<sheetId>`` on a spreadsheet URL, so a reviewer
        lands on the offending tab rather than on tab 1 of a 40-tab workbook.
        """
        for drift in self:
            dataset = drift.dataset_id
            link = dataset.node_id.web_view_link if dataset else False
            if link and dataset.sheet_gid and dataset.sheet_gid >= 0:
                link = '%s#gid=%s' % (link.split('#')[0], dataset.sheet_gid)
            drift.drive_link = link or False

    # ------------------------------------------------------------------
    # Taxonomy helpers — the single source of truth for category/severity
    # ------------------------------------------------------------------
    @api.model
    def category_of(self, drift_type: str) -> str:
        """Category for ``drift_type``.

        Raises:
            UserError: on an unknown type. Defaulting an unrecognised finding to
                ``drift`` would let a future taxonomy addition silently inflate
                ``drift_count``; defaulting it to ``data_quality`` would hide it.
                Neither is acceptable, so it is a hard error at the boundary.
        """
        try:
            return DRIFT_TYPE_CATEGORY[drift_type]
        except KeyError:
            raise UserError(_('Unknown drift type %r. It must be declared in '
                              'DRIFT_TYPE_CATEGORY before it can be recorded.') % (drift_type,))

    @api.model
    def default_severity_of(self, drift_type: str) -> str:
        """Default severity for ``drift_type`` (SPEC §9.3)."""
        return DRIFT_TYPE_SEVERITY.get(drift_type, 'warning')

    @api.model
    def _normalize_finding(self, values: dict[str, Any]) -> dict[str, Any]:
        """Coerce one reconciler finding dict into valid ``gdrive.drift`` values.

        The reconciler (``gdrive.reconciler.plan``) is pure and emits plain
        dicts. This is the only place those dicts become records, so it is also
        the only place that has to defend the invariants:

        * ``category`` is recomputed from ``drift_type`` and any caller-supplied
          value is overwritten — a caller cannot file a ``type_coercion`` as a
          ``drift``.
        * ``severity`` falls back to the taxonomy default rather than to a
          field default, so an omitted severity is never quietly ``warning``
          for a ``blocking`` structural failure.
        * ``delta_class`` is cleared for anything that is not a
          ``field_mismatch``; it has no meaning elsewhere and a stale value
          would light up the "cosmetic, not auto-written" banner on a finding
          that has nothing to do with cosmetics.
        * Unknown keys are dropped, with a warning, instead of raising: a
          planner emitting an extra diagnostic key must not abort a whole
          verification.
        """
        drift_type = values.get('drift_type')
        vals: dict[str, Any] = {
            key: value for key, value in values.items()
            if key in self._fields and key not in ('category', 'display_name', 'drive_link')
        }
        unknown = set(values) - set(vals) - {'category', 'display_name', 'drive_link'}
        if unknown:
            _logger.warning(
                "Dropping unknown key(s) %s from a %s finding; they are not fields of gdrive.drift.",
                sorted(unknown), drift_type,
            )
        vals['category'] = self.category_of(drift_type)
        vals['severity'] = values.get('severity') or self.default_severity_of(drift_type)
        if drift_type != 'field_mismatch':
            vals.pop('delta_class', None)
        if isinstance(vals.get('res_id'), str):
            # The planner carries res_id as a string in its JSON artefact.
            vals['res_id'] = int(vals['res_id']) if vals['res_id'].isdigit() else 0
        return vals

    @api.model
    def create_findings(self, verification, findings: Iterable[dict[str, Any]]):
        """Materialize planner findings against ``verification``.

        Returns the created recordset. Creation is a single batched ``create``
        because a large dataset can legitimately produce thousands of findings
        and one INSERT per finding turns a two-second verification into a
        two-minute one.
        """
        rows: list[dict[str, Any]] = []
        for finding in findings:
            vals = self._normalize_finding(finding)
            vals['verification_id'] = verification.id
            rows.append(vals)
        if not rows:
            return self.browse()
        drifts = self.create(rows)
        _logger.info(
            "Recorded %d finding(s) for verification %s: %d drift, %d data-quality, %d structural.",
            len(drifts), verification.id,
            len(drifts.filtered(lambda d: d.category == 'drift')),
            len(drifts.filtered(lambda d: d.category == 'data_quality')),
            len(drifts.filtered(lambda d: d.category == 'structural')),
        )
        return drifts

    # ------------------------------------------------------------------
    # ORM overrides
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list: list[dict[str, Any]]):
        """Enforce the category/severity taxonomy on *every* create path.

        Records are also created by the heal wizard and by tests, not only by
        :meth:`create_findings`, and an invariant that only holds on one path is
        not an invariant.
        """
        for vals in vals_list:
            drift_type = vals.get('drift_type')
            if drift_type:
                vals['category'] = self.category_of(drift_type)
                if not vals.get('severity'):
                    vals['severity'] = self.default_severity_of(drift_type)
                if drift_type != 'field_mismatch':
                    vals['delta_class'] = False
        return super().create(vals_list)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def is_blocking(self) -> bool:
        """True when any finding in ``self`` halts its dataset."""
        return any(drift.severity == 'blocking' for drift in self)

    def max_severity(self) -> str:
        """The loudest severity present, or ``'info'`` for an empty recordset."""
        if not self:
            return 'info'
        return max((d.severity for d in self), key=lambda s: SEVERITY_RANK.get(s, 0))

    def mark_planned(self, plan_action=None) -> None:
        """Link findings to the plan action that will address them."""
        vals: dict[str, Any] = {'resolution': 'planned'}
        if plan_action is not None and plan_action:
            vals['plan_action_id'] = plan_action.id
        self.filtered(lambda d: d.resolution == 'open').write(vals)

    def mark_applied(self) -> None:
        """Called by the promoter once the corresponding action succeeded."""
        self.filtered(lambda d: d.resolution in ('open', 'planned')).write({'resolution': 'applied'})

    def action_ignore(self):
        """Human triage: stop counting this finding as open.

        Deliberately *not* a delete. The finding stays visible, searchable and
        attributable — "somebody decided this was fine" is itself information,
        and a re-appearance of the same finding next run is only meaningful if
        the earlier decision is still on file.
        """
        self.write({'resolution': 'ignored'})
        _logger.info("User %s ignored %d drift finding(s): %s.",
                     self.env.user.login, len(self), self.ids)
        return True

    def action_reopen(self):
        """Undo an ignore/resolve decision."""
        self.write({'resolution': 'open'})
        return True

    def action_resolved_externally(self):
        """Mark findings fixed outside the sync (someone edited the sheet)."""
        self.write({'resolution': 'resolved_externally'})
        return True

    def action_open_drive(self):
        """Open the originating tab in Drive."""
        self.ensure_one()
        if not self.drive_link:
            raise UserError(_('No Drive link is known for this finding. The '
                              'originating file may have been removed from scope.'))
        return {'type': 'ir.actions.act_url', 'url': self.drive_link, 'target': 'new'}

    # ------------------------------------------------------------------
    # Serialization for the report artefact
    # ------------------------------------------------------------------
    def to_report_dict(self) -> list[dict[str, Any]]:
        """JSON-safe projection used by ``gdrive.verification`` report rendering.

        ``canon_sheet`` and ``canon_odoo`` are emitted verbatim. Anything that
        reformats them — stripping the type tag, casting to float, collapsing
        whitespace — destroys the only evidence that explains why two values the
        eye reads as identical hash differently.
        """
        return [{
            'id': drift.id,
            'category': drift.category,
            'drift_type': drift.drift_type,
            'severity': drift.severity,
            'delta_class': drift.delta_class or None,
            'sync_id': drift.sync_id or None,
            'natural_key': drift.natural_key or None,
            'res_model': drift.res_model or None,
            'res_id': drift.res_id or None,
            'field_name': drift.field_name or None,
            'canon_sheet': drift.canon_sheet,
            'canon_odoo': drift.canon_odoo,
            'source_ref': drift.source_ref or None,
            'drive_link': drift.drive_link or None,
            'message': drift.message or None,
            'resolution': drift.resolution,
        } for drift in self]
