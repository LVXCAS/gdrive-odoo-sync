# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. See LICENSE (LGPL-3) for full copyright and licensing details.
"""``gdrive.sync.run.line`` — one structured, machine-readable log record.

WHY a real model instead of ``_logger`` alone: the operator of this system needs
to answer "why does that dataset say *blocked*?" six hours after the cron ran,
from the Odoo UI, without shell access to a server log that has already rotated.
A run line carries a *stable machine code* (``EXPORT_SIZE_LIMIT``, ``TAB_MISSING``,
``RATE_LIMITED``…) so that support answers can be written against codes rather
than against English sentences that will be reworded next release.

Every message and every payload is pushed through :func:`services.errors.redact`
on the way in. That is not belt-and-braces: the service-account private key is a
bearer credential for the whole of Lucas's Drive, exception strings from
``google-auth`` routinely embed the credential dict, and ``ir.logging`` /
``gdrive.sync.run.line`` are both readable by every ``group_gdrive_user``.
Redacting at the *write* boundary is the only place it can be enforced once.
"""

import logging

from odoo import api, fields, models

from ..services.errors import redact

_logger = logging.getLogger(__name__)

#: The eight pipeline stages of SPEC §5, in execution order.
STAGE_SELECTION = [
    ('discover', 'Discover'),
    ('classify', 'Classify'),
    ('ingest', 'Ingest'),
    ('stage', 'Stage'),
    ('promote', 'Promote'),
    ('verify', 'Verify'),
    ('report', 'Report'),
    ('heal', 'Heal'),
]

LEVEL_SELECTION = [
    ('info', 'Info'),
    ('warning', 'Warning'),
    ('error', 'Error'),
]

#: Hard cap on a single stored message. A googleapiclient traceback carrying a
#: full HTTP body can run to hundreds of kilobytes; a cron that hits the same
#: error 5 000 times would otherwise bloat the database beyond usefulness.
MAX_MESSAGE_CHARS = 8000


def _redact_json(value, _depth=0):
    """Recursively redact secrets out of an arbitrary JSON-able payload.

    WHY recursive: the interesting payloads are nested — ``{'response': {'error':
    {'message': '...private_key...'}}}``. Redacting only the top level would let
    a credential through one dict deeper, which is exactly where API client
    libraries put it.

    Depth is bounded because ``payload`` arrives from callers we do not control
    and a self-referential structure would otherwise loop forever.
    """
    if _depth > 8:
        return '<truncated: nesting too deep>'
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {str(k): _redact_json(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_json(v, _depth + 1) for v in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact(str(value))


class GdriveSyncRunLine(models.Model):
    """A single structured event emitted during a :class:`gdrive.sync.run`."""

    _name = 'gdrive.sync.run.line'
    _description = 'Google Drive Sync Run Log Line'
    _order = 'run_id, id'

    run_id = fields.Many2one(
        'gdrive.sync.run', string='Run',
        required=True, index=True, ondelete='cascade',
    )
    node_id = fields.Many2one(
        'gdrive.node', string='Drive Node',
        index=True, ondelete='set null',
        help='The Drive object this line is about, when the event is file-scoped.',
    )
    dataset_id = fields.Many2one(
        'gdrive.dataset', string='Dataset',
        index=True, ondelete='set null',
        help='The spreadsheet tab this line is about, when the event is tab-scoped.',
    )
    stage = fields.Selection(STAGE_SELECTION, string='Stage', index=True)
    level = fields.Selection(LEVEL_SELECTION, string='Level', default='info', index=True)
    code = fields.Char(
        string='Code', index=True,
        help='Stable machine code, e.g. EXPORT_SIZE_LIMIT, TAB_MISSING, '
             'RATE_LIMITED. Support procedures are written against these, never '
             'against the human message.',
    )
    message = fields.Text(string='Message')
    duration_ms = fields.Integer(string='Duration (ms)')
    payload = fields.Json(
        string='Payload',
        help='Structured context. Unsearchable by design — anything that must '
             'be filtered on lives in a real column (code, level, stage).',
    )

    @api.depends('code', 'message')
    def _compute_display_name(self):
        """Odoo 18 replaces ``name_get()`` with ``_compute_display_name``.

        Defining it here (rather than relying on ``_rec_name``) means a run line
        referenced from a ``Many2one`` in a drift or a plan reads as
        ``TAB_MISSING: 'Q3 Pipeline' is gone`` instead of ``gdrive.sync.run.line,42``.
        """
        for line in self:
            code = line.code or (line.level or 'info').upper()
            text = (line.message or '').strip().splitlines()
            head = text[0] if text else ''
            line.display_name = ('%s: %s' % (code, head))[:120] if head else code

    @api.model_create_multi
    def create(self, vals_list):
        """Redact every inbound message and payload before it reaches storage.

        WHY here and not at each call site: there are dozens of call sites across
        lanes D and E, they will grow, and one that forgets leaks a private key
        into a table readable by every internal user. A single enforcement point
        at the ORM boundary cannot be forgotten.
        """
        for vals in vals_list:
            if vals.get('message'):
                vals['message'] = redact(str(vals['message']))[:MAX_MESSAGE_CHARS]
            if vals.get('payload') is not None:
                vals['payload'] = _redact_json(vals['payload'])
        return super().create(vals_list)

    def write(self, vals):
        """Apply the same redaction to updates as to creates."""
        if vals.get('message'):
            vals = dict(vals, message=redact(str(vals['message']))[:MAX_MESSAGE_CHARS])
        if vals.get('payload') is not None:
            vals = dict(vals, payload=_redact_json(vals['payload']))
        return super().write(vals)

    def to_log_dict(self) -> dict:
        """Return this line as a plain dict for the gzipped JSONL run artefact.

        Used by :meth:`gdrive.sync.run._write_log_attachment`. Kept on the line
        model so the serialization format lives next to the fields it serializes.
        """
        self.ensure_one()
        return {
            'id': self.id,
            'stage': self.stage or '',
            'level': self.level or 'info',
            'code': self.code or '',
            'message': self.message or '',
            'duration_ms': self.duration_ms or 0,
            'node_google_id': self.node_id.google_id or '',
            'dataset_id': self.dataset_id.id or 0,
            'payload': self.payload if self.payload is not None else {},
        }
