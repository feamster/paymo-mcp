"""Tests for the invoice fee/expense split — the bug where subtracting
calculated fees from invoice total reported missing time entries as
fake expenses (see git commit / feedback memory 2026-07-24)."""

import re
import unittest
from unittest.mock import patch

from paymo_timesheet import PaymoClient


def make_client_with_requests(routes):
    """Return a PaymoClient whose ._request dispatches through `routes`.

    `routes` is a list of (method, path_regex, response_dict). The regex
    matches against the PATH portion (everything before '?'), so tests
    can distinguish `invoices` (list) from `invoices/1234` (get one).
    First matching entry wins. Anything unmatched raises loudly.
    """
    compiled = [(m, re.compile(pat), body) for m, pat, body in routes]

    def fake_request(self, method, endpoint, **_kwargs):
        path = endpoint.split('?', 1)[0]
        for r_method, r_re, r_body in compiled:
            if method == r_method and r_re.fullmatch(path):
                return r_body
        raise AssertionError(f"unexpected request: {method} {endpoint}")

    client = PaymoClient(api_key="test")
    patcher = patch.object(PaymoClient, '_request', fake_request)
    patcher.start()
    return client, patcher


class GetInvoiceFinancialsTests(unittest.TestCase):
    """Direct tests of the fees/expenses split from invoice line items."""

    def test_mixed_invoice_splits_correctly(self):
        # Item 1 = fee line ($5000); Item 2 = expense line ($1500)
        # Expense record links its invoice_item_id to Item 2.
        routes = [
            ('GET', r'invoices/1234', {
                'invoices': [{
                    'id': 1234, 'number': 'INV-TEST-001',
                    'total': 6500.0, 'subtotal': 6500.0,
                    'date': '2026-05-01',
                    'project_id': 999,
                    'invoiceitems': [
                        {'id': 1, 'price_unit': 100, 'quantity': 50, 'subtotal': 5000.0},
                        {'id': 2, 'price_unit': 1, 'quantity': 1500, 'subtotal': 1500.0},
                    ],
                }],
            }),
            ('GET', r'expenses', {
                'expenses': [
                    {'id': 77, 'invoice_item_id': 2, 'amount': 1500.0,
                     'project_id': 999, 'date': '2026-04-20'},
                ],
            }),
        ]
        client, patcher = make_client_with_requests(routes)
        try:
            fin = client.get_invoice_financials(1234)
        finally:
            patcher.stop()

        self.assertEqual(fin['fees'], 5000.0)
        self.assertEqual(fin['expenses'], 1500.0)
        self.assertEqual(fin['total'], 6500.0)
        self.assertEqual(len(fin['fee_items']), 1)
        self.assertEqual(len(fin['expense_items']), 1)
        self.assertEqual(len(fin['linked_expenses']), 1)

    def test_fee_only_invoice_has_zero_expenses(self):
        routes = [
            ('GET', r'invoices/1234', {
                'invoices': [{
                    'id': 1234, 'number': 'INV-TEST-002',
                    'total': 43807.50, 'subtotal': 43807.50,
                    'date': '2026-03-31',
                    'project_id': 999,
                    'invoiceitems': [
                        {'id': 10, 'price_unit': 825, 'quantity': 53.1,
                         'subtotal': 43807.50},
                    ],
                }],
            }),
            # No expenses at all for the project.
            ('GET', r'expenses', {'expenses': []}),
        ]
        client, patcher = make_client_with_requests(routes)
        try:
            fin = client.get_invoice_financials(1234)
        finally:
            patcher.stop()

        self.assertEqual(fin['fees'], 43807.50)
        self.assertEqual(fin['expenses'], 0)

    def test_expense_on_wrong_invoice_does_not_leak(self):
        # An expense in the same project/date window links to a DIFFERENT
        # invoice's item — must not be counted here.
        routes = [
            ('GET', r'invoices/1234', {
                'invoices': [{
                    'id': 1234, 'number': 'INV-TEST-003',
                    'total': 5000.0, 'subtotal': 5000.0,
                    'date': '2026-05-01',
                    'project_id': 999,
                    'invoiceitems': [
                        {'id': 1, 'price_unit': 100, 'quantity': 50, 'subtotal': 5000.0},
                    ],
                }],
            }),
            ('GET', r'expenses', {
                'expenses': [
                    # This expense links to invoice item id 99999, not 1.
                    {'id': 77, 'invoice_item_id': 99999, 'amount': 250.0,
                     'project_id': 999, 'date': '2026-04-20'},
                ],
            }),
        ]
        client, patcher = make_client_with_requests(routes)
        try:
            fin = client.get_invoice_financials(1234)
        finally:
            patcher.stop()

        self.assertEqual(fin['fees'], 5000.0)
        self.assertEqual(fin['expenses'], 0)
        self.assertEqual(len(fin['linked_expenses']), 0)

    def test_tax_scaling(self):
        # Item subtotals sum to 1000, but invoice total is 1080 (8% tax).
        # Fees and expenses should be scaled to add back up to total.
        routes = [
            ('GET', r'invoices/1234', {
                'invoices': [{
                    'id': 1234, 'number': 'INV-TEST-004',
                    'total': 1080.0, 'subtotal': 1000.0,
                    'date': '2026-05-01',
                    'project_id': 999,
                    'invoiceitems': [
                        {'id': 1, 'price_unit': 100, 'quantity': 8, 'subtotal': 800.0},
                        {'id': 2, 'price_unit': 200, 'quantity': 1, 'subtotal': 200.0},
                    ],
                }],
            }),
            ('GET', r'expenses', {
                'expenses': [
                    {'id': 77, 'invoice_item_id': 2, 'amount': 200.0,
                     'project_id': 999, 'date': '2026-04-20'},
                ],
            }),
        ]
        client, patcher = make_client_with_requests(routes)
        try:
            fin = client.get_invoice_financials(1234)
        finally:
            patcher.stop()

        self.assertAlmostEqual(fin['fees'] + fin['expenses'], 1080.0, places=2)
        self.assertAlmostEqual(fin['fees'], 864.0, places=2)   # 800 × 1.08
        self.assertAlmostEqual(fin['expenses'], 216.0, places=2)  # 200 × 1.08


