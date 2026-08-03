# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. Licensed under LGPL-3.
"""Lane C — dates and datetimes (``docs/CANONICALIZATION.md`` §6).

WHY these cases
===============
Three failure modes dominate spreadsheet date handling, and all three are
silent:

* **Serial truncation.** ``45000.5`` arrives over the wire as
  ``45000.499999999996`` because the API returns a double. Truncating the
  fraction gives ``11:59:59`` instead of noon — an off-by-one-second that only
  shows up in a hash comparison, never on screen.
* **Timezoning a pure date.** Pushing a date through a timezone shifts it by a
  day, and characteristically only at night, when the cron runs and UTC has
  already rolled over. The symptom is a full-column false drift that
  disappears if a human re-runs it in the morning.
* **Fuzzy parsing.** ``"03/04/2026"`` is genuinely unresolvable. A fuzzy parser
  picks one reading silently and corrupts a year of data. Only strict
  ``strptime`` against the declared format list is permitted, and this module
  proves the same string reads differently under two different contracts.

The DST cases exist because a nonexistent local time is a *real* data defect
(the user wrote a wall-clock reading that never happened) and silently shifting
it invents an instant.
"""

from datetime import date, datetime, time
from decimal import Decimal

from odoo.tests.common import BaseCase

from odoo.addons.gdrive_odoo_sync.lib.contract import ColumnContract
from odoo.addons.gdrive_odoo_sync.lib.datetime_canon import (
    DATE_CANON,
    DATETIME_CANON,
    SERIAL_EPOCH,
    naive_to_serial,
    parse_strict,
    serial_to_naive,
)

NY = "America/New_York"


def _date_col(**kw):
    kw.setdefault("ctype", "date")
    kw.setdefault("key", "due")
    return ColumnContract(**kw)


def _dt_col(**kw):
    kw.setdefault("ctype", "datetime")
    kw.setdefault("key", "signed_at")
    kw.setdefault("sheet_timezone", NY)
    kw.setdefault("datetime_formats", ("%Y-%m-%d %H:%M:%S",))
    return ColumnContract(**kw)


class TestSerialConversion(BaseCase):
    """§6.1 — days since 1899-12-30, fraction rounded to whole seconds."""

    def test_epoch_is_1899_12_30(self):
        self.assertEqual(SERIAL_EPOCH, date(1899, 12, 30))
        self.assertEqual(serial_to_naive(0), datetime(1899, 12, 30, 0, 0, 0))

    def test_known_serial(self):
        self.assertEqual(serial_to_naive(45000), datetime(2023, 3, 15, 0, 0, 0))

    def test_float_noise_rounds_rather_than_truncates(self):
        # The whole reason `round` is specified instead of `int`: truncation
        # yields 11:59:59 and a permanent one-second phantom drift.
        self.assertEqual(serial_to_naive(45000.499999999996), datetime(2023, 3, 15, 12, 0, 0))
        self.assertEqual(serial_to_naive(45000.5), datetime(2023, 3, 15, 12, 0, 0))

    def test_86400_rollover_advances_the_day(self):
        # 23:59:59.6 rounds up to a whole day; leaving seconds at 86400 would
        # produce an invalid time-of-day.
        self.assertEqual(serial_to_naive(45000.9999999), datetime(2023, 3, 16, 0, 0, 0))

    def test_decimal_input_is_accepted_exactly(self):
        self.assertEqual(serial_to_naive(Decimal("45000.25")), datetime(2023, 3, 15, 6, 0, 0))

    def test_second_precision_is_the_contract(self):
        # One microsecond is 1.157e-11 of a day, below double precision at
        # serial magnitudes near 45 000. Pretending otherwise manufactures
        # differences out of float noise.
        self.assertEqual(serial_to_naive(45000.5).microsecond, 0)

    def test_round_trip(self):
        dt = datetime(2023, 3, 15, 12, 0, 0)
        self.assertEqual(serial_to_naive(naive_to_serial(dt)), dt)

    def test_non_numeric_serial_is_a_contract_error_not_a_data_error(self):
        with self.assertRaises(TypeError):
            serial_to_naive("45000")


