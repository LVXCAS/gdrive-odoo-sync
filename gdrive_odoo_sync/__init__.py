# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. See LICENSE (LGPL-3) for full copyright and licensing details.
"""Package root for the ``gdrive_odoo_sync`` Odoo addon.

Only ``models`` and ``wizard`` are imported here.

``tests`` is deliberately NOT imported: Odoo imports the ``tests`` package
itself, and only when the server is started with ``--test-enable``. Importing it
from here would drag ``odoo.tests`` (and every mock fixture) into every
production worker at boot, which both slows startup and can hard-fail an install
on a build that ships without the test extras.

``lib`` and ``services`` are not imported here either; they are plain Python
packages pulled in transitively by the model modules that actually use them.
"""

import logging

from . import models
from . import wizard

_logger = logging.getLogger(__name__)

#: Name of the partial unique index created on every promotion target table.
#: Kept as a module constant so the install hook, the uninstall hook and
#: ``gdrive.mapping.action_validate()`` all derive the same name from the same
#: rule and can therefore find (and drop) each other's work.
SYNC_ID_INDEX_SUFFIX = '_x_gdrive_sync_id_uniq'


def sync_id_index_name(table: str) -> str:
    """Return the deterministic index name used for ``table``'s sync-id index.

    WHY a helper rather than an f-string at each call site: the uninstall hook
    has to *find* indexes created by code that ran during a completely different
    server process, possibly several versions ago. A single derivation rule is
    the only thing that makes that reliable.
    """
    return '%s%s' % (table, SYNC_ID_INDEX_SUFFIX)


def _ensure_sync_id_unique_index(env, table: str) -> bool:
    """Create the partial unique index on ``table (x_gdrive_sync_id)``.

    WHY this index exists at all: it is what turns every ``create`` action in a
    plan into an idempotent upsert. A retried or duplicated create collapses
    into a no-op update instead of silently producing a second copy of the same
    business record. Without it, "apply the plan twice" (a completely ordinary
    outcome of a worker restart mid-batch) doubles the dataset.

    It is *partial* (``WHERE x_gdrive_sync_id IS NOT NULL``) because the target
    model is a shared business model: the thousands of ``res.partner`` rows a
    human created by hand carry NULL there and must not collide with each other.

    Returns True when the index exists after the call, False when the table or
    column is not present (a target model that was uninstalled, for example),
    which is a normal, non-fatal situation.
    """
    if not table or not table.replace('_', '').isalnum():
        _logger.warning("Refusing to build an index on suspicious table name %r", table)
        return False
    env.cr.execute(
        """
        SELECT 1
          FROM information_schema.columns
         WHERE table_name = %s
           AND column_name = 'x_gdrive_sync_id'
        """,
        (table,),
    )
    if not env.cr.fetchone():
        _logger.info(
            "Skipping sync-id index on %r: column x_gdrive_sync_id is absent "
            "(the mapping was never validated, or the target model is gone).",
            table,
        )
        return False
    index = sync_id_index_name(table)
    # CREATE UNIQUE INDEX can legitimately fail if pre-existing data already
    # violates uniqueness. That is a real integrity problem an administrator has
    # to see, so it is logged loudly — but it must not abort the install, or the
    # module becomes uninstallable on a database that merely has dirty data.
    try:
        env.cr.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS "%s" ON "%s" (x_gdrive_sync_id) '
            'WHERE x_gdrive_sync_id IS NOT NULL' % (index, table)
        )
    except Exception:  # pragma: no cover - depends on live database contents
        env.cr.rollback()
        _logger.exception(
            "Could not create %s. Duplicate x_gdrive_sync_id values almost "
            "certainly exist in %s; promotion will not be idempotent until they "
            "are resolved by hand.",
            index,
            table,
        )
        return False
    _logger.info("Ensured partial unique index %s on %s.", index, table)
    return True


