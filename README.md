# `gdrive_odoo_sync` — Google Drive → Odoo Sync & Verification

A real Odoo 18 addon (not an external script) that mirrors a Google Workspace Drive into Odoo,
stages every spreadsheet tab, proves both sides agree using content hashes, and heals Odoo only
through a plan a human approved.

- **Direction:** Google Drive → Odoo. Drive is the source of truth. Odoo never writes to Drive in v1.
- **Target:** Odoo **18.0**, Community *or* Enterprise, self-hosted or Odoo.sh.
- **Addon path:** [`gdrive_odoo_sync/`](gdrive_odoo_sync/)
- **Binding specs:** [`docs/SPEC.md`](docs/SPEC.md), [`docs/FILE_MANIFEST.md`](docs/FILE_MANIFEST.md), [`docs/CANONICALIZATION.md`](docs/CANONICALIZATION.md)

---

## ⚠️ Read this first: a service account sees *nothing* by default

This is the single most dangerous failure mode in the entire system, and the one thing almost
everybody gets wrong on the first attempt.

A Google Cloud service account is **a principal, not a view onto your organisation**. It has its
own Drive corpus. That corpus is empty, and since 1 June 2023 it has a **0 GB storage quota**, so
it cannot even create files.

With plain, non-delegated service-account credentials:

```python
creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
drive = build('drive', 'v3', credentials=creds, cache_discovery=False)
drive.files().list(q="trashed = false").execute()   # -> {'files': []}
```

You get **an empty list**, with **HTTP 200** and **no error**. Not a permission denial. Not a
warning. A perfectly successful response describing an empty universe.

That happens because `files.list` defaults to `corpora='user'`, and "user" means *the service
account*. The service account cannot see:

- your Workspace user's My Drive,
- your Workspace user's "Shared with me",
- folders that colleagues shared with *your user* — sharing is per-principal, and the service
  account is a different principal.

**An empty read is indistinguishable from "everything was deleted."** That is precisely why this
module has seven independent delete guards, a run-level `complete_read` flag, and an `EMPTY_TAB`
circuit breaker. But guards are a second line of defence. The first line is configuring
**domain-wide delegation** correctly, which is what the rest of this document is mostly about.

The fix in one line:

```python
creds = base_creds.with_subject('lucaso@avatarnaturalfoods.com')   # capture the return value!
```

`with_subject()` returns a **new** credentials object and does **not** mutate the original. Writing
`base_creds.with_subject(...)` without assigning the result leaves you authenticating as the bare
service account and silently seeing nothing at all.

---

## Table of contents

