# -*- coding: utf-8 -*-
"""The DriftWatch comparison engine.

WHY THIS MODULE EXISTS
=======================
Everything upstream of this file -- the Drive crawl, the Sheets stager, the
canonicalization library -- exists to produce two things this module compares:
a staged snapshot of a spreadsheet tab (``store.rows(dataset_id)``) and, when a
mapping says so, a live read of an Odoo model (``odoo.snapshot(...)``). This
file is where those two are actually held up against each other. Everything it
finds is written to ``store.record_drift(...)`` as a **finding**, never as an
instruction: this module has no write access to Odoo, does not know how to
create, update or delete anything there, and never will -- that is a different
layer's job, gated by a much higher evidence bar (SPEC 9.6).

WHY A ``mapping`` IS A PLAIN DICT, NOT A ``ColumnContract`` LIST
=================================================================
The real per-column contract (``lib/contract.py``) is rich: declared ctype,
decimal separators, currency scale, date formats, timezones, selection value
maps. A ``Verifier`` caller very often does not have all of that on hand --
it may just want to point a dataset at ``res.partner`` and a handful of field
names. So ``mapping`` here is deliberately the minimal shape:
``{'model', 'domain', 'key_column', 'key_field', 'columns': {sheet_col: odoo_field}}``.

That leaves a real gap: to canonicalize the *Odoo* side of a mapped column we
would normally need that column's declared ctype, and we do not have it. The
gap is closed by **inferring the type family from the tag of the already
canonical sheet-side token** (``store.rows()`` gives us ``canon_json``, which
was produced by the real per-dataset contract at staging time) and building a
throwaway :class:`~driftwatch.lib.contract.ColumnContract` of the matching
ctype with generic defaults (see ``_shadow_contract``). Both sides then go
through the *same* ``CANON`` dispatcher before anything is compared with
``tokens.equal()``. This is deliberately conservative: a numeric column whose
real contract used a three-decimal scale (a currency the shadow contract does
not know about) can produce a spurious ``field_mismatch`` here, but it can
never produce a false ``verified`` -- and per CANONICALIZATION's own governing
principle, a reported false positive is a vastly cheaper mistake than a
silent false pass. Where the sheet token's own precision is recoverable from
its payload (the number of digits after the decimal point) it is used
directly, which removes most of that gap for the common case.

There is exactly one thing this module is NOT willing to guess: **the identity
key**. A many2one column compared "by business key" needs the comodel's key
field resolved by the caller, and the raw ``[id, display_name]`` pair Odoo
hands back cannot be turned into that key here without another round trip
this module does not make. Relational columns are therefore always compared
by id (``m2o_compare_by='id'``) in the shadow contract; a mapping whose real
contract compares by key will see relation columns reported as
``field_mismatch`` even when they agree logically. That is a known, accepted
limitation of the simplified mapping shape, not an oversight -- see
``_shadow_contract``.

SAFETY PROPERTIES (do not weaken these without re-reading SPEC 9.6)
=====================================================================
* An incomplete read -- on either side -- is never compared. ``read_incomplete``
  is raised and the whole dataset comparison stops, because a truncated read
  compared against a full one turns every unread row into a fabricated
  deletion.
* Zero staged rows is never evidence of deletion. ``empty_tab`` is raised and
  ``missing_in_sheet`` is refused for every Odoo row the mapping would
  otherwise have flagged.
* A key that resolves to more than one Odoo record is ``multi_match``, not a
  coin flip -- the record is excluded from every other comparison for that
  identity rather than arbitrarily paired with one candidate.
* Every bucketing in this file is by identity **value**, never by row
  position, so a user re-sorting the sheet (or Odoo returning rows in a
  different order) cannot change a single finding.
* A per-dataset failure is caught and folded into that dataset's ``errors``
  list; it never aborts a run over every other dataset.
* No network call this module makes can mutate anything: ``odoo`` is used
  exclusively through ``snapshot`` / ``search_count`` / ``model_exists``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from .drift_rules import (
    EMPTY_TAB_ALWAYS_EMPTY,
    EMPTY_TAB_LOST_ROWS,
    coerce_rows,
    empty_tab_severity,
)
from .lib.canon import CANON
from .lib.contract import (
    CTYPE_BOOL,
    CTYPE_DATE,
    CTYPE_DATETIME,
    CTYPE_M2M,
    CTYPE_M2O,
    CTYPE_NUMBER,
    CTYPE_SELECTION,
    CTYPE_TEXT,
    ColumnContract,
)
from .lib.hashing import identity_key_bytes
from .lib.merkle import dataset_digest
from .lib.text_canon import TEXT_CANON
from .lib.tokens import (
    TAG_BOOL,
    TAG_DATE,
    TAG_DATETIME,
    TAG_NUMBER,
    TAG_REL,
    TAG_SELECTION,
    TAG_TEXT,
    equal as tokens_equal,
    is_error,
    is_null,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime dependency
    from .config import Config
    from .odoo_client import OdooClient
    from .store import Store

__all__ = ['Verifier', 'DRIFT_TYPES']

# --------------------------------------------------------------------------
# The exact, closed vocabulary this module is permitted to write to
# ``store.record_drift``. Anything not in this set is a bug in this file, not
# a new taxonomy entry invented on the fly -- the drift table is read by a
# dashboard and a human who both key off these literal strings.
# --------------------------------------------------------------------------
DRIFT_MISSING_IN_ODOO = 'missing_in_odoo'
DRIFT_MISSING_IN_SHEET = 'missing_in_sheet'
DRIFT_FIELD_MISMATCH = 'field_mismatch'
DRIFT_DUPLICATE_IDENTITY = 'duplicate_identity'
DRIFT_HEADER_CHANGE = 'header_change'
DRIFT_EMPTY_TAB = 'empty_tab'
DRIFT_TYPE_COERCION = 'type_coercion'
DRIFT_MULTI_MATCH = 'multi_match'
DRIFT_UNMANAGED_RECORD = 'unmanaged_record'
DRIFT_READ_INCOMPLETE = 'read_incomplete'
DRIFT_IDENTIFIER_NUMERIC = 'identifier_numeric'
DRIFT_SCHEMA_GROWTH = 'schema_growth'

def _prev_rows(dataset) -> int:
    """Rows the last complete read produced, tolerant of a pre-migration row."""
    try:
        value = dataset['prev_row_count']
    except (KeyError, IndexError, TypeError):
        return 0
    return coerce_rows(value)


def _empty_tab_finding(dataset) -> tuple:
    """``(drift_type, severity, detail)`` for a tab that staged zero rows.

    SPEC 9.6 defines the empty-tab guard as *0 data rows where the previous
    complete run had N > 0* -- a mass-delete signal. A tab that has always
    been empty is not that, and reporting the two identically is how the
    dangerous case gets buried: a corpus of ordinary blank ``Sheet2`` and
    ``Notes`` tabs produces hundreds of errors, and the one tab that just lost
    twenty thousand rows arrives as another line in that list.

    The guard's *behaviour* does not vary -- zero rows never become deletions
    either way. Only the volume does.

    The grading itself lives in ``drift_rules`` because the stager reaches the
    same conclusion one phase earlier, and the two must not drift apart.
    """
    previous = _prev_rows(dataset)
    if empty_tab_severity(previous) == EMPTY_TAB_LOST_ROWS:
        return (DRIFT_EMPTY_TAB, EMPTY_TAB_LOST_ROWS,
                'dataset %s (%s) staged zero rows; the last complete read had '
                '%s. SPEC 9.6 treats this as a mass-delete signal, never as '
                '"all rows deleted"'
                % (dataset['id'], dataset['tab_title'], previous))
    return (DRIFT_EMPTY_TAB, EMPTY_TAB_ALWAYS_EMPTY,
            'dataset %s (%s) has zero staged rows and no previous complete '
            'read had any; nothing was lost'
            % (dataset['id'], dataset['tab_title']))


DRIFT_TYPES = frozenset({
    DRIFT_MISSING_IN_ODOO, DRIFT_MISSING_IN_SHEET, DRIFT_FIELD_MISMATCH,
    DRIFT_DUPLICATE_IDENTITY, DRIFT_HEADER_CHANGE, DRIFT_EMPTY_TAB,
    DRIFT_TYPE_COERCION, DRIFT_MULTI_MATCH, DRIFT_UNMANAGED_RECORD,
    DRIFT_READ_INCOMPLETE, DRIFT_IDENTIFIER_NUMERIC, DRIFT_SCHEMA_GROWTH,
})

#: Fixed text options for turning an identity column's raw value into a join
#: key. ``text_case='fold'`` is a deliberate asymmetry with
#: ``lib.canon._KEY_TEXT_OPTS`` (which preserves case): a wrong *merge* here
#: only produces a ``field_mismatch`` for a human to look at, while a wrong
#: *split* -- treating "Jane@x.com" and "jane@x.com" as different identities
#: -- fabricates a phantom create/delete pair. Between those two failure
#: modes, folding case is the cheaper mistake.
_JOIN_KEY_OPTS = {
    'text_trim': True,
    'text_collapse_ws': True,
    'text_case': 'fold',
    'empty_is_null': True,
}

#: Map from a canonical token's tag to the ctype of the throwaway
#: ``ColumnContract`` used to canonicalize the corresponding raw Odoo value.
#: ``TAG_REL`` is resolved separately in ``_shadow_contract`` because the same
#: tag covers both many2one and many2many payloads.
_TAG_TO_CTYPE = {
    TAG_TEXT: CTYPE_TEXT,
    TAG_NUMBER: CTYPE_NUMBER,
    TAG_BOOL: CTYPE_BOOL,
    TAG_DATE: CTYPE_DATE,
    TAG_DATETIME: CTYPE_DATETIME,
    TAG_SELECTION: CTYPE_SELECTION,
}


def _now() -> str:
    """UTC timestamp in the same second-precision, ``Z``-suffixed shape the
    canonicalization library itself emits for instants (see
    ``datetime_canon.DATETIME_CANON``), so a drift's ``created_at`` sorts and
    reads consistently next to the values it is reporting on."""
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _load_json(text: Optional[str]) -> dict:
    """Parse a JSON object column, tolerating ``None``/garbage as empty.

    A staged row's ``canon_json``/``raw_json`` should never fail to parse --
    the stager wrote it with ``json.dumps`` -- but a Verifier's job is to
    notice when the world is not as documented, not to crash because of it.
    """
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _shadow_contract(sheet_token: str) -> ColumnContract:
    """Infer a throwaway single-purpose contract from a canonical token's tag.

    See the module docstring for why this exists at all. The number branch
    reads the scale directly off the sheet token's own payload (the count of
    digits after its decimal point) rather than defaulting to 2, which is
    what lets a 3-decimal-precision column compare correctly without any
    declared scale reaching this module.
    """
    tag = sheet_token[:2]
    payload = sheet_token[2:]

    if tag == TAG_NUMBER:
        scale = len(payload.split('.', 1)[1]) if '.' in payload else 0
        return ColumnContract(key='_shadow', ctype=CTYPE_NUMBER,
                              scale=scale, scale_mode='fixed')
    if tag == TAG_REL:
        if payload.startswith('['):
            return ColumnContract(key='_shadow', ctype=CTYPE_M2M)
        return ColumnContract(key='_shadow', ctype=CTYPE_M2O, m2o_compare_by='id')
    ctype = _TAG_TO_CTYPE.get(tag, CTYPE_TEXT)
    return ColumnContract(key='_shadow', ctype=ctype)


def _join_key(raw_value: Any) -> Optional[str]:
    """Canonicalize a raw identity value into a bucketing key, or ``None``
    when it is empty and therefore cannot identify anything."""
    token = TEXT_CANON(raw_value, _JOIN_KEY_OPTS)
    if is_null(token):
        return None
    return token[len(TAG_TEXT):]


class Verifier:
    """Compares a staged Drive dataset against Odoo and records drift.

    Read-only on both sides: it calls ``store.rows``/``store.datasets`` and,
    when a mapping is supplied, ``odoo.snapshot``/``odoo.model_exists``. It
    writes exactly one thing anywhere -- ``store.record_drift`` rows through
    the current run -- and never touches Odoo. One instance is cheap to
    construct and holds no per-verification state, so a single instance may
    be reused across an entire run.
    """

    def __init__(self, cfg: 'Config', store: 'Store',
                odoo: Optional['OdooClient'] = None) -> None:
        self.cfg = cfg
        self.store = store
        self.odoo = odoo

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def verify_dataset(self, dataset_id: int, mapping: Optional[dict] = None,
                       run_id: Optional[int] = None) -> dict:
        """Verify one dataset. Never raises.

        ``mapping is None`` means staging-only: only internal structural
        checks run (duplicate identities, an empty tab, hash stability
        against the stored digest) and no Odoo call is ever made. Any
        exception raised while verifying is caught here and folded into the
        returned ``errors`` list, per this module's hard requirement that a
        single bad dataset must never abort a run over every other one.
        """
        result: dict = {
            'dataset_id': dataset_id,
            'mode': 'staging_only' if mapping is None else 'compared',
            'rows_sheet': 0,
            'rows_odoo': None,
            'drift_count': 0,
            'by_type': {},
            'errors': [],
            'notes': [],
        }
        try:
            self._verify_dataset(dataset_id, mapping, run_id, result)
        except Exception as exc:  # noqa: BLE001 - a per-dataset failure must not propagate
            result['errors'].append('%s: %s' % (type(exc).__name__, exc))
        return result

    def verify_all(self, run_id: Optional[int] = None,
                   mapping_by_dataset: Optional[dict] = None) -> dict:
        """Verify every dataset in the store.

        ``mapping_by_dataset`` is ``{dataset_id: mapping}``; a dataset absent
        from it is verified staging-only. When ``run_id`` is not supplied
        this method opens and closes its own ``run`` row so a caller that
        just wants "verify everything and tell me what you found" does not
        have to know about the run lifecycle at all; a caller orchestrating
        several verifiers under one run passes its own ``run_id`` and owns
        finishing it.
        """
        mapping_by_dataset = mapping_by_dataset or {}
        stats: dict = {
            'datasets': 0,
            'compared': 0,
            'staging_only': 0,
            'drift_count': 0,
            'by_type': {},
            'errors': [],
        }
        owns_run = run_id is None
        if owns_run:
            run_id = self.store.start_run('verify', _now())
        status = 'ok'
        try:
            for dataset in self.store.datasets():
                stats['datasets'] += 1
                mapping = mapping_by_dataset.get(dataset['id'])
                per = self.verify_dataset(dataset['id'], mapping=mapping, run_id=run_id)
                if per['mode'] == 'compared':
                    stats['compared'] += 1
                else:
                    stats['staging_only'] += 1
                stats['drift_count'] += per['drift_count']
                for drift_type, count in per['by_type'].items():
                    stats['by_type'][drift_type] = stats['by_type'].get(drift_type, 0) + count
                for message in per['errors']:
                    stats['errors'].append('dataset %s: %s' % (dataset['id'], message))
        except Exception as exc:  # noqa: BLE001 - verify_all must not raise either
            status = 'error'
            stats['errors'].append('%s: %s' % (type(exc).__name__, exc))
        finally:
            if owns_run:
                self.store.finish_run(run_id, _now(), status, stats=stats)
        return stats

    # ------------------------------------------------------------------ #
    # dataset lookup
    # ------------------------------------------------------------------ #
    def _get_dataset(self, dataset_id: int):
        """Find one dataset row. ``store`` exposes no ``get_dataset(id)`` --
        its contract is fixed -- so this scans ``datasets()``, which is a
        small table (tabs, not rows) on any deployment this system targets.
        """
        for row in self.store.datasets():
            if row['id'] == dataset_id:
                return row
        return None

    # ------------------------------------------------------------------ #
    # per-dataset dispatch
    # ------------------------------------------------------------------ #
    def _verify_dataset(self, dataset_id: int, mapping: Optional[dict],
                        run_id: Optional[int], result: dict) -> None:
        now = _now()
        dataset = self._get_dataset(dataset_id)
        if dataset is None:
            raise ValueError('no such dataset: %r' % (dataset_id,))

        rows = self.store.rows(dataset_id)
        result['rows_sheet'] = len(rows)

        def record(drift_type: str, severity: str, **kw: Any) -> None:
            assert drift_type in DRIFT_TYPES, 'unknown drift type %r' % (drift_type,)
            self.store.record_drift(run_id, dataset_id, drift_type, now,
                                    severity=severity, **kw)
            result['drift_count'] += 1
            result['by_type'][drift_type] = result['by_type'].get(drift_type, 0) + 1

        # ---- guard: never compare a truncated sheet-side read -------------
        # A partial read and a mass deletion produce an identical row count;
        # `read_complete` is the only thing that tells them apart. Comparing
        # anyway would report every unread row as `missing_in_odoo` or every
        # Odoo row as `missing_in_sheet`, depending on which side was cut.
        if not dataset['read_complete']:
            record(DRIFT_READ_INCOMPLETE, 'error',
                  detail='dataset %s (%s) has read_complete=0; refusing to compare'
                         % (dataset_id, dataset['tab_title']))
            return

        if mapping is None:
            self._verify_staging_only(dataset, rows, record, result)
            return

        if self.odoo is None:
            # A mapping was supplied but this Verifier has no Odoo client.
            # Downgrading to staging-only is safer than raising: the caller
            # gets partial results plus a loud note, not a crashed run.
            result['errors'].append(
                'mapping given for dataset %s but no OdooClient configured; '
                'ran staging-only checks instead' % (dataset_id,))
            result['mode'] = 'staging_only'
            self._verify_staging_only(dataset, rows, record, result)
            return

        self._verify_against_odoo(dataset, rows, mapping, record, result)

    # ------------------------------------------------------------------ #
    # staging-only verification
    # ------------------------------------------------------------------ #
    def _verify_staging_only(self, dataset, rows, record, result: dict) -> None:
        """Internal-only checks: no Odoo-side finding is ever emitted here."""
        if not rows:
            drift_type, severity, detail = _empty_tab_finding(dataset)
            record(drift_type, severity, detail=detail)
            return

        # ---- duplicate identities, bucketed by the stager's own natural_key,
        # never by row position -- a re-sort of the sheet must not change
        # which rows are flagged.
        groups: dict[str, list] = {}
        for row in rows:
            key = (row['natural_key'] or '').strip()
            if not key:
                continue
            groups.setdefault(key, []).append(row)
        for key, group in groups.items():
            if len(group) <= 1:
                continue
            record(DRIFT_DUPLICATE_IDENTITY, 'warning',
                  row_ref=group[0]['a1_ref'], column_key='natural_key',
                  sheet_value=key,
                  detail='%d staged rows share natural_key %r: %s'
                         % (len(group), key, _row_refs(group)))

        # ---- hash stability: recompute the bucketed Merkle digest from the
        # rows as they actually are right now, and compare it to the digest
        # the stager last recorded. This is a *storage* self-check -- it has
        # nothing to do with Odoo -- and it exists to catch a dataset whose
        # rows changed (or were partially written) after `h_dataset` was
        # computed, which would otherwise sit in the database looking
        # `verified` forever. Skipped (not reported as a false positive)
        # whenever the inputs needed to reproduce it are not all present,
        # because a inconclusive check must never masquerade as a failed one.
        stored_digest = dataset['h_dataset']
        spec_version = dataset['spec_version']
        if stored_digest and spec_version:
            entries = []
            reproducible = True
            for row in rows:
                key, h_row_hex = row['natural_key'], row['h_row']
                if not key or not h_row_hex:
                    reproducible = False
                    break
                entries.append((identity_key_bytes([key]), bytes.fromhex(h_row_hex)))
            if reproducible:
                tab_uid = '%s/%s' % (dataset['file_id'], dataset['tab_id'])
                digest, _buckets = dataset_digest(entries, spec_version, tab_uid)
                if digest != stored_digest:
                    result['notes'].append(
                        'recomputed dataset digest %s does not match the stored '
                        'h_dataset %s for dataset %s. This assumes identity is a '
                        'single natural_key column (identity_key_bytes([natural_key])); '
                        'a real mismatch, a multi-column natural key, or a stale '
                        'stored digest are all indistinguishable from here.'
                        % (digest, stored_digest, dataset['id']))

    # ------------------------------------------------------------------ #
    # Odoo-mapped verification
    # ------------------------------------------------------------------ #
    def _verify_against_odoo(self, dataset, rows, mapping: dict, record, result: dict) -> None:
        model = mapping.get('model')
        key_field = mapping.get('key_field') or mapping.get('key_column')
        key_column = mapping.get('key_column') or mapping.get('key_field')
        columns: dict = dict(mapping.get('columns') or {})
        managed_field = mapping.get('managed_field')

        if not model or not key_field:
            result['errors'].append(
                'mapping for dataset %s is missing model/key_field; skipped Odoo comparison'
                % (dataset['id'],))
            return

        # ---- guard: an empty sheet side is never evidence of deletion -----
        # The refusal is unconditional. Only how loudly it is reported depends
        # on whether anything was actually lost.
        if not rows:
            drift_type, severity, detail = _empty_tab_finding(dataset)
            record(drift_type, severity,
                   detail=detail + '; refusing to treat this as evidence that '
                                   'the matching %s records were deleted' % model)
            return

        parsed_rows = [(row, _load_json(row['canon_json']), _load_json(row['raw_json']))
                       for row in rows]

        # ---- header_change: every mapped Odoo field must actually appear in
        # at least one staged row's canonical payload. If it never does, this
        # mapping and whatever produced these rows have drifted apart, and
        # every field comparison below would silently compare against
        # nothing rather than against an absent column -- SPEC 9.3 treats
        # that as a hard stop, not a per-row anomaly.
        all_canon_keys: set = set()
        for _row, canon, _raw in parsed_rows:
            all_canon_keys.update(canon.keys())
        missing_fields = sorted(set(columns.values()) - all_canon_keys)
        if missing_fields:
            record(DRIFT_HEADER_CHANGE, 'error',
                  column_key=','.join(missing_fields),
                  detail='mapped Odoo field(s) %s never appear in any staged row for '
                         'dataset %s; refusing to compare' % (missing_fields, dataset['id']))
            return

        # ---- schema_growth: sheet columns the mapping does not know about.
        # Informational and non-blocking (SPEC 9.3) -- an added notes column
        # is not a data change to any mapped field.
        raw_cols: set = set()
        for _row, _canon, raw in parsed_rows:
            raw_cols.update(raw.keys())
        extra_cols = sorted(raw_cols - set(columns.keys()) - {key_column})
        if extra_cols:
            record(DRIFT_SCHEMA_GROWTH, 'info',
                  detail='unmapped sheet column(s) present: %s' % (extra_cols,))

        # ---- bucket the sheet side by identity value, never by position ---
        sheet_groups: dict[str, list] = {}
        sheet_canon_by_key: dict[str, tuple] = {}
        for row, canon, raw in parsed_rows:
            raw_key = raw.get(key_column) if key_column else None
            if isinstance(raw_key, (int, float)) and not isinstance(raw_key, bool):
                # The identifier arrived as a number: Sheets has already
                # discarded any leading zeros / precision past 15 digits, so
                # there is nothing left to repair (CANONICALIZATION guard 2).
                # This row cannot be safely matched to anything and is
                # excluded from every comparison below.
                record(DRIFT_IDENTIFIER_NUMERIC, 'error',
                      row_ref=row['a1_ref'], column_key=key_column, sheet_value=raw_key,
                      detail='identity column %r arrived as a number (%r); the row is '
                             'excluded from comparison' % (key_column, raw_key))
                continue
            key = _join_key(raw_key)
            if key is None:
                continue
            sheet_groups.setdefault(key, []).append(row)
            sheet_canon_by_key[key] = (row, canon)

        for key, group in sheet_groups.items():
            if len(group) <= 1:
                continue
            record(DRIFT_DUPLICATE_IDENTITY, 'warning',
                  row_ref=group[0]['a1_ref'], column_key=key_column, sheet_value=key,
                  detail='%d sheet rows share identity %r: %s'
                         % (len(group), key, _row_refs(group)))
            sheet_canon_by_key.pop(key, None)  # quarantine the whole group

        # ---- exactly one Odoo call -----------------------------------------
        fields = sorted({key_field, *columns.values()})
        try:
            if hasattr(self.odoo, 'model_exists') and not self.odoo.model_exists(model):
                result['errors'].append(
                    'model %r does not exist on this Odoo database; skipped '
                    'dataset %s' % (model, dataset['id']))
                return
            snapshot = self.odoo.snapshot(model=model, domain=mapping.get('domain'),
                                          fields=fields, limit=0)
        except Exception as exc:  # noqa: BLE001 - an Odoo failure is this dataset's problem only
            result['errors'].append('Odoo snapshot of %s failed: %s: %s'
                                    % (model, type(exc).__name__, exc))
            return

        result['rows_odoo'] = snapshot.get('count')

        # ---- guard: never compare a truncated Odoo-side read ---------------
        if not snapshot.get('read_complete', True):
            record(DRIFT_READ_INCOMPLETE, 'error',
                  detail='Odoo snapshot of %s truncated (%d of %d rows); refusing to compare'
                         % (model, len(snapshot.get('rows') or []), snapshot.get('count') or 0))
            return

        odoo_groups: dict[str, list] = {}
        for orow in snapshot.get('rows') or []:
            key = _join_key(orow.get(key_field))
            if key is None:
                continue
            odoo_groups.setdefault(key, []).append(orow)

        multi_match_keys = {key for key, group in odoo_groups.items() if len(group) > 1}
        for key in sorted(multi_match_keys):
            ids = [orow.get('id') for orow in odoo_groups[key]]
            record(DRIFT_MULTI_MATCH, 'warning', column_key=key_field, sheet_value=key,
                  odoo_value=ids,
                  detail='%d %s records share identity %r; none picked, none compared'
                         % (len(ids), model, key))

        odoo_single = {key: group[0] for key, group in odoo_groups.items() if len(group) == 1}

        sheet_keys = set(sheet_canon_by_key)
        odoo_keys_all = set(odoo_groups)          # includes multi-match keys
        odoo_single_keys = set(odoo_single)

        missing_in_odoo_keys = sheet_keys - odoo_keys_all
        missing_in_sheet_keys = odoo_single_keys - sheet_keys
        matched_keys = sheet_keys & odoo_single_keys

        for key in sorted(missing_in_odoo_keys):
            row, _canon = sheet_canon_by_key[key]
            record(DRIFT_MISSING_IN_ODOO, 'warning', row_ref=row['a1_ref'],
                  column_key=key_field, sheet_value=key,
                  detail='sheet identity %r has no matching %s record' % (key, model))

        for key in sorted(missing_in_sheet_keys):
            orow = odoo_single[key]
            if managed_field and not orow.get(managed_field):
                # Matches the domain but was never created by this sync: not
                # this Verifier's to reconcile, ever (SPEC 9.3).
                record(DRIFT_UNMANAGED_RECORD, 'info', column_key=key_field, odoo_value=key,
                      detail='%s id=%s matches the mapping domain but was not created '
                             'by this sync' % (model, orow.get('id')))
            else:
                record(DRIFT_MISSING_IN_SHEET, 'warning', column_key=key_field, odoo_value=key,
                      detail='%s id=%s (identity %r) has no matching sheet row'
                             % (model, orow.get('id'), key))

        for key in sorted(matched_keys):
            row, canon = sheet_canon_by_key[key]
            orow = odoo_single[key]
            for odoo_field in sorted(set(columns.values())):
                if odoo_field == key_field:
                    continue  # the join key itself; comparing it is circular
                sheet_token = canon.get(odoo_field)
                if sheet_token is None or is_error(sheet_token):
                    continue  # column absent from this row, or already quarantined upstream
                outcome = self._compare_field(sheet_token, orow.get(odoo_field))
                if outcome is None:
                    continue
                kind, odoo_token = outcome
                if kind == DRIFT_TYPE_COERCION:
                    record(DRIFT_TYPE_COERCION, 'warning', row_ref=row['a1_ref'],
                          column_key=odoo_field, sheet_value=sheet_token,
                          odoo_value=orow.get(odoo_field),
                          detail='Odoo value could not be canonicalized into the same '
                                 'type family as the staged sheet value (%s)' % (odoo_token,))
                elif kind == DRIFT_FIELD_MISMATCH:
                    record(DRIFT_FIELD_MISMATCH, 'warning', row_ref=row['a1_ref'],
                          column_key=odoo_field, sheet_value=sheet_token, odoo_value=odoo_token,
                          detail='canonical values differ')

    # ------------------------------------------------------------------ #
    # field comparison
    # ------------------------------------------------------------------ #
    @staticmethod
    def _compare_field(sheet_token: str, raw_odoo: Any) -> Optional[tuple]:
        """Canonicalize ``raw_odoo`` to match ``sheet_token``'s type family and
        compare. Returns ``None`` when nothing is worth reporting, or
        ``(drift_type, odoo_token)`` otherwise.

        A shadow contract failure (a genuine ``ValueError`` from ``CANON`` --
        practically unreachable given the shadow contracts built here, since
        they are never handed a raw ``[id, display_name]`` pair, but treated
        defensively anyway) is silently skipped rather than propagated: one
        unusual cell must not abort the rest of the row.
        """
        shadow = _shadow_contract(sheet_token)
        try:
            odoo_token = CANON(raw_odoo, shadow, side='odoo')
        except ValueError:
            return None
        if is_error(odoo_token):
            return (DRIFT_TYPE_COERCION, odoo_token)
        if tokens_equal(sheet_token, odoo_token):
            return None
        return (DRIFT_FIELD_MISMATCH, odoo_token)


def _row_refs(group: list) -> str:
    """Human-readable list of a1_refs (falling back to row numbers) for a
    quarantined group, used only inside a drift's free-text ``detail``."""
    return ', '.join(row['a1_ref'] or ('row %s' % row['row_number']) for row in group)
