#!/usr/bin/env python3
"""
Paymo Timesheet Automation Script
Automate time entry creation in Paymo from structured meeting/work data.
Can run as CLI or MCP server.
"""

import requests
import yaml
import json
import sys
import time
import os
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple, Any
from pathlib import Path
import pytz
from dateutil import parser as dateparser
from rich.console import Console
from rich.table import Table
from rich import print as rprint
import click
import io

# When running as MCP server, disable all console output to avoid filling Claude's context
# Claude Desktop captures stderr output and includes it in the conversation
_is_mcp_mode = len(sys.argv) > 1 and sys.argv[1] == 'mcp'
console = Console(file=io.StringIO() if _is_mcp_mode else sys.stdout, quiet=_is_mcp_mode)

# MCP Server support (optional)
MCP_AVAILABLE = False
try:
    from mcp.server.fastmcp import FastMCP
    MCP_AVAILABLE = True
except ImportError:
    pass


class PaymoClient:
    """Wrapper for Paymo API calls"""

    def __init__(self, api_key: str, base_url: str = "https://app.paymoapp.com/api/"):
        self.api_key = api_key
        self.base_url = base_url.rstrip('/') + '/'
        self.session = requests.Session()
        self.session.auth = (api_key, 'X')
        self.session.headers.update({
            'Accept': 'application/json',
            'Content-Type': 'application/json'
        })

    def _handle_rate_limit(self, response) -> None:
        """Inspect rate-limit headers on a response and sleep if we've drained
        the budget. Used by both _request and file-upload paths so that write
        endpoints (which bypass _request via multipart) still respect Paymo's
        pacing. Without this, POST /files right after POST /expenses returns
        400 (not 429) because we overran the write-budget."""
        remaining = response.headers.get('X-Ratelimit-Remaining')
        limit = response.headers.get('X-Ratelimit-Limit')
        decay = response.headers.get('X-Ratelimit-Decay-Period')
        if not remaining:
            return
        try:
            remaining_values = [int(x.strip()) for x in remaining.split(',')]
            remaining_min = min(remaining_values)
        except ValueError:
            return
        if remaining_min < 5:
            console.print(f"[yellow]⚠ Rate limit: {remaining}/{limit} remaining (resets in {decay}s)[/yellow]")
        if remaining_min <= 1:
            try:
                wait = int(decay) if decay else 5
            except (ValueError, TypeError):
                wait = 5
            time.sleep(max(1, min(wait, 10)))

    def _request(self, method: str, endpoint: str, **kwargs) -> Dict:
        """Make API request with error handling"""
        url = f"{self.base_url}{endpoint.lstrip('/')}"

        try:
            response = self.session.request(method, url, **kwargs)
            self._handle_rate_limit(response)

            response.raise_for_status()
            return response.json()

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:
                retry_after = e.response.headers.get('Retry-After', '60')
                console.print(f"[red]Rate limit exceeded! Must wait {retry_after}s[/red]")
                # Re-raise with the retry_after info attached
                e.retry_after = int(retry_after)
            else:
                console.print(f"[red]API Error: {e}[/red]")
                if hasattr(e.response, 'text'):
                    console.print(f"[red]Response: {e.response.text}[/red]")
            raise
        except requests.exceptions.RequestException as e:
            console.print(f"[red]Request failed: {e}[/red]")
            raise

    def get_clients(self, active_only: bool = True) -> List[Dict]:
        """List all clients"""
        endpoint = "clients"
        if active_only:
            endpoint += "?where=active=true"

        response = self._request('GET', endpoint)
        return response.get('clients', [])

    def create_client(self, name: str, **kwargs) -> Dict:
        """Create a new client"""
        data = {'name': name, **kwargs}
        response = self._request('POST', 'clients', json=data)
        return response.get('clients', [{}])[0] if 'clients' in response else response

    def get_projects(self, active_only: bool = True) -> List[Dict]:
        """List all projects"""
        endpoint = "projects"
        if active_only:
            endpoint += "?where=active=true"

        response = self._request('GET', endpoint)
        return response.get('projects', [])

    def get_tasks(self, project_id: Optional[int] = None) -> List[Dict]:
        """List tasks, optionally filtered by project"""
        endpoint = "tasks"
        if project_id:
            endpoint += f"?where=project_id={project_id}"

        response = self._request('GET', endpoint)
        return response.get('tasks', [])

    def get_entries(self, start_date: Optional[str] = None, end_date: Optional[str] = None) -> List[Dict]:
        """List time entries within date range"""
        endpoint = "entries"

        if start_date and end_date:
            # Convert dates to ISO format
            start_dt = datetime.strptime(start_date, '%Y-%m-%d')
            end_dt = datetime.strptime(end_date, '%Y-%m-%d')

            start_iso = start_dt.strftime('%Y-%m-%dT00:00:00Z')
            end_iso = end_dt.strftime('%Y-%m-%dT23:59:59Z')

            endpoint += f'?where=time_interval in ("{start_iso}","{end_iso}")'

        response = self._request('GET', endpoint)
        return response.get('entries', [])

    def create_entry(self, task_id: int, **kwargs) -> Dict:
        """
        Create a new time entry

        Args:
            task_id: Required task ID
            **kwargs: Either (start_time, end_time) or (date, duration)
                     Plus optional description, billed, etc.
        """
        data = {'task_id': task_id, **kwargs}

        response = self._request('POST', 'entries', json=data)
        return response

    def create_entries_batch(self, entries: List[Dict]) -> Dict:
        """
        Create multiple time entries in one API call

        Args:
            entries: List of entry dicts, each with task_id and time data
        """
        response = self._request('POST', 'entries', json=entries)
        return response

    def delete_entry(self, entry_id: int) -> Dict:
        """Delete a time entry by ID"""
        response = self._request('DELETE', f'entries/{entry_id}')
        return response

    def update_entry(self, entry_id: int, **kwargs) -> Dict:
        """Update a time entry (billed, description, etc.)"""
        response = self._request('PUT', f'entries/{entry_id}', json=kwargs)
        return response.get('entries', [response])[0] if 'entries' in response else response

    def create_task(self, project_id: int, name: str, billable: bool = True) -> Dict:
        """Create a new task in a project"""
        data = {
            'project_id': project_id,
            'name': name,
            'billable': billable
        }
        response = self._request('POST', 'tasks', json=data)
        return response

    def update_task(self, task_id: int, **kwargs) -> Dict:
        """Update a task (name, billable, etc.)"""
        response = self._request('PUT', f'tasks/{task_id}', json=kwargs)
        return response.get('tasks', [response])[0] if 'tasks' in response else response

    def create_project(self, name: str, client_id: int, **kwargs) -> Dict:
        """Create a new project"""
        data = {'name': name, 'client_id': client_id, **kwargs}
        response = self._request('POST', 'projects', json=data)
        return response.get('projects', [{}])[0] if 'projects' in response else response

    def update_project(self, project_id: int, **kwargs) -> Dict:
        """Update an existing project"""
        response = self._request('PUT', f'projects/{project_id}', json=kwargs)
        return response.get('projects', [{}])[0] if 'projects' in response else response

    def get_expenses(self, project_id: Optional[int] = None,
                     start_date: Optional[str] = None,
                     end_date: Optional[str] = None) -> List[Dict]:
        """List expenses, optionally filtered by project and/or date range.

        Server-side project_id filter works. Date range is applied client-side
        because Paymo's `?where=date in ("start","end")` is literal set
        membership on those two dates - not a range - and silently returns
        wrong results (verified against live account 2026-07: range with 12
        real hits returned only the 2 records dated exactly on the endpoints).
        """
        endpoint = "expenses"
        if project_id:
            endpoint += f"?where=project_id={int(project_id)}"
        expenses = self._request('GET', endpoint).get('expenses', [])
        if start_date and end_date:
            expenses = [
                e for e in expenses
                if e.get('date') and start_date <= e.get('date') <= end_date
            ]
        return expenses

    def create_expense(self, project_id: int, **kwargs) -> Dict:
        """Create a single expense on a project. Paymo requires client_id in
        addition to project_id (400 "Missing required params: client_id"
        otherwise), so callers must pass it. See create_paymo_expense for
        auto-lookup from project."""
        data = {'project_id': int(project_id), **kwargs}
        r = self._request('POST', 'expenses', json=data)
        return r.get('expenses', [{}])[0] if 'expenses' in r else r

    def upload_expense_file(self, expense_id: int, file_path: str,
                            max_retries: int = 3) -> Dict:
        """Attach a file to an existing expense.

        Paymo's public write endpoint is POST /files with the target expense_id
        as a form field (verified 2026-07). The multipart PUT /expenses/{id}
        returns 200 but does not persist the file, so that path is a trap and
        should not be used.

        Rate-limit behavior: when the write budget is drained, Paymo returns
        400 "Bad Request" here (not 429). Retry with exponential backoff so
        the caller doesn't have to.
        """
        import mimetypes, os
        p = os.path.expanduser(str(file_path))
        if not os.path.exists(p):
            raise ValueError(f"Attachment not found: {p}")
        mime = mimetypes.guess_type(p)[0] or 'application/octet-stream'
        # Use a fresh request (not self.session.post) so the session's
        # Content-Type: application/json doesn't leak through and override the
        # multipart boundary that `files=` needs to set. Observed 2026-07:
        # PDFs were silently rejected with "Decoding failed: Syntax error" when
        # multipart was overridden by JSON Content-Type.
        auth = self.session.auth
        headers = {'Accept': 'application/json'}

        last_err = None
        for attempt in range(max_retries):
            with open(p, 'rb') as fh:
                resp = requests.post(
                    f"{self.base_url}files",
                    auth=auth,
                    headers=headers,
                    files={'file': (os.path.basename(p), fh, mime)},
                    data={'expense_id': int(expense_id)},
                )
            self._handle_rate_limit(resp)
            if resp.status_code < 400:
                body = resp.json()
                return body.get('files', [{}])[0] if 'files' in body else body
            # Retry on 400 or 429 - Paymo returns 400 when write budget drained
            if resp.status_code in (400, 429) and attempt < max_retries - 1:
                wait = 2 ** (attempt + 2)  # 4s, 8s, 16s
                console.print(f"[yellow]Upload attempt {attempt+1} got {resp.status_code}; retry in {wait}s[/yellow]")
                time.sleep(wait)
                continue
            last_err = f"{resp.status_code}: {resp.text[:200]}"
            break
        raise requests.exceptions.HTTPError(f"Upload failed after {max_retries} attempts: {last_err}")

    def delete_expense(self, expense_id: int) -> Dict:
        """Delete an expense by ID."""
        return self._request('DELETE', f'expenses/{int(expense_id)}')

    def update_expense(self, expense_id: int, **kwargs) -> Dict:
        """Update an expense (amount, name, notes, date, etc.).
        Preserves invoice_item_id / attachments — delete+recreate would not."""
        r = self._request('PUT', f'expenses/{int(expense_id)}', json=kwargs)
        return r.get('expenses', [{}])[0] if 'expenses' in r else r

    def delete_file(self, file_id: int) -> Dict:
        """Delete a file attachment by file ID."""
        return self._request('DELETE', f'files/{int(file_id)}')

    def get_invoices(self, client_id: Optional[int] = None, status: Optional[str] = None) -> List[Dict]:
        """
        List invoices, optionally filtered by client and status

        Args:
            client_id: Filter by client ID
            status: Filter by status (sent, viewed, paid, etc.)
        """
        endpoint = "invoices"
        filters = []

        if client_id:
            filters.append(f"client_id={client_id}")
        if status:
            filters.append(f"status={status}")

        if filters:
            endpoint += "?where=" + " and ".join(filters)

        response = self._request('GET', endpoint)
        return response.get('invoices', [])

    def get_invoice(self, invoice_id: int) -> Dict:
        """Get detailed invoice information"""
        response = self._request('GET', f'invoices/{invoice_id}')
        return response.get('invoices', [{}])[0]

    def update_invoice(self, invoice_id: int, **kwargs) -> Dict:
        """Update an existing invoice (status, dates, amounts, notes, etc.).

        Thin PUT wrapper matching the pattern of update_task /
        update_project / update_entry. Callers pass whatever fields
        Paymo's PUT /invoices/{id} accepts (verified via docs 2026-07-24
        at github.com/paymoapp/api/blob/master/sections/invoices.md).
        Higher-level callers should use `update_paymo_invoice_status` MCP
        tool for status-only changes because it validates the enum.
        """
        response = self._request('PUT', f'invoices/{invoice_id}', json=kwargs)
        return response.get('invoices', [response])[0] if 'invoices' in response else response

    def get_invoice_financials(self, invoice_id: int) -> Dict[str, Any]:
        """Split an invoice into fees vs expenses by reading its line items.

        Paymo has no explicit item-type flag distinguishing an expense line
        from a fee line — both are just `invoiceitem` rows. The reliable
        signal is that expense records point back at their invoice line via
        `expense.invoice_item_id`. So we classify each line item as an
        expense iff at least one expense record links to it, and everything
        else as a fee.

        Do NOT compute expenses as `invoice_total - (hours * rate)` — that
        was the previous approach and it silently reports missing/unlinked
        time entries as fake expenses (see feedback memory 2026-07-24).
        """
        response = self._request(
            'GET', f'invoices/{invoice_id}?include=invoiceitems'
        )
        invoice = response.get('invoices', [{}])[0]
        items = invoice.get('invoiceitems', []) or []
        invoice_total = float(invoice.get('total') or 0)
        invoice_subtotal = float(invoice.get('subtotal') or 0)

        item_id_set = {it.get('id') for it in items if it.get('id')}

        # Collect projects to bound the expense scan. Prefer the invoice's
        # project_id + options.linked_projects; fall back to an unscoped
        # scan only if neither is set.
        project_ids: List[int] = []
        if invoice.get('project_id'):
            project_ids.append(invoice.get('project_id'))
        opts = invoice.get('options') or {}
        for lp in (opts.get('linked_projects') or []):
            pid = lp.get('project_id')
            if pid and pid not in project_ids:
                project_ids.append(pid)

        # Bound the date window around the invoice date. Expenses linked
        # to an invoice are almost always within a few months of the
        # invoice date; ±180 days is generous without pulling the whole
        # expense table.
        inv_date_str = invoice.get('date') or ''
        exp_start: Optional[str] = None
        exp_end: Optional[str] = None
        if inv_date_str:
            try:
                inv_dt = datetime.strptime(inv_date_str, '%Y-%m-%d')
                exp_start = (inv_dt - timedelta(days=180)).strftime('%Y-%m-%d')
                exp_end = (inv_dt + timedelta(days=60)).strftime('%Y-%m-%d')
            except ValueError:
                pass

        expense_item_ids: set = set()
        linked_expenses: List[Dict] = []
        scan_targets = project_ids if project_ids else [None]
        for pid in scan_targets:
            try:
                exps = self.get_expenses(
                    project_id=pid,
                    start_date=exp_start,
                    end_date=exp_end,
                )
            except Exception:
                exps = []
            for e in exps:
                iid = e.get('invoice_item_id')
                if iid and iid in item_id_set:
                    expense_item_ids.add(iid)
                    linked_expenses.append(e)

        def _item_subtotal(it: Dict) -> float:
            # Prefer Paymo's own subtotal if present; else compute
            # from price_unit × quantity (the two required fields on write).
            sub = it.get('subtotal')
            if sub is not None:
                try:
                    return float(sub)
                except (TypeError, ValueError):
                    pass
            try:
                return float(it.get('price_unit') or 0) * float(it.get('quantity') or 0)
            except (TypeError, ValueError):
                return 0.0

        fee_items = [it for it in items if it.get('id') not in expense_item_ids]
        expense_items = [it for it in items if it.get('id') in expense_item_ids]

        fees_subtotal = sum(_item_subtotal(it) for it in fee_items)
        expenses_subtotal = sum(_item_subtotal(it) for it in expense_items)
        computed_subtotal = fees_subtotal + expenses_subtotal

        # If the invoice carries tax or a discount, `total` differs from
        # the sum of item subtotals. Scale proportionally so fees+expenses
        # add back up to `total`.
        if computed_subtotal > 0 and invoice_total > 0 and abs(invoice_total - computed_subtotal) > 0.01:
            ratio = invoice_total / computed_subtotal
            fees_out = round(fees_subtotal * ratio, 2)
            expenses_out = round(expenses_subtotal * ratio, 2)
        else:
            fees_out = round(fees_subtotal, 2)
            expenses_out = round(expenses_subtotal, 2)

        return {
            'invoice_id': invoice_id,
            'invoice_number': invoice.get('number'),
            'total': invoice_total,
            'subtotal': invoice_subtotal,
            'fees': fees_out,
            'expenses': expenses_out,
            'fee_items': fee_items,
            'expense_items': expense_items,
            'linked_expenses': linked_expenses,
            'invoice': invoice,
        }

    def get_most_recent_invoice(self, client_id: int) -> Optional[Dict]:
        """Return the most recent invoice for a client (by date desc), or None.
        Used to lift `title`/`bill_to`/`company_info` from a known-good
        prior invoice so new invoices don't come out blank."""
        invs = self.get_invoices(client_id=client_id)
        if not invs:
            return None
        dated = [(inv.get('date') or '', inv) for inv in invs]
        dated.sort(key=lambda kv: kv[0], reverse=True)
        return dated[0][1]

    # NOTE: Paymo has no API endpoint for emailing an invoice to a client.
    # Verified 2026-07-22 against the official docs at
    # github.com/paymoapp/api/blob/master/sections/invoices.md — the
    # invoices resource exposes only list / get / create / update / delete.
    # The docs explicitly say "An invoice sent to a client by email from
    # the Paymo app will be automatically changed to `sent`" — i.e., the
    # send flow is UI-only. Do not add a send_invoice method here.

    def find_invoice_by_number(self, invoice_number: str) -> Optional[Dict]:
        """
        Find invoice by its number (e.g., 'INV-20260331-241' or '#INV-20260331-241')

        Args:
            invoice_number: The invoice number to search for (with or without # prefix)

        Returns:
            Invoice dict if found, None otherwise
        """
        invoices = self.get_invoices()

        # Normalize: strip # prefix if present for comparison
        search_num = invoice_number.lstrip('#')

        for inv in invoices:
            inv_num = inv.get('number', '').lstrip('#')
            if inv_num == search_num:
                return inv
        return None

    def create_invoice(self, client_id: int, items: List[Dict],
                       project_id: Optional[int] = None,
                       date: Optional[str] = None,
                       due_date: Optional[str] = None,
                       number: Optional[str] = None,
                       currency: str = "USD",
                       language: Optional[str] = None,
                       notes: Optional[str] = None,
                       title: Optional[str] = None,
                       bill_to: Optional[str] = None,
                       company_info: Optional[str] = None,
                       notification_to: Optional[List[str]] = None,
                       footer: Optional[str] = None) -> Dict:
        """Create an invoice header + line items in one POST.

        Paymo's POST /invoices accepts an `items` array inline; each item
        creates a linked invoiceitem row and its id comes back in the
        response under `invoices[0].invoiceitems`. Callers who want to link
        time entries back to specific items should read those ids from the
        returned dict.

        Args:
            client_id: Paymo client id
            items: list of dicts, each with keys:
                item          — line title (Paymo's field is `item`, NOT `title`)
                price_unit    — per-unit price (Paymo's field, NOT `price`)
                quantity      — units (hours)
                description?  — optional detail line
                seq?          — display order (defaults to input order)
            project_id: Paymo project id — MUST be passed for the invoice
                to be linked to a project. Verified 2026-07-22 that omitting
                it produces a valid invoice with project_id=null (orphaned
                from the project it billed).
            date: Invoice date YYYY-MM-DD (default: Paymo uses today)
            due_date: Due date YYYY-MM-DD (default: Paymo uses its template)
            number: Custom invoice number (default: Paymo auto-generates
                using the account's numbering template, e.g. INV-YYYYMMDD-###)
            currency: ISO currency code (default USD)
            language: Optional invoice language override
            notes: Optional notes shown on the invoice
        """
        data: Dict[str, Any] = {
            'client_id': int(client_id),
            'currency': currency,
            'items': items,
        }
        if project_id is not None:
            data['project_id'] = int(project_id)
        if date:
            data['date'] = date
        if due_date:
            data['due_date'] = due_date
        if number:
            data['number'] = number
        if language:
            data['language'] = language
        if notes:
            data['notes'] = notes
        if title is not None:
            data['title'] = title
        if bill_to is not None:
            data['bill_to'] = bill_to
        if company_info is not None:
            data['company_info'] = company_info
        if footer is not None:
            data['footer'] = footer

        # Paymo's UI shows the Project column from `options.linked_projects`,
        # NOT from the invoice-level `project_id`. Passing project_id alone
        # leaves the summary view's Project column blank (verified 2026-07-22).
        # Compute the total from the items so linked_projects.amount matches.
        # `options.notification.to` sets who receives Paymo's send email —
        # without it Paymo falls back to bill_to but at least one prior
        # invoice with an empty list didn't route reliably.
        opts: Dict[str, Any] = {}
        if project_id is not None:
            item_total = sum(
                float(it.get('price_unit') or 0) * float(it.get('quantity') or 0)
                for it in items
            )
            opts['linked_projects'] = [{
                'amount': round(item_total, 2),
                'project_id': int(project_id),
            }]
        if notification_to:
            opts['notification'] = {'to': list(notification_to)}
        if opts:
            data['options'] = opts

        # Ask Paymo to echo the created line items so callers can map
        # entries -> invoice_item_id without a second GET round-trip.
        r = self._request('POST', 'invoices?include=invoiceitems', json=data)
        return r.get('invoices', [{}])[0] if 'invoices' in r else r

    def get_outstanding_invoices_last_week(self) -> List[Dict]:
        """Get outstanding invoices (sent or viewed) from the last 7 days"""
        from datetime import datetime, timedelta

        all_invoices = self.get_invoices()

        # Filter for outstanding (sent or viewed) from last 7 days
        week_ago = datetime.now() - timedelta(days=7)

        outstanding = []
        for inv in all_invoices:
            status = inv.get('status', '').lower()
            if status in ['sent', 'viewed']:
                inv_date_str = inv.get('date', '')
                if inv_date_str:
                    inv_date = datetime.strptime(inv_date_str, '%Y-%m-%d')
                    if inv_date >= week_ago:
                        outstanding.append(inv)

        return outstanding


    def export_invoice_formatted(self, invoice_number: str, strict: bool = True,
                                   tolerance: float = 0.05) -> str:
        """
        Export a formatted timesheet for an invoice, matching the standard billing format.

        This produces a clean CSV with:
        - Header section: Matter, Invoice, Period, Total Hours, Fees, Expenses, Total Due
        - Data section: Date, Start Time (HH:MM), End Time (HH:MM), Duration, Task, Description
        - Entries sorted by date (chronological order)
        - Footer with expenses

        STRICT MATCHING (default): Only includes time entries explicitly linked to this
        invoice via invoice_item_id. Validates that calculated totals match invoice totals.
        If entries don't match, raises an error suggesting to use date-range export instead.

        Args:
            invoice_number: Invoice number (e.g., 'INV-20260331-241')
            strict: If True (default), validate totals match and error if they don't.
                   If False, export whatever entries are linked without validation.
            tolerance: Allowed percentage difference for strict validation (default 5%)

        Returns:
            Formatted CSV content as string

        Raises:
            ValueError: If invoice not found, or if strict=True and totals don't match
        """
        import csv
        import io
        import html
        import re
        import time

        # Find invoice by number
        invoice = self.find_invoice_by_number(invoice_number)
        if not invoice:
            raise ValueError(f"Invoice not found: {invoice_number}")

        invoice_id = invoice.get('id')

        # Get invoice with items
        response = self._request('GET', f'invoices/{invoice_id}?include=invoiceitems')
        invoice = response.get('invoices', [{}])[0]
        invoice_items = invoice.get('invoiceitems', [])

        # Get invoice item IDs
        invoice_item_ids = set(item.get('id') for item in invoice_items if item.get('id'))

        # Get entries for this invoice
        inv_date = invoice.get('date', '')
        if inv_date:
            inv_dt = datetime.strptime(inv_date, '%Y-%m-%d')
            start_date = (inv_dt - timedelta(days=90)).strftime('%Y-%m-%d')
            end_date = inv_date
        else:
            now = datetime.now()
            start_date = (now - timedelta(days=90)).strftime('%Y-%m-%d')
            end_date = now.strftime('%Y-%m-%d')

        all_entries = self.get_entries(start_date, end_date)
        entries = [e for e in all_entries if e.get('invoice_item_id') in invoice_item_ids]

        # Sort entries by date and time (chronological in local timezone)
        def get_entry_sort_key(entry):
            # Get the date
            entry_date = entry.get('date', '')
            if not entry_date and entry.get('start_time'):
                entry_date = entry.get('start_time', '')[:10]

            # Get start time in local timezone for proper sorting
            start_time_sort = '00:00'
            if entry.get('start_time'):
                try:
                    start_dt = dateparser.parse(entry.get('start_time'))
                    if start_dt:
                        local_tz = pytz.timezone('America/Chicago')
                        if start_dt.tzinfo:
                            start_local = start_dt.astimezone(local_tz)
                        else:
                            start_local = local_tz.localize(start_dt)
                        start_time_sort = start_local.strftime('%H:%M')
                except Exception:
                    pass

            return (entry_date, start_time_sort)

        entries.sort(key=get_entry_sort_key)

        # Build task cache
        task_cache = {}
        unique_task_ids = set(e.get('task_id') for e in entries if e.get('task_id'))

        for task_id in unique_task_ids:
            try:
                time.sleep(2)
                task_response = self._request('GET', f'tasks/{task_id}')
                task_data = task_response.get('tasks', [{}])[0] if 'tasks' in task_response else {}
                task_cache[task_id] = task_data.get('name', '')
            except Exception as e:
                if '429' in str(e):
                    time.sleep(6)
                    try:
                        task_response = self._request('GET', f'tasks/{task_id}')
                        task_data = task_response.get('tasks', [{}])[0] if 'tasks' in task_response else {}
                        task_cache[task_id] = task_data.get('name', '')
                    except Exception:
                        task_cache[task_id] = ''
                else:
                    task_cache[task_id] = ''

        # Calculate totals and date range
        total_hours = 0
        earliest_date = None
        latest_date = None

        for entry in entries:
            if entry.get('duration'):
                total_hours += entry['duration'] / 3600
            elif entry.get('start_time') and entry.get('end_time'):
                start = dateparser.parse(entry.get('start_time', ''))
                end = dateparser.parse(entry.get('end_time', ''))
                if start and end:
                    total_hours += (end - start).total_seconds() / 3600

            entry_date_str = entry.get('date') or (entry.get('start_time', '')[:10] if entry.get('start_time') else '')
            if entry_date_str:
                if not earliest_date or entry_date_str < earliest_date:
                    earliest_date = entry_date_str
                if not latest_date or entry_date_str > latest_date:
                    latest_date = entry_date_str

        # Get matter name and hourly rate from project (first entry's project)
        matter_name = ''
        hourly_rate = 0
        if entries:
            project_id = entries[0].get('project_id')
            if project_id:
                try:
                    projects = self.get_projects(active_only=False)
                    for p in projects:
                        if p.get('id') == project_id:
                            matter_name = p.get('name', '')
                            hourly_rate = p.get('price_per_hour', 0) or 0
                            break
                except Exception:
                    pass

        # Get invoice financial info by reading the invoice's own line items
        # (fees vs expenses), not by subtracting calculated hours×rate from
        # the total. See get_invoice_financials() docstring for why.
        invoice_total = float(invoice.get('total') or 0)
        financials = self.get_invoice_financials(invoice_id)
        fees = financials['fees']
        expenses = financials['expenses']
        calculated_fees = total_hours * hourly_rate if hourly_rate else 0

        # Strict validation: check that time entries linked to this invoice
        # actually cover the invoice's fee lines. Compare to `fees`, NOT to
        # `invoice_total` — the old comparison tripped on every invoice with
        # any expense on it.
        if strict and hourly_rate and fees > 0:
            fee_ratio = calculated_fees / fees if fees else 0
            if fee_ratio < (1 - tolerance) or calculated_fees > fees * (1 + tolerance):
                missing = fees - calculated_fees
                raise ValueError(
                    f"Linked time entries don't cover invoice fees: "
                    f"{total_hours:.2f} hrs × ${hourly_rate}/hr = ${calculated_fees:,.2f} "
                    f"vs invoice fees ${fees:,.2f} (expenses ${expenses:,.2f}, total ${invoice_total:,.2f}). "
                    f"Missing ${missing:,.2f} of fees — likely time entries billed on this "
                    f"invoice aren't linked via invoice_item_id, or a different rate was used. "
                    f"Use export_paymo_timesheet(start_date, end_date) for date-range export instead."
                )

        # Build CSV output
        output = io.StringIO()
        writer = csv.writer(output)

        # Header section
        writer.writerow(['Matter', matter_name])
        writer.writerow(['Invoice', invoice_number])
        writer.writerow(['Period', f"{earliest_date or 'N/A'} to {latest_date or 'N/A'}"])
        writer.writerow(['Total Hours', f"{total_hours:.2f}"])
        writer.writerow(['Fees', f"${fees:,.2f}"])
        writer.writerow(['Expenses', f"${expenses:,.2f}"])
        writer.writerow(['Total Due', f"${invoice_total:,.2f}"])
        writer.writerow([])  # Blank line

        # Data header
        writer.writerow(['Date', 'Start Time', 'End Time', 'Duration (hours)', 'Task', 'Description'])

        # Data rows
        for entry in entries:
            task_id = entry.get('task_id')
            task_name = task_cache.get(task_id, '') if task_id else ''

            # Clean description
            description = entry.get('description', '')
            if description:
                description = re.sub(r'<[^>]+>', '', description)
                description = html.unescape(description).strip()

            # Calculate duration
            if entry.get('duration'):
                duration_hours = entry['duration'] / 3600
            else:
                start = dateparser.parse(entry.get('start_time', ''))
                end = dateparser.parse(entry.get('end_time', ''))
                duration_hours = (end - start).total_seconds() / 3600 if start and end else 0

            # Extract date
            entry_date = entry.get('date', '')
            if not entry_date and entry.get('start_time'):
                entry_date = entry.get('start_time', '')[:10]

            # Format times as HH:MM (local time, extracted from ISO)
            start_time_str = ''
            end_time_str = ''

            if entry.get('start_time'):
                try:
                    start_dt = dateparser.parse(entry.get('start_time'))
                    if start_dt:
                        # Convert to local timezone
                        local_tz = pytz.timezone('America/Chicago')
                        if start_dt.tzinfo:
                            start_local = start_dt.astimezone(local_tz)
                        else:
                            start_local = local_tz.localize(start_dt)
                        start_time_str = start_local.strftime('%H:%M')
                except Exception:
                    pass

            if entry.get('end_time'):
                try:
                    end_dt = dateparser.parse(entry.get('end_time'))
                    if end_dt:
                        local_tz = pytz.timezone('America/Chicago')
                        if end_dt.tzinfo:
                            end_local = end_dt.astimezone(local_tz)
                        else:
                            end_local = local_tz.localize(end_dt)
                        end_time_str = end_local.strftime('%H:%M')
                except Exception:
                    pass

            writer.writerow([
                entry_date,
                start_time_str,
                end_time_str,
                f"{duration_hours:.2f}",
                task_name,
                description
            ])

        # Footer
        writer.writerow([])  # Blank line
        writer.writerow(['Expenses', f"${expenses:,.2f}"])

        return output.getvalue()

    def export_invoice_paymo_format(self, invoice_number: str, strict: bool = True,
                                     tolerance: float = 0.05) -> str:
        """
        Export timesheet in EXACT Paymo export format with all standard columns.

        Columns match Paymo's native export:
        User, Internal User Id, Project, Internal Project Id, Project Description,
        Tasklist, Internal Tasklist Id, Task, Internal Task Id, Start Time, End Time,
        Worked Time, Decimal Hours, Time In Seconds

        Args:
            invoice_number: Invoice number (e.g., 'INV-20260331-241')
            strict: If True (default), validate totals match invoice
            tolerance: Allowed percentage difference for validation (default 5%)

        Returns:
            CSV content in exact Paymo format
        """
        import csv
        import io
        import html
        import re
        import time

        # Find invoice by number
        invoice = self.find_invoice_by_number(invoice_number)
        if not invoice:
            raise ValueError(f"Invoice not found: {invoice_number}")

        invoice_id = invoice.get('id')

        # Get invoice with items
        response = self._request('GET', f'invoices/{invoice_id}?include=invoiceitems')
        invoice = response.get('invoices', [{}])[0]
        invoice_items = invoice.get('invoiceitems', [])

        # Get invoice item IDs
        invoice_item_ids = set(item.get('id') for item in invoice_items if item.get('id'))

        # Get entries for this invoice
        inv_date = invoice.get('date', '')
        if inv_date:
            inv_dt = datetime.strptime(inv_date, '%Y-%m-%d')
            start_date = (inv_dt - timedelta(days=90)).strftime('%Y-%m-%d')
            end_date = inv_date
        else:
            now = datetime.now()
            start_date = (now - timedelta(days=90)).strftime('%Y-%m-%d')
            end_date = now.strftime('%Y-%m-%d')

        all_entries = self.get_entries(start_date, end_date)
        entries = [e for e in all_entries if e.get('invoice_item_id') in invoice_item_ids]

        # Sort entries by date and time (chronological in local timezone)
        def get_entry_sort_key(entry):
            entry_date = entry.get('date', '')
            if not entry_date and entry.get('start_time'):
                entry_date = entry.get('start_time', '')[:10]
            start_time_sort = '00:00'
            if entry.get('start_time'):
                try:
                    start_dt = dateparser.parse(entry.get('start_time'))
                    if start_dt:
                        local_tz = pytz.timezone('America/Chicago')
                        if start_dt.tzinfo:
                            start_local = start_dt.astimezone(local_tz)
                        else:
                            start_local = local_tz.localize(start_dt)
                        start_time_sort = start_local.strftime('%H:%M')
                except Exception:
                    pass
            return (entry_date, start_time_sort)

        entries.sort(key=get_entry_sort_key)

        # Build caches for tasks, projects, tasklists, users
        task_cache = {}
        project_cache = {}
        tasklist_cache = {}
        user_cache = {}

        unique_task_ids = set(e.get('task_id') for e in entries if e.get('task_id'))
        unique_project_ids = set(e.get('project_id') for e in entries if e.get('project_id'))
        unique_user_ids = set(e.get('user_id') for e in entries if e.get('user_id'))

        # Fetch projects
        try:
            projects = self.get_projects(active_only=False)
            for p in projects:
                project_cache[p.get('id')] = p
        except Exception:
            pass

        # Fetch tasks (and get tasklist info)
        for task_id in unique_task_ids:
            try:
                time.sleep(2)
                task_response = self._request('GET', f'tasks/{task_id}')
                task_data = task_response.get('tasks', [{}])[0] if 'tasks' in task_response else {}
                task_cache[task_id] = task_data

                # Get tasklist if not cached
                tasklist_id = task_data.get('tasklist_id')
                if tasklist_id and tasklist_id not in tasklist_cache:
                    try:
                        tl_response = self._request('GET', f'tasklists/{tasklist_id}')
                        tl_data = tl_response.get('tasklists', [{}])[0] if 'tasklists' in tl_response else {}
                        tasklist_cache[tasklist_id] = tl_data
                    except Exception:
                        tasklist_cache[tasklist_id] = {}
            except Exception as e:
                if '429' in str(e):
                    time.sleep(6)
                    try:
                        task_response = self._request('GET', f'tasks/{task_id}')
                        task_data = task_response.get('tasks', [{}])[0] if 'tasks' in task_response else {}
                        task_cache[task_id] = task_data
                    except Exception:
                        task_cache[task_id] = {}
                else:
                    task_cache[task_id] = {}

        # Fetch users
        for user_id in unique_user_ids:
            try:
                time.sleep(1)
                user_response = self._request('GET', f'users/{user_id}')
                user_data = user_response.get('users', [{}])[0] if 'users' in user_response else {}
                user_cache[user_id] = user_data
            except Exception:
                user_cache[user_id] = {}

        # Calculate total hours for validation
        total_hours = 0
        for entry in entries:
            if entry.get('duration'):
                total_hours += entry['duration'] / 3600
            elif entry.get('start_time') and entry.get('end_time'):
                start = dateparser.parse(entry.get('start_time', ''))
                end = dateparser.parse(entry.get('end_time', ''))
                if start and end:
                    total_hours += (end - start).total_seconds() / 3600

        # Get hourly rate from first project for validation
        hourly_rate = 0
        if entries and entries[0].get('project_id'):
            proj = project_cache.get(entries[0].get('project_id'), {})
            hourly_rate = proj.get('price_per_hour', 0) or 0

        # Strict validation: compare against invoice's fee lines (not the
        # total, which includes expenses). See export_invoice_formatted for
        # the same fix and the reason.
        invoice_total = float(invoice.get('total') or 0)
        financials = self.get_invoice_financials(invoice_id)
        fees = financials['fees']
        expenses = financials['expenses']
        calculated_fees = total_hours * hourly_rate if hourly_rate else 0

        if strict and hourly_rate and fees > 0:
            fee_ratio = calculated_fees / fees if fees else 0
            if fee_ratio < (1 - tolerance) or calculated_fees > fees * (1 + tolerance):
                missing = fees - calculated_fees
                raise ValueError(
                    f"Linked time entries don't cover invoice fees: "
                    f"{total_hours:.2f} hrs × ${hourly_rate}/hr = ${calculated_fees:,.2f} "
                    f"vs invoice fees ${fees:,.2f} (expenses ${expenses:,.2f}, total ${invoice_total:,.2f}). "
                    f"Missing ${missing:,.2f} of fees — likely time entries billed on this "
                    f"invoice aren't linked via invoice_item_id, or a different rate was used. "
                    f"Use export_paymo_timesheet(start_date, end_date) for date-range export instead."
                )

        # Build CSV output in Paymo format
        output = io.StringIO()
        writer = csv.writer(output)

        # Header - exact Paymo format
        writer.writerow([
            'User', 'Internal User Id', 'Project', 'Internal Project Id',
            'Project Description', 'Tasklist', 'Internal Tasklist Id',
            'Task', 'Internal Task Id', 'Start Time', 'End Time',
            'Worked Time', 'Decimal Hours', 'Time In Seconds'
        ])

        # Data rows
        for entry in entries:
            user_id = entry.get('user_id')
            user = user_cache.get(user_id, {})
            user_name = user.get('name', '')

            project_id = entry.get('project_id')
            project = project_cache.get(project_id, {})
            project_name = project.get('name', '')
            project_description = project.get('description', '') or ''

            task_id = entry.get('task_id')
            task = task_cache.get(task_id, {})
            task_name = task.get('name', '')
            tasklist_id = task.get('tasklist_id')

            tasklist = tasklist_cache.get(tasklist_id, {})
            tasklist_name = tasklist.get('name', '')

            # Times
            start_time = entry.get('start_time', '')
            end_time = entry.get('end_time', '')

            # Duration calculations
            if entry.get('duration'):
                time_in_seconds = entry['duration']
            elif start_time and end_time:
                start_dt = dateparser.parse(start_time)
                end_dt = dateparser.parse(end_time)
                time_in_seconds = int((end_dt - start_dt).total_seconds()) if start_dt and end_dt else 0
            else:
                time_in_seconds = 0

            decimal_hours = time_in_seconds / 3600

            # Worked time as HH:MM:SS
            hours = int(time_in_seconds // 3600)
            minutes = int((time_in_seconds % 3600) // 60)
            seconds = int(time_in_seconds % 60)
            worked_time = f"{hours:02d}:{minutes:02d}:{seconds:02d}"

            writer.writerow([
                user_name,
                user_id or '',
                project_name,
                project_id or '',
                project_description,
                tasklist_name,
                tasklist_id or '',
                task_name,
                task_id or '',
                start_time,
                end_time,
                worked_time,
                f"{decimal_hours:.2f}",
                time_in_seconds
            ])

        return output.getvalue()

    def export_timesheet_csv(self, start_date: str, end_date: str,
                            project_id: Optional[int] = None) -> str:
        """
        Export timesheet to CSV format by fetching entries and formatting

        Args:
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
            project_id: Optional project filter

        Returns:
            CSV content as string
        """
        import csv
        import io
        import html
        import time

        # Get all entries for date range
        entries = self.get_entries(start_date, end_date)

        # Filter by project if specified
        if project_id:
            entries = [e for e in entries if e.get('project_id') == project_id]

        # Sort entries by start date (earliest first)
        def get_entry_sort_key(entry):
            # Use start_time if available, otherwise use date
            if entry.get('start_time'):
                return entry.get('start_time')
            elif entry.get('date'):
                return entry.get('date')
            else:
                # Fallback to entry ID if no date info
                return str(entry.get('id', 0)).zfill(20)

        entries.sort(key=get_entry_sort_key)

        # Build task cache - fetch all unique tasks upfront
        task_cache = {}
        unique_task_ids = set(e.get('task_id') for e in entries if e.get('task_id'))

        for task_id in unique_task_ids:
            try:
                time.sleep(2)  # 2 second delay to avoid rate limits
                task_response = self._request('GET', f'tasks/{task_id}')
                task_data = task_response.get('tasks', [{}])[0] if 'tasks' in task_response else {}
                task_cache[task_id] = task_data.get('name', '')
            except Exception as e:
                # If we hit a rate limit, wait and retry once
                if '429' in str(e):
                    console.print(f"Rate limit hit, waiting 6 seconds...")
                    time.sleep(6)
                    try:
                        task_response = self._request('GET', f'tasks/{task_id}')
                        task_data = task_response.get('tasks', [{}])[0] if 'tasks' in task_response else {}
                        task_cache[task_id] = task_data.get('name', '')
                    except Exception as retry_err:
                        console.print(f"Warning: Failed to fetch task {task_id} after retry: {retry_err}")
                        task_cache[task_id] = ''
                else:
                    console.print(f"Warning: Failed to fetch task {task_id}: {e}")
                    task_cache[task_id] = ''

        # Create CSV
        output = io.StringIO()
        writer = csv.writer(output)

        # Header
        writer.writerow(['Date', 'Start Time', 'End Time', 'Duration (hours)',
                        'Task', 'Description', 'Billed', 'Entry ID'])

        # Rows
        for entry in entries:
            # Get task name from cache
            task_id = entry.get('task_id')
            task_name = task_cache.get(task_id, '') if task_id else ''

            # Clean description (strip HTML tags and decode entities)
            description = entry.get('description', '')
            if description:
                # Remove HTML tags
                import re
                description = re.sub(r'<[^>]+>', '', description)
                # Decode HTML entities
                description = html.unescape(description)
                description = description.strip()

            # Calculate duration
            if entry.get('duration'):
                duration_hours = entry['duration'] / 3600
            else:
                start = dateparser.parse(entry.get('start_time', ''))
                end = dateparser.parse(entry.get('end_time', ''))
                duration_hours = (end - start).total_seconds() / 3600 if start and end else 0

            # Extract date from start_time if date field is empty
            entry_date = entry.get('date', '')
            if not entry_date and entry.get('start_time'):
                entry_date = entry.get('start_time', '')[:10]

            writer.writerow([
                entry_date,
                entry.get('start_time', ''),
                entry.get('end_time', ''),
                f"{duration_hours:.2f}",
                task_name,
                description,
                'Yes' if entry.get('billed') else 'No',
                entry.get('id', '')
            ])

        return output.getvalue()

    def find_project_by_name(self, name: str) -> Optional[Dict]:
        """Find project by partial name match (case-insensitive)"""
        projects = self.get_projects()
        name_lower = name.lower()

        for project in projects:
            if name_lower in project.get('name', '').lower():
                return project

        return None

    def find_task_by_name(self, project_id: int, name: str) -> Optional[Dict]:
        """Find task within project by name"""
        tasks = self.get_tasks(project_id)
        name_lower = name.lower()

        for task in tasks:
            if name_lower in task.get('name', '').lower():
                return task

        return None


class TimesheetProcessor:
    """Process timesheet YAML and create Paymo entries"""

    def __init__(self, client: PaymoClient, config: Dict):
        self.client = client
        self.config = config
        self.default_tz = pytz.timezone(config.get('timezone', 'America/Chicago'))

    def load_timesheet(self, filepath: str) -> Dict:
        """Load and validate timesheet YAML"""
        with open(filepath, 'r') as f:
            data = yaml.safe_load(f)

        # Validate required fields
        if 'entries' not in data:
            raise ValueError("Timesheet must have 'entries' field")

        return data

    def resolve_project_task(self, matter: str) -> Tuple[int, int]:
        """Resolve matter name to (project_id, task_id)"""
        # First check config mappings
        projects_config = self.config.get('projects', {})

        if matter in projects_config:
            project_id = projects_config[matter].get('project_id')
            task_id = projects_config[matter].get('task_id')
            return project_id, task_id

        # Otherwise search by name
        project = self.client.find_project_by_name(matter)
        if not project:
            raise ValueError(f"Could not find project matching '{matter}'")

        project_id = project['id']

        # Get first task in project (or could prompt user)
        tasks = self.client.get_tasks(project_id)
        if not tasks:
            raise ValueError(f"Project '{project['name']}' has no tasks")

        task_id = tasks[0]['id']

        console.print(f"[yellow]Using project: {project['name']} (ID: {project_id})[/yellow]")
        console.print(f"[yellow]Using task: {tasks[0]['name']} (ID: {task_id})[/yellow]")

        return project_id, task_id

    def convert_to_utc(self, date: str, time: str, tz: str) -> str:
        """Convert local datetime to UTC ISO format"""
        timezone = pytz.timezone(tz) if tz else self.default_tz

        # Parse date and time
        dt_str = f"{date} {time}"
        local_dt = datetime.strptime(dt_str, '%Y-%m-%d %H:%M')

        # Localize and convert to UTC
        local_dt = timezone.localize(local_dt)
        utc_dt = local_dt.astimezone(pytz.UTC)

        return utc_dt.strftime('%Y-%m-%dT%H:%M:%SZ')

    def process_entry(self, entry: Dict, task_id: int) -> Dict:
        """Convert timesheet entry to Paymo API format"""
        # Allow entry to override task_id
        entry_task_id = entry.get('task_id', task_id)
        api_entry = {'task_id': entry_task_id}

        # Get timezone
        tz = entry.get('timezone', self.config.get('timezone', 'America/Chicago'))

        # Handle two formats: (start_time, end_time) or (duration_hours)
        if 'start_time' in entry and 'end_time' in entry:
            # Convert to UTC
            api_entry['start_time'] = self.convert_to_utc(
                entry['date'], entry['start_time'], tz
            )
            api_entry['end_time'] = self.convert_to_utc(
                entry['date'], entry['end_time'], tz
            )
        elif 'duration_hours' in entry:
            # Use date + duration
            api_entry['date'] = entry['date']
            api_entry['duration'] = int(entry['duration_hours'] * 3600)
        else:
            raise ValueError(f"Entry must have either (start_time, end_time) or duration_hours: {entry}")

        # Add description
        if 'description' in entry:
            api_entry['description'] = entry['description']

        # Add billed flag if specified
        if 'billed' in entry:
            api_entry['billed'] = entry['billed']

        return api_entry

    def calculate_duration(self, entry: Dict) -> float:
        """Calculate duration in hours for preview"""
        if 'duration_hours' in entry:
            return entry['duration_hours']

        # Calculate from start/end times
        tz = entry.get('timezone', self.config.get('timezone', 'America/Chicago'))
        timezone = pytz.timezone(tz)

        start_str = f"{entry['date']} {entry['start_time']}"
        end_str = f"{entry['date']} {entry['end_time']}"

        start_dt = datetime.strptime(start_str, '%Y-%m-%d %H:%M')
        end_dt = datetime.strptime(end_str, '%Y-%m-%d %H:%M')

        start_dt = timezone.localize(start_dt)
        end_dt = timezone.localize(end_dt)

        duration = (end_dt - start_dt).total_seconds() / 3600
        return duration

    def preview(self, filepath: str) -> List[Dict]:
        """Preview entries without creating"""
        data = self.load_timesheet(filepath)
        entries = data['entries']
        matter = data.get('matter', 'Unknown')
        rate = data.get('rate', 0)

        # Create table
        table = Table(title=f"Timesheet Preview: {matter}")
        table.add_column("Date", style="cyan")
        table.add_column("Time", style="magenta")
        table.add_column("Duration", style="green")
        table.add_column("Hours", style="yellow")
        table.add_column("Description", style="white")

        total_hours = 0

        for entry in entries:
            date = entry['date']
            duration_hours = self.calculate_duration(entry)
            total_hours += duration_hours

            # Format time range or duration
            if 'start_time' in entry:
                time_str = f"{entry['start_time']}-{entry['end_time']}"
            else:
                time_str = "—"

            # Format duration
            hours = int(duration_hours)
            minutes = int((duration_hours - hours) * 60)
            duration_str = f"{hours}:{minutes:02d}"

            description = entry.get('description', '')
            if len(description) > 50:
                description = description[:47] + "..."

            table.add_row(
                date,
                time_str,
                duration_str,
                f"{duration_hours:.2f}",
                description
            )

        console.print(table)

        # Summary
        total_billing = total_hours * rate if rate else 0
        console.print(f"\n[bold]Total: {total_hours:.2f} hours[/bold]", end="")
        if rate:
            console.print(f" [bold green](${total_billing:,.2f} at ${rate}/hr)[/bold green]")
        else:
            console.print()

        return entries

    def submit(self, filepath: str, dry_run: bool = False, auto_confirm: bool = False) -> List[Dict]:
        """Create all entries from timesheet"""
        data = self.load_timesheet(filepath)
        entries = data['entries']
        matter = data.get('matter')

        if not matter:
            raise ValueError("Timesheet must specify 'matter' field")

        # Resolve project and task
        console.print(f"\n[bold]Resolving project for matter: {matter}[/bold]")
        project_id, task_id = self.resolve_project_task(matter)

        # Preview first
        console.print(f"\n[bold]Preview of entries to create:[/bold]")
        self.preview(filepath)

        if dry_run:
            console.print("\n[yellow]Dry run - no entries created[/yellow]")
            return []

        # Confirm
        if not auto_confirm:
            if not click.confirm("\nCreate these entries in Paymo?"):
                console.print("[yellow]Cancelled[/yellow]")
                return []
        else:
            console.print("\n[green]Auto-confirmed - proceeding with creation[/green]")

        # Create entries as batch
        console.print(f"\n[bold]Creating {len(entries)} entries in batch...[/bold]")

        try:
            # Process all entries
            api_entries = [self.process_entry(entry, task_id) for entry in entries]

            # Try batch creation first
            try:
                result = self.client.create_entries_batch(api_entries)
                console.print(f"[green]✓ Successfully created {len(entries)} entries in one API call[/green]")
                return result
            except Exception as batch_error:
                # If batch fails, fall back to individual creation
                console.print(f"[yellow]Batch creation failed, trying individual entries...[/yellow]")
                console.print(f"[yellow]Error: {batch_error}[/yellow]")

                created = []
                for i, (entry, api_entry) in enumerate(zip(entries, api_entries), 1):
                    try:
                        console.print(f"[{i}/{len(entries)}] Creating entry for {entry['date']}...", end=" ")
                        result = self.client.create_entry(**api_entry)
                        created.append(result)
                        console.print("[green]✓[/green]")

                        # Add delay between calls to avoid rate limiting
                        if i < len(entries):
                            time.sleep(2)
                    except requests.exceptions.HTTPError as e:
                        if e.response.status_code == 429:
                            retry_after = getattr(e, 'retry_after', 60)
                            console.print(f"[yellow]⏳ Rate limited, waiting {retry_after}s...[/yellow]")
                            time.sleep(retry_after)
                            # Retry this entry
                            try:
                                result = self.client.create_entry(**api_entry)
                                created.append(result)
                                console.print("[green]✓ (after retry)[/green]")
                            except Exception as retry_error:
                                console.print(f"[red]✗ Retry failed: {retry_error}[/red]")
                        else:
                            console.print(f"[red]✗ Error: {e}[/red]")
                    except Exception as e:
                        console.print(f"[red]✗ Error: {e}[/red]")

                console.print(f"\n[bold green]Successfully created {len(created)} entries[/bold green]")
                return created

        except Exception as e:
            console.print(f"[red]Error processing entries: {e}[/red]")
            return []


def resolve_month(spec: str, today: Optional[datetime] = None) -> Tuple[str, str]:
    """Resolve a loose month spec to a (start_date, end_date) YYYY-MM-DD pair.

    Accepts:
      - "last" / "previous"      -> previous calendar month
      - "current" / "this"       -> current calendar month
      - "YYYY-MM"                -> that specific month
      - "Month" / "Month YYYY"   -> parsed via dateutil (bare month name
                                    assumes current year; if that lands in
                                    the future, rolls back one year — so
                                    "June" said in Feb 2027 means June 2026)

    Returns (start_date, end_date) — inclusive first/last day of the resolved
    month, both YYYY-MM-DD. Case-insensitive.

    Raises ValueError if the spec can't be parsed.
    """
    if not spec or not str(spec).strip():
        raise ValueError("month spec is empty")
    s = str(spec).strip().lower()
    now = today or datetime.now()

    def _month_bounds(year: int, month: int) -> Tuple[str, str]:
        start = datetime(year, month, 1)
        # Last day: jump to next month's 1st, subtract a day.
        if month == 12:
            next_start = datetime(year + 1, 1, 1)
        else:
            next_start = datetime(year, month + 1, 1)
        end = next_start - timedelta(days=1)
        return start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d')

    if s in ('last', 'previous', 'prev'):
        y, m = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
        return _month_bounds(y, m)
    if s in ('current', 'this'):
        return _month_bounds(now.year, now.month)

    # ISO YYYY-MM
    if len(s) == 7 and s[4] == '-' and s[:4].isdigit() and s[5:].isdigit():
        y, m = int(s[:4]), int(s[5:])
        if not 1 <= m <= 12:
            raise ValueError(f"month spec {spec!r} has invalid month {m}")
        return _month_bounds(y, m)

    # Natural language: "June", "Jun 2026", "June 2026"
    try:
        parsed = dateparser.parse(spec, default=datetime(now.year, 1, 1))
    except (ValueError, TypeError) as e:
        raise ValueError(f"could not parse month spec {spec!r}: {e}")
    y, m = parsed.year, parsed.month
    # Bare month name with no year: dateparser used our default year. If that
    # lands in the future, the user almost certainly meant the most recent
    # occurrence of that month → roll back one year.
    digit_run = ''.join(c for c in spec if c.isdigit())
    has_year = len(digit_run) >= 4
    if not has_year and datetime(y, m, 1) > datetime(now.year, now.month, 1):
        y -= 1
    return _month_bounds(y, m)


def load_config() -> Dict:
    """Load configuration from ~/.mcp-config/paymo/ and ~/.mcp-auth/paymo/"""
    config_dir = Path.home() / '.mcp-config' / 'paymo'
    auth_dir = Path.home() / '.mcp-auth' / 'paymo'

    # Start with defaults
    config = {
        'timezone': 'America/Chicago',
        'projects': {}
    }

    # Load non-sensitive config
    config_path = config_dir / 'config.json'
    if config_path.exists():
        with open(config_path, 'r') as f:
            config.update(json.load(f))

    # Load API key from auth
    auth_path = auth_dir / 'auth.json'
    if auth_path.exists():
        with open(auth_path, 'r') as f:
            auth_data = json.load(f)
            config['api_key'] = auth_data.get('api_key')
    else:
        console.print(f"[yellow]Warning: Auth file not found at {auth_path}[/yellow]")
        console.print("[yellow]Will prompt for API key if needed[/yellow]")

    return config


@click.group()
def cli():
    """Paymo Timesheet Automation Tool"""
    pass


@cli.command()
def list_projects():
    """List all active Paymo projects"""
    config = load_config()
    api_key = config.get('api_key') or click.prompt('Paymo API Key', hide_input=True)

    client = PaymoClient(api_key)
    projects = client.get_projects()

    table = Table(title="Paymo Projects")
    table.add_column("ID", style="cyan")
    table.add_column("Name", style="white")
    table.add_column("Client", style="magenta")
    table.add_column("Active", style="green")

    for project in projects:
        table.add_row(
            str(project['id']),
            project.get('name', ''),
            project.get('client_name', ''),
            "✓" if project.get('active') else "✗"
        )

    console.print(table)


@cli.command()
@click.option('--project-id', type=int, required=True, help='Project ID')
def list_tasks(project_id: int):
    """List tasks for a project"""
    config = load_config()
    api_key = config.get('api_key') or click.prompt('Paymo API Key', hide_input=True)

    client = PaymoClient(api_key)
    tasks = client.get_tasks(project_id)

    table = Table(title=f"Tasks for Project {project_id}")
    table.add_column("ID", style="cyan")
    table.add_column("Name", style="white")
    table.add_column("Billable", style="green")

    for task in tasks:
        table.add_row(
            str(task['id']),
            task.get('name', ''),
            "✓" if task.get('billable') else "✗"
        )

    console.print(table)


@cli.command()
@click.option('--start', help='Start date (YYYY-MM-DD)')
@click.option('--end', help='End date (YYYY-MM-DD)')
def list_entries(start: str, end: str):
    """List time entries for a date range"""
    config = load_config()
    api_key = config.get('api_key') or click.prompt('Paymo API Key', hide_input=True)

    client = PaymoClient(api_key)
    entries = client.get_entries(start, end)

    table = Table(title=f"Time Entries ({start} to {end})")
    table.add_column("ID", style="cyan")
    table.add_column("Date", style="magenta")
    table.add_column("Duration", style="green")
    table.add_column("Description", style="white")

    total_seconds = 0

    for entry in entries:
        entry_id = str(entry['id'])
        date = entry.get('date', '')

        # Calculate duration
        if entry.get('duration'):
            duration_sec = entry['duration']
        else:
            start_time = dateparser.parse(entry.get('start_time', ''))
            end_time = dateparser.parse(entry.get('end_time', ''))
            duration_sec = (end_time - start_time).total_seconds()

        total_seconds += duration_sec
        hours = int(duration_sec / 3600)
        minutes = int((duration_sec % 3600) / 60)
        duration_str = f"{hours}:{minutes:02d}"

        description = entry.get('description', '')[:50]

        table.add_row(entry_id, date, duration_str, description)

    console.print(table)

    total_hours = total_seconds / 3600
    console.print(f"\n[bold]Total: {total_hours:.2f} hours[/bold]")


@cli.command()
@click.argument('filepath', type=click.Path(exists=True))
def preview(filepath: str):
    """Preview timesheet entries without creating them"""
    config = load_config()
    api_key = config.get('api_key') or click.prompt('Paymo API Key', hide_input=True)

    client = PaymoClient(api_key)
    processor = TimesheetProcessor(client, config)

    processor.preview(filepath)


@cli.command()
@click.argument('filepath', type=click.Path(exists=True))
@click.option('--dry-run', is_flag=True, help='Preview only, do not create entries')
@click.option('--yes', '-y', is_flag=True, help='Skip confirmation prompt')
def submit(filepath: str, dry_run: bool, yes: bool):
    """Submit timesheet entries to Paymo"""
    config = load_config()
    api_key = config.get('api_key') or click.prompt('Paymo API Key', hide_input=True)

    client = PaymoClient(api_key)
    processor = TimesheetProcessor(client, config)

    processor.submit(filepath, dry_run=dry_run, auto_confirm=yes)


@cli.command()
@click.argument('entry_ids', nargs=-1, type=int, required=True)
@click.option('--yes', '-y', is_flag=True, help='Skip confirmation prompt')
def delete(entry_ids: tuple, yes: bool):
    """Delete time entries by ID"""
    config = load_config()
    api_key = config.get('api_key') or click.prompt('Paymo API Key', hide_input=True)

    client = PaymoClient(api_key)

    console.print(f"\n[bold red]About to delete {len(entry_ids)} entries:[/bold red]")
    for entry_id in entry_ids:
        console.print(f"  - Entry ID: {entry_id}")

    if not yes:
        if not click.confirm("\nAre you sure you want to delete these entries?"):
            console.print("[yellow]Cancelled[/yellow]")
            return

    deleted = 0
    for entry_id in entry_ids:
        try:
            console.print(f"Deleting entry {entry_id}...", end=" ")
            client.delete_entry(entry_id)
            console.print("[green]✓[/green]")
            deleted += 1
        except Exception as e:
            console.print(f"[red]✗ Error: {e}[/red]")

    console.print(f"\n[bold green]Successfully deleted {deleted} entries[/bold green]")


# Main entry point moved to end


@cli.command()
@click.option('--client-id', type=int, help='Filter by client ID')
def list_invoices(client_id: Optional[int]):
    """List Paymo invoices"""
    config = load_config()
    api_key = config.get('api_key') or click.prompt('Paymo API Key', hide_input=True)

    client = PaymoClient(api_key)
    invoices = client.get_invoices(client_id)

    table = Table(title="Paymo Invoices")
    table.add_column("ID", style="cyan")
    table.add_column("Number", style="white")
    table.add_column("Client", style="magenta")
    table.add_column("Amount", style="green")
    table.add_column("Date", style="yellow")
    table.add_column("Status", style="blue")

    for invoice in invoices:
        table.add_row(
            str(invoice.get('id', '')),
            invoice.get('number', ''),
            invoice.get('client_name', ''),
            f"${invoice.get('total', 0):,.2f}",
            invoice.get('date', ''),
            invoice.get('status', '')
        )

    console.print(table)


@cli.command()
@click.option('--start', required=True, help='Start date (YYYY-MM-DD)')
@click.option('--end', required=True, help='End date (YYYY-MM-DD)')
@click.option('--project-id', type=int, help='Filter by project ID')
@click.option('--output', '-o', help='Output file path')
def export_timesheet(start: str, end: str, project_id: Optional[int], output: Optional[str]):
    """Export timesheet to CSV"""
    config = load_config()
    api_key = config.get('api_key') or click.prompt('Paymo API Key', hide_input=True)

    client = PaymoClient(api_key)

    console.print(f"\n[bold]Exporting timesheet: {start} to {end}[/bold]")
    if project_id:
        console.print(f"[yellow]Project ID: {project_id}[/yellow]")

    try:
        csv_content = client.export_timesheet_csv(start, end, project_id)

        # Determine output filename
        if not output:
            output = f"paymo_timesheet_{start}_{end}.csv"

        # Save file
        with open(output, 'w') as f:
            f.write(csv_content)

        console.print(f"[green]✓ Exported to: {output}[/green]")
        console.print(f"[green]  Size: {len(csv_content):,} bytes[/green]")

    except Exception as e:
        console.print(f"[red]Error exporting timesheet: {e}[/red]")
        raise


# MCP Server Implementation
if MCP_AVAILABLE:
    mcp = FastMCP("Paymo Timesheet Manager")

    @mcp.tool()
    def list_paymo_clients(include_inactive: bool = False) -> List[Dict[str, Any]]:
        """
        List Paymo clients with essential details only

        Args:
            include_inactive: If True, includes inactive/archived clients (default: False)
        """
        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured in ~/.mcp-auth/paymo/auth.json")

        client = PaymoClient(api_key)
        clients = client.get_clients(active_only=not include_inactive)

        # Return only essential fields to minimize context usage
        return [{
            'id': c.get('id'),
            'name': c.get('name'),
            'active': c.get('active', True)
        } for c in clients]

    @mcp.tool()
    def create_paymo_client(
        name: str,
        address: Optional[str] = None,
        city: Optional[str] = None,
        state: Optional[str] = None,
        postal_code: Optional[str] = None,
        country: Optional[str] = None,
        phone: Optional[str] = None,
        email: Optional[str] = None,
        website: Optional[str] = None,
        fiscal_information: Optional[str] = None,
        active: bool = True
    ) -> Dict[str, Any]:
        """
        Create a new Paymo client with contact information

        Args:
            name: Client/company name (required, e.g., "Baker McKenzie")
            address: Street address
            city: City
            state: State/Province
            postal_code: ZIP/Postal code
            country: Country
            phone: Phone number
            email: Primary contact email
            website: Website URL
            fiscal_information: Tax ID or fiscal info
            active: Whether client is active (default: True)

        Returns:
            Created client details: id, name, email, active status
        """
        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured in ~/.mcp-auth/paymo/auth.json")

        client = PaymoClient(api_key)

        # Build kwargs for optional parameters
        kwargs = {'active': active}
        if address:
            kwargs['address'] = address
        if city:
            kwargs['city'] = city
        if state:
            kwargs['state'] = state
        if postal_code:
            kwargs['postal_code'] = postal_code
        if country:
            kwargs['country'] = country
        if phone:
            kwargs['phone'] = phone
        if email:
            kwargs['email'] = email
        if website:
            kwargs['website'] = website
        if fiscal_information:
            kwargs['fiscal_information'] = fiscal_information

        c = client.create_client(name, **kwargs)

        return {
            'id': c.get('id'),
            'name': c.get('name'),
            'email': c.get('email'),
            'active': c.get('active')
        }

    @mcp.tool()
    def list_paymo_projects(include_inactive: bool = False) -> List[Dict[str, Any]]:
        """
        List Paymo projects with essential details only

        Args:
            include_inactive: If True, includes archived/inactive projects (default: False)
        """
        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured in ~/.mcp-auth/paymo/auth.json")

        client = PaymoClient(api_key)
        projects = client.get_projects(active_only=not include_inactive)

        # Return essential fields for filtering/querying while removing noise
        # Keep: identification, status, billing fields
        # Remove: UI fields (color), internal IDs (workflow_id, budget_id), arrays (users, managers), timestamps
        return [{
            'id': p.get('id'),
            'name': p.get('name'),
            'code': p.get('code'),
            'client_id': p.get('client_id'),
            'client_name': p.get('client_name'),
            'active': p.get('active'),
            'billable': p.get('billable'),
            'price_per_hour': p.get('price_per_hour'),
            'flat_billing': p.get('flat_billing'),
            'invoiced': p.get('invoiced')
        } for p in projects]

    @mcp.tool()
    def create_paymo_project(
        name: str,
        client_id: int,
        code: Optional[str] = None,
        price_per_hour: Optional[float] = None,
        billable: bool = True,
        flat_billing: bool = False,
        active: bool = True,
        hourly_billing_mode: str = "project",
        adjustable_hours: bool = False
    ) -> Dict[str, Any]:
        """
        Create a new Paymo project

        Args:
            name: Project name (e.g., "MacKinnon v. Meta")
            client_id: Paymo client ID
            code: Short project code (e.g., "MVM")
            price_per_hour: Hourly billing rate (e.g., 675)
            billable: Whether project is billable (default: True)
            flat_billing: Use flat rate instead of hourly (default: False)
            active: Whether project is active (default: True)
            hourly_billing_mode: Billing mode - "project" or "task" (default: "project")
            adjustable_hours: Auto-adjust budget based on task budgets. Default False
                because True maps to adjust_price=True server-side, which hides the
                hourly rate in the Paymo UI gear view. Leave False unless you
                specifically want budget auto-adjustment.
        """
        # Convert parameters to proper types (MCP may pass strings)
        try:
            client_id = int(client_id)
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid client_id '{client_id}': {e}")
        if price_per_hour is not None:
            try:
                price_per_hour = float(price_per_hour)
            except (ValueError, TypeError) as e:
                raise ValueError(f"Invalid price_per_hour '{price_per_hour}': {e}")

        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured in ~/.mcp-auth/paymo/auth.json")

        client = PaymoClient(api_key)

        # Build kwargs for optional parameters
        kwargs = {
            'billable': billable,
            'flat_billing': flat_billing,
            'active': active,
            'hourly_billing_mode': hourly_billing_mode,
            'adjustable_hours': adjustable_hours,
            'adjust_price': adjustable_hours,  # explicit; defaults to False
        }
        if code:
            kwargs['code'] = code
        if price_per_hour is not None:
            kwargs['price_per_hour'] = price_per_hour

        p = client.create_project(name, client_id, **kwargs)

        return {
            'id': p.get('id'),
            'name': p.get('name'),
            'code': p.get('code'),
            'client_id': p.get('client_id'),
            'price_per_hour': p.get('price_per_hour'),
            'hourly_billing_mode': p.get('hourly_billing_mode'),
            'adjustable_hours': p.get('adjustable_hours'),
            'adjust_price': p.get('adjust_price'),
            'billable': p.get('billable'),
            'active': p.get('active')
        }

    @mcp.tool()
    def update_paymo_project(
        project_id: int,
        name: Optional[str] = None,
        code: Optional[str] = None,
        price_per_hour: Optional[float] = None,
        billable: Optional[bool] = None,
        flat_billing: Optional[bool] = None,
        active: Optional[bool] = None,
        hourly_billing_mode: Optional[str] = None,
        adjust_price: Optional[bool] = None
    ) -> Dict[str, Any]:
        """
        Update an existing Paymo project

        Args:
            project_id: Paymo project ID
            name: Project name
            code: Short project code
            price_per_hour: Hourly billing rate
            billable: Whether project is billable
            flat_billing: Use flat rate instead of hourly
            active: Whether project is active
            hourly_billing_mode: Billing mode - "project" or "task"
            adjust_price: Budget estimate adjusted automatically
        """
        # Convert parameters to proper types (MCP may pass strings)
        try:
            project_id = int(project_id)
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid project_id '{project_id}': {e}")
        if price_per_hour is not None:
            try:
                price_per_hour = float(price_per_hour)
            except (ValueError, TypeError) as e:
                raise ValueError(f"Invalid price_per_hour '{price_per_hour}': {e}")

        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured in ~/.mcp-auth/paymo/auth.json")

        client = PaymoClient(api_key)

        # Build payload with only provided values
        payload = {}
        if name is not None:
            payload['name'] = name
        if code is not None:
            payload['code'] = code
        if price_per_hour is not None:
            payload['price_per_hour'] = price_per_hour
        if billable is not None:
            payload['billable'] = billable
        if flat_billing is not None:
            payload['flat_billing'] = flat_billing
        if active is not None:
            payload['active'] = active
        if hourly_billing_mode is not None:
            payload['hourly_billing_mode'] = hourly_billing_mode
        if adjust_price is not None:
            payload['adjust_price'] = adjust_price

        p = client.update_project(project_id, **payload)

        return {
            'id': p.get('id'),
            'name': p.get('name'),
            'code': p.get('code'),
            'client_id': p.get('client_id'),
            'price_per_hour': p.get('price_per_hour'),
            'hourly_billing_mode': p.get('hourly_billing_mode'),
            'adjust_price': p.get('adjust_price'),
            'billable': p.get('billable'),
            'active': p.get('active')
        }

    @mcp.tool()
    def list_paymo_tasks(project_id: int) -> List[Dict[str, Any]]:
        """List tasks for a specific Paymo project with essential details only"""
        # Convert parameters to proper types (MCP may pass strings)
        project_id = int(project_id)

        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured")

        client = PaymoClient(api_key)
        tasks = client.get_tasks(project_id)

        # Return only essential fields to minimize context usage
        # Include description since it's often empty but useful when present
        return [{
            'id': t.get('id'),
            'name': t.get('name'),
            'description': t.get('description', ''),
            'billable': t.get('billable', True)
        } for t in tasks]

    @mcp.tool()
    def rename_paymo_task(task_id: int, name: str) -> Dict[str, Any]:
        """
        Rename a Paymo task

        Args:
            task_id: Paymo task ID
            name: New name for the task
        """
        # Convert parameters to proper types (MCP may pass strings)
        task_id = int(task_id)

        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured")

        client = PaymoClient(api_key)
        return client.update_task(task_id, name=name)

    @mcp.tool()
    def create_paymo_task(
        project_id: int,
        name: str,
        billable: bool = True
    ) -> Dict[str, Any]:
        """
        Create a new task in a Paymo project

        Args:
            project_id: Paymo project ID
            name: Task name (e.g., "Document Review")
            billable: Whether task is billable (default: True)
        """
        # Convert parameters to proper types (MCP may pass strings)
        project_id = int(project_id)

        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured")

        client = PaymoClient(api_key)
        result = client.create_task(project_id, name, billable)
        t = result.get('tasks', [{}])[0] if 'tasks' in result else result

        return {
            'id': t.get('id'),
            'name': t.get('name'),
            'project_id': t.get('project_id'),
            'billable': t.get('billable')
        }

    @mcp.tool()
    def create_paymo_entry(
        task_id: int,
        date: str,
        description: str,
        duration_hours: float = None,
        start_time: str = None,
        end_time: str = None,
        added_manually: bool = True,
        timezone: str = "America/Chicago"
    ) -> Dict[str, Any]:
        """
        Create a single time entry in Paymo.

        Provide EITHER:
        - duration_hours for simple duration logging (e.g., "3 hours on this task"), OR
        - start_time + end_time for precise time blocks (e.g., "12:30 PM - 3:21 PM")

        Args:
            task_id: Paymo task ID
            date: Date in YYYY-MM-DD format
            description: Entry description
            duration_hours: Hours worked (use this OR start_time/end_time)
            start_time: Start time in HH:MM 24-hour format (use with end_time)
            end_time: End time in HH:MM 24-hour format (use with start_time)
            added_manually: Entry type - True for manual/form entry (default), False for timer-tracked
            timezone: IANA timezone for start/end times (default: America/Chicago)
        """
        # Convert parameters to proper types (MCP may pass strings)
        task_id = int(task_id)
        if duration_hours is not None:
            duration_hours = float(duration_hours)

        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured")

        client = PaymoClient(api_key)

        # Build payload based on what was provided
        payload = {
            'task_id': task_id,
            'date': date,
            'description': description,
            'added_manually': added_manually
        }

        if start_time and end_time:
            # Precise time block entry - Paymo calculates duration from start/end
            # Convert local timezone to UTC for API
            from zoneinfo import ZoneInfo
            local_tz = ZoneInfo(timezone)
            utc = ZoneInfo("UTC")

            start_dt = datetime.strptime(f"{date} {start_time}", "%Y-%m-%d %H:%M")
            start_dt = start_dt.replace(tzinfo=local_tz).astimezone(utc)

            end_dt = datetime.strptime(f"{date} {end_time}", "%Y-%m-%d %H:%M")
            end_dt = end_dt.replace(tzinfo=local_tz).astimezone(utc)

            payload['start_time'] = start_dt.strftime("%Y-%m-%dT%H:%M:%S")
            payload['end_time'] = end_dt.strftime("%Y-%m-%dT%H:%M:%S")
        elif duration_hours is not None:
            # Simple duration entry
            payload['duration'] = int(duration_hours * 3600)
        else:
            raise ValueError("Provide either duration_hours OR both start_time and end_time")

        return client.create_entry(**payload)

    @mcp.tool()
    def submit_paymo_timesheet(yaml_content: str) -> Dict[str, Any]:
        """
        Submit a complete timesheet from YAML content

        Args:
            yaml_content: YAML timesheet content with entries

        Returns:
            Summary of created entries
        """
        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured")

        # Parse YAML
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_file = f.name

        try:
            client = PaymoClient(api_key)
            processor = TimesheetProcessor(client, config)
            created = processor.submit(yaml_file, auto_confirm=True)

            return {
                "success": True,
                "entries_created": len(created),
                "entries": created
            }
        finally:
            os.unlink(yaml_file)

    @mcp.tool()
    def export_paymo_timesheet(
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        project_id: Optional[int] = None,
        month: Optional[str] = None,
    ) -> str:
        """
        Export timesheet by DATE RANGE as CSV. Use this as a fallback when
        export_invoice_timesheet() fails or when you need entries by date
        regardless of invoice linkage.

        WHEN TO USE THIS TOOL:
        - User asks for "timesheet for March" or "entries from last month"
        - User needs entries by date range, not by invoice
        - export_invoice_timesheet() failed validation (entries on different invoice)
        - User wants ALL entries in a period, regardless of billing status

        WHEN TO USE export_invoice_timesheet() INSTEAD:
        - User asks for "timesheet for invoice X" or "INV-..."
        - User wants only entries billed on a specific invoice

        Args:
            start_date: Start date (YYYY-MM-DD) — omit if using `month`
            end_date: End date (YYYY-MM-DD) — omit if using `month`
            project_id: Optional - filter to specific project ID
            month: Shorthand for a calendar month; overrides start/end.
                Accepts "last", "current", "YYYY-MM", "June", "June 2026", etc.

        Returns:
            CSV content with columns: Date, Start Time, End Time, Duration (hours),
            Task, Description, Billed, Entry ID

        Examples:
            export_paymo_timesheet(month="last")
            export_paymo_timesheet(month="June", project_id=12345)
            export_paymo_timesheet("2026-03-01", "2026-03-31")
        """
        if month:
            if start_date or end_date:
                raise ValueError("Pass either `month` or `start_date`+`end_date`, not both")
            start_date, end_date = resolve_month(month)
        if not (start_date and end_date):
            raise ValueError("Provide `month` or both `start_date` and `end_date`")

        # Convert parameters to proper types (MCP may pass strings)
        if project_id is not None:
            project_id = int(project_id)

        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured")

        client = PaymoClient(api_key)
        return client.export_timesheet_csv(start_date, end_date, project_id)

    @mcp.tool()
    def export_glimpse_timesheet(
        project_id: int,
        keystone_project_code: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        month: Optional[str] = None,
        billable: bool = True,
        include_billed: bool = True,
    ) -> str:
        """
        Export a Keystone/Glimpse-format CSV timesheet for a date range + project.

        Columns match Keystone's csv-template (submitted through the Glimpse
        portal):
            Date, Duration Hours, Comment, Project Code, Billable

        Format details (verified against Keystone's template):
          - Date:            M/D/YYYY (no zero padding, US format)
          - Duration Hours:  decimal, 2dp (e.g. "1.00", "0.80")
          - Comment:         entry description
          - Project Code:    the code Keystone gave you — applied to every row
          - Billable:        "true" / "false" (lowercase)

        Every value is double-quoted (matches Keystone's own template).
        Entries are sorted chronologically. Glimpse keys off the Project Code
        so the caller MUST get that from Keystone before this can be submitted.

        Args:
            project_id: Paymo project ID
            keystone_project_code: The code Keystone gave you for this matter
            start_date: YYYY-MM-DD — omit if using `month`
            end_date: YYYY-MM-DD — omit if using `month`
            month: Shorthand for a calendar month; overrides start/end.
                Accepts "last", "current", "YYYY-MM", "June", "June 2026", etc.
            billable: Value for the Billable column (default True)
            include_billed: If False, only include unbilled entries (default True)

        Returns:
            CSV content as a string, ready to save + upload to Glimpse.

        Examples:
            export_glimpse_timesheet(project_id=3482327,
                keystone_project_code="Meta - McCarthy Tétrault LLP - Clare v Meta - NF",
                month="June")
            export_glimpse_timesheet(project_id=3482327,
                keystone_project_code="...", month="last")
        """
        import csv
        import io as _io
        import html as _html

        if month:
            if start_date or end_date:
                raise ValueError("Pass either `month` or `start_date`+`end_date`, not both")
            start_date, end_date = resolve_month(month)
        if not (start_date and end_date):
            raise ValueError("Provide `month` or both `start_date` and `end_date`")

        project_id = int(project_id)

        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured")

        client = PaymoClient(api_key)

        # Pull entries and filter to the project. Match export_paymo_timesheet's
        # date+project semantics so both tools return the same row set.
        entries = client.get_entries(start_date, end_date)
        entries = [e for e in entries if e.get('project_id') == project_id]
        if not include_billed:
            entries = [e for e in entries if not e.get('billed')]

        # Sort chronologically in local time, mirroring export_invoice_paymo_format.
        local_tz = pytz.timezone(config.get('timezone', 'America/Chicago'))

        def _entry_local_dt(e):
            st = e.get('start_time') or ''
            if st:
                try:
                    dt = dateparser.parse(st)
                    if dt.tzinfo:
                        return dt.astimezone(local_tz)
                    return local_tz.localize(dt)
                except Exception:
                    pass
            d = e.get('date') or ''
            if d:
                try:
                    return local_tz.localize(datetime.strptime(d, '%Y-%m-%d'))
                except Exception:
                    pass
            return datetime.min.replace(tzinfo=local_tz)

        entries.sort(key=_entry_local_dt)

        buf = _io.StringIO()
        writer = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator='\n')
        writer.writerow(['Date', 'Duration Hours', 'Comment', 'Project Code', 'Billable'])

        billable_str = 'true' if billable else 'false'
        for e in entries:
            local_dt = _entry_local_dt(e)
            date_str = f"{local_dt.month}/{local_dt.day}/{local_dt.year}"
            duration_sec = e.get('duration')
            if duration_sec is None and e.get('start_time') and e.get('end_time'):
                s = dateparser.parse(e['start_time'])
                x = dateparser.parse(e['end_time'])
                duration_sec = int((x - s).total_seconds())
            hours = round((duration_sec or 0) / 3600, 2)
            # Paymo round-trips descriptions through HTML entities (e.g.
            # &#039; for apostrophe, &#43; for plus). Decode before writing
            # so the CSV that reaches Glimpse contains readable text.
            comment = _html.unescape((e.get('description') or '').strip())
            writer.writerow([
                date_str,
                f"{hours:.2f}",
                comment,
                keystone_project_code,
                billable_str,
            ])

        return buf.getvalue()

    @mcp.tool()
    def list_paymo_invoices(
        client_id: Optional[int] = None,
        status: Optional[str] = None,
        include_financials: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        List Paymo invoices with essential details only.

        Args:
            client_id: Filter by client ID
            status: Filter by status (sent, viewed, paid)
            include_financials: If True, also fetch each invoice's line
                items and split `total` into `fees` and `expenses`. This
                costs one extra GET per invoice (plus expense scan), so
                only enable when you specifically need the breakdown.
        """
        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured")

        client = PaymoClient(api_key)
        invoices = client.get_invoices(client_id, status)

        # Return only essential fields to minimize context usage
        # Keep: identification, client, amounts, dates, status
        # Remove: internal IDs, arrays, detailed line items
        result: List[Dict[str, Any]] = []
        for inv in invoices:
            row: Dict[str, Any] = {
                'id': inv.get('id'),
                'number': inv.get('number'),
                'client_id': inv.get('client_id'),
                'client_name': inv.get('client_name'),
                'date': inv.get('date'),
                'due_date': inv.get('due_date'),
                'status': inv.get('status'),
                'subtotal': inv.get('subtotal'),
                'total': inv.get('total'),
                'currency': inv.get('currency', 'USD'),
            }
            if include_financials and inv.get('id'):
                try:
                    fin = client.get_invoice_financials(inv['id'])
                    row['fees'] = fin['fees']
                    row['expenses'] = fin['expenses']
                except Exception as e:
                    row['fees'] = None
                    row['expenses'] = None
                    row['financials_error'] = str(e)
            result.append(row)
        return result

    @mcp.tool()
    def get_paymo_invoice_financials(invoice_number: str) -> Dict[str, Any]:
        """
        Split one invoice's total into fees vs expenses by reading its line
        items (not by subtraction).

        Use this when you need to know how much of an invoice is
        professional fees vs pass-through expenses. The classification
        follows what's linked to each line item: any line item with at
        least one expense record pointing to it (via
        `expense.invoice_item_id`) counts as expenses; everything else
        counts as fees.

        Returns:
            {
                'invoice_id', 'invoice_number',
                'total', 'subtotal',
                'fees', 'expenses',
                'fee_item_count', 'expense_item_count',
                'linked_expense_count',
            }
        """
        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured")

        client = PaymoClient(api_key)
        invoice = client.find_invoice_by_number(invoice_number)
        if not invoice:
            raise ValueError(f"Invoice not found: {invoice_number}")

        fin = client.get_invoice_financials(invoice['id'])
        return {
            'invoice_id': fin['invoice_id'],
            'invoice_number': fin['invoice_number'],
            'total': fin['total'],
            'subtotal': fin['subtotal'],
            'fees': fin['fees'],
            'expenses': fin['expenses'],
            'fee_item_count': len(fin['fee_items']),
            'expense_item_count': len(fin['expense_items']),
            'linked_expense_count': len(fin['linked_expenses']),
        }

    # Paymo invoice status enum, per
    # github.com/paymoapp/api/blob/master/sections/invoices.md (verified
    # 2026-07-24). Keep in sync with any Paymo API change.
    _INVOICE_STATUSES = ('draft', 'sent', 'viewed', 'paid', 'void')

    @mcp.tool()
    def update_paymo_invoice(
        invoice_number: str,
        status: Optional[str] = None,
        date: Optional[str] = None,
        due_date: Optional[str] = None,
        notes: Optional[str] = None,
        title: Optional[str] = None,
        bill_to: Optional[str] = None,
        company_info: Optional[str] = None,
        footer: Optional[str] = None,
        currency: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Update fields on an existing Paymo invoice. Pass only the fields
        you want to change.

        Most common use: `status="paid"` after reconciling payment from a
        bank statement. Valid statuses: draft, sent, viewed, paid, void.
        Paymo docs explicitly permit manual status override via PUT
        /invoices/{id} (verified 2026-07-24), and the change is reversible
        by calling again with the previous value.

        Other editable fields include date, due_date, notes, and the
        header text (title / bill_to / company_info / footer).

        Args:
            invoice_number: Invoice number (with or without # prefix)
            status: New status, one of: draft, sent, viewed, paid, void
            date: Invoice date, YYYY-MM-DD
            due_date: Due date, YYYY-MM-DD
            notes: Freeform notes on the invoice
            title: Invoice title / header
            bill_to: "Bill to" address block
            company_info: Provider address block
            footer: Footer text
            currency: ISO currency code (e.g. USD)

        Returns the updated invoice (trimmed) plus `previous_status`.
        """
        payload: Dict[str, Any] = {}
        if status is not None:
            status_lower = status.strip().lower()
            if status_lower not in _INVOICE_STATUSES:
                raise ValueError(
                    f"Invalid status {status!r}. Must be one of: "
                    f"{', '.join(_INVOICE_STATUSES)}."
                )
            payload['status'] = status_lower
        if date is not None:
            payload['date'] = date
        if due_date is not None:
            payload['due_date'] = due_date
        if notes is not None:
            payload['notes'] = notes
        if title is not None:
            payload['title'] = title
        if bill_to is not None:
            payload['bill_to'] = bill_to
        if company_info is not None:
            payload['company_info'] = company_info
        if footer is not None:
            payload['footer'] = footer
        if currency is not None:
            payload['currency'] = currency

        if not payload:
            raise ValueError(
                "No fields provided to update. Pass at least one of: "
                "status, date, due_date, notes, title, bill_to, "
                "company_info, footer, currency."
            )

        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured")

        client = PaymoClient(api_key)
        invoice = client.find_invoice_by_number(invoice_number)
        if not invoice:
            raise ValueError(f"Invoice not found: {invoice_number}")

        updated = client.update_invoice(invoice['id'], **payload)
        # Trim to the fields callers actually use so we don't dump the
        # full invoice payload into context.
        return {
            'id': updated.get('id'),
            'number': updated.get('number'),
            'client_id': updated.get('client_id'),
            'date': updated.get('date'),
            'due_date': updated.get('due_date'),
            'previous_status': invoice.get('status'),
            'status': updated.get('status'),
            'total': updated.get('total'),
            'currency': updated.get('currency', 'USD'),
            'updated_fields': list(payload.keys()),
        }

    @mcp.tool()
    def create_paymo_invoice(
        project_id: int,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        month: Optional[str] = None,
        invoice_date: Optional[str] = None,
        due_date: Optional[str] = None,
        number: Optional[str] = None,
        currency: str = "USD",
        group_by: str = "matter",
        rate_override: Optional[float] = None,
        mark_billed: bool = True,
        include_unbilled_only: bool = True,
        notes: Optional[str] = None,
        title: Optional[str] = None,
        bill_to: Optional[str] = None,
        company_info: Optional[str] = None,
        notification_to: Optional[List[str]] = None,
        footer: Optional[str] = None,
        template_invoice_id: Optional[int] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """
        Create a Paymo invoice for one project's time entries in a date range.

        DEFAULT LAYOUT (group_by="matter"):
          - ONE line item per invoice, titled with the project name
            (matches the account's existing template — see e.g. any prior
            Keystone invoice)
          - Line item quantity = total hours; price_unit = project rate
          - Line item description auto-renders the per-task breakdown in
            Paymo's HTML style:
              Total Hours: <span style="color: #777777"><em>H hrs M min</em></span>

              <strong>Default Task List</strong>
              - <Task name> <span style="color: #777777"><em>H hrs M min</em></span>

        HEADER FIELDS:
          - title / bill_to / company_info auto-copy from the most recent
            invoice for the same client (so provider + customer blocks
            match your usual template without hardcoding bank info in
            source). Pass explicit values to override.
          - Also settable via `template_invoice_id` to copy from a specific
            prior invoice.

        WORKFLOW:
          1. Select entries in [start_date, end_date] on `project_id`
             (default: unbilled only).
          2. Group into line items per `group_by` (default "matter").
          3. Auto-fill title/bill_to/company_info from a template invoice.
          4. POST invoice + items to Paymo via /invoices.
          5. If `mark_billed=True`, update each source entry with the
             surviving invoice-item's id and set billed=true.

        SAFETY:
          - Set `dry_run=True` first to preview the invoice payload without
            hitting Paymo. Skips create + link entirely when no entries match.

        Common shortcuts:
            create_paymo_invoice(project_id=3482327, month="last", dry_run=True)
            create_paymo_invoice(project_id=3482327, month="June", dry_run=True)

        Args:
            project_id: Paymo project id to invoice
            start_date: Range start YYYY-MM-DD (inclusive) — omit if using `month`
            end_date: Range end YYYY-MM-DD (inclusive) — omit if using `month`
            month: Shorthand for a calendar month; overrides start/end.
                Accepts "last", "current", "YYYY-MM", "June", "June 2026", etc.
            invoice_date: Invoice header date (default: Paymo assigns today)
            due_date: Payment due date (default: Paymo template)
            number: Custom invoice number (default: Paymo auto-generates,
                usually INV-YYYYMMDD-### per account template)
            currency: ISO currency code (default USD)
            group_by: "matter" (default) — one line item per matter with
                       per-task HTML breakdown in the description;
                      "task" — one line item per task;
                      "single" — one generic aggregated line
            rate_override: Override the project's price_per_hour for line
                items (default: use project rate)
            mark_billed: Link entries to invoice items + mark billed (default True)
            include_unbilled_only: Skip already-billed entries (default True)
            notes: Optional notes shown on the invoice
            title: Invoice title (default: copied from most recent invoice for
                this client, usually "INVOICE")
            bill_to: Customer address block (default: copied from template)
            company_info: Provider address / bank info block (default: copied)
            template_invoice_id: Specific prior invoice id to copy header
                fields from (default: most recent invoice for this client)
            dry_run: If True, return the planned payload without POSTing

        Returns:
            Dict with keys:
              invoice: created invoice (id, number, date, total, ...) —
                       or {"planned": true, ...} if dry_run
              line_items: list of {title, hours, price, subtotal, entry_ids}
              entries_linked: number of entries updated (0 if dry_run or
                              mark_billed=False)
              total_hours, total_amount: aggregate figures
              template_source_invoice_id: which prior invoice's header
                                          fields were copied (or None)
        """
        if month:
            if start_date or end_date:
                raise ValueError("Pass either `month` or `start_date`+`end_date`, not both")
            start_date, end_date = resolve_month(month)
        if not (start_date and end_date):
            raise ValueError("Provide `month` or both `start_date` and `end_date`")

        project_id = int(project_id)

        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured")

        client = PaymoClient(api_key)

        # Resolve the project so we can grab client_id and rate.
        projects = client.get_projects(active_only=False)
        project = next((p for p in projects if p.get('id') == project_id), None)
        if not project:
            raise ValueError(f"Project {project_id} not found")

        client_id = project.get('client_id')
        if not client_id:
            raise ValueError(f"Project {project_id} has no client_id")
        project_name = project.get('name') or f"Project {project_id}"
        rate = float(rate_override) if rate_override is not None else float(
            project.get('price_per_hour') or 0
        )
        if rate <= 0:
            raise ValueError(
                f"No rate available (project.price_per_hour={project.get('price_per_hour')}); "
                "pass rate_override"
            )

        # Fetch entries for the window and filter to this project.
        entries = client.get_entries(start_date, end_date)
        entries = [e for e in entries if e.get('project_id') == project_id]
        if include_unbilled_only:
            entries = [e for e in entries if not e.get('billed')]

        if not entries:
            return {
                'invoice': None,
                'line_items': [],
                'entries_linked': 0,
                'total_hours': 0.0,
                'total_amount': 0.0,
                'note': f"No {'unbilled ' if include_unbilled_only else ''}entries "
                        f"found for project {project_id} in {start_date}..{end_date}",
            }

        # ---- helpers ----
        def _hours(e):
            d = e.get('duration')
            if d is not None:
                return d / 3600.0
            if e.get('start_time') and e.get('end_time'):
                s = dateparser.parse(e['start_time'])
                x = dateparser.parse(e['end_time'])
                return (x - s).total_seconds() / 3600.0
            return 0.0

        def _fmt_hm(hours: float) -> str:
            """Paymo-style 'H hrs M min' with hrs-only or min-only shortcuts."""
            total_min = int(round(hours * 60))
            h, m = divmod(total_min, 60)
            if h == 0:
                return f"{m} min"
            if m == 0:
                return f"{h} hrs"
            return f"{h} hrs {m} min"

        # ---- resolve template header fields ----
        template_src_id = None
        if (title is None or bill_to is None or company_info is None
                or notification_to is None or footer is None):
            tmpl = None
            if template_invoice_id is not None:
                tmpl = client.get_invoice(int(template_invoice_id))
            else:
                tmpl = client.get_most_recent_invoice(int(client_id))
            if tmpl:
                template_src_id = tmpl.get('id')
                if title is None:
                    title = tmpl.get('title')
                if bill_to is None:
                    bill_to = tmpl.get('bill_to')
                if company_info is None:
                    company_info = tmpl.get('company_info')
                if footer is None:
                    footer = tmpl.get('footer')
                if notification_to is None:
                    tmpl_notif = (
                        (tmpl.get('options') or {}).get('notification') or {}
                    ).get('to')
                    if tmpl_notif:
                        notification_to = list(tmpl_notif)

        # ---- group entries ----
        groups: List[Dict[str, Any]] = []
        if group_by in ("matter", "task"):
            # Both modes need per-task hour aggregations.
            task_names: Dict[int, str] = {}
            for tid in {e.get('task_id') for e in entries if e.get('task_id')}:
                try:
                    tr = client._request('GET', f'tasks/{tid}')
                    td = tr.get('tasks', [{}])[0] if 'tasks' in tr else {}
                    task_names[tid] = td.get('name') or f"Task {tid}"
                except Exception:
                    task_names[tid] = f"Task {tid}"

            by_task: Dict[Any, List[Dict]] = {}
            for e in entries:
                by_task.setdefault(e.get('task_id'), []).append(e)
            task_agg = []
            for tid, es in sorted(
                by_task.items(),
                key=lambda kv: min(
                    (x.get('start_time') or x.get('date') or '') for x in kv[1]
                ),
            ):
                task_agg.append({
                    'task_id': tid,
                    'name': task_names.get(tid, f"Task {tid}"),
                    'hours': round(sum(_hours(e) for e in es), 2),
                    'entries': es,
                })

            if group_by == "task":
                for t in task_agg:
                    groups.append({
                        'title': t['name'],
                        'hours': t['hours'],
                        'description': None,
                        'entries': t['entries'],
                    })
            else:  # matter mode
                total = round(sum(t['hours'] for t in task_agg), 2)
                lines = [
                    f'Total Hours: <span style="color: #777777"><em>{_fmt_hm(total)}</em></span>',
                    '',
                    '<strong>Default Task List</strong>',
                ]
                for t in task_agg:
                    lines.append(
                        f'- {t["name"]} <span style="color: #777777"><em>{_fmt_hm(t["hours"])}</em></span>'
                    )
                groups.append({
                    'title': project_name,
                    'hours': total,
                    'description': '\n'.join(lines),
                    'entries': [e for t in task_agg for e in t['entries']],
                })
        elif group_by == "single":
            hours = round(sum(_hours(e) for e in entries), 2)
            groups.append({
                'title': f"Professional services ({start_date} to {end_date})",
                'hours': hours,
                'description': None,
                'entries': entries,
            })
        else:
            raise ValueError(
                f"group_by must be 'matter', 'task', or 'single', got {group_by!r}"
            )

        # ---- assemble Paymo `items` payload ----
        # Paymo's invoiceitem schema requires `item` for the line title and
        # `price_unit` for the per-unit price (verified 2026-07-22: passing
        # `title`/`price` silently persisted only `quantity`, leaving totals
        # at $0). Also requires `project_id` at invoice level to link to
        # the source project.
        items_payload: List[Dict[str, Any]] = []
        for i, g in enumerate(groups, start=1):
            item_obj: Dict[str, Any] = {
                'item': g['title'],
                'price_unit': rate,
                'quantity': g['hours'],
                'seq': i,
            }
            if g.get('description'):
                item_obj['description'] = g['description']
            items_payload.append(item_obj)

        total_hours = round(sum(g['hours'] for g in groups), 2)
        total_amount = round(sum(g['hours'] * rate for g in groups), 2)

        # DRY RUN: return the planned payload and stop.
        if dry_run:
            return {
                'invoice': {
                    'planned': True,
                    'client_id': client_id,
                    'project_id': project_id,
                    'currency': currency,
                    'date': invoice_date,
                    'due_date': due_date,
                    'number': number,
                    'title': title,
                    'bill_to': bill_to,
                    'company_info': company_info,
                    'notification_to': notification_to,
                    'footer': footer,
                    'items': items_payload,
                    'notes': notes,
                },
                'line_items': [
                    {
                        'title': g['title'],
                        'hours': g['hours'],
                        'price': rate,
                        'subtotal': round(g['hours'] * rate, 2),
                        'entry_ids': [e.get('id') for e in g['entries']],
                    }
                    for g in groups
                ],
                'entries_linked': 0,
                'total_hours': total_hours,
                'total_amount': total_amount,
                'template_source_invoice_id': template_src_id,
            }

        # Create the invoice + items in one POST.
        created = client.create_invoice(
            client_id=client_id,
            project_id=project_id,
            items=items_payload,
            date=invoice_date,
            due_date=due_date,
            number=number,
            currency=currency,
            notes=notes,
            title=title,
            bill_to=bill_to,
            company_info=company_info,
            notification_to=notification_to,
            footer=footer,
        )

        # Map created invoice items back to their groups by seq.
        created_items = created.get('invoiceitems') or []
        created_items_by_seq = {
            (it.get('seq') if it.get('seq') is not None else idx): it
            for idx, it in enumerate(created_items)
        }

        line_items_out: List[Dict[str, Any]] = []
        entries_linked = 0
        for i, g in enumerate(groups, start=1):
            # Paymo's `seq` is 0-indexed on read even though our `seq` param
            # was 1-indexed on write; try both.
            item = created_items_by_seq.get(i) or created_items_by_seq.get(i - 1)
            item_id = item.get('id') if item else None
            entry_ids = [e.get('id') for e in g['entries']]
            if mark_billed and item_id:
                for eid in entry_ids:
                    try:
                        client.update_entry(
                            int(eid),
                            invoice_item_id=int(item_id),
                            billed=True,
                        )
                        entries_linked += 1
                    except Exception:
                        # Don't unwind the invoice on a link failure — surface
                        # the count so the caller can retry the unlinked ones.
                        pass
            line_items_out.append({
                'title': g['title'],
                'hours': g['hours'],
                'price': rate,
                'subtotal': round(g['hours'] * rate, 2),
                'invoice_item_id': item_id,
                'entry_ids': entry_ids,
            })

        return {
            'invoice': {
                'id': created.get('id'),
                'number': created.get('number'),
                'date': created.get('date'),
                'due_date': created.get('due_date'),
                'status': created.get('status'),
                'subtotal': created.get('subtotal'),
                'total': created.get('total'),
                'currency': created.get('currency', currency),
                'title': created.get('title'),
                'bill_to_set': bool(created.get('bill_to')),
                'company_info_set': bool(created.get('company_info')),
            },
            'line_items': line_items_out,
            'entries_linked': entries_linked,
            'total_hours': total_hours,
            'total_amount': total_amount,
            'template_source_invoice_id': template_src_id,
        }

    @mcp.tool()
    def preview_paymo_invoice_send(invoice_id: int) -> Dict[str, Any]:
        """
        Preview an invoice's send state — recipient, amount, PDF link.

        PAYMO'S API HAS NO SEND ENDPOINT.
        Sending an invoice by email happens only through the Paymo web
        UI. Verified 2026-07-22 against the official docs
        (github.com/paymoapp/api/blob/master/sections/invoices.md): the
        invoices resource exposes only list / get / create / update /
        delete. The docs state "An invoice sent to a client by email
        from the Paymo app will be automatically changed to `sent`"
        — the send is a UI action, not an API action.

        This tool therefore does NOT send. It returns everything the
        user needs to eyeball before hitting Send in the Paymo web UI:
        invoice number, amount, recipient email(s), current status, PDF
        link, and any warnings (missing email, already sent).

        Args:
            invoice_id: Paymo invoice id (integer, not the "#INV-..." string)

        Returns:
            {
              'preview': True,
              'send_ui_only': True,        # reminder: API cannot send
              'invoice_id', 'invoice_number', 'status',
              'total', 'currency',
              'bill_to_emails': [str, ...] # parsed from bill_to block
              'notification_to': [str, ...] # from options.notification.to
              'pdf_link',
              'warning'                    # string or None
            }
        """
        import re

        invoice_id = int(invoice_id)

        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured")
        client = PaymoClient(api_key)

        inv = client.get_invoice(invoice_id)
        if not inv or not inv.get('id'):
            raise ValueError(f"Invoice {invoice_id} not found")

        bill_to = inv.get('bill_to') or ''
        bill_to_emails = re.findall(r'[\w.+-]+@[\w.-]+\.[\w-]+', bill_to)
        options = inv.get('options') or {}
        notification_to = ((options.get('notification') or {}).get('to')) or []

        status = inv.get('status') or 'unknown'
        warnings = []
        if status != 'draft':
            warnings.append(
                f"Invoice status is {status!r}, not 'draft' — it may already have been sent."
            )
        if not (bill_to_emails or notification_to):
            warnings.append(
                "No email address on bill_to or options.notification.to."
            )

        return {
            'preview': True,
            'send_ui_only': True,
            'invoice_id': invoice_id,
            'invoice_number': inv.get('number'),
            'status': status,
            'total': inv.get('total'),
            'currency': inv.get('currency', 'USD'),
            'bill_to_emails': bill_to_emails,
            'notification_to': notification_to,
            'pdf_link': inv.get('pdf_link'),
            'warning': "; ".join(warnings) if warnings else None,
        }

    @mcp.tool()
    def get_projects_without_recent_invoices(days: int = 30) -> List[Dict[str, Any]]:
        """
        Get active projects that haven't been invoiced in the specified number of days.
        Efficient for queries like "which projects haven't I invoiced this month?"

        Args:
            days: Number of days to look back (default 30)

        Returns:
            List of projects without recent invoices: project name, client, last invoice date, days since last invoice
        """
        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured")

        client = PaymoClient(api_key)
        from datetime import datetime, timedelta

        cutoff_date = datetime.now() - timedelta(days=days)

        # Get all active projects
        projects = client.get_projects()

        # Get all invoices (we only need recent ones but API doesn't support date filtering)
        invoices = client.get_invoices()

        # Build map of project -> most recent invoice date
        project_last_invoice = {}
        for invoice in invoices:
            inv_date_str = invoice.get('date', '')
            if not inv_date_str:
                continue

            inv_date = datetime.strptime(inv_date_str, '%Y-%m-%d')

            # Find which projects are on this invoice by checking invoice items
            # For now, use client_id as proxy (simplification)
            client_id = invoice.get('client_id')
            for project in projects:
                if project.get('client_id') == client_id:
                    project_id = project.get('id')
                    if project_id not in project_last_invoice or inv_date > project_last_invoice[project_id]:
                        project_last_invoice[project_id] = inv_date

        # Build result: projects without recent invoices
        result = []
        for project in projects:
            if not project.get('active'):
                continue

            project_id = project.get('id')
            last_invoice_date = project_last_invoice.get(project_id)

            # Include if: no invoice ever, or last invoice before cutoff
            if not last_invoice_date or last_invoice_date < cutoff_date:
                days_since = (datetime.now() - last_invoice_date).days if last_invoice_date else 999
                result.append({
                    'project_id': project_id,
                    'project_name': project.get('name'),
                    'client_name': project.get('client_name'),
                    'last_invoice_date': last_invoice_date.strftime('%Y-%m-%d') if last_invoice_date else 'Never',
                    'days_since_invoice': days_since
                })

        # Sort by days since invoice descending
        result.sort(key=lambda x: x['days_since_invoice'], reverse=True)

        return result

    @mcp.tool()
    def get_outstanding_invoices_last_week() -> List[Dict[str, Any]]:
        """Get outstanding invoices (sent or viewed) from the last 7 days"""
        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured")

        client = PaymoClient(api_key)
        return client.get_outstanding_invoices_last_week()

    @mcp.tool()
    def export_invoice_timesheet(invoice_number: str, strict: bool = True) -> str:
        """
        Export a formatted timesheet CSV for an invoice. This is the PRIMARY tool for
        generating invoice timesheets.

        WHEN TO USE THIS TOOL:
        - User asks to "export timesheet for invoice X"
        - User asks to "generate a timesheet for INV-..."
        - User asks to "get the timesheet for invoice number ..."
        - User wants a billing-ready timesheet with Matter, Period, Hours, Fees summary

        STRICT MATCHING (default):
        Only exports entries explicitly linked to this invoice. Validates that the
        calculated total (hours × rate) matches the invoice total within 5%.
        If validation fails, raises an error suggesting export_paymo_timesheet() instead.

        This ensures you get EXACTLY the entries billed on that specific invoice,
        not entries that happen to fall within a date range.

        OUTPUT FORMAT:
        The CSV includes:
        - Header: Matter name, Invoice number, Period, Total Hours, Fees, Expenses, Total Due
        - Data: Date, Start Time (HH:MM), End Time (HH:MM), Duration, Task, Description
        - Entries sorted chronologically (earliest first)
        - Footer with expenses

        Args:
            invoice_number: Invoice number string (e.g., 'INV-20260331-241')
                           This is the invoice NUMBER, not the internal ID.
                           Users see this number on their invoices.
            strict: If True (default), validate totals match invoice.
                   If False, export linked entries without validation.

        Returns:
            Formatted CSV content ready for billing/records.

        ALTERNATIVE - DATE RANGE EXPORT:
        If strict validation fails or you need entries by date range regardless of
        invoice linkage, use export_paymo_timesheet(start_date, end_date, project_id)
        instead. That tool exports all entries in a date range.

        Example:
            export_invoice_timesheet("INV-20260331-241")
            export_invoice_timesheet("INV-20260331-241", strict=False)  # skip validation
        """
        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured")

        client = PaymoClient(api_key)
        return client.export_invoice_formatted(invoice_number, strict=strict)

    @mcp.tool()
    def export_invoice_paymo_format(invoice_number: str, strict: bool = True) -> str:
        """
        Export timesheet in EXACT Paymo native format with all standard columns.

        WHEN TO USE THIS TOOL:
        - User asks for "Paymo format" or "native Paymo export"
        - User needs the export to match Paymo's own export format exactly
        - User wants all internal IDs (user, project, task, tasklist)
        - User is importing into another system that expects Paymo format

        WHEN TO USE export_invoice_timesheet() INSTEAD:
        - User wants a clean, billing-ready format with summary header
        - User wants simple Date, Time, Duration, Task, Description columns
        - Default choice for invoice timesheets

        OUTPUT FORMAT (exact Paymo columns):
        User, Internal User Id, Project, Internal Project Id, Project Description,
        Tasklist, Internal Tasklist Id, Task, Internal Task Id, Start Time, End Time,
        Worked Time, Decimal Hours, Time In Seconds

        Args:
            invoice_number: Invoice number string (e.g., 'INV-20260331-241')
            strict: If True (default), validate totals match invoice.

        Returns:
            CSV content in exact Paymo export format.

        Example:
            export_invoice_paymo_format("INV-20260331-241")
        """
        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured")

        client = PaymoClient(api_key)
        return client.export_invoice_paymo_format(invoice_number, strict=strict)

    @mcp.tool()
    def delete_paymo_entry(entry_id: int) -> str:
        """
        Delete a time entry by ID

        Args:
            entry_id: The ID of the time entry to delete
        """
        # Convert parameters to proper types (MCP may pass strings)
        entry_id = int(entry_id)

        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured")

        client = PaymoClient(api_key)
        try:
            client.delete_entry(entry_id)
            return f"Successfully deleted entry {entry_id}"
        except Exception as e:
            return f"Failed to delete entry: {e}"

    @mcp.tool()
    def mark_paymo_entry_billed(entry_id: int, billed: bool = True) -> Dict[str, Any]:
        """
        Mark a time entry as billed or unbilled

        Args:
            entry_id: The ID of the time entry
            billed: True to mark as billed, False to mark as unbilled (default: True)
        """
        # Convert parameters to proper types (MCP may pass strings)
        entry_id = int(entry_id)

        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured")

        client = PaymoClient(api_key)
        return client.update_entry(entry_id, billed=billed)

    @mcp.tool()
    def update_paymo_entry(
        entry_id: int,
        description: Optional[str] = None,
        duration_hours: Optional[float] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        date: Optional[str] = None,
        task_id: Optional[int] = None,
        billed: Optional[bool] = None,
        timezone: str = "America/Chicago",
    ) -> Dict[str, Any]:
        """
        Adjust an existing time entry in place - avoids the delete + recreate
        cycle for typos, wrong task/matter, wrong duration, or date fixes.

        Only fields you pass are updated; anything left None is preserved.
        Provide EITHER duration_hours OR both start_time and end_time when
        changing the time - never mix.

        Args:
            entry_id: Paymo entry ID to update
            description: New description text
            duration_hours: New total hours (use this OR start_time+end_time)
            start_time: New start time HH:MM 24-hour (needs end_time and date)
            end_time: New end time HH:MM 24-hour (needs start_time and date)
            date: New date YYYY-MM-DD (required if start_time/end_time given)
            task_id: Move entry to a different task (also moves matter if
                the new task is on a different project)
            billed: Set billing status (True/False)
            timezone: IANA tz for start/end conversion (default America/Chicago)
        """
        entry_id = int(entry_id)

        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured")
        client = PaymoClient(api_key)

        payload: Dict[str, Any] = {}
        if description is not None:
            payload['description'] = description
        if task_id is not None:
            payload['task_id'] = int(task_id)
        if billed is not None:
            payload['billed'] = billed
        if date is not None:
            payload['date'] = date

        # Time changes: mirror create_paymo_entry's conversion logic
        if start_time is not None or end_time is not None:
            if not (start_time and end_time):
                raise ValueError("start_time and end_time must be provided together")
            if not date:
                raise ValueError("date is required when updating start_time/end_time")
            if duration_hours is not None:
                raise ValueError("Provide EITHER duration_hours OR start_time+end_time, not both")
            from zoneinfo import ZoneInfo
            local_tz = ZoneInfo(timezone)
            utc = ZoneInfo("UTC")
            start_dt = datetime.strptime(f"{date} {start_time}", "%Y-%m-%d %H:%M")
            start_dt = start_dt.replace(tzinfo=local_tz).astimezone(utc)
            end_dt = datetime.strptime(f"{date} {end_time}", "%Y-%m-%d %H:%M")
            end_dt = end_dt.replace(tzinfo=local_tz).astimezone(utc)
            payload['start_time'] = start_dt.strftime("%Y-%m-%dT%H:%M:%S")
            payload['end_time'] = end_dt.strftime("%Y-%m-%dT%H:%M:%S")
        elif duration_hours is not None:
            payload['duration'] = int(float(duration_hours) * 3600)

        if not payload:
            raise ValueError("No fields to update - pass at least one changed field")

        updated = client.update_entry(entry_id, **payload)

        # Trim the return like other tools
        return {
            'id': updated.get('id'),
            'project_id': updated.get('project_id'),
            'task_id': updated.get('task_id'),
            'date': updated.get('date'),
            'start_time': updated.get('start_time'),
            'end_time': updated.get('end_time'),
            'duration_hours': round((updated.get('duration') or 0) / 3600, 2),
            'description': updated.get('description'),
            'billed': updated.get('billed', False),
        }

    @mcp.tool()
    def list_paymo_entries(
        start_date: str,
        end_date: str,
        project_id: Optional[int] = None,
        billed: Optional[bool] = None
    ) -> List[Dict[str, Any]]:
        """
        List time entries with optional filters

        Args:
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
            project_id: Optional project filter
            billed: Optional filter - True for billed, False for unbilled, None for all

        Returns:
            List of time entries with task names, durations, descriptions
        """
        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured")

        client = PaymoClient(api_key)

        # Get entries
        entries = client.get_entries(start_date, end_date)

        # Filter by project if specified
        if project_id is not None:
            entries = [e for e in entries if e.get('project_id') == project_id]

        # Filter by billed status if specified
        if billed is not None:
            entries = [e for e in entries if e.get('billed') == billed]

        # Enhance entries with task names and readable data
        # Use cache to avoid repeated API calls for same task_id
        task_cache = {}
        result = []
        for entry in entries:
            # Get task name (with caching)
            task_id = entry.get('task_id')
            task_name = ''
            if task_id:
                # Check cache first
                if task_id in task_cache:
                    task_name = task_cache[task_id]
                else:
                    # Fetch from API and cache result
                    try:
                        import time
                        time.sleep(0.5)  # Small delay to avoid rate limits
                        task_response = client._request('GET', f'tasks/{task_id}')
                        task_data = task_response.get('tasks', [{}])[0]
                        task_name = task_data.get('name', '')
                        task_cache[task_id] = task_name
                    except Exception as e:
                        # If rate limited, retry once after delay
                        if '429' in str(e):
                            try:
                                time.sleep(2)
                                task_response = client._request('GET', f'tasks/{task_id}')
                                task_data = task_response.get('tasks', [{}])[0]
                                task_name = task_data.get('name', '')
                                task_cache[task_id] = task_name
                            except Exception as retry_err:
                                task_name = f'Task {task_id}'
                                task_cache[task_id] = task_name
                        else:
                            task_name = f'Task {task_id}'
                            task_cache[task_id] = task_name

            # Calculate duration in hours
            duration_hours = entry.get('duration', 0) / 3600 if entry.get('duration') else 0

            # Clean description
            description = entry.get('description', '')
            if description:
                import re
                import html
                description = re.sub(r'<[^>]+>', '', description)
                description = html.unescape(description).strip()

            result.append({
                'id': entry.get('id'),
                'project_id': entry.get('project_id'),
                'task_id': task_id,
                'task_name': task_name,
                'date': entry.get('date', ''),
                'start_time': entry.get('start_time', ''),
                'end_time': entry.get('end_time', ''),
                'duration_hours': round(duration_hours, 2),
                'description': description,
                'billed': entry.get('billed', False),
                'price': entry.get('price', 0)
            })

        return result

    @mcp.tool()
    def get_projects_needing_invoicing(
        month: str = None,
        min_unbilled_hours: float = 0.0
    ) -> Dict[str, Any]:
        """
        Single efficient query combining invoice recency, unbilled hours, and filtering.
        Perfect for "what active projects need invoicing?"

        Args:
            month: Month in YYYY-MM format (defaults to current month)
            min_unbilled_hours: Minimum unbilled hours to include (default 0)

        Returns:
            Dict with projects_needing_invoicing, month, total_unbilled
        """
        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured")

        client = PaymoClient(api_key)
        from datetime import datetime, timedelta

        # Parse month or default to current
        if month:
            month_start = datetime.strptime(month + '-01', '%Y-%m-%d')
        else:
            month_start = datetime.now().replace(day=1)
            month = month_start.strftime('%Y-%m')

        # Calculate date range: start of month to now (or end of month if past month)
        end_date = datetime.now()
        if end_date < month_start:
            # Future month requested, use end of that month
            next_month = month_start.replace(day=28) + timedelta(days=4)
            end_date = next_month - timedelta(days=next_month.day)

        # Get all active projects
        projects = client.get_projects()
        active_projects = [p for p in projects if p.get('active')]

        # Get all entries for the last 90 days (to check both billed and unbilled)
        # Use tomorrow as end date to catch entries for "today" in all timezones
        start_search = (datetime.now() - timedelta(days=90)).strftime('%Y-%m-%d')
        end_search = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
        all_entries = client.get_entries(start_search, end_search)

        # Process each project
        results = []
        total_unbilled = 0

        for project in active_projects:
            project_id = project.get('id')

            # Get entries for this project
            project_entries = [e for e in all_entries if e.get('project_id') == project_id]

            # Find last invoice date (most recent billed entry)
            billed_entries = [e for e in project_entries if e.get('invoice_item_id')]
            last_invoice_date = None
            if billed_entries:
                # Find most recent billed entry
                for entry in billed_entries:
                    entry_date_str = entry.get('date') or entry.get('start_time', '')[:10]
                    if entry_date_str:
                        entry_date = datetime.strptime(entry_date_str, '%Y-%m-%d')
                        if not last_invoice_date or entry_date > last_invoice_date:
                            last_invoice_date = entry_date

            # Calculate unbilled hours/amount
            unbilled_entries = [e for e in project_entries if not e.get('billed', False)]
            unbilled_hours = sum(e.get('duration', 0) / 3600 for e in unbilled_entries)

            rate = project.get('price_per_hour', 0)
            unbilled_amount = unbilled_hours * rate

            # Filter: must have unbilled hours AND not invoiced this month
            has_unbilled = unbilled_hours >= min_unbilled_hours
            not_invoiced_this_month = not last_invoice_date or last_invoice_date < month_start

            if has_unbilled and not_invoiced_this_month:
                results.append({
                    'project_id': project_id,
                    'project_name': project.get('name'),
                    'client_name': project.get('client_name'),
                    'rate': rate,
                    'last_invoice_date': last_invoice_date.strftime('%Y-%m-%d') if last_invoice_date else None,
                    'unbilled_hours': round(unbilled_hours, 2),
                    'unbilled_amount': round(unbilled_amount, 2)
                })
                total_unbilled += unbilled_amount

        # Sort by unbilled amount descending
        results.sort(key=lambda x: x['unbilled_amount'], reverse=True)

        return {
            'projects_needing_invoicing': results,
            'month': month,
            'total_unbilled': round(total_unbilled, 2)
        }

    @mcp.tool()
    def get_unbilled_summary(
        start_date: str = None,
        end_date: str = None
    ) -> List[Dict[str, Any]]:
        """
        Get unbilled hours and revenue summary by project.
        Efficient query that returns only aggregated data, not individual entries.

        Args:
            start_date: Optional start date (YYYY-MM-DD), defaults to 60 days ago
            end_date: Optional end date (YYYY-MM-DD), defaults to tomorrow (to catch today's entries in all timezones)

        Returns:
            List of projects with unbilled summary: project name, client, rate, unbilled hours, unbilled amount
        """
        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured")

        client = PaymoClient(api_key)

        # Default date range: last 60 days to tomorrow (to catch timezone edge cases)
        from datetime import datetime, timedelta
        if not start_date:
            start_date = (datetime.now() - timedelta(days=60)).strftime('%Y-%m-%d')
        if not end_date:
            # Use tomorrow to ensure we catch entries for "today" in all timezones
            # and any entries dated for tomorrow that were entered early
            end_date = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')

        # Get all projects
        projects = client.get_projects()

        # Get all unbilled entries (without fetching task names - more efficient)
        all_entries = client.get_entries(start_date, end_date)
        unbilled_entries = [e for e in all_entries if not e.get('billed', False)]

        # Aggregate by project
        project_summary = {}
        for entry in unbilled_entries:
            project_id = entry.get('project_id')
            if not project_id:
                continue

            if project_id not in project_summary:
                project_summary[project_id] = {
                    'total_hours': 0,
                    'total_amount': 0
                }

            # Add hours
            duration_hours = entry.get('duration', 0) / 3600 if entry.get('duration') else 0
            project_summary[project_id]['total_hours'] += duration_hours

            # Add amount (use entry price if available, otherwise calculate from duration)
            price = entry.get('price', 0)
            if not price and duration_hours > 0:
                # Find project rate
                project = next((p for p in projects if p.get('id') == project_id), None)
                if project:
                    price = duration_hours * project.get('price_per_hour', 0)

            project_summary[project_id]['total_amount'] += price

        # Build result with project details
        result = []
        for project_id, summary in project_summary.items():
            project = next((p for p in projects if p.get('id') == project_id), None)
            if project and summary['total_hours'] > 0:
                result.append({
                    'project_id': project_id,
                    'project_name': project.get('name'),
                    'client_name': project.get('client_name'),
                    'rate': project.get('price_per_hour'),
                    'unbilled_hours': round(summary['total_hours'], 2),
                    'unbilled_amount': round(summary['total_amount'], 2)
                })

        # Sort by unbilled amount descending
        result.sort(key=lambda x: x['unbilled_amount'], reverse=True)

        return result

    @mcp.tool()
    def list_paymo_expenses(
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        project_id: Optional[int] = None,
        billable_only: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        List Paymo expenses, optionally filtered by date range and/or project.

        Also used to answer "what was the last expense I filed?" (sort results
        by date/id descending) and "what expenses exist on matter X?".

        Args:
            start_date: YYYY-MM-DD (inclusive)
            end_date: YYYY-MM-DD (inclusive)
            project_id: Restrict to one project/matter
            billable_only: If True, return only billable expenses
        """
        if project_id is not None:
            try:
                project_id = int(project_id)
            except (ValueError, TypeError) as e:
                raise ValueError(f"Invalid project_id '{project_id}': {e}")

        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured in ~/.mcp-auth/paymo/auth.json")

        client = PaymoClient(api_key)

        expenses = client.get_expenses(
            project_id=project_id,
            start_date=start_date,
            end_date=end_date,
        )

        if billable_only:
            expenses = [e for e in expenses if e.get('billable')]

        return [{
            'id': e.get('id'),
            'project_id': e.get('project_id'),
            'client_id': e.get('client_id'),
            'name': e.get('name'),
            'notes': e.get('notes'),
            'date': e.get('date'),
            'amount': e.get('amount'),
            'currency': e.get('currency'),
            'billable': e.get('billable'),
            'invoiced': e.get('invoiced'),
            'invoice_item_id': e.get('invoice_item_id'),
        } for e in expenses]

    @mcp.tool()
    def create_paymo_expense(
        project_id: int,
        name: str,
        date: str,
        amount: float,
        description: Optional[str] = None,
        quantity: float = 1,
        billable: bool = True,
        currency: str = "USD",
        client_id: Optional[int] = None,
        attachment_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create a single expense on a project/matter, optionally with a file
        attachment (e.g. an .xlsx expense report).

        Args:
            project_id: Paymo project (matter) ID
            name: Short label, e.g. "United - DFW deposition travel"
            date: YYYY-MM-DD (charge date)
            amount: Total expense amount (price * quantity)
            description: Optional detail (merchant, purpose) -> `notes` field
            quantity: Default 1; price is derived as amount/quantity
            billable: Default True
            currency: Default USD
            client_id: Paymo client ID. Auto-looked-up from the project if
                omitted (Paymo requires this on POST /expenses).
            attachment_path: Optional local file path (absolute or ~-expanded)
                to attach after creation. Uploaded via POST /files.
        """
        try:
            project_id = int(project_id)
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid project_id '{project_id}': {e}")
        try:
            amount = float(amount)
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid amount '{amount}': {e}")
        try:
            quantity = float(quantity)
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid quantity '{quantity}': {e}")

        if quantity == 0:
            raise ValueError("quantity must be non-zero")

        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured in ~/.mcp-auth/paymo/auth.json")

        client = PaymoClient(api_key)

        # Paymo requires client_id on POST /expenses; auto-resolve from project
        if client_id is None:
            for p in client.get_projects(active_only=False):
                if p.get('id') == project_id:
                    client_id = p.get('client_id')
                    break
            if client_id is None:
                raise ValueError(
                    f"Could not auto-lookup client_id for project {project_id}; "
                    "pass client_id explicitly."
                )

        # Live shape check (2026-07): Paymo expense records carry `price` and
        # `amount` (equal on unit expenses); `quantity` is not persisted, and
        # the description-like field is `notes`, not `description`.
        payload = {
            'client_id': int(client_id),
            'name': name,
            'date': date,
            'amount': amount,
            'price': amount / quantity,
            'billable': billable,
            'currency': currency,
        }
        if description:
            payload['notes'] = description

        e = client.create_expense(project_id, **payload)

        attached = None
        if attachment_path:
            try:
                f = client.upload_expense_file(e.get('id'), attachment_path)
                attached = f.get('original_filename') or True
            except Exception as err:
                attached = f"FAILED: {err}"

        return {
            'id': e.get('id'),
            'project_id': e.get('project_id'),
            'client_id': e.get('client_id'),
            'name': e.get('name'),
            'notes': e.get('notes'),
            'date': e.get('date'),
            'amount': e.get('amount'),
            'billable': e.get('billable'),
            'attachment': attached,
        }

    @mcp.tool()
    def add_paymo_expense_attachment(expense_id: int, attachment_path: str) -> Dict:
        """
        Attach a file to an EXISTING expense. Paymo supports multiple files per
        expense — call this once per file. Use list_paymo_expense_files to
        verify what is actually attached (uploads can fail silently otherwise).

        Args:
            expense_id: Existing Paymo expense ID
            attachment_path: Local file path (absolute or ~-expanded)
        """
        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured in ~/.mcp-auth/paymo/auth.json")
        client = PaymoClient(api_key)
        f = client.upload_expense_file(int(expense_id), attachment_path)
        return {
            'expense_id': int(expense_id),
            'file_id': f.get('id'),
            'filename': f.get('original_filename') or f.get('name'),
            'size': f.get('size'),
        }

    @mcp.tool()
    def list_paymo_expense_files(expense_id: int) -> Dict:
        """
        List files actually attached to an expense (server-side truth).
        Use after uploads to verify attachments persisted.

        Args:
            expense_id: Paymo expense ID
        """
        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured in ~/.mcp-auth/paymo/auth.json")
        client = PaymoClient(api_key)
        r = client._request('GET', f'files?where=expense_id={int(expense_id)}')
        return {
            'expense_id': int(expense_id),
            'files': [
                {'file_id': f.get('id'),
                 'filename': f.get('original_filename') or f.get('name'),
                 'size': f.get('size')}
                for f in r.get('files', [])
            ],
        }

    @mcp.tool()
    def delete_paymo_expense(expense_id: int) -> str:
        """
        Delete an expense by ID. Corrections = delete then recreate.

        Args:
            expense_id: The ID of the expense to delete
        """
        expense_id = int(expense_id)

        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured")

        client = PaymoClient(api_key)
        try:
            client.delete_expense(expense_id)
            return f"Successfully deleted expense {expense_id}"
        except Exception as e:
            return f"Failed to delete expense: {e}"

    @mcp.tool()
    def audit_paymo_expenses(
        start_date: str,
        end_date: str,
        project_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Read-only audit of existing expenses over a date range. Never writes.
        Returns findings grouped by severity plus per-matter totals.

        Checks performed:
          - Duplicates (same project+date+amount+name -> error; same
            amount+name within +/-2 days across any project -> warn)
          - Math integrity (price vs amount, non-positive amounts,
            non-USD currency). Note: Paymo does not persist `quantity`, so
            we compare `price` vs `amount` directly.
          - Matter-mapping sanity (expense's project has zero logged hours in
            date+/-1 window, or a different project dominated the window)
          - Billing hygiene (billable+uninvoiced but matter has a later
            invoice; invoiced=true with no invoice_item_id link)

        Any fixes must be applied by the caller via delete_paymo_expense +
        create_paymo_expense.

        Args:
            start_date: YYYY-MM-DD (inclusive)
            end_date: YYYY-MM-DD (inclusive)
            project_id: Optional restriction to one project
        """
        if project_id is not None:
            try:
                project_id = int(project_id)
            except (ValueError, TypeError) as e:
                raise ValueError(f"Invalid project_id '{project_id}': {e}")

        config = load_config()
        api_key = config.get('api_key')
        if not api_key:
            raise ValueError("API key not configured in ~/.mcp-auth/paymo/auth.json")

        client = PaymoClient(api_key)

        expenses = client.get_expenses(
            project_id=project_id,
            start_date=start_date,
            end_date=end_date,
        )

        # Build task_id -> project_id map once (used for matter-mapping check)
        task_to_project: Dict[int, int] = {}
        try:
            for t in client.get_tasks():
                tid = t.get('id')
                pid = t.get('project_id')
                if tid is not None and pid is not None:
                    task_to_project[tid] = pid
        except Exception:
            pass

        # Cache project names
        project_names: Dict[int, str] = {}
        try:
            for p in client.get_projects(active_only=False):
                pid = p.get('id')
                if pid is not None:
                    project_names[pid] = p.get('name', f'Project {pid}')
        except Exception:
            pass

        # Invoices for billing-hygiene check
        try:
            invoices = client.get_invoices()
        except Exception:
            invoices = []

        # Cache time entries per (date-1, date+1) window to avoid repeated API calls
        entry_window_cache: Dict[Tuple[str, str], List[Dict]] = {}

        def get_entries_window(center_date: str) -> List[Dict]:
            try:
                d = datetime.strptime(center_date, '%Y-%m-%d')
            except (ValueError, TypeError):
                return []
            s = (d - timedelta(days=1)).strftime('%Y-%m-%d')
            e = (d + timedelta(days=1)).strftime('%Y-%m-%d')
            key = (s, e)
            if key not in entry_window_cache:
                try:
                    entry_window_cache[key] = client.get_entries(s, e)
                except Exception:
                    entry_window_cache[key] = []
            return entry_window_cache[key]

        findings: List[Dict[str, Any]] = []
        totals_by_matter: Dict[int, Dict[str, Any]] = {}

        # Group for exact-duplicate detection
        exact_group: Dict[Tuple[Any, Any, Any, Any], List[Dict]] = {}
        for e in expenses:
            key = (
                e.get('project_id'),
                e.get('date'),
                round(float(e.get('amount') or 0), 2),
                (e.get('name') or '').strip().lower(),
            )
            exact_group.setdefault(key, []).append(e)

        seen_exact_ids = set()
        for key, group in exact_group.items():
            if len(group) > 1:
                for dup in group[1:]:
                    if dup.get('id') in seen_exact_ids:
                        continue
                    seen_exact_ids.add(dup.get('id'))
                    findings.append({
                        'severity': 'error',
                        'expense_id': dup.get('id'),
                        'project_id': dup.get('project_id'),
                        'date': dup.get('date'),
                        'amount': dup.get('amount'),
                        'finding': (
                            f"Exact duplicate of expense {group[0].get('id')} "
                            f"(same project+date+amount+name)"
                        ),
                        'suggested_action': (
                            f"Delete expense {dup.get('id')} if confirmed a duplicate"
                        ),
                    })

        # Near-duplicate detection (+/-2 days, any project, same amount+name)
        def parse_date(s: str) -> Optional[datetime]:
            try:
                return datetime.strptime(s, '%Y-%m-%d')
            except (ValueError, TypeError):
                return None

        for i, e in enumerate(expenses):
            e_date = parse_date(e.get('date') or '')
            if not e_date:
                continue
            e_amount = round(float(e.get('amount') or 0), 2)
            e_name = (e.get('name') or '').strip().lower()
            for other in expenses[i + 1:]:
                if other.get('id') == e.get('id'):
                    continue
                o_date = parse_date(other.get('date') or '')
                if not o_date:
                    continue
                o_amount = round(float(other.get('amount') or 0), 2)
                o_name = (other.get('name') or '').strip().lower()
                if o_amount != e_amount or o_name != e_name:
                    continue
                delta_days = abs((o_date - e_date).days)
                # skip 0-day same-project (already handled as exact dupe)
                if delta_days == 0 and other.get('project_id') == e.get('project_id'):
                    continue
                if delta_days <= 2:
                    findings.append({
                        'severity': 'warn',
                        'expense_id': other.get('id'),
                        'project_id': other.get('project_id'),
                        'date': other.get('date'),
                        'amount': other.get('amount'),
                        'finding': (
                            f"Near-duplicate of expense {e.get('id')} "
                            f"(same amount+name, {delta_days}d apart)"
                        ),
                        'suggested_action': (
                            "Confirm both are real; delete one if duplicated"
                        ),
                    })

        # Math integrity + matter-mapping + billing hygiene per expense
        for e in expenses:
            eid = e.get('id')
            pid = e.get('project_id')
            edate = e.get('date') or ''
            amount = e.get('amount')
            price = e.get('price')
            currency = e.get('currency')

            # Track totals
            if pid is not None:
                slot = totals_by_matter.setdefault(pid, {
                    'name': project_names.get(pid, f'Project {pid}'),
                    'amount': 0.0,
                })
                try:
                    slot['amount'] += float(amount or 0)
                except (ValueError, TypeError):
                    pass

            # Math integrity: Paymo does not persist quantity, so on unit
            # expenses price == amount. Any drift is a data anomaly.
            try:
                if price is not None and amount is not None:
                    if abs(float(price) - float(amount)) > 0.01:
                        findings.append({
                            'severity': 'error',
                            'expense_id': eid,
                            'project_id': pid,
                            'date': edate,
                            'amount': amount,
                            'finding': (
                                f"price ({float(price):.2f}) does not match "
                                f"amount ({float(amount):.2f})"
                            ),
                            'suggested_action': (
                                "Delete and recreate with consistent price/amount"
                            ),
                        })
            except (ValueError, TypeError):
                pass

            try:
                if amount is not None and float(amount) <= 0:
                    findings.append({
                        'severity': 'warn',
                        'expense_id': eid,
                        'project_id': pid,
                        'date': edate,
                        'amount': amount,
                        'finding': "Amount is zero or negative",
                        'suggested_action': "Verify - most expenses should be positive",
                    })
            except (ValueError, TypeError):
                pass

            if currency and currency != 'USD':
                findings.append({
                    'severity': 'warn',
                    'expense_id': eid,
                    'project_id': pid,
                    'date': edate,
                    'amount': amount,
                    'finding': f"Non-USD currency: {currency}",
                    'suggested_action': "Confirm currency is intentional",
                })

            # Matter-mapping sanity
            if edate and pid is not None:
                window_entries = get_entries_window(edate)
                hours_by_project: Dict[int, float] = {}
                for ent in window_entries:
                    ent_pid = ent.get('project_id')
                    if ent_pid is None:
                        tid = ent.get('task_id')
                        if tid is not None:
                            ent_pid = task_to_project.get(tid)
                    if ent_pid is None:
                        continue
                    dur = ent.get('duration') or 0
                    try:
                        hours_by_project[ent_pid] = hours_by_project.get(ent_pid, 0.0) + float(dur) / 3600.0
                    except (ValueError, TypeError):
                        continue

                expense_hours = hours_by_project.get(pid, 0.0)
                if hours_by_project:
                    dominant_pid, dominant_hours = max(
                        hours_by_project.items(), key=lambda kv: kv[1]
                    )
                    if expense_hours == 0.0:
                        findings.append({
                            'severity': 'warn',
                            'expense_id': eid,
                            'project_id': pid,
                            'date': edate,
                            'amount': amount,
                            'finding': (
                                f"Possible misattribution: no hours logged on project "
                                f"{pid} in +/-1d window; dominant matter was "
                                f"'{project_names.get(dominant_pid, dominant_pid)}' "
                                f"({dominant_hours:.1f}h)"
                            ),
                            'suggested_action': (
                                f"Consider reassigning to project {dominant_pid} "
                                f"('{project_names.get(dominant_pid, dominant_pid)}')"
                            ),
                        })
                    elif dominant_pid != pid and dominant_hours > expense_hours * 2:
                        findings.append({
                            'severity': 'warn',
                            'expense_id': eid,
                            'project_id': pid,
                            'date': edate,
                            'amount': amount,
                            'finding': (
                                f"Possible misattribution: only {expense_hours:.1f}h "
                                f"on project {pid} vs {dominant_hours:.1f}h on "
                                f"'{project_names.get(dominant_pid, dominant_pid)}'"
                            ),
                            'suggested_action': (
                                f"Verify project attribution; dominant matter was "
                                f"{dominant_pid}"
                            ),
                        })

            # Billing hygiene - Paymo's real field is `invoiced` (bool);
            # `invoice_item_id` is the link.
            billable = e.get('billable')
            invoiced = e.get('invoiced')
            exp_date = parse_date(edate) if edate else None

            if billable and not invoiced and pid is not None and exp_date:
                later_invoice = False
                for inv in invoices:
                    inv_pids = inv.get('projects') or inv.get('project_ids') or []
                    if isinstance(inv_pids, list) and pid not in inv_pids and inv.get('project_id') != pid:
                        continue
                    inv_date = parse_date(inv.get('date') or '')
                    if inv_date and inv_date >= exp_date:
                        later_invoice = True
                        break
                if later_invoice:
                    findings.append({
                        'severity': 'info',
                        'expense_id': eid,
                        'project_id': pid,
                        'date': edate,
                        'amount': amount,
                        'finding': "Billable + uninvoiced but matter has a later invoice",
                        'suggested_action': "May belong on that filed invoice",
                    })

            if invoiced and not e.get('invoice_item_id'):
                findings.append({
                    'severity': 'warn',
                    'expense_id': eid,
                    'project_id': pid,
                    'date': edate,
                    'amount': amount,
                    'finding': "invoiced=true but no invoice_item_id link on the expense",
                    'suggested_action': "Verify billing state; may need re-linking",
                })

        counts = {
            'expenses': len(expenses),
            'errors': sum(1 for f in findings if f['severity'] == 'error'),
            'warnings': sum(1 for f in findings if f['severity'] == 'warn'),
            'info': sum(1 for f in findings if f['severity'] == 'info'),
        }

        # Round totals
        for pid, slot in totals_by_matter.items():
            slot['amount'] = round(slot['amount'], 2)

        return {
            'range': {'start': start_date, 'end': end_date},
            'counts': counts,
            'totals_by_matter': totals_by_matter,
            'findings': findings,
        }


def run_mcp_server():
    """Run as MCP server"""
    if not MCP_AVAILABLE:
        console.print("[red]Error: fastmcp not installed. Install with: pip install fastmcp[/red]")
        sys.exit(1)

    # Don't print to stdout - interferes with MCP JSON-RPC protocol
    mcp.run()



@cli.command()
@click.option('--status', help='Filter by status (sent, viewed, paid)')
@click.option('--last-week', is_flag=True, help='Only show invoices from last 7 days')
def list_invoices_filtered(status: Optional[str], last_week: bool):
    """List Paymo invoices with filters"""
    config = load_config()
    api_key = config.get('api_key') or click.prompt('Paymo API Key', hide_input=True)

    client = PaymoClient(api_key)

    if last_week:
        invoices = client.get_outstanding_invoices_last_week()
        console.print(f"\n[bold]Outstanding invoices from last 7 days[/bold]\n")
    else:
        invoices = client.get_invoices(status=status)

    table = Table(title="Paymo Invoices")
    table.add_column("ID", style="cyan")
    table.add_column("Number", style="white")
    table.add_column("Client", style="magenta")
    table.add_column("Amount", style="green")
    table.add_column("Date", style="yellow")
    table.add_column("Status", style="blue")

    total = 0
    for invoice in invoices:
        amount = invoice.get('total', 0)
        total += amount

        table.add_row(
            str(invoice.get('id', '')),
            invoice.get('number', ''),
            invoice.get('client_name', ''),
            f"${amount:,.2f}",
            invoice.get('date', ''),
            invoice.get('status', '')
        )

    console.print(table)
    console.print(f"\n[bold]Total: ${total:,.2f}[/bold]")
    console.print(f"[bold]Count: {len(invoices)} invoices[/bold]\n")


@cli.command()
@click.option('--invoice-number', '-n', help='Invoice number (e.g., INV-20260331-241)')
@click.option('--invoice-id', type=int, help='Specific invoice ID (deprecated, use --invoice-number)')
@click.option('--last-week', is_flag=True, help='Export for all outstanding invoices from last week')
@click.option('--output-dir', '-o', default='.', help='Output directory for exports')
def export_invoice_timesheets(invoice_number: Optional[str], invoice_id: Optional[int],
                              last_week: bool, output_dir: str):
    """Export formatted timesheets for invoice(s)"""
    config = load_config()
    api_key = config.get('api_key') or click.prompt('Paymo API Key', hide_input=True)

    client = PaymoClient(api_key)

    # Determine which invoices to export
    if invoice_number:
        inv = client.find_invoice_by_number(invoice_number)
        if not inv:
            console.print(f"[red]Error: Invoice not found: {invoice_number}[/red]")
            return
        invoices = [inv]
    elif invoice_id:
        invoices = [client.get_invoice(invoice_id)]
    elif last_week:
        invoices = client.get_outstanding_invoices_last_week()
        console.print(f"\n[bold]Found {len(invoices)} outstanding invoices from last week[/bold]\n")
    else:
        console.print("[red]Error: Must specify --invoice-number, --invoice-id, or --last-week[/red]")
        return

    if not invoices:
        console.print("[yellow]No invoices found[/yellow]")
        return

    # Export each invoice
    import os
    os.makedirs(output_dir, exist_ok=True)

    for inv in invoices:
        inv_number = inv.get('number', f"INV-{inv.get('id')}")

        console.print(f"\n[bold]Exporting: {inv_number}[/bold]")
        console.print(f"  Amount: ${inv.get('total', 0):,.2f}")

        try:
            # Export using the new formatted method
            csv_content = client.export_invoice_formatted(inv_number)

            # Save file
            filename = f"{inv_number.replace('#', '').replace('/', '-')}_timesheet.csv"
            output_path = os.path.join(output_dir, filename)

            with open(output_path, 'w') as f:
                f.write(csv_content)

            console.print(f"  [green]✓ Saved: {output_path}[/green]")

        except Exception as e:
            console.print(f"  [red]✗ Error: {e}[/red]")

    console.print(f"\n[bold green]Exported {len(invoices)} invoice timesheets[/bold green]\n")


if __name__ == '__main__':
    # Check if running as MCP server
    if len(sys.argv) > 1 and sys.argv[1] == 'mcp':
        run_mcp_server()
    else:
        cli()
