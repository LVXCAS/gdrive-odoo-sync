# `gdrive_odoo_sync` — Technical Specification

**Version:** 1.0.0 (contract-frozen)
**Target platform:** Odoo 18.0 Community *or* Enterprise, self-hosted or Odoo.sh.
**Module technical name:** `gdrive_odoo_sync`
**Repository layout:** the addon lives at repo path `gdrive_odoo_sync/`; `requirements.txt` lives at repo root (Odoo.sh installs it at build time).
**Direction:** Google Drive → Odoo. Drive is the source of truth. Odoo never writes to Drive in v1.
**Companion documents:** `docs/FILE_MANIFEST.md` (who builds what), `docs/CANONICALIZATION.md` (the exact byte-level canonicalization and hashing algorithm).

> This document is the contract. Where it says MUST, deviation is a defect. Where it says an exact string, literal, default, or field name, use that exact string, literal, default, or field name. Do not "improve" names — six lanes are building against them concurrently.

---

## 0. Executive summary

`gdrive_odoo_sync` is a real Odoo addon module (not an external script) that:

1. **Discovers** every Google Drive file and folder visible to a single impersonated Workspace identity (`lucaso@avatarnaturalfoods.com`) — My Drive, subfolders, *and* items shared by `michael@`, `Diego@`, `lvxxcas@gmail.com`, plus any shared drives.
2. **Classifies** each Drive object by MIME type into: folder, native Google Sheet, native Google Doc/Slides/Drawing, binary blob (`.xlsx`, `.pdf`, `.jpeg`, `.docx`…), shortcut, or unsupported.
3. **Ingests** non-spreadsheet content into `ir.attachment`, preserving the Drive folder hierarchy in a mirrored node tree.
4. **Stages** every *tab* of every spreadsheet (native Google Sheets **and** uploaded `.xlsx`) as its own dataset with its own header schema, into a schema-flexible staging model backed by `fields.Json` plus denormalized query columns.
5. **Promotes** staged rows into real business models (`res.partner`, `crm.lead`, …) **only** where an administrator has explicitly authored and enabled a declarative mapping. Never guessed. Never automatic.
6. **Verifies** by computing an order-insensitive, bucketed-Merkle content hash on both the Drive side and the Odoo side, drilling down only where hashes disagree.
7. **Reports** drift as first-class Odoo records plus a downloadable JSON/HTML artefact.
8. **Heals** Odoo to match Drive — but only through an explicit, fingerprint-guarded plan, **dry-run by default**, with per-mapping opt-in auto-heal that ships **off**.

**Identity rules that pervade the whole system:**
- A Drive file is identified by its **file id**, never its title. `Bettr_Bowl_Data_Request` exists twice; titles are display-only strings.
- A spreadsheet tab is identified by its numeric **`sheetId` (gid)**, never its title.
- A staged row is identified by a **ULID `_sync_id`** where available, falling back to a **declared natural key**. Never by row position. Never by a hash of mutable content.

---

## 1. Odoo version target and compatibility rules

The module targets **Odoo 18.0**. These are hard rules; violating any of them breaks installation.

| Rule | Detail |
|---|---|
| List views | Root element is `<list>`, never `<tree>`. `view_mode` is `list,form`, never `tree,form`. Applies to XML, Python action dicts, and xpaths. |
| Conditional display | Use `invisible="state == 'draft'"` / `readonly=` / `required=` direct expressions. `attrs=` and `states=` are **removed** and will fail to load. Any field referenced in such an expression MUST also appear in the view (`invisible="1"`, or `column_invisible="1"` in a list). |
| Chatter | `<chatter/>`. Not `<div class="oe_chatter">`. |
| `ir.cron` | MUST NOT contain `numbercall` or `doall` — those fields were removed in 18 and their presence hard-fails install. |
| Display name | Override `_compute_display_name` with `@api.depends`. `name_get()` is removed and is silently never called. |
| Aggregation | Use `aggregator='sum'`, not `group_operator='sum'`. |
| Access checks | Use `check_access(op)` / `has_access(op)` / `_filtered_access(op)`. |
| Enterprise | The module MUST NOT depend on `documents`. It builds on `ir.attachment` only, so it installs on Community. A future optional bridge addon `gdrive_odoo_sync_documents` (`depends: ['gdrive_odoo_sync','documents']`, `auto_install: True`) is explicitly **out of scope for v1**. |

`__manifest__.py` (lane A) is exactly:

```python
{
    'name': 'Google Drive → Odoo Sync & Verification',
    'version': '18.0.1.0.0',
    'summary': 'Mirror Google Drive into Odoo, stage spreadsheet tabs, verify with content hashes, heal on approval.',
    'author': 'Avatar Natural Foods',
    'website': 'https://avatarnaturalfoods.com',
    'category': 'Productivity/Documents',
    'license': 'LGPL-3',
    'application': True,
    'installable': True,
    'auto_install': False,
    'depends': ['base', 'mail'],
    'external_dependencies': {
        'python': ['google.oauth2', 'googleapiclient', 'openpyxl'],
    },
    'data': [
        'security/gdrive_security.xml',
        'security/ir.model.access.csv',
        'security/gdrive_record_rules.xml',
        'data/ir_sequence_data.xml',
        'data/ir_config_parameter_data.xml',
        'data/ir_cron_data.xml',
        'views/gdrive_connection_views.xml',
        'views/gdrive_node_views.xml',
        'views/gdrive_dataset_views.xml',
        'views/gdrive_staged_row_views.xml',
        'views/gdrive_mapping_views.xml',
        'views/gdrive_verification_views.xml',
        'views/gdrive_drift_views.xml',
        'views/gdrive_plan_views.xml',
        'views/gdrive_sync_run_views.xml',
        'views/res_config_settings_views.xml',
        'wizard/gdrive_connection_test_wizard_views.xml',
        'wizard/gdrive_mapping_builder_wizard_views.xml',
        'wizard/gdrive_heal_wizard_views.xml',
        'views/gdrive_menus.xml',
    ],
}
```

Notes on `external_dependencies`: entries are **import names**, not pip names. `requests` MUST NOT be listed (Odoo pins it already). `openpyxl` is already in Odoo's `requirements.txt` but is listed anyway so a stripped environment fails at install time rather than at runtime. Repo-root `requirements.txt` contains `google-api-python-client`, `google-auth`, `google-auth-httplib2`.

Security data loads **before** views (views reference groups). `views/gdrive_menus.xml` loads **last** (menus reference actions).

---

## 2. The service-account visibility problem — and the exact fix

### 2.1 The problem, stated plainly

A Google Cloud service account is a *principal*, not a *view onto your organisation*. It has its own Drive corpus. That corpus is **empty**, and since 1 June 2023 it has a **0 GB storage quota** so it cannot even create files.

With plain (non-delegated) service-account credentials:

```python
creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
drive = build('drive', 'v3', credentials=creds, cache_discovery=False)
drive.files().list(q="trashed = false").execute()   # -> {'files': []}
```

returns **an empty list**, with HTTP 200 and no error, because `files.list` defaults to `corpora='user'` and "user" means *the service account*. It cannot see `lucaso@avatarnaturalfoods.com`'s My Drive. It cannot see his "Shared with me". It cannot see the folders `michael@` and `Diego@` shared with *him* — sharing is per-principal, and the service account is a different principal.

This is the single most dangerous failure mode in the entire system, because **an empty read looks exactly like "everything was deleted"**. Section 9.6 (delete guards) exists largely because of it.

The two escape routes:

| Route | Applicability here | Verdict |
|---|---|---|
| **(A) Domain-wide delegation (DWD)** — the service account impersonates a real Workspace user via `creds.with_subject('lucaso@avatarnaturalfoods.com')`. Every API call then behaves exactly as if Lucas ran it: his My Drive, his Shared-with-me, his change cursor. | `avatarnaturalfoods.com` **is** a Google Workspace domain. Lucas administers it. | **This is the chosen mechanism.** |
| **(B) Explicit sharing** — a human shares each folder with the SA's `…iam.gserviceaccount.com` email; the crawler then works from `sharedWithMe = true` roots. | Works, but requires manual re-sharing every time a folder is created, and cannot reach items shared with Lucas by third parties (notably `lvxxcas@gmail.com`, a consumer account whose shares land in Lucas's Shared-with-me, not the SA's). | Supported as `auth_mode = 'sa_direct'` fallback for degraded operation only. |

A third case worth naming because it is a common misconception: DWD **cannot** impersonate an `@gmail.com` consumer account. `lvxxcas@gmail.com` will never be an impersonation subject. Its files reach us only because it shared them *with Lucas*, and we are Lucas.

### 2.2 One-time DWD setup (must be done by a Workspace super admin)

1. **Create the service account.** Google Cloud console → the project → IAM & Admin → Service Accounts → Create. Name it `gdrive-odoo-sync`. Do **not** grant it any Cloud IAM roles — it needs none.
2. **Enable APIs** on the project: *Google Drive API* and *Google Sheets API*.
3. **Create a JSON key** for the service account and download it. Guard it: it is a bearer credential for Lucas's entire Drive.
4. **Copy the numeric OAuth2 Client ID** from the service account's detail page. This is a ~21-digit number. It is **not** the `…iam.gserviceaccount.com` email. Pasting the email in step 6 is the most common setup error.
5. Admin console (`admin.google.com`) → **Security** → **Access and data control** → **API controls** → **Domain-wide delegation** → **Manage Domain Wide Delegation** → **Add new**.
6. Paste the numeric Client ID, and this exact comma-delimited scope string:

   ```
   https://www.googleapis.com/auth/drive.readonly,https://www.googleapis.com/auth/spreadsheets.readonly
   ```

7. **Authorize.** Propagation is usually a few minutes but Google documents up to 24 hours.

**Scope discipline.** The scopes passed to `from_service_account_info(..., scopes=…)` MUST be a **subset** of what is authorized in the Admin console. A mismatch produces `401 unauthorized_client` at token-exchange time — a confusing error that does not mention scopes. The module hard-codes the pair above and exposes it read-only in the UI; `gdrive.connection.scopes` is `readonly=True`.

Both scopes are read-only by design. The system never obtains write scope, which structurally guarantees v1 cannot damage Drive and makes `_sync_id` write-back (§8.3) unavailable — an accepted, deliberate v1 limitation.

### 2.3 Applying impersonation in code (lane B)

```python
from google.oauth2 import service_account

SCOPES = (
    'https://www.googleapis.com/auth/drive.readonly',
    'https://www.googleapis.com/auth/spreadsheets.readonly',
)

base_creds = service_account.Credentials.from_service_account_info(info, scopes=list(SCOPES))
creds = base_creds.with_subject(connection.subject_email)   # MUST capture the return value
```

`with_subject()` returns a **new** credentials object; it does not mutate. Writing `base_creds.with_subject(...)` without assignment leaves you authenticating as the bare SA and silently seeing nothing. Lane B MUST include a unit test asserting `creds is not base_creds`.

### 2.4 Verifying the setup before anything else is built

The **Test Connection** wizard (`gdrive.connection.test.wizard`, lane E) is the day-one artefact. It performs, in order, and reports each step's outcome individually:

