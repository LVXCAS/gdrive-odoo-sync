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

    @property
    def scopes(self) -> tuple:
        return SCOPES

    def service_account_info(self) -> dict:
        """Load and sanity-check the key. Raises before any network call."""
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

    return Config(
        sa_key_path=Path(get('DRIFTWATCH_SA_KEY',
                             str(Path.home() / 'Downloads' /
                                 'driftwatch-01-0616db5edf38.json'))),
        subject_email=get('DRIFTWATCH_SUBJECT', get('ODOO_LOGIN')),
        odoo_url=odoo_url,
        odoo_db=get('ODOO_DB', default_db),
        odoo_login=get('ODOO_LOGIN'),
        odoo_api_key=get('ODOO_API_KEY'),
        db_path=Path(get('DRIFTWATCH_DB', str(REPO_ROOT / 'driftwatch.sqlite3'))),
        max_files=int(get('DRIFTWATCH_MAX_FILES', '0') or 0),
        sheets_reads_per_min=int(get('DRIFTWATCH_SHEETS_RPM', '60') or 60),
        drive_reads_per_min=int(get('DRIFTWATCH_DRIVE_RPM', '300') or 300),
    )
