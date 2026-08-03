# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. Licensed under LGPL-3.
"""Lane D — staging one spreadsheet tab (SPEC §3.7, §5.4).

WHY the quarantine rules are absolute rather than best-effort
============================================================
Three rules here look pessimistic and are not:

* **Any ``e:`` token quarantines the whole row.** A half-written row is worse
  than an unwritten one: it looks successful, it passes every count check, and
  the fields that failed silently keep their previous Odoo values, so the drift
  report shows nothing.
* **A duplicated identity quarantines the entire group**, never "the first one".
  Picking one arbitrarily makes the promoted record alternate between the two
  rows' values on every run, producing drift that never converges and that no
  amount of reading the report explains.
* **The missing-row clock only starts on a complete read.** A partial
  ``batchGet``, an expired token, a hidden filter view and a range that stopped
  at row 1000 all look exactly like "these rows were deleted".

The header gate gets its own class because it guards the single most
destructive failure mode in sheet sync: reading an absent mapped column as a
column of empty cells writes NULL over an entire Odoo column, quietly, in one
run, with a green dashboard.
"""

from odoo.tests.common import TransactionCase

from odoo.addons.gdrive_odoo_sync.lib.contract import ColumnContract
from odoo.addons.gdrive_odoo_sync.lib.hashing import h_header

GSHEET = "application/vnd.google-apps.spreadsheet"


def _contracts(columns, *, natural_key_keys=(), sync_id_index=None,
               identity_strategy="sync_id_then_key", extra=(), spec_version="SPECV1"):
    """Build the plain contract bundle ``_stage_dataset_rows`` consumes.

    Assembled by hand here rather than through the mapping model so that the
    staging behaviour can be exercised without a validated mapping — which is
    exactly the common case, since staging is never opt-in.
    """
    return {
        "spec_version": spec_version,
        "contract": list(columns),
        "extra": list(extra),
        "natural_key_keys": list(natural_key_keys),
        "sync_id_index": sync_id_index,
        "identity_strategy": identity_strategy,
    }


class StagingCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.connection = cls.env["gdrive.connection"].create({
            "name": "Test — lucaso@",
            "subject_email": "lucaso@avatarnaturalfoods.com",
        })
        cls.node = cls.env["gdrive.node"].create({
            "connection_id": cls.connection.id,
            "google_id": "1abcDEF",
            "name": "Food CPG Master",
            "mime_type": GSHEET,
            "node_type": "spreadsheet",
        })

    def _dataset(self, **kw):
        vals = {
            "node_id": self.node.id,
            "source_kind": "gsheet",
            "sheet_gid": kw.pop("sheet_gid", 0),
            "tab_title": kw.pop("tab_title", "Investor Directory"),
            "header_row": 1,
            "first_data_row": 2,
        }
        vals.update(kw)
        return self.env["gdrive.dataset"].create(vals)

    def _columns(self, dataset, headers):
        """Upsert the observed header schema, matched on canonical header."""
        return self.env["gdrive.dataset.column"]._upsert_headers(
            dataset, headers, [])

    def _stage(self, dataset, columns, rows, contracts, run=None):
        return self.env["gdrive.staged.row"]._stage_dataset_rows(
            dataset, columns, rows, contracts, run)

    def _rows_of(self, dataset):
        return self.env["gdrive.staged.row"].search(
            [("dataset_id", "=", dataset.id)], order="row_number")


