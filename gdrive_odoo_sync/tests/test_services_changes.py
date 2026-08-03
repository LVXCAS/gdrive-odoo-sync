# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. Licensed under LGPL-3.
"""Lane B — the Drive Changes API (SPEC §4.3).

WHY the token rules get their own test module
=============================================
Incremental sync has exactly two moving parts and both are easy to get subtly,
permanently wrong:

* **``nextPageToken`` vs ``newStartPageToken``.** The first means "more pages in
  *this* poll"; the second is the cursor for the *next* poll and appears **only
  on the final page**. Conflating them advances the cursor past unread pages,
  and the changes in between are gone forever — no error, no retry, no way to
  notice except by comparing against a full re-enumeration weeks later.
* **Commit order.** The mirrored data must be committed *before* the token is
  persisted. Saving the token first means a crash between the two loses every
  change in that window permanently. This module proves the API hands the token
  out only on the last record of the last page, which is what makes the correct
  ordering the natural one to write.

Plus one hard rule: a token minted with ``driveId=X`` must always be replayed
with ``driveId=X``. Tokens are per ``(subject_email, drive_id)`` and are not
interchangeable; replaying one against the wrong corpus does not error, it
returns the wrong changes.
"""

from odoo.tests.common import BaseCase

from odoo.addons.gdrive_odoo_sync.services.drive_changes import (
    CHANGES_FIELDS,
    CHANGES_PAGE_SIZE,
    DriveChanges,
    cursor_key,
)
from odoo.addons.gdrive_odoo_sync.services.errors import (
    GDriveIncompleteRead,
    GDriveTokenInvalid,
)

from .test_services_drive import FakeDrive, FakeHttpError


def _change(file_id, removed=False, drive_id=None, mime="application/pdf"):
    change = {"changeType": "file", "fileId": file_id, "removed": removed,
              "time": "2026-07-28T10:00:00.000Z"}
    if not removed:
        change["file"] = {"id": file_id, "name": file_id, "mimeType": mime}
    if drive_id:
        change["driveId"] = drive_id
    return change


class TestFieldsMask(BaseCase):
    """Both tokens must be inside the mask or neither is ever returned."""

    def test_both_tokens_are_requested(self):
        self.assertIn("nextPageToken", CHANGES_FIELDS)
        self.assertIn("newStartPageToken", CHANGES_FIELDS)

    def test_change_envelope_fields_are_requested(self):
        for field in ("changeType", "time", "removed", "fileId", "driveId", "file"):
            with self.subTest(field=field):
                self.assertIn(field, CHANGES_FIELDS)


class TestBootstrap(BaseCase):
    """``getStartPageToken`` — once per ``(subject, drive)``, and never silently empty."""

    def test_user_corpus_token(self):
        drive = FakeDrive()
        drive.queue("changes", "getStartPageToken", {"startPageToken": "100"})
        self.assertEqual(DriveChanges(drive).get_start_page_token(), "100")
        kwargs = drive.calls_to("changes", "getStartPageToken")[0]["kwargs"]
        self.assertIs(kwargs["supportsAllDrives"], True)
        self.assertNotIn("driveId", kwargs)

    def test_per_drive_token_carries_its_drive_id(self):
        drive = FakeDrive()
        drive.queue("changes", "getStartPageToken", {"startPageToken": "200"})
        DriveChanges(drive).get_start_page_token(drive_id="0ABCdef")
        self.assertEqual(
            drive.calls_to("changes", "getStartPageToken")[0]["kwargs"]["driveId"], "0ABCdef"
        )

    def test_missing_token_is_refused_rather_than_stored_empty(self):
        # A cursor bootstrapped to '' would silently skip every change until the
        # next full re-enumeration.
        drive = FakeDrive()
        drive.queue("changes", "getStartPageToken", {})
        with self.assertRaises(GDriveIncompleteRead):
            DriveChanges(drive).get_start_page_token()