class ExportInvoiceStrictGuardTests(unittest.TestCase):
    """Verify the strict guard compares to invoice FEES, not invoice TOTAL.

    Before the fix, any invoice with any expense line item would trip the
    guard even when the linked time entries perfectly covered the fees.
    """

    # Two entries totaling 54.3 hours (matches the real CRA INV-250 case).
    ENTRIES = [
        {'id': 100, 'invoice_item_id': 500, 'date': '2026-04-15',
         'duration': 30 * 3600, 'project_id': 777},
        {'id': 101, 'invoice_item_id': 500, 'date': '2026-04-16',
         'duration': int(24.3 * 3600), 'project_id': 777},
    ]

    PROJECTS = [{'id': 777, 'name': 'Test Matter', 'price_per_hour': 825}]

    def _routes(self, invoice_items, expenses, number='INV-CRA-250', total=47313.99):
        invoice_body = {
            'invoices': [{
                'id': 1234, 'number': number,
                'total': total, 'subtotal': total,
                'date': '2026-05-01',
                'project_id': 777,
                'invoiceitems': invoice_items,
            }],
        }
        return [
            # order matters: put the more-specific `invoices/1234` route
            # BEFORE the bare `invoices` list route so the regex fullmatch
            # picks the right one.
            ('GET', r'invoices/1234', invoice_body),
            ('GET', r'invoices', invoice_body),
            ('GET', r'entries', {'entries': self.ENTRIES}),
            ('GET', r'expenses', {'expenses': expenses}),
            ('GET', r'projects', {'projects': self.PROJECTS}),
        ]

    def test_strict_passes_when_fees_match_despite_expenses(self):
        """Regression test for the CRA INV-250 case.

        54.30 hrs × $825/hr = $44,797.50 covers the invoice's fee line
        exactly. There's also an expense line ($2,516.49) making the
        invoice total $47,313.99. The OLD strict guard compared $44,797.50
        to $47,313.99 and errored out. It should now pass.
        """
        invoice_items = [
            {'id': 500, 'price_unit': 825, 'quantity': 54.30, 'subtotal': 44797.50},
            {'id': 501, 'price_unit': 1, 'quantity': 2516.49, 'subtotal': 2516.49},
        ]
        expenses = [
            {'id': 77, 'invoice_item_id': 501, 'amount': 2516.49,
             'project_id': 777, 'date': '2026-04-20'},
        ]
        client, patcher = make_client_with_requests(self._routes(invoice_items, expenses))
        try:
            csv_out = client.export_invoice_formatted('INV-CRA-250', strict=True)
        finally:
            patcher.stop()

        # Header should report the true split, not the subtraction hack.
        self.assertIn('Fees,"$44,797.50"', csv_out)
        self.assertIn('Expenses,"$2,516.49"', csv_out)
        self.assertIn('Total Due,"$47,313.99"', csv_out)

    def test_strict_errors_when_linked_entries_dont_cover_fees(self):
        """Regression test for the CRA INV-233 misdiagnosis.

        The invoice has $84,315 in fees, but only $57,750 (70 hrs × $825)
        of linked time entries. The strict guard MUST reject this rather
        than silently reporting the $26,565 gap as expenses.
        """
        # Only 70h worth of entries linked, on a $84,315 fee item.
        seventy_hour_entries = [
            {'id': 200, 'invoice_item_id': 800, 'date': '2026-02-15',
             'duration': 70 * 3600, 'project_id': 777},
        ]
        invoice_items = [
            {'id': 800, 'price_unit': 825, 'quantity': 102.2, 'subtotal': 84315.00},
            {'id': 801, 'price_unit': 1, 'quantity': 1609.23, 'subtotal': 1609.23},
        ]
        expenses = [
            {'id': 88, 'invoice_item_id': 801, 'amount': 1609.23,
             'project_id': 777, 'date': '2026-02-20'},
        ]
        invoice_body = {
            'invoices': [{
                'id': 1234, 'number': 'INV-CRA-233',
                'total': 85924.23, 'subtotal': 85924.23,
                'date': '2026-03-04',
                'project_id': 777,
                'invoiceitems': invoice_items,
            }],
        }
        routes = [
            ('GET', r'invoices/1234', invoice_body),
            ('GET', r'invoices', invoice_body),
            ('GET', r'entries', {'entries': seventy_hour_entries}),
            ('GET', r'expenses', {'expenses': expenses}),
            ('GET', r'projects', {'projects': self.PROJECTS}),
        ]
        client, patcher = make_client_with_requests(routes)
        try:
            with self.assertRaises(ValueError) as ctx:
                client.export_invoice_formatted('INV-CRA-233', strict=True)
        finally:
            patcher.stop()

        msg = str(ctx.exception)
        # New error must talk about missing fees, not "total mismatch"
        # (the old wording invited callers to infer expenses = total - fees).
        self.assertIn("don't cover invoice fees", msg)
        self.assertIn('Missing', msg)
        # Must NOT contain the old misleading phrasing.
        self.assertNotIn('Invoice total mismatch', msg)
        # Must surface the true expenses figure so the caller sees it.
        self.assertIn('$1,609.23', msg)