class TestHeaderSchema(StagingCase):
    """Columns resolve by canonical header, never by position."""

    def test_columns_are_created_from_the_header_row(self):
        dataset = self._dataset()
        columns = self._columns(dataset, ["Invoice Number", "Amount", "Due Date"])
        self.assertEqual(len(columns), 3)
        self.assertEqual([c.col_index for c in columns], [0, 1, 2])

    def test_slugs_are_jcs_safe_identifiers(self):
        import re
        dataset = self._dataset()
        columns = self._columns(dataset, ["Invoice Number", "Amount (USD)", "% Margin"])
        for column in columns:
            with self.subTest(header=column.header_raw):
                self.assertRegex(column.slug, r"^[a-z_][a-z0-9_]*$")
        self.assertEqual(len({c.slug for c in columns}), 3)

    def test_duplicate_headers_get_deduplicated_slugs(self):
        dataset = self._dataset()
        columns = self._columns(dataset, ["Amount", "Amount", "Amount"])
        slugs = [c.slug for c in columns]
        self.assertEqual(len(set(slugs)), 3, slugs)

    def test_reordering_columns_does_not_change_the_header_fingerprint(self):
        # Reordering is a genuine no-op by construction: columns resolve by
        # header_canon and rows hash by odoo_field.
        a = h_header(["s:Amount", "s:Due Date", "s:Invoice Number"])
        b = h_header(["s:Invoice Number", "s:Amount", "s:Due Date"])
        self.assertEqual(a, b)

    def test_reordering_updates_col_index_but_keeps_the_column_record(self):
        dataset = self._dataset()
        first = self._columns(dataset, ["Invoice Number", "Amount"])
        ids_before = {c.header_canon: c.id for c in first}
        second = self._columns(dataset, ["Amount", "Invoice Number"])
        ids_after = {c.header_canon: c.id for c in second}
        self.assertEqual(ids_before, ids_after)
        by_canon = {c.header_canon: c.col_index for c in second}
        self.assertEqual(by_canon[first[0].header_canon], 1)

    def test_inserting_a_column_left_of_a_blank_header_keeps_its_record(self):
        # The blank surrogate used to be the A1 letter ('s:column_C'), so
        # inserting any column to its left renamed it to 's:column_D': no
        # existing record matched, a new one was created with a new slug, the
        # old one was marked absent, and every row's h_extra changed with no
        # data change whatsoever.
        dataset = self._dataset()
        before = self._columns(dataset, ["Invoice Number", "", "Amount"])
        blank_id = before[1].id
        after = self._columns(dataset, ["Ref", "Invoice Number", "", "Amount"])
        self.assertEqual(after[2].id, blank_id)
        self.assertEqual(after[2].col_index, 2)

    def test_inserting_a_column_left_of_duplicate_headers_keeps_their_records(self):
        # Same defect for duplicates: 's:Amount (D)' renamed itself to
        # 's:Amount (E)' on any insertion to its left.
        dataset = self._dataset()
        before = self._columns(dataset, ["Amount", "Note", "Amount"])
        ids_before = [c.id for c in before]
        after = self._columns(dataset, ["Ref", "Amount", "Note", "Amount"])
        self.assertEqual([c.id for c in after][1:], ids_before)
        self.assertEqual(len({c.header_canon for c in after}), 4)

    def test_observed_kind_is_advisory_only(self):
        # It is recorded for the mapping builder UI and must never be consulted
        # by the canonicalizer: per-cell inference would canonicalize the same
        # raw value differently between runs.
        dataset = self._dataset()
        columns = self.env["gdrive.dataset.column"]._upsert_headers(
            dataset, ["Amount"], [[1], [2], [3]])
        self.assertTrue(hasattr(columns[0], "observed_kind"))


