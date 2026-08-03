# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. Licensed under LGPL-3.
"""Lane F — the views actually load, and stay Odoo 18 clean.

WHY a test for something the installer already checks
=====================================================
Odoo validates a view when it is loaded, so a broken view fails the install.
That is exactly the problem: the failure happens during ``-u``, on a production
database, halfway through an upgrade, with a traceback that names an XML id and
not the mistake. This test moves the failure to the developer's machine and
names the mistake.

Five Odoo-18 regressions are specifically hunted, because each one is
*syntactically fine* in Odoo 16 and therefore survives copy-paste from any
older module or any older answer on the internet:

* ``<tree>`` — removed; the root element of a list view is ``<list>``;
* ``view_mode="tree,form"`` — same, in action definitions;
* ``attrs="{...}"`` and ``states="..."`` — removed entirely, replaced by direct
  ``invisible=`` / ``readonly=`` / ``required=`` expressions;
* a field referenced in one of those expressions but **absent from the view** —
  the expression silently evaluates against an undefined name;
* ``<div class="oe_chatter">`` — replaced by ``<chatter/>``.

Tagged ``post_install`` so it runs against the fully loaded registry, with every
view, action and menu already in the database.
"""

import ast
import os
import re

from lxml import etree

from odoo.modules.module import get_module_path
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

MODULE = "gdrive_odoo_sync"

#: Attributes whose value is a Python expression evaluated against the record.
MODIFIER_ATTRS = ("invisible", "readonly", "required", "column_invisible")

#: Names that are always available inside a modifier expression and therefore
#: need no ``<field>`` in the view.
BUILTIN_NAMES = frozenset({
    "True", "False", "None", "id", "active_id", "uid", "context", "parent",
    "in", "not", "and", "or", "if", "else", "for", "len", "set", "list",
    "context_today", "current_date", "datetime", "time", "relativedelta",
})

_IDENT_RE = re.compile(r"\b[a-zA-Z_][a-zA-Z0-9_]*\b")

#: Quoted literals, stripped before identifiers are extracted — otherwise the
#: word inside ``state == 'draft'`` looks exactly like a field reference.
_STRING_RE = re.compile(r"'[^']*'|\"[^\"]*\"")

#: Tags that open a new modifier scope when they appear inside a ``<field>``.
SUBVIEW_TAGS = frozenset({
    "list", "form", "kanban", "search", "calendar", "graph", "pivot", "activity",
})


def _module_dir():
    return get_module_path(MODULE)


def _manifest():
    with open(os.path.join(_module_dir(), "__manifest__.py"), "r", encoding="utf-8") as handle:
        return ast.literal_eval(handle.read())


def _data_xml_files():
    """Every XML file the manifest loads, in manifest order."""
    root = _module_dir()
    return [
        (path, os.path.join(root, path))
        for path in _manifest().get("data", [])
        if path.endswith(".xml")
    ]


def _content_without_comments(absolute):
    """File text with XML comments removed.

    The comments in this module deliberately *name* the constructs that must not
    appear ("no numbercall, no doall"), so a naive substring scan over the raw
    file would flag its own documentation.
    """
    tree = etree.parse(absolute)
    root = tree.getroot()
    for comment in root.xpath("//comment()"):
        comment.getparent().remove(comment)
    return etree.tostring(root, encoding="unicode")


def _iter_view_archs():
    """Yield ``(relative_path, xml_id, arch_element)`` for every ir.ui.view."""
    for relative, absolute in _data_xml_files():
        tree = etree.parse(absolute)
        for record in tree.getroot().iter("record"):
            if record.get("model") != "ir.ui.view":
                continue
            arch = record.find("./field[@name='arch']")
            if arch is None:
                continue
            for child in arch:
                if isinstance(child.tag, str):
                    yield relative, record.get("id"), child


def _scope_elements(scope):
    """Yield the elements belonging to ``scope``, **not** descending into subviews.

    A ``<field>`` that itself contains a subview opens a new scope: its children
    belong to the embedded model. Walking into them from the parent would both
    let an embedded field satisfy a parent reference and make every embedded
    reference look unsatisfied — the two mistakes this walk exists to avoid.
    """
    yield scope
    for child in scope:
        if not isinstance(child.tag, str):
            continue
        if child.tag == "field" and any(
            isinstance(g.tag, str) and g.tag in SUBVIEW_TAGS for g in child
        ):
            yield child  # the field element itself may carry modifiers
            continue
        yield from _scope_elements(child)


def _scope_fields(scope):
    """Field names declared directly in ``scope``."""
    return {
        element.get("name")
        for element in _scope_elements(scope)
        if element.tag == "field" and element.get("name")
    }


