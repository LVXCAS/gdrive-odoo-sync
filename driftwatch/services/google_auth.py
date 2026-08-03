"""Service-account credential loading and domain-wide-delegation (lane B).

This module is the single place in the system that touches the service-account
private key. Everything else receives an already-built credentials object.

The two facts that govern every line here (SPEC §2):

1. **A service account sees nothing by default.** It is its own principal with its
   own empty, 0 GB-quota corpus. ``files.list`` returns ``{'files': []}`` with
   HTTP 200 and no error. An empty read looks exactly like "everything was
   deleted", which is why the delete guards in SPEC §9.6 exist and why this
   module's job — getting impersonation right — is load-bearing for data safety.
2. **``creds.with_subject()`` returns a new object; it does not mutate.** Writing
   ``base_creds.with_subject(email)`` without assigning the result leaves you
   authenticating as the bare service account and silently seeing nothing. Lane F
   asserts ``creds is not base_creds`` for exactly this reason.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Iterable, Mapping, Optional, Sequence

from .errors import (
    CODE_MISSING_DEPENDENCY,
    GDriveAuthError,
    GDriveScopeError,
    redact,
)

_logger = logging.getLogger(__name__)

__all__ = [
    'SCOPES',
    'SCOPES_STRING',
    'DEFAULT_KEY_ENV_VAR',
    'DEFAULT_KEY_PARAM_KEY',
    'SETUP_INSTRUCTIONS',
    'load_service_account_info',
    'parse_service_account_info',
    'build_credentials',
    'refresh_credentials',
    'credentials_for_connection',
    'key_summary',
]

#: The frozen, read-only scope pair. These MUST be a **subset** of what is granted
#: in the Admin console's domain-wide delegation entry, and the module never asks
#: for anything else. Read-only scope is what structurally guarantees v1 cannot
#: damage Drive — and, as an accepted consequence, why ``_sync_id`` write-back is
#: unavailable (SPEC §2.2).
SCOPES: Sequence[str] = (
    'https://www.googleapis.com/auth/drive.readonly',
    'https://www.googleapis.com/auth/spreadsheets.readonly',
)

#: Exactly the string to paste into the Admin console DWD form, comma-delimited
#: with no spaces. Exposed read-only on ``gdrive.connection.scopes``.
SCOPES_STRING = ','.join(SCOPES)

DEFAULT_KEY_ENV_VAR = 'GDRIVE_ODOO_SYNC_SA_KEY'
DEFAULT_KEY_PARAM_KEY = 'gdrive_odoo_sync.sa_key_json'

#: Fields that must be present for the key to be usable. Probe P1 of the Test
#: Connection wizard asserts exactly this set (SPEC §2.4).
REQUIRED_KEY_FIELDS = ('client_email', 'client_id', 'private_key')

SETUP_INSTRUCTIONS = """\
No Google service-account key is configured for this connection.

Provide it in one of two places, checked in this order:

  1. Environment variable %(env_var)s containing the raw JSON key.
     PREFERRED. On Odoo.sh set it as a project environment variable.
     Database dumps are downloadable and system parameters appear in them
     in cleartext; environment variables do not.

  2. System parameter %(param_key)s
     (Settings -> Technical -> System Parameters, or the Google Drive Sync
     settings block, which writes it there for you).

Then complete the one-time domain-wide delegation grant:

  * Google Cloud console -> the project -> enable the Google Drive API and the
    Google Sheets API.
  * Copy the service account's numeric OAuth2 Client ID (~21 digits). This is
    NOT the ...iam.gserviceaccount.com email address; pasting the email is the
    single most common setup error.
  * admin.google.com -> Security -> Access and data control -> API controls ->
    Domain-wide delegation -> Manage Domain Wide Delegation -> Add new.
  * Paste the numeric Client ID and this exact scope string:
        %(scopes)s
  * Authorize. Propagation is usually minutes but Google documents up to 24 hours.
""" % {
    'env_var': DEFAULT_KEY_ENV_VAR,
    'param_key': DEFAULT_KEY_PARAM_KEY,
    'scopes': SCOPES_STRING,
}

SCOPE_FAILURE_HELP = """\
Google refused the token exchange with 'unauthorized_client'.

This error does not mention scopes, but scopes (or the client id) are almost
always the cause. Check, in this order:

  1. The Admin console DWD entry uses the service account's NUMERIC Client ID
     (~21 digits), not its ...iam.gserviceaccount.com email address.
  2. The authorized scope string is exactly:
        %(scopes)s
     A superset is fine; a subset or a typo is not. The scopes requested here
     must be a subset of what is authorized.
  3. The impersonated subject (%%(subject)s) is a real, active user in the
     Workspace domain. Domain-wide delegation CANNOT impersonate an @gmail.com
     consumer account.
  4. The grant has propagated. Google documents up to 24 hours.
