# -*- coding: utf-8 -*-
"""The supervised loop: failure isolation, backoff, and alert-on-change."""
from __future__ import annotations

import argparse
import dataclasses
import logging
import tempfile
import unittest
from pathlib import Path

from driftwatch import daemon, notify
from driftwatch.config import Config, _duration, _flag
from driftwatch.store import Store


def make_config(tmp: Path, **kw) -> Config:
    base = dict(
        sa_key_path=tmp / 'key.json', subject_email='ops@example.com',
        odoo_url='https://example.odoo.com', odoo_db='example',
        odoo_login='ops@example.com', odoo_api_key='x',
        db_path=tmp / 'driftwatch.sqlite3', log_dir=tmp / 'logs',
        interval_seconds=3600)
    base.update(kw)
    return Config(**base)


def make_args(**kw) -> argparse.Namespace:
    base = dict(interval=None, once=True, no_email=False, verbose=False,
                incremental=False, limit=None, staging_only=True, mapping=None)
    base.update(kw)
    return argparse.Namespace(**base)


class StubDaemon(daemon.Daemon):
    """A daemon whose phases are a script, not a network."""

    def __init__(self, *a, phases=None, **kw):
        super().__init__(*a, **kw)
        self.phase_calls = 0
        self._phases = phases

    def _run_phases(self) -> None:
        self.phase_calls += 1
        if self._phases:
            self._phases(self)


class DaemonTestCase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = make_config(self.tmp)
        self.store = Store(self.cfg.db_path)
        self.dataset_id = self._seed_dataset()
        self.sent = []
        self._real_send = notify.send_digest
        notify.send_digest = lambda cfg, subject, body, **kw: self.sent.append(
            (subject, body))
        logging.getLogger('driftwatch').addHandler(logging.NullHandler())

    def tearDown(self):
        notify.send_digest = self._real_send
        self.store.close()
        self._tmp.cleanup()

    # -- helpers ---------------------------------------------------------- #
    def _seed_dataset(self) -> int:
        """A node and one tab, so drift rows have something real to hang off."""
        when = '2026-08-05 09:00:00'
        self.store.conn.execute(
            'INSERT INTO node (file_id, name, mime_type, first_seen, last_seen)'
            ' VALUES (?, ?, ?, ?, ?)',
            ('file1', 'Cashflow 2026',
             'application/vnd.google-apps.spreadsheet', when, when))
        cur = self.store.conn.execute(
            'INSERT INTO dataset (file_id, tab_id, tab_title, updated_at)'
            ' VALUES (?, ?, ?, ?)', ('file1', 'tab1', 'Q3', when))
        self.store.conn.commit()
        return int(cur.lastrowid)

    def record_drift(self, run_id: int, *rows) -> None:
        for row in rows:
            row = dict(row)
            self.store.record_drift(
                run_id, self.dataset_id,
                row.pop('drift_type', 'field_mismatch'),
                '2026-08-05 10:00:00', **row)
        self.store.conn.commit()