| Probe | Call | Pass criterion |
|---|---|---|
| P1 Key parses | `json.loads` | `client_email`, `client_id`, `private_key` present |
| P2 Token mints | `creds.refresh(Request())` | no `unauthorized_client` |
| P3 Impersonation works | `drive.about().get(fields='user(emailAddress),storageQuota')` | `user.emailAddress == subject_email` |
| P4 Corpus is non-empty | `drive.files().list(q="trashed = false", pageSize=10, fields='files(id,name,mimeType)')` | `len(files) > 0` |
| P5 Shared-with-me reachable | `drive.files().list(q="sharedWithMe = true and trashed = false", pageSize=10, …)` | reported, may legitimately be 0 |
| P6 Shared drives enumerable | `drive.drives().list(pageSize=100, fields='nextPageToken,drives(id,name)')` | reported |
| P7 Sheets API reachable | `sheets.spreadsheets().get(spreadsheetId=<first sheet found>, fields='spreadsheetId,sheets(properties(sheetId,title))')` | 200 |

**P3 failing while P2 passes** means the client ID or scopes in the Admin console are wrong. **P4 returning 0 while P3 passes** means the subject genuinely has an empty Drive — which for `lucaso@avatarnaturalfoods.com` means something is wrong, so the wizard renders that as a red error, not an empty state. `gdrive.connection.state` is only set to `ok` when P1–P4 all pass.

### 2.5 Where the key is stored

Resolution order, implemented once in `services/google_auth.py::load_service_account_info(env, connection)`:

1. `os.environ.get(connection.sa_key_env_var)` — default env var name `GDRIVE_ODOO_SYNC_SA_KEY`, holding the raw JSON. **Preferred.** On Odoo.sh set it as a project environment variable.
2. `env['ir.config_parameter'].sudo().get_param(connection.sa_key_param_key)` — default key `gdrive_odoo_sync.sa_key_json`.
3. Otherwise raise `UserError` with the setup instructions.

Hard rules:
- `.sudo()` is mandatory on every `ir.config_parameter` access. The model is restricted to `base.group_system`; a cron or a manager user without sudo raises `AccessError`.
- The key MUST NOT be a field on any regular model with a form view. It is entered through `res.config.settings` with `password="True"` and `groups="base.group_system"`, which writes it to `ir.config_parameter` and never persists it on the settings record.
- The key MUST NOT ship in any XML data file. `data/ir_config_parameter_data.xml` seeds only non-secret defaults (`gdrive_odoo_sync.default_subject`, `gdrive_odoo_sync.sheets_reads_per_min`, …), all inside `<data noupdate="1">`.
- **Prefer the env var.** Odoo.sh database dumps are downloadable and `ir_config_parameter` values appear in them in cleartext.
- `set_param` with a falsy value **deletes** the parameter. The settings inverse MUST branch on empty rather than writing `''` and assuming it stored an empty string.

---

## 3. Data model

21 new models: 16 persistent, 5 transient. Plus 2 `AbstractModel` engines and 1 `res.config.settings` extension.

Conventions applied to every model below: `_description` is always set; `_order` is always set; every `fields.Json` field is accompanied by the denormalized real columns needed to search, group, or sort — **`fields.Json` is not searchable, not indexable, has no `read_group` support, and returns a deep copy on read** (so `rec.payload['k'] = v` silently does nothing; always `rec.payload = {**rec.payload, 'k': v}`).

### 3.1 `gdrive.connection` — one Google identity we crawl as

`_order = 'sequence, id'`

| Field | Type | Meaning |
|---|---|---|
| `name` | Char, required | Human label, e.g. "Avatar — lucaso@". |
| `sequence` | Integer, default 10 | Ordering. |
| `active` | Boolean, default True | Archive flag. |
| `company_id` | Many2one `res.company`, default current | Multi-company scoping. |
| `auth_mode` | Selection `dwd` / `sa_direct`, default `dwd` | `dwd` = impersonate `subject_email`. `sa_direct` = bare SA, degraded (§2.1 route B). |
| `subject_email` | Char, default `lucaso@avatarnaturalfoods.com` | DWD impersonation subject. Required when `auth_mode == 'dwd'`. |
| `sa_key_env_var` | Char, default `GDRIVE_ODOO_SYNC_SA_KEY` | Env var checked first for the JSON key. |
| `sa_key_param_key` | Char, readonly, default `gdrive_odoo_sync.sa_key_json` | `ir.config_parameter` fallback key. |
| `sa_client_email` | Char, readonly, compute (non-stored) | Derived from the loaded key, for display and for "share this folder with" instructions. |
| `sa_client_id` | Char, readonly, compute (non-stored) | The numeric OAuth client ID to paste into the Admin console. |
| `scopes` | Char, readonly | The two read-only scopes, joined by `,`. |
| `include_shared_with_me` | Boolean, default True | Crawl `sharedWithMe = true` items. |
| `include_shared_drives` | Boolean, default True | Crawl shared drives the subject can access. |
| `include_trashed` | Boolean, default False | Almost always False. |
| `corpora_mode` | Selection `user` / `all_drives` / `per_drive`, default `per_drive` | `per_drive` iterates `drives.list` and issues one `corpora='drive'` query each, which is the only mode immune to `incompleteSearch`. |
| `max_blob_bytes` | Integer, default `104857600` | Blobs larger than this are recorded but not downloaded; node state `skipped`, reason `too_large`. |
| `sheets_reads_per_min` | Integer, default 50 | Client-side token bucket. Google's hard cap is 60/min/user. |
| `drive_units_per_min` | Integer, default 200000 | Client-side token bucket against the 325k/min/user Drive ceiling. |
| `http_timeout_connect` | Float, default 10.0 | `requests`/httplib2 connect timeout, seconds. |
| `http_timeout_read` | Float, default 120.0 | Read timeout, seconds. |
| `max_retry_attempts` | Integer, default 8 | Backoff attempts (§4.4). |
| `state` | Selection `draft` / `ok` / `error`, default `draft` | Set to `ok` only by a passing Test Connection (§2.4). |
| `last_test_date` | Datetime, readonly | |
| `last_error` | Text, readonly | Last connection-level failure. |
| `full_resync_requested` | Boolean, default False | Set by a button or by the weekly cron; forces mode `full` on the next run and invalidates cached hashes. |
| `scope_rule_ids` | One2many `gdrive.scope.rule` | §3.2. |
| `cursor_ids` | One2many `gdrive.change.cursor` | §3.3. |
| `node_ids` | One2many `gdrive.node` | |
| `node_count`, `dataset_count`, `drift_open_count` | Integer, compute, non-stored | Dashboard counters via `read_group`. |

`_sql_constraints`: `('subject_uniq', 'unique(subject_email, company_id)', 'One connection per subject per company.')`

### 3.2 `gdrive.scope.rule` — declarative crawl scoping

`_order = 'connection_id, sequence, id'`

| Field | Type | Meaning |
|---|---|---|
| `connection_id` | Many2one `gdrive.connection`, required, ondelete cascade | |
| `sequence` | Integer, default 10 | Evaluation order. |
| `kind` | Selection `include` / `exclude`, required | |
| `match_type` | Selection `folder_subtree` / `drive_id` / `owner_email` / `mime_type` / `name_glob` / `path_glob`, required | |
| `value` | Char, required | File id, drive id, email, MIME string, or `fnmatch` glob. |
| `applies_to` | Selection `all` / `spreadsheets` / `files`, default `all` | |
| `note` | Text | |

**Semantics (exact):** evaluation is default-allow. For a given `applies_to` class, if **any** `include` rule exists, that class becomes default-deny and a node must match at least one `include`. `exclude` rules are then applied and always win. A folder excluded by `folder_subtree` prunes its whole subtree from discovery. Rules never cause deletion of already-mirrored nodes; a node that falls out of scope moves to state `skipped` with reason `out_of_scope` and its dataset promotion is suspended, never deleted.

### 3.3 `gdrive.change.cursor` — Drive Changes API tokens

`_order = 'connection_id, drive_id'`

| Field | Type | Meaning |
|---|---|---|
| `connection_id` | Many2one, required, ondelete cascade | |
| `subject_email` | Char, required | Denormalized: cursors are scoped to a **principal**. |
| `drive_id` | Char | Shared-drive id, or empty for the user corpus. A token minted with `driveId=X` MUST always be replayed with `driveId=X`. |
| `page_token` | Char | The persisted `newStartPageToken`. |
| `last_polled_date` | Datetime | |
| `state` | Selection `valid` / `invalid` / `bootstrap`, default `bootstrap` | `invalid` forces a full re-enumeration plus a fresh `getStartPageToken`. |
| `invalid_reason` | Char | |

`_sql_constraints`: `('cursor_uniq', 'unique(connection_id, subject_email, drive_id)', 'One cursor per principal per drive.')`

### 3.4 `gdrive.node` — the mirrored Drive tree

`_order = 'path, name'`. `_rec_name = 'name'`. `_inherit = ['mail.thread']` for audit chatter on ingest failures.

| Field | Type | Meaning |
|---|---|---|
| `connection_id` | Many2one, required, index, ondelete cascade | |
| `google_id` | Char, required, index | **The identity.** Drive file id. |
| `name` | Char, required | Drive title. Display only — duplicates exist and are legal. |
| `mime_type` | Char, required, index | Raw Drive MIME. |
| `node_type` | Selection, required, index | `folder`, `spreadsheet`, `document`, `presentation`, `drawing`, `blob`, `shortcut`, `other_google`. Computed by `services/mimetypes.py::classify()`. |
| `parent_id` | Many2one `gdrive.node`, index, ondelete set null | Resolved primary parent. |
| `parent_google_ids` | Json | Full `parents` array. Only shared-drive files guarantee exactly one parent. |
| `is_orphan` | Boolean, stored | True when no parent in the array resolves to a visible node. Orphans hang off a synthetic `/(orphans)` root. |
| `path` | Char, stored, index | Materialized `/`-joined path of ancestor **names**, ending with this node's name. Display and glob matching only. Recomputed on parent change. |
| `depth` | Integer, stored | |
| `shared_drive_id` | Char, index | Empty for My Drive items. |
| `owner_email` | Char, index | `owners[0].emailAddress`. Files owned by `michael@`, `Diego@`, `lvxxcas@gmail.com` are in scope. |
| `is_shared_with_me` | Boolean | |
| `shortcut_target_google_id` | Char | `shortcutDetails.targetId`. |
| `shortcut_target_mime` | Char | `shortcutDetails.targetMimeType`. |
| `resolved_node_id` | Many2one `gdrive.node` | For shortcuts: the real node. Shortcuts are **never** ingested themselves. |
| `size_bytes` | Integer | Drive `size`. Absent (0) for native Google types. |
| `md5_checksum` | Char | Drive `md5Checksum`. Absent for native types. Used to skip re-download. |
| `drive_version` | Char | Drive `version` (int64 as string). Increments on metadata-only changes too — conservative in the safe direction. |
| `drive_modified_time` | Datetime | UTC-naive, as Odoo stores all datetimes. |
| `drive_created_time` | Datetime | |
| `web_view_link` | Char | Deep link back to Drive. |
| `can_download` | Boolean | `capabilities.canDownload`. |
| `trashed` | Boolean, index | |
| `ingest_policy` | Selection `auto` / `attachment` / `dataset` / `ignore`, default `auto` | Manual override of classification. |
| `state` | Selection, index, default `discovered` | `discovered`, `queued`, `ingested`, `skipped`, `error`, `gone`. |
| `skip_reason` | Selection | `too_large`, `out_of_scope`, `unsupported_mime`, `no_download_permission`, `shortcut`, `folder`. |
| `attachment_id` | Many2one `ir.attachment`, ondelete set null | Primary mirrored content. |
| `text_attachment_id` | Many2one `ir.attachment`, ondelete set null | Optional plain-text extraction (Docs). |
| `attachment_checksum` | Char | SHA-1 from the attachment; cross-check against `md5_checksum` semantics. |
| `last_ingest_date` | Datetime | |
| `last_seen_date` | Datetime | Last run in which discovery observed this node. |
| `gone_since` | Datetime | Set when discovery reports removed/trashed. |
| `last_error` | Text | |
| `dataset_ids` | One2many `gdrive.dataset` | |
| `active` | Boolean, default True | Set False when `state == 'gone'`. |

