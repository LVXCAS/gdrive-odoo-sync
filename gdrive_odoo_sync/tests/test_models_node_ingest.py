# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. Licensed under LGPL-3.
"""Lane D — the mirrored Drive tree and attachment ingest (SPEC §3.4, §5.1, §5.3).

WHY the attachment assertions are this specific
===============================================
``ir.attachment`` has three ways to make a mirrored file *appear to vanish*, and
none of them raises:

* setting ``res_field`` — ``ir.attachment``'s read/search override filters
  field-bound attachments out of the generic Attachments sidebar, so the file is
  stored, findable by SQL, and invisible in the UI;
* passing both ``raw`` and ``datas`` — the payload is double-encoded and the
  download is corrupt;
* writing without ``sudo()`` in a cron — attachment ACL derives from
  ``(res_model, res_id)``, so the write fails for exactly the user that a
  scheduled job runs as.

And one assertion that is about data safety rather than the UI: a node that
disappears from Drive is marked ``gone`` and **never unlinked**. ``removed=True``
means deleted *or* trashed *or* permission-revoked *or* moved out of scope —
four different events with one indistinguishable signal, only one of which is
"this data no longer exists".
"""

from odoo.tests.common import TransactionCase

from odoo.addons.gdrive_odoo_sync.models.gdrive_node import KEEP_VERSIONS

PDF = "application/pdf"
FOLDER = "application/vnd.google-apps.folder"
GSHEET = "application/vnd.google-apps.spreadsheet"
GDOC = "application/vnd.google-apps.document"
SHORTCUT = "application/vnd.google-apps.shortcut"


class GDriveNodeCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.connection = cls.env["gdrive.connection"].create({
            "name": "Test — lucaso@",
            "subject_email": "lucaso@avatarnaturalfoods.com",
        })

    def _node(self, google_id, name="file", mime=PDF, **kw):
        vals = {
            "connection_id": self.connection.id,
            "google_id": google_id,
            "name": name,
            "mime_type": mime,
            "node_type": kw.pop("node_type", "blob"),
        }
        vals.update(kw)
        return self.env["gdrive.node"].create(vals)


class TestMetadataMapping(GDriveNodeCase):
    """A literal ``files`` resource must map field for field."""

    META = {
        "id": "1abcDEF",
        "name": "Bettr_Bowl_Data_Request",
        "mimeType": GSHEET,
        "parents": ["0PARENT", "0SECOND"],
        "modifiedTime": "2026-07-28T10:11:12.000Z",
        "createdTime": "2026-01-02T03:04:05.000Z",
        "size": "2621440",
        "md5Checksum": "d41d8cd98f00b204e9800998ecf8427e",
        "version": 41,
        "trashed": False,
        "driveId": "0ABCdef",
        "owners": [{"emailAddress": "michael@avatarnaturalfoods.com"}],
        "webViewLink": "https://docs.google.com/spreadsheets/d/1abcDEF/edit",
        "capabilities": {"canDownload": True},
        "sharedWithMeTime": "2026-02-01T00:00:00.000Z",
    }

    def test_identity_is_the_file_id(self):
        vals = self.env["gdrive.node"]._vals_from_meta(self.connection, self.META)
        self.assertEqual(vals["google_id"], "1abcDEF")

    def test_classification_comes_from_the_mime_type(self):
        vals = self.env["gdrive.node"]._vals_from_meta(self.connection, self.META)
        self.assertEqual(vals["node_type"], "spreadsheet")

    def test_all_parents_are_retained(self):
        # Only shared-drive files are guaranteed one parent; the whole array is
        # kept so the tree resolver can pick the first visible one.
        vals = self.env["gdrive.node"]._vals_from_meta(self.connection, self.META)
        self.assertEqual(vals["parent_google_ids"], ["0PARENT", "0SECOND"])

    def test_owner_and_sharing_are_captured(self):
        vals = self.env["gdrive.node"]._vals_from_meta(self.connection, self.META)
        self.assertEqual(vals["owner_email"], "michael@avatarnaturalfoods.com")
        self.assertTrue(vals["is_shared_with_me"])

    def test_version_is_stored_as_a_string(self):
        # Drive `version` is an int64; forcing it through a Python int is a
        # silent overflow risk on some backends and the value is only ever
        # compared for equality.
        vals = self.env["gdrive.node"]._vals_from_meta(self.connection, self.META)
        self.assertEqual(vals["drive_version"], "41")

    def test_size_survives_being_a_string(self):
        vals = self.env["gdrive.node"]._vals_from_meta(self.connection, self.META)
        self.assertEqual(vals["size_bytes"], 2621440)

    def test_absent_size_and_checksum_on_native_types(self):
        native = dict(self.META)
        native.pop("size")
        native.pop("md5Checksum")
        vals = self.env["gdrive.node"]._vals_from_meta(self.connection, native)
        self.assertEqual(vals["size_bytes"], 0)
        self.assertFalse(vals["md5_checksum"])

    def test_shortcut_details_are_captured(self):
        meta = dict(self.META, mimeType=SHORTCUT,
                    shortcutDetails={"targetId": "1TARGET", "targetMimeType": GSHEET})
        vals = self.env["gdrive.node"]._vals_from_meta(self.connection, meta)
        self.assertEqual(vals["node_type"], "shortcut")
        self.assertEqual(vals["shortcut_target_google_id"], "1TARGET")


