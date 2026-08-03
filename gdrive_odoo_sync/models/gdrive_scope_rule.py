# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. See LICENSE (LGPL-3) for full copyright and licensing details.
"""``gdrive.scope.rule`` — declarative crawl scoping (SPEC §3.2).

WHY declarative rules rather than a hard-coded folder list: the set of folders
worth mirroring changes weekly, and every change would otherwise be a code
deploy. WHY *default-allow with include-flips-to-deny* rather than plain
whitelisting: on day one nobody knows what is in the Drive, so the useful
default is "crawl everything and let me exclude the noise"; the moment an
administrator writes a single ``include`` rule they have expressed an intent to
enumerate, and silently continuing to allow everything else would make that rule
a no-op.

**Scope rules never delete anything.** A node that falls out of scope moves to
``state='skipped'`` with ``skip_reason='out_of_scope'``; its record, its
attachment and its staged rows stay. Deleting on a scope change would make a
one-character typo in a glob indistinguishable from a mass deletion.
"""

import fnmatch
import logging

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

MATCH_TYPE_SELECTION = [
    ('folder_subtree', 'Folder Subtree'),
    ('drive_id', 'Shared Drive'),
    ('owner_email', 'Owner Email'),
    ('mime_type', 'MIME Type'),
    ('name_glob', 'Name Glob'),
    ('path_glob', 'Path Glob'),
]

APPLIES_TO_SELECTION = [
    ('all', 'Everything'),
    ('spreadsheets', 'Spreadsheets Only'),
    ('files', 'Non-Spreadsheet Files Only'),
]