`_sql_constraints`: `('node_uniq', 'unique(connection_id, google_id)', 'A Drive file id appears once per connection.')`

**Attachment mirroring rules.** `res_model = 'gdrive.node'`, `res_id = node.id`, `name` = Drive title with a correct extension appended if missing, `mimetype` set explicitly, `raw` written with plain bytes (never both `raw` and `datas`). **`res_field` MUST NOT be set** — attachments carrying `res_field` are filtered out of the generic Attachments sidebar by `ir.attachment`'s read/search override and would appear to vanish. All attachment writes in crons go through `.sudo()` because attachment ACL derives from the linked record.

**Export map for native Google types** (`services/mimetypes.py::EXPORT_MAP`):

| Node type | Primary export MIME | Secondary |
|---|---|---|
| `document` | `application/pdf` | `text/plain` → `text_attachment_id` |
| `presentation` | `application/pdf` | — |
| `drawing` | `application/pdf` | — |
| `spreadsheet` | **none by default** | — |

Native Sheets are **not** exported. Their content is read through the Sheets API (§5.3), which has no 10 MB cap and no first-tab-only truncation. `files.export` hard-fails at 10 MB with `403 exportSizeLimitExceeded` and chunked download does not help — the limit is on the generated artefact. Exporting a multi-tab Sheet to `text/csv` silently returns **only the first tab**; that path is forbidden. A per-connection flag `mirror_sheet_snapshot` (default False, stored on `gdrive.connection`) may additionally export an `.xlsx` snapshot for archival, with `exportSizeLimitExceeded` handled as a warning, not an error.

### 3.5 `gdrive.dataset` — one spreadsheet tab

`_order = 'node_id, tab_index'`

| Field | Type | Meaning |
|---|---|---|
| `node_id` | Many2one `gdrive.node`, required, index, ondelete cascade | |
| `connection_id` | Many2one, related `node_id.connection_id`, store, index | |
| `source_kind` | Selection `gsheet` / `xlsx`, required | Determines the reader. |
| `sheet_gid` | Integer, required, index | **The identity.** Native Sheets: the numeric `sheetId`, stable across renames. `.xlsx`: `-(1 + worksheet_index)` — a negative surrogate, since xlsx has no gid. |
| `tab_title` | Char, required | Display only. A rename updates this and logs INFO; it is never "tab deleted". |
| `tab_index` | Integer | Position in the workbook. |
| `hidden` | Boolean | |
| `sheet_type` | Char | `GRID` / `OBJECT` / `DATA_SOURCE`. Only `GRID` is ingestible. |
| `header_row` | Integer, default 1 | 1-based. |
| `first_data_row` | Integer, default 2 | 1-based. |
| `sheet_timezone` | Char, default `America/New_York` | IANA name. Required for any `datetime` column. |
| `header_fingerprint` | Char, index | Hash of the canonicalized header labels (§ CANONICALIZATION §7). |
| `column_ids` | One2many `gdrive.dataset.column` | |
| `used_range` | Char | The A1 range Sheets actually resolved, e.g. `Sheet1!A1:M2411`. Authoritative extent — **not** `gridProperties.rowCount/columnCount`, which is the allocated grid (usually 1000×26). |
| `row_count` | Integer | Data rows staged in the last complete read. |
| `spec_version` | Char, index | `H(contract ‖ normalizer version)`. Every cached hash is keyed by this. |
| `h_dataset_sheet` | Char | Hex, 64 chars. |
| `h_dataset_odoo` | Char | Hex, 64 chars. |
| `bucket_hashes` | Json | 256 hex strings (16-byte digests). ~4 KB. Never queried; Json is correct here. |
| `last_drive_version` | Char | L0 fast-path input. |
| `last_drive_modified` | Datetime | L0 fast-path input. |
| `last_odoo_count` | Integer | L0b fast-path input. |
| `last_odoo_max_write_date` | Datetime | L0b fast-path input. |
| `last_stage_date` | Datetime | |
| `last_verify_date` | Datetime | |
| `last_full_verify_date` | Datetime | Forced weekly regardless of fast paths. |
| `last_read_complete` | Boolean, default False | **Delete-planner gate.** §9.6. |
| `state` | Selection, index, default `new` | `new`, `staged`, `mapped`, `verified`, `drift`, `blocked`, `quarantined`. |
| `block_reason` | Selection | `mapped_column_missing`, `tab_missing`, `header_changed`, `empty_tab`, `access_lost`, `file_trashed`, `spec_mismatch`, `duplicate_identity`. |
| `block_detail` | Text | |
| `mapping_id` | Many2one `gdrive.mapping` | The single promotion mapping, if any. |
| `promotion_enabled` | Boolean, related `mapping_id.enabled`, store | |
| `auto_heal_enabled` | Boolean, related `mapping_id.auto_heal`, store, readonly | Ships False. |
| `staged_row_ids` | One2many `gdrive.staged.row` | |
| `active` | Boolean, default True | |

`_sql_constraints`: `('dataset_uniq', 'unique(node_id, sheet_gid)', 'A tab appears once per file.')`

### 3.6 `gdrive.dataset.column` — observed header schema

`_order = 'dataset_id, col_index'`

| Field | Type | Meaning |
|---|---|---|
| `dataset_id` | Many2one, required, index, ondelete cascade | |
| `col_index` | Integer, required | 0-based physical position. Advisory only — matching is by `header_canon`. |
| `a1_letter` | Char | `A`, `B`, … `AA`. |
| `header_raw` | Char | Exactly as read. |
| `header_canon` | Char, index | `TEXT_CANON` output (tagged, e.g. `s:Invoice Number`). **The join key.** |
| `slug` | Char, required, index | `^[a-z_][a-z0-9_]*$`, derived from `header_canon`, deduplicated with `_2`, `_3`… Used as the key inside `gdrive.staged.row.payload`. |
| `sample_values` | Json | Up to 10 raw sample cells, for the mapping builder UI. |
| `observed_kind` | Selection `empty` / `text` / `number` / `bool` / `date_serial` / `mixed` / `error` | **Advisory only.** Derived from the Sheets `effectiveValue` oneof distribution. Never used to canonicalize — types come from the mapping contract, always. |
| `nonempty_count` | Integer | |
| `distinct_count` | Integer | |
| `is_mapped` | Boolean, compute, store | True when a `gdrive.mapping.column` references this `header_canon`. |

`_sql_constraints`: `('col_slug_uniq', 'unique(dataset_id, slug)', 'Column slug must be unique in a tab.')`

### 3.7 `gdrive.staged.row` — the schema-flexible landing zone

`_order = 'dataset_id, row_number'`. This is the default destination for **every** row of **every** tab, whether or not a promotion mapping exists.

| Field | Type | Meaning |
|---|---|---|
| `dataset_id` | Many2one, required, index, ondelete cascade | |
| `connection_id` | Many2one, related, store, index | For record rules and dashboards. |
| `row_number` | Integer, index | 1-based sheet row. **Display only** — never identity. |
| `a1_ref` | Char | e.g. `'Wholesale — Leads'!A412`. Every report cites this so a human can click through. |
| `sync_id` | Char, index | ULID. Present when the tab carries a `_sync_id` column, or backfilled by natural-key match. |
| `natural_key` | Char, index | Canonical, length-prefixed join of the declared key columns (§ CANONICALIZATION §5). Empty when no mapping declares one. |
| `identity_source` | Selection `sync_id` / `natural_key` / `none`, index | Which strategy produced the effective identity. `none` ⇒ report-only, delete-disabled. |
| `payload` | Json | `{slug: raw_value}` for **every** column in the tab, mapped or not. The archive of record. |
| `canon` | Json | `{key: tagged_canonical_string}` for the contract columns, keyed by Odoo field name (mapped) or slug (unmapped). |
| `h_row` | Char(32), index | Hex of the 16-byte row digest. |
| `h_row_folded` | Char(32) | Cosmetic-folded variant, drives `COSMETIC` classification. |
| `h_extra` | Char(32) | Digest of the columns **not** in the contract, so schema growth is visible without polluting the compared hash. |
| `bucket` | Integer, index | 0–255. Merkle bucket. |
| `state` | Selection, index, default `staged` | `staged`, `quarantined`, `promoted`, `missing`, `obsolete`. |
| `quarantine_reason` | Selection | `type_coercion`, `duplicate_identity`, `multi_match`, `missing_required`, `identifier_numeric`, `bad_bool`, `bad_date`, `not_a_number`, `error_cell`, `orphan_reference`, `currency_mismatch`, `nonexistent_local_time`. |
| `quarantine_detail` | Text | Includes the offending column and raw value. |
| `first_seen_date` | Datetime | |
| `last_seen_date` | Datetime, index | |
| `missing_since` | Datetime, index | Drives the delete quarantine window. |
| `promotion_link_id` | Many2one `gdrive.promotion.link`, index, ondelete set null | |
| `run_id` | Many2one `gdrive.sync.run`, index, ondelete set null | Last run that touched the row. |

Design note, stated because it will be tempting to get wrong: `payload` and `canon` are `fields.Json` and are therefore **unsearchable and ungroupable**. Every attribute the UI or engine filters, sorts, or groups on — `sync_id`, `natural_key`, `h_row`, `bucket`, `state`, `row_number` — is a real indexed column. Adding a new filterable attribute later means adding a real column and a migration, not a Json key.

### 3.8 `gdrive.mapping` — the opt-in promotion contract (header)

`_order = 'dataset_id, id'`

| Field | Type | Meaning |
|---|---|---|
| `name` | Char, required | |
| `dataset_id` | Many2one `gdrive.dataset`, required, index | One mapping per dataset. |
| `target_model_id` | Many2one `ir.model`, required, `ondelete='cascade'` | e.g. `res.partner`. |
| `target_model` | Char, related `target_model_id.model`, store | |
| `enabled` | Boolean, default **False** | **Promotion is opt-in.** Nothing is promoted until a manager flips this. |
| `state` | Selection `draft` / `validated` / `active` / `blocked`, default `draft` | `active` requires `enabled` **and** a passing validation. |
| `validation_message` | Text, readonly | Output of `action_validate()`. |
| `identity_strategy` | Selection `sync_id` / `natural_key` / `sync_id_then_key`, default `sync_id_then_key` | |
| `sync_id_column_header` | Char, default `_sync_id` | Header label of the injected id column, if present. |
| `writeback_sync_id` | Boolean, default False, readonly | Requires Drive write scope, which v1 never has. Displayed as permanently disabled with an explanatory help string. |
| `domain` | Char, default `[]` | Additional scope on the target model. |
| `default_values` | Json | Field → literal applied on create only. |
| `create_allowed` | Boolean, default True | |
| `update_allowed` | Boolean, default True | |
| `delete_policy` | Selection `never` / `report` / `soft`, default `report` | `soft` sets `active=False` (or the declared `soft_delete_field`). **Hard delete is never available to any automated path.** |
| `soft_delete_field` | Char, default `active` | |
| `auto_heal` | Boolean, default **False** | Per-dataset opt-in. Even when True, `delete_policy='soft'` still requires human approval above threshold. |
| `dry_run_default` | Boolean, default True | |
| `create_threshold_abs` | Integer, default 50 | Circuit breaker: trip if creates > max(abs, pct%·rows). |
| `create_threshold_pct` | Float, default 20.0 | |
| `delete_threshold_abs` | Integer, default 20 | |
| `delete_threshold_pct` | Float, default 5.0 | |
| `quarantine_runs` | Integer, default 2 | A row must be absent this many consecutive complete runs before it is delete-eligible. |
| `quarantine_hours` | Integer, default 24 | …and for at least this long. Both conditions, ANDed. |
| `flap_limit` | Integer, default 3 | Consecutive writes of the same (sync_id, field) before `NON_CONVERGENT_FIELD`. |
| `spec_version` | Char, compute, store | `H(serialized column contract ‖ CANON_VERSION)`. Changing any column option changes this and invalidates every cached hash. |
| `column_ids` | One2many `gdrive.mapping.column` | |
| `promotion_link_ids` | One2many `gdrive.promotion.link` | |
| `active` | Boolean, default True | |

