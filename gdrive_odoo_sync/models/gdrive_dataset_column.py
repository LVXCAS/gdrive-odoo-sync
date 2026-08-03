# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. See LICENSE (LGPL-3) for full copyright and licensing details.
"""``gdrive.dataset.column`` — the observed header schema of one tab (SPEC §3.6).

The join key between a sheet and everything downstream is ``header_canon``, the
tagged :func:`TEXT_CANON` form of the header label — **not** the physical column
index. That single choice is what makes column reordering a genuine no-op: a
user dragging column D to position A changes ``col_index`` and nothing else, so
no mapping breaks and no hash moves.

``observed_kind`` is **advisory only**. It is derived from the distribution of
values in the column and it is never, under any circumstance, used to decide how
to canonicalize a cell. Types come from the mapping contract, always. Per-cell
type sniffing is precisely the bug that makes the text ``"1"``, the number ``1``
and the boolean ``true`` compare equal, and it is forbidden by
``docs/CANONICALIZATION.md`` §3.
"""

import logging

from odoo import api, fields, models

from ..lib.contract import slugify
from ..lib.text_canon import TEXT_CANON

_logger = logging.getLogger(__name__)

OBSERVED_KIND_SELECTION = [
    ('empty', 'Empty'),
    ('text', 'Text'),
    ('number', 'Number'),
    ('bool', 'Boolean'),
    ('date_serial', 'Date Serial'),
    ('mixed', 'Mixed'),
    ('error', 'Error'),
]

#: How many raw cells to keep per column for the mapping-builder UI. Ten is
#: enough for a human to recognise "this is an SKU, tick assert_string_value"
#: and small enough that a 40-column sheet costs a few kilobytes of Json.
SAMPLE_LIMIT = 10

#: ``col_index`` of a column that exists as a record but is no longer present in
#: the sheet. Absent columns are retained rather than deleted so that
#: ``header_change`` remains detectable and a mapping that references them can
#: still be inspected — but they are excluded from every "live column" query.
ABSENT_COL_INDEX = -1


def default_text_contract_dict(slug: str, header_canon: str = '', odoo_field: str = None) -> dict:
    """Return the plain contract dict for an **unmapped** column.

    Every option is stated explicitly rather than left to a downstream default,
    because ``compute_spec_version`` hashes this dict: an option that is absent
    here today and present tomorrow silently changes ``spec_version`` and
    invalidates every cached hash in the database for no functional reason.

    WHY unmapped columns get a contract at all: SPEC §5.4 requires every tab —
    including ``Lucas_Clothing_Shopping_List`` — to produce a stable dataset
    hash. Without a default contract there would be nothing to canonicalize and
    staging-only datasets could never be verified.
    """
    return {
        'slug': slug,
        'header_canon': header_canon,
        'odoo_field': odoo_field or slug,
        'sequence': 10,
        'ctype': 'text',
        'required': False,
        'is_natural_key': False,
        'authority': 'report',
        'empty_is_null': True,
        'text_trim': True,
        'text_collapse_ws': True,
        'text_case': 'preserve',
        'fold_punct': False,
        'decimal_sep': '.',
        'group_sep': ',',
        'accounting_negatives': True,
        'percent_mode': 'none',
        'scale_mode': 'fixed',
        'scale': 2,
        'rel_tol': 0.0,
        'abs_tol': 0.0,
        'date_formats': '%Y-%m-%d,%m/%d/%Y',
        'truthy': 'true,yes,y,1,x,✓',
        'falsy': 'false,no,n,0',
        'empty_means': 'false',
        'value_map': {},
        'comodel': False,
        'm2o_match_field': 'name',
        'm2o_create_missing': False,
        'assert_string_value': False,
    }


def a1_letter_for(index: int) -> str:
    """Convert a 0-based column index into its A1 letter (0→A, 26→AA).

    Spreadsheet columns are base-26 *bijective*, not base-26 positional: there
    is no zero digit, which is why the loop subtracts one before each division.
    Getting this wrong shows up first at column 27 — well past where anyone
    tests by hand.
    """
    if index < 0:
        return ''
    letters = ''
    n = index + 1
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        letters = chr(ord('A') + remainder) + letters
    return letters