""" % {'scopes': SCOPES_STRING}


# --------------------------------------------------------------------------- #
# Optional imports
#
# Imported at module scope so a real deployment fails fast and loudly, but guarded
# so this package stays importable in a bare checkout (lane F's canonicalization
# tests import nothing from google-*), and so the failure message says what to
# install instead of raising a bare ImportError from deep inside a cron.
# --------------------------------------------------------------------------- #
try:  # pragma: no cover - environment dependent
    from google.oauth2 import service_account as _service_account
except Exception:  # pragma: no cover
    _service_account = None

try:  # pragma: no cover - environment dependent
    from google.auth import exceptions as _google_auth_exceptions
except Exception:  # pragma: no cover
    _google_auth_exceptions = None


def _require_google_auth() -> Any:
    """Return the ``google.oauth2.service_account`` module or raise usefully."""
    if _service_account is None:
        raise GDriveAuthError(
            'The google-auth library is not installed. Add "google-auth" and '
            '"google-api-python-client" to requirements.txt and rebuild.',
            code=CODE_MISSING_DEPENDENCY,
        )
    return _service_account


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` off an Odoo recordset, a dict, or a plain object.

    WHY: lane D passes a ``gdrive.connection`` recordset; lane F's unit tests pass
    a ``SimpleNamespace`` or a dict. Accepting all three keeps this module
    testable without an Odoo database, which is the whole point of lane B being
    pure Python.
    """
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        value = obj.get(name, default)
    else:
        value = getattr(obj, name, default)
    if isinstance(default, bool):
        # A genuine False must survive for Boolean fields.
        return bool(value)
    if value is None or value is False or value == '':
        # Odoo returns False (not None, not '') for an unset Char field.
        return default
    return value


# --------------------------------------------------------------------------- #
# Key resolution
# --------------------------------------------------------------------------- #


def parse_service_account_info(raw: Any, source: str = 'configuration') -> dict:
    """Parse and structurally validate a service-account JSON key. Probe P1.

    :param raw: the JSON text (or an already-parsed dict).
    :param source: where it came from, for the error message only.
    :raises GDriveAuthError: with a redacted, actionable message.

    Validation is limited to *structure*. Whether the key actually works is P2
    (mint a token) and whether impersonation works is P3 (``about.get``); those
    are network probes and belong in the wizard, not here.
    """
    if isinstance(raw, Mapping):
        info = dict(raw)
    else:
        text = (raw or '').strip() if isinstance(raw, str) else ''
        if not text:
            raise GDriveAuthError(SETUP_INSTRUCTIONS, code='sa_key_missing')
        try:
            info = json.loads(text)
        except (ValueError, TypeError) as exc:
            raise GDriveAuthError(
                'The service-account key from %s is not valid JSON. Paste the whole '
                'downloaded key file, including the outer braces. (%s)'
                % (source, redact(exc)),
                code='sa_key_malformed',
            ) from exc
    if not isinstance(info, dict):
        raise GDriveAuthError(
            'The service-account key from %s is not a JSON object.' % source,
            code='sa_key_malformed',
        )
    if info.get('type') and info.get('type') != 'service_account':
        raise GDriveAuthError(
            'The key from %s has type=%r. This module needs a *service account* key, '
            'not an OAuth client-secret file. Create one under IAM & Admin -> '
            'Service Accounts -> Keys -> Add key -> JSON.' % (source, info.get('type')),
            code='sa_key_wrong_type',
        )
    missing = [f for f in REQUIRED_KEY_FIELDS if not info.get(f)]
    if missing:
        raise GDriveAuthError(
            'The service-account key from %s is missing required field(s): %s. '
            'It is probably truncated or is the wrong file.' % (source, ', '.join(missing)),
            code='sa_key_incomplete',
            details={'missing': missing},
        )
    return info


