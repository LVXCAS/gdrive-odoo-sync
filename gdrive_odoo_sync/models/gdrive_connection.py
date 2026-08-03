# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. See LICENSE (LGPL-3) for full copyright and licensing details.
"""``gdrive.connection`` — one Google identity we crawl as (SPEC §3.1).

WHY this model holds no credential
----------------------------------
Everything about a connection is configuration *except* the service-account
private key, which is a bearer credential for the entire Drive of the
impersonated Workspace user. It is therefore resolved on demand by
``services/google_auth.py::load_service_account_info`` (environment variable
first, ``ir.config_parameter`` second) and never stored on, cached on, or
computed into a field of this model. Three consequences are enforced here:

* ``sa_client_email`` / ``sa_client_id`` are **non-stored** computes derived from
  the loaded key. They expose only what :func:`~..services.google_auth.key_summary`
  returns, which deliberately omits ``private_key`` and ``private_key_id``.
* Every failure path passes its message through
  :func:`~..services.errors.redact` before it reaches ``last_error``, a log
  record, or the chatter. A ``google.auth`` traceback can carry the assertion it
  signed, and an unredacted repr of the key dict is one ``_logger.debug`` away
  from being permanent.
* Nothing in this file logs the parsed key dict, not even at DEBUG.

WHY the crons never flip ``state`` to ``error``
-----------------------------------------------
``state`` is a statement about *setup*, set to ``ok`` only by a passing Test
Connection (SPEC §2.4), and the cron drivers select on ``state = 'ok'``. If a
transient 503 flipped the state, the connection would drop out of that selection
and never sync again until a human noticed. Runtime failures therefore write
``last_error`` (and a chatter message) and leave ``state`` alone.
"""

import logging
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..services.drive_changes import DriveChanges
from ..services.drive_discovery import DriveDiscovery
from ..services.errors import GDriveError, GDriveTokenInvalid, redact
from ..services.google_auth import (
    DEFAULT_KEY_ENV_VAR,
    DEFAULT_KEY_PARAM_KEY,
    SCOPES_STRING,
    credentials_for_connection,
    key_summary,
    load_service_account_info,
)
from ..services.google_client import ConnectionContext, build_services
from .gdrive_sync_run import COMMIT_BATCH, CRON_BUDGET_SEC, trigger_cron

_logger = logging.getLogger(__name__)

#: First argument of ``pg_try_advisory_xact_lock``. A fixed namespace so a lock
#: taken on connection 3 by the discover cron collides with the one taken on
#: connection 3 by the ingest cron — that collision is the whole point.
LOCK_NAMESPACE = 'gdrive_odoo_sync'

AUTH_MODE_SELECTION = [
    ('dwd', 'Domain-Wide Delegation (impersonate a user)'),
    ('sa_direct', 'Bare Service Account (degraded)'),
]

CORPORA_MODE_SELECTION = [
    ('user', 'User corpus only'),
    ('all_drives', 'All drives in one query'),
    ('per_drive', 'One query per shared drive'),
]

STATE_SELECTION = [
    ('draft', 'Never Tested'),
    ('ok', 'Tested OK'),
    ('error', 'Error'),
]


class _StartTokenAdapter:
    """Adapt :class:`~..services.drive_changes.DriveChanges` for the cursor model.

    ``gdrive.change.cursor._bootstrap()`` calls ``get_start_token(drive_id=…)``;
    the transport class spells the same operation ``get_start_page_token`` (the
    Drive API's own name). Rather than have either lane reach across the boundary
    and rename the other's method, the mismatch is bridged here, at the single
    call site that owns both objects. Every other attribute is delegated
    unchanged, so the adapter can be passed anywhere the service is expected.
    """

    def __init__(self, changes: DriveChanges) -> None:
        self._changes = changes

    def get_start_token(self, drive_id: Optional[str] = None) -> str:
        return self._changes.get_start_page_token(drive_id=drive_id or None)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._changes, name)