def _iter_scopes(arch):
    """Yield every modifier scope in ``arch``: the root, plus each embedded subview."""
    yield arch
    for field in arch.iter("field"):
        for child in field:
            if isinstance(child.tag, str) and child.tag in SUBVIEW_TAGS:
                yield child


def _referenced_names(expression):
    """Identifiers a modifier expression depends on, ignoring string literals."""
    stripped = _STRING_RE.sub("", expression or "")
    return {
        name for name in _IDENT_RE.findall(stripped)
        if name not in BUILTIN_NAMES
    }


@tagged("post_install", "-at_install")
class TestManifestIntegrity(TransactionCase):
    """The manifest itself must be Odoo 18 shaped."""

    def test_version_series_is_18(self):
        self.assertTrue(_manifest()["version"].startswith("18.0."), _manifest()["version"])

    def test_every_declared_data_file_exists(self):
        root = _module_dir()
        for path in _manifest().get("data", []):
            with self.subTest(path=path):
                self.assertTrue(os.path.isfile(os.path.join(root, path)),
                                "declared in the manifest but missing on disk")

    def test_security_loads_before_views(self):
        # Views reference groups; loading them first fails the install with an
        # unhelpful "External ID not found".
        data = _manifest()["data"]
        first_view = min(i for i, p in enumerate(data) if p.startswith("views/"))
        last_security = max(i for i, p in enumerate(data) if p.startswith("security/"))
        self.assertLess(last_security, first_view)

    def test_menus_load_last(self):
        # Menus reference actions, which are defined in the view files.
        data = _manifest()["data"]
        self.assertEqual(data[-1], "views/gdrive_menus.xml", data[-1])

    def test_no_enterprise_dependency(self):
        # The module must install on Community; it builds on ir.attachment only.
        self.assertNotIn("documents", _manifest()["depends"])


@tagged("post_install", "-at_install")
class TestOdoo18ViewSyntax(TransactionCase):
    """The five constructs that are fine in 16 and fatal in 18."""

    def test_no_tree_element_anywhere(self):
        for relative, absolute in _data_xml_files():
            with self.subTest(path=relative):
                tree = etree.parse(absolute)
                offenders = [el.tag for el in tree.getroot().iter("tree")]
                self.assertEqual(offenders, [], "<tree> was removed in Odoo 18; use <list>")

    def test_no_attrs_attribute_anywhere(self):
        for relative, absolute in _data_xml_files():
            with self.subTest(path=relative):
                tree = etree.parse(absolute)
                offenders = [
                    el.tag for el in tree.getroot().iter()
                    if isinstance(el.tag, str) and el.get("attrs") is not None
                ]
                self.assertEqual(offenders, [],
                                 "attrs= was removed in Odoo 18; use direct expressions")

    def test_no_states_attribute_anywhere(self):
        for relative, absolute in _data_xml_files():
            with self.subTest(path=relative):
                tree = etree.parse(absolute)
                offenders = [
                    el.tag for el in tree.getroot().iter()
                    if isinstance(el.tag, str) and el.get("states") is not None
                ]
                self.assertEqual(offenders, [],
                                 "states= was removed in Odoo 18; use invisible= instead")

    def test_no_legacy_chatter_div(self):
        for relative, absolute in _data_xml_files():
            with self.subTest(path=relative):
                self.assertNotIn("oe_chatter", _content_without_comments(absolute),
                                 "use <chatter/> in Odoo 18")

    def test_no_group_operator_attribute(self):
        for relative, absolute in _data_xml_files():
            with self.subTest(path=relative):
                self.assertNotIn("group_operator", _content_without_comments(absolute),
                                 "renamed to aggregator in Odoo 18")

    def test_list_views_use_the_list_root(self):
        seen = 0
        for relative, xml_id, arch in _iter_view_archs():
            if arch.tag == "list":
                seen += 1
        self.assertGreater(seen, 0, "the module should ship at least one list view")


@tagged("post_install", "-at_install")
class TestActionViewModes(TransactionCase):
    """``view_mode`` is ``list,form`` — in XML and in the loaded records alike."""

    def test_no_tree_in_any_declared_view_mode(self):
        for relative, absolute in _data_xml_files():
            tree = etree.parse(absolute)
            for record in tree.getroot().iter("record"):
                if record.get("model") != "ir.actions.act_window":
                    continue
                mode = record.find("./field[@name='view_mode']")
                if mode is None or not mode.text:
                    continue
                with self.subTest(action=record.get("id")):
                    self.assertNotIn("tree", mode.text)

    def test_loaded_actions_agree(self):
        actions = self.env["ir.actions.act_window"].search([
            ("res_model", "like", "gdrive.%"),
        ])
        self.assertTrue(actions, "the module should ship act_window actions")
        for action in actions:
            with self.subTest(action=action.xml_id or action.name):
                self.assertNotIn("tree", action.view_mode)

    def test_list_comes_before_form_where_both_are_present(self):
        actions = self.env["ir.actions.act_window"].search([("res_model", "like", "gdrive.%")])
        for action in actions:
            modes = action.view_mode.split(",")
            if "list" in modes and "form" in modes:
                with self.subTest(action=action.name):
                    self.assertLess(modes.index("list"), modes.index("form"))


