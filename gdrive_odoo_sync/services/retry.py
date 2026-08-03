"""Centralized retry / backoff-with-jitter for every Google API call (lane B).

**Every** network call in this package goes through :func:`execute_with_retry`.
There are no exceptions and no second implementation.

WHY not ``googleapiclient``'s own ``request.execute(num_retries=N)``: it retries
only on ``{500, 502, 503, 504}`` plus a handful of transport errors. It does **not**
retry ``403 rateLimitExceeded`` / ``403 userRateLimitExceeded``, which is precisely
what Drive emits under sustained load (SPEC §4.4). Relying on it therefore produces
a crawler that works in testing and collapses on the first real corpus.

WHY full jitter: ``min(2**n + uniform(0, 1), 64)``. Without the random term, every
worker that hit the same rate limit wakes at the same instant and re-creates the
thundering herd that caused the limit. The bound of 64 s keeps a single call from
parking a cron worker for the entire wall-clock budget.

WHY the never-retry list is explicit and closed: retrying a genuinely permanent
failure (``insufficientPermissions``, ``exportSizeLimitExceeded``,
``dailyLimitExceeded``) burns ~2 minutes of budget per file and then reports the
same error. Worse, ``dailyLimitExceeded`` retries actively deepen the hole.
"""

from __future__ import annotations

import functools
import http.client
import json
import logging
import random
import socket
import ssl
import time
from typing import Any, Callable, Iterable, Optional

from .errors import (
    CODE_RATE_LIMITED,
    GDriveAuthError,
    GDriveError,
    GDriveExportTooLarge,
    GDrivePermanentError,
    GDriveQuotaError,
    GDriveScopeError,
    GDriveTokenInvalid,
    redact,
)

_logger = logging.getLogger(__name__)

__all__ = [
    'execute_with_retry',
    'with_retry',
    'RETRY_STATUS',
    'RETRY_403_REASONS',
    'NEVER_RETRY_REASONS',
    'DEFAULT_MAX_ATTEMPTS',
    'MAX_BACKOFF_SECONDS',
    'http_status_of',
    'reason_of',
    'message_of',
    'retry_after_of',
    'is_retryable',
    'translate_http_error',
]

#: Statuses that are always transient (SPEC §4.4).
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

#: ``403`` is overloaded by Google: it is used both for "you may never do this"
#: and for "you are going too fast". Only these reasons mean the latter.
RETRY_403_REASONS = frozenset({
    'rateLimitExceeded',
    'userRateLimitExceeded',
    'backendError',
    'internalError',
})

#: Reasons that must never be retried under any status.
NEVER_RETRY_REASONS = frozenset({
    'insufficientPermissions',
    'insufficientFilePermissions',
    'appNotAuthorizedToFile',
    'cannotDownloadAbusiveFile',
    'exportSizeLimitExceeded',
    'dailyLimitExceeded',
    'storageQuotaExceeded',
    'notFound',
    'fileNotDownloadable',
    'forbidden',
    'domainPolicy',
    'sharingRateLimitExceeded',
})

DEFAULT_MAX_ATTEMPTS = 8
MAX_BACKOFF_SECONDS = 64.0

#: Hard ceiling applied to a server-supplied ``Retry-After``. Google occasionally
#: returns hours on a daily-quota response; sleeping that long inside a cron would
#: hold the per-connection advisory lock and starve every other connection.
RETRY_AFTER_CAP_SECONDS = 300.0


def _transport_exception_types() -> tuple:
    """Collect the transport-level exception classes that are worth retrying.

    Built at import time from whatever is installed, because ``httplib2`` and
    ``google.auth`` are optional at *import* time (a developer running lane C's
    unit tests has neither) but present in a real deployment.
    """
    types: list = [
        socket.timeout,
        socket.gaierror,
        ConnectionError,
        TimeoutError,
        ssl.SSLError,
        http.client.IncompleteRead,
        http.client.BadStatusLine,
        http.client.CannotSendRequest,
        http.client.ResponseNotReady,
        http.client.RemoteDisconnected,
    ]
    try:  # pragma: no cover - depends on the deployed environment
        import httplib2

        types.append(httplib2.HttpLib2Error)
    except Exception:
        pass
    try:  # pragma: no cover - depends on the deployed environment
        from google.auth import exceptions as _google_auth_exceptions

        types.append(_google_auth_exceptions.TransportError)
    except Exception:
        pass
    return tuple(types)


