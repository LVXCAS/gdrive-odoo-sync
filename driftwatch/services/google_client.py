"""Thread-local Google API service factory (lane B).

WHY thread-local and not a module-level singleton: **``googleapiclient`` service
objects are not thread-safe.** They wrap a single ``httplib2.Http`` instance, which
holds one connection per host and is explicitly documented as not safe for
concurrent use. Sharing one ``drive`` object across a ``ThreadPoolExecutor``
produces interleaved responses — you get file B's JSON in answer to file A's
request — which corrupts the mirror *silently*, because both responses are
structurally valid (SPEC §4.1).

Contrast with :mod:`.rate_limiter`, whose buckets are deliberately process-wide:
the HTTP client is per-thread because it is not safe to share, and the quota
budget is shared because Google counts every thread against the same user.

Cache key is ``(connection_id, subject_email, api, private_key_id, thread_id)``.
Including the key id means rotating the service-account key invalidates cached
services automatically instead of leaving a worker authenticating with a revoked
credential until the next restart.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .errors import CODE_MISSING_DEPENDENCY, GDriveAuthError, redact
from .google_auth import (
    DEFAULT_KEY_ENV_VAR,
    DEFAULT_KEY_PARAM_KEY,
    SCOPES,
    _attr as _field,
    build_credentials,
    load_service_account_info,
)
from .rate_limiter import (
    API_DRIVE,
    API_SHEETS,
    DEFAULT_DRIVE_UNITS_PER_MIN,
    DEFAULT_SHEETS_READS_PER_MIN,
    REGISTRY,
    TokenBucket,
)

_logger = logging.getLogger(__name__)

__all__ = [
    'ConnectionContext',
    'build_services',
    'build_drive',
    'build_sheets',
    'reset_services',
    'DRIVE_API',
    'DRIVE_VERSION',
    'SHEETS_API',
    'SHEETS_VERSION',
]

DRIVE_API = 'drive'
DRIVE_VERSION = 'v3'
SHEETS_API = 'sheets'
SHEETS_VERSION = 'v4'

_local = threading.local()


@dataclass
class ConnectionContext:
    """A plain-Python snapshot of a ``gdrive.connection``.

    WHY a snapshot instead of passing the recordset around: FILE_MANIFEST forbids
    lane B from importing lane D, and more practically, a recordset carries a
    cursor and an environment into code that may run on a worker thread. Passing a
    frozen value object keeps every service in this package unit-testable with a
    three-line fixture and free of ORM lifetime hazards.

    All field names and defaults mirror SPEC §3.1 exactly.
    """

    connection_id: int = 0
    subject_email: str = ''
    auth_mode: str = 'dwd'
    sa_info: Mapping[str, Any] = field(default_factory=dict, repr=False)
    scopes: Sequence[str] = SCOPES
    include_shared_with_me: bool = True
    include_shared_drives: bool = True
    include_trashed: bool = False
    corpora_mode: str = 'per_drive'
    max_blob_bytes: int = 104857600
    sheets_reads_per_min: int = DEFAULT_SHEETS_READS_PER_MIN
    drive_units_per_min: int = DEFAULT_DRIVE_UNITS_PER_MIN
    http_timeout_connect: float = 10.0
    http_timeout_read: float = 120.0
    max_retry_attempts: int = 8
    sa_key_env_var: str = DEFAULT_KEY_ENV_VAR
    sa_key_param_key: str = DEFAULT_KEY_PARAM_KEY

    @classmethod
    def from_record(cls, env: Any, connection: Any, info: Optional[Mapping] = None) -> 'ConnectionContext':
        """Build a context from a ``gdrive.connection`` recordset.

        Loads the service-account key eagerly (env var first, then
        ``ir.config_parameter`` with ``.sudo()``), because doing it lazily on a
        worker thread would need an ORM cursor there.
        """
        if info is None:
            info = load_service_account_info(env, connection)
        return cls(
            connection_id=int(_field(connection, 'id', 0) or 0),
            subject_email=_field(connection, 'subject_email', '') or '',
            auth_mode=_field(connection, 'auth_mode', 'dwd') or 'dwd',
            sa_info=dict(info or {}),
            scopes=tuple(SCOPES),
            include_shared_with_me=bool(_field(connection, 'include_shared_with_me', True)),
            include_shared_drives=bool(_field(connection, 'include_shared_drives', True)),
            include_trashed=bool(_field(connection, 'include_trashed', False)),
            corpora_mode=_field(connection, 'corpora_mode', 'per_drive') or 'per_drive',
            max_blob_bytes=int(_field(connection, 'max_blob_bytes', 104857600) or 104857600),
            sheets_reads_per_min=int(
                _field(connection, 'sheets_reads_per_min', DEFAULT_SHEETS_READS_PER_MIN)
                or DEFAULT_SHEETS_READS_PER_MIN
            ),
            drive_units_per_min=int(
                _field(connection, 'drive_units_per_min', DEFAULT_DRIVE_UNITS_PER_MIN)
                or DEFAULT_DRIVE_UNITS_PER_MIN
            ),
            http_timeout_connect=float(_field(connection, 'http_timeout_connect', 10.0) or 10.0),
            http_timeout_read=float(_field(connection, 'http_timeout_read', 120.0) or 120.0),
            max_retry_attempts=int(_field(connection, 'max_retry_attempts', 8) or 8),
            sa_key_env_var=_field(connection, 'sa_key_env_var', DEFAULT_KEY_ENV_VAR)
            or DEFAULT_KEY_ENV_VAR,
            sa_key_param_key=_field(connection, 'sa_key_param_key', DEFAULT_KEY_PARAM_KEY)
            or DEFAULT_KEY_PARAM_KEY,
        )

    # -- derived ---------------------------------------------------------- #

    @property
    def effective_subject(self) -> str:
        """The impersonation subject, or ``''`` in ``sa_direct`` degraded mode."""
        return self.subject_email if self.auth_mode == 'dwd' else ''

    @property
    def sa_client_email(self) -> str:
        """The service account's own address — what a human shares folders with."""
        return (self.sa_info or {}).get('client_email') or ''

    @property
    def sa_client_id(self) -> str:
        """The numeric OAuth2 client id to paste into the Admin console."""
        return (self.sa_info or {}).get('client_id') or ''

    def drive_bucket(self) -> TokenBucket:
        """The shared Drive token bucket for this connection."""
        return REGISTRY.bucket(self.connection_id, API_DRIVE, self.drive_units_per_min)

    def sheets_bucket(self) -> TokenBucket:
        """The shared Sheets token bucket for this connection."""
        return REGISTRY.bucket(self.connection_id, API_SHEETS, self.sheets_reads_per_min)

    def without_key(self) -> 'ConnectionContext':
        """A copy with the key stripped — safe to log or embed in a payload."""
        from .google_auth import key_summary

        return replace(self, sa_info=key_summary(self.sa_info or {}))