class TestDateCanon(BaseCase):
    """§6.2 — dates never see a timezone, on either side."""

    def test_odoo_date_object_passes_straight_through(self):
        self.assertEqual(DATE_CANON(date(2026, 7, 31), _date_col(), side="odoo"), "d:2026-07-31")

    def test_sheet_serial_becomes_a_date(self):
        self.assertEqual(DATE_CANON(45000, _date_col()), "d:2023-03-15")

    def test_time_component_is_dropped_and_reported(self):
        sink = []
        token = DATE_CANON(45000.5, _date_col(), warnings=sink)
        self.assertEqual(token, "d:2023-03-15")
        self.assertTrue(sink, "a time component on a date column must be reported")

    def test_midnight_produces_no_warning(self):
        sink = []
        DATE_CANON(45000, _date_col(), warnings=sink)
        self.assertEqual(sink, [])

    def test_datetime_at_23_00_still_yields_its_own_calendar_day(self):
        # The classic off-by-one-day bug: converting this through UTC would
        # roll it to the next day, and only when the cron runs at night.
        token = DATE_CANON(datetime(2026, 7, 31, 23, 0, 0), _date_col(), side="odoo")
        self.assertEqual(token, "d:2026-07-31")

    def test_empty_forms(self):
        for raw in (None, False, "", "   "):
            with self.subTest(raw=repr(raw)):
                self.assertEqual(DATE_CANON(raw, _date_col()), "z:")

    def test_boolean_is_refused_rather_than_read_as_serial_zero(self):
        # bool is an int subclass; reading True as serial 1 would silently
        # produce 1899-12-31.
        self.assertEqual(DATE_CANON(True, _date_col()), "e:BAD_DATE")


class TestStrictParsing(BaseCase):
    """§6.2 step 4 — strict ``strptime`` against the declared list, in order."""

    def test_iso_parses_under_the_default_contract(self):
        self.assertEqual(DATE_CANON("2026-07-31", _date_col()), "d:2026-07-31")

    def test_unlisted_format_is_refused_not_guessed(self):
        col = _date_col(date_formats=("%Y-%m-%d",))
        self.assertEqual(DATE_CANON("03/04/2026", col), "e:BAD_DATE")

    def test_the_same_ambiguous_string_reads_differently_under_two_contracts(self):
        # This is precisely why no parser may choose for the user.
        us = _date_col(date_formats=("%m/%d/%Y",))
        eu = _date_col(date_formats=("%d/%m/%Y",))
        self.assertEqual(DATE_CANON("03/04/2026", us), "d:2026-03-04")
        self.assertEqual(DATE_CANON("03/04/2026", eu), "d:2026-04-03")

    def test_first_listed_format_wins(self):
        col = _date_col(date_formats=("%m/%d/%Y", "%d/%m/%Y"))
        self.assertEqual(DATE_CANON("03/04/2026", col), "d:2026-03-04")

    def test_parse_strict_returns_none_rather_than_raising(self):
        self.assertIsNone(parse_strict("not a date", ("%Y-%m-%d",)))
        self.assertEqual(parse_strict("2026-07-31", ("%Y-%m-%d",)), datetime(2026, 7, 31))

    def test_surrounding_whitespace_and_nbsp_are_tolerated(self):
        self.assertEqual(DATE_CANON("  2026-07-31 ", _date_col()), "d:2026-07-31")

    def test_garbage_is_an_error_token_not_an_exception(self):
        self.assertEqual(DATE_CANON("TBD", _date_col()), "e:BAD_DATE")


class TestDatetimeCanonOdooSide(BaseCase):
    """§6.3 step 2 — Odoo stores UTC-naive; never apply a display timezone."""

    def test_utc_naive_is_emitted_verbatim(self):
        token = DATETIME_CANON(datetime(2026, 7, 31, 14, 5, 0), _dt_col(), side="odoo")
        self.assertEqual(token, "t:2026-07-31T14:05:00Z")

    def test_sheet_timezone_is_irrelevant_on_the_odoo_side(self):
        a = DATETIME_CANON(datetime(2026, 7, 31, 14, 5), _dt_col(), NY, side="odoo")
        b = DATETIME_CANON(datetime(2026, 7, 31, 14, 5), _dt_col(), "Asia/Tokyo", side="odoo")
        self.assertEqual(a, b)

    def test_always_second_precision(self):
        token = DATETIME_CANON(datetime(2026, 7, 31, 14, 5, 9, 123456), _dt_col(), side="odoo")
        self.assertEqual(token, "t:2026-07-31T14:05:09Z")

    def test_empty(self):
        self.assertEqual(DATETIME_CANON(False, _dt_col(), side="odoo"), "z:")


