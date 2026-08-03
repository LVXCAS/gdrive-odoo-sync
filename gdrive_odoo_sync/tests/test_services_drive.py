# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. Licensed under LGPL-3.
"""Lane B — discovery, classification, download and retry policy (SPEC §4.2, §4.4, §4.5).

Fully mocked transport: the ``drive`` object below is a hand-written stub that
records every keyword argument it is handed. No network, no credentials.

WHY the assertions are about *request parameters* rather than about results
==========================================================================
Every failure this file guards against is silent. None of them raises, none of
them logs, and all of them look like "the user has less data than you thought":

* omitting ``supportsAllDrives`` / ``includeItemsFromAllDrives`` excludes every
  shared drive, with HTTP 200 and no warning;
* omitting ``nextPageToken`` from the ``fields`` mask stops pagination after
  page 1, so a 40 000-file Drive is reported as 1 000 files;
* ``drives.list`` silently coerces ``pageSize`` above 100, which looks exactly
  like an organisation that happens to have 100 shared drives;
* ``incompleteSearch: true`` is a **success** response that means rows are
  missing.

Each of those, fed into a delete planner, reads as "these rows were deleted".
That is why the transport-level assertions live here and are this pedantic.
"""

import json
from unittest import mock

from odoo.tests.common import BaseCase

from odoo.addons.gdrive_odoo_sync.services import mimetypes as gmimetypes
from odoo.addons.gdrive_odoo_sync.services import retry as gretry
from odoo.addons.gdrive_odoo_sync.services.drive_discovery import (
    DRIVES_PAGE_SIZE,
    FILES_LIST_FIELDS,
    PAGE_SIZE,
    DriveDiscovery,
)
from odoo.addons.gdrive_odoo_sync.services.drive_download import DriveDownloader
from odoo.addons.gdrive_odoo_sync.services.errors import (
    GDriveExportTooLarge,
    GDriveIncompleteRead,
    GDrivePermanentError,
)
from odoo.addons.gdrive_odoo_sync.services.mimetypes import (
    EXPORT_MAP,
    MIME_DOCUMENT,
    MIME_DRAWING,
    MIME_FOLDER,
    MIME_PDF,
    MIME_PRESENTATION,
    MIME_SHORTCUT,
    MIME_SPREADSHEET,
    MIME_TEXT,
    classify,
    extension_for,
    is_folder,
    is_native,
    is_spreadsheet_blob,
)
from odoo.addons.gdrive_odoo_sync.services.retry import (
    NEVER_RETRY_REASONS,
    RETRY_403_REASONS,
    RETRY_STATUS,
    coerce_page_size,
    execute_with_retry,
    is_retryable,
    retry_after_of,
)

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
XLS = "application/vnd.ms-excel"


# --------------------------------------------------------------------------- #
# Fake transport
# --------------------------------------------------------------------------- #


class FakeResponse(dict):
    """``httplib2``-style response: a dict subclass with a ``.status``."""

    def __init__(self, status=200, headers=None):
        super().__init__(headers or {})
        self.status = status


class FakeHttpError(Exception):
    """Duck-typed stand-in for ``googleapiclient.errors.HttpError``."""

    def __init__(self, status, reason=None, message="boom", headers=None):
        super().__init__("%s %s" % (status, message))
        self.resp = FakeResponse(status, headers)
        payload = {"error": {"code": status, "message": message}}
        if reason:
            payload["error"]["errors"] = [{"reason": reason, "message": message}]
        self.content = json.dumps(payload).encode("utf-8")


class FakeRequest:
    def __init__(self, recorder, name, kwargs, responses):
        self._recorder = recorder
        self._name = name
        self._kwargs = kwargs
        self._responses = responses

    def execute(self, num_retries=0):
        result = self._responses.pop(0) if self._responses else {}
        if isinstance(result, BaseException):
            raise result
        return result