def load_service_account_info(env: Any, connection: Any) -> dict:
    """Resolve the service-account JSON key for ``connection``. SPEC §2.5.

    Resolution order, implemented here once and nowhere else:

    1. ``os.environ[connection.sa_key_env_var]`` (default
       ``GDRIVE_ODOO_SYNC_SA_KEY``). **Preferred**, because Odoo.sh database
       dumps are downloadable and ``ir_config_parameter`` values appear in them
       in cleartext.
    2. ``env['ir.config_parameter'].sudo().get_param(connection.sa_key_param_key)``
       (default ``gdrive_odoo_sync.sa_key_json``). ``.sudo()`` is **mandatory**:
       the model is restricted to ``base.group_system``, so a cron running as a
       manager user, or any non-system user, gets ``AccessError`` without it.
    3. Otherwise raise with the full setup instructions.

    :param env: an Odoo ``Environment``. May be ``None`` in unit tests, in which
        case only the environment variable is consulted.
    :param connection: a ``gdrive.connection`` recordset (or any object/dict
        exposing ``sa_key_env_var`` / ``sa_key_param_key``).
    :raises UserError: (Odoo's, when Odoo is importable) when no key is
        configured, so the message reaches the user in a dialog rather than as a
        traceback. Falls back to :class:`~.errors.GDriveAuthError` outside Odoo.
    """
    env_var = _attr(connection, 'sa_key_env_var', DEFAULT_KEY_ENV_VAR) or DEFAULT_KEY_ENV_VAR
    param_key = _attr(connection, 'sa_key_param_key', DEFAULT_KEY_PARAM_KEY) or DEFAULT_KEY_PARAM_KEY

    raw = os.environ.get(env_var)
    if raw and raw.strip():
        _logger.debug('Service-account key resolved from environment variable %s.', env_var)
        return parse_service_account_info(raw, source='environment variable %s' % env_var)

    if env is not None:
        try:
            raw = env['ir.config_parameter'].sudo().get_param(param_key)
        except Exception as exc:  # noqa: BLE001 - re-raised as an auth error below
            raise GDriveAuthError(
                'Could not read system parameter %s: %s' % (param_key, redact(exc)),
                code='sa_key_unreadable',
            ) from exc
        if raw and str(raw).strip():
            _logger.debug('Service-account key resolved from system parameter %s.', param_key)
            return parse_service_account_info(raw, source='system parameter %s' % param_key)

    raise _user_error(SETUP_INSTRUCTIONS)


def _user_error(message: str) -> Exception:
    """Build an Odoo ``UserError`` when running inside Odoo, else a lane B error.

    WHY the soft import: SPEC §2.5 wants a ``UserError`` so the setup instructions
    render as a dialog. But lane B must stay importable and unit-testable with no
    Odoo on the path (FILE_MANIFEST lane isolation rules), so the import cannot be
    unconditional.
    """
    try:  # pragma: no cover - depends on whether Odoo is importable
        from odoo.exceptions import UserError

        return UserError(message)
    except Exception:  # pragma: no cover
        return GDriveAuthError(message, code='sa_key_missing')


def key_summary(info: Mapping[str, Any]) -> dict:
    """Return the **non-secret** identifying fields of a key, for display.

    ``client_email`` is what a user must share a folder with in ``sa_direct``
    mode; ``client_id`` is the numeric value to paste into the Admin console.
    ``private_key`` and ``private_key_id`` are deliberately never returned — this
    dict ends up in a form view and in wizard HTML.
    """
    return {
        'client_email': info.get('client_email') or '',
        'client_id': info.get('client_id') or '',
        'project_id': info.get('project_id') or '',
        'token_uri': info.get('token_uri') or 'https://oauth2.googleapis.com/token',
    }


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #


def build_credentials(
    info: Mapping[str, Any],
    scopes: Optional[Iterable[str]] = None,
    subject: Optional[str] = None,
) -> Any:
    """Build service-account credentials, optionally impersonating ``subject``.

    :param info: the parsed key dict.
    :param scopes: defaults to :data:`SCOPES`. Must be a subset of what the Admin
        console authorized, or the token exchange fails with
        ``401 unauthorized_client``.
    :param subject: the Workspace user to impersonate (``auth_mode = 'dwd'``).
        Pass ``None`` or ``''`` for ``sa_direct`` degraded mode.
    :returns: a ``google.oauth2.service_account.Credentials``.

    The critical line is::

        creds = base_creds.with_subject(subject)

    ``with_subject`` returns a **new** credentials object and does **not** mutate
    the receiver. Calling it without capturing the result is a silent no-op that
    leaves you authenticating as the bare service account — which reads an empty
    Drive, with HTTP 200 and no error, and looks identical to "the user deleted
    everything". This function exists largely so that mistake can only be made in
    one place, and lane F asserts the returned object is not the base object.
    """
    sa = _require_google_auth()
    scope_list = list(scopes or SCOPES)
    extra = [s for s in scope_list if s not in SCOPES]
    if extra:
        # Not fatal — an operator may have granted more — but it is a strong smell,
        # because the module has no code path that needs write scope and SPEC §2.2
        # relies on read-only scope as a structural safety guarantee.
        _logger.warning(
            'Requesting scope(s) outside the frozen read-only pair: %s. '
            'v1 never writes to Drive; a write scope grants damage potential for '
            'no functional benefit.', ', '.join(extra),
        )
    try:
        base_creds = sa.Credentials.from_service_account_info(dict(info), scopes=scope_list)
    except Exception as exc:  # noqa: BLE001 - re-raised as an auth error
        raise GDriveAuthError(
            'Could not build credentials from the service-account key: %s' % redact(exc),
            code='sa_key_unusable',
        ) from exc

    if not subject:
        _logger.info(
            'Building NON-delegated service-account credentials (sa_direct mode). '
            'The service account has its own empty corpus: only folders explicitly '
            'shared with %s will be visible.',
            info.get('client_email') or 'the service account',
        )
        return base_creds

    creds = base_creds.with_subject(subject)
    if creds is base_creds:  # pragma: no cover - would mean a google-auth regression
        raise GDriveAuthError(
            'with_subject() returned the same credentials object. The installed '
            'google-auth version does not support domain-wide delegation as '
            'expected; upgrade google-auth.',
            code='dwd_unsupported',
        )
    _logger.debug('Built delegated credentials impersonating %s.', subject)
    return creds


