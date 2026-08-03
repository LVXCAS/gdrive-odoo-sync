# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. Licensed under LGPL-3.
"""Test package for ``gdrive_odoo_sync``.

WHY this file matters more than it looks
========================================
Odoo discovers tests by importing **this** package and then walking the
``TestCase`` subclasses it can reach. A test module that is not imported here
is not "skipped" with a warning — it is silently never run, and a silently
absent test is worse than no test at all, because the suite still goes green.
So every module in ``tests/`` appears below, in dependency order, and adding a
file without adding a line here is a review defect.

WHY it is NOT imported from the module's top-level ``__init__.py``
-----------------------------------------------------------------
Odoo imports ``<module>.tests`` itself, and only under ``--test-enable``.
Importing it from the addon's ``__init__.py`` would pull ``unittest``, the test
fixtures and every mock into a production server process at load time, and any
import error in a test file would then break the *install*. FILE_MANIFEST
assigns every other ``__init__.py`` on the import path to lane A precisely so
that this one can stay out of it.

Layout, in the order the layers depend on each other:

* ``test_lib_*``      — lane C. Pure, stdlib-only, no database, no network.
                        These are the golden-vector tests; if they fail, every
                        hash in the system is meaningless.
* ``test_services_*`` — lane B. Fully mocked transport. No credentials, no
                        network, no Google account required.
* ``test_models_*``   — lane D. ``TransactionCase`` against a real registry.
* the remainder       — lane E's planner, guards, idempotency and convergence,
                        plus the view-loading regression test.

Run them with::

    odoo-bin -d testdb -i gdrive_odoo_sync --test-enable \\
             --test-tags=/gdrive_odoo_sync --stop-after-init
"""

# --- lane C: canonicalization and hashing (pure) --------------------------- #
from . import test_lib_contract
from . import test_lib_text_canon
from . import test_lib_number_canon
from . import test_lib_datetime_canon
from . import test_lib_bool_canon
from . import test_lib_hashing
from . import test_lib_merkle

# --- lane B: Google API client layer (mocked transport) -------------------- #
from . import test_services_auth
from . import test_services_drive
from . import test_services_changes
from . import test_services_sheets
from . import test_services_xlsx

# --- lane D: core Odoo models --------------------------------------------- #
from . import test_models_node_ingest
from . import test_models_staging

# --- lane E: planner, guards, execution ----------------------------------- #
from . import test_reconciler_plan
from . import test_delete_guards
from . import test_apply_idempotency
from . import test_convergence

# --- lane F: the views actually load -------------------------------------- #
from . import test_views_install
