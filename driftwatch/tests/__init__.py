# -*- coding: utf-8 -*-
"""Tests for the standalone DriftWatch service.

Plain ``unittest``, deliberately: these run on the machine that hosts the
service, which has Python and nothing else installed.

    python -m unittest discover -s driftwatch/tests -t .

The Odoo addon's own tests live in ``gdrive_odoo_sync/tests`` and need an Odoo
runtime; these need nothing and touch no network.
"""
