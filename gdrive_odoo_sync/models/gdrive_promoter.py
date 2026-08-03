# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. Licensed under LGPL-3.
"""``gdrive.promoter`` -- the executor (SPEC §3.17, §9.6-§9.7).

WHY execution is a separate ``AbstractModel`` from planning
=============================================================
``gdrive.reconciler`` is pure: plain dicts in, one plain dict out, no ORM
writes. Something still has to (a) read the Odoo side into the plain dicts
the planner consumes, and (b) turn an approved plan's actions into real
writes. Both of those are ORM work by definition, and keeping them in a
*different* class than the planner is what keeps the planner's purity a
structural guarantee rather than a promise someone can quietly break by
adding one convenient ``self.env[...]`` call.

WHY ``read_odoo_snapshot`` is one ``search_read``, never a loop
=================================================================
A per-row ``record.read()`` is N round trips for N rows; a dataset with
50,000 promoted records would make 50,000 queries just to *compare*, before
a single write happens. One ``search_read`` over the mapping's domain is the
whole cost, and it deliberately is **not** narrowed to already-owned records
(``x_gdrive_source_dataset`` matching this dataset): the planner needs to see
a pre-existing, unstamped record to back-fill its identity on the very first
sync (SPEC §5.5), and needs to see a record that belongs to someone else
entirely so it can report ``unmanaged_record`` instead of silently ignoring
it. Narrowing the read would make the ownership rule impossible to enforce.

WHY every ``create`` is a search-then-write, never a bare ``create()``
========================================================================
The partial unique index on ``x_gdrive_sync_id`` (SPEC §3.10) is what makes
a duplicated or retried create *safe* -- a second ``create()`` for the same
sync id fails at the database level rather than silently doubling the
dataset. But failing loudly is not the same as failing *usefully*: SPEC
§9.7 asks for a retried create to collapse into a no-op update. The only way
to get that behaviour is to look the record up by its sync id first, and
write to it if it is already there.

WHY every write here is minimal
=================================
A full-record ``write()`` stomps fields the sync does not manage and bumps
``write_date`` on every field regardless of whether it changed, which
poisons the L0b Odoo fast path (SPEC §9.1) and makes every later run do full
row-level work forever. ``update`` actions already carry only the differing
fields (the planner's job); this module additionally diffs before writing
the smaller, fixed payloads of a retried create, a soft delete and a
writeback, so that re-applying an already-converged plan is a true no-op at
the database level, not merely at the row-count level.

WHY savepoints, and why 200
=============================
Plan execution is at-least-once by construction -- a cron can be killed
mid-batch, a worker can be recycled, an operator can double-click. SPEC §9.7
asks for one savepoint per 200 actions, rolled back and retried row-by-row
on failure, so that one bad row (a stale id, a field that was removed from
the target model since the plan was made) cannot discard the 199 good rows
around it, and can never leave a half-applied batch on the table.
``gdrive.plan.action`` defines the same constant independently rather than
importing it from here, and this module returns the favour: see
``gdrive_reconciler.py`` for the same choice and the same reasoning.
"""

from __future__ import annotations

import ast
import logging

from odoo import api, fields, models

from ..lib import CANON, h_row, identity_key_bytes
from ..lib.contract import CTYPE_M2O
from ..lib.tokens import NULL_TOKEN

_logger = logging.getLogger(__name__)

#: Technical ownership markers written on every promoted record (SPEC §3.10).
#: Redefined here rather than imported, matching ``gdrive_reconciler.py`` and
#: ``gdrive_mapping.py``: each module that needs these two names carries its
#: own copy so that none of them depends on another lane's import order.
FIELD_SYNC_ID = 'x_gdrive_sync_id'
FIELD_SOURCE_DATASET = 'x_gdrive_source_dataset'

#: One savepoint per this many actions (SPEC §9.7). See the module docstring
#: for why this is a local constant rather than an import.
ACTION_BATCH_SIZE = 200