def post_init_hook(env):
    """Run after install *and* after every ``-u gdrive_odoo_sync`` upgrade.

    Two jobs, both about not lying to the user after a code change:

    1. **Invalidate every cached content hash.** Every stored ``h_dataset_*`` /
       ``bucket_hashes`` value is only meaningful under the exact normalizer
       that produced it. An upgrade may have bumped ``CANON_VERSION`` or changed
       a canonicalization rule. Serving a hash computed by the *old* normalizer
       as ``verified`` is a silent false pass — the single worst failure mode a
       verification system can have. So we clear the caches and flag every
       connection for a full resync; the next verify pass recomputes from
       scratch. Recomputation costs API calls. A false "verified" costs trust.

    2. **Re-assert the promotion upsert indexes**, because a target model may
       have been reinstalled (which drops and recreates its table, taking any
       non-Odoo-managed index with it) between our last run and now.

    Neither step may raise: a failing post_init hook aborts the module upgrade
    and leaves the database in a half-migrated state.
    """
    _logger.info("gdrive_odoo_sync post_init: invalidating cached hashes and re-asserting indexes.")

    try:
        datasets = env['gdrive.dataset'].sudo().with_context(active_test=False).search([])
        if datasets:
            datasets.write({
                'h_dataset_sheet': False,
                'h_dataset_odoo': False,
                'bucket_hashes': {},
                'last_drive_version': False,
                'last_drive_modified': False,
                'last_odoo_count': 0,
                'last_odoo_max_write_date': False,
            })
        _logger.info("Cleared cached verification hashes on %d dataset(s).", len(datasets))
    except Exception:  # pragma: no cover - fresh install has no datasets yet
        env.cr.rollback()
        _logger.exception("Could not clear cached dataset hashes; a manual full resync is required.")

    try:
        connections = env['gdrive.connection'].sudo().with_context(active_test=False).search([])
        if connections:
            connections.write({'full_resync_requested': True})
        _logger.info("Flagged %d connection(s) for a full resync.", len(connections))
    except Exception:  # pragma: no cover - fresh install has no connections yet
        env.cr.rollback()
        _logger.exception("Could not flag connections for full resync.")

    try:
        mappings = env['gdrive.mapping'].sudo().with_context(active_test=False).search([])
        tables = set()
        for mapping in mappings:
            model_name = mapping.target_model
            if not model_name or model_name not in env:
                continue
            tables.add(env[model_name]._table)
        for table in sorted(tables):
            _ensure_sync_id_unique_index(env, table)
    except Exception:  # pragma: no cover - fresh install has no mappings yet
        env.cr.rollback()
        _logger.exception("Could not re-assert promotion sync-id indexes.")


def uninstall_hook(env):
    """Run before the module's records are removed.

    Deliberately conservative. We drop only the artefacts this module created
    that Odoo will *not* clean up itself — the partial unique indexes on
    third-party tables — and we leave the data alone:

    * ``x_gdrive_sync_id`` / ``x_gdrive_source_dataset`` columns stay. They are
      the only surviving evidence of which business records came from a sheet.
      Dropping them silently destroys the ability to re-link on reinstall, and
      an uninstall is frequently a step in a reinstall.
    * Mirrored ``ir.attachment`` rows stay. They are the user's files.
    * ``active = False`` soft-deleted records stay soft-deleted; flipping them
      back would be a mass data change nobody asked for.

    Anything that cannot be cleaned is logged rather than raised: a hook that
    raises here leaves the module wedged in a half-uninstalled state.
    """
    _logger.info("gdrive_odoo_sync uninstall: dropping promotion sync-id indexes.")
    try:
        env.cr.execute(
            """
            SELECT indexname
              FROM pg_indexes
             WHERE schemaname = current_schema()
               AND indexname LIKE %s
            """,
            ('%' + SYNC_ID_INDEX_SUFFIX,),
        )
        names = [row[0] for row in env.cr.fetchall()]
        for name in names:
            env.cr.execute('DROP INDEX IF EXISTS "%s"' % name)
        _logger.info("Dropped %d sync-id index(es): %s", len(names), ', '.join(names) or '-')
    except Exception:  # pragma: no cover - depends on live database contents
        env.cr.rollback()
        _logger.exception("Could not drop sync-id indexes; they are harmless but must be removed by hand.")

    _logger.warning(
        "gdrive_odoo_sync uninstalled. Columns x_gdrive_sync_id and "
        "x_gdrive_source_dataset were intentionally LEFT IN PLACE on target "
        "models, as were all mirrored attachments. Remove them manually only if "
        "you are certain you will never reinstall."
    )
