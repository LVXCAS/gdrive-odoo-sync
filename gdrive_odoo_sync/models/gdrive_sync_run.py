# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. See LICENSE (LGPL-3) for full copyright and licensing details.
"""``gdrive.sync.run`` — the run log, and the module's cron plumbing.

WHY the run is a first-class record rather than a log line: ``complete_read`` is
a *proof obligation*. SPEC §9.6 forbids the delete planner from running when any
API call failed, any page was lost, or any read was partial — and "was that read
complete?" has to be answerable hours later, transactionally, by the planner.
A boolean on a persisted run record is the only representation that survives the
worker that computed it.

This module also owns the small amount of cron infrastructure shared by every
driver in lane D (budget constant, batch size, cron re-trigger helper), because
the alternative — a constants module not present in the file manifest — would be
a file nobody owns.
"""

import gzip
import io
import json
import logging
import time

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .gdrive_sync_run_line import STAGE_SELECTION

_logger = logging.getLogger(__name__)

#: Wall-clock budget for a single cron invocation, in seconds (SPEC §6).
#: Deliberately well under any reasonable worker ``limit_time_cpu``/``real`` so
#: the driver always gets to commit and re-trigger itself rather than being shot
#: mid-batch. Work left over is picked up by ``_trigger()``, not by waiting a
#: whole interval.
CRON_BUDGET_SEC = 600

#: Records processed between ``cr.commit()`` calls (SPEC §6). Small enough that a
#: crash loses little work, large enough that commit overhead stays negligible.
COMMIT_BATCH = 200

#: Rows handed to a single ``create()`` call. Larger than ``COMMIT_BATCH``
#: because staged rows are cheap, uniform inserts with no compute chain.
CREATE_BATCH = 500


def trigger_cron(env, xml_id: str) -> None:
    """Ask Odoo to run cron ``xml_id`` again as soon as a worker is free.

    WHY: when a driver exhausts :data:`CRON_BUDGET_SEC` with work remaining, the
    naive behaviour is to wait a full interval — which for the weekly resync
    means a week. ``_trigger()`` schedules an immediate follow-up instead, so a
    large backlog drains in consecutive short runs rather than over days.

    Never raises: a missing cron record (module partially upgraded, cron deleted
    by an administrator) must not turn a successful batch into a failed one.
    """
    cron = env.ref(xml_id, raise_if_not_found=False)
    if not cron:
        _logger.warning("Cannot re-trigger %s: cron record not found.", xml_id)
        return
    try:
        cron.sudo()._trigger()
    except Exception:  # pragma: no cover - depends on live cron table state
        _logger.exception("Could not re-trigger %s; the next scheduled run will pick up the backlog.", xml_id)