class TestListParameters(BaseCase):
    """Every parameter whose omission hides changes rather than erroring."""

    def setUp(self):
        super().setUp()
        self.drive = FakeDrive()

    def test_shared_drive_and_scope_parameters(self):
        self.drive.queue("changes", "list", {"changes": [], "newStartPageToken": "101"})
        DriveChanges(self.drive).poll("100")
        kwargs = self.drive.calls_to("changes", "list")[0]["kwargs"]
        self.assertIs(kwargs["supportsAllDrives"], True)
        self.assertIs(kwargs["includeItemsFromAllDrives"], True)
        # Defaulting restrictToMyDrive to True builds a mirror that never notices
        # anything a colleague edits.
        self.assertIs(kwargs["restrictToMyDrive"], False)
        self.assertEqual(kwargs["spaces"], "drive")
        self.assertEqual(kwargs["fields"], CHANGES_FIELDS)

    def test_removals_are_requested(self):
        self.drive.queue("changes", "list", {"changes": [], "newStartPageToken": "101"})
        DriveChanges(self.drive).poll("100")
        kwargs = self.drive.calls_to("changes", "list")[0]["kwargs"]
        self.assertIs(kwargs["includeRemoved"], True)
        self.assertIs(kwargs["includeCorpusRemovals"], True)

    def test_drive_scoped_token_is_replayed_with_its_drive_id(self):
        self.drive.queue("changes", "list", {"changes": [], "newStartPageToken": "201"})
        DriveChanges(self.drive).poll("200", drive_id="0ABCdef")
        self.assertEqual(
            self.drive.calls_to("changes", "list")[0]["kwargs"]["driveId"], "0ABCdef"
        )

    def test_user_corpus_token_is_replayed_without_a_drive_id(self):
        self.drive.queue("changes", "list", {"changes": [], "newStartPageToken": "101"})
        DriveChanges(self.drive).poll("100")
        self.assertNotIn("driveId", self.drive.calls_to("changes", "list")[0]["kwargs"])

    def test_page_size_is_clamped(self):
        self.drive.queue("changes", "list", {"changes": [], "newStartPageToken": "101"})
        DriveChanges(self.drive).poll("100", page_size=999999)
        self.assertEqual(
            self.drive.calls_to("changes", "list")[0]["kwargs"]["pageSize"], CHANGES_PAGE_SIZE
        )


class TestTokenSemantics(BaseCase):
    """``newStartPageToken`` comes from the final page and from nowhere else."""

    def test_multi_page_poll_returns_only_the_final_token(self):
        drive = FakeDrive()
        drive.queue(
            "changes", "list",
            {"changes": [_change("a")], "nextPageToken": "P2"},
            {"changes": [_change("b")], "nextPageToken": "P3"},
            {"changes": [_change("c")], "newStartPageToken": "999"},
        )
        changes, token = DriveChanges(drive).poll("100")
        self.assertEqual([c["fileId"] for c in changes], ["a", "b", "c"])
        self.assertEqual(token, "999")

    def test_intermediate_pages_yield_no_token(self):
        drive = FakeDrive()
        drive.queue(
            "changes", "list",
            {"changes": [_change("a"), _change("b")], "nextPageToken": "P2"},
            {"changes": [_change("c")], "newStartPageToken": "999"},
        )
        pairs = list(DriveChanges(drive).iter_changes("100"))
        tokens = [tok for _change_, tok in pairs]
        self.assertEqual(tokens, [None, None, "999"])

    def test_the_token_rides_on_the_last_record_of_the_last_page(self):
        # This is what makes "commit data, then persist the cursor" the natural
        # thing to write: the token simply is not available any earlier.
        drive = FakeDrive()
        drive.queue("changes", "list", {"changes": [_change("a"), _change("b")],
                                        "newStartPageToken": "999"})
        pairs = list(DriveChanges(drive).iter_changes("100"))
        self.assertEqual(pairs[0][1], None)
        self.assertEqual(pairs[-1][1], "999")
        self.assertEqual(pairs[-1][0]["fileId"], "b")

    def test_a_mid_stream_token_is_ignored(self):
        # Google does not do this; if it ever did, trusting it would advance the
        # cursor past unread pages.
        drive = FakeDrive()
        drive.queue(
            "changes", "list",
            {"changes": [_change("a")], "nextPageToken": "P2", "newStartPageToken": "BOGUS"},
            {"changes": [_change("b")], "newStartPageToken": "999"},
        )
        _changes, token = DriveChanges(drive).poll("100")
        self.assertEqual(token, "999")

    def test_empty_poll_still_advances_the_cursor(self):
        # Otherwise every poll re-reads the same window forever.
        drive = FakeDrive()
        drive.queue("changes", "list", {"changes": [], "newStartPageToken": "101"})
        changes, token = DriveChanges(drive).poll("100")
        self.assertEqual(changes, [])
        self.assertEqual(token, "101")

    def test_final_page_without_a_token_is_declared_incomplete(self):
        # Persisting nothing means endless replay; persisting a guess means
        # permanent data loss. Declaring the read incomplete is the only safe
        # third option.
        drive = FakeDrive()
        drive.queue("changes", "list", {"changes": [_change("a")]})
        with self.assertRaises(GDriveIncompleteRead):
            DriveChanges(drive).poll("100")

    def test_repeating_page_token_is_refused(self):
        drive = FakeDrive()
        drive.queue(
            "changes", "list",
            {"changes": [_change("a")], "nextPageToken": "100"},
        )
        with self.assertRaises(GDriveIncompleteRead):
            DriveChanges(drive).poll("100")

    def test_runaway_backlog_hits_the_page_safety_limit(self):
        drive = FakeDrive()
        drive.queue(
            "changes", "list",
            {"changes": [_change("a")], "nextPageToken": "P2"},
            {"changes": [_change("b")], "nextPageToken": "P3"},
            {"changes": [_change("c")], "nextPageToken": "P4"},
        )
        with self.assertRaises(GDriveIncompleteRead):
            DriveChanges(drive).poll("100", max_pages=2)