class TestUpsert(GDriveNodeCase):
    """Discovery is at-least-once, so every handler has to be idempotent."""

    def _meta(self, gid, **kw):
        meta = {"id": gid, "name": gid, "mimeType": PDF, "version": 1,
                "capabilities": {"canDownload": True}}
        meta.update(kw)
        return meta

    def test_first_pass_creates(self):
        nodes = self.env["gdrive.node"]._upsert_from_drive(
            self.connection, [self._meta("A"), self._meta("B")])
        self.assertEqual(len(nodes), 2)

    def test_second_pass_does_not_duplicate(self):
        Node = self.env["gdrive.node"]
        Node._upsert_from_drive(self.connection, [self._meta("A")])
        Node._upsert_from_drive(self.connection, [self._meta("A")])
        found = Node.with_context(active_test=False).search([
            ("connection_id", "=", self.connection.id), ("google_id", "=", "A")])
        self.assertEqual(len(found), 1)

    def test_two_files_with_the_same_title_are_two_nodes(self):
        # Both `Bettr_Bowl_Data_Request` files really exist. Titles are display
        # strings; file ids are identity.
        Node = self.env["gdrive.node"]
        Node._upsert_from_drive(self.connection, [
            self._meta("1FIRST", name="Bettr_Bowl_Data_Request"),
            self._meta("1SECOND", name="Bettr_Bowl_Data_Request"),
        ])
        found = Node.with_context(active_test=False).search([
            ("connection_id", "=", self.connection.id),
            ("name", "=", "Bettr_Bowl_Data_Request")])
        self.assertEqual(len(found), 2)

    def test_a_rename_updates_in_place(self):
        Node = self.env["gdrive.node"]
        Node._upsert_from_drive(self.connection, [self._meta("A", name="before")])
        Node._upsert_from_drive(self.connection, [self._meta("A", name="after", version=2)])
        node = Node.with_context(active_test=False).search([("google_id", "=", "A")])
        self.assertEqual(node.name, "after")

    def test_empty_batch_is_a_no_op(self):
        self.assertEqual(len(self.env["gdrive.node"]._upsert_from_drive(self.connection, [])), 0)
        self.assertEqual(
            len(self.env["gdrive.node"]._upsert_from_drive(self.connection, [{}, None])), 0)


