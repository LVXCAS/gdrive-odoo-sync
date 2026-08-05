# Running DriftWatch 24/7

DriftWatch is read-only on both sides. Running it continuously cannot damage
Drive or Odoo — the OAuth scopes are `drive.readonly` and
`spreadsheets.readonly`, and nothing writes to Odoo. The risk of unattended
operation here is not corruption; it is **silence** — a service that stopped
watching and did not say so. Everything below is shaped around noticing that.

## What the daemon does

```
python -m driftwatch daemon --interval 1h
```

One long-lived process. Every cycle: `crawl` → `stage` → `verify`, then decide
whether to mail. Between cycles it sleeps.

- **A failed cycle does not stop the service.** Drive rate limits, an Odoo
  restart and a suspended laptop all look like exceptions and all resolve on
  their own. The failure is logged, the next attempt backs off (60s, doubling,
  never past the interval), and a recovery is logged too.
- **Mail goes out when the set of findings *changes*** — a new drift type, a
  new row, a value that moved, or drift clearing entirely. An hourly mail that
  says the same thing every hour is a mail you stop reading. Identical findings
  from a fresh run are recognised as identical: the fingerprint deliberately
  ignores row ids and timestamps, which change every run by construction.
- **A failed send is retried.** The fingerprint only advances after the SMTP
  server accepts the message, so a mail outage delays the digest rather than
  losing it.
- **Shutdown is graceful.** SIGINT/SIGTERM/Ctrl-Break finish the current phase,
  then exit — no half-written store.

Why a loop in-process rather than letting the OS scheduler call `sync` every
hour: the service keeps state that only makes sense in sequence — the Drive
change cursor, the digest fingerprint, the consecutive-failure count. A fresh
process each hour has no memory of whether the last run failed, so it cannot
back off, and two slow runs can overlap and contend for the same SQLite file.
The OS still supervises; it just only has to keep one process alive.

## Install on Windows

```powershell
# 1. Configure
copy .env.example .env
notepad .env

# 2. Prove both connections before trusting either
& 'C:\Program Files\Python312\python.exe' -m driftwatch probe

# 3. Prove the mail path before walking away from it
& 'C:\Program Files\Python312\python.exe' -m driftwatch alert-test

# 4. Register the service (as Administrator for true 24/7)
powershell -ExecutionPolicy Bypass -File scripts\install-service.ps1
```

Steps 2 and 3 are not optional ceremony. An SMTP problem discovered the first
time there is real drift is discovered too late, and an empty Drive corpus —
the signature of a broken delegation grant — is indistinguishable from
"everything was deleted" if you meet it for the first time in a log at 3am.

### Elevated vs not

| Run as | Trigger | Runs when signed out? |
|---|---|---|
| Administrator | at boot, `S4U` logon | **Yes** — true 24/7 |
| Normal user | at sign-in | No — stops when you sign out |

The install script detects which it got and tells you which one you have. It
does not silently register the weaker option and call it done.

`-Force` replaces an existing task. Other switches (`-Interval 15m`,
`-StagingOnly`, `-Incremental`, `-Mapping path.json`, `-TaskName`) pass through
to the daemon.

### Why a scheduled task and not a real Windows service

A real service must speak the Service Control Manager protocol, which for a
Python program means `pywin32` plus a wrapper — two more dependencies and a
second thing to misconfigure. Task Scheduler starts a plain process at boot,
restarts it on failure, and is inspectable from a GUI that is already on the
machine. Since the daemon supervises its own cycles, the OS only has to keep
the process alive.

The task carries three protections: restart 3× at 1-minute intervals if the
process dies, a 15-minute repeating trigger as a backstop if that budget is
spent, and `MultipleInstances = IgnoreNew` so the backstop is a no-op while
the daemon is healthy.

## Install on Linux

`deploy/driftwatch.service` is a systemd unit — edit the user and paths, then:

```bash
sudo cp deploy/driftwatch.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now driftwatch
journalctl -u driftwatch -f
```

## Is it alive?

```
python -m driftwatch status
```

The first lines now report the daemon heartbeat — when the last cycle
finished, whether it succeeded, and the consecutive-failure count. It reads
from the store, so it works from any shell without parsing a log.

```
Get-Content logs\driftwatch.log -Tail 40 -Wait      # follow the log
Get-ScheduledTask DriftWatch | Get-ScheduledTaskInfo
```

Logs rotate daily and keep 14 days (`DRIFTWATCH_LOG_RETAIN`).

**The check worth automating:** if `daemon_cycle_finished_at` is older than a
few intervals, the service is not watching. That is the failure mode that
matters, and it is invisible from the inbox — a service that has stopped sends
no mail, which looks exactly like no drift.

## Email setup

Gmail needs an **App Password** (`myaccount.google.com/apppasswords`), not the
account password, and 2-Step Verification must be on. `DRIFTWATCH_ALERT_FROM`
should be the authenticated mailbox — most providers reject a `From:` that is
not.

Leaving `DRIFTWATCH_SMTP_HOST` or `DRIFTWATCH_ALERT_TO` empty turns alerting
off without stopping the service; findings are still recorded and logged, and
the fingerprint still advances, so switching mail on later sends the *current*
state rather than replaying a backlog.

## Operational notes

- **The datastore is not a cache.** `driftwatch.sqlite3` holds a copy of real
  staged Drive content. Keep it on the machine that produced it and out of any
  synced folder. It is gitignored; leave it that way.
- **Interval.** Drive's incremental change feed makes short intervals cheap,
  but `stage` re-reads tabs. Start at `1h`; drop to `15m` once you know the
  rate limits are comfortable, using `--incremental`.
- **Without a mapping every dataset is staging-only** — crawled, hashed and
  checked for internal consistency, but not compared to Odoo. That is the
  default and it needs no configuration. `--staging-only` skips Odoo entirely.
- **Drift rows accumulate across runs.** The digest and the heartbeat both
  scope to the latest verify run; `status` and `drift` show all history.

## Tests

```
python -m unittest discover -s driftwatch/tests -t .
```

No network, no Odoo, no extra packages — they run on the host machine as-is.
