# `gdrive_odoo_sync` — addon README

Odoo **18.0** addon. Mirrors a Google Workspace Drive into Odoo, stages every spreadsheet tab,
verifies both sides with content hashes, and heals only through an approved plan.

> The **full** administrator walkthrough — including the exhaustive Google Cloud / Workspace
> domain-wide-delegation procedure — lives in the repository-root `README.md`. This file is the
> short operational version for someone who already has the addon in front of them.
>
> The binding contracts are `docs/SPEC.md`, `docs/FILE_MANIFEST.md` and
> `docs/CANONICALIZATION.md`.

---

## ⚠️ A service account sees *nothing* by default

A Google Cloud service account is a **principal**, not a view onto your organisation. It has its
own Drive corpus, and that corpus is empty. With plain, non-delegated credentials:

```python
drive.files().list(q="trashed = false").execute()   # -> {'files': []}
```

returns **an empty list with HTTP 200 and no error**, because `files.list` defaults to
`corpora='user'` and "user" means *the service account*. It cannot see the Workspace user's My
Drive, their Shared-with-me, or anything colleagues shared with *them* — sharing is per-principal.

**An empty read is indistinguishable from "everything was deleted."** That is why this module has
seven delete guards, a run-level `complete_read` flag, and an `EMPTY_TAB` breaker — and why the
correct fix is **domain-wide delegation with subject impersonation**, not a workaround.

```python
creds = base_creds.with_subject(connection.subject_email)   # capture the return value
```

`with_subject()` returns a **new** object and does not mutate. Discarding the result leaves you
authenticating as the bare service account and silently seeing nothing.

---

## Install

```bash
# Dependencies, into the SAME interpreter that runs odoo-bin:
/path/to/venv/bin/python -m pip install -r <repo-root>/requirements.txt

# Install:
odoo-bin -c odoo.conf -d YOURDB -i gdrive_odoo_sync --stop-after-init
```

On Odoo.sh: `requirements.txt` must be at the **repository root**; the build installs it
automatically. Install the app from the Apps menu after the build goes green.

Requires `base` and `mail` only. It deliberately does **not** depend on Enterprise `documents`, so
it installs on Community.

---

## Domain-wide delegation, in brief

Full detail (with every common mistake) is in the root `README.md` §4–§7.

1. **Google Cloud console** → IAM & Admin → Service Accounts → create `gdrive-odoo-sync`.
   Grant it **no** Cloud IAM roles — they govern Cloud resources, not Drive, and would be pure
   downside.
2. **APIs & Services → Library** → enable **Google Drive API** *and* **Google Sheets API**.
3. **Keys → Add key → JSON.** Guard the file: it is a bearer credential for the impersonated
   user's entire Drive.
4. Copy the service account's **numeric OAuth2 Client ID** (~21 digits). It is **not** the
   `…iam.gserviceaccount.com` email. **If what you copied contains an `@`, it is wrong.**
5. **`admin.google.com`** → Security → Access and data control → API controls →
   Domain-wide delegation → **Manage Domain Wide Delegation** → **Add new**. This requires a
   **super administrator** of the Workspace domain.
6. Paste the numeric client ID and this **exact** comma-delimited scope string, with no spaces:

   ```
   https://www.googleapis.com/auth/drive.readonly,https://www.googleapis.com/auth/spreadsheets.readonly
   ```

7. **Authorize.** Propagation is usually minutes; Google documents up to **24 hours**.

The scopes passed in code MUST be a subset of what is authorised here. A mismatch produces
`401 unauthorized_client` at token-exchange time — an error that does not mention scopes at all.
The module therefore hard-codes the pair above and exposes `gdrive.connection.scopes` as
`readonly=True`, so there is no field to get wrong.

Both scopes are read-only by design: with no write scope, v1 is **structurally** incapable of
damaging Drive. The accepted cost is that `_sync_id` cannot be written back, so a dataset without a
`_sync_id` column has natural-key-only identity and its deletes are forced to `report`.

**Domain-wide delegation cannot impersonate an `@gmail.com` consumer account.** Files from such an
account are reachable only because they were shared *with* the Workspace subject — and we are that
subject.

---

## Where the key goes

Resolution order, in `services/google_auth.py::load_service_account_info()`:

1. `os.environ[connection.sa_key_env_var]` — default **`GDRIVE_ODOO_SYNC_SA_KEY`**, holding the raw
   JSON. **Preferred.**
2. `env['ir.config_parameter'].sudo().get_param(connection.sa_key_param_key)` — default
   `gdrive_odoo_sync.sa_key_json`, entered via **Settings → Google Drive Sync**
   (`password="True"`, `groups="base.group_system"`).
3. Otherwise a `UserError` with these instructions.

**Prefer the environment variable.** Odoo.sh database dumps are downloadable and
`ir_config_parameter` values appear in them in cleartext.