1. [What the system does](#1-what-the-system-does)
2. [Requirements](#2-requirements)
3. [Installation](#3-installation)
4. [Google Cloud: create the service account](#4-google-cloud-create-the-service-account)
5. [Google Cloud: enable the Drive and Sheets APIs](#5-google-cloud-enable-the-drive-and-sheets-apis)
6. [Google Cloud: create and download the JSON key](#6-google-cloud-create-and-download-the-json-key)
7. [Workspace admin: domain-wide delegation](#7-workspace-admin-domain-wide-delegation--the-step-everyone-gets-wrong)
8. [Where the key is stored, and where it must never go](#8-where-the-key-is-stored-and-where-it-must-never-go)
9. [Configure the connection in Odoo](#9-configure-the-connection-in-odoo)
10. [Test Connection: probes P1–P7 and what each failure means](#10-test-connection-probes-p1p7-and-what-each-failure-means)
11. [First run: discovery, ingest, staging](#11-first-run-discovery-ingest-staging)
12. [Promotion: mapping staged rows into business models](#12-promotion-mapping-staged-rows-into-business-models)
13. [Verification, drift and healing](#13-verification-drift-and-healing)
14. [Scheduled actions](#14-scheduled-actions)
15. [Security model](#15-security-model)
16. [Upgrades](#16-upgrades)
17. [Running the tests](#17-running-the-tests)
18. [Troubleshooting reference](#18-troubleshooting-reference)
19. [Explicit non-goals for v1](#19-explicit-non-goals-for-v1)

---

## 1. What the system does

Eight pipeline stages, each independently invocable, independently logged, and idempotent:

```
discover → classify → ingest → stage → promote → verify → report → heal
```

| Stage | What happens |
|---|---|
| **discover** | Enumerates every Drive file and folder visible to one impersonated Workspace identity — My Drive, subfolders, items shared *with* that identity by colleagues and by outside consumer accounts, and every shared drive the identity can reach. Delta runs replay the Drive Changes API; full runs re-enumerate. |
| **classify** | Sorts each object by MIME type into folder, native Google Sheet, native Doc/Slides/Drawing, binary blob (`.xlsx`, `.pdf`, `.jpeg`, `.docx`…), shortcut, or unsupported. |
| **ingest** | Mirrors non-spreadsheet content into `ir.attachment`, preserving the Drive folder hierarchy as a `gdrive.node` tree. Docs/Slides/Drawings export to PDF; Docs also get a `text/plain` extraction. Native Sheets are deliberately **not** exported. |
| **stage** | Reads every *tab* of every spreadsheet — native Google Sheets **and** uploaded `.xlsx` — into `gdrive.staged.row`, one dataset per tab, each with its own observed header schema. This happens for every tab always; it is never opt-in. |
| **promote** | Writes staged rows into real business models (`res.partner`, `crm.lead`, …) **only** where an administrator authored and enabled a declarative mapping. Never guessed. Never automatic. |
| **verify** | Computes an order-insensitive, bucketed-Merkle content hash on both the Drive side and the Odoo side and drills into individual rows only where the hashes disagree. |
| **report** | Records drift as first-class Odoo records plus a downloadable JSON + HTML artefact, and posts a digest to the connection's chatter. |
| **heal** | Brings Odoo back in line with Drive — but only through an explicit, fingerprint-guarded plan, dry-run by default, with per-mapping auto-heal that ships **off**. |

### Identity rules that pervade everything

- A Drive file is identified by its **file id**, never its title. Two files may legitimately have
  the same name; titles are display-only strings.
- A spreadsheet tab is identified by its numeric **`sheetId` (gid)**, never its title. Renaming a
  tab is a rename, not a delete.
- A staged row is identified by a **ULID `_sync_id`** where available, falling back to a **declared
  natural key**. Never by row position. Never by a hash of mutable content.

### Report-first is the default posture

Promotion is opt-in per dataset and ships disabled. Auto-heal is opt-in per mapping and ships
disabled. A deployment that never enables either is a **complete, correct, useful product**: it
mirrors your Drive, stages every spreadsheet tab, and tells you every day whether Odoo and Drive
still agree — without writing a single business record.

---

## 2. Requirements

| | |
|---|---|
| **Odoo** | 18.0 (Community or Enterprise). The module depends only on `base` and `mail`; it does **not** depend on Enterprise `documents`. |
| **Python** | 3.10+ (Odoo 18's own floor). |
| **Python packages** | `google-api-python-client`, `google-auth`, `google-auth-httplib2` — see [`requirements.txt`](requirements.txt). `openpyxl` already ships with Odoo. |
| **PostgreSQL** | Whatever your Odoo 18 runs on. The module creates one partial unique index per promotion target table. |
| **Google** | A **Google Workspace** domain (here `avatarnaturalfoods.com`) whose **super administrator** can authorise domain-wide delegation, plus a Google Cloud project. |

> **Consumer accounts cannot be impersonated.** Domain-wide delegation only works for users in
> *your* Workspace domain. An `@gmail.com` address (e.g. `lvxxcas@gmail.com`) will never be a valid
> impersonation subject. Files owned by such an account reach this system only because they were
> shared **with** the Workspace user we impersonate — and we are that user, so we see them.

### Import names vs pip names

These are different strings and confusing them is a common install bug:

| pip name (in `requirements.txt`) | import name (in `__manifest__.py`) |
|---|---|
| `google-api-python-client` | `googleapiclient` |
| `google-auth` | `google.oauth2` |
| `google-auth-httplib2` | *(not imported directly — it is the transport adapter `discovery.build()` selects)* |
| *(ships with Odoo)* | `openpyxl` |

`requests` is deliberately **not** in `requirements.txt`: Odoo pins its own version, and re-pinning
it here is how you get a dependency-resolver conflict that fails an entire Odoo.sh build.

---

## 3. Installation

### 3.1 Self-hosted

```bash
# 1. Put the addon on the addons path.
cd /opt/odoo/custom-addons
git clone <this-repo> gdrive-odoo-sync
# The addons path entry is the directory that CONTAINS gdrive_odoo_sync/,
# i.e. /opt/odoo/custom-addons/gdrive-odoo-sync — not the addon directory itself.

# 2. Install the Python dependencies into the SAME interpreter that runs odoo-bin.
#    Getting this wrong is the #1 cause of "external dependency not satisfied"
#    on a machine where `pip list` clearly shows the package.
sudo -u odoo /opt/odoo/venv/bin/python -m pip install -r gdrive-odoo-sync/requirements.txt

# 3. Point Odoo at it.
#    In odoo.conf:
#        addons_path = /opt/odoo/odoo/addons,/opt/odoo/custom-addons/gdrive-odoo-sync

# 4. Install the module.
sudo -u odoo /opt/odoo/venv/bin/python odoo-bin \
     -c /etc/odoo/odoo.conf -d YOURDB -i gdrive_odoo_sync --stop-after-init

# 5. Restart the service.
sudo systemctl restart odoo
```

### 3.2 Odoo.sh

1. Push this repository to the branch backing your Odoo.sh project. `requirements.txt` **must** be
   at the repository root — Odoo.sh installs it automatically at build time and will not find it
   anywhere else.
2. Wait for the build to go green. If it fails, open the build log and check the pip step before
   anything else.
3. Set the service-account key as a **project environment variable** rather than pasting it into
   the database — see [§8](#8-where-the-key-is-stored-and-where-it-must-never-go).
4. Install the module from **Apps** (update the app list first if it does not appear).

### 3.3 Verify the install

- Menus appear under **Google Drive Sync**.
- **Settings → Technical → Automation → Scheduled Actions** shows seven `GDrive Sync: …` actions.
- **Settings → Users & Companies → Groups** shows *Google Drive Sync / User*, *… / Manager*,
  *… / Administrator*.

Nothing will actually sync yet, because there is no connection and no credential. That is next.

---

## 4. Google Cloud: create the service account

You need the **Google Cloud console** (`console.cloud.google.com`) for steps 4–6, and the
**Workspace admin console** (`admin.google.com`) for step 7. They are different consoles with
different URLs and different permission models; you may well need two different accounts.

1. Go to <https://console.cloud.google.com/> and select (or create) a project. Any project works —
   the service account does not need to live in the same project as anything else.
2. **IAM & Admin → Service Accounts → + Create service account**.
3. **Service account name:** `gdrive-odoo-sync`.
   **Service account ID:** accept the generated value (e.g. `gdrive-odoo-sync`); it becomes the
   email `gdrive-odoo-sync@<project-id>.iam.gserviceaccount.com`.
   **Description:** something a future administrator will understand, e.g.
   `Reads Drive + Sheets as lucaso@avatarnaturalfoods.com for the Odoo sync module.`
4. Click **Create and continue**.
5. **"Grant this service account access to project" — SKIP IT.** Click **Continue** without
   selecting any role.

   > **Why no roles.** Cloud IAM roles govern access to *Google Cloud* resources (buckets, VMs,
   > BigQuery). They have nothing whatsoever to do with Drive file access, which is governed by
   > Drive's own sharing model and — for us — by domain-wide delegation. Granting this service
   > account `Editor` or `Owner` on the project would give it real power over your Cloud resources
   > and would still not let it read a single Drive file. It is a pure downside.

6. **"Grant users access to this service account" — skip that too.** Click **Done**.

You now have a service account that can do absolutely nothing. That is correct.

---

## 5. Google Cloud: enable the Drive and Sheets APIs

An API that is not enabled on the project returns `403 SERVICE_DISABLED` — a message that mentions
neither Drive nor delegation and sends people hunting through the admin console for hours.

1. **APIs & Services → Library**.
2. Search for **Google Drive API** → **Enable**.
3. Search for **Google Sheets API** → **Enable**.

Both are required. Drive alone is not enough: native Google Sheets are read through the Sheets API,
never through `files.export` (see the box below).

> **Why native Sheets are never exported.** `files.export` hard-fails at 10 MB with
> `403 exportSizeLimitExceeded`, and chunked download does not help because the limit is on the
> *generated artefact*, not the transfer. Worse, exporting a multi-tab spreadsheet to `text/csv`
> silently returns **only the first tab** — a data-loss bug that produces no error at all. The
> Sheets API has neither limitation, so it is the only supported path.

You do **not** need to configure an OAuth consent screen. That is for user-consent OAuth flows;
service accounts with domain-wide delegation do not use one.

---

## 6. Google Cloud: create and download the JSON key

1. **IAM & Admin → Service Accounts →** click `gdrive-odoo-sync`.
2. Open the **Keys** tab → **Add key → Create new key → JSON → Create**.
3. A `.json` file downloads. **This is the only copy.** Google cannot re-issue it; you can only
   create a new key and delete the old one.

Treat this file as what it is: **a bearer credential for the entire Google Drive of the user you
are about to let it impersonate.** Anyone holding it can read every file that user can read, from
anywhere, without a password and without an MFA prompt.

- Do not commit it to git.
- Do not paste it into a ticket, a chat message, or an email.
- Do not store it in a shared drive.
- Rotate it if it is ever exposed: create a new key, deploy it, then **delete** the old key from
  this same Keys tab. Deleting the key is what actually revokes it.

### 6.1 Copy the numeric OAuth2 Client ID — *not* the email

While you are on the service account's detail page, open the **Details** tab and copy the
**Unique ID** / **OAuth 2 Client ID**. It is a **~21-digit number**, for example:

```
114872365981274650931
```

You will paste this in step 7. It is also present in the JSON key file as the `client_id` field.

> **This is the single most common setup error in the entire process.** People paste the
> `gdrive-odoo-sync@<project>.iam.gserviceaccount.com` **email** into the domain-wide delegation
> screen instead of the numeric client ID. The admin console accepts it without complaint. Nothing
> works, and the resulting error at token-exchange time is `401 unauthorized_client`, which
> mentions neither the client ID nor the scopes.
>
> Rule of thumb: **if what you pasted contains an `@`, it is wrong.**

The connection form in Odoo displays both `sa_client_email` and `sa_client_id` read-only, derived
from the loaded key, precisely so you can copy the right one without opening the JSON by hand.

---

## 7. Workspace admin: domain-wide delegation — the step everyone gets wrong

Everything up to here happened in Google Cloud. This step happens in the **Workspace admin
console** and **must be performed by a super administrator of the `avatarnaturalfoods.com`
domain**. A delegated admin with only "Services" rights typically cannot see this screen.

### 7.1 The navigation path

```
admin.google.com
  → Security
    → Access and data control
      → API controls
        → Domain-wide delegation   (button: "Manage Domain Wide Delegation")
          → Add new
```

Google has moved this page several times. If the path above does not match your console, search the
admin console for **"domain-wide delegation"** — the direct URL is usually
`https://admin.google.com/ac/owl/domainwidedelegation`.

### 7.2 What to enter

**Client ID:** the ~21-digit number from [§6.1](#61-copy-the-numeric-oauth2-client-id--not-the-email).
No `@`. No quotes. No spaces.

**OAuth scopes:** paste this **exact** comma-delimited string, with **no spaces after the commas**:

```
https://www.googleapis.com/auth/drive.readonly,https://www.googleapis.com/auth/spreadsheets.readonly
```

Then click **Authorize**.

### 7.3 Scope discipline — why the exact string matters

The scopes passed to `from_service_account_info(..., scopes=…)` in code MUST be a **subset** of what
is authorised here. A mismatch produces:

```
401 unauthorized_client
```

at token-exchange time — a confusing error that **does not mention scopes at all**.

Because that error is so unhelpful, this module removes the possibility of drift: it **hard-codes**
the pair above and exposes `gdrive.connection.scopes` as `readonly=True`. There is no field for you
to get wrong. Paste the string above, exactly, and the code will match it.

Common ways to get this wrong:

| Mistake | Symptom |
|---|---|
| Pasted the service-account **email** instead of the numeric client ID | `401 unauthorized_client`, forever |
| Added a space after the comma | Some consoles trim it, some do not; when they do not, the second scope is authorised as `" https://…"` and never matches |
| Used `drive` or `drive.file` instead of `drive.readonly` | `drive` is a *superset* and works, but grants write access this module deliberately never wants. `drive.file` is a **subset** restricted to files the app created — which for a service account is nothing at all |
| Omitted the Sheets scope | Discovery and attachments work; every spreadsheet read fails with `403` |
| Authorised the scopes on the wrong client ID (an old key, a different service account) | `401 unauthorized_client` |
| Performed this in the **Cloud** console instead of the **admin** console | There is no such screen in the Cloud console; if you are on `console.cloud.google.com`, you are in the wrong place |

### 7.4 Both scopes are read-only, on purpose

`drive.readonly` and `spreadsheets.readonly` are the *only* scopes this module ever requests.

That is a structural guarantee, not a policy: with no write scope, **v1 physically cannot damage
your Drive**, no matter what bug exists in the code. There is no configuration flag, no advanced
mode, and no support procedure that turns on writing.

The accepted, deliberate cost of that guarantee is that `_sync_id` **cannot be written back** into
sheets. Consequence, stated plainly: for a dataset with no `_sync_id` column, identity is
natural-key-only, and **deletes are automatically disabled** for that mapping (`delete_policy` is
forced to `report`). A typo fix in a key column would otherwise read as delete + create.

### 7.5 Propagation

Grants usually take effect within a few minutes, but **Google documents up to 24 hours**. If
[§10](#10-test-connection-probes-p1p7-and-what-each-failure-means) probe P3 fails immediately after
you authorise, wait 15 minutes and run it again before you change anything. Changing settings while
propagation is in flight is how people end up with three half-configured grants.

### 7.6 The fallback: explicit sharing (`sa_direct`)

If you genuinely cannot get domain-wide delegation authorised, the module supports a degraded mode:
set `auth_mode = 'sa_direct'` on the connection and have a human **share each folder** with the
service account's `…iam.gserviceaccount.com` email address. The crawler then works from
`sharedWithMe = true` roots.

Understand what you lose:

- Every newly created folder must be shared again, by hand, forever.
- Items shared with *your user* by third parties — notably consumer `@gmail.com` accounts — are
  **unreachable**. They landed in your user's Shared-with-me, not the service account's.
- You are one forgotten share away from a dataset that looks empty. See the warning at the top of
  this file about what "empty" looks like to a delete planner.

Use it for degraded operation, not as a destination.

---

## 8. Where the key is stored, and where it must never go

Resolution order, implemented once in `services/google_auth.py::load_service_account_info()`:

1. **Environment variable** named by `connection.sa_key_env_var` — default
   `GDRIVE_ODOO_SYNC_SA_KEY` — holding the raw JSON. **Preferred.**
2. **`ir.config_parameter`** under `connection.sa_key_param_key` — default
   `gdrive_odoo_sync.sa_key_json`.
3. Otherwise: a `UserError` containing these setup instructions.

### 8.1 Prefer the environment variable

**Odoo.sh database dumps are downloadable, and `ir_config_parameter` values appear in them in
cleartext.** Anyone who can download a backup — a developer, a consultant, a compromised CI job —
gets the key. An environment variable is not in the dump.

On Odoo.sh: **Settings → Environment variables** on the project, then restart. On systemd:

```ini
# /etc/systemd/system/odoo.service.d/override.conf
[Service]
Environment="GDRIVE_ODOO_SYNC_SA_KEY={\"type\":\"service_account\",\"project_id\":\"…\",…}"
```

Then `sudo systemctl daemon-reload && sudo systemctl restart odoo`.

> Escaping tip: the JSON contains `"` and, inside `private_key`, literal `\n` sequences. Both
> survive a systemd `Environment=` line only if the value is quoted and the inner quotes are
> escaped, as above. If the module reports "could not parse the service account key", the escaping
> is the first thing to check — feed the value to `python -c "import json,os;json.loads(os.environ['GDRIVE_ODOO_SYNC_SA_KEY'])"`.

### 8.2 The `ir.config_parameter` fallback

**Settings → Google Drive Sync → Service account JSON.** The field is rendered `password="True"` and
gated `groups="base.group_system"` (Settings / Technical) — strictly narrower than the module's own
administrator group. Paste the whole JSON file contents.

Note the ORM behaviour this module handles explicitly: **`set_param` with a falsy value *deletes*
the parameter** rather than storing an empty string. Clearing the settings field therefore removes
the key entirely, which is the intended semantics, but it means "I blanked it and it still works"
usually means the environment variable is set and winning at step 1.

### 8.3 The invariants

1. The key is read from an env var first, `ir.config_parameter` second.
2. Every `ir.config_parameter` access is `.sudo()`. The model is restricted to `base.group_system`;
   a cron or a manager user without sudo raises `AccessError`.
3. The key **never** appears in a data XML file, **never** on a regular model's field, **never** in
   a log line, and **never** in a `gdrive.sync.run.line.payload`. `services/errors.py::redact()`
   strips anything containing `-----BEGIN PRIVATE KEY-----` or `"private_key"` from every error and
   log string this module emits.
4. Do not put the key on a model with a form view. Form views get exported, duplicated, and
   screenshotted.

---

## 9. Configure the connection in Odoo

**Google Drive Sync → Configuration → Connections → New.**

| Field | Value | Notes |
|---|---|---|
| `name` | e.g. `Avatar — lucaso@` | Human label only. |
| `auth_mode` | `dwd` | Leave it. `sa_direct` is the degraded fallback of [§7.6](#76-the-fallback-explicit-sharing-sa_direct). |
| `subject_email` | `lucaso@avatarnaturalfoods.com` | The Workspace identity impersonated. Must be a real user in your domain. |
| `sa_key_env_var` | `GDRIVE_ODOO_SYNC_SA_KEY` | Change only if you run several connections against different keys. |
| `sa_client_email` | *(read-only, derived)* | The `…iam.gserviceaccount.com` address — the one to use for explicit sharing. |
| `sa_client_id` | *(read-only, derived)* | The numeric client ID for [§7.2](#72-what-to-enter). |
| `scopes` | *(read-only)* | The frozen read-only pair. Not editable by design. |
| `include_shared_with_me` | `True` | Items colleagues and outside accounts shared with the subject. |
| `include_shared_drives` | `True` | Shared drives the subject can access. |
| `corpora_mode` | `per_drive` | Iterates `drives.list` and issues one `corpora='drive'` query each — the only mode immune to `incompleteSearch`. |
| `max_blob_bytes` | `104857600` (100 MB) | Larger blobs are *recorded* but not downloaded (`state='skipped'`, reason `too_large`). |
| `sheets_reads_per_min` | `50` | Client-side token bucket. Google's hard cap is 60/min/user, shared with everything else acting as this user. |
| `drive_units_per_min` | `200000` | Against a 325 000/min/user ceiling. |
| `max_retry_attempts` | `8` | Truncated exponential backoff with jitter, honouring `Retry-After`. |

Save. `state` is `draft` until a connection test passes.

---

## 10. Test Connection: probes P1–P7 and what each failure means

Press **Test Connection** on the connection form. The wizard runs seven probes **in order** and
reports each one individually — deliberately, so a failure tells you *which* configuration step is
wrong rather than just "it does not work".

| Probe | What it checks | Passes when |
|---|---|---|
| **P1** Key parses | `json.loads` of the resolved key | `client_email`, `client_id`, `private_key` all present |
| **P2** Token mints | `creds.refresh(Request())` | No `unauthorized_client` |
| **P3** Impersonation works | `drive.about().get(fields='user(emailAddress),storageQuota')` | Returned `user.emailAddress` **equals** `subject_email` |
| **P4** Corpus is non-empty | `files.list(q="trashed = false", pageSize=10)` | `len(files) > 0` |
| **P5** Shared-with-me reachable | `files.list(q="sharedWithMe = true and trashed = false")` | Reported; legitimately may be 0 |
| **P6** Shared drives enumerable | `drives.list(pageSize=100)` | Reported |
| **P7** Sheets API reachable | `spreadsheets.get` on the first sheet found | HTTP 200 |

`gdrive.connection.state` becomes `ok` only when **P1–P4 all pass**.

### Reading the failures

| Symptom | Diagnosis | Fix |
|---|---|---|
| **P1 fails** | The key was not found, or is not valid JSON | Check the env var name matches `sa_key_env_var`; check shell escaping ([§8.1](#81-prefer-the-environment-variable)) |
| **P2 fails**, `unauthorized_client` | Token exchange rejected | Client ID or scopes in the admin console are wrong, or DWD has not propagated. Re-read [§7.2](#72-what-to-enter) |
| **P2 fails**, `invalid_grant` | Clock skew, or the key was deleted in Cloud console | Sync the server clock (`timedatectl`); confirm the key still exists in the Keys tab |
| **P2 fails**, `SERVICE_DISABLED` | The Drive or Sheets API is not enabled on the project | [§5](#5-google-cloud-enable-the-drive-and-sheets-apis) |
| **P2 passes but P3 fails** | **The delegation grant is wrong.** Token minted, impersonation refused | Almost always the email-instead-of-client-ID mistake ([§6.1](#61-copy-the-numeric-oauth2-client-id--not-the-email)) or a scope-string typo |
| **P3 returns a *different* email** | You are authenticating as the bare service account | `with_subject()`'s return value was discarded, or `auth_mode` is `sa_direct` |
| **P3 passes but P4 returns 0** | The subject's Drive genuinely appears empty | Rendered as a **red error**, not an empty state — see below |
| **P4 passes, P7 fails** | Drive scope authorised, Sheets scope not | Re-paste the full two-scope string in the admin console |

> **Why P4 returning zero is a red error and not an empty state.** For a real Workspace user with a
> populated Drive, zero files is not a valid answer — it is the signature of a broken delegation
> grant or a service account crawling its own empty corpus. Rendering it as a neutral "no results"
> is how a misconfiguration gets mistaken for a successful setup, and how a delete planner is later
> handed an empty universe. It is treated as a failure until proven otherwise.

---

## 11. First run: discovery, ingest, staging

1. Press **Run Discovery** on the connection (or wait for the 15-minute cron).
2. Open **Google Drive Sync → Drive Nodes**. You should see My Drive, its subfolders, and folders
   shared by colleagues and outside accounts. Group by *Owner* to confirm shared content arrived.
3. Let **ingest** run (30-minute cron). Non-spreadsheet files appear as `ir.attachment` records
   linked to their node; Docs/Slides/Drawings arrive as PDFs, Docs additionally as `text/plain`.
4. Let **stage** run (hourly cron). Open **Datasets**.

Things you will see, all of which are correct:

- **Two files with the same title are two separate nodes.** Identity is the Drive file id.
- **Two tabs with the same title are two separate datasets.** Identity is `(node_id, sheet_gid)`.
- **`.xlsx` tabs have negative `sheet_gid`s** (`-1`, `-2`, …). Excel worksheets have no stable gid,
  so a negative surrogate derived from the worksheet index is used, and the reader matches by title
  first, index second, blocking with `XLSX_TAB_AMBIGUOUS` if both fail.
- **Personal spreadsheets are staged too.** Shopping lists and timesheets land in staging like
  everything else. Staging never touches a business model, so this is free and auditable. Use
  **scope rules** if you want to exclude a subtree.
- **A node in state `skipped`** with a `skip_reason` is recorded on purpose — its *existence* stays
  auditable even when its content is not ingested.

Open **Staged Rows** for a dataset. **This is already the whole staging product.** Nothing further
is required, and for many datasets nothing further is ever desirable.

---

## 12. Promotion: mapping staged rows into business models

Do this **only** when a dataset genuinely belongs in a business model.

1. Open the dataset → **Build Mapping**. The wizard materialises one line per observed column with a
   *suggested* type and Odoo field. Suggestions are suggestions; nothing is promoted by this wizard.
2. For each column set `ctype`, `odoo_field`, `is_natural_key`, and — critically —
   **`assert_string_value` on every identifier column** (SKU, barcode, invoice number, postal code,
   phone).

   > **Why `assert_string_value` matters.** With `UNFORMATTED_VALUE`, Sheets returns a numeric cell
   > as a number. `"007"` arrives as `7`; a 16-digit account number arrives with its last digits
   > mangled by float precision. `assert_string_value` checks the `effectiveValue` oneof branch and
   > raises `IDENTIFIER_NUMERIC`, refusing the cell and quarantining the row, instead of silently
   > promoting corrupted identifiers.

3. Press **Validate**. Validation refuses to reach `validated` unless all seven assertions hold —
   every mapped header resolves to exactly one live column, every Odoo field exists and is writable,
   every `selection` value maps to a real technical key, every `money` column has a resolvable
   currency, a natural key exists (or `_sync_id` does), and the technical fields
   `x_gdrive_sync_id` / `x_gdrive_source_dataset` exist on the target model.
4. Only then, deliberately, set **`enabled = True`**.

### Things worth knowing before you enable one

- **The first promotion is not special.** There is no separate "initial load" path — it is a plan
  whose actions are all `create`, subject to the same circuit breakers. `create_threshold_abs`
  defaults to **50**, so the initial load of a large dataset must be done once through the wizard,
  deliberately, or the threshold raised on that mapping with a recorded reason.
- **A missing mapped column is a hard stop.** If an enabled mapping references a header that is no
  longer in the sheet, the dataset is blocked, a `blocking` drift is raised, and **zero rows are
  staged**. Treating an absent column as empty cells would write NULL over an entire Odoo column —
  the single most destructive failure mode in sheet sync, and it is structurally prevented here.
- **A *new unmapped* column is not a problem.** It logs `SCHEMA_GROWTH` at `info`, syncing
  continues, and it is recorded in `h_extra` so the growth is visible without polluting the
  compared hash.
- **Column reordering is a no-op by construction.** Columns resolve by canonical header; rows hash
  by Odoo field name.
- **Records humans created in Odoo are never touched.** A business record is a delete candidate only
  if it carries the matching `x_gdrive_source_dataset`, a non-null `x_gdrive_sync_id`, *and* a live
  promotion link. Everything else in the mapping domain is reported as `UNMANAGED`.

---

## 13. Verification, drift and healing

### Layered comparison

| Layer | Work done | Cost |
|---|---|---|
| **L0 Drive** | Skip the file if `drive_version` and `drive_modified_time` are unchanged and it is not trashed | 1 metadata call, or 0 in delta mode |
| **L0b Odoo** | Skip only if **both** `search_count(domain)` **and** `max(write_date)` are unchanged | 2 cheap queries |
| **L1 Dataset** | One `batchGet` for all tabs; compute and compare `h_dataset` on both sides | 1 Sheets read per workbook |
| **L2 Bucket** | Compare the 256 bucket hashes; typically 1–2 differ | 0 API cost |
| **L3 Row/field** | Inside differing buckets only, join by identity and compare field by field | 0 API cost |

> **Why L0b needs both count and max-write-date.** A delete does not advance `max(write_date)`, and
> an in-place edit does not change the count. Either check alone misses one of the two most common
> changes. And L0 alone is not enough either: a Drive file can be untouched while Odoo changed
> underneath it — that is the half everyone forgets.

**Cache invalidation is non-negotiable.** Every stored hash is keyed by
`spec_version = H(contract ‖ CANON_VERSION)`. Change any normalization rule or any column option
and every cached hash becomes invalid. Serving a hash computed by an older normalizer as `verified`
is a **silent false pass** — the worst possible failure of a verification system. The module
therefore forces a full recompute on every upgrade via `post_init_hook`, and again weekly via cron.

**Independent controls.** Alongside the hashes, every verification records `rows_sheet`,
`rows_odoo`, and `column_totals` — a `Decimal` sum per numeric column computed from **raw**, not
canonical, values. If both sides are canonicalized wrongly *in the same way*, the hashes agree and
the canonical totals agree; the raw totals do not. This is the only check that catches a symmetric
normalizer bug.

### The three counts are kept disjoint

| Count | Contains |
|---|---|
| `drift_count` | Real disagreements: `missing_in_odoo`, `missing_in_sheet`, `field_mismatch`, `currency_mismatch`, `unmanaged_record`, `non_convergent` |
| `data_quality_count` | `type_coercion`, `identifier_numeric`, `orphan_reference`, `multi_match`, `duplicate_identity` |
| `structural_count` | `header_change`, `schema_growth`, `tab_missing`, `empty_tab`, `access_lost` |

They are separate columns so that "12 drifts" never silently means "12 cells I could not read".

### Healing

Healing runs from the scheduler **only** when `mapping.auto_heal` is True, and it **ships False**.
Everything else goes through the **Heal wizard**, which:

- defaults `dry_run = True`;
- computes the preview through the *same* `gdrive.reconciler.plan()` call that apply uses — if
  preview and apply had separate code paths the preview would be a plausible-looking lie;
- is `groups`-gated to *Google Drive Sync / Administrator* on apply;
- refuses if the plan expired (24 h);
- **re-reads every fingerprint before executing** and refuses with `refused_stale` if anything
  moved, turning "someone edited the sheet between preview and approval" from a corruption into a
  retry;
- executes in fixed order — writeback (10) → create (20) → update (30) → soft_delete (40) →
  quarantine (50) — and **skips all soft-deletes if anything earlier failed**, because an earlier
  error is evidence the system's view of the world is incomplete.

### Delete guards (all seven must hold)

1. `mapping.delete_policy == 'soft'` — never the default.
2. The record is **owned**: matching `x_gdrive_source_dataset`, non-null `x_gdrive_sync_id`, and a
   live promotion link.
3. `run.complete_read` **and** `dataset.last_read_complete` are both True.
4. The dataset is not blocked and has no `blocking` drift in this verification.
5. The identity has been absent for ≥ `quarantine_runs` consecutive **complete** runs **and**
   ≥ `quarantine_hours`.
6. The delete count is within `max(delete_threshold_abs, delete_threshold_pct% × rows_odoo)`;
   above that the breaker trips, approval is required, and **nothing executes**.
7. `identity_strategy` resolved to `sync_id` for that row.

**Hard delete is never available to any automated path, at any threshold, under any
configuration.** Execution is a soft delete — `active = False` with `x_gdrive_sync_id` retained, so
a restore is one flag flip.

### Convergence assertion

After a plan is applied, both dataset hashes are recomputed and asserted equal. If they are not,
`convergence_ok = False` and a `critical` `non_convergent` drift is raised — the system **alerts
rather than retries**.

The bug class this catches is the killer: the comparator says A ≠ B, the writer writes A, but A
round-trips through Odoo as A′, and A′ ≠ A under the normalizer — so the next run writes it again,
forever. Without the assertion the symptom is invisible: the dashboard says "3 fixes applied" every
single night and everyone assumes it is working. A flap counter backs it up, stopping writes to a
`(sync_id, field)` pair after `flap_limit` (default 3) consecutive runs and recording both
canonical forms, which tells the maintainer exactly which normalization rule is asymmetric.

---

## 14. Scheduled actions

**Settings → Technical → Automation → Scheduled Actions.**

| Action | Model | Interval | Priority |
|---|---|---|---|
| GDrive Sync: Discover (delta) | `gdrive.connection` | 15 minutes | 5 |
| GDrive Sync: Ingest queued nodes | `gdrive.node` | 30 minutes | 6 |
| GDrive Sync: Stage spreadsheet tabs | `gdrive.dataset` | 1 hour | 7 |
| GDrive Sync: Promote mapped datasets | `gdrive.mapping` | 1 hour | 8 |
| GDrive Sync: Verify & report | `gdrive.dataset` | 1 day | 9 |
| GDrive Sync: Weekly full recompute | `gdrive.connection` | 1 week | 10 |
| GDrive Sync: Housekeeping | `gdrive.sync.run` | 1 day | 20 |

All seven ship inside `<data noupdate="1">`, so **your interval, `active` and `nextcall` changes
survive `-u gdrive_odoo_sync`**. The corollary: a new shipped default does *not* reach an existing
database — that requires a migration or an explicit note in [§16](#16-upgrades).

Behavioural guarantees:

- **No cron method raises.** Odoo 18 auto-deactivates a scheduled action after repeated failures, so
  one unreadable spreadsheet must never be able to switch off the whole sync. Per-entity exceptions
  are caught, recorded as an `error` run line, and the run ends `partial`.
- Each is a **batch driver with a wall-clock budget** (600 s), committing at 200-record boundaries
  and calling `_trigger()` on itself when the budget expires with work remaining — so a backlog
  drains in minutes rather than one interval at a time.
- Each takes a **per-connection advisory lock** (`pg_try_advisory_xact_lock`) and returns
  immediately if it cannot, making overlapping runs structurally impossible.
- Long network work belongs in crons, never in a form button: Odoo.sh cuts HTTP requests at
  `limit_time_real` (default 120 s).

---

## 15. Security model

### Groups

| Group | Implies | Can |
|---|---|---|
| *Google Drive Sync / User* | `base.group_user` | Read nodes, datasets, staged rows, verifications, drift. Read-only. |
| *Google Drive Sync / Manager* | User | Author and enable mappings, edit scope rules, trigger runs, **preview** plans. **Cannot apply.** |
| *Google Drive Sync / Administrator* | Manager | Manage connections, approve and apply plans, enable `auto_heal`. |

Entering the service-account key is gated separately with `base.group_system` — strictly narrower
than the module's own administrator group.

Plan application is guarded **in Python** (`has_group('gdrive_odoo_sync.group_gdrive_admin')`), not
by ACL alone. Managers hold write on `gdrive.plan.action` only so the heal wizard can toggle each
line's `selected` flag while previewing.

### Record rules

Exactly five global multi-company rules ship, on `gdrive.connection`, `gdrive.node`,
`gdrive.dataset`, `gdrive.staged.row` and `gdrive.sync.run`:

```python
['|', ('company_id', '=', False), ('company_id', 'in', company_ids)]
```

They are global (empty `groups`) so they are ANDed with everything and **cannot be widened by
adding a user to another group** — exactly the semantics company isolation needs. That same
property is why nothing else ships as a global rule: a convenience rule added here cannot be
relaxed from the UI and will eventually lock an administrator out of data they are on call for.
There are no ownership rules; this is an administrative tool.

### Attachments

`ir.attachment` ACL derives from `(res_model, res_id)`. Mirrored files are linked to `gdrive.node`,
so read access on nodes grants read on the files. All cron-side attachment writes use `.sudo()`.
`public` is never set True and `access_token` is never generated automatically. Attachments are
written with `raw` (never both `raw` and `datas`) and **never** carry `res_field` — attachments with
`res_field` are filtered out of the generic Attachments sidebar and would appear to vanish.

---

## 16. Upgrades

```bash
git pull
sudo -u odoo /opt/odoo/venv/bin/python -m pip install -r requirements.txt   # in case pins moved
sudo -u odoo /opt/odoo/venv/bin/python odoo-bin \
     -c /etc/odoo/odoo.conf -d YOURDB -u gdrive_odoo_sync --stop-after-init
sudo systemctl restart odoo
```

On Odoo.sh, pushing to the branch triggers the build and upgrade automatically.

What `post_init_hook` does on **every** install and upgrade:

1. **Clears every cached content hash** (`h_dataset_sheet`, `h_dataset_odoo`, `bucket_hashes`, and
   the L0/L0b fingerprints) and flags every connection `full_resync_requested`. An upgrade may have
   changed `CANON_VERSION` or a normalization rule; a hash computed by the previous normalizer must
   never be served as `verified`. Recomputation costs API calls. A false `verified` costs trust.
2. **Re-asserts the partial unique index** `<table>_x_gdrive_sync_id_uniq` on every promotion target
   table, because a target model may have been reinstalled (dropping the index with its table)
   since the last run.

Expect the first verify pass after an upgrade to be slow and to consume Sheets quota. That is the
system doing its job.

What **uninstall** deliberately does *not* do: it drops only the partial unique indexes it created.
It leaves `x_gdrive_sync_id` and `x_gdrive_source_dataset` in place (they are the only surviving
evidence of which business records came from a sheet, and an uninstall is frequently a step in a
reinstall), leaves mirrored attachments alone (they are your files), and leaves soft-deleted records
soft-deleted.

Because the data files are `noupdate="1"`, changed defaults for crons and config parameters do not
propagate to existing databases. Check the release notes; adjusting them is a manual, deliberate act.

---

## 17. Running the tests

```bash
odoo-bin -d testdb -i gdrive_odoo_sync --test-enable \
         --test-tags=/gdrive_odoo_sync --stop-after-init
```

Use a scratch database. `--test-enable` runs against the database you name, and the model tests
create and delete records.

The pure canonicalization/hashing tests need no database and no network. They are the ones to run
while iterating on a normalization rule:

```bash
python -m pytest gdrive_odoo_sync/tests/test_lib_*.py -q
```

Every test module must be listed in `gdrive_odoo_sync/tests/__init__.py` — a module absent from it
is **silently never run**, which is indistinguishable from a module that passes. `tests` must never
be imported from the addon's top-level `__init__.py`; Odoo imports it itself, only under
`--test-enable`.

---

## 18. Troubleshooting reference

| Symptom | Cause | Fix |
|---|---|---|
| `files.list` returns `{'files': []}`, HTTP 200, no error | Bare service account, no impersonation | The whole of [§7](#7-workspace-admin-domain-wide-delegation--the-step-everyone-gets-wrong). Check P3 first. |
| `401 unauthorized_client` | Client ID or scope string wrong in the admin console; or DWD has not propagated | Numeric client ID, no `@`; the exact scope string; wait up to 24 h |
| `403 SERVICE_DISABLED` | Drive or Sheets API not enabled on the Cloud project | [§5](#5-google-cloud-enable-the-drive-and-sheets-apis) |
| `403 insufficientPermissions` | Scope authorised does not cover the call | Re-paste both scopes; never retried by design |
| `403 exportSizeLimitExceeded` | A native Doc/Slides export exceeds 10 MB | Node goes `skipped`; the limit is on the generated artefact, so chunking does not help. Out of scope for v1. |
| `403 rateLimitExceeded` | Drive quota pressure | Retried automatically with jittered backoff. If persistent, lower `drive_units_per_min`. |
| `429` on Sheets | More than 60 reads/min/user | Lower `sheets_reads_per_min`; pacing beats backing off |
| `400 Unable to parse range` | A tab title contains an apostrophe | Handled: apostrophes are doubled in quoted A1 ranges. If you see it, the range was built outside `SheetsReader`. |
| Module install fails on `numbercall` / `doall` | An `ir.cron` record carries a field removed in Odoo 18 | Not possible in shipped code; check any local customisation |
| Module install fails on `<tree>` or `attrs=` | Odoo 18 removed both | Views must use `<list>` and direct `invisible=` / `readonly=` / `required=` expressions |
| "External dependency not satisfied" though `pip list` shows the package | Installed into a different interpreter than the one running `odoo-bin` | Install with the venv's own `python -m pip` |
| Only ~100 files discovered | `nextPageToken` missing from a `fields` mask | Pagination silently stops after page 1. All shipped masks request it explicitly. |
| Shared-drive content missing entirely | `supportsAllDrives` / `includeItemsFromAllDrives` omitted | Silently excluded with **no error**. All shipped calls pass both. |
| `incompleteSearch = true` in a run | `corpora='allDrives'` behaviour | Sets `complete_read = False` and logs `INCOMPLETE_SEARCH`; the delete planner is disarmed for that run. Keep `corpora_mode = per_drive`. |
| Dataset blocked, `mapped_column_missing` | A mapped header vanished from the sheet | Intentional hard stop. Restore the column, or edit the mapping. Zero rows were staged. |
| Dataset blocked, `empty_tab` | 0 data rows where the last complete run had N > 0 | Treated as a mass-delete signal, never as "all rows deleted". Investigate the sheet before unblocking. |
| Rows quarantined, `identifier_numeric` | An identifier column returned `numberValue` | Correct behaviour — leading zeros are already gone. Format the column as plain text in Sheets. |
| Rows quarantined, `duplicate_identity` | Two rows share a `sync_id` or natural key | The whole key group is quarantined on purpose: picking one arbitrarily makes runs alternate between the two rows' values forever. Fix the sheet. |
| `.xls` file skipped | Legacy format unsupported in v1 | Re-save as `.xlsx` |
| xlsx cells read as `None` | The workbook was never recalculated by Excel, so `data_only=True` has no cached values | Logged `XLSX_NO_CACHED_VALUES`; affected cells are quarantined, not read as empty. Open and re-save the file in Excel. |
| Plan refuses with `refused_stale` | A fingerprint moved between preview and apply | Working as designed. Re-preview and re-approve. |
| Nightly "3 fixes applied", forever | A non-convergent field | The convergence assertion and flap counter catch this; look for a `non_convergent` drift and compare the two canonical forms. |

---

## 19. Explicit non-goals for v1

- **No writes to Google Drive of any kind.** No `_sync_id` write-back, no cell repair, no file
  creation. The OAuth scopes are read-only, structurally.
- No dependency on Odoo Enterprise `documents`.
- No bidirectional sync. (If it is ever added it must be a 3-way merge against the last synced
  canonical snapshot, with "both sides differ from base" resolving to `CONFLICT` and never
  auto-resolving.)
- No hard deletes from any automated path.
- No `.xls` (legacy), Google Forms, Apps Script, Sites, or Jamboard ingestion.
- No `files.download` long-running-operation path for exports over 10 MB.
- No per-user Admin SDK sweep. One subject: `lucaso@avatarnaturalfoods.com`.

---

## Repository layout

```
.
├── README.md                 ← you are here
├── requirements.txt          ← repo root; Odoo.sh installs this at build time
├── docs/
│   ├── SPEC.md               ← the binding technical contract
│   ├── FILE_MANIFEST.md      ← every file and its owning lane
│   └── CANONICALIZATION.md   ← the exact normalization + hashing algorithm, with golden vectors
└── gdrive_odoo_sync/         ← the Odoo addon
    ├── __manifest__.py
    ├── __init__.py           ← post_init_hook / uninstall_hook
    ├── data/                 ← crons, sequences, non-secret config parameters
    ├── security/             ← groups, ACLs, record rules
    ├── lib/                  ← canonicalization + hashing (stdlib only, no Odoo imports)
    ├── services/             ← Google API client layer (no Odoo model code)
    ├── models/               ← the 16 persistent models + 2 abstract engines
    ├── wizard/               ← the 5 transient models
    ├── views/                ← Odoo 18 XML UI
    ├── static/description/   ← app icon and store page
    └── tests/                ← imported only under --test-enable
```

**License:** LGPL-3. Built for Avatar Natural Foods.
