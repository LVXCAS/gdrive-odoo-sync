# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. Licensed under LGPL-3.
"""Lane C — the column contract and ``spec_version`` (CANONICALIZATION §3, §10).

WHY THESE TESTS EXIST
=====================
``spec_version`` is the *only* structural defence against a silent false pass.
A stored hash whose ``spec_version`` matches the current one is served as a
cache hit, so any pair of behaviourally different contracts that produce the
same ``spec_version`` means the system will report ``verified`` over data it
never compared under the current rules — with a green dashboard.

Two such collisions were real and are pinned here:

* delimiter injection in the option serializer (``{"a": "b,c=d"}`` versus
  ``{"a": "b", "c": "d"}``), and
* ``header_canon`` being excluded from the preimage, which let an administrator
  re-bind a contract key to a different physical column for free.

The third test is the import smoke test: this package is pure and stdlib-only,
so a module that does not exist on disk must fail here — loudly, in a fast test
— and never at install time in front of a user.
"""

from odoo.tests.common import BaseCase

from odoo.addons.gdrive_odoo_sync.lib.contract import (
    ColumnContract,
    serialize_contracts,
    spec_version_for_contracts,
)


def _selection(value_map):
    return ColumnContract(
        key="state", odoo_field="state", ctype="selection", value_map=value_map)


def _bool(truthy, falsy=("false", "no")):
    return ColumnContract(key="flag", odoo_field="flag", ctype="bool",
                          truthy=tuple(truthy), falsy=tuple(falsy))


class TestImportSmoke(BaseCase):
    """``import gdrive_odoo_sync.lib`` must succeed on its own."""

    def test_the_library_package_imports(self):
        # A missing lib/*.py module used to surface only as a ModuleNotFoundError
        # during registry load, i.e. as a failed install rather than a failed
        # test. Importing the package here makes CI the place that finds it.
        from odoo.addons.gdrive_odoo_sync import lib

        for name in lib.__all__:
            with self.subTest(name=name):
                self.assertTrue(hasattr(lib, name), 'lib.%s is re-exported but absent' % name)


class TestSpecVersionDelimiterInjection(BaseCase):
    """Two contracts that behave differently must never share a ``spec_version``."""

    def test_value_map_comma_and_equals_do_not_collide(self):
        # These canonicalize every cell in the column differently: the first maps
        # the single label "a", the second maps "a" and "c".
        a = spec_version_for_contracts([_selection({"a": "b,c=d"})])
        b = spec_version_for_contracts([_selection({"a": "b", "c": "d"})])
        self.assertNotEqual(a, b)

    def test_truthy_tokens_do_not_collide(self):
        # ("yes,no",) recognises the single literal token "yes,no"; ("yes","no")
        # recognises "yes" — and "no" is in falsy, so this flips every boolean
        # cell reading "no".
        self.assertNotEqual(
            spec_version_for_contracts([_bool(("yes,no",))]),
            spec_version_for_contracts([_bool(("yes", "no"))]),
        )

    def test_date_formats_do_not_collide(self):
        one = ColumnContract(key="d", odoo_field="d", ctype="date",
                             date_formats=("%Y-%m-%d,%m/%d/%Y",))
        two = ColumnContract(key="d", odoo_field="d", ctype="date",
                             date_formats=("%Y-%m-%d", "%m/%d/%Y"))
        self.assertNotEqual(spec_version_for_contracts([one]),
                            spec_version_for_contracts([two]))

    def test_every_serialized_key_is_a_jcs_identifier(self):
        from odoo.addons.gdrive_odoo_sync.lib.jcs import is_valid_jcs_key

        payload = serialize_contracts([_selection({"Draft state": "draft"})])
        for key, value in payload.items():
            with self.subTest(key=key):
                self.assertTrue(is_valid_jcs_key(key))
                self.assertIsInstance(value, str)


class TestSpecVersionBinding(BaseCase):
    """What the contract reads is part of the contract."""

    def test_rebinding_a_key_to_another_header_changes_the_spec_version(self):
        # Same odoo_field, same options, different physical column. Every stored
        # h_row was computed from the old column and must stop being a cache hit.
        old = ColumnContract(key="partner_vat", odoo_field="partner_vat",
                             header_canon="s:VAT", col_index=3)
        new = ColumnContract(key="partner_vat", odoo_field="partner_vat",
                             header_canon="s:Tax ID", col_index=7)
        self.assertNotEqual(spec_version_for_contracts([old]),
                            spec_version_for_contracts([new]))

    def test_a_pure_reorder_does_not_change_the_spec_version(self):
        # Dragging a column is not a data change: col_index moves, header_canon
        # does not, and the row hash keys on odoo_field either way.
        at_d = ColumnContract(key="amount", odoo_field="amount", header_canon="s:Amount", col_index=3)
        at_b = ColumnContract(key="amount", odoo_field="amount", header_canon="s:Amount", col_index=1)
        self.assertEqual(spec_version_for_contracts([at_d]),
                         spec_version_for_contracts([at_b]))

    def test_column_list_order_does_not_change_the_spec_version(self):
        a = ColumnContract(key="a", odoo_field="a", header_canon="s:A")
        b = ColumnContract(key="b", odoo_field="b", header_canon="s:B")
        self.assertEqual(spec_version_for_contracts([a, b]),
                         spec_version_for_contracts([b, a]))
