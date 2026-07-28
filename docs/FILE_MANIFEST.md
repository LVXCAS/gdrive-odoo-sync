# `gdrive_odoo_sync` — File Manifest

Every file in the deliverable, exactly once, assigned to exactly one of six lanes. **No path appears in two lanes.** Paths are repo-relative; the addon root is `gdrive_odoo_sync/`.

Read `docs/SPEC.md` before implementing anything, and `docs/CANONICALIZATION.md` before touching lanes C or E.

## Lane boundaries and cross-lane contracts

| Lane | Owns | Depends on (import direction) |
|---|---|---|
| **A** | Packaging, security, XML data, docs | nothing |
| **B** | Google API client layer — pure Python services | nothing in this repo (stdlib + `google-*` + `openpyxl`) |
| **C** | Canonicalization + hashing library — **dependency-free stdlib only** | nothing, ever |
| **D** | Core Odoo models: connection, node mirror, dataset, staged row, run log | B, C |
| **E** | Mapping/promotion engine, verification/drift engine, heal wizard | B, C, D |
| **F** | Views/XML UI, menus, tests | none at import time; tests exercise A–E |

**Hard rules for lane isolation:**
- Lane C MUST NOT `import odoo`, MUST NOT import lane B, and MUST NOT import any third-party package. It is `hashlib`, `decimal`, `unicodedata`, `datetime`, `zoneinfo`, `json`, `re` only. This makes it unit-testable outside Odoo and byte-reproducible.
- Lane B MUST NOT import lane C or lane D. It may import `odoo.exceptions` and `logging` only. It exchanges plain dicts/lists/bytes.
- Lane D MUST NOT import lane E. Lane E imports lane D freely.
- Lanes A and F never import Python from B–E; they reference model names and XML ids declared in `docs/SPEC.md`.

**The one `__init__.py` exception:** lane A owns every `__init__.py` on the installed module's import path. `gdrive_odoo_sync/tests/__init__.py` is **not** on that path — Odoo imports the `tests` package separately, only under `--test-enable`, and it must never be imported from the top-level `__init__.py`. It is therefore owned by lane F, which owns the test files it enumerates.

---

## Lane A — packaging, security, data, docs

