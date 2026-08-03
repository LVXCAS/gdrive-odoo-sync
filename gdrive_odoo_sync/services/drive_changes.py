"""Drive Changes API — incremental sync tokens (lane B).

The Changes API is what turns a 15-minute cron from "re-enumerate 40 000 files"
into "fetch the 3 things that moved". It is also the part of the Drive API with
the most ways to lose data quietly, so the semantics below are implemented
literally from SPEC §4.3 and none of them are negotiable:

* ``nextPageToken`` means *more pages in this poll*. ``newStartPageToken`` means
  *the cursor for the next poll* and appears **only on the final page**. Conflating
  them — using ``nextPageToken`` as the next cursor — skips every change that
  arrives between polls, forever, with no error.
* **Commit the mirrored data first; persist the cursor last.** Saving the token
  before the data means a crash in between loses those changes permanently: the
  token says "you have seen up to here" and the mirror says otherwise.
* Changes are **at-least-once**. Every handler above must be idempotent.
* ``removed=True`` means the file left *the subject's view* — deleted, trashed,
  permission revoked, or moved out of scope. It does **not** mean the file is
  gone, and it must never reach the business-record delete planner directly.
* Tokens are minted per ``(subject_email, drive_id)`` and are **not
  interchangeable**. A token minted with ``driveId=X`` must always be replayed
  with ``driveId=X``; replaying it without produces either garbage or a 404.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .drive_discovery import FILE_FIELDS, COST_LIST, COST_GET
from .errors import GDriveIncompleteRead, GDriveTokenInvalid
from .google_client import ConnectionContext
from .retry import DEFAULT_MAX_ATTEMPTS, coerce_page_size, execute_with_retry

_logger = logging.getLogger(__name__)

__all__ = ['CHANGES_FIELDS', 'CHANGES_PAGE_SIZE', 'MAX_POLL_PAGES', 'DriveChanges']

#: Both tokens are requested explicitly, alongside the same per-file mask used by
#: discovery, so a change record can be upserted without a follow-up ``files.get``.
CHANGES_FIELDS = (
    'nextPageToken,newStartPageToken,'
    'changes(changeType,time,removed,fileId,driveId,file(%s))' % FILE_FIELDS
)

CHANGES_PAGE_SIZE = 1000

#: Safety limit on a single poll. A cursor that has been stale for months can
#: legitimately return tens of thousands of changes; at some point re-enumerating
#: is cheaper and the caller should be told rather than left spinning inside one
#: cron slot with the connection's advisory lock held.
MAX_POLL_PAGES = 200


class DriveChanges:
    """Bootstrap and replay Drive change cursors for one connection.

    :param drive: a built Drive v3 service belonging to the calling thread.
    :param ctx: the :class:`~.google_client.ConnectionContext` (retry budget,
        trashed policy, rate limiter).
    :param limiter: overrides the shared Drive token bucket.
    """

    def __init__(
        self,
        drive: Any,
        ctx: Optional[ConnectionContext] = None,
        limiter: Any = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.drive = drive
        self.ctx = ctx
        self.limiter = limiter if limiter is not None else (ctx.drive_bucket() if ctx else None)
        self.max_attempts = int(getattr(ctx, 'max_retry_attempts', DEFAULT_MAX_ATTEMPTS) or
                                DEFAULT_MAX_ATTEMPTS)
        self.log = logger or _logger
        self.requests_made = 0
        self.units_used = 0
        self.changes_seen = 0

    # -- bootstrap -------------------------------------------------------- #

    def get_start_page_token(self, drive_id: Optional[str] = None) -> str:
        """Mint a fresh cursor for the user corpus, or for one shared drive.

        Call this exactly once per ``(subject_email, drive_id)`` at bootstrap, and
        again only after a :class:`~.errors.GDriveTokenInvalid`. Minting a fresh
        token skips everything that happened before the call, so it must always be
        paired with a full re-enumeration in the same run — otherwise the changes
        between the last valid token and the new one are lost silently.

        :returns: the opaque token string.
        """
        kwargs: Dict[str, Any] = {'supportsAllDrives': True}
        if drive_id:
            kwargs['driveId'] = drive_id
        request = self.drive.changes().getStartPageToken(**kwargs)
        self.requests_made += 1
        self.units_used += COST_GET
        response = execute_with_retry(
            request,
            max_attempts=self.max_attempts,
            label='changes.getStartPageToken(drive=%s)' % (drive_id or 'user'),
            limiter=self.limiter,
            cost=COST_GET,
        )
        token = (response or {}).get('startPageToken')
        if not token:
            raise GDriveIncompleteRead(
                'changes.getStartPageToken returned no startPageToken for drive=%r; '
                'refusing to bootstrap a cursor that would silently skip changes.'
                % (drive_id or 'user')
            )
        self.log.info(
            'Bootstrapped Drive change cursor for %s.', drive_id or 'the user corpus'
        )
        return str(token)

    # -- polling ---------------------------------------------------------- #

    def _build_list_request(
        self,
        page_token: str,
        drive_id: Optional[str],
        page_size: int,
        include_removed: bool,
    ) -> Any:
        kwargs: Dict[str, Any] = {
            'pageToken': page_token,
            'spaces': 'drive',
            'pageSize': page_size,
            'fields': CHANGES_FIELDS,
            'includeRemoved': include_removed,
            'includeCorpusRemovals': include_removed,
            # False so changes outside My Drive (shared-with-me, shared drives)
            # are reported. Defaulting to True is a common way to build a mirror
            # that never notices anything a colleague edits.
            'restrictToMyDrive': False,
            'supportsAllDrives': True,
            'includeItemsFromAllDrives': True,
        }
        if drive_id:
            # A drive-scoped token is only meaningful with its driveId. See the
            # module docstring: tokens are not interchangeable.
            kwargs['driveId'] = drive_id
        return self.drive.changes().list(**kwargs)

    def iter_changes(
        self,
        page_token: str,
        drive_id: Optional[str] = None,
        page_size: int = CHANGES_PAGE_SIZE,
        include_removed: bool = True,
        max_pages: int = MAX_POLL_PAGES,
    ) -> Iterator[Tuple[dict, Optional[str]]]:
        """Yield ``(change, new_start_page_token)`` pairs, streaming page by page.

        ``new_start_page_token`` is ``None`` for every change except those on the
        final page, where it carries the cursor to persist. Streaming form exists
        so a very large backlog can be committed incrementally instead of being
        accumulated in memory — but note the ordering rule still holds: the token
        may only be written after the data from **its own page and all prior
        pages** is committed.

        :raises GDriveTokenInvalid: the cursor is dead (404, or 400 ``Invalid
            Value``). The caller must mark the cursor invalid and force a full
            re-enumeration.
        :raises GDriveIncompleteRead: the final page arrived without a
            ``newStartPageToken``. Continuing would mean either persisting nothing
            (endless replay) or persisting a wrong token (permanent data loss), so
            the read is declared incomplete instead.
        """
        page_size = coerce_page_size(page_size, CHANGES_PAGE_SIZE, CHANGES_PAGE_SIZE)
        token: Optional[str] = page_token
        seen_tokens = {page_token}
        pages = 0
        label = 'changes.list(drive=%s)' % (drive_id or 'user')

        while True:
            request = self._build_list_request(token or '', drive_id, page_size, include_removed)
            self.requests_made += 1
            self.units_used += COST_LIST
            response = execute_with_retry(
                request,
                max_attempts=self.max_attempts,
                label='%s page=%d' % (label, pages + 1),
                limiter=self.limiter,
                cost=COST_LIST,
                # The one call in the system that translates a dead token into a
                # dedicated exception rather than a generic permanent error.
                token_aware=True,
            )
            pages += 1
            changes = response.get('changes') or []
            next_page_token = response.get('nextPageToken')
            new_start_page_token = response.get('newStartPageToken')
            is_final = not next_page_token

            if is_final and not new_start_page_token:
                raise GDriveIncompleteRead(
                    '%s returned a final page with neither nextPageToken nor '
                    'newStartPageToken after %d page(s). The cursor cannot be '
                    'advanced safely; treating this poll as incomplete.'
                    % (label, pages),
                    details={'drive_id': drive_id or '', 'pages': pages},
                )
            if new_start_page_token and next_page_token:
                # Google does not do this, but if it ever did, silently trusting a
                # mid-stream token would advance the cursor past unread pages.
                self.log.error(
                    '%s returned newStartPageToken on a non-final page; ignoring it '
                    'to avoid advancing the cursor past unread changes.', label,
                )
                new_start_page_token = None

            last_index = len(changes) - 1
            for index, change in enumerate(changes):
                self.changes_seen += 1
                # The token rides on the **last** change of the **final** page and
                # nowhere else, so a streaming consumer that commits on every
                # token can never persist the cursor while records from the same
                # page are still unprocessed.
                yield change, (new_start_page_token if (is_final and index == last_index) else None)

            if is_final:
                if not changes:
                    # An empty final page still carries the cursor, and the caller
                    # must still persist it — otherwise every poll re-reads the
                    # same window forever.
                    yield {'_empty': True, 'changeType': 'none'}, new_start_page_token
                return

            if next_page_token in seen_tokens:
                raise GDriveIncompleteRead(
                    '%s returned a repeating nextPageToken after %d page(s).'
                    % (label, pages)
                )
            seen_tokens.add(next_page_token)
            if pages >= max_pages:
                raise GDriveIncompleteRead(
                    '%s exceeded the %d-page safety limit with more pages pending. '
                    'The cursor is too far behind; force a full re-enumeration.'
                    % (label, max_pages),
                    details={'drive_id': drive_id or '', 'pages': pages},
                )
            token = next_page_token

    def poll(
        self,
        page_token: str,
        drive_id: Optional[str] = None,
        page_size: int = CHANGES_PAGE_SIZE,
        include_removed: bool = True,
        max_pages: int = MAX_POLL_PAGES,
    ) -> Tuple[List[dict], str]:
        """Drain every page for ``page_token`` and return ``(changes, new_token)``.

        The convenience form of :meth:`iter_changes` for the common case where the
        backlog fits comfortably in memory (it is bounded by
        ``max_pages * page_size`` and each record is small).

        The returned token is taken **only** from the final page. The caller must
        still commit the mirrored data before writing the token to
        ``gdrive.change.cursor.page_token`` — this method cannot enforce that
        ordering, and it is the ordering that makes a crash lose nothing.
        """
        collected: List[dict] = []
        new_token: Optional[str] = None
        for change, token in self.iter_changes(
            page_token,
            drive_id=drive_id,
            page_size=page_size,
            include_removed=include_removed,
            max_pages=max_pages,
        ):
            if not change.get('_empty'):
                collected.append(change)
            if token:
                new_token = token
        if not new_token:  # pragma: no cover - iter_changes raises before this
            raise GDriveIncompleteRead(
                'changes.list(drive=%s) completed without a newStartPageToken.'
                % (drive_id or 'user')
            )
        self.log.info(
            'Polled %d change(s) for %s over %d request(s).',
            len(collected), drive_id or 'the user corpus', self.requests_made,
        )
        return collected, new_token

    # -- interpretation helpers ------------------------------------------- #

    @staticmethod
    def is_removal(change: Any) -> bool:
        """True when the change means "this left the subject's view".

        Covers both the explicit ``removed`` flag and the case where the file
        payload is present but trashed. Lane D maps this to ``state='gone'``,
        ``gone_since=now``, ``active=False`` — and to **nothing else**. It never
        unlinks the node, never unlinks the attachment, and never reaches the
        business-record delete planner (SPEC §4.3).
        """
        if not isinstance(change, dict):
            return False
        if change.get('removed'):
            return True
        payload = change.get('file') or {}
        return bool(payload.get('trashed'))

    @staticmethod
    def file_id_of(change: Any) -> str:
        """The affected Drive file id, from either the change or its payload."""
        if not isinstance(change, dict):
            return ''
        return change.get('fileId') or (change.get('file') or {}).get('id') or ''

    def stats(self) -> dict:
        """Counters for ``gdrive.sync.run`` quota accounting."""
        return {
            'requests_made': self.requests_made,
            'drive_units_used': self.units_used,
            'changes_seen': self.changes_seen,
        }


def cursor_key(subject_email: str, drive_id: Optional[str]) -> Tuple[str, str]:
    """The identity of a change cursor: ``(subject_email, drive_id)``.

    Exposed so lane D and lane B agree on the key by construction rather than by
    convention. A token minted for one subject is meaningless for another — the
    Changes API reports changes to *a principal's view*, not to a file set.
    """
    return (subject_email or '', drive_id or '')
