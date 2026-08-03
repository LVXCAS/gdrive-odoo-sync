# -*- coding: utf-8 -*-
"""Drive crawl orchestration for DriftWatch.

This module owns none of the hard Google-API problems -- pagination, retry,
quota pacing, ``incompleteSearch`` detection, domain-wide-delegation
impersonation -- because :mod:`.services.drive_discovery` and
:mod:`.services.drive_changes` already solve them and are already tested. This
module's only job is to wire those pieces together the one correct way and to
hold the single rule the rest of DriftWatch depends on: :class:`Store` must
never be told a node is missing unless the crawl that produced that
conclusion was provably complete.

WHY that rule gets a whole module-level callout and not just a docstring line:
an empty (or truncated) Drive read is indistinguishable, byte for byte, from
"the user deleted everything" -- both come back as a shorter list of files
than last time, with HTTP 200 and no error anywhere in the chain. The only
thing standing between a network blip and a false mass-deletion report is
this module refusing to call ``store.mark_missing()`` unless nothing went
wrong. See the comment on that call in :meth:`Crawler.crawl`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from .config import Config
from .services.drive_changes import DriveChanges
from .services.drive_discovery import DriveDiscovery
from .services.errors import GDriveError
from .services.google_auth import build_credentials
from .services.google_client import ConnectionContext, build_drive
from .services.mimetypes import is_folder, is_native_spreadsheet, is_spreadsheet_blob
from .store import Store

_logger = logging.getLogger(__name__)

__all__ = ['Crawler']


def _now_iso() -> str:
    """A stable, UTC, ISO-8601 timestamp -- computed once per run.

    Every node upserted during a single crawl gets the *same* ``last_seen``
    value. That is what makes ``last_seen`` comparable across the run: if it
    were re-read per node, two nodes seen a few milliseconds apart could
    never be told apart from "seen in this run" vs. "seen in the next one" by
    timestamp alone.
    """
    return datetime.now(timezone.utc).isoformat()


def _node_from_record(record: dict) -> dict:
    """Map one raw Drive ``files.list`` record onto ``store.upsert_node``'s contract.

    ``record`` is whatever :meth:`DriveDiscovery.crawl` yields: the fields in
    :data:`~.services.drive_discovery.FILE_FIELDS`, plus the private
    ``_corpus`` / ``_drive_id`` annotations that method adds.
    """
    mime = record.get('mimeType') or ''
    parents = record.get('parents') or []
    owners = record.get('owners') or []

    size: Optional[int]
    raw_size = record.get('size')
    try:
        size = int(raw_size) if raw_size is not None else None
    except (TypeError, ValueError):
        # Native Google types (docs, sheets, folders) simply omit `size`; a
        # malformed value is not worth failing the whole node over.
        size = None

    return {
        'file_id': record.get('id'),
        'name': record.get('name') or '',
        'mime_type': mime,
        # Only shared-drive files are guaranteed exactly one parent (SPEC
        # note in drive_discovery.py); a My Drive item can technically have
        # more than one. The node table models a tree, so the first parent
        # is recorded -- good enough for staging/verification, which never
        # needs to reconstruct a multi-parent DAG.
        'parent_id': parents[0] if parents else None,
        'drive_version': record.get('version'),
        'modified_time': record.get('modifiedTime'),
        'size': size,
        'owner_email': owners[0].get('emailAddress') if owners else None,
        'is_folder': 1 if is_folder(mime) else 0,
        'trashed': 1 if record.get('trashed') else 0,
        'shared_drive': record.get('_drive_id') or None,
        'web_link': record.get('webViewLink'),
    }


class Crawler:
    """Walks the whole Drive corpus visible to ``cfg.subject_email`` and stages
    every object into :class:`~.store.Store`.
    """

    def __init__(self, cfg: Config, store: Store) -> None:
        self.cfg = cfg
        self.store = store

    def crawl(self, run_id: Optional[int] = None, full: bool = True) -> dict:
        """Enumerate the corpus and upsert every node.

        :param run_id: accepted so a caller that tracks ``store.start_run`` /
            ``finish_run`` can pass it straight through; unused here.
        :param full: v1 only implements a full enumeration. A correct
            incremental replay needs a token *per* ``(subject, drive_id)``
            (see ``services/drive_changes.py``), but ``store``'s ``cursor``
            table keys on ``subject`` alone -- one row, not one per shared
            drive. Doing "incremental for My Drive, silently nothing for
            shared drives" would reproduce the exact failure mode this
            module exists to prevent: a corpus that looks fully checked but
            is not. So ``full=False`` currently still performs a full crawl
            rather than a partial one that *looks* cheaper and safe.
        :returns: ``{'files_seen', 'folders', 'spreadsheets', 'read_complete',
            'marked_missing', 'errors'}``. Never raises for an API-layer
            failure -- those are collected into ``errors`` -- only a bug in
            this module's own code (a ``TypeError``/``KeyError`` and the
            like) propagates.
        """
        del full  # reserved; see docstring.
        now = _now_iso()
        stats: dict = {
            'files_seen': 0,
            'folders': 0,
            'spreadsheets': 0,
            'read_complete': False,
            'marked_missing': 0,
            'errors': [],
        }

        # ------------------------------------------------------------------ #
        # Credentials. build_credentials() already refuses (raises
        # GDriveAuthError) if google-auth's with_subject() ever returned the
        # same object it was called on -- see services/google_auth.py. The
        # explicit build-both-and-compare below is a second, independent
        # check of exactly that landmine: an un-impersonated service account
        # has its own empty, 0 GB corpus and a crawl against it would
        # "succeed" having seen nothing, which is indistinguishable from a
        # deleted Drive. Two guards for one silent failure mode is deliberate.
        # ------------------------------------------------------------------ #
        try:
            info = self.cfg.service_account_info()
        except Exception as exc:  # FileNotFoundError / ValueError -- config, not API
            stats['errors'].append(str(exc))
            return stats

        try:
            base_creds = build_credentials(info, self.cfg.scopes, subject=None)
            creds = build_credentials(info, self.cfg.scopes, subject=self.cfg.subject_email)
            assert creds is not base_creds, (
                'build_credentials() returned the bare service-account credentials; '
                'impersonation of %r did not take effect. Crawling with these would '
                'silently see an empty Drive.' % self.cfg.subject_email
            )
        except GDriveError as exc:
            stats['errors'].append(str(exc))
            return stats
        del creds, base_creds  # build_drive() below builds its own; these were only checks.

        ctx = ConnectionContext(
            connection_id=0,
            subject_email=self.cfg.subject_email,
            auth_mode='dwd',
            sa_info=info,
            scopes=tuple(self.cfg.scopes),
            include_shared_with_me=True,
            include_shared_drives=True,
            include_trashed=False,
            corpora_mode='per_drive',
            drive_units_per_min=self.cfg.drive_reads_per_min,
        )

        try:
            drive = build_drive(ctx)
        except GDriveError as exc:
            stats['errors'].append(str(exc))
            return stats

        # ------------------------------------------------------------------ #
        # Bootstrap the incremental cursor BEFORE enumerating, not after.
        # getStartPageToken() means "the next poll starts here"; minting it
        # first and then walking the corpus means any change landing *during*
        # this crawl is still caught by the first incremental poll afterwards.
        # Minting it last would open a window -- between "the enumeration
        # observed the corpus" and "the token started covering it" -- in
        # which a concurrent edit falls through and is never seen again. This
        # failure is not fatal to the crawl itself: incremental sync simply
        # stays unavailable until a later run mints one successfully.
        # ------------------------------------------------------------------ #
        cursor_token = ''
        try:
            cursor_token = DriveChanges(drive, ctx=ctx).get_start_page_token()
        except GDriveError as exc:
            stats['errors'].append(str(exc))

        discovery = DriveDiscovery(drive, ctx=ctx, strict_incomplete=False)

        seen_ids: set = set()
        truncated = False
        try:
            for record in discovery.crawl(
                include_shared_drives=True,
                include_shared_with_me=True,
                include_trashed=False,
                corpora_mode='per_drive',
            ):
                file_id = record.get('id')
                if not file_id:
                    continue
                seen_ids.add(file_id)
                self.store.upsert_node(_node_from_record(record), now)

                stats['files_seen'] += 1
                mime = record.get('mimeType') or ''
                if is_folder(mime):
                    stats['folders'] += 1
                elif is_native_spreadsheet(mime) or is_spreadsheet_blob(mime):
                    stats['spreadsheets'] += 1

                # cfg.max_files is a safety valve for a first run against an
                # unknown-sized corpus. Hitting it means the corpus was not
                # fully read, so it forces read_complete=False below exactly
                # like any other incomplete read.
                if self.cfg.max_files and len(seen_ids) >= self.cfg.max_files:
                    truncated = True
                    _logger.warning(
                        'Crawl stopped at max_files=%d; this run is necessarily '
                        'incomplete.', self.cfg.max_files,
                    )
                    break
        except GDriveError as exc:
            # Covers every failure this package raises -- auth, quota,
            # permanent, token-invalid, and GDriveIncompleteRead (it
            # subclasses GDriveError) -- since execute_with_retry() never lets
            # a raw transport exception escape (see services/retry.py).
            # Whatever was already upserted above stays upserted: seeing a
            # node is never wrong. What must not happen is treating this run
            # as authoritative about what is now missing.
            stats['errors'].append(str(exc))
            stats['read_complete'] = False
            stats['marked_missing'] = 0
            return stats

        # ------------------------------------------------------------------ #
        # THE SINGLE MOST IMPORTANT RULE IN THIS FILE.
        #
        # store.mark_missing() may run ONLY when this crawl was complete in
        # every sense checked below: no page raised, no retry budget was
        # exhausted (both would have hit the `except GDriveError` above and
        # already returned), no page reported `incompleteSearch` (tracked by
        # DriveDiscovery.complete_read), and max_files did not truncate the
        # walk. A partial read and a real mass-deletion produce the *identical*
        # observation -- a node that used to appear no longer does -- so
        # calling mark_missing() after anything less than a fully complete
        # read is exactly how a transient network blip becomes a false report
        # that the user deleted their entire Drive.
        # ------------------------------------------------------------------ #
        read_complete = discovery.complete_read and not truncated
        stats['read_complete'] = read_complete
        if read_complete:
            stats['marked_missing'] = self.store.mark_missing(seen_ids, now)
        else:
            stats['marked_missing'] = 0
            if truncated:
                stats['errors'].append(
                    'Crawl truncated by max_files=%d; missing-node detection '
                    'skipped for this run.' % self.cfg.max_files
                )
            if not discovery.complete_read:
                stats['errors'].extend(
                    'incompleteSearch: %s (q=%r)' % (d.get('label'), d.get('q'))
                    for d in discovery.incomplete_reads
                )

        # The cursor is persisted last, and unconditionally on `read_complete`
        # -- see the "bootstrap before enumerating" note above. It was minted
        # before the walk began, so it is valid as a forward-looking marker
        # regardless of whether this particular enumeration was complete; it
        # says nothing about, and grants no authority over, what is currently
        # missing (that is `mark_missing`'s job alone, gated above).
        if cursor_token:
            try:
                self.store.set_cursor(self.cfg.subject_email, cursor_token, now)
            except Exception as exc:  # sqlite-layer failure; do not lose the crawl's result
                stats['errors'].append('set_cursor failed: %s' % exc)

        return stats