@tagged("post_install", "-at_install")
class TestModifierExpressions(TransactionCase):
    """Every field used in an expression must be present in the same scope.

    Odoo does not error on a missing name — the expression simply evaluates
    against an undefined value, so the button that was supposed to be hidden
    stays visible, or the field that was supposed to be required is not.
    """

    def test_every_referenced_field_is_declared(self):
        problems = []
        for relative, xml_id, arch in _iter_view_archs():
            for scope in _iter_scopes(arch):
                declared = _scope_fields(scope)
                for element in _scope_elements(scope):
                    if not isinstance(element.tag, str):
                        continue
                    for attr in MODIFIER_ATTRS:
                        expression = element.get(attr)
                        if not expression or expression in ("1", "0", "True", "False"):
                            continue
                        for name in _referenced_names(expression):
                            if name not in declared:
                                problems.append(
                                    "%s / %s: <%s %s=%r> references %r which is not a "
                                    "field in that view scope"
                                    % (relative, xml_id, element.tag, attr, expression, name)
                                )
        self.assertEqual(problems, [], "\n".join(problems))

    def test_decoration_expressions_only_use_declared_fields(self):
        problems = []
        for relative, xml_id, arch in _iter_view_archs():
            for scope in _iter_scopes(arch):
                declared = _scope_fields(scope)
                for element in _scope_elements(scope):
                    if not isinstance(element.tag, str):
                        continue
                    for attr, expression in element.attrib.items():
                        if not attr.startswith("decoration-"):
                            continue
                        for name in _referenced_names(expression):
                            if name not in declared:
                                problems.append(
                                    "%s / %s: <%s %s=%r> references %r which is not a "
                                    "field in that view scope"
                                    % (relative, xml_id, element.tag, attr, expression, name)
                                )
        self.assertEqual(problems, [], "\n".join(problems))


@tagged("post_install", "-at_install")
class TestViewsAreLoaded(TransactionCase):
    """The registry must actually contain what the manifest declares."""

    #: Models a human navigates to directly and therefore needs a hand-written
    #: form for. Line models (plan actions, run log lines) are only ever seen
    #: embedded in their parent and deliberately have no standalone form.
    MODELS = [
        "gdrive.connection", "gdrive.scope.rule", "gdrive.node", "gdrive.dataset",
        "gdrive.dataset.column", "gdrive.staged.row", "gdrive.mapping",
        "gdrive.mapping.column", "gdrive.verification", "gdrive.drift",
        "gdrive.plan", "gdrive.sync.run",
    ]

    def test_every_core_model_has_a_form(self):
        for model in self.MODELS:
            with self.subTest(model=model):
                views = self.env["ir.ui.view"].search([("model", "=", model)])
                types = set(views.mapped("type"))
                self.assertIn("form", types, "%s has no form view" % model)

    def test_every_core_model_has_a_list(self):
        for model in self.MODELS:
            with self.subTest(model=model):
                views = self.env["ir.ui.view"].search([("model", "=", model)])
                self.assertIn("list", set(views.mapped("type")), "%s has no list view" % model)

    def test_the_triage_surface_groups_and_colours_drift(self):
        # The drift list is the screen a human lives in, so its severity
        # decorations and its group-bys are part of the deliverable.
        view = self.env.ref("%s.gdrive_drift_view_list" % MODULE)
        arch = etree.fromstring(view.arch_db.encode("utf-8"))
        decorations = [a for a in arch.attrib if a.startswith("decoration-")]
        self.assertTrue(decorations, "the drift list must colour-code severity")

        search = self.env.ref("%s.gdrive_drift_view_search" % MODULE)
        search_arch = etree.fromstring(search.arch_db.encode("utf-8"))
        group_bys = [
            el.get("context") for el in search_arch.iter("filter")
            if el.get("context") and "group_by" in el.get("context")
        ]
        joined = " ".join(group_bys)
        self.assertIn("dataset_id", joined)
        self.assertIn("drift_type", joined)
        self.assertIn("severity", joined)

    def test_the_drift_action_opens_on_open_findings(self):
        action = self.env.ref("%s.action_gdrive_drift" % MODULE)
        self.assertIn("search_default_filter_open", action.context or "")

    def test_staged_row_filters_never_touch_the_json_columns(self):
        # payload and canon are fields.Json: unsearchable, ungroupable, and a
        # filter on one either returns nothing or raises on read_group.
        search = self.env.ref("%s.gdrive_staged_row_view_search" % MODULE)
        arch = etree.fromstring(search.arch_db.encode("utf-8"))
        for element in arch.iter():
            for attr in ("domain", "context"):
                value = element.get(attr) or ""
                with self.subTest(attr=attr, value=value):
                    self.assertNotIn("'payload'", value)
                    self.assertNotIn("'canon'", value)