class TestRowStaging(StagingCase):
    """Every row of every tab lands here, mapping or no mapping."""

    def setUp(self):
        super().setUp()
        self.dataset = self._dataset()
        self.columns = self._columns(self.dataset, ["Invoice Number", "Amount"])
        self.contract = _contracts([
            ("invoice_no", ColumnContract(key="invoice_no", ctype="text"), 0),
            ("amount", ColumnContract(key="amount", ctype="number", scale=2), 1),
        ])

    def test_rows_are_persisted_with_their_payload_and_canon(self):
        self._stage(self.dataset, self.columns, [["INV-1", 100.5], ["INV-2", 200]],
                    self.contract)
        rows = self._rows_of(self.dataset)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].canon["amount"], "n:100.50")
        self.assertEqual(rows[1].canon["invoice_no"], "s:INV-2")

    def test_payload_keeps_every_column_mapped_or_not(self):
        self._stage(self.dataset, self.columns, [["INV-1", 100.5]], self.contract)
        payload = self._rows_of(self.dataset)[0].payload
        self.assertEqual(set(payload), {c.slug for c in self.columns})

    def test_row_number_is_display_only_and_starts_at_first_data_row(self):
        self._stage(self.dataset, self.columns, [["INV-1", 1], ["INV-2", 2]], self.contract)
        self.assertEqual([r.row_number for r in self._rows_of(self.dataset)], [2, 3])

    def test_a1_ref_lets_a_human_click_through(self):
        self._stage(self.dataset, self.columns, [["INV-1", 1]], self.contract)
        self.assertIn("!A2", self._rows_of(self.dataset)[0].a1_ref)

    def test_a1_ref_doubles_apostrophes_in_the_tab_title(self):
        dataset = self._dataset(sheet_gid=7, tab_title="Bob's Data")
        columns = self._columns(dataset, ["Invoice Number", "Amount"])
        self._stage(dataset, columns, [["INV-1", 1]], self.contract)
        self.assertIn("'Bob''s Data'", self._rows_of(dataset)[0].a1_ref)

    def test_ragged_row_shorter_than_the_header_is_null_not_an_error(self):
        self._stage(self.dataset, self.columns, [["INV-1"]], self.contract)
        row = self._rows_of(self.dataset)[0]
        self.assertEqual(row.canon["amount"], "z:")
        self.assertEqual(row.state, "staged")

    def test_hashes_and_bucket_are_denormalized_into_real_columns(self):
        # payload/canon are fields.Json and are unsearchable and ungroupable, so
        # everything the UI filters on has to be a real column.
        self._stage(self.dataset, self.columns, [["INV-1", 100.5]], self.contract)
        row = self._rows_of(self.dataset)[0]
        self.assertEqual(len(row.h_row), 32)
        self.assertEqual(len(row.h_row_folded), 32)
        self.assertEqual(len(row.h_extra), 32)
        self.assertGreaterEqual(row.bucket, 0)
        self.assertLess(row.bucket, 256)

    def test_restaging_identical_data_creates_no_duplicates(self):
        rows = [["INV-1", 100.5], ["INV-2", 200]]
        self._stage(self.dataset, self.columns, rows, self.contract)
        self._stage(self.dataset, self.columns, rows, self.contract)
        self.assertEqual(len(self._rows_of(self.dataset)), 2)

    def test_sorting_the_sheet_does_not_change_the_row_hashes(self):
        rows = [["INV-1", 100.5], ["INV-2", 200]]
        self._stage(self.dataset, self.columns, rows, self.contract)
        before = sorted(r.h_row for r in self._rows_of(self.dataset))
        self._stage(self.dataset, self.columns, list(reversed(rows)), self.contract)
        after = sorted(r.h_row for r in self._rows_of(self.dataset))
        self.assertEqual(before, after)

    def test_buckets_are_returned_for_the_merkle_rollup(self):
        result = self._stage(self.dataset, self.columns,
                             [["INV-1", 1], ["INV-2", 2]], self.contract)
        self.assertEqual(result["row_count"], 2)
        flattened = [e for entries in result["buckets"].values() for e in entries]
        self.assertEqual(len(flattened), 2)

    def test_a_quarantined_row_is_committed_to_the_rollup_not_dropped(self):
        # Dropping quarantined rows made "3 rows, one unreadable" and "2 rows,
        # the third deleted from the sheet" produce byte-identical dataset
        # digests: the same identity keys, the same row hashes, and total_rows
        # derived from the surviving entries. The Merkle fast path then found
        # zero differing buckets and reported `verified` while a row had
        # physically vanished from the sheet and its Odoo record had become a
        # candidate for the delete planner.
        from odoo.addons.gdrive_odoo_sync.lib.merkle import dataset_digest

        keyed = _contracts(
            [("invoice_no", ColumnContract(key="invoice_no", ctype="text",
                                           is_natural_key=True), 0),
             ("amount", ColumnContract(key="amount", ctype="number", scale=2), 1)],
            natural_key_keys=("invoice_no",), identity_strategy="natural_key",
        )
        three = self._stage(self.dataset, self.columns,
                            [["INV-1", 1], ["INV-2", 2], ["INV-3", "not a number"]], keyed)
        two = self._stage(self.dataset, self.columns,
                          [["INV-1", 1], ["INV-2", 2]], keyed)

        flat_three = [e for entries in three["buckets"].values() for e in entries]
        flat_two = [e for entries in two["buckets"].values() for e in entries]
        self.assertEqual(len(flat_three), 3, "the quarantined row must still be committed to")
        self.assertEqual(len(flat_two), 2)
        self.assertNotEqual(dataset_digest(flat_three, "SPECV1", "1abcDEF/0")[0],
                            dataset_digest(flat_two, "SPECV1", "1abcDEF/0")[0])