| Path | Lane | Responsibility |
|---|---|---|
| `requirements.txt` | A | Repo-root pip requirements installed by Odoo.sh at build: `google-api-python-client`, `google-auth`, `google-auth-httplib2`. Must **not** list `requests` (Odoo pins it) or `openpyxl` (Odoo ships it). |
| `gdrive_odoo_sync/__manifest__.py` | A | The manifest dict exactly as specified in SPEC §1: `version` `18.0.1.0.0`, `depends` `['base','mail']`, `external_dependencies.python` = `['google.oauth2','googleapiclient','openpyxl']` (import names, not pip names), and the ordered `data` list — security before views, `gdrive_menus.xml` last. |
| `gdrive_odoo_sync/__init__.py` | A | `from . import models` / `from . import wizard`. Declares `post_init_hook` / `uninstall_hook` functions with the Odoo 17/18 `def hook(env)` signature. `post_init` forces a full-recompute flag and creates the partial unique index helper. MUST NOT import `tests`. |
| `gdrive_odoo_sync/models/__init__.py` | A | Imports every module in `models/` in dependency order: `gdrive_connection`, `gdrive_scope_rule`, `gdrive_change_cursor`, `gdrive_node`, `gdrive_dataset`, `gdrive_dataset_column`, `gdrive_staged_row`, `gdrive_sync_run`, `gdrive_sync_run_line`, `gdrive_reconciler`, `gdrive_promoter`, `gdrive_mapping`, `gdrive_mapping_column`, `gdrive_promotion_link`, `gdrive_verification`, `gdrive_drift`, `gdrive_plan`, `gdrive_plan_action`, `res_config_settings`. |
| `gdrive_odoo_sync/lib/__init__.py` | A | Re-exports lane C's public surface: `CANON`, `TEXT_CANON`, `NUM_CANON`, `DATE_CANON`, `DATETIME_CANON`, `BOOL_CANON`, `h_row`, `h_bucket`, `h_dataset`, `bucket_of`, `jcs`, `new_ulid`, `CANON_VERSION`. |
| `gdrive_odoo_sync/services/__init__.py` | A | Re-exports lane B's public surface: `build_services`, `load_service_account_info`, `execute_with_retry`, `TokenBucket`, `DriveDiscovery`, `DriveDownloader`, `DriveChanges`, `SheetsReader`, `XlsxReader`, `classify`, `EXPORT_MAP`. |
| `gdrive_odoo_sync/wizard/__init__.py` | A | Imports `gdrive_connection_test_wizard`, `gdrive_mapping_builder_wizard`, `gdrive_heal_wizard`. |
| `gdrive_odoo_sync/security/gdrive_security.xml` | A | `res.groups`: `group_gdrive_user` (implies `base.group_user`), `group_gdrive_manager` (implies user), `group_gdrive_admin` (implies manager). Category `base.module_category_productivity`. |
| `gdrive_odoo_sync/security/ir.model.access.csv` | A | ACL rows for all 21 models per SPEC §7.2. Header exactly `id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink`. **No row may have an empty `group_id:id`.** Every transient wizard gets a row. |
| `gdrive_odoo_sync/security/gdrive_record_rules.xml` | A | Global (empty-`groups`, therefore ANDed) multi-company `ir.rule` on `gdrive.connection`, `gdrive.node`, `gdrive.dataset`, `gdrive.staged.row`, `gdrive.sync.run` using `['|',('company_id','=',False),('company_id','in',company_ids)]`. No other rules. |
| `gdrive_odoo_sync/data/ir_cron_data.xml` | A | The seven crons of SPEC §6 inside `<data noupdate="1">`. `state`=`code`, `user_id`=`base.user_root`, `model_id` via `model_<name>` refs. **No `numbercall`, no `doall`** (removed in Odoo 18). |
| `gdrive_odoo_sync/data/ir_config_parameter_data.xml` | A | `<data noupdate="1">` seeds of **non-secret** defaults only: `gdrive_odoo_sync.default_subject` = `lucaso@avatarnaturalfoods.com`, `gdrive_odoo_sync.sheets_reads_per_min` = `50`, `gdrive_odoo_sync.drive_units_per_min` = `200000`, `gdrive_odoo_sync.run_retention_days` = `90`, `gdrive_odoo_sync.plan_expiry_hours` = `24`. **The service-account key never appears here.** |
| `gdrive_odoo_sync/data/ir_sequence_data.xml` | A | `ir.sequence` `gdrive.sync.run`, prefix `SYNC/%(range_year)s/`, padding 5, inside `<data noupdate="1">`. |
| `gdrive_odoo_sync/README.md` | A | Install, DWD setup walkthrough (SPEC §2.2), env-var vs `ir.config_parameter` guidance, the "service account sees nothing by default" warning up top, upgrade notes, and the `odoo-bin --test-enable` command. |
| `gdrive_odoo_sync/static/description/icon.png` | A | 140×140 app icon referenced by the root menu's `web_icon`. |
| `gdrive_odoo_sync/static/description/index.html` | A | App-store description page. Static, no external assets. |

---

## Lane B — Google API client layer (pure Python services)

Every file here is plain Python with no Odoo model code. All network calls go through `execute_with_retry`. All list/get/media calls pass `supportsAllDrives=True`; all list calls pass `includeItemsFromAllDrives=True`; all `fields` masks include `nextPageToken` explicitly. All service objects are per-thread.