@tagged("post_install", "-at_install")
class TestMenus(TransactionCase):
    """Every menu resolves, and none of them is visible to a user with no group."""

    def test_the_root_menu_exists(self):
        root = self.env.ref("%s.menu_gdrive_root" % MODULE)
        self.assertFalse(root.parent_id)

    def test_every_menu_action_resolves(self):
        menus = self.env["ir.ui.menu"].with_context(active_test=False).search([])
        module_menus = menus.filtered(
            lambda m: (m.get_external_id().get(m.id) or "").startswith(MODULE + ".")
        )
        self.assertTrue(module_menus, "the module should ship menus")
        for menu in module_menus:
            if not menu.action:
                continue
            with self.subTest(menu=menu.complete_name):
                self.assertTrue(menu.action.exists())

    def test_every_menu_is_group_restricted(self):
        menus = self.env["ir.ui.menu"].with_context(active_test=False).search([])
        module_menus = menus.filtered(
            lambda m: (m.get_external_id().get(m.id) or "").startswith(MODULE + ".")
        )
        for menu in module_menus:
            with self.subTest(menu=menu.complete_name):
                self.assertTrue(
                    menu.groups_id,
                    "an unrestricted menu shows empty lists to users with no ACL row, "
                    "which reads as 'the sync is broken' rather than 'this is not yours'",
                )


@tagged("post_install", "-at_install")
class TestCronDefinitions(TransactionCase):
    """``numbercall`` and ``doall`` were removed in Odoo 18 and hard-fail install."""

    def test_no_removed_cron_fields(self):
        path = os.path.join(_module_dir(), "data", "ir_cron_data.xml")
        tree = etree.parse(path)
        declared = {
            field.get("name")
            for record in tree.getroot().iter("record")
            if record.get("model") == "ir.cron"
            for field in record.iter("field")
        }
        self.assertNotIn("numbercall", declared)
        self.assertNotIn("doall", declared)

    def test_crons_are_noupdate(self):
        # Without noupdate every upgrade stamps the shipped interval back over
        # the administrator's tuning.
        path = os.path.join(_module_dir(), "data", "ir_cron_data.xml")
        tree = etree.parse(path)
        data_nodes = tree.getroot().findall("./data")
        self.assertTrue(data_nodes, "crons must live inside <data noupdate='1'>")
        for node in data_nodes:
            self.assertEqual(node.get("noupdate"), "1")

    def test_every_cron_is_loaded(self):
        for xml_id in (
            "ir_cron_gdrive_discover", "ir_cron_gdrive_ingest", "ir_cron_gdrive_stage",
            "ir_cron_gdrive_promote", "ir_cron_gdrive_verify",
            "ir_cron_gdrive_full_resync", "ir_cron_gdrive_gc",
        ):
            with self.subTest(cron=xml_id):
                cron = self.env.ref("%s.%s" % (MODULE, xml_id))
                self.assertEqual(cron.state, "code")


@tagged("post_install", "-at_install")
class TestAccessRules(TransactionCase):
    """SPEC §7.2 — an empty ``group_id`` grants the permission to everybody."""

    def test_no_acl_row_has_an_empty_group(self):
        acls = self.env["ir.model.access"].search([("model_id.model", "like", "gdrive.%")])
        self.assertTrue(acls, "the module should ship ACL rows")
        for acl in acls:
            with self.subTest(acl=acl.name):
                self.assertTrue(
                    acl.group_id,
                    "an empty group_id grants this permission to every user, "
                    "including portal and public",
                )

    def test_every_gdrive_model_has_at_least_one_acl_row(self):
        models = self.env["ir.model"].search([("model", "like", "gdrive.%")])
        for model in models:
            with self.subTest(model=model.model):
                acls = self.env["ir.model.access"].search_count([("model_id", "=", model.id)])
                self.assertGreater(acls, 0, "a model with no ACL row is unusable")