# --------------------------------------------------------------------------- #
# Service construction
# --------------------------------------------------------------------------- #


def _thread_cache() -> Dict[Tuple, Any]:
    cache = getattr(_local, 'services', None)
    if cache is None:
        cache = {}
        _local.services = cache
    return cache


def _cache_key(ctx: ConnectionContext, api: str) -> Tuple:
    """Key including the key id so a rotated credential invalidates the cache.

    ``private_key_id`` is a public identifier, not the key itself, so it is safe
    to hold in a process-lifetime dict.
    """
    return (
        int(ctx.connection_id or 0),
        ctx.effective_subject,
        api,
        (ctx.sa_info or {}).get('private_key_id') or '',
        threading.get_ident(),
    )


def _build_http(ctx: ConnectionContext, creds: Any) -> Any:
    """Return an authorized httplib2 transport carrying the connection timeouts.

    Returns ``None`` when ``google-auth-httplib2`` is unavailable, in which case
    the caller falls back to ``build(..., credentials=creds)`` and accepts
    googleapiclient's default timeout.

    Note on the two timeout fields: ``httplib2`` exposes a **single** socket
    timeout, not separate connect/read values. ``http_timeout_read`` (default
    120 s) is applied here because it is the binding one for a 10 MB media chunk;
    ``http_timeout_connect`` governs the token-refresh transport, which uses
    ``requests``. Both are surfaced on the connection so an operator on a slow
    link can raise them without patching code.
    """
    try:  # pragma: no cover - environment dependent
        import google_auth_httplib2
        import httplib2
    except Exception:  # pragma: no cover
        _logger.debug(
            'google-auth-httplib2 not installed; falling back to default '
            'googleapiclient transport (connection timeouts will not be applied).'
        )
        return None
    http = httplib2.Http(timeout=float(ctx.http_timeout_read or 120.0))
    return google_auth_httplib2.AuthorizedHttp(creds, http=http)


