# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. See LICENSE (LGPL-3) for full copyright and licensing details.
"""``gdrive.mapping`` — the opt-in promotion contract (SPEC §3.8).

WHY this model is *opt-in and stays opt-in*
===========================================
Everything else in this addon is non-mutating: discovery reads Drive, staging
lands rows in ``gdrive.staged.row``, verification compares hashes. This model is
the single place where reading a spreadsheet turns into **writing somebody's
business records**. So the two switches that authorize that — ``enabled`` and
``auto_heal`` — both ship ``False``, and the promotion cron is a documented
no-op for every dataset that has not deliberately been turned on. Shipping the
cron active is therefore safe: with no enabled mapping it logs that it found
nothing to do and stops.

WHY ``spec_version`` lives here
===============================
Every stored hash in the database is an assertion of the form *"under normalizer
N and contract C, these two datasets are identical"*. Change any column option
and C moves — but the stored hashes do not. Serving one of those stale digests
as ``verified`` is a **silent false pass**, the worst failure a verification
system can have, because the dashboard stays green. ``spec_version`` is the
structural defence: it is recomputed from the whole column contract, and when it
moves, this model wipes the dataset's cached hashes so the next verify pass
rebuilds from scratch. Recomputation costs API calls. A false "verified" costs
trust.
"""

import logging
import time

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..lib.contract import contract_from_mapping_dict, spec_version_for_contracts
from ..lib.text_canon import TEXT_CANON
from .gdrive_sync_run import COMMIT_BATCH, CRON_BUDGET_SEC, trigger_cron

_logger = logging.getLogger(__name__)

#: Technical columns this module adds to every promotion target model. They are
#: what make a business record *ownable*: SPEC §3.10's ownership rule says a
#: record is a delete candidate only when it carries both of these AND has a
#: matching ``gdrive.promotion.link``. A record a human typed into Odoo by hand
#: carries neither and is therefore never the sync's to touch.
SYNC_ID_FIELD = 'x_gdrive_sync_id'
SOURCE_DATASET_FIELD = 'x_gdrive_source_dataset'

IDENTITY_STRATEGY_SELECTION = [
    ('sync_id', 'Sync Id only'),
    ('natural_key', 'Natural key only'),
    ('sync_id_then_key', 'Sync Id, then natural key'),
]

STATE_SELECTION = [
    ('draft', 'Draft'),
    ('validated', 'Validated'),
    ('active', 'Active'),
    ('blocked', 'Blocked'),
]

DELETE_POLICY_SELECTION = [
    ('never', 'Never'),
    ('report', 'Report only'),
    ('soft', 'Soft delete (archive)'),
]

#: Fields on ``gdrive.mapping.column`` that change what a cell canonicalizes to
#: and must therefore invalidate every cached hash when edited. Kept as one list
#: so the ``spec_version`` dependency and the human-readable rationale cannot
#: drift apart. ``col_index`` is deliberately absent: a physical column reorder
#: changes it and changes no data (see ``lib/contract._SPEC_IRRELEVANT``).
CONTRACT_FIELDS = (
    'sequence', 'header_canon', 'odoo_field', 'ctype', 'required',
    'is_natural_key', 'authority', 'empty_is_null',
    'text_trim', 'text_collapse_ws', 'text_case', 'fold_punct',
    'decimal_sep', 'group_sep', 'accounting_negatives', 'percent_mode',
    'scale_mode', 'scale', 'currency_field_id', 'default_currency_id',
    'rel_tol', 'abs_tol', 'date_formats', 'truthy', 'falsy', 'empty_means',
    'value_map', 'comodel', 'm2o_match_field', 'm2o_create_missing',
    'assert_string_value', 'dataset_column_id',
)

#: Cached-hash fields cleared on ``gdrive.dataset`` when ``spec_version`` moves.
#: Filtered against the live model at write time because ``gdrive_dataset.py``
#: is owned by another lane; a field it later renames must degrade to "one field
#: not cleared", never to a traceback inside a cron.
DATASET_CACHE_FIELDS = {
    'h_dataset_sheet': False,
    'h_dataset_odoo': False,
    'bucket_hashes': {},
    'last_drive_version': False,
    'last_drive_modified': False,
    'last_odoo_count': 0,
    'last_odoo_max_write_date': False,
    'last_verify_date': False,
}


