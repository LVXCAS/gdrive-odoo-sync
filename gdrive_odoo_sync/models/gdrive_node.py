# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. See LICENSE (LGPL-3) for full copyright and licensing details.
"""``gdrive.node`` — the mirrored Drive tree and the ingest stage (SPEC §3.4, §5.2–§5.3).

Identity is the **Drive file id**, never the title. ``Bettr_Bowl_Data_Request``
exists twice in this Drive; titles are display strings and duplicates are legal.
Every upsert, every dedup and every unique constraint in this file keys on
``(connection_id, google_id)``.

Two behaviours in here are load-bearing and easy to get wrong:

* **Nothing is ever unlinked.** A file that disappears from Drive becomes
  ``state='gone'``, ``active=False`` — the record, its mirrored attachments and
  its staged rows all survive. An empty read from a mis-scoped service account is
  byte-for-byte identical to "the user deleted everything" (SPEC §2.1), so
  deletion on absence is structurally forbidden here.
* **Attachments are written with ``raw``, never with ``res_field``, always
  through ``sudo()``.** Setting ``res_field`` makes an attachment invisible in
  the generic Attachments sidebar (``ir.attachment`` filters those out in its
  read/search override) and the file appears to have vanished. Writing both
  ``raw`` and ``datas`` double-encodes. And ``ir.attachment`` ACL derives from
  the linked record, which a cron running as root can only satisfy via sudo.
"""

import logging
import time
from datetime import datetime, timezone

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..services.drive_download import DriveDownloader
from ..services.errors import (
    GDriveError,
    GDriveExportTooLarge,
    GDrivePermanentError,
    redact,
)
from ..services.google_client import build_services
from ..services.mimetypes import (
    classify,
    export_targets_for,
    extension_for,
    is_folder,
    is_spreadsheet_blob,
)
from ..services.sheets_reader import SheetsReader
from .gdrive_sync_run import COMMIT_BATCH, CRON_BUDGET_SEC, trigger_cron

_logger = logging.getLogger(__name__)

#: How many historical attachments to retain per node before pruning the oldest.
#: Re-ingesting a changed file creates a *new* attachment and repoints the node;
#: the previous one is kept so "what did this PDF say last month?" is answerable.
#: Unbounded retention would make the filestore grow with every metadata touch.
KEEP_VERSIONS = 5

#: Export MIME used for the optional archival snapshot of a native Google Sheet.
#: Deliberately not in ``EXPORT_MAP``: native Sheets are read through the Sheets
#: API, and ``files.export`` hard-fails at 10 MB with ``exportSizeLimitExceeded``.
XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

NODE_TYPE_SELECTION = [
    ('folder', 'Folder'),
    ('spreadsheet', 'Google Sheet'),
    ('document', 'Google Doc'),
    ('presentation', 'Google Slides'),
    ('drawing', 'Google Drawing'),
    ('blob', 'Binary File'),
    ('shortcut', 'Shortcut'),
    ('other_google', 'Other Google Type'),
]

STATE_SELECTION = [
    ('discovered', 'Discovered'),
    ('queued', 'Queued'),
    ('ingested', 'Ingested'),
    ('skipped', 'Skipped'),
    ('error', 'Error'),
    ('gone', 'Gone'),
]

SKIP_REASON_SELECTION = [
    ('too_large', 'Too Large'),
    ('out_of_scope', 'Out of Scope'),
    ('unsupported_mime', 'Unsupported MIME Type'),
    ('no_download_permission', 'No Download Permission'),
    ('shortcut', 'Shortcut'),
    ('folder', 'Folder'),
]

#: Synthetic root for files whose parents are all invisible to us. My Drive's own
#: root folder is never returned by ``files.list``, so most top-level items land
#: here by construction — that is expected, not a bug.
ORPHAN_ROOT = '/(orphans)'


class _SkipIngest(Exception):
    """Internal control-flow signal: this node already reached a terminal state.

    WHY an exception rather than a return value: the skip decisions live inside
    the per-type handlers, several call levels below the place that writes the
    final ``state='ingested'``. Threading a sentinel back through every layer
    would make it easy for one branch to forget and mark a skipped node
    ingested; the exception makes "I already wrote a terminal state" impossible
    to ignore. It never escapes :meth:`GdriveNode._ingest_one`.
    """


def parse_drive_time(value):
    """Convert an RFC-3339 Drive timestamp into a UTC-naive ``datetime``.

    WHY UTC-naive: Odoo stores every ``fields.Datetime`` UTC-naive, and mixing an
    aware value into a comparison against a stored one raises at runtime — but
    only on the code path that happens to compare, which is typically the L0
    fast path that runs once a day. Normalizing at the boundary keeps the whole
    model layer in one time representation.

    Returns ``False`` (not ``None``) on anything unparseable, because ``False``
    is what the ORM writes for an empty Datetime.
    """
    if not value:
        return False
    text = str(value).strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        _logger.warning("Unparseable Drive timestamp %r; storing empty.", value)
        return False
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.replace(microsecond=0)