class TestErrorQuarantine(StagingCase):
    """Any ``e:`` token quarantines the whole row — never a partial write."""

    def setUp(self):
        super().setUp()
        self.dataset = self._dataset()
        self.columns = self._columns(self.dataset, ["Invoice Number", "Amount", "Active"])
        self.contract = _contracts([
            ("invoice_no", ColumnContract(key="invoice_no", ctype="text"), 0),
            ("amount", ColumnContract(key="amount", ctype="number", scale=2), 1),
            ("active", ColumnContract(key="active", ctype="bool"), 2),
        ])

    def test_unparseable_number_quarantines_the_row(self):
        self._stage(self.dataset, self.columns, [["INV-1", "not a number", "yes"]],
                    self.contract)
        row = self._rows_of(self.dataset)[0]
        self.assertEqual(row.state, "quarantined")
        self.assertEqual(row.quarantine_reason, "not_a_number")

    def test_unknown_boolean_quarantines_rather_than_defaulting_to_false(self):
        self._stage(self.dataset, self.columns, [["INV-1", 1, "pending"]], self.contract)
        row = self._rows_of(self.dataset)[0]
        self.assertEqual(row.state, "quarantined")
        self.assertEqual(row.quarantine_reason, "bad_bool")

    def test_the_good_columns_are_still_recorded_for_diagnosis(self):
        # The row is not promoted, but the operator must be able to see what was
        # read and what failed.
        self._stage(self.dataset, self.columns, [["INV-1", "oops", "yes"]], self.contract)
        row = self._rows_of(self.dataset)[0]
        self.assertEqual(row.canon["invoice_no"], "s:INV-1")
        self.assertTrue(row.canon["amount"].startswith("e:"))

    def test_the_quarantine_detail_names_the_column_and_the_raw_value(self):
        self._stage(self.dataset, self.columns, [["INV-1", "oops", "yes"]], self.contract)
        detail = self._rows_of(self.dataset)[0].quarantine_detail
        self.assertIn("amount", detail)
        self.assertIn("oops", detail)

    def test_missing_required_value_quarantines(self):
        contract = _contracts([
            ("invoice_no", ColumnContract(key="invoice_no", ctype="text", required=True), 0),
            ("amount", ColumnContract(key="amount", ctype="number", scale=2), 1),
            ("active", ColumnContract(key="active", ctype="bool"), 2),
        ])
        self._stage(self.dataset, self.columns, [["", 100, "yes"]], contract)
        row = self._rows_of(self.dataset)[0]
        self.assertEqual(row.state, "quarantined")
        self.assertEqual(row.quarantine_reason, "missing_required")

    def test_a_clean_row_alongside_a_bad_one_still_stages(self):
        self._stage(self.dataset, self.columns,
                    [["INV-1", "oops", "yes"], ["INV-2", 200, "no"]], self.contract)
        rows = self._rows_of(self.dataset)
        self.assertEqual(rows[0].state, "quarantined")
        self.assertEqual(rows[1].state, "staged")


