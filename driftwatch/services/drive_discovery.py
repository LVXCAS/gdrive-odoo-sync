"""Drive enumeration: My Drive, shared drives, shared-with-me (lane B).

This module owns the canonical ``fields`` mask and every ``files.list`` /
``drives.list`` call in the system.

Three rules govern every call here, and each of them fails **silently** when
broken — no exception, no warning, just missing data (SPEC §4.2):

1. ``supportsAllDrives=True`` and ``includeItemsFromAllDrives=True`` on every list
   call. Omit them and all shared-drive content vanishes with HTTP 200.
2. ``nextPageToken`` must appear **inside the fields mask**. Omit it and the
   response simply has no token, so a correct-looking pagination loop stops after
   page 1 and you conclude the user owns exactly 1000 files.
3. ``corpora='allDrives'`` is not used, because it is the mode that produces
   ``incompleteSearch=true`` — a *successful* response that means results are
   missing. Per-drive queries are the only mode immune to it.

An empty read is the most dangerous outcome in this system: it is indistinguishable
from "the user deleted everything" and would drive the delete planner if it were
ever trusted. Hence :attr:`DriveDiscovery.complete_read`, which lane D copies onto
``gdrive.sync.run.complete_read``, the gate SPEC §9.6 uses to forbid deletions.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Set

from .errors import CODE_INCOMPLETE_SEARCH, GDriveIncompleteRead
from .google_client import ConnectionContext
from .mimetypes import MIME_FOLDER, is_folder
from .retry import DEFAULT_MAX_ATTEMPTS, coerce_page_size, execute_with_retry, iter_pages

_logger = logging.getLogger(__name__)

__all__ = [
    'FILE_FIELDS',
    'FILES_LIST_FIELDS',
    'DRIVES_LIST_FIELDS',
    'PAGE_SIZE',
    'DRIVES_PAGE_SIZE',
    'DriveDiscovery',
]

#: The canonical per-file field mask (SPEC §4.2), verbatim.
#:
#: Every field here is load-bearing:
#:  * ``parents``     — the tree is reconstructed client-side; only shared-drive
#:                      files are guaranteed to have exactly one parent.
#:  * ``version``     — the L0 fast-path input. It increments on metadata-only
#:                      edits too, which errs toward "changed": the safe direction.
#:  * ``md5Checksum`` — blob-only; absent on native types. Skips re-download.
#:  * ``sharedWithMeTime`` — how ``include_shared_with_me`` is honoured without a
#:                      second query.
#:  * ``capabilities/canDownload`` — avoids a guaranteed 403 on restricted files.
FILE_FIELDS = (
    'id,name,mimeType,parents,modifiedTime,createdTime,size,'
    'md5Checksum,version,trashed,driveId,owners(emailAddress),'
    'shortcutDetails(targetId,targetMimeType),webViewLink,'
    'capabilities/canDownload,sharedWithMeTime'
)

#: ``nextPageToken`` and ``incompleteSearch`` are requested explicitly. See rule 2.
FILES_LIST_FIELDS = 'nextPageToken,incompleteSearch,files(%s)' % FILE_FIELDS

DRIVES_LIST_FIELDS = 'nextPageToken,drives(id,name)'

#: ``files.list`` caps at 1000 and silently coerces anything larger.
PAGE_SIZE = 1000

#: ``drives.list`` caps at **100** — a different, smaller limit that is also
#: silently coerced. Hard-coding 1000 here yields 100 results per page and a
#: pagination loop that still works but issues 10x the requests.
DRIVES_PAGE_SIZE = 100

# Approximate Drive quota-unit costs, used only for client-side pacing. Google
# does not publish a per-method table for Drive v3, so these are deliberately
# conservative: over-charging the bucket costs a little throughput, under-charging
# costs a 429 storm.
COST_LIST = 2
COST_GET = 1


class DriveDiscovery:
    """Paginated enumeration of everything the impersonated subject can see.

    :param drive: a built Drive v3 service (see :func:`~.google_client.build_drive`).
        Must belong to the calling thread — service objects are not thread-safe.
    :param ctx: the :class:`~.google_client.ConnectionContext`, used for the
        include/exclude flags, ``corpora_mode`` and the retry budget. Optional so
        the class can be exercised with a mocked transport and nothing else.
    :param limiter: a :class:`~.rate_limiter.TokenBucket`; defaults to the shared
        Drive bucket for ``ctx``.
    :param strict_incomplete: when True, ``incompleteSearch`` raises
        :class:`~.errors.GDriveIncompleteRead` immediately instead of being
        recorded. Default False, matching SPEC §4.2: the run continues but
        ``complete_read`` goes False and an ``INCOMPLETE_SEARCH`` warning line is
        logged, because partial data is still worth mirroring — it just must never
        authorize a delete.
    :param on_incomplete: callback ``(detail: dict) -> None`` so lane D can write
        the ``gdrive.sync.run.line`` without this module importing the ORM.
    """

    def __init__(
        self,
        drive: Any,
        ctx: Optional[ConnectionContext] = None,
        limiter: Any = None,
        strict_incomplete: bool = False,
        on_incomplete: Optional[Callable[[dict], None]] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.drive = drive
        self.ctx = ctx
        self.limiter = limiter if limiter is not None else (ctx.drive_bucket() if ctx else None)
        self.max_attempts = int(getattr(ctx, 'max_retry_attempts', DEFAULT_MAX_ATTEMPTS) or
                                DEFAULT_MAX_ATTEMPTS)
        self.strict_incomplete = bool(strict_incomplete)
        self.on_incomplete = on_incomplete
        self.log = logger or _logger
        #: Every ``incompleteSearch`` occurrence, as ``{'label':…, 'q':…}`` dicts.
        self.incomplete_reads: List[dict] = []
        #: Request and item counters for ``gdrive.sync.run`` quota accounting.
        self.requests_made = 0
        self.units_used = 0
        self.files_seen = 0

    # -- state ------------------------------------------------------------ #

    @property
    def complete_read(self) -> bool:
        """False once any page reported ``incompleteSearch``.

        Lane D ANDs this into ``gdrive.sync.run.complete_read``. It is the only
        thing standing between a partially visible corpus and a mass soft-delete.
        """
        return not self.incomplete_reads

    def _note_incomplete(self, label: str, query: str) -> None:
        detail = {'code': CODE_INCOMPLETE_SEARCH, 'label': label, 'q': query}
        self.incomplete_reads.append(detail)
        self.log.warning(
            'Drive reported incompleteSearch for %s (q=%r). The result set is '
            'missing rows; this run cannot be treated as a complete read.',
            label, query,
        )
        if self.on_incomplete is not None:
            self.on_incomplete(detail)
        if self.strict_incomplete:
            raise GDriveIncompleteRead(
                'incompleteSearch on %s (q=%r)' % (label, query), details=detail
            )

    # -- primitives ------------------------------------------------------- #

    def _trashed_clause(self, include_trashed: Optional[bool] = None) -> str:
        """Return the ``trashed`` term for a query.

        ``include_trashed`` defaults to the connection setting (normally False).
        Note that ``trashed = false`` is a *filter*, not a guarantee: a file
        trashed between two pages of the same listing simply disappears from the
        later pages, which is one more reason the mirror never deletes on absence.
        """
        if include_trashed is None:
            include_trashed = bool(getattr(self.ctx, 'include_trashed', False))
        return '' if include_trashed else 'trashed = false'

    @staticmethod
    def _and(*clauses: str) -> str:
        """Join non-empty query clauses with ``and``."""
        return ' and '.join(c for c in clauses if c)

    def list_files(
        self,
        q: str = '',
        corpora: str = 'user',
        drive_id: Optional[str] = None,
        order_by: Optional[str] = None,
        page_size: int = PAGE_SIZE,
        label: str = '',
    ) -> Iterator[dict]:
        """Yield every file matching ``q``, following ``nextPageToken`` to the end.

        This is the only place ``files().list`` is called. All three silent-failure
        parameters are set here so no caller can forget them.
        """
        label = label or ('files.list corpora=%s%s' % (corpora, (' drive=%s' % drive_id) if drive_id else ''))
        page_size = coerce_page_size(page_size, PAGE_SIZE, PAGE_SIZE)

        def make_request(page_token: Optional[str]) -> Any:
            kwargs: Dict[str, Any] = {
                'q': q or None,
                'corpora': corpora,
                'spaces': 'drive',
                'pageSize': page_size,
                'fields': FILES_LIST_FIELDS,
                # The two parameters whose omission silently hides every shared
                # drive. They are not optional and they are not defaults.
                'supportsAllDrives': True,
                'includeItemsFromAllDrives': True,
            }
            if drive_id:
                kwargs['driveId'] = drive_id
            if order_by:
                kwargs['orderBy'] = order_by
            if page_token:
                kwargs['pageToken'] = page_token
            kwargs = {k: v for k, v in kwargs.items() if v is not None}
            return self.drive.files().list(**kwargs)

        def on_page(response: dict) -> None:
            self.requests_made += 1
            self.units_used += COST_LIST
            if response.get('incompleteSearch'):
                self._note_incomplete(label, q)

        for item in iter_pages(
            make_request,
            lambda r: r.get('files', []),
            max_attempts=self.max_attempts,
            label=label,
            limiter=self.limiter,
            cost=COST_LIST,
            on_page=on_page,
        ):
            self.files_seen += 1
            yield item

    def get_file(self, file_id: str, fields: str = FILE_FIELDS) -> dict:
        """Fetch one file's metadata.

        ``supportsAllDrives=True`` is required here too: without it, a ``files.get``
        on a shared-drive item returns 404 even though the subject can read it.
        """
        request = self.drive.files().get(
            fileId=file_id, fields=fields, supportsAllDrives=True
        )
        self.requests_made += 1
        self.units_used += COST_GET
        return execute_with_retry(
            request,
            max_attempts=self.max_attempts,
            label='files.get(%s)' % file_id,
            limiter=self.limiter,
            cost=COST_GET,
        )

    def about(self) -> dict:
        """``about.get`` for the impersonated user. Probe P3.

        Returning ``user.emailAddress != subject_email`` is the definitive proof
        that domain-wide delegation is not in effect and that every subsequent
        listing would be the service account's own empty corpus.
        """
        request = self.drive.about().get(fields='user(emailAddress,displayName),storageQuota')
        self.requests_made += 1
        self.units_used += COST_GET
        return execute_with_retry(
            request,
            max_attempts=self.max_attempts,
            label='about.get',
            limiter=self.limiter,
            cost=COST_GET,
        )

    # -- corpora ---------------------------------------------------------- #

    def list_user_corpus(self, include_trashed: Optional[bool] = None) -> Iterator[dict]:
        """Everything in the subject's ``user`` corpus, flat.

        This single query returns My Drive **and** everything shared directly with
        the subject, at any nesting depth, in ``N/1000`` requests. The folder tree
        is then reconstructed client-side from the ``parents`` arrays — which is
        both cheaper and more correct than a recursive folder walk, because a
        recursive walk cannot see a deeply nested file whose intermediate folder
        was shared but not its ancestors.
        """
        yield from self.list_files(
            q=self._and(self._trashed_clause(include_trashed)),
            corpora='user',
            label='files.list user corpus',
        )

    def list_shared_with_me(self, include_trashed: Optional[bool] = None) -> Iterator[dict]:
        """Only the items other principals shared with the subject. Probe P5.

        Redundant with :meth:`list_user_corpus` for crawling (those items are
        already in the user corpus) and is therefore **not** used by
        :meth:`crawl`; it exists so the Test Connection wizard can report the
        count separately, which is how an operator confirms that folders from
        ``michael@``, ``Diego@`` and ``lvxxcas@gmail.com`` are actually reachable.
        """
        yield from self.list_files(
            q=self._and('sharedWithMe = true', self._trashed_clause(include_trashed)),
            corpora='user',
            label='files.list sharedWithMe',
        )

    def list_shared_drives(self, page_size: int = DRIVES_PAGE_SIZE) -> List[dict]:
        """Enumerate shared drives the subject can access. Probe P6.

        ``pageSize`` is clamped to 100 because Drive silently coerces anything
        larger — a coercion that is invisible in the response and looks like the
        organisation simply has 100 shared drives.
        """
        page_size = coerce_page_size(page_size, DRIVES_PAGE_SIZE, DRIVES_PAGE_SIZE)

        def make_request(page_token: Optional[str]) -> Any:
            kwargs: Dict[str, Any] = {'pageSize': page_size, 'fields': DRIVES_LIST_FIELDS}
            if page_token:
                kwargs['pageToken'] = page_token
            return self.drive.drives().list(**kwargs)

        def on_page(_response: dict) -> None:
            self.requests_made += 1
            self.units_used += COST_LIST

        return list(
            iter_pages(
                make_request,
                lambda r: r.get('drives', []),
                max_attempts=self.max_attempts,
                label='drives.list',
                limiter=self.limiter,
                cost=COST_LIST,
                on_page=on_page,
            )
        )

    def list_drive_corpus(
        self, drive_id: str, include_trashed: Optional[bool] = None
    ) -> Iterator[dict]:
        """Every file in one shared drive, via ``corpora='drive'``.

        One query per drive rather than a single ``corpora='allDrives'`` sweep.
        That costs a handful of extra requests and buys immunity from
        ``incompleteSearch``, which is the difference between a mirror you can
        trust to authorize deletions and one you cannot.
        """
        yield from self.list_files(
            q=self._and(self._trashed_clause(include_trashed)),
            corpora='drive',
            drive_id=drive_id,
            label='files.list drive=%s' % drive_id,
        )

    def list_all_drives_corpus(self, include_trashed: Optional[bool] = None) -> Iterator[dict]:
        """``corpora='allDrives'`` sweep — supported but not the default.

        Exposed for ``corpora_mode = 'all_drives'``. Expect ``incompleteSearch``
        on large corpora; when it happens :attr:`complete_read` goes False and the
        delete planner is disabled for the run, which is the entire reason
        ``per_drive`` is the shipped default.
        """
        yield from self.list_files(
            q=self._and(self._trashed_clause(include_trashed)),
            corpora='allDrives',
            label='files.list corpora=allDrives',
        )

    # -- bounded crawls --------------------------------------------------- #

    def list_folder_children(
        self, folder_id: str, include_trashed: Optional[bool] = None
    ) -> Iterator[dict]:
        """Direct children of one folder."""
        yield from self.list_files(
            q=self._and("'%s' in parents" % _escape_q(folder_id),
                        self._trashed_clause(include_trashed)),
            corpora='user',
            label='files.list children of %s' % folder_id,
        )

    def walk_folder(
        self,
        root_folder_id: str,
        max_depth: Optional[int] = None,
        include_trashed: Optional[bool] = None,
    ) -> Iterator[dict]:
        """Breadth-first walk of a folder subtree.

        Used for a bounded re-crawl when a user points the system at one folder.
        Not used for full discovery — see :meth:`list_user_corpus` for why a flat
        query beats a recursive walk there.

        Cycle-safe: Drive permits a file to have multiple parents, and a folder
        graph with a cycle would otherwise loop forever. Visited ids are tracked
        and each folder is expanded at most once. Yielded records carry
        ``_depth``.
        """
        visited: Set[str] = set()
        frontier: List[tuple] = [(root_folder_id, 0)]
        while frontier:
            folder_id, depth = frontier.pop(0)
            if folder_id in visited:
                continue
            visited.add(folder_id)
            for child in self.list_folder_children(folder_id, include_trashed=include_trashed):
                child = dict(child)
                child['_depth'] = depth + 1
                child['_corpus'] = 'folder'
                yield child
                if is_folder(child.get('mimeType')) and child.get('id') not in visited:
                    if max_depth is None or depth + 1 < max_depth:
                        frontier.append((child['id'], depth + 1))

    # -- the full crawl --------------------------------------------------- #

    def crawl(
        self,
        include_shared_drives: Optional[bool] = None,
        include_shared_with_me: Optional[bool] = None,
        include_trashed: Optional[bool] = None,
        corpora_mode: Optional[str] = None,
    ) -> Iterator[dict]:
        """Enumerate the whole visible corpus, deduplicated, in SPEC §4.2 order.

        Yields raw Drive file dicts annotated with two private keys:

        * ``_corpus`` — ``'user'``, ``'drive'`` or ``'all_drives'``;
        * ``_drive_id`` — the shared drive id, or ``''`` for My Drive items.

        Deduplication is by **file id**, which is the identity in this system. The
        same underlying file reachable both directly and through a shortcut, or
        listed in both the user corpus and a shared drive, is therefore emitted
        exactly once with no special-casing (SPEC §5.1).

        ``include_shared_with_me=False`` is honoured by dropping records that
        carry ``sharedWithMeTime``, rather than by a narrower query: the flat user
        corpus query returns them regardless, so filtering client-side is both
        correct and free.
        """
        ctx = self.ctx
        if include_shared_drives is None:
            include_shared_drives = bool(getattr(ctx, 'include_shared_drives', True))
        if include_shared_with_me is None:
            include_shared_with_me = bool(getattr(ctx, 'include_shared_with_me', True))
        if corpora_mode is None:
            corpora_mode = getattr(ctx, 'corpora_mode', 'per_drive') or 'per_drive'

        seen: Set[str] = set()

        def emit(record: dict, corpus: str, drive_id: str = '') -> Optional[dict]:
            file_id = record.get('id')
            if not file_id or file_id in seen:
                return None
            if not include_shared_with_me and record.get('sharedWithMeTime'):
                return None
            seen.add(file_id)
            out = dict(record)
            out['_corpus'] = corpus
            out['_drive_id'] = record.get('driveId') or drive_id or ''
            return out

        # 1. The user corpus, flat. My Drive plus everything shared with the
        #    subject, at any depth, reconstructed client-side from `parents`.
        for record in self.list_user_corpus(include_trashed=include_trashed):
            out = emit(record, 'user')
            if out is not None:
                yield out

        # 2. Shared drives.
        if include_shared_drives and corpora_mode != 'user':
            if corpora_mode == 'all_drives':
                for record in self.list_all_drives_corpus(include_trashed=include_trashed):
                    out = emit(record, 'all_drives')
                    if out is not None:
                        yield out
            else:
                drives = self.list_shared_drives()
                self.log.info('Discovered %d shared drive(s).', len(drives))
                for shared_drive in drives:
                    drive_id = shared_drive.get('id')
                    if not drive_id:
                        continue
                    for record in self.list_drive_corpus(
                        drive_id, include_trashed=include_trashed
                    ):
                        out = emit(record, 'drive', drive_id)
                        if out is not None:
                            yield out

        self.log.info(
            'Discovery finished: %d unique file(s) over %d request(s); complete_read=%s.',
            len(seen), self.requests_made, self.complete_read,
        )

    # -- helpers used by lane D ------------------------------------------- #

    @staticmethod
    def folder_query(parent_id: str) -> str:
        """Return the ``q`` selecting direct sub-folders of ``parent_id``."""
        return "'%s' in parents and mimeType = '%s' and trashed = false" % (
            _escape_q(parent_id), MIME_FOLDER,
        )

    def stats(self) -> dict:
        """Counters for ``gdrive.sync.run``."""
        return {
            'requests_made': self.requests_made,
            'drive_units_used': self.units_used,
            'files_seen': self.files_seen,
            'complete_read': self.complete_read,
            'incomplete_reads': list(self.incomplete_reads),
        }


def _escape_q(value: Any) -> str:
    """Escape a value for interpolation into a Drive ``q`` string literal.

    Drive's query language quotes literals with ``'`` and escapes ``\\`` and ``'``
    with a backslash. File ids never contain either, but folder ids arrive from
    user input via scope rules, and an unescaped apostrophe there produces a
    ``400`` that looks like a bug in this module rather than bad configuration.
    """
    return str(value or '').replace('\\', '\\\\').replace("'", "\\'")


def filter_by_owner(records: Iterable[dict], emails: Iterable[str]) -> Iterator[dict]:
    """Yield only records owned by one of ``emails`` (case-insensitive).

    A convenience for lane D's ``owner_email`` scope rules. Files with no
    ``owners`` array — which happens for some shared-drive items, where the drive
    itself is the owner — are **not** dropped by an include filter here; that
    decision belongs to the scope-rule evaluator, which knows whether the rule set
    is default-allow or default-deny.
    """
    wanted = {e.strip().lower() for e in emails if e}
    for record in records:
        owners = record.get('owners') or []
        addresses = {(o.get('emailAddress') or '').lower() for o in owners}
        if not addresses or (addresses & wanted):
            yield record