class UpdateInvoiceTests(unittest.TestCase):
    """Tests for PaymoClient.update_invoice and the update_paymo_invoice
    MCP tool. Guards against the two easy footguns: sending an unknown
    status enum, and PUTting an empty payload."""

    def test_low_level_update_invoice_sends_put_with_kwargs(self):
        captured = {}

        def fake_request(self, method, endpoint, **kwargs):
            captured['method'] = method
            captured['endpoint'] = endpoint
            captured['json'] = kwargs.get('json')
            return {'invoices': [{
                'id': 1234, 'number': 'INV-TEST-999',
                'status': 'paid', 'due_date': '2026-08-15',
                'total': 100.0,
            }]}

        client = PaymoClient(api_key='test')
        with patch.object(PaymoClient, '_request', fake_request):
            out = client.update_invoice(1234, status='paid', due_date='2026-08-15')

        self.assertEqual(captured['method'], 'PUT')
        self.assertEqual(captured['endpoint'], 'invoices/1234')
        self.assertEqual(captured['json'], {'status': 'paid', 'due_date': '2026-08-15'})
        self.assertEqual(out['status'], 'paid')

    def test_low_level_update_invoice_handles_bare_dict_response(self):
        """Paymo occasionally returns the invoice dict directly rather than
        wrapped in `{'invoices': [...]}`. The wrapper should tolerate both."""
        def fake_request(self, method, endpoint, **kwargs):
            return {'id': 1234, 'status': 'void'}

        client = PaymoClient(api_key='test')
        with patch.object(PaymoClient, '_request', fake_request):
            out = client.update_invoice(1234, status='void')
        self.assertEqual(out['status'], 'void')


