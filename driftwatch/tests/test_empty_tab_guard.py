# -*- coding: utf-8 -*-
"""The empty-tab guard (SPEC 9.6).

The guard exists to stop zero staged rows being read as "everything was
deleted". Two things are being pinned here, and they pull in opposite
directions:

* the **refusal never varies** -- zero rows are never evidence of deletion,
  whether or not the tab ever had any;
* the **report does** -- a tab that has always been empty is a fact, and a tab
  that just lost twenty thousand rows is an emergency. Reporting them
  identically is how the emergency gets buried.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from driftwatch.store import Store
from driftwatch.verifier import _empty_tab_finding, _prev_rows


def dataset(**kw) -> dict:
    base = {'id': 12, 'tab_title': 'Sheet2', 'prev_row_count': 0}
    base.update(kw)
    return base


class FindingTests(unittest.TestCase):

    def test_a_tab_that_never_had_rows_is_information(self):
        drift_type, severity, detail = _empty_tab_finding(dataset())
        self.assertEqual(drift_type, 'empty_tab')
        self.assertEqual(severity, 'info')
        self.assertIn('nothing was lost', detail)

    def test_a_tab_that_lost_its_rows_is_an_error(self):
        drift_type, severity, detail = _empty_tab_finding(
            dataset(prev_row_count=19845))
        self.assertEqual(drift_type, 'empty_tab')
        self.assertEqual(severity, 'error')
        self.assertIn('19845', detail)
        self.assertIn('mass-delete', detail)

    def test_one_lost_row_is_still_an_error(self):
        """The guard is about direction, not volume."""
        _, severity, _ = _empty_tab_finding(dataset(prev_row_count=1))
        self.assertEqual(severity, 'error')

    def test_a_row_from_before_the_migration_does_not_crash(self):
        _, severity, _ = _empty_tab_finding({'id': 1, 'tab_title': 'x'})
        self.assertEqual(severity, 'info')

    def test_prev_rows_tolerates_junk(self):
        self.assertEqual(_prev_rows({'prev_row_count': None}), 0)
        self.assertEqual(_prev_rows({'prev_row_count': 'nope'}), 0)
        self.assertEqual(_prev_rows({'prev_row_count': '42'}), 42)


class BaselineTests(unittest.TestCase):
    """How ``prev_row_count`` is maintained across reads."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.store = Store(self.tmp / 'd.sqlite3')
        self.store.conn.execute(
            'INSERT INTO node (file_id, name, mime_type, first_seen, last_seen)'
            " VALUES ('f1','Book','sheet','t','t')")
        self.store.conn.commit()

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def upsert(self, rows: int, complete: bool = True) -> int:
        return self.store.upsert_dataset(
            'f1', 't1', 'Tab', [], 'now', row_count=rows,
            read_complete=complete)

    def current(self) -> sqlite3.Row:
        return self.store.conn.execute(
            'SELECT row_count, prev_row_count, read_complete FROM dataset '
            "WHERE file_id='f1' AND tab_id='t1'").fetchone()

    def test_a_brand_new_tab_has_no_baseline(self):
        self.upsert(500)
        row = self.current()
        self.assertEqual(row['row_count'], 500)
        self.assertEqual(row['prev_row_count'], 0)

    def test_the_previous_complete_count_becomes_the_baseline(self):
        self.upsert(500)
        self.upsert(400)
        row = self.current()
        self.assertEqual(row['row_count'], 400)
        self.assertEqual(row['prev_row_count'], 500)

    def test_a_drop_to_zero_is_visible_to_the_guard(self):
        self.upsert(19845)
        self.upsert(0)
        _, severity, detail = _empty_tab_finding(self.current_dataset())
        self.assertEqual(severity, 'error')
        self.assertIn('19845', detail)

    def test_an_incomplete_read_does_not_erase_the_baseline(self):
        """A truncated read writing 0 here would mask the next real deletion --
        the exact failure the guard exists to catch."""
        self.upsert(19845)
        self.upsert(0, complete=False)      # truncated read
        self.assertEqual(self.current()['prev_row_count'], 19845)
        self.upsert(0)                      # a complete read, still empty
        self.assertEqual(self.current()['prev_row_count'], 19845)
        _, severity, _ = _empty_tab_finding(self.current_dataset())
        self.assertEqual(severity, 'error')

    def test_an_always_empty_tab_stays_quiet_across_many_reads(self):
        for _ in range(5):
            self.upsert(0)
        self.assertEqual(self.current()['prev_row_count'], 0)
        _, severity, _ = _empty_tab_finding(self.current_dataset())
        self.assertEqual(severity, 'info')

    def current_dataset(self) -> dict:
        row = self.store.conn.execute(
            "SELECT * FROM dataset WHERE file_id='f1' AND tab_id='t1'").fetchone()
        return dict(row)


class MigrationTests(unittest.TestCase):

    def test_an_older_store_gains_the_column_and_a_seeded_baseline(self):
        """Leaving every baseline at zero would blind the guard for exactly
        one cycle -- the one right after the upgrade."""
        # ignore_cleanup_errors: Windows keeps a handle on the SQLite file
        # briefly after close, and a temp-dir teardown race is not the thing
        # under test here.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / 'old.sqlite3'
            legacy = sqlite3.connect(str(path))
            legacy.executescript("""
                -- parent_id included because the live schema indexes it;
                -- only `dataset` is meant to be old-shaped here.
                CREATE TABLE node (file_id TEXT PRIMARY KEY, name TEXT,
                    mime_type TEXT, parent_id TEXT, first_seen TEXT,
                    last_seen TEXT, missing_since TEXT);
                CREATE TABLE dataset (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id TEXT NOT NULL, tab_id TEXT NOT NULL,
                    tab_title TEXT NOT NULL, header_json TEXT DEFAULT '[]',
                    row_count INTEGER NOT NULL DEFAULT 0,
                    col_count INTEGER NOT NULL DEFAULT 0,
                    spec_version TEXT, h_dataset TEXT, bucket_hashes TEXT,
                    read_complete INTEGER NOT NULL DEFAULT 1,
                    blocked_reason TEXT, updated_at TEXT NOT NULL,
                    UNIQUE(file_id, tab_id));
                INSERT INTO dataset (file_id, tab_id, tab_title, row_count,
                    read_complete, updated_at) VALUES ('f','a','Full',900,1,'t');
                INSERT INTO dataset (file_id, tab_id, tab_title, row_count,
                    read_complete, updated_at) VALUES ('f','b','Torn',700,0,'t');
            """)
            legacy.commit()
            legacy.close()

            store = Store(path)               # runs the migration
            try:
                rows = {r['tab_title']: r['prev_row_count'] for r in
                        store.conn.execute('SELECT tab_title, prev_row_count '
                                           'FROM dataset')}
                self.assertEqual(rows['Full'], 900)
                # An incomplete read's count is not a trustworthy baseline.
                self.assertEqual(rows['Torn'], 0)
                self.assertEqual(store.get_meta('schema_version'), '2')
            finally:
                store.close()

    def test_migration_is_idempotent(self):
        # ignore_cleanup_errors: Windows keeps a handle on the SQLite file
        # briefly after close, and a temp-dir teardown race is not the thing
        # under test here.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / 'd.sqlite3'
            Store(path).close()
            store = Store(path)               # second open must not throw
            try:
                cols = {r['name'] for r in
                        store.conn.execute('PRAGMA table_info(dataset)')}
                self.assertIn('prev_row_count', cols)
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
