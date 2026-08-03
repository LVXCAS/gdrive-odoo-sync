# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. See LICENSE (LGPL-3) for full copyright and licensing details.
"""``gdrive.reconciler`` — the pure planner (SPEC §3.17, §9.3–§9.6).

WHY this model is an ``AbstractModel`` that never touches the ORM
================================================================
Dry-run and apply call **this same function**; the only difference is whether
the returned plan is executed. If they had separate code paths the preview
would be a lie, and a preview that lies is worse than no preview at all — the
operator approves what they were shown and something else happens.

Three properties make that guarantee real, and all three are structural rather
than aspirational:

* **No ORM writes.** ``plan()`` receives plain dicts and returns plain dicts.
  It never sees a recordset, so it *cannot* create, write or unlink. A planner
  that can touch the ORM will eventually touch the ORM.
* **No network.** Both snapshots are taken by the callers (lane D for the
  sheet, :meth:`~odoo.addons.gdrive_odoo_sync.models.gdrive_promoter` for
  Odoo) before planning starts.
* **No ambient clock.** ``now`` is injected. A plan computed twice from the
  same inputs is identical, which is what makes "re-plan and compare before
  applying" a meaningful staleness check.

WHY absence is never treated as emptiness
=========================================
Every delete in this file is inferred from *absence*, and absence is precisely
what every read failure looks like: an expired token, a partial ``batchGet``, a
range that stopped at row 1000, a wrong Odoo domain. So a failed or partial
read is propagated as **unknown** (``read_complete = False``) and the delete
planner refuses to run against it. There is no read bug whose signature is
"invent 4000 new rows", which is why creates get a looser bar than deletes.

WHY floats are never compared with ``==``
=========================================
Comparison happens exclusively on the **tagged canonical strings** produced by
``lib/`` (``n:1234.50``), and any residual numeric work is done in
:class:`decimal.Decimal` against the declared rounding step. Sheets returns
IEEE-754 doubles and ``12.30`` is not representable; two paths to "twelve
thirty" land on ``12.299999999999999`` and ``12.300000000000001``. A tool that
claims two cells both reading ``12.30`` differ destroys trust on day one.
"""

import logging
import math
from decimal import Decimal, InvalidOperation
from datetime import timedelta

from odoo import api, fields, models

from ..lib.hashing import fold_canon_map
from ..lib.number_canon import near_boundary, step_for_scale
from ..lib.tokens import (
    ERR_CURRENCY_MISMATCH,
    ERR_IDENTIFIER_NUMERIC,
    ERR_MULTI_MATCH,
    ERR_ORPHAN_REFERENCE,
    NULL_TOKEN,
    TAG_BOOL,
    TAG_DATE,
    TAG_DATETIME,
    TAG_ERROR,
    TAG_NULL,
    TAG_NUMBER,
    TAG_REL,
    TAG_SELECTION,
    TAG_TEXT,
    is_error,
    payload_of,
)
from ..lib.ulid import new_ulid

_logger = logging.getLogger(__name__)

#: SPEC §9.7 — execution order lives in the *plan*, not in the executor, so a
#: stored plan replays identically no matter which worker picks it up.
ACTION_SEQUENCE = {
    'writeback_sync_id': 10,
    'create': 20,
    'update': 30,
    'soft_delete': 40,
    'quarantine': 50,
}

#: One savepoint per this many actions (SPEC §9.7). Encoded at plan time so the
#: batch boundaries are visible in the preview a human approves.
BATCH_SIZE = 200

#: SPEC §9.4 — a numeric mismatch whose absolute difference is within this
#: multiple of the rounding step is ``rounding``: reported, never auto-written.
#: Writing it makes the value flap between runs without ever converging.
ROUNDING_STEP_FACTOR = Decimal('0.51')

#: Technical ownership markers written on every promoted record (SPEC §3.10).
FIELD_SYNC_ID = 'x_gdrive_sync_id'
FIELD_SOURCE_DATASET = 'x_gdrive_source_dataset'

#: ``e:`` codes that are *drift* rather than *data quality*, with the severity
#: SPEC §9.3 assigns them. Everything else in the ``e:`` family is a cell we
#: could not read, which is a data-quality item and never inflates
#: ``drift_count`` — "12 drifts" must never silently mean "12 unreadable cells".
ERROR_DRIFT_TYPES = {
    ERR_CURRENCY_MISMATCH: ('drift', 'currency_mismatch', 'critical'),
    ERR_IDENTIFIER_NUMERIC: ('data_quality', 'identifier_numeric', 'warning'),
    ERR_ORPHAN_REFERENCE: ('data_quality', 'orphan_reference', 'warning'),
    ERR_MULTI_MATCH: ('data_quality', 'multi_match', 'warning'),
}