class TestDuplicateIdentity(StagingCase):
    """The entire key group is quarantined, never "the first one"."""

    def setUp(self):
        super().setUp()
        self.dataset = self._dataset()
        self.columns = self._columns(self.dataset, ["_sync_id", "Name"])
        self.contract = _contracts(
            [("name", ColumnContract(key="name", ctype="text"), 1)],
            sync_id_index=0,
            identity_strategy="sync_id",
        )

    def test_both_members_are_quarantined(self):
        self._stage(self.dataset, self.columns, [
            ["01JBX3T7QK9V2M4N6P8R0S1T2U", "ACME"],
            ["01JBX3T7QK9V2M4N6P8R0S1T2U", "ACME Foods"],
        ], self.contract)
        rows = self._rows_of(self.dataset)
        self.assertEqual([r.state for r in rows], ["quarantined", "quarantined"])
        self.assertEqual([r.quarantine_reason for r in rows],
                         ["duplicate_identity", "duplicate_identity"])

    def test_the_detail_cites_both_cell_references(self):
        self._stage(self.dataset, self.columns, [
            ["01JBX3T7QK9V2M4N6P8R0S1T2U", "ACME"],
            ["01JBX3T7QK9V2M4N6P8R0S1T2U", "ACME Foods"],
        ], self.contract)
        detail = self._rows_of(self.dataset)[0].quarantine_detail
        self.assertIn("!A2", detail)
        self.assertIn("!A3", detail)

    def test_distinct_identities_are_untouched(self):
        self._stage(self.dataset, self.columns, [
            ["01JBX3T7QK9V2M4N6P8R0S1T2U", "ACME"],
            ["01JBX3T7QK9V2M4N6P8R0S1T2V", "Bettr Bowl"],
        ], self.contract)
        self.assertEqual([r.state for r in self._rows_of(self.dataset)], ["staged", "staged"])

    def test_natural_key_duplicates_are_caught_too(self):
        dataset = self._dataset(sheet_gid=9, tab_title="By Key")
        columns = self._columns(dataset, ["Invoice Number", "Amount"])
        contract = _contracts(
            [("invoice_no", ColumnContract(key="invoice_no", ctype="text"), 0),
             ("amount", ColumnContract(key="amount", ctype="number", scale=2), 1)],
            natural_key_keys=["invoice_no"],
            identity_strategy="natural_key",
        )
        self._stage(dataset, columns, [["INV-1", 100], ["INV-1", 200]], contract)
        self.assertEqual([r.quarantine_reason for r in self._rows_of(dataset)],
                         ["duplicate_identity", "duplicate_identity"])

    def test_cross_strategy_collision_is_caught(self):
        # Under 'sync_id_then_key', one row carrying a _sync_id and one without
        # can hold identical natural-key values. Grouping on
        # (identity_source, sync_id or natural_key) put them in ('sync_id','…')
        # and ('natural_key','…'), saw one member each and quarantined neither —
        # and then, at match time, both resolved to the same stored record: the
        # second row's payload overwrote the first's, and the record the second
        # row should have matched fell into `vanished` with its
        # delete-quarantine clock started while it was still in the sheet.
        dataset = self._dataset(sheet_gid=11, tab_title="Mixed Identity")
        columns = self._columns(dataset, ["_sync_id", "Invoice Number"])
        contract = _contracts(
            [("invoice_no", ColumnContract(key="invoice_no", ctype="text"), 1)],
            sync_id_index=0, natural_key_keys=["invoice_no"],
            identity_strategy="sync_id_then_key",
        )
        rows = [["01JBX3T7QK9V2M4N6P8R0S1T2U", "INV-1"], ["", "INV-1"]]
        self._stage(dataset, columns, rows, contract)
        self.assertEqual([r.quarantine_reason for r in self._rows_of(dataset)],
                         ["duplicate_identity", "duplicate_identity"])

        # And on a second identical read nothing is written 'missing': both
        # stored rows are still there and both are still claimed.
        run = self.env["gdrive.sync.run"]._start(self.connection, trigger="manual")
        self._stage(dataset, columns, rows, contract, run)
        stored = self._rows_of(dataset)
        self.assertEqual(len(stored), 2)
        self.assertFalse(any(r.missing_since for r in stored))


