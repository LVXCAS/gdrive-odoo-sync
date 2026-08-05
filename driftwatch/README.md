# DriftWatch

Crawls Google Drive, stages every spreadsheet tab, and verifies that what is in
Drive matches what is in Odoo. Reports differences. Changes nothing.

Runs as a standalone Python service — **no Odoo addon required**, which is what
makes it usable against Odoo Online (SaaS), where custom addons cannot be
installed.

## Why it is read-only

The Google OAuth scopes are `drive.readonly` and `spreadsheets.readonly`, so
the service structurally cannot modify Drive. Nothing writes to Odoo either.
A tool that changes one of two systems while measuring whether they agree
cannot answer the question it was built to answer.

## Setup

```
pip install -r ../requirements.txt
```

Create `../.env` — copy `../.env.example`, which documents every setting:

```
DRIFTWATCH_SA_KEY=C:\path\to\service-account-key.json
DRIFTWATCH_SUBJECT=user@yourdomain.com
ODOO_URL=https://yourdb.odoo.com
ODOO_DB=yourdb
ODOO_LOGIN=user@yourdomain.com
ODOO_API_KEY=...
```

The service account needs domain-wide delegation authorized in the Workspace
admin console for its **numeric** OAuth2 client ID (not its
`…iam.gserviceaccount.com` email — that mix-up is the most common setup error),
with exactly these scopes:

```
https://www.googleapis.com/auth/drive.readonly,https://www.googleapis.com/auth/spreadsheets.readonly
```

Domain-wide delegation cannot impersonate consumer `@gmail.com` accounts.

## Use

```
python -m driftwatch probe             # prove both connections before trusting either
python -m driftwatch crawl             # walk Drive, record every object
python -m driftwatch stage --limit 25  # read spreadsheet tabs, canonicalize, hash
python -m driftwatch verify            # compare against Odoo, report drift
python -m driftwatch status            # what is in the local store
python -m driftwatch drift --type duplicate_identity
python -m driftwatch sync              # all of the above
```

## Running it unattended

```
python -m driftwatch daemon --interval 1h   # sync on a loop, mail on drift
python -m driftwatch alert-test             # prove SMTP before trusting it
python -m driftwatch status                 # includes the daemon heartbeat
python -m driftwatch dashboard --open       # the whole picture, on one page
python -m driftwatch dashboard --serve      # live at 127.0.0.1:8787
```

One long-lived process. A failed cycle backs off and retries rather than
stopping the service, and the drift digest is mailed when the *set of
findings changes* — not every cycle, because an hourly mail that says the
same thing every hour is a mail you stop reading.

The digest and the dashboard both report the **whole cycle** — crawl, stage
and verify — because staging records drift of its own, and `type_coercion` is
recorded nowhere else. Where both phases see the same thing, the verifier's
copy is the one reported, so nothing is counted twice.

`scripts/install-service.ps1` registers it as a Windows scheduled task;
`deploy/driftwatch.service` is the systemd equivalent. See
[DEPLOY.md](DEPLOY.md).

## How verification works

Each spreadsheet **tab** is its own dataset. Cells are canonicalized — text
trimmed and normalized, currency symbols and thousands separators stripped,
dates and booleans normalized — then hashed per cell, per row, per bucket, and
per dataset. Comparing one dataset hash answers "did anything change at all" in
a single step; only when digests differ does it walk rows to find *which* ones.

Both sides are canonicalized with the same rules before comparison. Comparing a
raw Odoo value against a canonical sheet token produces confident, well
formatted, wrong answers.

## The safety properties

These are the reason the code is shaped the way it is:

- **An incomplete read is never treated as deletion.** If a crawl misses a page,
  a Sheets read truncates, or an Odoo snapshot hits a limit, the comparison is
  refused outright. A short read looks exactly like mass deletion.
- **An empty tab is a signal, not an instruction.** Zero rows where there were N
  is the shape of a renamed tab, a revoked grant, or a failed read. Rows are
  left as they were rather than replaced with nothing. The refusal is
  unconditional, but the *report* distinguishes the two cases: a tab that has
  always been empty is `info`, and only a tab that lost rows it previously had
  is an `error`. A corpus full of blank `Sheet2` tabs would otherwise bury the
  one tab that just lost twenty thousand rows. The baseline only advances on a
  complete read, so a truncated one cannot erase it and mask the next real
  deletion. Both phases that can report an empty tab grade it the same way —
  the rule lives in `drift_rules.py` precisely so it cannot be applied in one
  phase and forgotten in the other.
- **Excel lock files are not spreadsheets.** Drive reports `~$Book.xlsx` under
  a spreadsheet MIME type; it is a few hundred bytes of Office lock metadata.
  They are skipped rather than parsed and failed on every cycle, because a
  permanent error nobody can act on is what teaches a reader to skim past the
  error list where a genuinely corrupt upload is waiting.
- **Identifier columns are never numerically coerced.** Sheets turns `0012345`
  into `12345` given the chance. Identifier-looking headers stay text, and a
  numeric value in one is quarantined as `type_coercion` rather than staged.
- **Formula cells with no cached value disarm deletions** for that dataset. An
  `.xlsx` written by a tool that never evaluated its formulas is not evidence
  about anything.
- **Records nobody synced are `unmanaged`** — reported, never reconciled. A
  record a human typed into Odoo is not this tool's to change.

## Drift types

`missing_in_odoo`, `missing_in_sheet`, `field_mismatch`, `duplicate_identity`,
`header_change`, `empty_tab`, `type_coercion`, `multi_match`,
`unmanaged_record`, `read_incomplete`, `identifier_numeric`, `schema_growth`

## Mappings

Without a mapping a dataset is **staging-only**: crawled, hashed, and checked
for internal consistency, but not compared to Odoo. That is the default and it
requires no configuration.

To compare a dataset against an Odoo model, pass `--mapping` a JSON file:

```json
{
  "12": {
    "model": "res.partner",
    "domain": [["is_company", "=", true]],
    "key_column": "email",
    "key_field": "email",
    "columns": {"name": "name", "phone": "phone"}
  }
}
```

Keys are dataset ids from `status`. Automatic key inference is deliberately
absent here — it guesses, and a wrong identity column produces thousands of
confident false findings.

## Layout

| Path | What |
|---|---|
| `lib/` | Canonicalization and Merkle hashing. Copied verbatim from the Odoo addon; zero dependencies on either platform. |
| `services/` | Google auth (domain-wide delegation), Drive discovery, changes cursor, Sheets and xlsx readers, retry, rate limiting. Also verbatim. |
| `config.py` | Environment and `.env` resolution. |
| `drift_rules.py` | Rules more than one phase applies, so they cannot disagree. |
| `store.py` | SQLite schema and all persistence. |
| `crawler.py` | Drive walk. |
| `stager.py` | Tabs to canonical hashed rows. |
| `odoo_client.py` | XML-RPC, standard Odoo models only. |
| `verifier.py` | The comparison engine. |
| `cli.py` | Command line. |
| `daemon.py` | The supervised loop: cycle, back off, stay alive. |
| `notify.py` | Drift digest — fingerprint, rendering, SMTP. |
| `dashboard.py` | The local one-page view. Read-only; never published. |
| `tests/` | Service tests. Stdlib `unittest`; no network, no Odoo. |

`lib/` and `services/` are byte-identical to their copies in the
`gdrive_odoo_sync` Odoo addon. If Odoo.sh ever becomes available, that addon
installs and reuses the same engine.

## The datastore holds real business data

`driftwatch.sqlite3` is a copy of staged Drive content, not a cache. It is
gitignored and should stay on the machine that produced it.