| Path | Lane | Responsibility |
|---|---|---|
| `gdrive_odoo_sync/services/errors.py` | B | Exception hierarchy: `GDriveError`, `GDriveAuthError`, `GDriveScopeError`, `GDriveQuotaError`, `GDrivePermanentError`, `GDriveExportTooLarge`, `GDriveTokenInvalid`, `GDriveIncompleteRead`. Plus `redact(text)` which strips anything resembling a private key from log/error strings. |
| `gdrive_odoo_sync/services/google_auth.py` | B | `load_service_account_info(env, connection) -> dict` implementing env-var-first, `ir.config_parameter`-second resolution with mandatory `.sudo()`. `build_credentials(info, scopes, subject)` — **captures the return value of `with_subject()`**. Exposes `SCOPES` as the frozen read-only pair. Raises `GDriveScopeError` with actionable text on `unauthorized_client`. |
| `gdrive_odoo_sync/services/google_client.py` | B | Thread-local service factory. `build_services(connection_ctx) -> (drive, sheets)` using `build(..., cache_discovery=False)`, keyed by `(connection_id, subject_email, api, thread_id)`. Documents and enforces that service objects are never shared across threads. |
| `gdrive_odoo_sync/services/retry.py` | B | `execute_with_retry(request, max_attempts, max_backoff)`. Retries `{429,500,502,503,504}` and `403` whose reason is in `{rateLimitExceeded,userRateLimitExceeded,backendError,internalError}`. Never retries `insufficientPermissions`, `appNotAuthorizedToFile`, `exportSizeLimitExceeded`, `dailyLimitExceeded`, `404`, any `400`. Backoff `min(2**n + uniform(0,1), 64)`; honours `Retry-After`. |
| `gdrive_odoo_sync/services/rate_limiter.py` | B | `TokenBucket` per `(connection, api)`. Defaults 50 Sheets reads/min, 200 000 Drive units/min. Blocking `acquire(cost)` with a deadline. Pacing up front rather than backing off after the fact. |
| `gdrive_odoo_sync/services/mimetypes.py` | B | `classify(mime, shortcut_details) -> node_type` using the `application/vnd.google-apps.` prefix test. `EXPORT_MAP` (Docs/Slides/Drawings → `application/pdf`; Docs also → `text/plain`; **native Sheets deliberately absent**). `extension_for(mime)`, `is_native(mime)`, `is_folder(mime)`, `is_spreadsheet_blob(mime)`. |
| `gdrive_odoo_sync/services/drive_discovery.py` | B | `DriveDiscovery`: paginated `files.list` for the user corpus; `drives.list` (pageSize max 100) then one `corpora='drive', driveId=X` pass per shared drive; recursive BFS helper for bounded folder crawls. Emits `incompleteSearch` as `GDriveIncompleteRead` signalling. Owns the canonical `FILE_FIELDS` mask. |
| `gdrive_odoo_sync/services/drive_changes.py` | B | `DriveChanges`: `getStartPageToken` bootstrap (user corpus and per-drive), `poll(token) -> (changes, new_start_token)`. Yields `newStartPageToken` **only from the final page**, and never conflates it with `nextPageToken`. Raises `GDriveTokenInvalid` on 404/`Invalid Value`. |
| `gdrive_odoo_sync/services/drive_download.py` | B | `DriveDownloader.fetch(file_id, mime) -> (bytes, effective_mime)`. Branches on the native/blob prefix: `get_media` + `MediaIoBaseDownload` (10 MB chunks) for blobs, `export_media` for native. Raises `GDriveExportTooLarge` on `403 exportSizeLimitExceeded`. Honours `max_blob_bytes` and `capabilities.canDownload`. |
| `gdrive_odoo_sync/services/sheets_reader.py` | B | `SheetsReader.list_tabs(sid)` (cheap `spreadsheets.get` with a properties-only fields mask). `SheetsReader.read_all(sid, titles)` — one `values.batchGet` for the whole workbook, `UNFORMATTED_VALUE` + `SERIAL_NUMBER`, apostrophes doubled in quoted A1 ranges, `vr.get('values', [])`, right-padding to max width, returning `used_range`. `SheetsReader.read_effective_values(sid, title, a1)` for `assert_string_value` columns, returning the `effectiveValue` oneof branch per cell. **Never emits `FORMATTED_VALUE`.** |
| `gdrive_odoo_sync/services/xlsx_reader.py` | B | `XlsxReader.read(bytes) -> [{'index','title','rows','used_range'}]` via `openpyxl.load_workbook(read_only=True, data_only=True)`. Flags `XLSX_NO_CACHED_VALUES` when `data_only` yields `None` for formula cells. Right-pads rows. Rejects legacy `.xls`. Presents the same row/tab shape as `sheets_reader` so lane D has one code path. |

---

## Lane C — canonicalization + hashing library (stdlib only)

Implements `docs/CANONICALIZATION.md` exactly. No Odoo imports. No third-party imports. Every function is pure and deterministic.

