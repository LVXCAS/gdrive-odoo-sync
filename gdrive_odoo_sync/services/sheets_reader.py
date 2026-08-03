"""Google Sheets v4 reader — tab enumeration and value reads (lane B).

Native Google Sheets are **never** exported (see :data:`~.mimetypes.EXPORT_MAP`);
their content comes through this module. Two structural reasons, both from
SPEC §3.4/§4.6: ``files.export`` hard-fails above 10 MB with
``exportSizeLimitExceeded`` (the ceiling is on the generated artefact, so chunking
cannot help), and exporting a multi-tab workbook to ``text/csv`` silently returns
**only the first tab** — data loss with a 200 response and no warning.

Four rules govern every read here. Each of them is a silent-corruption bug when
broken, which is why they are encoded as constants and code paths rather than as
parameters a caller could get wrong:

1. **``UNFORMATTED_VALUE`` + ``SERIAL_NUMBER``, always.** This module does not
   accept a ``valueRenderOption`` argument at all, so ``FORMATTED_VALUE`` is
   structurally unrequestable. Hashing formatted strings means that changing a
   column's number format — or the sheet's locale — rewrites every cell with zero
   data change and produces a full-dataset false drift.
2. **Apostrophes in A1 sheet titles are doubled.** A tab called ``Bob's Data``
   must be quoted as ``'Bob''s Data'``; failing to double yields
   ``400 Unable to parse range``, which reads like a bug in this module rather
   than like a legal tab title.
3. **``vr.get('values', [])``, never ``vr['values']``.** A completely empty tab
   omits the key entirely, so the subscript form raises ``KeyError`` on exactly
   the input that most needs a clean "0 rows" answer.
4. **``vr['range']`` is the authoritative extent**, not
   ``gridProperties.rowCount/columnCount``. The latter is the *allocated* grid —
   typically 1000×26 for a sheet holding 12 rows — and sizing anything from it
   manufactures a thousand phantom empty rows.

Everything returned here is plain dicts and lists. Nothing in this module imports
Odoo or lane C.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .errors import GDriveIncompleteRead, GDrivePermanentError
from .google_client import ConnectionContext
from .retry import DEFAULT_MAX_ATTEMPTS, execute_with_retry

_logger = logging.getLogger(__name__)

__all__ = [
    'SheetsReader',
    'VALUE_RENDER_OPTION',
    'DATE_TIME_RENDER_OPTION',
    'MAJOR_DIMENSION',
    'METADATA_FIELDS',
    'GRID_DATA_FIELDS',
    'MAX_RANGES_PER_REQUEST',
    'SHEET_TYPE_GRID',
    'EFFECTIVE_VALUE_KINDS',
    'KIND_EMPTY',
    'KIND_STRING',
    'KIND_NUMBER',
    'KIND_BOOL',
    'KIND_FORMULA',
    'KIND_ERROR',
    'column_letter',
    'column_index',
    'quote_title',
    'a1_ref',
    'range_for_title',
    'pad_rows',
    'is_error_cell',
    'violates_string_assertion',
]

# --------------------------------------------------------------------------- #
# Frozen render options (rule 1)
# --------------------------------------------------------------------------- #

#: The only value render option this module will ever send. Not a parameter.
VALUE_RENDER_OPTION = 'UNFORMATTED_VALUE'

#: Dates arrive as Lotus-style serials (days since 1899-12-30). Lane C's
#: ``serial_to_naive`` is written against exactly this representation; asking for
#: ``FORMATTED_STRING`` would hand it locale-dependent text instead.
DATE_TIME_RENDER_OPTION = 'SERIAL_NUMBER'

MAJOR_DIMENSION = 'ROWS'

#: Cheap, properties-only metadata mask (SPEC §4.6). ``includeGridData=False`` is
#: passed alongside it; together they turn "how many tabs does this workbook
#: have" into a few kilobytes instead of the whole grid.
METADATA_FIELDS = (
    'spreadsheetId,properties(title,timeZone,locale),'
    'sheets(properties(sheetId,title,index,sheetType,hidden,'
    'gridProperties(rowCount,columnCount,frozenRowCount)))'
)

#: Mask for the targeted ``effectiveValue`` probe used by ``assert_string_value``
#: columns. ``formattedValue`` is fetched purely for the human-readable side of a
#: drift report; it is **never** hashed and never reaches a canonical token.
GRID_DATA_FIELDS = (
    'sheets(properties(sheetId,title),'
    'data(startRow,startColumn,rowData(values(effectiveValue,formattedValue))))'
)

#: Ranges per ``values.batchGet``. The whole workbook in one request is the goal
#: (SPEC §4.6), but ranges travel as repeated query parameters and a workbook with
#: hundreds of long tab titles will otherwise blow the URL length limit and come
#: back as a ``400`` that looks nothing like "your URL was too long".
MAX_RANGES_PER_REQUEST = 100

#: Only ``GRID`` tabs hold cells. ``OBJECT`` tabs are charts and ``DATA_SOURCE``
#: tabs are BigQuery-connected; both return no values and must not be staged as
#: "an empty tab", which would trip the EMPTY_TAB mass-delete guard.
SHEET_TYPE_GRID = 'GRID'

#: One Sheets request against the per-user 60/min read quota.
COST_READ = 1

# The ``effectiveValue`` oneof branches, verbatim from the Sheets API, plus the
# synthetic ``empty`` used when a cell carries no value at all.
KIND_EMPTY = 'empty'
KIND_STRING = 'stringValue'
KIND_NUMBER = 'numberValue'
KIND_BOOL = 'boolValue'
KIND_FORMULA = 'formulaValue'
KIND_ERROR = 'errorValue'

EFFECTIVE_VALUE_KINDS = frozenset({
    KIND_EMPTY, KIND_STRING, KIND_NUMBER, KIND_BOOL, KIND_FORMULA, KIND_ERROR,
})


# --------------------------------------------------------------------------- #
# A1 helpers
#
# Shared with `xlsx_reader`, which imports `column_letter` and `quote_title` from
# here so the two readers cannot drift apart on how a reference is spelled. Every
# drift report cites an A1 reference so a human can click straight to the cell;
# two spellings of the same cell would make those references untrustworthy.
# --------------------------------------------------------------------------- #


def column_letter(index0: int) -> str:
    """Return the A1 column letters for a **0-based** column index.

    ``0 -> 'A'``, ``25 -> 'Z'``, ``26 -> 'AA'``. Implemented here rather than
    imported from ``openpyxl.utils`` because this module must stay importable in
    an environment with no openpyxl (lane F tests the Sheets path with a mocked
    transport and nothing else installed).
    """
    n = int(index0)
    if n < 0:
        raise ValueError('column index must be >= 0, got %r' % (index0,))
    letters = ''
    n += 1
    while n:
        n, remainder = divmod(n - 1, 26)
        letters = chr(ord('A') + remainder) + letters
    return letters


def column_index(letters: str) -> int:
    """Inverse of :func:`column_letter`: ``'AA' -> 26``. Case-insensitive."""
    value = 0
    for char in (letters or '').strip().upper():
        if not 'A' <= char <= 'Z':
            raise ValueError('not an A1 column reference: %r' % (letters,))
        value = value * 26 + (ord(char) - ord('A') + 1)
    if value == 0:
        raise ValueError('not an A1 column reference: %r' % (letters,))
    return value - 1


def quote_title(title: Any) -> str:
    """Quote a tab title for use in an A1 range, doubling apostrophes (rule 2).

    Quoting is applied **unconditionally**, not only when the title "needs" it.
    An unquoted title that happens to look like a cell reference (a tab named
    ``Q1``, ``A1`` or ``Sheet1!``) is parsed as a reference rather than as a sheet
    name, and the request silently reads the wrong thing instead of failing.
    """
    text = '' if title is None else str(title)
    return "'" + text.replace("'", "''") + "'"


def a1_ref(title: Any, row1: int, col0: int) -> str:
    """Build a fully qualified A1 reference, e.g. ``'Wholesale — Leads'!A412``.

    :param row1: 1-based sheet row.
    :param col0: 0-based column index.

    Every staged row and every drift record carries one of these
    (``gdrive.staged.row.a1_ref``, ``gdrive.drift.source_ref``) so a report reads
    as "here is the cell", not "here is row 412 of something".
    """
    return '%s!%s%d' % (quote_title(title), column_letter(col0), int(row1))


def range_for_title(title: Any, a1: str = '') -> str:
    """Return the batchGet range for a whole tab, or for ``a1`` inside it."""
    quoted = quote_title(title)
    return '%s!%s' % (quoted, a1) if a1 else quoted


def pad_rows(rows: Sequence[Sequence[Any]], fill: Any = '') -> Tuple[List[List[Any]], int]:
    """Right-pad ragged rows into a rectangle and return ``(rows, width)`` (rule 4).

    The Sheets API drops **trailing** empty cells from every row and **trailing**
    empty rows from every tab, while preserving interior empties as ``''``. A tab
    whose header is 12 columns wide therefore yields rows of length 12, 12, 3, 12,
    7… Indexing a header position into an unpadded row raises ``IndexError`` on
    some rows and, far worse, reads the *wrong column* on none of them — the
    failure is loud but only for some inputs, so it survives testing.

    Padding uses ``''`` rather than ``None`` because that is exactly what an
    interior empty cell already looks like; the two are indistinguishable to the
    canonicalizer (both become ``z:`` under the default ``empty_is_null``), and
    keeping them identical means a row's meaning cannot depend on whether the
    author happened to leave a trailing cell blank.
    """
    materialized = [list(row) for row in (rows or [])]
    width = max((len(row) for row in materialized), default=0)
    for row in materialized:
        if len(row) < width:
            row.extend([fill] * (width - len(row)))
    return materialized, width


def is_error_cell(cell: Optional[Dict[str, Any]]) -> bool:
    """True when an ``effectiveValue`` probe found a spreadsheet error value.

    ``#N/A``, ``#REF!``, ``#DIV/0!``, ``#VALUE!``, ``#NAME?``, ``#NUM!`` and
    ``#NULL!`` all land here. Lane C maps them to ``e:CELL_ERROR`` and lane D
    quarantines the row. They MUST NOT be read as empty: "this formula is broken"
    and "this cell is blank" are opposite facts, and conflating them writes NULL
    over real data.
    """
    return bool(cell) and cell.get('kind') == KIND_ERROR


def violates_string_assertion(cell: Optional[Dict[str, Any]]) -> bool:
    """True when an identifier column's cell came back as a number.

    Set ``assert_string_value = True`` on every SKU, barcode, invoice number,
    phone number, account number and postal code column, then call this. There is
    no recovery path and none is attempted: by the time Sheets reports
    ``numberValue`` the leading zeros are already gone (``"007"`` → ``7``) and
    anything past ~15 significant digits is already mangled
    (``12345678901234567890`` → ``1.2345678901234567e19``). The cell is refused
    and the row quarantined as ``IDENTIFIER_NUMERIC``.
    """
    return bool(cell) and cell.get('kind') == KIND_NUMBER


# --------------------------------------------------------------------------- #
# The reader
# --------------------------------------------------------------------------- #


class SheetsReader:
    """Read tab metadata and cell values from one Google Sheets workbook.

    :param sheets: a built Sheets v4 service belonging to the calling thread.
        ``googleapiclient`` service objects are not thread-safe; get one per
        thread from :func:`~.google_client.build_sheets`.
    :param ctx: the :class:`~.google_client.ConnectionContext`, supplying the
        retry budget and the shared Sheets token bucket. Optional so the class can
        be exercised against a mocked transport with nothing else configured.
    :param limiter: overrides the shared Sheets bucket (50 reads/min by default
        against Google's 60/min/user ceiling).

    The returned tab dicts use the **same key set** as
    :class:`~.xlsx_reader.XlsxReader` results, so lane D stages a native Sheet and
    an uploaded ``.xlsx`` through one code path.
    """

    def __init__(
        self,
        sheets: Any,
        ctx: Optional[ConnectionContext] = None,
        limiter: Any = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.sheets = sheets
        self.ctx = ctx
        self.limiter = limiter if limiter is not None else (ctx.sheets_bucket() if ctx else None)
        self.max_attempts = int(getattr(ctx, 'max_retry_attempts', DEFAULT_MAX_ATTEMPTS) or
                                DEFAULT_MAX_ATTEMPTS)
        self.log = logger or _logger
        self.requests_made = 0
        self.reads_used = 0
        self.tabs_read = 0
        self.rows_read = 0

    # -- metadata --------------------------------------------------------- #

    def get_metadata(self, spreadsheet_id: str) -> dict:
        """One cheap ``spreadsheets.get`` describing the workbook and its tabs.

        :returns: ``{'spreadsheet_id', 'title', 'time_zone', 'locale', 'tabs'}``
            where ``tabs`` is the list produced by :meth:`list_tabs`.

        ``time_zone`` matters: it is the workbook's own IANA zone and is the right
        default for ``gdrive.dataset.sheet_timezone``. A spreadsheet cell has no
        timezone of its own, so a ``datetime`` column with no declared zone is a
        contract error rather than a runtime guess (CANONICALIZATION §6.3), and
        this is where the sensible default comes from.
        """
        request = self.sheets.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            includeGridData=False,
            fields=METADATA_FIELDS,
        )
        self.requests_made += 1
        self.reads_used += COST_READ
        response = execute_with_retry(
            request,
            max_attempts=self.max_attempts,
            label='spreadsheets.get(%s)' % spreadsheet_id,
            limiter=self.limiter,
            cost=COST_READ,
        ) or {}
        properties = response.get('properties') or {}
        tabs = [self._tab_meta(raw) for raw in (response.get('sheets') or [])]
        return {
            'spreadsheet_id': response.get('spreadsheetId') or spreadsheet_id,
            'title': properties.get('title') or '',
            'time_zone': properties.get('timeZone') or '',
            'locale': properties.get('locale') or '',
            'tabs': tabs,
        }

    @staticmethod
    def _tab_meta(raw_sheet: Optional[Dict[str, Any]]) -> dict:
        """Flatten one ``sheets[]`` entry into the shared tab shape.

        Note the ``sheetId`` handling: the **first tab of every workbook has gid
        0**, which is falsy. ``properties.get('sheetId') or 0`` happens to be
        harmless, but ``if not gid`` or ``gid or fallback`` anywhere downstream
        would silently treat the primary tab as "no gid". The gid is the identity
        of a dataset (SPEC §3.5) and 0 is a perfectly ordinary value for it.
        """
        properties = (raw_sheet or {}).get('properties') or {}
        grid = properties.get('gridProperties') or {}
        gid = properties.get('sheetId')
        return {
            'source_kind': 'gsheet',
            'sheet_gid': int(gid) if gid is not None else 0,
            'tab_title': properties.get('title') or '',
            'tab_index': int(properties.get('index') or 0),
            'hidden': bool(properties.get('hidden')),
            'sheet_type': properties.get('sheetType') or SHEET_TYPE_GRID,
            'grid_rows': int(grid.get('rowCount') or 0),
            'grid_cols': int(grid.get('columnCount') or 0),
            'frozen_rows': int(grid.get('frozenRowCount') or 0),
        }

    def list_tabs(self, spreadsheet_id: str) -> List[dict]:
        """Enumerate every tab of a workbook — one request, properties only.

        Returns *all* tabs including hidden and non-``GRID`` ones, because their
        existence is auditable information: a tab that disappears from this list
        is a ``tab_missing`` structural drift, and a tab that was never listed
        cannot be distinguished from one that was deleted.
        """
        return self.get_metadata(spreadsheet_id)['tabs']

    @staticmethod
    def grid_tabs(tabs: Iterable[dict]) -> List[dict]:
        """Filter :meth:`list_tabs` output down to the ingestible ``GRID`` tabs.

        ``OBJECT`` (chart) and ``DATA_SOURCE`` (BigQuery-connected) tabs hold no
        cells. Staging them would produce a 0-row dataset, which the EMPTY_TAB
        guard correctly reads as a mass-delete signal — a false alarm generated
        entirely by us.
        """
        return [t for t in tabs if (t.get('sheet_type') or SHEET_TYPE_GRID) == SHEET_TYPE_GRID]

    # -- values ------------------------------------------------------------ #

    def read_all(self, spreadsheet_id: str, titles: Sequence[str]) -> List[dict]:
        """Read every listed tab's values, batching to as few requests as possible.

        :param titles: tab titles in the order the results are wanted. Titles, not
            gids: ``values.batchGet`` addresses tabs by A1 name only. That is also
            why a tab **rename between metadata and values** is detected here — the
            range simply fails to resolve — rather than silently reading a
            different tab.
        :returns: one dict per title, in the same order, with keys
            ``tab_title``, ``rows``, ``row_count``, ``col_count``, ``used_range``,
            ``read_complete``, ``warnings``, ``error_cells``.

        Uses exactly one ``values.batchGet`` per :data:`MAX_RANGES_PER_REQUEST`
        titles. One request for a whole workbook is the entire reason the Sheets
        quota (60 reads/min/user) is survivable at all: per-tab reads would burn
        the minute's budget on a single 60-tab workbook.
        """
        wanted = [t for t in (titles or [])]
        if not wanted:
            return []

        results: List[dict] = []
        for start in range(0, len(wanted), MAX_RANGES_PER_REQUEST):
            chunk = wanted[start:start + MAX_RANGES_PER_REQUEST]
            ranges = [range_for_title(title) for title in chunk]
            request = self.sheets.spreadsheets().values().batchGet(
                spreadsheetId=spreadsheet_id,
                ranges=ranges,
                majorDimension=MAJOR_DIMENSION,
                # Frozen by module contract — see rule 1 in the module docstring.
                valueRenderOption=VALUE_RENDER_OPTION,
                dateTimeRenderOption=DATE_TIME_RENDER_OPTION,
            )
            self.requests_made += 1
            self.reads_used += COST_READ
            response = execute_with_retry(
                request,
                max_attempts=self.max_attempts,
                label='values.batchGet(%s, %d range(s))' % (spreadsheet_id, len(ranges)),
                limiter=self.limiter,
                cost=COST_READ,
            ) or {}

            value_ranges = response.get('valueRanges') or []
            if len(value_ranges) != len(ranges):
                # The API contract is one valueRange per requested range, in
                # order. A short response means we cannot tell which tab we are
                # holding, and guessing by position would attribute one tab's rows
                # to another tab's dataset — silent cross-contamination.
                raise GDriveIncompleteRead(
                    'values.batchGet(%s) returned %d valueRange(s) for %d requested '
                    'range(s); refusing to match them up positionally.'
                    % (spreadsheet_id, len(value_ranges), len(ranges)),
                    details={
                        'spreadsheet_id': spreadsheet_id,
                        'requested': list(chunk),
                        'received': len(value_ranges),
                    },
                )
            for title, value_range in zip(chunk, value_ranges):
                results.append(self._tab_values(title, value_range))
        return results

    def _tab_values(self, title: str, value_range: dict) -> dict:
        """Turn one ``valueRange`` into the shared tab-values shape."""
        # Rule 3: an entirely empty tab omits `values` altogether.
        raw_rows = (value_range or {}).get('values', [])
        rows, width = pad_rows(raw_rows)
        # Rule 4: `range` is what Sheets actually resolved. `gridProperties` is the
        # allocated grid (usually 1000x26) and would manufacture phantom rows.
        used_range = (value_range or {}).get('range') or range_for_title(title)
        self.tabs_read += 1
        self.rows_read += len(rows)
        return {
            'tab_title': title,
            'rows': rows,
            'row_count': len(rows),
            'col_count': width,
            'used_range': used_range,
            # A successful batchGet is atomic for the tab: either the values came
            # back or `execute_with_retry` raised. Anything that makes a read
            # partial surfaces as an exception, never as a short result.
            'read_complete': True,
            'warnings': [],
            'error_cells': [],
        }

    def read_workbook(
        self,
        spreadsheet_id: str,
        grid_only: bool = True,
        include_hidden: bool = True,
    ) -> dict:
        """Metadata **and** values for a whole workbook: the call lane D wants.

        :returns: ``{'spreadsheet_id', 'title', 'time_zone', 'locale', 'tabs'}``
            where every tab dict merges :meth:`list_tabs` metadata with
            :meth:`read_all` values, giving the full shared shape (identical keys
            to :meth:`~.xlsx_reader.XlsxReader.read`).

        Costs exactly two Sheets reads for a workbook of up to
        :data:`MAX_RANGES_PER_REQUEST` tabs: one ``spreadsheets.get`` for the gids
        and one ``values.batchGet`` for every tab's cells.

        ``include_hidden`` defaults True. A hidden tab is still data, and hiding a
        tab in the UI is not a deletion; excluding it here would present as every
        row vanishing at once, which is precisely the signal the EMPTY_TAB guard
        treats as catastrophic.
        """
        metadata = self.get_metadata(spreadsheet_id)
        tabs = metadata['tabs']
        readable = self.grid_tabs(tabs) if grid_only else list(tabs)
        if not include_hidden:
            readable = [t for t in readable if not t.get('hidden')]

        values_by_title: Dict[str, dict] = {}
        for values in self.read_all(spreadsheet_id, [t['tab_title'] for t in readable]):
            values_by_title[values['tab_title']] = values

        merged: List[dict] = []
        for tab in tabs:
            entry = dict(tab)
            values = values_by_title.get(tab['tab_title'])
            if values is None:
                # Not read: a non-GRID tab, or hidden with include_hidden=False.
                # Reported with `read_complete=False` so lane D can never mistake
                # "we did not read this" for "this tab is empty".
                entry.update({
                    'rows': [],
                    'row_count': 0,
                    'col_count': 0,
                    'used_range': range_for_title(tab['tab_title']),
                    'read_complete': False,
                    'warnings': [{
                        'code': 'TAB_NOT_READ',
                        'message': 'Tab %r was not read (sheet_type=%s, hidden=%s).'
                                   % (tab['tab_title'], tab.get('sheet_type'), tab.get('hidden')),
                    }],
                    'error_cells': [],
                })
            else:
                entry.update(values)
                entry['tab_title'] = tab['tab_title']
            merged.append(entry)

        metadata = dict(metadata)
        metadata['tabs'] = merged
        self.log.info(
            'Read workbook %s (%r): %d tab(s), %d of them with values, %d row(s).',
            spreadsheet_id, metadata.get('title'), len(merged), len(values_by_title),
            sum(t.get('row_count') or 0 for t in merged),
        )
        return metadata

    # -- effectiveValue probe --------------------------------------------- #

    def read_effective_values(
        self,
        spreadsheet_id: str,
        title: str,
        a1: str = '',
    ) -> dict:
        """Read the ``effectiveValue`` oneof branch for every cell in a range.

        This is the second, targeted request that backs ``assert_string_value``
        (SPEC §4.6). ``values.batchGet`` cannot answer the question it asks:
        ``UNFORMATTED_VALUE`` hands back a Python ``float`` for both a numeric cell
        and a text cell that Sheets decided was numeric, so by the time the value
        reaches this process the distinction is gone. Only the typed
        ``effectiveValue`` union preserves it.

        :param a1: an A1 range **without** the sheet name, e.g. ``'C2:C500'``.
            Empty reads the whole tab, which is expensive — always scope it to the
            identifier columns.
        :returns: ``{'sheet_gid', 'tab_title', 'start_row', 'start_column',
            'cells'}`` where ``cells`` is a list of rows of cell dicts, each
            ``{'kind', 'value', 'formatted', 'row', 'col', 'a1'}`` with ``row``
            0-based absolute and ``col`` 0-based absolute.

        Use :func:`is_error_cell` and :func:`violates_string_assertion` on the
        cells rather than testing ``kind`` inline, so the two rules live in one
        place.
        """
        request = self.sheets.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            ranges=[range_for_title(title, a1)],
            includeGridData=True,
            fields=GRID_DATA_FIELDS,
        )
        self.requests_made += 1
        self.reads_used += COST_READ
        response = execute_with_retry(
            request,
            max_attempts=self.max_attempts,
            label='spreadsheets.get(%s, grid %s!%s)' % (spreadsheet_id, title, a1 or 'ALL'),
            limiter=self.limiter,
            cost=COST_READ,
        ) or {}

        sheets_payload = response.get('sheets') or []
        if not sheets_payload:
            raise GDrivePermanentError(
                'spreadsheets.get returned no sheet for range %s in %s; the tab was '
                'most likely renamed or deleted between metadata and read.'
                % (range_for_title(title, a1), spreadsheet_id),
                code='tab_missing',
            )
        sheet = sheets_payload[0]
        properties = sheet.get('properties') or {}
        data_blocks = sheet.get('data') or [{}]
        block = data_blocks[0] or {}
        # `startRow` / `startColumn` are **omitted when zero**, which is exactly
        # the common case of a range anchored at A1. `.get(..., 0)` is therefore
        # load-bearing: defaulting them to anything else shifts every A1 reference
        # in the resulting drift report by one block.
        start_row = int(block.get('startRow') or 0)
        start_column = int(block.get('startColumn') or 0)

        cells: List[List[dict]] = []
        for row_offset, row_data in enumerate(block.get('rowData') or []):
            row_cells: List[dict] = []
            for col_offset, raw_cell in enumerate((row_data or {}).get('values') or []):
                row_cells.append(self._effective_cell(
                    raw_cell, title, start_row + row_offset, start_column + col_offset,
                ))
            cells.append(row_cells)

        gid = properties.get('sheetId')
        return {
            'sheet_gid': int(gid) if gid is not None else 0,
            'tab_title': properties.get('title') or title,
            'start_row': start_row,
            'start_column': start_column,
            'cells': cells,
        }

    @staticmethod
    def _effective_cell(raw_cell: Any, title: str, row0: int, col0: int) -> dict:
        """Decode one ``CellData`` into ``{'kind', 'value', 'formatted', …}``.

        The ``effectiveValue`` union has exactly five branches and an absent
        union means an empty cell. ``errorValue`` is decoded to its ``#TYPE``
        spelling (``#REF!``, ``#DIV/0!``…) because that is what the user sees in
        the sheet, and a drift report that says ``CELL_ERROR`` without saying
        *which* error is a report nobody can act on.
        """
        cell = raw_cell or {}
        effective = cell.get('effectiveValue') or {}
        formatted = cell.get('formattedValue')
        base = {
            'formatted': formatted if formatted is not None else '',
            'row': int(row0),
            'col': int(col0),
            'a1': a1_ref(title, row0 + 1, col0),
        }
        if not effective:
            base.update({'kind': KIND_EMPTY, 'value': None})
            return base
        if KIND_ERROR in effective:
            error = effective.get(KIND_ERROR) or {}
            error_type = error.get('type') or 'ERROR'
            base.update({
                'kind': KIND_ERROR,
                'value': _ERROR_TYPE_TO_TOKEN.get(error_type, '#%s' % error_type),
                'error_type': error_type,
                'error_message': error.get('message') or '',
            })
            return base
        for kind in (KIND_STRING, KIND_NUMBER, KIND_BOOL, KIND_FORMULA):
            if kind in effective:
                base.update({'kind': kind, 'value': effective[kind]})
                return base
        # A branch Google added after this code was written. Surfacing it as
        # `empty` would silently drop data, so it is reported as an unknown kind
        # and lane D quarantines the row.
        unknown = sorted(effective.keys())[0]
        base.update({'kind': unknown, 'value': effective[unknown]})
        return base

    # -- accounting -------------------------------------------------------- #

    def stats(self) -> dict:
        """Counters for ``gdrive.sync.run.sheets_reads_used``."""
        return {
            'requests_made': self.requests_made,
            'sheets_reads_used': self.reads_used,
            'tabs_read': self.tabs_read,
            'rows_read': self.rows_read,
        }


#: Sheets reports an error's ``type`` as a bare enum; this is the spelling a user
#: recognises from the grid.
_ERROR_TYPE_TO_TOKEN = {
    'ERROR': '#ERROR!',
    'NULL_VALUE': '#NULL!',
    'DIVIDE_BY_ZERO': '#DIV/0!',
    'VALUE': '#VALUE!',
    'REF': '#REF!',
    'NAME': '#NAME?',
    'NUM': '#NUM!',
    'N_A': '#N/A',
    'LOADING': '#LOADING',
}