class GdriveMapping(models.Model):
    """One dataset's declarative promotion contract into one Odoo model."""

    _name = 'gdrive.mapping'
    _description = 'Google Drive Promotion Mapping'
    _inherit = ['mail.thread']
    _order = 'dataset_id, id'
    _rec_name = 'name'

    name = fields.Char(string='Name', required=True, tracking=True)
    dataset_id = fields.Many2one(
        'gdrive.dataset', string='Dataset',
        required=True, index=True, ondelete='cascade', tracking=True,
        help='The tab this contract promotes. One mapping per dataset.',
    )
    connection_id = fields.Many2one(
        related='dataset_id.connection_id', store=True, index=True, string='Connection')
    company_id = fields.Many2one(
        related='dataset_id.company_id', store=True, index=True, string='Company')

    target_model_id = fields.Many2one(
        'ir.model', string='Target Model',
        required=True, ondelete='cascade', tracking=True,
        help='The business model rows are promoted into, e.g. res.partner.',
    )
    target_model = fields.Char(
        related='target_model_id.model', store=True, string='Target Model Name', index=True)

    enabled = fields.Boolean(
        string='Enabled', default=False, tracking=True,
        help='PROMOTION IS OPT-IN. While this is off, rows stay in staging and '
             'no business record is ever created, written or archived. That is a '
             'complete and correct outcome for most datasets.',
    )
    state = fields.Selection(
        STATE_SELECTION, string='Status', default='draft', required=True, index=True, tracking=True,
        help="'active' requires BOTH enabled and a passing validation.",
    )
    validation_message = fields.Text(
        string='Validation Output', readonly=True,
        help='Everything action_validate() found, whether it passed or not.',
    )

    identity_strategy = fields.Selection(
        IDENTITY_STRATEGY_SELECTION, string='Identity Strategy',
        default='sync_id_then_key', required=True,
        help='How a sheet row is matched to an Odoo record. Never row position: '
             'a user sorting the sheet must be a no-op.',
    )
    sync_id_column_header = fields.Char(
        string='Sync Id Column', default='_sync_id',
        help='Header label of the injected identity column, when the sheet carries one.',
    )
    writeback_sync_id = fields.Boolean(
        string='Write Sync Id Back To Sheet', default=False, readonly=True,
        help='Permanently unavailable in v1: writing an id back into the sheet '
             'requires Drive write scope, which this module structurally never '
             'requests. Displayed so the limitation is visible, not surprising.',
    )

    domain = fields.Char(
        string='Target Domain', default='[]',
        help='Extra scope on the target model. Records outside it are never '
             'considered owned by this mapping and are never touched.',
    )
    default_values = fields.Json(
        string='Default Values',
        help='{field: literal} applied on CREATE only. Never on update — a '
             'default that reasserted itself every run would fight the user.',
    )

    create_allowed = fields.Boolean(string='Create Allowed', default=True, tracking=True)
    update_allowed = fields.Boolean(string='Update Allowed', default=True, tracking=True)
    delete_policy = fields.Selection(
        DELETE_POLICY_SELECTION, string='Delete Policy', default='report', required=True, tracking=True,
        help="'soft' sets the archive flag. HARD DELETE IS NEVER AVAILABLE TO "
             'ANY AUTOMATED PATH, at any threshold, under any configuration.',
    )
    soft_delete_field = fields.Char(
        string='Soft Delete Field', default='active',
        help='The boolean set to False by a soft delete. Must exist on the target model.',
    )

    auto_heal = fields.Boolean(
        string='Auto-heal', default=False, tracking=True,
        help='Per-dataset opt-in, ships OFF. Even when on, a soft delete above '
             'threshold still waits for a human. Read the drift reports for at '
             'least a week before turning this on.',
    )
    dry_run_default = fields.Boolean(
        string='Dry Run By Default', default=True,
        help='A plan built with this on is a preview: it computes every action '
             'and executes none of them.',
    )

    create_threshold_abs = fields.Integer(string='Create Threshold (abs)', default=50)
    create_threshold_pct = fields.Float(string='Create Threshold (%)', default=20.0)
    delete_threshold_abs = fields.Integer(string='Delete Threshold (abs)', default=20)
    delete_threshold_pct = fields.Float(string='Delete Threshold (%)', default=5.0)

    quarantine_runs = fields.Integer(
        string='Quarantine Runs', default=2,
        help='A row must be absent this many consecutive COMPLETE runs before it '
             'is delete-eligible. ANDed with the hours below, never ORed.',
    )
    quarantine_hours = fields.Integer(string='Quarantine Hours', default=24)
    flap_limit = fields.Integer(
        string='Flap Limit', default=3,
        help='Consecutive runs writing the same (sync_id, field) before the field '
             'is declared non-convergent and stops being written. Three writes of '
             'the same field is the signature of an asymmetric normalizer; '
             'continuing to write is the bug, stopping and reporting is the fix.',
    )

    spec_version = fields.Char(
        string='Spec Version', compute='_compute_spec_version', store=True, index=True,
        help='H(serialized column contract ‖ CANON_VERSION). Every cached hash is '
             'keyed by this; changing any column option invalidates all of them.',
    )

    column_ids = fields.One2many('gdrive.mapping.column', 'mapping_id', string='Columns')
    promotion_link_ids = fields.One2many('gdrive.promotion.link', 'mapping_id', string='Promotion Links')

    active = fields.Boolean(string='Active', default=True)

    # ------------------------------------------------------------------
    # Constraints
    # ------------------------------------------------------------------
    @api.constrains('dataset_id', 'active')
    def _check_one_mapping_per_dataset(self):
        """At most one *live* mapping per dataset (SPEC §3.8).

        A Python constraint rather than a SQL unique index on purpose: archiving
        a mapping and authoring a replacement is a normal, supported workflow,
        and a SQL constraint would forbid it. Only unarchived mappings compete.
        """
        for mapping in self:
            if not mapping.active or not mapping.dataset_id:
                continue
            other = self.search([
                ('dataset_id', '=', mapping.dataset_id.id),
                ('id', '!=', mapping.id),
                ('active', '=', True),
            ], limit=1)
            if other:
                raise ValidationError(_(
                    'Dataset %(tab)s already has an active mapping (%(other)s). '
                    'Archive it before creating another, so it is always '
                    'unambiguous which contract governs the tab.',
                    tab=mapping.dataset_id.display_name, other=other.display_name,
                ))

    @api.constrains('quarantine_runs', 'quarantine_hours', 'flap_limit')
    def _check_guard_values(self):
        """Refuse guard settings that would disable the guard entirely.

        A ``quarantine_runs`` of 0 means "delete on the first run in which a row
        is absent", which is exactly the behaviour every delete guard in SPEC
        §9.6 exists to prevent — a single failed read then looks like a mass
        deletion.
        """
        for mapping in self:
            if mapping.quarantine_runs < 1:
                raise ValidationError(_('Quarantine Runs must be at least 1: deleting on the '
                                        'first absence makes one failed read look like a mass delete.'))
            if mapping.quarantine_hours < 1:
                raise ValidationError(_('Quarantine Hours must be at least 1.'))
            if mapping.flap_limit < 1:
                raise ValidationError(_('Flap Limit must be at least 1.'))

    @api.depends('name', 'target_model')
    def _compute_display_name(self):
        """Odoo 18: ``name_get()`` is removed and silently never called."""
        for mapping in self:
            if mapping.target_model:
                mapping.display_name = '%s → %s' % (mapping.name or '', mapping.target_model)
            else:
                mapping.display_name = mapping.name or ''

    # ------------------------------------------------------------------
    # spec_version
    # ------------------------------------------------------------------
    @api.depends('target_model', 'dataset_id.sheet_timezone',
                 *['column_ids.%s' % name for name in CONTRACT_FIELDS])
    def _compute_spec_version(self):
        """Hash the whole column contract together with ``CANON_VERSION``.

        Deliberately does **not** validate: a compute that raises makes the
        record unopenable in the form view, and a half-authored mapping (a
        datetime column before the timezone is set, a selection column before
        its value_map is filled) is a completely normal intermediate state.
        Validation is ``action_validate()``'s job, where the user asked for it
        and can read the answer. A contract that cannot be serialized at all
        yields an empty ``spec_version``, which every cache treats as a miss —
        the safe direction.
        """
        for mapping in self:
            try:
                contracts = [
                    contract_from_mapping_dict(column.to_contract_dict())
                    for column in mapping.column_ids
                    if column.ctype != 'ignore'
                ]
                mapping.spec_version = spec_version_for_contracts(contracts)
            except Exception:
                _logger.exception(
                    "Could not serialize the column contract of mapping %s (id=%s); "
                    "spec_version left empty so every cached hash is treated as a miss.",
                    mapping.name, mapping.id or 'new',
                )
                mapping.spec_version = False

    def _invalidate_dataset_cache(self, reason='contract changed'):
        """Wipe the dataset's cached verification hashes.

        Called whenever the contract moves. WHY it must happen eagerly rather
        than "next time somebody notices": between the edit and the next verify
        pass, every stored ``h_row`` / ``bucket_hashes`` / ``h_dataset_*`` value
        was computed under the *previous* contract. A comparison that hits one of
        them reports ``verified`` over data it has never compared under the
        current rules.
        """
        for mapping in self:
            dataset = mapping.dataset_id
            if not dataset:
                continue
            vals = {k: v for k, v in DATASET_CACHE_FIELDS.items() if k in dataset._fields}
            if 'spec_version' in dataset._fields:
                vals['spec_version'] = mapping.spec_version or False
            try:
                dataset.sudo().write(vals)
            except Exception:
                _logger.exception(
                    "Could not clear cached hashes on dataset %s after %s; a manual "
                    "full resync is required before its verification results can be trusted.",
                    dataset.display_name, reason,
                )
                continue
            _logger.info(
                "Cleared cached verification hashes on dataset %s (%s, spec_version=%s).",
                dataset.display_name, reason, mapping.spec_version or '-',
            )
        return True

    def _on_contract_changed(self, reason='contract changed'):
        """Re-derive ``spec_version`` and invalidate anything keyed by it.

        Reading the stored compute forces any pending recomputation, so the
        value compared here is the *new* one. Called from this model's ``write``
        and from ``gdrive.mapping.column``'s create/write/unlink, because a
        contract edit usually arrives as a change to a child row and the parent's
        ``write`` is never called in that case.
        """
        for mapping in self:
            before = mapping.spec_version
            mapping.invalidate_recordset(['spec_version'])
            after = mapping.spec_version
            if before != after:
                mapping._invalidate_dataset_cache(reason)
        return True

    # ------------------------------------------------------------------
    # ORM overrides
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        """Create the mapping and stamp its dataset's ``spec_version``."""
        mappings = super().create(vals_list)
        mappings._link_dataset()
        mappings._invalidate_dataset_cache('mapping created')
        return mappings

    def write(self, vals):
        """Write, then downgrade the state if the contract or target moved.

        A validated mapping whose target model, domain or column contract just
        changed has **not** been validated — the assertions that passed were
        about a different contract. Silently keeping ``state='active'`` there is
        how a mapping ends up promoting into a field that no longer exists.
        """
        invalidating = {'target_model_id', 'dataset_id', 'identity_strategy',
                        'sync_id_column_header', 'domain', 'soft_delete_field'}
        downgrade = bool(invalidating & set(vals)) and 'state' not in vals
        result = super().write(vals)
        if downgrade:
            for mapping in self.filtered(lambda m: m.state in ('validated', 'active')):
                mapping.state = 'draft'
                mapping.validation_message = _(
                    'The contract changed after validation, so this mapping was '
                    'returned to draft. Re-run Validate before enabling it.')
                _logger.info("Mapping %s returned to draft: %s changed after validation.",
                             mapping.display_name, ', '.join(sorted(invalidating & set(vals))))
        if 'dataset_id' in vals:
            self._link_dataset()
        self._on_contract_changed('mapping written')
        return result

    def _link_dataset(self):
        """Point ``gdrive.dataset.mapping_id`` back at this mapping.

        The back-reference is what lets the staging lane find the contract for a
        tab without searching, and what drives ``dataset.promotion_enabled``.
        Written with ``sudo()`` because a manager may author a mapping while
        having only read rights on the dataset.
        """
        for mapping in self:
            dataset = mapping.dataset_id
            if not dataset or 'mapping_id' not in dataset._fields:
                continue
            if dataset.mapping_id.id != mapping.id:
                dataset.sudo().mapping_id = mapping.id
        return True

    # ------------------------------------------------------------------
    # The contract, serialized for lane C
    # ------------------------------------------------------------------
    def _contract_columns(self):
        """Return this mapping's live ``ColumnContract`` objects, in sequence order."""
        self.ensure_one()
        return [
            contract_from_mapping_dict(column.to_contract_dict())
            for column in self.column_ids.sorted(lambda c: (c.sequence, c.id))
            if column.ctype != 'ignore'
        ]

    def _natural_key_names(self):
        """The contract keys forming the composite natural key, in sequence order.

        Order is part of the key: ``identity_key_bytes`` is length-prefixed but
        not order-insensitive, so reordering the key columns would change every
        natural key in the tab. Sorting by ``sequence`` (the field the UI lets a
        human drag) makes that order explicit and stable.
        """
        self.ensure_one()
        return [
            column.contract_key
            for column in self.column_ids.sorted(lambda c: (c.sequence, c.id))
            if column.is_natural_key and column.ctype != 'ignore'
        ]

    def _sync_id_header_canon(self):
        """Canonical header of the injected ``_sync_id`` column, or ``''``."""
        self.ensure_one()
        if not self.sync_id_column_header:
            return ''
        return TEXT_CANON(self.sync_id_column_header, contract_from_mapping_dict({
            'key': 'header', 'ctype': 'text', 'text_trim': True,
            'text_collapse_ws': True, 'text_case': 'preserve',
        }))

    def _contract_bundle(self, dataset_columns):
        """Build the bundle ``gdrive.staged.row._stage_dataset_rows`` consumes.

        ``dataset_columns`` is the tab's live ``gdrive.dataset.column`` set. The
        join is on ``header_canon``, never on ``col_index``: that is the single
        choice which makes dragging a column in the spreadsheet a genuine no-op.

        Returns the documented staging bundle:

        ``contract``
            ``[(key, ColumnContract, col_index)]`` for every mapped column that
            still resolves to a physical column.
        ``extra``
            the same shape for every *unmapped* physical column, hashed
            separately into ``h_extra`` so schema growth is visible without
            polluting the compared hash.
        ``natural_key_keys``, ``sync_id_index``, ``identity_strategy``,
        ``spec_version``
            the identity cascade's inputs.
        """
        self.ensure_one()
        by_header = {}
        for column in dataset_columns:
            if column.col_index is not None and column.col_index >= 0:
                by_header.setdefault(column.header_canon, column)

        contract_entries = []
        mapped_headers = set()
        for column in self.column_ids.sorted(lambda c: (c.sequence, c.id)):
            if column.ctype == 'ignore':
                continue
            physical = by_header.get(column.header_canon)
            if physical is None:
                # Not an error here: the header gate in lane D is what turns a
                # missing mapped column into a hard stop with zero rows staged.
                # Silently dropping it from the contract would instead produce a
                # plausible hash over fewer columns.
                _logger.warning(
                    "Mapping %s references header %r, which is absent from tab %s.",
                    self.display_name, column.header_canon, self.dataset_id.display_name,
                )
                continue
            mapped_headers.add(column.header_canon)
            contract_entries.append((
                column.contract_key,
                contract_from_mapping_dict(column.to_contract_dict(physical)),
                physical.col_index,
            ))

        extra_entries = []
        sync_header = self._sync_id_header_canon()
        for column in dataset_columns:
            if column.col_index is None or column.col_index < 0:
                continue
            if column.header_canon in mapped_headers or column.header_canon == sync_header:
                continue
            extra_entries.append((
                column.slug,
                contract_from_mapping_dict({
                    'key': column.slug, 'slug': column.slug,
                    'header_canon': column.header_canon, 'ctype': 'text',
                    'authority': 'report', 'empty_is_null': True,
                }),
                column.col_index,
            ))

        sync_index = None
        if sync_header and sync_header in by_header:
            sync_index = by_header[sync_header].col_index

        return {
            'spec_version': self.spec_version or '',
            'contract': contract_entries,
            'extra': extra_entries,
            'natural_key_keys': self._natural_key_names(),
            'sync_id_index': sync_index,
            'identity_strategy': self.identity_strategy,
        }

    def tab_uid(self):
        """``"<google_id>/<sheet_gid>"`` — the value stored in ``x_gdrive_source_dataset``.

        Derived in exactly one place because the ownership rule of SPEC §3.10
        compares it literally: a record whose ``x_gdrive_source_dataset`` does
        not equal this string is UNMANAGED and is never touched.
        """
        self.ensure_one()
        dataset = self.dataset_id
        return '%s/%s' % (dataset.node_id.google_id or '', dataset.sheet_gid or 0)

    def to_contract(self):
        """The contract dict handed to ``gdrive.reconciler.plan()``."""
        self.ensure_one()
        return {
            'spec_version': self.spec_version or '',
            'tab_uid': self.tab_uid(),
            'target_model': self.target_model or '',
            'columns': [column.to_contract_dict() for column in
                        self.column_ids.sorted(lambda c: (c.sequence, c.id))
                        if column.ctype != 'ignore'],
        }

    def to_policy(self, dry_run=None):
        """The execution policy dict handed to ``gdrive.reconciler.plan()``.

        Every guard the planner applies is in here rather than read from the
        ORM inside the planner, because the planner is *pure*: the same inputs
        must always produce a byte-identical plan, which is impossible if it can
        observe a record that changed underneath it mid-run.
        """
        self.ensure_one()
        return {
            'identity_strategy': self.identity_strategy,
            'create_allowed': self.create_allowed,
            'update_allowed': self.update_allowed,
            'delete_policy': self.delete_policy,
            'soft_delete_field': self.soft_delete_field or 'active',
            'auto_heal': self.auto_heal,
            'dry_run': self.dry_run_default if dry_run is None else bool(dry_run),
            'create_threshold_abs': self.create_threshold_abs,
            'create_threshold_pct': self.create_threshold_pct,
            'delete_threshold_abs': self.delete_threshold_abs,
            'delete_threshold_pct': self.delete_threshold_pct,
            'quarantine_runs': self.quarantine_runs,
            'quarantine_hours': self.quarantine_hours,
            'flap_limit': self.flap_limit,
        }

    def sheet_snapshot(self):
        """The sheet side of the comparison, read out of ``gdrive.staged.row``.

        Quarantined rows are excluded from ``rows`` but **do** set ``blocking``:
        a row holding an ``e:`` token has no comparable content, and pretending
        it simply is not there would make "100 rows are broken" and "100 rows
        were deleted" produce the same snapshot — with the delete planner then
        acting on the difference.
        """
        self.ensure_one()
        staged = self.env['gdrive.staged.row'].sudo().search([
            ('dataset_id', '=', self.dataset_id.id),
            ('state', 'in', ('staged', 'promoted')),
        ])
        blocked = self.env['gdrive.staged.row'].sudo().search_count([
            ('dataset_id', '=', self.dataset_id.id),
            ('state', '=', 'quarantined'),
        ])
        rows = [{
            'sync_id': row.sync_id or '',
            'natural_key': row.natural_key or '',
            'identity_source': row.identity_source or 'none',
            'canon': row.canon or {},
            'h_row': row.h_row or '',
            'h_row_folded': row.h_row_folded or '',
            'a1_ref': row.a1_ref or '',
            'row_number': row.row_number or 0,
            'staged_row_id': row.id,
        } for row in staged]
        return {
            'rows': rows,
            'row_count': len(rows),
            'read_complete': bool(self.dataset_id.last_read_complete),
            'blocking': bool(blocked),
            'tab_uid': self.tab_uid(),
        }

    # ------------------------------------------------------------------
    # Validation (SPEC §3.8, the seven assertions)
    # ------------------------------------------------------------------
    def action_validate(self):
        """Assert the contract is executable, and refuse ``validated`` otherwise.

        Every assertion here is something that would otherwise fail *at run
        time*, inside a cron, halfway through writing business records. Failing
        loudly in a button the user just pressed is the entire point: a new Odoo
        selection value, a renamed spreadsheet header or a field that became
        readonly must stop the contract, not silently drift.
        """
        for mapping in self:
            errors, notes = mapping._validate_contract()
            if errors:
                mapping.write({
                    'state': 'blocked',
                    'validation_message': '\n'.join('✖ %s' % e for e in errors + notes),
                })
                _logger.warning("Mapping %s failed validation: %s",
                                mapping.display_name, ' | '.join(errors))
                continue

            # 6. Technical fields, and the partial unique index that makes every
            #    create an idempotent upsert.
            try:
                created = mapping._ensure_technical_fields()
                if created:
                    notes.append(_('Created technical field(s) on %(model)s: %(fields)s',
                                   model=mapping.target_model, fields=', '.join(created)))
                mapping._ensure_upsert_index()
            except Exception as exc:
                _logger.exception("Mapping %s: could not prepare the target model.",
                                  mapping.display_name)
                mapping.write({
                    'state': 'blocked',
                    'validation_message': _('Could not prepare target model %(model)s: %(err)s',
                                            model=mapping.target_model, err=exc),
                })
                continue

            # 7. Recompute spec_version; a change invalidates every cached hash.
            mapping._on_contract_changed('validated')

            mapping.write({
                'state': 'active' if mapping.enabled else 'validated',
                'validation_message': '\n'.join(
                    [_('Validation passed at %s.', fields.Datetime.now())] +
                    ['• %s' % n for n in notes]),
            })
            _logger.info("Mapping %s validated (state=%s, spec_version=%s).",
                         mapping.display_name, mapping.state, mapping.spec_version)
        return True

    def _validate_contract(self):
        """Run assertions 1–5 and return ``(errors, notes)``.

        Split out of :meth:`action_validate` so the mapping-builder wizard and
        the tests can ask "would this validate?" without mutating anything.
        """
        self.ensure_one()
        errors = []
        notes = []
        model_name = self.target_model

        if not model_name or model_name not in self.env:
            return ([_('Target model %s is not installed.', model_name or '-')], notes)
        target = self.env[model_name]

        live_columns = self.dataset_id.column_ids.filtered(lambda c: c.col_index >= 0)
        headers = {}
        for column in live_columns:
            headers.setdefault(column.header_canon, []).append(column)

        contract_columns = self.column_ids.filtered(lambda c: c.ctype != 'ignore')
        if not contract_columns:
            errors.append(_('The mapping declares no columns; there would be nothing to promote.'))

        field_defs = target.fields_get()
        seen_fields = {}

        for column in contract_columns:
            label = column.header_canon or _('(unnamed column)')

            # 1. Every header_canon resolves to exactly one live dataset column.
            matches = headers.get(column.header_canon, [])
            if not matches:
                errors.append(_('Column %(h)s does not exist in tab %(tab)s. Either the header '
                                'was renamed in the spreadsheet or the mapping is stale.',
                                h=label, tab=self.dataset_id.display_name))
            elif len(matches) > 1:
                errors.append(_('Header %(h)s appears %(n)s times in tab %(tab)s; the mapping '
                                'cannot say which physical column it means.',
                                h=label, n=len(matches), tab=self.dataset_id.display_name))

            # 2. Every odoo_field exists on the target and is writable.
            field_name = column.odoo_field
            if not field_name:
                errors.append(_('Column %s has no Odoo field and is not marked ignore.', label))
                continue
            definition = field_defs.get(field_name)
            if definition is None:
                errors.append(_('Field %(f)s does not exist on %(m)s.', f=field_name, m=model_name))
                continue
            if field_name in seen_fields:
                errors.append(_('Field %(f)s is targeted by two columns (%(a)s and %(b)s); one of '
                                'them would silently vanish from every row hash.',
                                f=field_name, a=seen_fields[field_name], b=label))
            seen_fields[field_name] = label
            if column.authority == 'sheet':
                descriptor = target._fields.get(field_name)
                if descriptor is not None and not descriptor.store:
                    errors.append(_('Field %s is a non-stored compute and cannot be written.', field_name))
                elif descriptor is not None and descriptor.readonly and not descriptor.inverse:
                    errors.append(_('Field %s is readonly with no inverse and cannot be written.', field_name))

            # 3. Selection value_map values are a subset of the field's keys.
            if column.ctype == 'selection':
                allowed = {key for key, _lbl in (definition.get('selection') or [])}
                declared = set((column.value_map or {}).values())
                unknown = declared - allowed
                if unknown:
                    errors.append(_('Column %(h)s maps to selection value(s) %(v)s, which do not '
                                    'exist on %(m)s.%(f)s. A new Odoo state must fail here, loudly, '
                                    'not drift silently at run time.',
                                    h=label, v=', '.join(sorted(unknown)), m=model_name, f=field_name))
                if not declared:
                    errors.append(_('Selection column %s has an empty value map; every sheet label '
                                    'would fail to resolve.', label))

            # 4. Every money column resolves a currency.
            if column.ctype == 'money' and not (column.currency_field_id or column.default_currency_id):
                errors.append(_('Money column %s resolves no currency: set a companion currency '
                                'field on the target model or a default currency. Guessing one '
                                'silently quantizes to the wrong number of decimals.', label))

            # datetime columns need a declared timezone; a spreadsheet has none.
            if column.ctype == 'datetime' and not self.dataset_id.sheet_timezone:
                errors.append(_('Column %s is a datetime but tab %s declares no timezone. Guessing '
                                'one shifts every value by hours.', label, self.dataset_id.display_name))

            if column.ctype in ('many2one', 'm2m'):
                comodel = column.comodel or definition.get('relation')
                if not comodel or comodel not in self.env:
                    errors.append(_('Relational column %(h)s targets comodel %(c)s, which is not '
                                    'installed.', h=label, c=comodel or '-'))
                elif column.m2o_match_field and column.m2o_match_field not in self.env[comodel]._fields:
                    errors.append(_('Column %(h)s matches %(c)s on field %(f)s, which does not exist.',
                                    h=label, c=comodel, f=column.m2o_match_field))

        # 5. An identity strategy that can actually identify something.
        sync_header = self._sync_id_header_canon()
        has_sync_column = bool(sync_header) and sync_header in headers
        has_natural_key = any(c.is_natural_key for c in contract_columns)
        if not has_natural_key:
            if self.identity_strategy == 'sync_id' and has_sync_column:
                notes.append(_('Identity comes from the %s column alone.', self.sync_id_column_header))
            else:
                errors.append(_('No column is marked as a natural key, and the tab carries no '
                                '%(col)s column under strategy %(s)s. Without a stable identity, a '
                                'row edit is indistinguishable from a delete plus a create.',
                                col=self.sync_id_column_header or '_sync_id', s=self.identity_strategy))
        if self.identity_strategy != 'natural_key' and not has_sync_column:
            notes.append(_('Tab carries no %s column; identity falls back to the natural key.',
                           self.sync_id_column_header or '_sync_id'))

        # Soft delete needs somewhere to write.
        if self.delete_policy == 'soft':
            flag = self.soft_delete_field or 'active'
            if flag not in field_defs:
                errors.append(_('Delete policy is soft but field %(f)s does not exist on %(m)s.',
                                f=flag, m=model_name))
            elif field_defs[flag].get('type') != 'boolean':
                errors.append(_('Soft delete field %s must be a boolean.', flag))

        # The declared domain must parse; a broken domain silently widens or
        # narrows what this mapping believes it owns.
        try:
            parsed = self._parsed_domain()
            if not isinstance(parsed, list):
                errors.append(_('Target domain must evaluate to a list.'))
        except Exception as exc:
            errors.append(_('Target domain does not parse: %s', exc))

        return errors, notes

    def _parsed_domain(self):
        """Evaluate ``domain`` safely, defaulting to the empty domain."""
        self.ensure_one()
        from odoo.tools.safe_eval import safe_eval
        return safe_eval(self.domain or '[]')

    def _ensure_technical_fields(self):
        """Create ``x_gdrive_sync_id`` / ``x_gdrive_source_dataset`` if absent.

        Created as ``state='manual'`` ``ir.model.fields`` so they survive on a
        model this module does not own, and so the uninstall hook can leave them
        in place — they are the only surviving evidence of which business
        records came from a sheet, and an uninstall is frequently a step in a
        reinstall.

        Returns the list of field names actually created.
        """
        self.ensure_one()
        target = self.env[self.target_model]
        created = []
        wanted = [
            (SYNC_ID_FIELD, 'Google Sheet Sync Id', True,
             'ULID assigned at plan time. The upsert key that makes every create idempotent.'),
            (SOURCE_DATASET_FIELD, 'Google Sheet Source', False,
             '"<google_id>/<sheet_gid>" of the tab this record came from. Ownership marker.'),
        ]
        for name, label, indexed, help_text in wanted:
            if name in target._fields:
                continue
            self.env['ir.model.fields'].sudo().create({
                'name': name,
                'field_description': label,
                'model_id': self.target_model_id.id,
                'ttype': 'char',
                'state': 'manual',
                'index': indexed,
                'copy': False,
                'store': True,
                'help': help_text,
            })
            created.append(name)
        if created:
            _logger.info("Created technical field(s) %s on %s.", ', '.join(created), self.target_model)
            # The registry must see the new columns before the index is built.
            self.env.flush_all()
        return created

    def _ensure_upsert_index(self):
        """Create the partial unique index on ``(x_gdrive_sync_id)``.

        This index is what turns every ``create`` action into an idempotent
        upsert: a retried or duplicated apply collapses into a no-op instead of
        producing a second copy of the same business record. It is *partial*
        because the target is a shared business model whose human-created rows
        carry NULL there and must not collide with one another.

        The import is local: ``gdrive_odoo_sync/__init__.py`` imports ``models``
        before it defines this helper, so a module-level import would be a
        circular import at install time.
        """
        self.ensure_one()
        from .. import _ensure_sync_id_unique_index
        table = self.env[self.target_model]._table
        return _ensure_sync_id_unique_index(self.env, table)

    # ------------------------------------------------------------------
    # Promotion cron
    # ------------------------------------------------------------------
    @api.model
    def _cron_promote(self):
        """Promote every eligible dataset (SPEC §6, cron ``ir_cron_gdrive_promote``).

        **This method must never raise.** Odoo 18 auto-deactivates a scheduled
        action after repeated failures, so one malformed spreadsheet would
        silently switch off promotion for the whole database and nobody would
        notice for weeks. Every per-mapping failure is caught, logged as a
        ``gdrive.sync.run.line`` at ``error``, and the run ends ``partial``.

        Eligibility is deliberately narrow — ``enabled`` AND ``state='active'``
        AND a complete last read — so that on a fresh install, where nothing is
        enabled, this is a no-op that logs that it found nothing to do.
        """
        started = time.monotonic()
        mappings = self.sudo().search([
            ('enabled', '=', True),
            ('state', '=', 'active'),
            ('active', '=', True),
        ])
        if not mappings:
            _logger.info("gdrive promote: no enabled, validated mapping; nothing to do.")
            return True

        # Grouped by connection because the advisory lock is per connection:
        # taking it once per group is what makes overlapping runs structurally
        # impossible without serializing unrelated connections behind each other.
        by_connection = {}
        for mapping in mappings:
            by_connection[mapping.connection_id] = (
                by_connection.get(mapping.connection_id, self.browse()) | mapping)

        processed = 0
        for connection, group in by_connection.items():
            if not connection:
                _logger.warning("Skipping %d mapping(s) with no connection.", len(group))
                continue
            try:
                if not connection._acquire_lock():
                    _logger.info("gdrive promote: connection %s is already being processed; skipping.",
                                 connection.display_name)
                    continue
            except Exception:
                _logger.exception("gdrive promote: could not take the advisory lock for %s.",
                                  connection.display_name)
                continue

            run = self.env['gdrive.sync.run']._start(
                connection, trigger='cron', mode='delta', stages=['promote'])
            try:
                for mapping in group:
                    if time.monotonic() - started > CRON_BUDGET_SEC:
                        run._log('BUDGET_EXHAUSTED',
                                 'Wall-clock budget reached with %d mapping(s) left; re-triggering.'
                                 % (len(group) - processed),
                                 level='warning', stage='promote')
                        trigger_cron(self.env, 'gdrive_odoo_sync.ir_cron_gdrive_promote')
                        break
                    try:
                        mapping._promote_once(run)
                    except Exception as exc:
                        # One bad mapping must not abort the others, and it must
                        # not be invisible: the failure is a first-class log line.
                        self.env.cr.rollback()
                        _logger.exception("Promotion failed for mapping %s.", mapping.display_name)
                        run._log('PROMOTE_FAILED',
                                 'Mapping %s failed: %s' % (mapping.display_name, exc),
                                 level='error', stage='promote', dataset=mapping.dataset_id)
                    processed += 1
                    if processed % COMMIT_BATCH == 0:
                        self.env.cr.commit()
            except Exception:
                self.env.cr.rollback()
                _logger.exception("gdrive promote: unexpected failure on connection %s.",
                                  connection.display_name)
            finally:
                try:
                    run._finish()
                    self.env.cr.commit()
                except Exception:
                    self.env.cr.rollback()
                    _logger.exception("Could not close promotion run %s.", run.name)

        _logger.info("gdrive promote finished: %d mapping(s) in %.1fs.",
                     processed, time.monotonic() - started)
        return True

    def _promote_once(self, run=None, dry_run=None):
        """Plan and (unless dry-run) apply one mapping's promotion.

        The three steps are strictly separated because only the middle one is
        pure: reading both sides touches the database and the clock, planning
        must not, and applying is the only step allowed to write. That split is
        what makes a dry-run preview and a real apply provably the same plan.

        Returns the ``gdrive.plan`` record, or an empty recordset when there was
        nothing to do.
        """
        self.ensure_one()
        if not self.enabled or self.state != 'active':
            _logger.info("Mapping %s is not enabled and validated; nothing promoted.",
                         self.display_name)
            return self.env['gdrive.plan']

        sheet = self.sheet_snapshot()
        if not sheet['read_complete']:
            message = ('Tab %s was not read completely; promotion is skipped. An incomplete '
                       'read is indistinguishable from a mass deletion.' % self.dataset_id.display_name)
            _logger.warning(message)
            if run:
                run._mark_incomplete('PROMOTE_INCOMPLETE_READ', message,
                                     stage='promote', dataset=self.dataset_id)
            return self.env['gdrive.plan']

        odoo_snapshot = self.env['gdrive.promoter'].read_odoo_snapshot(self)
        plan_dict = self.env['gdrive.reconciler'].plan(
            sheet, odoo_snapshot, self.to_contract(),
            self.to_policy(dry_run=dry_run), fields.Datetime.to_string(fields.Datetime.now()),
        )
        plan = self._materialize_plan(plan_dict, run=run, dry_run=dry_run)
        if run:
            run._log('PROMOTE_PLANNED',
                     'Mapping %s produced %d action(s) and %d drift(s).'
                     % (self.display_name, len(plan_dict.get('actions') or []),
                        len(plan_dict.get('drifts') or [])),
                     level='info', stage='promote', dataset=self.dataset_id)
        return plan

    def _materialize_plan(self, plan_dict, run=None, dry_run=None):
        """Persist a planner result as ``gdrive.plan`` + ``gdrive.plan.action``.

        Keys are filtered against the sibling models' live ``_fields`` rather
        than assumed: the planner is free to add diagnostic keys to its output
        (it already carries per-action classification detail the plan table does
        not store), and an unexpected key must not turn a good plan into a
        traceback inside a cron.
        """
        self.ensure_one()
        plan_model = self.env['gdrive.plan'].sudo()
        action_model = self.env['gdrive.plan.action'].sudo()

        header = {
            'dataset_id': self.dataset_id.id,
            'mapping_id': self.id,
            'dry_run': self.dry_run_default if dry_run is None else bool(dry_run),
            'run_id': run.id if run else False,
        }
        for key, value in (plan_dict or {}).items():
            if key in ('actions', 'drifts'):
                continue
            if key in plan_model._fields:
                header[key] = value
        header = {k: v for k, v in header.items() if k in plan_model._fields}
        plan = plan_model.create(header)

        rows = []
        for index, action in enumerate(plan_dict.get('actions') or []):
            vals = {k: v for k, v in action.items() if k in action_model._fields}
            vals['plan_id'] = plan.id
            vals.setdefault('sequence', (index + 1) * 10)
            rows.append(vals)
        if rows:
            action_model.create(rows)
        _logger.info("Mapping %s: plan %s materialized with %d action(s).",
                     self.display_name, plan.display_name, len(rows))
        return plan

    # ------------------------------------------------------------------
    # UI actions
    # ------------------------------------------------------------------
    def action_preview(self):
        """Build a dry-run plan and open it, without writing a single record."""
        self.ensure_one()
        if self.state != 'active':
            raise UserError(_('Validate and enable the mapping before previewing a promotion.'))
        plan = self._promote_once(dry_run=True)
        if not plan:
            raise UserError(_('No plan was produced: the tab has no complete read yet.'))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Promotion Plan'),
            'res_model': 'gdrive.plan',
            'view_mode': 'form',
            'res_id': plan.id,
        }

    def action_open_links(self):
        """Open this mapping's promotion links."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Promotion Links'),
            'res_model': 'gdrive.promotion.link',
            'view_mode': 'list,form',
            'domain': [('mapping_id', '=', self.id)],
            'context': {'default_mapping_id': self.id},
        }
