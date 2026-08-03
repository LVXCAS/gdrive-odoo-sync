# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. See LICENSE (LGPL-3) for full copyright and licensing details.
"""``gdrive.mapping.column`` — the per-column type coercion contract (SPEC §3.9).

**There is no type sniffing anywhere in this system.** The type of a cell comes
from ``ctype``, always. That is not a stylistic preference; it is the only
defensible design, because per-cell inference is *provably* ambiguous:

* ``"1"`` is legitimately the text ``1``, the number ``1.00`` and the boolean
  true, in three different columns, in the same spreadsheet.
* ``"1.234"`` is 1234 in de-DE and 1.234 in en-US, and no heuristic can tell.
* ``"03/04/2026"`` is March 4th or April 3rd, and no heuristic can tell.
* ``"maybe"`` is not false.

So every one of those decisions is **declared** here by a human, carried into
``lib.contract.ColumnContract`` by :meth:`to_contract_dict`, and consumed by the
pure canonicalization library, which never queries Odoo.

``assert_string_value`` deserves its own paragraph. Google Sheets returns an
``effectiveValue`` oneof; a cell containing ``00123`` comes back as the *number*
123 with its leading zeros already gone, and a 16-digit account number comes back
as an IEEE-754 double that has already lost its last digit. Ticking this flag on
every SKU, barcode, invoice number, phone number, account number and postal code
column turns that silent corruption into an ``IDENTIFIER_NUMERIC`` quarantine —
a loud refusal instead of a plausible wrong answer.
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from ..lib.contract import (
    DEFAULT_DATE_FORMATS,
    DEFAULT_FALSY,
    DEFAULT_TRUTHY,
    slugify,
)

_logger = logging.getLogger(__name__)

CTYPE_SELECTION = [
    ('text', 'Text'),
    ('number', 'Number'),
    ('money', 'Money'),
    ('bool', 'Boolean'),
    ('date', 'Date'),
    ('datetime', 'Datetime'),
    ('selection', 'Selection'),
    ('many2one', 'Many2one'),
    ('m2m', 'Many2many'),
    ('ignore', 'Ignore'),
]

AUTHORITY_SELECTION = [
    ('sheet', 'Sheet (the sheet wins)'),
    ('odoo', 'Odoo (report only)'),
    ('report', 'Report only'),
]

#: Odoo field types that can hold each ``ctype``. Checked at save time rather
#: than at promotion time so a mistyped contract fails while the author is
#: looking at it, instead of inside a cron three hours later.
CTYPE_TO_ODOO_TYPES = {
    'text': {'char', 'text', 'html'},
    'number': {'integer', 'float', 'monetary'},
    'money': {'float', 'monetary'},
    'bool': {'boolean'},
    'date': {'date'},
    'datetime': {'datetime'},
    'selection': {'selection'},
    'many2one': {'many2one'},
    'm2m': {'many2many', 'one2many'},
}

DEFAULT_DATE_FORMATS_CSV = ','.join(DEFAULT_DATE_FORMATS)
DEFAULT_TRUTHY_CSV = ','.join(DEFAULT_TRUTHY)
DEFAULT_FALSY_CSV = ','.join(DEFAULT_FALSY)


class GdriveMappingColumn(models.Model):
    """One column's declared canonicalization and write behaviour."""

    _name = 'gdrive.mapping.column'
    _description = 'Google Drive Mapping Column'
    _order = 'mapping_id, sequence, id'

    mapping_id = fields.Many2one(
        'gdrive.mapping', string='Mapping',
        required=True, index=True, ondelete='cascade',
    )
    sequence = fields.Integer(
        string='Sequence', default=10,
        help='Also the order in which natural-key columns are joined. Reordering '
             'key columns changes every natural key in the tab, which is why the '
             'order is explicit and draggable rather than implicit.',
    )
    target_model_id = fields.Many2one(
        related='mapping_id.target_model_id', string='Target Model', store=True, readonly=True,
        help='Denormalized so the odoo_field_id domain can be a plain field '
             'reference instead of a parent lookup that breaks in list views.',
    )
    dataset_id = fields.Many2one(
        related='mapping_id.dataset_id', string='Dataset', store=True, index=True, readonly=True)

    dataset_column_id = fields.Many2one(
        'gdrive.dataset.column', string='Sheet Column', ondelete='set null',
        domain="[('dataset_id', '=', dataset_id)]",
        help='UI convenience only. The DURABLE link is header_canon: a column '
             'record can be recreated by a re-read, and the contract must survive it.',
    )
    header_canon = fields.Char(
        string='Canonical Header', required=True, index=True,
        help='The tagged TEXT_CANON form of the sheet header, e.g. s:Invoice Number. '
             'This is the matching key, so a physical column reorder is a no-op.',
    )

    odoo_field_id = fields.Many2one(
        'ir.model.fields', string='Odoo Field', ondelete='cascade',
        domain="[('model_id', '=', target_model_id), ('store', '=', True)]",
        help='Empty means ctype must be ignore. Domain-filtered to stored fields '
             'of the target model, because a non-stored compute cannot be written '
             'and cannot be searched.',
    )
    odoo_field = fields.Char(
        related='odoo_field_id.name', store=True, string='Field Name', index=True,
        help='THE HASH KEY. Rows hash by Odoo field name rather than by sheet '
             'header, which is what makes a cosmetic header rename harmless.',
    )
    odoo_field_type = fields.Selection(
        related='odoo_field_id.ttype', string='Field Type', readonly=True,
        help='Carried into the contract so lane C can tell an empty Char from '
             'boolean false without inspecting the value.',
    )

    ctype = fields.Selection(
        CTYPE_SELECTION, string='Contract Type', required=True, default='text',
        help='DECLARED, never inferred. The same raw value "1" is legitimately '
             'text, a number and a boolean in three different columns.',
    )
    required = fields.Boolean(
        string='Required', default=False,
        help='An empty or invalid required cell quarantines the WHOLE row. A '
             'half-written row is worse than an unwritten one: it looks like data.',
    )
    is_natural_key = fields.Boolean(
        string='Natural Key', default=False,
        help='Ordered by sequence to form the composite identity key.',
    )
    authority = fields.Selection(
        AUTHORITY_SELECTION, string='Authority', default='sheet', required=True,
        help='Exactly one authority per column, so the write direction is never '
             'ambiguous. v1 writes only sheet-authority columns; odoo and report '
             'are compared and reported, never written.',
    )
    empty_is_null = fields.Boolean(
        string='Empty Is Null', default=True,
        help='When on, an empty cell canonicalizes to the null token rather than '
             'to an empty string, so "" and "absent" compare equal.',
    )

    # --- text -----------------------------------------------------------
    text_trim = fields.Boolean(string='Trim', default=True)
    text_collapse_ws = fields.Boolean(
        string='Collapse Whitespace', default=True,
        help='Leave on for names and labels. Turn it OFF for notes, code and '
             'address blocks, where runs of spaces are meaningful.',
    )
    text_case = fields.Selection(
        [('preserve', 'Preserve'), ('fold', 'Casefold')], string='Case', default='preserve',
        help="'fold' uses casefold(), not lower(): lower() does not fold ß to ss "
             'and would report two equal strings as different.',
    )
    fold_punct = fields.Boolean(
        string='Fold Punctuation', default=False,
        help='Affects the COSMETIC hash only, never the strict one. Folding smart '
             'quotes in the primary form would hide real edits.',
    )

    # --- numbers --------------------------------------------------------
    decimal_sep = fields.Char(
        string='Decimal Separator', size=1, default='.',
        help='DECLARED, NEVER GUESSED. "1.234" is 1234 in de-DE and 1.234 in en-US.',
    )
    group_sep = fields.Char(string='Group Separator', size=1, default=',')
    accounting_negatives = fields.Boolean(
        string='Accounting Negatives', default=True, help='(1,234.50) reads as -1234.50.')
    percent_mode = fields.Selection(
        [('none', 'None'), ('divide_100', 'Divide by 100')], string='Percent Mode', default='none')
    scale_mode = fields.Selection(
        [('currency', 'Currency precision'), ('uom', 'UoM precision'), ('fixed', 'Fixed')],
        string='Scale Mode', default='fixed', required=True,
    )
    scale = fields.Integer(string='Scale', default=2, help="Used when scale_mode is 'fixed'.")
    currency_field_id = fields.Many2one(
        'ir.model.fields', string='Currency Field', ondelete='set null',
        domain="[('model_id', '=', target_model_id), ('relation', '=', 'res.currency')]",
        help='Companion currency field on the Odoo record, for money columns.',
    )
    default_currency_id = fields.Many2one(
        'res.currency', string='Default Currency', ondelete='set null',
        help='Used when the record carries no companion currency field.',
    )
    rel_tol = fields.Float(
        string='Relative Tolerance', default=0.0, digits=(16, 12),
        help='Derived-float tolerance. Used ONLY to downgrade a difference to '
             'ROUNDING in the L3 drill-down; never to declare two values equal.',
    )
    abs_tol = fields.Float(string='Absolute Tolerance', default=0.0, digits=(16, 12))

    # --- dates ----------------------------------------------------------
    date_formats = fields.Char(
        string='Date Formats', default=DEFAULT_DATE_FORMATS_CSV,
        help='Strict strptime patterns, tried in order. NO FUZZY PARSING, EVER: '
             'a guesser silently corrupts a year of data and never says so.',
    )

    # --- booleans -------------------------------------------------------
    truthy = fields.Char(string='Truthy Tokens', default=DEFAULT_TRUTHY_CSV)
    falsy = fields.Char(string='Falsy Tokens', default=DEFAULT_FALSY_CSV)
    empty_means = fields.Selection(
        [('false', 'False'), ('null', 'Null'), ('error', 'Error')],
        string='Empty Means', default='false', required=True,
        help='An unticked checkbox column means false; an optional flag column '
             'usually means null. Choosing wrong makes every blank cell drift.',
    )

    # --- enumerations and relations -------------------------------------
    value_map = fields.Json(
        string='Value Map',
        help='{sheet label: Odoo technical key}. Selections compare TECHNICAL '
             'KEYS, never labels, because labels are translated.',
    )
    comodel = fields.Char(string='Comodel', help='For many2one / m2m columns.')
    m2o_match_field = fields.Char(
        string='Match Field', default='name',
        help='Field on the comodel used to resolve the sheet value.',
    )
    m2o_create_missing = fields.Boolean(
        string='Create Missing', default=False,
        help='OFF by default. An unresolvable reference becomes an '
             'ORPHAN_REFERENCE quarantine rather than an invented record.',
    )

    # --- identifier discipline ------------------------------------------
    assert_string_value = fields.Boolean(
        string='Is Identifier', default=False,
        help='Assert the Sheets effectiveValue oneof is stringValue; raise '
             'IDENTIFIER_NUMERIC otherwise. SET THIS TRUE on every SKU, barcode, '
             'invoice number, phone number, account number and postal code '
             'column: a 16-digit account number read as a double has already '
             'lost its last digit before anything in Odoo sees it.',
    )

    # ------------------------------------------------------------------
    # Derived helpers
    # ------------------------------------------------------------------
    @property
    def contract_key(self) -> str:
        """The JCS key this column contributes to the row payload.

        ``odoo_field`` when mapped, else the sheet column's slug. Keying by the
        Odoo field name is what makes the row hash invariant to column reordering
        *and* to cosmetic header renames at the same time.
        """
        self.ensure_one()
        return self.odoo_field or self.dataset_column_id.slug or slugify(self.header_canon or '')

    @api.depends('header_canon', 'odoo_field', 'ctype')
    def _compute_display_name(self):
        """Odoo 18: ``name_get()`` is removed and silently never called."""
        for column in self:
            header = (column.header_canon or '').removeprefix('s:')
            target = column.odoo_field or _('unmapped')
            column.display_name = '%s → %s (%s)' % (header, target, column.ctype or '')

    # ------------------------------------------------------------------
    # Onchange — suggestions only, never silent coercion
    # ------------------------------------------------------------------
    @api.onchange('dataset_column_id')
    def _onchange_dataset_column_id(self):
        """Copy the canonical header across when a sheet column is picked.

        Only ever *fills in* ``header_canon``; it never rewrites one the user
        already set, because ``header_canon`` is the durable link and silently
        re-pointing it would make every row read a different column.
        """
        for column in self:
            if column.dataset_column_id and not column.header_canon:
                column.header_canon = column.dataset_column_id.header_canon

    @api.onchange('ctype')
    def _onchange_ctype(self):
        """Warn when the declared ctype cannot fit the chosen Odoo field.

        A warning rather than a reset: the user may be mid-edit and about to
        change the field too, and silently clearing their work is worse than
        telling them.
        """
        self.ensure_one()
        allowed = CTYPE_TO_ODOO_TYPES.get(self.ctype)
        ttype = self.odoo_field_id.ttype
        if allowed and ttype and ttype not in allowed:
            return {'warning': {
                'title': _('Type mismatch'),
                'message': _(
                    'Column type %(c)s cannot be written to %(f)s, which is a %(t)s field. '
                    'Validation will refuse this mapping.',
                    c=self.ctype, f=self.odoo_field_id.name, t=ttype),
            }}
        return None

    # ------------------------------------------------------------------
    # Constraints
    # ------------------------------------------------------------------
    @api.constrains('ctype', 'odoo_field_id', 'authority')
    def _check_field_binding(self):
        """A non-ignored column must name a field, and the field must fit.

        Enforced here, at save time, because the alternative is discovering it in
        the middle of promoting forty thousand rows.
        """
        for column in self:
            if column.ctype == 'ignore':
                continue
            if not column.odoo_field_id:
                raise ValidationError(_(
                    'Column %s has no Odoo field. Set one, or set its type to '
                    "'ignore' to exclude it from the contract.", column.header_canon or ''))
            allowed = CTYPE_TO_ODOO_TYPES.get(column.ctype)
            ttype = column.odoo_field_id.ttype
            if allowed and ttype and ttype not in allowed:
                raise ValidationError(_(
                    'Column %(h)s is declared %(c)s but %(f)s is a %(t)s field. Coercing '
                    'between them would silently change what the data means.',
                    h=column.header_canon or '', c=column.ctype,
                    f=column.odoo_field_id.name, t=ttype))

    @api.constrains('decimal_sep', 'group_sep', 'ctype')
    def _check_separators(self):
        """Refuse an unparseable numeric contract.

        Identical separators make every value in the column ambiguous, and the
        canonicalizer would return an error token for every single cell — which
        looks like "the data is broken" rather than "the contract is broken".
        """
        for column in self:
            if column.ctype not in ('number', 'money'):
                continue
            if len(column.decimal_sep or '') != 1:
                raise ValidationError(_('Column %s must declare exactly one decimal separator '
                                        'character; it is never guessed.', column.header_canon or ''))
            if column.group_sep and column.group_sep == column.decimal_sep:
                raise ValidationError(_('Column %s uses the same character for the decimal and '
                                        'group separators; every value would be unparseable.',
                                        column.header_canon or ''))

    @api.constrains('truthy', 'falsy', 'ctype')
    def _check_boolean_tokens(self):
        """A token cannot be both true and false.

        Overlap resolves by whichever list is consulted first, which is an
        implementation detail nobody should have to know — so it is refused.
        """
        for column in self:
            if column.ctype != 'bool':
                continue
            truthy = {t.strip().casefold() for t in (column.truthy or '').split(',') if t.strip()}
            falsy = {f.strip().casefold() for f in (column.falsy or '').split(',') if f.strip()}
            if not truthy or not falsy:
                raise ValidationError(_('Boolean column %s must declare both truthy and falsy tokens.',
                                        column.header_canon or ''))
            overlap = truthy & falsy
            if overlap:
                raise ValidationError(_('Column %(h)s lists %(v)s as both truthy and falsy.',
                                        h=column.header_canon or '', v=', '.join(sorted(overlap))))

    @api.constrains('value_map', 'ctype')
    def _check_value_map(self):
        """A selection column needs a non-empty, string-keyed map."""
        for column in self:
            if column.ctype != 'selection':
                continue
            mapping = column.value_map or {}
            if not isinstance(mapping, dict) or not mapping:
                raise ValidationError(_('Selection column %s needs a value map; without one every '
                                        'sheet label fails to resolve.', column.header_canon or ''))
            bad = [k for k, v in mapping.items() if not isinstance(k, str) or not isinstance(v, str)]
            if bad:
                raise ValidationError(_('Value map of column %(h)s has non-string entries: %(v)s. '
                                        'Selections compare technical keys, which are strings.',
                                        h=column.header_canon or '', v=', '.join(map(str, bad))))

    @api.constrains('scale', 'scale_mode', 'ctype')
    def _check_scale(self):
        """A negative or absurd scale is a contract error, not a data error."""
        for column in self:
            if column.ctype in ('number', 'money') and column.scale_mode == 'fixed':
                if column.scale < 0 or column.scale > 12:
                    raise ValidationError(_('Column %s declares a fixed scale outside 0–12.',
                                            column.header_canon or ''))

    # ------------------------------------------------------------------
    # ORM overrides — every edit here moves spec_version
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        """Create the column, then invalidate everything keyed by the contract."""
        columns = super().create(vals_list)
        columns.mapping_id._on_contract_changed('column added')
        return columns

    def write(self, vals):
        """Write, then invalidate every hash computed under the old contract.

        The parent's ``write`` is never called when a child row changes, so the
        invalidation has to be triggered from here. Skipping it leaves stored
        digests that were computed under different coercion rules looking like
        valid cache hits — a green dashboard over data nobody compared.
        """
        mappings = self.mapping_id
        result = super().write(vals)
        # Both the old and the new parent: re-parenting a column changes two
        # contracts, and only invalidating the destination would leave the source
        # mapping's cached hashes claiming a column it no longer has.
        (mappings | self.mapping_id)._on_contract_changed('column edited')
        return result

    def unlink(self):
        """Drop the column and invalidate the contract it was part of."""
        mappings = self.mapping_id
        result = super().unlink()
        mappings.exists()._on_contract_changed('column removed')
        return result

    # ------------------------------------------------------------------
    # Serialization for lane C
    # ------------------------------------------------------------------
    def to_contract_dict(self, dataset_column=None) -> dict:
        """Serialize this column into ``lib.contract.ColumnContract`` keyword form.

        Everything the pure library needs must be *resolved here*, because lane C
        is forbidden from touching the ORM:

        * ``resolved_scale`` — the integer precision behind ``scale_mode`` of
          ``currency`` or ``uom``. An unresolved scale is a contract error there,
          not a silent fallback to two decimals: quantizing a three-decimal
          currency to two makes every subsequent ``verified`` a lie.
        * ``sheet_timezone`` — the tab's IANA name. A spreadsheet has no
          timezone; guessing one shifts every datetime by hours.
        * ``odoo_field_type`` — so ``False`` can be read as "empty Char" versus
          "boolean false" without inspecting the value.

        ``dataset_column`` may be passed by the caller when it has already
        resolved the physical column, saving a lookup per column per run.
        """
        self.ensure_one()
        physical = dataset_column if dataset_column is not None else self.dataset_column_id
        key = self.odoo_field or (physical.slug if physical else '') or slugify(self.header_canon or '')

        return {
            'key': key,
            'header_canon': self.header_canon or '',
            'odoo_field': self.odoo_field or '',
            'slug': (physical.slug if physical else '') or key,
            'ctype': self.ctype,
            'odoo_field_type': self.odoo_field_id.ttype or '',
            'sequence': self.sequence or 10,
            'col_index': (physical.col_index if physical else 0) or 0,

            'required': self.required,
            'is_natural_key': self.is_natural_key,
            'authority': self.authority,
            'empty_is_null': self.empty_is_null,

            'text_trim': self.text_trim,
            'text_collapse_ws': self.text_collapse_ws,
            'text_case': self.text_case,
            'fold_punct': self.fold_punct,

            'decimal_sep': self.decimal_sep or '.',
            'group_sep': self.group_sep or '',
            'accounting_negatives': self.accounting_negatives,
            'percent_mode': self.percent_mode,
            'scale_mode': self.scale_mode,
            'scale': self.scale,
            'resolved_scale': self._resolved_scale(),
            'currency_code': self.default_currency_id.name or '',
            'currency_symbols': self._currency_symbols(),
            'rel_tol': self.rel_tol,
            'abs_tol': self.abs_tol,

            'date_formats': self.date_formats or DEFAULT_DATE_FORMATS_CSV,
            'sheet_timezone': self.mapping_id.dataset_id.sheet_timezone or '',

            'truthy': self.truthy or DEFAULT_TRUTHY_CSV,
            'falsy': self.falsy or DEFAULT_FALSY_CSV,
            'empty_means': self.empty_means,

            'value_map': dict(self.value_map or {}),
            'comodel': self.comodel or (self.odoo_field_id.relation or ''),
            'm2o_match_field': self.m2o_match_field or 'name',
            'm2o_create_missing': self.m2o_create_missing,

            'assert_string_value': self.assert_string_value,
        }

    def _resolved_scale(self):
        """Resolve ``scale_mode`` into the integer precision lane C will apply.

        Returns ``None`` for ``fixed`` (the declared ``scale`` is used) and for
        an unresolvable currency/uom, so the library raises its explicit contract
        error instead of quietly rounding to two places.
        """
        self.ensure_one()
        if self.scale_mode == 'fixed':
            return None
        if self.scale_mode == 'currency':
            currency = self.default_currency_id or self._currency_from_field()
            if currency:
                return currency.decimal_places
            _logger.warning(
                "Column %s declares currency precision but resolves no currency; "
                "lane C will refuse it rather than guess a scale.", self.display_name)
            return None
        if self.scale_mode == 'uom':
            try:
                precision = self.env['decimal.precision'].precision_get('Product Unit of Measure')
            except Exception:
                _logger.exception("Could not read the 'Product Unit of Measure' decimal precision "
                                  "for column %s.", self.display_name)
                return None
            return precision
        return None

    def _currency_from_field(self):
        """Best-effort currency behind ``currency_field_id``.

        Only the *company* default is resolvable without a concrete record — the
        per-record currency is read at promotion time. Used purely to derive a
        scale; when it cannot be resolved the contract refuses rather than
        assuming two decimals.
        """
        self.ensure_one()
        if not self.currency_field_id:
            return self.env['res.currency']
        return self.env.company.currency_id

    def _currency_symbols(self):
        """Symbols the numeric canonicalizer may strip from a money cell.

        Only symbols that are unambiguously currency markers are listed; a bare
        ``.`` or ``,`` is never stripped, because those are separators whose
        meaning is declared, not decoration.
        """
        self.ensure_one()
        if self.ctype != 'money':
            return []
        symbols = []
        currency = self.default_currency_id
        if currency:
            if currency.symbol:
                symbols.append(currency.symbol)
            if currency.name:
                symbols.append(currency.name)
        return symbols