_TRANSPORT_RETRYABLE = _transport_exception_types()


# --------------------------------------------------------------------------- #
# HTTP error introspection
#
# Deliberately duck-typed rather than `isinstance(exc, HttpError)`: it keeps this
# module importable without googleapiclient and lets lane F's mocked-transport
# tests raise a trivial fake carrying `.resp` and `.content`.
# --------------------------------------------------------------------------- #


def http_status_of(exc: BaseException) -> Optional[int]:
    """Return the HTTP status carried by ``exc``, or ``None`` if it is not an
    HTTP error. Handles both ``HttpError.resp.status`` and the ``status_code``
    attribute used by the newer google-auth transports."""
    resp = getattr(exc, 'resp', None)
    status = getattr(resp, 'status', None)
    if status is None:
        status = getattr(exc, 'status_code', None)
    if status is None and isinstance(resp, dict):
        status = resp.get('status')
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _error_payload(exc: BaseException) -> dict:
    """Parse Google's JSON error envelope off ``exc.content``.

    Returns ``{}`` for anything unparseable — an unparseable body is a fact about
    the response, not a reason to crash inside the error handler.
    """
    content = getattr(exc, 'content', None)
    if content is None:
        return {}
    if isinstance(content, bytes):
        try:
            content = content.decode('utf-8', 'replace')
        except Exception:  # pragma: no cover - decode with 'replace' cannot raise
            return {}
    if not isinstance(content, str) or not content.strip():
        return {}
    try:
        parsed = json.loads(content)
    except (ValueError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    error = parsed.get('error')
    if isinstance(error, dict):
        return error
    if isinstance(error, str):
        # Token-endpoint style: {"error": "unauthorized_client",
        #                        "error_description": "Client is unauthorized…"}
        return {'status': error, 'message': parsed.get('error_description') or error}
    return {}


def reason_of(exc: BaseException) -> Optional[str]:
    """Return Google's ``reason`` string for ``exc``, verbatim, or ``None``.

    Falls back to the newer ``error.status`` enum (``PERMISSION_DENIED``,
    ``RESOURCE_EXHAUSTED``…) because Drive v3 and Sheets v4 do not agree on which
    envelope they emit, and both shapes appear in production on the same day.
    """
    error = _error_payload(exc)
    errors = error.get('errors')
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict) and first.get('reason'):
            return str(first['reason'])
    if error.get('status'):
        return str(error['status'])
    return None


def message_of(exc: BaseException) -> str:
    """Return the server's human message, redacted, falling back to ``str(exc)``."""
    error = _error_payload(exc)
    msg = error.get('message')
    if not msg:
        errors = error.get('errors')
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            msg = errors[0].get('message')
    return redact(msg or str(exc))


def retry_after_of(exc: BaseException) -> Optional[float]:
    """Return the ``Retry-After`` header value in seconds, if the server sent one.

    ``httplib2.Response`` is a ``dict`` subclass with lower-cased header keys;
    ``requests``-style responses expose ``.headers``. Both are supported because
    which one you get depends on whether the service was built with an
    ``AuthorizedHttp`` or with plain ``credentials=``.
    """
    resp = getattr(exc, 'resp', None)
    raw = None
    if isinstance(resp, dict):
        raw = resp.get('retry-after') or resp.get('Retry-After')
    if raw is None:
        headers = getattr(resp, 'headers', None)
        if isinstance(headers, dict):
            raw = headers.get('retry-after') or headers.get('Retry-After')
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        # HTTP-date form. Not worth parsing: fall back to computed backoff rather
        # than guessing, because a mis-parsed date could sleep for years.
        return None
    if value < 0:
        return None
    return min(value, RETRY_AFTER_CAP_SECONDS)


