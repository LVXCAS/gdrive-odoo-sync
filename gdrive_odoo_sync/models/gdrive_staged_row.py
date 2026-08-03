# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. See LICENSE (LGPL-3) for full copyright and licensing details.
"""``gdrive.staged.row`` — the schema-flexible landing zone (SPEC §3.7, §5.4).

Every row of every tab lands here, mapped or not. Staging is the default and is
never opt-in: a tab with no promotion mapping still gets a stable content hash,
a full ``payload`` archive and a place in the UI, and *that is already a
complete, correct outcome*.

Three rules in this file are absolute:

* **Whole-row quarantine.** One ``e:`` token anywhere in the row, or one empty
  ``required`` cell, quarantines the entire row. A half-written row is worse
  than an unwritten one: it looks like data, so nobody re-reads the source.
* **Json fields are never mutated in place.** ``fields.Json`` returns a deep
  copy on read, so ``rec.payload['k'] = v`` silently does nothing. Every write
  here assembles a fresh dict and assigns it whole.
* **Identity is never row position and never a hash of mutable content.** A
  user sorting the sheet would otherwise produce a full-dataset false drift, and
  a typo fix would otherwise read as a phantom delete plus a phantom create.
"""

import json
import logging

from odoo import _, api, fields, models

from ..lib.canon import CANON
from ..lib.hashing import H128, bucket_of, h_extra, h_row, h_row_folded, identity_key_bytes
from ..lib.text_canon import text_prepare
from .gdrive_sync_run import CREATE_BATCH

_logger = logging.getLogger(__name__)

#: Domain-separated prefix for the placeholder digest a quarantined row
#: contributes to the Merkle rollup. It commits to the row's *presence* without
#: asserting anything about its content, which is the only honest thing to say
#: about a row that holds an ``e:`` token.
QUARANTINE_DIGEST_PREFIX = b'gos1/quar\x00'

STATE_SELECTION = [
    ('staged', 'Staged'),
    ('quarantined', 'Quarantined'),
    ('promoted', 'Promoted'),
    ('missing', 'Missing'),
    ('obsolete', 'Obsolete'),
]

QUARANTINE_REASON_SELECTION = [
    ('type_coercion', 'Type Coercion'),
    ('duplicate_identity', 'Duplicate Identity'),
    ('multi_match', 'Multiple Matches'),
    ('missing_required', 'Missing Required Value'),
    ('identifier_numeric', 'Identifier Read As Number'),
    ('bad_bool', 'Unrecognized Boolean'),
    ('bad_date', 'Unparseable Date'),
    ('not_a_number', 'Not A Number'),
    ('error_cell', 'Spreadsheet Error Cell'),
    ('orphan_reference', 'Orphan Reference'),
    ('currency_mismatch', 'Currency Mismatch'),
    ('nonexistent_local_time', 'Nonexistent Local Time'),
]

#: Maps a lane-C ``e:`` error code onto the persisted quarantine reason. Kept
#: exhaustive against ``docs/CANONICALIZATION.md`` §2.1 so a new error code shows
#: up as an explicit KeyError-free fallback rather than being silently swallowed.
ERROR_TO_QUARANTINE = {
    'NOT_A_NUMBER': 'not_a_number',
    'NOT_FINITE': 'not_a_number',
    'BAD_DATE': 'bad_date',
    'BAD_BOOL': 'bad_bool',
    'CELL_ERROR': 'error_cell',
    'IDENTIFIER_NUMERIC': 'identifier_numeric',
    'UNRESOLVED_SELECTION': 'type_coercion',
    'ORPHAN_REFERENCE': 'orphan_reference',
    'NONEXISTENT_LOCAL_TIME': 'nonexistent_local_time',
    'TIME_COMPONENT_PRESENT': 'type_coercion',
    'MULTI_MATCH': 'multi_match',
    'CURRENCY_MISMATCH': 'currency_mismatch',
}


def jsonable(value):
    """Coerce a raw cell into something ``fields.Json`` can serialize.

    Sheets hands back ``str``/``float``/``bool``/``None`` and needs nothing.
    ``openpyxl`` hands back ``datetime`` and ``Decimal`` for some cells, which
    the Json column rejects. ``payload`` is the *archive of record*, so the
    conversion is to a lossless string form, never to ``None``.
    """
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return str(value)


