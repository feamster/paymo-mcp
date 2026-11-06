"""Tests for PaymoClient.get_entries: local-timezone UTC window and date trim."""

import unittest
from unittest.mock import patch
from urllib.parse import unquote

from paymo_timesheet import PaymoClient


def fetch(entries, start, end, timezone='Pacific/Auckland', **kwargs):
    """Run get_entries against canned `entries`; return (result, requested endpoint)."""
    seen = {}

    def fake_request(self, method, endpoint, **_kwargs):
        seen['endpoint'] = unquote(endpoint)
        return {'entries': entries}

    client = PaymoClient(api_key='test', timezone=timezone)
    with patch.object(PaymoClient, '_request', fake_request):
        result = client.get_entries(start, end, **kwargs)
    return result, seen['endpoint']


class WindowTests(unittest.TestCase):
    def test_nzdt_day_is_converted_to_utc(self):
        # NZDT is UTC+13, so local 19 Oct runs 18 Oct 11:00Z to 19 Oct 10:59:59Z
        _, endpoint = fetch([], '2026-10-19', '2026-10-19')
        self.assertIn('time_interval in ("2026-10-18T11:00:00Z","2026-10-19T10:59:59Z")', endpoint)

    def test_nzst_day_is_converted_to_utc(self):
        _, endpoint = fetch([], '2026-08-03', '2026-08-03')
        self.assertIn('time_interval in ("2026-08-02T12:00:00Z","2026-08-03T11:59:59Z")', endpoint)

    def test_range_spanning_dst_change_uses_each_endpoint_offset(self):
        # NZST ends 27 Sep 2026: 25 Sep is +12, 29 Sep is +13
        _, endpoint = fetch([], '2026-09-25', '2026-09-29')
        self.assertIn('("2026-09-24T12:00:00Z","2026-09-29T10:59:59Z")', endpoint)

    def test_user_id_is_combined_with_window(self):
        _, endpoint = fetch([], '2026-10-19', '2026-10-19', user_id=42)
        self.assertIn(' and user_id=42', endpoint)

    def test_no_dates_means_no_time_filter(self):
        _, endpoint = fetch([], None, None)
        self.assertEqual(endpoint, 'entries')


class TrimTests(unittest.TestCase):
    def test_trims_on_entry_date(self):
        entries = [
            {'id': 1, 'date': '2026-10-18'},
            {'id': 2, 'date': '2026-10-19'},
            {'id': 3, 'date': '2026-10-20'},
        ]
        result, _ = fetch(entries, '2026-10-19', '2026-10-19')
        self.assertEqual([e['id'] for e in result], [2])

    def test_block_entry_uses_local_date_not_utc_date(self):
        # 25 Oct 11:00Z is 26 Oct 00:00 in NZDT: it belongs to the 26th
        block = {'id': 9, 'date': None, 'start_time': '2026-10-25T11:00:00Z',
                 'end_time': '2026-10-25T12:00:00Z'}
        on_25th, _ = fetch([block], '2026-10-25', '2026-10-25')
        on_26th, _ = fetch([block], '2026-10-26', '2026-10-26')
        self.assertEqual(on_25th, [])
        self.assertEqual([e['id'] for e in on_26th], [9])

    def test_user_filter_is_enforced_client_side(self):
        entries = [
            {'id': 1, 'date': '2026-10-19', 'user_id': 42},
            {'id': 2, 'date': '2026-10-19', 'user_id': 7},
        ]
        result, _ = fetch(entries, '2026-10-19', '2026-10-19', user_id=42)
        self.assertEqual([e['id'] for e in result], [1])


if __name__ == '__main__':
    unittest.main()