class TestDatetimeCanonSheetSide(BaseCase):
    """§6.3 step 3 — a spreadsheet has no timezone, so one must be declared."""

    def test_naive_local_time_is_converted_to_utc(self):
        # 2026-07-31 is EDT (UTC-4) in New York.
        token = DATETIME_CANON(datetime(2026, 7, 31, 10, 0, 0), _dt_col())
        self.assertEqual(token, "t:2026-07-31T14:00:00Z")

    def test_winter_offset_differs_from_summer_offset(self):
        summer = DATETIME_CANON(datetime(2026, 7, 31, 10, 0, 0), _dt_col())
        winter = DATETIME_CANON(datetime(2026, 1, 31, 10, 0, 0), _dt_col())
        self.assertEqual(summer, "t:2026-07-31T14:00:00Z")
        self.assertEqual(winter, "t:2026-01-31T15:00:00Z")  # EST, UTC-5

    def test_string_input_uses_the_declared_datetime_formats(self):
        token = DATETIME_CANON("2026-07-31 10:00:00", _dt_col())
        self.assertEqual(token, "t:2026-07-31T14:00:00Z")

    def test_unparseable_string_is_an_error_token(self):
        self.assertEqual(DATETIME_CANON("31 July 2026, tea time", _dt_col()), "e:BAD_DATE")

    def test_missing_timezone_is_a_contract_error_not_a_guess(self):
        col = ColumnContract(ctype="datetime", key="signed_at", sheet_timezone="")
        with self.assertRaises(ValueError):
            DATETIME_CANON(datetime(2026, 7, 31, 10, 0), col)

    def test_unknown_timezone_name_is_a_contract_error(self):
        col = _dt_col(sheet_timezone="Mars/Olympus_Mons")
        with self.assertRaises(ValueError):
            DATETIME_CANON(datetime(2026, 7, 31, 10, 0), col)


class TestDaylightSaving(BaseCase):
    """The two hours a year that break naive localization."""

    def test_spring_forward_gap_is_refused(self):
        # 2026-03-08 02:00 -> 03:00 in New York: 02:30 never happened.
        token = DATETIME_CANON(datetime(2026, 3, 8, 2, 30, 0), _dt_col())
        self.assertEqual(token, "e:NONEXISTENT_LOCAL_TIME")

    def test_time_either_side_of_the_gap_is_fine(self):
        before = DATETIME_CANON(datetime(2026, 3, 8, 1, 30, 0), _dt_col())
        after = DATETIME_CANON(datetime(2026, 3, 8, 3, 30, 0), _dt_col())
        self.assertEqual(before, "t:2026-03-08T06:30:00Z")  # EST, UTC-5
        self.assertEqual(after, "t:2026-03-08T07:30:00Z")  # EDT, UTC-4

    def test_fall_back_ambiguity_resolves_to_the_first_occurrence(self):
        # 2026-11-01 01:30 happens twice. fold=0 selects the pre-transition
        # (EDT, UTC-4) reading. Deterministic by fiat is the requirement; which
        # of the two is chosen matters far less than choosing the same one every
        # single run.
        token = DATETIME_CANON(datetime(2026, 11, 1, 1, 30, 0), _dt_col())
        self.assertEqual(token, "t:2026-11-01T05:30:00Z")

    def test_fall_back_ambiguity_is_reported(self):
        sink = []
        DATETIME_CANON(datetime(2026, 11, 1, 1, 30, 0), _dt_col(), warnings=sink)
        self.assertTrue(sink, "an ambiguous local time must be visible to lane E")

    def test_determinism_across_repeated_calls(self):
        col = _dt_col()
        first = DATETIME_CANON(datetime(2026, 11, 1, 1, 30, 0), col)
        for _ in range(5):
            self.assertEqual(DATETIME_CANON(datetime(2026, 11, 1, 1, 30, 0), col), first)


class TestPurity(BaseCase):
    """§12 invariant 1 — no clock, no locale, no ambient timezone."""

    def test_same_input_same_output_regardless_of_call_order(self):
        col = _dt_col()
        values = [
            datetime(2026, 7, 31, 10, 0),
            datetime(2026, 1, 31, 10, 0),
            datetime(2026, 11, 1, 1, 30),
        ]
        forward = [DATETIME_CANON(v, col) for v in values]
        backward = [DATETIME_CANON(v, col) for v in reversed(values)]
        self.assertEqual(forward, list(reversed(backward)))

    def test_date_canon_never_consults_a_timezone(self):
        # Same calendar date, two wildly different declared zones: identical.
        a = DATE_CANON(45000, _date_col(sheet_timezone=NY))
        b = DATE_CANON(45000, _date_col(sheet_timezone="Pacific/Kiritimati"))
        self.assertEqual(a, b)
        self.assertEqual(a, "d:2023-03-15")

    def test_midnight_time_object_is_the_zero_time(self):
        self.assertEqual(serial_to_naive(45000).time(), time())