def is_retryable(exc: BaseException) -> bool:
    """Decide whether ``exc`` is worth another attempt.

    Order matters: the never-retry reason list wins over the status test, because
    ``403 dailyLimitExceeded`` and ``403 rateLimitExceeded`` share a status and
    demand opposite behaviour.
    """
    if isinstance(exc, _TRANSPORT_RETRYABLE):
        return True
    status = http_status_of(exc)
    if status is None:
        return False
    reason = reason_of(exc)
    if reason and reason in NEVER_RETRY_REASONS:
        return False
    if status == 403:
        return bool(reason and reason in RETRY_403_REASONS)
    if status == 429:
        return True
    if 400 <= status < 500:
        # 400/401/404 are contract or permission problems. Retrying a 400 is how
        # a malformed A1 range turns into eight identical malformed A1 ranges.
        return False
    return status in RETRY_STATUS


def translate_http_error(exc: BaseException, label: str = '') -> GDriveError:
    """Map a transport/HTTP exception onto this package's exception hierarchy.

    WHY translate at all: the layers above must not have to know that
    ``exportSizeLimitExceeded`` arrives as a ``403`` whose reason lives three
    levels deep in a JSON body. They ask "is this permanent?" by catching
    :class:`~.errors.GDrivePermanentError`.

    The original exception is always chained (``raise ... from exc`` at the call
    site) so nothing is swallowed and the traceback still points at the transport.
    """
    status = http_status_of(exc)
    reason = reason_of(exc)
    message = message_of(exc)
    prefix = ('%s: ' % label) if label else ''
    details = {'label': label} if label else {}

    if reason == 'exportSizeLimitExceeded':
        return GDriveExportTooLarge(prefix + message, details=details)

    if status in (401,) or (reason or '').startswith('unauthorized_client'):
        if 'unauthorized_client' in (reason or '') or 'unauthorized_client' in message:
            return GDriveScopeError(
                prefix + message,
                code='unauthorized_client',
                status=status,
                reason=reason,
                details=details,
            )
        return GDriveAuthError(prefix + message, status=status, reason=reason, details=details)

    if status == 404 or reason == 'notFound':
        return GDrivePermanentError(
            prefix + message, code='not_found', status=status, reason=reason, details=details
        )

    if status == 403 and reason in {'insufficientPermissions', 'insufficientFilePermissions',
                                    'appNotAuthorizedToFile', 'cannotDownloadAbusiveFile',
                                    'fileNotDownloadable'}:
        from .errors import CODE_NO_DOWNLOAD_PERMISSION

        return GDrivePermanentError(
            prefix + message,
            code=CODE_NO_DOWNLOAD_PERMISSION,
            status=status,
            reason=reason,
            details=details,
        )

    if status in (429,) or reason in RETRY_403_REASONS or reason == 'dailyLimitExceeded':
        return GDriveQuotaError(
            prefix + message, code=CODE_RATE_LIMITED, status=status, reason=reason, details=details
        )

    if status is not None and 400 <= status < 500:
        return GDrivePermanentError(
            prefix + message, status=status, reason=reason, details=details
        )

    return GDriveError(prefix + message, status=status, reason=reason, details=details)


def _is_token_invalid(exc: BaseException) -> bool:
    """True when ``exc`` says a Changes API page token is dead.

    Google signals this two different ways depending on corpus: ``404 notFound``
    for a user-corpus token and ``400`` with the message ``Invalid Value`` for a
    shared-drive token. Both mean the same thing and both must produce
    :class:`~.errors.GDriveTokenInvalid`.
    """
    status = http_status_of(exc)
    if status == 404:
        return True
    if status in (400, 410):
        msg = message_of(exc).lower()
        return 'invalid value' in msg or 'page token' in msg or 'starttoken' in msg
    return False


# --------------------------------------------------------------------------- #
# The wrapper
# --------------------------------------------------------------------------- #