| Path | Lane | Responsibility |
|---|---|---|
| `gdrive_odoo_sync/lib/spec_version.py` | C | `CANON_VERSION` (a frozen string, bumped on **any** behavioural change in this lane) and `compute_spec_version(contract_dict) -> str` = hex `H(b"gos1/spec\x00" + jcs(contract) + CANON_VERSION)`. Every cached hash is keyed by this. |
| `gdrive_odoo_sync/lib/tokens.py` | C | The tagged-token vocabulary: prefixes `z:`, `s:`, `n:`, `b:`, `d:`, `t:`, `r:`, `k:`, `e:`; the `e:` error code enum (`NOT_A_NUMBER`, `NOT_FINITE`, `BAD_DATE`, `BAD_BOOL`, `CELL_ERROR`, `IDENTIFIER_NUMERIC`, `UNRESOLVED_SELECTION`, `ORPHAN_REFERENCE`, `NONEXISTENT_LOCAL_TIME`); the domain-separation prefixes; helpers `is_error(tok)`, `tag_of(tok)`. |
| `gdrive_odoo_sync/lib/contract.py` | C | The plain-dict column-contract schema lane E serializes into and lane C consumes. `ColumnContract` dataclass with every option from SPEC §3.9, `validate_contract()`, and `contract_from_mapping_dict()`. Also `slugify(header_canon) -> str` producing `^[a-z_][a-z0-9_]*$` with deterministic `_2`/`_3` deduplication. |
| `gdrive_odoo_sync/lib/text_canon.py` | C | `TEXT_CANON(v, opts)` — the exact 10-step ordered algorithm (format-char strip → whitespace unification → **NFC**, never NFKC → trim → optional collapse → optional `casefold()` → empty→NULL). Plus `fold_punct(s)` used only for the cosmetic hash. |
| `gdrive_odoo_sync/lib/number_canon.py` | C | `NUM_CANON(v, col)` — `Decimal(repr(float))` never `Decimal(float)`; declared (never guessed) separators; accounting negatives; `quantize(..., ROUND_HALF_UP)`; `-0` collapse; fixed-point `format(q,'f')`, never scientific. Also `raw_decimal(v)` for the independent column-total control and `near_boundary(d, step)` for the rounding-boundary warning. |
| `gdrive_odoo_sync/lib/datetime_canon.py` | C | `serial_to_naive(serial)` (epoch 1899-12-30, `round` not truncate, 86400 rollover), `DATE_CANON`, `DATETIME_CANON` (localize with explicit `fold=0`, raise `NONEXISTENT_LOCAL_TIME` on the spring-forward gap, convert to UTC, `%Y-%m-%dT%H:%M:%SZ`). Strict `strptime` against the declared format list only — **no fuzzy parsing**. Dates never see a timezone. |
| `gdrive_odoo_sync/lib/bool_canon.py` | C | `BOOL_CANON(v, col)` — real booleans pass through; strings go through `TEXT_CANON` with `case=fold` then membership against `truthy`/`falsy`; NULL → `empty_means`; anything else → `e:BAD_BOOL`. **Never defaults unknown tokens to false.** |
| `gdrive_odoo_sync/lib/canon.py` | C | `CANON(raw, col, side)` — the dispatcher over `ctype`, plus the Odoo-side extractors (`False`-as-empty on Char/Text, boolean branching on field type, many2one → `r:<key>` never `display_name`, selection → `k:<technical_key>`, m2m → sorted id list). Emits tagged tokens exclusively. |
| `gdrive_odoo_sync/lib/jcs.py` | C | Restricted RFC 8785 JSON Canonicalization: keys sorted, `separators=(',',':')`, `ensure_ascii=False`, minimal JSON string escaping, UTF-8 output. Enforces that every key matches `^[A-Za-z_][A-Za-z0-9_]*$` (making byte order and UTF-16 code-unit order identical) and raises otherwise. No floats permitted in the payload. |
| `gdrive_odoo_sync/lib/hashing.py` | C | `H(b)` = `sha256`, `H128(b)` = first 16 bytes. `h_row(canon_map, spec_version)`, `h_row_folded(...)`, `h_extra(...)`, `h_header(labels)`, `identity_key_bytes(parts)` (length-prefixed, injection-proof), `bucket_of(key_bytes)` = `int.from_bytes(H(b"gos1/bkt\x00"+key)[:2],'big') % 256`. **SHA-256 only — never MD5/SHA-1/CRC32/xxhash**, because a collision here is a false VERIFIED. |
| `gdrive_odoo_sync/lib/merkle.py` | C | `h_bucket(entries)` and `h_dataset(bucket_hashes, spec_version, tab_uid, total_rows)` per CANONICALIZATION §8. Sorts by canonical key **bytes**, never by locale collation. `diff_buckets(a, b) -> [int]`. Order-insensitive by construction: sorting the sheet changes nothing. |
| `gdrive_odoo_sync/lib/ulid.py` | C | `new_ulid()` — 26-char Crockford base32, 48-bit ms timestamp + 80 bits `secrets.token_bytes`, lexicographically sortable, no dashes. `is_ulid(s)`. Generated at **plan** time so retries reuse the same id. |

---

## Lane D — core Odoo models

