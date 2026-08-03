# -*- coding: utf-8 -*-
"""Turn every discovered spreadsheet tab into a hashed, stored dataset.

WHY THIS MODULE EXISTS
=======================
The crawler finds Drive nodes; the canonicalization/hashing engine
(``lib.canon``, ``lib.hashing``, ``lib.merkle``) turns one cell, one row and
one whole tab into stable, comparable digests. Neither of them talks to the
other. This module is the glue: for every non-trashed spreadsheet node in the
store it reads every tab, builds a default column contract (there is no
human-authored mapping yet -- DriftWatch has no Odoo side to author one
against), canonicalizes and hashes each row, and persists the result through
:class:`~.store.Store`'s fixed contract.

**This module never reimplements canonicalization or hashing.** Every cell
goes through ``lib.canon.CANON``; every row hash comes from
``lib.hashing.h_row``; every dataset rollup comes from
``lib.merkle.dataset_digest``. Reimplementing any sliver of that here would
silently fork the rules the two engines are built to share, and a forked rule
is a digest that looks plausible and compares wrong.

TWO DELIBERATELY CONSERVATIVE CALLS
====================================
1. **No natural key unless the header says so unambiguously.** Without an
   administrator's mapping, this module cannot know which column is a
   business identity. Guessing from data (e.g. "the first column with no
   duplicates") would silently redefine a row's identity every time the data
   happened to change shape. So a column becomes the natural key only when
   its header, after canonicalization, is an *exact* match against a short,
   unambiguous vocabulary (``id``, ``sku``, ``barcode`` ...) -- see
   ``_STRICT_IDENTITY_SLUGS``. Anything less certain leaves ``natural_key``
   ``None`` and the row's identity falls back to its ``a1_ref``, exactly as
   the crawler's own contract prescribes.
2. **Every column is text, and identifier-looking columns assert it.** Per
   ``lib.contract.default_text_contract``, an unmapped column is text with
   ``empty_is_null=True`` -- the one setting that never coerces, rounds or
   guesses a separator. Columns whose header merely *suggests* an identifier
   (SKU, code, ID, barcode, ref, part number, ...) additionally get
   ``assert_string_value=True``, so ``CANON`` refuses -- rather than
   silently accepts -- a cell Sheets has already turned into a lossy number
   (``"007"`` -> ``7``). That refusal is reported as the ``type_coercion``
   drift, never repaired: by the time the number reaches this process the
   leading zeros are already gone.
"""
from __future__ import annotations

import datetime
import json
import logging
import re
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import Config
from .lib.canon import CANON
from .lib.contract import (
    CTYPE_TEXT,
    ColumnContract,
    slugify_all,
    spec_version_for_contracts,
    validate_contracts,
)
from .lib.hashing import h_header_hex, h_row, identity_key_bytes
from .lib.merkle import dataset_digest
from .lib.tokens import ERR_IDENTIFIER_NUMERIC, NULL_TOKEN, TAG_ERROR, is_error
from .services.drive_download import DriveDownloader
from .services.google_client import ConnectionContext, build_drive, build_sheets
from .services.mimetypes import MIME_SPREADSHEET, XLSX_MIMES, is_native_spreadsheet, is_spreadsheet_blob
from .services.rate_limiter import API_DRIVE, API_SHEETS, TokenBucket, bucket_for
from .services.sheets_reader import SheetsReader, a1_ref
from .services.xlsx_reader import XlsxReader, jsonable
from .store import Store

_logger = logging.getLogger(__name__)

__all__ = ['Stager']

#: Loose signal used only to decide ``assert_string_value`` -- whether a cell
#: that Sheets silently turned into a number should be refused as a
#: type_coercion drift instead of accepted as a legitimate value. Deliberately
#: broad: over-flagging costs nothing but an extra (harmless) guard on a text
#: column, while under-flagging loses a SKU's leading zeros permanently. See
#: ``lib.canon.violates_string_assertion``, which is what actually enforces
#: this once the contract flag is set.
_IDENTIFIER_HINT_RE = re.compile(
    r'\b('
    r'sku|barcode|upc|ean|isbn|'
    r'part\s*(?:no\.?|number)|item\s*(?:no\.?|number)|'
    r'product\s*(?:id|code)|serial(?:\s*(?:no\.?|number))?|'
    r'account\s*(?:no\.?|number)|invoice\s*(?:no\.?|number)|'
    r'order\s*(?:no\.?|number)|reference|ref|'
    r'postal\s*code|zip(?:\s*code)?|phone(?:\s*number)?|'
    r'code|id'
    r')\b',
    re.IGNORECASE,
)

