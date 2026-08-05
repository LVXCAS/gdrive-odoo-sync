# -*- coding: utf-8 -*-
"""A local dashboard: what the service is doing, on one page.

    python -m driftwatch dashboard --open        # write a snapshot and open it
    python -m driftwatch dashboard --serve       # live at http://127.0.0.1:8787

WHY the page is generated locally and never published: it embeds real staged
content -- Drive file names, tab titles, and the sheet and Odoo values behind
every finding. That is the same material as the datastore, which the project
treats as a copy of the corpus rather than a cache. A dashboard that is
convenient to share is a dashboard that leaks it.

WHY the served page opens the store READ-ONLY: the daemon is usually mid-cycle
and holds the write lock. A reader that also writes (even just a schema-version
bump) contends with it and can block a crawl behind a browser refresh.

The page is self-contained -- no external CSS, fonts, or scripts -- so it works
from a file:// URL with no network at all.
"""
from __future__ import annotations

import datetime
import html
import sqlite3
from pathlib import Path
from typing import Optional

#: Cycles older than this multiple of the interval mean nobody is watching.
STALE_AFTER = 2.5

#: Findings listed on the page. The rest stay in the store; the page says so
#: rather than quietly showing the first N as if they were all of them.
MAX_FINDINGS = 100


# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #
def open_readonly(db_path: Path) -> sqlite3.Connection:
    """A read-only connection, falling back if WAL recovery is needed.

    A read-only handle cannot replay a hot WAL. That only matters when the
    writer died uncleanly and no one has reopened the file since, so the
    fallback is a normal connection rather than an error the user has to
    interpret.
    """
    if not Path(db_path).exists():
        raise FileNotFoundError(
            f'No datastore at {db_path}. Run `python -m driftwatch sync` '
            f'first, or point --db at the right file.')
    try:
        conn = sqlite3.connect(f'file:{Path(db_path).as_posix()}?mode=ro',
                               uri=True)
        conn.execute('SELECT 1 FROM meta LIMIT 1').fetchone()
    except sqlite3.Error:
        conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _parse(stamp: str) -> Optional[datetime.datetime]:
    try:
        return datetime.datetime.strptime(stamp, '%Y-%m-%d %H:%M:%S').replace(
            tzinfo=datetime.timezone.utc)
    except (ValueError, TypeError):
        return None


def _meta(conn, key: str, default: str = '') -> str:
    row = conn.execute('SELECT value FROM meta WHERE key = ?', (key,)).fetchone()
    return row['value'] if row else default