**`action_validate()` MUST assert, and refuse to reach `validated` otherwise:**
1. Every `gdrive.mapping.column.header_canon` resolves to exactly one live `gdrive.dataset.column`.
2. Every `odoo_field_id` exists on `target_model` and is writable (not `readonly` without an inverse, not a non-stored compute).
3. Every `selection` column's `value_map` values are a subset of `fields_get(field)['selection']` keys. A new Odoo state must fail **loudly** here, not drift silently at run time.
4. Every `money` column has a resolvable currency (companion `currency_field_id` or `default_currency_id`).
5. At least one column has `is_natural_key = True` **unless** `identity_strategy == 'sync_id'` and the `_sync_id` column exists.
6. The technical fields `x_gdrive_sync_id` and `x_gdrive_source_dataset` exist on `target_model`, creating them if not (§3.10).
7. `spec_version` is recomputed and, if changed, `dataset_id` cached hashes are cleared.

### 3.9 `gdrive.mapping.column` — the per-column contract

`_order = 'mapping_id, sequence, id'`. This model is the single source of truth for canonicalization behaviour; `docs/CANONICALIZATION.md` defines exactly how each option is applied.

| Field | Type | Meaning |
|---|---|---|
| `mapping_id` | Many2one, required, index, ondelete cascade | |
| `sequence` | Integer, default 10 | |
| `dataset_column_id` | Many2one `gdrive.dataset.column` | UI convenience; the durable link is `header_canon`. |
| `header_canon` | Char, required, index | Matching key. Survives column reordering. |
| `odoo_field_id` | Many2one `ir.model.fields` | Domain-filtered to the target model. Empty ⇒ `ctype='ignore'`. |
| `odoo_field` | Char, related, store | The **hash key** (§ CANONICALIZATION §6) — stable under header renames. |
| `ctype` | Selection, required | `text`, `number`, `money`, `bool`, `date`, `datetime`, `selection`, `many2one`, `m2m`, `ignore`. |
| `required` | Boolean, default False | An empty/invalid required cell quarantines the row. |
| `is_natural_key` | Boolean, default False | Ordered by `sequence` to form the composite key. |
| `authority` | Selection `sheet` / `odoo` / `report`, default `sheet` | Exactly one authority per column. v1 writes only `sheet`-authority columns. `odoo` and `report` are report-only. |
| `empty_is_null` | Boolean, default True | |
| `text_trim` | Boolean, default True | |
| `text_collapse_ws` | Boolean, default True | Default ON for names/labels; set OFF for notes/code columns. |
| `text_case` | Selection `preserve` / `fold`, default `preserve` | `fold` uses `casefold()`, not `lower()`. |
| `fold_punct` | Boolean, default False | Affects the **cosmetic** hash only, never the strict hash. |
| `decimal_sep` | Char(1), default `.` | **Declared, never guessed.** |
| `group_sep` | Char(1), default `,` | |
| `accounting_negatives` | Boolean, default True | `(1,234.50)` → `-1234.50`. |
| `percent_mode` | Selection `none` / `divide_100`, default `none` | |
| `scale_mode` | Selection `currency` / `uom` / `fixed`, default `fixed` | |
| `scale` | Integer, default 2 | Used when `scale_mode='fixed'`. |
| `currency_field_id` | Many2one `ir.model.fields` | Companion currency on the Odoo record. |
| `default_currency_id` | Many2one `res.currency` | |
| `rel_tol` | Float, default 0.0 | Derived-float tolerance (L3 downgrade only). |
| `abs_tol` | Float, default 0.0 | |
| `date_formats` | Char, default `%Y-%m-%d,%m/%d/%Y` | Strict `strptime` patterns, in order. **No fuzzy parsing, ever.** |
| `truthy` | Char, default `true,yes,y,1,x,✓` | |
| `falsy` | Char, default `false,no,n,0` | |
| `empty_means` | Selection `false` / `null` / `error`, default `false` | |
| `value_map` | Json | Sheet label → Odoo technical key, for `selection`. |
| `comodel` | Char | For `many2one` / `m2m`. |
| `m2o_match_field` | Char, default `name` | Field on the comodel used to resolve the sheet value. |
| `m2o_create_missing` | Boolean, default **False** | Unresolvable ⇒ `ORPHAN_REFERENCE` quarantine. |
| `assert_string_value` | Boolean, default False | For identifier columns: assert the Sheets `effectiveValue` oneof is `stringValue`; raise `IDENTIFIER_NUMERIC` otherwise. **Set True on any SKU/barcode/invoice-number/postal-code column.** |

### 3.10 `gdrive.promotion.link` — row state on the Odoo side

`_order = 'mapping_id, id'`

| Field | Type | Meaning |
|---|---|---|
| `mapping_id` | Many2one, required, index, ondelete cascade | |
| `dataset_id` | Many2one, related, store, index | |
| `staged_row_id` | Many2one `gdrive.staged.row`, index, ondelete set null | |
| `sync_id` | Char, required, index | ULID. |
| `natural_key` | Char, index | Snapshot at last successful promotion. |
| `res_model` | Char, required, index | |
| `res_id` | Integer, required, index | The promoted business record. |
| `last_h_row` | Char(32) | |
| `last_promoted_date` | Datetime | |
| `last_seen_in_sheet_date` | Datetime, index | |
| `missing_since` | Datetime, index | Set on the first **complete** run in which the identity is absent; cleared the moment it reappears. |
| `missing_run_count` | Integer, default 0 | |
| `flap_counters` | Json | `{field_name: consecutive_write_count}`. |
| `state` | Selection, index, default `linked` | `linked`, `missing`, `quarantined`, `soft_deleted`, `unmanaged`, `non_convergent`. |
| `state_detail` | Text | |

`_sql_constraints`: `('link_sync_uniq', 'unique(mapping_id, sync_id)', 'One link per sync id per mapping.')`

**Technical fields on the target model.** `action_validate()` creates, if absent, via `env['ir.model.fields'].sudo().create({... 'state': 'manual' ...})`:
- `x_gdrive_sync_id` — Char, indexed, `copy=False`.
- `x_gdrive_source_dataset` — Char, `copy=False`, holding `"%s/%s" % (node.google_id, dataset.sheet_gid)`.

A partial unique index is created by raw SQL in `post_init` / on validation:
```sql
CREATE UNIQUE INDEX IF NOT EXISTS <table>_x_gdrive_sync_id_uniq
    ON <table> (x_gdrive_sync_id) WHERE x_gdrive_sync_id IS NOT NULL;
```
This index is what makes every `Create` action an idempotent upsert: a retried or duplicated create collapses into a no-op update instead of a duplicate record.

**Ownership rule, load-bearing:** a business record is a delete candidate **only** if it carries `x_gdrive_source_dataset` equal to this dataset **and** `x_gdrive_sync_id` non-null **and** a matching `gdrive.promotion.link`. Anything else in the mapping domain is `UNMANAGED` — reported, never touched. Records humans create directly in Odoo are not the sync's to delete.

### 3.11 `gdrive.verification` — one verification of one dataset

`_order = 'date desc, id desc'`

| Field | Type | Meaning |
|---|---|---|
| `name` | Char, compute | `"<tab> @ <date>"`. |
| `run_id` | Many2one `gdrive.sync.run`, index, ondelete cascade | |
| `dataset_id` | Many2one, required, index | |
| `mapping_id` | Many2one, index | Empty when the dataset is staging-only (§6.2). |
| `date` | Datetime, default now, index | |
| `mode` | Selection `cache` / `dataset` / `bucket` / `full` | Deepest layer reached (§9.1). |
| `result` | Selection `verified` / `drift` / `blocked` / `error`, index | |
| `h_dataset_sheet` | Char(64) | |
| `h_dataset_odoo` | Char(64) | |
| `rows_sheet` | Integer | |
| `rows_odoo` | Integer | |
| `buckets_differing` | Integer | |
| `rows_examined` | Integer | |
| `drift_count` | Integer | **Excludes** data-quality items. |
| `data_quality_count` | Integer | `TYPE_COERCION_MISMATCH` and friends, counted separately so "12 drifts" never silently means "12 cells I could not read". |
| `structural_count` | Integer | Header/tab/access failures. |
| `column_totals` | Json | Per numeric column, an independent `Decimal` sum computed from **raw** values on both sides. A cheap control that catches normalizer bugs the hash cannot. |
| `read_complete` | Boolean | Copied from the dataset read. |
| `duration_sec` | Float | |
| `drift_ids` | One2many `gdrive.drift` | |
| `plan_id` | Many2one `gdrive.plan` | The plan produced from this verification, if any. |
| `report_attachment_id` | Many2one `ir.attachment` | JSON + HTML artefact. |

### 3.12 `gdrive.drift` — a single drift finding

`_order = 'verification_id, severity desc, id'`

| Field | Type | Meaning |
|---|---|---|
| `verification_id` | Many2one, required, index, ondelete cascade | |
| `dataset_id` / `mapping_id` | Many2one, related, store, index | |
| `category` | Selection `drift` / `data_quality` / `structural`, required, index | Keeps the three counts disjoint. |
| `drift_type` | Selection, required, index | `missing_in_odoo`, `missing_in_sheet`, `field_mismatch`, `duplicate_identity`, `header_change`, `tab_missing`, `type_coercion`, `currency_mismatch`, `multi_match`, `orphan_reference`, `empty_tab`, `access_lost`, `unmanaged_record`, `non_convergent`, `identifier_numeric`, `schema_growth`. |
| `severity` | Selection `info` / `warning` / `critical` / `blocking`, required, index | `blocking` halts the dataset. |
| `delta_class` | Selection `cosmetic` / `rounding` / `substantive` | For `field_mismatch` only. |
| `sync_id`, `natural_key` | Char, index | |
| `staged_row_id` | Many2one, ondelete set null | |
| `res_model`, `res_id` | Char / Integer | |
| `field_name` | Char, index | |
| `canon_sheet`, `canon_odoo` | Char | Tagged canonical forms, verbatim. Debuggability depends on these being unmodified. |
| `source_ref` | Char | A1 reference. |
| `message` | Text | Human sentence. |
| `resolution` | Selection `open` / `planned` / `applied` / `ignored` / `resolved_externally`, default `open`, index | |
| `plan_action_id` | Many2one `gdrive.plan.action`, ondelete set null | |

### 3.13 `gdrive.plan` — a serialized, fingerprinted change plan

`_order = 'create_date desc'`