class _OdooSnapshotRows(list):
    """The value ``read_odoo_snapshot()`` returns -- a list that also answers
    like the wrapper dict ``gdrive.reconciler.plan()`` expects.

    WHY one object has to play both parts
    --------------------------------------
    Two callers already exist and neither is this file's to change:

    * ``gdrive.verification._odoo_snapshot()`` iterates
      ``read_odoo_snapshot(mapping)`` directly as a sequence of row dicts --
      matching SPEC §3.17's own description, "the Odoo-side snapshot reader
      ... -> rows".
    * ``gdrive.mapping._promote_once()`` hands that same return value
      straight to ``gdrive.reconciler.plan()`` as its ``odoo_snapshot``
      argument, which reads it as ``{'rows': [...], 'count': int,
      'read_complete': bool, 'max_write_date': ...}``.

    A plain list satisfies the first caller and breaks the second; a plain
    dict satisfies the second and breaks the first. This type is a genuine
    ``list`` (iteration, ``len()``, indexing, ``or []`` all behave exactly
    as they would for the rows themselves) that additionally implements
    ``.get(key, default)`` for the four keys the planner reads, so both call
    sites get back exactly the shape they already assume.
    """

    def __init__(self, rows, *, count=None, max_write_date=False, read_complete=True):
        super().__init__(rows)
        self._count = len(self) if count is None else count
        self._max_write_date = max_write_date
        self._read_complete = read_complete

    def get(self, key, default=None):
        if key == 'rows':
            return list(self)
        if key == 'count':
            return self._count
        if key == 'max_write_date':
            return self._max_write_date
        if key == 'read_complete':
            return self._read_complete
        return default