class FakeCollection:
    def __init__(self, recorder, name, script):
        self._recorder = recorder
        self._name = name
        self._script = script  # {method: [responses]}

    def __getattr__(self, method):
        def call(**kwargs):
            self._recorder.append({"collection": self._name, "method": method, "kwargs": kwargs})
            return FakeRequest(
                self._recorder,
                "%s.%s" % (self._name, method),
                kwargs,
                self._script.setdefault((self._name, method), []),
            )

        return call


class FakeDrive:
    """Minimal ``drive`` service stub with a scriptable response queue."""

    def __init__(self):
        self.calls = []
        self.script = {}

    def queue(self, collection, method, *responses):
        self.script.setdefault((collection, method), []).extend(responses)
        return self

    def files(self):
        return FakeCollection(self.calls, "files", self.script)

    def drives(self):
        return FakeCollection(self.calls, "drives", self.script)

    def about(self):
        return FakeCollection(self.calls, "about", self.script)

    def changes(self):
        return FakeCollection(self.calls, "changes", self.script)

    def calls_to(self, collection, method):
        return [c for c in self.calls if c["collection"] == collection and c["method"] == method]


def _file(fid, name="f", mime="application/pdf", **kw):
    rec = {"id": fid, "name": name, "mimeType": mime}
    rec.update(kw)
    return rec


class TestFieldsMask(BaseCase):
    """SPEC §4.2 — the mask is exact, and ``nextPageToken`` is in it."""

    def test_next_page_token_is_requested_explicitly(self):
        # Omit it and pagination stops after page 1 with no error at all.
        self.assertIn("nextPageToken", FILES_LIST_FIELDS)

    def test_incomplete_search_is_requested(self):
        self.assertIn("incompleteSearch", FILES_LIST_FIELDS)

    def test_every_field_the_node_model_needs_is_present(self):
        for field in (
            "id", "name", "mimeType", "parents", "modifiedTime", "createdTime",
            "size", "md5Checksum", "version", "trashed", "driveId",
            "owners", "shortcutDetails", "webViewLink", "capabilities",
            "sharedWithMeTime",
        ):
            with self.subTest(field=field):
                self.assertIn(field, FILES_LIST_FIELDS)

    def test_page_size_constants(self):
        self.assertEqual(PAGE_SIZE, 1000)
        self.assertEqual(DRIVES_PAGE_SIZE, 100)


class TestListFilesParameters(BaseCase):
    """Every parameter whose omission silently hides data."""

    def setUp(self):
        super().setUp()
        self.drive = FakeDrive()

    def test_shared_drive_parameters_are_always_set(self):
        self.drive.queue("files", "list", {"files": [_file("a")]})
        disco = DriveDiscovery(self.drive)
        list(disco.list_files(q="trashed = false"))

        kwargs = self.drive.calls_to("files", "list")[0]["kwargs"]
        self.assertIs(kwargs["supportsAllDrives"], True)
        self.assertIs(kwargs["includeItemsFromAllDrives"], True)
        self.assertEqual(kwargs["spaces"], "drive")
        self.assertEqual(kwargs["fields"], FILES_LIST_FIELDS)

    def test_drive_id_is_only_sent_for_a_drive_corpus(self):
        self.drive.queue("files", "list", {"files": []}, {"files": []})
        disco = DriveDiscovery(self.drive)
        list(disco.list_files(corpora="user"))
        list(disco.list_files(corpora="drive", drive_id="0ABCdef"))

        first, second = self.drive.calls_to("files", "list")
        self.assertNotIn("driveId", first["kwargs"])
        self.assertEqual(second["kwargs"]["driveId"], "0ABCdef")
        self.assertEqual(second["kwargs"]["corpora"], "drive")

    def test_page_size_is_clamped_to_the_api_maximum(self):
        self.drive.queue("files", "list", {"files": []})
        disco = DriveDiscovery(self.drive)
        list(disco.list_files(page_size=100000))
        self.assertEqual(self.drive.calls_to("files", "list")[0]["kwargs"]["pageSize"], PAGE_SIZE)

    def test_files_get_also_carries_supports_all_drives(self):
        # Without it a files.get on a shared-drive item returns 404 even though
        # the subject can read the file.
        self.drive.queue("files", "get", _file("a"))
        DriveDiscovery(self.drive).get_file("a")
        self.assertIs(self.drive.calls_to("files", "get")[0]["kwargs"]["supportsAllDrives"], True)

    def test_about_get_asks_for_the_impersonated_user(self):
        # Probe P3: user.emailAddress != subject_email is the definitive proof
        # that delegation is not in effect.
        self.drive.queue("about", "get", {"user": {"emailAddress": "lucaso@example.com"}})
        DriveDiscovery(self.drive).about()
        fields = self.drive.calls_to("about", "get")[0]["kwargs"]["fields"]
        self.assertIn("emailAddress", fields)
        self.assertIn("storageQuota", fields)


