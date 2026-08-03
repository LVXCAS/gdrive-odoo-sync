# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. See LICENSE (LGPL-3) for full copyright and licensing details.
"""``gdrive.dataset`` — one spreadsheet tab (SPEC §3.5, §5.4, §9.1).

A dataset is **one tab of one workbook**, and its identity is
``(node_id, sheet_gid)`` — never the title. Native Sheets keep their numeric
``sheetId`` across renames, so a rename is provably a rename. ``.xlsx``
worksheets have no stable id at all, so they get the negative surrogate
``-(1 + worksheet_index)``: negative on purpose, because gid ``0`` is a
perfectly ordinary native gid and a non-negative surrogate would collide with a
real one the day a workbook is converted to a native Sheet.

Four behaviours in this file are load-bearing and easy to get wrong:

* **The header gate is a hard stop.** If an enabled mapping references a
  ``header_canon`` that is no longer present in the sheet, the dataset stages
  **zero rows**. Reading an absent mapped column as empty cells would write NULL
  over an entire Odoo column — the single most destructive failure mode in sheet
  sync — so it is made structurally impossible rather than merely warned about.
* **``EMPTY_TAB`` is a mass-delete signal, never "all rows were deleted".** A
  tab that reads as zero rows where the previous complete read saw N > 0 blocks.
  A truncated read, a lost permission and a genuinely emptied tab are
  indistinguishable, and only one of those three interpretations is safe.
* **The fast path is keyed by ``spec_version``.** A stored hash whose
  ``spec_version`` differs from the current one is treated as *absent*, never as
  a cache hit. Serving a hash computed by an older normalizer as ``verified``
  is a silent false pass, which is the worst possible failure of a verification
  system.
* **Hashing is never reimplemented here.** Every digest comes from ``lib/``,
  which is stdlib-only and byte-reproducible by construction. A digest that
  depends on an ORM default or a server timezone reclassifies every dataset as
  drifted the day something unrelated is upgraded.
"""

import logging
import time
from collections import OrderedDict

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..lib.contract import (
    ColumnContract,
    contract_from_mapping_dict,
    default_text_contract,
    spec_version_for_contracts,
)
from ..lib.hashing import h_header_hex
from ..lib.merkle import dataset_digest
from ..lib.text_canon import TEXT_CANON
from ..services.drive_download import DriveDownloader
from ..services.errors import GDrivePermanentError, redact
from ..services.google_client import build_services
from ..services.sheets_reader import SheetsReader
from ..services.xlsx_reader import XlsxReader, gid_for_index
from .gdrive_sync_run import COMMIT_BATCH, CRON_BUDGET_SEC, trigger_cron

_logger = logging.getLogger(__name__)

#: The only ingestible ``sheetType``. ``OBJECT`` (chart) and ``DATA_SOURCE``
#: (BigQuery-connected) tabs hold no cells at all; staging them would produce a
#: 0-row dataset, which the EMPTY_TAB guard would correctly read as a mass-delete
#: signal — a false alarm generated entirely by us.
SHEET_TYPE_GRID = 'GRID'

#: Fallback IANA zone for a tab whose workbook does not declare one. A cell has
#: no timezone of its own, so a ``datetime`` column with no declared zone is a
#: *contract* error (validated on the mapping), never a runtime guess; this
#: default exists only so the field is never empty in the UI.
DEFAULT_SHEET_TIMEZONE = 'America/New_York'

#: Canonical header label of the injected identity column, used when no mapping
#: has declared one. Kept as the raw label, not the canonical token, because
#: ``TEXT_CANON`` is what turns one into the other and doing that in one place
#: is what keeps the two spellings from drifting apart.
DEFAULT_SYNC_ID_HEADER = '_sync_id'

SOURCE_KIND_SELECTION = [
    ('gsheet', 'Native Google Sheet'),
    ('xlsx', 'Uploaded .xlsx'),
]

STATE_SELECTION = [
    ('new', 'New'),
    ('staged', 'Staged'),
    ('mapped', 'Mapped'),
    ('verified', 'Verified'),
    ('drift', 'In Drift'),
    ('blocked', 'Blocked'),
    ('quarantined', 'Quarantined'),
]

BLOCK_REASON_SELECTION = [
    ('mapped_column_missing', 'Mapped Column Missing'),
    ('tab_missing', 'Tab Missing'),
    ('header_changed', 'Header Changed'),
    ('empty_tab', 'Tab Read As Empty'),
    ('access_lost', 'Access Lost'),
    ('file_trashed', 'File Trashed'),
    ('spec_mismatch', 'Spec Version Mismatch'),
    ('duplicate_identity', 'Duplicate Tab Identity'),
]

#: ``block_reason`` → the ``gdrive.drift.drift_type`` that describes it. Every
#: hard stop emits a ``structural`` finding so that "why did this tab stop?" is
#: answerable from the Drift list without reading the server log.
BLOCK_DRIFT_TYPE = {
    'mapped_column_missing': 'header_change',
    'header_changed': 'header_change',
    'tab_missing': 'tab_missing',
    'empty_tab': 'empty_tab',
    'access_lost': 'access_lost',
    'file_trashed': 'access_lost',
    'spec_mismatch': 'header_change',
    'duplicate_identity': 'duplicate_identity',
}


