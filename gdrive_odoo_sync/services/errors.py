"""Exception hierarchy and log redaction for the Google API client layer (lane B).

WHY a dedicated hierarchy instead of letting ``googleapiclient.errors.HttpError``
escape: the Odoo layers above (lanes D and E) must make *policy* decisions from an
error — "skip this node", "invalidate this cursor", "mark the run incomplete",
"stop the whole connection" — and they must be able to do that without importing
``googleapiclient`` or re-parsing Google's JSON error envelope in five places.
Every raised error therefore carries a stable, documented ``code`` string that maps
directly onto the selection values in ``gdrive.node.skip_reason`` and
``gdrive.sync.run.line.code``.

WHY :func:`redact` lives here and not in a logging helper: SPEC §7.4 makes it an
invariant that the service-account private key never reaches a log line, a chatter
message, or ``gdrive.sync.run.line.payload``. The only way to guarantee that is to
put the scrubber at the bottom of the dependency graph, where every module that can
possibly format an error message can reach it, and to run it inside
``GDriveError.__str__`` so that even a naive ``_logger.exception(exc)`` is safe.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

__all__ = [
    'GDriveError',
    'GDriveAuthError',
    'GDriveScopeError',
    'GDriveQuotaError',
    'GDrivePermanentError',
    'GDriveExportTooLarge',
    'GDriveTokenInvalid',
    'GDriveIncompleteRead',
    'redact',
    'CODE_TOO_LARGE',
    'CODE_OUT_OF_SCOPE',
    'CODE_UNSUPPORTED_MIME',
    'CODE_NO_DOWNLOAD_PERMISSION',
    'CODE_SHORTCUT',
    'CODE_FOLDER',
    'CODE_EXPORT_SIZE_LIMIT',
    'CODE_IDENTIFIER_NUMERIC',
    'CODE_INCOMPLETE_SEARCH',
    'CODE_TOKEN_INVALID',
    'CODE_RATE_LIMITED',
    'CODE_XLSX_NO_CACHED_VALUES',
    'CODE_XLSX_TAB_AMBIGUOUS',
    'CODE_XLSX_SCAN_INCOMPLETE',
    'CODE_MISSING_DEPENDENCY',
]

# --------------------------------------------------------------------------- #
# Stable machine codes.
#
# The first six are byte-identical to the `gdrive.node.skip_reason` selection
# keys in SPEC §3.4, so lane D can do `node.skip_reason = exc.code` with no
# translation table. The rest are `gdrive.sync.run.line.code` values.
# --------------------------------------------------------------------------- #
CODE_TOO_LARGE = 'too_large'
CODE_OUT_OF_SCOPE = 'out_of_scope'
CODE_UNSUPPORTED_MIME = 'unsupported_mime'
CODE_NO_DOWNLOAD_PERMISSION = 'no_download_permission'
CODE_SHORTCUT = 'shortcut'
CODE_FOLDER = 'folder'

CODE_EXPORT_SIZE_LIMIT = 'EXPORT_SIZE_LIMIT'
CODE_IDENTIFIER_NUMERIC = 'IDENTIFIER_NUMERIC'
CODE_INCOMPLETE_SEARCH = 'INCOMPLETE_SEARCH'
CODE_TOKEN_INVALID = 'TOKEN_INVALID'
CODE_RATE_LIMITED = 'RATE_LIMITED'
CODE_XLSX_NO_CACHED_VALUES = 'XLSX_NO_CACHED_VALUES'
CODE_XLSX_TAB_AMBIGUOUS = 'XLSX_TAB_AMBIGUOUS'
#: The formula pre-scan could not cover the workbook, so uncached-formula
#: detection is unreliable for the affected worksheets. They are reported
#: `read_complete=False` — an undetectable-formula workbook must never be
#: indistinguishable from a formula-free one.
CODE_XLSX_SCAN_INCOMPLETE = 'XLSX_SCAN_INCOMPLETE'
CODE_MISSING_DEPENDENCY = 'MISSING_DEPENDENCY'


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #

#: A PEM private key block, however it is line-wrapped (real newlines or the
#: literal ``\n`` two-character sequence that appears when the JSON key file is
#: dumped into a log). ``DOTALL`` so the body is matched across lines.
_PEM_RE = re.compile(
    r'-----BEGIN[ A-Z0-9]*PRIVATE KEY-----.*?-----END[ A-Z0-9]*PRIVATE KEY-----',
    re.DOTALL,
)

#: ``"private_key": "…"`` inside a serialized service-account JSON blob. The value
#: pattern understands backslash escapes so an embedded ``\"`` does not end the
#: match early.
_JSON_SECRET_RE = re.compile(
    r'("(?:private_key|private_key_id|refresh_token|access_token|client_secret)"'
    r'\s*:\s*)"(?:\\.|[^"\\])*"'
)

#: Bare OAuth2 bearer tokens. Google access tokens start with ``ya29.``; the
#: generic ``Authorization: Bearer`` header form is covered separately.
_BEARER_RE = re.compile(r'(?i)\b(bearer)\s+[A-Za-z0-9._\-]{8,}')
_YA29_RE = re.compile(r'\bya29\.[A-Za-z0-9._\-]{8,}')

_REDACTED = '[REDACTED]'


def redact(text: Any) -> str:
    """Strip anything resembling a credential out of ``text``.

    WHY this is mandatory rather than merely prudent: ``googleapiclient`` and
    ``google.auth`` both include the *request body* in some exception strings, and
    the JWT assertion built from the service-account key is a request body. A
    single ``_logger.error("auth failed: %s", exc)`` in an unguarded code path
    would therefore write a usable bearer credential for the whole of Lucas's
    Drive into the Odoo log file, which on Odoo.sh is downloadable.

    The function is deliberately total: it accepts ``None``, exceptions, dicts,
    anything, and always returns a ``str``. A redactor that can itself raise is a
    redactor that will be wrapped in a bare ``except`` and then bypassed.
    """
    if text is None:
        return ''
    try:
        s = text if isinstance(text, str) else str(text)
    except Exception:  # pragma: no cover - defensive; __str__ of a broken object
        return '<unprintable value>'
    s = _PEM_RE.sub(_REDACTED + ' PRIVATE KEY', s)
    s = _JSON_SECRET_RE.sub(r'\1"' + _REDACTED + '"', s)
    s = _BEARER_RE.sub(r'\1 ' + _REDACTED, s)
    s = _YA29_RE.sub(_REDACTED, s)
    return s


# --------------------------------------------------------------------------- #
# Hierarchy
# --------------------------------------------------------------------------- #


class GDriveError(Exception):
    """Base class for every failure raised by the lane B service layer.

    WHY it carries structured attributes rather than just a message: lane D writes
    ``gdrive.sync.run.line`` records with a machine-readable ``code`` column so
    that "how often did we get rate limited last week" is a ``read_group`` and not
    a log grep. Formatting that information into prose and re-parsing it later is
    how observability dies.

    :param message: human sentence, already safe to show a user.
    :param code: stable machine code (see the ``CODE_*`` constants).
    :param status: HTTP status, when the error came from an HTTP response.
    :param reason: Google's own ``error.errors[0].reason`` string, verbatim.
    :param details: any extra structured context; must be JSON-serializable
        because lane D puts it in ``gdrive.sync.run.line.payload``.
    """

    def __init__(
        self,
        message: str = '',
        code: Optional[str] = None,
        status: Optional[int] = None,
        reason: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.message = redact(message)
        self.code = code
        self.status = status
        self.reason = reason
        self.details = dict(details) if details else {}
        super().__init__(self.message)

    def __str__(self) -> str:
        """Render safely; the redaction is applied again because ``details`` and
        ``reason`` may have been mutated by a caller after construction."""
        bits = [self.message or self.__class__.__name__]
        if self.status is not None:
            bits.append('HTTP %s' % self.status)
        if self.reason:
            bits.append('reason=%s' % self.reason)
        if self.code:
            bits.append('code=%s' % self.code)
        return redact(' | '.join(bits))

    def as_dict(self) -> dict:
        """Return a JSON-serializable summary for ``gdrive.sync.run.line.payload``."""
        return {
            'error': type(self).__name__,
            'message': self.message,
            'code': self.code,
            'status': self.status,
            'reason': self.reason,
            'details': self.details,
        }


class GDriveAuthError(GDriveError):
    """Credentials could not be loaded, parsed, or exchanged for a token.

    Covers a malformed/absent key file and any token-endpoint refusal that is not
    specifically a scope problem. Never retryable: retrying a bad key eight times
    just wastes ten minutes and then reports the same thing.
    """


class GDriveScopeError(GDriveAuthError):
    """Domain-wide delegation is not (correctly) granted for these scopes.

    This is the single most common setup failure (SPEC §2.2 step 6) and Google
    reports it as ``401 unauthorized_client`` with *no mention of scopes*, so the
    remediation text carried on this exception is the whole value of having a
    distinct class.
    """


class GDriveQuotaError(GDriveError):
    """Rate limit or quota exhausted, and retries did not clear it.

    Distinct from :class:`GDrivePermanentError` because the correct response is to
    slow the token bucket down and resume next cron tick, not to mark the node
    permanently skipped.
    """


class GDrivePermanentError(GDriveError):
    """The call will never succeed as issued; do not retry, do not requeue.

    ``code`` is one of the ``skip_reason`` constants when the condition maps onto
    a node-level skip (too large, no download permission, unsupported MIME).
    """


class GDriveExportTooLarge(GDrivePermanentError):
    """``403 exportSizeLimitExceeded`` from ``files.export``.

    Chunked download does not help: the 10 MB ceiling is on the *generated*
    artefact, not on the transfer (SPEC §3.4). v1 records the node as skipped.
    """

    def __init__(self, message: str = '', **kw: Any) -> None:
        kw.setdefault('code', CODE_EXPORT_SIZE_LIMIT)
        kw.setdefault('status', 403)
        kw.setdefault('reason', 'exportSizeLimitExceeded')
        super().__init__(message or 'Export exceeded the 10 MB Drive export limit.', **kw)


class GDriveTokenInvalid(GDriveError):
    """A Changes API page token is no longer usable (404 / ``Invalid Value``).

    Forces ``gdrive.change.cursor.state = 'invalid'``, a full re-enumeration this
    run, and a freshly minted start token (SPEC §4.3). It is *not* an error the
    caller may ignore: silently continuing would leave the mirror permanently
    behind with no signal.
    """

    def __init__(self, message: str = '', **kw: Any) -> None:
        kw.setdefault('code', CODE_TOKEN_INVALID)
        super().__init__(message or 'Drive changes page token is invalid or expired.', **kw)


class GDriveIncompleteRead(GDriveError):
    """The read returned successfully but is known to be missing data.

    Raised for ``incompleteSearch = true`` in strict mode, for a truncated changes
    poll that never produced a ``newStartPageToken``, and for a partially consumed
    page. WHY this is an exception class and not a boolean return: an incomplete
    read must clear ``gdrive.sync.run.complete_read``, which is the gate SPEC §9.6
    uses to forbid the delete planner from running. An empty read looks exactly
    like "everything was deleted" (SPEC §2.1), so the difference between "I saw
    nothing" and "I could not see everything" has to be impossible to overlook.
    """

    def __init__(self, message: str = '', **kw: Any) -> None:
        kw.setdefault('code', CODE_INCOMPLETE_SEARCH)
        super().__init__(message or 'Drive reported an incomplete result set.', **kw)