class TestPagination(BaseCase):
    """A ``nextPageToken`` loop that stops early loses data invisibly."""

    def test_all_pages_are_followed(self):
        drive = FakeDrive()
        drive.queue(
            "files", "list",
            {"files": [_file("1"), _file("2")], "nextPageToken": "T1"},
            {"files": [_file("3")], "nextPageToken": "T2"},
            {"files": [_file("4")]},
        )
        disco = DriveDiscovery(drive)
        ids = [f["id"] for f in disco.list_files()]
        self.assertEqual(ids, ["1", "2", "3", "4"])

    def test_page_tokens_are_replayed_in_order(self):
        drive = FakeDrive()
        drive.queue(
            "files", "list",
            {"files": [], "nextPageToken": "T1"},
            {"files": [], "nextPageToken": "T2"},
            {"files": []},
        )
        list(DriveDiscovery(drive).list_files())
        calls = drive.calls_to("files", "list")
        self.assertEqual(len(calls), 3)
        self.assertNotIn("pageToken", calls[0]["kwargs"])
        self.assertEqual(calls[1]["kwargs"]["pageToken"], "T1")
        self.assertEqual(calls[2]["kwargs"]["pageToken"], "T2")

    def test_an_empty_final_page_terminates_cleanly(self):
        drive = FakeDrive()
        drive.queue("files", "list", {"files": [_file("1")], "nextPageToken": "T1"}, {"files": []})
        self.assertEqual([f["id"] for f in DriveDiscovery(drive).list_files()], ["1"])

    def test_missing_files_key_is_treated_as_an_empty_page(self):
        drive = FakeDrive()
        drive.queue("files", "list", {})
        self.assertEqual(list(DriveDiscovery(drive).list_files()), [])


class TestSharedDrives(BaseCase):
    """``drives.list`` silently coerces ``pageSize`` above 100."""

    def test_page_size_is_clamped_before_the_call(self):
        drive = FakeDrive()
        drive.queue("drives", "list", {"drives": [{"id": "0A", "name": "Ops"}]})
        DriveDiscovery(drive).list_shared_drives(page_size=1000)
        self.assertEqual(drive.calls_to("drives", "list")[0]["kwargs"]["pageSize"], DRIVES_PAGE_SIZE)

    def test_fields_mask_includes_next_page_token(self):
        drive = FakeDrive()
        drive.queue("drives", "list", {"drives": []})
        DriveDiscovery(drive).list_shared_drives()
        self.assertIn("nextPageToken", drive.calls_to("drives", "list")[0]["kwargs"]["fields"])

    def test_drives_are_paginated(self):
        drive = FakeDrive()
        drive.queue(
            "drives", "list",
            {"drives": [{"id": "0A"}], "nextPageToken": "D1"},
            {"drives": [{"id": "0B"}]},
        )
        self.assertEqual([d["id"] for d in DriveDiscovery(drive).list_shared_drives()], ["0A", "0B"])

    def test_coerce_page_size_helper(self):
        self.assertEqual(coerce_page_size(1000, 100, 100), 100)
        self.assertEqual(coerce_page_size(50, 100, 100), 50)
        self.assertEqual(coerce_page_size(0, 100, 100), 100)
        self.assertEqual(coerce_page_size("nonsense", 100, 100), 100)


class TestIncompleteSearch(BaseCase):
    """A 200 response that means "rows are missing" must disarm the delete planner."""

    def test_complete_read_starts_true(self):
        self.assertTrue(DriveDiscovery(FakeDrive()).complete_read)

    def test_incomplete_search_flips_complete_read(self):
        drive = FakeDrive()
        drive.queue("files", "list", {"files": [_file("1")], "incompleteSearch": True})
        disco = DriveDiscovery(drive)
        list(disco.list_files(q="trashed = false"))
        self.assertFalse(disco.complete_read)
        self.assertEqual(len(disco.incomplete_reads), 1)

    def test_the_partial_results_are_still_yielded(self):
        # The rows we did get are real; they are just not the whole story.
        drive = FakeDrive()
        drive.queue("files", "list", {"files": [_file("1")], "incompleteSearch": True})
        disco = DriveDiscovery(drive)
        self.assertEqual([f["id"] for f in disco.list_files()], ["1"])

    def test_callback_is_invoked_for_the_run_log(self):
        seen = []
        drive = FakeDrive()
        drive.queue("files", "list", {"files": [], "incompleteSearch": True})
        disco = DriveDiscovery(drive, on_incomplete=seen.append)
        list(disco.list_files())
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["code"], "INCOMPLETE_SEARCH")

    def test_strict_mode_raises(self):
        drive = FakeDrive()
        drive.queue("files", "list", {"files": [], "incompleteSearch": True})
        disco = DriveDiscovery(drive, strict_incomplete=True)
        with self.assertRaises(GDriveIncompleteRead):
            list(disco.list_files())


class TestClassification(BaseCase):
    """SPEC §5.2 — classification is by MIME type, never by file name."""

    def test_native_types(self):
        self.assertEqual(classify(MIME_FOLDER), "folder")
        self.assertEqual(classify(MIME_SPREADSHEET), "spreadsheet")
        self.assertEqual(classify(MIME_DOCUMENT), "document")
        self.assertEqual(classify(MIME_PRESENTATION), "presentation")
        self.assertEqual(classify(MIME_DRAWING), "drawing")

    def test_shortcut_by_mime_or_by_details(self):
        self.assertEqual(classify(MIME_SHORTCUT), "shortcut")
        # Drive has been observed returning the *target's* MIME on the shortcut
        # record; the presence of shortcutDetails alone must still classify it.
        self.assertEqual(
            classify(MIME_SPREADSHEET, {"targetId": "x", "targetMimeType": MIME_SPREADSHEET}),
            "shortcut",
        )

    def test_binary_blobs(self):
        self.assertEqual(classify("application/pdf"), "blob")
        self.assertEqual(classify("image/jpeg"), "blob")
        self.assertEqual(classify(XLSX), "blob")

    def test_unsupported_google_types_are_metadata_only(self):
        for mime in (
            "application/vnd.google-apps.form",
            "application/vnd.google-apps.script",
            "application/vnd.google-apps.site",
            "application/vnd.google-apps.jam",
        ):
            with self.subTest(mime=mime):
                self.assertEqual(classify(mime), "other_google")

    def test_name_is_never_consulted(self):
        # A file called budget.xlsx that Drive reports as PDF is a PDF.
        self.assertEqual(classify("application/pdf"), "blob")
        self.assertFalse(is_spreadsheet_blob("application/pdf"))
        self.assertTrue(is_spreadsheet_blob(XLSX))

    def test_helpers(self):
        self.assertTrue(is_folder(MIME_FOLDER))
        self.assertTrue(is_native(MIME_DOCUMENT))
        self.assertFalse(is_native("application/pdf"))
        self.assertEqual(extension_for(XLSX), ".xlsx")
        self.assertEqual(extension_for(MIME_PDF), ".pdf")

    def test_unknown_mime_still_classifies(self):
        self.assertEqual(classify("application/x-made-up"), "blob")
        self.assertEqual(classify(""), "blob")


class TestExportMap(BaseCase):
    """SPEC §3.4 — native Sheets are deliberately absent from the export map."""

    def test_native_sheets_have_no_export_target(self):
        # Exporting a multi-tab Sheet to CSV silently returns only the FIRST TAB.
        # There is no safe export, so there is no entry.
        self.assertNotIn("spreadsheet", EXPORT_MAP)

    def test_docs_export_to_pdf_and_plain_text(self):
        self.assertEqual(EXPORT_MAP["document"]["primary"], MIME_PDF)
        self.assertEqual(EXPORT_MAP["document"]["secondary"], MIME_TEXT)

    def test_slides_and_drawings_export_to_pdf_only(self):
        self.assertEqual(EXPORT_MAP["presentation"]["primary"], MIME_PDF)
        self.assertIsNone(EXPORT_MAP["presentation"]["secondary"])
        self.assertEqual(EXPORT_MAP["drawing"]["primary"], MIME_PDF)
        self.assertIsNone(EXPORT_MAP["drawing"]["secondary"])

    def test_map_covers_exactly_the_three_exportable_types(self):
        self.assertEqual(set(EXPORT_MAP), {"document", "presentation", "drawing"})


class TestDownloadBranching(BaseCase):
    """SPEC §4.5 — there is no unified download call; branch on the MIME prefix."""

    def test_native_document_uses_export_media(self):
        drive = FakeDrive()
        drive.queue("files", "export_media", b"%PDF-1.4 fake")
        data, mime = DriveDownloader(drive).fetch("doc1", MIME_DOCUMENT)
        self.assertEqual(data, b"%PDF-1.4 fake")
        self.assertEqual(mime, MIME_PDF)
        call = drive.calls_to("files", "export_media")[0]["kwargs"]
        self.assertEqual(call["fileId"], "doc1")
        self.assertEqual(call["mimeType"], MIME_PDF)

    def test_export_media_does_not_accept_supports_all_drives(self):
        # Adding it returns 400. This is not an oversight to be "fixed".
        drive = FakeDrive()
        drive.queue("files", "export_media", b"x")
        DriveDownloader(drive).fetch("doc1", MIME_DOCUMENT)
        self.assertNotIn("supportsAllDrives", drive.calls_to("files", "export_media")[0]["kwargs"])

    def test_native_sheet_is_refused_structurally(self):
        drive = FakeDrive()
        with self.assertRaises(GDrivePermanentError) as ctx:
            DriveDownloader(drive).fetch("sheet1", MIME_SPREADSHEET)
        self.assertEqual(ctx.exception.code, "unsupported_mime")
        self.assertEqual(drive.calls_to("files", "export_media"), [])

    def test_legacy_xls_is_refused_before_any_request(self):
        drive = FakeDrive()
        with self.assertRaises(GDrivePermanentError) as ctx:
            DriveDownloader(drive).fetch("old", XLS)
        self.assertEqual(ctx.exception.code, "unsupported_mime")
        self.assertEqual(drive.calls, [])

    def test_folder_and_shortcut_are_refused_with_skip_reason_codes(self):
        downloader = DriveDownloader(FakeDrive())
        with self.assertRaises(GDrivePermanentError) as ctx:
            downloader.fetch("f1", MIME_FOLDER)
        self.assertEqual(ctx.exception.code, "folder")
        with self.assertRaises(GDrivePermanentError) as ctx:
            downloader.fetch("s1", MIME_SHORTCUT)
        self.assertEqual(ctx.exception.code, "shortcut")

    def test_download_restricted_file_is_refused(self):
        downloader = DriveDownloader(FakeDrive())
        with self.assertRaises(GDrivePermanentError) as ctx:
            downloader.fetch("p1", "application/pdf", can_download=False)
        self.assertEqual(ctx.exception.code, "no_download_permission")

    def test_oversize_blob_is_refused_before_transfer(self):
        drive = FakeDrive()
        downloader = DriveDownloader(drive, max_blob_bytes=1024)
        with self.assertRaises(GDrivePermanentError) as ctx:
            downloader.fetch("big", "application/pdf", size_bytes=2048)
        self.assertEqual(ctx.exception.code, "too_large")
        self.assertEqual(drive.calls, [])

    def test_export_size_limit_is_permanent_and_not_retried(self):
        drive = FakeDrive()
        drive.queue(
            "files", "export_media",
            FakeHttpError(403, "exportSizeLimitExceeded", "This file is too large to be exported."),
        )
        with self.assertRaises(GDriveExportTooLarge):
            DriveDownloader(drive).fetch("huge_doc", MIME_DOCUMENT)
        # Exactly one attempt: chunked download does not help, the ceiling is on
        # the generated artefact, so retrying only burns quota.
        self.assertEqual(len(drive.calls_to("files", "export_media")), 1)

    def test_secondary_export_for_docs_only(self):
        drive = FakeDrive()
        drive.queue("files", "export_media", b"plain text")
        result = DriveDownloader(drive).fetch_secondary("doc1", MIME_DOCUMENT)
        self.assertEqual(result, (b"plain text", MIME_TEXT))
        self.assertIsNone(DriveDownloader(FakeDrive()).fetch_secondary("s1", MIME_PRESENTATION))