def _sleep_for(attempt: int, exc: BaseException, max_backoff: float,
               rng: random.Random) -> float:
    """Compute the delay before retry ``attempt`` (0-based).

    ``Retry-After`` wins when present because the server knows when its own limit
    resets and guessing shorter simply produces another 429 — but it wins
    *within* the caller's ceiling, and it still gets jitter.

    WHY the ceiling applies to the server hint too: ``retry_after_of`` caps only
    at ``RETRY_AFTER_CAP_SECONDS`` (300 s), nearly five times the 64 s
    ``max_backoff`` the caller passes. Returned unclamped, a Drive 429 answering
    ``Retry-After: 300`` on each of the 8 default attempts parked a single
    ``execute_with_retry`` call for 2 100 seconds — three and a half times the
    whole 600 s cron budget — while holding the connection's advisory lock, with
    nothing to interrupt it (the deadline is only checked between nodes) and the
    Odoo worker blocked until ``limit_time_real`` killed it.

    WHY jitter applies to the server hint too: a hint carries none, so every
    worker that hit the same limit resumed at the identical instant, recreating
    exactly the thundering herd jitter exists to prevent.
    """
    server_hint = retry_after_of(exc)
    if server_hint is not None:
        return min(float(server_hint), float(max_backoff)) + rng.uniform(0.0, 1.0)
    return min(float(2 ** attempt) + rng.uniform(0.0, 1.0), float(max_backoff))


def execute_with_retry(
    request: Any,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    max_backoff: float = MAX_BACKOFF_SECONDS,
    *,
    label: str = '',
    limiter: Any = None,
    cost: int = 1,
    token_aware: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    rng: Optional[random.Random] = None,
    on_retry: Optional[Callable[[int, float, BaseException], None]] = None,
    logger: Optional[logging.Logger] = None,
) -> Any:
    """Execute a Google API request with truncated exponential backoff + jitter.

    :param request: either a ``googleapiclient`` ``HttpRequest`` (anything with an
        ``.execute()`` method) or a zero-argument callable. The callable form
        exists for ``MediaIoBaseDownload.next_chunk`` and for the credential
        refresh, neither of which is an ``HttpRequest``.
    :param max_attempts: total attempts including the first. Comes from
        ``gdrive.connection.max_retry_attempts`` (default 8).
    :param max_backoff: ceiling for the computed sleep, seconds.
    :param label: short description used in log lines and error messages, e.g.
        ``"files.list(corpora=user)"``. Never include a file's *content*.
    :param limiter: an optional :class:`~.rate_limiter.TokenBucket`. Paced *before*
        each attempt, including retries — a retry that ignores the bucket is how a
        backoff storm turns into a quota ban.
    :param cost: units to charge the limiter per attempt.
    :param token_aware: when True, a dead page token is raised as
        :class:`~.errors.GDriveTokenInvalid` instead of a generic permanent error.
        Only ``changes.list`` sets this.
    :param sleep: injected for tests; the default is :func:`time.sleep`.
    :param rng: injected for tests so jitter is reproducible.
    :param on_retry: ``(attempt, delay, exc)`` callback, used by lane D to write a
        ``RATE_LIMITED`` run line so throttling is visible in the UI rather than
        only in the log.
    :returns: whatever the underlying call returns (a parsed dict for API calls).
    :raises GDriveError: always a subclass of it — never a raw ``HttpError`` and
        never ``None``. Failures are re-raised, never swallowed.
    """
    log = logger or _logger
    rng = rng or random
    attempts = max(1, int(max_attempts or 1))
    invoke: Callable[[], Any]
    if callable(request):
        invoke = request
    else:
        execute = getattr(request, 'execute', None)
        if not callable(execute):
            raise TypeError(
                'execute_with_retry expects an HttpRequest or a callable, got %r' % type(request)
            )
        # num_retries=0: this wrapper owns retry policy; layering googleapiclient's
        # own loop underneath would multiply the attempt count invisibly.
        invoke = functools.partial(execute, num_retries=0)

    last_exc: Optional[BaseException] = None
    for attempt in range(attempts):
        if limiter is not None:
            limiter.acquire(cost)
        try:
            return invoke()
        except GDriveError:
            # Already translated by an inner layer (e.g. a nested call). Trust it.
            raise
        except BaseException as exc:  # noqa: BLE001 - re-raised below, never swallowed
            last_exc = exc
            if token_aware and _is_token_invalid(exc):
                raise GDriveTokenInvalid(
                    '%s: %s' % (label or 'changes.list', message_of(exc)),
                    status=http_status_of(exc),
                    reason=reason_of(exc),
                ) from exc
            if not is_retryable(exc) or attempt == attempts - 1:
                break
            delay = _sleep_for(attempt, exc, max_backoff, rng)
            log.warning(
                'Google API call %s failed (attempt %d/%d, status=%s, reason=%s); '
                'retrying in %.2fs: %s',
                label or '<unlabelled>', attempt + 1, attempts,
                http_status_of(exc), reason_of(exc), delay, redact(exc),
            )
            if on_retry is not None:
                on_retry(attempt + 1, delay, exc)
            sleep(delay)

    assert last_exc is not None  # the loop can only exit here via `break`
    translated = translate_http_error(last_exc, label=label)
    if isinstance(translated, GDriveQuotaError):
        translated.details.setdefault('attempts', attempts)
        log.error(
            'Google API call %s exhausted %d attempts against a rate limit: %s',
            label or '<unlabelled>', attempts, translated,
        )
    else:
        log.error('Google API call %s failed permanently: %s', label or '<unlabelled>', translated)
    raise translated from last_exc