def refresh_credentials(creds: Any, subject: str = '', request: Any = None) -> Any:
    """Mint an access token. Probe P2 of the Test Connection wizard.

    :param creds: credentials from :func:`build_credentials`.
    :param subject: only used to render the remediation text.
    :param request: an injected ``google.auth.transport.Request``; built from the
        ``requests`` transport when omitted (Odoo already pins ``requests``).
    :raises GDriveScopeError: on ``unauthorized_client``, carrying the full
        remediation checklist — Google's own message does not mention scopes, and
        an operator staring at "unauthorized_client" has no way to guess that the
        cause is an email pasted where a numeric client id belongs.
    :raises GDriveAuthError: on any other refresh failure.
    """
    if request is None:
        request = _default_transport_request()
    try:
        creds.refresh(request)
    except Exception as exc:  # noqa: BLE001 - classified and re-raised below
        text = redact(exc)
        is_scope_failure = 'unauthorized_client' in text or 'access_denied' in text
        if _google_auth_exceptions is not None and isinstance(
            exc, getattr(_google_auth_exceptions, 'RefreshError', ())
        ):
            is_scope_failure = is_scope_failure or 'unauthorized' in text.lower()
        if is_scope_failure:
            raise GDriveScopeError(
                (SCOPE_FAILURE_HELP % {'subject': subject or '<not set>'})
                + '\n\nGoogle said: ' + text,
                code='unauthorized_client',
                status=401,
                reason='unauthorized_client',
            ) from exc
        raise GDriveAuthError(
            'Could not obtain an access token: %s' % text, code='token_refresh_failed'
        ) from exc
    return creds


def _default_transport_request() -> Any:
    """Return a ``google.auth.transport`` Request, preferring the requests one.

    ``requests`` is already pinned by Odoo, so the requests transport is the safe
    default; the httplib2 transport is used only if ``google-auth-httplib2`` is
    installed and ``requests`` somehow is not.
    """
    try:  # pragma: no cover - environment dependent
        from google.auth.transport.requests import Request

        return Request()
    except Exception:  # pragma: no cover
        pass
    try:  # pragma: no cover - environment dependent
        import httplib2
        from google_auth_httplib2 import Request as Httplib2Request

        return Httplib2Request(httplib2.Http())
    except Exception as exc:  # pragma: no cover
        raise GDriveAuthError(
            'No usable google-auth transport is installed. Install '
            '"google-auth" together with "requests" (or "google-auth-httplib2").',
            code=CODE_MISSING_DEPENDENCY,
        ) from exc


def credentials_for_connection(env: Any, connection: Any, info: Optional[Mapping] = None) -> Any:
    """One-call convenience: load the key and build credentials for ``connection``.

    Honours ``auth_mode``: ``'dwd'`` impersonates ``subject_email`` (and requires
    it), ``'sa_direct'`` uses the bare service account, which can only see what a
    human has explicitly shared with ``client_email`` and can never see items that
    third parties shared with the *subject*.
    """
    if info is None:
        info = load_service_account_info(env, connection)
    auth_mode = _attr(connection, 'auth_mode', 'dwd') or 'dwd'
    subject = _attr(connection, 'subject_email', '') or ''
    if auth_mode == 'dwd' and not subject:
        raise _user_error(
            'This connection uses domain-wide delegation but has no subject e-mail. '
            'Set "Subject Email" to the Workspace user whose Drive should be '
            'mirrored (for example lucaso@avatarnaturalfoods.com).'
        )
    return build_credentials(info, SCOPES, subject if auth_mode == 'dwd' else None)