class GdriveReconciler(models.AbstractModel):
    """The pure planner. Plain dicts in, one plain dict out."""

    _name = 'gdrive.reconciler'
    _description = 'Google Drive Sync Reconciler (pure planner)'

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #

    @api.model
    def plan(self, sheet_snapshot, odoo_snapshot, contract, policy, now):
        """Reconcile two snapshots into a change plan.

        :param dict sheet_snapshot: ``{'rows': [...], 'row_count': int,
            'read_complete': bool, 'blocking': bool, 'tab_uid': str}``. Each row
            carries ``sync_id``, ``natural_key``, ``identity_source``, ``canon``
            (``{key: tagged token}``), ``h_row``, ``a1_ref``, ``row_number`` and
            optionally ``typed`` (``{key: Odoo-native value}``).
        :param dict odoo_snapshot: ``{'rows': [...], 'count': int,
            'max_write_date': str}`` as returned by
            ``gdrive.promoter.read_odoo_snapshot()``.
        :param dict contract: ``{'spec_version', 'tab_uid', 'target_model',
            'columns': [{'key', 'ctype', 'authority', 'scale', 'rel_tol',
            'abs_tol'}]}``.
        :param dict policy: the mapping's execution policy (SPEC §3.8).
        :param str now: injected clock, ``'%Y-%m-%d %H:%M:%S'``. **Never** read
            from the environment — see the module docstring.
        :returns: a plain ``dict`` describing actions, drifts, counts and
            circuit-breaker state. Nothing in the return value is a recordset.
        """
        ctx = _PlanContext(sheet_snapshot, odoo_snapshot, contract, policy, now)

        self._detect_structural(ctx)
        self._detect_duplicate_identities(ctx)
        self._quarantine_unreadable_rows(ctx)
        self._match_identities(ctx)
        self._plan_matched_rows(ctx)
        self._plan_creates(ctx)
        self._plan_absences(ctx)
        self._apply_breakers(ctx)

        result = ctx.build()
        _logger.debug(
            "Reconciled %s: %d sheet rows vs %d Odoo rows -> %d actions "
            "(%d drift / %d data-quality / %d structural), breaker=%s",
            ctx.tab_uid, ctx.rows_sheet, ctx.rows_odoo, len(result['actions']),
            result['drift_count'], result['data_quality_count'],
            result['structural_count'], result['breaker_reason'] or 'none',
        )
        return result

    # ------------------------------------------------------------------ #
    # Stage 1 — structural findings (SPEC §9.3, category ``structural``)
    # ------------------------------------------------------------------ #

    def _detect_structural(self, ctx):
        """Raise the findings that stop a dataset outright.

        WHY ``empty_tab`` is a hard stop rather than "all rows deleted": zero
        rows where there were N is a *signal*, not an instruction. It is the
        exact shape of a renamed tab, a revoked grant and a truncated read, and
        the cost of guessing wrong is the whole dataset.
        """
        if not ctx.sheet_read_complete or not ctx.odoo_read_complete:
            ctx.add_drift(
                category='structural', drift_type='access_lost', severity='blocking',
                message=(
                    "The source read did not complete, so absence cannot be "
                    "distinguished from failure. No delete may be planned."
                ),
                source_ref=ctx.tab_uid,
            )
            ctx.trip('read_incomplete')

        if ctx.blocking_flagged:
            ctx.add_drift(
                category='structural', drift_type='header_change', severity='blocking',
                message="A blocking structural finding is open for this dataset.",
                source_ref=ctx.tab_uid,
            )
            ctx.blocked = True
            ctx.trip('header_blocked')

        if ctx.rows_sheet == 0 and ctx.rows_odoo > 0 and ctx.sheet_read_complete:
            ctx.add_drift(
                category='structural', drift_type='empty_tab', severity='blocking',
                message=(
                    "The tab returned zero rows while %d owned Odoo records exist. "
                    "This is treated as a failed read, never as a mass delete."
                    % ctx.rows_odoo
                ),
                source_ref=ctx.tab_uid,
            )
            ctx.blocked = True
            ctx.trip('empty_tab')

    # ------------------------------------------------------------------ #
    # Stage 2 — duplicate identities (SPEC §9.3, category ``data_quality``)
    # ------------------------------------------------------------------ #

    def _detect_duplicate_identities(self, ctx):
        """Quarantine any identity claimed by more than one row, on either side.

        Two sheet rows carrying the same ``_sync_id`` do not describe one
        record twice; they describe an editing accident. Writing either one is
        a coin flip, so neither is written.
        """
        self._flag_duplicates(ctx, ctx.sheet_rows, 'sync_id', side='sheet')
        self._flag_duplicates(ctx, ctx.odoo_rows, 'sync_id', side='odoo')

    def _flag_duplicates(self, ctx, rows, attr, side):
        seen = {}
        for row in rows:
            value = (row.get(attr) or '').strip()
            if not value:
                continue
            seen.setdefault(value, []).append(row)
        for value, group in seen.items():
            if len(group) < 2:
                continue
            ctx.trip('duplicate_identity')
            for row in group:
                ctx.excluded.add(id(row))
                ctx.add_drift(
                    category='data_quality', drift_type='duplicate_identity',
                    severity='critical', sync_id=value,
                    natural_key=row.get('natural_key') or '',
                    staged_row_id=row.get('staged_row_id') or False,
                    res_model=row.get('res_model') or ctx.target_model,
                    res_id=row.get('res_id') or 0,
                    source_ref=ctx.source_ref_of(row),
                    message=(
                        "Identity %r is claimed by %d rows on the %s side; none of "
                        "them is written." % (value, len(group), side)
                    ),
                )
                if side == 'sheet':
                    ctx.add_quarantine(row, 'duplicate_identity',
                                       "sync_id %r appears %d times in this tab."
                                       % (value, len(group)))

    # ------------------------------------------------------------------ #
    # Stage 3 — whole-row quarantine (SPEC §5.4)
    # ------------------------------------------------------------------ #

    def _quarantine_unreadable_rows(self, ctx):
        """One ``e:`` token anywhere in a sheet row quarantines the whole row.

        A half-written row is worse than an unwritten one: it looks like data,
        so nobody re-reads the source.
        """
        for row in ctx.sheet_rows:
            if id(row) in ctx.excluded:
                continue
            canon = row.get('canon') or {}
            bad = [(key, tok) for key, tok in sorted(canon.items()) if is_error(tok)]
            if not bad:
                continue
            ctx.excluded.add(id(row))
            key, token = bad[0]
            code = payload_of(token)
            category, drift_type, severity = ERROR_DRIFT_TYPES.get(
                code, ('data_quality', 'type_coercion', 'warning'))
            ctx.add_drift(
                category=category, drift_type=drift_type, severity=severity,
                sync_id=row.get('sync_id') or '',
                natural_key=row.get('natural_key') or '',
                field_name=key, canon_sheet=token, canon_odoo='',
                staged_row_id=row.get('staged_row_id') or False,
                source_ref=ctx.source_ref_of(row),
                message="Column %r could not be read (%s); the row is quarantined."
                        % (key, code),
            )
            ctx.add_quarantine(row, _quarantine_reason(code),
                               "Column %r produced %s." % (key, token))

    # ------------------------------------------------------------------ #
    # Stage 4 — the identity cascade (SPEC §5.5)
    # ------------------------------------------------------------------ #

    def _match_identities(self, ctx):
        """Pair sheet rows with Odoo rows by ``sync_id``, then by natural key.

        **Row position is never an identity.** A user sorting the sheet would
        otherwise turn every row into a delete plus a create, which is the
        single most destructive thing this system could do.
        """
        strategy = ctx.policy.get('identity_strategy') or 'sync_id_then_key'
        use_sync = strategy in ('sync_id', 'sync_id_then_key')
        use_key = strategy in ('natural_key', 'sync_id_then_key')

        by_sync = {}
        by_key = {}
        for row in ctx.odoo_rows:
            if id(row) in ctx.excluded:
                continue
            sync_id = (row.get('sync_id') or '').strip()
            if sync_id:
                by_sync.setdefault(sync_id, []).append(row)
            natural_key = (row.get('natural_key') or '').strip()
            if natural_key:
                by_key.setdefault(natural_key, []).append(row)

        # Pass 1: exact sync_id. Runs to completion before any natural-key work
        # so a ULID match always wins over a fuzzier key match.
        pending = []
        for row in ctx.sheet_rows:
            if id(row) in ctx.excluded:
                continue
            sync_id = (row.get('sync_id') or '').strip()
            candidates = by_sync.get(sync_id) or [] if (use_sync and sync_id) else []
            candidates = [c for c in candidates if id(c) not in ctx.consumed]
            if len(candidates) == 1:
                ctx.pair(row, candidates[0])
            else:
                pending.append(row)

        # Pass 2: the declared natural key, against whatever is left.
        for row in pending:
            natural_key = (row.get('natural_key') or '').strip()
            candidates = by_key.get(natural_key) or [] if (use_key and natural_key) else []
            candidates = [c for c in candidates if id(c) not in ctx.consumed]
            if len(candidates) == 1:
                ctx.pair(row, candidates[0])
            elif len(candidates) > 1:
                # Ambiguous: writing either record is a guess. Consume them all
                # so they are not then reported as missing from the sheet.
                for candidate in candidates:
                    ctx.consumed.add(id(candidate))
                ctx.excluded.add(id(row))
                ctx.trip('duplicate_identity')
                ctx.add_drift(
                    category='data_quality', drift_type='multi_match',
                    severity='critical', natural_key=natural_key,
                    sync_id=row.get('sync_id') or '',
                    staged_row_id=row.get('staged_row_id') or False,
                    res_model=ctx.target_model,
                    source_ref=ctx.source_ref_of(row),
                    message=(
                        "Natural key %r matches %d Odoo records; none is written "
                        "and nothing is created." % (natural_key, len(candidates))
                    ),
                )
                ctx.add_quarantine(row, 'multi_match',
                                   "Natural key %r matched %d records."
                                   % (natural_key, len(candidates)))
            else:
                ctx.unmatched_sheet.append(row)

    # ------------------------------------------------------------------ #
    # Stage 5 — field comparison for matched pairs (SPEC §9.4, L3)
    # ------------------------------------------------------------------ #

    def _plan_matched_rows(self, ctx):
        for sheet_row, odoo_row in ctx.pairs:
            deltas = []
            self._backfill_ownership(ctx, sheet_row, odoo_row, deltas)

            sheet_hash = sheet_row.get('h_row') or ''
            odoo_hash = odoo_row.get('h_row') or ''
            if sheet_hash and odoo_hash and sheet_hash == odoo_hash:
                # L3 is skipped entirely when the row digests agree. This is the
                # whole point of the layered comparison: identical rows cost
                # nothing beyond one string compare.
                self._emit_update(ctx, sheet_row, odoo_row, deltas)
                continue

            deltas.extend(self._compare_fields(ctx, sheet_row, odoo_row))
            self._emit_update(ctx, sheet_row, odoo_row, deltas)

    def _backfill_ownership(self, ctx, sheet_row, odoo_row, deltas):
        """Stamp identity and ownership onto a record matched by natural key.

        This is how the very first sync bootstraps against pre-existing Odoo
        data that carries no ids anywhere: match on the declared key once, then
        stamp the ULID so every later run matches on identity instead.
        """
        existing = (odoo_row.get('sync_id') or '').strip()
        if not existing:
            sync_id = (sheet_row.get('sync_id') or '').strip() or new_ulid()
            ctx.backfills.append({
                'res_model': odoo_row.get('res_model') or ctx.target_model,
                'res_id': odoo_row.get('res_id'),
                'sync_id': sync_id,
                'natural_key': sheet_row.get('natural_key') or '',
                'source_ref': ctx.source_ref_of(sheet_row),
            })
            deltas.append({
                'field': FIELD_SYNC_ID,
                'from': NULL_TOKEN,
                'to': TAG_TEXT + sync_id,
                'to_typed': sync_id,
            })
        if (odoo_row.get('source_dataset') or '') != ctx.tab_uid and ctx.tab_uid:
            if not (odoo_row.get('source_dataset') or ''):
                deltas.append({
                    'field': FIELD_SOURCE_DATASET,
                    'from': NULL_TOKEN,
                    'to': TAG_TEXT + ctx.tab_uid,
                    'to_typed': ctx.tab_uid,
                })

    def _compare_fields(self, ctx, sheet_row, odoo_row):
        """Compare one matched pair column by column and return writable deltas."""
        sheet_canon = sheet_row.get('canon') or {}
        odoo_canon = odoo_row.get('canon') or {}
        sheet_typed = sheet_row.get('typed') or {}
        deltas = []

        for col in ctx.columns:
            key = col['key']
            sheet_token = sheet_canon.get(key, TAG_NULL)
            odoo_token = odoo_canon.get(key, TAG_NULL)
            if sheet_token == odoo_token and not is_error(sheet_token):
                continue

            if is_error(odoo_token) or is_error(sheet_token):
                self._report_unreadable(ctx, sheet_row, odoo_row, key,
                                        sheet_token, odoo_token)
                continue

            delta_class = self._classify(sheet_token, odoo_token, col, ctx)
            ctx.add_drift(
                category='drift', drift_type='field_mismatch', severity='warning',
                delta_class=delta_class, field_name=key,
                sync_id=sheet_row.get('sync_id') or odoo_row.get('sync_id') or '',
                natural_key=sheet_row.get('natural_key') or '',
                staged_row_id=sheet_row.get('staged_row_id') or False,
                res_model=odoo_row.get('res_model') or ctx.target_model,
                res_id=odoo_row.get('res_id') or 0,
                # Verbatim, never re-rendered: debuggability depends on these
                # being exactly the strings that were hashed.
                canon_sheet=sheet_token, canon_odoo=odoo_token,
                source_ref=ctx.source_ref_of(sheet_row),
                message="Column %r differs (%s): sheet %s, Odoo %s."
                        % (key, delta_class, sheet_token, odoo_token),
            )

            if not self._is_writable(ctx, col, delta_class):
                continue

            flaps = (odoo_row.get('flap_counters') or {}).get(key, 0)
            if flaps >= int(ctx.policy.get('flap_limit') or 3):
                # Three consecutive writes of the same (sync_id, field) is the
                # signature of an asymmetric normalizer. Continuing to write is
                # the bug; stopping and reporting is the fix.
                ctx.add_drift(
                    category='drift', drift_type='non_convergent', severity='critical',
                    field_name=key,
                    sync_id=sheet_row.get('sync_id') or odoo_row.get('sync_id') or '',
                    natural_key=sheet_row.get('natural_key') or '',
                    res_model=odoo_row.get('res_model') or ctx.target_model,
                    res_id=odoo_row.get('res_id') or 0,
                    canon_sheet=sheet_token, canon_odoo=odoo_token,
                    source_ref=ctx.source_ref_of(sheet_row),
                    message=(
                        "Column %r has been written %d consecutive times without "
                        "converging; writing it is stopped. The normalization rule "
                        "is asymmetric between %s and %s."
                        % (key, flaps, sheet_token, odoo_token)
                    ),
                )
                continue

            typed, resolvable = self._typed_value(sheet_token, col, sheet_typed, key)
            if not resolvable:
                ctx.add_drift(
                    category='data_quality', drift_type='type_coercion',
                    severity='warning', field_name=key,
                    sync_id=sheet_row.get('sync_id') or '',
                    canon_sheet=sheet_token, canon_odoo=odoo_token,
                    source_ref=ctx.source_ref_of(sheet_row),
                    message=(
                        "Column %r is a relation whose Odoo-native value was not "
                        "resolved by the reader; it is reported, never guessed."
                        % key
                    ),
                )
                continue

            deltas.append({
                'field': key,
                'from': odoo_token,
                'to': sheet_token,
                'to_typed': typed,
            })
        return deltas

    def _report_unreadable(self, ctx, sheet_row, odoo_row, key, sheet_token, odoo_token):
        token = sheet_token if is_error(sheet_token) else odoo_token
        code = payload_of(token)
        category, drift_type, severity = ERROR_DRIFT_TYPES.get(
            code, ('data_quality', 'type_coercion', 'warning'))
        ctx.add_drift(
            category=category, drift_type=drift_type, severity=severity,
            field_name=key,
            sync_id=sheet_row.get('sync_id') or odoo_row.get('sync_id') or '',
            natural_key=sheet_row.get('natural_key') or '',
            res_model=odoo_row.get('res_model') or ctx.target_model,
            res_id=odoo_row.get('res_id') or 0,
            canon_sheet=sheet_token, canon_odoo=odoo_token,
            source_ref=ctx.source_ref_of(sheet_row),
            message=("Column %r could not be compared (%s); it is never written "
                     "on a value nobody could read." % (key, code)),
        )

    def _is_writable(self, ctx, col, delta_class):
        """Only ``substantive`` differences on ``sheet``-authority columns.

        ``cosmetic`` and ``rounding`` are reported and never auto-written:
        writing them makes the value flap between runs forever without ever
        converging, and the dashboard then reports "3 fixes applied" every
        single night while nothing is actually fixed.
        """
        if ctx.blocked or not ctx.policy.get('update_allowed', True):
            return False
        if (col.get('authority') or 'sheet') != 'sheet':
            return False
        return delta_class == 'substantive'

    def _classify(self, sheet_token, odoo_token, col, ctx):
        """Classify a field mismatch as cosmetic, rounding or substantive."""
        folded_sheet = fold_canon_map({'k': sheet_token}).get('k')
        folded_odoo = fold_canon_map({'k': odoo_token}).get('k')
        if folded_sheet == folded_odoo:
            return 'cosmetic'

        if sheet_token.startswith(TAG_NUMBER) and odoo_token.startswith(TAG_NUMBER):
            return self._classify_numeric(sheet_token, odoo_token, col, ctx)
        return 'substantive'

    def _classify_numeric(self, sheet_token, odoo_token, col, ctx):
        """Numeric downgrade rule (SPEC §9.4, CANONICALIZATION §5.1).

        Tolerance may only *downgrade* ``substantive`` to ``rounding``; it may
        never upgrade an inequality into equality. And the arithmetic is
        ``Decimal`` throughout — comparing the underlying floats with ``==`` is
        the bug this whole layer exists to avoid.
        """
        try:
            left = Decimal(payload_of(sheet_token))
            right = Decimal(payload_of(odoo_token))
        except (InvalidOperation, ValueError):
            _logger.warning(
                "Non-decimal payload in a numeric token (%r vs %r); treated as "
                "substantive rather than silently equal.", sheet_token, odoo_token)
            return 'substantive'

        scale = int(col.get('scale') or 0)
        step = step_for_scale(scale)
        difference = abs(left - right)
        if difference <= ROUNDING_STEP_FACTOR * step:
            if near_boundary(left, step) or near_boundary(right, step):
                _logger.warning(
                    "ROUNDING_BOUNDARY on %s in %s: %s vs %s sit within 1e-9 of a "
                    "half-step and will flip classification between runs.",
                    col.get('key'), ctx.tab_uid, sheet_token, odoo_token)
            return 'rounding'

        rel_tol = float(col.get('rel_tol') or 0.0)
        abs_tol = float(col.get('abs_tol') or 0.0)
        if (rel_tol or abs_tol) and math.isclose(
                float(left), float(right), rel_tol=rel_tol, abs_tol=abs_tol):
            return 'rounding'
        return 'substantive'

    def _emit_update(self, ctx, sheet_row, odoo_row, deltas):
        """Queue an ``update`` action, or nothing at all when nothing differs.

        A full-record ``write()`` stomps fields the sync does not manage and
        bumps ``write_date`` on everything, which poisons the L0b Odoo fast
        path and makes every later run do full work forever.
        """
        if not deltas or ctx.blocked:
            return
        ctx.add_action({
            'action_type': 'update',
            'sync_id': (sheet_row.get('sync_id') or odoo_row.get('sync_id') or ''),
            'staged_row_id': sheet_row.get('staged_row_id') or False,
            'res_model': odoo_row.get('res_model') or ctx.target_model,
            'res_id': odoo_row.get('res_id'),
            'payload': {},
            'deltas': deltas,
            'source_ref': ctx.source_ref_of(sheet_row),
        })

    # ------------------------------------------------------------------ #
    # Stage 6 — creates (SPEC §9.3 ``missing_in_odoo``)
    # ------------------------------------------------------------------ #

    def _plan_creates(self, ctx):
        for row in ctx.unmatched_sheet:
            identity_source = row.get('identity_source') or 'none'
            ctx.add_drift(
                category='drift', drift_type='missing_in_odoo', severity='warning',
                sync_id=row.get('sync_id') or '',
                natural_key=row.get('natural_key') or '',
                staged_row_id=row.get('staged_row_id') or False,
                res_model=ctx.target_model,
                source_ref=ctx.source_ref_of(row),
                message="This row exists in the sheet and has no matching Odoo record.",
            )
            if identity_source == 'none':
                # No identity means no way to recognise the record next run, so
                # a create would produce a fresh duplicate every single night.
                ctx.add_drift(
                    category='data_quality', drift_type='duplicate_identity',
                    severity='warning',
                    staged_row_id=row.get('staged_row_id') or False,
                    source_ref=ctx.source_ref_of(row),
                    message=("The row carries no usable identity, so it is "
                             "report-only: creating it would duplicate it on "
                             "every later run."),
                )
                continue
            if ctx.blocked or not ctx.policy.get('create_allowed', True):
                continue

            # Minted at *plan* time, so a retried apply reuses the same id and
            # the partial unique index collapses the retry into a no-op update.
            sync_id = (row.get('sync_id') or '').strip() or new_ulid()
            payload = self._create_payload(ctx, row, sync_id)
            ctx.add_action({
                'action_type': 'create',
                'sync_id': sync_id,
                'staged_row_id': row.get('staged_row_id') or False,
                'res_model': ctx.target_model,
                'res_id': False,
                'payload': payload,
                'deltas': [],
                'source_ref': ctx.source_ref_of(row),
            })

    def _create_payload(self, ctx, row, sync_id):
        canon = row.get('canon') or {}
        typed_map = row.get('typed') or {}
        payload = dict(ctx.policy.get('default_values') or {})
        for col in ctx.columns:
            key = col['key']
            if (col.get('authority') or 'sheet') != 'sheet':
                continue
            token = canon.get(key)
            if token is None or is_error(token):
                continue
            typed, resolvable = self._typed_value(token, col, typed_map, key)
            if not resolvable:
                continue
            payload[key] = typed
        payload[FIELD_SYNC_ID] = sync_id
        payload[FIELD_SOURCE_DATASET] = ctx.tab_uid
        return payload

    # ------------------------------------------------------------------ #
    # Stage 7 — absences and the seven delete guards (SPEC §9.6)
    # ------------------------------------------------------------------ #

    def _plan_absences(self, ctx):
        """Report every Odoo record the sheet no longer mentions, and plan a
        soft delete only when **all seven** guards in SPEC §9.6 hold.

        A wrongly created record is deleted in seconds. A wrongly deleted Odoo
        record takes its journal entries, attachments and message threads with
        it and often cannot be restored at all. Hence the asymmetry.
        """
        policy = ctx.policy
        strategy_allows = (policy.get('identity_strategy') or 'sync_id_then_key') != 'natural_key'
        policy_allows = policy.get('delete_policy') == 'soft'
        window_runs = int(policy.get('quarantine_runs') or 0)
        window_hours = int(policy.get('quarantine_hours') or 0)

        for row in ctx.odoo_rows:
            if id(row) in ctx.consumed or id(row) in ctx.excluded:
                continue

            owned = bool(ctx.tab_uid) and (row.get('source_dataset') or '') == ctx.tab_uid
            if not owned:
                # Records a human created directly in Odoo are not the sync's to
                # delete, at any threshold, under any configuration.
                ctx.add_drift(
                    category='drift', drift_type='unmanaged_record', severity='info',
                    sync_id=row.get('sync_id') or '',
                    natural_key=row.get('natural_key') or '',
                    res_model=row.get('res_model') or ctx.target_model,
                    res_id=row.get('res_id') or 0,
                    source_ref=ctx.source_ref_of(row),
                    message=("This record is inside the mapping domain but is not "
                             "owned by this dataset; it is reported, never touched."),
                )
                continue

            ctx.add_drift(
                category='drift', drift_type='missing_in_sheet', severity='warning',
                sync_id=row.get('sync_id') or '',
                natural_key=row.get('natural_key') or '',
                res_model=row.get('res_model') or ctx.target_model,
                res_id=row.get('res_id') or 0,
                source_ref=ctx.source_ref_of(row),
                message="This owned record has no matching row in the sheet.",
            )

            reason = self._delete_blocked_reason(
                ctx, row, policy_allows, strategy_allows, window_runs, window_hours)
            if reason:
                _logger.debug("Delete of %s,%s withheld: %s",
                              row.get('res_model'), row.get('res_id'), reason)
                continue

            soft_field = policy.get('soft_delete_field') or 'active'
            ctx.add_action({
                'action_type': 'soft_delete',
                'sync_id': row.get('sync_id') or '',
                'staged_row_id': False,
                'res_model': row.get('res_model') or ctx.target_model,
                'res_id': row.get('res_id'),
                # A flag flip, with the identity retained so a restore is one
                # flag flip back. Hard delete is unreachable from here.
                'payload': {soft_field: False},
                'deltas': [],
                'source_ref': ctx.source_ref_of(row),
            })

    def _delete_blocked_reason(self, ctx, row, policy_allows, strategy_allows,
                               window_runs, window_hours):
        """Return the first guard that withholds this delete, or ``''``."""
        if not policy_allows:                                   # Guard 1
            return 'delete_policy'
        if not (row.get('sync_id') or '').strip():              # Guard 2
            return 'no_sync_id'
        if not row.get('has_link'):                             # Guard 2
            return 'no_promotion_link'
        if not (ctx.sheet_read_complete and ctx.odoo_read_complete):  # Guard 3
            return 'read_incomplete'
        if ctx.blocked:                                         # Guard 4
            return 'dataset_blocked'
        if not strategy_allows:                                 # Guard 7
            return 'natural_key_identity'
        if _identity_source_of(row) != 'sync_id':               # Guard 7
            return 'row_identity_not_sync_id'

        # Guard 5 — absent for N consecutive complete runs AND for H hours.
        # ANDed, never ORed: either alone is satisfied by a single bad read.
        if int(row.get('missing_run_count') or 0) < window_runs:
            return 'quarantine_runs'
        missing_since = fields.Datetime.to_datetime(row.get('missing_since') or False)
        if not missing_since:
            return 'never_observed_missing'
        if ctx.now_dt and missing_since > ctx.now_dt - timedelta(hours=window_hours):
            return 'quarantine_hours'
        return ''

    # ------------------------------------------------------------------ #
    # Stage 8 — circuit breakers (SPEC §9.6)
    # ------------------------------------------------------------------ #

    def _apply_breakers(self, ctx):
        """Trip the breakers, then decide whether a human must approve.

        Exceeding the create threshold does not mean 4000 invoices appeared; it
        means the identity strategy broke — a renamed key column, a wrong
        domain, an empty Odoo read.
        """
        policy = ctx.policy
        creates = ctx.count('create')
        deletes = ctx.count('soft_delete')

        delete_limit = max(
            float(policy.get('delete_threshold_abs') or 0),
            float(policy.get('delete_threshold_pct') or 0.0) / 100.0 * ctx.rows_odoo,
        )
        if deletes > delete_limit:
            ctx.trip('deletes_exceed_threshold')
            _logger.warning(
                "Delete breaker tripped for %s: %d soft deletes against a limit "
                "of %.2f. Nothing executes until a human approves.",
                ctx.tab_uid, deletes, delete_limit)

        create_limit = max(
            float(policy.get('create_threshold_abs') or 0),
            float(policy.get('create_threshold_pct') or 0.0) / 100.0 * ctx.rows_sheet,
        )
        if creates > create_limit:
            ctx.trip('creates_exceed_threshold')
            _logger.warning(
                "Create breaker tripped for %s: %d creates against a limit of "
                "%.2f. This usually means the identity strategy broke.",
                ctx.tab_uid, creates, create_limit)

        ctx.requires_approval = bool(
            ctx.breaker_tripped or deletes or not policy.get('auto_heal'))

    # ------------------------------------------------------------------ #
    # Typed values
    # ------------------------------------------------------------------ #

    def _typed_value(self, token, col, typed_map, key):
        """Turn a canonical token into an Odoo-native value.

        :returns: ``(value, resolvable)``. ``resolvable`` is False for relation
            tokens the reader did not accompany with a typed value: ``r:`` may
            hold either a database id or a business key and guessing which is
            exactly the bug ``M2O_CANON`` refuses to commit. Reported, never
            invented.
        """
        if key in typed_map:
            return typed_map[key], True
        if token is None:
            return False, True
        if token.startswith(TAG_ERROR):
            return False, False
        if token == TAG_NULL or token == NULL_TOKEN:
            return False, True
        payload = payload_of(token)
        if token.startswith(TAG_TEXT) or token.startswith(TAG_SELECTION):
            return payload, True
        if token.startswith(TAG_NUMBER):
            try:
                return float(Decimal(payload)), True
            except (InvalidOperation, ValueError):
                _logger.warning("Unparseable numeric token %r for %r.", token, key)
                return False, False
        if token.startswith(TAG_BOOL):
            return payload == '1', True
        if token.startswith(TAG_DATE) or token.startswith(TAG_DATETIME):
            return payload, True
        if token.startswith(TAG_REL):
            return False, False
        _logger.warning("Unknown canonical tag in %r for column %r.", token, key)
        return False, False


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #


def _identity_source_of(row):
    """Resolve a row's effective identity source without guessing silently."""
    declared = row.get('identity_source')
    if declared:
        return declared
    if (row.get('sync_id') or '').strip():
        return 'sync_id'
    if (row.get('natural_key') or '').strip():
        return 'natural_key'
    return 'none'


def _quarantine_reason(code):
    """Map a lane-C error code onto a ``gdrive.staged.row`` quarantine reason."""
    return {
        'NOT_A_NUMBER': 'not_a_number',
        'NOT_FINITE': 'not_a_number',
        'BAD_DATE': 'bad_date',
        'BAD_BOOL': 'bad_bool',
        'CELL_ERROR': 'error_cell',
        'IDENTIFIER_NUMERIC': 'identifier_numeric',
        'UNRESOLVED_SELECTION': 'type_coercion',
        'ORPHAN_REFERENCE': 'orphan_reference',
        'NONEXISTENT_LOCAL_TIME': 'nonexistent_local_time',
        'TIME_COMPONENT_PRESENT': 'type_coercion',
        'MULTI_MATCH': 'multi_match',
        'CURRENCY_MISMATCH': 'currency_mismatch',
    }.get(code, 'type_coercion')


#: Breaker reasons in the order they are reported when several apply at once.
#: Evidence about the *read* outranks evidence about the *volume*: a truncated
#: read explains the volume, and reporting the volume first would send the
#: operator chasing the symptom.
BREAKER_PRECEDENCE = (
    'read_incomplete',
    'empty_tab',
    'header_blocked',
    'duplicate_identity',
    'deletes_exceed_threshold',
    'creates_exceed_threshold',
)


