# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. Licensed under LGPL-3.
"""``gdrive.connection.test.wizard`` — the day-one artefact (SPEC §2.4).

WHY this wizard exists before anything else in the product
------------------------------------------------------------
A Google Cloud service account is a *principal*, not a view onto an
organisation. Without domain-wide delegation it authenticates perfectly,
returns HTTP 200, and lists **zero files** — because "user" means the service
account, whose own Drive is empty and quota-less. An empty read is
indistinguishable from "everything was deleted", which is why every delete
guard in SPEC §9.6 exists, and why setup is proved probe by probe here rather
than behind one green tick:

    P1 key parses            P5 shared-with-me reachable
    P2 token mints           P6 shared drives enumerable
    P3 impersonation works   P7 Sheets API reachable
    P4 corpus is non-empty

Each probe is caught and rendered individually — one probe raising must never
stop the rest from running, and none of this may ever reach the user as a
Python traceback. P4 returning zero while P3 passed is rendered as a RED
ERROR, never as an empty state, because for a real Workspace subject an empty
Drive is not a plausible reading of reality; it is the signature of a broken
delegation grant. ``connection.state`` is moved to ``'ok'`` only when P1-P4
all pass, reusing the bookkeeping helpers already on ``gdrive.connection``
rather than duplicating them here.

Probe results are rendered straight into ``result_html`` as one HTML block
instead of one-line-per-record in a 22nd model (FILE_MANIFEST §3.16). Every
piece of text that could contain attacker-influenced content — a Drive file
title, an error message that may echo back part of a query — is escaped with
``odoo.tools.html_escape`` at render time, never assumed safe because it
"looks like" a filename.
"""

import itertools
import logging
from typing import Dict, List, Optional

from odoo import _, fields, models
from odoo.exceptions import UserError
from odoo.tools import html_escape

from ..services.drive_discovery import DriveDiscovery
from ..services.errors import redact
from ..services.google_auth import SCOPES_STRING, refresh_credentials
from ..services.mimetypes import MIME_SPREADSHEET

_logger = logging.getLogger(__name__)

STATE_SELECTION = [
    ('setup', 'Setup'),
    ('result', 'Result'),
]

#: Sample size for every bounded probe query (P4, P5). Matches SPEC §2.4's
#: ``pageSize=10`` exactly; only this many items are ever pulled off the
#: (lazily paginated) generators the discovery service returns, so a probe
#: never triggers a second page fetch merely to prove non-emptiness.
SAMPLE_SIZE = 10

#: Ordered, human labels for every probe, reused both when a probe actually
#: runs and when it is reported ``skip`` because an earlier probe failed.
PROBE_LABELS = [
    ('P1', 'Key parses'),
    ('P2', 'Token mints'),
    ('P3', 'Impersonation works'),
    ('P4', 'Corpus is non-empty'),
    ('P5', 'Shared-with-me reachable'),
    ('P6', 'Shared drives enumerable'),
    ('P7', 'Sheets API reachable'),
]


def _probe_result(code: str, label: str, status: str, detail: str) -> Dict[str, str]:
    """Build one probe row. ``status`` is one of ``pass`` / ``fail`` / ``skip``."""
    return {'code': code, 'label': label, 'status': status, 'detail': detail or ''}