| Path | Lane | Responsibility |
|---|---|---|
| `gdrive_odoo_sync/models/gdrive_connection.py` | D | `gdrive.connection` (SPEC §3.1). Credential resolution, `sa_client_email`/`sa_client_id` computes, `action_test_connection()` launching the wizard, `action_run_discovery()`, `action_request_full_resync()`. Cron entry points `_cron_discover()` and `_cron_full_resync()`, each with the `pg_try_advisory_xact_lock` guard, the wall-clock budget, batch commits, `_trigger()` continuation, and a no-raise contract. |
| `gdrive_odoo_sync/models/gdrive_scope_rule.py` | D | `gdrive.scope.rule` (§3.2). `matches(node_meta) -> bool` and the default-allow / include-flips-to-deny / exclude-always-wins evaluation, plus subtree pruning. |
| `gdrive_odoo_sync/models/gdrive_change_cursor.py` | D | `gdrive.change.cursor` (§3.3). Bootstrap, replay, and the invariant that data is committed **before** the token is persisted. Marks itself `invalid` on `GDriveTokenInvalid` and forces a full enumeration. |
| `gdrive_odoo_sync/models/gdrive_node.py` | D | `gdrive.node` (§3.4). Upsert from Drive metadata, parent/orphan resolution, `path`/`depth` materialization, shortcut resolution, `_cron_ingest()`, attachment mirroring (`raw` not `datas`, **never** `res_field`, always `.sudo()`), md5-based download skip, version retention pruning, `state='gone'` handling that never unlinks anything. |
| `gdrive_odoo_sync/models/gdrive_dataset.py` | D | `gdrive.dataset` (§3.5). Tab discovery from `list_tabs`/`XlsxReader`, gid-keyed identity (negative surrogates for xlsx), `_cron_stage()`, the header gate (missing mapped column ⇒ hard stop, zero rows staged), the `EMPTY_TAB` guard, `last_read_complete` bookkeeping, and the L0/L0b fingerprint fields. |
| `gdrive_odoo_sync/models/gdrive_dataset_column.py` | D | `gdrive.dataset.column` (§3.6). Header upsert matched on `header_canon`, slug generation and dedup, `sample_values`, `observed_kind` (advisory only — never used to canonicalize), `is_mapped` compute. |
| `gdrive_odoo_sync/models/gdrive_staged_row.py` | D | `gdrive.staged.row` (§3.7). Row staging, `payload`/`canon` Json construction (always reassigning whole dicts — never in-place mutation), denormalized `sync_id`/`natural_key`/`h_row`/`bucket`/`state` columns, whole-row quarantine on any `e:` token or missing required value, duplicate-identity group quarantine, `missing_since` bookkeeping gated on `last_read_complete`. |
| `gdrive_odoo_sync/models/gdrive_sync_run.py` | D | `gdrive.sync.run` (§3.15). Sequence-named run header, stage orchestration, counters, quota accounting, the run-level `complete_read` flag, gzipped JSONL log attachment, `_cron_gc()`. |
| `gdrive_odoo_sync/models/gdrive_sync_run_line.py` | D | `gdrive.sync.run.line` (§3.15). Structured per-entity log lines with stable machine `code`s. Routes every message through `services.errors.redact`. |
| `gdrive_odoo_sync/models/res_config_settings.py` | D | `res.config.settings` extension. Service-account JSON field with `config_parameter='gdrive_odoo_sync.sa_key_json'`, `groups="base.group_system"`, rendered `password="True"`. Handles the `set_param`-with-falsy-value-deletes semantics explicitly. Also exposes the non-secret pacing/retention defaults. |

---

## Lane E — mapping/promotion engine, verification/drift engine, heal wizard