def _build(api: str, version: str, ctx: ConnectionContext) -> Any:
    """Construct one discovery-built service object for the current thread."""
    try:  # pragma: no cover - environment dependent
        from googleapiclient.discovery import build as _discovery_build
    except Exception as exc:  # pragma: no cover
        raise GDriveAuthError(
            'google-api-python-client is not installed. Add '
            '"google-api-python-client" to requirements.txt and rebuild.',
            code=CODE_MISSING_DEPENDENCY,
        ) from exc

    creds = build_credentials(ctx.sa_info, ctx.scopes, ctx.effective_subject or None)
    authed_http = _build_http(ctx, creds)
    try:
        if authed_http is not None:
            # `http=` and `credentials=` are mutually exclusive in googleapiclient.
            service = _discovery_build(api, version, http=authed_http, cache_discovery=False)
        else:
            service = _discovery_build(api, version, credentials=creds, cache_discovery=False)
    except Exception as exc:  # noqa: BLE001 - re-raised with context
        raise GDriveAuthError(
            'Could not build the Google %s %s client: %s' % (api, version, redact(exc)),
            code='service_build_failed',
        ) from exc
    _logger.debug(
        'Built %s/%s service for connection %s subject %r on thread %s.',
        api, version, ctx.connection_id, ctx.effective_subject or '<service account>',
        threading.get_ident(),
    )
    return service


def _get_service(ctx: ConnectionContext, api: str, version: str) -> Any:
    cache = _thread_cache()
    key = _cache_key(ctx, api)
    service = cache.get(key)
    if service is None:
        service = _build(api, version, ctx)
        cache[key] = service
    return service


def build_drive(connection_ctx: ConnectionContext) -> Any:
    """Return this thread's Drive v3 service for ``connection_ctx``.

    ``cache_discovery=False`` is mandatory: the default writes a discovery cache
    file to disk (a container filesystem write, often read-only) and emits the
    familiar ``file_cache is only supported with oauth2client<4.0.0`` warning on
    every single call.
    """
    return _get_service(connection_ctx, DRIVE_API, DRIVE_VERSION)


def build_sheets(connection_ctx: ConnectionContext) -> Any:
    """Return this thread's Sheets v4 service for ``connection_ctx``."""
    return _get_service(connection_ctx, SHEETS_API, SHEETS_VERSION)


def build_services(connection_ctx: ConnectionContext) -> Tuple[Any, Any]:
    """Return ``(drive, sheets)`` service objects for the **current thread**.

    Both are cached per thread. **Never** hand either object to another thread:
    see the module docstring. If you need concurrency, call ``build_services``
    again inside each worker — the credentials are cheap to reuse and the
    discovery document is cached by ``googleapiclient`` at the module level.
    """
    return build_drive(connection_ctx), build_sheets(connection_ctx)


def reset_services(connection_id: Optional[int] = None) -> None:
    """Drop this thread's cached service objects.

    Called after a credential change or from tests. Only affects the calling
    thread, which is correct: another thread's service object is still valid for
    the credentials it was built with, and will be rebuilt on its next miss
    because the cache key contains ``private_key_id``.
    """
    cache = _thread_cache()
    if connection_id is None:
        cache.clear()
        return
    for key in [k for k in cache if k[0] == int(connection_id)]:
        del cache[key]
