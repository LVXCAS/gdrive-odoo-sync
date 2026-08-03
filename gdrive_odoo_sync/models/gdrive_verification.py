# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. See LICENSE (LGPL-3) for full copyright and licensing details.
"""``gdrive.verification`` — the layered comparison of SPEC §9.1–§9.5.

WHY a persisted record per pass, and why it stores its inputs
-------------------------------------------------------------
"Verified" is a claim, and a claim is only worth anything if it can be
re-examined later. Every verification therefore records *how deep it looked*
(``mode``), *what it compared* (``h_dataset_sheet`` / ``h_dataset_odoo``), and
*two independent controls* that do not share code with the hasher
(``rows_sheet`` / ``rows_odoo`` and ``column_totals``). A pass whose evidence is
not stored is indistinguishable from a pass that never ran.

The layering, restated because it is the whole design (SPEC §9.1):

* **L0**   Drive fingerprint unchanged → the file did not move.
* **L0b**  Odoo ``(count, max(write_date))`` unchanged → this side did not move
  either. **Both** halves are required: a delete does not advance
  ``max(write_date)`` and an in-place edit does not change the count, so either
  check alone has a blind spot big enough to drive a mass-delete through.
  L0 **and** L0b clean ⇒ ``mode='cache'`` at zero API cost.
* **L1**   One dataset hash per side. Equal ⇒ verified, no row work.
* **L2**   256 bucket hashes localize the mismatch, typically to one or two.
* **L3**   Row and field comparison inside the differing buckets only.

**Cache invalidation is not optional.** Every stored hash is keyed by
``spec_version``. When the mapping's ``spec_version`` no longer matches the one
the dataset's hashes were computed under, the fast paths are refused outright:
serving a hash computed by an older normalizer as ``verified`` is a silent false
pass, which is the single worst failure mode a verification system has.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from decimal import Decimal
from typing import Any, Iterable

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import html_escape

from ..lib import (
    CANON_VERSION,
    bucket_of,
    diff_buckets,
    h_row,
    identity_key_bytes,
    raw_decimal,
)
# `dataset_digest` is the one-call rollup (digest + the 256 bucket hex strings).
# It is imported from the submodule rather than the package facade because the
# facade deliberately re-exports only the primitives; taking the count from the
# entry list itself is what guarantees the row count committed to in the hash
# can never disagree with the rows actually hashed.
from ..lib.merkle import dataset_digest
from .gdrive_sync_run import COMMIT_BATCH, CRON_BUDGET_SEC, trigger_cron

_logger = logging.getLogger(__name__)

#: Staged-row states that represent "this row is in the tab right now".
#: ``missing`` and ``obsolete`` rows are precisely the ones that are *not*, and
#: including them would make the sheet-side hash describe a tab that no longer
#: exists. ``quarantined`` rows *are* in the tab — unreadable, but present — so
#: excluding them would make an unparseable cell look like a deleted row, which
#: is exactly the confusion the disjoint drift categories exist to prevent.
PRESENT_ROW_STATES = ('staged', 'promoted', 'quarantined')

#: ``gdrive.dataset.block_reason`` → the structural drift it corresponds to.
#: ``spec_mismatch`` is absent on purpose: it is a cache-invalidation condition,
#: not a finding about the data, and it is handled by refusing the fast paths.
BLOCK_REASON_DRIFT = {
    'mapped_column_missing': 'header_change',
    'header_changed': 'header_change',
    'tab_missing': 'tab_missing',
    'empty_tab': 'empty_tab',
    'access_lost': 'access_lost',
    'file_trashed': 'access_lost',
    'duplicate_identity': 'duplicate_identity',
}

#: Contract types whose raw values are summed into ``column_totals``.
NUMERIC_CTYPES = ('number', 'money')


class GdriveVerification(models.Model):
    """One verification pass over one dataset."""

    _name = 'gdrive.verification'
    _description = 'Google Drive Sync Verification'
    _order = 'date desc, id desc'

    name = fields.Char(string='Reference', compute='_compute_name', store=True)
    run_id = fields.Many2one(
        'gdrive.sync.run', string='Run', index=True, ondelete='cascade',
    )
    dataset_id = fields.Many2one(
        'gdrive.dataset', string='Dataset', required=True, index=True, ondelete='cascade',
    )
    mapping_id = fields.Many2one(
        'gdrive.mapping', string='Mapping', index=True, ondelete='set null',
        help='Empty when the dataset is staging-only. A staging-only dataset is '
             'still verified — against its own mirror — because a partial read '
             'must be visible even when nothing is promoted.',
    )
    date = fields.Datetime(string='Date', default=fields.Datetime.now, required=True, index=True)

    mode = fields.Selection(
        [
            ('cache', 'Cache (no read)'),
            ('dataset', 'Dataset hash'),
            ('bucket', 'Bucket'),
            ('full', 'Row / field'),
        ],
        string='Depth Reached', default='cache', required=True, index=True,
        help='The deepest layer the comparison actually reached. A dataset '
             'stuck on `cache` forever is the signature of a fingerprint that '
             'stopped advancing, not of a dataset that never changes.',
    )
    result = fields.Selection(
        [
            ('verified', 'Verified'),
            ('drift', 'Drift'),
            ('blocked', 'Blocked'),
            ('error', 'Error'),
        ],
        string='Result', default='verified', required=True, index=True,
    )

    h_dataset_sheet = fields.Char(string='Sheet Hash', size=64)
    h_dataset_odoo = fields.Char(string='Odoo Hash', size=64)
    spec_version = fields.Char(
        string='Spec Version', size=64,
        help='H(contract ‖ normalizer version) under which these hashes were '
             'computed. A hash carried forward from a different spec_version is '
             'not a comparison, it is a coincidence.',
    )

    rows_sheet = fields.Integer(string='Rows (Sheet)', aggregator='sum')
    rows_odoo = fields.Integer(string='Rows (Odoo)', aggregator='sum')
    buckets_differing = fields.Integer(string='Buckets Differing', aggregator='sum')
    rows_examined = fields.Integer(string='Rows Examined', aggregator='sum')

    drift_count = fields.Integer(
        string='Drift', aggregator='sum',
        help='Real differences only. Data-quality items are excluded by '
             'construction, so "12 drifts" can never silently mean "12 cells I '
             'could not read".',
    )
    data_quality_count = fields.Integer(string='Data Quality', aggregator='sum')
    structural_count = fields.Integer(string='Structural', aggregator='sum')

    column_totals = fields.Json(
        string='Column Totals',
        help='Per numeric column, a Decimal sum computed from RAW values on '
             'both sides. Shares no code with the normalizer, which is why it '
             'is the only control that can catch a symmetric canonicalization '
             'bug: when both sides are normalized wrongly in the same way the '
             'hashes agree and the canonical totals agree — only these do not.',
    )
    read_complete = fields.Boolean(
        string='Read Complete', default=False,
        help='Copied from the dataset read. While False, absence of a row is '
             'not evidence of deletion and every delete guard stays engaged.',
    )
    duration_sec = fields.Float(string='Duration (s)', aggregator='sum')
    error_detail = fields.Text(string='Error Detail', readonly=True)

    drift_ids = fields.One2many('gdrive.drift', 'verification_id', string='Findings')
    plan_id = fields.Many2one('gdrive.plan', string='Plan', ondelete='set null')
    report_attachment_id = fields.Many2one(
        'ir.attachment', string='Report (HTML)', ondelete='set null', copy=False,
    )
    report_json_attachment_id = fields.Many2one(
        'ir.attachment', string='Report (JSON)', ondelete='set null', copy=False,
        help='The machine artefact: fingerprints, hashes, counts, and every '
             'finding with both canonical forms and its A1 reference.',
    )

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------
    @api.depends('dataset_id', 'dataset_id.tab_title', 'date')
    def _compute_name(self) -> None:
        """``"<tab> @ <date>"`` (SPEC §3.11)."""
        for verification in self:
            tab = verification.dataset_id.tab_title or verification.dataset_id.display_name or _('Dataset')
            when = fields.Datetime.to_string(verification.date) if verification.date else ''
            verification.name = ('%s @ %s' % (tab, when)).strip()

    # ------------------------------------------------------------------
    # Cron driver
    # ------------------------------------------------------------------
    @api.model
    def _cron_verify(self, limit: int | None = None) -> int:
        """Verify datasets that are due, oldest first, within a wall-clock budget.

        Contract (SPEC §6), and the reason each clause exists:

        * **Never raises.** Odoo 18 auto-deactivates a scheduled action after
          repeated failures, so one unreadable spreadsheet would otherwise
          switch off verification for the whole database — silently, and
          precisely when it is most needed.
        * **Budgeted.** Work left when :data:`CRON_BUDGET_SEC` expires
          re-triggers the cron rather than waiting a whole day.
        * **Committed in batches.** A worker killed mid-run keeps the
          verifications it already finished.

        Returns the number of datasets verified, so a caller (the dataset cron,
        which owns the ``ir.cron`` record) can log a meaningful count.
        """
        started = time.monotonic()
        datasets = self._datasets_due(limit=limit)
        if not datasets:
            _logger.info("Verification cron: nothing due.")
            return 0

        done = 0
        for dataset in datasets:
            if time.monotonic() - started > CRON_BUDGET_SEC:
                _logger.info(
                    "Verification cron hit its %ss budget after %d dataset(s); re-triggering.",
                    CRON_BUDGET_SEC, done,
                )
                trigger_cron(self.env, 'gdrive_odoo_sync.ir_cron_gdrive_verify')
                break
            try:
                self.verify_dataset(dataset)
            except Exception:
                # Logged with the traceback, recorded on the dataset by
                # verify_dataset's own handler, and then swallowed *here only* so
                # the remaining datasets still get verified.
                _logger.exception(
                    "Verification of dataset %s (id=%s) failed; continuing with the rest.",
                    dataset.display_name, dataset.id,
                )
            done += 1
            if done % COMMIT_BATCH == 0 and not self.env.registry.in_test_mode():
                self.env.cr.commit()
        _logger.info("Verification cron verified %d dataset(s) in %.1fs.", done, time.monotonic() - started)
        return done

    @api.model
    def _datasets_due(self, limit: int | None = None):
        """Datasets to verify, never-verified ones first.

        Never-verified datasets are fetched by a separate search rather than by
        ordering on ``last_verify_date``: PostgreSQL sorts NULLs *last* in an
        ascending order, so the naive single query would put every brand-new
        dataset at the back of the queue and verify it only once everything else
        had been done — which on a large corpus means never.
        """
        Dataset = self.env['gdrive.dataset']
        base = [('active', '=', True), ('node_id.trashed', '=', False)]
        cap = limit or 200
        fresh = Dataset.search(base + [('last_verify_date', '=', False)], limit=cap)
        remaining = cap - len(fresh)
        if remaining <= 0:
            return fresh
        stale = Dataset.search(
            base + [('last_verify_date', '!=', False), ('id', 'not in', fresh.ids)],
            order='last_verify_date asc, id asc', limit=remaining,
        )
        return fresh | stale

    # ------------------------------------------------------------------
    # The verification itself
    # ------------------------------------------------------------------
    @api.model
    def verify_dataset(self, dataset, run=None, force_full: bool = False):
        """Run the layered comparison over ``dataset`` and record the result.

        Args:
            dataset: a single ``gdrive.dataset``.
            run: optional ``gdrive.sync.run`` to attribute the pass to.
            force_full: skip L0/L0b. Set by the weekly full recompute, whose
                entire purpose is to re-derive from scratch what the fast paths
                have been asserting all week.

        Returns the created ``gdrive.verification``. Read-only with respect to
        business data: nothing outside this module's own models is written, and
        no plan is executed. Report-first is the product's default posture.
        """
        dataset.ensure_one()
        started = time.monotonic()
        mapping = dataset.mapping_id
        vals: dict[str, Any] = {
            'dataset_id': dataset.id,
            'mapping_id': mapping.id if mapping else False,
            'run_id': run.id if run else False,
            'date': fields.Datetime.now(),
            'read_complete': bool(dataset.last_read_complete),
            'spec_version': self._effective_spec_version(dataset, mapping),
        }
        verification = self.create(vals)
        findings: list[dict[str, Any]] = []

        try:
            findings += self._structural_findings(dataset)
            if any(f['drift_type'] in ('tab_missing', 'empty_tab', 'access_lost', 'header_change')
                   for f in findings):
                # A blocked dataset is not compared. Comparing a tab we could not
                # read produces a confident, well-formatted, wrong answer — and
                # its shape is always "every row was deleted".
                verification.write({'mode': 'cache', 'result': 'blocked'})
                self._finish(verification, findings, started)
                return verification

            if not force_full and self._fast_paths_clean(dataset, mapping):
                verification.write({'mode': 'cache', 'result': 'verified'})
                _logger.info(
                    "Dataset %s verified from cache: neither Drive nor Odoo moved.",
                    dataset.display_name,
                )
                self._finish(verification, findings, started)
                return verification

            sheet_rows = self._sheet_snapshot(dataset)
            odoo_rows = self._odoo_snapshot(mapping)
            spec_version = verification.spec_version
            tab_uid = self._tab_uid(dataset)

            h_sheet, sheet_buckets = dataset_digest(
                self._entries(sheet_rows, spec_version), spec_version, tab_uid)
            h_odoo, odoo_buckets = dataset_digest(
                self._entries(odoo_rows, spec_version), spec_version, tab_uid)

            verification.write({
                'h_dataset_sheet': h_sheet,
                'h_dataset_odoo': h_odoo,
                'rows_sheet': len(sheet_rows),
                'rows_odoo': len(odoo_rows),
                'mode': 'dataset',
            })

            self._refresh_dataset_cache(dataset, mapping, h_sheet, h_odoo,
                                        sheet_buckets, len(sheet_rows), force_full)

            totals, total_findings = self._column_totals(dataset, mapping, sheet_rows, odoo_rows)
            verification.column_totals = totals
            findings += total_findings

            if h_sheet == h_odoo and not total_findings:
                verification.result = 'verified'
                self._finish(verification, findings, started)
                return verification

            differing = diff_buckets(sheet_buckets, odoo_buckets)
            verification.write({'mode': 'bucket', 'buckets_differing': len(differing)})

            examined_sheet = [r for r in sheet_rows if r['bucket'] in differing]
            examined_odoo = [r for r in odoo_rows if r['bucket'] in differing]
            verification.rows_examined = len(examined_sheet) + len(examined_odoo)

            findings += self._row_findings(
                verification, dataset, mapping, examined_sheet, examined_odoo)
            verification.mode = 'full'
        except Exception as exc:
            # Recorded, never swallowed: a verification that failed must not be
            # readable as "verified", and the reason has to survive the worker.
            _logger.exception("Verification of dataset %s failed.", dataset.display_name)
            verification.write({'result': 'error', 'error_detail': '%s: %s' % (type(exc).__name__, exc)})
            self._finish(verification, findings, started)
            raise

        self._finish(verification, findings, started)
        return verification

    # ------------------------------------------------------------------
    # Layer helpers
    # ------------------------------------------------------------------
    @api.model
    def _effective_spec_version(self, dataset, mapping) -> str:
        """The spec version these hashes are keyed by."""
        return (mapping.spec_version if mapping else '') or dataset.spec_version or CANON_VERSION

    @api.model
    def _tab_uid(self, dataset) -> str:
        """``"<google file id>/<sheet gid>"`` — see ``lib.merkle.h_dataset``.

        Binding the digest to the file *id* rather than the title is what stops
        two workbooks that happen to share a name from ever comparing equal.
        """
        return '%s/%d' % (dataset.node_id.google_id or 'unknown', dataset.sheet_gid or 0)

    @api.model
    def _fast_paths_clean(self, dataset, mapping) -> bool:
        """L0 and L0b: True only when *both* sides provably did not move."""
        node = dataset.node_id
        if node.trashed:
            return False
        if not dataset.h_dataset_sheet or not dataset.bucket_hashes:
            return False  # nothing cached to serve

        # Cache invalidation (SPEC §9.1). Refusing here costs one full pass;
        # not refusing serves a hash computed by a different normalizer as
        # proof of equality, which is a false pass.
        if dataset.spec_version != self._effective_spec_version(dataset, mapping):
            _logger.info(
                "Dataset %s: spec_version changed, cached hashes are void; forcing a full pass.",
                dataset.display_name,
            )
            return False

        l0_clean = bool(
            node.drive_version
            and node.drive_version == dataset.last_drive_version
            and node.drive_modified_time == dataset.last_drive_modified
        )
        if not l0_clean:
            return False
        if not mapping:
            # Staging-only: there is no Odoo side to have moved.
            return True

        count, max_write_date = self._odoo_fingerprint(mapping)
        # Both halves are required. A delete does not advance max(write_date)
        # and an in-place edit does not change the count.
        return count == dataset.last_odoo_count and max_write_date == dataset.last_odoo_max_write_date

    @api.model
    def _odoo_fingerprint(self, mapping) -> tuple[int, Any]:
        """``(search_count, max(write_date))`` over the mapping's domain."""
        if not mapping or not mapping.target_model:
            return 0, False
        Target = self.env[mapping.target_model].sudo()
        domain = self._mapping_domain(mapping)
        count = Target.search_count(domain)
        newest = Target.search(domain, order='write_date desc', limit=1)
        return count, newest.write_date if newest else False

    @api.model
    def _mapping_domain(self, mapping) -> list:
        """The mapping's own domain, scoped to records this dataset owns.

        Ownership is the load-bearing half (SPEC §3.10): anything inside the
        domain that does not carry this dataset's ``x_gdrive_source_dataset`` is
        ``unmanaged`` — reported, never touched, and deliberately not part of
        the Odoo-side fingerprint, so a human adding an unrelated record does
        not invalidate the fast path.
        """
        domain = []
        if mapping.domain:
            try:
                domain = list(literal_eval_domain(mapping.domain))
            except Exception:
                _logger.exception(
                    "Mapping %s has an unparseable domain %r; falling back to the owned-records scope only.",
                    mapping.display_name, mapping.domain,
                )
                domain = []
        return domain + [('x_gdrive_source_dataset', '=', mapping.dataset_id.id)]

    @api.model
    def _sheet_snapshot(self, dataset) -> list[dict[str, Any]]:
        """Materialize the sheet side from the staged mirror. No API cost."""
        rows = self.env['gdrive.staged.row'].search([
            ('dataset_id', '=', dataset.id),
            ('state', 'in', list(PRESENT_ROW_STATES)),
        ])
        snapshot = []
        for row in rows:
            snapshot.append({
                'staged_row_id': row.id,
                'sync_id': row.sync_id or '',
                'natural_key': row.natural_key or '',
                'identity_source': row.identity_source,
                'row_number': row.row_number,
                'source_ref': row.a1_ref or '',
                'canon': dict(row.canon or {}),
                'payload': dict(row.payload or {}),
                'h_row': row.h_row or '',
                'h_row_folded': row.h_row_folded or '',
                'h_extra': row.h_extra or '',
                'bucket': row.bucket,
                'state': row.state,
                'quarantine_reason': row.quarantine_reason,
            })
        return snapshot

    @api.model
    def _odoo_snapshot(self, mapping) -> list[dict[str, Any]]:
        """Read the Odoo side through ``gdrive.promoter.read_odoo_snapshot``.

        Staging-only datasets legitimately have no Odoo side; they return an
        empty snapshot, which makes ``h_dataset_odoo`` the digest of an empty
        dataset rather than a missing value. That is deliberate: "nothing is
        promoted" is a fact worth hashing.
        """
        if not mapping:
            return []
        promoter = self.env['gdrive.promoter']
        if not hasattr(promoter, 'read_odoo_snapshot'):
            raise UserError(_('gdrive.promoter does not expose read_odoo_snapshot(); '
                              'the Odoo side of the comparison cannot be read.'))
        snapshot = []
        for row in promoter.read_odoo_snapshot(mapping) or []:
            entry = dict(row)
            entry.setdefault('sync_id', '')
            entry.setdefault('natural_key', '')
            entry.setdefault('canon', {})
            entry.setdefault('raw', {})
            entry.setdefault('source_ref', '')
            snapshot.append(entry)
        return snapshot

    @api.model
    def _identity_parts(self, row: dict[str, Any]) -> list[str]:
        """Identity key parts for one row of either snapshot.

        ``sync_id`` wins when present — it is exact and 1:1. The natural key is
        the documented fallback and is already the canonical length-prefixed
        join, so it is passed through as a single part. A row with neither is
        keyed by its A1 reference so it still lands in a bucket and shows up as
        an unmatched row rather than silently colliding with every other
        identity-less row in bucket ``''``.
        """
        if row.get('sync_id'):
            return [row['sync_id']]
        if row.get('natural_key'):
            return [row['natural_key']]
        return ['@' + (row.get('source_ref') or str(row.get('row_number') or ''))]

    @api.model
    def _entries(self, rows: Iterable[dict[str, Any]], spec_version: str) -> list[tuple[bytes, bytes]]:
        """``[(identity_key_bytes, h_row)]``, computing ``h_row`` when absent.

        Recomputing rather than trusting a stored hex digest whenever one is
        missing keeps the two sides symmetrical: the Odoo snapshot has no stored
        row hash, and comparing a stored digest against a freshly computed one
        would compare two different functions.
        """
        entries: list[tuple[bytes, bytes]] = []
        for row in rows:
            key = identity_key_bytes(self._identity_parts(row))
            digest_hex = row.get('h_row')
            if digest_hex:
                try:
                    digest = bytes.fromhex(digest_hex)
                except ValueError:
                    _logger.warning("Row %r carries a malformed h_row %r; recomputing.",
                                    row.get('source_ref'), digest_hex)
                    digest = h_row(row.get('canon') or {}, spec_version)
            else:
                digest = h_row(row.get('canon') or {}, spec_version)
            row['bucket'] = bucket_of(key)
            entries.append((key, digest))
        return entries

    # ------------------------------------------------------------------
    # Findings
    # ------------------------------------------------------------------
    @api.model
    def _structural_findings(self, dataset) -> list[dict[str, Any]]:
        """Structural findings derivable without any API call.

        Two sources: a dataset the staging lane already blocked, and unmapped
        columns that carry data (``schema_growth``). Schema growth is ``info``
        and never blocking — an added column is additive information, and a
        system that halts a nightly sync because somebody added a Notes column
        gets switched off within a week.
        """
        out: list[dict[str, Any]] = []
        if dataset.state == 'blocked' and dataset.block_reason in BLOCK_REASON_DRIFT:
            out.append({
                'drift_type': BLOCK_REASON_DRIFT[dataset.block_reason],
                'source_ref': dataset.used_range or dataset.tab_title or '',
                'message': dataset.block_detail or _(
                    'Dataset blocked: %s.') % dataset.block_reason,
            })
        if dataset.mapping_id:
            for column in dataset.column_ids:
                if column.is_mapped or not column.nonempty_count:
                    continue
                out.append({
                    'drift_type': 'schema_growth',
                    'field_name': column.slug,
                    'source_ref': '%s!%s%s' % (dataset.tab_title, column.a1_letter or '?',
                                               dataset.header_row or 1),
                    'canon_sheet': column.header_canon or '',
                    'message': _('Column %s carries data but is not in the mapping contract. '
                                 'It is excluded from the compared hash and reported here so '
                                 'schema growth stays visible.') % (column.header_raw or column.slug),
                })
        return out

    @api.model
    def _row_findings(self, verification, dataset, mapping, sheet_rows, odoo_rows) -> list[dict[str, Any]]:
        """L3: hand the differing buckets to the pure planner and take its findings.

        The planner is the *only* implementation of the identity cascade and the
        drift classification, and dry-run and apply both call it. Duplicating any
        part of it here would mean the preview and the execution could disagree,
        which makes the preview a lie.
        """
        reconciler = self.env['gdrive.reconciler']
        if not hasattr(reconciler, 'plan'):
            raise UserError(_('gdrive.reconciler does not expose plan(); '
                              'row-level comparison is unavailable.'))
        contract = self._build_contract(mapping)
        policy = self._build_policy(dataset, mapping)
        result = reconciler.plan(sheet_rows, odoo_rows, contract, policy, fields.Datetime.now())
        findings = list(result.get('drifts') or [])
        planner_counts = {
            key: result.get(key) for key in ('drift_count', 'data_quality_count', 'structural_count')
            if result.get(key) is not None
        }
        if planner_counts:
            _logger.debug("Planner reported %s for dataset %s.", planner_counts, dataset.display_name)
        return findings

    @api.model
    def _build_contract(self, mapping) -> list[dict[str, Any]]:
        """Serialize the mapping columns for the pure planner.

        Passed as plain dicts: the planner and the canonicalization library are
        forbidden from touching the ORM, and handing them a recordset would make
        every hash depend on registry state.
        """
        if not mapping:
            return []
        contract = []
        for column in mapping.column_ids:
            if not hasattr(column, 'to_contract_dict'):
                raise UserError(_('gdrive.mapping.column does not expose to_contract_dict(); '
                                  'the column contract cannot be serialized.'))
            contract.append(column.to_contract_dict())
        return contract

    @api.model
    def _build_policy(self, dataset, mapping) -> dict[str, Any]:
        """Everything the planner needs to decide what it is *allowed* to plan.

        ``read_complete`` and ``dataset_blocked`` are in here rather than being
        checked afterwards because a delete inferred from an incomplete read
        must never enter a plan in the first place — every read failure has the
        exact signature of "everything was deleted" (SPEC §9.6).
        """
        policy: dict[str, Any] = {
            'read_complete': bool(dataset.last_read_complete),
            'dataset_blocked': dataset.state == 'blocked',
            'rows_odoo_known': dataset.row_count or 0,
            'sheet_timezone': dataset.sheet_timezone or 'UTC',
            'dataset_id': dataset.id,
            'tab_title': dataset.tab_title,
        }
        if not mapping:
            # No mapping means report-only: nothing may be created, updated or
            # deleted, and the planner must be told so explicitly rather than
            # inferring it from an empty contract.
            policy.update({
                'create_allowed': False, 'update_allowed': False,
                'delete_policy': 'never', 'auto_heal': False,
                'identity_strategy': 'natural_key',
            })
            return policy
        policy.update({
            'target_model': mapping.target_model,
            'identity_strategy': mapping.identity_strategy,
            'create_allowed': mapping.create_allowed,
            'update_allowed': mapping.update_allowed,
            'delete_policy': mapping.delete_policy,
            'soft_delete_field': mapping.soft_delete_field,
            'auto_heal': mapping.auto_heal,
            'create_threshold_abs': mapping.create_threshold_abs,
            'create_threshold_pct': mapping.create_threshold_pct,
            'delete_threshold_abs': mapping.delete_threshold_abs,
            'delete_threshold_pct': mapping.delete_threshold_pct,
            'quarantine_runs': mapping.quarantine_runs,
            'quarantine_hours': mapping.quarantine_hours,
            'flap_limit': mapping.flap_limit,
            'default_values': dict(mapping.default_values or {}),
        })
        return policy

    # ------------------------------------------------------------------
    # Independent controls
    # ------------------------------------------------------------------
    @api.model
    def _column_totals(self, dataset, mapping, sheet_rows, odoo_rows) -> tuple[dict, list[dict]]:
        """Per numeric column, a raw-value Decimal sum on each side.

        Deliberately derived from ``payload`` (sheet) and ``raw`` (Odoo) — never
        from the canonical forms. If both sides are canonicalized wrongly in the
        same way, the hashes agree *and* the canonical totals agree; only a sum
        that shares no code with the normalizer disagrees. That makes this the
        one check capable of catching a symmetric normalizer bug, and it is
        worth the extra pass over the rows.

        A disagreement while the hashes agree is reported as a ``critical``
        ``field_mismatch``: it is a real difference that the primary comparison
        provably cannot see.
        """
        if not mapping:
            return {}, []
        columns = [c for c in mapping.column_ids if c.ctype in NUMERIC_CTYPES and c.odoo_field]
        if not columns:
            return {}, []

        totals: dict[str, dict[str, str | None]] = {}
        findings: list[dict[str, Any]] = []
        for column in columns:
            slug = self._slug_for(dataset, column)
            sheet_sum = self._sum_raw(sheet_rows, 'payload', slug)
            odoo_sum = self._sum_raw(odoo_rows, 'raw', column.odoo_field)
            delta = sheet_sum - odoo_sum
            totals[column.odoo_field] = {
                'sheet': str(sheet_sum),
                'odoo': str(odoo_sum),
                'delta': str(delta),
                'header_canon': column.header_canon or '',
            }
            if delta != 0:
                _logger.warning(
                    "Raw column total mismatch on %s.%s: sheet=%s odoo=%s delta=%s.",
                    dataset.display_name, column.odoo_field, sheet_sum, odoo_sum, delta,
                )
                findings.append({
                    'drift_type': 'field_mismatch',
                    'severity': 'critical',
                    'delta_class': 'substantive',
                    'field_name': column.odoo_field,
                    'canon_sheet': 'raw_total:%s' % sheet_sum,
                    'canon_odoo': 'raw_total:%s' % odoo_sum,
                    'source_ref': dataset.used_range or dataset.tab_title or '',
                    'message': _('Independent raw total for column %s differs by %s. Raw totals '
                                 'share no code with the normalizer, so this difference is real '
                                 'even when the content hashes agree.')
                               % (column.odoo_field, delta),
                })
        return totals, findings

    @api.model
    def _sum_raw(self, rows: Iterable[dict[str, Any]], container: str, key: str) -> Decimal:
        """Sum ``rows[*][container][key]`` with minimal parsing, skipping junk.

        ``raw_decimal`` returns ``None`` for empty, unparseable, NaN and infinite
        values; those are skipped rather than treated as zero. Treating an
        unparseable cell as zero would make a broken column silently balance.
        """
        total = Decimal(0)
        for row in rows:
            value = (row.get(container) or {}).get(key)
            parsed = raw_decimal(value)
            if parsed is not None:
                total += parsed
        return total

    @api.model
    def _slug_for(self, dataset, column) -> str:
        """The staged-row payload key for a mapping column.

        ``payload`` is keyed by the *dataset column* slug, while the contract is
        keyed by ``odoo_field``; the durable join between them is
        ``header_canon`` (SPEC §3.9), never the physical column position.
        """
        match = dataset.column_ids.filtered(lambda c: c.header_canon == column.header_canon)
        return match[0].slug if match else (column.odoo_field or '')

    # ------------------------------------------------------------------
    # Finalization
    # ------------------------------------------------------------------
    def _finish(self, verification, findings: list[dict[str, Any]], started: float) -> None:
        """Materialize findings, roll up the three disjoint counts, render the report."""
        drifts = self.env['gdrive.drift'].create_findings(verification, findings)
        counts = {
            'drift_count': len(drifts.filtered(lambda d: d.category == 'drift')),
            'data_quality_count': len(drifts.filtered(lambda d: d.category == 'data_quality')),
            'structural_count': len(drifts.filtered(lambda d: d.category == 'structural')),
            'duration_sec': time.monotonic() - started,
        }
        if verification.result not in ('error', 'blocked'):
            counts['result'] = 'drift' if counts['drift_count'] or counts['structural_count'] else 'verified'
        verification.write(counts)
        verification.dataset_id.last_verify_date = verification.date
        try:
            verification.render_report()
        except Exception:
            # A failed report must not invalidate a completed verification: the
            # findings are already persisted and are the artefact that matters.
            _logger.exception("Could not render the report for verification %s.", verification.id)

    # ------------------------------------------------------------------
    # Report artefact (SPEC §5.7)
    # ------------------------------------------------------------------
    def render_report(self):
        """Write ``verification.json`` and ``verification.html`` as attachments.

        Both are attached to the verification itself (``res_model`` /
        ``res_id``), so the evidence travels with the record and survives a
        purge of the run log.
        """
        Attachment = self.env['ir.attachment'].sudo()
        for verification in self:
            payload = verification._report_payload()
            json_bytes = json.dumps(payload, indent=2, sort_keys=True, default=str).encode('utf-8')
            html_bytes = verification._render_html(payload).encode('utf-8')
            base = 'verification-%s' % verification.id
            attachments = Attachment.create([
                {
                    'name': '%s.json' % base,
                    'res_model': 'gdrive.verification',
                    'res_id': verification.id,
                    'type': 'binary',
                    'mimetype': 'application/json',
                    'datas': base64.b64encode(json_bytes),
                },
                {
                    'name': '%s.html' % base,
                    'res_model': 'gdrive.verification',
                    'res_id': verification.id,
                    'type': 'binary',
                    'mimetype': 'text/html',
                    'datas': base64.b64encode(html_bytes),
                },
            ])
            verification.write({
                'report_json_attachment_id': attachments[0].id,
                'report_attachment_id': attachments[1].id,
            })
        return True

    def _report_payload(self) -> dict[str, Any]:
        """The machine artefact. Everything needed to re-argue the conclusion."""
        self.ensure_one()
        dataset = self.dataset_id
        node = dataset.node_id
        return {
            'verification_id': self.id,
            'name': self.name,
            'date': fields.Datetime.to_string(self.date),
            'dataset': {
                'id': dataset.id,
                'tab_title': dataset.tab_title,
                'sheet_gid': dataset.sheet_gid,
                'used_range': dataset.used_range,
                'node': {
                    'google_id': node.google_id,
                    'name': node.name,
                    'web_view_link': node.web_view_link,
                    'drive_version': node.drive_version,
                    'drive_modified_time': fields.Datetime.to_string(node.drive_modified_time)
                                           if node.drive_modified_time else None,
                },
            },
            'mapping': {
                'id': self.mapping_id.id,
                'target_model': self.mapping_id.target_model,
            } if self.mapping_id else None,
            'spec_version': self.spec_version,
            'mode': self.mode,
            'result': self.result,
            'hashes': {'sheet': self.h_dataset_sheet, 'odoo': self.h_dataset_odoo},
            'controls': {
                'rows_sheet': self.rows_sheet,
                'rows_odoo': self.rows_odoo,
                'column_totals': self.column_totals or {},
                'read_complete': self.read_complete,
            },
            'counts': {
                'drift': self.drift_count,
                'data_quality': self.data_quality_count,
                'structural': self.structural_count,
                'buckets_differing': self.buckets_differing,
                'rows_examined': self.rows_examined,
            },
            'duration_sec': round(self.duration_sec, 3),
            'error_detail': self.error_detail or None,
            'drifts': self.drift_ids.to_report_dict(),
        }

    def _render_html(self, payload: dict[str, Any]) -> str:
        """A human artefact grouped by drift type, with Drive deep links.

        Every value is escaped: canonical forms are attacker-influenced strings
        straight out of a spreadsheet, and this file is opened in a browser.
        """
        self.ensure_one()
        esc = html_escape
        grouped: dict[str, list[dict[str, Any]]] = {}
        for drift in payload['drifts']:
            grouped.setdefault(drift['drift_type'], []).append(drift)

        parts = [
            '<!DOCTYPE html><html><head><meta charset="utf-8"/>',
            '<title>%s</title>' % esc(payload['name'] or ''),
            '<style>body{font-family:system-ui,sans-serif;margin:2rem;}'
            'table{border-collapse:collapse;width:100%;margin-bottom:2rem;}'
            'th,td{border:1px solid #ccc;padding:.35rem .5rem;text-align:left;'
            'font-size:.85rem;vertical-align:top;}'
            'th{background:#f4f4f4;} code{white-space:pre-wrap;word-break:break-all;}'
            '.blocking,.critical{color:#a00;font-weight:bold;} .info{color:#666;}</style>',
            '</head><body>',
            '<h1>%s</h1>' % esc(payload['name'] or ''),
            '<p>Mode <b>%s</b> &mdash; result <b>%s</b> &mdash; %s row(s) in the sheet, '
            '%s in Odoo, read %s.</p>' % (
                esc(payload['mode']), esc(payload['result']),
                payload['controls']['rows_sheet'], payload['controls']['rows_odoo'],
                'complete' if payload['controls']['read_complete'] else '<b>INCOMPLETE</b>',
            ),
            '<p>Sheet hash <code>%s</code><br/>Odoo hash <code>%s</code><br/>'
            'spec_version <code>%s</code></p>' % (
                esc(payload['hashes']['sheet'] or '-'),
                esc(payload['hashes']['odoo'] or '-'),
                esc(payload['spec_version'] or '-'),
            ),
            '<p>%d real difference(s), %d data-quality item(s), %d structural finding(s). '
            'The three counts are disjoint: a data-quality item is a cell that could not be '
            'read, not a difference.</p>' % (
                payload['counts']['drift'], payload['counts']['data_quality'],
                payload['counts']['structural'],
            ),
        ]
        link = payload['dataset']['node']['web_view_link']
        if link:
            parts.append('<p><a href="%s" target="_blank" rel="noopener">Open in Google Drive</a></p>'
                         % esc(link))

        if payload['controls']['column_totals']:
            parts.append('<h2>Independent raw column totals</h2><table>'
                         '<tr><th>Field</th><th>Sheet</th><th>Odoo</th><th>Delta</th></tr>')
            for field_name, total in sorted(payload['controls']['column_totals'].items()):
                parts.append('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
                    esc(field_name), esc(str(total.get('sheet'))),
                    esc(str(total.get('odoo'))), esc(str(total.get('delta')))))
            parts.append('</table>')

        for drift_type, items in sorted(grouped.items()):
            parts.append('<h2>%s <small>(%d)</small></h2>' % (esc(drift_type), len(items)))
            parts.append('<table><tr><th>Severity</th><th>Cell</th><th>Field</th>'
                         '<th>Sheet says</th><th>Odoo says</th><th>Message</th></tr>')
            for item in items:
                parts.append(
                    '<tr><td class="%s">%s</td><td><code>%s</code></td><td>%s</td>'
                    '<td><code>%s</code></td><td><code>%s</code></td><td>%s</td></tr>' % (
                        esc(item['severity']), esc(item['severity']),
                        esc(item['source_ref'] or ''), esc(item['field_name'] or ''),
                        esc(item['canon_sheet'] or ''), esc(item['canon_odoo'] or ''),
                        esc(item['message'] or ''),
                    ))
            parts.append('</table>')
        if not grouped:
            parts.append('<p>No findings.</p>')
        parts.append('</body></html>')
        return ''.join(parts)

    # ------------------------------------------------------------------
    # UI actions
    # ------------------------------------------------------------------
    def action_open_report(self):
        """Download the HTML report."""
        self.ensure_one()
        if not self.report_attachment_id:
            raise UserError(_('This verification has no rendered report yet.'))
        return {
            'type': 'ir.actions.act_url',
            'url': '/web/content/%s?download=true' % self.report_attachment_id.id,
            'target': 'self',
        }

    def action_view_drifts(self):
        """Open this verification's findings in the triage screen."""
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('gdrive_odoo_sync.action_gdrive_drift')
        action['domain'] = [('verification_id', '=', self.id)]
        action['context'] = {'search_default_group_drift_type': 1}
        return action

    def action_verify_now(self):
        """Re-run the full comparison for this verification's dataset."""
        for verification in self:
            self.verify_dataset(verification.dataset_id, force_full=True)
        return True


def literal_eval_domain(value: str) -> list:
    """Parse a stored domain string safely.

    ``ast.literal_eval`` and not ``eval``: ``gdrive.mapping.domain`` is
    administrator-supplied text, and evaluating it with the full Python
    namespace would turn a mapping form into remote code execution.
    """
    import ast
    parsed = ast.literal_eval(value)
    if not isinstance(parsed, (list, tuple)):
        raise ValueError('A domain must be a list, got %r' % (type(parsed).__name__,))
    return list(parsed)