class GdriveStagedRow(models.Model):
    """One row of one spreadsheet tab, canonicalized and hashed."""

    _name = 'gdrive.staged.row'
    _description = 'Google Drive Staged Row'
    _order = 'dataset_id, row_number'

    dataset_id = fields.Many2one(
        'gdrive.dataset', string='Dataset',
        required=True, index=True, ondelete='cascade',
    )
    connection_id = fields.Many2one(
        related='dataset_id.connection_id', store=True, index=True, string='Connection')
    company_id = fields.Many2one(
        related='dataset_id.company_id', store=True, index=True, string='Company')

    row_number = fields.Integer(
        string='Row', index=True,
        help='1-based sheet row. DISPLAY ONLY — never identity. A user sorting '
             'the sheet changes every row number and changes no data.',
    )
    a1_ref = fields.Char(
        string='A1 Reference',
        help="e.g. 'Wholesale — Leads'!A412. Every report cites this so a human "
             'can click through to the exact cell.',
    )
    sync_id = fields.Char(
        string='Sync Id', index=True,
        help='ULID. Present when the tab carries a _sync_id column, or '
             'backfilled by a natural-key match during promotion.',
    )
    natural_key = fields.Char(
        string='Natural Key', index=True,
        help='Hex of the length-prefixed canonical join of the declared key '
             'columns. Length-prefixed, so ("a|b","c") cannot collide with '
             '("a","b|c").',
    )
    identity_source = fields.Selection(
        [('sync_id', 'Sync Id'), ('natural_key', 'Natural Key'), ('none', 'None')],
        string='Identity Source', default='none', required=True, index=True,
        help="'none' means report-only: deletes are structurally disabled for "
             'this row because there is no stable way to say it disappeared.',
    )

    payload = fields.Json(string='Payload', help='{slug: raw_value} for every column. The archive of record.')
    canon = fields.Json(string='Canonical', help='{key: tagged_token} for the contract columns.')

    h_row = fields.Char(string='Row Hash', size=32, index=True)
    h_row_folded = fields.Char(string='Folded Row Hash', size=32,
                               help='Cosmetic-folded variant; drives COSMETIC drift classification.')
    h_extra = fields.Char(string='Extra Hash', size=32,
                          help='Digest of the columns outside the contract, so schema growth is '
                               'visible without polluting the compared hash.')
    bucket = fields.Integer(string='Bucket', index=True, help='0–255 Merkle bucket.')

    state = fields.Selection(STATE_SELECTION, string='Status', default='staged', required=True, index=True)
    quarantine_reason = fields.Selection(QUARANTINE_REASON_SELECTION, string='Quarantine Reason')
    quarantine_detail = fields.Text(string='Quarantine Detail')

    first_seen_date = fields.Datetime(string='First Seen')
    last_seen_date = fields.Datetime(string='Last Seen', index=True)
    missing_since = fields.Datetime(
        string='Missing Since', index=True,
        help='Set only on a COMPLETE read. Drives the delete quarantine window.',
    )

    promotion_link_id = fields.Many2one(
        'gdrive.promotion.link', string='Promotion Link', index=True, ondelete='set null')
    run_id = fields.Many2one('gdrive.sync.run', string='Last Run', index=True, ondelete='set null')

    payload_pretty = fields.Text(string='Payload (pretty)', compute='_compute_pretty')
    canon_pretty = fields.Text(string='Canonical (pretty)', compute='_compute_pretty')

    @api.depends('payload', 'canon')
    def _compute_pretty(self):
        """Render the Json columns for the form view.

        The Json fields themselves cannot be searched, grouped or sorted, and
        rendering them raw in a form is unreadable. A computed Text field is the
        supported way to show them; it is non-stored so it costs nothing.
        """
        for row in self:
            row.payload_pretty = json.dumps(row.payload or {}, indent=2, ensure_ascii=False, sort_keys=True)
            row.canon_pretty = json.dumps(row.canon or {}, indent=2, ensure_ascii=False, sort_keys=True)

    @api.depends('a1_ref', 'row_number', 'sync_id')
    def _compute_display_name(self):
        """Odoo 18 display name; ``name_get()`` is removed."""
        for row in self:
            row.display_name = row.a1_ref or (row.sync_id or _('row %s', row.row_number or 0))

    # ------------------------------------------------------------------
    # Staging
    # ------------------------------------------------------------------
    @api.model
    def _stage_dataset_rows(self, dataset, columns, data_rows, contracts, run,
                            tab_read_complete=None):
        """Canonicalize, hash, identify and persist every data row of one tab.

        Returns a dict with ``row_count``, ``quarantined`` and ``buckets``
        (``{bucket_index: [(identity_key_bytes, h_row_bytes)]}``) so the caller
        can roll the Merkle tree up without re-reading what it just wrote.

        The ordering of the steps matters: identity is computed from the
        *canonical* tokens (never the raw ones), duplicates are detected across
        the whole tab before anything is persisted, and quarantine decisions are
        made per row before the first ``create``.

        ``tab_read_complete`` is the per-tab ``read_complete`` flag produced by
        :class:`~..services.sheets_reader.SheetsReader` and
        :class:`~..services.xlsx_reader.XlsxReader`. Lane D must pass it: a tab
        the reader did not read reports zero rows, and without the flag that is
        indistinguishable from a tab whose rows were all deleted.
        """
        spec_version = contracts['spec_version']
        contract_cols = contracts['contract']
        extra_cols = contracts['extra']
        natural_key_keys = contracts['natural_key_keys']
        sync_id_index = contracts['sync_id_index']
        strategy = contracts['identity_strategy']
        slug_by_index = {c.col_index: c.slug for c in columns}
        first_data_row = dataset.first_data_row or 2
        now = fields.Datetime.now()

        prepared = []
        for offset, raw_row in enumerate(data_rows):
            row_number = first_data_row + offset
            payload = {}
            for index, slug in slug_by_index.items():
                payload[slug] = jsonable(raw_row[index]) if index < len(raw_row) else None

            canon_map = {}
            errors = []
            missing_required = []
            for key, contract, index in contract_cols:
                raw = raw_row[index] if 0 <= index < len(raw_row) else None
                token = CANON(raw, contract, 'sheet')
                canon_map[key] = token
                if token.startswith('e:'):
                    errors.append((key, token, raw))
                elif token == 'z:' and getattr(contract, 'required', False):
                    missing_required.append((key, raw))

            extra_map = {}
            for slug, contract, index in extra_cols:
                raw = raw_row[index] if 0 <= index < len(raw_row) else None
                extra_map[slug] = CANON(raw, contract, 'sheet')

            row_hash = h_row(canon_map, spec_version).hex()
            folded_hash = h_row_folded(canon_map, spec_version).hex()
            extra_hash = h_extra(extra_map, spec_version).hex()

            sync_id = ''
            if sync_id_index is not None and 0 <= sync_id_index < len(raw_row):
                # text_prepare, not str().strip(). str.strip() removes ASCII
                # whitespace and NBSP but leaves U+200B, U+FEFF, U+200D and
                # U+00AD in place — exactly the characters a browser copy-paste
                # injects. One of them in a _sync_id cell produced different
                # identity_key_bytes, a different bucket, no match in by_sync, a
                # brand-new staged row, and the previous row falling into
                # `vanished` with the delete-quarantine clock started on a
                # record still sitting in the sheet. Natural keys never had this
                # problem because they are built from canonical tokens; the
                # asymmetry was an oversight, not a design.
                sync_id = text_prepare(raw_row[sync_id_index])

            natural_key = ''
            if natural_key_keys and not errors:
                natural_key = identity_key_bytes(
                    [canon_map.get(key, 'z:') for key in natural_key_keys]).hex()

            if sync_id and strategy in ('sync_id', 'sync_id_then_key'):
                identity_source = 'sync_id'
                key_bytes = identity_key_bytes([sync_id])
            elif natural_key and strategy in ('natural_key', 'sync_id_then_key'):
                identity_source = 'natural_key'
                key_bytes = identity_key_bytes([canon_map.get(k, 'z:') for k in natural_key_keys])
            else:
                # No declared identity. The row is report-only and deletes are
                # structurally disabled for it. For the *content* rollup we still
                # need an order-insensitive key, so the row hash itself is used —
                # legitimate here precisely because it is never used to decide
                # that something disappeared.
                identity_source = 'none'
                key_bytes = identity_key_bytes(['h', row_hash])

            prepared.append({
                'row_number': row_number,
                'a1_ref': "'%s'!A%d" % ((dataset.tab_title or '').replace("'", "''"), row_number),
                'payload': payload,
                'canon': canon_map,
                'h_row': row_hash,
                'h_row_folded': folded_hash,
                'h_extra': extra_hash,
                'sync_id': sync_id,
                'natural_key': natural_key,
                'identity_source': identity_source,
                'key_bytes': key_bytes,
                'bucket': bucket_of(key_bytes),
                'errors': errors,
                'missing_required': missing_required,
            })

        self._flag_duplicate_identities(prepared, dataset, run)

        result = self._persist_rows(dataset, prepared, run, now, tab_read_complete)
        result['buckets'] = self._bucket_entries(prepared)
        return result

    @api.model
    def _flag_duplicate_identities(self, prepared, dataset, run):
        """Quarantine **every** member of a duplicated identity group.

        Never "pick the first one": with two rows claiming the same key, each run
        would nondeterministically choose one and the promoted record would
        alternate between the two rows' values forever, generating drift that
        never converges and that no amount of reading the report explains.
        """
        groups = {}
        for row in prepared:
            if row['identity_source'] == 'none':
                continue
            # Group on key_bytes — the value that actually decides bucket
            # membership and promotion — rather than on
            # (identity_source, sync_id or natural_key).
            groups.setdefault(('identity', row['key_bytes']), []).append(row)
            # And separately on the natural key, whatever the row's own identity
            # source. Under strategy 'sync_id_then_key' a row carrying a
            # _sync_id and a row without one can hold identical natural-key
            # values: the old grouping put them in ('sync_id', 'S1') and
            # ('natural_key', 'NK'), saw one member each, quarantined neither,
            # and then both resolved to the same stored record at match time.
            if row['natural_key']:
                groups.setdefault(('natural_key', row['natural_key']), []).append(row)

        flagged = set()
        for (source, value), members in groups.items():
            if len(members) < 2:
                continue
            printable = value.hex() if isinstance(value, (bytes, bytearray)) else value
            refs = ', '.join(m['a1_ref'] for m in members)
            for member in members:
                member['duplicate_detail'] = (
                    'Identity (%s = %s) is claimed by %d rows: %s'
                    % (source, printable, len(members), refs))
            if run and not all(id(m) in flagged for m in members):
                run._log('DUPLICATE_IDENTITY',
                         'Tab %r: %d rows share the identity %s=%s (%s). The whole group is quarantined.'
                         % (dataset.tab_title, len(members), source, printable, refs),
                         level='warning', stage='stage', dataset=dataset)
            flagged.update(id(m) for m in members)
        return prepared

    @api.model
    def _persist_rows(self, dataset, prepared, run, now, tab_read_complete=None):
        """Upsert the prepared rows and reconcile the ones that disappeared.

        Matching against what is already stored is **dispatched on the row's own
        ``identity_source``**: a ``sync_id`` row is looked up only in the
        sync-id index, a ``natural_key`` row only in the natural-key index, and
        a row with no declared identity only by ``row_number``. There is no
        fallback from one mechanism to another — that fallback let two sheet
        rows collapse onto one stored record. The ``row_number`` case is
        explicitly *not* an identity claim; it exists so a staging-only tab does
        not churn its whole table on every run, and such rows can never reach
        the delete planner because their ``identity_source`` is ``none``.

        :param tab_read_complete: the reader's per-tab ``read_complete`` flag,
            ``None`` when the caller did not distinguish per tab. ``False``
            forbids starting the missing clock even on a run that otherwise
            looks complete.
        """
        existing = self.sudo().search([('dataset_id', '=', dataset.id)])
        by_sync = {}
        by_natural = {}
        by_row = {}
        for row in existing:
            if row.sync_id:
                by_sync.setdefault(row.sync_id, []).append(row)
            if row.natural_key:
                by_natural.setdefault(row.natural_key, []).append(row)
            by_row.setdefault(row.row_number, row)

        matched_ids = set()

        def claim(index, key):
            """Return the first record under ``key`` no other row has claimed.

            Lists rather than ``setdefault(key, row)``, and a claimed-id check,
            because a stored record may legitimately carry *both* a sync_id and
            a natural_key: without this, two prepared rows could resolve to the
            same record, the second row's payload would overwrite the first's,
            and the record the second row should have matched would fall into
            ``vanished`` and have its delete-quarantine clock started while it
            was still sitting in the sheet.
            """
            for candidate in index.get(key) or ():
                if candidate.id not in matched_ids:
                    return candidate
            return None

        to_create = []
        staged = 0
        quarantined = 0

        for item in prepared:
            reason, detail = self._quarantine_decision(item)
            state = 'quarantined' if reason else 'staged'
            if reason:
                quarantined += 1
            else:
                staged += 1

            vals = {
                'dataset_id': dataset.id,
                'row_number': item['row_number'],
                'a1_ref': item['a1_ref'],
                'sync_id': item['sync_id'] or False,
                'natural_key': item['natural_key'] or False,
                'identity_source': item['identity_source'],
                'payload': item['payload'],
                'canon': item['canon'],
                'h_row': item['h_row'],
                'h_row_folded': item['h_row_folded'],
                'h_extra': item['h_extra'],
                'bucket': item['bucket'],
                'state': state,
                'quarantine_reason': reason or False,
                'quarantine_detail': detail or False,
                'last_seen_date': now,
                'missing_since': False,
                'run_id': run.id if run else False,
            }

            # Match on the identity the row actually claims — never fall through
            # from one identity mechanism to another. The old chain tried
            # by_sync then by_natural for every row, so a row whose identity is
            # its sync_id and a row whose identity is its natural key could both
            # land on one stored record that happened to carry both fields.
            if item['identity_source'] == 'sync_id':
                match = claim(by_sync, item['sync_id'])
            elif item['identity_source'] == 'natural_key':
                match = claim(by_natural, item['natural_key'])
            else:
                # No declared identity: row_number is a churn-avoidance
                # heuristic, explicitly not an identity claim. Such rows can
                # never reach the delete planner.
                match = by_row.get(item['row_number'])
                if match is not None and (
                        match.identity_source != 'none' or match.id in matched_ids):
                    match = None

            if match is None:
                vals['first_seen_date'] = now
                to_create.append(vals)
            else:
                matched_ids.add(match.id)
                # Reassign the Json columns whole. fields.Json returns a deep
                # copy on read, so in-place mutation is a silent no-op.
                changed = {k: v for k, v in vals.items()
                           if k != 'dataset_id' and _differs(match, k, v)}
                if match.state == 'promoted' and state == 'staged' and match.h_row == item['h_row']:
                    # Nothing about the row moved; leave the promotion state alone
                    # so the promoter's own bookkeeping is not stomped.
                    changed.pop('state', None)
                if changed:
                    match.sudo().write(changed)

        created = self.browse()
        for start in range(0, len(to_create), CREATE_BATCH):
            created |= self.sudo().create(to_create[start:start + CREATE_BATCH])

        # --- rows that were here last time and are not here now ----------------
        # Gated on the read being complete. A partial batchGet, an expired token
        # or a range that stopped at row 1000 all look exactly like "these rows
        # were deleted"; marking them missing on an incomplete read starts the
        # delete quarantine clock on data that never went anywhere.
        vanished = existing.filtered(lambda r: r.id not in matched_ids and r.state != 'obsolete')
        if vanished:
            # `run is None` used to take the *permissive* branch, so any caller
            # that stages without a run — a manual re-stage button, a repair
            # script, a fixture — started the delete quarantine clock with zero
            # evidence the read was complete. The safe answer to "I have no
            # proof" is the incomplete branch.
            #
            # tab_read_complete is the per-tab flag both readers already emit
            # (sheets_reader for a tab it did not read, xlsx_reader for a
            # worksheet with an uncached formula). None means the caller did not
            # distinguish per tab and the run-level flag stands; False is
            # honoured even when the run as a whole looks complete.
            if run is not None and run.complete_read and tab_read_complete is not False:
                vanished.sudo().write({'state': 'missing', 'missing_since': now})
                if run:
                    run._log('ROWS_MISSING',
                             '%d row(s) present in the previous read of %r are absent now.'
                             % (len(vanished), dataset.tab_title),
                             level='info', stage='stage', dataset=dataset)
            elif run:
                run._log('ROWS_MISSING_IGNORED',
                         '%d row(s) are absent from %r, but the read was incomplete, so the '
                         'missing clock was NOT started.' % (len(vanished), dataset.tab_title),
                         level='warning', stage='stage', dataset=dataset)

        if run:
            run._bump(rows_staged=staged, rows_quarantined=quarantined)
        return {
            'row_count': len(prepared),
            'staged': staged,
            'quarantined': quarantined,
            'created': len(created),
        }

    @api.model
    def _quarantine_decision(self, item):
        """Return ``(reason, detail)`` for one prepared row, or ``(None, None)``.

        Duplicates outrank cell errors in the report because a duplicate identity
        is a *structural* problem a human must resolve in the sheet, whereas a
        bad cell is usually a single typo.
        """
        if item.get('duplicate_detail'):
            return 'duplicate_identity', item['duplicate_detail']
        if item['errors']:
            key, token, raw = item['errors'][0]
            code = token[2:]
            detail_lines = [
                'Column %s produced %s from raw value %r at %s.' % (key, token, raw, item['a1_ref']),
            ]
            if len(item['errors']) > 1:
                detail_lines.append('Other affected columns: %s'
                                    % ', '.join(k for k, _t, _r in item['errors'][1:]))
            return ERROR_TO_QUARANTINE.get(code, 'type_coercion'), '\n'.join(detail_lines)
        if item['missing_required']:
            keys = ', '.join(k for k, _raw in item['missing_required'])
            return 'missing_required', 'Required column(s) empty at %s: %s' % (item['a1_ref'], keys)
        return None, None

    @api.model
    def _bucket_entries(self, prepared):
        """Group ``(identity_key_bytes, digest)`` by Merkle bucket.

        A quarantined row contributes a **sentinel** digest derived from its
        identity key alone, never its row hash. That is the only honest encoding
        of what is known about it: an ``e:`` token is never equal to anything,
        including a byte-identical ``e:`` token, so the row has no comparable
        content — but it does exist, and its existence has to be committed to.

        Dropping such rows entirely (the previous behaviour) made a tab with N
        quarantined rows and a tab where those N rows had been *deleted from the
        sheet* produce byte-identical dataset digests: ``merkle.dataset_digest``
        derives ``total_rows`` from the entries it is handed, so 1000 rows with
        100 quarantined and 900 rows after those 100 were deleted both hashed as
        ``total_rows=900`` over the same 900 entries. The Merkle fast path then
        found zero differing buckets and reported ``verified`` while 100 rows had
        physically disappeared and the corresponding Odoo records were queued for
        the delete planner with no drift record explaining why. ``h_dataset``'s
        contract is that a truncated read cannot produce a matching hash, and
        silently dropping rows is exactly a truncated read.
        """
        buckets = {}
        for item in prepared:
            if item.get('duplicate_detail') or item['errors'] or item['missing_required']:
                digest = H128(QUARANTINE_DIGEST_PREFIX + item['key_bytes'])
            else:
                digest = bytes.fromhex(item['h_row'])
            buckets.setdefault(item['bucket'], []).append((item['key_bytes'], digest))
        return buckets

    # ------------------------------------------------------------------
    # UI actions
    # ------------------------------------------------------------------
    def action_open_dataset(self):
        """Jump from a staged row to its tab."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Dataset'),
            'res_model': 'gdrive.dataset',
            'view_mode': 'form',
            'res_id': self.dataset_id.id,
        }

    def action_mark_obsolete(self):
        """Retire rows by hand so housekeeping may eventually prune them.

        Deliberately not an unlink: the row is the only surviving evidence of
        what the sheet said, and housekeeping's 30-day delay is what makes an
        accidental click recoverable.
        """
        self.sudo().write({'state': 'obsolete'})
        return True


def _differs(record, field_name, value) -> bool:
    """Compare a stored value with a candidate, tolerating ORM representations.

    ``fields.Json`` compares fine as plain Python, but empty values arrive as
    ``False`` from the ORM and as ``''``/``None`` from the builder, and a naive
    ``!=`` would then rewrite every row on every run — bumping ``write_date`` on
    the whole table and poisoning the L0b Odoo fast path in SPEC §9.1.
    """
    current = record[field_name]
    if isinstance(current, models.BaseModel):
        current = current.id or False
    if current in (False, None, '') and value in (False, None, ''):
        return False
    return current != value