| Path | Lane | Responsibility |
|---|---|---|
| `gdrive_odoo_sync/models/gdrive_mapping.py` | E | `gdrive.mapping` (§3.8). `enabled` ships False. `action_validate()` performing all seven assertions, including creating `x_gdrive_sync_id` / `x_gdrive_source_dataset` as manual `ir.model.fields` and the partial unique index. `spec_version` compute that clears cached dataset hashes on change. `_cron_promote()`. |
| `gdrive_odoo_sync/models/gdrive_mapping_column.py` | E | `gdrive.mapping.column` (§3.9). The per-column contract, `to_contract_dict()` feeding lane C, `value_map` validation against `fields_get`, currency resolution, and the `assert_string_value` flag for identifier columns. |
| `gdrive_odoo_sync/models/gdrive_promotion_link.py` | E | `gdrive.promotion.link` (§3.10). Ownership bookkeeping, `missing_since` / `missing_run_count` quarantine clock, `flap_counters`, `state` transitions including `unmanaged` and `non_convergent`. |
| `gdrive_odoo_sync/models/gdrive_promoter.py` | E | `gdrive.promoter` (`AbstractModel`). `read_odoo_snapshot(mapping)` via a single `search_read` (never N reads), always fetching `id` and `write_date`. `execute(plan)` — fixed sequence order, 200-row savepoint batches with individual retry on failure, minimal-field `write()` only, upsert-by-unique-index creates, soft delete only. |
| `gdrive_odoo_sync/models/gdrive_reconciler.py` | E | `gdrive.reconciler` (`AbstractModel`). The **pure** planner: `plan(sheet_snapshot, odoo_snapshot, contract, policy, now) -> plan_dict`. No ORM writes, no network, no ambient clock. Implements the identity cascade, the L1/L2/L3 drill-down, the drift taxonomy and classification, and every circuit breaker and delete guard. Dry-run and apply both call this and only this. |
| `gdrive_odoo_sync/models/gdrive_verification.py` | E | `gdrive.verification` (§3.11). Orchestrates L0/L0b/L1/L2/L3, records mode/result/hashes/counts, computes `column_totals` from **raw** values as an independent control, `_cron_verify()`, and renders the JSON + HTML report attachment. |
| `gdrive_odoo_sync/models/gdrive_drift.py` | E | `gdrive.drift` (§3.12). Drift record creation with the three disjoint categories, severity assignment, `cosmetic`/`rounding`/`substantive` classification, A1 `source_ref` and Drive deep links, and the `resolution` lifecycle. |
| `gdrive_odoo_sync/models/gdrive_plan.py` | E | `gdrive.plan` (§3.13). Fingerprint capture, breaker evaluation, `requires_approval` computation, `expiry_date`, `action_approve()`, `action_apply()` (admin-gated, refuses on stale fingerprints or expiry), and the post-apply convergence assertion with the flap detector. |
| `gdrive_odoo_sync/models/gdrive_plan_action.py` | E | `gdrive.plan.action` (§3.14). Typed serializable actions, sequence-encoded ordering, ULIDs generated at plan time, `payload`/`deltas` Json, per-action state and error capture. |
| `gdrive_odoo_sync/wizard/gdrive_connection_test_wizard.py` | E | `gdrive.connection.test.wizard`. Probes P1–P7 (SPEC §2.4) with per-probe pass/fail and actionable remediation text. Renders P4-returning-zero as a **red error**, not an empty state, because an empty corpus is the signature of a broken DWD grant. Sets `connection.state='ok'` only when P1–P4 all pass. |
| `gdrive_odoo_sync/wizard/gdrive_mapping_builder_wizard.py` | E | `gdrive.mapping.builder.wizard` + `.line`. Materializes one line per `gdrive.dataset.column` with a **suggested** `ctype` and Odoo field, defaults `assert_string_value` True for identifier-looking headers, and on Apply creates a `gdrive.mapping` in state `draft` with `enabled = False`. Suggestions only — nothing is ever promoted by this wizard. |
| `gdrive_odoo_sync/wizard/gdrive_heal_wizard.py` | E | `gdrive.heal.wizard` + `.line`. `dry_run` defaults **True**. `action_preview()` recomputes the plan through `gdrive.reconciler` and re-opens the same wizard record. `action_apply()` is admin-gated, refuses while `dry_run`, refuses on stale fingerprints, refuses on expiry, surfaces breaker warnings prominently, and executes via `gdrive.promoter`. |

---

## Lane F — views, menus, tests

All views are Odoo 18: `<list>` not `<tree>`, `view_mode` `list,form`, direct `invisible=`/`readonly=`/`required=` expressions (no `attrs=`/`states=`), `column_invisible="1"` to hide list columns, `<chatter/>` for chatter. Any field used in an expression is present in the view.