def _ago(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 90:
        return f'{seconds}s ago'
    if seconds < 5400:
        return f'{seconds // 60}m ago'
    if seconds < 172800:
        return f'{seconds // 3600}h ago'
    return f'{seconds // 86400}d ago'


def _health(conn, interval: int) -> dict:
    """Alive, stale, or broken -- and how it knows."""
    finished = _meta(conn, 'daemon_cycle_finished_at')
    status = _meta(conn, 'daemon_status')
    failures = _meta(conn, 'daemon_consecutive_failures', '0')
    error = _meta(conn, 'daemon_error')

    when = _parse(finished)
    age = (_utc_now() - when).total_seconds() if when else None

    if not finished:
        state, note = 'unknown', 'The daemon has not completed a cycle yet.'
    elif status == 'failed':
        state = 'failed'
        note = error or 'The last cycle raised an exception.'
    elif age is not None and age > interval * STALE_AFTER:
        # The failure that matters most: a stopped service sends no mail,
        # which is indistinguishable from no drift.
        state = 'stale'
        note = (f'The last cycle finished {_ago(age)}, more than '
                f'{STALE_AFTER:g}x the {interval // 60}m interval. '
                f'Nothing is being watched.')
    else:
        state, note = 'ok', 'Cycles are completing on schedule.'

    return {
        'state': state, 'note': note, 'finished_at': finished,
        'age': age, 'ago': _ago(age) if age is not None else '--',
        'failures': int(failures) if failures.isdigit() else 0,
        'error': error, 'interval': interval,
    }


def collect(conn, cfg) -> dict:
    """Everything the page shows, in one pass over the store."""
    interval = getattr(cfg, 'interval_seconds', 3600) or 3600

    counts = conn.execute(
        'SELECT mime_type, COUNT(*) n FROM node WHERE missing_since IS NULL '
        'GROUP BY mime_type ORDER BY n DESC').fetchall()
    total_nodes = sum(r['n'] for r in counts)
    folders = sum(r['n'] for r in counts if 'folder' in (r['mime_type'] or ''))
    sheets = sum(r['n'] for r in counts
                 if 'spreadsheet' in (r['mime_type'] or '')
                 or 'sheet' in (r['mime_type'] or '')
                 or 'excel' in (r['mime_type'] or ''))

    ds = conn.execute(
        'SELECT COUNT(*) n, COALESCE(SUM(row_count), 0) rows, '
        ' SUM(CASE WHEN read_complete = 0 THEN 1 ELSE 0 END) incomplete, '
        ' SUM(CASE WHEN blocked_reason IS NOT NULL AND blocked_reason != \'\' '
        '     THEN 1 ELSE 0 END) blocked FROM dataset').fetchone()

    run = conn.execute("SELECT id FROM run WHERE kind = 'verify' "
                       'ORDER BY id DESC LIMIT 1').fetchone()
    run_id = run['id'] if run else None

    by_type, findings, severities, total_drift = [], [], {}, 0
    if run_id is not None:
        by_type = [dict(r) for r in conn.execute(
            'SELECT drift_type, COUNT(*) n, '
            " MAX(CASE severity WHEN 'error' THEN 2 WHEN 'warning' THEN 1 "
            '  ELSE 0 END) sev_rank '
            'FROM drift WHERE run_id = ? GROUP BY drift_type ORDER BY n DESC',
            (run_id,))]
        total_drift = sum(r['n'] for r in by_type)
        severities = {r['severity']: r['n'] for r in conn.execute(
            'SELECT severity, COUNT(*) n FROM drift WHERE run_id = ? '
            'GROUP BY severity', (run_id,))}
        findings = [dict(r) for r in conn.execute(
            'SELECT dr.*, d.tab_title, n.name AS file_name FROM drift dr '
            'LEFT JOIN dataset d ON d.id = dr.dataset_id '
            'LEFT JOIN node n ON n.file_id = d.file_id '
            'WHERE dr.run_id = ? ORDER BY CASE dr.severity '
            "  WHEN 'error' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, "
            'dr.id LIMIT ?', (run_id, MAX_FINDINGS))]

    runs = [dict(r) for r in conn.execute(
        'SELECT id, kind, status, started_at, finished_at FROM run '
        'ORDER BY id DESC LIMIT 10')]
    for r in runs:
        start, end = _parse(r['started_at']), _parse(r['finished_at'])
        r['duration'] = (f'{int((end - start).total_seconds())}s'
                         if start and end else '--')

    return {
        'generated_at': _utc_now().strftime('%Y-%m-%d %H:%M:%S'),
        'store_path': _meta(conn, '__path__', '') or '',
        'health': _health(conn, interval),
        'alerts_enabled': getattr(cfg, 'alerts_enabled', False),
        'alert_to': list(getattr(cfg, 'alert_to', ())),
        'subject': getattr(cfg, 'subject_email', ''),
        'odoo_url': getattr(cfg, 'odoo_url', ''),
        'corpus': {'total': total_nodes, 'folders': folders,
                   'spreadsheets': sheets,
                   'by_mime': [dict(r) for r in counts[:12]]},
        'datasets': {'count': ds['n'] or 0, 'rows': ds['rows'] or 0,
                     'incomplete': ds['incomplete'] or 0,
                     'blocked': ds['blocked'] or 0},
        'drift': {'run_id': run_id, 'total': total_drift, 'by_type': by_type,
                  'severities': severities, 'findings': findings},
        'runs': runs,
    }


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def _e(value) -> str:
    """Escape everything. File names and cell values are attacker-adjacent
    text: they come from spreadsheets nobody on this project wrote."""
    return html.escape('' if value is None else str(value), quote=True)


def _n(value) -> str:
    try:
        return f'{int(value):,}'
    except (TypeError, ValueError):
        return str(value)


#: Status marks are glyph + word, never colour alone.
STATE_MARK = {'ok': ('&#10003;', 'Watching'), 'stale': ('&#9650;', 'Stale'),
              'failed': ('&#10007;', 'Failed'), 'unknown': ('&#8226;', 'No data')}

#: severity -> (glyph, css suffix). `info` is deliberately quiet: it exists so
#: a fact can be recorded without being shouted.
SEV_MARK = {'error': '&#10007;', 'warning': '&#9650;', 'info': '&#8226;'}
SEV_BY_RANK = {2: 'error', 1: 'warning', 0: 'info'}


def _sev(name: str) -> tuple:
    name = name if name in SEV_MARK else 'warning'
    return SEV_MARK[name], name

CSS = """
*,*::before,*::after{box-sizing:border-box}
.viz-root{
  color-scheme:light;
  --plane:#f9f9f7; --surface:#fcfcfb;
  --ink:#0b0b0b; --ink-2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --ring:rgba(11,11,11,.10);
  --series-1:#2a78d6; --track:#e1e0d9;
  --good:#0ca30c; --warning:#fab219; --serious:#ec835a; --critical:#d03b3b;
}
@media (prefers-color-scheme:dark){
  :root:where(:not([data-theme="light"])) .viz-root{
    color-scheme:dark;
    --plane:#0d0d0d; --surface:#1a1a19;
    --ink:#fff; --ink-2:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,.10);
    --series-1:#3987e5; --track:#2c2c2a;
  }
}
:root[data-theme="dark"] .viz-root{
  color-scheme:dark;
  --plane:#0d0d0d; --surface:#1a1a19;
  --ink:#fff; --ink-2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,.10);
  --series-1:#3987e5; --track:#2c2c2a;
}
body{margin:0;background:var(--plane)}
.viz-root{
  font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;
  color:var(--ink); background:var(--plane);
  padding:32px 24px 64px; max-width:1080px; margin:0 auto;
}
h1{font-size:20px;font-weight:600;margin:0}
h2{font-size:13px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;
   color:var(--muted);margin:40px 0 12px}
.sub{color:var(--ink-2);font-size:13px;margin:4px 0 0}
.card{background:var(--surface);border:1px solid var(--ring);border-radius:10px;padding:20px}

/* health banner ------------------------------------------------------- */
.health{display:flex;gap:16px;align-items:flex-start;margin:24px 0 0;
        border-left:3px solid var(--state);}
.health .mark{font-size:20px;line-height:1.2;color:var(--state)}
.state-ok{--state:var(--good)} .state-stale{--state:var(--warning)}
.state-failed{--state:var(--critical)} .state-unknown{--state:var(--muted)}
.health .word{font-weight:600}
.health .note{color:var(--ink-2);font-size:13px;margin-top:2px}

/* hero + tiles -------------------------------------------------------- */
.hero{margin:20px 0 0}
.hero .value{font-size:52px;font-weight:600;line-height:1.05;letter-spacing:-.02em}
.hero .label{color:var(--ink-2);font-size:14px;margin-top:2px}
.tiles{display:grid;gap:12px;margin-top:16px;
       grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
.tile .label{color:var(--muted);font-size:12px}
.tile .value{font-size:26px;font-weight:600;margin-top:2px}
.tile .foot{color:var(--muted);font-size:12px;margin-top:2px}

/* bars ---------------------------------------------------------------- */
.bars{display:grid;gap:10px}
.bar-row{display:grid;grid-template-columns:minmax(150px,220px) 1fr;
         gap:14px;align-items:center}
.bar-name{font-size:13px;display:flex;gap:8px;align-items:center;
          justify-content:space-between}
.bar-type{font-variant-numeric:tabular-nums}
.bar-track{display:flex;align-items:center;gap:8px}
.bar{height:14px;min-width:3px;border-radius:0 4px 4px 0;
     background:var(--series-1)}
.bar-value{font-size:13px;color:var(--ink-2);font-variant-numeric:tabular-nums;
           white-space:nowrap}
.sev{font-size:11px;font-weight:600;white-space:nowrap;color:var(--ink-2)}
.sev .g{color:var(--sev)}
.sev-error{--sev:var(--critical)} .sev-warning{--sev:var(--warning)}
.sev-info{--sev:var(--muted)}

/* tables -------------------------------------------------------------- */
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px}
th{text-align:left;font-weight:600;color:var(--muted);font-size:11px;
   letter-spacing:.05em;text-transform:uppercase;
   padding:0 12px 8px 0;border-bottom:1px solid var(--grid);white-space:nowrap}
td{padding:8px 12px 8px 0;border-bottom:1px solid var(--grid);
   vertical-align:top}
td.num{font-variant-numeric:tabular-nums;white-space:nowrap}
tr:last-child td{border-bottom:none}
.where{color:var(--ink-2)}
.val{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;
     color:var(--ink-2);word-break:break-word}
.note{color:var(--muted);font-size:12px;margin-top:10px}
footer{margin-top:48px;padding-top:16px;border-top:1px solid var(--grid);
       color:var(--muted);font-size:12px}
footer code{font-size:11px;word-break:break-all}
"""


def _bar_rows(by_type: list) -> str:
    if not by_type:
        return '<p class="note">No findings in the latest verification run.</p>'
    top = max(r['n'] for r in by_type) or 1
    out = []
    for row in by_type:
        # Scale to 85% so the value at the bar tip always has room; relative
        # proportions are untouched.
        width = max(0.4, row['n'] / top * 85)
        glyph, sev = _sev(SEV_BY_RANK.get(row.get('sev_rank'), 'warning'))
        out.append(
            f'<div class="bar-row">'
            f'<div class="bar-name"><span class="bar-type">{_e(row["drift_type"])}</span>'
            f'<span class="sev sev-{sev}"><span class="g">{glyph}</span> {sev}</span></div>'
            f'<div class="bar-track"><div class="bar" style="width:{width:.2f}%"></div>'
            f'<span class="bar-value">{_n(row["n"])}</span></div>'
            f'</div>')
    return f'<div class="bars">{"".join(out)}</div>'


def _findings_table(drift: dict) -> str:
    findings = drift['findings']
    if not findings:
        return '<p class="note">Nothing to list.</p>'
    rows = []
    for f in findings:
        where = f'{f.get("file_name") or "?"} / {f.get("tab_title") or "?"}'
        at = ' '.join(x for x in (f.get('row_ref'), f.get('column_key')) if x)
        glyph, sev = _sev(f.get('severity') or 'warning')
        values = ''
        if f.get('sheet_value') is not None or f.get('odoo_value') is not None:
            values = (f'sheet={_e(f.get("sheet_value"))}<br>'
                      f'odoo={_e(f.get("odoo_value"))}')
        elif f.get('detail'):
            values = _e(f['detail'])
        rows.append(
            f'<tr><td class="num"><span class="sev sev-{sev}">'
            f'<span class="g">{glyph}</span> {sev}</span></td>'
            f'<td class="num">{_e(f.get("drift_type"))}</td>'
            f'<td class="where">{_e(where)}</td>'
            f'<td class="num">{_e(at) or "&mdash;"}</td>'
            f'<td class="val">{values or "&mdash;"}</td></tr>')

    omitted = ''
    if drift['total'] > len(findings):
        omitted = (f'<p class="note">Showing {len(findings)} of '
                   f'{_n(drift["total"])}. The rest are in the store: '
                   f'<code>python -m driftwatch drift --limit 500</code></p>')
    return (f'<div class="scroll"><table><thead><tr><th>Severity</th>'
            f'<th>Type</th><th>File / tab</th><th>At</th><th>Values</th>'
            f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>{omitted}')


def render(data: dict, refresh: int = 60) -> str:
    """The whole page, self-contained."""
    h = data['health']
    glyph, word = STATE_MARK[h['state']]
    drift = data['drift']

    mail = ('on &rarr; ' + _e(', '.join(data['alert_to']))
            if data['alerts_enabled'] else
            'off &mdash; findings are recorded but not mailed')

    tiles = [
        ('Drive objects', _n(data['corpus']['total']),
         f'{_n(data["corpus"]["spreadsheets"])} spreadsheets'),
        ('Datasets (tabs)', _n(data['datasets']['count']),
         f'{_n(data["datasets"]["rows"])} staged rows'),
        ('Errors', _n(drift['severities'].get('error', 0)),
         'in the latest run'),
        ('Warnings', _n(drift['severities'].get('warning', 0)),
         'in the latest run'),
        ('Incomplete reads', _n(data['datasets']['incomplete']),
         'deletions disarmed'),
        ('Blocked tabs', _n(data['datasets']['blocked']), 'not staged'),
    ]
    tile_html = ''.join(
        f'<div class="card tile"><div class="label">{label}</div>'
        f'<div class="value">{value}</div><div class="foot">{foot}</div></div>'
        for label, value, foot in tiles)

    run_rows = ''.join(
        f'<tr><td class="num">#{_e(r["id"])}</td><td class="num">{_e(r["kind"])}</td>'
        f'<td class="num">{_e(r["status"])}</td>'
        f'<td class="num">{_e(r["started_at"])}</td>'
        f'<td class="num">{_e(r["duration"])}</td></tr>' for r in data['runs'])

    mime_rows = ''.join(
        f'<tr><td class="where">{_e(r["mime_type"])}</td>'
        f'<td class="num">{_n(r["n"])}</td></tr>'
        for r in data['corpus']['by_mime'])

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="{refresh}">
<title>DriftWatch</title><style>{CSS}</style></head>
<body><div class="viz-root">

<h1>DriftWatch</h1>
<p class="sub">Google Drive vs Odoo, read-only. Nothing on this page was
changed in either system.</p>

<div class="card health state-{h['state']}">
  <div class="mark">{glyph}</div>
  <div>
    <div class="word">{word}</div>
    <div class="note">{_e(h['note'])}</div>
    <div class="note">Last cycle {_e(h['finished_at'] or 'never')}
      {'UTC (' + _e(h['ago']) + ')' if h['finished_at'] else ''}
      &middot; every {h['interval'] // 60}m
      &middot; {h['failures']} consecutive failure(s)
      &middot; email {mail}</div>
  </div>
</div>

<div class="hero">
  <div class="value">{_n(drift['total'])}</div>
  <div class="label">findings in verify run
    {('#' + str(drift['run_id'])) if drift['run_id'] else '(none yet)'}</div>
</div>

<div class="tiles">{tile_html}</div>

<h2>Findings by type</h2>
{_bar_rows(drift['by_type'])}
<p class="note">Datasets without a mapping are staging-only &mdash; checked for
internal consistency, never compared to Odoo. Most findings here are structural,
not disagreements with Odoo.</p>

<h2>Findings</h2>
{_findings_table(drift)}

<h2>Recent runs</h2>
<div class="scroll"><table><thead><tr><th>Run</th><th>Kind</th><th>Status</th>
<th>Started (UTC)</th><th>Took</th></tr></thead>
<tbody>{run_rows}</tbody></table></div>

<h2>Corpus</h2>
<div class="scroll"><table><thead><tr><th>Type</th><th>Count</th></tr></thead>
<tbody>{mime_rows}</tbody></table></div>

<footer>
Generated {_e(data['generated_at'])} UTC &middot; refreshes every {refresh}s
&middot; impersonating {_e(data['subject'])} &middot; {_e(data['odoo_url'])}<br>
This page embeds real staged content. It stays on this machine.
</footer>

</div></body></html>"""


# --------------------------------------------------------------------------- #
# entry points
# --------------------------------------------------------------------------- #
def build(conn, cfg, refresh: int = 60) -> str:
    return render(collect(conn, cfg), refresh=refresh)


def write(conn, cfg, path: Path, refresh: int = 60) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build(conn, cfg, refresh), encoding='utf-8')
    return path


def serve(cfg, db_path: Path, host: str, port: int, refresh: int = 30) -> int:
    """Serve the dashboard on the loopback interface only.

    Bound to 127.0.0.1 deliberately: the page contains staged business content
    and there is no authentication in front of it.
    """
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):                       # noqa: N802
            if self.path not in ('/', '/index.html'):
                self.send_error(404)
                return
            try:
                conn = open_readonly(db_path)
                try:
                    body = build(conn, cfg, refresh).encode('utf-8')
                finally:
                    conn.close()
            except Exception as exc:            # noqa: BLE001
                body = (f'<!doctype html><meta charset="utf-8">'
                        f'<title>DriftWatch</title>'
                        f'<body style="font:15px system-ui;padding:32px">'
                        f'<h1>Dashboard unavailable</h1><p>{html.escape(str(exc))}'
                        f'</p></body>').encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):           # keep the console clean
            pass

    server = HTTPServer((host, port), Handler)
    print(f'DriftWatch dashboard on http://{host}:{port}  (Ctrl-C to stop)')
    print('Loopback only -- this page contains staged business content.')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nstopped')
    finally:
        server.server_close()
    return 0