def with_retry(
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    max_backoff: float = MAX_BACKOFF_SECONDS,
    *,
    label: str = '',
) -> Callable:
    """Decorator form of :func:`execute_with_retry` for whole functions.

    Used for multi-call operations that are only safely retryable as a unit — the
    chunked media download being the example. Individual API calls should use
    :func:`execute_with_retry` directly so the label identifies the exact call.
    """

    def decorate(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return execute_with_retry(
                lambda: fn(*args, **kwargs),
                max_attempts=max_attempts,
                max_backoff=max_backoff,
                label=label or fn.__name__,
            )

        return wrapper

    return decorate


def coerce_page_size(value: Any, maximum: int, default: int) -> int:
    """Clamp a page size into ``[1, maximum]``.

    WHY it exists: ``drives.list`` silently coerces ``pageSize > 100`` down to 100
    and ``files.list`` caps at 1000. Silent coercion combined with a hand-written
    pagination loop is a classic way to lose pages, so the clamp is explicit and
    shared (SPEC §4.2).
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    if n < 1:
        n = default
    return min(n, maximum)


def iter_pages(
    make_request: Callable[[Optional[str]], Any],
    extract: Callable[[dict], Iterable],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    label: str = '',
    limiter: Any = None,
    cost: int = 1,
    on_page: Optional[Callable[[dict], None]] = None,
    max_pages: Optional[int] = None,
) -> Iterable:
    """Drive a ``nextPageToken`` pagination loop to exhaustion.

    ``make_request(page_token)`` must build (not execute) the request; ``extract``
    pulls the item list out of the parsed response.

    WHY a shared loop: every hand-rolled pagination loop in the wild has the same
    bug — it stops after page 1 because ``nextPageToken`` was not requested in the
    ``fields`` mask, or it loops forever because the token is echoed unchanged.
    This loop guards both: callers cannot forget the mask (the field constants
    include it) and a repeated token aborts with :class:`GDriveIncompleteRead`.
    """
    from .errors import GDriveIncompleteRead

    page_token: Optional[str] = None
    seen_tokens: set = set()
    pages = 0
    while True:
        response = execute_with_retry(
            make_request(page_token),
            max_attempts=max_attempts,
            label='%s page=%d' % (label or 'list', pages + 1),
            limiter=limiter,
            cost=cost,
        )
        pages += 1
        if on_page is not None:
            on_page(response)
        for item in extract(response) or ():
            yield item
        page_token = response.get('nextPageToken')
        if not page_token:
            return
        if page_token in seen_tokens:
            raise GDriveIncompleteRead(
                '%s returned a repeating nextPageToken after %d pages; aborting to '
                'avoid an infinite loop.' % (label or 'list', pages)
            )
        seen_tokens.add(page_token)
        if max_pages is not None and pages >= max_pages:
            raise GDriveIncompleteRead(
                '%s stopped after the %d-page safety limit with more pages pending.'
                % (label or 'list', max_pages)
            )