| Path | Lane | Responsibility |
|---|---|---|
| `gdrive_odoo_sync/views/gdrive_connection_views.xml` | F | List/form/search for `gdrive.connection`. Form surfaces `sa_client_email` and `sa_client_id` with the DWD setup instructions inline, the Test Connection button, Run Discovery, Request Full Resync, and a status statusbar. Credential fields are **not** here — they live in settings. |
| `gdrive_odoo_sync/views/gdrive_node_views.xml` | F | List/form/search/kanban for `gdrive.node`, plus a hierarchy-ordered list on `path`. Search filters: by `node_type`, `owner_email`, `shared_drive_id`, `state`, orphans, trashed. Form shows the attachment, the Drive deep link, and `last_error`. |
| `gdrive_odoo_sync/views/gdrive_dataset_views.xml` | F | List/form/search for `gdrive.dataset` and an embedded `gdrive.dataset.column` list. Form shows the hash/fingerprint block, `block_reason`, `last_read_complete`, and buttons for Stage Now / Verify Now / Build Mapping. |
| `gdrive_odoo_sync/views/gdrive_staged_row_views.xml` | F | List/form/search for `gdrive.staged.row`. Filters and group-bys use only the **real** columns (`state`, `bucket`, `dataset_id`, `quarantine_reason`) — never the Json fields, which are unsearchable and ungroupable. `payload`/`canon` render through a computed pretty-printed Text field. |
| `gdrive_odoo_sync/views/gdrive_mapping_views.xml` | F | List/form/search for `gdrive.mapping` with an embedded editable `gdrive.mapping.column` list. The `enabled` and `auto_heal` toggles carry explicit warning help text. Validate button, validation output, and the threshold/quarantine policy block. |
| `gdrive_odoo_sync/views/gdrive_verification_views.xml` | F | List/form/search/graph/pivot for `gdrive.verification`. Separate columns for `drift_count`, `data_quality_count`, `structural_count` so the three are never conflated. Report attachment download. |
| `gdrive_odoo_sync/views/gdrive_drift_views.xml` | F | List/form/search for `gdrive.drift`. Group-by drift type, category, severity, dataset. Shows `canon_sheet` vs `canon_odoo` side by side with `source_ref` and a Drive link. |
| `gdrive_odoo_sync/views/gdrive_plan_views.xml` | F | List/form for `gdrive.plan` with an embedded `gdrive.plan.action` list. Prominent dry-run / breaker / approval banners. Approve and Apply buttons gated with `groups="gdrive_odoo_sync.group_gdrive_admin"`. |
| `gdrive_odoo_sync/views/gdrive_sync_run_views.xml` | F | List/form/search for `gdrive.sync.run` with the embedded `gdrive.sync.run.line` list, counters, `complete_read` badge, and the log attachment. |
| `gdrive_odoo_sync/views/res_config_settings_views.xml` | F | Settings block: service-account JSON (`password="True"`, `groups="base.group_system"`), the env-var guidance note, pacing and retention defaults. |
| `gdrive_odoo_sync/wizard/gdrive_connection_test_wizard_views.xml` | F | Dialog form (`target='new'`) with the P1–P7 result panel and a `<footer>` with Run / Close. |
| `gdrive_odoo_sync/wizard/gdrive_mapping_builder_wizard_views.xml` | F | Dialog form with the editable column-suggestion list and a `<footer>` with Create Mapping / Cancel (`special="cancel"`). |
| `gdrive_odoo_sync/wizard/gdrive_heal_wizard_views.xml` | F | Dialog form implementing the setup → preview → applied state machine, with the action list, the dry-run toggle defaulted on, breaker warnings, and a `<footer>` whose Apply button is `invisible="state != 'preview' or dry_run"` and `groups`-gated. |
| `gdrive_odoo_sync/views/gdrive_menus.xml` | F | Root menu `Google Drive Sync` (`web_icon`), and children: Overview (runs), Drive Nodes, Datasets, Staged Rows, Verifications, Drift, Plans, Configuration → Connections / Mappings / Scope Rules. Loaded **last** in the manifest because menus reference actions. |
| `gdrive_odoo_sync/tests/__init__.py` | F | Imports every test module below. **Must not be imported from the module's top-level `__init__.py`** — modules absent from this file are silently never run. |
| `gdrive_odoo_sync/tests/test_lib_text_canon.py` | F | Table-driven tests for the 10-step text algorithm: BOM/ZWSP/soft-hyphen stripping, NBSP→space, NFC (and an assertion that NFKC folding does **not** occur), trim, collapse, `casefold` vs `lower`, empty→NULL. |
| `gdrive_odoo_sync/tests/test_lib_number_canon.py` | F | `Decimal(repr(x))` vs `Decimal(x)`, declared separators, accounting negatives, `ROUND_HALF_UP` vs banker's, `-0.00` collapse, fixed-point never scientific, `e:NOT_A_NUMBER`, boundary flagging. |
| `gdrive_odoo_sync/tests/test_lib_datetime_canon.py` | F | Serial→date rounding (`45000.499999999996`), 86400 rollover, dates never tz-converted, DST fold and the spring-forward gap raising `NONEXISTENT_LOCAL_TIME`, strict `strptime` and the rejection of `03/04/2026` under a single-format contract. |
| `gdrive_odoo_sync/tests/test_lib_bool_canon.py` | F | Truthy/falsy membership, `empty_means`, and the assertion that `"pending"`/`"TBD"` produce `e:BAD_BOOL` rather than false. |
| `gdrive_odoo_sync/tests/test_lib_hashing.py` | F | Tag-family collision resistance (`"1"` vs `1` vs `true`), JCS determinism and key-charset enforcement, delimiter-injection resistance of `identity_key_bytes`, and `spec_version` invalidation. |
| `gdrive_odoo_sync/tests/test_lib_merkle.py` | F | Shuffled vs sorted datasets hash identically; a single changed cell perturbs exactly one bucket; `diff_buckets` localizes correctly; row-count and column-total controls behave independently of the hash. |
| `gdrive_odoo_sync/tests/test_services_auth.py` | F | Mocked. `with_subject()` return value is captured and is a new object; env-var beats `ir.config_parameter`; `.sudo()` is used; `unauthorized_client` maps to `GDriveScopeError`; private keys are redacted from every error string. |
| `gdrive_odoo_sync/tests/test_services_drive.py` | F | Mocked transport. Asserts `supportsAllDrives`/`includeItemsFromAllDrives` on every relevant call, `nextPageToken` in every fields mask, multi-page pagination, `drives.list` pageSize coercion, `incompleteSearch` handling, shortcut resolution and dedup, native-vs-blob download branching, `exportSizeLimitExceeded` handling. |
| `gdrive_odoo_sync/tests/test_services_changes.py` | F | `newStartPageToken` taken only from the final page; token persisted only after data commit; `GDriveTokenInvalid` forces full re-enumeration; per-drive tokens never replayed without their `driveId`. |
| `gdrive_odoo_sync/tests/test_services_sheets.py` | F | Apostrophe doubling in A1 ranges; `vr.get('values', [])` on an empty tab; ragged-row right-padding; `used_range` preferred over `gridProperties`; `FORMATTED_VALUE` never requested; `effectiveValue` oneof validation raising `IDENTIFIER_NUMERIC`. |
| `gdrive_odoo_sync/tests/test_services_xlsx.py` | F | openpyxl `read_only`/`data_only` behaviour, `XLSX_NO_CACHED_VALUES` flagging, negative-surrogate gids, worksheet rename ambiguity blocking. |
| `gdrive_odoo_sync/tests/test_models_node_ingest.py` | F | Attachment mirroring writes `raw` only, never sets `res_field`, uses `.sudo()`, skips download on unchanged md5, keeps prior versions, and marks removed files `gone` without unlinking. |
| `gdrive_odoo_sync/tests/test_models_staging.py` | F | Header gate hard-stops and stages zero rows; unmapped new column is non-blocking and lands in `h_extra`; duplicate identity quarantines the whole group; `EMPTY_TAB` blocks; whole-row quarantine on any `e:` token. |
| `gdrive_odoo_sync/tests/test_reconciler_plan.py` | F | The planner is pure (no writes, no network); identity cascade including natural-key backfill and `MULTI_MATCH`; drift classification into `cosmetic`/`rounding`/`substantive`; data-quality items excluded from `drift_count`. |
| `gdrive_odoo_sync/tests/test_delete_guards.py` | F | Each of the seven §9.6 conditions independently blocks a `soft_delete`; the percentage breaker trips; hard delete is unreachable from every automated path. |
| `gdrive_odoo_sync/tests/test_apply_idempotency.py` | F | Applying the same plan twice yields identical state and no duplicates; ULIDs are stable across retries; a mutated fingerprint yields `refused_stale`; an expired plan refuses; batch failure rolls back and retries individually. |
| `gdrive_odoo_sync/tests/test_convergence.py` | F | A deliberately asymmetric normalizer is caught by the post-apply hash assertion; the flap counter raises `NON_CONVERGENT_FIELD` at N=3 and stops writing that field. |
| `gdrive_odoo_sync/tests/test_views_install.py` | F | `@tagged('post_install','-at_install')`. Loads every view in the manifest and asserts none use `<tree>`, `attrs=`, or `states=`, and that every field referenced in an `invisible`/`readonly`/`required` expression is present in the view. |

---

## Summary

| Lane | Files |
|---|---|
| A | 16 |
| B | 11 |
| C | 12 |
| D | 10 |
| E | 12 |
| F | 33 |
| **Total** | **94** |
