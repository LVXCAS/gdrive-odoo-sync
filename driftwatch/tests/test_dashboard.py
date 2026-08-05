# -*- coding: utf-8 -*-
"""The local dashboard: health arithmetic, escaping, and honest counts."""
from __future__ import annotations

import dataclasses
import datetime
import tempfile
import unittest
from pathlib import Path

from driftwatch import dashboard
from driftwatch.store import Store
from driftwatch.tests.test_daemon import make_config


def stamp(minutes_ago: int) -> str:
    when = (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(minutes=minutes_ago))
    return when.strftime('%Y-%m-%d %H:%M:%S')


class DashboardTestCase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = make_config(self.tmp)
        self.store = Store(self.cfg.db_path)
        when = '2026-08-05 09:00:00'
        self.store.conn.execute(
            'INSERT INTO node (file_id, name, mime_type, first_seen, last_seen)'
            ' VALUES (?, ?, ?, ?, ?)',
            ('f1', 'Cashflow 2026',
             'application/vnd.google-apps.spreadsheet', when, when))
        cur = self.store.conn.execute(
            'INSERT INTO dataset (file_id, tab_id, tab_title, row_count,'
            ' updated_at) VALUES (?, ?, ?, ?, ?)', ('f1', 't1', 'Q3', 40, when))
        self.dataset_id = int(cur.lastrowid)
        self.store.conn.commit()

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def seed_verify(self, *findings) -> int:
        run_id = self.store.start_run('verify', stamp(5))
        for f in findings:
            f = dict(f)
            self.store.record_drift(run_id, self.dataset_id,
                                    f.pop('drift_type', 'field_mismatch'),
                                    stamp(5), **f)
        self.store.conn.commit()
        return run_id

    def heartbeat(self, status: str, minutes_ago: int, failures: int = 0):
        self.store.set_meta('daemon_status', status)
        self.store.set_meta('daemon_cycle_finished_at', stamp(minutes_ago))
        self.store.set_meta('daemon_consecutive_failures', str(failures))


class HealthTests(DashboardTestCase):

    def test_a_recent_ok_cycle_is_watching(self):
        self.heartbeat('ok', 5)
        data = dashboard.collect(self.store.conn, self.cfg)
        self.assertEqual(data['health']['state'], 'ok')

    def test_an_old_cycle_is_stale_even_though_it_succeeded(self):
        """The failure that matters most: a stopped service sends no mail,
        which looks exactly like no drift."""
        self.heartbeat('ok', 60 * 5)          # interval is 1h
        data = dashboard.collect(self.store.conn, self.cfg)
        self.assertEqual(data['health']['state'], 'stale')
        self.assertIn('Nothing is being watched', data['health']['note'])

    def test_a_failed_cycle_outranks_freshness(self):
        self.heartbeat('failed', 1, failures=3)
        self.store.set_meta('daemon_error', 'drive said no')
        data = dashboard.collect(self.store.conn, self.cfg)
        self.assertEqual(data['health']['state'], 'failed')
        self.assertEqual(data['health']['failures'], 3)

    def test_no_cycle_yet_is_unknown_not_healthy(self):
        data = dashboard.collect(self.store.conn, self.cfg)
        self.assertEqual(data['health']['state'], 'unknown')

    def test_staleness_scales_with_a_longer_interval(self):
        self.heartbeat('ok', 60 * 5)
        cfg = dataclasses.replace(self.cfg, interval_seconds=86400)
        data = dashboard.collect(self.store.conn, cfg)
        self.assertEqual(data['health']['state'], 'ok')