Invariants: every `ir.config_parameter` access is `.sudo()`; the key never appears in a data XML
file, on a regular model's field, in a log line, or in a run-line payload — `services/errors.py`'s
`redact()` strips anything containing `-----BEGIN PRIVATE KEY-----` or `"private_key"`. Note that
`set_param` with a falsy value **deletes** the parameter rather than storing `''`; the settings
inverse branches on empty explicitly.

---

## First-run checklist

1. **Google Drive Sync → Configuration → Connections → New.** Leave `auth_mode = dwd`; set
   `subject_email`.
2. Read `sa_client_id` off the form, complete the admin-console grant, wait for propagation.
3. **Test Connection.** Probes P1–P7 report individually. `state` becomes `ok` only when **P1–P4**
   all pass.
   - *P2 passes, P3 fails* ⇒ the client ID or scopes in the admin console are wrong.
   - *P3 passes, P4 returns 0* ⇒ rendered as a **red error**, not an empty state: for a real
     Workspace user, an empty corpus is the signature of a broken grant, and it is exactly what a
     delete planner must never be handed.
4. **Run Discovery**, then let ingest and stage run. Inspect **Drive Nodes** and **Datasets**.
5. Inspect **Staged Rows**. *This is already the whole staging product* — nothing further is
   required, and for most datasets nothing further is desirable.
6. Only for a dataset that genuinely belongs in a business model: **Build Mapping**, set each
   column's `ctype` / `odoo_field` / `is_natural_key` / `assert_string_value`, **Validate**, then
   deliberately set `enabled = True`.
7. Leave `auto_heal = False`. Read the drift reports for at least a week before considering
   otherwise.

---

## Upgrade notes

```bash
odoo-bin -c odoo.conf -d YOURDB -u gdrive_odoo_sync --stop-after-init
```

`post_init_hook` runs on install **and on every upgrade** and does two things:

1. **Clears every cached hash** and flags every connection for a full resync. An upgrade may have
   changed `CANON_VERSION` or a normalization rule; serving a hash computed by the previous
   normalizer as `verified` is a silent false pass — the worst failure a verification system can
   have. Expect the first verify pass after an upgrade to be slow and to consume Sheets quota.
2. **Re-asserts the partial unique index** `<table>_x_gdrive_sync_id_uniq` on every promotion target
   table. That index is what makes each `create` an idempotent upsert, so a retried plan collapses
   into a no-op instead of duplicating records.

All of `data/` ships inside `<data noupdate="1">`, so an administrator's cron intervals, `active`
flags and tuned config parameters **survive** an upgrade. The corollary: a *new shipped default*
does not reach an existing database without a migration or a manual change.

`uninstall_hook` is deliberately conservative — it drops only the indexes this module created. It
leaves `x_gdrive_sync_id` / `x_gdrive_source_dataset` on target models (the only surviving evidence
of which records came from a sheet, and uninstall is often a step in a reinstall), leaves mirrored
attachments alone, and leaves soft-deleted records soft-deleted.

---

## Odoo 18 compatibility rules honoured here

| Rule | Detail |
|---|---|
| List views | `<list>`, never `<tree>`; `view_mode="list,form"`, never `tree,form` |
| Conditional display | Direct `invisible=` / `readonly=` / `required=` expressions; `attrs=` and `states=` are removed and hard-fail |
| Chatter | `<chatter/>` |
| `ir.cron` | **No `numbercall`, no `doall`** — removed in 18; their presence hard-fails install |
| Display name | `_compute_display_name` with `@api.depends`; `name_get()` is removed and silently never called |
| Aggregation | `aggregator='sum'`, not `group_operator='sum'` |
| Access checks | `check_access(op)` / `has_access(op)` / `_filtered_access(op)` |

---

## Tests

```bash
odoo-bin -d testdb -i gdrive_odoo_sync --test-enable \
         --test-tags=/gdrive_odoo_sync --stop-after-init
```

The `lib/` tests are pure — no database, no network — and can be run directly:

```bash
python -m pytest gdrive_odoo_sync/tests/test_lib_*.py -q
```

`tests/__init__.py` must import every test module: one that is not listed is **silently never
run**, which is indistinguishable from one that passes. `tests` is deliberately **not** imported
from this addon's top-level `__init__.py` — Odoo imports it itself, only under `--test-enable`.

---

## Security summary

| Group | Implies | Can |
|---|---|---|
| `group_gdrive_user` | `base.group_user` | Read nodes, datasets, staged rows, verifications, drift |
| `group_gdrive_manager` | user | Author/enable mappings, edit scope rules, trigger runs, **preview** plans |
| `group_gdrive_admin` | manager | Manage connections, **apply** plans, enable `auto_heal` |

Credential entry is additionally gated on `base.group_system` — narrower than
`group_gdrive_admin`. Plan application is guarded in Python via `has_group(...)`, not by ACL alone.
Five global multi-company `ir.rule` records ship and nothing else; global rules cannot be widened by
group membership, which is why they are right for company isolation and wrong for everything else.

**License:** LGPL-3.
