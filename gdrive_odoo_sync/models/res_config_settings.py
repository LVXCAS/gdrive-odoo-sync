# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. See LICENSE (LGPL-3) for full copyright and licensing details.
"""``res.config.settings`` extension — where the credential is entered (SPEC §2.5).

WHY the service-account key lives here and nowhere else
------------------------------------------------------
It is a bearer credential for the entire Google Drive of the impersonated
Workspace user. ``res.config.settings`` is a ``TransientModel``: the value is
written straight through to ``ir.config_parameter`` and the settings record is
discarded. A regular model would expose it to export, duplicate, chatter
tracking and the generic export wizard.

WHY these fields do not use ``config_parameter=``
-------------------------------------------------
``config_parameter`` is the right default for ordinary settings, but two rules
in SPEC §2.5 are incompatible with its automatic round-trip:

1. **``set_param`` with a falsy value DELETES the parameter.** It does not store
   an empty string. Any code that writes ``''`` and then assumes ``get_param``
   returns ``''`` is wrong — it returns the *default* argument, or ``False``.
   Deletion is in fact the behaviour we want when the field is cleared (so the
   environment variable takes over), but it has to be deliberate, logged, and
   visible at the call site rather than an emergent property. The same trap
   silently eats an integer setting of ``0``.
2. **The stored key is never echoed back to the browser.** ``password="True"``
   only masks the rendered input; the value still travels to the client and
   sits in the form's data. :func:`get_values` therefore returns
   :data:`KEY_SENTINEL` when a key is stored, and :func:`set_values` treats that
   sentinel as "leave the stored key exactly as it is". Clearing the field still
   deletes the parameter, so the credential remains removable.

Everything here reads and writes ``ir.config_parameter`` through ``.sudo()``,
which is mandatory: the model is restricted to ``base.group_system``, and an
administrator opening the Settings page is not necessarily a system user.
"""

import logging
from typing import Optional

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..services.google_auth import DEFAULT_KEY_ENV_VAR, DEFAULT_KEY_PARAM_KEY, parse_service_account_info
from ..services.errors import redact

_logger = logging.getLogger(__name__)

#: Returned in place of a stored key so the credential never reaches the client.
#: Chosen to be something no service-account JSON could ever be, and stable, so
#: an unchanged Settings save is a no-op rather than a silent overwrite.
KEY_SENTINEL = '__gdrive_stored_key_unchanged__'

PARAM_SA_KEY = DEFAULT_KEY_PARAM_KEY
PARAM_DEFAULT_SUBJECT = 'gdrive_odoo_sync.default_subject'
PARAM_SHEETS_READS = 'gdrive_odoo_sync.sheets_reads_per_min'
PARAM_DRIVE_UNITS = 'gdrive_odoo_sync.drive_units_per_min'
PARAM_RUN_RETENTION = 'gdrive_odoo_sync.run_retention_days'
PARAM_PLAN_EXPIRY = 'gdrive_odoo_sync.plan_expiry_hours'