class TestSyncIdNormalization(StagingCase):
    """A sync_id is an identity, so it gets the same invisible-character strip."""

    def setUp(self):
        super().setUp()
        self.dataset = self._dataset(sheet_gid=13, tab_title="Pasted Ids")
        self.columns = self._columns(self.dataset, ["_sync_id", "Name"])
        self.contract = _contracts(
            [("name", ColumnContract(key="name", ctype="text"), 1)],
            sync_id_index=0, identity_strategy="sync_id",
        )

    # Written numerically, per the convention in lib/text_canon.py: a literal
    # soft hyphen in a source file is invisible in every review that would have
    # caught its loss.
    ULID = "01JBX3T7QK9V2M4N6P8R0S1T2U"
    INVISIBLES = (0x200B, 0xFEFF, 0x00AD, 0x200D)

    def test_a_zero_width_space_does_not_split_one_id_into_two(self):
        # str().strip() removes ASCII whitespace and NBSP but leaves U+200B,
        # U+FEFF, U+200D and U+00AD - exactly what a browser copy-paste injects.
        # One of them produced different identity_key_bytes, a different bucket,
        # no match in by_sync, a brand-new staged row, and the previous row
        # written state='missing' with the delete-quarantine clock started on a
        # record that never left the sheet.
        run = self.env["gdrive.sync.run"]._start(self.connection, trigger="manual")
        self._stage(self.dataset, self.columns, [[self.ULID, "ACME"]], self.contract, run)
        self._stage(self.dataset, self.columns,
                    [[self.ULID + chr(0x200B), "ACME"]], self.contract, run)
        stored = self._rows_of(self.dataset)
        self.assertEqual(len(stored), 1, "the pasted id must resolve to the same row")
        self.assertEqual(stored[0].sync_id, self.ULID)
        self.assertFalse(stored[0].missing_since)

    def test_every_invisible_leaves_the_identity_unchanged(self):
        for codepoint in self.INVISIBLES:
            with self.subTest(codepoint=hex(codepoint)):
                dataset = self._dataset(sheet_gid=codepoint,
                                        tab_title="Pasted %d" % codepoint)
                columns = self._columns(dataset, ["_sync_id", "Name"])
                self._stage(dataset, columns, [[self.ULID, "ACME"]], self.contract)
                self._stage(dataset, columns,
                            [[chr(codepoint) + self.ULID, "ACME"]], self.contract)
                self.assertEqual(len(self._rows_of(dataset)), 1)
                self.assertEqual(self._rows_of(dataset)[0].sync_id, self.ULID)