class GdriveConnectionTestWizard(models.TransientModel):
    """Runs probes P1-P7 against one connection and reports each individually."""

    _name = 'gdrive.connection.test.wizard'
    _description = 'Test Google Drive Connection'

    connection_id = fields.Many2one(
        'gdrive.connection', string='Connection', required=True,
        help='The connection to test. Nothing is written to Drive or to Odoo by '
             'this wizard except this connection\'s status and test timestamp.',
    )
    state = fields.Selection(
        STATE_SELECTION, string='Status', default='setup', required=True,
        help='"setup" shows the connection picker; "result" shows the probe '
             'report after Run Probes has executed at least once.',
    )
    result_html = fields.Html(
        string='Result', readonly=True, sanitize=False, copy=False,
        help='The per-probe pass/fail report. Rendered here, as one HTML block, '
             'instead of a 22nd model with one record per probe line (SPEC §3.16).',
    )
    sample_file_ids = fields.Text(
        string='Sample Files', readonly=True, copy=False,
        help='The first files seen by P4, one title per line. Seeing files owned '
             'by other people here is expected: we act as the impersonated user, '
             'so anything shared with them is visible to us.',
    )

    # ------------------------------------------------------------------
    # Button
    # ------------------------------------------------------------------
    def action_run_probes(self):
        """Run P1-P7 against ``connection_id`` and render the report.

        Every probe is individually try/excepted: a probe that raises is
        recorded as a failed probe with a redacted message, never as a
        traceback shown to the user, and never stops the probes after it.
        """
        self.ensure_one()
        connection = self.connection_id
        if not connection:
            raise UserError(_('Select a connection to test first.'))

        probes: List[Dict[str, str]] = []
        sample_titles: List[str] = []
        passed = {'P1': False, 'P2': False, 'P3': False, 'P4': False}

        # ---- P1: key parses (pure, offline — always attempted) ---------
        info: Optional[dict] = None
        try:
            info = connection._load_key_info()
        except Exception as exc:  # noqa: BLE001 - rendered as a failed probe
            probes.append(_probe_result('P1', _('Key parses'), 'fail', redact(exc)))
        else:
            passed['P1'] = True
            probes.append(_probe_result(
                'P1', _('Key parses'), 'pass',
                _('client_email, client_id and private_key are all present '
                  '(client_email=%s).') % (info.get('client_email') or ''),
            ))

        # ---- Consumer @gmail.com subjects can never be impersonated ----
        # WHY this is checked here, before any network call: DWD has no admin
        # console entry point for a consumer account, so P2-P7 would each
        # fail with a confusing, unrelated-looking error. Failing fast with
        # one clear explanation is more actionable than six noisy failures.
        gmail_blocked = (
            connection.auth_mode == 'dwd'
            and (connection.subject_email or '').strip().lower().endswith('@gmail.com')
        )
        if gmail_blocked:
            remediation = _(
                'Domain-wide delegation cannot impersonate a consumer @gmail.com '
                'account. "%s" will never be a valid impersonation subject — there '
                'is no Workspace admin console entry that can grant it delegation. '
                'Set "Subject Email" to a real user of the Workspace domain instead. '
                'A @gmail.com account\'s files reach this system only through what '
                'it explicitly shared with that Workspace subject, never through '
                'impersonation.'
            ) % (connection.subject_email or '')
            for code, label in PROBE_LABELS[1:]:
                probes.append(_probe_result(code, _(label), 'skip', remediation))
            connection._mark_error(
                _('Subject email %s is a consumer @gmail.com account; domain-wide '
                  'delegation cannot impersonate it.') % (connection.subject_email or ''),
                tested=True,
            )
            self.write({
                'state': 'result',
                'result_html': self._render_result_html(probes),
                'sample_file_ids': False,
            })
            return self._reopen()

        # ---- P2: token mints --------------------------------------------
        creds = None
        if info is not None:
            try:
                creds = connection._credentials(info=info)
                refresh_credentials(creds, subject=connection.subject_email or '')
            except Exception as exc:  # noqa: BLE001 - rendered as a failed probe
                probes.append(_probe_result('P2', _('Token mints'), 'fail', redact(exc)))
            else:
                passed['P2'] = True
                probes.append(_probe_result(
                    'P2', _('Token mints'), 'pass',
                    _('An access token was obtained successfully.'),
                ))
        else:
            probes.append(_probe_result(
                'P2', _('Token mints'), 'skip', _('Skipped: no usable key (see P1).'),
            ))

        # ---- Build the Drive/Sheets services once, for P3-P7 ------------
        discovery: Optional[DriveDiscovery] = None
        sheets = None
        services_error = ''
        if info is not None and passed['P2']:
            try:
                drive, sheets, ctx = connection._build_services(info=info)
                discovery = DriveDiscovery(drive, ctx)
            except Exception as exc:  # noqa: BLE001 - surfaces as P3's failure
                services_error = redact(exc)

        # ---- P3: impersonation works --------------------------------------
        if discovery is None:
            probes.append(_probe_result(
                'P3', _('Impersonation works'),
                'fail' if services_error else 'skip',
                services_error or _('Skipped: no working token (see P2).'),
            ))
        else:
            try:
                about = discovery.about()
            except Exception as exc:  # noqa: BLE001 - rendered as a failed probe
                probes.append(_probe_result(
                    'P3', _('Impersonation works'), 'fail', _(
                        '%s\n\nIf the token mints (P2 passed) but this call fails, '
                        'the numeric client id or the scope string in the Admin '
                        'console is almost always the cause.'
                    ) % redact(exc),
                ))
            else:
                actual_email = (about.get('user') or {}).get('emailAddress') or ''
                expected_email = (connection.subject_email or '').strip()
                if connection.auth_mode == 'dwd' and actual_email.lower() != expected_email.lower():
                    probes.append(_probe_result(
                        'P3', _('Impersonation works'), 'fail', _(
                            'about.get returned user "%(actual)s" but this connection '
                            'is configured to impersonate "%(expected)s". Domain-wide '
                            'delegation is not taking effect for this subject. Check, '
                            'in this order: (a) the Admin console\'s Domain-Wide '
                            'Delegation entry uses the service account\'s numeric '
                            'OAuth2 Client ID, NOT its ...iam.gserviceaccount.com '
                            'EMAIL address — pasting the email is the single most '
                            'common setup error; (b) the authorized scope string is '
                            'exactly:\n      %(scopes)s'
                        ) % {
                            'actual': actual_email or _('<the bare service account>'),
                            'expected': expected_email,
                            'scopes': SCOPES_STRING,
                        },
                    ))
                else:
                    passed['P3'] = True
                    probes.append(_probe_result(
                        'P3', _('Impersonation works'), 'pass',
                        _('Authenticated as %s.') % (actual_email or _('the service account')),
                    ))

        # ---- P4: corpus is non-empty ----------------------------------
        files_sample: List[dict] = []
        if discovery is None:
            probes.append(_probe_result(
                'P4', _('Corpus is non-empty'), 'skip',
                _('Skipped: impersonation/token not available (see P2/P3).'),
            ))
        else:
            try:
                trashed_clause = '' if connection.include_trashed else 'trashed = false'
                files_sample = list(itertools.islice(
                    discovery.list_files(
                        q=trashed_clause, corpora='user', page_size=SAMPLE_SIZE,
                        label='P4 corpus is non-empty',
                    ),
                    SAMPLE_SIZE,
                ))
            except Exception as exc:  # noqa: BLE001 - rendered as a failed probe
                probes.append(_probe_result('P4', _('Corpus is non-empty'), 'fail', redact(exc)))
            else:
                sample_titles = [f.get('name') or f.get('id') or '' for f in files_sample]
                if files_sample:
                    passed['P4'] = True
                    probes.append(_probe_result(
                        'P4', _('Corpus is non-empty'), 'pass',
                        _('%(count)d file(s) seen in the first page, e.g. "%(name)s".') % {
                            'count': len(files_sample), 'name': sample_titles[0],
                        },
                    ))
                elif passed['P3']:
                    # CRITICAL (SPEC §2.4): an empty corpus after a passing
                    # impersonation probe is not a plausible empty Drive for a
                    # real Workspace subject — it is the signature of a broken
                    # domain-wide-delegation grant. Rendered as a red error,
                    # never as an innocuous empty state.
                    probes.append(_probe_result(
                        'P4', _('Corpus is non-empty'), 'fail', _(
                            'The corpus for %(subject)s is empty even though '
                            'impersonation succeeded (P3 passed). For a real '
                            'Workspace user this is NOT a plausible empty Drive — it '
                            'is the signature of a broken domain-wide-delegation '
                            'grant. The two most common causes: (a) the Admin '
                            'console\'s Domain-Wide Delegation entry was created with '
                            'the service account\'s ...iam.gserviceaccount.com EMAIL '
                            'address instead of its numeric OAuth2 Client ID — '
                            'pasting the email is the single most common setup '
                            'error; (b) the authorized scope string does not exactly '
                            'match what this module requests. It must be exactly:\n'
                            '      %(scopes)s\n'
                            'Also possible: the grant has not propagated yet (Google '
                            'documents up to 24 hours), or it was authorized for a '
                            'different service account than the one whose key is '
                            'configured here.'
                        ) % {'subject': connection.subject_email or '', 'scopes': SCOPES_STRING},
                    ))
                else:
                    probes.append(_probe_result(
                        'P4', _('Corpus is non-empty'), 'fail', _(
                            'The corpus is empty, but impersonation (P3) did not '
                            'succeed either, so this is expected until P3 is fixed — '
                            'fix that first.'
                        ),
                    ))

        # ---- P5: shared-with-me reachable ------------------------------
        if discovery is None:
            probes.append(_probe_result(
                'P5', _('Shared-with-me reachable'), 'skip',
                _('Skipped: impersonation/token not available (see P2/P3).'),
            ))
        else:
            try:
                shared = list(itertools.islice(discovery.list_shared_with_me(), SAMPLE_SIZE))
            except Exception as exc:  # noqa: BLE001 - rendered as a failed probe
                probes.append(_probe_result('P5', _('Shared-with-me reachable'), 'fail', redact(exc)))
            else:
                probes.append(_probe_result(
                    'P5', _('Shared-with-me reachable'), 'pass', _(
                        '%(count)d item(s) seen in "Shared with me" (0 is a '
                        'legitimate result — it just means nobody has shared '
                        'anything directly with %(subject)s).'
                    ) % {'count': len(shared), 'subject': connection.subject_email or _('the subject')},
                ))

        # ---- P6: shared drives enumerable ------------------------------
        if discovery is None:
            probes.append(_probe_result(
                'P6', _('Shared drives enumerable'), 'skip',
                _('Skipped: impersonation/token not available (see P2/P3).'),
            ))
        else:
            try:
                drives = discovery.list_shared_drives()
            except Exception as exc:  # noqa: BLE001 - rendered as a failed probe
                probes.append(_probe_result('P6', _('Shared drives enumerable'), 'fail', redact(exc)))
            else:
                probes.append(_probe_result(
                    'P6', _('Shared drives enumerable'), 'pass',
                    _('%d shared drive(s) enumerable.') % len(drives),
                ))

        # ---- P7: Sheets API reachable -----------------------------------
        if sheets is None:
            probes.append(_probe_result(
                'P7', _('Sheets API reachable'), 'skip',
                _('Skipped: no working token (see P2).'),
            ))
        else:
            spreadsheet_id = ''
            for meta in files_sample:
                if meta.get('mimeType') == MIME_SPREADSHEET:
                    spreadsheet_id = meta.get('id') or ''
                    break
            if not spreadsheet_id and discovery is not None:
                try:
                    extra = list(itertools.islice(
                        discovery.list_files(
                            q="mimeType = '%s' and trashed = false" % MIME_SPREADSHEET,
                            corpora='user', page_size=1, label='P7 find a spreadsheet',
                        ),
                        1,
                    ))
                except Exception:  # noqa: BLE001 - falls through to the "none found" skip
                    extra = []
                if extra:
                    spreadsheet_id = extra[0].get('id') or ''

            if not spreadsheet_id:
                probes.append(_probe_result(
                    'P7', _('Sheets API reachable'), 'skip', _(
                        'Skipped: no Google Sheets spreadsheet was found to test '
                        'against. This is not a failure by itself — it means the '
                        'visible corpus has no spreadsheets, or P3/P4 did not '
                        'succeed.'
                    ),
                ))
            else:
                try:
                    sheets.spreadsheets().get(
                        spreadsheetId=spreadsheet_id,
                        fields='spreadsheetId,sheets(properties(sheetId,title))',
                    ).execute()
                except Exception as exc:  # noqa: BLE001 - rendered as a failed probe
                    probes.append(_probe_result('P7', _('Sheets API reachable'), 'fail', redact(exc)))
                else:
                    probes.append(_probe_result(
                        'P7', _('Sheets API reachable'), 'pass',
                        _('The Sheets API responded for spreadsheet %s.') % spreadsheet_id,
                    ))

        # ---- Bookkeeping: state='ok' only when P1-P4 all pass -----------
        if passed['P1'] and passed['P2'] and passed['P3'] and passed['P4']:
            connection._mark_test_ok(_('Test Connection passed: P1-P4 all green.'))
        else:
            failed_codes = [
                p['code'] for p in probes
                if p['status'] == 'fail' and p['code'] in ('P1', 'P2', 'P3', 'P4')
            ]
            connection._mark_error(
                _('Test Connection failed: probe(s) %s did not pass.')
                % (', '.join(failed_codes) or '?'),
                tested=True,
            )

        self.write({
            'state': 'result',
            'result_html': self._render_result_html(probes),
            'sample_file_ids': '\n'.join(t for t in sample_titles if t) or False,
        })
        return self._reopen()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def _render_result_html(self, probes: List[Dict[str, str]]) -> str:
        """Render the probe list as one HTML table.

        Every dynamic value — including probe detail text, which can carry a
        Drive file title or a redacted-but-otherwise-unfiltered error message —
        is passed through ``html_escape`` here, at the single point where it
        becomes markup. Nothing upstream is trusted to have escaped anything.
        """
        esc = html_escape
        status_label = {'pass': _('PASS'), 'fail': _('FAIL'), 'skip': _('SKIPPED')}
        status_class = {'pass': 'gdts-pass', 'fail': 'gdts-fail', 'skip': 'gdts-skip'}
        parts = [
            '<style>'
            '.gdts-table{border-collapse:collapse;width:100%;}'
            '.gdts-table th,.gdts-table td{border:1px solid #ccc;padding:.4rem .6rem;'
            'text-align:left;vertical-align:top;font-size:.85rem;}'
            '.gdts-table th{background:#f4f4f4;}'
            '.gdts-pass{color:#1e7e34;font-weight:bold;}'
            '.gdts-fail{color:#a00;font-weight:bold;}'
            '.gdts-skip{color:#888;font-style:italic;}'
            '.gdts-detail{white-space:pre-wrap;}'
            '</style>',
            '<table class="gdts-table">'
            '<tr><th>Probe</th><th>Check</th><th>Result</th><th>Detail</th></tr>',
        ]
        for probe in probes:
            css_class = status_class.get(probe['status'], '')
            parts.append(
                '<tr><td>%s</td><td>%s</td><td class="%s">%s</td>'
                '<td class="gdts-detail">%s</td></tr>' % (
                    esc(probe['code']),
                    esc(probe['label']),
                    css_class,
                    esc(status_label.get(probe['status'], probe['status'])),
                    esc(probe['detail']),
                )
            )
        parts.append('</table>')
        return ''.join(parts)

    def _reopen(self):
        """Re-open this same wizard record so the result screen replaces setup.

        A button of type ``object`` that returns nothing leaves the dialog's
        fate up to the client's default handling; returning an explicit
        ``ir.actions.act_window`` targeting this record's own id removes any
        ambiguity about whether the just-written ``result_html`` is shown.
        """
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Test Google Drive Connection'),
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }
