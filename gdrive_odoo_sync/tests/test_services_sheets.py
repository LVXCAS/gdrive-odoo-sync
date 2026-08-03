# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. Licensed under LGPL-3.
"""Lane B — the Sheets reader (SPEC §4.6).

WHY the render options are asserted rather than assumed
=======================================================
``FORMATTED_VALUE`` **must never be hashed.** Changing a column's number format
or the spreadsheet's locale rewrites every string with zero data change, which
produces a full-dataset false drift the morning after somebody clicked a
currency button. ``UNFORMATTED_VALUE`` + ``SERIAL_NUMBER`` is the most
format-invariant pair the API offers, and it is the only pair permitted here.

Three more silent killers, each with a test below:

* **Apostrophes in tab titles.** A tab called ``Bob's Data`` must be quoted as
  ``'Bob''s Data'``. Failing to double it yields ``400 Unable to parse range``
  — which at least fails loudly — but the near-miss variants do not.
* **``vr['values']`` on an empty tab.** A completely empty tab omits the key
  entirely, so ``vr['values']`` raises ``KeyError`` inside a cron and the whole
  workbook read fails. ``vr.get('values', [])`` is mandatory.
* **``gridProperties`` instead of ``range``.** ``gridProperties.rowCount`` is the
  *allocated* grid, typically 1000×26. Sizing anything from it means reading
  974 phantom empty rows and then treating them as deleted data.
"""

from odoo.tests.common import BaseCase

from odoo.addons.gdrive_odoo_sync.services.sheets_reader import SheetsReader

from .test_services_drive import FakeCollection, FakeHttpError


class FakeSheets:
    """Minimal ``sheets`` service stub."""

    def __init__(self):
        self.calls = []
        self.script = {}

    def queue(self, collection, method, *responses):
        self.script.setdefault((collection, method), []).extend(responses)
        return self

    def spreadsheets(self):
        return _FakeSpreadsheets(self)

    def calls_to(self, collection, method):
        return [c for c in self.calls if c["collection"] == collection and c["method"] == method]


class _FakeSpreadsheets(FakeCollection):
    def __init__(self, service):
        super().__init__(service.calls, "spreadsheets", service.script)
        self._service = service

    def values(self):
        return FakeCollection(self._service.calls, "values", self._service.script)


TABS_RESPONSE = {
    "spreadsheetId": "SID1",
    "properties": {"title": "Food CPG Master", "timeZone": "America/New_York", "locale": "en_US"},
    "sheets": [
        {"properties": {"sheetId": 0, "title": "Investor Directory (79)", "index": 0,
                        "sheetType": "GRID", "hidden": False,
                        "gridProperties": {"rowCount": 1000, "columnCount": 26}}},
        {"properties": {"sheetId": 1874663210, "title": "Bob's Data", "index": 1,
                        "sheetType": "GRID", "hidden": False,
                        "gridProperties": {"rowCount": 1000, "columnCount": 26}}},
        {"properties": {"sheetId": 55, "title": "Chart", "index": 2,
                        "sheetType": "OBJECT", "hidden": True,
                        "gridProperties": {}}},
    ],
}


class TestListTabs(BaseCase):
    """One cheap request per workbook, properties only."""

    def test_grid_data_is_not_requested(self):
        sheets = FakeSheets()
        sheets.queue("spreadsheets", "get", TABS_RESPONSE)
        SheetsReader(sheets).list_tabs("SID1")
        kwargs = sheets.calls_to("spreadsheets", "get")[0]["kwargs"]
        self.assertFalse(kwargs.get("includeGridData", False))
        self.assertIn("sheetId", kwargs["fields"])
        self.assertNotIn("rowData", kwargs["fields"])

    def test_gid_is_the_identity_and_title_is_display_only(self):
        sheets = FakeSheets()
        sheets.queue("spreadsheets", "get", TABS_RESPONSE)
        tabs = SheetsReader(sheets).list_tabs("SID1")
        by_gid = {t["sheet_gid"]: t for t in tabs}
        self.assertEqual(set(by_gid), {0, 1874663210, 55})
        self.assertEqual(by_gid[0]["tab_title"], "Investor Directory (79)")

    def test_gid_zero_is_a_real_gid_not_a_missing_one(self):
        # The first tab of every Google Sheet has sheetId 0. Any code that does
        # `if gid:` loses it.
        sheets = FakeSheets()
        sheets.queue("spreadsheets", "get", TABS_RESPONSE)
        tabs = SheetsReader(sheets).list_tabs("SID1")
        self.assertIn(0, [t["sheet_gid"] for t in tabs])

    def test_sheet_type_and_hidden_are_reported(self):
        sheets = FakeSheets()
        sheets.queue("spreadsheets", "get", TABS_RESPONSE)
        tabs = {t["sheet_gid"]: t for t in SheetsReader(sheets).list_tabs("SID1")}
        self.assertEqual(tabs[0]["sheet_type"], "GRID")
        self.assertEqual(tabs[55]["sheet_type"], "OBJECT")
        self.assertTrue(tabs[55]["hidden"])

    def test_index_is_preserved(self):
        sheets = FakeSheets()
        sheets.queue("spreadsheets", "get", TABS_RESPONSE)
        tabs = SheetsReader(sheets).list_tabs("SID1")
        self.assertEqual([t["tab_index"] for t in tabs], [0, 1, 2])


