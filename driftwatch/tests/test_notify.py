# -*- coding: utf-8 -*-
"""The digest fingerprint and the rendered mail."""
from __future__ import annotations

import unittest

from driftwatch import notify


def finding(**kw) -> dict:
    base = {
        'id': 1, 'run_id': 7, 'created_at': '2026-08-05 10:00:00',
        'drift_type': 'field_mismatch', 'severity': 'warning',
        'dataset_id': 12, 'row_ref': 'row 41', 'column_key': 'amount',
        'sheet_value': '1200.00', 'odoo_value': '1150.00', 'detail': '',
        'file_name': 'Cashflow 2026', 'tab_title': 'Q3',
    }
    base.update(kw)
    return base


class FingerprintTests(unittest.TestCase):

    def test_order_does_not_matter(self):
        a = finding(row_ref='row 1')
        b = finding(row_ref='row 2')
        self.assertEqual(notify.fingerprint([a, b]),
                         notify.fingerprint([b, a]))

    def test_ids_and_timestamps_do_not_matter(self):
        """Every verify run rewrites drift rows with new ids and times.

        If those counted, every cycle would look like a change and the digest
        would go out hourly -- which is the failure this design exists to
        avoid.
        """
        before = finding(id=1, run_id=7, created_at='2026-08-05 10:00:00')
        after = finding(id=999, run_id=8, created_at='2026-08-05 11:00:00')
        self.assertEqual(notify.fingerprint([before]),
                         notify.fingerprint([after]))

    def test_a_changed_value_changes_the_fingerprint(self):
        self.assertNotEqual(notify.fingerprint([finding()]),
                            notify.fingerprint([finding(odoo_value='9.99')]))

    def test_a_new_finding_changes_the_fingerprint(self):
        one = [finding()]
        two = [finding(), finding(row_ref='row 42')]
        self.assertNotEqual(notify.fingerprint(one), notify.fingerprint(two))

    def test_drift_clearing_changes_the_fingerprint(self):
        self.assertNotEqual(notify.fingerprint([finding()]),
                            notify.fingerprint([]))

    def test_empty_is_stable(self):
        self.assertEqual(notify.fingerprint([]), notify.fingerprint([]))


class RenderTests(unittest.TestCase):

    def test_clear_subject_when_there_is_no_drift(self):
        subject, body = notify.render_digest([], {})
        self.assertIn('clear', subject)
        self.assertIn('No findings', body)

    def test_subject_counts_and_names_the_top_types(self):
        findings = [finding() for _ in range(3)] + [
            finding(drift_type='missing_in_odoo')]
        summary = {'field_mismatch': 3, 'missing_in_odoo': 1}
        subject, _ = notify.render_digest(findings, summary)
        self.assertIn('4 findings', subject)
        self.assertIn('3 field_mismatch', subject)

    def test_singular_finding_reads_correctly(self):
        subject, _ = notify.render_digest([finding()], {'field_mismatch': 1})
        self.assertIn('1 finding (', subject)

    def test_long_runs_are_capped_and_say_so(self):
        """A silent truncation reads as 'that was all of it'."""
        findings = [finding(row_ref=f'row {i}') for i in range(200)]
        _, body = notify.render_digest(findings, {'field_mismatch': 200})
        self.assertIn(f'first {notify.MAX_LISTED} of 200', body)
        self.assertIn(f'and {200 - notify.MAX_LISTED} more', body)

    def test_body_carries_the_values_and_the_location(self):
        _, body = notify.render_digest([finding()], {'field_mismatch': 1})
        self.assertIn('Cashflow 2026 / Q3', body)
        self.assertIn('row 41', body)
        self.assertIn('1200.00', body)
        self.assertIn('1150.00', body)

    def test_body_states_that_nothing_was_changed(self):
        _, body = notify.render_digest([finding()], {'field_mismatch': 1})
        self.assertIn('read-only', body)

    def test_previous_count_is_reported_as_a_delta(self):
        _, body = notify.render_digest([finding()], {'field_mismatch': 1},
                                       previously=8)
        self.assertIn('8 -> 1', body)


if __name__ == '__main__':
    unittest.main()