class GdriveDataset(models.Model):
    """One tab of one spreadsheet, with its header schema and content rollup."""

    _name = 'gdrive.dataset'
    _description = 'Google Drive Dataset (Spreadsheet Tab)'
    _order = 'node_id, tab_index'
    _rec_name = 'tab_title'

    node_id = fields.Many2one(
        'gdrive.node', string='Drive File', required=True, index=True, ondelete='cascade')
    connection_id = fields.Many2one(
        related='node_id.connection_id', store=True, index=True, string='Connection')
    company_id = fields.Many2one(
        related='node_id.company_id', store=True, index=True, string='Company')

    source_kind = fields.Selection(
        SOURCE_KIND_SELECTION, string='Source', required=True, default='gsheet',
        help='Determines which reader produces the values: the Sheets API or openpyxl.')
    sheet_gid = fields.Integer(
        string='Tab Id', required=True, index=True,
        help='THE identity. Native Sheets: the numeric sheetId, stable across '
             'renames. .xlsx: -(1 + worksheet_index), a negative surrogate, '
             'because an xlsx worksheet carries no stable id of its own.')
    tab_title = fields.Char(
        string='Tab', required=True,
        help='Display only. A rename updates this and logs INFO; it is never '
             'read as "the tab was deleted".')
    tab_index = fields.Integer(string='Position', default=0)
    hidden = fields.Boolean(
        string='Hidden', default=False,
        help='Hiding a tab in the UI is not a deletion, so hidden tabs are still read.')
    sheet_type = fields.Char(
        string='Sheet Type', default=SHEET_TYPE_GRID,
        help='GRID / OBJECT / DATA_SOURCE. Only GRID holds cells and only GRID is staged.')

    header_row = fields.Integer(string='Header Row', default=1, required=True, help='1-based.')
    first_data_row = fields.Integer(string='First Data Row', default=2, required=True, help='1-based.')
    sheet_timezone = fields.Char(
        string='Sheet Timezone', default=DEFAULT_SHEET_TIMEZONE,
        help='IANA name. Required for any datetime column: a spreadsheet cell '
             'has no timezone of its own and guessing one is a contract error.')

    header_fingerprint = fields.Char(
        string='Header Fingerprint', index=True,
        help='Hash of the sorted canonical header labels. Sorted, so a column '
             'reorder — which is a genuine no-op here — does not move it.')
    column_ids = fields.One2many('gdrive.dataset.column', 'dataset_id', string='Columns')
    used_range = fields.Char(
        string='Used Range',
        help="The A1 range the reader actually resolved, e.g. Sheet1!A1:M2411. "
             "Authoritative extent — never gridProperties, which is the "
             "allocated 1000x26 grid and would manufacture phantom rows.")
    row_count = fields.Integer(
        string='Rows', aggregator='sum', help='Data rows staged in the last complete read.')

    spec_version = fields.Char(
        string='Spec Version', index=True,
        help='H(contract, normalizer version). Every cached hash below is keyed '
             'by this: a stored hash whose spec_version differs must be treated '
             'as absent, never as a cache hit.')
    h_dataset_sheet = fields.Char(string='Sheet Hash', size=64)
    h_dataset_odoo = fields.Char(string='Odoo Hash', size=64)
    bucket_hashes = fields.Json(
        string='Bucket Hashes',
        help='The 256 Merkle bucket digests as hex. ~4 KB, never queried, never '
             'grouped — which is exactly what a Json column is for.')

    last_drive_version = fields.Char(string='Drive Version (cached)')
    last_drive_modified = fields.Datetime(string='Drive Modified (cached)')
    last_odoo_count = fields.Integer(string='Odoo Count (cached)')
    last_odoo_max_write_date = fields.Datetime(string='Odoo max(write_date) (cached)')

    last_stage_date = fields.Datetime(string='Last Staged')
    last_verify_date = fields.Datetime(string='Last Verified')
    last_full_verify_date = fields.Datetime(
        string='Last Full Verify', help='Forced weekly regardless of any fast path.')
    last_read_complete = fields.Boolean(
        string='Last Read Complete', default=False,
        help='The delete-planner gate. A partial read is byte-identical to "all '
             'the rows were deleted", so the proof that the read finished is a '
             'stored fact, not an assumption.')

    state = fields.Selection(
        STATE_SELECTION, string='Status', default='new', required=True, index=True)
    block_reason = fields.Selection(BLOCK_REASON_SELECTION, string='Block Reason')
    block_detail = fields.Text(string='Block Detail')

    mapping_id = fields.Many2one(
        'gdrive.mapping', string='Mapping', ondelete='set null',
        help='The single promotion mapping, if a human has built one.')
    promotion_enabled = fields.Boolean(
        related='mapping_id.enabled', store=True, string='Promotion Enabled')
    auto_heal_enabled = fields.Boolean(
        related='mapping_id.auto_heal', store=True, readonly=True, string='Auto-heal Enabled')

    staged_row_ids = fields.One2many('gdrive.staged.row', 'dataset_id', string='Staged Rows')
    active = fields.Boolean(string='Active', default=True)

    _sql_constraints = [
        ('dataset_uniq', 'unique(node_id, sheet_gid)', 'A tab appears once per file.'),
    ]

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------
    @api.depends('tab_title', 'node_id.name')
    def _compute_display_name(self):
        """Qualify the tab with its file: tab titles repeat across workbooks."""
        for dataset in self:
            file_name = dataset.node_id.name or ''
            dataset.display_name = ('%s — %s' % (file_name, dataset.tab_title or '')).strip(' —')

    # ------------------------------------------------------------------
    # Contract construction
    # ------------------------------------------------------------------
    def _header_contract(self) -> ColumnContract:
        """The contract used to canonicalize *header labels* themselves.

        Header matching has to be its own contract and not borrow a data
        column's: a mapping may declare ``text_case='fold'`` for a value column,
        and folding the header would make ``Invoice Number`` and
        ``INVOICE NUMBER`` the same join key — quietly merging two real columns.
        """
        return default_text_contract('header')

    def _tab_uid(self) -> str:
        """``"<drive file id>/<gid>"`` — the tab identity baked into the hash.

        Ties a digest to a specific tab of a specific Drive *file id*, so a hash
        can never be reused across the two distinct files that happen to share a
        title. Titles are display strings; file ids are identity.
        """
        self.ensure_one()
        return '%s/%d' % (self.node_id.google_id or '', self.sheet_gid or 0)

    def _mapping_is_usable(self) -> bool:
        """True when a mapping exists and is meant to drive canonicalization.

        Deliberately *not* gated on ``enabled``: a validated-but-disabled
        mapping still describes how the columns should be read, and using its
        contract for staging is what lets an administrator inspect real
        canonical values before switching promotion on. ``enabled`` gates
        *promotion*, which lives in lane E and never in this file.
        """
        self.ensure_one()
        mapping = self.mapping_id
        return bool(mapping and mapping.exists() and mapping.column_ids)

    def _contract_bundle(self, columns):
        """Build the plain dict ``_stage_dataset_rows`` consumes.

        ``columns`` is the ordered recordset of *live* ``gdrive.dataset.column``
        records (left to right). The bundle is a plain dict of plain values on
        purpose: lane C is forbidden from touching the ORM, so every ORM lookup
        — the contract options, the resolved timezone, the natural-key order —
        happens here, once, before a single cell is canonicalized.

        With no mapping every live column still gets a default *text* contract
        keyed by its slug. That is what makes ``Lucas_Clothing_Shopping_List``
        produce a stable dataset hash: staging is never opt-in (SPEC §5.4), and
        a tab with no contract could never be verified at all.
        """
        self.ensure_one()
        header_contract = self._header_contract()
        timezone = self.sheet_timezone or DEFAULT_SHEET_TIMEZONE
        index_by_canon = {c.header_canon: c.col_index for c in columns}
        slug_by_canon = {c.header_canon: c.slug for c in columns}

        contract_cols = []
        extra_cols = []
        natural_keys = []
        mapped_canons = set()

        if self._mapping_is_usable():
            for mapping_column in self.mapping_id.column_ids.sorted(
                    key=lambda c: (c.sequence, c.id)):
                canon = mapping_column.header_canon or ''
                mapped_canons.add(canon)
                if mapping_column.ctype == 'ignore':
                    # An ignored column keeps its slug and its place in h_extra,
                    # but contributes nothing to the compared row hash.
                    continue
                index = index_by_canon.get(canon)
                if index is None:
                    # Unreachable in practice: the header gate refuses to stage a
                    # dataset whose mapped column vanished. Skipping rather than
                    # indexing None keeps a mis-ordered caller from producing a
                    # plausible-looking hash over a column it never read.
                    _logger.error(
                        "Dataset %s: mapped header %r is absent while building the "
                        "contract; the header gate should have blocked this tab.",
                        self.id, canon)
                    continue
                contract = self._contract_for_mapping_column(mapping_column, canon, index)
                contract = contract.with_options(sheet_timezone=timezone, col_index=index)
                contract_cols.append((contract.hash_key, contract, index))
                if mapping_column.is_natural_key:
                    natural_keys.append((mapping_column.sequence, mapping_column.id,
                                         contract.hash_key))

            for column in columns:
                if column.header_canon in mapped_canons:
                    continue
                contract = default_text_contract(
                    column.slug, header_canon=column.header_canon, slug=column.slug)
                contract = contract.with_options(
                    sheet_timezone=timezone, col_index=column.col_index)
                extra_cols.append((column.slug, contract, column.col_index))
        else:
            for column in columns:
                contract = default_text_contract(
                    column.slug, header_canon=column.header_canon, slug=column.slug)
                contract = contract.with_options(
                    sheet_timezone=timezone, col_index=column.col_index)
                contract_cols.append((column.slug, contract, column.col_index))

        # --- identity -----------------------------------------------------
        sync_header = DEFAULT_SYNC_ID_HEADER
        if self._mapping_is_usable() and self.mapping_id.sync_id_column_header:
            sync_header = self.mapping_id.sync_id_column_header
        sync_canon = TEXT_CANON(sync_header, header_contract)
        sync_id_index = index_by_canon.get(sync_canon)

        natural_key_keys = [key for _seq, _mid, key in sorted(natural_keys)]
        if self._mapping_is_usable():
            strategy = self.mapping_id.identity_strategy or 'sync_id_then_key'
        elif sync_id_index is not None:
            # An unmapped tab that nevertheless carries a _sync_id column gets a
            # stable identity for free, which is what stops a user sorting the
            # sheet from churning every staged row. Deletes remain structurally
            # impossible for it: they require a mapping with delete_policy soft.
            strategy = 'sync_id'
        else:
            strategy = 'none'

        contracts = [c for _k, c, _i in contract_cols] + [c for _s, c, _i in extra_cols]
        return {
            'spec_version': spec_version_for_contracts(contracts),
            'contract': contract_cols,
            'extra': extra_cols,
            'natural_key_keys': natural_key_keys,
            'sync_id_index': sync_id_index,
            'identity_strategy': strategy,
            'slug_by_canon': slug_by_canon,
        }

    def _contract_for_mapping_column(self, mapping_column, canon, index) -> ColumnContract:
        """Translate one ``gdrive.mapping.column`` into a lane-C contract.

        Lane E owns ``to_contract_dict()``; this method exists so that a mapping
        model which has not yet grown it degrades to a *documented, loud* text
        contract rather than to a traceback in the middle of a cron. The
        fallback is text with ``empty_is_null`` — the only canonicalization that
        cannot silently be wrong, because it never coerces, rounds or guesses.
        """
        self.ensure_one()
        to_dict = getattr(mapping_column, 'to_contract_dict', None)
        if callable(to_dict):
            return contract_from_mapping_dict(to_dict())
        _logger.warning(
            "gdrive.mapping.column %s exposes no to_contract_dict(); falling back to "
            "a default text contract for header %r on dataset %s.",
            mapping_column.id, canon, self.id)
        key = mapping_column.odoo_field or self.column_ids.filtered(
            lambda c: c.header_canon == canon)[:1].slug or canon
        return default_text_contract(key, header_canon=canon, slug=key)

    # ------------------------------------------------------------------
    # Tab discovery
    # ------------------------------------------------------------------
    @api.model
    def _sync_tabs_from_sheets(self, node, sheets_reader, run=None):
        """Enumerate the tabs of a native Google Sheet (SPEC §5.3).

        One ``spreadsheets.get``; no cell values. Ingest discovers *tabs*,
        staging reads *rows* — keeping the two apart is what lets the stage cron
        skip an untouched workbook without ever having paid for its metadata.
        """
        metadata = sheets_reader.get_metadata(node.google_id)
        return self._sync_tabs(node, metadata.get('tabs') or [], run=run,
                               workbook_timezone=metadata.get('time_zone'))

    @api.model
    def _sync_tabs_from_xlsx(self, node, data, run=None):
        """Enumerate the worksheets of an uploaded ``.xlsx`` (SPEC §4.7)."""
        reader = XlsxReader(logger=_logger)
        try:
            tabs = reader.read(data)
        except GDrivePermanentError as exc:
            # Legacy .xls, encrypted, corrupt, or beyond the extent limits. The
            # node handler turns this into a skip; re-raise rather than return an
            # empty tab list, which would present as "every tab was deleted".
            _logger.warning("Cannot parse %r as xlsx: %s", node.name, exc)
            raise
        return self._sync_tabs(node, tabs, run=run)

    @api.model
    def _sync_tabs(self, node, tabs, run=None, workbook_timezone=None):
        """Upsert one dataset per tab; block the ones that disappeared.

        Dispatch on ``source_kind`` because the two identity models are
        genuinely different, not merely differently spelled: a native gid is
        stable across renames, whereas an xlsx worksheet has to be re-identified
        by title and then by position on every single read.

        Never unlinks. A tab that is gone becomes ``state='blocked'`` with
        ``block_reason='tab_missing'`` and keeps every staged row it ever had.
        """
        if not tabs:
            _logger.info("No tabs reported for %r (%s); leaving existing datasets untouched.",
                         node.name, node.google_id)
            return self.browse()
        source_kind = 'xlsx' if (tabs[0].get('source_kind') == 'xlsx') else 'gsheet'
        if source_kind == 'xlsx':
            return self._sync_tabs_by_position(node, tabs, run)
        return self._sync_tabs_by_gid(node, tabs, run, workbook_timezone)

    @api.model
    def _sync_tabs_by_gid(self, node, tabs, run=None, workbook_timezone=None):
        """Native Sheets: identity is the numeric ``sheetId``, full stop.

        ``sheetId`` survives a rename, so a title change here is INFO-level
        bookkeeping and never a structural event. The first tab of every
        workbook has gid ``0``, which is falsy — every comparison below is
        therefore against ``None``, never against truthiness.
        """
        existing = self.sudo().with_context(active_test=False).search(
            [('node_id', '=', node.id), ('source_kind', '=', 'gsheet')])
        by_gid = {d.sheet_gid: d for d in existing}
        seen_gids = set()
        touched = self.browse()

        for tab in tabs:
            gid = int(tab.get('sheet_gid') or 0)
            seen_gids.add(gid)
            dataset = by_gid.get(gid)
            vals = {
                'tab_title': tab.get('tab_title') or '(untitled)',
                'tab_index': int(tab.get('tab_index') or 0),
                'hidden': bool(tab.get('hidden')),
                'sheet_type': tab.get('sheet_type') or SHEET_TYPE_GRID,
            }
            if dataset is None:
                dataset = self.sudo().create(dict(
                    vals,
                    node_id=node.id,
                    source_kind='gsheet',
                    sheet_gid=gid,
                    sheet_timezone=workbook_timezone or DEFAULT_SHEET_TIMEZONE,
                    active=True,
                ))
                if run:
                    run._log('TAB_DISCOVERED',
                             'New tab %r (gid %d) in %r.' % (vals['tab_title'], gid, node.name),
                             level='info', stage='ingest', dataset=dataset, node=node)
            else:
                if dataset.tab_title != vals['tab_title'] and run:
                    run._log('TAB_RENAMED',
                             'Tab gid %d in %r was renamed from %r to %r. The gid is the '
                             'identity, so this is a rename and not a deletion.'
                             % (gid, node.name, dataset.tab_title, vals['tab_title']),
                             level='info', stage='ingest', dataset=dataset, node=node)
                changed = {k: v for k, v in vals.items() if dataset[k] != v}
                if not dataset.active:
                    changed['active'] = True
                if dataset.block_reason == 'tab_missing':
                    # It came back: clear the hard stop so the next stage pass
                    # re-reads it instead of leaving it blocked forever.
                    changed.update({'state': 'new', 'block_reason': False, 'block_detail': False})
                if changed:
                    dataset.sudo().write(changed)
            touched |= dataset

        vanished = existing.filtered(lambda d: d.sheet_gid not in seen_gids)
        for dataset in vanished:
            dataset._block(
                'tab_missing',
                'Tab gid %d (%r) is no longer present in %r. A gid never changes, so this '
                'is an absence — which is exactly what a truncated read also looks like. '
                'Nothing is deleted and nothing is staged.'
                % (dataset.sheet_gid, dataset.tab_title, node.name),
                run=run, stage='ingest')
        return touched

    @api.model
    def _sync_tabs_by_position(self, node, tabs, run=None):
        """``.xlsx``: re-identify each worksheet by title, then by position.

        An xlsx worksheet carries no stable identifier, so a rename and a
        delete-then-create are literally the same bytes at the same index
        (SPEC §4.7). ``XlsxReader.match_tab`` implements the documented
        resolution order and reports which strategy won; when neither resolves,
        the dataset is blocked rather than bound to a guess, because binding one
        tab's rows to another tab's dataset is unrecoverable.
        """
        existing = self.sudo().with_context(active_test=False).search(
            [('node_id', '=', node.id), ('source_kind', '=', 'xlsx')])
        claimed_indices = set()
        touched = self.browse()

        for dataset in existing:
            match = XlsxReader.match_tab(tabs, dataset.tab_title, dataset.tab_index)
            tab = match.get('tab')
            if tab is None or int(tab.get('tab_index') or 0) in claimed_indices:
                dataset._block(
                    'tab_missing', match.get('message') or
                    'Worksheet %r could not be re-identified in %r.'
                    % (dataset.tab_title, node.name),
                    run=run, stage='ingest')
                continue
            index = int(tab.get('tab_index') or 0)
            claimed_indices.add(index)
            if match.get('strategy') != 'title' and run:
                run._log('XLSX_TAB_REMATCHED', match.get('message') or '',
                         level='info', stage='ingest', dataset=dataset, node=node)
            vals = {
                'tab_title': tab.get('tab_title') or dataset.tab_title,
                'tab_index': index,
                'sheet_gid': gid_for_index(index),
                'hidden': bool(tab.get('hidden')),
                'sheet_type': tab.get('sheet_type') or SHEET_TYPE_GRID,
            }
            changed = {k: v for k, v in vals.items() if dataset[k] != v}
            if not dataset.active:
                changed['active'] = True
            if dataset.block_reason == 'tab_missing':
                changed.update({'state': 'new', 'block_reason': False, 'block_detail': False})
            if changed:
                dataset.sudo().write(changed)
            touched |= dataset

        for tab in tabs:
            index = int(tab.get('tab_index') or 0)
            if index in claimed_indices:
                continue
            dataset = self.sudo().create({
                'node_id': node.id,
                'source_kind': 'xlsx',
                'sheet_gid': gid_for_index(index),
                'tab_title': tab.get('tab_title') or '(untitled)',
                'tab_index': index,
                'hidden': bool(tab.get('hidden')),
                'sheet_type': tab.get('sheet_type') or SHEET_TYPE_GRID,
                'active': True,
            })
            claimed_indices.add(index)
            if run:
                run._log('TAB_DISCOVERED',
                         'New worksheet %r (position %d) in %r.'
                         % (dataset.tab_title, index, node.name),
                         level='info', stage='ingest', dataset=dataset, node=node)
            touched |= dataset
        return touched

    # ------------------------------------------------------------------
    # Blocking
    # ------------------------------------------------------------------
    def _block(self, reason, detail, run=None, stage='stage'):
        """Hard-stop this dataset, log it, and emit a ``blocking`` drift.

        A blocked dataset stages zero rows and promotes nothing. It keeps every
        staged row it already had: the block means "I cannot vouch for what this
        tab says right now", which is emphatically not "the tab is empty".
        """
        self.ensure_one()
        self.sudo().write({
            'state': 'blocked',
            'block_reason': reason,
            'block_detail': detail,
            # An unverifiable tab must never license a delete. This is the flag
            # SPEC §9.6 condition 3 reads, and clearing it here is what disarms
            # the delete planner for the whole dataset.
            'last_read_complete': False,
        })
        if run:
            run._log('DATASET_BLOCKED', '%s: %s' % (self.display_name, detail),
                     level='error', stage=stage, dataset=self, node=self.node_id)
            run._mark_incomplete(
                'DATASET_BLOCKED',
                'Dataset %s is hard-stopped (%s); this run cannot be treated as a '
                'complete read of it.' % (self.display_name, reason),
                stage=stage, dataset=self, node=self.node_id)
        else:
            _logger.error("Dataset %s blocked (%s): %s", self.id, reason, detail)
        self._emit_structural_drift(reason, detail)
        return True

    def _emit_structural_drift(self, reason, detail):
        """Record the hard stop as a ``structural`` / ``blocking`` finding.

        Best-effort by design: the block itself is already persisted and is the
        thing that protects the data. A failure to write the *report* of the
        block is logged with its traceback — never swallowed — but must not roll
        back the protection.
        """
        self.ensure_one()
        if 'gdrive.drift' not in self.env.registry:
            _logger.error("gdrive.drift is not in the registry; dataset %s blocked (%s) "
                          "with no drift record.", self.id, reason)
            return False
        try:
            self.env['gdrive.drift'].sudo().create({
                'dataset_id': self.id,
                'mapping_id': self.mapping_id.id or False,
                'category': 'structural',
                'drift_type': BLOCK_DRIFT_TYPE.get(reason, 'header_change'),
                'severity': 'blocking',
                'message': detail,
                'source_ref': self.used_range or self.tab_title or '',
            })
        except Exception:  # noqa: BLE001 - reporting must not undo the protection
            _logger.exception(
                "Could not record a structural drift for blocked dataset %s (%s). The "
                "block itself is persisted; only its report is missing.", self.id, reason)
            return False
        return True

    # ------------------------------------------------------------------
    # Fast path
    # ------------------------------------------------------------------
    def _fast_path_clean(self, spec_version) -> bool:
        """True when re-reading this tab provably cannot change anything.

        The **L0 Drive** short-circuit of SPEC §9.1, plus the cache-key check
        that makes it safe. Every one of these conditions is load-bearing:

        * ``drive_version`` **and** ``drive_modified_time`` must both match.
          ``version`` bumps on metadata-only edits, so it errs toward "changed"
          — the correct direction for a cache key, since a needless re-read
          costs bandwidth while a missed change costs a wrong answer.
        * ``md5Checksum`` is deliberately absent from this test: it is blob-only
          and simply does not exist on native Sheets, so a checksum-based
          comparison would mark every Google Sheet permanently unchanged.
        * ``spec_version`` must match. A normalizer or contract change makes
          every stored hash meaningless, and treating one as a cache hit is a
          silent false pass.
        * ``h_dataset_sheet`` must exist and ``last_read_complete`` must be
          True. There is no such thing as a cache hit against a read that never
          finished.
        """
        self.ensure_one()
        node = self.node_id
        if node.trashed or not node.active:
            return False
        if self.state == 'blocked':
            # A blocked tab is re-read on every pass on purpose: the fix (a
            # corrected mapping, a restored column) happens outside Drive and
            # would therefore never move drive_version.
            return False
        if not self.last_read_complete or not self.h_dataset_sheet or not self.bucket_hashes:
            return False
        if (self.spec_version or '') != (spec_version or ''):
            return False
        if (self.last_drive_version or '') != (node.drive_version or ''):
            return False
        if self.last_drive_modified != node.drive_modified_time:
            return False
        return True

    def _clear_cached_hashes(self):
        """Drop every cached digest so the next pass recomputes from scratch.

        Called by the weekly full recompute and whenever a mapping's
        ``spec_version`` moves. The fast paths are an optimisation built on
        assumptions; this is the lever that exists for the day one of those
        assumptions turns out to be false.
        """
        self.sudo().write({
            'h_dataset_sheet': False,
            'h_dataset_odoo': False,
            'bucket_hashes': False,
            'last_drive_version': False,
            'last_drive_modified': False,
            'last_odoo_count': 0,
            'last_odoo_max_write_date': False,
        })
        _logger.info("Cleared cached hashes on %d dataset(s).", len(self))
        return True

    # ------------------------------------------------------------------
    # Staging
    # ------------------------------------------------------------------
    @api.model
    def _cron_stage(self):
        """Cron entry point ``ir_cron_gdrive_stage`` — read tabs, land rows.

        Batch driver with a wall-clock budget (SPEC §6): per-connection advisory
        lock, bounded slice, commit at :data:`COMMIT_BATCH` boundaries, and a
        ``_trigger()`` follow-up when the budget expires with work remaining.

        **Never raises.** Odoo 18 auto-deactivates a scheduled action after
        repeated failures, so one unreadable spreadsheet would silently switch
        off staging for the whole database.
        """
        deadline = time.monotonic() + CRON_BUDGET_SEC
        connections = self.env['gdrive.connection'].sudo().search([
            ('active', '=', True), ('state', '=', 'ok'),
        ])
        backlog = False
        for connection in connections:
            if time.monotonic() > deadline:
                backlog = True
                break
            if not connection._acquire_lock():
                _logger.info("Staging skipped for %s: another run holds the advisory lock.",
                             connection.display_name)
                continue
            try:
                backlog = self._stage_connection(connection, deadline) or backlog
            except Exception as exc:  # noqa: BLE001 - a cron may never raise
                self.env.cr.rollback()
                _logger.exception("Staging failed for connection %s.", connection.display_name)
                connection.sudo().write({'last_error': redact(str(exc))})
                self.env.cr.commit()
        if backlog:
            trigger_cron(self.env, 'gdrive_odoo_sync.ir_cron_gdrive_stage')
        return True

    @api.model
    def _stage_connection(self, connection, deadline):
        """Stage every due tab of one connection. Returns True if work remains."""
        datasets = self.sudo().search([
            ('connection_id', '=', connection.id),
            ('active', '=', True),
            ('sheet_type', '=', SHEET_TYPE_GRID),
            ('node_id.active', '=', True),
            ('node_id.trashed', '=', False),
            ('node_id.state', '=', 'ingested'),
        ], order='node_id, tab_index')
        if not datasets:
            return False

        run = self.env['gdrive.sync.run']._start(
            connection, trigger='cron', mode='delta', stages=['stage'])
        # Commit the run header immediately. Without this, a failure on the very
        # first tab rolls back the transaction that created the run, and the next
        # statement inserts a run *line* whose run_id points at a row that no
        # longer exists — an IntegrityError that escapes the per-tab handler and
        # kills the whole pass, leaving no run and no log for anyone to find.
        self.env.cr.commit()
        run = run.browse(run.id)

        ctx = connection._service_context()
        drive, sheets = build_services(ctx)
        sheets_reader = SheetsReader(sheets, ctx)
        downloader = DriveDownloader(drive, ctx)

        by_node = OrderedDict()
        for dataset in datasets:
            by_node.setdefault(dataset.node_id.id, self.browse())
            by_node[dataset.node_id.id] |= dataset

        processed = 0
        remaining = False
        try:
            for node_id, node_datasets in by_node.items():
                if time.monotonic() > deadline:
                    remaining = True
                    run._log('BUDGET_EXHAUSTED',
                             'Staging budget exhausted with %d file(s) still queued.'
                             % (len(by_node) - processed),
                             level='info', stage='stage')
                    break
                node = self.env['gdrive.node'].browse(node_id)
                try:
                    self._stage_node(node, node_datasets, run, sheets_reader, downloader)
                except Exception as exc:  # noqa: BLE001 - per-entity isolation
                    self.env.cr.rollback()
                    # The rollback invalidated every cached record in this
                    # environment. Re-browse before touching any of them.
                    run = run.browse(run.id)
                    node = node.browse(node_id)
                    _logger.exception("Staging failed for %r.", node.name)
                    run._log('STAGE_FAILED', 'Staging %r failed: %s' % (node.name, redact(str(exc))),
                             level='error', stage='stage', node=node)
                    # A tab we failed to read is a tab whose rows we cannot vouch
                    # for, and complete_read=True is precisely what licenses the
                    # delete quarantine clock to start on rows we never saw.
                    run._mark_incomplete(
                        'STAGE_FAILED',
                        'Staging of %r failed (%s); this run is not a complete read.'
                        % (node.name, type(exc).__name__),
                        stage='stage', node=node)
                processed += 1
                if processed % COMMIT_BATCH == 0:
                    self.env.cr.commit()
                    run = run.browse(run.id)
            self.env.cr.commit()
        finally:
            run = run.browse(run.id)
            run._finish()
            self.env.cr.commit()
        return remaining

    def _stage_node(self, node, datasets, run, sheets_reader, downloader):
        """Read one workbook once and stage each of its due tabs.

        Grouping by workbook is not tidiness: ``values.batchGet`` reads every tab
        of a workbook in a single request, and the Sheets quota (60 reads per
        minute, shared across the whole Workspace user) is only survivable
        because of it. Per-tab reads would spend a whole minute's budget on one
        sixty-tab file.
        """
        due = self.browse()
        skipped = self.browse()
        for dataset in datasets:
            bundle_version = dataset._contract_bundle(
                self.env['gdrive.dataset.column']._live(dataset))['spec_version']
            if dataset._fast_path_clean(bundle_version):
                skipped |= dataset
            else:
                due |= dataset
        if skipped:
            run._log('STAGE_FAST_PATH',
                     '%d tab(s) of %r skipped: Drive version, modified time and '
                     'spec_version are all unchanged since the last complete read.'
                     % (len(skipped), node.name),
                     level='info', stage='stage', node=node)
        if not due:
            return 0

        tabs_by_gid = self._read_tabs(node, due, run, sheets_reader, downloader)
        staged_tabs = 0
        for dataset in due.exists():
            tab = tabs_by_gid.get(dataset.sheet_gid)
            if tab is None:
                # _read_tabs already blocked it (tab_missing / ambiguous) or the
                # reader declined to read it; either way there is nothing here to
                # stage and "no values" must never be staged as "no rows".
                continue
            dataset._stage_tab(tab, run)
            staged_tabs += 1
        run._bump(datasets_seen=staged_tabs)
        return staged_tabs

    def _read_tabs(self, node, datasets, run, sheets_reader, downloader):
        """Fetch values for ``node`` and return ``{sheet_gid: tab_dict}``.

        Re-runs tab discovery first, because the workbook may have been renamed,
        re-ordered or had a tab deleted since ingest. Discovering here as well is
        one extra cheap metadata call and it is what keeps a renamed tab from
        being read as a missing one.
        """
        if node.node_type == 'spreadsheet':
            workbook = sheets_reader.read_workbook(node.google_id, grid_only=True,
                                                   include_hidden=True)
            tabs = workbook.get('tabs') or []
            self._sync_tabs(node, tabs, run=run,
                            workbook_timezone=workbook.get('time_zone'))
            run._bump(sheets_reads_used=getattr(sheets_reader, 'reads_used', 0) and 0 or 0)
            return {int(t.get('sheet_gid') or 0): t for t in tabs}

        data = self._xlsx_bytes(node, downloader)
        reader = XlsxReader(logger=_logger)
        tabs = reader.read(data)
        self._sync_tabs(node, tabs, run=run)
        return {gid_for_index(int(t.get('tab_index') or 0)): t for t in tabs}

    def _xlsx_bytes(self, node, downloader):
        """Return the workbook bytes for an uploaded spreadsheet.

        Prefers the mirrored attachment written at ingest — the bytes are
        already local and re-downloading them would spend Drive quota to learn
        nothing. Falls back to a fresh download for a node whose
        ``ingest_policy`` is ``dataset`` and therefore has no attachment at all.
        """
        attachment = node.attachment_id.sudo()
        if attachment and attachment.exists() and attachment.raw:
            return bytes(attachment.raw)
        _logger.info("Node %s has no mirrored attachment; downloading it to stage its tabs.",
                     node.google_id)
        data, _mime = downloader.fetch(node.google_id, node.mime_type)
        return bytes(data)

    def _stage_tab(self, tab, run):
        """Canonicalize, hash and persist one tab's rows (SPEC §5.4).

        The step order is the specification's, and each step guards the next:
        headers are reconciled before the gate can be evaluated, the gate runs
        before a single row is canonicalized, and the rollup is computed from
        exactly the entries that were persisted.
        """
        self.ensure_one()
        started = time.monotonic()
        run._add_stage('stage')
        node = self.node_id

        if (tab.get('sheet_type') or SHEET_TYPE_GRID) != SHEET_TYPE_GRID:
            # Not blocked: a chart tab holding no cells is a normal, correct
            # state of the world, not a failure.
            _logger.info("Dataset %s is a %s tab; nothing to stage.", self.id, tab.get('sheet_type'))
            return False
        if not tab.get('read_complete', True):
            warnings = '; '.join(w.get('message', '') for w in (tab.get('warnings') or []))
            self.sudo().write({'last_read_complete': False})
            run._mark_incomplete(
                'TAB_READ_INCOMPLETE',
                'Tab %r was not fully read (%s); its rows are left exactly as they were.'
                % (self.tab_title, warnings or 'no detail reported'),
                stage='stage', dataset=self, node=node)
            return False

        rows = tab.get('rows') or []
        header_index = max((self.header_row or 1) - 1, 0)
        header_cells = list(rows[header_index]) if header_index < len(rows) else []
        data_rows = rows[max((self.first_data_row or 2) - 1, 0):]

        columns = self.env['gdrive.dataset.column']._upsert_headers(self, header_cells, data_rows)
        live_canons = [c.header_canon for c in columns]
        fingerprint = h_header_hex(live_canons)

        # --- step 3: the header gate ---------------------------------------
        if self._mapping_is_usable():
            required = {c.header_canon for c in self.mapping_id.column_ids
                        if c.ctype != 'ignore' and c.header_canon}
            missing = sorted(required.difference(live_canons))
            if missing:
                self.sudo().write({'header_fingerprint': fingerprint})
                self._block(
                    'mapped_column_missing',
                    'Mapped column(s) %s are absent from tab %r. Zero rows were staged: '
                    'reading an absent mapped column as empty cells would write NULL over '
                    'the whole corresponding Odoo column.'
                    % (', '.join(repr(m) for m in missing), self.tab_title),
                    run=run, stage='stage')
                return False
            grown = sorted(set(live_canons).difference(required))
            if grown and self.header_fingerprint and self.header_fingerprint != fingerprint:
                # Non-blocking by design: an unmapped new column changes nothing
                # about what is promoted, and it is already accounted for in
                # h_extra so the growth is visible without polluting h_row.
                run._log('SCHEMA_GROWTH',
                         'Tab %r gained unmapped column(s) %s. Syncing continues; they are '
                         'recorded in h_extra.'
                         % (self.tab_title, ', '.join(repr(g) for g in grown)),
                         level='info', stage='stage', dataset=self, node=node)

        # --- step 9: the EMPTY_TAB guard ------------------------------------
        if not data_rows and self.row_count > 0 and self.last_read_complete:
            self.sudo().write({'header_fingerprint': fingerprint})
            self._block(
                'empty_tab',
                'Tab %r read as zero data rows, but the previous complete read saw %d. '
                'That is a mass-delete signal, never "every row was deleted": a truncated '
                'read, a revoked permission and an emptied tab are indistinguishable here.'
                % (self.tab_title, self.row_count),
                run=run, stage='stage')
            return False

        # --- steps 4-7: canonicalize, hash, identify, persist ---------------
        contracts = self._contract_bundle(columns)
        result = self.env['gdrive.staged.row']._stage_dataset_rows(
            self, columns, data_rows, contracts, run,
            tab_read_complete=bool(tab.get('read_complete', True)))

        # --- step 8: the rollup and the fast-path inputs --------------------
        entries = []
        for bucket_entries in (result.get('buckets') or {}).values():
            entries.extend(bucket_entries)
        digest, bucket_hex = dataset_digest(entries, contracts['spec_version'], self._tab_uid())

        self.sudo().write({
            'header_fingerprint': fingerprint,
            'used_range': tab.get('used_range') or self.used_range,
            'row_count': result.get('row_count') or 0,
            'spec_version': contracts['spec_version'],
            'h_dataset_sheet': digest,
            'bucket_hashes': bucket_hex,
            'last_drive_version': node.drive_version or False,
            'last_drive_modified': node.drive_modified_time or False,
            'last_stage_date': fields.Datetime.now(),
            'last_read_complete': True,
            'state': 'mapped' if self._mapping_is_usable() else 'staged',
            'block_reason': False,
            'block_detail': False,
        })
        run._log('TAB_STAGED',
                 'Staged %d row(s) from %r (%d quarantined).'
                 % (result.get('row_count') or 0, self.tab_title, result.get('quarantined') or 0),
                 level='info', stage='stage', dataset=self, node=node,
                 duration_ms=int((time.monotonic() - started) * 1000))
        return True

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------
    @api.model
    def _cron_verify(self):
        """Cron entry point ``ir_cron_gdrive_verify``.

        The cron is declared against ``gdrive.dataset`` because a verification is
        always *of a dataset*, but the L0/L0b/L1/L2/L3 orchestration itself lives
        on ``gdrive.verification``. This is the dispatcher that keeps the XML and
        the implementation from having to agree on a model name.

        Never raises, for the same reason as every other cron here.
        """
        if 'gdrive.verification' not in self.env.registry:
            _logger.error("gdrive.verification is not in the registry; verification skipped.")
            return True
        try:
            return self.env['gdrive.verification']._cron_verify()
        except Exception:  # noqa: BLE001 - a cron may never raise
            self.env.cr.rollback()
            _logger.exception("Verification cron failed; the next scheduled pass will retry.")
            return True

    # ------------------------------------------------------------------
    # UI actions
    # ------------------------------------------------------------------
    def action_stage_now(self):
        """Re-read the selected tabs immediately.

        Safe by construction, and worth saying so on the button: staging writes
        ``gdrive.staged.row`` and nothing else. No business model is touched, no
        promotion runs, and the delete planner is not even consulted.
        """
        for dataset in self:
            connection = dataset.connection_id
            if not connection:
                raise UserError(_('This dataset has no connection to read through.'))
            run = self.env['gdrive.sync.run']._start(
                connection, trigger='manual', mode='full', stages=['stage'])
            try:
                ctx = connection._service_context()
                drive, sheets = build_services(ctx)
                dataset._stage_node(
                    dataset.node_id, dataset, run,
                    SheetsReader(sheets, ctx), DriveDownloader(drive, ctx))
            except Exception as exc:  # noqa: BLE001 - reported to the user, never hidden
                _logger.exception("Manual staging of dataset %s failed.", dataset.id)
                run._log('STAGE_FAILED', 'Manual staging failed: %s' % redact(str(exc)),
                         level='error', stage='stage', dataset=dataset)
                run._finish()
                raise UserError(_('Staging %(tab)s failed: %(error)s',
                                  tab=dataset.tab_title, error=redact(str(exc)))) from exc
            run._finish()
        return True

    def action_verify_now(self):
        """Recompute both content hashes and drill down where they disagree."""
        self.ensure_one()
        if not self.mapping_id:
            raise UserError(_('Verification compares a tab against Odoo records, which '
                              'needs a mapping. Build one first.'))
        if 'gdrive.verification' not in self.env.registry:
            raise UserError(_('The verification engine is not installed in this database.'))
        return self.env['gdrive.verification']._verify_dataset(self)

    def action_build_mapping(self):
        """Open the mapping builder on this dataset.

        The wizard only ever *suggests*: it creates a ``gdrive.mapping`` in state
        ``draft`` with ``enabled = False``, so opening it cannot promote
        anything.
        """
        self.ensure_one()
        if not self.column_ids:
            raise UserError(_('This tab has no observed columns yet. Stage it once first, '
                              'so the builder has a real header schema to work from.'))
        action = self.env['ir.actions.act_window']._for_xml_id(
            'gdrive_odoo_sync.action_gdrive_mapping_builder_wizard')
        action['context'] = {'default_dataset_id': self.id}
        return action

    def action_open_in_drive(self):
        """Open the underlying Drive file in a new tab."""
        self.ensure_one()
        link = self.node_id.web_view_link
        if not link:
            raise UserError(_('No Drive link has been recorded for this file yet.'))
        return {'type': 'ir.actions.act_url', 'url': link, 'target': 'new'}