class ContentTests(DashboardTestCase):

    def test_counts_come_from_the_latest_verify_run_only(self):
        self.seed_verify({'row_ref': 'row 1'}, {'row_ref': 'row 2'})
        second = self.seed_verify({'row_ref': 'row 9'})
        data = dashboard.collect(self.store.conn, self.cfg)
        self.assertEqual(data['drift']['run_id'], second)
        self.assertEqual(data['drift']['total'], 1)

    def test_severity_split_is_reported(self):
        self.seed_verify({'row_ref': 'a', 'severity': 'error'},
                         {'row_ref': 'b', 'severity': 'warning'},
                         {'row_ref': 'c', 'severity': 'warning'})
        data = dashboard.collect(self.store.conn, self.cfg)
        self.assertEqual(data['drift']['severities']['error'], 1)
        self.assertEqual(data['drift']['severities']['warning'], 2)

    def test_corpus_and_dataset_totals(self):
        data = dashboard.collect(self.store.conn, self.cfg)
        self.assertEqual(data['corpus']['total'], 1)
        self.assertEqual(data['datasets']['count'], 1)
        self.assertEqual(data['datasets']['rows'], 40)

    def test_a_capped_list_says_what_it_left_out(self):
        """Silent truncation reads as 'that was all of it'."""
        self.seed_verify(*[{'row_ref': f'row {i}'}
                           for i in range(dashboard.MAX_FINDINGS + 25)])
        data = dashboard.collect(self.store.conn, self.cfg)
        page = dashboard.render(data)
        self.assertEqual(len(data['drift']['findings']), dashboard.MAX_FINDINGS)
        self.assertIn(f'Showing {dashboard.MAX_FINDINGS} of', page)

    def test_errors_are_listed_before_warnings(self):
        self.seed_verify({'row_ref': 'w', 'severity': 'warning'},
                         {'row_ref': 'e', 'severity': 'error'})
        data = dashboard.collect(self.store.conn, self.cfg)
        self.assertEqual(data['drift']['findings'][0]['severity'], 'error')


class RenderTests(DashboardTestCase):

    def test_spreadsheet_content_is_escaped(self):
        """File names and cell values come from spreadsheets nobody here
        wrote; they are untrusted text on the way into HTML."""
        self.store.conn.execute(
            "UPDATE node SET name = ? WHERE file_id = 'f1'",
            ('<script>alert(1)</script>',))
        self.store.conn.commit()
        self.seed_verify({'row_ref': 'row 1',
                          'sheet_value': '<img src=x onerror=alert(2)>'})
        page = dashboard.render(dashboard.collect(self.store.conn, self.cfg))
        self.assertNotIn('<script>alert(1)</script>', page)
        self.assertNotIn('<img src=x', page)
        self.assertIn('&lt;script&gt;', page)

    def test_page_is_self_contained(self):
        """It must render from file:// with no network at all."""
        page = dashboard.render(dashboard.collect(self.store.conn, self.cfg))
        for remote in ('http://', 'https://cdn', '<script src', '@import'):
            self.assertNotIn(remote, page.replace('https://', 'X', 1)
                             if remote == 'http://' else page)

    def test_both_themes_are_defined(self):
        page = dashboard.render(dashboard.collect(self.store.conn, self.cfg))
        self.assertIn('prefers-color-scheme:dark', page)
        self.assertIn('[data-theme="dark"]', page)

    def test_renders_with_an_empty_store(self):
        page = dashboard.render(dashboard.collect(self.store.conn, self.cfg))
        self.assertIn('DriftWatch', page)
        self.assertIn('(none yet)', page)

    def test_bars_scale_to_the_largest_and_leave_room_for_the_value(self):
        self.seed_verify(*([{'row_ref': f'a{i}'} for i in range(10)]
                           + [{'row_ref': 'b', 'drift_type': 'empty_tab'}]))
        data = dashboard.collect(self.store.conn, self.cfg)
        bars = dashboard._bar_rows(data['drift']['by_type'])
        self.assertIn('width:85.00%', bars)      # the largest, capped at 85
        self.assertIn('8.50%', bars)             # 1/10th of it, proportional

    def test_write_creates_the_file_and_parent(self):
        out = self.tmp / 'nested' / 'dashboard.html'
        dashboard.write(self.store.conn, self.cfg, out)
        self.assertTrue(out.exists())
        self.assertIn('DriftWatch', out.read_text(encoding='utf-8'))


class ReadOnlyTests(DashboardTestCase):

    def test_missing_store_says_what_to_do(self):
        with self.assertRaises(FileNotFoundError) as caught:
            dashboard.open_readonly(self.tmp / 'nope.sqlite3')
        self.assertIn('driftwatch sync', str(caught.exception))

    def test_readonly_connection_cannot_write(self):
        import sqlite3
        conn = dashboard.open_readonly(self.cfg.db_path)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO meta(key, value) VALUES('x','y')")
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
