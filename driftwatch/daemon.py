# -*- coding: utf-8 -*-
"""Unattended supervision: run the sync cycle forever, survive its failures.

    python -m driftwatch daemon --interval 1h

WHY a loop in-process rather than "let the OS scheduler call `sync`": the
service keeps state that only makes sense in sequence -- the Drive change
cursor, the digest fingerprint, the consecutive-failure count. An OS scheduler
that fires a fresh process every hour has no memory of whether the last run
failed, so it cannot back off, and two slow runs can overlap and contend for
the same SQLite file. One long-lived process with one store handle is the
smaller, more honest design. The OS still supervises: it starts this process at
boot and restarts it if it dies.

WHY a failed cycle does not stop the service: Drive rate limits, an Odoo
restart, and a laptop's suspended network all look like exceptions and all
resolve on their own. A watcher that exits on the first transient error stops
watching precisely when something is happening.
"""
from __future__ import annotations

import contextlib
import datetime
import io
import logging
import signal
import sys
import threading
from argparse import Namespace
from logging.handlers import TimedRotatingFileHandler
from typing import Optional

from . import notify
from .config import _duration

log = logging.getLogger('driftwatch')

#: First retry after a failed cycle. Doubles per consecutive failure, never
#: past the normal interval -- a broken cycle should not become a busy loop,
#: and it should not stop checking either.
BACKOFF_START = 60


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        '%Y-%m-%d %H:%M:%S')


def setup_logging(cfg, verbose: bool = False) -> None:
    """Log to stdout and to a daily-rotated file.

    Stdout is what a service manager captures; the file is what survives the
    service manager being reconfigured.
    """
    root = logging.getLogger('driftwatch')
    if root.handlers:                      # idempotent: tests call this twice
        return
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter('%(asctime)s %(levelname)-7s %(message)s',
                            datefmt='%Y-%m-%d %H:%M:%S')

    # Under pythonw.exe and some service hosts there is no console at all and
    # sys.stderr is None; a StreamHandler on that raises on the first record.
    if sys.stderr is not None:
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        root.addHandler(stream)

    try:
        cfg.log_dir.mkdir(parents=True, exist_ok=True)
        rotating = TimedRotatingFileHandler(
            cfg.log_dir / 'driftwatch.log', when='midnight',
            backupCount=cfg.log_retain, encoding='utf-8', utc=True)
        rotating.setFormatter(fmt)
        root.addHandler(rotating)
    except OSError as exc:
        # An unwritable log directory is not a reason to stop watching Drive.
        root.warning('file logging disabled: %s', exc)