class CycleTests(DaemonTestCase):

    def test_a_failing_cycle_does_not_raise_out_of_the_loop(self):
        """A transient Drive or Odoo failure must not stop the watching."""
        def boom(_self):
            raise RuntimeError('drive said no')

        d = StubDaemon(self.cfg, self.store, make_args(), phases=boom)
        self.assertEqual(d.run(), 0)
        self.assertEqual(d.failures, 1)

    def test_backoff_doubles_then_stops_at_the_interval(self):
        def boom(_self):
            raise RuntimeError('still no')

        d = StubDaemon(self.cfg, self.store, make_args(), phases=boom)
        seen = [d.cycle() for _ in range(8)]
        self.assertEqual(seen[0], daemon.BACKOFF_START)
        self.assertEqual(seen[1], daemon.BACKOFF_START * 2)
        self.assertEqual(seen[2], daemon.BACKOFF_START * 4)
        self.assertTrue(all(s <= self.cfg.interval_seconds for s in seen))
        self.assertEqual(seen[-1], self.cfg.interval_seconds)

    def test_a_good_cycle_clears_the_failure_count(self):
        state = {'fail': True}

        def flaky(_self):
            if state['fail']:
                state['fail'] = False
                raise RuntimeError('one blip')

        d = StubDaemon(self.cfg, self.store, make_args(), phases=flaky)
        self.assertEqual(d.cycle(), daemon.BACKOFF_START)
        self.assertEqual(d.cycle(), self.cfg.interval_seconds)
        self.assertEqual(d.failures, 0)

    def test_heartbeat_records_success_and_failure(self):
        d = StubDaemon(self.cfg, self.store, make_args())
        d.cycle()
        self.assertEqual(self.store.get_meta('daemon_status'), 'ok')
        self.assertTrue(self.store.get_meta('daemon_cycle_finished_at'))

        d2 = StubDaemon(self.cfg, self.store, make_args(),
                        phases=lambda _s: (_ for _ in ()).throw(
                            RuntimeError('nope')))
        d2.cycle()
        self.assertEqual(self.store.get_meta('daemon_status'), 'failed')
        self.assertIn('nope', self.store.get_meta('daemon_error'))

    def test_once_runs_exactly_one_cycle(self):
        d = StubDaemon(self.cfg, self.store, make_args(once=True))
        d.run()
        self.assertEqual(d.phase_calls, 1)

    def test_a_stop_signal_skips_the_remaining_phases(self):
        d = daemon.Daemon(self.cfg, self.store, make_args())
        d.stop.set()
        d._run_phases()          # must return without importing/calling crawl
        self.assertTrue(d.stop.is_set())