#: Narrow signal used to pick a natural-key column with no human-authored
#: mapping to consult. An EXACT slug match against a short, unambiguous
#: vocabulary -- "do not invent identity" means a column merely *containing*
#: the word "code" (e.g. "Discount Code Notes") must never become a row's
#: identity, but a column whose entire header canonicalizes to "SKU" or "ID"
#: leaves nothing left to guess.
_STRICT_IDENTITY_SLUGS = frozenset({
    'id', 'sku', 'barcode', 'upc', 'ean', 'isbn',
    'code', 'ref', 'reference',
    'part_number', 'item_id', 'item_number',
    'product_id', 'product_code',
    'serial', 'serial_number',
})


def _now() -> str:
    """UTC timestamp in the ISO-8601 shape every ``now`` column expects."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


def _looks_like_identifier(header_text: str) -> bool:
    """True when ``header_text`` merely *suggests* an identifier column."""
    return bool(_IDENTIFIER_HINT_RE.search(header_text or ''))


def _row_is_blank(row: Sequence[Any]) -> bool:
    """True when every cell in ``row`` is empty -- used for the "no header" test."""
    return all(cell is None or (isinstance(cell, str) and cell.strip() == '') for cell in row)


def _label_of(token: str) -> str:
    """Strip a ``CANON`` tag off a header token, e.g. ``'s:SKU' -> 'SKU'``."""
    return token[2:] if isinstance(token, str) and len(token) >= 2 else ''


def _header_labels_from_row(row: Sequence[Any]) -> List[str]:
    """Canonicalize a raw header row into stable text labels.

    Uses ``CANON`` with no column contract (``col=None``), which dispatches to
    the default text canonicalizer: trimmed, whitespace-collapsed, case
    preserved. This is deliberately the *same* canonicalizer every other text
    cell goes through -- a header is just a cell that happens to sit in row 1.
    """
    return [_label_of(CANON(cell, None, side='sheet')) for cell in row]


def _blocked_reason_for(warnings: Sequence[dict]) -> Optional[str]:
    """Return the first warning ``code`` in ``warnings``, or ``None``."""
    for warning in warnings or ():
        code = warning.get('code') if isinstance(warning, dict) else None
        if code:
            return str(code)
    return None


class Stager:
    """Read every spreadsheet tab in the store, canonicalize it, and persist it.

    :param cfg: resolved :class:`~.config.Config` -- supplies credentials,
        the subject to impersonate, and the Sheets/Drive pacing rates.
    :param store: the open :class:`~.store.Store` this run reads nodes from
        and writes datasets, rows and drift into.

    Google service objects and rate-limit buckets are built lazily, once, on
    first use, and reused for the object's lifetime -- building them eagerly
    in ``__init__`` would mean a ``Stager()`` cannot be constructed (for a
    dry run, or in a test) without live credentials on disk.
    """

    def __init__(self, cfg: Config, store: Store) -> None:
        self.cfg = cfg
        self.store = store
        self._ctx: Optional[ConnectionContext] = None
        self._sheets_reader: Optional[SheetsReader] = None
        self._drive_downloader: Optional[DriveDownloader] = None
        self._xlsx_reader: Optional[XlsxReader] = None

    # ------------------------------------------------------------------ #
    # lazy service / rate-limiter construction
    # ------------------------------------------------------------------ #
    def _get_ctx(self) -> ConnectionContext:
        if self._ctx is None:
            info = self.cfg.service_account_info()
            self._ctx = ConnectionContext(
                connection_id=0,
                subject_email=self.cfg.subject_email,
                sa_info=info,
                scopes=self.cfg.scopes,
                sheets_reads_per_min=self.cfg.sheets_reads_per_min,
                drive_units_per_min=self.cfg.drive_reads_per_min,
            )
        return self._ctx

    def _sheets_bucket(self) -> TokenBucket:
        """The shared Sheets token bucket, paced to ``cfg.sheets_reads_per_min``.

        WHY the shared registry rather than a private bucket: Google counts
        every caller against the same per-user quota regardless of which
        object in this process issued the request, so a bucket that is not
        shared would let this module out-pace a concurrent caller into a 429
        neither of them can see coming.
        """
        return bucket_for(self._get_ctx().connection_id, API_SHEETS, self.cfg.sheets_reads_per_min)

    def _drive_bucket(self) -> TokenBucket:
        """The shared Drive token bucket, paced to ``cfg.drive_reads_per_min``."""
        return bucket_for(self._get_ctx().connection_id, API_DRIVE, self.cfg.drive_reads_per_min)

    def _get_sheets_reader(self) -> SheetsReader:
        if self._sheets_reader is None:
            ctx = self._get_ctx()
            self._sheets_reader = SheetsReader(build_sheets(ctx), ctx=ctx, limiter=self._sheets_bucket())
        return self._sheets_reader

    def _get_drive_downloader(self) -> DriveDownloader:
        if self._drive_downloader is None:
            ctx = self._get_ctx()
            self._drive_downloader = DriveDownloader(build_drive(ctx), ctx=ctx, limiter=self._drive_bucket())
        return self._drive_downloader

    def _get_xlsx_reader(self) -> XlsxReader:
        if self._xlsx_reader is None:
            self._xlsx_reader = XlsxReader()
        return self._xlsx_reader

    # ------------------------------------------------------------------ #
    # node discovery
    # ------------------------------------------------------------------ #
    def _spreadsheet_nodes(self) -> List[sqlite3.Row]:
        """Every non-trashed node that yields datasets: native Sheets + xlsx blobs.

        Two disjoint mime families, queried separately because
        ``Store.nodes()`` filters on exact equality, not a set membership
        test. De-duplicated by ``file_id`` even though the two families are
        already disjoint, because a defensive dedupe here is free and a
        double-staged file is not.
        """
        seen: Dict[str, sqlite3.Row] = {}
        for row in self.store.nodes(mime_type=MIME_SPREADSHEET):
            seen[row['file_id']] = row
        for mime in XLSX_MIMES:
            for row in self.store.nodes(mime_type=mime):
                seen[row['file_id']] = row
        return sorted(seen.values(), key=lambda r: (r['name'] or '', r['file_id']))

    def _find_node(self, file_id: str) -> Optional[sqlite3.Row]:
        for row in self._spreadsheet_nodes():
            if row['file_id'] == file_id:
                return row
        return None

    def _previous_dataset(self, file_id: str, tab_id: str) -> Optional[sqlite3.Row]:
        """The dataset row from the last run, if this ``(file_id, tab_id)`` exists."""
        for row in self.store.datasets(file_id=file_id):
            if row['tab_id'] == tab_id:
                return row
        return None

    # ------------------------------------------------------------------ #
    # column contract
    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_columns(header_labels: Sequence[str]) -> List[ColumnContract]:
        """Build the default text-only contract for one tab's header row.

        Every column is ``ctype=text`` -- see the module docstring for why
        that is the only choice lane C's own docs endorse in the absence of a
        human-authored mapping. ``slugify_all`` guarantees JCS-safe, deduped
        keys, which is what makes ``validate_contracts`` below a formality
        rather than a real risk of raising.
        """
        slugs = slugify_all(list(header_labels))
        return [
            ColumnContract(
                key=slug,
                header_canon=label,
                slug=slug,
                ctype=CTYPE_TEXT,
                col_index=idx,
                empty_is_null=True,
                assert_string_value=_looks_like_identifier(label),
                detect_error_literals=True,
            )
            for idx, (label, slug) in enumerate(zip(header_labels, slugs))
        ]

    @staticmethod
    def _natural_key_index(columns: Sequence[ColumnContract]) -> Optional[int]:
        """Index of the sole unambiguous identity column, or ``None``.

        ``None`` both when no column qualifies and when more than one does --
        two candidate identity columns is exactly the ambiguity "do not
        invent identity" forbids resolving by guesswork.
        """
        matches = [i for i, col in enumerate(columns) if col.slug in _STRICT_IDENTITY_SLUGS]
        return matches[0] if len(matches) == 1 else None

    # ------------------------------------------------------------------ #
    # reading
    # ------------------------------------------------------------------ #
    def _read_gsheet_tabs(self, file_id: str) -> List[dict]:
        workbook = self._get_sheets_reader().read_workbook(file_id, grid_only=True, include_hidden=True)
        return workbook.get('tabs') or []

    def _read_xlsx_tabs(self, node: sqlite3.Row) -> List[dict]:
        data = self._get_drive_downloader().fetch_blob(
            node['file_id'], mime=node['mime_type'], size_bytes=node['size'], can_download=True,
        )
        return self._get_xlsx_reader().read(data)

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def stage_all(self, run_id: Optional[int] = None, limit: Optional[int] = None) -> dict:
        """Stage every spreadsheet tab known to the store.

        :param run_id: an existing ``run`` id to attribute drift records to.
            When ``None`` this method opens and closes its own run, so it can
            be called standalone (e.g. from a CLI) without a caller managing
            run lifecycle.
        :param limit: cap the number of *files* processed this call. Combined
            with ``cfg.max_files`` (0 = unlimited) by taking the smaller of
            the two, never the larger -- a caller-supplied limit narrows the
            configured ceiling, it never widens it.
        :returns: ``{'files', 'tabs', 'rows', 'blocked', 'errors'}``.
        """
        own_run = run_id is None
        if own_run:
            run_id = self.store.start_run('stage', _now())

        stats: dict = {'files': 0, 'tabs': 0, 'rows': 0, 'blocked': 0, 'errors': []}
        status = 'ok'
        try:
            nodes = self._spreadsheet_nodes()
            cap = self.cfg.max_files or None
            if limit is not None:
                cap = limit if cap is None else min(cap, limit)
            if cap is not None:
                nodes = nodes[:cap]

            for node in nodes:
                # stage_file() never raises -- every per-file failure is
                # caught inside it and folded into its own `errors` list --
                # but the run as a whole must survive even a bug in that
                # contract, so nothing here is unguarded either.
                try:
                    file_stats = self.stage_file(node['file_id'], run_id=run_id)
                except Exception as exc:  # noqa: BLE001 - last-resort guard, see above
                    file_stats = {'tabs': 0, 'rows': 0, 'blocked': 0,
                                  'errors': [{'file_id': node['file_id'], 'name': node['name'],
                                              'error': str(exc)}]}
                stats['files'] += 1
                stats['tabs'] += file_stats['tabs']
                stats['rows'] += file_stats['rows']
                stats['blocked'] += file_stats['blocked']
                stats['errors'].extend(file_stats['errors'])
            if stats['errors']:
                status = 'partial'
        except Exception as exc:  # noqa: BLE001 - the run must still be closed out below
            status = 'error'
            stats['errors'].append({'error': str(exc)})
            raise
        finally:
            if own_run:
                self.store.finish_run(
                    run_id, _now(), status, stats=stats,
                    error=str(stats['errors'][-1]) if status == 'error' and stats['errors'] else '',
                )
        return stats

    def stage_file(self, file_id: str, run_id: Optional[int] = None) -> dict:
        """Stage every tab of one spreadsheet file.

        :returns: ``{'tabs', 'rows', 'blocked', 'errors'}``. Never raises: a
            file that cannot be read (revoked share, deleted mid-run,
            corrupt xlsx, quota exhaustion) is recorded in ``errors`` and
            everything else in the run continues.
        """
        stats: dict = {'tabs': 0, 'rows': 0, 'blocked': 0, 'errors': []}
        now = _now()

        node = self._find_node(file_id)
        if node is None:
            stats['errors'].append({
                'file_id': file_id,
                'error': 'no spreadsheet node found for this file_id (absent, trashed, '
                         'or not a spreadsheet mime type)',
            })
            return stats

        try:
            if is_native_spreadsheet(node['mime_type']):
                tabs = self._read_gsheet_tabs(file_id)
            elif is_spreadsheet_blob(node['mime_type']):
                tabs = self._read_xlsx_tabs(node)
            else:
                stats['errors'].append({'file_id': file_id, 'name': node['name'],
                                         'error': 'unsupported mime type %r' % node['mime_type']})
                return stats
        except Exception as exc:  # noqa: BLE001 - one unreadable workbook must not abort the run
            _logger.warning('Failed to read workbook %s (%r): %s', file_id, node['name'], exc)
            stats['errors'].append({'file_id': file_id, 'name': node['name'], 'error': str(exc)})
            return stats

        for tab in tabs:
            try:
                tab_stats = self._stage_tab(file_id, tab, run_id, now)
            except Exception as exc:  # noqa: BLE001 - one bad tab must not sink the workbook
                _logger.warning('Failed to stage tab %r of %s (%r): %s',
                                tab.get('tab_title'), file_id, node['name'], exc)
                stats['errors'].append({'file_id': file_id, 'name': node['name'],
                                         'tab_title': tab.get('tab_title'), 'error': str(exc)})
                continue
            stats['tabs'] += 1
            stats['rows'] += tab_stats['rows']
            stats['blocked'] += tab_stats['blocked']

        return stats

    # ------------------------------------------------------------------ #
    # one tab
    # ------------------------------------------------------------------ #
    def _stage_tab(self, file_id: str, tab: dict, run_id: Optional[int], now: str) -> dict:
        """Canonicalize, hash and persist one tab. Returns ``{'rows', 'blocked'}``."""
        tab_title = tab.get('tab_title') or ''
        sheet_gid = int(tab.get('sheet_gid') or 0)
        tab_id = str(sheet_gid)
        rows_raw: List[List[Any]] = tab.get('rows') or []
        read_complete = bool(tab.get('read_complete', True))

        prev = self._previous_dataset(file_id, tab_id)
        prev_header_list: List[str] = []
        prev_header_fp: Optional[str] = None
        if prev is not None:
            try:
                prev_header_list = json.loads(prev['header_json'] or '[]') or []
            except (TypeError, ValueError):
                prev_header_list = []
            if prev_header_list:
                prev_header_fp = h_header_hex(prev_header_list)

        no_rows = len(rows_raw) == 0
        no_header = (not no_rows) and _row_is_blank(rows_raw[0])

        # -- a read that Sheets/xlsx itself flagged incomplete -------------
        # Never replace rows here: stale-but-known rows beat rows from a
        # partial read, and this branch also covers non-GRID tabs (charts),
        # which never had cells to begin with and must not trip the
        # empty-tab guard below.
        if not read_complete:
            header_labels = [] if no_rows else _header_labels_from_row(rows_raw[0])
            blocked_reason = _blocked_reason_for(tab.get('warnings')) or 'read_incomplete'
            self.store.upsert_dataset(
                file_id, tab_id, tab_title, header_labels, now,
                row_count=max(0, len(rows_raw) - 1), col_count=tab.get('col_count') or 0,
                spec_version=None, h_dataset=None, bucket_hashes=[],
                read_complete=False, blocked_reason=blocked_reason,
            )
            return {'rows': 0, 'blocked': 1}

        # -- a fully read tab that has nothing usable in it -----------------
        # Zero rows where there were N last time is the exact signature of a
        # renamed tab, a revoked grant, or a truncated read (module WHY).
        # Rows are therefore never replaced here either.
        if no_rows or no_header:
            header_labels = [] if no_rows else _header_labels_from_row(rows_raw[0])
            dataset_id = self.store.upsert_dataset(
                file_id, tab_id, tab_title, header_labels, now,
                row_count=0, col_count=tab.get('col_count') or 0,
                spec_version=None, h_dataset=None, bucket_hashes=[],
                read_complete=True, blocked_reason='empty_tab',
            )
            self.store.record_drift(
                run_id, dataset_id, 'empty_tab', now, severity='warning',
                detail='Tab %r read 0 usable data row(s) (previous row_count=%s); rows '
                       'left unchanged rather than replaced with zero.'
                       % (tab_title, prev['row_count'] if prev is not None else 'n/a'),
            )
            return {'rows': 0, 'blocked': 1}

        # -- the normal path: a header and at least one data row ------------
        header_row = rows_raw[0]
        header_labels = _header_labels_from_row(header_row)
        columns = self._build_columns(header_labels)
        validate_contracts(columns)
        spec_version = spec_version_for_contracts(columns)
        key_idx = self._natural_key_index(columns)

        # Registered now (row_count/h_dataset filled in below) so drift
        # records produced while walking the rows have a dataset_id to
        # attach to, rather than being orphaned or requiring a second pass.
        dataset_id = self.store.upsert_dataset(
            file_id, tab_id, tab_title, header_labels, now,
            row_count=0, col_count=tab.get('col_count') or len(header_labels),
            spec_version=spec_version, h_dataset=None, bucket_hashes=[],
            read_complete=True, blocked_reason=None,
        )

        if prev_header_fp is not None and h_header_hex(header_labels) != prev_header_fp:
            self.store.record_drift(
                run_id, dataset_id, 'header_change', now, severity='warning',
                detail=json.dumps({'previous': prev_header_list, 'current': header_labels},
                                  ensure_ascii=False),
            )

        kept_rows: List[dict] = []
        entries: List[Tuple[bytes, bytes]] = []
        # natural_key -> every row_number that produced it, for duplicate_identity.
        seen_keys: Dict[str, List[int]] = {}

        for offset, row in enumerate(rows_raw[1:]):
            row_number = offset + 2  # 1-based; row 1 is the header
            a1 = a1_ref(tab_title, row_number, 0)
            canon: Dict[str, str] = {}
            raw: Dict[str, Any] = {}
            coercions: List[ColumnContract] = []
            quarantined = False

            for col_idx, col in enumerate(columns):
                cell = row[col_idx] if col_idx < len(row) else ''
                token = CANON(cell, col, side='sheet')
                canon[col.key] = token
                raw[col.key] = jsonable(cell)
                if is_error(token):
                    quarantined = True
                    if token == TAG_ERROR + ERR_IDENTIFIER_NUMERIC:
                        coercions.append(col)

            if quarantined:
                # A single e: token quarantines the whole row (CANONICALIZATION
                # invariant): a half-canonicalized row is worse than no row.
                # type_coercion is the one error family this contract asks to
                # be surfaced as its own drift, per offending column.
                for col in coercions:
                    self.store.record_drift(
                        run_id, dataset_id, 'type_coercion', now, severity='warning',
                        row_ref=a1, column_key=col.key, sheet_value=raw.get(col.key),
                        detail='Column %r looks numeric-coerced: Sheets returned a number '
                               'for a declared identifier column, and any leading zeros or '
                               'high-precision digits are already unrecoverable.'
                               % (col.header_canon,),
                    )
                continue

            natural_key: Optional[str] = None
            if key_idx is not None:
                key_token = canon.get(columns[key_idx].key, NULL_TOKEN)
                if key_token != NULL_TOKEN and not is_error(key_token):
                    natural_key = _label_of(key_token)
                    seen_keys.setdefault(natural_key, []).append(row_number)

            row_hash = h_row(canon, spec_version)
            identity_parts = [natural_key] if natural_key is not None else [a1]
            entries.append((identity_key_bytes(identity_parts), row_hash))
            kept_rows.append({
                'row_number': row_number,
                'a1_ref': a1,
                'natural_key': natural_key,
                'canon': canon,
                'raw': raw,
                'h_row': row_hash.hex(),
            })

        for key, row_numbers in seen_keys.items():
            if len(row_numbers) > 1:
                self.store.record_drift(
                    run_id, dataset_id, 'duplicate_identity', now, severity='warning',
                    column_key=columns[key_idx].key if key_idx is not None else None,
                    sheet_value=key,
                    detail='natural_key %r repeats on rows %r' % (key, row_numbers),
                )

        tab_uid = '%s/%d' % (file_id, sheet_gid)
        h_dataset_hex, bucket_hashes = dataset_digest(entries, spec_version, tab_uid)

        self.store.replace_rows(dataset_id, kept_rows)
        self.store.upsert_dataset(
            file_id, tab_id, tab_title, header_labels, now,
            row_count=len(kept_rows), col_count=tab.get('col_count') or len(header_labels),
            spec_version=spec_version, h_dataset=h_dataset_hex, bucket_hashes=bucket_hashes,
            read_complete=True, blocked_reason=None,
        )

        return {'rows': len(kept_rows), 'blocked': 0}