class TestTreeResolution(GDriveNodeCase):
    """Parents legitimately arrive after their children in a flat enumeration."""

    def test_path_and_depth_are_materialized(self):
        root = self._node("R", name="My Drive", mime=FOLDER, node_type="folder")
        mid = self._node("M", name="Finance", mime=FOLDER, node_type="folder",
                         parent_google_ids=["R"])
        leaf = self._node("L", name="Cashflow.xlsx", parent_google_ids=["M"])
        self.env["gdrive.node"]._resolve_tree(self.connection)

        self.assertEqual(leaf.parent_id, mid)
        self.assertEqual(mid.parent_id, root)
        self.assertGreater(leaf.depth, mid.depth)
        self.assertIn("Finance", leaf.path)
        self.assertIn("Cashflow.xlsx", leaf.path)

    def test_children_discovered_before_parents_still_resolve(self):
        leaf = self._node("L", name="leaf", parent_google_ids=["P"])
        self.env["gdrive.node"]._resolve_tree(self.connection)
        self.assertTrue(leaf.is_orphan)

        parent = self._node("P", name="parent", mime=FOLDER, node_type="folder")
        self.env["gdrive.node"]._resolve_tree(self.connection)
        self.assertFalse(leaf.is_orphan)
        self.assertEqual(leaf.parent_id, parent)

    def test_invisible_parents_make_an_orphan_not_an_error(self):
        node = self._node("L", name="shared file", parent_google_ids=["NOT_VISIBLE"])
        self.env["gdrive.node"]._resolve_tree(self.connection)
        self.assertTrue(node.is_orphan)
        self.assertFalse(node.parent_id)

    def test_first_resolvable_parent_wins(self):
        second = self._node("P2", name="Second", mime=FOLDER, node_type="folder")
        node = self._node("L", name="multi", parent_google_ids=["MISSING", "P2"])
        self.env["gdrive.node"]._resolve_tree(self.connection)
        self.assertEqual(node.parent_id, second)

    def test_self_reference_does_not_loop(self):
        node = self._node("S", name="weird", parent_google_ids=["S"])
        self.env["gdrive.node"]._resolve_tree(self.connection)
        self.assertFalse(node.parent_id)


class TestAttachmentMirroring(GDriveNodeCase):
    """SPEC §5.3 — the three ways to make a mirrored file invisible."""

    def test_content_is_stored_and_readable(self):
        node = self._node("A", name="Report", mime=PDF)
        attachment = node._store_attachment("Report.pdf", b"%PDF-1.7 hello", PDF)
        self.assertEqual(attachment.raw, b"%PDF-1.7 hello")
        self.assertEqual(node.attachment_id, attachment)

    def test_res_field_is_never_set(self):
        # An attachment carrying res_field is filtered out of the Attachments
        # sidebar by ir.attachment's own read/search override.
        node = self._node("A")
        attachment = node._store_attachment("Report.pdf", b"data", PDF)
        self.assertFalse(attachment.res_field)

    def test_attachment_is_linked_to_the_node_record(self):
        node = self._node("A")
        attachment = node._store_attachment("Report.pdf", b"data", PDF)
        self.assertEqual(attachment.res_model, "gdrive.node")
        self.assertEqual(attachment.res_id, node.id)
        self.assertEqual(attachment.type, "binary")

    def test_mimetype_is_set_explicitly(self):
        # Left to Odoo's guess, a .xlsx becomes application/zip.
        node = self._node("A", name="Cashflow")
        xlsx = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        attachment = node._store_attachment("Cashflow.xlsx", b"PK\x03\x04", xlsx)
        self.assertEqual(attachment.mimetype, xlsx)

    def test_checksum_is_recorded_for_cross_checking(self):
        node = self._node("A")
        attachment = node._store_attachment("Report.pdf", b"data", PDF)
        self.assertEqual(node.attachment_checksum, attachment.checksum)

    def test_public_flag_is_never_set(self):
        node = self._node("A")
        attachment = node._store_attachment("Report.pdf", b"data", PDF)
        self.assertFalse(attachment.public)

    def test_reingest_creates_a_new_attachment_and_keeps_the_old_one(self):
        node = self._node("A")
        first = node._store_attachment("Report.pdf", b"v1", PDF)
        second = node._store_attachment("Report.pdf", b"v2", PDF)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(node.attachment_id, second)
        self.assertTrue(first.exists(), "history must survive a bad edit in Drive")
        self.assertEqual(first.raw, b"v1")

    def test_filename_gains_a_correct_extension(self):
        node = self._node("A", name="Quarterly Report", mime=PDF)
        self.assertEqual(node._attachment_filename(PDF), "Quarterly Report.pdf")

    def test_existing_extension_is_not_duplicated(self):
        node = self._node("A", name="Quarterly Report.pdf", mime=PDF)
        self.assertEqual(node._attachment_filename(PDF), "Quarterly Report.pdf")