class GdriveConnection(models.Model):
    """One Google identity, its crawl policy, and its pacing budget."""

    _name = 'gdrive.connection'
    _description = 'Google Drive Connection'
    _inherit = ['mail.thread']
    _order = 'sequence, id'

    name = fields.Char(string='Name', required=True, tracking=True)
    sequence = fields.Integer(string='Sequence', default=10)
    active = fields.Boolean(string='Active', default=True)
    company_id = fields.Many2one(
        'res.company', string='Company', index=True,
        default=lambda self: self.env.company,
        help='Multi-company scoping. The global record rule of SPEC §7.3 filters '
             'every model in this module through this value.',
    )

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------
    auth_mode = fields.Selection(
        AUTH_MODE_SELECTION, string='Authentication Mode',
        default='dwd', required=True, tracking=True,
        help='Domain-wide delegation impersonates Subject Email and sees exactly '
             'what that user sees. A bare service account is its own principal '
             'with an empty Drive: it sees only what a human explicitly shared '
             'with its ...iam.gserviceaccount.com address, and can never see '
             'items third parties shared with the subject.',
    )
    subject_email = fields.Char(
        string='Subject Email', tracking=True,
        default=lambda self: self._default_subject_email(),
        help='The Workspace user to impersonate. Domain-wide delegation cannot '
             'impersonate a consumer @gmail.com account.',
    )
    sa_key_env_var = fields.Char(
        string='Key Environment Variable', default=DEFAULT_KEY_ENV_VAR, required=True,
        help='Checked first, and preferred: Odoo.sh database dumps are '
             'downloadable and ir.config_parameter values appear in them in '
             'cleartext, while environment variables do not.',
    )
    sa_key_param_key = fields.Char(
        string='Key System Parameter', default=DEFAULT_KEY_PARAM_KEY,
        readonly=True, required=True,
        help='Fallback location of the JSON key, written by the Settings page. '
             'Readonly because the resolution order is part of the security '
             'model, not a per-connection preference.',
    )
    sa_client_email = fields.Char(
        string='SA Client Email', compute='_compute_sa_identity', store=False, readonly=True,
        help='The service account\'s own address. In degraded (bare) mode this is '
             'what folders must be shared with.',
    )
    sa_client_id = fields.Char(
        string='SA Client ID', compute='_compute_sa_identity', store=False, readonly=True,
        help='The ~21-digit numeric OAuth2 client id to paste into the Google '
             'Admin console. Pasting the e-mail address instead is the single '
             'most common setup error.',
    )
    scopes = fields.Char(
        string='Scopes', readonly=True, default=SCOPES_STRING,
        help='Frozen and read-only by design. The module never obtains write '
             'scope, which is what structurally guarantees it cannot damage Drive.',
    )

    # ------------------------------------------------------------------
    # Crawl scope
    # ------------------------------------------------------------------
    include_shared_with_me = fields.Boolean(string='Include Shared With Me', default=True)
    include_shared_drives = fields.Boolean(string='Include Shared Drives', default=True)
    include_trashed = fields.Boolean(
        string='Include Trashed', default=False,
        help='Almost always False. Trashed items are still recorded when they '
             'were seen before being trashed; they move to state "gone".',
    )
    corpora_mode = fields.Selection(
        CORPORA_MODE_SELECTION, string='Corpora Mode', default='per_drive', required=True,
        help='per_drive issues one corpora=drive query per shared drive, which is '
             'the only mode immune to Drive\'s incompleteSearch truncation.',
    )
    max_blob_bytes = fields.Integer(
        string='Max Blob Bytes', default=104857600,
        help='Larger blobs are recorded but not downloaded: state "skipped", '
             'reason "too_large".',
    )

    # ------------------------------------------------------------------
    # Pacing
    # ------------------------------------------------------------------
    sheets_reads_per_min = fields.Integer(
        string='Sheets Reads / min', default=50,
        help="Client-side token bucket. Google's hard cap is 60 per minute per "
             'user, shared with every other client acting as that user.',
    )
    drive_units_per_min = fields.Integer(
        string='Drive Units / min', default=200000,
        help='Against the documented 325 000/min/user Drive ceiling.',
    )
    http_timeout_connect = fields.Float(string='Connect Timeout (s)', default=10.0)
    http_timeout_read = fields.Float(string='Read Timeout (s)', default=120.0)
    max_retry_attempts = fields.Integer(
        string='Max Retry Attempts', default=8,
        help='Exponential backoff attempts before a call is treated as failed and '
             'the run is marked incomplete.',
    )

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    state = fields.Selection(
        STATE_SELECTION, string='Status', default='draft', required=True,
        index=True, tracking=True, copy=False,
        help='Set to "ok" only by a Test Connection whose probes P1-P4 all pass. '
             'The crons process connections in state "ok" and no other.',
    )
    last_test_date = fields.Datetime(string='Last Tested', readonly=True, copy=False)
    last_error = fields.Text(
        string='Last Error', readonly=True, copy=False,
        help='The last connection-level failure, redacted. Never contains key '
             'material.',
    )
    full_resync_requested = fields.Boolean(
        string='Full Resync Requested', default=False, copy=False,
        help='Forces mode "full" on the next discovery pass and invalidates every '
             'cached content hash. Set by the weekly cron, by the button, and by '
             'the post-init hook after any upgrade.',
    )

    # ------------------------------------------------------------------
    # Relations and counters
    # ------------------------------------------------------------------
    scope_rule_ids = fields.One2many('gdrive.scope.rule', 'connection_id', string='Scope Rules')
    cursor_ids = fields.One2many('gdrive.change.cursor', 'connection_id', string='Change Cursors')
    node_ids = fields.One2many('gdrive.node', 'connection_id', string='Drive Nodes')

    node_count = fields.Integer(string='Nodes', compute='_compute_counts')
    dataset_count = fields.Integer(string='Datasets', compute='_compute_counts')
    drift_open_count = fields.Integer(string='Open Drift', compute='_compute_counts')

    _sql_constraints = [
        ('subject_uniq',
         'unique(subject_email, company_id)',
         'One connection per subject per company.'),
    ]

    # ------------------------------------------------------------------
    # Defaults and computes
    # ------------------------------------------------------------------
    @api.model
    def _default_subject_email(self) -> str:
        """Pre-fill the impersonation subject from the seeded system parameter.

        ``.sudo()`` is mandatory on every ``ir.config_parameter`` read: the model
        is restricted to ``base.group_system`` and a ``group_gdrive_admin`` user
        creating a connection is not necessarily a system user.
        """
        return self.env['ir.config_parameter'].sudo().get_param(
            'gdrive_odoo_sync.default_subject', '') or ''

    @api.depends('sa_key_env_var', 'sa_key_param_key')
    def _compute_sa_identity(self):
        """Expose the key's *non-secret* identifying fields, or blanks.

        This compute runs on every form read, including for a brand-new record
        with no key configured anywhere. It must therefore never raise: a missing
        key is the normal state during setup, and an exception here would make
        the very screen that explains how to fix it un-openable. The failure is
        logged once at DEBUG (redacted) and the fields render empty.
        """
        for connection in self:
            summary = {}
            try:
                info = load_service_account_info(self.env, connection)
                summary = key_summary(info)
            except Exception as exc:  # noqa: BLE001 - a missing key is expected here
                _logger.debug(
                    'No usable service-account key for connection %s yet: %s',
                    connection.display_name or connection.name or '<new>', redact(exc),
                )
            connection.sa_client_email = summary.get('client_email') or ''
            connection.sa_client_id = summary.get('client_id') or ''

    def _compute_counts(self):
        """Dashboard counters, aggregated once for the whole recordset.

        ``_read_group`` rather than a ``search_count`` per record: the list view
        renders these for every connection at once, and a per-record count is an
        N+1 query pattern that gets slower exactly as the mirror grows.
        """
        if not self:
            return
        node_counts = dict(self.env['gdrive.node'].sudo()._read_group(
            [('connection_id', 'in', self.ids)],
            groupby=['connection_id'], aggregates=['__count'],
        ))
        dataset_counts = dict(self.env['gdrive.dataset'].sudo()._read_group(
            [('connection_id', 'in', self.ids)],
            groupby=['connection_id'], aggregates=['__count'],
        ))
        # gdrive.drift carries no connection_id of its own (SPEC §3.12); it is
        # reached through its dataset. Grouping by dataset and folding the result
        # keeps this to one query regardless of how many drifts are open.
        drift_by_dataset = dict(self.env['gdrive.drift'].sudo()._read_group(
            [('dataset_id.connection_id', 'in', self.ids), ('resolution', '=', 'open')],
            groupby=['dataset_id'], aggregates=['__count'],
        ))
        drift_counts: Dict[int, int] = {}
        for dataset, count in drift_by_dataset.items():
            connection_id = dataset.connection_id.id
            drift_counts[connection_id] = drift_counts.get(connection_id, 0) + count

        for connection in self:
            connection.node_count = node_counts.get(connection, 0)
            connection.dataset_count = dataset_counts.get(connection, 0)
            connection.drift_open_count = drift_counts.get(connection.id, 0)

    @api.constrains('auth_mode', 'subject_email')
    def _check_subject_email(self):
        """Domain-wide delegation without a subject silently reads an empty Drive."""
        for connection in self:
            if connection.auth_mode == 'dwd' and not (connection.subject_email or '').strip():
                raise UserError(_(
                    'Connection %(name)s uses domain-wide delegation but has no '
                    'Subject Email. Without a subject the service account '
                    'authenticates as itself and sees an empty Drive — which is '
                    'indistinguishable from "everything was deleted".',
                    name=connection.display_name or connection.name or '',
                ))

    @api.onchange('auth_mode')
    def _onchange_auth_mode(self):
        """Warn, in the form, about what degraded mode actually costs."""
        if self.auth_mode == 'sa_direct':
            return {'warning': {
                'title': _('Degraded mode'),
                'message': _(
                    'A bare service account has its own empty Drive. It will see '
                    'only folders a human has explicitly shared with %s, and can '
                    'never see items that third parties shared with %s.',
                    self.sa_client_email or _('the service account'),
                    self.subject_email or _('the subject'),
                ),
            }}
        return None

    # ------------------------------------------------------------------
    # Credentials — the only place model code touches the key
    # ------------------------------------------------------------------
    def _load_key_info(self) -> dict:
        """Resolve and structurally validate the service-account key. Probe P1.

        :returns: the parsed key dict — **secret**. Callers must never write it
            to a field, a log record, a chatter message or a wizard payload. Use
            :meth:`_key_summary` for anything user-visible.
        """
        self.ensure_one()
        return load_service_account_info(self.env, self)

    def _key_summary(self) -> dict:
        """The non-secret identifying fields of the key, safe to display."""
        self.ensure_one()
        return key_summary(self._load_key_info())

    def _credentials(self, info: Optional[dict] = None) -> Any:
        """Build credentials honouring ``auth_mode``. Feeds probe P2.

        Impersonation is applied by ``build_credentials``, which captures the
        return value of ``with_subject()`` — the omission of that assignment is
        the classic silent failure this module structurally prevents.
        """
        self.ensure_one()
        return credentials_for_connection(self.env, self, info=info)

    def _service_context(self, info: Optional[dict] = None) -> ConnectionContext:
        """Snapshot this record as a plain :class:`ConnectionContext`.

        The services layer never receives a recordset: it may run on a worker
        thread, and a recordset carries a cursor and an environment that are not
        safe to take there. This is the boundary.
        """
        self.ensure_one()
        return ConnectionContext.from_record(self.env, self, info=info)

    def _build_services(self, info: Optional[dict] = None) -> Tuple[Any, Any, ConnectionContext]:
        """Return ``(drive, sheets, ctx)`` for this connection, on this thread."""
        self.ensure_one()
        ctx = self._service_context(info=info)
        drive, sheets = build_services(ctx)
        return drive, sheets, ctx

    # ------------------------------------------------------------------
    # Status bookkeeping — used by the Test Connection wizard
    # ------------------------------------------------------------------
    def _mark_test_ok(self, summary: str = '') -> bool:
        """Record a passing Test Connection (probes P1-P4 all green, SPEC §2.4)."""
        self.ensure_one()
        self.sudo().write({
            'state': 'ok',
            'last_test_date': fields.Datetime.now(),
            'last_error': False,
        })
        _logger.info('Connection %s passed its setup probes.', self.display_name)
        self.message_post(body=summary or _('Connection test passed.'))
        return True

    def _mark_error(self, error: Any, tested: bool = False) -> str:
        """Record a failure, redacted, and return the stored text.

        :param error: an exception or a message.
        :param tested: True when this came from the Test Connection wizard, which
            is the only caller allowed to move ``state`` to ``error``. Runtime
            (cron) failures deliberately leave ``state`` alone — see the module
            docstring.

        The message is passed through :func:`redact` unconditionally. A
        ``google.auth`` failure can carry the signed assertion, and an operator
        pasting ``last_error`` into a support ticket must not be leaking a
        credential by doing so.
        """
        self.ensure_one()
        message = redact(error if isinstance(error, str) else str(error))
        vals = {'last_error': message}
        if tested:
            vals['state'] = 'error'
            vals['last_test_date'] = fields.Datetime.now()
        self.sudo().write(vals)
        _logger.warning('Connection %s failed: %s', self.display_name, message)
        return message

    # ------------------------------------------------------------------
    # Concurrency
    # ------------------------------------------------------------------
    def _acquire_lock(self) -> bool:
        """Take the per-connection advisory lock, or return False (SPEC §6).

        ``pg_try_advisory_xact_lock`` never blocks and is released automatically
        when the transaction ends. That auto-release is the reason every batch
        driver in lane D re-acquires after each ``cr.commit()``: a commit hands
        the lock back, and continuing to write without it would let two workers
        interleave on the same connection.
        """
        self.ensure_one()
        self.env.cr.execute(
            'SELECT pg_try_advisory_xact_lock(hashtext(%s), %s)',
            (LOCK_NAMESPACE, self.id),
        )
        row = self.env.cr.fetchone()
        return bool(row and row[0])

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------
    def action_test_connection(self):
        """Open the Test Connection wizard (probes P1-P7, SPEC §2.4).

        The probes themselves live in the wizard, not here: they are network
        calls, and a form button runs inside an HTTP request that Odoo.sh cuts at
        ``limit_time_real``. The wizard is a dialog the user is already waiting
        on, and it reports each probe separately instead of one green tick.
        """
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Test Google Drive Connection'),
            'res_model': 'gdrive.connection.test.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_connection_id': self.id},
        }

    def action_run_discovery(self):
        """Queue a discovery pass rather than running one in this request.

        Long network work belongs in the scheduler (SPEC §6): a full enumeration
        of a large corpus takes minutes and an Odoo.sh HTTP worker is killed at
        120 seconds by default, which would leave the pass half-applied and the
        advisory lock cycling.
        """
        self.ensure_one()
        if self.state != 'ok':
            raise UserError(_(
                'Test this connection first. Discovery only runs against a '
                'connection whose setup probes have passed — an untested '
                'credential that reads zero files looks exactly like an empty Drive.'
            ))
        trigger_cron(self.env, 'gdrive_odoo_sync.ir_cron_gdrive_discover')
        self.message_post(body=_('Discovery queued by %s.', self.env.user.display_name))
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Discovery queued'),
                'message': _('The scheduler will pick this connection up within moments.'),
                'type': 'success',
                'sticky': False,
            },
        }

    def action_request_full_resync(self):
        """Flag a full recompute and drop every cached content hash.

        Clearing the caches is the point: a stored ``h_dataset_*`` is only
        meaningful under the normalizer that produced it, and serving a stale
        hash as ``verified`` is a silent false pass — the worst failure a
        verification system can have.
        """
        for connection in self:
            connection._request_full_resync()
            connection.message_post(body=_(
                'Full resync requested by %s. Cached hashes cleared; the next '
                'verification pass recomputes from scratch.',
                self.env.user.display_name,
            ))
        return True

    def _request_full_resync(self) -> int:
        """Set the flag and invalidate cached hashes. Returns datasets touched."""
        self.ensure_one()
        self.sudo().write({'full_resync_requested': True})
        datasets = self.env['gdrive.dataset'].sudo().with_context(active_test=False).search(
            [('connection_id', '=', self.id)])
        if datasets:
            datasets.write({
                'h_dataset_sheet': False,
                'h_dataset_odoo': False,
                'bucket_hashes': {},
                'last_drive_version': False,
                'last_drive_modified': False,
                'last_odoo_count': 0,
                'last_odoo_max_write_date': False,
            })
        _logger.info(
            'Full resync requested for %s; cleared cached hashes on %d dataset(s).',
            self.display_name, len(datasets),
        )
        return len(datasets)

    # ------------------------------------------------------------------
    # Crons
    # ------------------------------------------------------------------
    @api.model
    def _cron_discover(self):
        """Cron ``ir_cron_gdrive_discover`` — delta (or full) discovery.

        Batch driver with a wall-clock budget, per-connection advisory lock, and
        a ``_trigger()`` continuation (SPEC §6).

        **Never raises.** Odoo 18 deactivates a scheduled action after repeated
        failures; a discovery cron that switches itself off leaves the mirror
        frozen while every downstream stage keeps reporting confidently on stale
        data.
        """
        deadline = time.monotonic() + CRON_BUDGET_SEC
        connections = self.sudo().search([('active', '=', True), ('state', '=', 'ok')])
        backlog = False
        for connection in connections:
            if time.monotonic() > deadline:
                backlog = True
                break
            if not connection._acquire_lock():
                _logger.info(
                    'Discovery skipped for %s: another run holds the advisory lock.',
                    connection.display_name)
                continue
            try:
                backlog = connection._discover(deadline, trigger='cron') or backlog
            except Exception as exc:  # noqa: BLE001 - a cron may never raise
                self.env.cr.rollback()
                _logger.exception('Discovery failed for connection %s.', connection.display_name)
                connection.browse(connection.id)._mark_error(exc)
                self.env.cr.commit()
        if backlog:
            trigger_cron(self.env, 'gdrive_odoo_sync.ir_cron_gdrive_discover')
        return True

    @api.model
    def _cron_full_resync(self):
        """Cron ``ir_cron_gdrive_full_resync`` — weekly full recompute (SPEC §6).

        The fast paths (Drive ``version``, ``modifiedTime``, cached hashes) are an
        optimization built on assumptions. This job is what catches the week one
        of those assumptions turns out to be false. Like every cron here it never
        raises.
        """
        connections = self.sudo().search([('active', '=', True)])
        for connection in connections:
            try:
                connection._request_full_resync()
                self.env.cr.commit()
            except Exception as exc:  # noqa: BLE001 - a cron may never raise
                self.env.cr.rollback()
                _logger.exception(
                    'Weekly full-resync flag could not be set for %s.', connection.display_name)
                connection.browse(connection.id)._mark_error(exc)
                self.env.cr.commit()
        _logger.info('Weekly full recompute requested for %d connection(s).', len(connections))
        return True

    # ------------------------------------------------------------------
    # Discovery (SPEC §5.1)
    # ------------------------------------------------------------------
    def _needs_full_enumeration(self) -> bool:
        """True when a delta poll cannot be trusted to be sufficient.

        Any of: an explicit full-resync request, no cursor at all, or a cursor
        that is not replayable (invalid, un-bootstrapped, or minted for a subject
        we no longer impersonate). Each of those means there is a window of time
        whose changes nobody will ever deliver, and only a full enumeration
        closes it.
        """
        self.ensure_one()
        if self.full_resync_requested:
            return True
        cursors = self.cursor_ids
        if not cursors:
            return True
        return any(not cursor._is_replayable(self) for cursor in cursors)

    def _discover(self, deadline: float, trigger: str = 'cron') -> bool:
        """Run one discovery pass. Returns True when work remains.

        Opens a ``gdrive.sync.run``, commits it immediately (so a later failure
        cannot roll back the row that subsequent log lines point at), then
        delegates to the full or delta path.
        """
        self.ensure_one()
        mode = 'full' if self._needs_full_enumeration() else 'delta'
        run = self.env['gdrive.sync.run']._start(
            self, trigger=trigger, mode=mode, stages=['discover'])
        self.env.cr.commit()
        run = run.browse(run.id)
        self._acquire_lock()

        remaining = False
        try:
            drive, _sheets, ctx = self._build_services()
            discovery = DriveDiscovery(drive, ctx)
            changes = DriveChanges(drive, ctx)
            if mode == 'full':
                remaining = self._discover_full(run, discovery, changes, deadline)
            else:
                remaining = self._discover_delta(run, discovery, changes, deadline)
            self.env.cr.commit()
        except GDriveError as exc:
            # A typed transport failure: the corpus we read is by definition
            # partial, so the run must not be usable as evidence that anything
            # disappeared. complete_read=False is what disarms the delete planner.
            self.env.cr.rollback()
            run = run.browse(run.id)
            run._mark_incomplete('DISCOVER_FAILED', redact(exc), stage='discover')
            run._log('DISCOVER_FAILED', 'Discovery failed: %s' % redact(exc),
                     level='error', stage='discover')
            self._mark_error(exc)
            self.env.cr.commit()
        finally:
            # Closing the run is best-effort by construction: if the failure that
            # brought us here also poisoned the cursor, an exception raised *in a
            # finally block* would replace the real one and the operator would
            # debug the wrong problem. Quota counters are bumped by the two paths
            # above, which own the service objects that hold them.
            try:
                run = run.browse(run.id)
                run._finish()
                self.env.cr.commit()
            except Exception:  # noqa: BLE001 - never mask the original failure
                self.env.cr.rollback()
                _logger.exception('Could not close the discovery run for %s.', self.display_name)
        return remaining

    def _discover_full(self, run, discovery: DriveDiscovery, changes: DriveChanges,
                       deadline: float) -> bool:
        """Full enumeration (SPEC §4.2, §5.1). Returns True when work remains.

        Ordering matters and is not arbitrary:

        1. Enumerate and upsert in committed batches.
        2. Only if the enumeration was *complete* — no ``incompleteSearch``, no
           exhausted budget — mark everything not seen as ``gone``. A partial
           read that marked the remainder gone would be the mass-deletion bug
           this whole system is designed to make impossible.
        3. Resolve the tree and apply scope rules once, at the end, because
           parents legitimately arrive after their children in a flat listing.
        4. Bootstrap the change cursors **last**. A token means "the world as of
           now"; minting it before the enumeration would open a window in which
           changes are neither enumerated nor delivered.
        """
        self.ensure_one()
        node_model = self.env['gdrive.node']
        started_at = fields.Datetime.now()
        batch: List[dict] = []
        seen_drive_ids: Set[str] = set()
        budget_exhausted = False
        lock_lost = False
        total = 0

        for meta in discovery.crawl(
            include_shared_drives=self.include_shared_drives,
            include_shared_with_me=self.include_shared_with_me,
            include_trashed=self.include_trashed,
            corpora_mode=self.corpora_mode,
        ):
            drive_id = meta.get('_drive_id') or ''
            if drive_id:
                seen_drive_ids.add(drive_id)
            batch.append(meta)
            if len(batch) >= COMMIT_BATCH:
                total += len(batch)
                node_model._upsert_from_drive(self, batch, run)
                batch = []
                self.env.cr.commit()
                run = run.browse(run.id)
                if not self._acquire_lock():
                    lock_lost = True
                    break
            if time.monotonic() > deadline:
                budget_exhausted = True
                break

        if batch and not lock_lost:
            total += len(batch)
            node_model._upsert_from_drive(self, batch, run)
            self.env.cr.commit()
            run = run.browse(run.id)
            self._acquire_lock()

        run._bump(
            drive_units_used=int(getattr(discovery, 'units_used', 0)),
        )
        complete = discovery.complete_read and not budget_exhausted and not lock_lost
        if not complete:
            reason = ('BUDGET_EXHAUSTED' if budget_exhausted else
                      'LOCK_LOST' if lock_lost else 'INCOMPLETE_SEARCH')
            run._mark_incomplete(
                reason,
                'Full enumeration of %s did not complete (%d file(s) seen). Nothing '
                'will be marked gone from this run.' % (self.display_name, total),
                stage='discover')
        else:
            stale = self.env['gdrive.node'].sudo().with_context(active_test=False).search([
                ('connection_id', '=', self.id),
                ('state', '!=', 'gone'),
                '|', ('last_seen_date', '=', False), ('last_seen_date', '<', started_at),
            ])
            if stale:
                stale._mark_gone(run=run, reason='not_seen_in_full_enumeration')
                _logger.info('Full enumeration marked %d node(s) gone on %s.',
                             len(stale), self.display_name)

        self.env['gdrive.node']._resolve_tree(self)
        self.env['gdrive.node']._apply_scope_rules(self, run)
        self.env.cr.commit()
        run = run.browse(run.id)

        run._log('DISCOVER_FULL',
                 'Full enumeration saw %d file(s); complete_read=%s.' % (total, complete),
                 level='info', stage='discover')

        if complete:
            self._bootstrap_cursors(changes, seen_drive_ids)
            self.sudo().write({'full_resync_requested': False, 'last_error': False})
            self.env.cr.commit()

        return budget_exhausted or lock_lost

    def _bootstrap_cursors(self, changes: DriveChanges, drive_ids: Set[str]) -> None:
        """Mint a fresh start token for the user corpus and each shared drive.

        Called only after a complete enumeration. A cursor whose bootstrap fails
        is left in its previous state so the next run enumerates again rather
        than starting delta polls from a token that does not exist.
        """
        self.ensure_one()
        adapter = _StartTokenAdapter(changes)
        cursor_model = self.env['gdrive.change.cursor']
        targets = [''] + sorted(drive_ids)
        for drive_id in targets:
            cursor = cursor_model._get_or_create(self, drive_id=drive_id)
            try:
                cursor._bootstrap(adapter)
            except Exception as exc:  # noqa: BLE001 - one bad drive must not sink the pass
                self.env.cr.rollback()
                cursor = cursor.browse(cursor.id)
                cursor._mark_invalid('bootstrap_failed')
                _logger.warning(
                    'Could not bootstrap the change cursor for %s (drive %r): %s',
                    self.display_name, drive_id or 'user corpus', redact(exc))
            self.env.cr.commit()

    def _discover_delta(self, run, discovery: DriveDiscovery, changes: DriveChanges,
                        deadline: float) -> bool:
        """Replay every cursor through the Changes API (SPEC §4.3, §5.1).

        The invariant that makes a crash lose nothing: **commit the mirrored data
        first, persist the token last.** Committing the token first would mean a
        crash in between loses those changes permanently — the next poll starts
        after them and nothing ever re-reports them. Doing it in this order can
        only cause a replay, and every handler in lane D is idempotent.
        """
        self.ensure_one()
        node_model = self.env['gdrive.node']
        remaining = False
        touched = 0

        for cursor in self.cursor_ids:
            if time.monotonic() > deadline:
                remaining = True
                run._log('BUDGET_EXHAUSTED',
                         'Delta budget exhausted before polling every cursor.',
                         level='info', stage='discover')
                break
            if not cursor._is_replayable(self):
                # Not an error: the next pass will see the invalid cursor and
                # escalate the whole connection to a full enumeration.
                continue
            drive_id = cursor.drive_id or ''
            try:
                records, new_token = changes.poll(cursor.page_token, drive_id=drive_id or None)
            except GDriveTokenInvalid as exc:
                cursor._mark_invalid('token_invalid')
                run._mark_incomplete(
                    'CURSOR_INVALID',
                    'The change cursor for %s expired (%s); a full re-enumeration '
                    'is required and has been scheduled.'
                    % (drive_id or 'the user corpus', redact(exc)),
                    stage='discover')
                self.env.cr.commit()
                run = run.browse(run.id)
                remaining = True
                continue

            metas = []
            removed_ids = []
            for change in records:
                file_id = changes.file_id_of(change)
                if not file_id:
                    continue
                if changes.is_removal(change):
                    removed_ids.append(file_id)
                    continue
                payload = change.get('file') or {}
                if payload.get('id'):
                    # The changes field mask carries the same per-file mask as
                    # discovery, so no follow-up files.get is needed.
                    payload = dict(payload)
                    payload.setdefault('_drive_id', change.get('driveId') or drive_id or '')
                    metas.append(payload)
                else:
                    removed_ids.append(file_id)

            if metas:
                node_model._upsert_from_drive(self, metas, run)
                touched += len(metas)
            if removed_ids:
                gone = node_model.sudo().with_context(active_test=False).search([
                    ('connection_id', '=', self.id),
                    ('google_id', 'in', removed_ids),
                ])
                if gone:
                    # `removed` means deleted OR trashed OR permission-revoked OR
                    # moved out of scope. It never unlinks and never reaches the
                    # business-record delete planner (SPEC §4.3).
                    gone._mark_gone(run=run, reason='changes_removed')

            # Data first...
            self.env.cr.commit()
            run = run.browse(run.id)
            cursor = cursor.browse(cursor.id)
            # ...token last.
            cursor._persist_token(new_token)
            self.env.cr.commit()
            if not self._acquire_lock():
                _logger.warning(
                    'Lost the advisory lock for %s mid-delta; stopping this pass.',
                    self.display_name)
                remaining = True
                break

        if touched:
            node_model._resolve_tree(self)
            node_model._apply_scope_rules(self, run)
            self.env.cr.commit()
            run = run.browse(run.id)

        run._bump(drive_units_used=int(getattr(changes, 'units_used', 0)))
        run._log('DISCOVER_DELTA',
                 'Delta poll applied %d changed file(s) across %d cursor(s).'
                 % (touched, len(self.cursor_ids)),
                 level='info', stage='discover')
        return remaining
