# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. Licensed under LGPL-3.
"""Model package for ``gdrive_odoo_sync``.

WHY the import order below is explicit and not alphabetical
-----------------------------------------------------------
Odoo builds its registry from the classes that exist *after* every module in
this package has been imported, so for ordinary field/model definitions the
order is irrelevant. It stops being irrelevant the moment a module does
anything at import time that touches another module — a shared constant, a
selection list built from another model's helpers, a subclass of an
``AbstractModel`` defined here rather than in ``base``.

Rather than leave that to luck, the order below is the *dependency* order
declared in ``docs/FILE_MANIFEST.md``:

1. ``gdrive_connection`` first — everything else carries a ``connection_id``.
2. The discovery-side models (scope rule, change cursor, node).
3. The staging-side models (dataset, dataset column, staged row).
4. The run log.
5. The two ``AbstractModel`` engines (``gdrive.reconciler``,
   ``gdrive.promoter``) *before* the models that call into them, so that a
   model referencing ``self.env['gdrive.reconciler']`` in a default or a
   selection callable cannot observe a half-built registry.
6. The mapping/promotion/verification/drift/plan models.
7. ``res_config_settings`` last: it is an extension of a ``base`` model and
   references config parameters owned by everything above it.

Changing this order is a real change. Do not sort it.
"""

from . import gdrive_connection
from . import gdrive_scope_rule
from . import gdrive_change_cursor
from . import gdrive_node
from . import gdrive_dataset
from . import gdrive_dataset_column
from . import gdrive_staged_row
from . import gdrive_sync_run
from . import gdrive_sync_run_line
from . import gdrive_reconciler
from . import gdrive_promoter
from . import gdrive_mapping
from . import gdrive_mapping_column
from . import gdrive_promotion_link
from . import gdrive_verification
from . import gdrive_drift
from . import gdrive_plan
from . import gdrive_plan_action
from . import res_config_settings