class TestDeadToken(BaseCase):
    """A dead cursor must force a full re-enumeration, not a silent empty poll."""

    def test_404_becomes_a_token_invalid_error(self):
        drive = FakeDrive()
        drive.queue("changes", "list", FakeHttpError(404, "notFound", "Page token not found"))
        with self.assertRaises(GDriveTokenInvalid):
            DriveChanges(drive).poll("dead-token")

    def test_400_invalid_value_becomes_a_token_invalid_error(self):
        drive = FakeDrive()
        drive.queue("changes", "list", FakeHttpError(400, "invalid", "Invalid Value"))
        with self.assertRaises(GDriveTokenInvalid):
            DriveChanges(drive).poll("dead-token")

    def test_a_dead_token_is_not_retried(self):
        # Retrying a dead token is eight identical failures and a wasted minute.
        drive = FakeDrive()
        drive.queue("changes", "list", FakeHttpError(404, "notFound", "Page token not found"))
        with self.assertRaises(GDriveTokenInvalid):
            DriveChanges(drive).poll("dead-token")
        self.assertEqual(len(drive.calls_to("changes", "list")), 1)


class TestChangeInterpretation(BaseCase):
    """``removed=True`` means "left the subject's view", not "deleted"."""

    def test_removed_flag(self):
        self.assertTrue(DriveChanges.is_removal(_change("a", removed=True)))
        self.assertFalse(DriveChanges.is_removal(_change("a")))

    def test_trashed_file_counts_as_a_removal(self):
        change = _change("a")
        change["file"]["trashed"] = True
        self.assertTrue(DriveChanges.is_removal(change))

    def test_file_id_extraction(self):
        self.assertEqual(DriveChanges.file_id_of(_change("abc")), "abc")
        self.assertEqual(DriveChanges.file_id_of({"file": {"id": "xyz"}}), "xyz")
        self.assertEqual(DriveChanges.file_id_of({}), "")

    def test_removal_is_never_a_business_record_delete_signal(self):
        # Documented here because the type system cannot enforce it: a removal
        # sets node state 'gone' and never reaches the promotion delete planner.
        # Permission revocation, a move out of scope and an actual delete are
        # indistinguishable at this layer.
        change = _change("a", removed=True)
        self.assertTrue(DriveChanges.is_removal(change))
        self.assertNotIn("file", change)


class TestCursorKey(BaseCase):
    """Tokens are per ``(subject_email, drive_id)`` and are never interchangeable."""

    def test_user_corpus_and_shared_drive_keys_differ(self):
        a = cursor_key("lucaso@avatarnaturalfoods.com", None)
        b = cursor_key("lucaso@avatarnaturalfoods.com", "0ABCdef")
        self.assertNotEqual(a, b)

    def test_different_subjects_never_share_a_cursor(self):
        a = cursor_key("lucaso@avatarnaturalfoods.com", "0ABCdef")
        b = cursor_key("michael@avatarnaturalfoods.com", "0ABCdef")
        self.assertNotEqual(a, b)

    def test_empty_and_none_drive_id_are_the_same_corpus(self):
        self.assertEqual(
            cursor_key("lucaso@avatarnaturalfoods.com", None),
            cursor_key("lucaso@avatarnaturalfoods.com", ""),
        )


class TestPollStats(BaseCase):
    """Quota accounting is per poll, for the run log."""

    def test_requests_are_counted_across_pages(self):
        drive = FakeDrive()
        drive.queue(
            "changes", "list",
            {"changes": [_change("a")], "nextPageToken": "P2"},
            {"changes": [_change("b")], "newStartPageToken": "999"},
        )
        changes_api = DriveChanges(drive)
        changes_api.poll("100")
        stats = changes_api.stats()
        self.assertEqual(stats["requests_made"], 2)
        self.assertEqual(stats["changes_seen"], 2)