class GdriveSyncRun(models.Model):
    """One execution of one or more pipeline stages against one connection."""

    _name = 'gdrive.sync.run'
    _description = 'Google Drive Sync Run'
    _order = 'date_start desc'
    _rec_name = 'name'

    name = fields.Char(string='Reference', required=True, copy=False, readonly=True, index=True)
    connection_id = fields.Many2one(
        'gdrive.connection', string='Connection',
        required=True, index=True, ondelete='cascade',
    )
    company_id = fields.Many2one(
        related='connection_id.company_id', store=True, index=True, string='Company',
        help='Denormalized so the multi-company record rule of SPEC §7.3 can be '
             'a plain domain instead of a join.',
    )
    trigger = fields.Selection(
        [('cron', 'Scheduler'), ('manual', 'Manual'), ('button', 'Button')],
        string='Trigger', default='cron', required=True, index=True,
    )
    stages = fields.Char(
        string='Stages',
        help='Comma-joined names of the stages actually executed, so a run that '
             'died after discover is distinguishable from one that never tried '
             'to ingest.',
    )
    mode = fields.Selection(
        [('delta', 'Delta'), ('full', 'Full')], string='Mode',
        default='delta', required=True, index=True,
    )
    date_start = fields.Datetime(string='Start', default=fields.Datetime.now, required=True, index=True)
    date_end = fields.Datetime(string='End')
    duration_sec = fields.Float(string='Duration (s)', aggregator='sum')
    state = fields.Selection(
        [
            ('running', 'Running'),
            ('done', 'Done'),
            ('partial', 'Partial'),
            ('failed', 'Failed'),
            ('aborted', 'Aborted'),
        ],
        string='Status', default='running', required=True, index=True,
    )

    complete_read = fields.Boolean(
        string='Complete Read', default=False,
        help='Run-level proof that nothing was missed. False whenever a Drive or '
             'Sheets call failed, a token expired, a page was lost, or a dataset '
             'read was partial. SPEC §9.6 forbids the delete planner from '
             'running while this is False, because every read failure looks '
             'exactly like "everything was deleted".',
    )
    incomplete_reason = fields.Char(
        string='Incomplete Reason', readonly=True,
        help='The first machine code that set complete_read to False. The first '
             'one is kept, not the last: it is the root cause, and everything '
             'after it is downstream noise.',
    )

    nodes_seen = fields.Integer(string='Nodes Seen', aggregator='sum')
    nodes_ingested = fields.Integer(string='Nodes Ingested', aggregator='sum')
    attachments_written = fields.Integer(string='Attachments Written', aggregator='sum')
    datasets_seen = fields.Integer(string='Datasets Seen', aggregator='sum')
    rows_staged = fields.Integer(string='Rows Staged', aggregator='sum')
    rows_quarantined = fields.Integer(string='Rows Quarantined', aggregator='sum')
    records_created = fields.Integer(string='Records Created', aggregator='sum')
    records_updated = fields.Integer(string='Records Updated', aggregator='sum')
    records_soft_deleted = fields.Integer(string='Records Soft-Deleted', aggregator='sum')
    drift_count = fields.Integer(string='Drifts', aggregator='sum')
    error_count = fields.Integer(string='Errors', aggregator='sum')
    warning_count = fields.Integer(string='Warnings', aggregator='sum')

    drive_units_used = fields.Integer(string='Drive Units Used', aggregator='sum')
    sheets_reads_used = fields.Integer(string='Sheets Reads Used', aggregator='sum')

    line_ids = fields.One2many('gdrive.sync.run.line', 'run_id', string='Log Lines')
    log_attachment_id = fields.Many2one(
        'ir.attachment', string='Log Archive', readonly=True, ondelete='set null',
        help='The full structured log as gzipped JSONL. Kept separately from '
             'line_ids so housekeeping can prune the rows while the artefact '
             'survives, and so a 200 000-line run does not have to be rendered '
             'in a list view to be readable.',
    )

    _sql_constraints = [
        ('name_uniq', 'unique(name)', 'A sync run reference must be unique.'),
    ]

    # ------------------------------------------------------------------
    # Naming
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        """Assign the ``SYNC/…`` sequence reference at create time.

        WHY at create and not as a compute: the reference is quoted in chatter
        digests, drift reports and support tickets. It must be immutable and it
        must exist before the first log line is written.
        """
        for vals in vals_list:
            if not vals.get('name'):
                vals['name'] = self.env['ir.sequence'].sudo().next_by_code('gdrive.sync.run') or '/'
        return super().create(vals_list)

    @api.depends('name', 'connection_id')
    def _compute_display_name(self):
        """Odoo 18: ``name_get()`` is removed and silently never called."""
        for run in self:
            run.display_name = '%s — %s' % (run.name or '/', run.connection_id.name or '')

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @api.model
    def _start(self, connection, trigger='cron', mode='delta', stages=None):
        """Open a run for ``connection`` and return it.

        Created with ``sudo()`` because crons run as ``base.user_root`` but a
        *button* trigger runs as a manager, who by SPEC §7.2 has no create right
        on the run log. The run log is an audit artefact of the system, not a
        user document, so the system writes it regardless of who asked.
        """
        stage_names = list(stages or [])
        return self.sudo().create({
            'connection_id': connection.id,
            'trigger': trigger,
            'mode': mode,
            'stages': ','.join(stage_names),
            'date_start': fields.Datetime.now(),
            'state': 'running',
            'complete_read': True,
        })

    def _add_stage(self, stage):
        """Record that ``stage`` actually executed in this run."""
        self.ensure_one()
        current = [s for s in (self.stages or '').split(',') if s]
        if stage not in current:
            current.append(stage)
            self.sudo().write({'stages': ','.join(current)})

    def _log(self, code, message, level='info', stage=None, node=None,
             dataset=None, payload=None, duration_ms=0):
        """Write one structured log line and mirror it to the server log.

        Returns the created line so callers can attach it to a drift record.

        WHY mirror to ``_logger`` as well: during an incident the operator is
        usually tailing the Odoo log, and a database row that is still inside an
        uncommitted transaction is invisible there. The duplication is cheap and
        it is the difference between debugging live and debugging tomorrow.
        """
        self.ensure_one()
        line = self.env['gdrive.sync.run.line'].sudo().create({
            'run_id': self.id,
            'node_id': node.id if node else False,
            'dataset_id': dataset.id if dataset else False,
            'stage': stage,
            'level': level,
            'code': code,
            'message': message,
            'duration_ms': int(duration_ms or 0),
            'payload': payload,
        })
        if level == 'error':
            self._bump(error_count=1)
            _logger.error('[%s] %s: %s', self.name, code, line.message)
        elif level == 'warning':
            self._bump(warning_count=1)
            _logger.warning('[%s] %s: %s', self.name, code, line.message)
        else:
            _logger.info('[%s] %s: %s', self.name, code, line.message)
        return line

    def _bump(self, **counters):
        """Increment integer counters on the run.

        Read-modify-write rather than a raw SQL ``+=`` on purpose: a run is
        touched by exactly one worker (the advisory lock in
        :meth:`gdrive.connection._acquire_lock` guarantees it), so there is no
        concurrency to lose, and the ORM path keeps the values visible to the
        rest of the transaction.
        """
        if not self:
            return
        self.ensure_one()
        vals = {}
        for field_name, delta in counters.items():
            vals[field_name] = (self[field_name] or 0) + delta
        if vals:
            self.sudo().write(vals)

    def _mark_incomplete(self, code, message, stage=None, node=None, dataset=None, payload=None):
        """Set ``complete_read = False`` and log why.

        This is the single most consequential state change in the whole system:
        it is what stands between a transient 503 and a plan that soft-deletes
        four thousand partners. It is therefore a named method, logged at
        ``warning`` at minimum, and the *first* reason is retained.
        """
        self.ensure_one()
        vals = {'complete_read': False}
        if not self.incomplete_reason:
            vals['incomplete_reason'] = code
        self.sudo().write(vals)
        return self._log(code, message, level='warning', stage=stage,
                         node=node, dataset=dataset, payload=payload)

    def _finish(self, state=None):
        """Close the run, derive the final state, and materialize the log archive.

        The derived state is deliberately conservative: any error line at all
        makes the run ``partial``, even if every entity eventually succeeded on
        retry, because "was this run clean?" must never be answered optimistically.
        """
        for run in self:
            end = fields.Datetime.now()
            final = state
            if final is None:
                if run.error_count and run.nodes_seen + run.datasets_seen == 0:
                    final = 'failed'
                elif run.error_count or not run.complete_read:
                    final = 'partial'
                else:
                    final = 'done'
            duration = 0.0
            if run.date_start:
                duration = (end - run.date_start).total_seconds()
            run.sudo().write({
                'date_end': end,
                'duration_sec': duration,
                'state': final,
            })
            run._write_log_attachment()
        return True

    def _write_log_attachment(self):
        """Materialize ``line_ids`` as a gzipped JSONL ``ir.attachment``.

        JSONL rather than JSON: the file stays greppable and streamable, and a
        truncated download is still parseable up to the last complete line.
        Gzipped because a discovery run over 40 000 Drive objects produces a log
        that compresses roughly 12:1 and would otherwise dominate the filestore.

        Failure here is logged, never raised — an unwritable filestore must not
        turn a good run into a failed one.
        """
        self.ensure_one()
        if not self.line_ids:
            return False
        try:
            buf = io.BytesIO()
            with gzip.GzipFile(fileobj=buf, mode='wb', mtime=0) as gz:
                for line in self.line_ids:
                    gz.write((json.dumps(line.to_log_dict(), ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8'))
            attachment = self.env['ir.attachment'].sudo().create({
                'name': '%s.jsonl.gz' % (self.name or 'run').replace('/', '-'),
                'raw': buf.getvalue(),
                'res_model': 'gdrive.sync.run',
                'res_id': self.id,
                'mimetype': 'application/gzip',
                'type': 'binary',
            })
            self.sudo().write({'log_attachment_id': attachment.id})
            return attachment
        except Exception:  # pragma: no cover - filestore dependent
            _logger.exception("Could not write the log archive for run %s.", self.name)
            return False

    # ------------------------------------------------------------------
    # Housekeeping cron
    # ------------------------------------------------------------------
    @api.model
    def _cron_gc(self):
        """Housekeeping (SPEC §6): prune runs, expire plans, drop obsolete rows.

        Cron entry point for ``ir_cron_gdrive_gc``. Like every cron in this
        module it **must not raise**: Odoo 18 auto-deactivates a scheduled action
        after repeated failures, and a housekeeping job that disables itself
        leaves the database growing without bound and nobody notices for months.
        """
        started = time.monotonic()
        icp = self.env['ir.config_parameter'].sudo()
        try:
            retention_days = int(icp.get_param('gdrive_odoo_sync.run_retention_days', 90))
        except (TypeError, ValueError):
            retention_days = 90

        # 1. Old runs. Deleting the run cascades its lines; the log attachment is
        #    dropped with it because ir.attachment is cleaned by res_model/res_id
        #    garbage collection, and the artefact has no value once the run row is
        #    gone.
        try:
            cutoff = fields.Datetime.subtract(fields.Datetime.now(), days=max(retention_days, 1))
            old_runs = self.sudo().search([('date_start', '<', cutoff), ('state', '!=', 'running')])
            count_runs = len(old_runs)
            for chunk_start in range(0, count_runs, COMMIT_BATCH):
                old_runs[chunk_start:chunk_start + COMMIT_BATCH].unlink()
                self.env.cr.commit()
            _logger.info("Housekeeping removed %d sync run(s) older than %d days.", count_runs, retention_days)
        except Exception:
            self.env.cr.rollback()
            _logger.exception("Housekeeping could not prune old sync runs.")

        # 2. Expired plans. A plan is a *fingerprinted* promise about the state of
        #    the world; past its expiry the promise is void and it must not be
        #    applicable, so the state is moved rather than merely checked at apply
        #    time.
        try:
            plans = self.env['gdrive.plan'].sudo().search([
                ('state', 'in', ('preview', 'approved')),
                ('expiry_date', '<', fields.Datetime.now()),
            ])
            if plans:
                plans.write({'state': 'expired'})
                _logger.info("Housekeeping expired %d stale plan(s).", len(plans))
            self.env.cr.commit()
        except Exception:
            self.env.cr.rollback()
            _logger.exception("Housekeeping could not expire stale plans.")

        # 3. Obsolete staged rows. State 'obsolete' means a human or the promoter
        #    has explicitly retired the row; 30 days is long enough to answer
        #    "what did this look like before?" and short enough to bound growth.
        try:
            row_cutoff = fields.Datetime.subtract(fields.Datetime.now(), days=30)
            rows = self.env['gdrive.staged.row'].sudo().search([
                ('state', '=', 'obsolete'),
                ('write_date', '<', row_cutoff),
            ])
            total = len(rows)
            for chunk_start in range(0, total, CREATE_BATCH):
                rows[chunk_start:chunk_start + CREATE_BATCH].unlink()
                self.env.cr.commit()
            _logger.info("Housekeeping removed %d obsolete staged row(s).", total)
        except Exception:
            self.env.cr.rollback()
            _logger.exception("Housekeeping could not prune obsolete staged rows.")

        _logger.info("gdrive housekeeping finished in %.1fs.", time.monotonic() - started)
        return True

    # ------------------------------------------------------------------
    # UI helpers
    # ------------------------------------------------------------------
    def action_open_lines(self):
        """Open this run's log lines.

        ``view_mode`` is ``list,form`` — Odoo 18 removed ``tree`` from action
        view modes and an action declaring ``tree,form`` fails to load.
        """
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Run Log'),
            'res_model': 'gdrive.sync.run.line',
            'view_mode': 'list,form',
            'domain': [('run_id', '=', self.id)],
            'context': {'default_run_id': self.id, 'search_default_group_by_stage': 1},
        }

    def action_download_log(self):
        """Return a download action for the gzipped JSONL archive."""
        self.ensure_one()
        attachment = self.log_attachment_id or self._write_log_attachment()
        if not attachment:
            raise UserError(_('This run has no log archive: it produced no log lines.'))
        return {
            'type': 'ir.actions.act_url',
            'url': '/web/content/%s?download=true' % attachment.id,
            'target': 'self',
        }

    @api.model
    def _stage_names(self):
        """Expose the canonical stage vocabulary to sibling models.

        Kept as a method rather than re-importing the selection list all over
        lane D, so the eight stage names of SPEC §5 have exactly one definition.
        """
        return [code for code, _label in STAGE_SELECTION]