#: Google's documented hard ceiling for Sheets reads, per minute per user. Not
#: enforced (an operator may know something we do not) but warned about, because
#: exceeding it does not fail loudly — it degrades into 429 storms.
SHEETS_HARD_CAP = 60


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    gdrive_sa_key_json = fields.Char(
        string='Service Account Key (JSON)',
        groups='base.group_system',
        help='The downloaded service-account JSON key. Prefer setting the '
             '%s environment variable instead and leaving this empty: it is '
             'checked first, and database dumps are downloadable while '
             'environment variables are not. Clearing this field deletes the '
             'stored parameter.' % DEFAULT_KEY_ENV_VAR,
    )
    gdrive_default_subject = fields.Char(
        string='Default Impersonation Subject',
        help='Pre-filled on new connections. Domain-wide delegation cannot '
             'impersonate a consumer @gmail.com account.',
    )
    gdrive_sheets_reads_per_min = fields.Integer(
        string='Sheets Reads / min',
        help="Client-side token bucket. Google's hard cap is %d per minute per "
             'user, shared with every other client acting as that user.' % SHEETS_HARD_CAP,
    )
    gdrive_drive_units_per_min = fields.Integer(
        string='Drive Quota Units / min',
        help='Against the documented 325 000/min/user Drive ceiling.',
    )
    gdrive_run_retention_days = fields.Integer(
        string='Run Retention (days)',
        help='How long sync runs, their log lines and their gzipped log '
             'archives survive the housekeeping cron.',
    )
    gdrive_plan_expiry_hours = fields.Integer(
        string='Plan Expiry (hours)',
        help='A plan describes a world that was true when it was computed; '
             'past this age it can no longer be applied.',
    )

    # ------------------------------------------------------------------
    # Parameter helpers
    # ------------------------------------------------------------------
    def _icp(self):
        """``ir.config_parameter`` as sudo. Never call it any other way."""
        return self.env['ir.config_parameter'].sudo()

    def _get_int_param(self, key: str, default: int) -> int:
        """Read an integer parameter, tolerating a hand-edited garbage value.

        A malformed system parameter must not make the Settings page un-openable:
        the operator's only route to fixing it *is* the Settings page.
        """
        raw = self._icp().get_param(key)
        try:
            return int(raw)
        except (TypeError, ValueError):
            if raw not in (False, None, ''):
                _logger.warning(
                    'System parameter %s holds a non-integer value %r; falling back to %d.',
                    key, raw, default)
            return default

    def _set_param(self, key: str, value: Optional[str]) -> None:
        """Write ``key``, or **delete** it when ``value`` is empty.

        ``set_param(key, '')`` already deletes the row, but going through it
        blind is how "I cleared the field and it still uses the old value"
        happens: the caller reads back a default and believes it was stored.
        Deletion is therefore explicit and logged, and the two branches are
        visible to the next person who reads this method.
        """
        icp = self._icp()
        if value in (False, None, ''):
            existing = icp.search([('key', '=', key)])
            if existing:
                existing.unlink()
                _logger.info('Deleted system parameter %s (cleared in Settings).', key)
            return
        icp.set_param(key, value)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------
    @api.model
    def get_values(self):
        """Populate the Settings form from ``ir.config_parameter``.

        The key itself is deliberately **not** returned. Only its presence is,
        as :data:`KEY_SENTINEL`, and only to a ``base.group_system`` user — a
        non-system user must not even learn whether a credential exists.
        """
        res = super().get_values()
        res.update({
            'gdrive_default_subject': self._icp().get_param(PARAM_DEFAULT_SUBJECT, '') or '',
            'gdrive_sheets_reads_per_min': self._get_int_param(PARAM_SHEETS_READS, 50),
            'gdrive_drive_units_per_min': self._get_int_param(PARAM_DRIVE_UNITS, 200000),
            'gdrive_run_retention_days': self._get_int_param(PARAM_RUN_RETENTION, 90),
            'gdrive_plan_expiry_hours': self._get_int_param(PARAM_PLAN_EXPIRY, 24),
        })
        if self.env.user.has_group('base.group_system'):
            stored = self._icp().get_param(PARAM_SA_KEY)
            res['gdrive_sa_key_json'] = KEY_SENTINEL if (stored and str(stored).strip()) else ''
        return res

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------
    def set_values(self):
        """Persist the Settings form, one explicit branch per empty value."""
        super().set_values()
        for record in self:
            record._set_param(PARAM_DEFAULT_SUBJECT, (record.gdrive_default_subject or '').strip())
            record._set_param(PARAM_SHEETS_READS, record._sanitized_sheets_reads())
            record._set_param(PARAM_DRIVE_UNITS, str(max(int(record.gdrive_drive_units_per_min or 0), 1)))
            record._set_param(PARAM_RUN_RETENTION, str(max(int(record.gdrive_run_retention_days or 0), 1)))
            record._set_param(PARAM_PLAN_EXPIRY, str(max(int(record.gdrive_plan_expiry_hours or 0), 1)))
            if self.env.user.has_group('base.group_system'):
                record._store_sa_key()
        return True

    def _sanitized_sheets_reads(self) -> str:
        """Clamp to at least 1 and warn above Google's documented ceiling."""
        self.ensure_one()
        value = max(int(self.gdrive_sheets_reads_per_min or 0), 1)
        if value > SHEETS_HARD_CAP:
            _logger.warning(
                'Sheets reads/min set to %d, above the documented cap of %d per '
                'user. Google will answer the excess with 403 rateLimitExceeded, '
                'which costs a retry budget rather than returning data.',
                value, SHEETS_HARD_CAP)
        return str(value)

    def _store_sa_key(self) -> None:
        """Apply the three possible intents for the key field.

        * the sentinel  → the admin did not touch it; leave the stored key alone;
        * empty         → delete the parameter, so the environment variable (or
                          nothing at all) takes over;
        * anything else → validate the JSON *structurally* and store it.

        Validation happens before storage on purpose: a truncated paste that is
        accepted here surfaces hours later as an ``unauthorized_client`` inside a
        cron, which is the least debuggable place it could possibly appear. Only
        the redacted parse error is ever shown — never the value.
        """
        self.ensure_one()
        raw = self.gdrive_sa_key_json
        if raw == KEY_SENTINEL:
            return
        value = (raw or '').strip()
        if not value:
            self._set_param(PARAM_SA_KEY, '')
            _logger.warning(
                'The stored Google service-account key was deleted from the '
                'database. Authentication now depends entirely on the %s '
                'environment variable.', DEFAULT_KEY_ENV_VAR)
            return
        try:
            info = parse_service_account_info(value, source=_('the Settings page'))
        except Exception as exc:  # noqa: BLE001 - re-raised as a user-facing error
            # redact() guarantees no key material reaches the dialog, the log or
            # the traceback that Odoo renders for an unhandled exception.
            from odoo.exceptions import UserError

            _logger.error('Refused to store an unusable service-account key: %s', redact(exc))
            raise UserError(_(
                'That service-account key was not stored because it is not '
                'usable:\n\n%(reason)s',
                reason=redact(exc),
            )) from exc
        self._set_param(PARAM_SA_KEY, value)
        _logger.info(
            'Stored a Google service-account key for client_email=%s (client_id=%s). '
            'Consider moving it to the %s environment variable: system parameters '
            'appear in cleartext in database dumps.',
            info.get('client_email') or '?', info.get('client_id') or '?', DEFAULT_KEY_ENV_VAR,
        )