class GdriveScopeRule(models.Model):
    """One include/exclude rule evaluated against every discovered Drive object."""

    _name = 'gdrive.scope.rule'
    _description = 'Google Drive Scope Rule'
    _order = 'connection_id, sequence, id'

    connection_id = fields.Many2one(
        'gdrive.connection', string='Connection',
        required=True, index=True, ondelete='cascade',
    )
    sequence = fields.Integer(string='Sequence', default=10)
    kind = fields.Selection(
        [('include', 'Include'), ('exclude', 'Exclude')],
        string='Kind', required=True, default='exclude',
    )
    match_type = fields.Selection(
        MATCH_TYPE_SELECTION, string='Match On', required=True, default='name_glob',
    )
    value = fields.Char(
        string='Value', required=True,
        help='A Drive file id (folder_subtree), a shared drive id, an email '
             'address, a MIME string, or an fnmatch glob.',
    )
    applies_to = fields.Selection(
        APPLIES_TO_SELECTION, string='Applies To', required=True, default='all',
    )
    note = fields.Text(string='Note')

    @api.constrains('match_type', 'value')
    def _check_value(self):
        """Reject obviously wrong values at write time rather than at crawl time.

        A folder-subtree rule holding a folder *name* instead of a Drive file id
        matches nothing, silently, forever. Catching it here turns a mystery
        ("why is that folder still being crawled?") into an immediate error.
        """
        for rule in self:
            value = (rule.value or '').strip()
            if not value:
                raise ValidationError(_('A scope rule needs a value.'))
            if rule.match_type == 'owner_email' and '@' not in value:
                raise ValidationError(
                    _('Owner-email rules need an email address, got %r.', value))
            if rule.match_type in ('folder_subtree', 'drive_id') and any(c in value for c in '*?/ '):
                raise ValidationError(
                    _('%s rules take a Drive id, not a name or a glob. Got %r.',
                      dict(MATCH_TYPE_SELECTION)[rule.match_type], value))

    @api.depends('kind', 'match_type', 'value')
    def _compute_display_name(self):
        """Odoo 18 display name: ``exclude name_glob ~ *.tmp``."""
        labels = dict(MATCH_TYPE_SELECTION)
        for rule in self:
            rule.display_name = '%s %s ~ %s' % (
                rule.kind or 'exclude',
                labels.get(rule.match_type, rule.match_type or ''),
                rule.value or '',
            )

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------
    def _class_of(self, meta) -> str:
        """Return the ``applies_to`` class of a node meta dict: spreadsheets/files.

        Folders count as ``files``: a folder is not a spreadsheet, and a rule
        scoped to spreadsheets should never prune a folder out of the tree —
        doing so would orphan every spreadsheet underneath it.
        """
        if meta.get('node_type') == 'spreadsheet' or meta.get('is_spreadsheet_blob'):
            return 'spreadsheets'
        return 'files'

    def _applies(self, meta) -> bool:
        """True when this rule is in play for ``meta``'s class."""
        self.ensure_one()
        return self.applies_to == 'all' or self.applies_to == self._class_of(meta)

    def matches(self, meta) -> bool:
        """Return True when this single rule matches the node described by ``meta``.

        ``meta`` is a plain dict (see :meth:`gdrive.node._scope_meta`) so this
        function stays testable without an ORM record and can be evaluated over
        Drive metadata *before* a node exists.
        """
        self.ensure_one()
        value = (self.value or '').strip()
        if not value:
            return False
        if self.match_type == 'folder_subtree':
            # A folder matches its own subtree rule, so excluding a folder also
            # excludes the folder record itself, not merely its contents.
            return value == meta.get('google_id') or value in (meta.get('ancestor_google_ids') or ())
        if self.match_type == 'drive_id':
            return value == (meta.get('shared_drive_id') or '')
        if self.match_type == 'owner_email':
            return value.casefold() == (meta.get('owner_email') or '').casefold()
        if self.match_type == 'mime_type':
            mime = meta.get('mime_type') or ''
            return mime == value or fnmatch.fnmatchcase(mime, value)
        if self.match_type == 'name_glob':
            return fnmatch.fnmatch(meta.get('name') or '', value)
        if self.match_type == 'path_glob':
            return fnmatch.fnmatch(meta.get('path') or '', value)
        _logger.warning("Unknown scope rule match_type %r on rule %s; treated as no match.",
                        self.match_type, self.id)
        return False

    def evaluate(self, meta) -> bool:
        """Evaluate this whole ruleset against ``meta``. True ⇒ in scope.

        The exact semantics of SPEC §3.2, in order:

        1. Evaluation is **default-allow**.
        2. For the node's ``applies_to`` class, if *any* ``include`` rule exists
           for that class, the class becomes **default-deny** and the node must
           match at least one such include.
        3. ``exclude`` rules are applied last and **always win**.

        Step 3 being last and unconditional is what makes the ruleset
        predictable: an administrator can always add one exclude line and be
        certain it takes effect, without auditing the ordering of everything
        that came before it.
        """
        if not self:
            return True
        node_class = self._class_of(meta)
        relevant = self.filtered(lambda r: r.applies_to in ('all', node_class))
        includes = relevant.filtered(lambda r: r.kind == 'include')
        if includes and not any(rule.matches(meta) for rule in includes):
            return False
        for rule in relevant.filtered(lambda r: r.kind == 'exclude').sorted(lambda r: (r.sequence, r.id)):
            if rule.matches(meta):
                return False
        return True

    def excluded_subtree_roots(self) -> set:
        """Drive ids of folders whose entire subtree is pruned from discovery.

        Returned as a set so the discovery pass can prune during the tree walk
        rather than re-evaluating every rule for every one of a folder's 4 000
        descendants.
        """
        return {
            (rule.value or '').strip()
            for rule in self
            if rule.kind == 'exclude' and rule.match_type == 'folder_subtree' and rule.value
        }

    def included_subtree_roots(self) -> set:
        """Drive ids of folders declared as positive crawl roots."""
        return {
            (rule.value or '').strip()
            for rule in self
            if rule.kind == 'include' and rule.match_type == 'folder_subtree' and rule.value
        }