class AlertTests(DaemonTestCase):

    def setUp(self):
        super().setUp()
        self.cfg = dataclasses.replace(
            self.cfg, smtp_host='smtp.example.com',
            alert_to=('ops@example.com',), alert_from='bot@example.com')

    def test_first_cycle_with_findings_sends_a_baseline_digest(self):
        run_id = self.store.start_run('verify', '2026-08-05 10:00:00')
        self.record_drift(run_id, {'row_ref': 'row 1', 'sheet_value': 'a',
                                   'odoo_value': 'b'})
        d = StubDaemon(self.cfg, self.store, make_args())
        d.cycle()
        self.assertEqual(len(self.sent), 1)
        self.assertIn('1 finding', self.sent[0][0])

    def test_an_unchanged_finding_set_sends_nothing_the_second_time(self):
        run_id = self.store.start_run('verify', '2026-08-05 10:00:00')
        self.record_drift(run_id, {'row_ref': 'row 1', 'sheet_value': 'a',
                                   'odoo_value': 'b'})
        d = StubDaemon(self.cfg, self.store, make_args())
        d.cycle()

        # A second verify run that finds exactly the same thing: new rows,
        # new ids, same meaning. Silence is correct.
        run2 = self.store.start_run('verify', '2026-08-05 11:00:00')
        self.record_drift(run2, {'row_ref': 'row 1', 'sheet_value': 'a',
                                 'odoo_value': 'b'})
        d.cycle()
        self.assertEqual(len(self.sent), 1)

    def test_a_new_finding_sends_again(self):
        run_id = self.store.start_run('verify', '2026-08-05 10:00:00')
        self.record_drift(run_id, {'row_ref': 'row 1', 'sheet_value': 'a',
                                   'odoo_value': 'b'})
        d = StubDaemon(self.cfg, self.store, make_args())
        d.cycle()

        run2 = self.store.start_run('verify', '2026-08-05 11:00:00')
        self.record_drift(run2,
                          {'row_ref': 'row 1', 'sheet_value': 'a',
                           'odoo_value': 'b'},
                          {'row_ref': 'row 2', 'sheet_value': 'c',
                           'odoo_value': 'd'})
        d.cycle()
        self.assertEqual(len(self.sent), 2)
        self.assertIn('2 findings', self.sent[1][0])

    def test_drift_clearing_is_itself_worth_a_mail(self):
        run_id = self.store.start_run('verify', '2026-08-05 10:00:00')
        self.record_drift(run_id, {'row_ref': 'row 1'})
        d = StubDaemon(self.cfg, self.store, make_args())
        d.cycle()

        self.store.start_run('verify', '2026-08-05 11:00:00')   # found nothing
        d.cycle()
        self.assertEqual(len(self.sent), 2)
        self.assertIn('clear', self.sent[1][0])

    def test_a_failed_send_is_retried_next_cycle(self):
        """The fingerprint advances only on a confirmed send."""
        def explode(cfg, subject, body, **kw):
            raise OSError('smtp refused')

        notify.send_digest = explode
        run_id = self.store.start_run('verify', '2026-08-05 10:00:00')
        self.record_drift(run_id, {'row_ref': 'row 1'})
        d = StubDaemon(self.cfg, self.store, make_args())
        d.cycle()
        self.assertEqual(self.store.get_meta('alert_fingerprint'), '')

        notify.send_digest = lambda cfg, s, b, **kw: self.sent.append((s, b))
        run2 = self.store.start_run('verify', '2026-08-05 11:00:00')
        self.record_drift(run2, {'row_ref': 'row 1'})
        d.cycle()
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.store.get_meta('alert_fingerprint'))

    def test_a_failed_send_does_not_fail_the_cycle(self):
        def explode(cfg, subject, body, **kw):
            raise OSError('smtp refused')

        notify.send_digest = explode
        run_id = self.store.start_run('verify', '2026-08-05 10:00:00')
        self.record_drift(run_id, {'row_ref': 'row 1'})
        d = StubDaemon(self.cfg, self.store, make_args())
        self.assertEqual(d.cycle(), self.cfg.interval_seconds)
        self.assertEqual(self.store.get_meta('daemon_status'), 'ok')

    def test_no_email_config_still_tracks_the_fingerprint(self):
        """Turning mail on later should not mail the whole backlog at once."""
        cfg = make_config(self.tmp)          # no smtp_host
        run_id = self.store.start_run('verify', '2026-08-05 10:00:00')
        self.record_drift(run_id, {'row_ref': 'row 1'})
        d = StubDaemon(cfg, self.store, make_args())
        d.cycle()
        self.assertEqual(self.sent, [])
        self.assertTrue(self.store.get_meta('alert_fingerprint'))

    def test_no_verify_run_means_no_mail(self):
        d = StubDaemon(self.cfg, self.store, make_args())
        d.cycle()
        self.assertEqual(self.sent, [])