class UpdatePaymoInvoiceToolTests(unittest.TestCase):
    """Tests for the MCP tool `update_paymo_invoice` itself — enum
    validation, empty-payload rejection, and multi-field PUT."""

    def _run(self, **kwargs):
        # The tool is defined inside `if MCP_AVAILABLE:` and exposed at
        # module scope by the same name — call it directly. Stub
        # load_config so the tool doesn't touch ~/.mcp-auth for an API key.
        import paymo_timesheet
        with patch.object(
            paymo_timesheet, 'load_config',
            lambda: {'api_key': 'test'},
        ):
            return paymo_timesheet.update_paymo_invoice(**kwargs)

    def test_rejects_invalid_status(self):
        with self.assertRaises(ValueError) as ctx:
            self._run(invoice_number='INV-X', status='mostly-paid')
        self.assertIn('Invalid status', str(ctx.exception))

    def test_rejects_empty_payload(self):
        with self.assertRaises(ValueError) as ctx:
            self._run(invoice_number='INV-X')
        self.assertIn('No fields provided', str(ctx.exception))

    def test_puts_multiple_fields_and_reports_previous_status(self):
        # Mock the whole request pipeline so we can watch what got PUT.
        captured = {}

        def fake_request(self, method, endpoint, **kwargs):
            if method == 'GET' and endpoint.startswith('invoices'):
                # find_invoice_by_number -> get_invoices
                return {'invoices': [{
                    'id': 42, 'number': 'INV-CRA-233',
                    'status': 'sent', 'due_date': '2026-04-03',
                    'total': 85924.23,
                }]}
            if method == 'PUT' and endpoint == 'invoices/42':
                captured['put'] = kwargs.get('json')
                return {'invoices': [{
                    'id': 42, 'number': 'INV-CRA-233',
                    'status': 'paid', 'due_date': '2026-05-01',
                    'total': 85924.23, 'currency': 'USD',
                }]}
            raise AssertionError(f"unexpected: {method} {endpoint}")

        with patch.object(PaymoClient, '_request', fake_request):
            out = self._run(
                invoice_number='INV-CRA-233',
                status='paid',
                due_date='2026-05-01',
            )

        self.assertEqual(captured['put'], {'status': 'paid', 'due_date': '2026-05-01'})
        self.assertEqual(out['status'], 'paid')
        self.assertEqual(out['previous_status'], 'sent')
        self.assertEqual(sorted(out['updated_fields']), ['due_date', 'status'])


if __name__ == '__main__':
    unittest.main()