| Field | Type | Meaning |
|---|---|---|
| `name` | Char, compute | |
| `verification_id` | Many2one, required, index | |
| `dataset_id`, `mapping_id`, `run_id` | Many2one, related/stored, index | |
| `state` | Selection, default `preview`, index | `preview`, `approved`, `applied`, `refused_stale`, `aborted`, `expired`. |
| `dry_run` | Boolean, default True | |
| `fp_drive_version` | Char | Fingerprints captured at plan time. |
| `fp_drive_modified` | Datetime | |
| `fp_odoo_count` | Integer | |
| `fp_odoo_max_write_date` | Datetime | |
| `fp_h_sheet`, `fp_h_odoo` | Char(64) | |
| `fp_spec_version` | Char | |
| `create_count`, `update_count`, `soft_delete_count`, `quarantine_count` | Integer | |
| `breaker_tripped` | Boolean | |
| `breaker_reason` | Char | `creates_exceed_threshold`, `deletes_exceed_threshold`, `read_incomplete`, `empty_tab`, `header_blocked`, `duplicate_identity`. |
| `requires_approval` | Boolean | True when any breaker tripped, any soft-delete exists, or `auto_heal` is False. |
| `approved_by_id` | Many2one `res.users`, readonly | |
| `approved_date` | Datetime, readonly | |
| `applied_by_id` | Many2one `res.users`, readonly | |
| `applied_date` | Datetime, readonly | |
| `apply_result` | Selection `success` / `partial` / `failed` / `refused` | |
| `convergence_ok` | Boolean | Post-apply re-verification result (§9.8). |
| `expiry_date` | Datetime | Plans expire 24 h after creation; expired plans cannot be applied. |
| `action_ids` | One2many `gdrive.plan.action` | |

### 3.14 `gdrive.plan.action` — one executable step

`_order = 'plan_id, sequence, id'`

| Field | Type | Meaning |
|---|---|---|
| `plan_id` | Many2one, required, index, ondelete cascade | |
| `sequence` | Integer | Encodes execution order (§9.7): 10 = writeback, 20 = create, 30 = update, 40 = soft_delete, 50 = quarantine. |
| `batch_index` | Integer | 200 actions per savepoint. |
| `action_type` | Selection `create` / `update` / `soft_delete` / `writeback_sync_id` / `quarantine`, required, index | |
| `sync_id` | Char, index | Generated **at plan time**, never at execution time, so a retry reuses the same ULID. |
| `staged_row_id` | Many2one, ondelete set null | |
| `res_model` | Char | |
| `res_id` | Integer, index | Empty for `create`. |
| `payload` | Json | Typed values to write (`create`) — Odoo-native Python values serialized to JSON. |
| `deltas` | Json | `[{"field": …, "from": "<canon>", "to": "<canon>", "to_typed": …}]` (`update`). Only differing fields; **never** a full-record write. |
| `source_ref` | Char | |
| `state` | Selection `pending` / `applied` / `skipped` / `failed`, default `pending`, index | |
| `error` | Text | |

### 3.15 `gdrive.sync.run` / `gdrive.sync.run.line` — the run log

`gdrive.sync.run`, `_order = 'date_start desc'`:

| Field | Type | Meaning |
|---|---|---|
| `name` | Char, required | `ir.sequence` `gdrive.sync.run`, format `SYNC/%(year)s/%(range_year)s/#####`. |
| `connection_id` | Many2one, required, index | |
| `trigger` | Selection `cron` / `manual` / `button` | |
| `stages` | Char | Comma-joined stage names actually executed. |
| `mode` | Selection `delta` / `full`, default `delta` | |
| `date_start`, `date_end` | Datetime | |
| `duration_sec` | Float | |
| `state` | Selection `running` / `done` / `partial` / `failed` / `aborted`, index | |
| `complete_read` | Boolean, default False | **Run-level proof.** False if any Drive/Sheets call failed, any token expired, any page was lost, or any dataset read was partial. §9.6 forbids the delete planner from running when this is False. |
| `nodes_seen`, `nodes_ingested`, `attachments_written`, `datasets_seen`, `rows_staged`, `rows_quarantined`, `records_created`, `records_updated`, `records_soft_deleted`, `drift_count`, `error_count`, `warning_count` | Integer | Counters, `aggregator='sum'`. |
| `drive_units_used`, `sheets_reads_used` | Integer | Quota accounting. |
| `line_ids` | One2many `gdrive.sync.run.line` | |
| `log_attachment_id` | Many2one `ir.attachment` | Full structured log, gzipped JSONL. |

`gdrive.sync.run.line`, `_order = 'run_id, id'`: `run_id` (required, index, ondelete cascade), `node_id`, `dataset_id`, `stage` (Selection: `discover`/`classify`/`ingest`/`stage`/`promote`/`verify`/`report`/`heal`), `level` (Selection `info`/`warning`/`error`, index), `code` (Char, index — a stable machine code such as `EXPORT_SIZE_LIMIT`, `RATE_LIMITED`, `TAB_MISSING`), `message` (Text), `duration_ms` (Integer), `payload` (Json).

### 3.16 Transient models

- **`gdrive.connection.test.wizard`** — `connection_id`, `state` (`setup`/`result`), `result_html` (Html, readonly), `probe_ids` (One2many of a lightweight inline line model rendered as a list — implemented as a Json-backed Html block to avoid a 22nd model), `sample_file_ids` (Text listing the first 10 discovered titles). Runs probes P1–P7 (§2.4).
- **`gdrive.mapping.builder.wizard`** + **`gdrive.mapping.builder.wizard.line`** — takes a `dataset_id`, materializes one line per `gdrive.dataset.column` pre-populated with a *suggested* `ctype` and Odoo field, and on Apply creates a `gdrive.mapping` in state `draft` with `enabled = False`. Suggestions are suggestions: nothing is promoted until a human enables the mapping.
- **`gdrive.heal.wizard`** + **`gdrive.heal.wizard.line`** — the approval surface. Fields: `plan_id` (or `dataset_ids` to plan on the fly), `dry_run` (Boolean, **default True**), `state` (`setup`/`preview`/`applied`), `summary` (Text), `line_ids` (one per `gdrive.plan.action`, each with `selected` Boolean default True, `action_type`, `source_ref`, `description`), `breaker_warning` (Html). `action_preview()` recomputes the plan and re-opens the same wizard record. `action_apply()` is `groups`-gated to `group_gdrive_admin`, refuses if `dry_run` is still True, refuses if fingerprints moved, and refuses if the plan expired.

### 3.17 Abstract engines

- **`gdrive.reconciler`** (`AbstractModel`) — the **pure** planner. `plan(sheet_snapshot, odoo_snapshot, contract, policy) -> dict` with no ORM writes, no network, no clock reads except an injected `now`. Dry-run and apply call this **same** function; the only difference is whether the result is executed. If dry-run and apply have separate code paths, the preview is a lie — this is non-negotiable.
- **`gdrive.promoter`** (`AbstractModel`) — the executor. `execute(plan) -> result` and the Odoo-side snapshot reader `read_odoo_snapshot(mapping) -> rows`.

---

## 4. Google API client layer (lane B) — behavioural contract

Lane B ships **pure Python service modules** with no Odoo imports beyond `odoo.exceptions` and `logging`. They accept plain dicts and return plain dicts. Lane D/E wrap them.

### 4.1 Service construction

```python
build('drive', 'v3', credentials=creds, cache_discovery=False)
build('sheets', 'v4', credentials=creds, cache_discovery=False)
```

`cache_discovery=False` is mandatory (avoids the oauth2client file-cache warning and a filesystem write). **`googleapiclient` service objects are not thread-safe.** `services/google_client.py` MUST hand out one service per thread via `threading.local()`, keyed by `(connection_id, subject_email, api)`. Never share one `drive` object across a `ThreadPoolExecutor`.

### 4.2 Enumeration

Every `files.list` and `changes.list` call MUST pass `supportsAllDrives=True` and `includeItemsFromAllDrives=True`. Omitting them silently excludes all shared-drive content with **no error**. `supportsAllDrives=True` is also required on `files.get` and `files.get_media`.

The `fields` mask is exactly:

```
nextPageToken, incompleteSearch, files(id,name,mimeType,parents,modifiedTime,createdTime,size,
md5Checksum,version,trashed,driveId,owners(emailAddress),shortcutDetails(targetId,targetMimeType),
webViewLink,capabilities/canDownload,sharedWithMeTime)
```

`nextPageToken` MUST be requested explicitly inside the mask. Omitting it means pagination silently stops after page 1 and you conclude the user has 100 files.

`pageSize=1000`. Full-crawl strategy, in this order:

1. **User corpus (flat).** `q="trashed = false"`, `corpora='user'`. Returns My Drive *and* items shared directly with the subject, regardless of nesting. The parent tree is reconstructed client-side from the `parents` arrays. Cost: N/1000 requests.
2. **Shared drives.** `drives.list(pageSize=100, fields='nextPageToken,drives(id,name)')` — note `pageSize` max is 100 and larger values are silently coerced. Then one `files.list(corpora='drive', driveId=X, includeItemsFromAllDrives=True, supportsAllDrives=True, q="trashed = false")` per drive.
3. `corpora='allDrives'` is **not** used, because it is the mode that produces `incompleteSearch=true` — a non-error response that means results are missing. If `incompleteSearch` is ever True, the run's `complete_read` is set False and a `warning` line with code `INCOMPLETE_SEARCH` is logged.

Bounded re-crawls (a user pointing at one folder) use recursive BFS on `q="'<FOLDER_ID>' in parents and trashed = false"`.

### 4.3 Incremental sync (Changes API)

Bootstrap: `changes().getStartPageToken(supportsAllDrives=True)` for the user corpus, and `getStartPageToken(driveId=X, supportsAllDrives=True)` per shared drive. Persist to `gdrive.change.cursor`.

Each delta run replays `changes().list(pageToken=…, spaces='drive', pageSize=1000, includeRemoved=True, includeCorpusRemovals=True, restrictToMyDrive=False, supportsAllDrives=True, includeItemsFromAllDrives=True, fields="nextPageToken, newStartPageToken, changes(changeType,time,removed,fileId,driveId,file(<same mask as §4.2>))")`.

Exact semantics that MUST be implemented:
- `nextPageToken` = more pages **in this poll**. `newStartPageToken` = the cursor for the **next** poll and appears **only on the final page**. Never conflate them.
- **Commit the mirrored data first, persist the cursor last.** Saving the token before committing loses changes permanently on a crash.
- Changes are at-least-once. Every handler is idempotent.
- `removed=True` means the file left the subject's view — deleted, trashed, permission revoked, or moved out of scope. Set node `state='gone'`, `gone_since=now`, `active=False`. **Never** delete the node, never delete its attachment, never let this reach the business-record delete planner directly.
- A `404` or `Invalid Value` on the page token ⇒ set cursor `state='invalid'`, force a **full** re-enumeration this run, then mint a fresh start token.
- Tokens are per `(subject_email, drive_id)` and are not interchangeable.

### 4.4 Retry, backoff, and pacing

```python
RETRY_STATUS = {429, 500, 502, 503, 504}
RETRY_403_REASONS = {'rateLimitExceeded', 'userRateLimitExceeded', 'backendError', 'internalError'}
```

Truncated exponential backoff with full jitter: `sleep = min(2**n + random.uniform(0, 1.0), 64.0)`, up to `connection.max_retry_attempts`. Honour a `Retry-After` header in preference to the computed value.

`googleapiclient`'s built-in `execute(num_retries=N)` is **insufficient**: it does not retry `403 rateLimitExceeded`, which is exactly what Drive emits under load. Lane B's `services/retry.py::execute_with_retry(request)` wrapper is mandatory and every API call goes through it.