class GdriveNode(models.Model):
    """One Google Drive object (folder, file, native doc or shortcut)."""

    _name = 'gdrive.node'
    _description = 'Google Drive Node'
    _inherit = ['mail.thread']
    _order = 'path, name'
    _rec_name = 'name'

    connection_id = fields.Many2one(
        'gdrive.connection', string='Connection',
        required=True, index=True, ondelete='cascade',
    )
    company_id = fields.Many2one(
        related='connection_id.company_id', store=True, index=True, string='Company')
    google_id = fields.Char(
        string='Drive Id', required=True, index=True, copy=False,
        help='THE identity. Titles duplicate freely and are display-only.',
    )
    name = fields.Char(string='Name', required=True, index=True, tracking=True)
    mime_type = fields.Char(string='MIME Type', required=True, index=True)
    node_type = fields.Selection(NODE_TYPE_SELECTION, string='Type', required=True, index=True)

    parent_id = fields.Many2one(
        'gdrive.node', string='Parent', index=True, ondelete='set null',
        help='The first entry of parent_google_ids that resolves to a visible node.',
    )
    child_ids = fields.One2many('gdrive.node', 'parent_id', string='Children')
    parent_google_ids = fields.Json(
        string='Parent Drive Ids',
        help='The full Drive parents array. Only shared-drive files are '
             'guaranteed to have exactly one parent.',
    )
    is_orphan = fields.Boolean(
        string='Orphan', default=False, index=True,
        help='No parent in the array resolves to a visible node. Expected for '
             'top-level My Drive items: the My Drive root itself is never '
             'returned by files.list.',
    )
    path = fields.Char(string='Path', index=True, help='Materialized "/"-joined ancestor names. Display and glob matching only.')
    depth = fields.Integer(string='Depth', default=0)

    shared_drive_id = fields.Char(string='Shared Drive Id', index=True)
    owner_email = fields.Char(string='Owner', index=True)
    is_shared_with_me = fields.Boolean(string='Shared With Me', default=False)

    shortcut_target_google_id = fields.Char(string='Shortcut Target Id')
    shortcut_target_mime = fields.Char(string='Shortcut Target MIME')
    resolved_node_id = fields.Many2one(
        'gdrive.node', string='Resolved Target', ondelete='set null',
        help='For shortcuts: the real node. Shortcuts are never ingested '
             'themselves — the target is stored once, keyed by its file id, so '
             'reaching a file both directly and via a shortcut dedups for free.',
    )

    size_bytes = fields.Integer(string='Size (bytes)', aggregator='sum')
    md5_checksum = fields.Char(string='Drive MD5', help='Absent for native Google types.')
    drive_version = fields.Char(
        string='Drive Version',
        help='Increments on metadata-only changes too. That errs toward '
             '"changed", which is the safe direction for a cache key.',
    )
    drive_modified_time = fields.Datetime(string='Modified (Drive)', index=True)
    drive_created_time = fields.Datetime(string='Created (Drive)')
    web_view_link = fields.Char(string='Drive Link')
    can_download = fields.Boolean(string='Downloadable', default=True)
    trashed = fields.Boolean(string='Trashed', default=False, index=True)

    ingest_policy = fields.Selection(
        [('auto', 'Automatic'), ('attachment', 'Attachment Only'),
         ('dataset', 'Dataset Only'), ('ignore', 'Ignore')],
        string='Ingest Policy', default='auto', required=True,
        help='Manual override of the MIME classification.',
    )
    state = fields.Selection(
        STATE_SELECTION, string='Status', default='discovered', required=True, index=True, tracking=True)
    skip_reason = fields.Selection(SKIP_REASON_SELECTION, string='Skip Reason')

    attachment_id = fields.Many2one('ir.attachment', string='Content', ondelete='set null')
    text_attachment_id = fields.Many2one('ir.attachment', string='Text Extract', ondelete='set null')
    attachment_checksum = fields.Char(string='Attachment SHA-1')

    last_ingest_date = fields.Datetime(string='Last Ingest')
    last_seen_date = fields.Datetime(string='Last Seen', index=True)
    gone_since = fields.Datetime(string='Gone Since')
    last_error = fields.Text(string='Last Error')

    dataset_ids = fields.One2many('gdrive.dataset', 'node_id', string='Datasets')
    dataset_count = fields.Integer(string='Tabs', compute='_compute_dataset_count')
    active = fields.Boolean(string='Active', default=True)

    _sql_constraints = [
        ('node_uniq', 'unique(connection_id, google_id)',
         'A Drive file id appears once per connection.'),
    ]

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------
    @api.depends('dataset_ids')
    def _compute_dataset_count(self):
        """Non-stored tab counter for the node form."""
        grouped = {}
        if self.ids:
            for node, count in self.env['gdrive.dataset']._read_group(
                    [('node_id', 'in', self.ids)], groupby=['node_id'], aggregates=['__count']):
                grouped[node.id] = count
        for node in self:
            node.dataset_count = grouped.get(node.id, 0)

    @api.depends('name', 'path')
    def _compute_display_name(self):
        """Show the path, because names are ambiguous by design in this dataset."""
        for node in self:
            node.display_name = node.path or node.name or node.google_id or ''

    # ------------------------------------------------------------------
    # Discovery: upsert
    # ------------------------------------------------------------------
    @api.model
    def _vals_from_meta(self, connection, meta) -> dict:
        """Translate one Drive ``files`` resource into ORM values.

        Kept separate from the upsert so the mapping is testable against a
        literal API response, which is how every field-name mistake in a
        ``fields`` mask is actually caught.
        """
        mime = meta.get('mimeType') or 'application/octet-stream'
        shortcut = meta.get('shortcutDetails') or {}
        owners = meta.get('owners') or []
        capabilities = meta.get('capabilities') or {}
        try:
            size = int(meta.get('size') or 0)
        except (TypeError, ValueError):
            size = 0
        return {
            'connection_id': connection.id,
            'google_id': meta['id'],
            'name': meta.get('name') or meta['id'],
            'mime_type': mime,
            'node_type': classify(mime, shortcut or None),
            'parent_google_ids': list(meta.get('parents') or []),
            'shared_drive_id': meta.get('driveId') or False,
            'owner_email': (owners[0].get('emailAddress') if owners else False) or False,
            'is_shared_with_me': bool(meta.get('sharedWithMeTime')),
            'shortcut_target_google_id': shortcut.get('targetId') or False,
            'shortcut_target_mime': shortcut.get('targetMimeType') or False,
            'size_bytes': size,
            'md5_checksum': meta.get('md5Checksum') or False,
            'drive_version': str(meta['version']) if meta.get('version') is not None else False,
            'drive_modified_time': parse_drive_time(meta.get('modifiedTime')),
            'drive_created_time': parse_drive_time(meta.get('createdTime')),
            'web_view_link': meta.get('webViewLink') or False,
            'can_download': bool(capabilities.get('canDownload', True)),
            'trashed': bool(meta.get('trashed')),
        }

    @api.model
    def _upsert_from_drive(self, connection, metas, run=None):
        """Create or update nodes from a batch of Drive ``files`` resources.

        Returns the affected recordset.

        The **fast-path short-circuit** lives here: a node is only re-queued for
        ingest when ``(drive_version, modifiedTime, md5Checksum)`` actually
        moved. ``version`` bumps on metadata-only edits too, so this errs toward
        re-ingesting — the correct direction for a cache key, since the cost of a
        needless re-download is bandwidth while the cost of a missed change is a
        wrong answer.

        ``md5Checksum`` and ``headRevisionId`` are *not* used as the sole test:
        they are blob-only and simply absent on native Sheets and Docs, so a
        checksum-only comparison would mark every native file permanently
        unchanged.
        """
        metas = [m for m in metas if m and m.get('id')]
        if not metas:
            return self.browse()
        now = fields.Datetime.now()
        by_gid = {m['id']: m for m in metas}
        existing = self.sudo().with_context(active_test=False).search([
            ('connection_id', '=', connection.id),
            ('google_id', 'in', list(by_gid)),
        ])
        existing_by_gid = {n.google_id: n for n in existing}

        to_create = []
        touched = self.browse()
        for gid, meta in by_gid.items():
            vals = self._vals_from_meta(connection, meta)
            vals['last_seen_date'] = now
            node = existing_by_gid.get(gid)
            if not node:
                vals['state'] = 'queued'
                vals['active'] = True
                to_create.append(vals)
                continue

            content_moved = (
                (node.drive_version or '') != (vals.get('drive_version') or '')
                or node.drive_modified_time != vals.get('drive_modified_time')
                or (node.md5_checksum or '') != (vals.get('md5_checksum') or '')
            )
            vals['active'] = True
            vals['gone_since'] = False
            if node.state == 'gone':
                # Reappeared: a permission was restored or the file was untrashed.
                vals['state'] = 'queued'
            elif content_moved:
                vals['state'] = 'queued'
                vals['last_error'] = False
            elif node.state in ('discovered', 'error'):
                vals['state'] = 'queued'
            # A 'skipped' node whose content did not move stays skipped: re-queuing
            # a 4 GB file every 15 minutes just to skip it again is pure waste.
            changed = {}
            for key, value in vals.items():
                if key == 'connection_id':
                    # Immutable by construction: the search that found this node
                    # was already scoped to the connection.
                    continue
                if key == 'parent_google_ids':
                    if (node.parent_google_ids or []) != (value or []):
                        changed[key] = value
                    continue
                if node[key] != value:
                    changed[key] = value
            if changed:
                node.sudo().write(changed)
            touched |= node

        if to_create:
            touched |= self.sudo().create(to_create)
        if run:
            run._bump(nodes_seen=len(by_gid))
        return touched

    # ------------------------------------------------------------------
    # Discovery: tree materialization
    # ------------------------------------------------------------------
    @api.model
    def _resolve_tree(self, connection):
        """Resolve parents, orphans, ``path``, ``depth`` and shortcut targets.

        Runs once at the end of a discovery pass rather than per node, because
        parents legitimately arrive *after* their children in a flat
        ``files.list`` enumeration — resolving eagerly would mark half the drive
        orphaned and then have to undo it.

        Returns the number of nodes whose materialized position changed.
        """
        nodes = self.sudo().with_context(active_test=False).search([
            ('connection_id', '=', connection.id),
            ('state', '!=', 'gone'),
        ])
        if not nodes:
            return 0
        by_gid = {n.google_id: n for n in nodes}

        # --- parents / orphans -------------------------------------------------
        parent_of = {}
        for node in nodes:
            resolved = False
            for gid in (node.parent_google_ids or []):
                candidate = by_gid.get(gid)
                if candidate is not None and candidate.id != node.id:
                    resolved = candidate
                    break
            parent_of[node.id] = resolved

        # --- path / depth, breadth-first from the resolvable roots -------------
        # Iterative rather than recursive: a pathological parents array (or a
        # cycle produced by a Drive move race) would blow the Python stack, and a
        # depth cap turns that into a bounded, logged anomaly instead.
        placed = {}
        pending = list(nodes)
        for _round in range(64):
            still_pending = []
            for node in pending:
                parent = parent_of[node.id]
                if not parent:
                    placed[node.id] = ('%s/%s' % (ORPHAN_ROOT, node.name or node.google_id), 0, True)
                elif parent.id in placed:
                    parent_path, parent_depth, _po = placed[parent.id]
                    placed[node.id] = ('%s/%s' % (parent_path, node.name or node.google_id),
                                       parent_depth + 1, False)
                else:
                    still_pending.append(node)
            if not still_pending or len(still_pending) == len(pending):
                pending = still_pending
                break
            pending = still_pending
        for node in pending:
            # Unreachable within the depth cap: treat as an orphan rather than
            # leaving path empty, so glob rules and the UI still work.
            _logger.warning("Node %s (%s) exceeded the tree resolution depth cap; treating as orphan.",
                            node.google_id, node.name)
            placed[node.id] = ('%s/%s' % (ORPHAN_ROOT, node.name or node.google_id), 0, True)

        changed = 0
        for node in nodes:
            path, depth, orphan = placed[node.id]
            parent = parent_of[node.id]
            vals = {}
            if node.path != path:
                vals['path'] = path
            if node.depth != depth:
                vals['depth'] = depth
            if node.is_orphan != orphan:
                vals['is_orphan'] = orphan
            parent_id = parent.id if parent else False
            if node.parent_id.id != parent_id:
                vals['parent_id'] = parent_id
            if vals:
                node.sudo().write(vals)
                changed += 1

        # --- shortcut targets --------------------------------------------------
        for node in nodes.filtered(lambda n: n.node_type == 'shortcut' and n.shortcut_target_google_id):
            target = by_gid.get(node.shortcut_target_google_id)
            target_id = target.id if target else False
            if node.resolved_node_id.id != target_id:
                node.sudo().write({'resolved_node_id': target_id})
                changed += 1
        return changed

    def _scope_meta(self) -> dict:
        """Return the plain dict a :class:`gdrive.scope.rule` evaluates against.

        A dict rather than the record itself so rule evaluation is a pure
        function of data and can be unit-tested without a database.
        """
        self.ensure_one()
        ancestors = []
        current = self.parent_id
        guard = 0
        while current and guard < 64:
            ancestors.append(current.google_id)
            current = current.parent_id
            guard += 1
        return {
            'google_id': self.google_id,
            'name': self.name or '',
            'mime_type': self.mime_type or '',
            'node_type': self.node_type or '',
            'is_spreadsheet_blob': is_spreadsheet_blob(self.mime_type or ''),
            'owner_email': self.owner_email or '',
            'shared_drive_id': self.shared_drive_id or '',
            'path': self.path or '',
            'ancestor_google_ids': ancestors,
        }

    @api.model
    def _apply_scope_rules(self, connection, run=None):
        """Move nodes in and out of ``skip_reason='out_of_scope'``.

        Never deletes and never touches already-mirrored content: a node leaving
        scope keeps its attachment and its staged rows, and its promotion is
        merely suspended. A one-character typo in a glob must not be able to
        destroy data.
        """
        rules = connection.scope_rule_ids
        nodes = self.sudo().with_context(active_test=False).search([
            ('connection_id', '=', connection.id),
            ('state', '!=', 'gone'),
        ])
        if not nodes:
            return 0
        excluded_roots = rules.excluded_subtree_roots()
        changed = 0
        for node in nodes:
            meta = node._scope_meta()
            pruned = bool(excluded_roots and (
                node.google_id in excluded_roots
                or excluded_roots.intersection(meta['ancestor_google_ids'])
            ))
            in_scope = (not pruned) and rules.evaluate(meta)
            if not in_scope and node.skip_reason != 'out_of_scope':
                node.sudo().write({'state': 'skipped', 'skip_reason': 'out_of_scope'})
                changed += 1
            elif in_scope and node.skip_reason == 'out_of_scope':
                node.sudo().write({'state': 'queued', 'skip_reason': False})
                changed += 1
        if run and changed:
            run._log('SCOPE_APPLIED', 'Scope rules moved %d node(s) in or out of scope.' % changed,
                     level='info', stage='discover')
        return changed

    # ------------------------------------------------------------------
    # Disappearance
    # ------------------------------------------------------------------
    def _mark_gone(self, run=None, reason='removed'):
        """Record that a Drive object left the subject's view.

        **This never unlinks anything.** ``removed=True`` from the Changes API
        means deleted *or* trashed *or* permission-revoked *or* moved out of
        scope — four very different events with one indistinguishable signal.
        The node, its attachments and its staged rows are retained; only the
        business-record delete planner (lane E) may act on absence, and only
        after every guard in SPEC §9.6.
        """
        now = fields.Datetime.now()
        for node in self:
            if node.state == 'gone':
                continue
            node.sudo().write({'state': 'gone', 'gone_since': now, 'active': False})
            if run:
                run._log('NODE_GONE', 'Drive object %r (%s) is no longer visible (%s).'
                         % (node.name, node.google_id, reason),
                         level='info', stage='discover', node=node)
        return True

    # ------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------
    @api.model
    def _cron_ingest(self):
        """Cron entry point ``ir_cron_gdrive_ingest`` — fetch bytes for queued nodes.

        Batch driver with a wall-clock budget (SPEC §6): bounded slice, commit
        every :data:`COMMIT_BATCH` records, re-``browse`` after commit, and
        ``_trigger()`` a follow-up when the budget runs out with work remaining.

        **Never raises.** Odoo 18 auto-deactivates a cron after repeated
        failures; a per-node exception is recorded as an ``error`` run line and
        the driver moves on.
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
                _logger.info("Ingest skipped for %s: another run holds the advisory lock.", connection.display_name)
                continue
            try:
                remaining = self._ingest_connection(connection, deadline)
                backlog = backlog or remaining
            except Exception as exc:  # noqa: BLE001 - a cron may never raise
                self.env.cr.rollback()
                _logger.exception("Ingest failed for connection %s.", connection.display_name)
                connection.sudo().write({'last_error': redact(str(exc))})
                self.env.cr.commit()
        if backlog:
            trigger_cron(self.env, 'gdrive_odoo_sync.ir_cron_gdrive_ingest')
        return True

    @api.model
    def _ingest_connection(self, connection, deadline):
        """Ingest the queued nodes of one connection. Returns True if work remains."""
        queued = self.sudo().search([
            ('connection_id', '=', connection.id),
            ('state', '=', 'queued'),
            ('active', '=', True),
            ('trashed', '=', False),
        ], order='node_type, id')
        if not queued:
            return False

        run = self.env['gdrive.sync.run']._start(
            connection, trigger='cron', mode='delta', stages=['ingest'])
        # Commit the run row immediately. Without this, a failure on the *first*
        # node rolls back the transaction that created it, and the very next
        # statement (run._log) inserts a gdrive.sync.run.line whose run_id
        # foreign key points at a row that no longer exists — an IntegrityError
        # that escapes the per-node handler, kills the whole cron pass, discards
        # the node's own state='error' write (so it stays queued and fails
        # identically every 30 minutes) and leaves no run and no log line for
        # anyone to find.
        self.env.cr.commit()
        run = run.browse(run.id)
        ctx = connection._service_context()
        drive, sheets = build_services(ctx)
        downloader = DriveDownloader(drive, ctx)
        sheets_reader = SheetsReader(sheets, ctx)

        processed = 0
        remaining = False
        try:
            for node in queued:
                if time.monotonic() > deadline:
                    remaining = True
                    run._log('BUDGET_EXHAUSTED',
                             'Ingest budget exhausted with %d node(s) still queued.'
                             % (len(queued) - processed),
                             level='info', stage='ingest')
                    break
                try:
                    node._ingest_one(run, ctx, downloader, sheets_reader)
                except Exception as exc:  # noqa: BLE001 - per-entity isolation
                    self.env.cr.rollback()
                    # The rollback invalidated every cached record in this
                    # environment, the run included. Re-browse before touching
                    # either of them.
                    node = node.browse(node.id)
                    run = run.browse(run.id)
                    node.sudo().write({'state': 'error', 'last_error': redact(str(exc))})
                    run._log('INGEST_FAILED', 'Ingest of %r failed: %s' % (node.name, exc),
                             level='error', stage='ingest', node=node)
                    # Default to incomplete. A node we failed to read is a node
                    # whose rows we cannot vouch for, and complete_read=True is
                    # what licenses _persist_rows to write missing_since over
                    # every row it did not see — starting the delete quarantine
                    # clock on data that never went anywhere. GDriveQuotaError
                    # (8 exhausted attempts against a 429) and GDriveIncompleteRead
                    # (a short batchGet, a lost changes cursor) both land here,
                    # and neither is a GDrivePermanentError, so before this the
                    # only caller of _mark_incomplete in the whole codebase was
                    # unreachable for them.
                    run._mark_incomplete(
                        'INGEST_FAILED',
                        'Ingest of %r failed (%s); this run cannot be treated as a '
                        'complete read.' % (node.name, type(exc).__name__),
                        stage='ingest', node=node)
                    node.message_post(body=_('Ingest failed: %s', redact(str(exc))))
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

    def _ingest_one(self, run, ctx, downloader, sheets_reader):
        """Materialize one node's content. Idempotent by construction.

        Dispatch is on ``node_type`` (SPEC §5.2), with ``ingest_policy``
        overriding the automatic classification. Every branch ends in a terminal
        state — ``ingested`` or ``skipped`` with a reason — so a node can never
        sit in ``queued`` forever and silently never be looked at again.
        """
        self.ensure_one()
        started = time.monotonic()
        run._add_stage('ingest')

        if self.ingest_policy == 'ignore':
            self.sudo().write({'state': 'skipped', 'skip_reason': 'out_of_scope'})
            return True
        if self.node_type == 'folder' or is_folder(self.mime_type or ''):
            # Folders carry structure, never content. The tree is already
            # materialized by _resolve_tree; there is nothing to fetch.
            self.sudo().write({'state': 'skipped', 'skip_reason': 'folder', 'last_ingest_date': fields.Datetime.now()})
            return True
        if self.node_type == 'shortcut':
            self.sudo().write({'state': 'skipped', 'skip_reason': 'shortcut'})
            return True
        if self.node_type == 'other_google':
            self.sudo().write({'state': 'skipped', 'skip_reason': 'unsupported_mime'})
            run._log('UNSUPPORTED_MIME', 'No handler for %s (%r); metadata only.'
                     % (self.mime_type, self.name), level='info', stage='ingest', node=self)
            return True

        try:
            if self.node_type == 'spreadsheet':
                self._ingest_native_spreadsheet(run, ctx, downloader, sheets_reader)
            elif self.node_type in ('document', 'presentation', 'drawing'):
                self._ingest_native_export(run, downloader)
            else:
                self._ingest_blob(run, downloader)
        except _SkipIngest:
            # A handler already wrote a terminal 'skipped' state and logged why.
            return True
        except GDriveExportTooLarge as exc:
            # Documented v1 limitation: files.export caps the *generated artefact*
            # at 10 MB and chunked download does not help. Skip loudly.
            self.sudo().write({'state': 'skipped', 'skip_reason': 'too_large',
                               'last_error': redact(str(exc))})
            run._log('EXPORT_SIZE_LIMIT',
                     'Google refused to export %r: the generated file exceeds 10 MB.' % self.name,
                     level='warning', stage='ingest', node=self)
            return True
        except GDrivePermanentError as exc:
            self.sudo().write({'state': 'skipped', 'skip_reason': 'no_download_permission',
                               'last_error': redact(str(exc))})
            run._log('ACCESS_LOST', 'Permanent Drive error on %r: %s' % (self.name, exc),
                     level='error', stage='ingest', node=self)
            run._mark_incomplete('ACCESS_LOST',
                                 'A previously readable object became unreadable: %r.' % self.name,
                                 stage='ingest', node=self)
            return True

        self.sudo().write({
            'state': 'ingested',
            'skip_reason': False,
            'last_ingest_date': fields.Datetime.now(),
            'last_error': False,
        })
        run._bump(nodes_ingested=1)
        run._log('NODE_INGESTED', 'Ingested %r.' % self.name, level='info', stage='ingest',
                 node=self, duration_ms=int((time.monotonic() - started) * 1000))
        return True

    def _ingest_blob(self, run, downloader):
        """Download a binary file into ``ir.attachment``; parse xlsx into datasets."""
        self.ensure_one()
        max_bytes = self.connection_id.max_blob_bytes or 0
        if max_bytes and self.size_bytes and self.size_bytes > max_bytes:
            self.sudo().write({'state': 'skipped', 'skip_reason': 'too_large'})
            run._log('BLOB_TOO_LARGE',
                     '%r is %d bytes, above the %d byte limit; recorded but not downloaded.'
                     % (self.name, self.size_bytes, max_bytes),
                     level='info', stage='ingest', node=self)
            raise _SkipIngest()
        if not self.can_download:
            self.sudo().write({'state': 'skipped', 'skip_reason': 'no_download_permission'})
            run._log('NO_DOWNLOAD_PERMISSION',
                     'capabilities.canDownload is false for %r.' % self.name,
                     level='warning', stage='ingest', node=self)
            raise _SkipIngest()

        if self.ingest_policy != 'dataset':
            if self._blob_is_current():
                run._log('DOWNLOAD_SKIPPED',
                         'md5Checksum unchanged for %r; reusing the existing attachment.' % self.name,
                         level='info', stage='ingest', node=self)
                data = self.attachment_id.sudo().raw if is_spreadsheet_blob(self.mime_type or '') else None
            else:
                data, effective_mime = downloader.fetch(self.google_id, self.mime_type)
                self._store_attachment(self._attachment_filename(effective_mime or self.mime_type),
                                       data, effective_mime or self.mime_type)
                run._bump(attachments_written=1)
        else:
            data, _mime = downloader.fetch(self.google_id, self.mime_type)

        if is_spreadsheet_blob(self.mime_type or '') and self.ingest_policy != 'attachment':
            if data is None:
                data = self.attachment_id.sudo().raw
            if data:
                self.env['gdrive.dataset']._sync_tabs_from_xlsx(self, bytes(data), run)
        return True

    def _blob_is_current(self) -> bool:
        """True when the stored attachment already holds this exact blob.

        Drive's ``md5Checksum`` changes on any content change, so an unchanged
        checksum plus an existing attachment means downloading again would
        transfer bytes we already have. Native Google types have no checksum, so
        this returns False for them and they always re-export — correct, since
        their content can change without any checksum to prove it.
        """
        self.ensure_one()
        return bool(self.md5_checksum and self.attachment_id and self.attachment_id.exists())

    def _ingest_native_export(self, run, downloader):
        """Export a native Doc/Slides/Drawing to PDF (plus text for Docs)."""
        self.ensure_one()
        # EXPORT_MAP is keyed by *node_type* and its values are
        # {'primary','secondary'} dicts, not bare MIME strings. Looking it up by
        # self.mime_type ('application/vnd.google-apps.document') always missed,
        # so every Doc, Slides deck and Drawing in the corpus was silently
        # written state='skipped'/'unsupported_mime' and no PDF was ever
        # exported. export_targets_for() is the accessor that gets both right.
        export_mime = export_targets_for(self.node_type).get('primary')
        if not export_mime:
            self.sudo().write({'state': 'skipped', 'skip_reason': 'unsupported_mime'})
            run._log('NO_EXPORT_MAPPING',
                     'No export MIME declared for node type %s (%s).'
                     % (self.node_type, self.mime_type),
                     level='info', stage='ingest', node=self)
            raise _SkipIngest()
        data, effective_mime = downloader.fetch(self.google_id, self.mime_type)
        self._store_attachment(self._attachment_filename(effective_mime or export_mime),
                               data, effective_mime or export_mime)
        run._bump(attachments_written=1)

        if self.node_type == 'document':
            # The plain-text extract exists so drift reports and search can quote
            # a Doc without rasterizing a PDF. Its failure is never fatal: the
            # PDF is the artefact of record.
            try:
                text_data, _tm = downloader.fetch(self.google_id, self.mime_type, export_mime='text/plain')
                self._store_attachment('%s.txt' % (self.name or self.google_id), text_data,
                                       'text/plain', field='text_attachment_id')
                run._bump(attachments_written=1)
            except GDriveError as exc:
                run._log('TEXT_EXPORT_FAILED',
                         'Plain-text export of %r failed (the PDF is unaffected): %s' % (self.name, exc),
                         level='warning', stage='ingest', node=self)
        return True

    def _ingest_native_spreadsheet(self, run, ctx, downloader, sheets_reader):
        """Enumerate the tabs of a native Google Sheet; optionally archive an xlsx.

        Native Sheets are deliberately **not** exported for their content:
        ``files.export`` to CSV silently returns only the first tab, and to xlsx
        it hard-fails at 10 MB. Their values are read through the Sheets API,
        which has neither limitation.
        """
        self.ensure_one()
        self.env['gdrive.dataset']._sync_tabs_from_sheets(self, sheets_reader, run)
        if self.connection_id.mirror_sheet_snapshot:
            try:
                data, _mime = downloader.fetch(self.google_id, self.mime_type, export_mime=XLSX_MIME)
                self._store_attachment('%s.xlsx' % (self.name or self.google_id), data, XLSX_MIME)
                run._bump(attachments_written=1)
            except GDriveExportTooLarge:
                # A warning, not an error: the snapshot is archival, and the
                # authoritative read went through the Sheets API regardless.
                run._log('EXPORT_SIZE_LIMIT',
                         'Archival xlsx snapshot of %r exceeds the 10 MB export cap; '
                         'tab data was still read through the Sheets API.' % self.name,
                         level='warning', stage='ingest', node=self)
        return True

    # ------------------------------------------------------------------
    # Attachments
    # ------------------------------------------------------------------
    def _attachment_filename(self, mime) -> str:
        """Drive title with a correct extension appended when it lacks one."""
        self.ensure_one()
        name = (self.name or self.google_id or 'file').strip()
        ext = extension_for(mime or self.mime_type or '')
        if ext and not name.lower().endswith(ext.lower()):
            name = '%s%s' % (name, ext)
        return name

    def _store_attachment(self, filename, data, mimetype, field='attachment_id'):
        """Create a new ``ir.attachment`` and repoint the node at it.

        Deliberate choices, each of which has a failure mode attached:

        * ``raw`` and never ``datas`` — passing both double-encodes the payload.
        * **no** ``res_field`` — ``ir.attachment``'s read/search override filters
          field-bound attachments out of the generic Attachments sidebar, so the
          mirrored file would appear to have vanished.
        * ``sudo()`` — attachment ACL derives from ``(res_model, res_id)``, and
          the cron user must be able to write regardless of the node's rules.
        * a **new** record rather than a rewrite — the previous version is
          retained (SPEC §5.3) so history survives a bad edit in Drive.
        """
        self.ensure_one()
        attachment = self.env['ir.attachment'].sudo().create({
            'name': filename,
            'raw': data,
            'res_model': 'gdrive.node',
            'res_id': self.id,
            'mimetype': mimetype,
            'type': 'binary',
        })
        vals = {field: attachment.id}
        if field == 'attachment_id':
            vals['attachment_checksum'] = attachment.checksum
        self.sudo().write(vals)
        self._prune_attachments()
        return attachment

    def _prune_attachments(self):
        """Keep at most :data:`KEEP_VERSIONS` historical attachments per node.

        The currently referenced attachments are never candidates for pruning,
        so this cannot orphan ``attachment_id`` no matter how the retention
        constant is tuned.
        """
        self.ensure_one()
        keep_ids = {self.attachment_id.id, self.text_attachment_id.id} - {False}
        attachments = self.env['ir.attachment'].sudo().search(
            [('res_model', '=', 'gdrive.node'), ('res_id', '=', self.id)],
            order='id desc')
        prunable = attachments.filtered(lambda a: a.id not in keep_ids)
        excess = prunable[KEEP_VERSIONS:]
        if excess:
            _logger.info("Pruning %d old attachment(s) from node %s.", len(excess), self.google_id)
            excess.unlink()
        return True

    # ------------------------------------------------------------------
    # UI actions
    # ------------------------------------------------------------------
    def action_open_in_drive(self):
        """Open the Drive deep link in a new tab."""
        self.ensure_one()
        if not self.web_view_link:
            raise UserError(_('This node has no Drive link recorded yet.'))
        return {'type': 'ir.actions.act_url', 'url': self.web_view_link, 'target': 'new'}

    def action_requeue(self):
        """Force a re-ingest of the selected nodes on the next ingest cron."""
        self.sudo().write({'state': 'queued', 'skip_reason': False, 'last_error': False})
        return True

    def action_open_datasets(self):
        """Open the tabs discovered inside this file."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Datasets'),
            'res_model': 'gdrive.dataset',
            'view_mode': 'list,form',
            'domain': [('node_id', '=', self.id)],
            'context': {'default_node_id': self.id},
        }


