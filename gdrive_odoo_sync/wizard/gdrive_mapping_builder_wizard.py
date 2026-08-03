# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. Licensed under LGPL-3.
"""``gdrive.mapping.builder.wizard`` + ``.line`` — suggestions, and only
suggestions (SPEC §3.16).

This wizard reads the observed header schema of one ``gdrive.dataset`` (one
tab) and proposes a ``ctype`` and an Odoo field for every column, so a human
does not have to hand-author forty rows of ``gdrive.mapping.column`` from a
blank list. On **Apply** it creates exactly one ``gdrive.mapping`` in state
``draft`` with ``enabled = False``. Nothing is promoted, nothing is written to
a business model, and no automated path here ever flips ``enabled``.

WHY the suggestions are heuristics on header text, never inference on values
====================================================================
``docs/CANONICALIZATION.md`` §3 forbids per-cell type sniffing anywhere in this
system, and that ban does not relax just because the output is "only a
suggestion" here: sample values are per-cell evidence, and a column of eleven
``1``/``0`` cells is exactly as likely to be a quantity as a boolean. So this
module looks **only** at the header label — the one piece of metadata a human
actually chose on purpose — and applies fixed, deterministic keyword/token
rules. No sampling of ``sample_values``, no statistics, no LLM call, ever. If
the header gives no signal the suggestion falls back to ``ctype='text'``,
which is the same safe default ``lib.contract.default_text_contract_dict``
uses for an entirely unmapped column: text never destroys information the way
a wrong ``number`` or ``date`` coercion would.

The heuristics only ever suggest {``text``, ``number``, ``money``, ``bool``,
``date``, ``datetime``}. ``selection``, ``many2one`` and ``m2m`` are
deliberately never suggested: each of those needs a hand-authored
``value_map`` or ``comodel`` that no header-text rule can safely invent, and
``gdrive.mapping.column``'s own constraints (``_check_value_map``,
§3.9) refuse to save them empty anyway.

WHY ``assert_string_value`` defaults True on identifier-looking headers
====================================================================
A column recognised as an identifier (SKU, barcode, code, reference, part
number, invoice number, phone number, account number, postal code, …) is
suggested with ``ctype='text'`` **and** ``assert_string_value=True`` together.
That is the direction whose default failure is loud: an over-eager tick on an
ordinary numeric column costs one ``IDENTIFIER_NUMERIC`` quarantine that a
human notices and unticks, while the missing tick on a real identifier costs
silently mangled leading zeros nobody notices until reconciliation is already
wrong (SPEC §4.6, §3.9).
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..lib.contract import slugify
from ..models.gdrive_mapping_column import CTYPE_SELECTION, CTYPE_TO_ODOO_TYPES

_logger = logging.getLogger(__name__)

#: Odoo fields never offered as a suggestion target, regardless of header
#: match quality. ``id``/``display_name``/audit columns cannot be written by a
#: sheet-authority column at all (``gdrive.mapping._validate_contract``
#: assertion 2 would refuse them); ``active`` is excluded on purpose even
#: though it is a plain boolean, because it is also the sync engine's own
#: soft-delete flag (``gdrive.mapping.soft_delete_field``, default
#: ``'active'``) and silently aiming a sheet column at it would fight the
#: delete planner. The two technical columns are excluded because a human
#: authoring a mapping should never see them as an option.
EXCLUDED_FIELD_NAMES = frozenset({
    'id', 'display_name', '__last_update',
    'create_date', 'write_date', 'create_uid', 'write_uid',
    'active', 'x_gdrive_sync_id', 'x_gdrive_source_dataset',
})

#: Header token(s) → candidate Odoo field name(s), tried in order, before the
#: generic fuzzy scorer runs. These exist because the generic scorer compares
#: *tokens*, and some of the commonest business synonyms share no token at all
#: with the field they mean ("SKU" vs. ``default_code``). Every candidate is
#: still required to actually exist (and be type-compatible) on the target
#: model before it is offered — this table only shortens the search, it never
#: invents a field.
ALIAS_FIELD_NAMES = {
    ('sku',): ('default_code',),
    ('part', 'number'): ('default_code',),
    ('item', 'number'): ('default_code',),
    ('product', 'code'): ('default_code',),
    ('internal', 'reference'): ('default_code',),
    ('reference',): ('ref', 'default_code'),
    ('ref',): ('ref',),
    ('phone', 'number'): ('phone',),
    ('phone',): ('phone', 'mobile'),
    ('mobile', 'number'): ('mobile',),
    ('mobile',): ('mobile', 'phone'),
    ('fax',): ('fax',),
    ('email',): ('email',),
    ('e', 'mail'): ('email',),
    ('zip', 'code'): ('zip',),
    ('postal', 'code'): ('zip',),
    ('post', 'code'): ('zip',),
    ('zip',): ('zip',),
    ('website',): ('website',),
    ('web', 'site'): ('website',),
    ('url',): ('website',),
    ('tax', 'id'): ('vat',),
    ('vat',): ('vat',),
    ('city',): ('city',),
    ('street', 'address'): ('street',),
    ('address',): ('street',),
    ('street',): ('street',),
    ('address', 'line', '2'): ('street2',),
    ('country',): ('country_id',),
    ('state',): ('state_id',),
    ('province',): ('state_id',),
    ('full', 'name'): ('name',),
    ('company', 'name'): ('name',),
    ('name',): ('name',),
    ('job', 'title'): ('function',),
    ('title',): ('function', 'name'),
    ('notes',): ('comment',),
    ('description',): ('comment', 'description'),
}

# --------------------------------------------------------------------------
# Header-text heuristics — pure, deterministic, no ORM, no sampling.
# --------------------------------------------------------------------------

#: Single tokens that, alone, mark a column as an identifier. Matched against
#: the column's own ``slug`` (already lower-cased, underscore-joined ASCII —
#: see ``lib.contract.slugify``), so "SKU", "Sku#" and "S.K.U." all normalize
#: to the same token.
IDENTITY_TOKENS = frozenset({
    'sku', 'barcode', 'upc', 'ean', 'isbn', 'imei', 'vin', 'ssn', 'ein',
    'iban', 'zip', 'id', 'code', 'ref', 'reference', 'serial', 'phone',
    'mobile', 'fax', 'pin', 'sin', 'passport', 'license', 'plate',
})

#: Adjacent-token pairs that mark an identifier only in combination — "number"
#: alone is a generic count (SPEC's own NUMBER_TOKENS below), but "invoice
#: number" is exactly the identifier example SPEC §3.9's help text calls out
#: by name.
IDENTITY_BIGRAMS = frozenset({
    ('part', 'number'), ('part', 'no'), ('part', 'num'),
    ('model', 'number'),
    ('invoice', 'number'), ('invoice', 'no'), ('invoice', 'num'),
    ('account', 'number'), ('acct', 'number'), ('account', 'no'),
    ('phone', 'number'), ('mobile', 'number'),
    ('postal', 'code'), ('zip', 'code'), ('post', 'code'),
    ('tracking', 'number'),
    ('order', 'number'), ('po', 'number'), ('purchase', 'order'),
    ('serial', 'number'), ('serial', 'no'),
    ('reference', 'number'), ('ref', 'number'),
    ('tax', 'id'), ('vat', 'number'), ('vat', 'id'),
    ('routing', 'number'),
    ('id', 'number'), ('identification', 'number'),
    ('customer', 'number'), ('client', 'number'),
    ('national', 'id'), ('social', 'security'),
    ('license', 'number'), ('license', 'plate'),
})

DATETIME_TOKENS = frozenset({'timestamp', 'datetime', 'time'})
DATE_TOKENS = frozenset({
    'date', 'created', 'updated', 'modified', 'birthday', 'dob',
    'deadline', 'expiry', 'expires', 'due', 'anniversary', 'dated',
})
BOOL_TOKENS = frozenset({
    'active', 'enabled', 'disabled', 'flag', 'bool', 'archived',
    'deleted', 'verified', 'confirmed', 'approved', 'published',
    'visible', 'available', 'subscribed', 'opted',
})
MONEY_TOKENS = frozenset({
    'price', 'amount', 'total', 'cost', 'revenue', 'balance', 'payment',
    'fee', 'salary', 'budget', 'subtotal', 'value', 'rate', 'fare',
    'charge', 'expense', 'income', 'discount', 'tax', 'wage', 'deposit',
})
NUMBER_TOKENS = frozenset({
    'qty', 'quantity', 'count', 'number', 'num', 'age', 'score',
    'rating', 'weight', 'height', 'length', 'width', 'duration',
    'percent', 'percentage', 'rank', 'position', 'level', 'units',
})


def _tokens(slug):
    """Split an already-slugified header (``invoice_number``) into tokens."""
    return tuple(t for t in (slug or '').split('_') if t)


def _suggest_ctype(slug):
    """Return ``(ctype, assert_string_value)`` for one column's slug.

    Checked in a fixed priority order so a header carrying more than one
    signal resolves the same way every time: identifier beats every other
    reading (an "Invoice Number" must never fall through to ``number``), then
    datetime beats date (checked first because ``{'date', 'time'}`` as two
    separate tokens must not be read as plain ``date``), then date, bool,
    money, and finally the generic numeric tokens. Anything left over is
    ``text`` — the same safe default the rest of this addon uses when a
    contract decision cannot be made responsibly.
    """
    tokens = _tokens(slug)
    if not tokens:
        return 'text', False
    token_set = set(tokens)
    bigrams = set(zip(tokens, tokens[1:]))
    if (token_set & IDENTITY_TOKENS) or (bigrams & IDENTITY_BIGRAMS):
        return 'text', True
    if (token_set & DATETIME_TOKENS) or (('date', 'time') in bigrams):
        return 'datetime', False
    if token_set & DATE_TOKENS:
        return 'date', False
    if (token_set & BOOL_TOKENS) or tokens[0] in ('is', 'has'):
        return 'bool', False
    if token_set & MONEY_TOKENS:
        return 'money', False
    if token_set & NUMBER_TOKENS:
        return 'number', False
    return 'text', False


def _field_score(token_set, slug, field):
    """Score how well ``field`` (an ``ir.model.fields`` record) fits ``slug``.

    100/95 for an exact match on the technical name or the slugified label;
    75/70 when the header's tokens are a subset (or superset) of the field's
    tokens, which catches e.g. ``customer_email`` for the field ``email`` or
    the header ``Email`` for the field ``partner_email``; otherwise a plain
    Jaccard overlap, thresholded at 0.5 so two columns that merely share one
    common word (both mention "date", say) do not collide.
    """
    name_tokens = set(t for t in field.name.split('_') if t)
    label_tokens = set(t for t in slugify(field.field_description or '').split('_') if t)
    if slug == field.name:
        return 100
    if slug and slug == slugify(field.field_description or ''):
        return 95
    if token_set and name_tokens and (token_set <= name_tokens or name_tokens <= token_set):
        return 75
    if token_set and label_tokens and (token_set <= label_tokens or label_tokens <= token_set):
        return 70
    best = 0.0
    for candidate_tokens in (name_tokens, label_tokens):
        union = token_set | candidate_tokens
        if not union:
            continue
        best = max(best, len(token_set & candidate_tokens) / len(union))
    if best >= 0.5:
        return int(40 + best * 20)
    return 0


class GdriveMappingBuilderWizard(models.TransientModel):
    """One-shot builder: observed columns in, a draft, disabled mapping out."""

    _name = 'gdrive.mapping.builder.wizard'
    _description = 'Google Drive Mapping Builder Wizard'

    dataset_id = fields.Many2one(
        'gdrive.dataset', string='Dataset',
        required=True, ondelete='cascade',
        help='The tab whose observed columns are being turned into a contract.',
    )
    name = fields.Char(
        string='Mapping Name', required=True,
        help='Name of the gdrive.mapping this wizard will create.',
    )
    target_model_id = fields.Many2one(
        'ir.model', string='Target Model', required=True,
        help='The business model the mapping will promote rows into. Choosing '
             'it (re)runs the Odoo-field suggestions below for every column '
             'that has not already been given one.',
    )
    line_ids = fields.One2many(
        'gdrive.mapping.builder.wizard.line', 'wizard_id', string='Column Suggestions')

    # ------------------------------------------------------------------
    # Materialize one line per observed column
    # ------------------------------------------------------------------
    @api.model
    def default_get(self, fields_list):
        """Pre-populate ``line_ids`` from the dataset's live columns.

        ``dataset_id`` itself arrives through the standard Odoo
        ``default_dataset_id`` context key (set by
        ``gdrive.dataset.action_build_mapping``) and needs no help here — the
        base implementation already resolves it. What the base implementation
        cannot do is fan that one id out into one wizard line per column, in a
        stable order, which is this override's whole job.

        Ordering is by ``col_index`` — the dataset column model's own
        ``_live()`` order — never by dict iteration, so re-opening the same
        wizard twice against an unchanged tab produces byte-identical lines.
        """
        defaults = super().default_get(fields_list)
        dataset_id = defaults.get('dataset_id')
        if not dataset_id:
            return defaults
        dataset = self.env['gdrive.dataset'].browse(dataset_id)
        if not dataset.exists():
            return defaults

        if 'name' in fields_list and not defaults.get('name'):
            defaults['name'] = dataset.display_name or dataset.tab_title or ''

        if 'line_ids' in fields_list:
            columns = self.env['gdrive.dataset.column']._live(dataset)
            line_cmds = []
            for index, column in enumerate(columns, start=1):
                ctype, is_identifier = _suggest_ctype(column.slug or '')
                line_cmds.append((0, 0, {
                    'dataset_column_id': column.id,
                    'header_canon': column.header_canon or '',
                    'slug': column.slug or '',
                    'sequence': index * 10,
                    'ctype': ctype,
                    'assert_string_value': is_identifier,
                }))
            defaults['line_ids'] = line_cmds
        return defaults

    # ------------------------------------------------------------------
    # Odoo-field suggestions — run once a target model is chosen
    # ------------------------------------------------------------------
    @api.onchange('target_model_id')
    def _onchange_target_model_id(self):
        """Suggest an Odoo field for every line that does not already have one.

        Never overwrites a field a human already picked (or a previous pass
        already suggested), mirroring ``gdrive.mapping.column``'s own
        ``_onchange_dataset_column_id``: a suggestion only ever *fills in*,
        it never re-points a choice someone already made. Two lines are never
        suggested the same field — ``gdrive.mapping._validate_contract``
        assertion 2 refuses a mapping where two columns target one field, so
        offering that here would just mean re-doing the same fix by hand.
        """
        for wizard in self:
            if not wizard.target_model_id:
                continue
            if not wizard.name:
                wizard.name = _('%(tab)s to %(model)s') % {
                    'tab': wizard.dataset_id.display_name or wizard.dataset_id.tab_title or '',
                    'model': wizard.target_model_id.name or wizard.target_model_id.model,
                }
            used_ids = {line.odoo_field_id.id for line in wizard.line_ids if line.odoo_field_id}
            candidates_by_types = {}
            for line in wizard.line_ids:
                if line.odoo_field_id:
                    continue
                allowed = CTYPE_TO_ODOO_TYPES.get(line.ctype)
                if not allowed:
                    continue
                key = tuple(sorted(allowed))
                candidates = candidates_by_types.get(key)
                if candidates is None:
                    candidates = wizard._candidate_fields(allowed)
                    candidates_by_types[key] = candidates
                available = candidates.filtered(lambda f: f.id not in used_ids)
                match = wizard._best_field_match(line.slug or '', available)
                if match:
                    line.odoo_field_id = match.id
                    used_ids.add(match.id)

    def _candidate_fields(self, allowed_ttypes):
        """Stored fields of ``target_model_id`` whose type fits ``allowed_ttypes``.

        Restricted to ``store=True`` for the same reason
        ``gdrive.mapping.column.odoo_field_id``'s own domain is: a non-stored
        compute cannot be written and would fail
        ``gdrive.mapping._validate_contract`` assertion 2 anyway, so it is
        never worth offering as a suggestion.
        """
        self.ensure_one()
        if not self.target_model_id or not allowed_ttypes:
            return self.env['ir.model.fields']
        return self.env['ir.model.fields'].sudo().search([
            ('model_id', '=', self.target_model_id.id),
            ('store', '=', True),
            ('ttype', 'in', sorted(allowed_ttypes)),
            ('name', 'not in', sorted(EXCLUDED_FIELD_NAMES)),
        ])

    def _best_field_match(self, slug, candidates):
        """Pick the single best-fitting field in ``candidates`` for ``slug``.

        Tries the alias table first (longest, most specific token sequence
        first), then falls back to :func:`_field_score`. Ties are broken by
        the shortest field name and then alphabetically, so the result is the
        same every time this runs against the same inputs — required, since
        this feeds a suggestion a human will see and must be able to trust as
        reproducible, not a coin flip between two equally-plausible fields.
        """
        if not slug or not candidates:
            return None
        tokens = _tokens(slug)
        for key in sorted(ALIAS_FIELD_NAMES, key=len, reverse=True):
            n = len(key)
            if n and any(tokens[i:i + n] == key for i in range(len(tokens) - n + 1)):
                for name in ALIAS_FIELD_NAMES[key]:
                    hit = candidates.filtered(lambda f, name=name: f.name == name)
                    if hit:
                        return hit[0]
        token_set = set(tokens)
        scored = [(_field_score(token_set, slug, field), field) for field in candidates]
        scored = [pair for pair in scored if pair[0] > 0]
        if not scored:
            return None
        scored.sort(key=lambda pair: (-pair[0], len(pair[1].name), pair[1].name))
        return scored[0][1]

    # ------------------------------------------------------------------
    # Apply — the only place this wizard writes anything permanent
    # ------------------------------------------------------------------
    def action_create_mapping(self):
        """Create a ``draft``, disabled ``gdrive.mapping`` from the lines below.

        Every value copied across is one already visible and editable on the
        list above: nothing here consults sample values, re-runs a
        suggestion, or invents a default the human has not already seen. A
        line with no chosen Odoo field becomes ``ctype='ignore'`` on the real
        column record, because ``gdrive.mapping.column._check_field_binding``
        refuses any other combination — the text under the list ("a column
        left without an Odoo field is treated as ignore") is not just UI
        copy, it is what this method actually does.
        """
        self.ensure_one()
        if not self.dataset_id:
            raise UserError(_('This wizard has no dataset attached. Re-open it from the '
                              "dataset's Build Mapping button."))
        if not self.target_model_id:
            raise UserError(_('Choose a target model before creating the mapping.'))
        if not self.name:
            raise UserError(_('Name the mapping before creating it.'))
        if not self.line_ids:
            raise UserError(_('Tab %s has no observed columns to build a contract from. '
                              'Stage it at least once first.', self.dataset_id.display_name))

        column_cmds = []
        for line in self.line_ids.sorted(lambda l: (l.sequence, l.id)):
            resolved_ctype = line.ctype if line.odoo_field_id else 'ignore'
            column_cmds.append((0, 0, {
                'sequence': line.sequence,
                'dataset_column_id': line.dataset_column_id.id,
                'header_canon': line.header_canon,
                'odoo_field_id': line.odoo_field_id.id,
                'ctype': resolved_ctype,
                'required': line.required,
                'is_natural_key': line.is_natural_key,
                'assert_string_value': line.assert_string_value,
            }))

        mapping = self.env['gdrive.mapping'].create({
            'name': self.name,
            'dataset_id': self.dataset_id.id,
            'target_model_id': self.target_model_id.id,
            'enabled': False,
            'state': 'draft',
            'column_ids': column_cmds,
        })
        _logger.info(
            "Mapping builder wizard created mapping %s (id=%s) for dataset %s with %d "
            "column(s); draft and disabled, as every mapping this wizard creates is.",
            mapping.name, mapping.id, self.dataset_id.display_name, len(column_cmds))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Promotion Mapping'),
            'res_model': 'gdrive.mapping',
            'view_mode': 'form',
            'res_id': mapping.id,
        }


class GdriveMappingBuilderWizardLine(models.TransientModel):
    """One suggested column, one row on the wizard's editable list."""

    _name = 'gdrive.mapping.builder.wizard.line'
    _description = 'Google Drive Mapping Builder Wizard Line'
    _order = 'wizard_id, sequence, id'

    wizard_id = fields.Many2one(
        'gdrive.mapping.builder.wizard', string='Wizard',
        required=True, ondelete='cascade', index=True,
    )
    target_model_id = fields.Many2one(
        related='wizard_id.target_model_id', string='Target Model', store=True, readonly=True,
        help='Denormalized purely so the odoo_field_id domain below can be a '
             'plain field reference, matching gdrive.mapping.column\'s own pattern.',
    )
    sequence = fields.Integer(
        string='Sequence', default=10,
        help='Materialized from the dataset column order (col_index), never '
             'from dict iteration, so re-opening this wizard is reproducible.',
    )

    dataset_column_id = fields.Many2one(
        'gdrive.dataset.column', string='Sheet Column', ondelete='set null',
        help='UI convenience only, exactly as on gdrive.mapping.column: the '
             'durable link carried onto the created mapping column is header_canon.',
    )
    header_canon = fields.Char(
        string='Header (canonical)', required=True,
        help='The tagged TEXT_CANON form of the sheet header. Copied verbatim '
             'onto the created gdrive.mapping.column as its matching key.',
    )
    slug = fields.Char(
        string='Slug',
        help='The dataset column\'s payload key, shown for reference and used '
             'as the input to the deterministic ctype/field suggestions.',
    )

    ctype = fields.Selection(
        CTYPE_SELECTION, string='Contract Type', required=True, default='text',
        help='A SUGGESTED type, derived only from the header text — never from '
             'sampled values, which is exactly the per-cell sniffing this '
             'addon forbids everywhere else. Review it before creating the mapping.',
    )
    odoo_field_id = fields.Many2one(
        'ir.model.fields', string='Odoo Field', ondelete='set null',
        domain="[('model_id', '=', target_model_id), ('store', '=', True)]",
        help='SUGGESTED once a target model is chosen above. Left empty, this '
             'column is created as ctype=ignore: it stays in staging and is '
             'never wrong to leave unmapped.',
    )
    required = fields.Boolean(
        string='Required', default=False,
        help='An empty or invalid required cell will quarantine the whole row. '
             'Never suggested True — that is a business decision, not a header-text one.',
    )
    is_natural_key = fields.Boolean(
        string='Natural Key', default=False,
        help='Ordered by sequence to form the composite identity key. Never '
             'suggested True: guessing an identity column wrong is exactly the '
             'kind of silent, structural error this wizard exists to avoid.',
    )
    assert_string_value = fields.Boolean(
        string='Identifier', default=False,
        help='Pre-ticked on identifier-looking headers (SKU, code, ID, barcode, '
             'reference, part/invoice/account/phone number, postal code, …). '
             'Tick it on every identifier column the heuristic missed: once such '
             'a value is read as a number its leading zeros are already gone.',
    )