class _PlanContext:
    """Mutable scratch space for one ``plan()`` call.

    Kept off the model so two concurrent plans cannot share state through
    ``self``, and so nothing in the planner is tempted to reach for ``env``.
    """

    def __init__(self, sheet_snapshot, odoo_snapshot, contract, policy, now):
        self.sheet_rows = list(sheet_snapshot.get('rows') or [])
        self.odoo_rows = list(odoo_snapshot.get('rows') or [])
        self.rows_sheet = int(sheet_snapshot.get('row_count', len(self.sheet_rows)))
        self.rows_odoo = int(odoo_snapshot.get('count', len(self.odoo_rows)))
        self.sheet_read_complete = bool(sheet_snapshot.get('read_complete', True))
        self.odoo_read_complete = bool(odoo_snapshot.get('read_complete', True))
        self.blocking_flagged = bool(sheet_snapshot.get('blocking'))
        self.contract = contract or {}
        self.columns = list(self.contract.get('columns') or [])
        self.target_model = self.contract.get('target_model') or ''
        self.tab_uid = self.contract.get('tab_uid') or sheet_snapshot.get('tab_uid') or ''
        self.spec_version = self.contract.get('spec_version') or ''
        self.policy = policy or {}
        self.now = now
        self.now_dt = fields.Datetime.to_datetime(now) if now else False

        self.blocked = False
        self.breaker_reasons = set()
        self.requires_approval = False
        self.drifts = []
        self._actions = []
        self.pairs = []
        self.unmatched_sheet = []
        self.backfills = []
        self.consumed = set()
        self.excluded = set()
        self.fingerprints = {
            'fp_h_sheet': sheet_snapshot.get('h_dataset') or '',
            'fp_h_odoo': odoo_snapshot.get('h_dataset') or '',
            'fp_drive_version': sheet_snapshot.get('drive_version') or '',
            'fp_drive_modified': sheet_snapshot.get('drive_modified') or False,
            'fp_odoo_count': self.rows_odoo,
            'fp_odoo_max_write_date': odoo_snapshot.get('max_write_date') or False,
            'fp_spec_version': self.spec_version,
        }

    # -- accumulation ---------------------------------------------------- #

    def pair(self, sheet_row, odoo_row):
        self.consumed.add(id(odoo_row))
        self.pairs.append((sheet_row, odoo_row))

    def trip(self, reason):
        self.breaker_reasons.add(reason)

    @property
    def breaker_tripped(self):
        return bool(self.breaker_reasons)

    def add_drift(self, **vals):
        drift = {
            'category': vals.get('category', 'drift'),
            'drift_type': vals['drift_type'],
            'severity': vals.get('severity', 'warning'),
            'delta_class': vals.get('delta_class', False),
            'sync_id': vals.get('sync_id', ''),
            'natural_key': vals.get('natural_key', ''),
            'staged_row_id': vals.get('staged_row_id', False),
            'res_model': vals.get('res_model', ''),
            'res_id': vals.get('res_id', 0),
            'field_name': vals.get('field_name', ''),
            'canon_sheet': vals.get('canon_sheet', ''),
            'canon_odoo': vals.get('canon_odoo', ''),
            'source_ref': vals.get('source_ref', ''),
            'message': vals.get('message', ''),
            'resolution': 'open',
        }
        self.drifts.append(drift)
        return drift

    def add_action(self, vals):
        vals.setdefault('deltas', [])
        vals.setdefault('payload', {})
        vals['sequence'] = ACTION_SEQUENCE[vals['action_type']]
        vals.setdefault('state', 'pending')
        self._actions.append(vals)
        return vals

    def add_quarantine(self, row, reason, detail):
        self.add_action({
            'action_type': 'quarantine',
            'sync_id': row.get('sync_id') or '',
            'staged_row_id': row.get('staged_row_id') or False,
            'res_model': row.get('res_model') or self.target_model,
            'res_id': row.get('res_id') or False,
            'payload': {'quarantine_reason': reason, 'quarantine_detail': detail},
            'source_ref': self.source_ref_of(row),
        })

    def count(self, action_type):
        return sum(1 for a in self._actions if a['action_type'] == action_type)

    def source_ref_of(self, row):
        """Every finding cites something a human can click through to."""
        ref = row.get('a1_ref')
        if ref:
            return ref
        if row.get('res_id'):
            return '%s,%s' % (row.get('res_model') or self.target_model, row['res_id'])
        return self.tab_uid

    # -- result ---------------------------------------------------------- #

    def build(self):
        """Freeze the accumulated state into the plain-dict plan."""
        # Sorted by execution sequence; ``sorted`` is stable, so rows keep the
        # order they were discovered in and two identical inputs produce two
        # byte-identical plans.
        actions = sorted(self._actions, key=lambda a: a['sequence'])
        for index, action in enumerate(actions):
            action['batch_index'] = index // BATCH_SIZE

        reason = next((r for r in BREAKER_PRECEDENCE if r in self.breaker_reasons), '')
        by_category = {'drift': 0, 'data_quality': 0, 'structural': 0}
        for drift in self.drifts:
            by_category[drift['category']] = by_category.get(drift['category'], 0) + 1

        result = {
            'actions': actions,
            'drifts': self.drifts,
            'backfills': self.backfills,
            'drift_count': by_category['drift'],
            'data_quality_count': by_category['data_quality'],
            'structural_count': by_category['structural'],
            'create_count': self.count('create'),
            'update_count': self.count('update'),
            'soft_delete_count': self.count('soft_delete'),
            'quarantine_count': self.count('quarantine'),
            'rows_sheet': self.rows_sheet,
            'rows_odoo': self.rows_odoo,
            'read_complete': self.sheet_read_complete and self.odoo_read_complete,
            'blocked': self.blocked,
            'breaker_tripped': self.breaker_tripped,
            'breaker_reason': reason,
            'requires_approval': self.requires_approval,
            'result': 'blocked' if self.blocked else (
                'drift' if by_category['drift'] else 'verified'),
        }
        result.update(self.fingerprints)
        return result