class TestA1Quoting(BaseCase):
    """Apostrophes in a tab title must be doubled inside the quoted range."""

    def test_plain_title(self):
        self.assertEqual(SheetsReader.quote_title("Sheet1"), "'Sheet1'")

    def test_apostrophe_is_doubled(self):
        self.assertEqual(SheetsReader.quote_title("Bob's Data"), "'Bob''s Data'")

    def test_multiple_apostrophes(self):
        self.assertEqual(SheetsReader.quote_title("A'B'C"), "'A''B''C'")

    def test_em_dash_and_parentheses_need_no_escaping_but_do_need_quoting(self):
        # The real tab titles in this deployment contain U+2014 and parentheses.
        self.assertEqual(
            SheetsReader.quote_title("Food CPG Master — Investor Directory (79)"),
            "'Food CPG Master — Investor Directory (79)'",
        )

    def test_ranges_sent_to_batch_get_are_quoted(self):
        sheets = FakeSheets()
        sheets.queue("values", "batchGet", {"valueRanges": [
            {"range": "'Bob''s Data'!A1:B2", "values": [["a", "b"], ["c", "d"]]},
        ]})
        SheetsReader(sheets).read_all("SID1", ["Bob's Data"])
        ranges = sheets.calls_to("values", "batchGet")[0]["kwargs"]["ranges"]
        self.assertEqual(ranges, ["'Bob''s Data'"])


class TestRenderOptions(BaseCase):
    """The mandated pair, and the forbidden one."""

    def _read(self):
        sheets = FakeSheets()
        sheets.queue("values", "batchGet", {"valueRanges": [
            {"range": "Sheet1!A1:B1", "values": [["a", "b"]]},
        ]})
        SheetsReader(sheets).read_all("SID1", ["Sheet1"])
        return sheets.calls_to("values", "batchGet")[0]["kwargs"]

    def test_unformatted_value_is_requested(self):
        self.assertEqual(self._read()["valueRenderOption"], "UNFORMATTED_VALUE")

    def test_serial_number_dates_are_requested(self):
        self.assertEqual(self._read()["dateTimeRenderOption"], "SERIAL_NUMBER")

    def test_formatted_value_is_never_requested(self):
        # Re-formatting a column would otherwise rewrite every hashed string
        # with zero data change.
        self.assertNotIn("FORMATTED_VALUE", str(self._read()))

    def test_major_dimension_is_rows(self):
        self.assertEqual(self._read()["majorDimension"], "ROWS")

    def test_one_request_for_the_whole_workbook(self):
        sheets = FakeSheets()
        sheets.queue("values", "batchGet", {"valueRanges": [
            {"range": "'A'!A1:A1", "values": [["1"]]},
            {"range": "'B'!A1:A1", "values": [["2"]]},
            {"range": "'C'!A1:A1", "values": [["3"]]},
        ]})
        SheetsReader(sheets).read_all("SID1", ["A", "B", "C"])
        self.assertEqual(len(sheets.calls_to("values", "batchGet")), 1)


class TestResponseHandling(BaseCase):
    """Ragged rows, absent keys and the authoritative used range."""

    def test_empty_tab_omits_the_values_key_entirely(self):
        # vr['values'] would raise KeyError inside a cron and fail the whole
        # workbook; vr.get('values', []) is mandatory.
        sheets = FakeSheets()
        sheets.queue("values", "batchGet", {"valueRanges": [{"range": "'Empty'!A1:Z1000"}]})
        result = SheetsReader(sheets).read_all("SID1", ["Empty"])
        self.assertEqual(result[0]["rows"], [])

    def test_ragged_rows_are_right_padded_to_the_widest(self):
        sheets = FakeSheets()
        sheets.queue("values", "batchGet", {"valueRanges": [
            {"range": "'S'!A1:C3", "values": [[1, 2, 3], [4], [5, 6]]},
        ]})
        rows = SheetsReader(sheets).read_all("SID1", ["S"])[0]["rows"]
        self.assertEqual([len(r) for r in rows], [3, 3, 3])
        self.assertEqual(rows[1][0], 4)

    def test_interior_empties_are_preserved(self):
        sheets = FakeSheets()
        sheets.queue("values", "batchGet", {"valueRanges": [
            {"range": "'S'!A1:C1", "values": [["a", "", "c"]]},
        ]})
        rows = SheetsReader(sheets).read_all("SID1", ["S"])[0]["rows"]
        self.assertEqual(rows[0], ["a", "", "c"])

    def test_used_range_comes_from_the_response_not_from_grid_properties(self):
        # gridProperties.rowCount is the allocated grid (typically 1000);
        # sizing from it means reading hundreds of phantom rows and then
        # treating them as deleted.
        sheets = FakeSheets()
        sheets.queue("values", "batchGet", {"valueRanges": [
            {"range": "'Wholesale — Leads'!A1:M2411", "values": [["h"]]},
        ]})
        result = SheetsReader(sheets).read_all("SID1", ["Wholesale — Leads"])
        self.assertEqual(result[0]["used_range"], "'Wholesale — Leads'!A1:M2411")

    def test_value_ranges_come_back_in_request_order(self):
        sheets = FakeSheets()
        sheets.queue("values", "batchGet", {"valueRanges": [
            {"range": "'A'!A1:A1", "values": [["first"]]},
            {"range": "'B'!A1:A1", "values": [["second"]]},
        ]})
        result = SheetsReader(sheets).read_all("SID1", ["A", "B"])
        self.assertEqual([r["tab_title"] for r in result], ["A", "B"])
        self.assertEqual(result[0]["rows"][0][0], "first")
        self.assertEqual(result[1]["rows"][0][0], "second")

    def test_no_titles_makes_no_request(self):
        sheets = FakeSheets()
        self.assertEqual(SheetsReader(sheets).read_all("SID1", []), [])
        self.assertEqual(sheets.calls, [])


