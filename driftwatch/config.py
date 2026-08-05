# -*- coding: utf-8 -*-
"""Configuration for the DriftWatch service.

Everything is read from the environment, falling back to a ``.env`` file next
to the repository root. Nothing here has a hard-coded secret and nothing here
is committed: ``.env`` is blocked by ``.gitignore``.

WHY a service account key path rather than the JSON inline: the key is a bearer
credential for the whole impersonated Drive corpus. Keeping it as a file the
process reads once, rather than an environment variable that shows up in
``ps``, process dumps and crash reports, is the smaller blast radius.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Read-only, and deliberately so. The service structurally cannot modify Drive.
SCOPES = (
    'https://www.googleapis.com/auth/drive.readonly',
    'https://www.googleapis.com/auth/spreadsheets.readonly',
)


def _load_dotenv(path: Path) -> dict:
    """Parse a ``KEY=value`` file. Absent file is not an error."""
    out: dict = {}
    if not path.exists():
        return out
    # utf-8-sig: PowerShell's Set-Content writes a BOM, and a BOM on the first
    # key silently produces a key nobody can look up.
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _duration(text: str, default: int) -> int:
    """Seconds from ``900``, ``15m``, ``2h`` or ``1d``.

    Bare numbers stay seconds so an existing integer setting keeps its meaning.
    """
    text = (text or '').strip().lower()
    if not text:
        return default
    units = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
    scale = units.get(text[-1], 1)
    if scale != 1:
        text = text[:-1]
    try:
        return max(1, int(float(text) * scale))
    except ValueError:
        return default


def _flag(text: str, default: bool) -> bool:
    text = (text or '').strip().lower()
    if not text:
        return default
    return text in ('1', 'true', 'yes', 'on')


@dataclass(frozen=True)
class Config:
    """Resolved runtime configuration."""

    sa_key_path: Path
    subject_email: str
    odoo_url: str
    odoo_db: str
    odoo_login: str
    odoo_api_key: str
    db_path: Path

    # Guardrails. These exist because an incomplete read and a real deletion
    # look identical, so the service refuses to act on suspicious reads.
    max_files: int = 0                 # 0 = unlimited
    sheets_reads_per_min: int = 60
    drive_reads_per_min: int = 300

    # Unattended operation. Only the daemon reads these; the one-shot commands
    # behave exactly as they did before.
    interval_seconds: int = 3600
    log_dir: Path = REPO_ROOT / 'logs'
    log_retain: int = 14               # rotated files kept, one per day
    #: Rewritten after every cycle. Embeds staged content, so it is gitignored
    #: and belongs beside the logs, not anywhere that syncs.
    dashboard_path: Path = REPO_ROOT / 'logs' / 'dashboard.html'

    # Drift digest email. Absent SMTP settings disable alerting; they do not
    # stop the service. A crawler that refuses to run because nobody set up
    # mail is a crawler that silently stops watching.
    smtp_host: str = ''
    smtp_port: int = 587
    smtp_user: str = ''
    smtp_password: str = ''
    smtp_starttls: bool = True
    alert_from: str = ''
    alert_to: tuple = ()

    @property
    def scopes(self) -> tuple:
        return SCOPES

    @property
    def alerts_enabled(self) -> bool:
        return bool(self.smtp_host and self.alert_to)

    @property
    def sa_key_configured(self) -> bool:
        """``Path('')`` normalises to ``Path('.')``, which exists -- so an
        unset key would otherwise sail past an ``.exists()`` check and fail
        later as a confusing JSON error on a directory."""
        return str(self.sa_key_path) not in ('', '.')

    def service_account_info(self) -> dict:
        """Load and sanity-check the key. Raises before any network call."""
        if not self.sa_key_configured:
            raise FileNotFoundError(
                'DRIFTWATCH_SA_KEY is not set. Point it at the '
                'service-account JSON key (see .env.example).')
        if not self.sa_key_path.exists():
            raise FileNotFoundError(
                f'Service-account key not found at {self.sa_key_path}. '
                f'Set DRIFTWATCH_SA_KEY to its path.')
        info = json.loads(self.sa_key_path.read_text(encoding='utf-8'))
        missing = [k for k in ('client_email', 'client_id', 'private_key')
                   if not info.get(k)]
        if missing:
            raise ValueError(f'Service-account key is missing {missing}.')
        return info

    def redacted(self) -> dict:
        """Safe to log. Never includes the key or the Odoo API key."""
        return {
            'sa_key_path': str(self.sa_key_path),
            'subject_email': self.subject_email,
            'odoo_url': self.odoo_url,
            'odoo_db': self.odoo_db,
            'odoo_login': self.odoo_login,
            'odoo_api_key': '***' if self.odoo_api_key else '(unset)',
            'db_path': str(self.db_path),
            'interval_seconds': self.interval_seconds,
            'log_dir': str(self.log_dir),
            'dashboard_path': str(self.dashboard_path),
            'smtp_host': self.smtp_host or '(unset)',
            'smtp_port': self.smtp_port,
            'smtp_user': self.smtp_user or '(unset)',
            'smtp_password': '***' if self.smtp_password else '(unset)',
            'alert_from': self.alert_from or '(unset)',
            'alert_to': list(self.alert_to),
            'alerts_enabled': self.alerts_enabled,
        }


def load_config(dotenv: Optional[Path] = None) -> Config:
    """Build a :class:`Config` from the environment plus ``.env``.

    Real environment variables win over ``.env`` so a deployment can override
    a developer's local file without editing it.
    """
    env = dict(_load_dotenv(dotenv or (REPO_ROOT / '.env')))
    env.update({k: v for k, v in os.environ.items() if k.startswith(
        ('DRIFTWATCH_', 'ODOO_'))})

    def get(key: str, default: str = '') -> str:
        return env.get(key, default)

    odoo_url = get('ODOO_URL').rstrip('/')
    # The Odoo Online database name is the subdomain unless told otherwise.
    default_db = ''
    if odoo_url:
        host = odoo_url.split('//')[-1]
        default_db = host.split('.')[0]

    log_dir = Path(get('DRIFTWATCH_LOG_DIR', str(REPO_ROOT / 'logs')))

    return Config(
        # No default on purpose. This used to fall back to a named key in
        # ~/Downloads -- a folder people clear out, and a path no deployment
        # should depend on. An unset key must fail loudly at startup, not
        # resolve to somewhere the credential happens to have been once.
        sa_key_path=Path(get('DRIFTWATCH_SA_KEY')),
        subject_email=get('DRIFTWATCH_SUBJECT', get('ODOO_LOGIN')),
        odoo_url=odoo_url,
        odoo_db=get('ODOO_DB', default_db),
        odoo_login=get('ODOO_LOGIN'),
        odoo_api_key=get('ODOO_API_KEY'),
        db_path=Path(get('DRIFTWATCH_DB', str(REPO_ROOT / 'driftwatch.sqlite3'))),
        max_files=int(get('DRIFTWATCH_MAX_FILES', '0') or 0),
        sheets_reads_per_min=int(get('DRIFTWATCH_SHEETS_RPM', '60') or 60),
        drive_reads_per_min=int(get('DRIFTWATCH_DRIVE_RPM', '300') or 300),
        interval_seconds=_duration(get('DRIFTWATCH_INTERVAL'), 3600),
        log_dir=log_dir,
        log_retain=int(get('DRIFTWATCH_LOG_RETAIN', '14') or 14),
        dashboard_path=Path(get('DRIFTWATCH_DASHBOARD',
                                str(log_dir / 'dashboard.html'))),
        smtp_host=get('DRIFTWATCH_SMTP_HOST'),
        smtp_port=int(get('DRIFTWATCH_SMTP_PORT', '587') or 587),
        smtp_user=get('DRIFTWATCH_SMTP_USER'),
        smtp_password=get('DRIFTWATCH_SMTP_PASSWORD'),
        smtp_starttls=_flag(get('DRIFTWATCH_SMTP_STARTTLS'), True),
        # Most providers reject a From: that is not the authenticated mailbox,
        # so defaulting to the SMTP user is the setting that actually delivers.
        alert_from=get('DRIFTWATCH_ALERT_FROM', get('DRIFTWATCH_SMTP_USER')),
        alert_to=tuple(a.strip() for a in get('DRIFTWATCH_ALERT_TO').split(',')
                       if a.strip()),
    )