class EmptyCorpusTests(DaemonTestCase):
    """A complete crawl that saw nothing must not read as 'no drift'."""

    def setUp(self):
        super().setUp()
        self.cfg = dataclasses.replace(
            self.cfg, smtp_host='smtp.example.com',
            alert_to=('ops@example.com',), subject_email='ops@example.com')

    def finish_crawl(self, **stats) -> None:
        run_id = self.store.start_run('crawl', '2026-08-05 10:00:00')
        self.store.finish_run(run_id, '2026-08-05 10:01:00', 'done', stats)

    def test_zero_objects_on_a_complete_crawl_is_reported(self):
        self.finish_crawl(read_complete=True, files_seen=0)
        self.store.start_run('verify', '2026-08-05 10:02:00')
        d = StubDaemon(self.cfg, self.store, make_args())
        d.cycle()
        self.assertEqual(len(self.sent), 1)
        subject, body = self.sent[0]
        self.assertIn('empty_corpus', subject)
        self.assertIn('0 objects', body)

    def test_a_populated_corpus_reports_nothing_extra(self):
        self.finish_crawl(read_complete=True, files_seen=1200)
        self.store.start_run('verify', '2026-08-05 10:02:00')
        d = StubDaemon(self.cfg, self.store, make_args())
        d.cycle()
        self.assertEqual(len(self.sent), 1)
        self.assertIn('clear', self.sent[0][0])

    def test_an_incomplete_crawl_is_not_an_empty_corpus(self):
        """A short read is already refused upstream; do not double-report it."""
        self.finish_crawl(read_complete=False, files_seen=0)
        self.store.start_run('verify', '2026-08-05 10:02:00')
        d = StubDaemon(self.cfg, self.store, make_args())
        self.assertEqual(d._watching_nothing(), [])

    def test_the_condition_clearing_is_itself_a_change(self):
        self.finish_crawl(read_complete=True, files_seen=0)
        self.store.start_run('verify', '2026-08-05 10:02:00')
        d = StubDaemon(self.cfg, self.store, make_args())
        d.cycle()

        self.finish_crawl(read_complete=True, files_seen=1200)
        self.store.start_run('verify', '2026-08-05 11:02:00')
        d.cycle()
        self.assertEqual(len(self.sent), 2)
        self.assertIn('clear', self.sent[1][0])

    def test_it_does_not_re_alert_every_cycle(self):
        for hour in (10, 11, 12):
            self.finish_crawl(read_complete=True, files_seen=0)
            self.store.start_run('verify', f'2026-08-05 {hour}:02:00')
            StubDaemon(self.cfg, self.store, make_args()).cycle()
        self.assertEqual(len(self.sent), 1)

    def test_no_crawl_yet_is_not_an_empty_corpus(self):
        d = StubDaemon(self.cfg, self.store, make_args())
        self.assertEqual(d._watching_nothing(), [])


class StoreTests(DaemonTestCase):

    def test_latest_run_id_picks_the_newest_of_that_kind(self):
        self.store.start_run('crawl', '2026-08-05 09:00:00')
        first = self.store.start_run('verify', '2026-08-05 10:00:00')
        second = self.store.start_run('verify', '2026-08-05 11:00:00')
        self.store.start_run('crawl', '2026-08-05 12:00:00')
        self.assertEqual(self.store.latest_run_id('verify'), second)
        self.assertNotEqual(self.store.latest_run_id('verify'), first)

    def test_latest_run_id_is_none_when_there_are_none(self):
        self.assertIsNone(self.store.latest_run_id('verify'))


class ConfigTests(unittest.TestCase):

    def test_duration_accepts_bare_seconds_and_suffixes(self):
        self.assertEqual(_duration('900', 1), 900)
        self.assertEqual(_duration('15m', 1), 900)
        self.assertEqual(_duration('2h', 1), 7200)
        self.assertEqual(_duration('1d', 1), 86400)

    def test_duration_falls_back_rather_than_raising(self):
        """A typo in .env must not stop the service from starting."""
        self.assertEqual(_duration('every hour', 3600), 3600)
        self.assertEqual(_duration('', 3600), 3600)

    def test_duration_never_returns_zero(self):
        self.assertGreaterEqual(_duration('0', 3600), 1)

    def test_flag_parsing(self):
        for yes in ('1', 'true', 'YES', 'on'):
            self.assertTrue(_flag(yes, False))
        for no in ('0', 'false', 'off', 'no'):
            self.assertFalse(_flag(no, True))
        self.assertTrue(_flag('', True))

    def test_alerts_need_both_a_host_and_a_recipient(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            self.assertFalse(make_config(tmp).alerts_enabled)
            self.assertFalse(make_config(tmp, smtp_host='h').alerts_enabled)
            self.assertFalse(make_config(tmp, alert_to=('a@b',)).alerts_enabled)
            self.assertTrue(make_config(tmp, smtp_host='h',
                                        alert_to=('a@b',)).alerts_enabled)

    def test_redacted_never_leaks_a_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp), smtp_password='hunter2',
                              odoo_api_key='sekrit')
            blob = repr(cfg.redacted())
            self.assertNotIn('hunter2', blob)
            self.assertNotIn('sekrit', blob)


if __name__ == '__main__':
    unittest.main()