class TestUnchangedBlobSkip(BaseCase):
    """SPEC §4.5 — an unchanged md5 with an existing attachment skips the download."""

    def test_same_checksum_and_existing_attachment_skips(self):
        downloader = DriveDownloader(FakeDrive())
        self.assertTrue(downloader.should_skip_unchanged("abc123", "abc123", True))

    def test_changed_checksum_downloads(self):
        downloader = DriveDownloader(FakeDrive())
        self.assertFalse(downloader.should_skip_unchanged("abc123", "def456", True))

    def test_no_attachment_downloads_even_if_checksum_matches(self):
        # The checksum says the bytes are the same; it says nothing about
        # whether we ever actually stored them.
        downloader = DriveDownloader(FakeDrive())
        self.assertFalse(downloader.should_skip_unchanged("abc123", "abc123", False))

    def test_absent_checksum_never_skips(self):
        # Native Google types have no md5Checksum at all; treating absent as
        # equal would freeze them forever at their first ingest.
        downloader = DriveDownloader(FakeDrive())
        self.assertFalse(downloader.should_skip_unchanged("", "", True))
        self.assertFalse(downloader.should_skip_unchanged(None, None, True))


class TestRetryPolicy(BaseCase):
    """SPEC §4.4 — ``execute(num_retries=N)`` is not sufficient; this policy is."""

    def test_403_rate_limited_is_retryable(self):
        for reason in RETRY_403_REASONS:
            with self.subTest(reason=reason):
                self.assertTrue(is_retryable(FakeHttpError(403, reason)))

    def test_403_permission_problems_are_not_retryable(self):
        for reason in ("insufficientPermissions", "appNotAuthorizedToFile",
                       "cannotDownloadAbusiveFile", "exportSizeLimitExceeded",
                       "dailyLimitExceeded"):
            with self.subTest(reason=reason):
                self.assertIn(reason, NEVER_RETRY_REASONS)
                self.assertFalse(is_retryable(FakeHttpError(403, reason)))

    def test_server_errors_are_retryable(self):
        for status in RETRY_STATUS:
            with self.subTest(status=status):
                self.assertTrue(is_retryable(FakeHttpError(status)))

    def test_client_errors_are_not_retryable(self):
        # Retrying a 400 is how one malformed A1 range becomes eight identical
        # malformed A1 ranges.
        for status in (400, 401, 404):
            with self.subTest(status=status):
                self.assertFalse(is_retryable(FakeHttpError(status)))

    def test_retry_then_succeed(self):
        drive = FakeDrive()
        drive.queue(
            "files", "list",
            FakeHttpError(403, "rateLimitExceeded"),
            FakeHttpError(429),
            {"files": [_file("1")]},
        )
        disco = DriveDiscovery(drive)
        # Patch the delay computation rather than time.sleep: the `sleep`
        # parameter's default is bound at definition time, so patching
        # time.sleep would not reach it.
        with mock.patch.object(gretry, "_sleep_for", return_value=0.0) as delay:
            files = list(disco.list_files())
        self.assertEqual([f["id"] for f in files], ["1"])
        self.assertEqual(len(drive.calls_to("files", "list")), 3)
        self.assertEqual(delay.call_count, 2)

    def test_permanent_error_is_raised_after_one_attempt(self):
        drive = FakeDrive()
        drive.queue("files", "list", FakeHttpError(403, "insufficientPermissions"))
        disco = DriveDiscovery(drive)
        with self.assertRaises(Exception):
            list(disco.list_files())
        self.assertEqual(len(drive.calls_to("files", "list")), 1)

    def test_attempts_are_bounded(self):
        drive = FakeDrive()
        drive.queue("files", "list", *[FakeHttpError(503) for _ in range(20)])
        with mock.patch.object(gretry, "_sleep_for", return_value=0.0):
            with self.assertRaises(Exception):
                list(DriveDiscovery(drive).list_files())
        self.assertLessEqual(len(drive.calls_to("files", "list")), 8)

    def test_retry_after_header_is_honoured(self):
        exc = FakeHttpError(429, headers={"retry-after": "12"})
        self.assertEqual(retry_after_of(exc), 12.0)

    def test_retry_after_http_date_falls_back_to_computed_backoff(self):
        # Mis-parsing an HTTP-date could sleep for years; refusing to parse it is
        # the safe failure.
        exc = FakeHttpError(429, headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"})
        self.assertIsNone(retry_after_of(exc))

    def test_backoff_is_capped(self):
        sleeps = []
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 6:
                raise FakeHttpError(503)
            return {"ok": True}

        result = execute_with_retry(
            flaky, max_attempts=8, sleep=sleeps.append, label="unit-test"
        )
        self.assertEqual(result, {"ok": True})
        self.assertTrue(sleeps)
        self.assertTrue(all(s <= 64.0 for s in sleeps), sleeps)

    def test_nothing_is_swallowed(self):
        # A retry wrapper that returns None on exhaustion turns an outage into
        # "the sheet is empty", which is the delete planner's worst input.
        def always_fails():
            raise FakeHttpError(500)

        with self.assertRaises(Exception):
            execute_with_retry(always_fails, max_attempts=2, sleep=lambda s: None)


class TestQuotaAccounting(BaseCase):
    """The run log records what was actually spent, not an estimate."""

    def test_requests_and_units_are_counted(self):
        drive = FakeDrive()
        drive.queue("files", "list", {"files": [_file("1")], "nextPageToken": "T"}, {"files": []})
        disco = DriveDiscovery(drive)
        list(disco.list_files())
        stats = disco.stats()
        self.assertEqual(stats["requests_made"], 2)
        self.assertGreater(stats["drive_units_used"], 0)
        self.assertEqual(stats["files_seen"], 1)
        self.assertTrue(stats["complete_read"])


class TestMimeModuleShadowing(BaseCase):
    """``services/mimetypes.py`` shadows a stdlib name; prove we got ours."""

    def test_the_imported_module_is_the_package_local_one(self):
        self.assertTrue(hasattr(gmimetypes, "EXPORT_MAP"))
        self.assertTrue(gmimetypes.__name__.endswith("gdrive_odoo_sync.services.mimetypes"))