class GdrivePromoter(models.AbstractModel):
    """The executor: reads the Odoo side, and applies an approved plan."""

    _name = 'gdrive.promoter'
    _description = 'Google Drive Sync Promoter (executor)'

    # ------------------------------------------------------------------ #
    # Odoo-side snapshot reader
    # ------------------------------------------------------------------ #

    @api.model
    def read_odoo_snapshot(self, mapping):
        """The Odoo-side rows ``gdrive.reconciler.plan()`` compares against.

        One ``search_read`` over ``mapping.domain`` -- see the module
        docstring for why this is never narrowed to already-owned records,
        and never a loop of per-row reads. Every row always carries
        ``res_id`` (the record id) and ``write_date``, plus a ``canon``
        token per contract column computed through the exact same lane-C
        dispatcher (``CANON``, ``side='odoo'``) the sheet side uses through
        ``side='sheet'``, so the two sides are byte-comparable.

        :returns: an :class:`_OdooSnapshotRows` -- iterate it for plain row
            dicts, or read it like the wrapper dict the planner expects.
        """
        mapping.ensure_one()
        model_name = mapping.target_model
        if not model_name or model_name not in self.env:
            _logger.warning(
                "Mapping %s targets %r, which is not installed; the Odoo side "
                "reads as empty rather than raising inside a cron.",
                mapping.display_name, model_name or '-')
            return _OdooSnapshotRows([])

        columns = mapping._contract_columns()
        natural_key_keys = mapping._natural_key_names()
        field_names = self._snapshot_field_names(columns)

        Model = self.env[model_name].sudo()
        domain = self._mapping_domain(mapping)
        records = Model.search_read(domain, field_names)
        if not records:
            return _OdooSnapshotRows([])

        m2o_keys = self._resolve_m2o_business_keys(columns, records)
        links = self._promotion_links_by_sync_id(mapping, records)
        spec_version = mapping.spec_version or ''

        rows = []
        max_write_date = False
        for record in records:
            canon = {}
            raw = {}
            for col in columns:
                if not col.odoo_field:
                    continue
                value = record.get(col.odoo_field)
                raw[col.odoo_field] = value
                if (col.ctype == CTYPE_M2O and col.m2o_compare_by == 'key'
                        and isinstance(value, (list, tuple))):
                    # A raw [id, display_name] pair is refused by CANON for a
                    # key-compared many2one (display_name is rendered and
                    # translated); resolve it to the business key here.
                    value = m2o_keys.get((col.comodel, value[0] if value else None), False)
                canon[col.key] = CANON(value, col, side='odoo')

            sync_id = (record.get(FIELD_SYNC_ID) or '').strip()
            natural_key = (
                identity_key_bytes(
                    [canon.get(key, NULL_TOKEN) for key in natural_key_keys]).hex()
                if natural_key_keys else ''
            )
            identity_source = 'sync_id' if sync_id else ('natural_key' if natural_key else 'none')
            link = links.get(sync_id) if sync_id else None

            write_date = record.get('write_date')
            if write_date and (max_write_date is False or write_date > max_write_date):
                max_write_date = write_date

            rows.append({
                'res_id': record['id'],
                'res_model': model_name,
                'sync_id': sync_id,
                'natural_key': natural_key,
                'identity_source': identity_source,
                'canon': canon,
                'raw': raw,
                'h_row': h_row(canon, spec_version).hex(),
                'source_dataset': record.get(FIELD_SOURCE_DATASET) or '',
                'write_date': write_date,
                'has_link': bool(link),
                'missing_since': (link or {}).get('missing_since') or False,
                'missing_run_count': (link or {}).get('missing_run_count') or 0,
                'flap_counters': dict((link or {}).get('flap_counters') or {}),
            })

        return _OdooSnapshotRows(
            rows, count=len(rows),
            max_write_date=fields.Datetime.to_string(max_write_date) if max_write_date else False,
        )

    @api.model
    def _snapshot_field_names(self, columns):
        """Every field one ``search_read`` needs: always ``id``/``write_date``,
        plus every contract column's ``odoo_field`` and the two ownership
        markers (SPEC §3.10)."""
        names = {'id', 'write_date', FIELD_SYNC_ID, FIELD_SOURCE_DATASET}
        for col in columns:
            if col.odoo_field:
                names.add(col.odoo_field)
        return sorted(names)

    @api.model
    def _mapping_domain(self, mapping):
        """The mapping's declared scope, parsed safely.

        Deliberately just ``mapping.domain`` -- see the module docstring for
        why this is never narrowed to owned records: the bootstrap match and
        the ``unmanaged_record`` finding both depend on seeing records this
        dataset does not (yet) own.
        """
        if not mapping.domain:
            return []
        try:
            parsed = ast.literal_eval(mapping.domain)
        except Exception:
            _logger.exception(
                "Mapping %s has an unparseable domain %r; reading the "
                "unfiltered target model instead of raising inside a cron.",
                mapping.display_name, mapping.domain)
            return []
        if not isinstance(parsed, (list, tuple)):
            _logger.warning("Mapping %s domain %r is not a list; ignoring it.",
                            mapping.display_name, mapping.domain)
            return []
        return list(parsed)

    @api.model
    def _resolve_m2o_business_keys(self, columns, records):
        """``{(comodel, id): business_key}`` for every ``many2one`` compared
        by key, resolved once per comodel across every row -- never once per
        row, and never per column instance of the same comodel.
        """
        cache = {}
        ids_by_comodel = {}
        for col in columns:
            if col.ctype != CTYPE_M2O or col.m2o_compare_by != 'key' or not col.comodel:
                continue
            if col.comodel not in self.env:
                continue
            match_field = col.m2o_match_field or 'name'
            ids = ids_by_comodel.setdefault((col.comodel, match_field), set())
            for record in records:
                value = record.get(col.odoo_field)
                if isinstance(value, (list, tuple)) and value:
                    ids.add(value[0])

        for (comodel, match_field), ids in ids_by_comodel.items():
            if not ids:
                continue
            try:
                resolved = self.env[comodel].sudo().browse(list(ids)).read([match_field])
            except Exception:
                _logger.exception(
                    "Could not resolve %s.%s for %d id(s); those cells compare as "
                    "null rather than a guessed business key.",
                    comodel, match_field, len(ids))
                continue
            for row in resolved:
                cache[(comodel, row['id'])] = row.get(match_field)
        return cache

    @api.model
    def _promotion_links_by_sync_id(self, mapping, records):
        """``{sync_id: link_row}`` -- the missing/flap bookkeeping SPEC §9.6's
        delete guards and SPEC §9.8's flap detector both need, read in one
        bulk ``search_read`` rather than once per record.
        """
        if 'gdrive.promotion.link' not in self.env:
            return {}
        sync_ids = {r.get(FIELD_SYNC_ID) for r in records if r.get(FIELD_SYNC_ID)}
        if not sync_ids:
            return {}
        rows = self.env['gdrive.promotion.link'].sudo().search_read(
            [('mapping_id', '=', mapping.id), ('sync_id', 'in', list(sync_ids))],
            ['sync_id', 'missing_since', 'missing_run_count', 'flap_counters'],
        )
        return {row['sync_id']: row for row in rows}

    # ------------------------------------------------------------------ #
    # Execution (SPEC §9.7)
    # ------------------------------------------------------------------ #

    @api.model
    def execute(self, plan):
        """Apply every action of ``plan``, in the fixed SPEC §9.7 order.

        Actions are already ordered by ``sequence`` (10 writeback, 20
        create, 30 update, 40 soft delete, 50 quarantine) and are processed
        in savepoint batches of :data:`ACTION_BATCH_SIZE`. A batch that
        fails as a whole is rolled back and retried action by action, so one
        bad row never discards the good ones around it and never leaves a
        half-applied batch behind.

        If any action before sequence 40 ends up ``failed``, every
        ``soft_delete`` action -- wherever it falls, in this batch or a
        later one -- is skipped with reason ``'earlier_errors'`` rather than
        attempted: a failure earlier in the run is evidence the system's
        view of the world may be incomplete, and absence is exactly what an
        incomplete view looks like (SPEC §9.6).

        This method never raises: every action's own failure is caught,
        recorded on the action, and execution continues, which is what lets
        it be called safely from a cron.
        """
        plan.ensure_one()
        actions = plan.action_ids.sorted(key=lambda a: (a.sequence, a.id))

        earlier_failed = False
        for offset in range(0, len(actions), ACTION_BATCH_SIZE):
            chunk = actions[offset:offset + ACTION_BATCH_SIZE]

            pre_skip = chunk.filtered(
                lambda a: earlier_failed and a.action_type == 'soft_delete')
            if pre_skip:
                pre_skip._mark_skipped('earlier_errors')
            runnable = chunk - pre_skip
            if not runnable:
                continue

            if self._apply_batch(runnable):
                continue

            # The optimistic whole-batch attempt failed and was rolled back
            # in full; retry action by action so exactly one bad row is
            # isolated, re-checking the skip condition as we go, because the
            # very action that fails during this pass may itself be what
            # withholds a soft_delete later in this same batch.
            for action in runnable:
                if action.action_type == 'soft_delete' and earlier_failed:
                    action._mark_skipped('earlier_errors')
                    continue
                if not self._apply_action(action) and action.action_type != 'soft_delete':
                    earlier_failed = True

        self._finalize_apply_result(plan, actions)
        return True

    @api.model
    def _apply_batch(self, actions):
        """Try a whole batch inside one savepoint. Returns False on any failure.

        The optimistic path: on an ordinary night every action succeeds, and
        one savepoint for up to 200 actions is far cheaper than 200. On
        failure the savepoint discards everything the batch did -- including
        any ``_mark_applied`` calls already made within it -- so the actions
        are left exactly as they started, ready for the row-by-row retry.
        """
        try:
            with self.env.cr.savepoint():
                for action in actions:
                    res_id = self._execute_one(action)
                    action._mark_applied(res_id=res_id)
            return True
        except Exception:
            _logger.info(
                "Batch of %d action(s) on plan %s rolled back as a whole; "
                "retrying individually.", len(actions), actions[:1].plan_id.display_name)
            return False

    @api.model
    def _apply_action(self, action):
        """Apply exactly one action inside its own savepoint; never raises.

        Isolating every retried row in its own savepoint is what lets one
        bad row fail alone: a raised exception here rolls back only this
        row's writes, and the connection is valid again immediately
        afterwards for the next row or for ``_mark_failed`` itself.
        """
        try:
            with self.env.cr.savepoint():
                res_id = self._execute_one(action)
                action._mark_applied(res_id=res_id)
            return True
        except Exception as exc:
            _logger.exception("Plan action %s (%s) failed.", action.id, action.action_type)
            action._mark_failed(str(exc))
            return False

    @api.model
    def _execute_one(self, action):
        """Perform the write for one action. Raises on failure; never marks
        state itself, so both the optimistic batch path and the per-row
        retry path can decide what "success" means for their own scope.

        :returns: the new record id for a ``create``, else ``None``.
        """
        action_type = action.action_type
        if action_type == 'create':
            return self._do_create(action)
        if action_type == 'update':
            return self._do_update(action)
        if action_type == 'writeback_sync_id':
            return self._do_writeback(action)
        if action_type == 'soft_delete':
            return self._do_soft_delete(action)
        if action_type == 'quarantine':
            return self._do_quarantine(action)
        raise ValueError('Unknown plan action type %r.' % (action_type,))

    # ------------------------------------------------------------------ #
    # Per-type handlers
    # ------------------------------------------------------------------ #

    @api.model
    def _do_create(self, action):
        """Upsert by ``x_gdrive_sync_id``: a retried create collapses into a
        no-op (or minimal) update rather than colliding with the partial
        unique index (SPEC §3.10) or silently doubling the dataset.

        ``active_test=False`` on the lookup so a retry against a record that
        was archived since the plan was made still finds it, instead of
        missing it and colliding with the unique index on a second create.
        """
        Model = self.env[action.res_model].sudo()
        payload = dict(action.payload or {})
        sync_id = (action.sync_id or payload.get(FIELD_SYNC_ID) or '').strip()

        existing = Model.browse()
        if sync_id:
            existing = Model.with_context(active_test=False).search(
                [(FIELD_SYNC_ID, '=', sync_id)], limit=1)
        if existing:
            self._diff_and_write(existing, payload)
            return existing.id

        record = Model.create(payload)
        return record.id

    @api.model
    def _do_update(self, action):
        """Write only the differing fields (SPEC §9.7): never a full-record
        write, because that stomps fields the sync does not manage and
        poisons the L0b Odoo fast path.
        """
        if not action.res_id:
            raise ValueError('Update action carries no target record id.')
        record = self.env[action.res_model].sudo().browse(action.res_id)
        if not record.exists():
            raise ValueError('%s,%s no longer exists.' % (action.res_model, action.res_id))
        vals = {delta['field']: delta.get('to_typed')
                for delta in (action.deltas or []) if delta.get('field')}
        if vals:
            record.write(vals)
        return None

    @api.model
    def _do_writeback(self, action):
        """Stamp identity fields onto a pre-existing record (SPEC §9.7,
        sequence 10), so later create/update passes in the same run -- and
        every run after it -- can match this record by identity.
        """
        if not action.res_id:
            raise ValueError('Writeback action carries no target record id.')
        record = self.env[action.res_model].sudo().browse(action.res_id)
        if not record.exists():
            raise ValueError('%s,%s no longer exists.' % (action.res_model, action.res_id))
        self._diff_and_write(record, dict(action.payload or {}))
        return None

    @api.model
    def _do_soft_delete(self, action):
        """A flag flip, never ``unlink()`` (SPEC §9.6): hard delete is
        unreachable from any automated path, at any threshold. The identity
        fields are never in ``payload`` -- ``gdrive.plan.action`` enforces
        that at creation time -- so they survive untouched and a restore is
        one flag flip back.
        """
        if not action.res_id:
            raise ValueError('Soft-delete action carries no target record id.')
        record = self.env[action.res_model].sudo().browse(action.res_id)
        if not record.exists():
            raise ValueError('%s,%s no longer exists.' % (action.res_model, action.res_id))
        self._diff_and_write(record, dict(action.payload or {}))
        return None

    @api.model
    def _do_quarantine(self, action):
        """Record a row-level quarantine finding on the staged row, if any.

        Quarantine actions never touch a business record: the whole point is
        that the row was never written anywhere.
        """
        payload = action.payload or {}
        if action.staged_row_id:
            vals = {'state': 'quarantined'}
            reason = payload.get('quarantine_reason')
            if reason:
                vals['quarantine_reason'] = reason
            if 'quarantine_detail' in payload:
                vals['quarantine_detail'] = payload.get('quarantine_detail')
            action.staged_row_id.sudo().write(vals)
        return None

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    @api.model
    def _diff_and_write(self, record, vals):
        """Write only the entries of ``vals`` that actually differ.

        Used for a retried create's collapse-to-update, a soft delete's flag
        flip and a writeback's identity stamp -- every case where the caller
        already knows the *complete* set of fields it might need to touch,
        as opposed to ``update`` actions, which arrive pre-diffed as
        ``deltas`` from the planner. A write that changes nothing still
        bumps ``write_date``, so it is skipped whenever the comparison can
        be made safely; when it cannot (an unexpected field or type), the
        value is written rather than silently dropped.
        """
        diff = {}
        for key, value in vals.items():
            try:
                if record[key] == value:
                    continue
            except Exception:
                pass
            diff[key] = value
        if diff:
            record.write(diff)

    @api.model
    def _finalize_apply_result(self, plan, actions):
        """Roll the per-action outcomes up into ``plan.apply_result``.

        Written here rather than left entirely to ``gdrive.plan.action_apply()``
        because ``execute()`` is called directly in places that never go
        through it -- the idempotency and batch-failure tests, and the heal
        wizard's own apply path. ``action_apply()`` still computes its own
        fallback, but only when this method left ``apply_result`` empty,
        which after this call it never does.
        """
        actions.invalidate_recordset(['state'])
        failed = actions.filtered(lambda a: a.state == 'failed')
        applied = actions.filtered(lambda a: a.state == 'applied')
        if failed and applied:
            result = 'partial'
        elif failed:
            result = 'failed'
        else:
            result = 'success'
        plan.sudo().write({'apply_result': result})