**Never retried:** `403 insufficientPermissions`, `403 appNotAuthorizedToFile`, `403 cannotDownloadAbusiveFile`, `403 exportSizeLimitExceeded`, `403 dailyLimitExceeded`, `404 notFound`, any `400`.

**Pacing beats retrying.** `services/rate_limiter.py` implements a token bucket per `(connection, api)`, defaulting to 50 Sheets reads/min (against Google's 60/min/user cap) and 200 000 Drive units/min. Backoff after the fact wastes far more wall-clock than pacing up front.

### 4.5 Download and export

Branch on the MIME prefix — there is no unified download call:

```python
is_native = mime.startswith('application/vnd.google-apps.')
```

`get_media()` on a native file returns `403 Only files with binary content can be downloaded`. `export_media()` on a binary returns `403 Export only supports Docs Editors files`.

Blobs: `files().get_media(fileId=…, supportsAllDrives=True)` streamed through `MediaIoBaseDownload(buf, req, chunksize=10*1024*1024)`. Native Docs/Slides/Drawings: `files().export_media(fileId=…, mimeType=EXPORT_MAP[mime])` — note `export_media` accepts **no** `supportsAllDrives` and no range. On `403 exportSizeLimitExceeded`, log code `EXPORT_SIZE_LIMIT`, set node `state='skipped'`, and (v1) stop. The `files.download` long-running-operation path is documented as the v2 remedy and is out of scope.

Blobs whose `md5Checksum` equals `node.md5_checksum` **and** which already have an `attachment_id` are skipped without downloading.

### 4.6 Sheets reading

Enumerate tabs cheaply, one request per workbook:

```python
sheets.spreadsheets().get(
    spreadsheetId=SID, includeGridData=False,
    fields="spreadsheetId,properties(title,timeZone,locale),"
           "sheets(properties(sheetId,title,index,sheetType,hidden,"
           "gridProperties(rowCount,columnCount,frozenRowCount)))").execute()
```

Read values for **all** tabs of a workbook in **one** `values().batchGet`:

```python
ranges = ["'" + title.replace("'", "''") + "'" for title in titles]   # apostrophes DOUBLED
sheets.spreadsheets().values().batchGet(
    spreadsheetId=SID, ranges=ranges, majorDimension='ROWS',
    valueRenderOption='UNFORMATTED_VALUE',
    dateTimeRenderOption='SERIAL_NUMBER').execute()
```

A tab titled `Bob's Data` becomes `'Bob''s Data'`; failing to double the apostrophe yields `400 Unable to parse range`.

`UNFORMATTED_VALUE` + `SERIAL_NUMBER` is the mandated pair — the most format-invariant combination. `FORMATTED_VALUE` MUST NEVER be hashed: re-formatting a column's number format or changing the sheet locale rewrites every string with zero data change, producing a full-dataset false drift.

Response handling:
- `valueRanges` come back in the same order as `ranges`.
- `vr.get('values', [])` — **never** `vr['values']`. A completely empty tab omits the key entirely.
- Rows are ragged: trailing empty cells are dropped, trailing empty rows vanish, interior empties are preserved as `''`. Right-pad every row to `max(len(r))` before anything else.
- `vr['range']` is the authoritative used range → `dataset.used_range`. `gridProperties.rowCount/columnCount` is the allocated grid (typically 1000×26) and MUST NOT be used to size anything.

For columns with `assert_string_value = True`, a second targeted `spreadsheets().get` with mask `sheets(properties(sheetId),data(rowData(values(effectiveValue,formattedValue))))` scoped to those columns' A1 range validates the `effectiveValue` oneof branch. `numberValue` in an identifier column ⇒ `IDENTIFIER_NUMERIC`, cell refused, row quarantined — leading zeros are already gone (`"007"` → `7`) and >15 significant digits are already mangled. `errorValue` (`#N/A`, `#REF!`, `#DIV/0!`) maps to the `e:` token family and never to NULL.

### 4.7 `.xlsx` handling

The Sheets API **cannot** read an `.xlsx` in Drive: it has no `spreadsheetId`, and `spreadsheets.get` on its file id returns 404. Do not attempt it, and do not attempt the convert-to-native-Sheet workaround (it needs write scope, and the service account's 0 GB quota makes `files.copy` fail with `storageQuotaExceeded` unless parented into a shared drive).

The supported path is download + local parse:

```python
data = execute_with_retry(drive.files().get_media(fileId=fid, supportsAllDrives=True))
wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
```

`read_only=True` streams (essential for the 2.6 MB `06-29-2026 Avatar Cashflow.xlsx` and anything larger). `data_only=True` returns cached computed values — and returns `None` if the file was never recalculated by Excel, which is logged as `XLSX_NO_CACHED_VALUES` and quarantines affected cells rather than reading them as empty. Legacy `.xls` is unsupported in v1 (node `skipped`, reason `unsupported_mime`).

Each worksheet becomes a `gdrive.dataset` with `source_kind='xlsx'` and `sheet_gid = -(1 + worksheet_index)`. Because xlsx worksheets have no stable gid, a worksheet **rename** is indistinguishable from delete+create at the index level; the reader matches first by title, then by index, and logs `XLSX_TAB_AMBIGUOUS` (blocking) if both fail.

---

## 5. The pipeline

Eight stages. Each is independently invocable, independently logged, and idempotent.

```
discover → classify → ingest → stage → promote → verify → report → heal
```

### 5.1 `discover`

Input: a `gdrive.connection`. Output: `gdrive.node` rows created/updated, `run.nodes_seen`.

- Delta mode: replay every `gdrive.change.cursor` (§4.3). Full mode: enumerate per §4.2 and mark every node not seen as `gone` **only if the run's `complete_read` is True**.
- Shortcuts are recorded but never ingested; `resolved_node_id` is linked after the pass so the target's ordering does not matter. The same underlying file reachable both directly and via a shortcut is stored once (identity is the file id) — deduplication is automatic.
- Multiple parents are stored in `parent_google_ids`; `parent_id` is the first entry that resolves. Files whose parents are all invisible become `is_orphan = True`.
- Scope rules (§3.2) are applied here. Out-of-scope nodes are recorded with `state='skipped'` so their existence is still auditable.
- `complete_read` is set False by: any non-retryable API error, any exhausted retry, `incompleteSearch=true`, an invalid change token, or a partially consumed page.

### 5.2 `classify`

Pure function `services/mimetypes.py::classify(mime, shortcut_details) -> node_type`. `ingest_policy='auto'` then resolves to a handler:

| `node_type` | Handler |
|---|---|
| `folder` | Structure only. No content. |
| `spreadsheet` | Datasets via Sheets API. Optional archival xlsx snapshot. |
| `blob` + xlsx MIME | Attachment **and** datasets via openpyxl. |
| `blob` (pdf/jpeg/docx/other) | Attachment only. |
| `document`, `presentation`, `drawing` | Attachment (PDF), plus `text/plain` for Docs. |
| `shortcut` | Resolve and skip. |
| `other_google` (forms, scripts, sites, maps…) | Metadata only, `state='skipped'`, reason `unsupported_mime`. |

### 5.3 `ingest`

Fetches bytes and/or values. Writes `ir.attachment` for content nodes, writes `gdrive.dataset` + `gdrive.dataset.column` for spreadsheet nodes. Never writes `gdrive.staged.row`.

Attachment creation (always `.sudo()` in cron context):

```python
self.env['ir.attachment'].sudo().create({
    'name': filename,
    'raw': content_bytes,          # bytes; never both raw and datas
    'res_model': 'gdrive.node',
    'res_id': node.id,
    'mimetype': mime,
    'type': 'binary',
})
```

Re-ingesting a changed file creates a **new** attachment and repoints `node.attachment_id`; the previous attachment is retained (never unlinked) so history survives. Attachment count per node is capped at `keep_versions = 5` (module constant), oldest pruned.

### 5.4 `stage`

For each `gdrive.dataset` in a `GRID` tab:

1. Read values (§4.6 / §4.7). Right-pad ragged rows.
2. Read `header_row`; canonicalize each header with `TEXT_CANON`; upsert `gdrive.dataset.column` matched on `header_canon`; compute `header_fingerprint`.
3. **Header gate.** If the dataset has an enabled mapping and any mapped `header_canon` is absent, the dataset is **hard-stopped**: `state='blocked'`, `block_reason='mapped_column_missing'`, a `blocking` drift is emitted, and **no rows are staged and no promotion runs**. Treating an absent mapped column as empty cells would write NULL over an entire Odoo column — the single most destructive failure mode in sheet sync, and it must be structurally impossible. An *unmapped* new column is non-blocking: log `SCHEMA_GROWTH` at `info`, keep syncing, and record it in `h_extra`.
4. For each data row from `first_data_row`: build `payload = {slug: raw}` for **every** column; build `canon` per the mapping contract if one exists, else with a default text contract (every column `ctype='text'`, `empty_is_null=True`) so unmapped datasets still get a stable hash; compute `h_row`, `h_row_folded`, `h_extra`, `bucket`.
5. Any cell canonicalizing to an `e:` token, or any missing `required` value, quarantines the **whole row** (`state='quarantined'`) — never a partial write. Half-written rows are worse than unwritten ones.
6. Detect duplicate identities. Two rows with the same `sync_id`, or the same `natural_key`, quarantine the **entire key group** with `quarantine_reason='duplicate_identity'` and both `a1_ref`s in the detail. Picking one arbitrarily makes runs alternate between the two rows' values forever.
7. Rows present last run but absent now (identity-matched) get `state='missing'`, `missing_since` set — **only if `last_read_complete` is True**.
8. `dataset.row_count`, `used_range`, `h_dataset_sheet`, `bucket_hashes`, `last_stage_date`, `last_read_complete` written.
9. `EMPTY_TAB` guard: 0 data rows where the previous complete run had N > 0 ⇒ `state='blocked'`, `block_reason='empty_tab'`, `blocking` drift. This is treated as a mass-delete signal, never as "all rows deleted".

**Staging is the default and is never opt-in.** Every tab of every discovered spreadsheet lands here, including `Lucas_Clothing_Shopping_List` and `Weekly time sheet`. Staging never touches a business model.

### 5.5 `promote`

Runs **only** for datasets whose `mapping_id.enabled` and `mapping_id.state == 'active'`. For everything else this stage is a no-op — the data sits in staging and that is a complete, correct outcome.

Promotion is expressed as a `gdrive.plan` and executed through the same machinery as healing (§9.7). There is no separate "initial load" code path. The first promotion of a dataset is simply a plan whose actions are all `create`, subject to the same circuit breakers — which is why `create_threshold_abs` defaults to 50 and the wizard must be used, once, deliberately, for the initial load of a large dataset (or the threshold raised on that mapping with a recorded reason).

Identity cascade, in this exact order:
1. Match by `sync_id` where present on both sides. Exact, 1:1.
2. For unmatched leftovers, match by canonical `natural_key`. On success **backfill** `x_gdrive_sync_id` on the Odoo record and `sync_id` on the staged row. This is how sync #1 bootstraps against pre-existing Odoo data with no ids anywhere.
3. A natural-key match that is 1:many ⇒ `MULTI_MATCH`, quarantine, human decision.
4. Remaining sheet-only rows ⇒ `create` candidates. Remaining Odoo-only records ⇒ `missing_in_sheet`, subject to every delete guard.

Because v1 has no Drive write scope, `sync_id` is never written back to the sheet. Consequence, stated explicitly: for datasets with no `_sync_id` column, identity is natural-key-only, and **deletes are disabled** (`delete_policy` is forced to `report`) for any mapping whose `identity_strategy` resolves to `natural_key` at run time. A typo fix in a key column would otherwise read as delete + create.

### 5.6 `verify`

The layered comparison of §9.1–§9.5. Produces one `gdrive.verification` and N `gdrive.drift` per dataset.

### 5.7 `report`

Materializes each `gdrive.verification` into an `ir.attachment` (`res_model='gdrive.verification'`, `res_id=verification.id`) containing:
- `verification.json` — the machine artefact: fingerprints, hashes, counts, every drift with `canon_sheet`/`canon_odoo`/`source_ref`.
- `verification.html` — a human artefact grouped by drift type, with Drive deep links (`node.web_view_link`) and A1 references.

A run-level digest is posted to the `gdrive.connection` chatter when `drift_count > 0` or `structural_count > 0`. Report-first is the product's default posture: **a verification run with `auto_heal = False` everywhere is a complete, useful, non-mutating product.**

### 5.8 `heal`

Never runs from the scheduler unless `mapping.auto_heal` is True (ships False), and even then never executes `soft_delete` actions above threshold without human approval. Everything else goes through `gdrive.heal.wizard`. §9.

---

## 6. Cron design

All crons live in `data/ir_cron_data.xml` inside `<data noupdate="1">`. **`noupdate="1"` is mandatory**: without it, every `-u gdrive_odoo_sync` upgrade overwrites the administrator's interval/active/nextcall changes back to the shipped values.

No `numbercall`, no `doall` (removed in Odoo 18). `state` is always `code`. `user_id` is `base.user_root`. `model_id` refs use the auto-generated `model_<name_with_underscores>` external ids.

| xml_id | Name | Model | Code | Interval | Priority |
|---|---|---|---|---|---|
| `ir_cron_gdrive_discover` | GDrive Sync: Discover (delta) | `gdrive.connection` | `model._cron_discover()` | 15 minutes | 5 |
| `ir_cron_gdrive_ingest` | GDrive Sync: Ingest queued nodes | `gdrive.node` | `model._cron_ingest()` | 30 minutes | 6 |
| `ir_cron_gdrive_stage` | GDrive Sync: Stage spreadsheet tabs | `gdrive.dataset` | `model._cron_stage()` | 1 hours | 7 |
| `ir_cron_gdrive_promote` | GDrive Sync: Promote mapped datasets | `gdrive.mapping` | `model._cron_promote()` | 1 hours | 8 |
| `ir_cron_gdrive_verify` | GDrive Sync: Verify & report | `gdrive.dataset` | `model._cron_verify()` | 1 days | 9 |
| `ir_cron_gdrive_full_resync` | GDrive Sync: Weekly full recompute | `gdrive.connection` | `model._cron_full_resync()` | 1 weeks | 10 |
| `ir_cron_gdrive_gc` | GDrive Sync: Housekeeping | `gdrive.sync.run` | `model._cron_gc()` | 1 days | 20 |

Reference XML (18-safe):

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
  <data noupdate="1">
    <record id="ir_cron_gdrive_discover" model="ir.cron">
      <field name="name">GDrive Sync: Discover (delta)</field>
      <field name="model_id" ref="model_gdrive_connection"/>
      <field name="state">code</field>
      <field name="code">model._cron_discover()</field>
      <field name="interval_number">15</field>
      <field name="interval_type">minutes</field>
      <field name="active" eval="True"/>
      <field name="user_id" ref="base.user_root"/>
      <field name="priority">5</field>
      <field name="nextcall" eval="(DateTime.now() + timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M:%S')"/>
    </record>
  </data>
</odoo>
```

**Cron behavioural rules:**
- Every cron method is a **batch driver with a wall-clock budget** (`_CRON_BUDGET_SEC = 600`). It processes a bounded slice, `self.env.cr.commit()`s at batch boundaries (200 records), and re-`browse`s afterwards. Long network work belongs in crons, never in a form button — Odoo.sh cuts HTTP requests at `limit_time_real` (default 120 s).
- When the budget is exhausted with work remaining, the cron calls `self.env.ref('gdrive_odoo_sync.ir_cron_gdrive_<x>')._trigger()` to schedule an immediate follow-up rather than waiting a full interval.
- `_cron_full_resync` sets `connection.full_resync_requested = True` and clears `dataset.h_dataset_sheet/h_dataset_odoo/bucket_hashes` so the next verify pass is a full recompute. **The fast path is an optimization built on assumptions; the periodic full pass is what catches the day one of those assumptions is wrong.** A full recompute is also forced on every module upgrade via `post_init` / a `spec_version` mismatch.
- `_cron_gc` prunes `gdrive.sync.run` older than 90 days (configurable via `gdrive_odoo_sync.run_retention_days`), expires `gdrive.plan` records past `expiry_date`, and deletes `gdrive.staged.row` in state `obsolete` older than 30 days.
- Odoo 18 auto-deactivates a cron after repeated failures (`failure_count` / `first_failure_date`). Therefore **no cron method may raise**: each catches per-entity exceptions, records them as `gdrive.sync.run.line` at level `error`, sets `run.state='partial'`, and returns normally. A raise is reserved for a genuinely unrecoverable module-level fault.
- Every cron acquires a per-connection advisory lock (`SELECT pg_try_advisory_xact_lock(hashtext('gdrive_odoo_sync'), connection.id)`) and returns immediately if it cannot, so overlapping runs are impossible.

---

## 7. Security model

### 7.1 Groups (`security/gdrive_security.xml`)

| xml_id | Name | Implies | Purpose |
|---|---|---|---|
| `group_gdrive_user` | Google Drive Sync / User | `base.group_user` | Read nodes, datasets, staged rows, verifications, drift. Read-only consumer of the report. |
| `group_gdrive_manager` | Google Drive Sync / Manager | `group_gdrive_user` | Author and enable mappings, trigger runs, preview plans. Cannot apply a plan. |
| `group_gdrive_admin` | Google Drive Sync / Administrator | `group_gdrive_manager` | Manage connections, apply plans, enable `auto_heal`. |

Credential entry (`res.config.settings` fields for the SA key) is additionally gated with `groups="base.group_system"` — strictly narrower than `group_gdrive_admin`.

The category is `base.module_category_productivity`.

### 7.2 `security/ir.model.access.csv`

Header (authoritative order):

```
id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink
```

Every one of the 21 models gets rows. **No row may have an empty `group_id:id`** — an empty group grants that permission to every user including portal and public. Conversely, a model with zero rows is unusable by non-superusers. Transient wizards need their own rows.

Pattern (abbreviated; lane A writes all of them):

| Model | user | manager | admin |
|---|---|---|---|
| `gdrive.connection` | 1,0,0,0 | 1,0,0,0 | 1,1,1,1 |
| `gdrive.scope.rule` | 1,0,0,0 | 1,1,1,1 | 1,1,1,1 |
| `gdrive.change.cursor` | 0,0,0,0 *(no row for user)* | 1,0,0,0 | 1,1,1,1 |
| `gdrive.node` | 1,0,0,0 | 1,1,0,0 | 1,1,1,1 |
| `gdrive.dataset` | 1,0,0,0 | 1,1,0,0 | 1,1,1,1 |
| `gdrive.dataset.column` | 1,0,0,0 | 1,1,0,0 | 1,1,1,1 |
| `gdrive.staged.row` | 1,0,0,0 | 1,1,0,0 | 1,1,1,1 |
| `gdrive.mapping` | 1,0,0,0 | 1,1,1,1 | 1,1,1,1 |
| `gdrive.mapping.column` | 1,0,0,0 | 1,1,1,1 | 1,1,1,1 |
| `gdrive.promotion.link` | 1,0,0,0 | 1,0,0,0 | 1,1,1,1 |
| `gdrive.verification` | 1,0,0,0 | 1,0,0,0 | 1,1,1,1 |
| `gdrive.drift` | 1,0,0,0 | 1,1,0,0 | 1,1,1,1 |
| `gdrive.plan` | 1,0,0,0 | 1,1,1,0 | 1,1,1,1 |
| `gdrive.plan.action` | 1,0,0,0 | 1,1,1,0 | 1,1,1,1 |
| `gdrive.sync.run` | 1,0,0,0 | 1,1,1,0 | 1,1,1,1 |
| `gdrive.sync.run.line` | 1,0,0,0 | 1,0,0,0 | 1,1,1,1 |
| all 5 transient wizards | — | 1,1,1,1 | 1,1,1,1 |

`gdrive.plan.action` write for managers exists only so the heal wizard can toggle `selected`; the **apply** entry point is guarded in Python by `self.env.user.has_group('gdrive_odoo_sync.group_gdrive_admin')`, not by ACL alone.

### 7.3 Record rules (`security/gdrive_record_rules.xml`)

- **Multi-company, global** (empty `groups`, therefore ANDed with everything) on `gdrive.connection`, `gdrive.node`, `gdrive.dataset`, `gdrive.staged.row`, `gdrive.sync.run`:
  `['|', ('company_id','=',False), ('company_id','in',company_ids)]`
  Global rules can never be widened by a group rule — this is deliberate, and it is also the reason no other global rule is shipped. Adding a global rule "for convenience" can lock administrators out of data they need.
- No ownership rules. This is an administrative tool; the group hierarchy is the access model.

### 7.4 Secrets

Covered in §2.5. Restated as invariants:
1. The SA key is read from an env var first, `ir.config_parameter` second.
2. Every `ir.config_parameter` access is `.sudo()`.
3. The key never appears in a data XML file, never on a regular model's field, never in a log line, never in a `gdrive.sync.run.line.payload`. Lane B's error formatter MUST redact any string containing `-----BEGIN PRIVATE KEY-----` or `"private_key"`.
4. `res.config.settings` exposes it with `password="True"` and `groups="base.group_system"`.

### 7.5 Attachment access

`ir.attachment` ACL derives from `(res_model, res_id)`. Attachments are linked to `gdrive.node`, so `group_gdrive_user` read on `gdrive.node` grants read on the mirrored files. All cron-side attachment writes use `.sudo()`. `public` is never set True and `access_token` is never generated automatically.

---

## 8. Configuration guide (what an administrator actually does)

1. **Install** the module. Menus appear under **Google Drive Sync**.
2. **Settings → Google Drive Sync** (`res.config.settings`): paste the service-account JSON (or, preferred, set `GDRIVE_ODOO_SYNC_SA_KEY` on the Odoo.sh project and leave the field empty).
3. **Google Drive Sync → Configuration → Connections → New**: name it, leave `auth_mode = dwd`, set `subject_email = lucaso@avatarnaturalfoods.com`.
4. Read `sa_client_id` off the connection form and complete the Admin-console DWD grant (§2.2). Wait for propagation.
5. **Test Connection.** All of P1–P4 must be green. `state` becomes `ok`.
6. **Run Discovery (manual).** Inspect **Drive Nodes** — the tree should show My Drive, subfolders, and folders shared by `michael@`, `Diego@`, `lvxxcas@gmail.com`.
7. Let ingest and stage run. Inspect **Datasets**: `Food CPG Master — Investor Directory (79)` and `… Companies (36)` will appear as separate `gdrive.dataset` records even if a tab title repeats, because identity is `(node_id, sheet_gid)`. Both `Bettr_Bowl_Data_Request` files appear as two distinct nodes because identity is the Drive file id.
8. Inspect **Staged Rows** for a dataset. This is already the whole staging product; nothing further is required.
9. **Only when a dataset genuinely belongs in a business model**: open it, run **Build Mapping**, set each column's `ctype`, `odoo_field`, `is_natural_key`, and `assert_string_value` where the column is an identifier. Validate. Then, deliberately, set `enabled = True`.
10. Leave `auto_heal = False`. Read the drift reports for at least a week before considering otherwise.

---

## 9. Verification and healing

`docs/CANONICALIZATION.md` defines the exact algorithms. This section defines the policy around them.

### 9.1 Layered comparison

| Layer | What it does | Cost |
|---|---|---|
| **L0 Drive** | Skip the file entirely if `drive_version == last_drive_version` **and** `drive_modified_time == last_drive_modified` **and** not trashed. `version` increments on metadata-only edits, so it errs toward "changed" — the correct direction. `md5Checksum` and `headRevisionId` MUST NOT be used: they are blob-only and simply absent on native Sheets. | 1 metadata call, or 0 in delta mode. |
| **L0b Odoo** | Skip only if `(search_count(domain), max(write_date))` equals `(last_odoo_count, last_odoo_max_write_date)`. **Both are required**: a delete does not advance `max(write_date)`, and an in-place edit does not change the count. | 2 cheap queries. |
| | L0 **and** L0b both clean ⇒ `mode='cache'`, `result='verified'`. Zero API cost. This is the half everyone forgets — a file can be untouched while Odoo changed. | |
| **L1 Dataset** | One `batchGet` for all tabs; canonicalize; compute `h_dataset_sheet`. Compute `h_dataset_odoo` identically from `search_read`. Equal ⇒ `verified`, done. No row-level work. | 1 Sheets read/workbook. |
| **L2 Bucket** | Compare the 256 bucket hashes. Typically 1–2 differ, so ~0.4 % of rows are materialized. | 0 API cost. |
| **L3 Row/field** | Within differing buckets, join by identity, compare `h_row`, and only for mismatches compare field by field. | 0 API cost. |

**Cache invalidation is mandatory and non-negotiable.** Every stored hash is keyed by `spec_version = H(contract ‖ CANON_VERSION)`. Change a normalization rule and every cached hash becomes invalid. Serving a stale hash computed by an older normalizer as `verified` is a silent **false pass** — the worst possible failure of a verification system.

### 9.2 Independent controls

Alongside hashes, every verification records `rows_sheet`, `rows_odoo`, and `column_totals` — a `Decimal` sum per numeric column computed from **raw** values, not canonical ones. If both sides are canonicalized wrongly in the same way, hashes agree *and* canonical totals agree; raw totals do not. This is the only check that catches a symmetric normalizer bug.

### 9.3 Drift taxonomy

**Category `drift`** (counted in `drift_count`):

| Type | Severity | Default action |
|---|---|---|
| `missing_in_odoo` | warning | `create` candidate, subject to the create breaker. |
| `missing_in_sheet` | warning | `missing_in_sheet` drift + link `state='missing'`. Never an immediate delete. |
| `field_mismatch` | warning | Per-field delta, classified `cosmetic` / `rounding` / `substantive`. |
| `currency_mismatch` | critical | Never auto-written. Comparing bare amounts across currencies is meaningless. |
| `unmanaged_record` | info | Reported. Never touched. |
| `non_convergent` | critical | Writing that field is stopped. §9.8. |

**Category `data_quality`** (counted in `data_quality_count`, never in `drift_count`):
`type_coercion`, `identifier_numeric`, `orphan_reference`, `multi_match`, `duplicate_identity`.

**Category `structural`** (counted in `structural_count`, all `blocking` except where noted):
`header_change` (mapped column missing/renamed — hard stop), `schema_growth` (unmapped column added — `info`, non-blocking), `tab_missing` (gid absent from `spreadsheets.get` — hard stop, **never** "all rows deleted"), `empty_tab` (hard stop), `access_lost` (403/404 on a previously readable file — hard stop and page a human).

Column **reordering** is a no-op by construction: columns resolve by `header_canon`, rows hash by `odoo_field`.

### 9.4 `field_mismatch` classification

- **`cosmetic`** — strict hashes differ but folded hashes match (smart quotes, case, whitespace runs). Reported, **not** auto-written by default. Folding real edits in the primary canonical form would hide genuine changes and prevent convergence.
- **`rounding`** — numeric, and the pre-quantization absolute difference is ≤ 0.51 × the rounding step. Reported, **not** auto-written; writing it makes the value flap between runs.
- **`substantive`** — everything else. Eligible for `update`.

### 9.5 Write direction

Exactly **one** authority per column (`sheet` / `odoo` / `report`). Only `sheet`-authority columns are ever written. Bidirectional sync is a trap and is not implemented in v1; if it is ever added it MUST be a 3-way merge against the last synced canonical snapshot, with "both sides differ from base" resolving to `CONFLICT` and never auto-resolving.

### 9.6 Delete guards

Deletes get a materially higher evidence bar than creates and updates, for three independent reasons:

1. **Asymmetric cost.** A wrongly created record is deleted in seconds. A wrongly deleted Odoo record takes its journal entries, attachments, message threads and many2one back-references with it, may be legally required to exist, and often cannot be restored at all.
2. **Asymmetric evidence.** Creates and updates are asserted by positive data present in the source. Deletes are inferred from **absence** — and absence is exactly what every read failure looks like: an empty service-account corpus, an expired token, a renamed tab, a partial `batchGet`, a range that stopped at row 1000, a hidden filter view, a wrong Odoo domain, a `spec_version` bump mid-deploy. Every one of those maps precisely onto "delete everything". There is no read bug whose signature is "invent 4000 new rows".
3. **Non-locality.** A create or update touches one record. A mass-delete event touches the whole dataset at once and is the only failure unbounded in blast radius.

Therefore, **all** of the following must hold before a `soft_delete` action may even enter a plan:

1. `mapping.delete_policy == 'soft'` (never the default).
2. The record is **owned**: matching `x_gdrive_source_dataset`, non-null `x_gdrive_sync_id`, and a live `gdrive.promotion.link`. Anything else is `unmanaged`.
3. `run.complete_read` is True **and** `dataset.last_read_complete` is True.
4. `dataset.state` is not `blocked` and no `blocking` drift exists for this dataset in this verification.
5. The identity has been absent for ≥ `mapping.quarantine_runs` consecutive complete runs **and** ≥ `mapping.quarantine_hours`.
6. The delete count is ≤ `max(delete_threshold_abs, delete_threshold_pct% × rows_odoo)`; above that the plan trips the breaker, `requires_approval = True`, and **nothing is executed** until a human approves.
7. `identity_strategy` resolved to `sync_id` for that row (natural-key-only identity forces `delete_policy = 'report'`).

Execution is a **soft** delete — `active = False` (or `mapping.soft_delete_field`) with `x_gdrive_sync_id` retained, so a restore is one flag flip. **Hard delete is never available to any automated path, at any threshold, under any configuration.** It is a human action performed in the Odoo UI.

Creates get a looser but real breaker: `max(create_threshold_abs, create_threshold_pct% × rows_sheet)`. Exceeding it means the identity strategy broke — a renamed key column, a wrong domain, an empty Odoo read — not that 4000 invoices appeared.

### 9.7 Plan execution

Order is fixed by `sequence`: `writeback_sync_id` (10) → `create` (20) → `update` (30) → `soft_delete` (40) → `quarantine` (50). **Deletes go last and conditionally**, because an error earlier in the run is evidence that the system's view of the world is incomplete: if any action before sequence 40 failed unexpectedly, all `soft_delete` actions are skipped with `state='skipped'` and reason `earlier_errors`.

- One savepoint per batch of 200. On batch failure, roll back the batch and retry rows individually, quarantining the offender. Never leave a partially applied batch.
- Every `create` is an upsert keyed by the partial unique index on `x_gdrive_sync_id`. A retried create becomes a no-op update.
- ULIDs are generated at **plan** time, so retries reuse the same id.
- Every `update` writes **only** the differing fields. A full-record `write()` stomps fields the sync does not manage and bumps `write_date` on everything, poisoning the L0b Odoo fast path.
- Before executing, `apply()` re-reads `(drive_version, drive_modified, odoo_count, odoo_max_write_date, h_sheet, h_odoo, spec_version)` and **refuses** with `state='refused_stale'` if any changed. This turns "someone edited the sheet between preview and approval" from a corruption into a retry.

### 9.8 Convergence

After executing a plan, both dataset hashes are recomputed and asserted equal. If they are not, `convergence_ok = False`, a `non_convergent` drift is raised at `critical`, and the system **alerts rather than retries**.

The bug class this catches is the killer: the comparator says A ≠ B, the writer writes A, but A round-trips through Odoo as A′, and A′ ≠ A under the normalizer — so the next run writes it again, forever. Without this assertion the symptom is invisible: the dashboard says "3 fixes applied" every single night and everyone assumes it is working.

The flap detector backs it up: `gdrive.promotion.link.flap_counters` counts consecutive runs in which a given `(sync_id, field)` was written. At `mapping.flap_limit` (default 3) the field stops being written, `state='non_convergent'` is set, and the drift record includes both canonical forms — which tells the maintainer exactly which normalization rule is asymmetric.

---

## 10. Testing requirements (lane F)

`tests/` MUST be imported from `tests/__init__.py` and MUST NOT be imported from the module's top-level `__init__.py` — Odoo imports it itself only under `--test-enable`, and importing it at module load breaks production installs. Test modules not listed in `tests/__init__.py` are silently skipped.

```
odoo-bin -d testdb -i gdrive_odoo_sync --test-enable --test-tags=/gdrive_odoo_sync --stop-after-init
```

Required coverage:
- **Lane C (pure, no Odoo)** — table-driven tests over every canonicalization rule in `docs/CANONICALIZATION.md`, plus property-based tests (Hypothesis if available; otherwise a fixed hostile corpus) asserting `CANON(x) == CANON(CANON_roundtrip(x))` and that distinct type families never collide.
- **Merkle** — a sorted dataset and a shuffled dataset produce identical `h_dataset`. One changed cell changes exactly one bucket.
- **Google layer** — fully mocked `HttpMock`/stub transport. Assert: `with_subject` returns a new object; `supportsAllDrives` present on every list/get/media call; `nextPageToken` present in every `fields` mask; `newStartPageToken` persisted only from the final page; `403 rateLimitExceeded` retried; `403 insufficientPermissions` not retried; apostrophe doubling in A1 ranges; `vr.get('values', [])` on an empty tab.
- **Ragged rows** — `[[1,2,3],[4]]` right-pads to width 3 and hashes stably.
- **Header gate** — removing a mapped column blocks the dataset and stages zero rows.
- **Delete guards** — each of the seven conditions in §9.6 independently blocks a `soft_delete` action.
- **Idempotency** — applying the same plan twice produces the same Odoo state and creates no duplicates.
- **Staleness** — mutating a fingerprint between preview and apply yields `refused_stale`.
- **Convergence** — a deliberately asymmetric normalizer is detected by the post-apply assertion and by the flap counter at N=3.
- **Views** — `@tagged('post_install','-at_install')` install test asserting every view in `data` loads (catches `<tree>`, `attrs=`, and missing-field-in-expression regressions).

---

## 11. Explicit non-goals for v1

- No writes to Google Drive of any kind. No `_sync_id` write-back, no cell repair, no file creation. The OAuth scopes are read-only, structurally.
- No dependency on Odoo Enterprise `documents`.
- No bidirectional sync.
- No hard deletes from any automated path.
- No `.xls` (legacy), Google Forms, Apps Script, Sites, or Jamboard ingestion.
- No `files.download` long-running-operation path for >10 MB exports.
- No per-user Admin SDK sweep (`users.list` + per-user impersonation). One subject: `lucaso@avatarnaturalfoods.com`.