class Daemon:
    """The supervised cycle. One instance per process."""

    def __init__(self, cfg, store, args: Namespace):
        self.cfg = cfg
        self.store = store
        self.args = args
        # --interval takes the same 900 / 15m / 2h forms as DRIFTWATCH_INTERVAL.
        self.interval = _duration(str(args.interval or ''), cfg.interval_seconds)
        self.stop = threading.Event()
        self.failures = 0
        self.cycles = 0

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def install_signal_handlers(self) -> None:
        def handler(signum, _frame):
            log.info('signal %s received -- finishing the current phase and '
                     'shutting down', signum)
            self.stop.set()

        for name in ('SIGINT', 'SIGTERM', 'SIGBREAK'):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                # Not the main thread, or the platform will not take it.
                pass

    def run(self) -> int:
        log.info('DriftWatch daemon starting')
        for key, value in self.cfg.redacted().items():
            log.info('  %-16s %s', key, value)
        log.info('  %-16s %s', 'interval', f'{self.interval}s')
        if not self.cfg.alerts_enabled:
            log.warning('drift email is OFF (set DRIFTWATCH_SMTP_HOST and '
                        'DRIFTWATCH_ALERT_TO to turn it on); findings will '
                        'still be recorded and logged')
        if not self.cfg.subject_email:
            log.warning('DRIFTWATCH_SUBJECT is not set. Credentials will be '
                        'built in non-delegated sa_direct mode, which sees '
                        'only what was shared with the service account '
                        'directly -- usually nothing. Run `python -m '
                        'driftwatch probe` before relying on this service.')

        while not self.stop.is_set():
            slept = self.cycle()
            if self.args.once or self.stop.is_set():
                break
            log.info('next cycle in %ss', slept)
            self.stop.wait(slept)

        log.info('DriftWatch daemon stopped after %s cycle(s)', self.cycles)
        return 0

    # ------------------------------------------------------------------ #
    # one cycle
    # ------------------------------------------------------------------ #
    def cycle(self) -> int:
        """Run crawl -> stage -> verify, then alert. Returns seconds to sleep."""
        self.cycles += 1
        started = _now()
        log.info('--- cycle %s started %s UTC ---', self.cycles, started)
        try:
            self._run_phases()
        except Exception as exc:                      # noqa: BLE001
            self.failures += 1
            log.exception('cycle failed (%s consecutive): %s: %s',
                          self.failures, type(exc).__name__, exc)
            self._heartbeat(started, 'failed', str(exc))
            backoff = min(self.interval,
                          BACKOFF_START * (2 ** (self.failures - 1)))
            return backoff

        if self.failures:
            log.info('recovered after %s failed cycle(s)', self.failures)
        self.failures = 0
        self._heartbeat(started, 'ok', '')

        self._write_dashboard()

        try:
            self._maybe_alert(started)
        except Exception as exc:                      # noqa: BLE001
            # Alerting is downstream of the watching. Losing the mail must not
            # lose the crawl, and the unchanged fingerprint means the next
            # cycle tries again.
            log.error('drift digest not sent: %s: %s', type(exc).__name__, exc)
        return self.interval

    def _run_phases(self) -> None:
        from .cli import cmd_crawl, cmd_stage, cmd_verify   # avoids a cycle

        for name, fn in (('crawl', cmd_crawl), ('stage', cmd_stage),
                         ('verify', cmd_verify)):
            if self.stop.is_set():
                log.info('shutdown requested -- skipping %s', name)
                return
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = fn(self.cfg, self.store, self.args)
            for line in buf.getvalue().splitlines():
                if line.strip():
                    log.info('%s', line.rstrip())
            if rc:
                raise RuntimeError(f'{name} returned exit code {rc}')

    def _write_dashboard(self) -> None:
        """Refresh the local page so it is current without anything serving it.

        Uses this process's own connection rather than reopening the store:
        the daemon is the writer, so it already has the consistent view.
        """
        try:
            from . import dashboard
            dashboard.write(self.store.conn, self.cfg, self.cfg.dashboard_path)
        except Exception as exc:                      # noqa: BLE001
            # A dashboard is a convenience. It never costs a cycle.
            log.warning('dashboard not written: %s: %s',
                        type(exc).__name__, exc)

    def _heartbeat(self, started: str, status: str, error: str) -> None:
        """Liveness a human (or a check script) can read without log parsing."""
        try:
            self.store.set_meta('daemon_cycle_started_at', started)
            self.store.set_meta('daemon_cycle_finished_at', _now())
            self.store.set_meta('daemon_status', status)
            self.store.set_meta('daemon_error', error[:500])
            self.store.set_meta('daemon_consecutive_failures', str(self.failures))
        except Exception as exc:                      # noqa: BLE001
            log.warning('could not write heartbeat: %s', exc)

    # ------------------------------------------------------------------ #
    # alerting
    # ------------------------------------------------------------------ #
    def _watching_nothing(self) -> list:
        """A complete crawl that saw zero objects is a finding, not a clean bill.

        This is the failure that unattended operation is worst at: a revoked
        delegation grant, a key rotated out from under the service, or a
        subject that fell back to the non-delegated ``sa_direct`` mode all
        produce an empty corpus, read "completely", and therefore no drift --
        forever, quietly. An empty corpus is indistinguishable from "every
        file was deleted", and the service already refuses to treat that as
        deletion. It should equally refuse to treat it as agreement.
        """
        stats = self.store.latest_run_stats('crawl')
        if not stats or not stats.get('read_complete'):
            return []
        if stats.get('files_seen', 0) > 0:
            return []
        subject = self.cfg.subject_email or '(unset)'
        return [{
            'drift_type': 'empty_corpus',
            'severity': 'error',
            'dataset_id': None,
            'row_ref': None,
            'column_key': None,
            'sheet_value': None,
            'odoo_value': None,
            'file_name': 'Drive',
            'tab_title': '(whole corpus)',
            'detail': (f'A complete crawl saw 0 objects as {subject}. Nothing '
                       f'is being watched. Check the domain-wide delegation '
                       f'grant and DRIFTWATCH_SUBJECT, then run '
                       f'`python -m driftwatch probe`.'),
        }]

    def _maybe_alert(self, started: str) -> None:
        run_id = self.store.latest_run_id('verify')
        if run_id is None:
            log.info('no verify run to report on')
            return

        # The whole cycle, not just the verify run: staging records drift of
        # its own, and a digest that omits it is a digest that never mentions
        # the coercions -- the findings that cannot be undone later.
        cycle = self.store.latest_cycle_run_ids()
        findings = [dict(r) for r in self.store.cycle_drifts()]
        summary = self.store.cycle_drift_summary()

        empty = self._watching_nothing()
        if empty:
            log.error('%s', empty[0]['detail'])
            findings = empty + findings
            summary = dict(summary)
            summary['empty_corpus'] = 1
        current = notify.fingerprint(findings)
        previous = self.store.get_meta('alert_fingerprint')

        log.info('cycle %s (verify run #%s): %s finding(s)',
                 '+'.join(str(r) for r in cycle) or 'none', run_id, len(findings))
        if current == previous:
            log.info('finding set unchanged -- no digest sent')
            return

        if not self.cfg.alerts_enabled:
            log.info('finding set changed, but email is not configured')
            self.store.set_meta('alert_fingerprint', current)
            self.store.set_meta('alert_finding_count', str(len(findings)))
            return

        prior_count: Optional[int] = None
        raw = self.store.get_meta('alert_finding_count')
        if raw.isdigit():
            prior_count = int(raw)

        subject, body = notify.render_digest(
            findings, summary,
            db_path=str(self.store.path),
            finished_at=_now(),
            host=notify.hostname(),
            previously=prior_count)
        notify.send_digest(self.cfg, subject, body)
        log.info('digest sent to %s: %s', ', '.join(self.cfg.alert_to), subject)

        # Only after a confirmed send, so a failed delivery is retried.
        self.store.set_meta('alert_fingerprint', current)
        self.store.set_meta('alert_finding_count', str(len(findings)))


def run(cfg, store, args: Namespace) -> int:
    setup_logging(cfg, verbose=getattr(args, 'verbose', False))
    daemon = Daemon(cfg, store, args)
    daemon.install_signal_handlers()
    return daemon.run()