class TestEffectiveValues(BaseCase):
    """``assert_string_value`` — the identifier guard (SPEC §4.6, CANON §3.1)."""

    def _reader_with(self, cells):
        sheets = FakeSheets()
        sheets.queue("spreadsheets", "get", {
            "sheets": [{"properties": {"sheetId": 0},
                        "data": [{"rowData": [{"values": [c]} for c in cells]}]}],
        })
        return SheetsReader(sheets), sheets

    def test_fields_mask_requests_effective_value(self):
        reader, sheets = self._reader_with([{"effectiveValue": {"stringValue": "007"}}])
        reader.read_effective_values("SID1", "Sheet1", "A2:A2")
        fields = sheets.calls_to("spreadsheets", "get")[0]["kwargs"]["fields"]
        self.assertIn("effectiveValue", fields)

    def _first_cell(self, reader):
        # read_effective_values returns ONE dict describing the block, whose
        # 'cells' is a list of *rows* of cell dicts — not a flat cell list. The
        # fixture puts one cell per row, so [row][0] is the cell.
        result = reader.read_effective_values("SID1", "Sheet1", "A2:A2")
        self.assertIn("cells", result)
        return result["cells"][0][0]

    def test_string_value_branch_is_reported(self):
        reader, _ = self._reader_with([{"effectiveValue": {"stringValue": "007"}}])
        cell = self._first_cell(reader)
        self.assertEqual(cell["kind"], "stringValue")
        self.assertEqual(cell["value"], "007")

    def test_number_value_branch_is_reported_so_the_caller_can_refuse(self):
        # "007" read as a number is already 7; the leading zeros are gone and no
        # recovery is possible, which is why the cell is refused rather than
        # coerced back to a string.
        reader, _ = self._reader_with([{"effectiveValue": {"numberValue": 7}}])
        self.assertEqual(self._first_cell(reader)["kind"], "numberValue")

    def test_error_value_branch_is_reported(self):
        # #N/A must map to the e: token family and never to NULL.
        reader, _ = self._reader_with([
            {"effectiveValue": {"errorValue": {"type": "NA", "message": "#N/A"}}},
        ])
        self.assertEqual(self._first_cell(reader)["kind"], "errorValue")

    def test_absent_cell_is_reported_as_empty_not_as_a_string(self):
        # KIND_EMPTY, not None: an absent cell is a distinct, named branch of the
        # oneof, and None would be indistinguishable from "the reader did not
        # decide".
        from odoo.addons.gdrive_odoo_sync.services.sheets_reader import KIND_EMPTY

        reader, _ = self._reader_with([{}])
        cell = self._first_cell(reader)
        self.assertEqual(cell["kind"], KIND_EMPTY)
        self.assertIsNone(cell["value"])


class TestErrorPropagation(BaseCase):
    """Nothing is swallowed: an unreadable tab must not look like an empty tab."""

    def test_http_error_is_raised(self):
        sheets = FakeSheets()
        sheets.queue("values", "batchGet", FakeHttpError(403, "insufficientPermissions"))
        with self.assertRaises(Exception):
            SheetsReader(sheets).read_all("SID1", ["Sheet1"])

    def test_a_bad_range_is_not_retried(self):
        sheets = FakeSheets()
        sheets.queue("values", "batchGet", FakeHttpError(400, "badRequest", "Unable to parse range"))
        with self.assertRaises(Exception):
            SheetsReader(sheets).read_all("SID1", ["Sheet1"])
        self.assertEqual(len(sheets.calls_to("values", "batchGet")), 1)