class TestIdentitySource(StagingCase):
    """SPEC §5.5 — the identity cascade, and what happens when it finds nothing."""

    def test_sync_id_wins_when_present(self):
        dataset = self._dataset()
        columns = self._columns(dataset, ["_sync_id", "Invoice Number"])
        contract = _contracts(
            [("invoice_no", ColumnContract(key="invoice_no", ctype="text"), 1)],
            sync_id_index=0, natural_key_keys=["invoice_no"],
            identity_strategy="sync_id_then_key",
        )
        self._stage(dataset, columns, [["01JBX3T7QK9V2M4N6P8R0S1T2U", "INV-1"]], contract)
        row = self._rows_of(dataset)[0]
        self.assertEqual(row.identity_source, "sync_id")
        self.assertEqual(row.sync_id, "01JBX3T7QK9V2M4N6P8R0S1T2U")

    def test_natural_key_is_used_when_no_sync_id_column_exists(self):
        dataset = self._dataset(sheet_gid=2)
        columns = self._columns(dataset, ["Invoice Number"])
        contract = _contracts(
            [("invoice_no", ColumnContract(key="invoice_no", ctype="text"), 0)],
            natural_key_keys=["invoice_no"], identity_strategy="sync_id_then_key",
        )
        self._stage(dataset, columns, [["INV-1"]], contract)
        row = self._rows_of(dataset)[0]
        self.assertEqual(row.identity_source, "natural_key")
        self.assertTrue(row.natural_key)

    def test_no_declared_identity_is_report_only(self):
        # Such rows can never reach the delete planner; the bucket key falls back
        # to the row hash purely so the content rollup stays order-insensitive.
        dataset = self._dataset(sheet_gid=3)
        columns = self._columns(dataset, ["Note"])
        contract = _contracts([("note", ColumnContract(key="note", ctype="text"), 0)])
        self._stage(dataset, columns, [["anything"]], contract)
        row = self._rows_of(dataset)[0]
        self.assertEqual(row.identity_source, "none")
        self.assertFalse(row.sync_id)
        self.assertFalse(row.natural_key)

    def test_a_key_column_error_suppresses_the_natural_key(self):
        # An identity computed from an uncomparable value is not an identity.
        dataset = self._dataset(sheet_gid=4)
        columns = self._columns(dataset, ["Amount"])
        contract = _contracts(
            [("amount", ColumnContract(key="amount", ctype="number", scale=2), 0)],
            natural_key_keys=["amount"], identity_strategy="natural_key",
        )
        self._stage(dataset, columns, [["not a number"]], contract)
        row = self._rows_of(dataset)[0]
        self.assertFalse(row.natural_key)
        self.assertEqual(row.identity_source, "none")


class TestMissingRowClock(StagingCase):
    """The delete quarantine clock only starts on a proven-complete read."""

    def setUp(self):
        super().setUp()
        self.dataset = self._dataset()
        self.columns = self._columns(self.dataset, ["_sync_id", "Name"])
        self.contract = _contracts(
            [("name", ColumnContract(key="name", ctype="text"), 1)],
            sync_id_index=0, identity_strategy="sync_id",
        )
        self.rows = [["01AAA", "ACME"], ["01BBB", "Bettr Bowl"]]

    def _run(self, complete):
        run = self.env["gdrive.sync.run"]._start(self.connection, trigger="manual")
        run.sudo().write({"complete_read": complete})
        return run

    def test_vanished_row_is_marked_missing_after_a_complete_read(self):
        self._stage(self.dataset, self.columns, self.rows, self.contract, self._run(True))
        self._stage(self.dataset, self.columns, self.rows[:1], self.contract, self._run(True))
        gone = self._rows_of(self.dataset).filtered(lambda r: r.sync_id == "01BBB")
        self.assertEqual(gone.state, "missing")
        self.assertTrue(gone.missing_since)

    def test_vanished_row_is_left_alone_after_an_incomplete_read(self):
        self._stage(self.dataset, self.columns, self.rows, self.contract, self._run(True))
        self._stage(self.dataset, self.columns, self.rows[:1], self.contract, self._run(False))
        gone = self._rows_of(self.dataset).filtered(lambda r: r.sync_id == "01BBB")
        self.assertNotEqual(gone.state, "missing")
        self.assertFalse(gone.missing_since)

    def test_a_reappearing_row_clears_the_clock(self):
        self._stage(self.dataset, self.columns, self.rows, self.contract, self._run(True))
        self._stage(self.dataset, self.columns, self.rows[:1], self.contract, self._run(True))
        self._stage(self.dataset, self.columns, self.rows, self.contract, self._run(True))
        back = self._rows_of(self.dataset).filtered(lambda r: r.sync_id == "01BBB")
        self.assertEqual(back.state, "staged")
        self.assertFalse(back.missing_since)

    def test_nothing_is_unlinked(self):
        self._stage(self.dataset, self.columns, self.rows, self.contract, self._run(True))
        self._stage(self.dataset, self.columns, [], self.contract, self._run(True))
        self.assertEqual(len(self._rows_of(self.dataset)), 2)


