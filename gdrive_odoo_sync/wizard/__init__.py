# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. Licensed under LGPL-3.
"""Transient (wizard) model package for ``gdrive_odoo_sync``.

WHY wizards live in their own package rather than in ``models/``: every model
in here is a ``TransientModel`` whose records Odoo's vacuum deletes on a timer.
Keeping them physically separate makes it impossible to accidentally reference
one from a persistent model's ``One2many`` — a foreign key onto a
garbage-collected table, which produces intermittent, unreproducible
``MissingError`` in production.

Order matters here for one reason only: ``gdrive_mapping_builder_wizard`` and
``gdrive_heal_wizard`` each define a header model *and* its line model in the
same file, and the header's ``One2many`` is resolved lazily by name, so no
cross-file ordering constraint exists between the three. The order below is
simply the order an administrator meets them in: test the connection, build a
mapping, then heal.
"""

from . import gdrive_connection_test_wizard
from . import gdrive_mapping_builder_wizard
from . import gdrive_heal_wizard