class TestVersionRetention(GDriveNodeCase):
    """Unbounded retention grows the filestore with every metadata touch."""

    def test_old_versions_are_pruned_to_the_cap(self):
        node = self._node("A")
        for i in range(KEEP_VERSIONS + 4):
            node._store_attachment("Report.pdf", b"v%d" % i, PDF)
        attachments = self.env["ir.attachment"].sudo().search([
            ("res_model", "=", "gdrive.node"), ("res_id", "=", node.id)])
        self.assertLessEqual(len(attachments), KEEP_VERSIONS + 1)

    def test_the_current_attachment_is_never_pruned(self):
        node = self._node("A")
        for i in range(KEEP_VERSIONS + 4):
            node._store_attachment("Report.pdf", b"v%d" % i, PDF)
        self.assertTrue(node.attachment_id.exists())
        self.assertEqual(node.attachment_id.raw, b"v%d" % (KEEP_VERSIONS + 3))

    def test_the_text_extract_is_never_pruned(self):
        node = self._node("D", name="Doc", mime=GDOC, node_type="document")
        node._store_attachment("Doc.txt", b"plain", "text/plain", field="text_attachment_id")
        for i in range(KEEP_VERSIONS + 4):
            node._store_attachment("Doc.pdf", b"v%d" % i, PDF)
        self.assertTrue(node.text_attachment_id.exists())


class TestUnchangedBlobSkip(GDriveNodeCase):
    """An unchanged md5 with an attachment means the bytes are already here."""

    def test_current_when_checksum_and_attachment_are_present(self):
        node = self._node("A", md5_checksum="abc123")
        node._store_attachment("Report.pdf", b"data", PDF)
        self.assertTrue(node._blob_is_current())

    def test_not_current_without_an_attachment(self):
        node = self._node("A", md5_checksum="abc123")
        self.assertFalse(node._blob_is_current())

    def test_native_types_are_never_considered_current(self):
        # Native Google types have no md5Checksum at all; treating absent as
        # equal would freeze them at their first export forever.
        node = self._node("S", name="Sheet", mime=GSHEET, node_type="spreadsheet")
        node._store_attachment("Sheet.pdf", b"data", PDF)
        self.assertFalse(node._blob_is_current())


class TestGoneHandling(GDriveNodeCase):
    """SPEC §4.3 — absence is recorded, never acted on."""

    def test_state_and_flags(self):
        node = self._node("A")
        node._mark_gone()
        self.assertEqual(node.state, "gone")
        self.assertFalse(node.active)
        self.assertTrue(node.gone_since)

    def test_the_node_is_not_unlinked(self):
        node = self._node("A")
        node_id = node.id
        node._mark_gone()
        self.assertTrue(self.env["gdrive.node"].browse(node_id).exists())

    def test_the_attachment_is_not_unlinked(self):
        node = self._node("A")
        attachment = node._store_attachment("Report.pdf", b"data", PDF)
        node._mark_gone()
        self.assertTrue(attachment.exists())
        self.assertEqual(attachment.raw, b"data")

    def test_marking_gone_twice_is_idempotent(self):
        node = self._node("A")
        node._mark_gone()
        first_seen = node.gone_since
        node._mark_gone()
        self.assertEqual(node.gone_since, first_seen)

    def test_reappearing_is_possible_because_nothing_was_destroyed(self):
        node = self._node("A")
        node._mark_gone()
        node.sudo().write({"state": "ingested", "active": True, "gone_since": False})
        self.assertTrue(node.active)
        self.assertEqual(node.state, "ingested")


class TestUniqueness(GDriveNodeCase):
    """One Drive file id appears once per connection."""

    def test_duplicate_google_id_is_refused(self):
        from psycopg2 import IntegrityError
        from odoo.tools import mute_logger
        self._node("A")
        self.env.flush_all()
        with self.assertRaises(IntegrityError), mute_logger("odoo.sql_db"):
            with self.cr.savepoint():
                self._node("A")
                self.env.flush_all()
