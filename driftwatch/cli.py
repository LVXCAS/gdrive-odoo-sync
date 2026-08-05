# -*- coding: utf-8 -*-
"""DriftWatch command line.

    python -m driftwatch probe      # prove the Google + Odoo connections
    python -m driftwatch crawl      # walk Drive, record every object
    python -m driftwatch stage      # read every spreadsheet tab, hash it
    python -m driftwatch verify     # compare Drive against Odoo, report drift
    python -m driftwatch status     # what is in the local store
    python -m driftwatch drift      # list findings
    python -m driftwatch sync       # crawl + stage + verify
    python -m driftwatch daemon     # sync on a loop, forever, and mail on drift
    python -m driftwatch alert-test # prove the mail path before trusting it

WHY every command is read-only by default: this tool exists to tell you whether
two systems agree, and a tool that changes one of them while measuring it
cannot answer that question. Nothing here writes to Drive (the OAuth scopes are
read-only, so it structurally cannot) and nothing here writes to Odoo.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from typing import Optional

from .config import load_config
from .store import Store


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def _fmt(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_probe(cfg, store, args) -> int:
    """Prove both connections before trusting anything either one says."""
    print(f'Google subject : {cfg.subject_email}')
    ok = True

    try:
        info = cfg.service_account_info()
        print(f'  key          : OK  client_id={info["client_id"]}')
    except Exception as exc:
        print(f'  key          : FAIL  {exc}')
        return 1

    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        base = service_account.Credentials.from_service_account_info(
            info, scopes=list(cfg.scopes))
        creds = base.with_subject(cfg.subject_email)
        # with_subject() returns a NEW object. Not capturing it leaves you as
        # the bare service account, which sees an empty Drive -- and an empty
        # Drive is indistinguishable from "everything was deleted".
        assert creds is not base
        creds.refresh(Request())
        drive = build('drive', 'v3', credentials=creds, cache_discovery=False)
        about = drive.about().get(fields='user(emailAddress)').execute()
        acting = about['user']['emailAddress']
        match = acting.lower() == cfg.subject_email.lower()
        print(f'  impersonation: {"OK" if match else "FAIL"}  acting as {acting}')
        ok &= match
        files = drive.files().list(q='trashed = false', pageSize=10,
                                   fields='files(id,name,mimeType)').execute()
        n = len(files.get('files', []))
        print(f'  corpus       : {"OK" if n else "FAIL"}  {_fmt(n, "file")} visible'
              + ('' if n else '  <-- empty corpus is the signature of a broken grant'))
        ok &= bool(n)
    except Exception as exc:
        print(f'  google       : FAIL  {type(exc).__name__}: {exc}')
        return 1

    print(f'\nOdoo           : {cfg.odoo_url}  db={cfg.odoo_db}')
    try:
        from .odoo_client import OdooClient
        odoo = OdooClient(cfg)
        uid = odoo.authenticate()
        ver = odoo.version()
        print(f'  auth         : OK  uid={uid}')
        print(f'  version      : {ver.get("server_version")}')
        print(f'  res.partner  : {odoo.search_count("res.partner")} record(s)')
    except Exception as exc:
        print(f'  odoo         : FAIL  {type(exc).__name__}: {exc}')
        ok = False

    print('\n' + ('ALL CONNECTIONS OK' if ok else 'ONE OR MORE CHECKS FAILED'))
    return 0 if ok else 1


def cmd_crawl(cfg, store, args) -> int:
    from .crawler import Crawler

    run_id = store.start_run('crawl', _now())
    stats = Crawler(cfg, store).crawl(run_id=run_id, full=not args.incremental)
    status = 'done' if stats.get('read_complete') else 'incomplete'
    store.finish_run(run_id, _now(), status, stats)

    print(f'crawl #{run_id}: {_fmt(stats.get("files_seen", 0), "object")} seen  '
          f'({stats.get("folders", 0)} folders, '
          f'{stats.get("spreadsheets", 0)} spreadsheets)')
    if not stats.get('read_complete'):
        print('  READ INCOMPLETE -- nothing was marked missing. This is deliberate:\n'
              '  a partial read is indistinguishable from a mass deletion.')
    else:
        print(f'  marked missing: {stats.get("marked_missing", 0)}')
    for err in stats.get('errors', [])[:10]:
        print(f'  ! {err}')
    return 0


def cmd_stage(cfg, store, args) -> int:
    from .stager import Stager

    run_id = store.start_run('stage', _now())
    stats = Stager(cfg, store).stage_all(run_id=run_id, limit=args.limit)
    store.finish_run(run_id, _now(), 'done', stats)

    print(f'stage #{run_id}: {_fmt(stats.get("files", 0), "workbook")}, '
          f'{_fmt(stats.get("tabs", 0), "tab")}, '
          f'{_fmt(stats.get("rows", 0), "row")}')
    if stats.get('blocked'):
        print(f'  blocked: {stats["blocked"]} tab(s) -- see `drift` for why')
    for err in stats.get('errors', [])[:10]:
        print(f'  ! {err}')
    return 0


def cmd_verify(cfg, store, args) -> int:
    from .verifier import Verifier

    odoo = None
    if not args.staging_only:
        from .odoo_client import OdooClient
        odoo = OdooClient(cfg)

    mapping = None
    if args.mapping:
        mapping = json.loads(open(args.mapping, encoding='utf-8').read())

    run_id = store.start_run('verify', _now())
    stats = Verifier(cfg, store, odoo).verify_all(
        run_id=run_id, mapping_by_dataset=mapping)
    store.finish_run(run_id, _now(), 'done', stats)

    print(f'verify #{run_id}: {stats.get("datasets", 0)} dataset(s), '
          f'{stats.get("compared", 0)} compared, '
          f'{stats.get("staging_only", 0)} staging-only')
    by_type = stats.get('by_type') or {}
    if by_type:
        print('  findings:')
        for k, v in sorted(by_type.items(), key=lambda kv: -kv[1]):
            print(f'    {k:<20} {v}')
    else:
        print('  no drift found')
    for err in stats.get('errors', [])[:10]:
        print(f'  ! {err}')
    return 0


def cmd_status(cfg, store, args) -> int:
    counts = store.count_nodes()
    total = sum(counts.values())
    print(f'store: {store.path}')

    # Whether the unattended service is alive, without reading a log file.
    last = store.get_meta('daemon_cycle_finished_at')
    if last:
        state = store.get_meta('daemon_status') or '?'
        fails = store.get_meta('daemon_consecutive_failures') or '0'
        print(f'daemon: last cycle {state} at {last} UTC'
              + (f'  ({fails} consecutive failure(s))' if fails != '0' else ''))
        err = store.get_meta('daemon_error')
        if err:
            print(f'  last error: {err}')
    print(f'\n{_fmt(total, "live Drive object")}:')
    for mime, n in list(counts.items())[:15]:
        short = mime.rsplit('.', 1)[-1] if '.' in mime else mime
        print(f'  {n:>6}  {short}')

    datasets = store.datasets()
    rows = sum(d['row_count'] for d in datasets)
    print(f'\n{_fmt(len(datasets), "dataset")} (spreadsheet tabs), '
          f'{_fmt(rows, "staged row")}')
    for d in datasets[:15]:
        flag = '' if d['read_complete'] else '  [INCOMPLETE]'
        blocked = f"  [{d['blocked_reason']}]" if d['blocked_reason'] else ''
        print(f"  {d['row_count']:>6}  {d['file_name']} / {d['tab_title']}{flag}{blocked}")
    if len(datasets) > 15:
        print(f'  ... and {len(datasets) - 15} more')

    summary = store.drift_summary()
    if summary:
        print('\ndrift findings (all runs):')
        for k, v in sorted(summary.items(), key=lambda kv: -kv[1]):
            print(f'  {k:<20} {v}')
    return 0


def cmd_drift(cfg, store, args) -> int:
    findings = store.drifts(drift_type=args.type)
    if not findings:
        print('no findings')
        return 0
    print(f'{_fmt(len(findings), "finding")}:\n')
    for d in findings[:args.limit or 50]:
        where = f"{d['file_name'] or '?'} / {d['tab_title'] or '?'}"
        print(f"  [{d['severity']}] {d['drift_type']}  {where}")
        if d['row_ref'] or d['column_key']:
            print(f"      at {d['row_ref'] or '?'} column {d['column_key'] or '?'}")
        if d['sheet_value'] is not None or d['odoo_value'] is not None:
            print(f"      sheet={d['sheet_value']!r}  odoo={d['odoo_value']!r}")
        if d['detail']:
            print(f"      {d['detail']}")
    if len(findings) > (args.limit or 50):
        print(f'\n  ... and {len(findings) - (args.limit or 50)} more')
    return 0


def cmd_sync(cfg, store, args) -> int:
    for fn in (cmd_crawl, cmd_stage, cmd_verify):
        print()
        rc = fn(cfg, store, args)
        if rc:
            return rc
    return 0


def cmd_daemon(cfg, store, args) -> int:
    """Run the sync cycle on a loop until something stops the process."""
    from dataclasses import replace

    from . import daemon

    if args.no_email:
        cfg = replace(cfg, alert_to=())
    return daemon.run(cfg, store, args)


def cmd_alert_test(cfg, store, args) -> int:
    """Send one digest built from the latest verify run.

    Worth running once before you walk away from the service: an SMTP problem
    discovered the first time there is real drift is discovered too late.
    """
    from . import notify

    if not cfg.alerts_enabled:
        print('email is not configured -- set DRIFTWATCH_SMTP_HOST and '
              'DRIFTWATCH_ALERT_TO (see .env.example)')
        return 1

    run_id = store.latest_run_id('verify')
    findings = [dict(r) for r in store.drifts(run_id=run_id)] if run_id else []
    summary = store.drift_summary(run_id=run_id) if run_id else {}
    subject, body = notify.render_digest(
        findings, summary, db_path=str(store.path), finished_at=_now(),
        host=notify.hostname())
    subject = f'[test] {subject}'

    print(f'sending to {", ".join(cfg.alert_to)} via '
          f'{cfg.smtp_host}:{cfg.smtp_port} ...')
    try:
        notify.send_digest(cfg, subject, body)
    except Exception as exc:
        print(f'FAILED  {type(exc).__name__}: {exc}')
        return 1
    print(f'sent: {subject}')
    return 0


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='driftwatch',
        description='Sync and verify Google Drive against Odoo. Read-only.')
    p.add_argument('--db', help='override the SQLite path')
    sub = p.add_subparsers(dest='command', required=True)

    sub.add_parser('probe', help='prove the Google and Odoo connections')

    c = sub.add_parser('crawl', help='walk Drive and record every object')
    c.add_argument('--incremental', action='store_true',
                   help='use the stored change cursor instead of a full walk')

    s = sub.add_parser('stage', help='read every spreadsheet tab and hash it')
    s.add_argument('--limit', type=int, default=None,
                   help='stage at most N workbooks (for a first run)')

    v = sub.add_parser('verify', help='compare Drive against Odoo')
    v.add_argument('--staging-only', action='store_true',
                   help='structural checks only; do not contact Odoo')
    v.add_argument('--mapping', help='path to a JSON dataset->model mapping')

    sub.add_parser('status', help='what is in the local store')

    d = sub.add_parser('drift', help='list findings')
    d.add_argument('--type', help='filter by drift type')
    d.add_argument('--limit', type=int, default=50)

    y = sub.add_parser('sync', help='crawl + stage + verify')
    y.add_argument('--incremental', action='store_true')
    y.add_argument('--limit', type=int, default=None)
    y.add_argument('--staging-only', action='store_true')
    y.add_argument('--mapping', default=None)

    m = sub.add_parser('daemon', help='run sync on a loop and mail on drift')
    m.add_argument('--interval', default=None,
                   help='time between cycles: 900, 15m, 2h, 1d '
                        '(default: DRIFTWATCH_INTERVAL, or 1h)')
    m.add_argument('--once', action='store_true',
                   help='run a single cycle and exit -- for testing, or for '
                        'driving the service from an external scheduler')
    m.add_argument('--no-email', action='store_true',
                   help='record and log drift, but send no digest')
    m.add_argument('--verbose', action='store_true')
    # Passed straight through to the crawl/stage/verify phases.
    m.add_argument('--incremental', action='store_true')
    m.add_argument('--limit', type=int, default=None)
    m.add_argument('--staging-only', action='store_true')
    m.add_argument('--mapping', default=None)

    sub.add_parser('alert-test', help='send one digest now, to prove SMTP works')
    return p


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config()
    store = Store(args.db or cfg.db_path)
    handler = {
        'probe': cmd_probe, 'crawl': cmd_crawl, 'stage': cmd_stage,
        'verify': cmd_verify, 'status': cmd_status, 'drift': cmd_drift,
        'sync': cmd_sync, 'daemon': cmd_daemon, 'alert-test': cmd_alert_test,
    }[args.command]
    try:
        return handler(cfg, store, args)
    finally:
        store.close()


if __name__ == '__main__':
    sys.exit(main())