class TestSchemaGrowth(StagingCase):
    """An unmapped new column is non-blocking and lands in ``h_extra``."""

    def test_extra_column_changes_h_extra_but_not_h_row(self):
        dataset = self._dataset()
        columns = self._columns(dataset, ["Invoice Number", "Amount"])
        base = _contracts([
            ("invoice_no", ColumnContract(key="invoice_no", ctype="text"), 0),
            ("amount", ColumnContract(key="amount", ctype="number", scale=2), 1),
        ])
        self._stage(dataset, columns, [["INV-1", 100]], base)
        before = self._rows_of(dataset)[0]
        h_row_before, h_extra_before = before.h_row, before.h_extra

        grown_columns = self._columns(dataset, ["Invoice Number", "Amount", "Notes"])
        grown = _contracts(
            [("invoice_no", ColumnContract(key="invoice_no", ctype="text"), 0),
             ("amount", ColumnContract(key="amount", ctype="number", scale=2), 1)],
            extra=[("notes", ColumnContract(key="notes", ctype="text"), 2)],
        )
        self._stage(dataset, grown_columns, [["INV-1", 100, "call back"]], grown)
        after = self._rows_of(dataset)[0]

        self.assertEqual(after.h_row, h_row_before, "an unmapped column must not move the row hash")
        self.assertNotEqual(after.h_extra, h_extra_before, "schema growth must be visible")

    def test_the_new_column_is_archived_in_the_payload(self):
        dataset = self._dataset(sheet_gid=11)
        columns = self._columns(dataset, ["Invoice Number", "Notes"])
        contract = _contracts(
            [("invoice_no", ColumnContract(key="invoice_no", ctype="text"), 0)],
            extra=[("notes", ColumnContract(key="notes", ctype="text"), 1)],
        )
        self._stage(dataset, columns, [["INV-1", "call back"]], contract)
        payload = self._rows_of(dataset)[0].payload
        self.assertIn("call back", list(payload.values()))


class TestJsonFieldDiscipline(StagingCase):
    """``fields.Json`` returns a deep copy on read; in-place mutation is a no-op."""

    def test_in_place_mutation_of_payload_does_not_persist(self):
        dataset = self._dataset()
        columns = self._columns(dataset, ["Invoice Number"])
        contract = _contracts([("invoice_no", ColumnContract(key="invoice_no", ctype="text"), 0)])
        self._stage(dataset, columns, [["INV-1"]], contract)
        row = self._rows_of(dataset)[0]

        row.payload["injected"] = "value"
        row.invalidate_recordset(["payload"])
        self.assertNotIn("injected", row.payload)

    def test_whole_dict_reassignment_persists(self):
        dataset = self._dataset(sheet_gid=12)
        columns = self._columns(dataset, ["Invoice Number"])
        contract = _contracts([("invoice_no", ColumnContract(key="invoice_no", ctype="text"), 0)])
        self._stage(dataset, columns, [["INV-1"]], contract)
        row = self._rows_of(dataset)[0]

        row.payload = {**row.payload, "injected": "value"}
        row.invalidate_recordset(["payload"])
        self.assertEqual(row.payload["injected"], "value")

    def test_the_pretty_printed_mirrors_render_something_readable(self):
        dataset = self._dataset(sheet_gid=13)
        columns = self._columns(dataset, ["Invoice Number"])
        contract = _contracts([("invoice_no", ColumnContract(key="invoice_no", ctype="text"), 0)])
        self._stage(dataset, columns, [["INV-1"]], contract)
        row = self._rows_of(dataset)[0]
        self.assertIn("INV-1", row.payload_pretty)
        self.assertIn("s:INV-1", row.canon_pretty)