def observed_kind_for(values) -> str:
    """Classify a column's raw sample. **Advisory only — never canonicalizing.**

    Returned for the mapping-builder UI so a human can see "this looks numeric"
    before choosing a ``ctype``. The value is deliberately never consulted by
    :func:`CANON`.
    """
    kinds = set()
    for value in values:
        if value is None or value == '':
            continue
        if isinstance(value, bool):
            kinds.add('bool')
        elif isinstance(value, (int, float)):
            # A Sheets serial for a date is indistinguishable from a plain
            # number without a number format, which is exactly why this is
            # advisory. The 20000–60000 window covers 1954-2064.
            kinds.add('date_serial' if 20000 <= float(value) <= 60000 else 'number')
        elif isinstance(value, str):
            kinds.add('error' if value.startswith('#') and value.endswith(('!', '?', 'A', '0')) else 'text')
        else:
            kinds.add('text')
    if not kinds:
        return 'empty'
    if len(kinds) == 1:
        return kinds.pop()
    if kinds == {'number', 'date_serial'}:
        return 'number'
    return 'mixed'


class GdriveDatasetColumn(models.Model):
    """One observed header of one spreadsheet tab."""

    _name = 'gdrive.dataset.column'
    _description = 'Google Drive Dataset Column'
    _order = 'dataset_id, col_index'

    dataset_id = fields.Many2one(
        'gdrive.dataset', string='Dataset',
        required=True, index=True, ondelete='cascade',
    )
    col_index = fields.Integer(
        string='Index', required=True, default=0,
        help='0-based physical position, or -1 when the column is no longer '
             'present in the sheet. Advisory: matching is by header_canon, so '
             'reordering columns is a no-op.',
    )
    a1_letter = fields.Char(string='A1 Letter')
    header_raw = fields.Char(string='Header (raw)')
    header_canon = fields.Char(
        string='Header (canonical)', index=True,
        help='TEXT_CANON output, tagged (e.g. "s:Invoice Number"). THE join key.',
    )
    slug = fields.Char(
        string='Slug', required=True, index=True, default='column',
        help='^[a-z_][a-z0-9_]*$, derived from header_canon and deduplicated '
             'with _2/_3. Used as the key inside gdrive.staged.row.payload, and '
             'as a JCS key, which is why the charset is restricted.',
    )
    sample_values = fields.Json(string='Samples', help='Up to 10 raw cells, for the mapping builder UI.')
    observed_kind = fields.Selection(
        OBSERVED_KIND_SELECTION, string='Observed Kind', default='empty',
        help='ADVISORY ONLY. Never used to canonicalize — types come from the '
             'mapping contract, always.',
    )
    nonempty_count = fields.Integer(string='Non-empty Cells', aggregator='sum')
    distinct_count = fields.Integer(string='Distinct Values', aggregator='sum')
    is_mapped = fields.Boolean(
        string='Mapped', compute='_compute_is_mapped', store=True, index=True,
        help='True when a gdrive.mapping.column references this header_canon.',
    )

    _sql_constraints = [
        ('col_slug_uniq', 'unique(dataset_id, slug)', 'Column slug must be unique in a tab.'),
    ]

    @api.depends('header_canon', 'dataset_id.mapping_id', 'dataset_id.mapping_id.column_ids.header_canon')
    def _compute_is_mapped(self):
        """Stored flag: does the dataset's mapping reference this header?

        Stored so views can filter on it. The dependency reaches across into
        lane E's ``gdrive.mapping.column`` by field path only — no import — which
        keeps the lane boundary intact while still triggering recomputation when
        a mapping column is edited.
        """
        for column in self:
            mapping = column.dataset_id.mapping_id
            if not mapping or not column.header_canon:
                column.is_mapped = False
                continue
            column.is_mapped = any(
                mc.header_canon == column.header_canon and mc.ctype != 'ignore'
                for mc in mapping.column_ids
            )

    @api.depends('header_raw', 'slug', 'col_index')
    def _compute_display_name(self):
        """Odoo 18 display name: ``B · Invoice Number``."""
        for column in self:
            letter = column.a1_letter or a1_letter_for(column.col_index)
            label = column.header_raw or column.slug or ''
            column.display_name = ('%s · %s' % (letter, label)).strip(' ·')

    # ------------------------------------------------------------------
    # Header upsert
    # ------------------------------------------------------------------
    @api.model
    def _upsert_headers(self, dataset, header_cells, data_rows):
        """Reconcile the stored schema of ``dataset`` with the headers just read.

        Matching is on ``header_canon`` so a reordered sheet reuses its existing
        column records (and therefore keeps its mapping intact). Columns that
        vanished are **retained** with ``col_index = -1`` rather than deleted:
        deleting them would cascade-delete the mapping columns that reference
        them, turning a transient bad read into permanent loss of an
        administrator's hand-authored contract.

        Returns the ordered recordset of *live* columns (left to right).
        """
        header_contract = dataset._header_contract()
        existing = self.sudo().search([('dataset_id', '=', dataset.id)])
        by_canon = {}
        for column in existing:
            by_canon.setdefault(column.header_canon or '', column)
        taken_slugs = {c.slug for c in existing}

        live_ids = []
        occurrences = {}
        blank_count = 0
        for index, raw in enumerate(header_cells):
            canon = TEXT_CANON(raw, header_contract)
            if canon == 'z:':
                # An unlabelled column still holds data and still has to appear
                # in payload and h_extra, so it gets a surrogate label rather
                # than being dropped. The surrogate is an *occurrence ordinal*,
                # not the A1 letter: 's:column_C' renamed itself to 's:column_D'
                # the moment anyone inserted a column to its left, so no
                # existing record matched, a new one was created, the old one
                # was marked absent, and every row's h_extra changed with no
                # data change at all.
                blank_count += 1
                canon = 's:column#%d' % blank_count
            seen = occurrences.get(canon, 0) + 1
            occurrences[canon] = seen
            if seen > 1:
                # Two columns carrying the same label. Disambiguate by
                # first-seen ordinal rather than by current position: the A1
                # letter made 's:Amount (D)' rename itself on any insertion to
                # its left, and swapping two identically-labelled columns made
                # each physical column resolve to the *other* column's record —
                # so both records swapped their slug and their mapping binding
                # and every row in the tab reported drift after a pure reorder,
                # which this module's own contract promises is a no-op.
                #
                # The while loop covers the pathological tab that *literally*
                # contains a header spelled 'Amount#2': the derived name must
                # never steal an ordinal a real header already claimed.
                ordinal = seen
                candidate = '%s#%d' % (canon, ordinal)
                while candidate in occurrences:
                    ordinal += 1
                    candidate = '%s#%d' % (canon, ordinal)
                canon = candidate
                occurrences[canon] = 1

            samples = []
            nonempty = 0
            distinct = set()
            for row in data_rows:
                if index >= len(row):
                    continue
                value = row[index]
                if value is None or value == '':
                    continue
                nonempty += 1
                distinct.add(repr(value))
                if len(samples) < SAMPLE_LIMIT:
                    samples.append(value)

            column = by_canon.get(canon)
            vals = {
                'dataset_id': dataset.id,
                'col_index': index,
                'a1_letter': a1_letter_for(index),
                'header_raw': ('' if raw is None else str(raw))[:512],
                'header_canon': canon,
                'sample_values': [_jsonable_sample(s) for s in samples],
                'observed_kind': observed_kind_for(samples),
                'nonempty_count': nonempty,
                'distinct_count': len(distinct),
            }
            if column:
                changed = {k: v for k, v in vals.items()
                           if k != 'dataset_id' and column[k] != v}
                if changed:
                    column.sudo().write(changed)
            else:
                slug = _unique_slug(canon, taken_slugs)
                taken_slugs.add(slug)
                vals['slug'] = slug
                column = self.sudo().create(vals)
                by_canon[canon] = column
            live_ids.append(column.id)

        # Columns no longer present: mark absent, never delete.
        gone = existing.filtered(lambda c: c.id not in live_ids and c.col_index != ABSENT_COL_INDEX)
        if gone:
            gone.sudo().write({'col_index': ABSENT_COL_INDEX, 'a1_letter': False})
            _logger.info("Dataset %s: %d column(s) disappeared from the sheet and were marked absent.",
                         dataset.id, len(gone))
        return self.browse(live_ids)

    @api.model
    def _live(self, dataset):
        """Return the columns currently present in the sheet, left to right."""
        return self.sudo().search(
            [('dataset_id', '=', dataset.id), ('col_index', '>=', 0)], order='col_index')


def _unique_slug(header_canon: str, taken: set) -> str:
    """Slugify ``header_canon`` and disambiguate against ``taken``.

    Lane C's :func:`slugify` is a pure single-argument function by contract, so
    deduplication cannot live there — it needs the set of slugs already assigned
    within the tab. The suffix sequence (``_2``, ``_3``, …) is deterministic, so
    re-reading the same header row twice yields the same slugs and therefore the
    same JCS keys and the same row hashes.
    """
    base = slugify(header_canon) or 'column'
    if base not in taken:
        return base
    suffix = 2
    while '%s_%d' % (base, suffix) in taken:
        suffix += 1
    return '%s_%d' % (base, suffix)


def _jsonable_sample(value):
    """Coerce a raw cell into something ``fields.Json`` can store.

    ``openpyxl`` hands back ``datetime`` objects for date-formatted cells; the
    Json column would raise on them. Samples are for human eyes only, so an ISO
    string is a faithful representation.
    """
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return str(value)
