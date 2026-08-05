# -*- coding: utf-8 -*-
"""Drift digest email.

WHY a fingerprint rather than "mail me every cycle": a service that runs every
hour and mails every hour trains you to filter it, and the one message that
mattered gets filtered with the rest. The digest goes out when the *set of
findings changes* -- a new drift type, a new row, a value that moved, or drift
clearing entirely. An unchanged finding set is silence.

WHY the fingerprint ignores ids and timestamps: every verify run writes fresh
``drift`` rows with new ids and a new ``created_at``. Hashing those would make
every cycle look like a change, which is the same as mailing every cycle.

Nothing here is an instruction to anyone. The digest reports; the service still
changes nothing on either side.
"""
from __future__ import annotations

import hashlib
import smtplib
import socket
from email.message import EmailMessage
from typing import Iterable, Optional, Sequence

#: Fields that describe *what* the finding is, as opposed to when it was found.
IDENTITY_FIELDS = (
    'drift_type', 'severity', 'dataset_id', 'row_ref', 'column_key',
    'sheet_value', 'odoo_value', 'detail',
)

#: Listing every row of a 4,000-finding run makes an unreadable mail and can
#: trip provider size limits. The digest says how many it left out.
MAX_LISTED = 50


def _key(finding: dict) -> tuple:
    return tuple(str(finding.get(f, '')) for f in IDENTITY_FIELDS)


def fingerprint(findings: Iterable[dict]) -> str:
    """A stable digest of a finding set. Order-independent."""
    h = hashlib.sha256()
    for key in sorted(_key(f) for f in findings):
        h.update('\x1f'.join(key).encode('utf-8'))
        h.update(b'\x1e')
    return h.hexdigest()


def _where(finding: dict) -> str:
    return (f"{finding.get('file_name') or '?'} / "
            f"{finding.get('tab_title') or '?'}")


def render_digest(findings: Sequence[dict], summary: dict, *,
                  db_path: str = '', finished_at: str = '',
                  host: str = '', previously: Optional[int] = None) -> tuple:
    """Return ``(subject, body)`` for a plain-text digest.

    Plain text on purpose: it renders identically in every client, survives
    forwarding into a ticket, and has no way to leak a value by mangling it
    into markup.
    """
    total = len(findings)
    by_type = sorted((summary or {}).items(), key=lambda kv: -kv[1])

    if total == 0:
        subject = 'DriftWatch: clear -- no drift'
    else:
        top = ', '.join(f'{n} {t}' for t, n in by_type[:3])
        subject = f'DriftWatch: {total} finding{"" if total == 1 else "s"}'
        if top:
            subject += f' ({top})'

    lines = [subject.replace('DriftWatch: ', ''), '']
    if previously is not None and previously != total:
        lines.append(f'Changed since the last digest: {previously} -> {total}.')
    else:
        lines.append('The set of findings changed since the last digest.')
    lines.append('')
    if finished_at:
        lines.append(f'Cycle finished : {finished_at} UTC')
    if host:
        lines.append(f'Host           : {host}')
    if db_path:
        lines.append(f'Store          : {db_path}')
    lines.append('')

    if by_type:
        lines.append('BY TYPE')
        for drift_type, count in by_type:
            lines.append(f'  {drift_type:<22} {count}')
        lines.append('')

    if total:
        shown = list(findings)[:MAX_LISTED]
        header = 'FINDINGS'
        if total > len(shown):
            header += f'  (first {len(shown)} of {total})'
        lines.append(header)
        for f in shown:
            lines.append(f"  [{f.get('severity') or 'warning'}] "
                         f"{f.get('drift_type')}  {_where(f)}")
            if f.get('row_ref') or f.get('column_key'):
                lines.append(f"      at {f.get('row_ref') or '?'} "
                             f"column {f.get('column_key') or '?'}")
            if f.get('sheet_value') is not None or f.get('odoo_value') is not None:
                lines.append(f"      sheet={f.get('sheet_value')!r}  "
                             f"odoo={f.get('odoo_value')!r}")
            if f.get('detail'):
                lines.append(f"      {f['detail']}")
        if total > len(shown):
            lines.append(f'  ... and {total - len(shown)} more '
                         f'(run `python -m driftwatch drift` for the rest)')
        lines.append('')
    else:
        lines.append('No findings in the latest verification run.')
        lines.append('')

    lines += [
        '-' * 68,
        'DriftWatch is read-only. Nothing in Drive or Odoo was changed to',
        'produce this report, and no finding here has been applied anywhere.',
    ]
    return subject, '\n'.join(lines)


def send_digest(cfg, subject: str, body: str, *, timeout: int = 30) -> None:
    """Send the digest over SMTP. Raises on failure; the caller decides.

    Deliberately not swallowing errors here: a mail failure that only appears
    as a missing message is indistinguishable from "no drift", which is the
    exact wrong thing for an alerting path to be ambiguous about.
    """
    if not cfg.alerts_enabled:
        raise RuntimeError('SMTP is not configured (need DRIFTWATCH_SMTP_HOST '
                           'and DRIFTWATCH_ALERT_TO).')

    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = cfg.alert_from or cfg.smtp_user
    msg['To'] = ', '.join(cfg.alert_to)
    msg['Auto-Submitted'] = 'auto-generated'   # keeps vacation autoresponders quiet
    msg.set_content(body)

    # Port 465 is implicit TLS from the first byte; STARTTLS on it hangs.
    if cfg.smtp_port == 465 or not cfg.smtp_starttls:
        client = smtplib.SMTP_SSL if cfg.smtp_port == 465 else smtplib.SMTP
        with client(cfg.smtp_host, cfg.smtp_port, timeout=timeout) as smtp:
            if cfg.smtp_user:
                smtp.login(cfg.smtp_user, cfg.smtp_password)
            smtp.send_message(msg)
        return

    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=timeout) as smtp:
        smtp.starttls()
        if cfg.smtp_user:
            smtp.login(cfg.smtp_user, cfg.smtp_password)
        smtp.send_message(msg)


def hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return ''
