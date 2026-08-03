# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. See LICENSE (LGPL-3) for full copyright and licensing details.
"""``gdrive.change.cursor`` — Drive Changes API page tokens (SPEC §3.3, §4.3).

Three invariants live in this file, and every one of them exists because of a
specific way incremental sync silently loses data:

1. **A cursor belongs to a principal, not to a database.** ``subject_email`` is
   denormalized onto the row. A token minted while impersonating Lucas is
   meaningless when replayed as the bare service account, and Google will
   happily return *someone else's* empty change list rather than an error.

2. **A token minted with ``driveId=X`` must always be replayed with ``driveId=X``.**
   Replaying a shared-drive token against the user corpus returns changes for the
   wrong corpus, again with no error.

3. **Commit the mirrored data first, persist the token last.** Saving the token
   before committing the nodes means a crash in between loses those changes
   *permanently*: the next poll starts after them and nothing ever re-reports
   them. Committing data first can only cause a replay, and every handler in
   lane D is idempotent, so a replay is free.
"""

import logging

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class GdriveChangeCursor(models.Model):
    """One Drive Changes cursor per ``(connection, subject, drive)`` triple."""

    _name = 'gdrive.change.cursor'
    _description = 'Google Drive Change Cursor'
    _order = 'connection_id, drive_id'

    connection_id = fields.Many2one(
        'gdrive.connection', string='Connection',
        required=True, index=True, ondelete='cascade',
    )
    subject_email = fields.Char(
        string='Subject', required=True, index=True,
        help='The impersonated principal this token was minted for. Cursors are '
             'scoped to a principal: replaying one as a different identity '
             'returns a silently wrong change list.',
    )
    drive_id = fields.Char(
        string='Shared Drive Id', index=True,
        help='Empty for the user corpus (My Drive + shared-with-me). Otherwise '
             'the shared drive this token belongs to; it may never be replayed '
             'against a different corpus.',
    )
    page_token = fields.Char(
        string='Page Token',
        help='The persisted newStartPageToken — the cursor for the NEXT poll. '
             'Never a nextPageToken, which only paginates within one poll.',
    )
    last_polled_date = fields.Datetime(string='Last Polled')
    state = fields.Selection(
        [('bootstrap', 'Needs Bootstrap'), ('valid', 'Valid'), ('invalid', 'Invalid')],
        string='Status', default='bootstrap', required=True, index=True,
    )
    invalid_reason = fields.Char(string='Invalid Reason')

    _sql_constraints = [
        ('cursor_uniq',
         'unique(connection_id, subject_email, drive_id)',
         'One cursor per principal per drive.'),
    ]

    @api.depends('subject_email', 'drive_id', 'state')
    def _compute_display_name(self):
        """Odoo 18 display name; ``name_get()`` is removed."""
        for cursor in self:
            corpus = cursor.drive_id or _('user corpus')
            cursor.display_name = '%s @ %s [%s]' % (
                cursor.subject_email or '?', corpus, cursor.state or 'bootstrap')

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @api.model
    def _get_or_create(self, connection, drive_id=''):
        """Return the cursor for ``(connection, subject, drive_id)``, creating it.

        The subject is snapshotted onto the row at creation. If an administrator
        later edits ``connection.subject_email``, the mismatch is detected by
        :meth:`_is_replayable` and the cursor is invalidated rather than
        replayed against the wrong principal.
        """
        subject = connection.subject_email if connection.auth_mode == 'dwd' else (connection.sa_client_email or 'service-account')
        cursor = self.sudo().search([
            ('connection_id', '=', connection.id),
            ('subject_email', '=', subject),
            ('drive_id', '=', drive_id or False),
        ], limit=1)
        if not cursor:
            cursor = self.sudo().create({
                'connection_id': connection.id,
                'subject_email': subject,
                'drive_id': drive_id or False,
                'state': 'bootstrap',
            })
        return cursor

    def _is_replayable(self, connection) -> bool:
        """True when this token may safely be sent to Google right now.

        Guards the two silent-wrongness cases: no token at all, and a token
        minted for a principal we are no longer impersonating.
        """
        self.ensure_one()
        if self.state != 'valid' or not self.page_token:
            return False
        expected = connection.subject_email if connection.auth_mode == 'dwd' else (connection.sa_client_email or 'service-account')
        if (self.subject_email or '') != (expected or ''):
            self._mark_invalid('subject_changed')
            return False
        return True

    def _bootstrap(self, changes_service):
        """Mint a fresh start token via ``changes.getStartPageToken``.

        Called after a full enumeration, never before it: the token marks "the
        world as of now", so any change that happened *before* we enumerated is
        already reflected in the mirror, and any change after it is delivered by
        the next poll. Minting the token first would open a window in which
        changes are neither enumerated nor delivered.
        """
        self.ensure_one()
        token = changes_service.get_start_token(drive_id=self.drive_id or None)
        self.sudo().write({
            'page_token': token,
            'state': 'valid',
            'invalid_reason': False,
            'last_polled_date': fields.Datetime.now(),
        })
        _logger.info("Bootstrapped change cursor %s with a fresh start token.", self.display_name)
        return token

    def _persist_token(self, token):
        """Store the new start token. **Call this only after committing data.**

        Separated into its own one-line method precisely so the ordering rule is
        visible at every call site: ``... apply changes ...; cr.commit();
        cursor._persist_token(tok); cr.commit()``.
        """
        self.ensure_one()
        if not token:
            _logger.warning(
                "Change poll for %s returned no newStartPageToken; keeping the "
                "previous cursor so the next poll replays rather than skips.",
                self.display_name)
            self.sudo().write({'last_polled_date': fields.Datetime.now()})
            return False
        self.sudo().write({
            'page_token': token,
            'state': 'valid',
            'invalid_reason': False,
            'last_polled_date': fields.Datetime.now(),
        })
        return True

    def _mark_invalid(self, reason):
        """Invalidate the cursor, forcing a full re-enumeration on the next run.

        Google returns ``404`` or ``Invalid Value`` for a token that has aged out
        (they are not kept forever) or that was minted against a different
        corpus. The only safe recovery is a full enumeration followed by a fresh
        ``getStartPageToken``: continuing with delta polls would silently mirror
        an ever-more-stale view.
        """
        self.ensure_one()
        self.sudo().write({
            'state': 'invalid',
            'invalid_reason': reason,
            'page_token': False,
        })
        _logger.warning("Change cursor %s invalidated (%s); a full re-enumeration is now required.",
                        self.display_name, reason)
        return True

    def action_invalidate(self):
        """Manual invalidation button: force a full re-enumeration next run."""
        for cursor in self:
            cursor._mark_invalid('manual')
        return True
