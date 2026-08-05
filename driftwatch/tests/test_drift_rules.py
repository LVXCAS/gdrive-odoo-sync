# -*- coding: utf-8 -*-
"""Rules both phases have to agree on, and the surfaces that report them.

Three defects are pinned here, all of the same shape: a rule that existed in
one place and not the other, so the same corpus produced two answers depending
on which phase or which surface you asked.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from driftwatch.drift_rules import (
    EMPTY_TAB_ALWAYS_EMPTY,
    EMPTY_TAB_LOST_ROWS,
    carried_baseline,
    empty_tab_severity,
)
from driftwatch.stager import _is_office_lock_file
from driftwatch.store import Store, cycle_drift_where, cycle_run_ids
from driftwatch.verifier import _empty_tab_finding


def row(**kw) -> dict:
    """A dataset row as sqlite3.Row-style subscripting sees it."""
    base = {'row_count': 0, 'prev_row_count': 0, 'read_complete': 1}
    base.update(kw)
    return base


def seed_node(store: Store, file_id: str = 'f') -> None:
    """`dataset.file_id` is a foreign key -- a dataset needs its Drive node."""
    store.upsert_node({'file_id': file_id, 'name': 'Book.xlsx',
                       'mime_type': 'application/vnd.google-apps.spreadsheet',
                       'parent_id': None, 'drive_version': '1',
                       'modified_time': '2026-01-01', 'size': 0,
                       'owner_email': 'x@example.com', 'is_folder': 0,
                       'trashed': 0, 'shared_drive': None, 'web_link': ''},
                      '2026-01-01')


class EmptyTabSeverityTests(unittest.TestCase):
    """The grading rule itself."""

    def test_a_tab_that_never_had_rows_is_information(self):
        self.assertEqual(empty_tab_severity(0), EMPTY_TAB_ALWAYS_EMPTY)

    def test_a_tab_that_lost_rows_is_an_error(self):
        self.assertEqual(empty_tab_severity(19845), EMPTY_TAB_LOST_ROWS)

    def test_one_lost_row_is_still_an_error(self):
        """Direction, not volume."""
        self.assertEqual(empty_tab_severity(1), EMPTY_TAB_LOST_ROWS)


class BothPhasesAgreeTests(unittest.TestCase):
    """The defect: the stager hardcoded `warning` while the verifier graded.

    A stage phase and a verify phase looking at the same tab in the same cycle
    must not file it under two different severities.
    """

    def test_stage_and_verify_grade_an_always_empty_tab_alike(self):
        staged = empty_tab_severity(carried_baseline(row()))
        _, verified, _ = _empty_tab_finding(
            {'id': 1, 'tab_title': 'Sheet2', 'prev_row_count': 0})
        self.assertEqual(staged, verified)
        self.assertEqual(staged, EMPTY_TAB_ALWAYS_EMPTY)

    def test_stage_and_verify_grade_a_tab_that_lost_rows_alike(self):
        staged = empty_tab_severity(carried_baseline(row(row_count=19845)))
        _, verified, _ = _empty_tab_finding(
            {'id': 1, 'tab_title': 'AR', 'prev_row_count': 19845})
        self.assertEqual(staged, verified)
        self.assertEqual(staged, EMPTY_TAB_LOST_ROWS)

    def test_severity_is_never_the_old_hardcoded_warning(self):
        for baseline in (0, 1, 19845):
            self.assertNotEqual(empty_tab_severity(baseline), 'warning')


class CarriedBaselineTests(unittest.TestCase):
    """`carried_baseline` must mirror the ON CONFLICT arithmetic in the SQL."""

    def test_a_complete_previous_read_becomes_the_baseline(self):
        self.assertEqual(carried_baseline(row(row_count=500)), 500)

    def test_an_incomplete_previous_read_carries_the_older_baseline(self):
        """A truncated read must not erase the number the guard fires on."""
        self.assertEqual(
            carried_baseline(row(row_count=0, prev_row_count=500,
                                 read_complete=0)),
            500)

    def test_no_previous_dataset_is_a_zero_baseline(self):
        self.assertEqual(carried_baseline(None), 0)

    def test_junk_reads_as_zero_rather_than_raising(self):
        self.assertEqual(carried_baseline(row(row_count='not a number')), 0)

    def test_it_agrees_with_the_sql_it_mirrors(self):
        """Pinned against the real upsert, not against its docstring."""
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'x.sqlite3')
            seed_node(store)
            store.upsert_dataset('f', '0', 'Tab', ['a'], '2026-01-01',
                                 row_count=500, col_count=1, spec_version='1',
                                 h_dataset='ab', bucket_hashes=[],
                                 read_complete=True)
            previous = store.datasets(file_id='f')[0]
            expected = carried_baseline(previous)
            store.upsert_dataset('f', '0', 'Tab', ['a'], '2026-01-02',
                                 row_count=0, col_count=1, spec_version=None,
                                 h_dataset=None, bucket_hashes=[],
                                 read_complete=True, blocked_reason='empty_tab')
            landed = store.datasets(file_id='f')[0]['prev_row_count']
            self.assertEqual(expected, landed)
            self.assertEqual(landed, 500)
            store.close()


class OfficeLockFileTests(unittest.TestCase):
    """`~$Book.xlsx` is a lock file Drive mislabels as a spreadsheet."""

    def test_lock_files_are_recognised(self):
        for name in ('~$Neuhauser Statements.xlsx',
                     "~$2022 Yaya's Open Purchase Orders.xlsx"):
            self.assertTrue(_is_office_lock_file(name), name)

    def test_ordinary_workbooks_are_not(self):
        for name in ('Neuhauser Statements.xlsx', 'MASTER FRESH.xlsx',
                     'budget~$draft.xlsx', '', None):
            self.assertFalse(_is_office_lock_file(name), name)


class CycleRunIdsTests(unittest.TestCase):
    """The defect: the digest and the dashboard read only the verify run.

    Staging records drift of its own -- type_coercion above all, which is
    unrecoverable by the time it is detected -- and reporting on the verify
    run alone dropped every one of them.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'cycle.sqlite3')
        seed_node(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _cycle(self, coercions: int, duplicates: int) -> tuple:
        """One crawl/stage/verify triple, shaped like a real one.

        `duplicates` is recorded by *both* phases, as the real stager and
        verifier both do; `coercions` only by the stager, which is the only
        phase that can detect them.
        """
        crawl = self.store.start_run('crawl', '2026-01-01 00:00:00')
        self.store.finish_run(crawl, '2026-01-01 00:01:00', 'done', {})
        stage = self.store.start_run('stage', '2026-01-01 00:01:00')
        dataset_id = self.store.upsert_dataset(
            'f', '0', 'Tab', ['sku'], '2026-01-01', row_count=1, col_count=1,
            spec_version='1', h_dataset='ab', bucket_hashes=[],
            read_complete=True)
        for _ in range(coercions):
            self.store.record_drift(stage, dataset_id, 'type_coercion',
                                    '2026-01-01', severity='warning')
        for _ in range(duplicates):
            # As the stager writes it: the real column, no row reference.
            self.store.record_drift(stage, dataset_id, 'duplicate_identity',
                                    '2026-01-01', severity='warning',
                                    column_key='sku', sheet_value='K1')
        self.store.finish_run(stage, '2026-01-01 00:02:00', 'done', {})
        verify = self.store.start_run('verify', '2026-01-01 00:02:00')
        for _ in range(duplicates):
            # As the verifier writes it: same finding, different columns.
            self.store.record_drift(verify, dataset_id, 'duplicate_identity',
                                    '2026-01-01', severity='warning',
                                    row_ref='A2', column_key='natural_key',
                                    sheet_value='K1')
        self.store.finish_run(verify, '2026-01-01 00:03:00', 'done', {})
        return crawl, stage, verify

    def test_a_cycle_is_its_crawl_stage_and_verify(self):
        crawl, stage, verify = self._cycle(coercions=3, duplicates=1)
        self.assertEqual(self.store.latest_cycle_run_ids(),
                         [crawl, stage, verify])

    def test_stage_only_findings_reach_the_digest(self):
        """The omission this fix exists for: coercions were never reported."""
        self._cycle(coercions=3, duplicates=1)
        self.assertEqual(self.store.cycle_drift_summary().get('type_coercion'), 3)

    def test_a_finding_both_phases_record_is_counted_once(self):
        """The trap on the other side: a plain union doubles every overlap."""
        self._cycle(coercions=0, duplicates=7)
        summary = self.store.cycle_drift_summary()
        self.assertEqual(summary.get('duplicate_identity'), 7)
        self.assertEqual(len(self.store.cycle_drifts()), 7)

    def test_the_verify_copy_is_the_one_kept(self):
        """Verify's row carries the A1 reference; the stager's does not."""
        self._cycle(coercions=0, duplicates=1)
        kept = self.store.cycle_drifts()
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]['row_ref'], 'A2')
        self.assertEqual(kept[0]['column_key'], 'natural_key')

    def test_totals_add_up_across_both_phases(self):
        self._cycle(coercions=3, duplicates=7)
        self.assertEqual(len(self.store.cycle_drifts()), 10)

    def test_a_previous_cycle_is_not_absorbed(self):
        self._cycle(coercions=99, duplicates=0)
        self._cycle(coercions=2, duplicates=0)
        self.assertEqual(self.store.cycle_drift_summary().get('type_coercion'), 2)

    def test_the_dashboard_connection_reaches_the_same_answer(self):
        """Same rule, whether the caller has a Store or a read-only conn."""
        self._cycle(coercions=2, duplicates=3)
        conn = sqlite3.connect(self.store.path)
        try:
            self.assertEqual(cycle_run_ids(conn),
                             self.store.latest_cycle_run_ids())
            where, args = cycle_drift_where(conn)
            total = conn.execute(
                'SELECT COUNT(*) FROM drift WHERE %s' % where, args).fetchone()[0]
            self.assertEqual(total, len(self.store.cycle_drifts()))
            self.assertEqual(total, 5)
        finally:
            conn.close()

    def test_no_verify_run_yet_is_an_empty_cycle(self):
        run = self.store.start_run('crawl', '2026-01-01 00:00:00')
        self.store.finish_run(run, '2026-01-01 00:01:00', 'done', {})
        self.assertEqual(self.store.latest_cycle_run_ids(), [])
        self.assertEqual(self.store.cycle_drifts(), [])
        self.assertEqual(self.store.cycle_drift_summary(), {})

    def test_an_empty_cycle_reports_nothing_rather_than_everything(self):
        """`run_ids=[]` is 'no runs', not 'do not filter'."""
        self._cycle(coercions=5, duplicates=0)
        self.assertEqual(self.store.drifts(run_ids=[]), [])
        self.assertEqual(self.store.drift_summary(run_ids=[]), {})
        self.assertGreater(len(self.store.drifts()), 0)


if __name__ == '__main__':
    unittest.main()
